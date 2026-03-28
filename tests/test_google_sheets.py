"""tests/test_google_sheets.py — Unit tests for tools/google_sheets.py"""

from unittest.mock import MagicMock, patch

import gspread
import pytest

import tools.google_sheets as sheets_mod
from tools.google_sheets import (
    RateLimitError,
    RowNotFoundError,
    SheetsWriteError,
    TabNotFoundError,
    WorkbookNotFoundError,
    _quote_tab,
    _range,
    _retry,
    _TokenBucket,
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
    for row in rows or []:
        all_records.append(dict(zip(headers, row, strict=False)))
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

    @patch("tools.google_sheets.gspread.Client")
    @patch("tools.google_sheets.google.auth.default")
    def test_opens_spreadsheet_by_key(self, mock_auth, mock_client_cls):
        mock_creds = MagicMock()
        mock_auth.return_value = (mock_creds, None)
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        init_sheets_client("default")
        mock_client.open_by_key.assert_called_once_with("spreadsheet-123")

    @patch("tools.google_sheets.gspread.Client")
    @patch("tools.google_sheets.google.auth.default")
    def test_raises_workbook_not_found_on_missing_spreadsheet(self, mock_auth, mock_client_cls):
        mock_creds = MagicMock()
        mock_auth.return_value = (mock_creds, None)
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
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
        # batch_append_rows now uses ws.insert_rows(values, 2, ...) so the call
        # lands on the worksheet mock, not on the spreadsheet mock.
        ws.insert_rows.assert_called_once()
        values_arg = ws.insert_rows.call_args[0][0]
        assert values_arg == [["2026-03-14", "test", "smoke"]]

    def test_noop_on_empty_rows(self):
        ws, ss = self._setup_spreadsheet(["timestamp"])
        batch_append_rows("Logs", [], "default")
        ws.insert_rows.assert_not_called()

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
        ws = _make_ws("Logs", ["col"])
        sheets_mod._spreadsheet = _make_spreadsheet({"Logs": ws})
        get_all_records("Logs", "my-custom-project")  # must not raise


# ── update_row ────────────────────────────────────────────────────────────


class TestUpdateRow:
    def _setup(self, headers, rows=None):
        ws = _make_ws("Approvals", headers, rows or [])
        ws.row_count = 100
        sheets_mod._spreadsheet = _make_spreadsheet({"Approvals": ws})
        return ws

    def test_update_by_int_row_index(self):
        ws = self._setup(["ID", "Status", "Notes"])
        update_row("Approvals", 2, {"Status": "Approved"}, "default")
        ws.update_cells.assert_called_once()

    def test_update_by_string_id_lookup(self):
        ws = self._setup(["ID", "Status"])
        cell_mock = MagicMock()
        cell_mock.row = 3
        ws.find.return_value = cell_mock
        update_row("Approvals", "uuid-abc", {"Status": "Deployed"}, "default")
        ws.find.assert_called_once_with("uuid-abc", in_column=1)
        ws.update_cells.assert_called_once()

    def test_update_string_id_not_found_raises(self):
        ws = self._setup(["ID", "Status"])
        ws.find.return_value = None  # gspread 6.x returns None, not an exception
        with pytest.raises(RowNotFoundError, match="uuid-missing"):
            update_row("Approvals", "uuid-missing", {"Status": "x"}, "default")

    def test_update_int_row_out_of_range_raises(self):
        ws = self._setup(["ID", "Status"])
        ws.row_count = 5
        with pytest.raises(RowNotFoundError, match="999"):
            update_row("Approvals", 999, {"Status": "x"}, "default")

    def test_update_unknown_column_raises(self):
        self._setup(["ID", "Status"])
        with pytest.raises(SheetsWriteError, match="NonExistent"):
            update_row("Approvals", 2, {"NonExistent": "x"}, "default")

    def test_raises_tab_not_found(self):
        sheets_mod._spreadsheet = _make_spreadsheet({})
        with pytest.raises(TabNotFoundError):
            update_row("Missing", 2, {"col": "val"}, "default")


# ── read_range ────────────────────────────────────────────────────────────


class TestReadRange:
    def test_returns_nested_list(self):
        ws = _make_ws("Sheet1", ["A", "B"])
        ss = _make_spreadsheet({"Sheet1": ws})
        ss.values_get.return_value = {"values": [["h1", "h2"], ["v1", "v2"]]}
        sheets_mod._spreadsheet = ss
        result = read_range("Sheet1", "A1:B2", "default")
        assert result == [["h1", "h2"], ["v1", "v2"]]

    def test_returns_empty_list_when_no_values(self):
        ws = _make_ws("Sheet1", ["A"])
        ss = _make_spreadsheet({"Sheet1": ws})
        ss.values_get.return_value = {}
        sheets_mod._spreadsheet = ss
        result = read_range("Sheet1", "A1:A10", "default")
        assert result == []

    def test_quotes_tab_name_in_range(self):
        ws = _make_ws("My Sheet", ["A"])
        ss = _make_spreadsheet({"My Sheet": ws})
        ss.values_get.return_value = {"values": []}
        sheets_mod._spreadsheet = ss
        read_range("My Sheet", "A1:A5", "default")
        call_args = ss.values_get.call_args[0][0]
        assert call_args == "'My Sheet'!A1:A5"

    def test_raises_tab_not_found(self):
        sheets_mod._spreadsheet = _make_spreadsheet({})
        with pytest.raises(TabNotFoundError):
            read_range("Missing", "A1:A5", "default")


# ── _retry helper ─────────────────────────────────────────────────────────


class TestRetry:
    def test_returns_on_first_success(self):
        fn = MagicMock(return_value="ok")
        assert _retry(fn, "arg1") == "ok"
        assert fn.call_count == 1

    def test_retries_on_429(self):
        api_exc = gspread.exceptions.APIError(MagicMock())
        api_exc.response = MagicMock()
        api_exc.response.status_code = 429
        fn = MagicMock(side_effect=[api_exc, api_exc, "success"])
        assert _retry(fn) == "success"
        assert fn.call_count == 3

    def test_retries_on_500(self):
        api_exc = gspread.exceptions.APIError(MagicMock())
        api_exc.response = MagicMock()
        api_exc.response.status_code = 500
        fn = MagicMock(side_effect=[api_exc, "ok"])
        assert _retry(fn) == "ok"

    def test_raises_rate_limit_after_3_retries(self):
        api_exc = gspread.exceptions.APIError(MagicMock())
        api_exc.response = MagicMock()
        api_exc.response.status_code = 429
        fn = MagicMock(side_effect=[api_exc, api_exc, api_exc])
        with pytest.raises(RateLimitError, match="3 retries"):
            _retry(fn)

    def test_non_retryable_error_raises_immediately(self):
        api_exc = gspread.exceptions.APIError(MagicMock())
        api_exc.response = MagicMock()
        api_exc.response.status_code = 403
        fn = MagicMock(side_effect=api_exc)
        with pytest.raises(gspread.exceptions.APIError):
            _retry(fn)
        assert fn.call_count == 1


# ── _TokenBucket ──────────────────────────────────────────────────────────


class TestTokenBucket:
    def test_initial_consume_succeeds(self):
        bucket = _TokenBucket(rate=5.0, capacity=10.0)
        assert bucket.consume(timeout=0.1) is True

    def test_capacity_is_respected(self):
        bucket = _TokenBucket(rate=0.0, capacity=2.0)  # no refill
        assert bucket.consume(timeout=0.0) is True
        assert bucket.consume(timeout=0.0) is True
        assert bucket.consume(timeout=0.0) is False  # exhausted

    def test_bucket_refills_over_time(self):
        import time as time_mod

        bucket = _TokenBucket(rate=1000.0, capacity=1.0)  # fast refill
        bucket.consume(timeout=0.0)  # drain
        time_mod.sleep(0.015)  # allow refill at 1000 tokens/sec
        assert bucket.consume(timeout=0.0) is True


# ── batch_append_rows uses INSERT_ROWS mode ───────────────────────────────


class TestBatchAppendRowsInsertMode:
    def test_calls_insert_rows_at_row_2(self):
        """Newest-first: rows are inserted at position 2 (below the header)."""
        ws = _make_ws("Logs", ["timestamp", "message"])
        ss = _make_spreadsheet({"Logs": ws})
        sheets_mod._spreadsheet = ss
        batch_append_rows(
            "Logs",
            [{"timestamp": "2026-01-01", "message": "hi"}],
            "default",
        )
        ws.insert_rows.assert_called_once()
        _, row_arg = ws.insert_rows.call_args[0]
        assert row_arg == 2

    def test_values_ordered_by_header(self):
        """Values list must follow tab header column order."""
        ws = _make_ws("Logs", ["timestamp", "message"])
        ss = _make_spreadsheet({"Logs": ws})
        sheets_mod._spreadsheet = ss
        batch_append_rows("Logs", [{"timestamp": "2026-01-01", "message": "hi"}], "default")
        values_arg = ws.insert_rows.call_args[0][0]
        assert values_arg == [["2026-01-01", "hi"]]
