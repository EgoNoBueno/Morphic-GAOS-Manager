"""
scripts/provision_infra.py — Infrastructure change manager for Morphic-G AOS.

Compares desired infrastructure state (Cloud Scheduler jobs, BigQuery staging
tables, Secret Manager secrets) against live GCP state, then sends a plain-
language Chat card to the owner with Approve / Reject buttons.

The owner taps Approve → Chat callback → POST /chat → Nexus-Prime applies the
changes, runs health checks, and rolls back automatically on failure.

Usage:
    python scripts/provision_infra.py [--project <project_id>] [--space <chat_space>]

    --project  GCP project ID (default: gcp.project_id from settings.yaml)
    --space    Google Chat space resource name to send card to
               (default: chat.owner_space from settings.yaml)
    --dry-run  Print the diff without sending a card (no GCP writes)

Prerequisites:
  - ADC configured:  gcloud auth application-default login
  - .venv activated
  - Run from repo root

Exit codes:
  0 — diff succeeded; card sent (or no changes needed)
  1 — diff failed or card delivery failed
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import google.auth
import yaml
from googleapiclient.discovery import build as gapi_build

# Ensure repo root is on sys.path so local imports work when running from scripts/.
_REPO = Path(__file__).parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from tools.infra_provision import (  # noqa: E402
    ChangeKind,
    build_manifest,
)

DEFAULT_PROJECT = "morphic-gaos-prod"


# ── Settings helpers ──────────────────────────────────────────────────────────


def _load_raw_settings() -> dict:
    """Load config/settings.yaml without triggering the Pydantic model."""
    path = _REPO / "config" / "settings.yaml"
    if not path.exists():
        print("ERROR: config/settings.yaml not found. Run setup_workspace.py first.")
        sys.exit(1)
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ── Nexus-Prime URL resolver ──────────────────────────────────────────────────


def _get_nexus_url(run_client: object, project_id: str, region: str) -> str:
    """Resolve the live Cloud Run URL for nexus-prime.

    Args:
        run_client: Cloud Run Admin API v2 client.
        project_id: GCP project ID.
        region: GCP region.

    Returns:
        HTTPS service URL, or empty string if not found.
    """
    from googleapiclient.errors import HttpError  # type: ignore[import-untyped]

    name = f"projects/{project_id}/locations/{region}/services/nexus-prime"
    try:
        svc = run_client.projects().locations().services().get(name=name).execute()  # type: ignore[union-attr]
        return svc.get("uri", "")
    except HttpError as exc:
        print(f"  WARNING: Could not fetch nexus-prime URL: {exc}")
        return ""


# ── Print helpers ─────────────────────────────────────────────────────────────

_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"
_RESET = "\033[0m"
_RED = "\033[31m"


def _print_change(kind: ChangeKind, description: str) -> None:
    colour = {ChangeKind.CREATE: _GREEN, ChangeKind.UPDATE: _YELLOW}.get(kind, _RESET)
    tag = {ChangeKind.CREATE: "[CREATE]", ChangeKind.UPDATE: "[UPDATE]"}.get(kind, "[     ]")
    print(f"  {colour}{tag}{_RESET}  {description}")


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Infrastructure change manager for GAOS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--project", default=None, help="GCP project ID")
    parser.add_argument("--space", default=None, help="Google Chat space resource name")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print diff only — do not write to Sheet or send Chat card",
    )
    args = parser.parse_args()

    settings = _load_raw_settings()
    project_id: str = args.project or settings.get("gcp", {}).get("project_id", DEFAULT_PROJECT)
    region: str = settings.get("gcp", {}).get("region", "us-central1")
    owner_space: str = args.space or settings.get("chat", {}).get("owner_space", "")
    sa_email = f"nexus-prime-sa@{project_id}.iam.gserviceaccount.com"

    print(f"Project  : {project_id}")
    print(f"Region   : {region}")
    print(f"SA email : {sa_email}")
    print(f"Dry run  : {args.dry_run}")
    print()

    print("Authenticating via ADC...")
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])  # noqa: F823

    run_client = gapi_build("run", "v2", credentials=creds)
    print("Resolving nexus-prime Cloud Run URL...")
    nexus_url = _get_nexus_url(run_client, project_id, region)
    if nexus_url:
        print(f"  nexus-prime URL: {nexus_url}")
    else:
        print("  nexus-prime URL not found — scheduler job targets will use empty URL")
    print()

    print("Building infrastructure diff...")
    manifest = build_manifest(
        project_id=project_id,
        region=region,
        nexus_url=nexus_url,
        sa_email=sa_email,
    )
    print()

    # ── Print diff ────────────────────────────────────────────────────────────
    resource_headers = {
        "SCHEDULER_JOB": "Cloud Scheduler Jobs",
        "BQ_TABLE": "BigQuery Staging Tables",
        "SECRET": "Secret Manager Secrets",
    }
    last_rtype = None
    any_action = False
    for entry in manifest.changes:
        if entry.resource_type != last_rtype:
            print(
                f"{_CYAN}── {resource_headers.get(str(entry.resource_type), str(entry.resource_type))} ──{_RESET}"
            )
            last_rtype = entry.resource_type
        if entry.kind != ChangeKind.NO_CHANGE:
            any_action = True
            _print_change(entry.kind, entry.human_description)
        else:
            print(f"  [  OK  ]  {entry.resource_id}")
    print()

    if not any_action:
        print(f"{_GREEN}✅ Nothing to do — all resources are already up to date.{_RESET}")
        sys.exit(0)

    # Summary
    creates = sum(1 for e in manifest.actionable if e.kind == ChangeKind.CREATE)
    updates = sum(1 for e in manifest.actionable if e.kind == ChangeKind.UPDATE)
    irreversible = sum(1 for e in manifest.actionable if e.irreversible)

    print(f"Changes: {creates} to create, {updates} to update")
    if irreversible:
        print(
            f"{_YELLOW}⚠️  {irreversible} action(s) cannot be automatically reversed "
            f"(BigQuery table creation is permanent storage).{_RESET}"
        )
    print()

    if args.dry_run:
        print("Dry-run mode — stopping here. No card sent, no Sheet written.")
        sys.exit(0)

    if not owner_space:
        print(
            "ERROR: No Chat space configured. Set chat.owner_space in settings.yaml "
            "or pass --space spaces/XXXXXXX"
        )
        sys.exit(1)

    print("Writing proposal to Agent_Approvals and sending Chat card...")
    print("(This calls POST /infra-provision on nexus-prime)")
    print()

    # Trigger the plan handler on nexus-prime via a direct HTTP call.
    # This keeps auth (OIDC) and sheet-write logic in one place (orchestrator).
    import httpx

    try:
        import google.oauth2.id_token
        from google.auth.transport.requests import Request as GoogleRequest

        id_token = google.oauth2.id_token.fetch_id_token(GoogleRequest(), audience=nexus_url)
        headers = {
            "Authorization": f"Bearer {id_token}",
            "Content-Type": "application/json",
        }
    except Exception as exc:
        print(f"WARNING: Could not fetch OIDC token ({exc}) — trying without auth (local dev)")
        headers = {"Content-Type": "application/json"}

    endpoint = nexus_url.rstrip("/") + "/infra-provision"
    payload = {"project_id": project_id, "space_name": owner_space}

    try:
        resp = httpx.post(endpoint, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        print(f"  proposal_id  : {data.get('proposal_id', 'unknown')}")
        print(f"  change_count : {data.get('change_count', '?')}")
        print()
        print("✅ Card sent. Waiting for owner approval in Google Chat.")
    except Exception as exc:
        print(f"\nERROR: Could not reach nexus-prime at {endpoint}: {exc}")
        print(
            "If running locally without a deployed nexus-prime, use --dry-run "
            "or deploy the service first."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
