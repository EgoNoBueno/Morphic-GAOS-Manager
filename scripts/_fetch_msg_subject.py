"""Fetch subject/from headers for specific Gmail message IDs."""

import json

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

with open("oauth_creds_output.json", encoding="utf-8") as f:
    creds_data = json.load(f)
creds = Credentials(
    token=creds_data.get("token"),
    refresh_token=creds_data.get("refresh_token"),
    token_uri=creds_data.get("token_uri", "https://oauth2.googleapis.com/token"),
    client_id=creds_data.get("client_id"),
    client_secret=creds_data.get("client_secret"),
)
svc = build("gmail", "v1", credentials=creds)

for mid in ["19d985880369320e", "19d9862198d66c8b"]:  # pragma: allowlist secret
    try:
        msg = (
            svc.users()
            .messages()
            .get(userId="me", id=mid, format="metadata", metadataHeaders=["Subject", "From"])
            .execute()
        )
        hdrs = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        subject = hdrs.get("Subject", "(no subject header)")
        from_addr = hdrs.get("From", "?")
        print(f"{mid}: From={from_addr!r}  Subject={subject!r}")
    except Exception as exc:
        print(f"{mid}: ERROR {exc}")
