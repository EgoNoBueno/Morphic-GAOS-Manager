"""
scripts/setup_workspace.py — One-time workspace provisioning script.

Creates the full Google Drive + Sheets structure for Morphic-G AOS:

  Google Drive/
  └── Morphic-G AOS/                       ← project root folder
      ├── Morphic-G AOS — Control Plane    ← spreadsheet (14 tabs + headers)
      └── Knowledge/                       ← §6 knowledge folder
          ├── workflows/
          ├── procedures/
          ├── policies/
          └── archive/

After running, prints all IDs needed for config/settings.yaml.

Prerequisites:
  - ADC configured: gcloud auth application-default login (see §0.4)
  - .venv activated
  - Run from repo root: python scripts/setup_workspace.py
"""
from __future__ import annotations

import sys

import google.auth
import gspread
from googleapiclient.discovery import build

# ── Config ────────────────────────────────────────────────────────────────────

PROJECT = "morphic-gaos-prod"
DRIVE_ROOT_NAME = "Morphic-G AOS"
SHEET_NAME = "Morphic-G AOS — Control Plane"
KNOWLEDGE_FOLDER_NAME = "Knowledge"

SERVICE_ACCOUNTS = [
    "nexus-prime-sa",
    "ledger-sa",
    "beacon-sa",
    "pursuit-sa",
    "foreman-sa",
    "steward-sa",
    "scout-sa",
]

TABS = [
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
    "Memory Repository Size",
]

HEADERS: dict[str, list[str]] = {
    "Agent_Approvals": [
        "ID", "Agent ID", "Issue", "Trigger Reason", "Stopping Constraint",
        "Iterations Run", "Total Cost USD", "Proposed Code", "Status",
        "Timestamp", "Approved By", "Approver Tier", "code_sha256",
    ],
    "Authorized Approvers": [
        "email", "name", "tier", "active", "added_date", "notes",
    ],
    "Project Registry": [
        "project_id", "project_name", "status", "sheet_workbook_id",
        "drive_folder_id", "budget_ceiling_usd", "owner_email",
        "created_date", "notes",
    ],
    "Logs": [
        "timestamp", "agent_id", "level", "message", "project_id",
    ],
    "Error Logs": [
        "timestamp", "agent_id", "error_type", "message", "traceback", "project_id",
    ],
    "Pending_Knowledge": [
        "timestamp", "agent_id", "observation", "source", "confidence",
        "status", "project_id",
    ],
    "Memory Repository Size": [
        "timestamp", "corpus_name", "document_count", "size_bytes", "project_id",
    ],
}

KNOWLEDGE_SUBFOLDERS = ["workflows", "procedures", "policies", "archive"]

# ── Auth ──────────────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    # Full Drive scope required — accessing shared folders not owned by the ADC identity.
    # drive.file only covers files the app itself created.
    "https://www.googleapis.com/auth/" + "drive",
]


def _get_clients():
    creds, _ = google.auth.default(scopes=SCOPES)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    gc = gspread.Client(auth=creds)
    return drive, gc


# ── Drive helpers ─────────────────────────────────────────────────────────────

def create_folder(drive, name: str, parent_id: str | None = None) -> str:
    """Create a Drive folder and return its ID."""
    metadata: dict = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        metadata["parents"] = [parent_id]
    folder = drive.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def create_spreadsheet_in_folder(drive, name: str, parent_id: str) -> str:
    """Create a Google Sheet inside a Drive folder and return its ID."""
    metadata: dict = {
        "name": name,
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": [parent_id],
    }
    f = drive.files().create(body=metadata, fields="id").execute()
    return f["id"]


def share_with_sa(drive, file_id: str, sa_email: str, role: str = "writer") -> None:
    """Grant a service account access to a Drive file/folder."""
    drive.permissions().create(
        fileId=file_id,
        body={"type": "user", "role": role, "emailAddress": sa_email},
        sendNotificationEmail=False,
    ).execute()


# ── Sheet helpers ─────────────────────────────────────────────────────────────

def setup_tabs(gc: gspread.Client, spreadsheet_id: str) -> None:
    """Rename Sheet1 to first tab, add remaining tabs, add header rows."""
    sh = gc.open_by_key(spreadsheet_id)

    # Rename the default "Sheet1" to the first tab
    default_sheet = sh.sheet1
    default_sheet.update_title(TABS[0])

    # Add remaining tabs
    existing = {ws.title for ws in sh.worksheets()}
    for tab in TABS[1:]:
        if tab not in existing:
            sh.add_worksheet(title=tab, rows=1000, cols=26)
            print(f"  + tab: {tab}")

    # Add headers
    for tab_name, headers in HEADERS.items():
        ws = sh.worksheet(tab_name)
        if ws.row_values(1) != headers:
            ws.update([headers], "A1")
            print(f"  + headers: {tab_name}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Authenticating via ADC...")
    drive, gc = _get_clients()

    # 1. Root folder
    print(f"\nCreating Drive folder: '{DRIVE_ROOT_NAME}'...")
    root_id = create_folder(drive, DRIVE_ROOT_NAME)
    print(f"  root folder ID: {root_id}")

    # 2. Spreadsheet inside root folder
    print(f"\nCreating spreadsheet: '{SHEET_NAME}'...")
    sheet_id = create_spreadsheet_in_folder(drive, SHEET_NAME, root_id)
    print(f"  spreadsheet ID: {sheet_id}")

    # 3. Knowledge folder + subfolders
    print(f"\nCreating Knowledge/ folder structure...")
    knowledge_id = create_folder(drive, KNOWLEDGE_FOLDER_NAME, root_id)
    print(f"  Knowledge/ folder ID: {knowledge_id}")
    for sub in KNOWLEDGE_SUBFOLDERS:
        sub_id = create_folder(drive, sub, knowledge_id)
        print(f"  + {sub}/ : {sub_id}")

    # 4. Create tabs + headers in spreadsheet
    print(f"\nSetting up tabs and headers...")
    setup_tabs(gc, sheet_id)

    # 5. Share root folder with all SAs (inherits to children)
    print(f"\nSharing root folder with service accounts...")
    for sa in SERVICE_ACCOUNTS:
        email = f"{sa}@{PROJECT}.iam.gserviceaccount.com"
        share_with_sa(drive, root_id, email, role="writer")
        print(f"  + {email}")

    # 6. Print settings.yaml values
    print("\n" + "=" * 60)
    print("SUCCESS — add these to config/settings.yaml:")
    print("=" * 60)
    print(f"  sheet.workbook_id:              {sheet_id}")
    print(f"  projects.default.sheet_id:      {sheet_id}")
    print(f"  projects.default.drive_folder_id: {knowledge_id}")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        raise
