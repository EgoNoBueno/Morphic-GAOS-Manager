"""
tests/test_skill_request.py — Tests for Phase 2.5 Step 7: SKILL_REQUEST flow.

TestHandleSkillRequestInbound (6 tests):
  SR1  Happy path: proposal parked, Sheet row written, Chat card sent.
  SR2  Missing package_name: returns state unchanged with no side-effects.
  SR3  No owner_space configured: Sheet row written, card NOT sent.
  SR4  append_row failure: graceful — state still returned, no exception.
  SR5  send_skill_import_card failure: graceful — state still returned.
  SR6  No incoming_message: returns state unchanged.

TestHandleSkillRequestResolution (5 tests):
  SR7  Approved: update_row called with Approved, SKILL_REQUEST published to agent topic.
  SR8  Rejected: update_row called with Rejected, ALERT published to agent topic.
  SR9  Missing proposal_id: update_row not called, publish still attempted.
  SR10 publish failure on resolution: graceful — no exception propagated.
  SR11 Rejected removes proposal_id from parked_proposals.

TestSkillRequestRouting (1 test):
  SR12 route() returns "handle_skill_request" for SKILL_REQUEST message type.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ── Settings fixture ──────────────────────────────────────────────────────────

_SETTINGS_YAML_WITH_CHAT = """\
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
chat:
  owner_space: spaces/OWNER_SPACE_XYZ
"""

_SETTINGS_YAML_NO_CHAT = """\
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
chat:
  owner_space: ""
"""


def _make_settings(tmp_path, yaml_text: str = _SETTINGS_YAML_WITH_CHAT) -> None:
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml_text)
    import config
    config._reset_for_testing()
    config.load_settings(cfg)


@pytest.fixture(autouse=True)
def reset_settings():
    yield
    import config
    config._reset_for_testing()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_a2a(msg_type, payload=None, source_agent="scout"):
    from models import A2AMessage
    return A2AMessage(
        source_agent=source_agent,
        target_agent="nexus-prime",
        project_id="test-project",
        task_id="task-sr-001",
        message_type=msg_type,
        priority=3,
        payload=payload or {},
    )


def _base_state(parked=None, extra=None) -> dict:
    state: dict = {
        "task_id": "task-sr-001",
        "project_id": "test-project",
        "cost_usd": 0.0,
        "tokens_used": 0,
        "messages": [],
        "parked_proposals": list(parked) if parked else [],
    }
    if extra:
        state.update(extra)
    return state


# ── TestHandleSkillRequestInbound ─────────────────────────────────────────────


class TestHandleSkillRequestInbound:
    """Tests for the inbound path (no 'status' in payload)."""

    def _run(
        self,
        tmp_path,
        yaml_text=_SETTINGS_YAML_WITH_CHAT,
        package_name="requests",
        agent_id="scout",
        reason="ModuleNotFoundError",
        pypi_url="https://pypi.org/project/requests/",
        sheet_raises=None,
        card_raises=None,
    ):
        _make_settings(tmp_path, yaml_text)
        from models import MessageType
        msg = _make_a2a(
            MessageType.SKILL_REQUEST,
            payload={
                "package_name": package_name,
                "agent_id": agent_id,
                "reason": reason,
                "pypi_url": pypi_url,
            },
            source_agent=agent_id,
        )
        state = _base_state()
        state["incoming_message"] = msg

        mock_append = MagicMock() if not sheet_raises else MagicMock(side_effect=sheet_raises)
        mock_card = MagicMock() if not card_raises else MagicMock(side_effect=card_raises)

        with (
            patch("tools.google_sheets.append_row", mock_append),
            patch("tools.google_chat.send_skill_import_card", mock_card),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            from agents.nexus_prime.orchestrator import handle_skill_request
            result = handle_skill_request(state)
        return result, mock_append, mock_card

    # SR1
    def test_happy_path_parks_proposal_writes_sheet_sends_card(self, tmp_path):
        result, mock_append, mock_card = self._run(tmp_path)

        # Proposal parked in state
        assert len(result["parked_proposals"]) == 1

        # Sheet row written with correct fields
        mock_append.assert_called_once()
        tab, row, pid = mock_append.call_args.args
        assert tab == "Agent_Approvals"
        assert row["Agent ID"] == "scout"
        assert row["Status"] == "Pending"
        assert "requests" in row["Issue"]
        assert pid == "test-project"

        # Chat card sent to owner space
        mock_card.assert_called_once()
        card_kwargs = mock_card.call_args.kwargs
        assert card_kwargs["space_name"] == "spaces/OWNER_SPACE_XYZ"
        assert card_kwargs["package_name"] == "requests"
        assert card_kwargs["agent_id"] == "scout"
        assert card_kwargs["proposal_id"] == result["parked_proposals"][0]

    # SR2
    def test_missing_package_name_returns_state_unchanged(self, tmp_path):
        _make_settings(tmp_path)
        from models import MessageType
        msg = _make_a2a(
            MessageType.SKILL_REQUEST,
            payload={"agent_id": "scout", "reason": "no package"},
        )
        state = _base_state()
        state["incoming_message"] = msg

        with (
            patch("tools.google_sheets.append_row") as mock_append,
            patch("tools.google_chat.send_skill_import_card") as mock_card,
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            from agents.nexus_prime.orchestrator import handle_skill_request
            result = handle_skill_request(state)

        assert result["parked_proposals"] == []
        mock_append.assert_not_called()
        mock_card.assert_not_called()

    # SR3
    def test_no_owner_space_writes_sheet_but_skips_card(self, tmp_path):
        result, mock_append, mock_card = self._run(
            tmp_path, yaml_text=_SETTINGS_YAML_NO_CHAT
        )
        mock_append.assert_called_once()
        mock_card.assert_not_called()
        assert len(result["parked_proposals"]) == 1

    # SR4
    def test_append_row_failure_is_graceful(self, tmp_path):
        result, mock_append, mock_card = self._run(
            tmp_path, sheet_raises=RuntimeError("Sheets quota exceeded")
        )
        # State returned without raising
        assert result["project_id"] == "test-project"

    # SR5
    def test_chat_card_failure_is_graceful(self, tmp_path):
        result, mock_append, mock_card = self._run(
            tmp_path, card_raises=RuntimeError("Chat API down")
        )
        # State returned without raising; Sheet row still written
        mock_append.assert_called_once()
        assert result["project_id"] == "test-project"

    # SR6
    def test_no_incoming_message_returns_state_unchanged(self, tmp_path):
        _make_settings(tmp_path)
        state = _base_state()

        with (
            patch("tools.google_sheets.append_row") as mock_append,
            patch("tools.google_chat.send_skill_import_card") as mock_card,
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            from agents.nexus_prime.orchestrator import handle_skill_request
            result = handle_skill_request(state)

        assert result == state
        mock_append.assert_not_called()
        mock_card.assert_not_called()


# ── TestHandleSkillRequestResolution ─────────────────────────────────────────


class TestHandleSkillRequestResolution:
    """Tests for the resolution path ('status' present in payload)."""

    def _run(
        self,
        tmp_path,
        status="Approved",
        proposal_id="prop-001",
        package_name="requests",
        agent_id="scout",
        approved_by="owner@example.com",
        parked=None,
        update_raises=None,
        publish_raises=None,
    ):
        _make_settings(tmp_path)
        from models import MessageType
        msg = _make_a2a(
            MessageType.SKILL_REQUEST,
            payload={
                "status": status,
                "proposal_id": proposal_id,
                "package_name": package_name,
                "agent_id": agent_id,
                "approved_by": approved_by,
                "source": "google_chat",
                "space_name": "spaces/OWNER",
            },
            source_agent="google-chat",
        )
        initial_parked = list(parked) if parked is not None else [proposal_id]
        state = _base_state(parked=initial_parked)
        state["incoming_message"] = msg

        mock_update = MagicMock() if not update_raises else MagicMock(side_effect=update_raises)
        mock_publish = MagicMock() if not publish_raises else MagicMock(side_effect=publish_raises)

        with (
            patch("tools.google_sheets.update_row", mock_update),
            patch("tools.pubsub.publish", mock_publish),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            from agents.nexus_prime.orchestrator import handle_skill_request
            result = handle_skill_request(state)
        return result, mock_update, mock_publish

    # SR7
    def test_approved_updates_row_and_publishes_skill_request(self, tmp_path):
        result, mock_update, mock_publish = self._run(tmp_path, status="Approved")

        # Sheet row updated
        mock_update.assert_called_once()
        tab, pid, updates, project_id = mock_update.call_args.args
        assert tab == "Agent_Approvals"
        assert updates["Status"] == "Approved"

        # SKILL_REQUEST published back to requesting agent
        mock_publish.assert_called_once()
        topic, msg_obj, proj = mock_publish.call_args.args
        assert "scout" in topic
        assert msg_obj.message_type.value == "SKILL_REQUEST"
        assert msg_obj.payload["status"] == "Approved"
        assert msg_obj.payload["package_name"] == "requests"

    # SR8
    def test_rejected_updates_row_and_publishes_alert(self, tmp_path):
        result, mock_update, mock_publish = self._run(tmp_path, status="Rejected")

        # Sheet row updated to Rejected
        mock_update.assert_called_once()
        _, _, updates, _ = mock_update.call_args.args
        assert updates["Status"] == "Rejected"

        # ALERT published to requesting agent
        mock_publish.assert_called_once()
        topic, msg_obj, proj = mock_publish.call_args.args
        assert "scout" in topic
        assert msg_obj.message_type.value == "ALERT"
        assert msg_obj.payload["reason"] == "skill_request_rejected"

    # SR9
    def test_missing_proposal_id_skips_update_row(self, tmp_path):
        result, mock_update, mock_publish = self._run(
            tmp_path, proposal_id="", parked=[]
        )
        mock_update.assert_not_called()
        # publish is still attempted using payload agent_id
        mock_publish.assert_called_once()

    # SR10
    def test_publish_failure_on_resolution_is_graceful(self, tmp_path):
        result, mock_update, mock_publish = self._run(
            tmp_path,
            status="Approved",
            publish_raises=RuntimeError("Pub/Sub unavailable"),
        )
        # No exception propagated; update_row still called
        mock_update.assert_called_once()
        assert result["project_id"] == "test-project"

    # SR11
    def test_rejected_removes_proposal_from_parked(self, tmp_path):
        result, _, _ = self._run(
            tmp_path,
            status="Rejected",
            proposal_id="prop-001",
            parked=["prop-001", "prop-002"],
        )
        assert "prop-001" not in result["parked_proposals"]
        assert "prop-002" in result["parked_proposals"]


# ── TestSkillRequestRouting ───────────────────────────────────────────────────


class TestSkillRequestRouting:
    # SR12
    def test_route_dispatches_skill_request_to_handler(self, tmp_path):
        _make_settings(tmp_path)
        from models import MessageType
        msg = _make_a2a(
            MessageType.SKILL_REQUEST,
            payload={"package_name": "numpy", "agent_id": "scout"},
        )
        state = _base_state()
        state["incoming_message"] = msg

        from agents.nexus_prime.orchestrator import route
        destination = route(state)
        assert destination == "handle_skill_request"
