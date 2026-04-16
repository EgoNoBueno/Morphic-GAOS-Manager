"""One-off: tail nexus-prime logs over a configurable window, filtered by keyword."""

import sys
from datetime import UTC, datetime, timedelta

import google.auth
from google.cloud import logging as gcloud_logging

try:
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 6
except ValueError:
    print("Usage: python _check_logs_range.py [hours:int] [pattern:str]")
    sys.exit(1)
pattern = sys.argv[2] if len(sys.argv) > 2 else "process_gmail"

creds, _ = google.auth.default(quota_project_id="morphic-gaos-prod")
client = gcloud_logging.Client(project="morphic-gaos-prod", credentials=creds)

since = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
filter_str = (
    'resource.type="cloud_run_revision" '
    'resource.labels.service_name="nexus-prime" '
    f'timestamp>="{since}"'
)
entries = list(
    client.list_entries(
        filter_=filter_str,
        order_by=gcloud_logging.ASCENDING,
        max_results=500,
    )
)
matched = []
for e in entries:
    payload = e.payload if isinstance(e.payload, str) else str(e.payload)
    if pattern.lower() in payload.lower():
        matched.append((e.timestamp, payload))

if not matched:
    print(f"No entries matching '{pattern}' in last {hours} hours.")
else:
    for ts, payload in matched:
        ts_str = ts.strftime("%H:%M:%S") if ts else "??:??:??"
        print(f"[{ts_str}] {payload[:400]}")
