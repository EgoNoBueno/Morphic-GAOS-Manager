"""Renew the Gmail push watch using credentials stored in Secret Manager.

Loads GMAIL_OAUTH_CREDENTIALS from Secret Manager (no client_secrets.json required),
builds the Gmail service, and registers a fresh watch.  Run this locally whenever
the watch has expired or needs to be re-registered after a topic/label change.

Usage:
    python scripts/renew_gmail_watch.py --project morphic-gaos-prod
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# Allow imports from the project root (tools/, config/, etc.)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import get_settings
from tools.secrets import get_secret

DEFAULT_PROJECT = "morphic-gaos-prod"


def renew_watch(project_id: str) -> None:
    """Fetch credentials from Secret Manager and re-register the Gmail watch.

    Args:
        project_id: GCP project that owns the GMAIL_OAUTH_CREDENTIALS secret.
    """
    settings = get_settings()
    topic_name: str = settings.gmail.pubsub_topic
    # Always watch INBOX — Gmail applies the watch filter before user label rules run,
    # so custom labels (e.g. Label_6 / GAOS-Tasks) never trigger push for plain inbox mail.
    labels: list[str] = ["INBOX"]

    print(f"[1/4] Loading GMAIL_OAUTH_CREDENTIALS from Secret Manager ({project_id})...")
    token_json = get_secret("GMAIL_OAUTH_CREDENTIALS", project_id)
    token_data = json.loads(token_json)

    print("[2/4] Building Gmail service...")
    try:
        creds = Credentials.from_authorized_user_info(token_data)
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    except Exception as exc:
        print(f"      ERROR: Failed to build Gmail service: {exc}", file=sys.stderr)
        sys.exit(1)

    # Verify which account we're operating on
    try:
        profile = service.users().getProfile(userId="me").execute()
    except Exception as exc:
        print(f"      ERROR: Failed to fetch Gmail profile: {exc}", file=sys.stderr)
        sys.exit(1)
    monitored = profile.get("emailAddress", "<unknown>")
    print(f"      Authenticated as: {monitored}")

    print(f"[3/4] Registering watch → topic={topic_name}, labelIds={labels}...")
    body: dict = {
        "topicName": topic_name,
        "labelIds": labels,
        "labelFilterBehavior": "INCLUDE",
    }
    try:
        result = service.users().watch(userId="me", body=body).execute()
    except Exception as exc:
        print(f"      ERROR: Failed to register Gmail watch: {exc}", file=sys.stderr)
        sys.exit(1)

    expiration_ms = int(result.get("expiration", 0))
    history_id = result.get("historyId", "<none>")
    expires_dt = datetime.fromtimestamp(expiration_ms / 1000, tz=UTC)

    print("[4/4] Watch registered successfully.")
    print(f"      historyId : {history_id}")
    print(f"      Expires   : {expires_dt.isoformat()}")
    print(
        "\nNext: send a test email to the monitored inbox and check Cloud Run logs for "
        "nexus-prime /gmail-webhook hits."
    )


def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(description="Renew the GAOS Gmail push watch.")
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="GCP project ID")
    args = parser.parse_args()
    renew_watch(args.project)


if __name__ == "__main__":
    main()
