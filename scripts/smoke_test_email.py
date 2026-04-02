"""
scripts/smoke_test_email.py — Smoke test: Gmail Pub/Sub push → Nexus-Prime process_gmail.

What this script does:
  1. Resolves the live Cloud Run URL (from --url flag, CLOUD_RUN_URL env var,
     or gcloud CLI fallback).
  2. Reads gmail_last_history_id from System_State; seeds it to --seed-history-id
     (default 244838) if the row is missing.
  3. Warns if the Gmail watch is within 24 h of expiry.
  4. POSTs a synthetic Gmail Pub/Sub push notification to /gmail-webhook with
     a Google-signed OIDC token so Bearer-token verification passes.
  5. Polls Cloud Logging for a process_gmail task entry from nexus-prime.
  6. Prints PASS or FAIL with the Cloud Logging console URL for the run.

Note: The orchestrator will call fetch_new_messages() against the real Gmail
API.  If real emails have arrived since the seeded history ID, they will be
processed and may trigger actual replies.  This is intentional — the script
exercises the full pipeline.  Use --seed-history-id with a recent historyId
to minimise unexpected processing.

Pre-conditions:
  - Application Default Credentials (ADC) scoped to the GCP project.
  - The Cloud Run service URL is reachable.
  - config/settings.yaml has gmail.* fields set.
  - The Gmail watch is live (within 7-day window after setup_watch / renew).

Run from repo root (venv active):
  python scripts/smoke_test_email.py --url https://nexus-prime-XXXX-uc.a.run.app
  python scripts/smoke_test_email.py                   # auto-resolves URL via gcloud
  python scripts/smoke_test_email.py --seed-history-id 244838 --skip-log-poll
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import google.auth
import google.auth.transport.requests
import google.oauth2.id_token
import httpx
import yaml

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

SETTINGS_PATH = _REPO_ROOT / "config" / "settings.yaml"
_ENV_PATH = _REPO_ROOT / ".env"


def _load_dotenv() -> None:
    """Load key=value pairs from .env into os.environ (no-op if file absent)."""
    if not _ENV_PATH.exists():
        return
    with open(_ENV_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


_load_dotenv()
POLL_INTERVAL = 5  # seconds between Cloud Logging checks
POLL_TIMEOUT = 90  # seconds to wait for process_gmail log entry
_WATCH_WARN_THRESHOLD_MS = 24 * 60 * 60 * 1000  # 24 h in milliseconds


# ── Helpers ───────────────────────────────────────────────────────────────────


def _load_settings() -> dict:
    with open(SETTINGS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _resolve_url(explicit: str | None, settings: dict) -> str:
    """
    Return the Nexus-Prime Cloud Run base URL.

    Resolution order:
      1. --url CLI flag
      2. CLOUD_RUN_URL environment variable
      3. ``gcloud run services describe`` (requires gcloud CLI + permissions)

    Args:
        explicit: Value of the --url CLI argument, or None.
        settings: Parsed settings.yaml dict.

    Returns:
        The Cloud Run base URL, stripped of trailing slash.

    Raises:
        SystemExit: If the URL cannot be determined by any method.
    """
    url = explicit or os.environ.get("CLOUD_RUN_URL", "").strip()
    if url:
        return url.rstrip("/")

    project_id = (settings.get("gcp") or {}).get("project_id", "")
    region = (settings.get("gcp") or {}).get("region", "us-central1")
    print(f"  Resolving URL via gcloud (project={project_id}, region={region})...")
    try:
        result = subprocess.run(
            [
                "gcloud",
                "run",
                "services",
                "describe",
                "nexus-prime",
                "--region",
                region,
                "--project",
                project_id,
                "--format",
                "value(status.url)",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        url = result.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(
            f"  ✗ gcloud lookup failed: {exc}\n"
            "  Pass --url https://nexus-prime-XXX-uc.a.run.app explicitly."
        )
        sys.exit(1)

    if not url:
        print(
            "  ✗ gcloud returned an empty URL. The service may not be deployed.\n"
            "  Check: gcloud run services list --region us-central1"
        )
        sys.exit(1)

    return url.rstrip("/")


def _get_id_token(audience: str, project_id: str = "") -> str:
    """
    Obtain a Google-signed OIDC ID token for the given audience.

    Tries two methods in order:
      1. ``google.oauth2.id_token.fetch_id_token`` — works on GCP compute or
         when ADC points to a service account key file.
      2. ``gcloud auth print-identity-token`` with SA impersonation — works
         with user ADC from ``gcloud auth application-default login``.

    Args:
        audience:   The Cloud Run service URL used as the token audience.
        project_id: GCP project ID used for service account impersonation in
                    the ``gcloud auth print-identity-token`` fallback path.
                    When non-empty and ADC resolves to a user credential,
                    the ``--impersonate-service-account`` flag is set to
                    ``nexus-prime-sa@<project_id>.iam.gserviceaccount.com``.
                    Defaults to ``""`` (no impersonation).

    Returns:
        A signed JWT string.

    Raises:
        RuntimeError: If neither method succeeds.
    """
    try:
        auth_req = google.auth.transport.requests.Request()
        return google.oauth2.id_token.fetch_id_token(auth_req, audience)
    except Exception as e:
        print(
            f"[smoke_test_email] fetch_id_token failed (audience={audience}): {e}",
            file=sys.stderr,
        )

    import platform

    # Probe PATH for the gcloud executable rather than relying on a hardcoded path.
    # On Windows prefer "gcloud.cmd" (the shim installed by Cloud SDK); fall back
    # to "gcloud" (works when the SDK bin dir is on PATH without the .cmd extension).
    if platform.system() == "Windows":
        gcloud_cmd = shutil.which("gcloud.cmd") or shutil.which("gcloud")
    else:
        gcloud_cmd = shutil.which("gcloud")
    if not gcloud_cmd:
        raise RuntimeError(
            "gcloud executable not found on PATH. "
            "Install the Google Cloud SDK and ensure its bin directory is on PATH."
        )
    sa = (
        os.getenv("SERVICE_ACCOUNT_EMAIL") or f"nexus-prime-sa@{project_id}.iam.gserviceaccount.com"
    )
    try:
        result = subprocess.run(
            [
                gcloud_cmd,
                "auth",
                "print-identity-token",
                f"--impersonate-service-account={sa}",
                f"--audiences={audience}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        token = result.stdout.strip()
        if token:
            return token
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise RuntimeError(
            f"gcloud auth print-identity-token failed: {exc}\n"
            "  Run: gcloud auth application-default login"
        ) from exc

    raise RuntimeError("Could not obtain an ID token via any available method.")


def _seed_history_id(project_id: str, history_id: str) -> str:
    """
    Ensure gmail_last_history_id exists in the System_State sheet.

    Reads the current value first.  If the row is missing, appends it with
    *history_id*.  If it already exists, leaves it untouched and returns the
    existing value — so the caller knows the true startHistoryId in use.

    Args:
        project_id: GCP project that owns the workbook.
        history_id: Fallback value to write if the row is absent.

    Returns:
        The effective history ID now stored in System_State.
    """
    from tools.google_sheets import append_row, find_row, init_sheets_client

    init_sheets_client(project_id)
    row = find_row("System_State", "key", "gmail_last_history_id", project_id)
    if row:
        stored = str(row.get("value", "")).strip()
        if stored:
            print(f"  gmail_last_history_id already set: {stored} (not overwriting)")
            return stored
    # Row missing or value blank — seed it
    print(f"  Seeding gmail_last_history_id = {history_id}")
    append_row("System_State", {"key": "gmail_last_history_id", "value": history_id}, project_id)
    return history_id


def _check_watch_expiry(project_id: str) -> None:
    """
    Read gmail_watch_expiration from System_State and warn if < 24 h remain.

    Non-fatal — the smoke test proceeds regardless.

    Args:
        project_id: GCP project that owns the workbook.
    """
    from tools.google_sheets import find_row, init_sheets_client

    init_sheets_client(project_id)
    row = find_row("System_State", "key", "gmail_watch_expiration", project_id)
    if not row:
        print("  ⚠  gmail_watch_expiration not found in System_State — watch may not be set up.")
        return

    try:
        raw = str(row.get("value", "0")).strip()
        if raw.isdigit():
            expiry_ms = int(raw)
        else:
            dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.UTC)
            expiry_ms = int(dt.timestamp() * 1000)
        now_ms = int(time.time() * 1000)
        remaining_ms = expiry_ms - now_ms
        remaining_h = remaining_ms / (1000 * 60 * 60)
        if remaining_ms <= 0:
            print("  ✗  Gmail watch has EXPIRED. Run: python scripts/renew_gmail_watch.py")
        elif remaining_ms <= _WATCH_WARN_THRESHOLD_MS:
            print(
                f"  ⚠  Gmail watch expires in {remaining_h:.1f} h — renew soon.\n"
                "  Run: python scripts/renew_gmail_watch.py"
            )
        else:
            print(f"  Gmail watch OK — expires in {remaining_h:.1f} h.")
    except (ValueError, TypeError) as exc:
        print(f"  ⚠  Could not parse gmail_watch_expiration: {exc}")


def _build_pubsub_payload(monitored_address: str, history_id: str, project_id: str = "") -> dict:
    """
    Build a synthetic Gmail Pub/Sub push notification payload.

    Gmail encodes the notification body as base64 JSON with the shape
    ``{"emailAddress": "...", "historyId": "..."}``.  This mirrors exactly
    what Google's push subscription sends to /gmail-webhook.

    Args:
        monitored_address: The Gmail address being watched.
        history_id:        The historyId value Gmail would include in the push.
        project_id:        GCP project ID used to construct the Pub/Sub
                           subscription path in the payload.  When empty,
                           falls back to ``GMAIL_SUBSCRIPTION`` env var or the
                           hard-coded ``morphic-gaos-prod`` default.
                           Defaults to ``""``.

    Returns:
        A dict in Pub/Sub push message format.
    """
    data = json.dumps({"emailAddress": monitored_address, "historyId": history_id})
    data_b64 = base64.b64encode(data.encode("utf-8")).decode("utf-8")
    subscription = os.getenv("GMAIL_SUBSCRIPTION") or (
        f"projects/{project_id}/subscriptions/gmail-notifications-push"
        if project_id
        else "projects/morphic-gaos-prod/subscriptions/gmail-notifications-push"
    )
    return {
        "message": {
            "data": data_b64,
            "messageId": f"smoke-test-{uuid.uuid4().hex[:12]}",
            "publishTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "attributes": {},
        },
        "subscription": subscription,
    }


def _post_webhook(endpoint: str, payload: dict, id_token: str) -> httpx.Response:
    """
    POST the Pub/Sub push payload to /gmail-webhook with a Bearer token.

    Args:
        endpoint: Full URL including path.
        payload: The Pub/Sub push message dict.
        id_token: Google-signed OIDC token for the Cloud Run audience.

    Returns:
        The httpx Response object.
    """
    headers = {
        "Authorization": f"Bearer {id_token}",
        "Content-Type": "application/json",
    }
    return httpx.post(endpoint, json=payload, headers=headers, timeout=30)


def _poll_cloud_logging(project_id: str) -> bool:
    """
    Poll Cloud Logging for a ``gaos-agents`` process_gmail task entry.

    Looks for structured log entries from nexus-prime with log_type=``task``
    containing "gmail" emitted within the last 3 minutes (to avoid matching
    entries from a prior run).

    Args:
        project_id: GCP project ID.

    Returns:
        True if a matching entry is found within POLL_TIMEOUT seconds.
    """
    from google.cloud import logging as gcloud_logging

    client = gcloud_logging.Client(project=project_id)
    # Anchor the search to entries written after the script started
    start_epoch = int(time.time()) - 10  # 10 s buffer for clock skew
    start_rfc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start_epoch))
    filter_str = (
        f'logName="projects/{project_id}/logs/gaos-agents" '
        'jsonPayload.agent_id="nexus-prime" '
        'jsonPayload.log_type="task" '
        f'timestamp>="{start_rfc}"'
    )

    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        entries = list(
            client.list_entries(
                filter_=filter_str, order_by=gcloud_logging.DESCENDING, max_results=20
            )
        )
        for entry in entries:
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            msg = payload.get("message", "")
            if "gmail" in msg.lower() or "process_gmail" in msg.lower():
                print(f"\n  Cloud Log entry found: {msg!r}")
                return True
        remaining = int(deadline - time.time())
        print(f"  Waiting for Cloud Log entry... ({remaining}s left)  ", end="\r", flush=True)
        time.sleep(POLL_INTERVAL)

    print()
    return False


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Email smoke test: Gmail Pub/Sub push → Nexus-Prime process_gmail"
    )
    parser.add_argument(
        "--url",
        default=None,
        help=(
            "Nexus-Prime Cloud Run base URL "
            "(e.g. https://nexus-prime-XXX-uc.a.run.app). "
            "Falls back to CLOUD_RUN_URL env var, then gcloud CLI."
        ),
    )
    parser.add_argument(
        "--seed-history-id",
        default="244838",
        metavar="ID",
        help=(
            "historyId to seed into System_State if the row is absent "
            "(default: 244838). The push notification will use this ID + 1."
        ),
    )
    parser.add_argument(
        "--skip-sheet-seed",
        action="store_true",
        default=os.environ.get("SMOKE_EMAIL_SKIP_SHEET_SEED", "").lower() in ("1", "true", "yes"),
        help=(
            "Skip System_State read/write. Use when ADC lacks Sheets access locally "
            "or the history ID is already seeded. Pairs with --seed-history-id. "
            "Auto-enabled by SMOKE_EMAIL_SKIP_SHEET_SEED=1 in .env."
        ),
    )
    parser.add_argument(
        "--skip-log-poll",
        action="store_true",
        help="Skip Cloud Logging poll. Use when you only want to confirm /gmail-webhook → 200.",
    )
    args = parser.parse_args()

    print("\n=== Smoke Test: Gmail Pub/Sub push → Nexus-Prime process_gmail ===\n")

    settings = _load_settings()
    project_id: str = (settings.get("gcp") or {}).get("project_id", "")
    monitored_address: str = (settings.get("gmail") or {}).get("monitored_address", "")

    if not project_id:
        print("✗  gcp.project_id is not set in config/settings.yaml. Aborting.")
        sys.exit(1)
    if not monitored_address:
        print("✗  gmail.monitored_address is not set in config/settings.yaml. Aborting.")
        sys.exit(1)

    # ── Step 1: resolve URL ───────────────────────────────────────────────────
    print("Step 1: Resolving Cloud Run URL...")
    base_url = _resolve_url(args.url, settings)
    webhook_url = f"{base_url}/gmail-webhook"
    print(f"  Target: {webhook_url}")

    # ── Step 2: seed System_State + check watch ───────────────────────────────
    print("\nStep 2: Checking System_State (gmail_last_history_id, watch expiry)...")
    if args.skip_sheet_seed:
        effective_history_id = args.seed_history_id
        print(
            f"  --skip-sheet-seed set — using --seed-history-id={effective_history_id} without reading System_State."
        )
    else:
        try:
            effective_history_id = _seed_history_id(project_id, args.seed_history_id)
        except Exception as exc:
            print(
                f"  ✗ System_State seed failed: {type(exc).__name__}: {exc!r}\n"
                "  Possible causes:\n"
                "    • ADC not configured: run 'gcloud auth application-default login'\n"
                "    • Sheet not shared with your ADC identity\n"
                "    • No sheet_id under projects.morphic-gaos-prod in settings.yaml\n"
                "  Use --skip-sheet-seed to bypass this step and push the webhook directly."
            )
            sys.exit(1)

    if not args.skip_sheet_seed:
        try:
            _check_watch_expiry(project_id)
        except Exception as exc:
            print(f"  ⚠  Watch expiry check failed (non-fatal): {type(exc).__name__}: {exc!r}")

    # Push historyId = stored + 1 so the orchestrator advances the watermark
    # even when there are no real messages in range.
    try:
        push_history_id = str(int(effective_history_id) + 1)
    except ValueError:
        push_history_id = effective_history_id
    print(f"  Pushing historyId={push_history_id} (stored watermark: {effective_history_id})")

    # ── Step 3: obtain ID token ───────────────────────────────────────────────
    print("\nStep 3: Obtaining Google-signed OIDC token...")
    try:
        id_token = _get_id_token(
            base_url, project_id
        )  # Cloud Run ingress verifies audience = base URL
        print("  Token obtained.")
    except Exception as exc:
        print(
            f"  ✗ Could not obtain ID token: {exc}\n"
            "  Ensure ADC is configured: gcloud auth application-default login"
        )
        sys.exit(1)

    # ── Step 4: POST the push notification ───────────────────────────────────
    payload = _build_pubsub_payload(monitored_address, push_history_id, project_id)
    print(f"\nStep 4: POSTing Gmail push notification to {webhook_url}...")
    print(f"  historyId={push_history_id}  monitored={monitored_address}")
    try:
        resp = _post_webhook(webhook_url, payload, id_token)
    except httpx.RequestError as exc:
        print(f"  ✗ Request failed: {exc}")
        sys.exit(1)

    if resp.status_code != 200:
        print(f"  ✗ Unexpected HTTP {resp.status_code}: {resp.text}")
        sys.exit(1)

    print(f"  HTTP {resp.status_code} — webhook accepted.  Response: {resp.text[:120]}")

    if args.skip_log_poll:
        print("\n✓  PASS  (--skip-log-poll: HTTP 200 received, log poll skipped)\n")
        return

    # ── Step 5: poll Cloud Logging ────────────────────────────────────────────
    print(f"\nStep 5: Polling Cloud Logging for process_gmail entry (timeout {POLL_TIMEOUT}s)...")
    found = _poll_cloud_logging(project_id)

    # ── Step 6: result ────────────────────────────────────────────────────────
    logs_url = (
        f"https://console.cloud.google.com/logs/query"
        f";query=logName%3D%22projects%2F{project_id}%2Flogs%2Fgaos-agents%22"
        f"%0AjsonPayload.agent_id%3D%22nexus-prime%22"
        f"?project={project_id}"
    )
    print(f"\nCloud Logging URL:\n  {logs_url}\n")

    if found:
        print("✓  PASS  — process_gmail log entry confirmed.\n")
    else:
        print(
            "✗  FAIL  — no process_gmail log entry found within timeout.\n"
            "  Possible causes:\n"
            "  • Orchestrator event loop not running (Cloud Run scaled to 0?).\n"
            "  • Pub/Sub subscription not attached to the push topic.\n"
            "  • Gmail watch expired — run: python scripts/renew_gmail_watch.py\n"
            "  • ADC lacks Logging read permissions.\n"
            "  Check logs at the URL above for details.\n"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
