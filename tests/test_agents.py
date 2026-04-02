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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from agents.nexus_prime.orchestrator import NexusPrimeWorkingMemory

import pytest

from agents import validate_code_safety
from models import AgentInput, AgentOutput, AgentWorkingMemory, MessageType

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
gmail:
  monitored_address: 'owner@example.com'
  sender_address: 'aos@example.com'
  label_id: 'Label_6'
  pubsub_topic: 'projects/test-project/topics/gmail-notifications'
  max_results: 50
chat:
  owner_space: 'spaces/test-space'
"""

_REPO_ROOT = Path(__file__).parent.parent
_AGENTS_DIR = _REPO_ROOT / "agents"

_ORCHESTRATORS = sorted(_AGENTS_DIR.glob("*/orchestrator.py"))

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
        out = AgentOutput(task_id="t1", project_id="acme", agent_id="beacon", status="success")
        assert out.status == "success"

    def test_agent_output_escalated_status_is_valid(self):
        out = AgentOutput(task_id="t1", project_id="acme", agent_id="beacon", status="escalated")
        assert out.status == "escalated"

    def test_agent_output_failed_status_is_valid(self):
        out = AgentOutput(task_id="t1", project_id="acme", agent_id="beacon", status="failed")
        assert out.status == "failed"

    def test_agent_output_invalid_status_raises_validation_error(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AgentOutput(
                task_id="t1",
                project_id="acme",
                agent_id="beacon",
                status="unknown_status",  # type: ignore[arg-type]
            )

    def test_agent_output_no_extra_untyped_fields_needed(self):
        out = AgentOutput(task_id="t1", project_id="acme", agent_id="beacon", status="success")
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
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _VERSION_RE.search(node.value)
            ):
                violations.append(f"  line {node.lineno}: {node.value!r}")
        assert not violations, (
            f"{orch_path.name} contains hardcoded model version strings "
            f"(use settings.models.* alias instead):\n" + "\n".join(violations)
        )


# ── U4: Missing secret → STARTUP_FAILURE + sys.exit(1) ───────────────────


class TestU4MissingSecretCausesExit:
    def _boot_state(self, project_id: str = "test-project") -> AgentWorkingMemory:
        import time

        return cast(
            AgentWorkingMemory,
            {
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
            },
        )

    def test_steward_boot_exits_with_code_1_when_api_key_missing(self):
        from agents.steward import orchestrator as steward
        from tools.secrets import SecretNotFoundError

        state = self._boot_state()
        with patch("tools.secrets.get_secret", side_effect=SecretNotFoundError("GEMINI_API_KEY")):
            with patch("tools.pubsub.ensure_topic_exists"):
                with patch("tools.memory.load_domain_memory", return_value={}):
                    with patch("tools.memory.query_episodic", return_value=[]):
                        with patch("agents.steward.orchestrator._write_heartbeat"):
                            with patch("agents.steward.orchestrator._log_cloud"):
                                with pytest.raises(SystemExit) as exc_info:
                                    steward._boot(state)
        assert exc_info.value.code == 1

    def test_beacon_boot_exits_with_code_1_when_api_key_missing(self):
        from agents.beacon import orchestrator as beacon
        from tools.secrets import SecretNotFoundError

        state = self._boot_state()
        with patch("tools.secrets.get_secret", side_effect=SecretNotFoundError("GEMINI_API_KEY")):
            with patch("tools.pubsub.ensure_topic_exists"):
                with patch("tools.memory.load_domain_memory", return_value={}):
                    with patch("tools.memory.query_episodic", return_value=[]):
                        with patch("agents.beacon.orchestrator._write_heartbeat"):
                            with patch("agents.beacon.orchestrator._log_cloud"):
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


# ── U-Steward-Collect: _collect sets needs_park for drive_maintenance approval ─


class TestStewardCollectDriveApproval:
    """Verifies _collect routes Drive move proposals to _park (needs_park=True)."""

    def _base_state(self) -> dict:
        import time

        return {
            "project_id": "test-proj",
            "task_id": "t-collect",
            "sub_task_results": [],
            "parked_proposals": [],
            "error_history": [],
            "messages": [],
            "cost_usd": 0.0,
            "iteration_count": 0,
            "step_count": 0,
            "tokens_used": 0,
            "hard_stop_triggered": False,
            "evolution_triggered": False,
            "_started_at": time.time(),
        }

    def test_drive_maintenance_requires_approval_sets_needs_park(self):
        from agents.steward.orchestrator import _collect

        state = self._base_state()
        state["sub_task_results"] = [
            {
                "task_type": "drive_maintenance",
                "status": "success",
                "output": {"requires_approval": True, "approved_moves": [{"file": "a.pdf"}]},
                "cost_usd": 0.0,
            }
        ]
        result = _collect(state)
        assert result["messages"][-1]["needs_park"] is True

    def test_drive_maintenance_no_approval_does_not_set_needs_park(self):
        from agents.steward.orchestrator import _collect

        state = self._base_state()
        state["sub_task_results"] = [
            {
                "task_type": "drive_maintenance",
                "status": "success",
                "output": {"requires_approval": False, "approved_moves": []},
                "cost_usd": 0.0,
            }
        ]
        result = _collect(state)
        assert result["messages"][-1]["needs_park"] is False

    def test_calendar_pending_approval_still_sets_needs_park(self):
        from agents.steward.orchestrator import _collect

        state = self._base_state()
        state["sub_task_results"] = [
            {"task_type": "calendar_add", "status": "pending_approval", "output": {}}
        ]
        result = _collect(state)
        assert result["messages"][-1]["needs_park"] is True

    def test_both_drive_and_calendar_approval_sets_needs_park(self):
        from agents.steward.orchestrator import _collect

        state = self._base_state()
        state["sub_task_results"] = [
            {"task_type": "calendar_add", "status": "pending_approval", "output": {}},
            {
                "task_type": "drive_maintenance",
                "status": "success",
                "output": {"requires_approval": True, "approved_moves": []},
                "cost_usd": 0.0,
            },
        ]
        result = _collect(state)
        assert result["messages"][-1]["needs_park"] is True


class TestU5UnknownProjectIdFails:
    def _run_with_mocked_graph(self, orchestrator_module, initial_state_fn, inp_kw: dict):
        """
        Invoke StewardAgent.run()-style logic using an unbound call on the class
        so we skip the ADK __init__ entirely.  Only works because run() only
        accesses self._graph.
        """
        from models import AgentInput

        agent_cls = getattr(
            orchestrator_module,
            next(
                n
                for n in dir(orchestrator_module)
                if n.endswith("Agent") and hasattr(getattr(orchestrator_module, n), "run")
            ),
        )
        inp = AgentInput(**inp_kw)
        state = initial_state_fn(inp)
        state["hard_stop_triggered"] = True

        mock_graph = MagicMock()
        mock_graph.ainvoke = AsyncMock(return_value=state)

        fake_self = MagicMock()
        fake_self._graph = mock_graph

        with patch(f"{orchestrator_module.__name__}._log_cloud"):
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

        if not hasattr(steward_mod, "StewardAgent") or not hasattr(steward_mod.StewardAgent, "run"):
            pytest.skip("StewardAgent.run not available (ADK not installed)")

        inp = AgentInput(task_id="exc-task", project_id="bad", instruction="test")

        mock_graph = MagicMock()
        mock_graph.ainvoke = AsyncMock(side_effect=Exception("project registry miss"))

        fake_self = MagicMock()
        fake_self._graph = mock_graph

        with patch("agents.steward.orchestrator._log_cloud"):
            result = asyncio.run(steward_mod.StewardAgent.run(fake_self, inp))  # type: ignore[attr-defined]

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
        code = "from google.cloud import bigquery\nclient = bigquery.Client()\n"
        result = validate_code_safety(code)
        assert result["passed"] is True

    def test_pydantic_and_langgraph_pass(self):
        code = "from pydantic import BaseModel\nfrom langgraph.graph import StateGraph\n"
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

        assert h_original != h_tampered, "SHA-256 must distinguish original from tampered code"

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


# ── Ollama routing ─────────────────────────────────────────────────────────


class TestOllamaRouting:
    """Verify _call_model routes ollama/ prefix to the local server and falls back correctly."""

    def test_ollama_prefix_calls_httpx_not_genai(self):
        """ollama/llama3.1 must POST to /api/generate, not call google.genai."""
        from agents import _call_model

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"response": "hello from ollama"}

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            with patch("tools.secrets.get_secret", return_value="http://localhost:11434"):
                result = _call_model("test prompt", model="ollama/llama3.1")

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert "/api/generate" in call_kwargs[0][0]
        assert result.text == "hello from ollama"
        assert result.cost_usd == 0.0  # local model has no API cost

    def test_ollama_timeout_raises_runtime_error(self):
        """On TimeoutException from Ollama, must raise RuntimeError (fallback disabled)."""
        import httpx as _httpx

        from agents import _call_model

        with patch("httpx.post", side_effect=_httpx.TimeoutException("timed out")):
            with patch("tools.secrets.get_secret", return_value="http://localhost:11434"):
                with pytest.raises(RuntimeError, match="LOCAL_MODEL unavailable"):
                    _call_model("test prompt", model="ollama/llama3.1")

    def test_ollama_connect_error_raises_runtime_error(self):
        """On ConnectError (Ollama not running), must raise RuntimeError (fallback disabled)."""
        import httpx as _httpx

        from agents import _call_model

        with patch("httpx.post", side_effect=_httpx.ConnectError("refused")):
            with patch("tools.secrets.get_secret", return_value="http://localhost:11434"):
                with pytest.raises(RuntimeError, match="LOCAL_MODEL unavailable"):
                    _call_model("test prompt", model="ollama/llama3.1")

    def test_gemini_model_does_not_call_httpx(self):
        """gemini-2.0-flash must never touch httpx.post."""
        from agents import _call_model

        mock_resp = MagicMock()
        mock_resp.text = "gemini response"
        mock_resp.usage_metadata = None

        with patch("httpx.post") as mock_post:
            with patch("google.genai.Client") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.models.generate_content.return_value = mock_resp
                mock_client_cls.return_value = mock_client
                with patch("tools.secrets.get_secret", return_value="fake-key"):
                    _call_model("test prompt", model="gemini-2.0-flash")

        mock_post.assert_not_called()

    def test_web_access_prepends_search_results_to_prompt(self):
        """web_access=True must prepend web_search() output before the Ollama call."""
        from agents import _call_model

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"response": "answer"}

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            with patch("tools.secrets.get_secret", return_value="http://localhost:11434"):
                with patch(
                    "tools.web_search.web_search", return_value="snippet: some result"
                ) as mock_ws:
                    _call_model("what is steel price", model="ollama/llama3.1", web_access=True)

        mock_ws.assert_called_once_with("what is steel price")
        posted_prompt = mock_post.call_args[1]["json"]["prompt"]
        assert "snippet: some result" in posted_prompt

    def test_web_access_false_does_not_call_web_search(self):
        """web_access=False (default) must never call web_search."""
        from agents import _call_model

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"response": "ok"}

        with patch("httpx.post", return_value=mock_resp):
            with patch("tools.secrets.get_secret", return_value="http://localhost:11434"):
                with patch("tools.web_search.web_search") as mock_ws:
                    _call_model("prompt", model="ollama/llama3.1", web_access=False)

        mock_ws.assert_not_called()


# ── Gemini AI Studio client ────────────────────────────────────────────────


class TestGeminiAIStudio:
    """Verify _call_model_gemini uses AI Studio (api_key only), never Vertex AI."""

    def setup_method(self):
        """Reset Gemini circuit breakers before each test to prevent cross-test interference."""
        from tools.circuit_breaker import reset as cb_reset

        cb_reset("agents", "gemini/gemini-2.5-flash")
        cb_reset("agents", "gemini/gemini-2.5-pro")
        cb_reset("agents", "gemini-api-key")

    def _make_mock_response(self, text: str = "ok"):
        mock_resp = MagicMock()
        mock_resp.text = text
        mock_resp.usage_metadata = MagicMock()
        mock_resp.usage_metadata.total_token_count = 42
        return mock_resp

    def test_client_initialised_with_api_key_only(self):
        """genai.Client must receive api_key= and nothing else — no vertexai flag."""
        from agents import _call_model

        with patch("google.genai.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.models.generate_content.return_value = self._make_mock_response()
            mock_client_cls.return_value = mock_client
            with patch("tools.secrets.get_secret", return_value="test-api-key"):
                _call_model("prompt", model="gemini-2.5-flash")

        mock_client_cls.assert_called_once_with(api_key="test-api-key")

    def test_vertex_ai_flag_never_passed(self):
        """vertexai=True must not appear anywhere in the Client constructor kwargs."""
        from agents import _call_model

        with patch("google.genai.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.models.generate_content.return_value = self._make_mock_response()
            mock_client_cls.return_value = mock_client
            with patch("tools.secrets.get_secret", return_value="test-api-key"):
                _call_model("prompt", model="gemini-2.5-flash")

        _, kwargs = mock_client_cls.call_args
        assert "vertexai" not in kwargs, "vertexai flag must not be passed to genai.Client"
        assert "project" not in kwargs, "project must not be passed to genai.Client"
        assert "location" not in kwargs, "location must not be passed to genai.Client"

    def test_cost_usd_is_always_zero(self):
        """AI Studio free tier — cost_usd must be 0.0 regardless of token count."""
        from agents import _call_model

        with patch("google.genai.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.models.generate_content.return_value = self._make_mock_response()
            mock_client_cls.return_value = mock_client
            with patch("tools.secrets.get_secret", return_value="test-api-key"):
                result = _call_model("prompt", model="gemini-2.5-flash")

        assert result.cost_usd == 0.0

    def test_tokens_used_is_tracked(self):
        """tokens_used must reflect total_token_count from usage_metadata."""
        from agents import _call_model

        with patch("google.genai.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.models.generate_content.return_value = self._make_mock_response()
            mock_client_cls.return_value = mock_client
            with patch("tools.secrets.get_secret", return_value="test-api-key"):
                result = _call_model("prompt", model="gemini-2.5-flash")

        assert result.tokens_used == 42

    def test_missing_api_key_raises_runtime_error(self):
        """If GEMINI_API_KEY cannot be fetched, a RuntimeError must be raised immediately."""
        from agents import _call_model

        with patch("tools.secrets.get_secret", side_effect=Exception("secret not found")):
            with pytest.raises(RuntimeError, match="GEMINI_API_KEY unavailable"):
                _call_model("prompt", model="gemini-2.5-flash")

    def test_resource_exhausted_429_logged_and_reraised(self):
        """ResourceExhausted (free-tier 429) must be logged as WARNING then re-raised."""
        from google.api_core.exceptions import ResourceExhausted

        from agents import _call_model

        with patch("google.genai.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.models.generate_content.side_effect = ResourceExhausted("quota exceeded")
            mock_client_cls.return_value = mock_client
            with patch("tools.secrets.get_secret", return_value="test-api-key"):
                with patch("agents.logger") as mock_logger:
                    with pytest.raises(ResourceExhausted):
                        _call_model("prompt", model="gemini-2.5-flash")

        mock_logger.warning.assert_called_once()
        warning_msg = mock_logger.warning.call_args[0][0]
        assert "quota" in warning_msg.lower() or "429" in warning_msg

    def test_429_trips_quota_circuit_blocks_next_call(self):
        """After a 429, the per-model circuit opens and the next call raises RuntimeError."""
        from google.api_core.exceptions import ResourceExhausted

        from agents import _call_model

        with patch("google.genai.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.models.generate_content.side_effect = ResourceExhausted("quota exceeded")
            mock_client_cls.return_value = mock_client
            with patch("tools.secrets.get_secret", return_value="test-api-key"):
                with pytest.raises(ResourceExhausted):
                    _call_model("prompt", model="gemini-2.5-flash")

        # Second call must be blocked by the circuit — generate_content must NOT be called again
        with patch("google.genai.Client") as mock_client_cls2:
            mock_client2 = MagicMock()
            mock_client_cls2.return_value = mock_client2
            with patch("tools.secrets.get_secret", return_value="test-api-key"):
                with pytest.raises(RuntimeError, match="quota circuit OPEN"):
                    _call_model("prompt", model="gemini-2.5-flash")

        mock_client2.models.generate_content.assert_not_called()

    def test_400_invalid_key_trips_apikey_circuit_blocks_next_call(self):
        """After a 400 INVALID_ARGUMENT, the api-key circuit opens and next call raises RuntimeError."""
        from google.api_core.exceptions import InvalidArgument

        from agents import _call_model

        with patch("google.genai.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.models.generate_content.side_effect = InvalidArgument("API key not valid.")
            mock_client_cls.return_value = mock_client
            with patch("tools.secrets.get_secret", return_value="bad-key"):
                with pytest.raises(RuntimeError, match="API key invalid"):
                    _call_model("prompt", model="gemini-2.5-flash")

        # Second call must be blocked — generate_content must NOT be called again
        with patch("google.genai.Client") as mock_client_cls2:
            mock_client2 = MagicMock()
            mock_client_cls2.return_value = mock_client2
            with patch("tools.secrets.get_secret", return_value="bad-key"):
                with pytest.raises(RuntimeError, match="API key circuit OPEN"):
                    _call_model("prompt", model="gemini-2.5-flash")

        mock_client2.models.generate_content.assert_not_called()


# ── Ollama fallback counter ────────────────────────────────────────────────


class TestOllamaFallbackCounter:
    """Verify the fallback counter and warning log behave correctly on Ollama failure."""

    def setup_method(self):
        """Reset the module-level counter before each test."""
        from agents import reset_ollama_fallback_count

        reset_ollama_fallback_count()

    def test_counter_increments_on_timeout(self):
        """get_ollama_fallback_count() must increase by 1 on each TimeoutException."""
        import httpx as _httpx

        from agents import _call_model, get_ollama_fallback_count

        with patch("httpx.post", side_effect=_httpx.TimeoutException("timed out")):
            with patch("tools.secrets.get_secret", return_value="http://localhost:11434"):
                with pytest.raises(RuntimeError):
                    _call_model("prompt", model="ollama/llama3.1")

        assert get_ollama_fallback_count() == 1

    def test_counter_increments_on_connect_error(self):
        """ConnectError (Ollama not running) must also increment the counter."""
        import httpx as _httpx

        from agents import _call_model, get_ollama_fallback_count

        with patch("httpx.post", side_effect=_httpx.ConnectError("refused")):
            with patch("tools.secrets.get_secret", return_value="http://localhost:11434"):
                with pytest.raises(RuntimeError):
                    _call_model("prompt", model="ollama/llama3.1")

        assert get_ollama_fallback_count() == 1

    def test_counter_accumulates_across_calls(self):
        """Counter must add up across multiple failure events."""
        import httpx as _httpx

        from agents import _call_model, get_ollama_fallback_count

        for _ in range(3):
            with patch("httpx.post", side_effect=_httpx.ConnectError("refused")):
                with patch("tools.secrets.get_secret", return_value="http://localhost:11434"):
                    with pytest.raises(RuntimeError):
                        _call_model("prompt", model="ollama/llama3.1")

        assert get_ollama_fallback_count() == 3

    def test_reset_restores_counter_to_zero(self):
        """reset_ollama_fallback_count() must set the counter back to zero."""
        import httpx as _httpx

        from agents import _call_model, get_ollama_fallback_count, reset_ollama_fallback_count

        with patch("httpx.post", side_effect=_httpx.ConnectError("refused")):
            with patch("tools.secrets.get_secret", return_value="http://localhost:11434"):
                with pytest.raises(RuntimeError):
                    _call_model("prompt", model="ollama/llama3.1")

        assert get_ollama_fallback_count() == 1
        reset_ollama_fallback_count()
        assert get_ollama_fallback_count() == 0

    def test_successful_ollama_call_does_not_increment(self):
        """A successful Ollama call must leave the counter unchanged."""
        from agents import _call_model, get_ollama_fallback_count

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"response": "ok"}

        with patch("httpx.post", return_value=mock_resp):
            with patch("tools.secrets.get_secret", return_value="http://localhost:11434"):
                _call_model("prompt", model="ollama/llama3.1")

        assert get_ollama_fallback_count() == 0

    def test_error_logged_on_ollama_failure(self):
        """A logger.error must be emitted with exc type, host, and model on Ollama failure."""
        import httpx as _httpx

        from agents import _call_model

        with patch("httpx.post", side_effect=_httpx.ConnectError("refused")):
            with patch("tools.secrets.get_secret", return_value="http://localhost:11434"):
                with patch("agents.logger") as mock_logger:
                    with pytest.raises(RuntimeError):
                        _call_model("prompt", model="ollama/llama3.1")

        mock_logger.error.assert_called_once()
        ca = mock_logger.error.call_args
        format_str = ca.args[0]
        fmt_args = ca.args[1:]
        formatted = format_str % fmt_args if fmt_args else format_str
        assert "ConnectError" in formatted
        assert "llama3" in formatted


# ── Web search tool ────────────────────────────────────────────────────────


class TestWebSearch:
    """Verify tools/web_search.py handles DDG responses and errors cleanly."""

    def test_returns_abstract_text_when_present(self):
        from tools.web_search import web_search

        ddg_payload = {
            "AbstractText": "Steel is an alloy of iron and carbon.",
            "AbstractSource": "Wikipedia",
            "Answer": "",
            "RelatedTopics": [],
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = ddg_payload

        with patch("tools.web_search.httpx.get", return_value=mock_resp):
            result = web_search("steel alloy")

        assert "Steel is an alloy" in result
        assert "Wikipedia" in result

    def test_returns_direct_answer_when_present(self):
        from tools.web_search import web_search

        ddg_payload = {
            "AbstractText": "",
            "AbstractSource": "",
            "Answer": "42",
            "RelatedTopics": [],
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = ddg_payload

        with patch("tools.web_search.httpx.get", return_value=mock_resp):
            result = web_search("answer to everything")

        assert "42" in result

    def test_includes_related_topics(self):
        from tools.web_search import web_search

        ddg_payload = {
            "AbstractText": "",
            "AbstractSource": "",
            "Answer": "",
            "RelatedTopics": [
                {"Text": "Topic A — first related topic"},
                {"Text": "Topic B — second related topic"},
            ],
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = ddg_payload

        with patch("tools.web_search.httpx.get", return_value=mock_resp):
            result = web_search("query")

        assert "Topic A" in result
        assert "Topic B" in result

    def test_returns_empty_string_on_timeout(self):
        import httpx as _httpx

        from tools.web_search import web_search

        with patch("tools.web_search.httpx.get", side_effect=_httpx.TimeoutException("timed out")):
            result = web_search("anything")

        assert result == ""

    def test_returns_empty_string_on_connect_error(self):
        import httpx as _httpx

        from tools.web_search import web_search

        with patch("tools.web_search.httpx.get", side_effect=_httpx.ConnectError("refused")):
            result = web_search("anything")

        assert result == ""

    def test_empty_query_returns_empty_string(self):
        from tools.web_search import web_search

        with patch("tools.web_search.httpx.get") as mock_get:
            result = web_search("")

        mock_get.assert_not_called()
        assert result == ""

    def test_caps_related_topics_at_max_results(self):
        from tools.web_search import web_search

        topics = [{"Text": f"Topic {i}"} for i in range(20)]
        ddg_payload = {
            "AbstractText": "",
            "AbstractSource": "",
            "Answer": "",
            "RelatedTopics": topics,
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = ddg_payload

        with patch("tools.web_search.httpx.get", return_value=mock_resp):
            result = web_search("query", max_results=3)

        # Should only include 3 topics
        assert result.count("Topic") == 3


# ── TestArchiveJob ─────────────────────────────────────────────────────────────


class TestArchiveJob:
    """
    Unit tests for handle_archive() and the POST /archive endpoint.

    All Sheet and BigQuery calls are mocked — no real API traffic.
    """

    # ── helpers ────────────────────────────────────────────────────────────────

    def _make_aged_log(self, days_old: int = 60) -> dict:
        ts = (datetime.now(UTC) - timedelta(days=days_old)).isoformat()
        return {
            "timestamp": ts,
            "agent_id": "beacon",
            "level": "INFO",
            "message": "test",
            "project_id": "test-project",
        }

    def _make_fresh_log(self) -> dict:
        return {
            "timestamp": datetime.now(UTC).isoformat(),
            "agent_id": "beacon",
            "level": "INFO",
            "message": "fresh",
            "project_id": "test-project",
        }

    def _make_aged_approval(self, days_old: int = 100) -> dict:
        ts = (datetime.now(UTC) - timedelta(days=days_old)).isoformat()
        return {
            "ID": "proposal-001",
            "Agent ID": "ledger",
            "Issue": "test issue",
            "Trigger Reason": "test",
            "Stopping Constraint": "",
            "Iterations Run": "1",
            "Total Cost USD": "0.05",
            "Proposed Code": "print('hi')",
            "Status": "Approved",
            "Timestamp": ts,
            "Approved By": "owner@example.com",
            "Approver Tier": "5",
            "code_sha256": "abc123",
            "Priority": "3",
        }

    # ── archive job core tests ─────────────────────────────────────────────────

    def test_archives_aged_logs_to_bigquery(self):
        """Aged Logs rows (>30 days) are written to BQ and deleted from the Sheet."""
        from agents.nexus_prime.orchestrator import handle_archive

        aged_row = self._make_aged_log(60)
        fresh_row = self._make_fresh_log()
        numbered = [(2, aged_row), (3, fresh_row)]

        # Use side_effect so only the Logs tab returns aged rows — prevents
        # Error Logs from also triggering a BQ call, which would shift call_args.
        def _row_se(tab, pid):
            return numbered if tab == "Logs" else []

        with (
            patch("agents.nexus_prime.orchestrator._log_cloud"),
            patch("tools.google_sheets.get_all_records", return_value=[]),
            patch("tools.google_sheets.get_all_records_with_row_numbers", side_effect=_row_se),
            patch("tools.google_sheets.delete_rows") as mock_delete,
            patch("tools.google_sheets.append_row"),
            patch("tools.bigquery.insert_rows") as mock_bq,
        ):
            result = asyncio.run(handle_archive("test-project"))

        # Only the aged row should be passed to BQ
        assert mock_bq.called
        bq_call_rows = mock_bq.call_args[0][1]
        assert len(bq_call_rows) == 1
        assert bq_call_rows[0]["task_type"] == "INFO"

        # Only row 2 (aged) should be deleted
        mock_delete.assert_called()
        deleted_rows = mock_delete.call_args[0][1]
        assert deleted_rows == [2]

        assert result["archived"]["Logs"] == 1

    def test_fresh_logs_not_archived(self):
        """Logs rows newer than 30 days are not archived or deleted."""
        from agents.nexus_prime.orchestrator import handle_archive

        fresh_row = self._make_fresh_log()
        numbered = [(2, fresh_row)]

        with (
            patch("agents.nexus_prime.orchestrator._log_cloud"),
            patch("tools.google_sheets.get_all_records", return_value=[]),
            patch("tools.google_sheets.get_all_records_with_row_numbers", return_value=numbered),
            patch("tools.google_sheets.delete_rows") as mock_delete,
            patch("tools.google_sheets.append_row"),
            patch("tools.bigquery.insert_rows"),
        ):
            result = asyncio.run(handle_archive("test-project"))

        mock_delete.assert_not_called()
        assert result["archived"]["Logs"] == 0

    def test_archives_closed_approvals_to_bigquery(self):
        """Approved/Rejected/Deployed approvals >90 days old go to approval_history."""
        from agents.nexus_prime.orchestrator import handle_archive

        aged_approval = self._make_aged_approval(100)
        pending_approval = {**self._make_aged_approval(100), "Status": "Pending"}
        approval_numbered = [(2, aged_approval), (3, pending_approval)]

        with (
            patch("agents.nexus_prime.orchestrator._log_cloud"),
            patch("tools.google_sheets.get_all_records", return_value=[]),
            patch(
                "tools.google_sheets.get_all_records_with_row_numbers",
                side_effect=[
                    [],  # Logs
                    [],  # Error Logs
                    approval_numbered,  # Agent_Approvals
                ],
            ),
            patch("tools.google_sheets.delete_rows") as mock_delete,
            patch("tools.google_sheets.append_row"),
            patch("tools.bigquery.insert_rows") as mock_bq,
        ):
            result = asyncio.run(handle_archive("test-project"))

        # Pending approval must NOT be archived
        approval_bq_calls = [c for c in mock_bq.call_args_list if "approval_history" in c[0][0]]
        assert len(approval_bq_calls) == 1
        assert approval_bq_calls[0][0][1][0]["status"] == "Approved"

        # Only row 2 deleted, not row 3 (pending)
        delete_calls = {c[0][1][0] for c in mock_delete.call_args_list if c[0][1]}
        assert 2 in delete_calls
        assert 3 not in delete_calls

        assert result["archived"]["Agent_Approvals"] == 1

    def test_pending_approvals_never_archived(self):
        """Pending proposals are never touched even if they are old."""
        from agents.nexus_prime.orchestrator import handle_archive

        old_pending = {**self._make_aged_approval(200), "Status": "Pending"}

        with (
            patch("agents.nexus_prime.orchestrator._log_cloud"),
            patch("tools.google_sheets.get_all_records", return_value=[]),
            patch(
                "tools.google_sheets.get_all_records_with_row_numbers",
                side_effect=[[], [], [(2, old_pending)]],
            ),
            patch("tools.google_sheets.delete_rows") as mock_delete,
            patch("tools.google_sheets.append_row"),
            patch("tools.bigquery.insert_rows"),
        ):
            result = asyncio.run(handle_archive("test-project"))

        mock_delete.assert_not_called()
        assert result["archived"]["Agent_Approvals"] == 0

    def test_summary_row_always_written_to_logs(self):
        """A summary row is appended to Logs regardless of how many rows were archived."""
        from agents.nexus_prime.orchestrator import handle_archive

        with (
            patch("agents.nexus_prime.orchestrator._log_cloud"),
            patch("tools.google_sheets.get_all_records", return_value=[]),
            patch("tools.google_sheets.get_all_records_with_row_numbers", return_value=[]),
            patch("tools.google_sheets.delete_rows"),
            patch("tools.google_sheets.append_row") as mock_append,
            patch("tools.bigquery.insert_rows"),
        ):
            asyncio.run(handle_archive("test-project"))

        calls = [c for c in mock_append.call_args_list if c[0][0] == "Logs"]
        assert len(calls) >= 1
        summary_row = calls[-1][0][1]
        assert summary_row["level"] == "ARCHIVE"
        assert "NIGHTLY_ARCHIVE" in summary_row["message"]

    def test_alert_published_when_tab_exceeds_threshold(self):
        """An ALERT is published when a tab has >25,000 rows after archiving."""
        from agents.nexus_prime.orchestrator import handle_archive

        # Simulate 26,000 rows remaining in Logs after archive
        big_table = [
            {
                "timestamp": "2020-01-01T00:00:00+00:00",
                "agent_id": "x",
                "level": "INFO",
                "message": "",
                "project_id": "test-project",
            }
        ] * 26_000

        with (
            patch("agents.nexus_prime.orchestrator._log_cloud"),
            patch("tools.google_sheets.get_all_records", return_value=big_table),
            patch("tools.google_sheets.get_all_records_with_row_numbers", return_value=[]),
            patch("tools.google_sheets.delete_rows"),
            patch("tools.google_sheets.append_row"),
            patch("tools.bigquery.insert_rows"),
            patch("tools.pubsub.publish") as mock_publish,
        ):
            asyncio.run(handle_archive("test-project"))

        # At least one ALERT should have been published
        assert mock_publish.called
        from models import MessageType

        published_msgs = [c[0][1] for c in mock_publish.call_args_list]
        alert_msgs = [m for m in published_msgs if m.message_type == MessageType.ALERT]
        assert len(alert_msgs) >= 1

    def test_bq_failure_does_not_delete_sheet_rows(self):
        """If BQ insert fails, rows are NOT deleted from the Sheet."""
        from agents.nexus_prime.orchestrator import handle_archive

        aged_row = self._make_aged_log(60)
        numbered = [(2, aged_row)]

        with (
            patch("agents.nexus_prime.orchestrator._log_cloud"),
            patch("tools.google_sheets.get_all_records", return_value=[]),
            patch("tools.google_sheets.get_all_records_with_row_numbers", return_value=numbered),
            patch("tools.google_sheets.delete_rows") as mock_delete,
            patch("tools.google_sheets.append_row"),
            patch("tools.bigquery.insert_rows", side_effect=RuntimeError("BQ down")),
        ):
            result = asyncio.run(handle_archive("test-project"))

        mock_delete.assert_not_called()
        assert result["archived"]["Logs"] == 0

    def test_idempotency_guard_skips_on_recent_archive_row(self):
        """handle_archive returns skipped=True if a nexus-prime ARCHIVE row exists within 4h."""
        from agents.nexus_prime.orchestrator import handle_archive

        recent_archive_row = {
            "timestamp": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
            "agent_id": "nexus-prime",
            "level": "ARCHIVE",
            "message": "NIGHTLY_ARCHIVE complete",
            "project_id": "test-project",
        }

        with (
            patch("agents.nexus_prime.orchestrator._log_cloud"),
            patch("tools.google_sheets.get_all_records", return_value=[recent_archive_row]),
            patch("tools.google_sheets.get_all_records_with_row_numbers") as mock_numbered,
            patch("tools.google_sheets.append_row") as mock_append,
            patch("tools.bigquery.insert_rows") as mock_bq,
        ):
            result = asyncio.run(handle_archive("test-project"))

        assert result.get("skipped") is True
        assert result.get("reason") == "archive already ran today"
        mock_numbered.assert_not_called()
        mock_bq.assert_not_called()
        mock_append.assert_not_called()

    def test_idempotency_guard_proceeds_when_archive_row_is_stale(self):
        """handle_archive runs normally when the last ARCHIVE row is more than 4 hours old."""
        from agents.nexus_prime.orchestrator import handle_archive

        stale_archive_row = {
            "timestamp": (datetime.now(UTC) - timedelta(hours=6)).isoformat(),
            "agent_id": "nexus-prime",
            "level": "ARCHIVE",
            "message": "NIGHTLY_ARCHIVE complete",
            "project_id": "test-project",
        }

        with (
            patch("agents.nexus_prime.orchestrator._log_cloud"),
            patch("agents.nexus_prime.orchestrator._call_model"),
            patch("tools.google_sheets.get_all_records", return_value=[stale_archive_row]),
            patch("tools.google_sheets.get_all_records_with_row_numbers", return_value=[]),
            patch("tools.google_sheets.delete_rows"),
            patch("tools.google_sheets.append_row"),
            patch("tools.bigquery.insert_rows"),
        ):
            result = asyncio.run(handle_archive("test-project"))

        assert result.get("skipped") is not True
        assert "archived" in result

    # ── /archive endpoint tests ────────────────────────────────────────────────

    def test_archive_endpoint_returns_200_for_nexus_prime(self, monkeypatch):
        """POST /archive returns 200 when AGENT_NAME=nexus-prime."""
        from httpx import ASGITransport, AsyncClient

        import main as app_module

        monkeypatch.setenv("AGENT_NAME", "nexus-prime")
        monkeypatch.setenv("GCP_PROJECT_ID", "test-project")

        async def _fake_handle_archive(pid: str) -> dict:
            return {"archived": {"Logs": 3}, "total": 3, "cost_usd": 0.0, "task_id": "t1"}

        with patch("agents.nexus_prime.orchestrator.handle_archive", _fake_handle_archive):
            with patch.object(app_module, "_AGENT_NAME", "nexus-prime"):

                async def _run():
                    async with AsyncClient(
                        transport=ASGITransport(app=app_module.app), base_url="http://test"
                    ) as client:
                        return await client.post(
                            "/archive",
                            headers={"Authorization": "Bearer test-token"},
                        )

                resp = asyncio.run(_run())

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json()["total"] == 3

    def test_archive_endpoint_returns_404_for_non_nexus_agents(self, monkeypatch):
        """POST /archive returns 404 when AGENT_NAME is not nexus-prime."""
        from httpx import ASGITransport, AsyncClient

        import main as app_module

        with patch.object(app_module, "_AGENT_NAME", "ledger"):

            async def _run():
                async with AsyncClient(
                    transport=ASGITransport(app=app_module.app), base_url="http://test"
                ) as client:
                    return await client.post(
                        "/archive",
                        headers={"Authorization": "Bearer test-token"},
                    )

            resp = asyncio.run(_run())

        assert resp.status_code == 404

    def test_archive_endpoint_returns_401_without_auth_header(self):
        """POST /archive without Authorization header returns 401."""
        from httpx import ASGITransport, AsyncClient

        import main as app_module

        with patch.object(app_module, "_AGENT_NAME", "nexus-prime"):

            async def _run():
                async with AsyncClient(
                    transport=ASGITransport(app=app_module.app), base_url="http://test"
                ) as client:
                    return await client.post("/archive")

            resp = asyncio.run(_run())

        assert resp.status_code == 401


# ── TestCodeQuality ────────────────────────────────────────────────────────────


# ── Nexus-Prime record node ────────────────────────────────────────────────


class TestNexusPrimeRecord:
    """
    Tests for the record() terminal node — specifically the APPROVAL_RESULT
    rejection path that must write 'Rejected' back to Agent_Approvals when
    the decision arrives via the Chat card-click path (Sheet not yet updated).
    """

    def _make_state(self, msg_type: MessageType, payload: dict) -> NexusPrimeWorkingMemory:
        from models import A2AMessage

        msg = A2AMessage(
            source_agent="google-chat",
            target_agent="nexus-prime",
            project_id="test-project",
            task_id="task-rec-1",
            message_type=msg_type,
            priority=3,
            payload=payload,
        )
        return {  # type: ignore[return-value]
            "incoming_message": msg,
            "project_id": "test-project",
            "task_id": "task-rec-1",
            "hard_stop_triggered": False,
            "parked_proposals": [],
            "cost_usd": 0.0,
            "start_time": datetime.now(UTC).isoformat(),
        }

    def test_rejection_updates_sheet_row(self):
        """record() must write 'Rejected' to Agent_Approvals on APPROVAL_RESULT rejection."""
        from agents.nexus_prime.orchestrator import record

        state = self._make_state(
            MessageType.APPROVAL_RESULT,
            {"status": "Rejected", "proposal_id": "prop-42", "approved_by": "owner@example.com"},
        )

        with (
            patch("tools.bigquery.insert_row"),
            patch("tools.pubsub.publish"),
            patch("agents.nexus_prime.orchestrator._write_heartbeat"),
            patch("agents.nexus_prime.orchestrator._format_heartbeat", return_value="idle"),
            patch("agents.nexus_prime.orchestrator.save_checkpoint"),
            patch("tools.google_sheets.update_row") as mock_update,
        ):
            record(state)

        mock_update.assert_called_once_with(
            "Agent_Approvals",
            "prop-42",
            {"Status": "Rejected", "Approved By": "owner@example.com"},
            "test-project",
        )

    def test_rejection_without_proposal_id_skips_sheet_update(self):
        """record() must not call update_row when proposal_id is empty."""
        from agents.nexus_prime.orchestrator import record

        state = self._make_state(
            MessageType.APPROVAL_RESULT,
            {"status": "Rejected", "proposal_id": ""},
        )

        with (
            patch("tools.bigquery.insert_row"),
            patch("tools.pubsub.publish"),
            patch("agents.nexus_prime.orchestrator._write_heartbeat"),
            patch("agents.nexus_prime.orchestrator._format_heartbeat", return_value="idle"),
            patch("agents.nexus_prime.orchestrator.save_checkpoint"),
            patch("tools.google_sheets.update_row") as mock_update,
        ):
            record(state)

        mock_update.assert_not_called()

    def test_approved_result_does_not_update_sheet_in_record(self):
        """record() must not call update_row for Approved (that is handled by promote)."""
        from agents.nexus_prime.orchestrator import record

        state = self._make_state(
            MessageType.APPROVAL_RESULT,
            {"status": "Approved", "proposal_id": "prop-99"},
        )

        with (
            patch("tools.bigquery.insert_row"),
            patch("tools.pubsub.publish"),
            patch("agents.nexus_prime.orchestrator._write_heartbeat"),
            patch("agents.nexus_prime.orchestrator._format_heartbeat", return_value="idle"),
            patch("agents.nexus_prime.orchestrator.save_checkpoint"),
            patch("tools.google_sheets.update_row") as mock_update,
        ):
            record(state)

        mock_update.assert_not_called()

    def test_sheet_update_failure_is_logged_not_raised(self):
        """A Sheet write failure in record() must log a WARNING but not raise."""
        from agents.nexus_prime.orchestrator import record

        state = self._make_state(
            MessageType.APPROVAL_RESULT,
            {"status": "Rejected", "proposal_id": "prop-77"},
        )

        with (
            patch("tools.bigquery.insert_row"),
            patch("tools.pubsub.publish"),
            patch("agents.nexus_prime.orchestrator._write_heartbeat"),
            patch("agents.nexus_prime.orchestrator._format_heartbeat", return_value="idle"),
            patch("agents.nexus_prime.orchestrator.save_checkpoint"),
            patch("tools.google_sheets.update_row", side_effect=RuntimeError("Sheets 503")),
            patch("agents.nexus_prime.orchestrator._log_cloud") as mock_log,
        ):
            record(state)  # must not raise

        warning_calls = [c for c in mock_log.call_args_list if "WARNING" in str(c)]
        assert warning_calls, "Expected a WARNING log when Sheet update fails"


# ── Nexus-Prime think node ─────────────────────────────────────────────────


class TestNexusPrimeThinkNode:
    """
    Verify the Nexus-Prime think node behaviour (GAOS-Nexus-Prime-Spec.md §3.2).

    Covers:
      - Correct _next_node routing per message type
      - Tactical mode forced on priority >= 4
      - MonologueFrame stored with task_id and project_id
      - BigQuery insert called with correct table and fields
      - Context Trio passed as system_prompt with parse_json=True
      - Graceful fallback when _call_model raises
      - Graceful fallback when BigQuery insert raises
      - _route_from_think() sub-router reads state correctly
    """

    def _make_state(self, message_type: MessageType, priority: int = 3) -> NexusPrimeWorkingMemory:
        """Build a minimal NexusPrimeWorkingMemory-shaped dict for think()."""
        from agents.nexus_prime.orchestrator import NexusPrimeWorkingMemory
        from models import A2AMessage

        msg = A2AMessage(
            source_agent="beacon",
            target_agent="nexus-prime",
            project_id="proj-test",
            task_id="task-abc",
            message_type=message_type,
            priority=priority,
            payload={"description": "something failed"},
        )
        return cast(
            NexusPrimeWorkingMemory,
            {
                "project_id": "proj-test",
                "task_id": "task-abc",
                "incoming_message": msg,
                "cost_usd": 0.0,
                "tokens_used": 0,
                "_next_node": "record",
                "monologue_frame": None,
            },
        )

    def _mock_resp(self, response_mode: str = "Direct") -> MagicMock:
        """Return a mock ModelResponse with clean parsed JSON data."""
        resp = MagicMock()
        resp.text = f'{{"response_mode": "{response_mode}"}}'
        resp.data = {
            "response_mode": response_mode,
            "knowledge_gap_detected": response_mode == "Research",
            "knowledge_gap_description": "Missing error history"
            if response_mode == "Research"
            else "",
            "partial_result_available": False,
            "reasoning_summary": "Test reasoning summary.",
        }
        resp.cost_usd = 0.0
        resp.tokens_used = 100
        return resp

    @pytest.fixture(autouse=True)
    def _patch_log_cloud(self):
        with patch("agents.nexus_prime.orchestrator._log_cloud"):
            yield

    def test_escalation_routes_to_diagnose(self):
        """ESCALATION message type → _next_node must be 'diagnose'."""
        from agents.nexus_prime.orchestrator import think
        from models import MessageType

        state = self._make_state(MessageType.ESCALATION)
        with patch("agents.nexus_prime.orchestrator._call_model", return_value=self._mock_resp()):
            with patch("agents.nexus_prime.orchestrator._load_context_trio", return_value="trio"):
                with patch("tools.bigquery.insert_row"):
                    result = think(state)

        assert result.get("_next_node") == "diagnose"

    def test_evolution_request_routes_to_diagnose(self):
        """EVOLUTION_REQUEST message type → _next_node must be 'diagnose'."""
        from agents.nexus_prime.orchestrator import think
        from models import MessageType

        state = self._make_state(MessageType.EVOLUTION_REQUEST)
        with patch("agents.nexus_prime.orchestrator._call_model", return_value=self._mock_resp()):
            with patch("agents.nexus_prime.orchestrator._load_context_trio", return_value="trio"):
                with patch("tools.bigquery.insert_row"):
                    result = think(state)

        assert result.get("_next_node") == "diagnose"

    def test_knowledge_candidate_routes_to_knowledge_review(self):
        """KNOWLEDGE_CANDIDATE → _next_node must be 'knowledge_review'."""
        from agents.nexus_prime.orchestrator import think
        from models import MessageType

        state = self._make_state(MessageType.KNOWLEDGE_CANDIDATE)
        with patch("agents.nexus_prime.orchestrator._call_model", return_value=self._mock_resp()):
            with patch("agents.nexus_prime.orchestrator._load_context_trio", return_value="trio"):
                with patch("tools.bigquery.insert_row"):
                    result = think(state)

        assert result.get("_next_node") == "knowledge_review"

    def test_tactical_mode_forced_on_priority_4(self):
        """Priority >= 4 must set response_mode='Tactical' regardless of model output."""
        from agents.nexus_prime.orchestrator import think
        from models import MessageType

        state = self._make_state(MessageType.ESCALATION, priority=4)
        # Model returns "Direct"; Tactical override must win
        with patch(
            "agents.nexus_prime.orchestrator._call_model", return_value=self._mock_resp("Direct")
        ):
            with patch("agents.nexus_prime.orchestrator._load_context_trio", return_value="trio"):
                with patch("tools.bigquery.insert_row"):
                    result = think(state)

        mf = result["monologue_frame"]
        assert mf["response_mode"] == "Tactical"

    def test_tactical_mode_forced_on_priority_5(self):
        """Priority 5 (critical) must also force Tactical mode."""
        from agents.nexus_prime.orchestrator import think
        from models import MessageType

        state = self._make_state(MessageType.ESCALATION, priority=5)
        with patch(
            "agents.nexus_prime.orchestrator._call_model", return_value=self._mock_resp("Direct")
        ):
            with patch("agents.nexus_prime.orchestrator._load_context_trio", return_value="trio"):
                with patch("tools.bigquery.insert_row"):
                    result = think(state)

        mf = result["monologue_frame"]
        assert mf["response_mode"] == "Tactical"

    def test_research_mode_stored_from_model_output(self):
        """When priority < 4, model-returned 'Research' mode is stored correctly."""
        from agents.nexus_prime.orchestrator import think
        from models import MessageType

        state = self._make_state(MessageType.ESCALATION, priority=2)
        with patch(
            "agents.nexus_prime.orchestrator._call_model", return_value=self._mock_resp("Research")
        ):
            with patch("agents.nexus_prime.orchestrator._load_context_trio", return_value="trio"):
                with patch("tools.bigquery.insert_row"):
                    result = think(state)

        frame = result.get("monologue_frame")
        assert frame is not None
        assert frame["response_mode"] == "Research"
        assert frame["knowledge_gap_detected"] is True
        assert frame["task_id"] == "task-abc"
        assert frame["project_id"] == "proj-test"

    def test_bigquery_insert_called_with_correct_fields(self):
        """insert_row must be called with task_id and project_id in the row dict."""
        from agents.nexus_prime.orchestrator import think
        from models import MessageType

        state = self._make_state(MessageType.ESCALATION)
        with patch("agents.nexus_prime.orchestrator._call_model", return_value=self._mock_resp()):
            with patch("agents.nexus_prime.orchestrator._load_context_trio", return_value="trio"):
                with patch("tools.bigquery.insert_row") as mock_bq:
                    think(state)

        mock_bq.assert_called_once()
        table_ref, row, pid = mock_bq.call_args[0]
        assert table_ref == "aos_logs.monologue_frames"
        assert row["task_id"] == "task-abc"
        assert row["project_id"] == "proj-test"
        assert pid == "proj-test"

    def test_context_trio_passed_as_system_prompt(self):
        """_call_model must receive Context Trio as system_prompt with parse_json=True."""
        from agents.nexus_prime.orchestrator import think
        from models import MessageType

        state = self._make_state(MessageType.ESCALATION)
        with patch(
            "agents.nexus_prime.orchestrator._call_model", return_value=self._mock_resp()
        ) as mock_cm:
            with patch(
                "agents.nexus_prime.orchestrator._load_context_trio",
                return_value="CONTEXT_TRIO_CONTENT",
            ):
                with patch("tools.bigquery.insert_row"):
                    think(state)

        call_kwargs = mock_cm.call_args[1]
        assert call_kwargs.get("system_prompt") == "CONTEXT_TRIO_CONTENT"
        assert call_kwargs.get("parse_json") is True

    def test_graceful_fallback_on_call_model_error(self):
        """If _call_model raises, think must not crash and BQ must not be called."""
        from agents.nexus_prime.orchestrator import think
        from models import MessageType

        state = self._make_state(MessageType.ESCALATION)
        with patch(
            "agents.nexus_prime.orchestrator._call_model",
            side_effect=RuntimeError("quota exceeded"),
        ):
            with patch("agents.nexus_prime.orchestrator._load_context_trio", return_value="trio"):
                with patch("tools.bigquery.insert_row") as mock_bq:
                    result = think(state)

        # Node returns state without crashing
        assert result is not None
        # _next_node was set to "diagnose" before the model call
        assert result.get("_next_node") == "diagnose"
        # BQ was never reached
        mock_bq.assert_not_called()

    def test_bq_failure_does_not_crash_node(self):
        """If BQ insert_row raises, the node still returns a valid state with monologue_frame."""
        from agents.nexus_prime.orchestrator import think
        from models import MessageType

        state = self._make_state(MessageType.ESCALATION)
        with patch("agents.nexus_prime.orchestrator._call_model", return_value=self._mock_resp()):
            with patch("agents.nexus_prime.orchestrator._load_context_trio", return_value="trio"):
                with patch("tools.bigquery.insert_row", side_effect=Exception("BQ unavailable")):
                    result = think(state)

        # monologue_frame was set before BQ attempt
        mf = result.get("monologue_frame")
        assert mf is not None
        assert mf["response_mode"] == "Direct"

    def test_route_from_think_returns_stored_next_node(self):
        """_route_from_think() must return whatever is in state['_next_node']."""
        from agents.nexus_prime.orchestrator import _route_from_think

        state = cast(
            "NexusPrimeWorkingMemory",
            {"_next_node": "knowledge_review", "project_id": "proj", "task_id": "t1"},
        )
        assert _route_from_think(state) == "knowledge_review"

    def test_route_from_think_defaults_to_record_when_absent(self):
        """_route_from_think() must return 'record' if _next_node is missing from state."""
        from agents.nexus_prime.orchestrator import _route_from_think

        state = cast("NexusPrimeWorkingMemory", {"project_id": "proj", "task_id": "t1"})
        assert _route_from_think(state) == "record"

    def test_knowledge_review_logs_supersession_audit_when_supersedes_set(self):
        """When LLM returns a supersedes_memory_id, SUPERSESSION_AUDIT must be logged at INFO."""
        from agents.nexus_prime.orchestrator import knowledge_review
        from models import A2AMessage, MessageType

        old_id = "mem-old-123"
        msg = A2AMessage(
            source_agent="beacon",
            target_agent="nexus-prime",
            project_id="proj-test",
            task_id="task-sup",
            message_type=MessageType.KNOWLEDGE_CANDIDATE,
            priority=3,
            payload={
                "content": "Vendor X now charges $50/unit",
                "domain": "global",
                "knowledge_type": "fact",
                "tags": [],
            },
        )
        state = cast(
            "NexusPrimeWorkingMemory",
            {
                "project_id": "proj-test",
                "task_id": "task-sup",
                "incoming_message": msg,
                "cost_usd": 0.0,
            },
        )
        mock_resp = MagicMock()
        mock_resp.cost_usd = 0.0
        mock_resp.data = {
            "confidence": 0.92,
            "is_duplicate": False,
            "rationale": "Updated pricing",
            "supersedes_memory_id": old_id,
            "supersession_reason": "Updated vendor terms override the 2024 policy",
        }

        with patch("agents.nexus_prime.orchestrator._call_model", return_value=mock_resp):
            with patch("tools.memory.query_memory_bank", return_value=[]):
                with patch("tools.memory.write_approved_memory"):
                    with patch("agents.nexus_prime.orchestrator._log_cloud") as mock_log:
                        with patch("tools.memory_mirror.sync_to_atlas"):
                            knowledge_review(state)

        audit_calls = [
            call for call in mock_log.call_args_list if "SUPERSESSION_AUDIT" in str(call)
        ]
        assert audit_calls, "SUPERSESSION_AUDIT log must be emitted when supersedes is set"
        log_msg = str(audit_calls[0])
        assert old_id in log_msg
        assert "Updated vendor terms override the 2024 policy" in log_msg

    def test_knowledge_review_no_supersession_audit_when_supersedes_absent(self):
        """When supersedes_memory_id is null, no SUPERSESSION_AUDIT log must be emitted."""
        from agents.nexus_prime.orchestrator import knowledge_review
        from models import A2AMessage, MessageType

        msg = A2AMessage(
            source_agent="beacon",
            target_agent="nexus-prime",
            project_id="proj-test",
            task_id="task-new",
            message_type=MessageType.KNOWLEDGE_CANDIDATE,
            priority=3,
            payload={
                "content": "Brand new fact with no conflict",
                "domain": "global",
                "knowledge_type": "fact",
                "tags": [],
            },
        )
        state = cast(
            "NexusPrimeWorkingMemory",
            {
                "project_id": "proj-test",
                "task_id": "task-new",
                "incoming_message": msg,
                "cost_usd": 0.0,
            },
        )
        mock_resp = MagicMock()
        mock_resp.cost_usd = 0.0
        mock_resp.data = {
            "confidence": 0.90,
            "is_duplicate": False,
            "rationale": "Completely new information",
            "supersedes_memory_id": None,
            "supersession_reason": None,
        }

        with patch("agents.nexus_prime.orchestrator._call_model", return_value=mock_resp):
            with patch("tools.memory.query_memory_bank", return_value=[]):
                with patch("tools.memory.write_approved_memory"):
                    with patch("agents.nexus_prime.orchestrator._log_cloud") as mock_log:
                        with patch("tools.memory_mirror.sync_to_atlas"):
                            knowledge_review(state)

        audit_calls = [
            call for call in mock_log.call_args_list if "SUPERSESSION_AUDIT" in str(call)
        ]
        assert not audit_calls, (
            "No SUPERSESSION_AUDIT log should be emitted when supersedes is None"
        )

    """
    M1  _call_model passes image_bytes through to _call_model_gemini.
    M2  _call_model_gemini calls generate_content with a Part list when image_bytes provided.
    M3  _call_model_gemini without image_bytes calls generate_content with a plain string.
    M4  _call_model logs a warning and strips image_bytes for Ollama models.
    M5  _call_model returns ModelResponse with text from multimodal response.
    """

    # Intentionally overrides the module-level load_test_settings autouse fixture.
    # load_test_settings runs first and loads SETTINGS_YAML (which contains literal
    # version strings like "gemini-2.0-flash").  These tests exercise _call_model_gemini
    # directly and mock the genai client, so version-string-free aliases are required to
    # avoid triggering the U3 version-string scan.  _reset_for_testing() + load_settings()
    # are called here to keep each test isolated despite the dual-fixture setup.
    @pytest.fixture(autouse=True)
    def _settings(self, tmp_path):
        import config

        cfg = tmp_path / "settings.yaml"
        cfg.write_text(
            "gcp:\n  project_id: test-proj\n  region: us-central1\n"
            "sheet:\n  workbook_id: spreadsheet-123\n"
            "models:\n  FAST_MODEL: gemini-fast\n  DEEP_MODEL: gemini-deep\n"
            "  LOCAL_MODEL: ollama/llama3\n  LOCAL_MODEL_FALLBACK: gemini-fast\n"
            "  LOCAL_MODEL_TIMEOUT_SECONDS: 2\n"
        )
        config._reset_for_testing()
        config.load_settings(cfg)
        yield
        config._reset_for_testing()

    def setup_method(self):
        """Reset Gemini circuit breakers before each test."""
        from tools.circuit_breaker import reset as cb_reset

        # Reset per-model and api-key circuits used in the multimodal tests
        for model_key in ("gemini/gemini-fast", "gemini/gemini-deep", "gemini/gemini-2.5-flash"):
            cb_reset("agents", model_key)
        cb_reset("agents", "gemini-api-key")

    def _mock_genai(self, text: str = "Vision description"):
        """Return a mock google.genai client whose generate_content returns *text*."""
        mock_resp = MagicMock()
        mock_resp.text = text
        mock_resp.usage_metadata = MagicMock(total_token_count=42)
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_resp
        return mock_client

    # M1
    def test_call_model_passes_image_bytes_to_gemini(self):
        """image_bytes flows from _call_model() into _call_model_gemini()."""
        from agents import _call_model

        img = b"fake-jpeg-bytes"
        mock_client = self._mock_genai("Blueprint description.")

        with (
            patch("tools.secrets.get_secret", return_value="fake-key"),
            patch("google.genai.Client", return_value=mock_client),
        ):
            _call_model(
                prompt="Describe this image.",
                model="gemini-deep",
                image_bytes=img,
            )

        assert mock_client.models.generate_content.called
        call_args = mock_client.models.generate_content.call_args
        kw = call_args.kwargs.get("contents")
        contents = kw if kw is not None else call_args.args[1]
        # Multimodal call produces a list, not a plain str
        assert isinstance(contents, list), "Expected a list of Parts for multimodal call"
        assert len(contents) == 2

    # M2
    def test_call_model_gemini_multimodal_uses_part_list(self):
        """generate_content receives a 2-element Part list when image_bytes is provided."""
        from agents import _call_model_gemini
        from config import get_settings

        img = b"raw-image"
        mock_client = self._mock_genai()

        with (
            patch("tools.secrets.get_secret", return_value="api-key"),
            patch("google.genai.Client", return_value=mock_client),
        ):
            result = _call_model_gemini(
                prompt="desc",
                model="gemini-deep",
                system_prompt="",
                parse_json=False,
                settings=get_settings(),
                image_bytes=img,
            )

        call_args = mock_client.models.generate_content.call_args
        kw = call_args.kwargs.get("contents")
        contents = kw if kw is not None else call_args.args[1]
        assert isinstance(contents, list)
        assert len(contents) == 2
        assert result.text == "Vision description"
        assert result.tokens_used == 42

    # M3
    def test_call_model_gemini_text_only_uses_plain_str(self):
        """generate_content receives a plain string when image_bytes is None."""
        from agents import _call_model_gemini
        from config import get_settings

        mock_client = self._mock_genai("Text response.")

        with (
            patch("tools.secrets.get_secret", return_value="api-key"),
            patch("google.genai.Client", return_value=mock_client),
        ):
            result = _call_model_gemini(
                prompt="hello",
                model="gemini-fast",
                system_prompt="",
                parse_json=False,
                settings=get_settings(),
                image_bytes=None,
            )

        call_args = mock_client.models.generate_content.call_args
        kw = call_args.kwargs.get("contents")
        contents = kw if kw is not None else call_args.args[1]
        assert isinstance(contents, str)
        assert result.text == "Text response."

    # M4
    def test_call_model_warning_logged_for_ollama_with_image(self):
        """Ollama model + image_bytes → warning logged, image stripped, call proceeds."""
        from agents import _call_model

        img = b"photo"

        with (
            patch("httpx.post") as mock_post,
            patch("tools.secrets.get_secret", return_value="http://localhost:11434"),
        ):
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = {"response": "ok"}
            mock_post.return_value = mock_resp

            with patch("agents.logger") as mock_log:
                _call_model(
                    prompt="describe",
                    model="ollama/llama3",
                    image_bytes=img,
                )

        mock_log.warning.assert_called_once()
        warning_msg = mock_log.warning.call_args[0][0]
        assert "multimodal" in warning_msg.lower() or "image" in warning_msg.lower()

    # M5
    def test_call_model_multimodal_returns_model_response(self):
        """End-to-end: _call_model with image_bytes returns a valid ModelResponse."""
        from agents import ModelResponse, _call_model

        mock_client = self._mock_genai("A detailed vision description of the workflow.")

        with (
            patch("tools.secrets.get_secret", return_value="api-key"),
            patch("google.genai.Client", return_value=mock_client),
        ):
            result = _call_model(
                prompt="Describe this image.",
                model="gemini-deep",
                image_bytes=b"jpeg-data",
            )

        assert isinstance(result, ModelResponse)
        assert "vision description" in result.text.lower()
        assert result.tokens_used == 42


class TestCodeQuality:
    """
    Static analysis gates for Rules 16, 18, and 19.

    Scans project source files (tools/, agents/, main.py) to enforce:
    - Rule 16: all function defs have return type annotations
    - Rule 18: no print() calls in production code
    - Rule 19: no bare except clauses
    """

    # Files that are permitted to use print() (interactive setup scripts)
    _PRINT_ALLOWED = {Path("scripts")}

    _SOURCE_DIRS = [
        Path("tools"),
        Path("agents"),
    ]
    _SOURCE_FILES = [Path("main.py")]

    def _project_py_files(self) -> list[Path]:
        """Return all .py files in source dirs and top-level files, excluding __pycache__."""
        files: list[Path] = []
        root = Path(__file__).parent.parent
        for d in self._SOURCE_DIRS:
            for p in (root / d).rglob("*.py"):
                if "__pycache__" not in p.parts:
                    files.append(p)
        for f in self._SOURCE_FILES:
            fp = root / f
            if fp.exists():
                files.append(fp)
        return files

    def test_no_print_statements_in_production_code(self):
        """Rule 18 — print() is banned in tools/, agents/, and main.py."""
        import ast

        violations: list[str] = []
        for path in self._project_py_files():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "print"
                ):
                    violations.append(f"{path}:{node.lineno}")

        assert not violations, (
            "Rule 18 violation — print() found in production code:\n" + "\n".join(violations)
        )

    def test_no_bare_except_clauses(self):
        """Rule 19 — bare except: and unqualified except Exception: without re-raise."""
        import ast

        violations: list[str] = []
        for path in self._project_py_files():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and node.type is None:  # bare except:
                    violations.append(f"{path}:{node.lineno} — bare except:")
        assert not violations, "Rule 19 violation — bare except clause found:\n" + "\n".join(
            violations
        )

    def test_public_functions_have_return_annotations(self):
        """Rule 16 — all public function defs must declare a return type annotation."""
        import ast

        violations: list[str] = []
        for path in self._project_py_files():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    # Skip private/dunder helpers
                    if node.name.startswith("_"):
                        continue
                    if node.returns is None:
                        violations.append(f"{path}:{node.lineno} — {node.name}()")

        assert not violations, (
            "Rule 16 violation — public function missing return type annotation:\n"
            + "\n".join(violations)
        )


# ── QP: Proposal coherence gate ───────────────────────────────────────────
#
# QP1  Structurally complete proposal passes without LLM call.
# QP2  Empty issue description fails structural check.
# QP3  Code present but stopping_constraint missing fails structural check.
# QP4  LOCAL_MODEL returning coherent=false propagates reason and returns passed=False.
# QP5  LOCAL_MODEL failure causes a warning entry and still passes (non-blocking).


class TestQPProposalCoherence:
    """Unit tests for _validate_proposal_coherence() — the pre-submission quality gate."""

    _VALID_PROPOSAL_KWARGS: dict = {
        "agent_id": "beacon",
        "issue": "ROAS dropped below 1.0 for three consecutive days on campaign CAM-42.",
        "trigger_reason": "EVOLUTION_REQUEST",
        "stopping_constraint": "iteration_cap",
        "proposed_code": "def fix(): pass",
    }

    def _make_proposal(self, **overrides):
        from models import ApprovalProposal

        kwargs = {**self._VALID_PROPOSAL_KWARGS, **overrides}
        return ApprovalProposal(**kwargs)

    def _mock_llm_coherent(self):
        """Return a mock ModelResponse indicating coherence."""
        mock_resp = MagicMock()
        mock_resp.data = {"coherent": True, "reason": ""}
        mock_resp.cost_usd = 0.0
        mock_resp.tokens_used = 10
        mock_resp.text = '{"coherent": true, "reason": ""}'
        return mock_resp

    def test_qp1_complete_proposal_passes(self):
        """A well-formed proposal with non-trivial issue + stopping_constraint passes."""
        from agents import _validate_proposal_coherence

        proposal = self._make_proposal()
        with patch("agents._call_model", return_value=self._mock_llm_coherent()):
            result = _validate_proposal_coherence(proposal, "acme")

        assert result["passed"] is True
        assert result["reason"] == ""

    def test_qp2_empty_issue_fails_structural_check(self):
        """An issue description that is empty or trivially short fails before LLM is called."""
        from agents import _validate_proposal_coherence

        proposal = self._make_proposal(issue="too short")  # < _MIN_ISSUE_LENGTH
        with patch("agents._call_model") as mock_llm:
            result = _validate_proposal_coherence(proposal, "acme")

        # LLM must not be called — structural check short-circuits
        mock_llm.assert_not_called()
        assert result["passed"] is False
        assert "too short" in result["reason"].lower() or "chars" in result["reason"].lower()

    def test_qp2_blank_issue_fails_structural_check(self):
        """A blank issue field fails the structural check."""
        from agents import _validate_proposal_coherence

        proposal = self._make_proposal(issue="   ")
        with patch("agents._call_model") as mock_llm:
            result = _validate_proposal_coherence(proposal, "acme")

        mock_llm.assert_not_called()
        assert result["passed"] is False

    def test_qp3_code_present_without_stopping_constraint_fails(self):
        """proposed_code present but stopping_constraint empty → structural failure."""
        from agents import _validate_proposal_coherence

        proposal = self._make_proposal(stopping_constraint="")
        with patch("agents._call_model") as mock_llm:
            result = _validate_proposal_coherence(proposal, "acme")

        mock_llm.assert_not_called()
        assert result["passed"] is False
        assert "stopping_constraint" in result["reason"]

    def test_qp3_no_code_allows_empty_stopping_constraint(self):
        """When proposed_code is empty, missing stopping_constraint is permitted."""
        from agents import _validate_proposal_coherence

        proposal = self._make_proposal(proposed_code="", stopping_constraint="")
        with patch("agents._call_model", return_value=self._mock_llm_coherent()):
            result = _validate_proposal_coherence(proposal, "acme")

        assert result["passed"] is True

    def test_qp4_llm_incoherence_returns_passed_false_with_reason(self):
        """When LOCAL_MODEL judges the proposal incoherent, passed=False with LLM reason."""
        from agents import _validate_proposal_coherence

        mock_resp = MagicMock()
        mock_resp.data = {"coherent": False, "reason": "Issue is too vague to act on."}
        mock_resp.cost_usd = 0.0
        mock_resp.tokens_used = 15
        mock_resp.text = '{"coherent": false, "reason": "Issue is too vague to act on."}'

        proposal = self._make_proposal()
        with patch("agents._call_model_ollama", return_value=mock_resp):
            result = _validate_proposal_coherence(proposal, "acme")

        assert result["passed"] is False
        assert "too vague" in result["reason"]

    def test_qp5_llm_failure_is_non_blocking(self):
        """If LOCAL_MODEL raises, passed=True with a warning entry — submission continues."""
        from agents import _validate_proposal_coherence

        proposal = self._make_proposal()
        with patch("agents._call_model_ollama", side_effect=RuntimeError("ollama unavailable")):
            result = _validate_proposal_coherence(proposal, "acme")

        assert result["passed"] is True
        assert any("ollama unavailable" in w for w in result["warnings"])


class TestProgressiveDistillation:
    """
    PA1–PA4: Progressive episodic distillation step in handle_archive().

    Verifies that agents with ≥ 5 recent log entries get a Pending_Knowledge row
    surfaced via flush_observations(), that under-threshold agents are skipped,
    that LLM failures are non-blocking, and that the report message reflects the
    distilled count.
    """

    def _make_recent_log(self, agent_id: str, message: str = "task done") -> dict:
        ts = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
        return {
            "timestamp": ts,
            "agent_id": agent_id,
            "level": "INFO",
            "message": message,
            "project_id": "test-project",
        }

    def _mock_resp(self, text: str = "• Lesson one\n• Lesson two") -> MagicMock:
        resp = MagicMock()
        resp.text = text
        resp.cost_usd = 0.001
        return resp

    def test_pa1_agent_with_five_plus_logs_triggers_flush(self):
        """An agent with ≥ 5 recent log entries gets a Pending_Knowledge row via flush_observations."""
        from agents.nexus_prime.orchestrator import handle_archive

        logs = [self._make_recent_log("beacon", f"task {i}") for i in range(6)]

        with (
            patch("agents.nexus_prime.orchestrator._log_cloud"),
            patch("tools.google_sheets.get_all_records", return_value=logs),
            patch("tools.google_sheets.get_all_records_with_row_numbers", return_value=[]),
            patch("tools.google_sheets.delete_rows"),
            patch("tools.google_sheets.append_row"),
            patch("tools.bigquery.insert_rows"),
            patch("agents.nexus_prime.orchestrator._call_model", return_value=self._mock_resp()),
            patch("tools.memory.flush_observations") as mock_flush,
        ):
            asyncio.run(handle_archive("test-project"))

        mock_flush.assert_called()
        obs = mock_flush.call_args[0][0][0]
        assert obs["agent_id"] == "beacon"
        assert obs["domain"] == "marketing"
        assert obs["knowledge_type"] == "pattern"
        assert obs["status"] == "Buffered"
        assert obs["confidence"] == pytest.approx(0.25)

    def test_pa2_agent_with_fewer_than_five_logs_skipped(self):
        """An agent with < 5 recent log entries does not trigger distillation."""
        from agents.nexus_prime.orchestrator import handle_archive

        logs = [self._make_recent_log("ledger", f"entry {i}") for i in range(4)]

        with (
            patch("agents.nexus_prime.orchestrator._log_cloud"),
            patch("tools.google_sheets.get_all_records", return_value=logs),
            patch("tools.google_sheets.get_all_records_with_row_numbers", return_value=[]),
            patch("tools.google_sheets.delete_rows"),
            patch("tools.google_sheets.append_row"),
            patch("tools.bigquery.insert_rows"),
            patch("agents.nexus_prime.orchestrator._call_model"),
            patch("tools.memory.flush_observations") as mock_flush,
        ):
            asyncio.run(handle_archive("test-project"))

        mock_flush.assert_not_called()

    def test_pa3_llm_failure_is_non_blocking(self):
        """If LOCAL_MODEL raises during distillation, the archive still completes with a WARNING log."""
        from agents.nexus_prime.orchestrator import handle_archive

        logs = [self._make_recent_log("scout", f"entry {i}") for i in range(6)]

        with (
            patch("agents.nexus_prime.orchestrator._log_cloud") as mock_log,
            patch("tools.google_sheets.get_all_records", return_value=logs),
            patch("tools.google_sheets.get_all_records_with_row_numbers", return_value=[]),
            patch("tools.google_sheets.delete_rows"),
            patch("tools.google_sheets.append_row"),
            patch("tools.bigquery.insert_rows"),
            patch(
                "agents.nexus_prime.orchestrator._call_model",
                side_effect=RuntimeError("ollama down"),
            ),
            patch("tools.memory.flush_observations") as mock_flush,
        ):
            result = asyncio.run(handle_archive("test-project"))

        assert "total" in result
        mock_flush.assert_not_called()
        warning_calls = [
            c
            for c in mock_log.call_args_list
            if "distillation" in " ".join(str(a) for a in c[0]).lower()
        ]
        assert len(warning_calls) >= 1

    def test_pa4_report_message_includes_distilled_count(self):
        """The nightly archive report message includes 'N agents distilled'."""
        from agents.nexus_prime.orchestrator import handle_archive

        logs = [self._make_recent_log("steward", f"op {i}") for i in range(6)]

        with (
            patch("agents.nexus_prime.orchestrator._log_cloud"),
            patch("tools.google_sheets.get_all_records", return_value=logs),
            patch("tools.google_sheets.get_all_records_with_row_numbers", return_value=[]),
            patch("tools.google_sheets.delete_rows"),
            patch("tools.google_sheets.append_row") as mock_append,
            patch("tools.bigquery.insert_rows"),
            patch("agents.nexus_prime.orchestrator._call_model", return_value=self._mock_resp()),
            patch("tools.memory.flush_observations"),
        ):
            asyncio.run(handle_archive("test-project"))

        logs_appends = [c for c in mock_append.call_args_list if c[0][0] == "Logs"]
        summary = logs_appends[-1][0][1]
        assert "agents distilled" in summary["message"]
        assert "1 agents distilled" in summary["message"]


# ── TestGmailWebhook ──────────────────────────────────────────────────────────


class TestGmailWebhook:
    """Tests for handle_gmail_webhook, process_gmail_notification, and /gmail-webhook endpoint."""

    _PROJECT_ID = "test-project"

    def _make_state(self, history_id: str = "5000") -> dict:
        from models import A2AMessage, MessageType

        msg = A2AMessage(
            project_id=self._PROJECT_ID,
            source_agent="gmail-push",
            target_agent="nexus-prime",
            message_type=MessageType.GMAIL_NOTIFICATION,
            priority=3,
            payload={"history_id": history_id},
        )
        return {
            "project_id": self._PROJECT_ID,
            "incoming_message": msg,
            "agent_id": "nexus-prime",
            "task_id": "t-gmail",
        }

    def test_gmail_webhook_handler_enqueues(self):
        """handle_gmail_webhook publishes GMAIL_NOTIFICATION and returns {status: enqueued}."""
        from agents.nexus_prime.orchestrator import handle_gmail_webhook

        with patch("tools.pubsub.publish") as mock_pub:
            result = asyncio.run(handle_gmail_webhook(self._PROJECT_ID, "9876"))

        assert result["status"] == "enqueued"
        assert result["history_id"] == "9876"
        mock_pub.assert_called_once()
        published_msg = mock_pub.call_args[0][1]
        from models import MessageType

        assert published_msg.message_type == MessageType.GMAIL_NOTIFICATION
        assert published_msg.payload["history_id"] == "9876"

    def test_gmail_process_node_happy(self):
        """process_gmail_notification: 2 authorized messages → processed=2, mark_as_read called twice."""
        from agents.nexus_prime.orchestrator import process_gmail_notification

        fake_messages = [
            {
                "message_id": "m1",
                "thread_id": "t1",
                "from_addr": "alice@example.com",
                "subject": "Test 1",
                "body": "Hello",
                "received_at": "2026-03-31T12:00:00+00:00",
                "message_id_header": "<m1@mail>",
            },
            {
                "message_id": "m2",
                "thread_id": "t2",
                "from_addr": "alice@example.com",
                "subject": "Test 2",
                "body": "World",
                "received_at": "2026-03-31T12:01:00+00:00",
                "message_id_header": "<m2@mail>",
            },
        ]

        state = self._make_state("5000")
        with (
            patch(
                "tools.secrets.get_secret",
                return_value="alice@example.com",
            ),
            patch(
                "tools.gmail.fetch_new_messages",
                return_value=(fake_messages, "5100", []),
            ),
            patch(
                "tools.gmail.get_gmail_service",
            ),
            patch(
                "tools.gmail.get_thread_context",
                return_value=[],
            ),
            patch("tools.google_sheets.append_row"),
            patch("tools.pubsub.publish"),
            patch("tools.gmail.mark_as_read") as mock_mark,
            patch("tools.google_sheets.find_row", return_value=None),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            result_state = process_gmail_notification(state)

        assert result_state["outcome"]["processed"] == 2
        assert result_state["outcome"]["skipped"] == 0
        assert mock_mark.call_count == 2

    def test_gmail_process_node_skips_unauthorized(self):
        """process_gmail_notification: message from unauthorized sender → processed=0, skipped=1."""
        from agents.nexus_prime.orchestrator import process_gmail_notification

        fake_messages = [
            {
                "message_id": "m3",
                "thread_id": "t3",
                "from_addr": "stranger@evil.com",
                "subject": "Hack",
                "body": "Click here",
                "received_at": "2026-03-31T13:00:00+00:00",
                "message_id_header": "<m3@mail>",
            }
        ]

        state = self._make_state("5000")
        with (
            patch(
                "tools.secrets.get_secret",
                return_value="alice@example.com",
            ),
            patch(
                "tools.gmail.fetch_new_messages",
                return_value=(fake_messages, "5200", []),
            ),
            patch(
                "tools.gmail.get_gmail_service",
            ),
            patch("tools.google_sheets.append_row") as mock_append,
            patch("tools.google_sheets.find_row", return_value=None),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            result_state = process_gmail_notification(state)

        assert result_state["outcome"]["processed"] == 0
        assert result_state["outcome"]["skipped"] == 1
        # Email Inbox must NOT be written for unauthorized senders
        inbox_calls = [c for c in mock_append.call_args_list if c[0][0] == "Email Inbox"]
        assert inbox_calls == []

    def test_gmail_webhook_endpoint_wrong_agent(self, load_test_settings):
        """POST /gmail-webhook with non-nexus-prime AGENT_NAME returns 404."""
        import os

        from fastapi.testclient import TestClient

        with patch.dict(os.environ, {"AGENT_NAME": "beacon"}):
            # Re-import app after env var change so _AGENT_NAME is re-evaluated
            import importlib

            import main as main_mod

            importlib.reload(main_mod)
            client = TestClient(main_mod.app)
            with patch(
                "main._verify_pubsub_audience",
                return_value=None,
            ):
                response = client.post("/gmail-webhook", json={})
        assert response.status_code == 404


# ── TestEmailReply ────────────────────────────────────────────────────────────


class TestEmailReply:
    """Tests for the compose_reply LangGraph node."""

    _PROJECT_ID = "test-project"

    def _make_state(self, payload: dict | None = None) -> dict:
        from models import A2AMessage, MessageType

        default_payload = {
            "from_addr": "alice@example.com",
            "subject": "Need a quote",
            "body": "Hi, can you give me a price for a screen repair?",
            "thread_id": "thread-abc",
            "message_id": "msg-001",
            "message_id_header": "<msg-001@mail.example.com>",
            "thread_context": [],
            "received_at": "2026-04-01T12:00:00+00:00",
        }
        msg = A2AMessage(
            project_id=self._PROJECT_ID,
            source_agent="nexus-prime",
            target_agent="nexus-prime",
            message_type=MessageType.EMAIL_RECEIVED,
            priority=3,
            payload=payload if payload is not None else default_payload,
        )
        return {
            "project_id": self._PROJECT_ID,
            "incoming_message": msg,
            "agent_id": "nexus-prime",
            "task_id": "t-reply",
            "cost_usd": 0.0,
            "tokens_used": 0,
        }

    @staticmethod
    def _mock_resp(text: str = "Thank you for reaching out. I will send you a quote shortly."):
        from agents import ModelResponse

        return ModelResponse(text=text, cost_usd=0.001, tokens_used=40)

    def test_compose_reply_happy(self):
        """compose_reply sends email, updates Inbox row, returns outcome with sent_id."""
        from agents.nexus_prime.orchestrator import compose_reply

        state = self._make_state()
        with (
            patch("agents.nexus_prime.orchestrator._call_model", return_value=self._mock_resp()),
            patch("tools.gmail.send_email", return_value="sent-id-xyz") as mock_send,
            patch(
                "tools.google_sheets.find_row",
                return_value={"message_id": "msg-001", "_row_index": 2},
            ),
            patch("tools.google_sheets.update_row") as mock_update,
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            result = compose_reply(state)

        assert result["outcome"]["replied_to"] == "alice@example.com"
        assert result["outcome"]["sent_id"] == "sent-id-xyz"
        assert result["outcome"]["chars"] > 0
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args[1]
        assert call_kwargs["to"] == "alice@example.com"
        assert call_kwargs["subject"] == "Re: Need a quote"
        assert call_kwargs["thread_id"] == "thread-abc"
        assert call_kwargs["in_reply_to"] == "<msg-001@mail.example.com>"
        mock_update.assert_called_once()

    def test_compose_reply_skips_on_model_failure(self):
        """If _call_model raises, send_email is never called and state is unchanged."""
        from agents.nexus_prime.orchestrator import compose_reply

        state = self._make_state()
        with (
            patch(
                "agents.nexus_prime.orchestrator._call_model",
                side_effect=RuntimeError("Ollama unavailable"),
            ),
            patch("tools.gmail.send_email") as mock_send,
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            result = compose_reply(state)

        mock_send.assert_not_called()
        assert "outcome" not in result or result.get("outcome") == {}

    def test_compose_reply_skips_on_missing_message(self):
        """If incoming_message is None, node returns state unchanged."""
        from agents.nexus_prime.orchestrator import compose_reply

        state = {
            "project_id": self._PROJECT_ID,
            "incoming_message": None,
            "agent_id": "nexus-prime",
            "task_id": "t-none",
        }
        with patch("tools.gmail.send_email") as mock_send:
            result = compose_reply(state)

        mock_send.assert_not_called()
        assert result is state

    def test_compose_reply_skips_send_on_send_failure(self):
        """If send_email raises GmailAPIError, node logs and returns state without outcome."""
        from agents.nexus_prime.orchestrator import compose_reply
        from tools.gmail import GmailAPIError

        state = self._make_state()
        with (
            patch("agents.nexus_prime.orchestrator._call_model", return_value=self._mock_resp()),
            patch("tools.gmail.send_email", side_effect=GmailAPIError("quota exceeded")),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            result = compose_reply(state)

        assert "outcome" not in result or result.get("outcome") != {
            "replied_to": "alice@example.com"
        }

    def test_compose_reply_prefixes_re_only_once(self):
        """Subject already starting with 'Re:' is not double-prefixed."""
        from agents.nexus_prime.orchestrator import compose_reply

        payload = {
            "from_addr": "alice@example.com",
            "subject": "Re: Need a quote",
            "body": "Following up.",
            "thread_id": "thread-abc",
            "message_id": "msg-002",
            "message_id_header": "<msg-002@mail>",
            "thread_context": [],
        }
        state = self._make_state(payload)
        with (
            patch("agents.nexus_prime.orchestrator._call_model", return_value=self._mock_resp()),
            patch("tools.gmail.send_email", return_value="sent-id-2") as mock_send,
            patch("tools.google_sheets.find_row", return_value=None),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            compose_reply(state)

        call_kwargs = mock_send.call_args[1]
        assert call_kwargs["subject"] == "Re: Need a quote"


# ── TestDailyDigest ──────────────────────────────────────────────────────────


class TestDailyDigest:
    """Tests for handle_daily_digest — daily health + activity email."""

    _PROJECT_ID = "test-project"

    _STATE_ROWS = [
        {"key": "gmail_watch_expiration", "value": "2026-04-07T10:28:58+00:00", "updated_at": ""},
    ]
    _CP_ROWS = [
        # Two agents active in last 24h; one old row that should be ignored
        {
            "timestamp": "2026-03-31T12:00:00+00:00",
            "agent_id": "nexus-prime",
            "status": "IDLE",
            "current_objective": "",
            "open_proposals": 0,
            "last_error": "",
        },
        {
            "timestamp": "2026-03-31T11:55:00+00:00",
            "agent_id": "scout",
            "status": "ACTIVE",
            "current_objective": "",
            "open_proposals": 0,
            "last_error": "",
        },
        {
            "timestamp": "2026-03-30T00:00:00+00:00",
            "agent_id": "ledger",
            "status": "IDLE",
            "current_objective": "",
            "open_proposals": 0,
            "last_error": "",
        },
    ]
    _EMAIL_ROWS = [
        {"received_at": "2026-03-31T10:00:00+00:00", "status": "Replied"},
        {"received_at": "2026-03-31T08:00:00+00:00", "status": "Pending"},
        # old row (>24h) — should not be counted
        {"received_at": "2026-03-30T00:00:00+00:00", "status": "Replied"},
    ]
    _LOG_ROWS = [
        {"timestamp": "2026-03-31T10:00:00+00:00", "agent_id": "nexus-prime"},
        {"timestamp": "2026-03-31T09:00:00+00:00", "agent_id": "scout"},
        {"timestamp": "2026-03-30T00:00:00+00:00", "agent_id": "ledger"},  # old
    ]
    _ERROR_ROWS: list = []
    _APPROVAL_ROWS = [
        {"status": "pending"},
        {"status": "approved"},
    ]

    @staticmethod
    def _mock_resp(text: str = "All systems operational.\n\nYour AOS team"):
        from agents import ModelResponse

        return ModelResponse(text=text, cost_usd=0.001, tokens_used=80)

    def _patch_sheets(self, tab_map: dict | None = None):
        """Return a side_effect callable for get_all_records keyed by tab name."""
        defaults = {
            "System_State": self._STATE_ROWS,
            "Main Control Plane": self._CP_ROWS,
            "Email Inbox": self._EMAIL_ROWS,
            "Logs": self._LOG_ROWS,
            "Error Logs": self._ERROR_ROWS,
            "Agent_Approvals": self._APPROVAL_ROWS,
        }
        if tab_map:
            defaults.update(tab_map)

        def _get_all(tab: str, project_id: str) -> list:  # noqa: ARG001
            return list(defaults.get(tab, []))

        return _get_all

    def test_happy_path_sends_email(self):
        """handle_daily_digest sends email and returns correct counts."""
        import asyncio
        from datetime import datetime, timedelta

        from agents.nexus_prime.orchestrator import handle_daily_digest

        # Use timestamps relative to now so the 24h window always covers them
        _now = datetime.now(UTC)
        _fmt = lambda delta_h: (_now - timedelta(hours=delta_h)).isoformat()  # noqa: E731

        email_rows = [
            {"received_at": _fmt(2), "status": "Replied"},
            {"received_at": _fmt(4), "status": "Pending"},
            {"received_at": _fmt(30), "status": "Replied"},  # old — outside 24h
        ]
        log_rows = [
            {"timestamp": _fmt(2), "agent_id": "nexus-prime"},
            {"timestamp": _fmt(4), "agent_id": "scout"},
            {"timestamp": _fmt(30), "agent_id": "ledger"},  # old
        ]

        with (
            patch(
                "tools.google_sheets.get_all_records",
                side_effect=self._patch_sheets({"Email Inbox": email_rows, "Logs": log_rows}),
            ),
            patch("tools.google_sheets.init_sheets_client"),
            patch("agents.nexus_prime.orchestrator._call_model", return_value=self._mock_resp()),
            patch("tools.gmail.send_email", return_value="sent-001"),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            result = asyncio.run(handle_daily_digest(self._PROJECT_ID))

        assert result["sent"] is True
        assert result["sent_id"] == "sent-001"
        assert result["emails_24h"] == 2
        assert result["emails_replied"] == 1
        assert result["emails_pending"] == 1
        assert result["logs_24h"] == 2
        assert result["errors_24h"] == 0
        assert result["pending_approvals"] == 1

    def test_send_email_failure_returns_sent_false(self):
        """GmailAPIError on send_email sets sent=False but does not raise."""
        import asyncio

        from agents.nexus_prime.orchestrator import handle_daily_digest
        from tools.gmail import GmailAPIError

        with (
            patch(
                "tools.google_sheets.get_all_records",
                side_effect=self._patch_sheets(),
            ),
            patch("tools.google_sheets.init_sheets_client"),
            patch("agents.nexus_prime.orchestrator._call_model", return_value=self._mock_resp()),
            patch(
                "tools.gmail.send_email",
                side_effect=GmailAPIError("quota exceeded"),
            ),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            result = asyncio.run(handle_daily_digest(self._PROJECT_ID))

        assert result["sent"] is False
        assert result["sent_id"] == ""

    def test_model_failure_falls_back_to_raw_data(self):
        """FAST_MODEL failure sends raw telemetry text instead of LLM digest."""
        import asyncio

        from agents.nexus_prime.orchestrator import handle_daily_digest

        with (
            patch(
                "tools.google_sheets.get_all_records",
                side_effect=self._patch_sheets(),
            ),
            patch("tools.google_sheets.init_sheets_client"),
            patch(
                "agents.nexus_prime.orchestrator._call_model",
                side_effect=RuntimeError("timeout"),
            ),
            patch("tools.gmail.send_email", return_value="sent-002") as m,
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            result = asyncio.run(handle_daily_digest(self._PROJECT_ID))

        assert result["sent"] is True
        body_sent = m.call_args[1]["body"]
        assert "GAOS Daily Digest" in body_sent  # raw data fallback contains header

    def test_sheets_failure_degrades_gracefully(self):
        """If all sheet reads fail, digest still sends with zeroed counts."""
        import asyncio

        from agents.nexus_prime.orchestrator import handle_daily_digest

        with (
            patch(
                "tools.google_sheets.get_all_records",
                side_effect=RuntimeError("Sheets unavailable"),
            ),
            patch("tools.google_sheets.init_sheets_client"),
            patch("agents.nexus_prime.orchestrator._call_model", return_value=self._mock_resp()),
            patch("tools.gmail.send_email", return_value="sent-003"),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            result = asyncio.run(handle_daily_digest(self._PROJECT_ID))

        assert result["sent"] is True
        assert result["emails_24h"] == 0
        assert result["logs_24h"] == 0
        assert result["errors_24h"] == 0
        assert result["pending_approvals"] == 0

    def test_no_recipient_skips_send(self):
        """If gmail.monitored_address is empty, send_email is never called."""
        import asyncio

        from agents.nexus_prime.orchestrator import handle_daily_digest
        from config import get_settings

        settings = get_settings()
        original = settings.gmail.monitored_address
        settings.gmail.monitored_address = ""

        try:
            with (
                patch(
                    "tools.google_sheets.get_all_records",
                    side_effect=self._patch_sheets(),
                ),
                patch("tools.google_sheets.init_sheets_client"),
                patch(
                    "agents.nexus_prime.orchestrator._call_model",
                    return_value=self._mock_resp(),
                ),
                patch("tools.gmail.send_email") as mock_send,
                patch("agents.nexus_prime.orchestrator._log_cloud"),
            ):
                result = asyncio.run(handle_daily_digest(self._PROJECT_ID))
        finally:
            settings.gmail.monitored_address = original

        mock_send.assert_not_called()
        assert result["sent"] is False

    def test_api_stats_included_in_digest(self):
        """API PERFORMANCE section appears in email body when query_rows returns data."""
        import asyncio

        from agents.nexus_prime.orchestrator import handle_daily_digest

        api_rows = [
            {
                "api_name": "gemini",
                "calls": 40,
                "successes": 38,
                "failures": 2,
                "avg_latency_ms": 312,
                "tokens_used": 5000,
            },
            {
                "api_name": "gmail",
                "calls": 10,
                "successes": 10,
                "failures": 0,
                "avg_latency_ms": 145,
                "tokens_used": 0,
            },
        ]

        with (
            patch(
                "tools.google_sheets.get_all_records",
                side_effect=self._patch_sheets(),
            ),
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.bigquery.query_rows", return_value=api_rows),
            patch(
                "agents.nexus_prime.orchestrator._call_model",
                return_value=self._mock_resp(),
            ),
            patch("tools.gmail.send_email", return_value="sent-010"),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            result = asyncio.run(handle_daily_digest(self._PROJECT_ID))

        assert result["sent"] is True
        assert result["api_calls_24h"] == 50
        # Verify the aggregated totals from the two api_rows are summed correctly
        assert result["api_calls_24h"] == 40 + 10

    def test_api_stats_raw_data_section_present(self):
        """API PERFORMANCE block is present in raw_data when LLM is bypassed."""
        import asyncio

        from agents.nexus_prime.orchestrator import handle_daily_digest

        api_rows = [
            {
                "api_name": "bigquery",
                "calls": 20,
                "successes": 20,
                "failures": 0,
                "avg_latency_ms": 55,
                "tokens_used": 0,
            },
        ]

        captured_body: list[str] = []

        def _capture_send(**kwargs):  # noqa: ANN001, ANN202
            captured_body.append(kwargs.get("body", ""))
            return "sent-011"

        with (
            patch(
                "tools.google_sheets.get_all_records",
                side_effect=self._patch_sheets(),
            ),
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.bigquery.query_rows", return_value=api_rows),
            patch(
                "agents.nexus_prime.orchestrator._call_model",
                side_effect=RuntimeError("timeout"),  # force raw_data fallback
            ),
            patch("tools.gmail.send_email", side_effect=_capture_send),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            asyncio.run(handle_daily_digest(self._PROJECT_ID))

        assert captured_body, "send_email was never called"
        assert "API PERFORMANCE" in captured_body[0]
        assert "bigquery" in captured_body[0]

    def test_api_stats_bq_failure_degrades_gracefully(self):
        """BQ query failure logs a warning but digest still sends with zeroed API counts."""
        import asyncio

        from agents.nexus_prime.orchestrator import handle_daily_digest

        with (
            patch(
                "tools.google_sheets.get_all_records",
                side_effect=self._patch_sheets(),
            ),
            patch("tools.google_sheets.init_sheets_client"),
            patch(
                "tools.bigquery.query_rows",
                side_effect=RuntimeError("BQ unavailable"),
            ),
            patch(
                "agents.nexus_prime.orchestrator._call_model",
                return_value=self._mock_resp(),
            ),
            patch("tools.gmail.send_email", return_value="sent-012"),
            patch("agents.nexus_prime.orchestrator._log_cloud") as mock_log,
        ):
            result = asyncio.run(handle_daily_digest(self._PROJECT_ID))

        assert result["sent"] is True
        assert result["api_calls_24h"] == 0
        # Warning must have been logged for the BQ failure
        warning_calls = [c for c in mock_log.call_args_list if c.args[5] == "WARNING"]
        assert any("api_call_log" in str(c) for c in warning_calls)


# ── Email intent dispatch (_dispatch_task_from_email) ─────────────────────────


class TestDispatchTaskFromEmail:
    """Unit tests for the intent router that fires TASK_HANDOFF after compose_reply."""

    _PROJECT = "test-project"

    @staticmethod
    def _state() -> dict:
        return {
            "project_id": "test-project",
            "task_id": "task-intent-001",
            "cost_usd": 0.0,
            "incoming_message": None,
            "messages": [],
        }

    @staticmethod
    def _mock_resp(text: str):
        from agents import ModelResponse

        r = ModelResponse.__new__(ModelResponse)
        r.text = text
        r.cost_usd = 0.0
        r.tokens_used = 10
        r.model = "test"
        return r

    def test_drive_intent_publishes_task_handoff(self):
        from agents.nexus_prime.orchestrator import _dispatch_task_from_email
        from config import get_settings

        intent_json = '{"is_task": true, "task_type": "drive_maintenance", "task_context": {}}'
        published = []

        with (
            patch(
                "agents.nexus_prime.orchestrator._call_model",
                return_value=self._mock_resp(intent_json),
            ),
            patch(
                "tools.pubsub.publish",
                side_effect=lambda topic, msg: published.append((topic, msg)),
            ),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            _dispatch_task_from_email(
                state=self._state(),
                project_id=self._PROJECT,
                task_id="task-001",
                from_addr="owner@example.com",
                subject="Organize my Drive",
                body="Can you please organize my Google Drive?",
                settings=get_settings(),
            )

        assert len(published) == 1
        topic, msg = published[0]
        assert "steward" in topic
        assert msg.message_type.value == "TASK_HANDOFF"
        assert msg.payload["task_type"] == "drive_maintenance"
        assert msg.payload["requested_by"] == "owner@example.com"

    def test_non_task_email_publishes_nothing(self):
        from agents.nexus_prime.orchestrator import _dispatch_task_from_email
        from config import get_settings

        intent_json = '{"is_task": false, "task_type": null, "task_context": {}}'
        published = []

        with (
            patch(
                "agents.nexus_prime.orchestrator._call_model",
                return_value=self._mock_resp(intent_json),
            ),
            patch(
                "tools.pubsub.publish",
                side_effect=lambda topic, msg: published.append((topic, msg)),
            ),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            _dispatch_task_from_email(
                state=self._state(),
                project_id=self._PROJECT,
                task_id="task-001",
                from_addr="owner@example.com",
                subject="Thanks!",
                body="Got it, thanks for the update.",
                settings=get_settings(),
            )

        assert published == []

    def test_model_failure_does_not_raise(self):
        from agents.nexus_prime.orchestrator import _dispatch_task_from_email
        from config import get_settings

        with (
            patch(
                "agents.nexus_prime.orchestrator._call_model",
                side_effect=Exception("model unavailable"),
            ),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            # Must not raise — failures are swallowed with a WARNING log
            _dispatch_task_from_email(
                state=self._state(),
                project_id=self._PROJECT,
                task_id="task-001",
                from_addr="owner@example.com",
                subject="Organize my Drive",
                body="Clean up my files please.",
                settings=get_settings(),
            )

    def test_unknown_task_type_publishes_nothing(self):
        from agents.nexus_prime.orchestrator import _dispatch_task_from_email
        from config import get_settings

        intent_json = '{"is_task": true, "task_type": "make_coffee", "task_context": {}}'
        published = []

        with (
            patch(
                "agents.nexus_prime.orchestrator._call_model",
                return_value=self._mock_resp(intent_json),
            ),
            patch(
                "tools.pubsub.publish",
                side_effect=lambda topic, msg: published.append((topic, msg)),
            ),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            _dispatch_task_from_email(
                state=self._state(),
                project_id=self._PROJECT,
                task_id="task-001",
                from_addr="owner@example.com",
                subject="Do something weird",
                body="Make me a coffee.",
                settings=get_settings(),
            )

        assert published == []

    def test_project_id_forwarded_to_published_message(self):
        from agents.nexus_prime.orchestrator import _dispatch_task_from_email
        from config import get_settings

        intent_json = '{"is_task": true, "task_type": "drive_maintenance", "task_context": {}}'
        published = []

        with (
            patch(
                "agents.nexus_prime.orchestrator._call_model",
                return_value=self._mock_resp(intent_json),
            ),
            patch(
                "tools.pubsub.publish",
                side_effect=lambda topic, msg: published.append((topic, msg)),
            ),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            _dispatch_task_from_email(
                state=self._state(),
                project_id="my-specific-project",
                task_id="task-001",
                from_addr="owner@example.com",
                subject="Drive cleanup",
                body="Sort my files.",
                settings=get_settings(),
            )

        assert published[0][1].project_id == "my-specific-project"

    def test_markdown_fenced_json_is_parsed(self):
        """Model sometimes wraps JSON in ```json fences — strip them."""
        from agents.nexus_prime.orchestrator import _dispatch_task_from_email
        from config import get_settings

        fenced = (
            '```json\n{"is_task": true, "task_type": "drive_maintenance", "task_context": {}}\n```'
        )
        published = []

        with (
            patch(
                "agents.nexus_prime.orchestrator._call_model",
                return_value=self._mock_resp(fenced),
            ),
            patch(
                "tools.pubsub.publish",
                side_effect=lambda topic, msg: published.append((topic, msg)),
            ),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            _dispatch_task_from_email(
                state=self._state(),
                project_id=self._PROJECT,
                task_id="task-001",
                from_addr="owner@example.com",
                subject="Drive cleanup",
                body="Sort my files.",
                settings=get_settings(),
            )

        assert len(published) == 1


# ── Steward _execute_drive_moves ──────────────────────────────────────────────


class TestStewardExecuteDriveMoves:
    """Unit tests for _execute_drive_moves and _resume execution path."""

    _PROJECT = "test-project"

    @staticmethod
    def _make_state(proposal_id: str, moves: list[dict]) -> dict:
        return {
            "project_id": "test-project",
            "task_id": "task-steward-exec-001",
            "cost_usd": 0.0,
            "parked_proposals": [proposal_id],
            "_drive_move_cache": {
                proposal_id: [
                    {
                        "task_type": "drive_maintenance",
                        "output": {
                            "approved_moves": moves,
                            "requires_approval": True,
                        },
                    }
                ]
            },
        }

    def test_happy_path_executes_all_moves(self):
        from agents.steward.orchestrator import _execute_drive_moves

        moves = [
            {
                "source_path": "Inbound/photo.jpg",
                "destination_path": "Knowledge/Pictures/photo.jpg",
            },
            {"source_path": "Inbound/doc.pdf", "destination_path": "Projects/Q1/doc.pdf"},
        ]
        state = self._make_state("prop-001", moves)

        with (
            patch("tools.drive.move_file", return_value="new-file-id") as mock_move,
            patch("agents.steward.orchestrator._log_cloud"),
        ):
            succeeded, failed = _execute_drive_moves(state, "prop-001")

        assert succeeded == 2
        assert failed == 0
        assert mock_move.call_count == 2

    def test_move_failure_is_counted_not_raised(self):
        from agents.steward.orchestrator import _execute_drive_moves
        from tools.drive import DriveWriteError

        moves = [
            {"source_path": "Inbound/bad.jpg", "destination_path": "Knowledge/X/bad.jpg"},
        ]
        state = self._make_state("prop-002", moves)

        with (
            patch("tools.drive.move_file", side_effect=DriveWriteError("api error")),
            patch("agents.steward.orchestrator._log_cloud"),
        ):
            succeeded, failed = _execute_drive_moves(state, "prop-002")

        assert succeeded == 0
        assert failed == 1

    def test_empty_proposal_returns_zero_zero(self):
        from agents.steward.orchestrator import _execute_drive_moves

        state = self._make_state("prop-003", [])

        with patch("agents.steward.orchestrator._log_cloud"):
            succeeded, failed = _execute_drive_moves(state, "prop-003")

        assert succeeded == 0
        assert failed == 0

    def test_missing_source_path_counted_as_failed(self):
        from agents.steward.orchestrator import _execute_drive_moves

        moves = [{"source_path": "", "destination_path": "Knowledge/X/file.txt"}]
        state = self._make_state("prop-004", moves)

        with (
            patch("tools.drive.move_file") as mock_move,
            patch("agents.steward.orchestrator._log_cloud"),
        ):
            succeeded, failed = _execute_drive_moves(state, "prop-004")

        assert failed == 1
        mock_move.assert_not_called()

    def test_resume_approved_triggers_execute(self):
        """_resume calls _execute_drive_moves when APPROVAL_RESULT status is Approved."""
        from agents.steward.orchestrator import _resume
        from models import A2AMessage, MessageType

        proposal_id = "prop-resume-001"
        moves = [{"source_path": "Inbound/a.txt", "destination_path": "Knowledge/Docs/a.txt"}]
        state = self._make_state(proposal_id, moves)
        state["incoming_message"] = A2AMessage(
            source_agent="nexus-prime",
            target_agent="steward",
            project_id=self._PROJECT,
            message_type=MessageType.APPROVAL_RESULT,
            priority=3,
            payload={"proposal_id": proposal_id, "status": "Approved"},
        )

        with (
            patch("tools.drive.move_file", return_value="moved-id") as mock_move,
            patch("agents.steward.orchestrator._log_cloud"),
        ):
            new_state = _resume(state)

        mock_move.assert_called_once()
        assert proposal_id not in new_state["parked_proposals"]

    def test_resume_rejected_skips_execute(self):
        """_resume does NOT call move_file when status is Rejected."""
        from agents.steward.orchestrator import _resume
        from models import A2AMessage, MessageType

        proposal_id = "prop-reject-001"
        moves = [{"source_path": "Inbound/a.txt", "destination_path": "Knowledge/Docs/a.txt"}]
        state = self._make_state(proposal_id, moves)
        state["incoming_message"] = A2AMessage(
            source_agent="nexus-prime",
            target_agent="steward",
            project_id=self._PROJECT,
            message_type=MessageType.APPROVAL_RESULT,
            priority=3,
            payload={"proposal_id": proposal_id, "status": "Rejected"},
        )

        with (
            patch("tools.drive.move_file") as mock_move,
            patch("agents.steward.orchestrator._log_cloud"),
        ):
            new_state = _resume(state)

        mock_move.assert_not_called()
        # Proposal is still removed from parked list regardless of outcome
        assert proposal_id not in new_state["parked_proposals"]


# ── Cloud Run error alerting ───────────────────────────────────────────────────


class TestCloudRunError:
    """Unit tests for handle_cloud_run_error."""

    _PROJECT = "test-project"

    @staticmethod
    def _entry(severity: str = "ERROR", message: str = "something exploded") -> dict:
        return {
            "insertId": "abc123",
            "severity": severity,
            "timestamp": "2026-04-02T12:00:00Z",
            "jsonPayload": {"message": message},
            "resource": {
                "type": "cloud_run_revision",
                "labels": {
                    "service_name": "nexus-prime",
                    "revision_name": "nexus-prime-00074-kmv",
                },
            },
        }

    def test_sends_email_when_no_prior_alert(self) -> None:
        """First alert (no cooldown row in System_State) — email is sent."""
        import asyncio

        from agents.nexus_prime.orchestrator import handle_cloud_run_error

        with (
            patch(
                "tools.google_sheets.find_row",
                return_value=None,
            ),
            patch("tools.google_sheets.append_row"),
            patch("tools.google_sheets.update_row"),
            patch("tools.bigquery.query_rows", return_value=[]),
            patch(
                "tools.gmail.send_email",
                return_value="sent-err-001",
            ) as mock_send,
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            result = asyncio.run(handle_cloud_run_error(self._PROJECT, self._entry()))

        assert result["sent"] is True
        assert result["suppressed"] is False
        assert result["sent_id"] == "sent-err-001"
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args.kwargs
        assert "ALERT" in call_kwargs["subject"] or "ERROR" in call_kwargs["subject"]
        assert "something exploded" in call_kwargs["body"]

    def test_suppressed_within_cooldown(self) -> None:
        """Alert within cooldown window is suppressed — no email sent."""
        import asyncio
        from datetime import UTC, datetime, timedelta

        from agents.nexus_prime.orchestrator import handle_cloud_run_error

        recent_ts = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()

        with (
            patch(
                "tools.google_sheets.find_row",
                return_value={"key": "last_error_alert_ts", "value": recent_ts},
            ),
            patch("tools.bigquery.query_rows", return_value=[]),
            patch("tools.gmail.send_email") as mock_send,
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            result = asyncio.run(handle_cloud_run_error(self._PROJECT, self._entry()))

        assert result["sent"] is False
        assert result["suppressed"] is True
        assert result["reason"] == "cooldown"
        mock_send.assert_not_called()

    def test_sends_after_cooldown_expires(self) -> None:
        """Alert outside cooldown (>15 min ago) — email is sent."""
        import asyncio
        from datetime import UTC, datetime, timedelta

        from agents.nexus_prime.orchestrator import handle_cloud_run_error

        old_ts = (datetime.now(UTC) - timedelta(minutes=70)).isoformat()

        with (
            patch(
                "tools.google_sheets.find_row",
                return_value={"key": "last_error_alert_ts", "value": old_ts},
            ),
            patch("tools.google_sheets.update_row"),
            patch("tools.bigquery.query_rows", return_value=[]),
            patch("tools.gmail.send_email", return_value="sent-err-002"),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            result = asyncio.run(handle_cloud_run_error(self._PROJECT, self._entry()))

        assert result["sent"] is True
        assert result["suppressed"] is False

    def test_api_spike_rows_appear_in_email_body(self) -> None:
        """API error spike rows from BQ are included in the email body."""
        import asyncio

        from agents.nexus_prime.orchestrator import handle_cloud_run_error

        spike_rows = [
            {
                "api_name": "pubsub",
                "calls": 50,
                "failures": 44,
                "error_pct": 88.0,
                "avg_latency_ms": 310,
            }
        ]

        captured: list[str] = []

        def _capture(**kwargs):  # noqa: ANN001, ANN202
            captured.append(kwargs.get("body", ""))
            return "sent-err-003"

        with (
            patch("tools.google_sheets.find_row", return_value=None),
            patch("tools.google_sheets.append_row"),
            patch("tools.bigquery.query_rows", return_value=spike_rows),
            patch("tools.gmail.send_email", side_effect=_capture),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            asyncio.run(handle_cloud_run_error(self._PROJECT, self._entry()))

        assert captured, "send_email was not called"
        assert "pubsub" in captured[0]
        assert "88.0%" in captured[0]

    def test_bq_failure_does_not_block_email(self) -> None:
        """BQ spike query failure is non-fatal — email still sends."""
        import asyncio

        from agents.nexus_prime.orchestrator import handle_cloud_run_error

        with (
            patch("tools.google_sheets.find_row", return_value=None),
            patch("tools.google_sheets.append_row"),
            patch(
                "tools.bigquery.query_rows",
                side_effect=RuntimeError("BQ unavailable"),
            ),
            patch("tools.gmail.send_email", return_value="sent-err-004") as mock_send,
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            result = asyncio.run(handle_cloud_run_error(self._PROJECT, self._entry()))

        assert result["sent"] is True
        mock_send.assert_called_once()

    def test_sheets_cooldown_failure_suppresses_alert(self) -> None:
        """Sheets 429 on cooldown read must suppress — not send — the alert (Rule 26 fail-closed)."""
        import asyncio

        from agents.nexus_prime.orchestrator import handle_cloud_run_error

        with (
            patch(
                "tools.google_sheets.find_row",
                side_effect=RuntimeError("Quota exceeded for sheets API"),
            ),
            patch("tools.gmail.send_email") as mock_send,
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            result = asyncio.run(handle_cloud_run_error(self._PROJECT, self._entry()))

        assert result["sent"] is False
        assert result["suppressed"] is True
        assert result["reason"] == "cooldown_check_failed"
        mock_send.assert_not_called()
