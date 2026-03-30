"""scripts/_sync_e2e_test.py — §4d Approval Gate Chat-path E2E test.

End-to-end verification that the /sync endpoint correctly processes an approval:
  1. Writes a minimal test proposal row to Agent_Approvals (Status=Pending)
  2. Gets an OIDC token for nexus-prime via gcloud SA impersonation
  3. POSTs the proposal_id to nexus-prime /sync
  4. Polls Agent_Approvals until Status = 'Deployed' (or 'Needs Revision' on tamper)
  5. Cleans up the test row and reports PASS / FAIL

--full-loop mode (Chat card end-to-end):
  1. Writes a minimal test proposal row to Agent_Approvals
  2. Publishes APPROVAL_REQUEST to agent/approvals/events via Pub/Sub
  3. Waits for the owner to tap Approve or Reject in Google Chat (manual step)
  4. Once the sheet Status changes from Pending, polls for 'Deployed' via /sync
  5. Cleans up the test row and reports PASS / FAIL

Pre-conditions:
  - ADC configured: `gcloud auth application-default login --scopes=...`
  - gcloud installed and on PATH
  - config/settings.yaml has correct gcp.project_id and sheet.workbook_id
  - nexus-prime Cloud Run service is running

Run from repo root (venv active):
  python scripts/_sync_e2e_test.py
  python scripts/_sync_e2e_test.py --project-id morphic-gaos-prod
  python scripts/_sync_e2e_test.py --full-loop
"""

from __future__ import annotations

import argparse
import hashlib
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
_TOKEN_PATH = _REPO_ROOT / "config" / "keys" / "sheets_token.json"
_OAUTH_CLIENT_PATH = _REPO_ROOT / "oauth-client.json"

NEXUS_PRIME_URL = "https://nexus-prime-7bu22bxlda-uc.a.run.app"
POLL_INTERVAL = 4  # seconds
POLL_TIMEOUT = 90  # seconds — /sync does hash verification + vertex sync
FULL_LOOP_HUMAN_TIMEOUT = 300  # seconds — time budget for human to tap Approve in Chat


# ── Minimal safe code that passes Gate 2 ─────────────────────────────────────
_TEST_CODE = (
    "import hashlib\n"
    "def _e2e_noop(x: str) -> str:\n"
    "    return hashlib.sha256(x.encode()).hexdigest()\n"
)
_TEST_SHA256 = hashlib.sha256(_TEST_CODE.encode()).hexdigest()

# ── Helpers ───────────────────────────────────────────────────────────────────


def load_settings() -> dict:
    with open(SETTINGS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_sheets_client() -> gspread.Client:
    """Return an authenticated gspread client.

    Prefers the cached OAuth token from provision_sheet_controls.py
    (config/keys/sheets_token.json) which carries the Sheets scope.
    Falls back to ADC if the token file is absent.
    """
    if _TOKEN_PATH.exists():
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        creds = Credentials.from_authorized_user_file(str(_TOKEN_PATH), SHEETS_SCOPES)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(_OAUTH_CLIENT_PATH), SHEETS_SCOPES
                )
                creds = flow.run_local_server(port=0)
            _TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        return gspread.authorize(creds)

    # Fallback: ADC with Sheets scope. Re-login if this fails:
    # gcloud auth application-default login \
    #   --scopes=https://www.googleapis.com/auth/spreadsheets,https://www.googleapis.com/auth/cloud-platform
    creds, _ = google.auth.default(scopes=SHEETS_SCOPES)
    creds.refresh(Request())
    return gspread.authorize(creds)


def get_id_token(audience: str, service_account: str) -> str:
    """Obtain an OIDC ID token via SA impersonation.

    Uses the Python google-auth library (ADC) rather than the gcloud CLI so
    that it works without a separate `gcloud auth login` step.
    Requires the ADC principal to have roles/iam.serviceAccountTokenCreator
    on the target SA.
    """
    from google.auth import impersonated_credentials
    from google.auth.transport.requests import Request as GoogleRequest

    source_creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    target_creds = impersonated_credentials.IDTokenCredentials(
        target_credentials=impersonated_credentials.Credentials(
            source_credentials=source_creds,
            target_principal=service_account,
            target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
        ),
        target_audience=audience,
        include_email=True,
    )
    target_creds.refresh(GoogleRequest())
    return target_creds.token


def append_test_row(ws: gspread.Worksheet, proposal_id: str) -> int:
    """Append a test proposal row and return its 1-based row number.

    Status (column I) is left EMPTY intentionally — writing any value
    would overwrite the dropdown validation on rows outside the pre-validated
    range.  Nexus-prime's promote node writes 'Deployed' when /sync succeeds;
    poll_status() detects that transition.
    """
    ts = datetime.now(UTC).replace(microsecond=0).isoformat()
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
        "",  # I: Status — left empty; dropdown rule stays intact
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
    """Poll column I (Status) until it is non-empty and not 'Pending', or timeout."""
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        values = ws.row_values(row)
        status = values[8] if len(values) >= 9 else ""
        if status and status not in ("", "Pending"):
            return status
        remaining = int(deadline - time.time())
        print(f"  Polling... status={status!r} ({remaining}s left)", end="\r", flush=True)
        time.sleep(POLL_INTERVAL)
    return "TIMEOUT"


def cleanup_row(ws: gspread.Worksheet, row: int) -> None:
    """Delete the test row entirely so no blank row remains."""
    ws.delete_rows(row)


def publish_approval_request(proposal_id: str, project_id: str) -> str:
    """Publish an APPROVAL_REQUEST to agent/approvals/events via Pub/Sub.

    Uses tools.pubsub.publish (same path as a real domain agent _park() call).
    Returns the Pub/Sub message ID.
    """
    from models import A2AMessage, MessageType
    from tools.pubsub import publish

    msg = A2AMessage(
        source_agent="e2e-test",
        target_agent="nexus-prime",
        project_id=project_id,
        task_id=proposal_id,
        message_type=MessageType.APPROVAL_REQUEST,
        priority=3,
        payload={
            "proposal_id": proposal_id,
            "code_sha256": _TEST_SHA256,
            "description": "E2E test — verifying Chat card delivery. Safe to approve.",
        },
    )
    return publish("agent/approvals/events", msg)


def wait_for_human_action(ws: gspread.Worksheet, row: int) -> str:
    """Block until the owner changes the Status cell away from Pending/empty.

    Returns the status string they set (e.g. 'Approved', 'Rejected') or
    'TIMEOUT' if FULL_LOOP_HUMAN_TIMEOUT seconds elapse.
    """
    deadline = time.time() + FULL_LOOP_HUMAN_TIMEOUT
    while time.time() < deadline:
        values = ws.row_values(row)
        status = values[8] if len(values) >= 9 else ""
        if status and status not in ("", "Pending"):
            return status
        remaining = int(deadline - time.time())
        print(
            f"  Waiting for Chat card tap... status={status!r} ({remaining}s left)",
            end="\r",
            flush=True,
        )
        time.sleep(POLL_INTERVAL)
    return "TIMEOUT"


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="§4d Approval Gate /sync E2E test")
    parser.add_argument("--project-id", default="morphic-gaos-prod")
    parser.add_argument("--url", default=NEXUS_PRIME_URL, help="nexus-prime Cloud Run URL")
    parser.add_argument("--keep-row", action="store_true", help="Don't delete the test row")
    parser.add_argument(
        "--full-loop",
        action="store_true",
        help=(
            "Chat card E2E: publish APPROVAL_REQUEST via Pub/Sub, wait for owner "
            "to tap Approve in Chat, then drive /sync to Deployed. "
            "Requires nexus-prime to be running and owner_space configured."
        ),
    )
    args = parser.parse_args()

    project_id: str = args.project_id
    nexus_url: str = args.url.rstrip("/")
    invoker_sa = f"nexus-prime-sa@{project_id}.iam.gserviceaccount.com"

    settings = load_settings()
    workbook_id: str = settings["sheet"]["workbook_id"]

    proposal_id = str(uuid.uuid4())
    mode_label = "Chat-card full-loop" if args.full_loop else "/sync direct"
    print(f"\n=== §4d Approval Gate E2E [{mode_label}] ===")
    print(f"  project_id:  {project_id}")
    print(f"  proposal_id: {proposal_id}")
    print(f"  nexus URL:   {nexus_url}")
    print(f"  workbook:    {workbook_id}")
    print()

    if args.full_loop:
        _run_full_loop(args, project_id, nexus_url, invoker_sa, workbook_id, proposal_id)
        return

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


def _run_full_loop(
    args: argparse.Namespace,
    project_id: str,
    nexus_url: str,
    invoker_sa: str,
    workbook_id: str,
    proposal_id: str,
) -> None:
    """Full Chat-card loop: pub APPROVAL_REQUEST → human taps Approve → /sync → Deployed."""

    # Step 1 — Write test row to Agent_Approvals
    print("[1/5] Writing test proposal row to Agent_Approvals...")
    client = get_sheets_client()
    wb = client.open_by_key(workbook_id)
    ws = wb.worksheet("Agent_Approvals")
    row_num = append_test_row(ws, proposal_id)
    print(f"  → Row {row_num} written.")

    # Step 2 — Publish APPROVAL_REQUEST to Pub/Sub
    print("\n[2/5] Publishing APPROVAL_REQUEST to agent/approvals/events...")
    try:
        msg_id = publish_approval_request(proposal_id, project_id)
        print(f"  → Published (msg_id={msg_id}).")
        print(
            f"\n  ACTION REQUIRED: Check Google Chat for the approval card.\n"
            f"  Tap 'Approve' (or 'Reject') to continue.\n"
            f"  Timeout: {FULL_LOOP_HUMAN_TIMEOUT}s\n"
        )
    except Exception as exc:
        print(f"  FAIL: {exc}")
        if not args.keep_row:
            cleanup_row(ws, row_num)
        sys.exit(1)

    # Step 3 — Wait for human to act in Chat / Sheet
    print("[3/5] Waiting for owner action in Google Chat...")
    human_status = wait_for_human_action(ws, row_num)
    print()
    if human_status == "TIMEOUT":
        print(f"  FAIL: No action taken within {FULL_LOOP_HUMAN_TIMEOUT}s.")
        if not args.keep_row:
            cleanup_row(ws, row_num)
        sys.exit(1)
    print(f"  → Owner set Status='{human_status}'.")

    if human_status not in ("Approved",):
        print(f"\n  Test stopped: owner chose '{human_status}' (not Approved). Chat card PASS.")
        if not args.keep_row:
            cleanup_row(ws, row_num)
        sys.exit(0)

    # Step 4 — Get OIDC token and POST /sync (same as direct mode)
    print(f"\n[4/5] Getting OIDC token (impersonating {invoker_sa})...")
    try:
        token = get_id_token(nexus_url, invoker_sa)
        print(f"  → Token obtained ({len(token)} chars).")
    except Exception as exc:
        print(f"  FAIL: {exc}")
        if not args.keep_row:
            cleanup_row(ws, row_num)
        sys.exit(1)

    print(f"\n[4/5] POSTing to {nexus_url}/sync ...")
    sync_payload = {
        "proposal_id": proposal_id,
        "project_id": project_id,
        "status": "Approved",
        "approved_by": "e2e-test@full-loop",
    }
    try:
        resp = httpx.post(
            f"{nexus_url}/sync",
            json=sync_payload,
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

    # Step 5 — Poll for Deployed
    print(f"\n[5/5] Polling Agent_Approvals row {row_num} for 'Deployed'...")
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
