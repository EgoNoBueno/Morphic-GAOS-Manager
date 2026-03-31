"""
scripts/provision_schedulers.py — Idempotently provision Cloud Scheduler jobs
for the two Nexus-Prime scheduled endpoints.

Jobs created (or patched if they already exist):
  gaos-archive    → POST /archive     at 02:00 daily  (America/Argentina/Buenos_Aires)
  gaos-daily-sync → POST /daily-sync  at 06:00 daily  (America/Argentina/Buenos_Aires)

Both jobs authenticate with an OIDC token issued for the nexus-prime service
account, which is the same identity that runs the Cloud Run service.

Prerequisites:
  - ADC configured:  gcloud auth application-default login
  - .venv activated
  - Run from repo root: python scripts/provision_schedulers.py [--project <project_id>]

Design notes (chapter 7 of OpenClaw Paradigm Book):
  - Idempotent: calling this script multiple times is safe. Existing jobs are
    patched (PUT) using the updateMask parameter so the schedule and body are
    brought into sync with this script without deleting and recreating.
  - The nexus-prime Cloud Run URL is resolved dynamically via the Cloud Run
    Admin API — no hardcoded URLs.
  - Cloud Scheduler location defaults to the project region in settings.yaml.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import google.auth
import yaml
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_PROJECT = "morphic-gaos-prod"
TIMEZONE = "America/Argentina/Buenos_Aires"

JOBS: list[dict] = [
    {
        "id": "gaos-archive",
        "schedule": "0 2 * * *",
        "path": "/archive",
        "description": "Nightly Sheet → BigQuery archive sweep (Nexus-Prime §9.5)",
    },
    {
        "id": "gaos-daily-sync",
        "schedule": "0 6 * * *",
        "path": "/daily-sync",
        "description": "Morning briefing Chat card (Nexus-Prime §2.5)",
    },
    {
        "id": "gaos-sheets-sync",
        "schedule": "*/5 * * * *",
        "path": "/sheets-sync",
        "description": "Near-real-time Sheet → BQ staging sync for Grafana (every 5 min)",
    },
    {
        "id": "gaos-gmail-renew-watch",
        "schedule": "0 */23 * * *",
        "path": "/gmail-renew-watch",
        "description": "Renew Gmail watch() subscription (expires every 7 days max)",
    },
    {
        "id": "gaos-daily-digest",
        "schedule": "0 14 * * *",  # 6 AM PST (UTC-8) = 14:00 UTC
        "path": "/daily-digest",
        "description": "Daily system health + activity digest emailed to owner at 6 AM PST",
    },
]

SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
]


# ── Helpers ───────────────────────────────────────────────────────────────────


def load_settings() -> dict:
    """Load config/settings.yaml and return its parsed contents.

    Returns:
        Parsed YAML dict.

    Raises:
        SystemExit: If settings.yaml is not found.
    """
    path = Path(__file__).parent.parent / "config" / "settings.yaml"
    if not path.exists():
        print("ERROR: config/settings.yaml not found. Run setup_workspace.py first.")
        sys.exit(1)
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def get_nexus_prime_url(run_client, project_id: str, region: str) -> str:
    """Resolve the live Cloud Run URL for the nexus-prime service.

    Args:
        run_client: Cloud Run Admin API client (v2).
        project_id: GCP project ID.
        region: Cloud Run region (e.g. ``us-central1``).

    Returns:
        The HTTPS service URL, e.g. ``https://nexus-prime-xxxx-uc.a.run.app``.

    Raises:
        SystemExit: If the service cannot be found.
    """
    name = f"projects/{project_id}/locations/{region}/services/nexus-prime"
    try:
        svc = run_client.projects().locations().services().get(name=name).execute()
    except HttpError as exc:
        print(f"ERROR: Could not fetch nexus-prime Cloud Run service: {exc}")
        sys.exit(1)
    url = svc.get("uri", "")
    if not url:
        print("ERROR: nexus-prime service has no URI. Has it been deployed?")
        sys.exit(1)
    return url


def build_job_body(job: dict, nexus_url: str, sa_email: str) -> dict:
    """Construct the Cloud Scheduler Job resource body.

    Args:
        job: Entry from the JOBS constant list.
        nexus_url: Base HTTPS URL of the nexus-prime Cloud Run service.
        sa_email: Service account email for OIDC token generation.

    Returns:
        Cloud Scheduler Job resource dict suitable for create/patch.
    """
    target_url = nexus_url.rstrip("/") + job["path"]
    return {
        "description": job["description"],
        "schedule": job["schedule"],
        "timeZone": TIMEZONE,
        "httpTarget": {
            "uri": target_url,
            "httpMethod": "POST",
            "headers": {"Content-Type": "application/json"},
            "body": "",  # endpoints require no body
            "oidcToken": {
                "serviceAccountEmail": sa_email,
                "audience": target_url,
            },
        },
        "retryConfig": {
            "retryCount": 1,
            "minBackoffDuration": "5s",
            "maxBackoffDuration": "3600s",
            "maxDoublings": 5,
        },
    }


def upsert_job(
    scheduler_client,
    parent: str,
    job_id: str,
    body: dict,
) -> bool:
    """Create or update a Cloud Scheduler job idempotently.

    If the job already exists it is patched with an updateMask covering
    schedule, timeZone, httpTarget, retryConfig, and description so the live
    state is always brought in sync with this script.

    Args:
        scheduler_client: Cloud Scheduler API client (v1).
        parent: Parent resource path (``projects/{p}/locations/{l}``).
        job_id: Short job identifier, e.g. ``gaos-archive``.
        body: Job resource dict from :func:`build_job_body`.

    Returns:
        True on success (job created or patched). False on any HttpError —
        the error is printed but not re-raised so other jobs can still run.
    """
    full_name = f"{parent}/jobs/{job_id}"
    body_with_name = {**body, "name": full_name}

    # Attempt to GET the job first to decide create vs patch.
    try:
        scheduler_client.projects().locations().jobs().get(name=full_name).execute()
        job_exists = True
    except HttpError as exc:
        if exc.resp.status == 404:
            job_exists = False
        else:
            print(f"  ERROR checking {job_id}: {exc}")
            return False

    if job_exists:
        try:
            scheduler_client.projects().locations().jobs().patch(
                name=full_name,
                updateMask="schedule,timeZone,httpTarget,retryConfig,description",
                body=body_with_name,
            ).execute()
            print(f"  PATCHED  {job_id}  ({body['schedule']} {TIMEZONE})")
        except HttpError as exc:
            print(f"  ERROR patching {job_id}: {exc}")
            return False
    else:
        try:
            scheduler_client.projects().locations().jobs().create(
                parent=parent,
                body=body_with_name,
            ).execute()
            print(f"  CREATED  {job_id}  ({body['schedule']} {TIMEZONE})")
        except HttpError as exc:
            print(f"  ERROR creating {job_id}: {exc}")
            return False

    return True


def ensure_invoker_iam(run_client, project_id: str, region: str, sa_email: str) -> None:
    """Grant roles/run.invoker on the nexus-prime Cloud Run service to sa_email.

    Uses a read-modify-write cycle on the service IAM policy. Exits silently if
    the binding already exists.

    Args:
        run_client: Cloud Run Admin API client (v2).
        project_id: GCP project ID.
        region: Cloud Run region.
        sa_email: Service account to grant the invoker role to.
    """
    resource = f"projects/{project_id}/locations/{region}/services/nexus-prime"
    member = f"serviceAccount:{sa_email}"
    role = "roles/run.invoker"

    try:
        policy = (
            run_client.projects().locations().services().getIamPolicy(resource=resource).execute()
        )
    except HttpError as exc:
        print(f"  WARNING: Could not read IAM policy for nexus-prime: {exc}")
        return

    bindings: list[dict] = policy.get("bindings", [])
    for binding in bindings:
        if binding.get("role") == role and member in binding.get("members", []):
            print(f"  IAM      {sa_email} already has {role} — no change")
            return

    # Add the binding.
    bindings.append({"role": role, "members": [member]})
    policy["bindings"] = bindings
    try:
        run_client.projects().locations().services().setIamPolicy(
            resource=resource,
            body={"policy": policy},
        ).execute()
        print(f"  IAM      granted {role} → {sa_email}")
    except HttpError as exc:
        print(f"  WARNING: Could not set IAM policy on nexus-prime: {exc}")
        print("  Manual fix: gcloud run services add-iam-policy-binding nexus-prime \\")
        print(f"    --member=serviceAccount:{sa_email} --role={role} --region={region}")


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision Cloud Scheduler jobs for GAOS.")
    parser.add_argument(
        "--project",
        default=None,
        help="GCP project ID (default: gcp.project_id from settings.yaml)",
    )
    args = parser.parse_args()

    settings = load_settings()
    project_id: str = args.project or settings.get("gcp", {}).get("project_id", DEFAULT_PROJECT)
    region: str = settings.get("gcp", {}).get("region", "us-central1")
    sa_email = f"nexus-prime-sa@{project_id}.iam.gserviceaccount.com"
    parent = f"projects/{project_id}/locations/{region}"

    print(f"Project : {project_id}")
    print(f"Region  : {region}")
    print(f"SA      : {sa_email}")
    print()

    print("Authenticating via ADC...")
    creds, _ = google.auth.default(scopes=SCOPES)

    run_client = build("run", "v2", credentials=creds, cache_discovery=False)
    scheduler_client = build("cloudscheduler", "v1", credentials=creds, cache_discovery=False)

    print("Resolving nexus-prime Cloud Run URL...")
    nexus_url = get_nexus_prime_url(run_client, project_id, region)
    print(f"  URL: {nexus_url}")
    print()

    print("Ensuring IAM binding (roles/run.invoker)...")
    ensure_invoker_iam(run_client, project_id, region, sa_email)
    print()

    print("Upserting Cloud Scheduler jobs...")
    failures: list[str] = []
    for job in JOBS:
        body = build_job_body(job, nexus_url, sa_email)
        ok = upsert_job(scheduler_client, parent, job["id"], body)
        if not ok:
            failures.append(job["id"])

    print()
    if failures:
        print(f"ERROR: {len(failures)} job(s) failed to upsert: {', '.join(failures)}")
        sys.exit(1)
    print("Done. Verify in the GCP console:")
    print(f"  https://console.cloud.google.com/cloudscheduler?project={project_id}")


if __name__ == "__main__":
    main()
