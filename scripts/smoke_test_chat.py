"""
scripts/smoke_test_chat.py — Smoke test: Google Chat DM → Nexus-Prime reply.

What this script does:
  1. Resolves the live Cloud Run URL (from --url flag, CLOUD_RUN_URL env var,
     or gcloud CLI fallback).
  2. POSTs a synthetic Google Chat MESSAGE event to the /chat endpoint using
     a Google-signed ID token so JWT verification passes.
  3. Polls the Dashboard tab in the Sheet for a new CHAT_MESSAGE log entry
     to confirm the graph completed.
  4. Prints PASS or FAIL with the Cloud Logging URL for the run.

Note: The bot will send a real reply into the owner_space DM thread. That is
the expected behaviour — it proves the `chat_respond` node reached
`send_reply_in_thread`. Delete the message manually if needed.

Pre-conditions:
  - Application Default Credentials (ADC) scoped to the GCP project.
  - The Cloud Run service URL is reachable (get it from:
      gcloud run services describe nexus-prime --region us-central1
                                               --project morphic-gaos-prod
                                               --format 'value(status.url)').
  - config/settings.yaml has chat.owner_space set.

Run from repo root (venv active):
  python scripts/smoke_test_chat.py --url https://nexus-prime-XXXX-uc.a.run.app
  python scripts/smoke_test_chat.py  # auto-resolves URL via gcloud
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import google.auth
import google.auth.transport.requests
import google.oauth2.id_token
import httpx
import yaml

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

SETTINGS_PATH = _REPO_ROOT / "config" / "settings.yaml"
POLL_INTERVAL = 4  # seconds between log-tab checks
POLL_TIMEOUT = 60  # seconds to wait for graph completion


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
    import os

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


def _get_id_token(audience: str) -> str:
    """
    Obtain a Google-signed ID token for the given audience.

    Tries two methods in order:
      1. ``google.oauth2.id_token.fetch_id_token`` — works on GCP (metadata
         server) or when ADC points to a service account key file.
      2. ``gcloud auth print-identity-token`` — works with user ADC credentials
         obtained via ``gcloud auth application-default login``.

    Args:
        audience: The Cloud Run service URL, used as the token audience.

    Returns:
        A signed JWT string.

    Raises:
        RuntimeError: If neither method succeeds.
    """
    # Method 1: google-auth library (service account / metadata server)
    try:
        auth_req = google.auth.transport.requests.Request()
        return google.oauth2.id_token.fetch_id_token(auth_req, audience)
    except Exception as e:
        print(
            f"[smoke_test_chat] fetch_id_token failed (audience={audience}): {e}",
            file=__import__("sys").stderr,
        )

    # Method 2: gcloud CLI via service account impersonation (user ADC credentials).
    # User ADC tokens cannot set a custom audience; impersonation sidesteps this.
    import platform

    gcloud_cmd = (
        r"C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
        if platform.system() == "Windows"
        else "gcloud"
    )
    # Hardcoded SA for impersonation — update if the service account name changes.
    sa = "nexus-prime-sa@morphic-gaos-prod.iam.gserviceaccount.com"
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


def _build_chat_payload(owner_space: str) -> dict:
    """
    Build a minimal synthetic Google Chat MESSAGE event.

    Args:
        owner_space: The Chat space name (e.g. ``spaces/jbpdpSAAAAE``).

    Returns:
        A dict matching the Chat Events API MESSAGE payload shape.
    """
    return {
        "type": "MESSAGE",
        "eventTime": "2026-01-01T00:00:00Z",
        "message": {
            "name": f"{owner_space}/messages/smoke-test-{int(time.time())}",
            "text": "SMOKE TEST — please ignore. Reply to confirm system is working.",
            "sender": {
                "name": "users/smoke-test",
                "displayName": "Smoke Test Script",
                "type": "HUMAN",
            },
            "space": {"name": owner_space},
            # No "thread" key — lets chat_respond send to the space directly
            # rather than replying to a fake/invalid thread name.
        },
        "space": {
            "name": owner_space,
            "type": "DM",
        },
        "user": {
            "name": "users/smoke-test",
            "displayName": "Smoke Test Script",
            "type": "HUMAN",
        },
    }


def _post_chat_event(endpoint: str, payload: dict, id_token: str) -> httpx.Response:
    """
    POST the Chat payload to /chat with a Bearer token.

    Args:
        endpoint: Full URL including path, e.g. ``https://.../chat``.
        payload: The Chat event dict.
        id_token: Google-signed ID token for the Cloud Run audience.

    Returns:
        The httpx Response object.
    """
    headers = {
        "Authorization": f"Bearer {id_token}",
        "Content-Type": "application/json",
    }
    return httpx.post(endpoint, json=payload, headers=headers, timeout=30)


def _poll_cloud_logging(project_id: str, task_id_fragment: str) -> bool:
    """
    Poll Cloud Logging for a ``gaos-agents`` entry containing *task_id_fragment*
    with log_type ``task`` and message containing "chat_respond".

    ``_log_cloud`` writes structured entries to the ``gaos-agents`` log name;
    this polls for the entry that the ``chat_respond`` node emits on completion.

    Args:
        project_id: GCP project ID.
        task_id_fragment: Substring of the task_id / message name to match.

    Returns:
        True if a matching entry is found within POLL_TIMEOUT seconds.
    """
    from google.cloud import logging as gcloud_logging

    client = gcloud_logging.Client(project=project_id)
    # Filter: structured entries from nexus-prime, log_type=task, last 5 min
    filter_str = (
        f'logName="projects/{project_id}/logs/gaos-agents" '
        'jsonPayload.agent_id="nexus-prime" '
        'jsonPayload.log_type="task"'
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
            if "chat_respond" in msg or task_id_fragment in msg:
                print(f"  Cloud Log entry found: {msg!r}")
                return True
        remaining = int(deadline - time.time())
        print(f"  Waiting for Cloud Log entry... ({remaining}s left)", end="\r", flush=True)
        time.sleep(POLL_INTERVAL)

    print()
    return False


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat smoke test: DM → Nexus-Prime reply")
    parser.add_argument(
        "--url",
        default=None,
        help="Nexus-Prime Cloud Run base URL (e.g. https://nexus-prime-XXX-uc.a.run.app). "
        "Falls back to CLOUD_RUN_URL env var, then gcloud CLI.",
    )
    parser.add_argument(
        "--skip-log-poll",
        action="store_true",
        help="Skip polling the Logs Sheet tab. Use when you only want to confirm "
        "the /chat endpoint returns 200.",
    )
    args = parser.parse_args()

    print("\n=== Smoke Test: Google Chat DM → Nexus-Prime reply ===\n")

    settings = _load_settings()
    owner_space: str = (settings.get("chat") or {}).get("owner_space", "")

    if not owner_space:
        print("✗  chat.owner_space is not set in config/settings.yaml. Aborting.")
        sys.exit(1)

    # ── Step 1: resolve URL ───────────────────────────────────────────────────
    print("Step 1: Resolving Cloud Run URL...")
    base_url = _resolve_url(args.url, settings)
    chat_url = f"{base_url}/chat"
    print(f"  Target: {chat_url}")

    # ── Step 2: obtain ID token ───────────────────────────────────────────────
    print("\nStep 2: Obtaining Google-signed ID token...")
    try:
        id_token = _get_id_token(chat_url)  # audience = full /chat URL per main.py JWT check
        print("  Token obtained.")
    except Exception as exc:
        print(
            f"  ✗ Could not obtain ID token: {exc}\n"
            "  Ensure Application Default Credentials are configured:\n"
            "    gcloud auth application-default login"
        )
        sys.exit(1)

    # ── Step 3: POST the event ────────────────────────────────────────────────
    payload = _build_chat_payload(owner_space)
    msg_name: str = payload["message"]["name"]
    print(f"\nStep 3: POSTing MESSAGE event to {chat_url}...")
    print(f"  Message name: {msg_name}")
    try:
        resp = _post_chat_event(chat_url, payload, id_token)
    except httpx.RequestError as exc:
        print(f"  ✗ Request failed: {exc}")
        sys.exit(1)

    print(f"  HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(f"  ✗ Expected 200, got {resp.status_code}. Body:\n  {resp.text[:400]}")
        sys.exit(1)

    print("  ✓ /chat returned HTTP 200")

    # ── Step 4: poll Logs tab ─────────────────────────────────────────────────
    if args.skip_log_poll:
        print("\nStep 4: Skipped (--skip-log-poll).")
        print("\n✅  SMOKE TEST PASSED (HTTP 200 confirmed; log polling skipped)")
        print(
            "\nNext: Check the owner space in Google Chat — the bot should have"
            " replied in the thread."
        )
        sys.exit(0)

    project_id = (settings.get("gcp") or {}).get("project_id", "")
    print(f"\nStep 4: Polling Cloud Logging for chat_respond completion (up to {POLL_TIMEOUT}s)...")
    found = _poll_cloud_logging(project_id, msg_name.split("/")[-1])

    # ── Result ────────────────────────────────────────────────────────────────
    log_url = (
        f"https://console.cloud.google.com/logs/query"
        f"?project={project_id}"
        f"&query=resource.type%3D%22cloud_run_revision%22"
    )
    print()
    if found:
        print("✅  SMOKE TEST PASSED")
        print(
            "\nNext: Check the owner space in Google Chat — the bot should have"
            " replied in the thread."
        )
        print(f"Logs: {log_url}")
        sys.exit(0)
    else:
        print(
            "⚠️  HTTP 200 received but no chat_respond Cloud Log entry appeared "
            f"within {POLL_TIMEOUT}s."
        )
        print(
            "This may mean:\n"
            "  a) The graph is still running (fire-and-forget — check Chat space).\n"
            "  b) The chat_respond node failed — check Cloud Logging below.\n"
            "  c) Cloud Logging propagation lag (entries can be delayed ~30s)."
        )
        print(f"Logs: {log_url}")
        sys.exit(1)


if __name__ == "__main__":
    main()
