"""
tools/google_sheets.py — Google Sheets accessor for Morphic-G AOS.

All Sheet I/O goes through this module. No agent may call gspread directly.
Call init_sheets_client(project_id) once during the boot sequence before
using any other function.

Key behaviours enforced here:
  - Tab names with spaces are single-quoted in all range strings (Sheets API
    returns 400 otherwise — see GAOS-Tools-Spec.md §3 Tab Name Quoting Rule).
  - Write calls are rate-limited to 300 req/min via a token bucket.
  - 429 and 5xx errors are retried up to 3 times with exponential backoff.
  - batch_append_rows() uses a single API call regardless of row count.

Spec: GAOS-Tools-Spec.md §3
"""

from __future__ import annotations

import threading
import time
from typing import Any

import google.auth
import gspread
import gspread.exceptions

from config import get_settings
from tools import tracked

# ── Error types ────────────────────────────────────────────────────────────


class TabNotFoundError(Exception):
    """Named tab does not exist in the workbook."""


class RowNotFoundError(Exception):
    """Row index is out of range for the given tab."""


class RateLimitError(Exception):
    """Sheets API quota exceeded after retry exhaustion."""


class SheetsReadError(Exception):
    """Unrecoverable read error."""


class SheetsWriteError(Exception):
    """Unrecoverable write error."""


class WorkbookNotFoundError(Exception):
    """Spreadsheet ID not found or inaccessible."""


# ── Module-level state ─────────────────────────────────────────────────────

_client: gspread.Client | None = None
_spreadsheet: gspread.Spreadsheet | None = None


# ── Token bucket — 300 req/min ─────────────────────────────────────────────


class _TokenBucket:
    """Thread-safe token bucket for rate limiting (300 tokens/min = 5/sec)."""

    def __init__(self, rate: float = 5.0, capacity: float = 300.0) -> None:
        self._tokens = capacity
        self._rate = rate
        self._capacity = capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity,
                    self._tokens + (now - self._last) * self._rate,
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.2, remaining))


_bucket = _TokenBucket()


# ── Internal helpers ───────────────────────────────────────────────────────


def _quote_tab(tab: str) -> str:
    """
    Return the tab name wrapped in single quotes for A1 range notation.
    Idempotent — safe to call on already-quoted names.
    """
    stripped = tab.strip("'")
    return f"'{stripped}'"


def _range(tab: str, a1: str = "") -> str:
    """Build a properly quoted range string, e.g. "'Sales by Product'!A2:D"."""
    quoted = _quote_tab(tab)
    return f"{quoted}!{a1}" if a1 else quoted


def _retry(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """
    Run fn(*args, **kwargs) with up to 3 attempts.
    Retries on 429 (quota) and 5xx (server) responses only.
    All other errors propagate immediately.
    """
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as exc:
            code = getattr(exc.response, "status_code", 0)
            if code == 429 or code >= 500:
                last_err = exc
                time.sleep(2**attempt)
                continue
            raise
    raise RateLimitError(f"Sheets API still failing after 3 retries: {last_err}") from last_err


def _get_worksheet(tab: str) -> gspread.Worksheet:
    """Return the gspread Worksheet for the given tab name."""
    if _spreadsheet is None:
        raise RuntimeError("init_sheets_client() must be called before any Sheet operation.")
    try:
        return _spreadsheet.worksheet(tab)
    except gspread.exceptions.WorksheetNotFound as exc:
        raise TabNotFoundError(f"Tab '{tab}' not found in workbook.") from exc


def _col_index(ws: gspread.Worksheet, header: str) -> int:
    """Return the 1-based column index for a header label."""
    headers = ws.row_values(1)
    try:
        return headers.index(header) + 1
    except ValueError as exc:
        raise SheetsReadError(f"Column '{header}' not found in tab headers: {headers}") from exc


# ── Initialisation ─────────────────────────────────────────────────────────


@tracked("google_sheets")
def init_sheets_client(project_id: str) -> None:
    """
    Authenticate gspread via Application Default Credentials (ADC) and open
    the workbook for this project. Must be called before any other function
    in this module.

    Adds retry logic for transient errors.

    Args:
        project_id: The AOS project namespace (matches a key in settings.yaml
                    under 'projects').

    Raises:
        WorkbookNotFoundError: No sheet_id configured for this project_id,
                               or the spreadsheet is inaccessible.
    """
    global _client, _spreadsheet

    settings = get_settings()
    project = settings.get_project(project_id)
    if project is None or not project.sheet_id:
        raise WorkbookNotFoundError(
            f"No sheet_id configured for project_id '{project_id}'. "
            "Add it under 'projects.<project_id>.sheet_id' in config/settings.yaml."
        )

    creds, _ = google.auth.default(
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.file",
        ]
    )
    _client = gspread.Client(auth=creds)

    retries = 3
    for attempt in range(retries):
        try:
            _spreadsheet = _client.open_by_key(project.sheet_id)
            break
        except (gspread.exceptions.APIError, gspread.exceptions.SpreadsheetNotFound) as exc:
            if attempt < retries - 1 and not isinstance(
                exc, gspread.exceptions.SpreadsheetNotFound
            ):
                time.sleep(2**attempt)  # Exponential backoff
            else:
                raise WorkbookNotFoundError(
                    f"Spreadsheet '{project.sheet_id}' not found or inaccessible. "
                    "Ensure the ADC identity has been granted access to the workbook."
                ) from exc


# ── Core functions ─────────────────────────────────────────────────────────


@tracked("google_sheets")
def append_row(tab: str, row: dict[str, Any], project_id: str) -> None:
    """
    Append one row to the named Sheet tab.
    Keys in `row` must match the tab's header row exactly (case-sensitive).

    Prefer batch_append_rows() when writing more than one row.

    Raises:
        TabNotFoundError, RateLimitError, SheetsWriteError.
    """
    batch_append_rows(tab, [row], project_id)


@tracked("google_sheets")
def batch_append_rows(tab: str, rows: list[dict[str, Any]], project_id: str) -> None:
    """
    Insert multiple rows immediately below the header row (row 2) so the newest
    entry always appears at the top of the tab. All rows must have identical key
    sets matching the tab header.

    Internally uses INSERT_DIMENSION + values.update (2 API calls); still counts
    as one token-bucket consumption since throughput is well within 300 req/min.

    Raises:
        TabNotFoundError, RateLimitError, SheetsWriteError.
    """
    if not rows:
        return

    ws = _get_worksheet(tab)
    headers = _retry(ws.row_values, 1)
    if not headers:
        raise SheetsWriteError(f"Tab '{tab}' has no header row.")

    values = []
    for row in rows:
        values.append([str(row.get(h, "")) for h in headers])

    if not _bucket.consume():
        raise RateLimitError("Token bucket exhausted — too many write requests.")

    # Insert at row 2 (immediately below the header) so newest entries appear
    # at the top without requiring any manual scrolling.
    try:
        _retry(ws.insert_rows, values, 2, value_input_option="USER_ENTERED")
    except gspread.exceptions.APIError as exc:
        raise SheetsWriteError(f"Write to '{tab}' failed: {exc}") from exc


@tracked("google_sheets")
def get_all_records(tab: str, project_id: str) -> list[dict[str, Any]]:
    """
    Return all rows in a tab as a list of dicts keyed by the header row.
    Empty rows are excluded. Result is not cached.

    Raises:
        TabNotFoundError, RateLimitError, SheetsReadError.
    """
    ws = _get_worksheet(tab)
    if not _bucket.consume():
        raise RateLimitError("Token bucket exhausted.")
    try:
        return _retry(ws.get_all_records)
    except gspread.exceptions.APIError as exc:
        raise SheetsReadError(f"Read from '{tab}' failed: {exc}") from exc


@tracked("google_sheets")
def read_range(tab: str, a1_range: str, project_id: str) -> list[list[Any]]:
    """
    Return raw cell values for an A1-notation range (e.g. "A2:D50").
    Returns list of rows; each row is a list of cell values (str or "").

    Raises:
        TabNotFoundError, RateLimitError, SheetsReadError.
    """
    _get_worksheet(tab)  # validates tab exists — raises if _spreadsheet is None
    assert _spreadsheet is not None
    if not _bucket.consume():
        raise RateLimitError("Token bucket exhausted.")
    full_range = _range(tab, a1_range)
    try:
        return _retry(_spreadsheet.values_get, full_range).get("values", [])
    except gspread.exceptions.APIError as exc:
        raise SheetsReadError(f"Range read '{full_range}' failed: {exc}") from exc


@tracked("google_sheets")
def update_row(tab: str, row_index: int | str, updates: dict[str, Any], project_id: str) -> None:
    """
    Update specific columns in an existing row.

    `row_index` can be:
    - **int**: 1-based sheet row number (row 1 = header).
    - **str**: A value to look up in the first column (ID) of the tab.
      The function finds the row where col A equals this string.

    `updates` is a dict of {column_header: new_value}.

    Raises:
        TabNotFoundError, RowNotFoundError, RateLimitError, SheetsWriteError.
    """
    ws = _get_worksheet(tab)

    if isinstance(row_index, str):
        # Resolve string ID to sheet row number via gspread cell lookup.
        # gspread 6.x returns None (not an exception) when nothing matches.
        cell = _retry(ws.find, row_index, in_column=1)
        if cell is None:
            raise RowNotFoundError(f"No row with ID='{row_index}' in tab '{tab}'.")
        actual_row = cell.row
    else:
        row_count = ws.row_count
        if row_index < 1 or row_index > row_count:
            raise RowNotFoundError(
                f"Row {row_index} is out of range for tab '{tab}' (1–{row_count})."
            )
        actual_row = row_index

    headers = _retry(ws.row_values, 1)
    cells_to_update = []
    for col_header, value in updates.items():
        if col_header not in headers:
            raise SheetsWriteError(f"Column '{col_header}' not found in tab '{tab}' headers.")
        col_idx = headers.index(col_header) + 1
        cells_to_update.append(gspread.Cell(row=actual_row, col=col_idx, value=str(value)))

    if not cells_to_update:
        return

    if not _bucket.consume():
        raise RateLimitError("Token bucket exhausted.")
    try:
        _retry(ws.update_cells, cells_to_update)
    except gspread.exceptions.APIError as exc:
        raise SheetsWriteError(f"Row update on '{tab}' row {row_index} failed: {exc}") from exc


@tracked("google_sheets")
def find_row(tab: str, column: str, value: str, project_id: str) -> dict[str, Any] | None:
    """
    Return the first row where `column` equals `value`, as a dict.
    Returns None if no matching row exists. Case-sensitive match.

    Raises:
        TabNotFoundError, RateLimitError, SheetsReadError.
    """
    records = get_all_records(tab, project_id)
    for record in records:
        if str(record.get(column, "")) == value:
            return record
    return None


@tracked("google_sheets")
def find_rows(tab: str, column: str, value: str, project_id: str) -> list[dict[str, Any]]:
    """
    Return all rows where `column` equals `value`. Returns empty list if none match.
    """
    records = get_all_records(tab, project_id)
    return [r for r in records if str(r.get(column, "")) == value]


@tracked("google_sheets")
def get_all_records_with_row_numbers(tab: str, project_id: str) -> list[tuple[int, dict[str, Any]]]:
    """
    Return all data rows as (sheet_row_number, record) pairs.

    Row numbers are 1-based sheet row numbers (row 1 = header, so data
    starts at row 2). Useful when you need to delete specific rows by
    position after filtering.

    Raises:
        TabNotFoundError, RateLimitError, SheetsReadError.
    """
    ws = _get_worksheet(tab)
    if not _bucket.consume():
        raise RateLimitError("Token bucket exhausted.")
    try:
        all_values: list[list[Any]] = _retry(ws.get_all_values)
    except gspread.exceptions.APIError as exc:
        raise SheetsReadError(f"Read from '{tab}' failed: {exc}") from exc

    if not all_values:
        return []

    headers = all_values[0]
    result: list[tuple[int, dict[str, Any]]] = []
    for sheet_row_number, row_values in enumerate(all_values[1:], start=2):
        record = {
            headers[j]: (row_values[j] if j < len(row_values) else "") for j in range(len(headers))
        }
        result.append((sheet_row_number, record))
    return result


@tracked("google_sheets")
def delete_rows(tab: str, row_numbers: list[int], project_id: str) -> None:
    """
    Delete specific rows from a tab by their 1-based sheet row numbers.

    Processes rows in descending order so earlier deletions do not shift
    the indices of rows yet to be deleted. Row 1 (header) is never deleted.

    Args:
        tab:         Tab name.
        row_numbers: 1-based sheet row numbers to delete.
        project_id:  AOS project namespace (passed for API symmetry).

    Raises:
        TabNotFoundError, RateLimitError, SheetsWriteError.
    """
    if not row_numbers:
        return

    ws = _get_worksheet(tab)
    for row_num in sorted(row_numbers, reverse=True):
        if row_num <= 1:
            continue  # Never delete the header row
        if not _bucket.consume():
            raise RateLimitError("Token bucket exhausted.")
        try:
            _retry(ws.delete_rows, row_num)
        except gspread.exceptions.APIError as exc:
            raise SheetsWriteError(f"Delete row {row_num} from '{tab}' failed: {exc}") from exc
