"""
tests/test_vision_workflow.py — Tests for Phase 2.5 Step 5: Vision workflow.

TestVisionBlueprintNode (8 tests):
  VB1  Happy path: creates doc, appends Sheet row, sends approval card.
  VB2  Empty vision_text: skips doc creation, no Sheet/Chat calls.
  VB3  Docs API failure: logs error, still returns state (graceful).
  VB4  Sheets API failure: logs warning, still returns state (graceful).
  VB5  Chat card failure: logs warning, still returns state (graceful).
  VB6  active_blueprints updated with blueprint_id → doc_id mapping.
  VB7  No incoming_message: returns state unchanged.
  VB8  Falls back to chat.owner_space when payload space_name is empty.

TestIteratePlanNode (6 tests):
  IP1  Happy path: constraint appended to blueprint_constraints.
  IP2  Empty constraint_text: skips, no mutation.
  IP3  Compaction triggers at _COMPACTION_THRESHOLD (5 constraints).
  IP4  After compaction, constraint list reduced to 1 compacted entry.
  IP5  append_content called with constraint text.
  IP6  No incoming_message: returns state unchanged.

TestRunCompaction (3 tests):
  RC1  BQ insert called with correct fields for each constraint.
  RC2  Returns compacted paragraph text from model response.
  RC3  BQ failure is graceful (no exception propagated).

TestHandlePollComments (4 tests):
  PC1  Publishes COMMENT_RECEIVED for each unresolved comment.
  PC2  Skips comments marked resolved=True.
  PC3  No-op when Project_Incubator is empty.
  PC4  Docs read failure is graceful (errors counter incremented).

TestVisionEndpoint (5 tests):
  E1   POST /vision returns 200 with task_id for nexus-prime.
  E2   POST /vision returns 404 for non-nexus-prime agents.
  E3   POST /vision returns 401 without Authorization header.
  E4   POST /vision returns 400 when vision_text is missing.
  E5   POST /vision passes correct payload fields to agent.run().

TestPollCommentsEndpoint (4 tests):
  P1   POST /poll-comments returns 200 with docs_polled for nexus-prime.
  P2   POST /poll-comments returns 404 for non-nexus-prime agents.
  P3   POST /poll-comments returns 401 without Authorization header.
  P4   POST /poll-comments response includes comments_published count.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ── Settings fixture ─────────────────────────────────────────────────────────

_SETTINGS_YAML = """\
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
  owner_space: spaces/OWNER_SPACE
docs:
  blueprints_folder_id: folder-blueprints
"""

_AUTH_HEADER = {"Authorization": "Bearer fake-token"}


def _make_settings(tmp_path, yaml_text: str = _SETTINGS_YAML):
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


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_a2a(msg_type, payload=None, source_agent="owner"):
    from models import A2AMessage

    return A2AMessage(
        source_agent=source_agent,
        target_agent="nexus-prime",
        project_id="test-project",
        task_id="task-abc",
        message_type=msg_type,
        priority=3,
        payload=payload or {},
    )


def _base_state(extra=None):
    state = {
        "task_id": "task-abc",
        "project_id": "test-project",
        "cost_usd": 0.0,
        "tokens_used": 0,
        "messages": [],
        "active_blueprints": {},
        "blueprint_constraints": [],
    }
    if extra:
        state.update(extra)
    return state


def _fake_model_resp(text="Blueprint content"):
    from agents import ModelResponse

    return ModelResponse(text=text, data={}, cost_usd=0.001, tokens_used=100)


# ── TestVisionBlueprintNode ───────────────────────────────────────────────────


class TestVisionBlueprintNode:
    """Tests for agents.nexus_prime.orchestrator.vision_blueprint()."""

    def _run(
        self,
        tmp_path,
        vision_text="Build a loyalty programme",
        doc_id="doc-123",
        space_name="",
        extra_state=None,
        mock_doc_raises=None,
        mock_sheet_raises=None,
        mock_chat_raises=None,
    ):
        _make_settings(tmp_path)
        from models import MessageType

        msg = _make_a2a(
            MessageType.VISION_SUBMITTED,
            {
                "vision_text": vision_text,
                "submitted_by": "owner@example.com",
                "space_name": space_name,
            },
        )
        state = _base_state(extra_state)
        state["incoming_message"] = msg

        mock_create = (
            MagicMock(return_value=doc_id)
            if not mock_doc_raises
            else MagicMock(side_effect=mock_doc_raises)
        )
        mock_append_row = (
            MagicMock() if not mock_sheet_raises else MagicMock(side_effect=mock_sheet_raises)
        )
        mock_card = MagicMock() if not mock_chat_raises else MagicMock(side_effect=mock_chat_raises)

        with (
            patch("agents.nexus_prime.orchestrator._call_model", return_value=_fake_model_resp()),
            patch("tools.google_docs.create_document", mock_create),
            patch("tools.google_sheets.append_row", mock_append_row),
            patch("tools.google_chat.send_approval_card", mock_card),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            from agents.nexus_prime.orchestrator import vision_blueprint

            result = vision_blueprint(state)
        return result, mock_create, mock_append_row, mock_card

    # VB1
    def test_happy_path_calls_all_downstream(self, tmp_path):
        result, mock_create, mock_sheet, mock_card = self._run(tmp_path)
        mock_create.assert_called_once()
        mock_sheet.assert_called_once()
        mock_card.assert_called_once()

    # VB2
    def test_empty_vision_text_skips_all(self, tmp_path):
        result, mock_create, mock_sheet, mock_card = self._run(tmp_path, vision_text="")
        mock_create.assert_not_called()
        mock_sheet.assert_not_called()
        mock_card.assert_not_called()

    # VB3
    def test_docs_api_failure_is_graceful(self, tmp_path):
        result, mock_create, mock_sheet, mock_card = self._run(
            tmp_path, mock_doc_raises=RuntimeError("Docs API down")
        )
        # Sheet + card still attempted (doc_id will be empty)
        mock_sheet.assert_called_once()

    # VB4
    def test_sheets_failure_is_graceful(self, tmp_path):
        result, mock_create, mock_sheet, mock_card = self._run(
            tmp_path, mock_sheet_raises=RuntimeError("Sheet unavailable")
        )
        # Node should still complete and return state
        assert result["project_id"] == "test-project"

    # VB5
    def test_chat_failure_is_graceful(self, tmp_path):
        result, _, _, mock_card = self._run(
            tmp_path, mock_chat_raises=RuntimeError("Chat API error")
        )
        assert result["project_id"] == "test-project"

    # VB6
    def test_active_blueprints_updated(self, tmp_path):
        result, _, _, _ = self._run(tmp_path, doc_id="doc-xyz")
        # active_blueprints should contain one entry mapping blueprint_id → doc_id
        active = result.get("active_blueprints", {})
        assert "doc-xyz" in active.values()

    # VB7
    def test_no_incoming_message_returns_unchanged(self, tmp_path):
        _make_settings(tmp_path)
        state = _base_state()
        # no incoming_message key set
        with patch("agents.nexus_prime.orchestrator._log_cloud"):
            from agents.nexus_prime.orchestrator import vision_blueprint

            result = vision_blueprint(state)
        assert result == state

    # VB8
    def test_falls_back_to_settings_owner_space(self, tmp_path):
        # space_name is empty in payload — should use settings.chat.owner_space
        _, _, _, mock_card = self._run(tmp_path, space_name="")
        # Card should still be sent (to settings.chat.owner_space = spaces/OWNER_SPACE)
        mock_card.assert_called_once()
        call_kwargs = mock_card.call_args
        assert "spaces/OWNER_SPACE" in str(call_kwargs)


# ── TestIteratePlanNode ───────────────────────────────────────────────────────


class TestIteratePlanNode:
    """Tests for agents.nexus_prime.orchestrator.iterate_plan()."""

    def _run(
        self,
        tmp_path,
        constraint_text="No automated discounts",
        blueprint_id="bp-1",
        existing_constraints=None,
        doc_id=None,
    ):
        _make_settings(tmp_path)
        from models import MessageType

        msg = _make_a2a(
            MessageType.PLAN_REVIEW,
            {
                "blueprint_id": blueprint_id,
                "constraint_text": constraint_text,
                "comment_author": "owner@example.com",
            },
        )
        active = {blueprint_id: doc_id} if doc_id else {}
        state = _base_state(
            {
                "incoming_message": msg,
                "active_blueprints": active,
                "blueprint_constraints": list(existing_constraints or []),
            }
        )

        mock_append = MagicMock()
        mock_compaction = MagicMock(return_value="Compacted paragraph.")
        with (
            patch("tools.google_docs.append_content", mock_append),
            patch("agents.nexus_prime.orchestrator._run_compaction", mock_compaction),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            from agents.nexus_prime.orchestrator import iterate_plan

            result = iterate_plan(state)
        return result, mock_append, mock_compaction

    # IP1
    def test_constraint_appended(self, tmp_path):
        result, _, _ = self._run(tmp_path)
        constraints = result["blueprint_constraints"]
        assert any("No automated discounts" in c.get("text", "") for c in constraints)

    # IP2
    def test_empty_constraint_text_skips(self, tmp_path):
        _make_settings(tmp_path)
        from models import MessageType

        msg = _make_a2a(
            MessageType.PLAN_REVIEW,
            {
                "blueprint_id": "bp-1",
                "constraint_text": "",
            },
        )
        state = _base_state({"incoming_message": msg, "blueprint_constraints": []})
        with patch("agents.nexus_prime.orchestrator._log_cloud"):
            from agents.nexus_prime.orchestrator import iterate_plan

            result = iterate_plan(state)
        assert result["blueprint_constraints"] == []

    # IP3
    def test_compaction_triggers_at_threshold(self, tmp_path):
        from agents.nexus_prime.orchestrator import _COMPACTION_THRESHOLD

        existing = [
            {
                "blueprint_id": "bp-1",
                "text": f"C{i}",
                "comment_author": "a",
                "comment_timestamp": "t",
            }
            for i in range(_COMPACTION_THRESHOLD - 1)
        ]
        result, _, mock_compaction = self._run(
            tmp_path, existing_constraints=existing, blueprint_id="bp-1"
        )
        mock_compaction.assert_called_once()

    # IP4
    def test_after_compaction_constraints_reduced(self, tmp_path):
        from agents.nexus_prime.orchestrator import _COMPACTION_THRESHOLD

        existing = [
            {
                "blueprint_id": "bp-1",
                "text": f"C{i}",
                "comment_author": "a",
                "comment_timestamp": "t",
            }
            for i in range(_COMPACTION_THRESHOLD - 1)
        ]
        result, _, _ = self._run(tmp_path, existing_constraints=existing, blueprint_id="bp-1")
        bp_constraints = [
            c for c in result["blueprint_constraints"] if c.get("blueprint_id") == "bp-1"
        ]
        # After compaction, all bp-1 constraints replaced by exactly 1 compacted entry
        assert len(bp_constraints) == 1
        assert "COMPACTED_CONSTRAINTS" in bp_constraints[0]["text"]

    # IP5
    def test_append_content_called_when_doc_known(self, tmp_path):
        _, mock_append, _ = self._run(tmp_path, blueprint_id="bp-1", doc_id="doc-123")
        mock_append.assert_called_once()
        call_kwargs = mock_append.call_args
        assert "doc-123" in str(call_kwargs)

    # IP6
    def test_no_incoming_message_returns_unchanged(self, tmp_path):
        _make_settings(tmp_path)
        state = _base_state()
        with patch("agents.nexus_prime.orchestrator._log_cloud"):
            from agents.nexus_prime.orchestrator import iterate_plan

            result = iterate_plan(state)
        assert result == state


# ── TestRunCompaction ─────────────────────────────────────────────────────────


class TestRunCompaction:
    """Tests for agents.nexus_prime.orchestrator._run_compaction()."""

    def _constraints(self, n=5):
        return [
            {
                "blueprint_id": "bp-1",
                "text": f"Constraint {i}",
                "comment_author": "owner",
                "comment_timestamp": "2026-01-01T00:00:00Z",
            }
            for i in range(n)
        ]

    # RC1
    def test_bq_insert_called_with_correct_fields(self, tmp_path):
        _make_settings(tmp_path)
        state = _base_state()
        mock_insert = MagicMock()
        with (
            patch(
                "agents.nexus_prime.orchestrator._call_model",
                return_value=_fake_model_resp("Compacted."),
            ),
            patch("tools.bigquery.insert_rows", mock_insert),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            from agents.nexus_prime.orchestrator import _run_compaction

            _run_compaction(state, "bp-1", self._constraints(3))

        mock_insert.assert_called_once()
        rows = mock_insert.call_args[0][1]  # positional arg: rows list
        assert len(rows) == 3
        assert all("blueprint_id" in r and "constraint_text" in r for r in rows)

    # RC2
    def test_returns_compacted_text(self, tmp_path):
        _make_settings(tmp_path)
        state = _base_state()
        with (
            patch(
                "agents.nexus_prime.orchestrator._call_model",
                return_value=_fake_model_resp("Summarised result."),
            ),
            patch("tools.bigquery.insert_rows"),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            from agents.nexus_prime.orchestrator import _run_compaction

            result = _run_compaction(state, "bp-1", self._constraints(2))
        assert result == "Summarised result."

    # RC3
    def test_bq_failure_is_graceful(self, tmp_path):
        _make_settings(tmp_path)
        state = _base_state()
        with (
            patch(
                "agents.nexus_prime.orchestrator._call_model", return_value=_fake_model_resp("OK.")
            ),
            patch("tools.bigquery.insert_rows", side_effect=RuntimeError("BQ down")),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            from agents.nexus_prime.orchestrator import _run_compaction

            result = _run_compaction(state, "bp-1", self._constraints(2))
        assert result == "OK."


# ── TestHandlePollComments ────────────────────────────────────────────────────


class TestHandlePollComments:
    """Tests for agents.nexus_prime.orchestrator.handle_poll_comments()."""

    _INCUBATOR_ROWS = [
        {"id": "bp-1", "doc_id": "doc-111", "status": "Pending Review"},
        {"id": "bp-2", "doc_id": "doc-222", "status": "Pending Review"},
    ]

    def _run(self, tmp_path, incubator=None, comments_by_doc=None):
        _make_settings(tmp_path)
        incubator_rows = incubator if incubator is not None else self._INCUBATOR_ROWS
        comments_map = comments_by_doc or {"doc-111": [], "doc-222": []}

        def _fake_list_comments(doc_id, project_id):
            return comments_map.get(doc_id, [])

        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.get_all_records", return_value=incubator_rows),
            patch("tools.google_docs.list_comments", side_effect=_fake_list_comments),
            patch("tools.pubsub.publish") as mock_publish,
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            from agents.nexus_prime.orchestrator import handle_poll_comments

            result = asyncio.run(handle_poll_comments("test-project"))
        return result, mock_publish

    # PC1
    def test_publishes_comment_received_for_unresolved(self, tmp_path):
        comments_map = {
            "doc-111": [
                {
                    "id": "c1",
                    "content": "Make it premium",
                    "resolved": False,
                    "author": "owner",
                    "created_time": "2026-01-01T00:00:00Z",
                }
            ],
            "doc-222": [],
        }
        result, mock_publish = self._run(tmp_path, comments_by_doc=comments_map)
        assert result["comments_published"] == 1
        mock_publish.assert_called_once()
        call_kwargs = mock_publish.call_args[0]  # positional: topic, message, project_id
        assert call_kwargs[0] == "agent.nexus-prime.events"

    # PC2
    def test_skips_resolved_comments(self, tmp_path):
        comments_map = {
            "doc-111": [
                {
                    "id": "c1",
                    "content": "Done",
                    "resolved": True,
                    "author": "owner",
                    "created_time": "2026-01-01",
                }
            ],
            "doc-222": [],
        }
        result, mock_publish = self._run(tmp_path, comments_by_doc=comments_map)
        assert result["comments_published"] == 0
        mock_publish.assert_not_called()

    # PC3
    def test_noop_when_incubator_empty(self, tmp_path):
        result, mock_publish = self._run(tmp_path, incubator=[])
        assert result["docs_polled"] == 0
        assert result["comments_published"] == 0
        mock_publish.assert_not_called()

    # PC4
    def test_docs_list_comments_failure_is_graceful(self, tmp_path):
        _make_settings(tmp_path)
        with (
            patch("tools.google_sheets.get_all_records", return_value=self._INCUBATOR_ROWS),
            patch("tools.google_docs.list_comments", side_effect=RuntimeError("Docs API down")),
            patch("tools.pubsub.publish"),
            patch("agents.nexus_prime.orchestrator._log_cloud"),
        ):
            from agents.nexus_prime.orchestrator import handle_poll_comments

            result = asyncio.run(handle_poll_comments("test-project"))
        assert result["errors"] == 2  # one per doc
        assert result["comments_published"] == 0


# ── TestVisionEndpoint ────────────────────────────────────────────────────────


class TestVisionEndpoint:
    """Tests for POST /vision in main.py."""

    def _client(self, tmp_path, agent_name="nexus-prime"):
        """Reload main with the specified AGENT_NAME environment variable."""
        import importlib

        import main as main_mod

        _make_settings(tmp_path)
        with patch.dict(os.environ, {"AGENT_NAME": agent_name, "GCP_PROJECT_ID": "test-project"}):
            importlib.reload(main_mod)
        main_mod._agent_instance = None
        return TestClient(main_mod.app, raise_server_exceptions=False), main_mod

    def _client_with_mock_agent(self, tmp_path, agent_name="nexus-prime"):
        client, main_mod = self._client(tmp_path, agent_name)
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=MagicMock(task_id="task-xyz"))
        main_mod._agent_instance = mock_agent
        return client, main_mod

    # E1
    def test_returns_200_with_task_id(self, tmp_path):
        client, _ = self._client_with_mock_agent(tmp_path)
        resp = client.post(
            "/vision",
            json={"vision_text": "Build a loyalty programme", "submitted_by": "owner@example.com"},
            headers=_AUTH_HEADER,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert "task_id" in resp.json()

    # E2
    def test_returns_404_for_non_nexus(self, tmp_path):
        client, _ = self._client(tmp_path, agent_name="beacon")
        resp = client.post(
            "/vision",
            json={"vision_text": "Some idea"},
            headers=_AUTH_HEADER,
        )
        assert resp.status_code == 404

    # E3
    def test_returns_401_without_auth(self, tmp_path):
        client, _ = self._client_with_mock_agent(tmp_path)
        resp = client.post("/vision", json={"vision_text": "Some idea"})
        assert resp.status_code == 401

    # E4
    def test_returns_400_missing_vision_text(self, tmp_path):
        client, _ = self._client_with_mock_agent(tmp_path)
        resp = client.post("/vision", json={"submitted_by": "owner"}, headers=_AUTH_HEADER)
        assert resp.status_code == 400

    # E5
    def test_correct_payload_forwarded(self, tmp_path):
        client, main_mod = self._client_with_mock_agent(tmp_path)
        resp = client.post(
            "/vision",
            json={
                "vision_text": "Build a tool",
                "submitted_by": "alice@example.com",
                "space_name": "spaces/XYZ",
            },
            headers=_AUTH_HEADER,
        )
        assert resp.status_code == 200
        call_arg = main_mod._agent_instance.run.call_args[0][0]
        import base64
        import json

        data = json.loads(base64.b64decode(call_arg["message"]["data"]))
        assert data["payload"]["vision_text"] == "Build a tool"
        assert data["payload"]["submitted_by"] == "alice@example.com"
        assert data["payload"]["space_name"] == "spaces/XYZ"


# ── TestPollCommentsEndpoint ──────────────────────────────────────────────────


class TestPollCommentsEndpoint:
    """Tests for POST /poll-comments in main.py."""

    _POLL_RESULT = {
        "docs_polled": 2,
        "comments_published": 1,
        "errors": 0,
        "task_id": "task-poll-1",
    }

    def _client(self, tmp_path, agent_name="nexus-prime"):
        import importlib

        import main as main_mod

        _make_settings(tmp_path)
        with patch.dict(os.environ, {"AGENT_NAME": agent_name, "GCP_PROJECT_ID": "test-project"}):
            importlib.reload(main_mod)
        main_mod._agent_instance = None
        return TestClient(main_mod.app, raise_server_exceptions=False), main_mod

    # P1
    def test_returns_200_with_docs_polled(self, tmp_path):
        client, _ = self._client(tmp_path)
        with patch(
            "agents.nexus_prime.orchestrator.handle_poll_comments",
            new=AsyncMock(return_value=self._POLL_RESULT),
        ):
            resp = client.post("/poll-comments", headers=_AUTH_HEADER)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json()["docs_polled"] == 2

    # P2
    def test_returns_404_for_non_nexus(self, tmp_path):
        client, _ = self._client(tmp_path, agent_name="ledger")
        resp = client.post("/poll-comments", headers=_AUTH_HEADER)
        assert resp.status_code == 404

    # P3
    def test_returns_401_without_auth(self, tmp_path):
        client, _ = self._client(tmp_path)
        resp = client.post("/poll-comments")
        assert resp.status_code == 401

    # P4
    def test_response_includes_comments_published(self, tmp_path):
        client, _ = self._client(tmp_path)
        with patch(
            "agents.nexus_prime.orchestrator.handle_poll_comments",
            new=AsyncMock(return_value=self._POLL_RESULT),
        ):
            resp = client.post("/poll-comments", headers=_AUTH_HEADER)
        assert resp.json()["comments_published"] == 1
