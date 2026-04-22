"""Live pipeline trace — queries the last 20 minutes of nexus-prime logs."""

import os
from datetime import UTC, datetime, timedelta

import google.auth
from google.cloud import logging as gcloud_logging

MAX_LOG_FETCH = int(os.environ.get("MAX_LOG_FETCH", "500"))


def main() -> None:
    creds, _ = google.auth.default()
    creds = creds.with_quota_project("morphic-gaos-prod")
    client = gcloud_logging.Client(project="morphic-gaos-prod", credentials=creds)

    now = datetime.now(UTC)
    start = now - timedelta(minutes=20)
    start_str = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    filter_str = (
        'resource.type="cloud_run_revision" '
        'resource.labels.service_name="nexus-prime" '
        f'timestamp>="{start_str}" '
        f'timestamp<="{end_str}"'
    )
    try:
        entries = list(
            client.list_entries(
                filter_=filter_str,
                order_by=gcloud_logging.ASCENDING,
                max_results=MAX_LOG_FETCH,
            )
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch log entries (filter={filter_str!r}): {exc}") from exc

    if len(entries) == MAX_LOG_FETCH:
        print(
            f"WARNING: result set hit the {MAX_LOG_FETCH}-entry limit — output may be truncated.\n"
        )

    print(f"=== nexus-prime logs {start_str} → {end_str} ({len(entries)} entries) ===\n")
    for e in entries:
        ts = e.timestamp.strftime("%H:%M:%S") if e.timestamp else "?"
        payload = e.payload
        # Highlight key pipeline steps
        marker = ""
        if isinstance(payload, dict):
            msg = payload.get("message", "")
            if any(
                k in msg
                for k in (
                    "process_gmail",
                    "compose_reply",
                    "send_email",
                    "inbound",
                    "classify",
                    "draft",
                    "idempotency",
                    "skipping",
                    "flood",
                    "outbound",
                )
            ):
                marker = "  <<<<<"
        print(f"[{ts}] {str(payload)[:500]}{marker}")

    print(f"\n--- {len(entries)} entries ---")


if __name__ == "__main__":
    main()
