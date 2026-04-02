"""
tests/conftest.py — Shared pytest fixtures for the GAOS test suite.

Autouse fixtures that prevent real GCP calls during unit tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

# ── Regression payload schema validation ──────────────────────────────────────

_PAYLOADS_DIR = Path(__file__).parent / "payloads"
_REQUIRED_META_KEYS = {"scenario", "agent", "captured", "expected"}
_REQUIRED_ENVELOPE_KEYS = {"message", "subscription"}


def _validate_regression_payload(path: Path) -> list[str]:
    """
    Validate a single regression payload file against the required schema.

    Returns a list of error strings (empty means valid).
    """
    errors: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"Invalid JSON: {exc}"]

    # Files in payloads/<agent>/ subdirectories are regression payloads.
    # Files directly in payloads/ (legacy flat files) are skipped.
    if path.parent == _PAYLOADS_DIR:
        return []

    meta = data.get("_meta")
    if not isinstance(meta, dict):
        errors.append("Missing or non-dict '_meta' block")
    else:
        missing_meta = _REQUIRED_META_KEYS - meta.keys()
        if missing_meta:
            errors.append(f"_meta missing keys: {sorted(missing_meta)}")

        expected = meta.get("expected")
        if not isinstance(expected, dict):
            errors.append("_meta.expected must be a dict")
        else:
            route = expected.get("route")
            if not route or route == "__FILL_ME_IN__":
                errors.append(
                    "_meta.expected.route must be filled in "
                    "(replace '__FILL_ME_IN__' with the expected LangGraph node name)"
                )

    missing_env = _REQUIRED_ENVELOPE_KEYS - data.keys()
    if missing_env:
        errors.append(f"Envelope missing keys: {sorted(missing_env)}")

    msg = data.get("message", {})
    if not isinstance(msg, dict) or "data" not in msg:
        errors.append("'message.data' (base64 A2AMessage) is required")

    return errors


def pytest_collection_finish(session: pytest.Session) -> None:
    """
    Validate all regression payload files under tests/payloads/<agent>/
    at collection time so a malformed or unscrubbed payload fails loudly
    before any test runs.
    """
    invalid: list[tuple[Path, list[str]]] = []
    for json_file in sorted(_PAYLOADS_DIR.rglob("*.json")):
        errors = _validate_regression_payload(json_file)
        if errors:
            invalid.append((json_file, errors))

    if invalid:
        lines = ["", "Regression payload validation failed:"]
        for path, errs in invalid:
            rel = path.relative_to(_PAYLOADS_DIR.parent)
            lines.append(f"  {rel}:")
            for err in errs:
                lines.append(f"    - {err}")
        pytest.exit("\n".join(lines), returncode=1)


@pytest.fixture(autouse=True)
def _mock_init_sheets_client():
    """
    Prevent init_sheets_client from attempting a real gspread/auth call in
    unit tests. All sheet operation functions (get_all_records, append_row,
    delete_rows, etc.) are mocked individually per-test; this fixture ensures
    the initialization guard also never fires.
    """
    with patch("tools.google_sheets.init_sheets_client", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _mock_agents_log_cloud():
    """
    Prevent _log_cloud from making real Cloud Logging API calls during unit
    tests.  The function itself has ``except Exception: pass``, but the Google
    Cloud retry layer issues ``time.sleep()`` calls *before* the final
    exception is raised, causing tests to hang for minutes without this patch.

    Two patch targets are required:
    - ``agents._log_cloud`` — patches the canonical definition.
    - ``agents.nexus_prime.orchestrator._log_cloud`` — patches the module-local
      name created by ``from agents import _log_cloud`` in orchestrator.py.
      Without this second patch the orchestrator's local binding still points
      to the live function and Cloud Logging calls escape the mock.
    """
    with patch("agents._log_cloud"), patch("agents.nexus_prime.orchestrator._log_cloud"):
        yield


@pytest.fixture(autouse=True)
def _suppress_telemetry_writes():
    """
    Prevent @tracked tool functions from making real BigQuery API calls during
    unit tests.  Without this, every tracked tool call attempts an HTTP INSERT
    to `test-project.aos_logs.api_call_log`, receives a 401 (no credentials in
    test env), and floods the error log — producing the high "API failure rate"
    visible in monitoring.  Best-effort telemetry writes are suppressed globally
    here; individual test files that verify telemetry behaviour patch
    _write_metric directly.
    """
    with patch("tools._write_metric"):
        yield


# ── Shared model-mock helpers ─────────────────────────────────────────────────
# These centralise the patch-target strings and canonical response shape so
# that a signature change to _call_model or ModelResponse requires updating
# only this file, not the ~15 inline patch() calls scattered across test files.
#
# Usage (opt-in, not autouse):
#   def test_something(mock_nexus_model):
#       mock_nexus_model.return_value = fake_model_response("custom text")
#       ...
#
# Existing tests that use inline ``with patch(...)`` are unaffected.

# Patch-target constants — import these in test files instead of hardcoding strings.
NEXUS_MODEL_TARGET: str = "agents.nexus_prime.orchestrator._call_model"
SCOUT_MODEL_TARGET: str = "agents.scout.orchestrator._call_model"
AGENTS_MODEL_TARGET: str = "agents._call_model"
AGENTS_OLLAMA_TARGET: str = "agents._call_model_ollama"


def fake_model_response(text: str = "OK.", data: dict | None = None) -> object:
    """Return a canonical ModelResponse test double.

    Use this factory in tests instead of constructing inline MagicMock objects
    so that a shape change to ModelResponse requires updating only this file.

    Args:
        text: The simulated model text output.
        data: Optional parsed JSON payload. Defaults to an empty dict.

    Returns:
        A real ModelResponse instance with deterministic test values.
    """
    from agents import ModelResponse

    return ModelResponse(text=text, data=data or {}, cost_usd=0.001, tokens_used=100)


@pytest.fixture()
def mock_nexus_model():
    """Patch nexus_prime._call_model and yield the mock for per-test configuration.

    Yields:
        unittest.mock.MagicMock: The mock replacing _call_model. Override
        ``return_value`` or ``side_effect`` within the test as needed.
    """
    with patch(NEXUS_MODEL_TARGET, return_value=fake_model_response()) as m:
        yield m


@pytest.fixture()
def mock_scout_model():
    """Patch scout._call_model and yield the mock for per-test configuration.

    Yields:
        unittest.mock.MagicMock: The mock replacing _call_model.
    """
    with patch(SCOUT_MODEL_TARGET, return_value=fake_model_response()) as m:
        yield m


@pytest.fixture()
def mock_agents_model():
    """Patch agents._call_model (the canonical definition in agents/__init__.py).

    Yields:
        unittest.mock.MagicMock: The mock replacing _call_model.
    """
    with patch(AGENTS_MODEL_TARGET, return_value=fake_model_response()) as m:
        yield m


@pytest.fixture()
def mock_agents_ollama():
    """Patch agents._call_model_ollama (the Ollama routing path in agents/__init__.py).

    Yields:
        unittest.mock.MagicMock: The mock replacing _call_model_ollama.
    """
    with patch(AGENTS_OLLAMA_TARGET, return_value=fake_model_response()) as m:
        yield m
