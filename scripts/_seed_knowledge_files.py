"""
Create 13 placeholder seed knowledge files in the Drive Knowledge/ folder.
Safe to re-run — skips files that already exist.
"""
import io

import google.auth
import googleapiclient.discovery
import googleapiclient.http
import yaml
from google.auth.transport.requests import Request

SEED_FILES = [
    # (subfolder, filename, short content)
    ("policies", "expense_approval_policy.md", "# Expense Approval Policy\n\nPlaceholder — to be completed.\n"),
    ("policies", "vendor_payment_terms.md", "# Vendor Payment Terms\n\nPlaceholder — to be completed.\n"),
    ("policies", "data_retention_policy.md", "# Data Retention Policy\n\nPlaceholder — to be completed.\n"),
    ("policies", "communications_policy.md", "# Communications Policy\n\nPlaceholder — to be completed.\n"),
    ("policies", "research_policy.md", "# Research Policy\n\nPlaceholder — to be completed.\n"),
    ("procedures", "invoice_matching.md", "# Invoice Matching Procedure\n\nPlaceholder — to be completed.\n"),
    ("procedures", "lead_scoring_criteria.md", "# Lead Scoring Criteria\n\nPlaceholder — to be completed.\n"),
    ("procedures", "inventory_reorder_trigger.md", "# Inventory Reorder Trigger\n\nPlaceholder — to be completed.\n"),
    ("procedures", "document_filing.md", "# Document Filing Procedure\n\nPlaceholder — to be completed.\n"),
    ("procedures", "competitive_intelligence.md", "# Competitive Intelligence Methodology\n\nPlaceholder — to be completed.\n"),
    ("workflows", "ap_reconciliation.md", "# AP Reconciliation Workflow\n\nPlaceholder — to be completed.\n"),
    ("workflows", "order_fulfillment.md", "# Order Fulfillment Workflow\n\nPlaceholder — to be completed.\n"),
    ("workflows", "weekly_reporting.md", "# Weekly Reporting Workflow\n\nPlaceholder — to be completed.\n"),
]


def get_or_create_folder(svc, name: str, parent_id: str) -> str:
    escaped_name = name.replace("\\", "\\\\").replace("'", "\\'")
    q = f"'{parent_id}' in parents and name='{escaped_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = svc.files().list(q=q, fields="files(id,name)").execute().get("files", [])
    if results:
        return results[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    f = svc.files().create(body=meta, fields="id").execute()
    print(f"  Created folder: {name}")
    return f["id"]


def file_exists(svc, name: str, parent_id: str) -> bool:
    escaped_name = name.replace("\\", "\\\\").replace("'", "\\'")
    q = f"'{parent_id}' in parents and name='{escaped_name}' and trashed=false"
    results = svc.files().list(q=q, fields="files(id)").execute().get("files", [])
    return bool(results)


def main() -> None:
    with open("config/settings.yaml") as _f:
        settings = yaml.safe_load(_f)
    knowledge_folder_id = settings["projects"]["default"]["drive_folder_id"]

    # Full drive scope required: script lists/creates files inside a pre-existing
    # Knowledge folder not created by this app; drive.file scope cannot grant that access.
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/drive"])
    creds.refresh(Request())
    svc = googleapiclient.discovery.build("drive", "v3", credentials=creds, cache_discovery=False)

    folder_cache: dict[str, str] = {}
    created = 0
    skipped = 0

    for subfolder, filename, content in SEED_FILES:
        if subfolder not in folder_cache:
            folder_cache[subfolder] = get_or_create_folder(svc, subfolder, knowledge_folder_id)
        parent_id = folder_cache[subfolder]

        if file_exists(svc, filename, parent_id):
            print(f"  SKIP (exists): {subfolder}/{filename}")
            skipped += 1
            continue

        media = googleapiclient.http.MediaIoBaseUpload(
            io.BytesIO(content.encode()),
            mimetype="text/plain",
            resumable=False,
        )
        meta = {"name": filename, "parents": [parent_id]}
        svc.files().create(body=meta, media_body=media, fields="id").execute()
        print(f"  CREATED: {subfolder}/{filename}")
        created += 1

    print(f"\nDone. Created: {created}, Skipped (already existed): {skipped}")


if __name__ == "__main__":
    main()
