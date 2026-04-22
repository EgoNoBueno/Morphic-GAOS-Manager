"""One-off: check Cloud Logging for send_email / email-sent entries (last 24 h)."""

import json
from datetime import UTC, datetime, timedelta

import google.auth
from google.cloud import logging as gcloud_logging


def main() -> None:
    creds, _ = google.auth.default()
    creds = creds.with_quota_project("morphic-gaos-prod")
    client = gcloud_logging.Client(project="morphic-gaos-prod", credentials=creds)

    since = (datetime.now(UTC) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    filter_str = (
        'resource.type="cloud_run_revision" '
        'resource.labels.service_name="nexus-prime" '
        f'timestamp>="{since}" '
        'jsonPayload.message=~"(?i)send_email|email sent|reply sent|msg_id"'
    )
    entries = list(
        client.list_entries(
            filter_=filter_str,
            order_by=gcloud_logging.DESCENDING,
            max_results=50,
        )
    )
    if not entries:
        print("No send_email entries in the last 24 hours.")
    else:
        print(f"Found {len(entries)} entries:\n")
        for e in reversed(entries):
            ts = e.timestamp.strftime("%Y-%m-%d %H:%M:%S") if e.timestamp else "?"
            payload = e.payload
            if isinstance(payload, dict):
                msg = payload.get("message", "")
                display = msg[:300] if msg else json.dumps(payload, indent=2)[:300]
            else:
                display = str(payload)[:300]
            print(f"[{ts}] {display}")


if __name__ == "__main__":
    main()
