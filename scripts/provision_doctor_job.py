"""
scripts/provision_doctor_job.py — Idempotently provision the GAOS-Doctor
Cloud Run Job and its daily Cloud Scheduler trigger.

Components created (or patched if they already exist):
  Cloud Run Job:       gaos-doctor
  Cloud Scheduler job: gaos-doctor-daily  → 4x/day (every 6 hours) PT

The Cloud Scheduler job calls the Cloud Run Jobs v2 API to trigger a run:
  POST https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs/gaos-doctor:run

The job runs as nexus-prime-sa, which already has the necessary GCP permissions.
The scheduler authenticates via oauthToken (scope: cloud-platform) — NOT an OIDC
token, because this targets the Cloud Run Jobs API, not a Cloud Run HTTP service.

Prerequisites:
  1. Build and push the doctor image:
     gcloud builds submit --tag us-central1-docker.pkg.dev/morphic-gaos-prod/cloud-run-source-deploy/gaos-doctor:latest --dockerfile=Dockerfile.doctor .
  2. ADC configured:  gcloud auth application-default login
  3. .venv activated
  4. Run from repo root:  python scripts/provision_doctor_job.py [--project <project_id>]

Design notes:
  - Idempotent: existing resources are patched, not recreated.
  - The Cloud Run Job runs with max-retries=0 — Doctor failures should surface
    immediately, not be silently retried.
  - DOCTOR_SEND_REPORT=1 is baked into Dockerfile.doctor, not set here, so the
    env var is always set in the job container.
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
TIMEZONE = "America/Los_Angeles"
DOCTOR_IMAGE_TEMPLATE = (
    "us-central1-docker.pkg.dev/{project}/cloud-run-source-deploy/gaos-doctor:latest"
)
JOB_NAME = "gaos-doctor"
SCHEDULER_JOB_NAME = "gaos-doctor-daily"
SCHEDULE = "0 */6 * * *"  # 4x/day: midnight, 6 AM, noon, 6 PM PT (America/Los_Angeles handles DST)
JOB_TIMEOUT = "600s"  # 10 minutes max per run
SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


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


def build_job_body(project_id: str, region: str, sa_email: str) -> dict:
    """Construct the Cloud Run Job (v2) resource body.

    Args:
        project_id: GCP project ID.
        region:     Cloud Run region (e.g. ``us-central1``).
        sa_email:   Service account email the job runs as.

    Returns:
        Cloud Run Job resource dict suitable for create/patch.
    """
    image = DOCTOR_IMAGE_TEMPLATE.format(project=project_id)
    return {
        "template": {
            "template": {
                "containers": [
                    {
                        "image": image,
                        "env": [{"name": "DOCTOR_SEND_REPORT", "value": "1"}],
                        "resources": {
                            "limits": {"cpu": "1", "memory": "512Mi"},
                        },
                    }
                ],
                "serviceAccount": sa_email,
                "timeout": JOB_TIMEOUT,
                "maxRetries": 0,  # Doctor failures surface immediately — no silent retries
            }
        }
    }


def upsert_cloud_run_job(
    run_client,
    project_id: str,
    region: str,
    job_body: dict,
) -> bool:
    """Create or update the gaos-doctor Cloud Run Job idempotently.

    Args:
        run_client: Cloud Run Admin API client (v2).
        project_id: GCP project ID.
        region:     Cloud Run region.
        job_body:   Job resource dict from :func:`build_job_body`.

    Returns:
        True on success, False on any HttpError.
    """
    parent = f"projects/{project_id}/locations/{region}"
    full_name = f"{parent}/jobs/{JOB_NAME}"

    try:
        run_client.projects().locations().jobs().get(name=full_name).execute()
        job_exists = True
    except HttpError as exc:
        if exc.resp.status == 404:
            job_exists = False
        else:
            print(f"  ERROR checking {JOB_NAME}: {exc}")
            return False

    if job_exists:
        try:
            run_client.projects().locations().jobs().patch(
                name=full_name,
                body={**job_body, "name": full_name},
            ).execute()
            print(f"  PATCHED  Cloud Run Job: {JOB_NAME}")
        except HttpError as exc:
            print(f"  ERROR patching {JOB_NAME}: {exc}")
            return False
    else:
        try:
            run_client.projects().locations().jobs().create(
                parent=parent,
                jobId=JOB_NAME,
                body=job_body,
            ).execute()
            print(f"  CREATED  Cloud Run Job: {JOB_NAME}")
        except HttpError as exc:
            print(f"  ERROR creating {JOB_NAME}: {exc}")
            return False

    return True


def ensure_job_invoker_iam(
    run_client,
    project_id: str,
    region: str,
    sa_email: str,
) -> None:
    """Grant roles/run.invoker on the gaos-doctor job to sa_email.

    Cloud Scheduler needs this permission to trigger the Cloud Run Job via the
    Jobs API `:run` endpoint.

    Args:
        run_client: Cloud Run Admin API client (v2).
        project_id: GCP project ID.
        region:     Cloud Run region.
        sa_email:   Service account to grant the invoker role to.
    """
    resource = f"projects/{project_id}/locations/{region}/jobs/{JOB_NAME}"
    member = f"serviceAccount:{sa_email}"
    role = "roles/run.invoker"

    try:
        policy = run_client.projects().locations().jobs().getIamPolicy(resource=resource).execute()
    except HttpError as exc:
        print(f"  WARNING: Could not read IAM policy for {JOB_NAME}: {exc}")
        print(f"  Manual fix: gcloud run jobs add-iam-policy-binding {JOB_NAME} \\")
        print(f"    --member={member} --role={role} --region={region}")
        return

    bindings: list[dict] = policy.get("bindings", [])
    for binding in bindings:
        if binding.get("role") == role and member in binding.get("members", []):
            print(f"  IAM      {sa_email} already has {role} on {JOB_NAME} — no change")
            return

    bindings.append({"role": role, "members": [member]})
    policy["bindings"] = bindings
    try:
        run_client.projects().locations().jobs().setIamPolicy(
            resource=resource,
            body={"policy": policy},
        ).execute()
        print(f"  IAM      granted {role} → {sa_email} on {JOB_NAME}")
    except HttpError as exc:
        print(f"  WARNING: Could not set IAM policy on {JOB_NAME}: {exc}")
        print(f"  Manual fix: gcloud run jobs add-iam-policy-binding {JOB_NAME} \\")
        print(f"    --member={member} --role={role} --region={region}")


def upsert_scheduler_job(
    scheduler_client,
    project_id: str,
    region: str,
    sa_email: str,
) -> bool:
    """Create or update the gaos-doctor-daily Cloud Scheduler job idempotently.

    The Scheduler job triggers the Cloud Run Job via an HTTP POST to the Cloud
    Run Jobs v2 API (:run endpoint), authenticated with an oauthToken.

    Args:
        scheduler_client: Cloud Scheduler API client (v1).
        project_id:       GCP project ID.
        region:           Cloud Scheduler + Cloud Run region.
        sa_email:         Service account for oauthToken generation.

    Returns:
        True on success, False on any HttpError.
    """
    parent = f"projects/{project_id}/locations/{region}"
    full_name = f"{parent}/jobs/{SCHEDULER_JOB_NAME}"
    run_jobs_api_url = (
        f"https://run.googleapis.com/v2/projects/{project_id}"
        f"/locations/{region}/jobs/{JOB_NAME}:run"
    )

    body = {
        "name": full_name,
        "description": "GAOS-Doctor health check — every 6 hours (Rule 29)",
        "schedule": SCHEDULE,
        "timeZone": TIMEZONE,
        "httpTarget": {
            "uri": run_jobs_api_url,
            "httpMethod": "POST",
            "headers": {"Content-Type": "application/json"},
            "body": "",  # :run endpoint accepts an empty POST body
            "oauthToken": {
                # oauthToken (not oidcToken) — this targets a Google API, not a
                # Cloud Run HTTP service. The scope grants permission to call the
                # Cloud Run Admin API.
                "serviceAccountEmail": sa_email,
                "scope": "https://www.googleapis.com/auth/cloud-platform",
            },
        },
        "retryConfig": {
            "retryCount": 0,  # Doctor runs are one-shot; no retry on scheduler side either
        },
    }

    try:
        scheduler_client.projects().locations().jobs().get(name=full_name).execute()
        job_exists = True
    except HttpError as exc:
        if exc.resp.status == 404:
            job_exists = False
        else:
            print(f"  ERROR checking {SCHEDULER_JOB_NAME}: {exc}")
            return False

    update_mask = "schedule,timeZone,httpTarget,retryConfig,description"
    if job_exists:
        try:
            scheduler_client.projects().locations().jobs().patch(
                name=full_name,
                updateMask=update_mask,
                body=body,
            ).execute()
            print(f"  PATCHED  Cloud Scheduler job: {SCHEDULER_JOB_NAME}  ({SCHEDULE} {TIMEZONE})")
        except HttpError as exc:
            print(f"  ERROR patching {SCHEDULER_JOB_NAME}: {exc}")
            return False
    else:
        try:
            scheduler_client.projects().locations().jobs().create(
                parent=parent,
                body=body,
            ).execute()
            print(f"  CREATED  Cloud Scheduler job: {SCHEDULER_JOB_NAME}  ({SCHEDULE} {TIMEZONE})")
        except HttpError as exc:
            print(f"  ERROR creating {SCHEDULER_JOB_NAME}: {exc}")
            return False

    return True


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Provision the GAOS-Doctor Cloud Run Job and daily Scheduler trigger."
    )
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
    image = DOCTOR_IMAGE_TEMPLATE.format(project=project_id)

    print(f"Project : {project_id}")
    print(f"Region  : {region}")
    print(f"SA      : {sa_email}")
    print(f"Image   : {image}")
    print()

    print("Authenticating via ADC...")
    creds, _ = google.auth.default(scopes=SCOPES)

    run_client = build("run", "v2", credentials=creds, cache_discovery=False)
    scheduler_client = build("cloudscheduler", "v1", credentials=creds, cache_discovery=False)

    print()
    print("Step 1 — Cloud Run Job: gaos-doctor")
    job_body = build_job_body(project_id, region, sa_email)
    job_ok = upsert_cloud_run_job(run_client, project_id, region, job_body)
    if not job_ok:
        print("ERROR: Cloud Run Job provisioning failed. Aborting.")
        sys.exit(1)

    print()
    print("Step 2 — IAM: grant roles/run.invoker to nexus-prime-sa on gaos-doctor job")
    ensure_job_invoker_iam(run_client, project_id, region, sa_email)

    print()
    print("Step 3 — Cloud Scheduler job: gaos-doctor-daily")
    sched_ok = upsert_scheduler_job(scheduler_client, project_id, region, sa_email)
    if not sched_ok:
        print("ERROR: Cloud Scheduler job provisioning failed.")
        sys.exit(1)

    print()
    print("Done. Next steps:")
    print(
        f"  1. Verify job in GCP console: "
        f"https://console.cloud.google.com/run/jobs?project={project_id}"
    )
    print(
        f"  2. Verify scheduler job: "
        f"https://console.cloud.google.com/cloudscheduler?project={project_id}"
    )
    print(f"  3. Test manually: gcloud run jobs execute {JOB_NAME} --region={region}")


if __name__ == "__main__":
    main()
