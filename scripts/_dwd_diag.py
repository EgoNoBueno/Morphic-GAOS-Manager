"""Diagnostic: test DWD (Domain-Wide Delegation) step by step."""

from __future__ import annotations

import sys

import google.auth  # type: ignore[import-untyped]
from google.auth import iam as google_auth_iam  # type: ignore[import-untyped]
from google.auth.transport import requests as google_auth_requests  # type: ignore[import-untyped]
from google.oauth2 import service_account  # type: ignore[import-untyped]
from googleapiclient.discovery import build  # type: ignore[import-untyped]

SA_EMAIL = "nexus-prime-sa@morphic-gaos-prod.iam.gserviceaccount.com"
DWD_SUBJECT = "dhess@sl10repairtechs.com"
SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]


def main() -> None:
    print("1. Getting ADC credentials...")
    source_creds, project = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    request = google_auth_requests.Request()
    source_creds.refresh(request)
    print(f"   ADC project: {project}")
    print(f"   ADC SA: {getattr(source_creds, 'service_account_email', 'N/A')}")

    print("2. Creating IAM Signer...")
    signer = google_auth_iam.Signer(
        request=request,
        credentials=source_creds,
        service_account_email=SA_EMAIL,
    )

    print(f"3. Creating DWD credentials (subject={DWD_SUBJECT})...")
    creds = service_account.Credentials(
        signer=signer,
        service_account_email=SA_EMAIL,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
        subject=DWD_SUBJECT,
    )

    print("4. Refreshing (DWD token exchange)...")
    try:
        creds.refresh(request)
        print(f"   Token obtained! Expires: {creds.expiry}")
        print(f"   Token prefix: {creds.token[:20]}...")
    except Exception as exc:
        print(f"   FAILED: {type(exc).__name__}: {exc}")
        sys.exit(1)

    print("5. Testing Docs API create...")
    svc = build("docs", "v1", credentials=creds, cache_discovery=False)
    try:
        doc = svc.documents().create(body={"title": "DWD Test Doc"}).execute()
        doc_id = doc["documentId"]
        print(f"   SUCCESS! Doc created: {doc_id}")
        # Clean up
        drive_svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        drive_svc.files().delete(fileId=doc_id).execute()
        print("   Cleaned up test doc.")
    except Exception as exc:
        print(f"   FAILED: {type(exc).__name__}: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
