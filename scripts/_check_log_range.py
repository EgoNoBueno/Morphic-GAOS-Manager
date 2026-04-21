"""Show earliest and latest log timestamps available in Cloud Logging for nexus-prime."""

from datetime import UTC, datetime, timedelta

import google.auth
from google.cloud import logging as gcloud_logging

PROJECT_ID = "morphic-gaos-prod"

creds, _ = google.auth.default()
creds = creds.with_quota_project(PROJECT_ID)
client = gcloud_logging.Client(project=PROJECT_ID, credentials=creds)

# Go back 48 hours
since = (datetime.now(UTC) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
filter_str = (
    'resource.type="cloud_run_revision" '
    'resource.labels.service_name="nexus-prime" '
    f'timestamp>="{since}"'
)

# Get oldest 5
oldest = list(
    client.list_entries(
        filter_=filter_str,
        order_by=gcloud_logging.ASCENDING,
        max_results=5,
    )
)
# Get newest 5
newest = list(
    client.list_entries(
        filter_=filter_str,
        order_by=gcloud_logging.DESCENDING,
        max_results=5,
    )
)

print("=== Oldest 5 entries ===")
for e in oldest:
    print(f"  {e.timestamp}  {str(e.payload)[:120]}")

print("\n=== Newest 5 entries ===")
for e in newest:
    print(f"  {e.timestamp}  {str(e.payload)[:120]}")

print(f"\nCurrent UTC time: {datetime.now(UTC)}")
