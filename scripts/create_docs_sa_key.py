"""
scripts/create_docs_sa_key.py — Create a service account key for nexus-prime-sa
and store it in Secret Manager as 'docs-service-account-key'.

WHY THIS EXISTS
---------------
Cloud Run ADC issues tokens with cloud-platform scope only. The Google Docs API
explicitly rejects cloud-platform tokens — it requires 'documents' or 'drive'
scope. A service account key file bypasses the metadata server entirely and
requests exactly the scopes needed.

This script runs ONCE. After it runs, the key lives in Secret Manager and is
never written to disk permanently. The container reads it at call time via
tools.secrets.get_secret("docs-service-account-key", project_id).

Run with user ADC (gcloud auth application-default login already done):
    python scripts/create_docs_sa_key.py

SECURITY NOTE
-------------
Service account keys are sensitive credentials. This script:
- Creates a temporary key file in memory (bytes), not on disk
- Deletes the local temp file immediately after storing in Secret Manager
- Stores the key in Secret Manager (encrypted at rest by Google)
- The key is scoped to nexus-prime-sa which already has limited IAM permissions
"""

from __future__ import annotations

import json
import sys

import google.auth
from google.auth.exceptions import DefaultCredentialsError
from google.cloud import secretmanager
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

PROJECT_ID = "morphic-gaos-prod"
SA_EMAIL = "nexus-prime-sa@morphic-gaos-prod.iam.gserviceaccount.com"
SECRET_ID = "docs-service-account-key"

SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/iam",
]


def main() -> None:
    print(f"Creating SA key for {SA_EMAIL} ...")

    try:
        creds, _ = google.auth.default(scopes=SCOPES)
    except DefaultCredentialsError as exc:
        print(f"ERROR: No ADC — run `gcloud auth application-default login` first.\n{exc}")
        sys.exit(1)

    iam = build("iam", "v1", credentials=creds, cache_discovery=False)
    sm = secretmanager.SecretManagerServiceClient(credentials=creds)

    # 1. Create the SA key (returns JSON bytes)
    try:
        key_resp = (
            iam.projects()
            .serviceAccounts()
            .keys()
            .create(
                name=f"projects/{PROJECT_ID}/serviceAccounts/{SA_EMAIL}",
                body={
                    "keyAlgorithm": "KEY_ALG_RSA_2048",
                    "privateKeyType": "TYPE_GOOGLE_CREDENTIALS_FILE",  # pragma: allowlist secret
                },
            )
            .execute()
        )
    except HttpError as exc:
        print(f"ERROR: Failed to create SA key: {exc}")
        sys.exit(1)

    import base64

    key_bytes: bytes = base64.b64decode(key_resp["privateKeyData"])
    key_json_str = key_bytes.decode("utf-8")

    # Validate it's valid JSON
    try:
        json.loads(key_json_str)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Key response is not valid JSON: {exc}")
        sys.exit(1)

    print(f"SA key created: {key_resp.get('name', '?')}")

    # 2. Store in Secret Manager — create secret if it doesn't exist
    secret_name = f"projects/{PROJECT_ID}/secrets/{SECRET_ID}"
    try:
        sm.get_secret(name=secret_name)
        print(f"Secret '{SECRET_ID}' already exists — adding a new version.")
    except Exception:
        # Create the secret
        sm.create_secret(
            parent=f"projects/{PROJECT_ID}",
            secret_id=SECRET_ID,
            secret={"replication": {"automatic": {}}},
        )
        print(f"Secret '{SECRET_ID}' created.")

    version = sm.add_secret_version(
        parent=secret_name,
        payload={"data": key_json_str.encode("utf-8")},
    )
    print(f"Secret version stored: {version.name}")

    # 3. Grant nexus-prime-sa access to the new secret so it can read it on Cloud Run
    try:
        resource = sm.get_iam_policy(request={"resource": secret_name})
        # Build the binding
        already_bound = any(
            b.role == "roles/secretmanager.secretAccessor"
            and f"serviceAccount:{SA_EMAIL}" in b.members
            for b in resource.bindings
        )
        if not already_bound:
            resource.bindings.add(
                role="roles/secretmanager.secretAccessor",
                members=[f"serviceAccount:{SA_EMAIL}"],
            )
            sm.set_iam_policy(request={"resource": secret_name, "policy": resource})
            print(f"Granted secretAccessor on '{SECRET_ID}' to {SA_EMAIL}")
        else:
            print(f"{SA_EMAIL} already has secretAccessor on '{SECRET_ID}' — skipped.")
    except Exception as exc:
        print(f"WARNING: Could not set IAM on secret (may already be accessible): {exc}")

    print("\nDone. The docs-service-account-key secret is ready.")
    print("Next: deploy the updated google_docs.py so containers pick it up automatically.")


if __name__ == "__main__":
    main()
