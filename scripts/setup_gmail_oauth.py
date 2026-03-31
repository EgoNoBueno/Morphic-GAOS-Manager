"""
scripts/setup_gmail_oauth.py — One-time interactive Gmail OAuth2 setup.

Walks through the full Gmail push integration setup:
  1. OAuth2 flow → refresh token → prints GMAIL_OAUTH_CREDENTIALS JSON blob
  2. Prints gcloud commands to create the gmail-notifications Pub/Sub topic
  3. Lists Gmail labels; creates GAOS-Tasks label if missing; prints label_id
  4. Calls setup_watch() to register the initial watch; prints expiration

Idempotent: safe to re-run if a prior step failed.

Prerequisites:
  - .venv activated
  - ADC configured OR a client_secrets.json from GCP Console:
      APIs & Services → Credentials → OAuth 2.0 Client IDs → Download JSON
  - Run from repo root: python scripts/setup_gmail_oauth.py [--project <project_id>]

Output values to copy into:
  - Secret Manager: GMAIL_OAUTH_CREDENTIALS
  - config/settings.yaml: gmail.monitored_address, gmail.label_id, gmail.pubsub_topic
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

DEFAULT_PROJECT = "morphic-gaos-prod"
GAOS_LABEL_NAME = "GAOS-Tasks"
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


# ── Step helpers ──────────────────────────────────────────────────────────────


def _run_oauth_flow(client_secrets_path: Path) -> dict:
    """Run InstalledAppFlow and return the credential dict.

    Args:
        client_secrets_path: Path to the client_secrets.json downloaded from GCP Console.

    Returns:
        Dict with ``client_id``, ``client_secret``, ``refresh_token``.

    Raises:
        SystemExit: If the flow fails.
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print(
            "\n[ERROR] google-auth-oauthlib not installed.\n"
            "  Run: pip install google-auth-oauthlib\n"
        )
        sys.exit(1)

    print(f"\n[Step 1] Running OAuth2 flow using: {client_secrets_path}")
    print("         A browser tab will open. Sign in and grant Gmail access.\n")

    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secrets_path), scopes=GMAIL_SCOPES
        )
        creds = flow.run_local_server(port=0)
    except Exception as exc:
        print(f"\n[ERROR] OAuth2 flow failed: {exc}\n")
        sys.exit(1)

    if not creds.refresh_token:
        print(
            "\n[ERROR] No refresh_token returned. "
            "This usually means the account already granted access in a prior session.\n"
            "Revoke access at https://myaccount.google.com/permissions and re-run.\n"
        )
        sys.exit(1)

    return {
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "refresh_token": creds.refresh_token,
    }


def _build_gmail_service(cred_dict: dict):
    """Build an authenticated Gmail service from a credential dict.

    Args:
        cred_dict: Dict with ``client_id``, ``client_secret``, ``refresh_token``.

    Returns:
        Authenticated googleapiclient Gmail resource.
    """
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=cred_dict["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=cred_dict["client_id"],
        client_secret=cred_dict["client_secret"],
        scopes=GMAIL_SCOPES,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _get_or_create_label(service, label_name: str) -> str:
    """Return the label ID for ``label_name``, creating it if missing.

    Args:
        service: Authenticated Gmail API service resource.
        label_name: Display name of the Gmail label.

    Returns:
        The Gmail label ID string (e.g. ``Label_1234567890``).
    """
    response = service.users().labels().list(userId="me").execute()
    labels: list[dict] = response.get("labels", [])

    for label in labels:
        if label.get("name", "").lower() == label_name.lower():
            label_id = label["id"]
            print(f"[Step 3] Label '{label_name}' already exists: {label_id}")
            return label_id

    # Create the label
    print(f"[Step 3] Label '{label_name}' not found — creating it...")
    new_label = service.users().labels().create(userId="me", body={"name": label_name}).execute()
    label_id = new_label["id"]
    print(f"         Created label: {label_id}")
    return label_id


def _register_watch(service, project_id: str, topic_name: str, label_id: str) -> str:
    """Register a Gmail watch() and return the expiration epoch ms as a string.

    Args:
        service: Authenticated Gmail API service resource.
        project_id: GCP project ID.
        topic_name: Full Pub/Sub topic path (``projects/<pid>/topics/<name>``).
        label_id: Gmail label ID to filter notifications.

    Returns:
        Expiration epoch milliseconds as a string.
    """
    body: dict = {
        "topicName": topic_name,
        "labelIds": [label_id],
        "labelFilterBehavior": "INCLUDE",
    }
    result = service.users().watch(userId="me", body=body).execute()
    return str(result.get("expiration", ""))


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    """Run the interactive Gmail OAuth2 setup wizard."""
    parser = argparse.ArgumentParser(description="Set up Gmail OAuth2 for GAOS push integration.")
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="GCP project ID")
    parser.add_argument(
        "--secrets",
        default="client_secrets.json",
        help="Path to client_secrets.json (default: ./client_secrets.json)",
    )
    args = parser.parse_args()

    project_id: str = args.project
    secrets_path = Path(args.secrets)

    if not secrets_path.exists():
        print(
            f"\n[ERROR] client_secrets.json not found at: {secrets_path}\n"
            "Download it from:\n"
            "  GCP Console → APIs & Services → Credentials → OAuth 2.0 Client IDs → Download JSON\n"
        )
        sys.exit(1)

    print("\n" + "=" * 60)
    print("  GAOS Gmail OAuth2 Setup")
    print("=" * 60)

    # ── Step 1: OAuth2 flow ───────────────────────────────────────────────────
    cred_dict = _run_oauth_flow(secrets_path)
    cred_json = json.dumps(cred_dict)

    print("\n" + "=" * 60)
    print("[Step 1 OUTPUT] Store this as Secret Manager secret: GMAIL_OAUTH_CREDENTIALS")
    print("=" * 60)
    print(cred_json)

    # ── Step 2: Pub/Sub topic setup ──────────────────────────────────────────
    topic_short = "gmail-notifications"
    topic_full = f"projects/{project_id}/topics/{topic_short}"

    print("\n" + "=" * 60)
    print("[Step 2] Run these gcloud commands to create the Pub/Sub topic:")
    print("=" * 60)
    print(f"  gcloud pubsub topics create {topic_short} --project={project_id}")
    print(
        f"  gcloud pubsub topics add-iam-policy-binding {topic_short} \\\n"
        f"    --role=roles/pubsub.publisher \\\n"
        f"    --member=serviceAccount:gmail-api-push@system.gserviceaccount.com \\\n"
        f"    --project={project_id}"
    )
    print("\nPress Enter after running those commands to continue...")
    input()

    # ── Step 3: Build service + resolve label ────────────────────────────────
    service = _build_gmail_service(cred_dict)

    # Get authenticated email address for monitored_address
    profile = service.users().getProfile(userId="me").execute()
    monitored_address: str = profile.get("emailAddress", "")
    print(f"\n[Step 3] Authenticated as: {monitored_address}")

    label_id = _get_or_create_label(service, GAOS_LABEL_NAME)

    # ── Step 4: Register initial watch ──────────────────────────────────────
    print(f"\n[Step 4] Registering Gmail watch() for topic: {topic_full}")
    try:
        expiration_ms = _register_watch(service, project_id, topic_full, label_id)
    except Exception as exc:
        print(f"\n[ERROR] setup_watch failed: {exc}")
        print(
            "This usually means the Pub/Sub topic does not exist yet or the IAM binding "
            "for gmail-api-push@system.gserviceaccount.com is missing.\n"
            "Complete Step 2 and re-run.\n"
        )
        sys.exit(1)

    from datetime import UTC, datetime

    expires_dt = datetime.fromtimestamp(int(expiration_ms) / 1000, tz=UTC)
    print(f"         Watch registered. Expires: {expires_dt.isoformat()}")

    # ── Final output ─────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("[DONE] Add these values to config/settings.yaml under the gmail: section:")
    print("=" * 60)
    print(f"  monitored_address: '{monitored_address}'")
    print(f"  label_id: '{label_id}'")
    print(f"  pubsub_topic: '{topic_full}'")
    print("  max_results: 50")
    print("\nStore the GMAIL_OAUTH_CREDENTIALS JSON (printed above) in Secret Manager.")
    print("Also store your authorized sender list as: GMAIL_AUTHORIZED_SENDERS")
    print("  e.g. 'alice@example.com,bob@example.com'\n")


if __name__ == "__main__":
    main()
