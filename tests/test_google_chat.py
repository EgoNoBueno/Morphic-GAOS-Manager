"""
tests/test_google_chat.py — Unit tests for tools/google_chat.py and the POST /chat endpoint.

TestGoogleChatTool (12 tests):
  C1   send_message raises ChatConfigError on empty space_name.
  C2   send_message truncates text longer than 4096 chars.
  C3   send_message calls Chat API with correct parent and body.
  C4   send_message wraps HttpError as ChatDeliveryError.
  C5   send_card raises ChatConfigError on empty space_name.
  C6   send_card builds a cardsV2 body and calls the API.
  C7   send_approval_card raises ChatConfigError on empty proposal_id.
  C8   send_approval_card card contains Approve and Reject buttons.
  C9   send_approval_card includes View Blueprint button only when doc_url provided.
  C10  send_skill_import_card raises ChatConfigError on empty package_name.
  C11  send_skill_import_card card contains Install and Deny buttons.
  C12  parse_chat_event raises ChatEventParseError on missing 'type' field.

TestParseChatEvent (6 tests):
  P1   MESSAGE event returns correct event_type, text, sender_email.
  P2   CARD_CLICKED approve event returns action_name='approve' + parameters.
  P3   CARD_CLICKED reject returns action_name='reject'.
  P4   CARD_CLICKED skill_approve returns action_name and package_name parameter.
  P5   ADDED_TO_SPACE returns event_type without error.
  P6   Unknown CARD_CLICKED action still parses without exception.

TestChatEndpoint (7 tests):
  E1   POST /chat with MESSAGE body dispatches CHAT_MESSAGE to agent.run().
  E2   POST /chat with CARD_CLICKED approve dispatches APPROVAL_RESULT.
  E3   POST /chat with CARD_CLICKED reject dispatches APPROVAL_RESULT with status Rejected.
  E4   POST /chat with CARD_CLICKED skill_approve dispatches SKILL_REQUEST.
  E5   POST /chat with ADDED_TO_SPACE returns 200 without calling agent.run().
  E6   POST /chat with unrecognised action returns 200 without calling agent.run().
  E7   POST /chat returns 404 when AGENT_NAME != nexus-prime.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError

from tools.google_chat import (
    ChatConfigError,
    ChatDeliveryError,
    ChatEventParseError,
    parse_chat_event,
    send_approval_card,
    send_card,
    send_message,
    send_skill_import_card,
)

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

_SPACE = "spaces/TEST_SPACE_XYZ"
_PROPOSAL_ID = "prop-abc-123"
_PACKAGE = "pandas"


@pytest.fixture(autouse=True)
def load_test_settings(tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(SETTINGS_YAML)
    import config

    config._reset_for_testing()
    config.load_settings(cfg)
    yield
    config._reset_for_testing()


def _fake_service(captured: dict | None = None):
    """Return a mock googleapiclient service that records the last create() call body."""
    mock_msg = {"name": "spaces/TEST_SPACE_XYZ/messages/abc123"}

    create_mock = MagicMock()
    create_mock.execute.return_value = mock_msg

    messages_mock = MagicMock()
    messages_mock.create.return_value = create_mock
    if captured is not None:
        def _create(parent, body):
            captured["parent"] = parent
            captured["body"] = body
            return create_mock
        messages_mock.create.side_effect = _create

    spaces_mock = MagicMock()
    spaces_mock.messages.return_value = messages_mock

    service = MagicMock()
    service.spaces.return_value = spaces_mock
    return service


# ── TestGoogleChatTool ─────────────────────────────────────────────────────


class TestGoogleChatTool:
    # C1
    def test_send_message_raises_on_empty_space_name(self):
        with pytest.raises(ChatConfigError, match="space_name"):
            send_message("", "hello")

    # C2
    def test_send_message_truncates_overlong_text(self):
        captured: dict = {}
        svc = _fake_service(captured)
        text = "x" * 5000
        with patch("tools.google_chat._get_chat_service", return_value=svc):
            send_message(_SPACE, text)
        sent_text = captured["body"]["text"]
        assert len(sent_text) == 4096
        assert sent_text.endswith("...")

    # C3
    def test_send_message_calls_api_with_correct_parent_and_body(self):
        captured: dict = {}
        svc = _fake_service(captured)
        with patch("tools.google_chat._get_chat_service", return_value=svc):
            result = send_message(_SPACE, "hello world")
        assert captured["parent"] == _SPACE
        assert captured["body"]["text"] == "hello world"
        assert result["name"] == "spaces/TEST_SPACE_XYZ/messages/abc123"

    # C4
    def test_send_message_wraps_http_error_as_chat_delivery_error(self):
        svc = _fake_service()
        resp = MagicMock()
        resp.status = 403
        resp.reason = "Forbidden"
        svc.spaces().messages().create().execute.side_effect = HttpError(resp=resp, content=b"")
        with patch("tools.google_chat._get_chat_service", return_value=svc):
            with pytest.raises(ChatDeliveryError, match="403"):
                send_message(_SPACE, "test")

    # C5
    def test_send_card_raises_on_empty_space_name(self):
        with pytest.raises(ChatConfigError, match="space_name"):
            send_card("", {"header": {}, "sections": []})

    # C6
    def test_send_card_builds_cards_v2_body(self):
        captured: dict = {}
        svc = _fake_service(captured)
        card = {"cardId": "test-card", "header": {"title": "T"}, "sections": []}
        with patch("tools.google_chat._get_chat_service", return_value=svc):
            send_card(_SPACE, card)
        assert "cardsV2" in captured["body"]
        card_in_body = captured["body"]["cardsV2"][0]
        assert card_in_body["cardId"] == "test-card"
        assert card_in_body["card"] == card

    # C7
    def test_send_approval_card_raises_on_empty_proposal_id(self):
        with pytest.raises(ChatConfigError, match="proposal_id"):
            svc = _fake_service()
            with patch("tools.google_chat._get_chat_service", return_value=svc):
                send_approval_card(_SPACE, "", "beacon", "issue", "action", 4, 0.05)

    # C8
    def test_send_approval_card_contains_approve_and_reject_buttons(self):
        captured: dict = {}
        svc = _fake_service(captured)
        with patch("tools.google_chat._get_chat_service", return_value=svc):
            send_approval_card(_SPACE, _PROPOSAL_ID, "beacon", "issue text", "action text", 4, 0.01)

        card = captured["body"]["cardsV2"][0]["card"]
        widgets = card["sections"][0]["widgets"]
        button_list = next(w for w in widgets if "buttonList" in w)
        action_names = [
            b["onClick"]["action"]["actionMethodName"]
            for b in button_list["buttonList"]["buttons"]
            if "action" in b["onClick"]
        ]
        assert "approve" in action_names
        assert "reject" in action_names

    # C9
    def test_send_approval_card_adds_view_blueprint_button_when_doc_url_given(self):
        captured: dict = {}
        svc = _fake_service(captured)
        with patch("tools.google_chat._get_chat_service", return_value=svc):
            send_approval_card(
                _SPACE, _PROPOSAL_ID, "beacon", "issue", "action", 4, 0.0,
                doc_url="https://docs.google.com/document/d/fake"
            )

        card = captured["body"]["cardsV2"][0]["card"]
        widgets = card["sections"][0]["widgets"]
        button_list = next(w for w in widgets if "buttonList" in w)
        open_link_texts = [
            b["text"]
            for b in button_list["buttonList"]["buttons"]
            if "openLink" in b.get("onClick", {})
        ]
        assert "View Blueprint" in open_link_texts

    # C10
    def test_send_skill_import_card_raises_on_empty_package_name(self):
        svc = _fake_service()
        with patch("tools.google_chat._get_chat_service", return_value=svc):
            with pytest.raises(ChatConfigError, match="package_name"):
                send_skill_import_card(_SPACE, _PROPOSAL_ID, "scout", "", "need it")

    # C11
    def test_send_skill_import_card_contains_install_and_deny_buttons(self):
        captured: dict = {}
        svc = _fake_service(captured)
        with patch("tools.google_chat._get_chat_service", return_value=svc):
            send_skill_import_card(_SPACE, _PROPOSAL_ID, "scout", _PACKAGE, "need pandas")

        card = captured["body"]["cardsV2"][0]["card"]
        widgets = card["sections"][0]["widgets"]
        button_list = next(w for w in widgets if "buttonList" in w)
        action_names = [
            b["onClick"]["action"]["actionMethodName"]
            for b in button_list["buttonList"]["buttons"]
            if "action" in b["onClick"]
        ]
        assert "skill_approve" in action_names
        assert "skill_reject" in action_names

    # C12
    def test_parse_chat_event_raises_on_missing_type(self):
        with pytest.raises(ChatEventParseError, match="type"):
            parse_chat_event({})


# ── TestParseChatEvent ────────────────────────────────────────────────────


class TestParseChatEvent:
    def _message_body(self, text: str = "hello bot") -> dict:
        return {
            "type": "MESSAGE",
            "space": {"name": _SPACE},
            "user": {"email": "owner@example.com"},
            "message": {
                "text": text,
                "name": f"{_SPACE}/messages/msg-1",
            },
        }

    def _card_clicked_body(self, action_method: str, params: list[dict] | None = None) -> dict:
        return {
            "type": "CARD_CLICKED",
            "space": {"name": _SPACE},
            "user": {"email": "owner@example.com"},
            "message": {"text": "", "name": f"{_SPACE}/messages/msg-2"},
            "action": {
                "actionMethodName": action_method,
                "parameters": params or [],
            },
        }

    # P1
    def test_message_event_parsed_correctly(self):
        event = parse_chat_event(self._message_body("tell me the status"))
        assert event["event_type"] == "MESSAGE"
        assert event["text"] == "tell me the status"
        assert event["sender_email"] == "owner@example.com"
        assert event["space_name"] == _SPACE
        assert event["action_name"] == ""
        assert event["parameters"] == {}

    # P2
    def test_card_clicked_approve_returns_correct_fields(self):
        params = [{"key": "proposal_id", "value": _PROPOSAL_ID}]
        event = parse_chat_event(self._card_clicked_body("approve", params))
        assert event["event_type"] == "CARD_CLICKED"
        assert event["action_name"] == "approve"
        assert event["parameters"]["proposal_id"] == _PROPOSAL_ID

    # P3
    def test_card_clicked_reject_returns_action_reject(self):
        params = [{"key": "proposal_id", "value": _PROPOSAL_ID}]
        event = parse_chat_event(self._card_clicked_body("reject", params))
        assert event["action_name"] == "reject"

    # P4
    def test_card_clicked_skill_approve_captures_package_name(self):
        params = [
            {"key": "proposal_id", "value": _PROPOSAL_ID},
            {"key": "package_name", "value": _PACKAGE},
        ]
        event = parse_chat_event(self._card_clicked_body("skill_approve", params))
        assert event["action_name"] == "skill_approve"
        assert event["parameters"]["package_name"] == _PACKAGE

    # P5
    def test_added_to_space_event_parses_without_error(self):
        body = {
            "type": "ADDED_TO_SPACE",
            "space": {"name": _SPACE},
            "user": {"email": "owner@example.com"},
            "message": {},
        }
        event = parse_chat_event(body)
        assert event["event_type"] == "ADDED_TO_SPACE"

    # P6
    def test_unknown_action_name_parses_without_exception(self):
        event = parse_chat_event(self._card_clicked_body("unknown_action"))
        assert event["action_name"] == "unknown_action"
        assert event["parameters"] == {}


# ── TestChatEndpoint ──────────────────────────────────────────────────────


def _build_client(agent_name: str = "nexus-prime") -> TestClient:
    """Import main.py fresh with the given AGENT_NAME env var."""
    with patch.dict(os.environ, {"AGENT_NAME": agent_name, "GCP_PROJECT_ID": "test-project"}):
        import importlib
        import main as main_mod

        importlib.reload(main_mod)
        return TestClient(main_mod.app, raise_server_exceptions=False)


def _extract_envelope(run_call_args) -> dict:
    """Decode the synthetic A2AMessage from the mocked agent.run() call."""
    envelope = run_call_args[0][0]
    data = base64.b64decode(envelope["message"]["data"]).decode()
    return json.loads(data)


class TestChatEndpoint:
    _HEADERS = {"Authorization": "Bearer fake-oidc-token"}
    _SPACE = _SPACE

    def _message_body(self, text: str = "status update") -> dict:
        return {
            "type": "MESSAGE",
            "space": {"name": self._SPACE},
            "user": {"email": "owner@example.com"},
            "message": {"text": text, "name": f"{self._SPACE}/messages/m1"},
        }

    def _card_body(self, action: str, params: list[dict] | None = None) -> dict:
        return {
            "type": "CARD_CLICKED",
            "space": {"name": self._SPACE},
            "user": {"email": "owner@example.com"},
            "message": {"text": "", "name": f"{self._SPACE}/messages/m2"},
            "action": {
                "actionMethodName": action,
                "parameters": params or [],
            },
        }

    def _reloaded_client(self, mock_agent: MagicMock, agent_name: str = "nexus-prime"):
        """Reload main with env vars set, then patch _get_agent on the live module."""
        import importlib
        import main as main_mod

        with patch.dict(
            os.environ,
            {"AGENT_NAME": agent_name, "GCP_PROJECT_ID": "test-project"},
        ):
            importlib.reload(main_mod)

        # Patch after reload so the live module reference is used
        patcher = patch.object(main_mod, "_get_agent", return_value=mock_agent)
        patcher.start()
        client = TestClient(main_mod.app, raise_server_exceptions=False)
        return client, main_mod, patcher

    # E1
    def test_message_dispatches_chat_message_type(self):
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=MagicMock(task_id="t1"))

        client, main_mod, patcher = self._reloaded_client(mock_agent)
        try:
            resp = client.post("/chat", json=self._message_body(), headers=self._HEADERS)
        finally:
            patcher.stop()

        assert resp.status_code == 200
        assert mock_agent.run.called
        msg = _extract_envelope(mock_agent.run.call_args)
        assert msg["message_type"] == "CHAT_MESSAGE"
        assert msg["payload"]["text"] == "status update"
        assert msg["payload"]["sender_email"] == "owner@example.com"

    # E2
    def test_card_approve_dispatches_approval_result_approved(self):
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=MagicMock(task_id="t2"))

        params = [{"key": "proposal_id", "value": _PROPOSAL_ID}]
        body = self._card_body("approve", params)

        client, main_mod, patcher = self._reloaded_client(mock_agent)
        try:
            resp = client.post("/chat", json=body, headers=self._HEADERS)
        finally:
            patcher.stop()

        assert resp.status_code == 200
        msg = _extract_envelope(mock_agent.run.call_args)
        assert msg["message_type"] == "APPROVAL_RESULT"
        assert msg["payload"]["status"] == "Approved"
        assert msg["payload"]["proposal_id"] == _PROPOSAL_ID
        assert msg["payload"]["source"] == "google_chat"

    # E3
    def test_card_reject_dispatches_approval_result_rejected(self):
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=MagicMock(task_id="t3"))

        params = [{"key": "proposal_id", "value": _PROPOSAL_ID}]
        body = self._card_body("reject", params)

        client, main_mod, patcher = self._reloaded_client(mock_agent)
        try:
            resp = client.post("/chat", json=body, headers=self._HEADERS)
        finally:
            patcher.stop()

        assert resp.status_code == 200
        msg = _extract_envelope(mock_agent.run.call_args)
        assert msg["message_type"] == "APPROVAL_RESULT"
        assert msg["payload"]["status"] == "Rejected"

    # E4
    def test_card_skill_approve_dispatches_skill_request(self):
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=MagicMock(task_id="t4"))

        params = [
            {"key": "proposal_id", "value": _PROPOSAL_ID},
            {"key": "package_name", "value": _PACKAGE},
        ]
        body = self._card_body("skill_approve", params)

        client, main_mod, patcher = self._reloaded_client(mock_agent)
        try:
            resp = client.post("/chat", json=body, headers=self._HEADERS)
        finally:
            patcher.stop()

        assert resp.status_code == 200
        msg = _extract_envelope(mock_agent.run.call_args)
        assert msg["message_type"] == "SKILL_REQUEST"
        assert msg["payload"]["package_name"] == _PACKAGE
        assert msg["payload"]["status"] == "Approved"

    # E5
    def test_added_to_space_returns_200_without_calling_agent(self):
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock()

        body = {
            "type": "ADDED_TO_SPACE",
            "space": {"name": self._SPACE},
            "user": {"email": "owner@example.com"},
            "message": {},
        }

        client, main_mod, patcher = self._reloaded_client(mock_agent)
        try:
            resp = client.post("/chat", json=body, headers=self._HEADERS)
        finally:
            patcher.stop()

        assert resp.status_code == 200
        mock_agent.run.assert_not_called()

    # E6
    def test_unrecognised_card_action_returns_200_without_calling_agent(self):
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock()

        body = self._card_body("open_external_link")

        client, main_mod, patcher = self._reloaded_client(mock_agent)
        try:
            resp = client.post("/chat", json=body, headers=self._HEADERS)
        finally:
            patcher.stop()

        assert resp.status_code == 200
        mock_agent.run.assert_not_called()

    # E7
    def test_chat_endpoint_returns_404_for_non_nexus_agent(self):
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock()

        client, main_mod, patcher = self._reloaded_client(mock_agent, agent_name="scout")
        try:
            resp = client.post("/chat", json=self._message_body(), headers=self._HEADERS)
        finally:
            patcher.stop()

        assert resp.status_code == 404
