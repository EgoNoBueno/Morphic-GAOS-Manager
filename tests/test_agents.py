"""
tests/test_agents.py — Spec §8 unit tests (U1–U5) and static analysis gate (S1–S4).

U1  Valid AgentInput produces typed AgentOutput with valid status.
U2  project_id is preserved across the initial state boundary.
U3  No literal LLM version strings in any orchestrator source file.
U4  Missing GEMINI_API_KEY causes STARTUP_FAILURE log + sys.exit(1).
U5  hard_stop_triggered=True or graph exception → status: "failed".

S1  os.system(...) in agent-generated code is blocked pre-deployment.
S2  import requests in agent-generated code is blocked.
S3  Code using only allowlisted imports passes Gate 2.
S4  SHA-256 hash detects post-submission code tampering.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents import validate_code_safety
from models import AgentInput, AgentOutput


# ── Settings fixture ───────────────────────────────────────────────────────

SETTINGS_YAML = """\
gcp:
  project_id: test-project
  region: us-central1
sheet:
  workbook_id: spreadsheet-123
models:
  LOCAL_MODEL: ollama/llama3.1
  FAST_MODEL: gemini-2.0-flash
  DEEP_MODEL: gemini-2.0-pro
  LOCAL_MODEL_FALLBACK: gemini-2.0-flash
  LOCAL_MODEL_TIMEOUT_SECONDS: 2
projects:
  default:
    sheet_id: spreadsheet-123
    drive_folder_id: folder-abc
"""

_REPO_ROOT = Path(__file__).parent.parent
_AGENTS_DIR = _REPO_ROOT / "agents"

_ORCHESTRATORS = [
    _AGENTS_DIR / "beacon" / "orchestrator.py",
    _AGENTS_DIR / "foreman" / "orchestrator.py",
    _AGENTS_DIR / "ledger" / "orchestrator.py",
    _AGENTS_DIR / "nexus_prime" / "orchestrator.py",
    _AGENTS_DIR / "pursuit" / "orchestrator.py",
    _AGENTS_DIR / "scout" / "orchestrator.py",
    _AGENTS_DIR / "steward" / "orchestrator.py",
]

# Pattern for hardcoded LLM version strings (not settings aliases)
_VERSION_RE = re.compile(r"gemini-\d|gemini-pro|llama3\.\d+|llama-\d|ollama/")


@pytest.fixture(autouse=True)
def load_test_settings(tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(SETTINGS_YAML)
    import config

    config._reset_for_testing()
    config.load_settings(cfg)
    yield
    config._reset_for_testing()


# ── U1: AgentInput → AgentOutput types ────────────────────────────────────


class TestU1AgentIO:
    def test_agent_input_validates_required_fields(self):
        inp = AgentInput(task_id="t1", project_id="acme", instruction="do work")
        assert inp.task_id == "t1"
        assert inp.project_id == "acme"
        assert inp.instruction == "do work"

    def test_agent_input_context_defaults_to_empty_dict(self):
        inp = AgentInput(task_id="t2", project_id="acme", instruction="work")
        assert inp.context == {}

    def test_agent_output_success_status_is_valid(self):
        out = AgentOutput(
            task_id="t1", project_id="acme", agent_id="beacon", status="success"
        )
        assert out.status == "success"

    def test_agent_output_escalated_status_is_valid(self):
        out = AgentOutput(
            task_id="t1", project_id="acme", agent_id="beacon", status="escalated"
        )
        assert out.status == "escalated"

    def test_agent_output_failed_status_is_valid(self):
        out = AgentOutput(
            task_id="t1", project_id="acme", agent_id="beacon", status="failed"
        )
        assert out.status == "failed"

    def test_agent_output_invalid_status_raises_validation_error(self):
        with pytest.raises(Exception):  # pydantic.ValidationError
            AgentOutput(
                task_id="t1",
                project_id="acme",
                agent_id="beacon",
                status="unknown_status",
            )

    def test_agent_output_no_extra_untyped_fields_needed(self):
        out = AgentOutput(
            task_id="t1", project_id="acme", agent_id="beacon", status="success"
        )
        # result is a typed dict, cost_usd is float — no raw Any at top level
        assert isinstance(out.result, dict)
        assert isinstance(out.cost_usd, float)


# ── U2: project_id preserved through initial state ────────────────────────


class TestU2ProjectIdPreserved:
    def test_steward_initial_state_preserves_project_id(self):
        from agents.steward.orchestrator import _initial_state

        inp = AgentInput(task_id="tid-1", project_id="acme", instruction="work")
        state = _initial_state(inp)
        assert state["project_id"] == "acme"

    def test_steward_initial_state_preserves_task_id(self):
        from agents.steward.orchestrator import _initial_state

        inp = AgentInput(task_id="unique-task-99", project_id="acme", instruction="work")
        state = _initial_state(inp)
        assert state["task_id"] == "unique-task-99"

    def test_beacon_initial_state_preserves_project_id(self):
        from agents.beacon.orchestrator import _initial_state

        inp = AgentInput(task_id="t-b", project_id="northstar", instruction="research")
        state = _initial_state(inp)
        assert state["project_id"] == "northstar"

    def test_ledger_initial_state_preserves_project_id(self):
        from agents.ledger.orchestrator import _initial_state

        inp = AgentInput(task_id="t-l", project_id="acme-finance", instruction="audit")
        state = _initial_state(inp)
        assert state["project_id"] == "acme-finance"


# ── U3: No literal LLM version strings in orchestrator source ────────────


class TestU3NoLiteralModelVersions:
    @pytest.mark.parametrize(
        "orch_path",
        _ORCHESTRATORS,
        ids=[p.parent.name for p in _ORCHESTRATORS],
    )
    def test_no_hardcoded_model_version_in_string_literals(self, orch_path):
        source = orch_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(orch_path))
        violations: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _VERSION_RE.search(node.value):
                    violations.append(
                        f"  line {node.lineno}: {node.value!r}"
                    )
        assert not violations, (
            f"{orch_path.name} contains hardcoded model version strings "
            f"(use settings.models.* alias instead):\n"
            + "\n".join(violations)
        )


# ── U4: Missing secret → STARTUP_FAILURE + sys.exit(1) ───────────────────


class TestU4MissingSecretCausesExit:
    def _boot_state(self, project_id: str = "test-project") -> dict:
        import time

        return {
            "project_id": project_id,
            "task_id": "t-boot",
            "current_objective": "Booting",
            "sub_task_results": [],
            "parked_proposals": [],
            "error_history": [],
            "memory_context": {},
            "episodic_cache": {},
            "observation_buffer": [],
            "cost_usd": 0.0,
            "iteration_count": 0,
            "step_count": 0,
            "tokens_used": 0,
            "incoming_message": None,
            "messages": [],
            "hard_stop_triggered": False,
            "evolution_triggered": False,
            "_started_at": time.time(),
        }

    def test_steward_boot_exits_with_code_1_when_api_key_missing(self):
        from tools.secrets import SecretNotFoundError
        from agents.steward import orchestrator as steward

        state = self._boot_state()
        with patch("tools.secrets.get_secret", side_effect=SecretNotFoundError("GEMINI_API_KEY")):
            with patch("tools.pubsub.ensure_topic_exists"):
                with patch("tools.memory.load_domain_memory", return_value={}):
                    with patch("tools.memory.query_episodic", return_value=[]):
                        with patch("agents._write_heartbeat"):
                            with patch("agents._log_cloud"):
                                with pytest.raises(SystemExit) as exc_info:
                                    steward._boot(state)
        assert exc_info.value.code == 1

    def test_beacon_boot_exits_with_code_1_when_api_key_missing(self):
        from tools.secrets import SecretNotFoundError
        from agents.beacon import orchestrator as beacon

        state = self._boot_state()
        with patch("tools.secrets.get_secret", side_effect=SecretNotFoundError("GEMINI_API_KEY")):
            with patch("tools.pubsub.ensure_topic_exists"):
                with patch("tools.memory.load_domain_memory", return_value={}):
                    with patch("tools.memory.query_episodic", return_value=[]):
                        with patch("agents._write_heartbeat"):
                            with patch("agents._log_cloud"):
                                with pytest.raises(SystemExit) as exc_info:
                                    beacon._boot(state)
        assert exc_info.value.code == 1


# ── U5: hard_stop_triggered → status: "failed" ────────────────────────────


def _make_fake_agent(agent_module, run_method_name="run"):
    """Return a minimal object that has only `_graph` — avoids full ADK init."""

    class _Stub:
        pass

    stub = _Stub()
    return stub


class TestU5UnknownProjectIdFails:
    def _run_with_mocked_graph(self, orchestrator_module, initial_state_fn, inp_kw: dict):
        """
        Invoke StewardAgent.run()-style logic using an unbound call on the class
        so we skip the ADK __init__ entirely.  Only works because run() only
        accesses self._graph.
        """
        from models import AgentInput

        agent_cls = getattr(orchestrator_module, next(
            n for n in dir(orchestrator_module)
            if n.endswith("Agent") and hasattr(getattr(orchestrator_module, n), "run")
        ))
        inp = AgentInput(**inp_kw)
        state = initial_state_fn(inp)
        state["hard_stop_triggered"] = True

        mock_graph = MagicMock()
        mock_graph.ainvoke = AsyncMock(return_value=state)

        fake_self = MagicMock()
        fake_self._graph = mock_graph

        with patch("agents._log_cloud"):
            result = asyncio.run(agent_cls.run(fake_self, inp))
        return result

    def test_steward_returns_failed_when_hard_stop_triggered(self):
        from agents.steward import orchestrator as mod

        if not hasattr(mod, "StewardAgent") or not hasattr(mod.StewardAgent, "run"):
            pytest.skip("StewardAgent.run not available (ADK not installed)")

        result = self._run_with_mocked_graph(
            mod,
            mod._initial_state,
            {"task_id": "t1", "project_id": "unknown-project", "instruction": "test"},
        )
        assert result.status == "failed"

    def test_steward_returns_failed_when_graph_raises_exception(self):
        from agents.steward import orchestrator as steward_mod

        if not hasattr(steward_mod, "StewardAgent") or not hasattr(
            steward_mod.StewardAgent, "run"
        ):
            pytest.skip("StewardAgent.run not available (ADK not installed)")

        inp = AgentInput(task_id="exc-task", project_id="bad", instruction="test")

        mock_graph = MagicMock()
        mock_graph.ainvoke = AsyncMock(side_effect=Exception("project registry miss"))

        fake_self = MagicMock()
        fake_self._graph = mock_graph

        with patch("agents._log_cloud"):
            result = asyncio.run(steward_mod.StewardAgent.run(fake_self, inp))

        assert result.status == "failed"


# ── S1: os.system blocked ─────────────────────────────────────────────────


class TestS1BlockedPattern:
    def test_os_system_pattern_is_blocked(self):
        # No explicit import — exercises the pattern scanner, not the import check
        code = "result = os.system('id')"
        result = validate_code_safety(code)
        assert result["passed"] is False
        assert "os.system" in result["reason"]

    def test_subprocess_run_pattern_is_blocked(self):
        code = "subprocess.run(['ls', '-la'], shell=True)"
        result = validate_code_safety(code)
        assert result["passed"] is False
        assert "subprocess.run" in result["reason"]

    def test_pickle_loads_pattern_is_blocked(self):
        code = "data = pickle.loads(untrusted_bytes)"
        result = validate_code_safety(code)
        assert result["passed"] is False
        assert "pickle.loads" in result["reason"]

    def test_eval_built_in_is_blocked(self):
        code = "result = eval('1+1')"
        result = validate_code_safety(code)
        assert result["passed"] is False
        assert "eval" in result["reason"]


# ── S2: Unapproved imports blocked ────────────────────────────────────────


class TestS2UnapprovedImport:
    def test_import_requests_is_blocked(self):
        code = "import requests\nrequests.get('http://example.com')"
        result = validate_code_safety(code)
        assert result["passed"] is False
        assert "requests" in result["reason"]

    def test_from_requests_import_is_blocked(self):
        code = "from requests import get\nget('http://example.com')"
        result = validate_code_safety(code)
        assert result["passed"] is False

    def test_import_flask_is_blocked(self):
        code = "import flask\napp = flask.Flask(__name__)"
        result = validate_code_safety(code)
        assert result["passed"] is False
        assert "flask" in result["reason"].lower()

    def test_import_boto3_is_blocked(self):
        code = "import boto3"
        result = validate_code_safety(code)
        assert result["passed"] is False


# ── S3: Allowlisted imports pass ──────────────────────────────────────────


class TestS3AllowlistedImportsPasses:
    def test_stdlib_only_code_passes(self):
        code = (
            "import json\n"
            "import hashlib\n"
            "from datetime import datetime\n"
            "result = hashlib.sha256(b'data').hexdigest()\n"
        )
        result = validate_code_safety(code)
        assert result["passed"] is True

    def test_google_cloud_imports_pass(self):
        code = (
            "from google.cloud import bigquery\n"
            "client = bigquery.Client()\n"
        )
        result = validate_code_safety(code)
        assert result["passed"] is True

    def test_pydantic_and_langgraph_pass(self):
        code = (
            "from pydantic import BaseModel\n"
            "from langgraph.graph import StateGraph\n"
        )
        result = validate_code_safety(code)
        assert result["passed"] is True

    def test_internal_tools_and_models_imports_pass(self):
        code = (
            "from tools.bigquery import insert_row\n"
            "from models import A2AMessage\n"
            "from config import get_settings\n"
        )
        result = validate_code_safety(code)
        assert result["passed"] is True

    def test_syntax_error_returns_not_passed(self):
        code = "def broken(:\n    pass"
        result = validate_code_safety(code)
        assert result["passed"] is False
        assert "SyntaxError" in result["reason"]


# ── S4: SHA-256 detects post-submission tampering ─────────────────────────


class TestS4HashMismatch:
    def test_different_code_produces_different_hash(self):
        original = "x = 1\nprint(x)\n"
        tampered = "x = 2  # attacker modified this\nprint(x)\n"

        h_original = hashlib.sha256(original.encode()).hexdigest()
        h_tampered = hashlib.sha256(tampered.encode()).hexdigest()

        assert h_original != h_tampered, (
            "SHA-256 must distinguish original from tampered code"
        )

    def test_identical_code_produces_identical_hash(self):
        code = "x = 1\nprint(x)\n"
        assert hashlib.sha256(code.encode()).hexdigest() == (
            hashlib.sha256(code.encode()).hexdigest()
        )

    def test_whitespace_change_is_detected(self):
        original = "x = 1"
        modified = "x = 1 "  # trailing space added by editor
        assert (
            hashlib.sha256(original.encode()).hexdigest()
            != hashlib.sha256(modified.encode()).hexdigest()
        )

    def test_approval_proposal_stores_code_sha256(self):
        """
        ApprovalProposal.code_sha256 can hold a SHA-256 hex digest,
        enabling Col-H tamper detection at deploy time.
        """
        from models import ApprovalProposal

        code = "result = 42"
        digest = hashlib.sha256(code.encode()).hexdigest()
        proposal = ApprovalProposal(
            agent_id="nexus-prime",
            issue="capability gap",
            trigger_reason="evolution",
            proposed_code=code,
            code_sha256=digest,
        )
        assert proposal.code_sha256 == digest
        assert len(proposal.code_sha256) == 64  # hex SHA-256 is always 64 chars
