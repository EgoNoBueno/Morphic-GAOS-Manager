"""scripts/_sync_e2e_test.py — §4d Approval Gate Chat-path E2E test.

End-to-end verification that the /sync endpoint correctly processes an approval:
  1. Writes a minimal test proposal row to Agent_Approvals (Status=Pending)
  2. Gets an OIDC token for nexus-prime via gcloud SA impersonation
  3. POSTs the proposal_id to nexus-prime /sync
  4. Polls Agent_Approvals until Status = 'Deployed' (or 'Needs Revision' on tamper)
  5. Cleans up the test row and reports PASS / FAIL

Pre-conditions:
  - ADC configured: `gcloud auth application-default login --scopes=...`
  - gcloud installed and on PATH
  - config/settings.yaml has correct gcp.project_id and sheet.workbook_id
  - nexus-prime Cloud Run service is running

Run from repo root (venv active):
  python scripts/_sync_e2e_test.py
  python scripts/_sync_e2e_test.py --project-id morphic-gaos-prod
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import google.auth
import gspread
import httpx
import yaml
from google.auth.transport.requests import Request

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

SETTINGS_PATH = _REPO_ROOT / "config" / "settings.yaml"
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

NEXUS_PRIME_URL = "https://nexus-prime-7bu22bxlda-uc.a.run.app"
POLL_INTERVAL = 4  # seconds
POLL_TIMEOUT = 90  # seconds — /sync does hash verification + vertex sync


# ── Minimal safe code that passes Gate 2 ─────────────────────────────────────
_TEST_CODE = (
    "import hashlib\n"
    "def _e2e_noop(x: str) -> str:\n"
    "    return hashlib.sha256(x.encode()).hexdigest()\n"
)
_TEST_SHA256 = hashlib.sha256(_TEST_CODE.encode()).hexdigest()

# ── Helpers ───────────────────────────────────────────────────────────────────


def load_settings() -> dict:
    with open(SETTINGS_PATH) as f:
        return yaml.safe_load(f)


def get_sheets_client() -> gspread.Client:
    creds, _ = google.auth.default(scopes=SHEETS_SCOPES)
    creds.refresh(Request())
    return gspread.authorize(creds)


def get_id_token(audience: str, service_account: str) -> str:
    """Obtain an OIDC ID token via gcloud SA impersonation."""
    gcloud = shutil.which("gcloud") or "gcloud"
    result = subprocess.run(
        [
            gcloud,
            "auth",
            "print-identity-token",
            f"--impersonate-service-account={service_account}",
            f"--audiences={audience}",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        shell=(sys.platform == "win32"),
    )
    if result.returncode != 0:
        raise RuntimeError(f"gcloud failed: {result.stderr.strip()[-200:]}")
    return result.stdout.strip()


def append_test_row(ws: gspread.Worksheet, proposal_id: str) -> int:
    """Append a test proposal row and return its 1-based row number."""
    ts = datetime.now(UTC).isoformat()
    # Column order: ID | Agent ID | Issue | Trigger Reason | Stopping Constraint |
    # Iterations Run | Total Cost USD | Proposed Code | Status | Timestamp |
    # Approved By | Approver Tier | code_sha256
    row_data = [
        proposal_id,  # A: ID
        "e2e-test",  # B: Agent ID
        "E2E test — safe to delete",  # C: Issue
        "E2E_SYNC_TEST",  # D: Trigger Reason
        "success",  # E: Stopping Constraint
        1,  # F: Iterations Run
        0.0,  # G: Total Cost USD
        _TEST_CODE,  # H: Proposed Code
        "Pending",  # I: Status
        ts,  # J: Timestamp
        "",  # K: Approved By
        "",  # L: Approver Tier
        _TEST_SHA256,  # M: code_sha256
    ]
    ws.append_row(row_data, value_input_option="RAW")

    # Locate the row we just wrote
    for _attempt in range(6):
        cell = ws.find(proposal_id, in_column=1)
        if cell is not None:
            return cell.row
        time.sleep(2)
    raise RuntimeError(f"Test row with ID={proposal_id} not found after append.")


def poll_status(ws: gspread.Worksheet, row: int) -> str:
    """Poll column I (Status) until it changes from 'Pending', or timeout."""
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        values = ws.row_values(row)
        status = values[8] if len(values) >= 9 else ""
        if status and status != "Pending":
            return status
        remaining = int(deadline - time.time())
        print(f"  Polling... status={status!r} ({remaining}s left)", end="\r", flush=True)
        time.sleep(POLL_INTERVAL)
    return "TIMEOUT"


def cleanup_row(ws: gspread.Worksheet, row: int) -> None:
    ws.batch_clear([f"A{row}:M{row}"])


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="§4d Approval Gate /sync E2E test")
    parser.add_argument("--project-id", default="morphic-gaos-prod")
    parser.add_argument("--url", default=NEXUS_PRIME_URL, help="nexus-prime Cloud Run URL")
    parser.add_argument("--keep-row", action="store_true", help="Don't delete the test row")
    args = parser.parse_args()

    project_id: str = args.project_id
    nexus_url: str = args.url.rstrip("/")
    invoker_sa = f"nexus-prime-sa@{project_id}.iam.gserviceaccount.com"

    settings = load_settings()
    workbook_id: str = settings["sheet"]["workbook_id"]

    proposal_id = str(uuid.uuid4())
    print("\n=== §4d Approval Gate E2E ===")
    print(f"  project_id:  {project_id}")
    print(f"  proposal_id: {proposal_id}")
    print(f"  nexus URL:   {nexus_url}")
    print(f"  workbook:    {workbook_id}")
    print()

    # Step 1 — Write test row to Agent_Approvals
    print("[1/4] Writing test proposal row to Agent_Approvals...")
    client = get_sheets_client()
    wb = client.open_by_key(workbook_id)
    ws = wb.worksheet("Agent_Approvals")
    row_num = append_test_row(ws, proposal_id)
    print(f"  → Row {row_num} written. Status=Pending, SHA256 set.")

    # Step 2 — Get OIDC token
    print(f"\n[2/4] Getting OIDC token (impersonating {invoker_sa})...")
    try:
        token = get_id_token(nexus_url, invoker_sa)
        print(f"  → Token obtained ({len(token)} chars).")
    except Exception as exc:
        print(f"  FAIL: {exc}")
        if not args.keep_row:
            cleanup_row(ws, row_num)
        sys.exit(1)

    # Step 3 — POST to /sync
    print(f"\n[3/4] POSTing to {nexus_url}/sync ...")
    payload = {
        "proposal_id": proposal_id,
        "project_id": project_id,
        "status": "Approved",
        "approved_by": "e2e-test@local",
    }
    try:
        resp = httpx.post(
            f"{nexus_url}/sync",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
        print(f"  → HTTP {resp.status_code}")
        if resp.status_code >= 400:
            print(f"  Response: {resp.text[:400]}")
            if not args.keep_row:
                cleanup_row(ws, row_num)
            sys.exit(1)
    except httpx.TimeoutException:
        print("  FAIL: request timed out after 60s")
        if not args.keep_row:
            cleanup_row(ws, row_num)
        sys.exit(1)
    except httpx.RequestError as exc:
        print(f"  FAIL: network error — {exc}")
        if not args.keep_row:
            cleanup_row(ws, row_num)
        sys.exit(1)

    # Step 4 — Poll for Status change
    print(f"\n[4/4] Polling Agent_Approvals row {row_num} for status change...")
    final_status = poll_status(ws, row_num)
    print()

    passed = final_status == "Deployed"
    result_label = "PASS" if passed else "FAIL"
    print(f"\n{'=' * 40}")
    print(f"  Result:       {result_label}")
    print(f"  Final status: {final_status}")
    if not passed:
        values = ws.row_values(row_num)
        print(f"  Row values:   {values}")
    print(f"{'=' * 40}\n")

    if not args.keep_row:
        print("Cleaning up test row...")
        cleanup_row(ws, row_num)
        print("  Done.")

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
