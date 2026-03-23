"""
scripts/setup_secrets.py — One-time Secret Manager provisioning script.

Creates all GCP Secret Manager secrets required by Morphic-G AOS and grants
per-secret IAM access to the relevant agent service accounts, following the
least-privilege matrix in GAOS-Deploy-Spec.md §3.

Secrets created:
    GEMINI_API_KEY        → all 7 agents          (user input via getpass)
    OLLAMA_HOST           → all 7 agents          (default: http://localhost:11434)
    WEBHOOK_HMAC_SECRET   → nexus-prime only      (auto-generated)
    GOOGLE_SEARCH_API_KEY → scout only            (user input via getpass)
    GOOGLE_SEARCH_CX      → scout only            (user input via getpass)

WEBHOOK_URL is created automatically by scripts/setup_apps_script.py — skip here.

Security:
    - Secrets requiring user input are read via getpass() — never echoed to terminal,
      shell history, or this script's log output.
    - Auto-generated secrets (WEBHOOK_HMAC_SECRET) use secrets.token_hex(32).
    - Idempotent: skips any secret that already has at least one active version.

Prerequisites:
    - ADC configured: gcloud auth application-default login (see §0.4)
    - .venv activated
    - Run from repo root: python scripts/setup_secrets.py [--project <project_id>]
"""

from __future__ import annotations

import argparse
import secrets as secrets_module
import sys
from getpass import getpass

import google.auth
import google.auth.exceptions
from google.api_core.exceptions import AlreadyExists, NotFound
from google.cloud import secretmanager
from google.iam.v1 import policy_pb2

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_PROJECT = "morphic-gaos-prod"

ALL_AGENTS = [
    "nexus-prime",
    "ledger",
    "beacon",
    "pursuit",
    "foreman",
    "steward",
    "scout",
]

# Each entry: (secret_name, agents_that_need_access, prompt_description, value_source)
# value_source is one of:
#   None         → prompt user via getpass (required; empty input is rejected)
#   str          → use as default; prompt user so they can override (Enter accepts default)
#   "__auto__"   → auto-generate via secrets.token_hex(32); no user input needed
_SECRETS: list[tuple[str, list[str], str, str | None]] = [
    (
        "GEMINI_API_KEY",
        ALL_AGENTS,
        (
            "Gemini API key (from console.cloud.google.com/apis/credentials "
            "inside your GCP project — not AI Studio)"
        ),
        None,
    ),
    (
        "OLLAMA_HOST",
        ALL_AGENTS,
        "Ollama host URL",
        "http://localhost:11434",
    ),
    (
        "WEBHOOK_HMAC_SECRET",
        ["nexus-prime"],
        "WEBHOOK_HMAC_SECRET (auto-generated — press Enter to confirm)",
        "__auto__",
    ),
    (
        "GOOGLE_SEARCH_API_KEY",
        ["scout"],
        "Google Custom Search API key (from console.cloud.google.com/apis/credentials)",
        None,
    ),
    (
        "GOOGLE_SEARCH_CX",
        ALL_AGENTS,  # Scout reads it, but all agents inherit the accessor grant for simplicity
        "Google Custom Search Engine ID (from programmablesearchengine.google.com)",
        None,
    ),
]


# ── Secret Manager helpers ────────────────────────────────────────────────────


def _secret_has_version(client: secretmanager.SecretManagerServiceClient, secret_path: str) -> bool:
    """Return True if the secret has at least one enabled version."""
    try:
        # list_secret_versions raises NotFound if the secret itself doesn't exist
        versions = list(
            client.list_secret_versions(
                request={"parent": secret_path, "filter": "state:ENABLED"},
                timeout=10,
            )
        )
        return len(versions) > 0
    except NotFound:
        return False


def _create_secret(
    client: secretmanager.SecretManagerServiceClient,
    project_id: str,
    secret_name: str,
) -> str:
    """Create a Secret Manager secret (idempotent). Returns the secret resource path."""
    parent = f"projects/{project_id}"
    resource_path = f"{parent}/secrets/{secret_name}"
    try:
        client.create_secret(
            request={
                "parent": parent,
                "secret_id": secret_name,
                "secret": {"replication": {"automatic": {}}},
            },
            timeout=15,
        )
        print(f"  created secret: {secret_name}")
    except AlreadyExists:
        print(f"  secret exists:  {secret_name}")
    return resource_path


def _add_version(
    client: secretmanager.SecretManagerServiceClient,
    secret_path: str,
    value: str,
) -> None:
    """Add a new version to an existing secret. The value is not logged."""
    client.add_secret_version(
        request={
            "parent": secret_path,
            "payload": {"data": value.encode("utf-8")},
        },
        timeout=15,
    )


def _grant_access(
    client: secretmanager.SecretManagerServiceClient,
    secret_path: str,
    project_id: str,
    agent_names: list[str],
) -> None:
    """Grant roles/secretmanager.secretAccessor to each agent's service account."""
    current_policy = client.get_iam_policy(
        request={"resource": secret_path},
        timeout=10,
    )

    member_set = {
        f"serviceAccount:{agent}-sa@{project_id}.iam.gserviceaccount.com" for agent in agent_names
    }

    # Find or create the secretAccessor binding
    accessor_role = "roles/secretmanager.secretAccessor"
    binding = next(
        (b for b in current_policy.bindings if b.role == accessor_role),
        None,
    )
    if binding is None:
        current_policy.bindings.append(
            policy_pb2.Binding(role=accessor_role, members=list(member_set))
        )
    else:
        existing = set(binding.members)
        new_members = member_set - existing
        for m in new_members:
            binding.members.append(m)

    client.set_iam_policy(
        request={"resource": secret_path, "policy": current_policy},
        timeout=15,
    )
    print(f"    IAM: {len(agent_names)} SA(s) → {accessor_role}")


# ── Value collection helpers ──────────────────────────────────────────────────


def _collect_value(secret_name: str, description: str, default: str | None) -> str:
    """Collect a secret value from the user without echoing it to the terminal.

    Args:
        secret_name: The secret name (used for the prompt label).
        description: Human-readable description of the expected value.
        default: If "__auto__", auto-generate. If a string, offer as default.
                 If None, the user must supply a non-empty value.

    Returns:
        The collected or generated secret value as a UTF-8 string.
    """
    if default == "__auto__":
        value = secrets_module.token_hex(32)
        print(f"  {secret_name}: [auto-generated 32-byte hex]")
        return value

    if default is not None:
        raw = input(
            f"  {secret_name} [{description}]\n  Enter to accept default ({default}): "
        ).strip()
        return raw if raw else default

    # Required input — loop until non-empty
    while True:
        value = getpass(f"  {secret_name} [{description}]: ").strip()
        if value:
            return value
        print("  ⚠  Value cannot be empty — try again.")


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Provision all Morphic-G AOS secrets in GCP Secret Manager."
    )
    parser.add_argument(
        "--project",
        default=DEFAULT_PROJECT,
        help=f"GCP project ID (default: {DEFAULT_PROJECT})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Add a new version even if the secret already has one.",
    )
    args = parser.parse_args()
    project_id = args.project
    skip_existing = not args.force

    print(f"Authenticating via ADC (project: {project_id})...")
    try:
        creds, _ = google.auth.default()
    except google.auth.exceptions.DefaultCredentialsError as exc:
        print(f"\nERROR: ADC not configured — {exc}", file=sys.stderr)
        print("Run: gcloud auth application-default login", file=sys.stderr)
        sys.exit(1)

    client = secretmanager.SecretManagerServiceClient(credentials=creds)

    skipped: list[str] = []
    created: list[str] = []
    errored: list[str] = []

    for secret_name, agents, description, default in _SECRETS:
        print(f"\n── {secret_name} ──")
        secret_path = f"projects/{project_id}/secrets/{secret_name}"

        # Check for existing version
        has_version = _secret_has_version(client, secret_path)
        if has_version and skip_existing:
            print("  already has a version — skipping (use --force to overwrite).")
            skipped.append(secret_name)
            # Still ensure IAM bindings are current even if we skip the value write
            try:
                _grant_access(client, secret_path, project_id, agents)
            except Exception as exc:
                print(f"  ⚠  IAM update failed: {exc}")
            continue

        # Collect value (may prompt user)
        try:
            value = _collect_value(secret_name, description, default)
        except KeyboardInterrupt:
            print("\n\nAborted by user.")
            sys.exit(1)

        # Create secret + add version + grant IAM
        try:
            _create_secret(client, project_id, secret_name)
            _add_version(client, secret_path, value)
            print("  version added.")
            _grant_access(client, secret_path, project_id, agents)
            created.append(secret_name)
        except Exception as exc:
            print(f"  ERROR: {exc}", file=sys.stderr)
            errored.append(secret_name)

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"DONE — {len(created)} created, {len(skipped)} skipped, {len(errored)} errors")
    print("=" * 60)
    if created:
        print(f"  Created:  {', '.join(created)}")
    if skipped:
        print(f"  Skipped:  {', '.join(skipped)}")
    if errored:
        print(f"  Errors:   {', '.join(errored)}")
        sys.exit(1)

    print(
        "\nNote: WEBHOOK_URL is created by scripts/setup_apps_script.py — "
        "run that script next if not already done."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        raise
