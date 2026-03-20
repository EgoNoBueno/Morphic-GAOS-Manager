"""tests/test_memory.py — Unit tests for tools/memory.py"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from tools.memory import (
    MemoryBankError,
    flush_observations,
    load_domain_memory,
    query_episodic,
    write_approved_memory,
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


@pytest.fixture(autouse=True)
def load_test_settings(tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(SETTINGS_YAML)
    import config

    config._reset_for_testing()
    config.load_settings(cfg)
    yield
    config._reset_for_testing()


# ── Vertex AI Memory Bank mock ─────────────────────────────────────────────


@pytest.fixture()
def mock_memory_bank():
    """
    Inject a fake vertexai.preview.memory module so tests never need real
    Vertex AI credentials.  Yields (MockClass, mock_instance).
    """
    mock_instance = MagicMock()
    mock_cls = MagicMock(return_value=mock_instance)
    fake_module = MagicMock()
    fake_module.MemoryBankClient = mock_cls

    with patch.dict(sys.modules, {"vertexai.preview.memory": fake_module}):
        yield mock_cls, mock_instance


# ── query_episodic ─────────────────────────────────────────────────────────


class TestQueryEpisodic:
    def test_returns_list_of_row_dicts_on_success(self):
        fake_row = {"task_id": "t1", "status": "success", "result_summary": "done"}
        mock_client = MagicMock()
        mock_client.query.return_value = [fake_row]

        with patch("google.cloud.bigquery.Client", return_value=mock_client):
            result = query_episodic("beacon", "test-project", "research", limit=3)

        assert result == [fake_row]

    def test_raises_memory_bank_error_on_query_failure(self):
        mock_client = MagicMock()
        mock_client.query.side_effect = Exception("BQ unreachable")

        with patch("google.cloud.bigquery.Client", return_value=mock_client):
            with pytest.raises(MemoryBankError, match="query_episodic"):
                query_episodic("beacon", "test-project", "research")

    def test_passes_agent_id_and_task_type_as_query_params(self):
        mock_client = MagicMock()
        mock_client.query.return_value = []

        with patch("google.cloud.bigquery.Client", return_value=mock_client):
            query_episodic("ledger", "test-project", "invoice_parse", limit=5)

        # Verify query was called with a job config (params are set)
        assert mock_client.query.call_count == 1
        _, kwargs = mock_client.query.call_args
        job_cfg = kwargs.get("job_config") or mock_client.query.call_args[0][1]
        param_names = [p.name for p in job_cfg.query_parameters]
        assert "agent_id" in param_names
        assert "task_type" in param_names
        assert "limit" in param_names


# ── flush_observations ─────────────────────────────────────────────────────


class TestFlushObservations:
    def test_empty_list_is_a_no_op(self):
        with patch("tools.google_sheets.find_row") as mock_find:
            flush_observations([], "test-project")
        mock_find.assert_not_called()

    def test_duplicate_content_hash_is_skipped(self):
        obs = [{"content_hash": "abc123", "content": "already stored"}]

        with patch("tools.google_sheets.find_row", return_value={"row_num": 5}):
            with patch("tools.google_sheets.batch_append_rows") as mock_append:
                flush_observations(obs, "test-project")

        mock_append.assert_not_called()

    def test_new_observation_is_appended(self):
        obs = [{"content_hash": "new_hash_xyz", "content": "brand new fact"}]

        with patch("tools.google_sheets.find_row", return_value=None):
            with patch("tools.google_sheets.batch_append_rows") as mock_append:
                flush_observations(obs, "test-project")

        mock_append.assert_called_once_with("Pending_Knowledge", obs, "test-project")

    def test_observation_without_hash_bypasses_dedup_and_is_appended(self):
        obs = [{"content": "no hash on this one"}]

        with patch("tools.google_sheets.find_row") as mock_find:
            with patch("tools.google_sheets.batch_append_rows") as mock_append:
                flush_observations(obs, "test-project")

        mock_find.assert_not_called()
        mock_append.assert_called_once()

    def test_mixed_new_and_duplicate_only_appends_new(self):
        obs = [
            {"content_hash": "dup", "content": "existing"},
            {"content_hash": "new", "content": "fresh"},
        ]

        def _find(tab, col, val, pid):
            return {"row_num": 2} if val == "dup" else None

        with patch("tools.google_sheets.find_row", side_effect=_find):
            with patch("tools.google_sheets.batch_append_rows") as mock_append:
                flush_observations(obs, "test-project")

        called_rows = mock_append.call_args[0][1]
        assert len(called_rows) == 1
        assert called_rows[0]["content_hash"] == "new"

    def test_raises_memory_bank_error_on_write_failure(self):
        obs = [{"content_hash": "h1", "content": "fact"}]

        with patch("tools.google_sheets.find_row", return_value=None):
            with patch(
                "tools.google_sheets.batch_append_rows",
                side_effect=Exception("sheets error"),
            ):
                with pytest.raises(MemoryBankError, match="flush_observations"):
                    flush_observations(obs, "test-project")


# ── load_domain_memory ─────────────────────────────────────────────────────


def _make_entry(knowledge_type: str, content: str) -> MagicMock:
    e = MagicMock()
    e.knowledge_type = knowledge_type
    e.memory_id = f"mid-{knowledge_type}-{content[:8]}"
    e.content = content
    e.tags = ["test"]
    return e


class TestLoadDomainMemory:
    def test_groups_entries_by_knowledge_type(self, mock_memory_bank):
        mock_cls, mock_instance = mock_memory_bank
        mock_instance.list.return_value = [
            _make_entry("fact", "revenue is monthly"),
            _make_entry("rule", "never miss a filing"),
            _make_entry("fact", "Q4 ends Dec 31"),
            _make_entry("pattern", "invoices arrive Fridays"),
        ]

        ctx = load_domain_memory("ledger", "test-project")

        assert len(ctx["fact"]) == 2
        assert len(ctx["rule"]) == 1
        assert len(ctx["pattern"]) == 1
        assert ctx["preference"] == []

    def test_returns_correct_entry_fields(self, mock_memory_bank):
        mock_cls, mock_instance = mock_memory_bank
        entry = _make_entry("fact", "tax deadline is April 15")
        entry.memory_id = "mem-001"
        entry.tags = ["tax", "deadline"]
        mock_instance.list.return_value = [entry]

        ctx = load_domain_memory("ledger", "test-project")

        assert ctx["fact"][0] == {
            "memory_id": "mem-001",
            "content": "tax deadline is April 15",
            "tags": ["tax", "deadline"],
        }

    def test_empty_memory_bank_returns_empty_buckets(self, mock_memory_bank):
        mock_cls, mock_instance = mock_memory_bank
        mock_instance.list.return_value = []

        ctx = load_domain_memory("beacon", "test-project")

        assert ctx == {"fact": [], "pattern": [], "rule": [], "preference": []}

    def test_raises_memory_bank_error_on_api_failure(self, mock_memory_bank):
        mock_cls, mock_instance = mock_memory_bank
        mock_instance.list.side_effect = Exception("Vertex API error")

        with pytest.raises(MemoryBankError, match="load_domain_memory"):
            load_domain_memory("ledger", "test-project")

    def test_passes_agent_id_filter_to_memory_bank(self, mock_memory_bank):
        mock_cls, mock_instance = mock_memory_bank
        mock_instance.list.return_value = []

        load_domain_memory("beacon", "test-project")

        call_kwargs = mock_instance.list.call_args[1]
        filters = call_kwargs.get("filters") or mock_instance.list.call_args[0][0]
        assert filters["agent_id"] == "beacon"


# ── write_approved_memory ──────────────────────────────────────────────────


class TestWriteApprovedMemory:
    def _make_entry(self, **overrides):
        from models import MemoryEntry

        defaults = dict(
            agent_id="beacon",
            project_id="test-project",
            knowledge_type="fact",
            domain="market",
            content="Market closes Friday at 4pm",
        )
        return MemoryEntry(**{**defaults, **overrides})

    def test_creates_entry_and_returns_memory_id(self, mock_memory_bank):
        mock_cls, mock_instance = mock_memory_bank
        mock_record = MagicMock()
        mock_record.memory_id = "mem-999"
        mock_instance.create.return_value = mock_record

        result = write_approved_memory(self._make_entry(), "test-project")

        assert result == "mem-999"
        mock_instance.update.assert_not_called()

    def test_supersedes_marks_old_entry_inactive_before_creating(self, mock_memory_bank):
        mock_cls, mock_instance = mock_memory_bank
        mock_record = MagicMock()
        mock_record.memory_id = "mem-new"
        mock_instance.create.return_value = mock_record

        entry = self._make_entry(content="Updated market hours", supersedes="mem-old-123")
        write_approved_memory(entry, "test-project")

        mock_instance.update.assert_called_once_with("mem-old-123", {"active": False})
        mock_instance.create.assert_called_once()

    def test_raises_memory_bank_error_on_api_failure(self, mock_memory_bank):
        mock_cls, mock_instance = mock_memory_bank
        mock_instance.create.side_effect = Exception("Vertex write error")

        with pytest.raises(MemoryBankError, match="write_approved_memory"):
            write_approved_memory(self._make_entry(), "test-project")
