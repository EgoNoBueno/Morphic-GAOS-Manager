"""
scripts/smoke_test_4.py — Smoke test 4: onEdit approval trigger.

What this script does:
  1. Appends a throwaway test row to Agent_Approvals (Status = 'Pending')
  2. Pauses and prompts you to type 'Approved' in cell I<N> of the sheet
  3. Waits up to 60 s polling for J/K/L to populate on that row
  4. Checks the Logs tab for a new APPROVAL entry matching the test row
  5. Clears the test row and prints PASS or FAIL with details

Run from repo root (venv active):
  python scripts/smoke_test_4.py
"""
from __future__ import annotations

import datetime
import sys
import time
from pathlib import Path

import google.auth
import gspread
import yaml
from google.auth.transport.requests import Request

SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Columns (1-indexed) — matches setup_workspace.py HEADERS
# A=ID, B=Agent, C=Issue, D=Trigger, E=Stopping, F=Iterations, G=Cost,
# H=Code, I=Status, J=Timestamp, K=Approved By, L=Approver Tier, M=SHA, N=Priority
COL_STATUS = 9   # I — filled by user in UI
COL_TS     = 10  # J — Timestamp (set by trigger)
COL_BY     = 11  # K — Approved By (set by trigger)
COL_TIER   = 12  # L — Approver Tier (set by trigger)

POLL_INTERVAL = 3   # seconds between checks
POLL_TIMEOUT  = 60  # seconds total wait


def load_workbook_id() -> str:
    with open(SETTINGS_PATH) as f:
        return yaml.safe_load(f)["sheet"]["workbook_id"]


def get_client() -> gspread.Client:
    creds, _ = google.auth.default(scopes=SCOPES)
    creds.refresh(Request())
    return gspread.authorize(creds)


def append_test_row(sheet: gspread.Worksheet) -> tuple[int, str]:
    """Append a throwaway proposal row, return (row_number, test_id)."""
    test_id = f"SMOKE4-{datetime.datetime.now().strftime('%H%M%S')}"
    # 14 columns: A–N; col I (index 8) = 'Pending'
    row_data = [
        test_id,                              # A: ID
        "smoke-test",                         # B: Agent ID
        "Smoke test — safe to delete",        # C: Issue
        "SMOKE_TEST",                         # D: Trigger Reason
        "",                                   # E: Stopping Constraint
        "",                                   # F: Iterations
        "",                                   # G: Cost USD
        "",                                   # H: Proposed Code
        "Pending",                            # I: Status (user will change to Approved)
        "",                                   # J: Timestamp
        "",                                   # K: Approved By
        "",                                   # L: Approver Tier
        "",                                   # M: code_sha256
        "1",                                  # N: Priority (1 = any tier can approve)
    ]
    sheet.append_row(row_data, value_input_option="USER_ENTERED")
    # Find the row we just added by searching for the unique test_id.
    # Retry a few times to handle transient Sheets API propagation delays.
    cell = None
    for attempt in range(5):
        cell = sheet.find(test_id, in_column=1)
        if cell is not None:
            break
        time.sleep(2)
    if cell is None:
        raise RuntimeError(
            f"Could not locate test row for test_id={test_id!r} after 5 attempts. "
            "The append may have failed or Sheets API is lagging."
        )
    return cell.row, test_id


def cleanup_test_row(sheet: gspread.Worksheet, row: int) -> None:
    """Clear all cells in the test row (non-destructive: leaves row structure intact)."""
    last_col_letter = "N"
    sheet.batch_clear([f"A{row}:{last_col_letter}{row}"])
    print(f"  Test row {row} cleared.")


def poll_for_stamp(sheet: gspread.Worksheet, row: int) -> bool:
    """Poll until K or L is populated on the given row, or timeout."""
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        values = sheet.row_values(row)
        approved_by = values[COL_BY - 1]   if len(values) >= COL_BY   else ""
        tier        = values[COL_TIER - 1] if len(values) >= COL_TIER else ""
        timestamp   = values[COL_TS - 1]   if len(values) >= COL_TS   else ""
        if approved_by or tier:
            print(f"\n  J (Timestamp):     {timestamp or '(empty)'}")
            print(f"  K (Approved By):   {approved_by or '(empty)'}")
            print(f"  L (Approver Tier): {tier or '(empty)'}")
            return True
        remaining = int(deadline - time.time())
        print(f"  Waiting... ({remaining}s left)", end="\r", flush=True)
        time.sleep(POLL_INTERVAL)
    return False


def check_logs(wb: gspread.Spreadsheet, test_id: str) -> bool:
    """Return True if the Logs tab has an APPROVAL entry for the test proposal."""
    try:
        logs = wb.worksheet("Logs")
    except gspread.WorksheetNotFound:
        print("  ⚠  Logs tab not found")
        return False
    rows = logs.get_all_values()
    for row in reversed(rows[1:]):  # skip header, newest first
        if len(row) > 1 and row[1] == "APPROVAL" and (not test_id or test_id in row):
            print(f"  Logs entry found: {row}")
            return True
    print("  ⚠  No APPROVAL entry found in Logs tab for this test run")
    return False


def main() -> None:
    print("\n=== Smoke Test 4: onEdit approval trigger ===\n")

    wid   = load_workbook_id()
    gc    = get_client()
    wb    = gc.open_by_key(wid)
    sheet = wb.worksheet("Agent_Approvals")

    # ── Step 1: append a fresh test row ──────────────────────────────────────
    print("Step 1: Appending throwaway test row...")
    test_row, test_id = append_test_row(sheet)
    print(f"  Test row appended at row {test_row} (ID: {test_id})")

    # ── Step 2: prompt ────────────────────────────────────────────────────────
    sheet_url = f"https://docs.google.com/spreadsheets/d/{wid}/edit"
    print(
        f"\nStep 2: In the sheet, click cell  I{test_row}  and type  'Approved'  then press Enter."
        f"\n        (Row {test_row} already has Status = Pending)"
        f"\n\n        Sheet: {sheet_url}"
        f"\n\n        Once you've typed Approved and pressed Enter in the sheet,"
        f"\n        come back here and press Enter to start polling."
    )
    input("\n  [Press Enter when done] ")

    # ── Step 3: poll ──────────────────────────────────────────────────────────
    print(f"\nStep 3: Polling row {test_row} for trigger stamp (up to {POLL_TIMEOUT}s)...")
    stamped = poll_for_stamp(sheet, test_row)

    # ── Step 4: logs ──────────────────────────────────────────────────────────
    print("\nStep 4: Checking Logs tab...")
    logged = check_logs(wb, test_id)

    # ── Step 5: cleanup ───────────────────────────────────────────────────────
    print("\nStep 5: Cleaning up test row...")
    try:
        cleanup_test_row(sheet, test_row)
    except Exception as e:
        print(f"  ⚠  Cleanup failed (manual delete row {test_row} if needed): {e}")

    # ── Result ────────────────────────────────────────────────────────────────
    print()
    if stamped and logged:
        print("✅  SMOKE TEST 4 PASSED")
        sys.exit(0)
    else:
        failures = []
        if not stamped:
            failures.append(f"trigger did not stamp K/L within {POLL_TIMEOUT}s")
        if not logged:
            failures.append("no APPROVAL entry in Logs tab")
        print("❌  SMOKE TEST 4 FAILED: " + "; ".join(failures))
        sys.exit(1)


if __name__ == "__main__":
    main()
