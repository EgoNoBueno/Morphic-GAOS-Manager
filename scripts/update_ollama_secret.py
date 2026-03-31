"""Update the OLLAMA_HOST secret in Secret Manager with a provided URL.

Usage (must have gcloud auth with permission to write secrets):

    python scripts/update_ollama_secret.py --url https://abc.ngrok.io --project morphic-gaos-prod

This will create a new Secret Manager version for the secret named `OLLAMA_HOST`.
If the secret does not exist, the script will create it.
"""

import argparse
import sys

from google.api_core.exceptions import AlreadyExists
from google.cloud import secretmanager


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True, help="OLLAMA host URL (e.g. https://abc.ngrok.io)")
    p.add_argument("--project", required=True, help="GCP project id, e.g. morphic-gaos-prod")
    args = p.parse_args()

    client = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{args.project}"
    secret_id = "OLLAMA_HOST"
    secret_name = f"projects/{args.project}/secrets/{secret_id}"

    # Create secret if missing
    try:
        client.create_secret(
            request={
                "parent": parent,
                "secret_id": secret_id,
                "secret": {"replication": {"automatic": {}}},
            }
        )
        print(f"Created secret {secret_id} in project {args.project}")
    except AlreadyExists:
        pass

    # Add new version
    payload = args.url.strip().encode("utf-8")
    response = client.add_secret_version(
        request={"parent": secret_name, "payload": {"data": payload}}
    )
    print(f"Added secret version: {response.name}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        raise
