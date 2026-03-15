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

import json
import threading
import time
from typing import Any

import gspread
import gspread.exceptions

from config import get_settings
from tools.secrets import get_secret


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
                time.sleep(2 ** attempt)
                continue
            raise
    raise RateLimitError(
        f"Sheets API still failing after 3 retries: {last_err}"
    ) from last_err


def _get_worksheet(tab: str) -> gspread.Worksheet:
    """Return the gspread Worksheet for the given tab name."""
    if _spreadsheet is None:
        raise RuntimeError("init_sheets_client() must be called before any Sheet operation.")
    try:
        return _spreadsheet.worksheet(tab)
    except gspread.exceptions.WorksheetNotFound:
        raise TabNotFoundError(f"Tab '{tab}' not found in workbook.")


def _col_index(ws: gspread.Worksheet, header: str) -> int:
    """Return the 1-based column index for a header label."""
    headers = ws.row_values(1)
    try:
        return headers.index(header) + 1
    except ValueError:
        raise SheetsReadError(
            f"Column '{header}' not found in tab headers: {headers}"
        )


# ── Initialisation ─────────────────────────────────────────────────────────


def init_sheets_client(project_id: str) -> None:
    """
    Authenticate gspread with the GSHEETS_SERVICE_ACCOUNT secret and open the
    workbook for this project. Must be called before any other function in
    this module.

    Args:
        project_id: The AOS project namespace (matches a key in settings.yaml
                    under 'projects').

    Raises:
        WorkbookNotFoundError: No sheet_id configured for this project_id,
                               or the spreadsheet is inaccessible.
        SecretNotFoundError:   Propagated from get_secret().
    """
    global _client, _spreadsheet

    settings = get_settings()
    project = settings.get_project(project_id)
    if project is None or not project.sheet_id:
        raise WorkbookNotFoundError(
            f"No sheet_id configured for project_id '{project_id}'. "
            "Add it under 'projects.<project_id>.sheet_id' in config/settings.yaml."
        )

    sa_json = get_secret("GSHEETS_SERVICE_ACCOUNT", settings.GCP_PROJECT_ID)
    sa_info = json.loads(sa_json)
    _client = gspread.service_account_from_dict(sa_info)

    try:
        _spreadsheet = _client.open_by_key(project.sheet_id)
    except gspread.exceptions.SpreadsheetNotFound:
        raise WorkbookNotFoundError(
            f"Spreadsheet '{project.sheet_id}' not found or the service account "
            "does not have access. Share the workbook with the service account email."
        )


# ── Core functions ─────────────────────────────────────────────────────────


def append_row(tab: str, row: dict[str, Any], project_id: str) -> None:
    """
    Append one row to the named Sheet tab.
    Keys in `row` must match the tab's header row exactly (case-sensitive).

    Prefer batch_append_rows() when writing more than one row.

    Raises:
        TabNotFoundError, RateLimitError, SheetsWriteError.
    """
    batch_append_rows(tab, [row], project_id)


def batch_append_rows(
    tab: str, rows: list[dict[str, Any]], project_id: str
) -> None:
    """
    Append multiple rows in a single API call (values_append).
    All rows must have identical key sets matching the tab header.

    Counts as one API request regardless of row count.

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

    # Use the tab name as the range so values_append inserts after the last
    # populated row (INSERT_ROWS mode). Do NOT append a row number — ws.row_count
    # returns the pre-allocated grid size, not the count of populated rows.
    range_notation = _range(tab)
    try:
        _retry(
            _spreadsheet.values_append,
            range_notation,
            {"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            {"values": values},
        )
    except gspread.exceptions.APIError as exc:
        raise SheetsWriteError(f"Write to '{tab}' failed: {exc}") from exc


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


def read_range(tab: str, a1_range: str, project_id: str) -> list[list[Any]]:
    """
    Return raw cell values for an A1-notation range (e.g. "A2:D50").
    Returns list of rows; each row is a list of cell values (str or "").

    Raises:
        TabNotFoundError, RateLimitError, SheetsReadError.
    """
    _get_worksheet(tab)  # validates tab exists
    if not _bucket.consume():
        raise RateLimitError("Token bucket exhausted.")
    full_range = _range(tab, a1_range)
    try:
        return _retry(_spreadsheet.values_get, full_range).get("values", [])
    except gspread.exceptions.APIError as exc:
        raise SheetsReadError(f"Range read '{full_range}' failed: {exc}") from exc


def update_row(
    tab: str, row_index: int | str, updates: dict[str, Any], project_id: str
) -> None:
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
            raise RowNotFoundError(
                f"No row with ID='{row_index}' in tab '{tab}'."
            )
        actual_row = cell.row
    else:
        row_count = ws.row_count
        if row_index < 1 or row_index > row_count:
            raise RowNotFoundError(
                f"Row {row_index} is out of range for tab '{tab}' "
                f"(1–{row_count})."
            )
        actual_row = row_index

    headers = _retry(ws.row_values, 1)
    cells_to_update = []
    for col_header, value in updates.items():
        if col_header not in headers:
            raise SheetsWriteError(
                f"Column '{col_header}' not found in tab '{tab}' headers."
            )
        col_idx = headers.index(col_header) + 1
        cells_to_update.append(
            gspread.Cell(row=actual_row, col=col_idx, value=str(value))
        )

    if not cells_to_update:
        return

    if not _bucket.consume():
        raise RateLimitError("Token bucket exhausted.")
    try:
        _retry(ws.update_cells, cells_to_update)
    except gspread.exceptions.APIError as exc:
        raise SheetsWriteError(
            f"Row update on '{tab}' row {row_index} failed: {exc}"
        ) from exc


def find_row(
    tab: str, column: str, value: str, project_id: str
) -> dict[str, Any] | None:
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


def find_rows(
    tab: str, column: str, value: str, project_id: str
) -> list[dict[str, Any]]:
    """
    Return all rows where `column` equals `value`. Returns empty list if none match.
    """
    records = get_all_records(tab, project_id)
    return [r for r in records if str(r.get(column, "")) == value]
