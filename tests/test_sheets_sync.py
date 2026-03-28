"""
tests/test_sheets_sync.py — Tests for handle_sheets_sync() and POST /sheets-sync.

TestHandleSheetsSync (5 tests):
  SS1  Happy path — all 4 tabs sync; replace_rows called 4× with normalized rows.
  SS2  One tab TabNotFoundError — other 3 still sync; failed tab has {"error": ...}.
  SS3  BigQueryInsertError on one table — logged WARNING, others continue.
  SS4  All rows empty (0 Pending_Knowledge rows) — replace_rows called with [].
  SS5  Header normalization — "Approved By" → "approved_by",
       "Total Cost USD" → "total_cost_usd", "Agent ID" → "agent_id".

TestSheetsSyncEndpoint (4 tests):
  E1   POST /sheets-sync returns 200 with status=ok for nexus-prime.
  E2   POST /sheets-sync returns 404 for non-nexus-prime agents.
  E3   POST /sheets-sync returns 401 without Authorization header.
  E4   POST /sheets-sync response body includes staging table keys.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tools.bigquery import BigQueryInsertError

# ── Settings fixture ─────────────────────────────────────────────────────────

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

_AUTH_HEADER = {"Authorization": "Bearer fake-token"}

_APPROVALS_ROWS = [
    {
        "ID": "1",
        "Agent ID": "beacon",
        "Issue": "test issue",
        "Trigger Reason": "budget",
        "Stopping Constraint": "none",
        "Iterations Run": "3",
        "Total Cost USD": "0.05",
        "Proposed Code": "print('hi')",
        "Status": "Pending",
        "Timestamp": "2026-03-28T10:00:00Z",
        "Approved By": "",
        "Approver Tier": "",
        "code_sha256": "abc123",
        "Priority": "2",
    }
]
_LOGS_ROWS = [
    {
        "timestamp": "2026-03-28T10:00:00Z",
        "agent_id": "scout",
        "level": "INFO",
        "message": "task done",
        "project_id": "test-project",
    }
]
_ERROR_ROWS = [
    {
        "timestamp": "2026-03-28T09:00:00Z",
        "agent_id": "foreman",
        "error_type": "API",
        "message": "timeout",
        "traceback": "",
        "project_id": "test-project",
    }
]
_KNOWLEDGE_ROWS = [
    {
        "timestamp": "2026-03-28T08:00:00Z",
        "agent_id": "steward",
        "observation": "cost up",
        "source": "ledger",
        "confidence": "0.9",
        "status": "Pending",
        "project_id": "test-project",
    }
]


def _make_settings(tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(SETTINGS_YAML)
    import config

    config._reset_for_testing()
    config.load_settings(cfg)
    return cfg


@pytest.fixture(autouse=True)
def reset_settings():
    yield
    import config

    config._reset_for_testing()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _run_sync(tmp_path, get_all_records_side_effect, replace_rows_side_effect=None):
    """Run handle_sheets_sync() with mocked Sheet reads and BQ replace_rows."""
    _make_settings(tmp_path)

    replace_mock = MagicMock(side_effect=replace_rows_side_effect)

    with (
        patch("tools.google_sheets.get_all_records", side_effect=get_all_records_side_effect),
        patch("tools.google_sheets.init_sheets_client"),
        patch("tools.bigquery.replace_rows", replace_mock),
        patch("agents.nexus_prime.orchestrator._log_cloud"),
    ):
        from agents.nexus_prime.orchestrator import handle_sheets_sync

        result = asyncio.run(handle_sheets_sync("test-project"))

    return result, replace_mock


# ── TestHandleSheetsSync ──────────────────────────────────────────────────────


class TestHandleSheetsSync:
    """Tests for agents.nexus_prime.orchestrator.handle_sheets_sync()."""

    # SS1
    def test_happy_path_all_tabs_sync(self, tmp_path):
        """replace_rows is called 4× and row counts are returned."""

        def _fake_get(tab, project_id):
            return {
                "Agent_Approvals": _APPROVALS_ROWS,
                "Logs": _LOGS_ROWS,
                "Error Logs": _ERROR_ROWS,
                "Pending_Knowledge": _KNOWLEDGE_ROWS,
            }[tab]

        result, replace_mock = _run_sync(tmp_path, _fake_get)

        assert replace_mock.call_count == 4
        assert result["staging_approvals"] == 1
        assert result["staging_logs"] == 1
        assert result["staging_errors"] == 1
        assert result["staging_pending_knowledge"] == 1
        assert "synced_at" in result
        assert "task_id" in result

    # SS2
    def test_one_tab_missing_others_still_sync(self, tmp_path):
        """If one tab raises an exception, the other 3 still sync successfully."""

        def _fake_get(tab, project_id):
            if tab == "Error Logs":
                raise RuntimeError("Tab not found: Error Logs")
            return {
                "Agent_Approvals": _APPROVALS_ROWS,
                "Logs": _LOGS_ROWS,
                "Pending_Knowledge": _KNOWLEDGE_ROWS,
            }[tab]

        result, replace_mock = _run_sync(tmp_path, _fake_get)

        # 3 successful + 1 failed
        assert replace_mock.call_count == 3
        assert result["staging_approvals"] == 1
        assert result["staging_logs"] == 1
        assert result["staging_pending_knowledge"] == 1
        # Failed tab gets error key
        assert isinstance(result["staging_errors"], dict)
        assert "error" in result["staging_errors"]

    # SS3
    def test_bq_insert_error_on_one_table_others_continue(self, tmp_path):
        """BigQueryInsertError on one replace_rows call is non-fatal."""

        def _fake_get(tab, project_id):
            return {
                "Agent_Approvals": _APPROVALS_ROWS,
                "Logs": _LOGS_ROWS,
                "Error Logs": _ERROR_ROWS,
                "Pending_Knowledge": _KNOWLEDGE_ROWS,
            }[tab]

        call_count = {"n": 0}

        def _bq_side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise BigQueryInsertError("BQ staging_approvals DELETE failed")
            # All other calls succeed
            return None

        result, replace_mock = _run_sync(tmp_path, _fake_get, _bq_side_effect)

        # All 4 tabs attempted replace_rows
        assert replace_mock.call_count == 4
        # First table failed
        assert isinstance(result["staging_approvals"], dict)
        assert "error" in result["staging_approvals"]
        # Remaining tables succeeded
        assert result["staging_logs"] == 1
        assert result["staging_errors"] == 1
        assert result["staging_pending_knowledge"] == 1

    # SS4
    def test_empty_rows_calls_replace_with_empty_list(self, tmp_path):
        """Empty Pending_Knowledge tab results in replace_rows([], ...) call."""

        def _fake_get(tab, project_id):
            if tab == "Pending_Knowledge":
                return []
            return {
                "Agent_Approvals": _APPROVALS_ROWS,
                "Logs": _LOGS_ROWS,
                "Error Logs": _ERROR_ROWS,
            }[tab]

        result, replace_mock = _run_sync(tmp_path, _fake_get)

        # replace_rows called 4 times — empty list is still a valid call
        assert replace_mock.call_count == 4
        assert result["staging_pending_knowledge"] == 0

        # Verify the call for Pending_Knowledge passed an empty list of data rows
        # (each normalized row has "synced_at" added, so empty input → empty list)
        calls_for_pk = [
            c for c in replace_mock.call_args_list if "staging_pending_knowledge" in c.args[0]
        ]
        assert len(calls_for_pk) == 1
        assert calls_for_pk[0].args[1] == []

    # SS5
    def test_header_normalization(self, tmp_path):
        """Sheet headers with spaces are normalized to snake_case BQ column names."""
        approvals_row = {
            "ID": "42",
            "Agent ID": "beacon",
            "Approved By": "owner@example.com",
            "Total Cost USD": "1.23",
            "Proposed Code": "x=1",
            "Issue": "mem leak",
            "Trigger Reason": "cost spike",
            "Stopping Constraint": "budget",
            "Iterations Run": "5",
            "Status": "Pending",
            "Timestamp": "2026-03-28T12:00:00Z",
            "Approver Tier": "1",
            "code_sha256": "deadbeef",
            "Priority": "1",
        }

        def _fake_get(tab, project_id):
            if tab == "Agent_Approvals":
                return [approvals_row]
            return []

        result, replace_mock = _run_sync(tmp_path, _fake_get)

        # Find the replace_rows call for staging_approvals
        approval_calls = [
            c for c in replace_mock.call_args_list if "staging_approvals" in c.args[0]
        ]
        assert len(approval_calls) == 1
        normalized_rows = approval_calls[0].args[1]
        assert len(normalized_rows) == 1
        row = normalized_rows[0]

        # Verify key normalizations
        assert "agent_id" in row, "Agent ID → agent_id"
        assert "approved_by" in row, "Approved By → approved_by"
        assert "total_cost_usd" in row, "Total Cost USD → total_cost_usd"
        assert "synced_at" in row, "synced_at injected"

        # Verify values preserved
        assert row["agent_id"] == "beacon"
        assert row["approved_by"] == "owner@example.com"
        assert row["total_cost_usd"] == "1.23"


# ── TestSheetsSyncEndpoint ────────────────────────────────────────────────────


class TestSheetsSyncEndpoint:
    """Tests for POST /sheets-sync in main.py."""

    @pytest.fixture(autouse=True)
    def setup_nexus_env(self, tmp_path):
        _make_settings(tmp_path)
        os.environ["AGENT_NAME"] = "nexus-prime"
        os.environ["GCP_PROJECT_ID"] = "test-project"
        yield
        os.environ.pop("AGENT_NAME", None)
        os.environ.pop("GCP_PROJECT_ID", None)

    def _client(self):
        import importlib

        import main

        importlib.reload(main)
        return TestClient(main.app, raise_server_exceptions=False)

    # E1
    def test_returns_200_for_nexus_prime(self, tmp_path):
        mock_result = {
            "staging_approvals": 5,
            "staging_logs": 12,
            "staging_errors": 1,
            "staging_pending_knowledge": 3,
            "synced_at": "2026-03-28T12:00:00Z",
            "task_id": "test-task-id",
        }
        with (
            patch(
                "agents.nexus_prime.orchestrator.handle_sheets_sync",
                new=AsyncMock(return_value=mock_result),
            ),
            patch("main._verify_pubsub_audience"),
        ):
            client = self._client()
            resp = client.post("/sheets-sync", headers=_AUTH_HEADER)

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["staging_approvals"] == 5

    # E2
    def test_returns_404_for_non_nexus_prime_agent(self, tmp_path):
        os.environ["AGENT_NAME"] = "beacon"
        with patch("main._verify_pubsub_audience"):
            client = self._client()
            resp = client.post("/sheets-sync", headers=_AUTH_HEADER)
        assert resp.status_code == 404

    # E3
    def test_returns_401_without_auth_header(self, tmp_path):
        client = self._client()
        resp = client.post("/sheets-sync")
        assert resp.status_code == 401

    # E4
    def test_response_body_includes_staging_table_keys(self, tmp_path):
        mock_result = {
            "staging_approvals": 2,
            "staging_logs": 8,
            "staging_errors": 0,
            "staging_pending_knowledge": 1,
            "synced_at": "2026-03-28T12:00:00Z",
            "task_id": "abc",
        }
        with (
            patch(
                "agents.nexus_prime.orchestrator.handle_sheets_sync",
                new=AsyncMock(return_value=mock_result),
            ),
            patch("main._verify_pubsub_audience"),
        ):
            client = self._client()
            resp = client.post("/sheets-sync", headers=_AUTH_HEADER)

        body = resp.json()
        assert "staging_approvals" in body
        assert "staging_logs" in body
        assert "staging_errors" in body
        assert "staging_pending_knowledge" in body
        assert "synced_at" in body
