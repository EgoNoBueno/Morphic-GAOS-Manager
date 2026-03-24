"""
scripts/share_blueprints_folder.py — Grant nexus-prime SA editor access
to the Project_Incubator Drive folder.

Run once with your user ADC (gcloud auth application-default login already done).
This does NOT require any GCP service account keys — it uses the locally cached
OAuth user credentials from `gcloud auth application-default login`.

Usage:
    python scripts/share_blueprints_folder.py
"""

from __future__ import annotations

import sys

import google.auth
from google.auth.exceptions import DefaultCredentialsError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

FOLDER_ID = "16zys8fDgYaUyn-FyFb2lhrU3Asb2VYLR"  # Project_Incubator
SA_EMAIL = "nexus-prime-sa@morphic-gaos-prod.iam.gserviceaccount.com"

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]


def main() -> None:
    print(f"Sharing folder {FOLDER_ID} with {SA_EMAIL} as editor...")

    try:
        creds, project = google.auth.default(scopes=DRIVE_SCOPES)
    except DefaultCredentialsError as exc:
        print(f"ERROR: No ADC found — run `gcloud auth application-default login` first.\n{exc}")
        sys.exit(1)

    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    permission = {
        "type": "user",
        "role": "writer",
        "emailAddress": SA_EMAIL,
    }

    try:
        result = (
            drive.permissions()
            .create(
                fileId=FOLDER_ID,
                body=permission,
                sendNotificationEmail=False,
                fields="id,emailAddress,role",
            )
            .execute()
        )
        print(
            f"SUCCESS: permission id={result['id']} role={result['role']} email={result['emailAddress']}"
        )
    except HttpError as exc:
        print(f"ERROR: Drive API call failed: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
