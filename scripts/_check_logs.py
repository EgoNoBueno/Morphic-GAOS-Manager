"""One-off: tail recent nexus-prime Cloud Run logs via Python SDK."""

from datetime import UTC, datetime, timedelta

import google.auth
from google.cloud import logging as gcloud_logging

creds, _ = google.auth.default()
creds = creds.with_quota_project("morphic-gaos-prod")
client = gcloud_logging.Client(project="morphic-gaos-prod", credentials=creds)

since = (datetime.now(UTC) - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
filter_str = (
    'resource.type="cloud_run_revision" '
    'resource.labels.service_name="nexus-prime" '
    f'timestamp>="{since}"'
)
entries = list(
    client.list_entries(
        filter_=filter_str,
        order_by=gcloud_logging.DESCENDING,
        max_results=60,
        page_size=60,
    )
)
if not entries:
    print("No log entries in last 20 minutes.")
else:
    for e in reversed(entries):
        ts = e.timestamp.strftime("%H:%M:%S") if e.timestamp else "??:??:??"
        payload = str(e.payload)[:300]
        print(f"[{ts}] {payload}")
