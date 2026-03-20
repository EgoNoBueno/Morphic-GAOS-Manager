"""
tests/test_daily_sync.py — Tests for handle_daily_sync() and POST /daily-sync.

TestHandleDailySync (9 tests):
  D1   Returns correct overnight_logs count (only rows within 24 h counted).
  D2   Returns correct overnight_errors count.
  D3   Returns correct pending_approvals count (only pending / empty status).
  D4   Calls send_card() with the configured owner_space.
  D5   Does NOT call send_card() when chat.owner_space is empty.
  D6   Card header subtitle contains today date string.
  D7   Card activity section reflects log count and agent names.
  D8   Logs tab read failure is graceful (overnight_logs=0, no exception raised).
  D9   Returns expected summary dict keys.

TestDailySyncEndpoint (4 tests):
  S1   POST /daily-sync returns 200 with status=ok for nexus-prime.
  S2   POST /daily-sync returns 404 for non-nexus-prime agents.
  S3   POST /daily-sync returns 401 without Authorization header.
  S4   POST /daily-sync response body includes overnight_logs, overnight_errors,
       pending_approvals from handle_daily_sync result.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

# ── Settings fixture ─────────────────────────────────────────────────────

SETTINGS_YAML_NO_CHAT = """\
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

SETTINGS_YAML_WITH_CHAT = (
    SETTINGS_YAML_NO_CHAT
    + """\
chat:
  owner_space: spaces/OWNER_SPACE_XYZ
"""
)

_AUTH_HEADER = {"Authorization": "Bearer fake-token"}

# Timestamps relative to actual now — avoids needing to freeze/mock datetime
_NOW = datetime.now(UTC)
_RECENT = (_NOW - timedelta(hours=2)).isoformat()  # within 24 h window
_OLD = (_NOW - timedelta(hours=36)).isoformat()  # outside 24 h window


def _make_settings(tmp_path, yaml_text: str):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml_text)
    import config

    config._reset_for_testing()
    config.load_settings(cfg)
    return cfg


@pytest.fixture(autouse=True)
def reset_settings():
    yield
    import config

    config._reset_for_testing()


# ── TestHandleDailySync ──────────────────────────────────────────────────────


class TestHandleDailySync:
    """Tests for agents.nexus_prime.orchestrator.handle_daily_sync()."""

    def _run(
        self, tmp_path, yaml_text=SETTINGS_YAML_WITH_CHAT, logs=None, errors=None, approvals=None
    ):
        """Call handle_daily_sync() with mocked Sheet reads and send_card."""
        _make_settings(tmp_path, yaml_text)

        logs_rows = logs if logs is not None else []
        error_rows = errors if errors is not None else []
        approval_rows = approvals if approvals is not None else []

        def _fake_get_all_records(tab, project_id):
            if tab == "Logs":
                return logs_rows
            if tab == "Error Logs":
                return error_rows
            if tab == "Agent_Approvals":
                return approval_rows
            return []

        with (
            patch("tools.google_sheets.get_all_records", side_effect=_fake_get_all_records),
            patch("tools.google_chat.send_card") as mock_send,
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            from agents.nexus_prime.orchestrator import handle_daily_sync

            result = asyncio.run(handle_daily_sync("test-project"))
        return result, mock_send

    # D1
    def test_counts_only_recent_logs(self, tmp_path):
        logs = [
            {"timestamp": _RECENT, "agent_id": "beacon"},
            {"timestamp": _RECENT, "agent_id": "ledger"},
            {"timestamp": _OLD, "agent_id": "scout"},
        ]
        result, _ = self._run(tmp_path, logs=logs)
        assert result["overnight_logs"] == 2

    # D2
    def test_counts_only_recent_errors(self, tmp_path):
        errors = [
            {"timestamp": _RECENT, "agent_id": "foreman"},
            {"timestamp": _OLD, "agent_id": "steward"},
        ]
        result, _ = self._run(tmp_path, errors=errors)
        assert result["overnight_errors"] == 1

    # D3
    def test_counts_pending_approvals_only(self, tmp_path):
        approvals = [
            {"status": "pending"},
            {"status": ""},
            {"status": "Approved"},
            {"status": "Rejected"},
        ]
        result, _ = self._run(tmp_path, approvals=approvals)
        assert result["pending_approvals"] == 2

    # D4
    def test_sends_card_to_owner_space(self, tmp_path):
        _, mock_send = self._run(tmp_path, yaml_text=SETTINGS_YAML_WITH_CHAT)
        mock_send.assert_called_once()
        assert mock_send.call_args[0][0] == "spaces/OWNER_SPACE_XYZ"

    # D5
    def test_no_card_sent_when_owner_space_empty(self, tmp_path):
        _, mock_send = self._run(tmp_path, yaml_text=SETTINGS_YAML_NO_CHAT)
        mock_send.assert_not_called()

    # D6
    def test_card_header_subtitle_contains_date(self, tmp_path):
        _, mock_send = self._run(tmp_path, yaml_text=SETTINGS_YAML_WITH_CHAT)
        card = mock_send.call_args[0][1]
        subtitle = card["header"]["subtitle"]
        assert str(_NOW.year) in subtitle
        assert _NOW.strftime("%B") in subtitle

    # D7
    def test_card_activity_section_reflects_agent_names(self, tmp_path):
        logs = [
            {"timestamp": _RECENT, "agent_id": "beacon"},
            {"timestamp": _RECENT, "agent_id": "ledger"},
        ]
        _, mock_send = self._run(tmp_path, yaml_text=SETTINGS_YAML_WITH_CHAT, logs=logs)
        card = mock_send.call_args[0][1]
        text = card["sections"][0]["widgets"][0]["textParagraph"]["text"]
        assert "beacon" in text or "ledger" in text

    # D8
    def test_logs_tab_error_is_graceful(self, tmp_path):
        _make_settings(tmp_path, SETTINGS_YAML_NO_CHAT)

        def _bad_get(tab, project_id):
            if tab == "Logs":
                raise RuntimeError("network blip")
            return []

        with (
            patch("tools.google_sheets.get_all_records", side_effect=_bad_get),
            patch("tools.google_chat.send_card"),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            from agents.nexus_prime.orchestrator import handle_daily_sync

            result = asyncio.run(handle_daily_sync("test-project"))
        assert result["overnight_logs"] == 0

    # D9
    def test_returns_expected_keys(self, tmp_path):
        result, _ = self._run(tmp_path)
        for key in (
            "overnight_logs",
            "overnight_errors",
            "pending_approvals",
            "space_name",
            "task_id",
        ):
            assert key in result


# ── TestDailySyncEndpoint ─────────────────────────────────────────────────────


class TestDailySyncEndpoint:
    """Integration tests for POST /daily-sync via FastAPI TestClient."""

    def _reloaded_client(self, mock_daily_sync, agent_name: str = "nexus-prime"):
        """Reload main, then patch handle_daily_sync on the live orchestrator module."""
        import importlib

        import main as main_mod

        with patch.dict(
            os.environ,
            {"AGENT_NAME": agent_name, "GCP_PROJECT_ID": "test-project"},
        ):
            importlib.reload(main_mod)

        patcher = patch(
            "agents.nexus_prime.orchestrator.handle_daily_sync",
            new=mock_daily_sync,
        )
        patcher.start()
        client = TestClient(main_mod.app, raise_server_exceptions=False)
        return client, main_mod, patcher

    def _fake_result(self) -> dict:
        return {
            "overnight_logs": 5,
            "overnight_errors": 1,
            "pending_approvals": 2,
            "space_name": "spaces/OWNER_SPACE_XYZ",
            "task_id": "task-test-uuid",
        }

    # S1
    def test_daily_sync_returns_200_for_nexus_prime(self, tmp_path):
        _make_settings(tmp_path, SETTINGS_YAML_WITH_CHAT)
        mock_fn = AsyncMock(return_value=self._fake_result())
        client, _, patcher = self._reloaded_client(mock_fn)
        try:
            resp = client.post("/daily-sync", headers=_AUTH_HEADER)
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"
        finally:
            patcher.stop()

    # S2
    def test_daily_sync_returns_404_for_non_nexus_agent(self, tmp_path):
        _make_settings(tmp_path, SETTINGS_YAML_WITH_CHAT)
        mock_fn = AsyncMock(return_value=self._fake_result())
        client, _, patcher = self._reloaded_client(mock_fn, agent_name="scout")
        try:
            resp = client.post("/daily-sync", headers=_AUTH_HEADER)
            assert resp.status_code == 404
        finally:
            patcher.stop()

    # S3
    def test_daily_sync_returns_401_without_auth(self, tmp_path):
        _make_settings(tmp_path, SETTINGS_YAML_WITH_CHAT)
        mock_fn = AsyncMock(return_value=self._fake_result())
        client, _, patcher = self._reloaded_client(mock_fn)
        try:
            resp = client.post("/daily-sync")
            assert resp.status_code == 401
        finally:
            patcher.stop()

    # S4
    def test_daily_sync_response_includes_result_fields(self, tmp_path):
        _make_settings(tmp_path, SETTINGS_YAML_WITH_CHAT)
        mock_fn = AsyncMock(return_value=self._fake_result())
        client, _, patcher = self._reloaded_client(mock_fn)
        try:
            resp = client.post("/daily-sync", headers=_AUTH_HEADER)
            body = resp.json()
            assert body["overnight_logs"] == 5
            assert body["overnight_errors"] == 1
            assert body["pending_approvals"] == 2
        finally:
            patcher.stop()
