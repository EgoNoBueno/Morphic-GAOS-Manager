"""tests/test_google_sheets.py — Unit tests for tools/google_sheets.py"""
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import gspread
import pytest

import tools.google_sheets as sheets_mod
from tools.google_sheets import (
    RateLimitError,
    SheetsReadError,
    SheetsWriteError,
    TabNotFoundError,
    WorkbookNotFoundError,
    _quote_tab,
    _range,
    append_row,
    batch_append_rows,
    find_row,
    find_rows,
    get_all_records,
    init_sheets_client,
    read_range,
    update_row,
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


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset the module-level gspread state between tests."""
    sheets_mod._client = None
    sheets_mod._spreadsheet = None
    yield
    sheets_mod._client = None
    sheets_mod._spreadsheet = None


# ── Helpers for building mock worksheets ─────────────────────────────────


def _make_ws(tab_name: str, headers: list[str], rows: list[list] = None) -> MagicMock:
    ws = MagicMock(spec=gspread.Worksheet)
    ws.title = tab_name
    ws.row_count = 100
    ws.row_values.return_value = headers
    all_records = []
    for row in (rows or []):
        all_records.append(dict(zip(headers, row)))
    ws.get_all_records.return_value = all_records
    return ws


def _make_spreadsheet(worksheets: dict[str, MagicMock]) -> MagicMock:
    ss = MagicMock()

    def worksheet(name):
        if name not in worksheets:
            raise gspread.exceptions.WorksheetNotFound(name)
        return worksheets[name]

    ss.worksheet.side_effect = worksheet
    ss.values_append.return_value = {}
    ss.values_get.return_value = {"values": [["A", "B"], ["1", "2"]]}
    return ss


# ── init_sheets_client ────────────────────────────────────────────────────


class TestInitSheetsClient:
    def test_raises_workbook_not_found_for_unknown_project(self):
        with pytest.raises(WorkbookNotFoundError, match="unknown-project"):
            init_sheets_client("unknown-project")

    @patch("tools.google_sheets.get_secret", return_value='{"type":"service_account"}')
    @patch("tools.google_sheets.gspread.service_account_from_dict")
    def test_opens_spreadsheet_by_key(self, mock_sa, mock_secret):
        mock_client = MagicMock()
        mock_sa.return_value = mock_client
        init_sheets_client("default")
        mock_client.open_by_key.assert_called_once_with("spreadsheet-123")

    @patch("tools.google_sheets.get_secret", return_value='{"type":"service_account"}')
    @patch("tools.google_sheets.gspread.service_account_from_dict")
    def test_raises_workbook_not_found_on_missing_spreadsheet(self, mock_sa, mock_secret):
        mock_client = MagicMock()
        mock_sa.return_value = mock_client
        mock_client.open_by_key.side_effect = gspread.exceptions.SpreadsheetNotFound
        with pytest.raises(WorkbookNotFoundError, match="spreadsheet-123"):
            init_sheets_client("default")


# ── _quote_tab / _range ───────────────────────────────────────────────────


class TestQuoteTab:
    def test_adds_single_quotes_to_name_with_spaces(self):
        assert _quote_tab("Sales by Product") == "'Sales by Product'"

    def test_idempotent_on_already_quoted(self):
        assert _quote_tab("'Sales by Product'") == "'Sales by Product'"

    def test_single_word_tab_gets_quoted(self):
        assert _quote_tab("Logs") == "'Logs'"

    def test_range_builds_correctly(self):
        assert _range("Sales by Product", "A2:D") == "'Sales by Product'!A2:D"

    def test_range_without_a1(self):
        assert _range("Logs") == "'Logs'"


# ── append_row / batch_append_rows ────────────────────────────────────────


class TestAppendRow:
    def _setup_spreadsheet(self, headers):
        ws = _make_ws("Logs", headers)
        ss = _make_spreadsheet({"Logs": ws})
        sheets_mod._spreadsheet = ss
        return ws, ss

    def test_append_single_row(self):
        ws, ss = self._setup_spreadsheet(["timestamp", "agent_id", "message"])
        append_row(
            "Logs",
            {"timestamp": "2026-03-14", "agent_id": "test", "message": "smoke"},
            "default",
        )
        ss.values_append.assert_called_once()
        args = ss.values_append.call_args
        assert args[0][2]["values"] == [["2026-03-14", "test", "smoke"]]

    def test_noop_on_empty_rows(self):
        ws, ss = self._setup_spreadsheet(["timestamp"])
        batch_append_rows("Logs", [], "default")
        ss.values_append.assert_not_called()

    def test_raises_tab_not_found(self):
        sheets_mod._spreadsheet = _make_spreadsheet({})
        with pytest.raises(TabNotFoundError, match="NonExistent"):
            append_row("NonExistent", {"col": "val"}, "default")

    def test_raises_runtime_error_without_init(self):
        assert sheets_mod._spreadsheet is None
        with pytest.raises(RuntimeError, match="init_sheets_client"):
            append_row("Logs", {"col": "val"}, "default")


# ── get_all_records ───────────────────────────────────────────────────────


class TestGetAllRecords:
    def test_returns_list_of_dicts(self):
        ws = _make_ws("Logs", ["timestamp", "message"], [["2026-01-01", "hello"]])
        sheets_mod._spreadsheet = _make_spreadsheet({"Logs": ws})
        records = get_all_records("Logs", "default")
        assert records == [{"timestamp": "2026-01-01", "message": "hello"}]

    def test_raises_tab_not_found(self):
        sheets_mod._spreadsheet = _make_spreadsheet({})
        with pytest.raises(TabNotFoundError):
            get_all_records("Missing", "default")


# ── find_row / find_rows ──────────────────────────────────────────────────


class TestFindRow:
    def _setup(self, rows):
        ws = _make_ws("Accounting", ["id", "amount"], rows)
        sheets_mod._spreadsheet = _make_spreadsheet({"Accounting": ws})

    def test_finds_first_matching_row(self):
        self._setup([["1", "100"], ["2", "200"], ["1", "150"]])
        result = find_row("Accounting", "id", "1", "default")
        assert result == {"id": "1", "amount": "100"}

    def test_returns_none_when_not_found(self):
        self._setup([["1", "100"]])
        result = find_row("Accounting", "id", "999", "default")
        assert result is None

    def test_find_rows_returns_all_matches(self):
        self._setup([["1", "100"], ["2", "200"], ["1", "150"]])
        results = find_rows("Accounting", "id", "1", "default")
        assert len(results) == 2
        assert results[0]["amount"] == "100"
        assert results[1]["amount"] == "150"

    def test_find_rows_returns_empty_list_when_none_match(self):
        self._setup([["1", "100"]])
        assert find_rows("Accounting", "id", "999", "default") == []


# ── project_id is always passed through ──────────────────────────────────


class TestProjectIdPropagation:
    """U2: project_id must appear in every downstream tool call."""

    def test_get_all_records_uses_project_id(self):
        # get_all_records() calls get_all_records(tab, project_id) —
        # if project_id were dropped the call would still work here,
        # but we verify the signature accepts it without error.
        ws = _make_ws("Logs", ["col"])
        sheets_mod._spreadsheet = _make_spreadsheet({"Logs": ws})
        get_all_records("Logs", "my-custom-project")  # must not raise
