"""
scripts/provision_sheet_controls.py — Idempotent script to provision missing
Sheet tabs and add data-validation controls to the Morphic-G AOS control plane
spreadsheet.

Actions
-------
1. Creates any tabs from the full TABS list that do not already exist,
   writing the correct header row for each.
2. Adds a Status dropdown (Pending / Approved / Rejected / Deployed /
   Needs Revision) to Agent_Approvals Column I for all data rows.

Safe to re-run — existing tabs and validation rules are replaced, not
duplicated.

Prerequisites
-------------
  - ADC configured: gcloud auth application-default login
  - .venv activated
  - Run from repo root: python scripts/provision_sheet_controls.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import gspread
import yaml
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

_TOKEN_FILE = Path(__file__).parent.parent / "config" / "keys" / "sheets_token.json"
_CLIENT_FILE = Path(__file__).parent.parent / "oauth-client.json"

# ── Tab definitions ────────────────────────────────────────────────────────────

# All expected tabs in display order.
TABS: list[str] = [
    "Project Registry",
    "Agent_Approvals",
    "Authorized Approvers",
    "Accounting",
    "Marketing",
    "Sales by Product",
    "Sales Graphs",
    "Ad Response/Spend/Recommendations",
    "Shipping and Receiving",
    "Logs",
    "Error Logs",
    "Research Products",
    "Pending_Knowledge",
    "Project_Incubator",
    "Memory Repository Size",
]

# All Tier 2 agent domain tabs share the same 5-column schema written by
# every agent's _report() node.
_AGENT_DOMAIN_HEADERS: list[str] = [
    "timestamp",
    "agent_id",
    "task_type",
    "status",
    "summary",
]

# Header rows per tab. Tabs absent from this dict are display-only and are
# created empty (no agent writes to them programmatically).
HEADERS: dict[str, list[str]] = {
    "Agent_Approvals": [
        "ID",
        "Agent ID",
        "Issue",
        "Trigger Reason",
        "Stopping Constraint",
        "Iterations Run",
        "Total Cost USD",
        "Proposed Code",
        "Status",
        "Timestamp",
        "Approved By",
        "Approver Tier",
        "code_sha256",
        "Priority",
    ],
    "Authorized Approvers": ["email", "name", "tier", "active", "added_date", "notes"],
    "Project Registry": [
        "project_id",
        "project_name",
        "status",
        "sheet_workbook_id",
        "drive_folder_id",
        "budget_ceiling_usd",
        "owner_email",
        "created_date",
        "notes",
    ],
    "Accounting": _AGENT_DOMAIN_HEADERS,
    "Marketing": _AGENT_DOMAIN_HEADERS,
    "Sales by Product": _AGENT_DOMAIN_HEADERS,
    "Shipping and Receiving": _AGENT_DOMAIN_HEADERS,
    "Research Products": _AGENT_DOMAIN_HEADERS,
    "Logs": ["timestamp", "agent_id", "level", "message", "project_id"],
    "Error Logs": [
        "timestamp",
        "agent_id",
        "error_type",
        "message",
        "traceback",
        "project_id",
    ],
    "Pending_Knowledge": [
        "timestamp",
        "agent_id",
        "observation",
        "source",
        "confidence",
        "status",
        "project_id",
    ],
    "Project_Incubator": [
        "id",
        "vision_text",
        "submitted_by",
        "submitted_at",
        "status",
        "doc_id",
        "project_id",
    ],
    "Memory Repository Size": [
        "timestamp",
        "corpus_name",
        "document_count",
        "size_bytes",
        "project_id",
    ],
}

# Valid Status values for the Agent_Approvals Column I dropdown.
# Matches all values written by update_row() calls in the orchestrator.
APPROVAL_STATUS_VALUES: list[str] = [
    "Pending",
    "Approved",
    "Rejected",
    "Deployed",
    "Needs Revision",
]

# Column I = index 8 (0-based, A=0).
_STATUS_COL_INDEX = 8


# ── Helpers ────────────────────────────────────────────────────────────────────


def _get_workbook_id() -> str:
    """Read sheet.workbook_id from config/settings.yaml."""
    cfg_path = Path(__file__).parent.parent / "config" / "settings.yaml"
    with cfg_path.open() as fh:
        cfg = yaml.safe_load(fh)
    wid: str = cfg.get("sheet", {}).get("workbook_id", "")
    if not wid:
        print("ERROR: sheet.workbook_id not set in config/settings.yaml", file=sys.stderr)
        sys.exit(1)
    return wid


# ── Step 1 — Tab provisioning ──────────────────────────────────────────────────


def provision_missing_tabs(sh: gspread.Spreadsheet) -> None:
    """
    Create any tab in TABS that does not already exist and write its header row
    if one is defined in HEADERS.

    Idempotent: existing tabs and existing header rows are not modified.
    """
    existing = {ws.title for ws in sh.worksheets()}

    for tab in TABS:
        if tab not in existing:
            sh.add_worksheet(title=tab, rows=1000, cols=26)
            print(f"  + created tab: {tab}")

        headers = HEADERS.get(tab)
        if not headers:
            # Display-only tab — no programmatic writes expected.
            status = "exists" if tab in existing else "created"
            print(f"  ✓ {status} (display-only, no headers): {tab}")
            continue

        ws = sh.worksheet(tab)
        current = ws.row_values(1)
        if not current:
            ws.update([[h for h in headers]], "A1")  # type: ignore[call-arg]
            print(f"  + wrote headers to: {tab}")
        else:
            status = "exists" if tab in existing else "created"
            print(f"  ✓ {status} with headers: {tab}")


# ── Step 2 — Agent_Approvals Status dropdown ───────────────────────────────────


def add_status_dropdown(sh: gspread.Spreadsheet) -> None:
    """
    Apply a ONE_OF_LIST data validation rule to Agent_Approvals Column I
    (Status) for all data rows (row 2 downward).

    Uses strict=False so the Sheets API can still write any value
    programmatically; the dropdown is a UI convenience only.
    """
    ws = sh.worksheet("Agent_Approvals")

    rule = {
        "setDataValidation": {
            "range": {
                "sheetId": ws.id,
                "startRowIndex": 1,  # 0-indexed: row 2 (skips header)
                "startColumnIndex": _STATUS_COL_INDEX,
                "endColumnIndex": _STATUS_COL_INDEX + 1,
                # No endRowIndex → applies to all rows below the header.
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": v} for v in APPROVAL_STATUS_VALUES],
                },
                "showCustomUi": True,  # renders as a dropdown arrow in the cell
                "strict": False,  # allows API writes of any value; UI shows arrow
            },
        }
    }

    sh.batch_update({"requests": [rule]})
    options = ", ".join(APPROVAL_STATUS_VALUES)
    print(f"  + Status dropdown applied to Agent_Approvals Column I ({options})")


# ── Entrypoint ─────────────────────────────────────────────────────────────────


def main() -> None:
    wid = _get_workbook_id()

    # ── Auth: use stored token or run interactive OAuth flow ──────────────────
    creds: Credentials | None = None
    if _TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(_CLIENT_FILE), SCOPES)
            creds = flow.run_local_server(port=0)  # type: ignore[assignment]
        _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_FILE.write_text(creds.to_json())  # type: ignore[union-attr]

    gc = gspread.Client(auth=creds)  # type: ignore[arg-type]
    sh = gc.open_by_key(wid)
    print(f"Opened: {sh.title}  ({wid})\n")

    print("Step 1 — Provisioning missing tabs...")
    provision_missing_tabs(sh)

    print("\nStep 2 — Adding Status dropdown to Agent_Approvals Column I...")
    add_status_dropdown(sh)

    print("\nDone.")


if __name__ == "__main__":
    main()
