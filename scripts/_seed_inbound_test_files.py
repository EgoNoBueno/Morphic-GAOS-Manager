"""One-off script to seed Drive Inbound/ with test files for smoke_test_archivist."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import google.auth
import google.auth.transport.requests
from google.auth import impersonated_credentials
from googleapiclient.discovery import build

source_creds, _ = google.auth.default()
source_creds.refresh(google.auth.transport.requests.Request())

# Use impersonated credentials for listing/reading (SA has enough perms)
sa_email = "steward-sa@morphic-gaos-prod.iam.gserviceaccount.com"
creds = impersonated_credentials.Credentials(
    source_credentials=source_creds,
    target_principal=sa_email,
    target_scopes=["https://www.googleapis.com/auth/drive"],
)

svc = build("drive", "v3", credentials=creds, cache_discovery=False)

root_id = "1FIFeXy3wuJHIZgczA838nLrq0tjnXdaF"

# Check if Inbound/ already exists
query = f"'{root_id}' in parents and name = 'Inbound' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
resp = svc.files().list(q=query, fields="files(id)").execute()

if resp.get("files"):
    inbound_id = resp["files"][0]["id"]
    print(f"Inbound/ folder already exists: {inbound_id}")
else:
    folder = (
        svc.files()
        .create(
            body={
                "name": "Inbound",
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [root_id],
            },
            fields="id",
        )
        .execute()
    )
    inbound_id = folder["id"]
    print(f"Created Inbound/ folder: {inbound_id}")

# For file creation, use native Google Docs format (no SA storage quota issue)
test_files = [
    "2025-Q1-Invoice-Acme-Corp",
    "marketing-campaign-brief-draft",
    "employee-onboarding-checklist",
    "random-notes",
    "project-phoenix-strategy-v2",
]

# Google Docs (native format) don't consume SA storage quota
for name in test_files:
    try:
        f = (
            svc.files()
            .create(
                body={
                    "name": name,
                    "mimeType": "application/vnd.google-apps.document",
                    "parents": [inbound_id],
                },
                fields="id,name",
            )
            .execute()
        )
        print(f"  Created: {f['name']} ({f['id']})")
    except Exception as exc:
        print(f"  Failed to create {name}: {exc}")

print("Done")
