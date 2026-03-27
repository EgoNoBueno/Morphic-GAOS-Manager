"""
tests/test_regression_payloads.py — Regression tests for real captured Pub/Sub messages.

Each JSON file under tests/payloads/<agent>/ is a scrubbed Pub/Sub push envelope
produced by scripts/scrub_payload.py.  These tests verify that monitor() decodes
the message correctly and route() dispatches it to the expected LangGraph node.

Adding a new regression case:
    1. Capture the raw push envelope from Cloud Logging or a live run.
    2. Run: python scripts/scrub_payload.py <raw.json> <agent> <scenario>
    3. Edit _meta.expected.route in the generated file to the correct node name.
    4. Run pytest tests/test_regression_payloads.py to confirm green.
    5. Commit both the payload file and any orchestrator fix in one commit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from agents.nexus_prime.orchestrator import monitor, route

_PAYLOADS_DIR = Path(__file__).parent / "payloads"

# ── Payload discovery ──────────────────────────────────────────────────────────


def _discover_payloads() -> list[Any]:
    """
    Find all regression payload files under tests/payloads/<agent>/*.json.

    Files directly in tests/payloads/ (legacy flat files) are skipped —
    those are used by test_google_chat.py for HTTP dispatch tests.
    """
    params = []
    for json_file in sorted(_PAYLOADS_DIR.rglob("*.json")):
        if json_file.parent == _PAYLOADS_DIR:
            continue  # flat legacy file — not a regression payload
        data = json.loads(json_file.read_text(encoding="utf-8"))
        meta = data.get("_meta", {})
        scenario = meta.get("scenario", json_file.stem)
        agent = meta.get("agent", json_file.parent.name)
        expected_route = meta.get("expected", {}).get("route", "")
        params.append(
            pytest.param(
                json_file,
                expected_route,
                id=f"{agent}/{scenario}",
            )
        )
    return params


_PAYLOAD_PARAMS = _discover_payloads()

# ── Base initial_state used for all monitor() calls ───────────────────────────

_BASE_STATE: dict = {
    "task_id": "regression-test",
    "project_id": "test-project",
    "current_objective": "",
    "sub_task_results": [],
    "parked_proposals": [],
    "error_history": [],
    "memory_context": "",
    "episodic_cache": [],
    "observation_buffer": [],
    "cost_usd": 0.0,
    "iteration_count": 0,
    "step_count": 0,
    "tokens_used": 0,
}


# ── Tests ──────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not _PAYLOAD_PARAMS, reason="No regression payloads found")
@pytest.mark.parametrize("payload_path,expected_route", _PAYLOAD_PARAMS)
def test_regression_payload_routes_correctly(
    payload_path: Path,
    expected_route: str,
) -> None:
    """
    Verify that a captured and scrubbed Pub/Sub push envelope:
    1. Decodes successfully through monitor().
    2. Produces an incoming_message with a recognisable MessageType.
    3. Routes to the expected LangGraph node via route().

    If this test fails after a code change it means the routing of a
    previously-observed real message changed.  Either fix the regression
    or update _meta.expected.route if the new behaviour is intentional.
    """
    envelope = json.loads(payload_path.read_text(encoding="utf-8"))
    # Strip the _meta block before passing to monitor() — it's test metadata only.
    raw_envelope = {k: v for k, v in envelope.items() if k != "_meta"}

    state = {**_BASE_STATE, "_raw_incoming": raw_envelope}

    with patch("agents.nexus_prime.orchestrator._log_cloud"):
        # Call monitor() with the real decode_push_message so the full
        # decode → validate → route path is exercised end-to-end.

        result_state = monitor(state)  # type: ignore[arg-type]

    assert "incoming_message" in result_state, (
        f"monitor() failed to populate incoming_message for {payload_path.name}. "
        "Check that the envelope is a valid Pub/Sub push format."
    )
    incoming_message = result_state["incoming_message"]
    assert incoming_message is not None, (
        f"monitor() set incoming_message to None for {payload_path.name}."
    )

    actual_route = route(result_state)  # type: ignore[arg-type]
    assert actual_route == expected_route, (
        f"Routing regression for '{payload_path.name}':\n"
        f"  message_type = {incoming_message.message_type.value}\n"
        f"  expected route = '{expected_route}'\n"
        f"  actual route   = '{actual_route}'\n"
        "Update _meta.expected.route in the payload file if the new behaviour is correct."
    )
