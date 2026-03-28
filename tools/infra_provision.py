"""
tools/infra_provision.py — Infrastructure state management for Morphic-G AOS.

Provides diff, apply, health check, and rollback operations for the three GCP
resource types managed outside OpenTofu: Cloud Scheduler jobs, BigQuery staging
tables, and Secret Manager secrets.

Workflow (split across two entry points):
  PLAN phase  → scripts/provision_infra.py (CLI, local or CI)
    1. Build manifest (diff desired vs actual GCP state)
    2. Write ApprovalProposal row to Agent_Approvals sheet
    3. Send infra proposal Chat card → owner taps Approve / Reject

  APPLY phase → handle_infra_provision() in nexus_prime/orchestrator.py
    4. Read manifest JSON back from Agent_Approvals.proposed_code
    5. Apply changes: secrets → BQ tables → scheduler jobs (safe order)
    6. Run targeted health checks on changed resources
    7. If health check fails → rollback applied changes automatically
    8. Send plain-language result card to owner

Spec: GAOS-Deploy-Spec.md §20 (Infra Provisioner)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

# ── Desired state registry ────────────────────────────────────────────────────
# Single authoritative list for every GCP resource managed by this provisioner.
# Import from here rather than duplicating in scripts.

DESIRED_SCHEDULER_JOBS: list[dict[str, str]] = [
    {
        "id": "gaos-archive",
        "schedule": "0 2 * * *",
        "path": "/archive",
        "description": "Nightly Sheet → BigQuery archive sweep",
    },
    {
        "id": "gaos-daily-sync",
        "schedule": "0 6 * * *",
        "path": "/daily-sync",
        "description": "Morning briefing Chat card",
    },
    {
        "id": "gaos-sheets-sync",
        "schedule": "*/5 * * * *",
        "path": "/sheets-sync",
        "description": "Near-real-time Sheet → BQ staging sync for Grafana (every 5 min)",
    },
]

DESIRED_BQ_TABLES: list[str] = [
    "staging_approvals",
    "staging_logs",
    "staging_errors",
    "staging_pending_knowledge",
]

BQ_DATASET = "aos_logs"

DESIRED_SECRETS: list[str] = [
    "GEMINI_API_KEY",
    "OLLAMA_HOST",
    "WEBHOOK_HMAC_SECRET",
    "WEBHOOK_URL",
    "GOOGLE_SEARCH_API_KEY",
    "GOOGLE_SEARCH_CX",
]

# BQ DDL — CREATE TABLE IF NOT EXISTS (idempotent).
_BQ_DDL: dict[str, str] = {
    "staging_approvals": (
        "CREATE TABLE IF NOT EXISTS `{project}.aos_logs.staging_approvals` ("
        "id STRING, agent_id STRING, issue STRING, trigger_reason STRING,"
        " stopping_constraint STRING, iterations_run STRING, total_cost_usd STRING,"
        " proposed_code STRING, status STRING, timestamp STRING, approved_by STRING,"
        " approver_tier STRING, code_sha256 STRING, priority STRING, synced_at TIMESTAMP)"
    ),
    "staging_logs": (
        "CREATE TABLE IF NOT EXISTS `{project}.aos_logs.staging_logs` ("
        "timestamp STRING, agent_id STRING, level STRING, message STRING,"
        " project_id STRING, synced_at TIMESTAMP)"
    ),
    "staging_errors": (
        "CREATE TABLE IF NOT EXISTS `{project}.aos_logs.staging_errors` ("
        "timestamp STRING, agent_id STRING, error_type STRING, message STRING,"
        " traceback STRING, project_id STRING, synced_at TIMESTAMP)"
    ),
    "staging_pending_knowledge": (
        "CREATE TABLE IF NOT EXISTS `{project}.aos_logs.staging_pending_knowledge` ("
        "timestamp STRING, agent_id STRING, observation STRING, source STRING,"
        " confidence STRING, status STRING, project_id STRING, synced_at TIMESTAMP)"
    ),
}

# ── Change model ──────────────────────────────────────────────────────────────


class ChangeKind(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    NO_CHANGE = "NO_CHANGE"


class ResourceType(StrEnum):
    SCHEDULER_JOB = "SCHEDULER_JOB"
    BQ_TABLE = "BQ_TABLE"
    SECRET = "SECRET"


@dataclass
class ChangeEntry:
    """A single resource's diff result with human-readable description."""

    resource_type: ResourceType
    resource_id: str
    kind: ChangeKind
    desired: dict[str, Any]
    actual: dict[str, Any] = field(default_factory=dict)
    irreversible: bool = False
    human_description: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON storage in Agent_Approvals.proposed_code."""
        return {
            "resource_type": str(self.resource_type),
            "resource_id": self.resource_id,
            "kind": str(self.kind),
            "desired": self.desired,
            "actual": self.actual,
            "irreversible": self.irreversible,
            "human_description": self.human_description,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ChangeEntry:
        """Deserialize from Agent_Approvals.proposed_code JSON."""
        return cls(
            resource_type=ResourceType(d["resource_type"]),
            resource_id=d["resource_id"],
            kind=ChangeKind(d["kind"]),
            desired=d.get("desired", {}),
            actual=d.get("actual", {}),
            irreversible=d.get("irreversible", False),
            human_description=d.get("human_description", ""),
        )


@dataclass
class InfraManifest:
    """Complete diff manifest produced by build_manifest()."""

    proposal_id: str
    project_id: str
    region: str
    nexus_url: str
    sa_email: str
    changes: list[ChangeEntry] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def actionable(self) -> list[ChangeEntry]:
        """Changes that actually need to be applied (excludes NO_CHANGE)."""
        return [c for c in self.changes if c.kind != ChangeKind.NO_CHANGE]

    @property
    def has_changes(self) -> bool:
        return bool(self.actionable)

    @property
    def has_irreversible(self) -> bool:
        return any(c.irreversible for c in self.actionable)

    def to_json(self) -> str:
        """Serialize for storage in Agent_Approvals.proposed_code."""
        return json.dumps(
            {
                "proposal_id": self.proposal_id,
                "project_id": self.project_id,
                "region": self.region,
                "nexus_url": self.nexus_url,
                "sa_email": self.sa_email,
                "created_at": self.created_at,
                "changes": [c.to_dict() for c in self.changes],
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> InfraManifest:
        """Deserialize from Agent_Approvals.proposed_code JSON."""
        d = json.loads(raw)
        return cls(
            proposal_id=d["proposal_id"],
            project_id=d["project_id"],
            region=d["region"],
            nexus_url=d["nexus_url"],
            sa_email=d["sa_email"],
            created_at=d.get("created_at", ""),
            changes=[ChangeEntry.from_dict(c) for c in d.get("changes", [])],
        )


@dataclass
class ApplyResult:
    """Outcome of apply_manifest()."""

    applied: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    # Entries that were successfully applied — used to scope rollback.
    applied_entries: list[ChangeEntry] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not self.failed


# ── Diff helpers ──────────────────────────────────────────────────────────────


def _diff_scheduler_jobs(
    scheduler_client: Any,
    parent: str,
    jobs: list[dict[str, str]],
) -> list[ChangeEntry]:
    """Compare desired Cloud Scheduler jobs against live GCP state.

    Args:
        scheduler_client: Cloud Scheduler Admin API client (v1).
        parent: Resource parent, e.g. ``projects/p/locations/region``.
        jobs: Desired job definitions from DESIRED_SCHEDULER_JOBS.

    Returns:
        ChangeEntry list (one per desired job).
    """
    existing: dict[str, dict[str, Any]] = {}
    try:
        resp = scheduler_client.projects().locations().jobs().list(parent=parent).execute()
        for job in resp.get("jobs", []):
            job_id = job.get("name", "").split("/")[-1]
            existing[job_id] = job
    except Exception:
        # If we can't list, treat all as CREATE.
        return [
            ChangeEntry(
                resource_type=ResourceType.SCHEDULER_JOB,
                resource_id=j["id"],
                kind=ChangeKind.CREATE,
                desired=j,
                human_description=f'Register scheduled job "{j["id"]}" — {j.get("description", "")}',
            )
            for j in jobs
        ]

    entries: list[ChangeEntry] = []
    for job in jobs:
        job_id = job["id"]
        if job_id not in existing:
            entries.append(
                ChangeEntry(
                    resource_type=ResourceType.SCHEDULER_JOB,
                    resource_id=job_id,
                    kind=ChangeKind.CREATE,
                    desired=job,
                    human_description=(
                        f'Register scheduled job "{job_id}" — {job.get("description", "")}'
                    ),
                )
            )
        else:
            live = existing[job_id]
            live_schedule = live.get("schedule", "")
            if live_schedule != job["schedule"]:
                entries.append(
                    ChangeEntry(
                        resource_type=ResourceType.SCHEDULER_JOB,
                        resource_id=job_id,
                        kind=ChangeKind.UPDATE,
                        desired=job,
                        actual={"schedule": live_schedule},
                        human_description=(
                            f'Update job "{job_id}" schedule: '
                            f'"{live_schedule}" → "{job["schedule"]}"'
                        ),
                    )
                )
            else:
                entries.append(
                    ChangeEntry(
                        resource_type=ResourceType.SCHEDULER_JOB,
                        resource_id=job_id,
                        kind=ChangeKind.NO_CHANGE,
                        desired=job,
                        actual=live,
                    )
                )
    return entries


def _diff_bq_tables(
    bq_client: Any,
    project_id: str,
    dataset_id: str,
    desired_tables: list[str],
) -> list[ChangeEntry]:
    """Compare desired BigQuery tables against the live dataset.

    Args:
        bq_client: google.cloud.bigquery.Client.
        project_id: GCP project ID.
        dataset_id: BigQuery dataset name (e.g. ``"aos_logs"``).
        desired_tables: List of table IDs to ensure exist.

    Returns:
        ChangeEntry list.
    """
    existing: set[str] = set()
    try:
        dataset_ref = bq_client.dataset(dataset_id)
        for tbl in bq_client.list_tables(dataset_ref):
            existing.add(tbl.table_id)
    except Exception:
        pass

    entries: list[ChangeEntry] = []
    for table_id in desired_tables:
        full_id = f"{dataset_id}.{table_id}"
        if table_id not in existing:
            entries.append(
                ChangeEntry(
                    resource_type=ResourceType.BQ_TABLE,
                    resource_id=full_id,
                    kind=ChangeKind.CREATE,
                    desired={"table_id": table_id, "dataset_id": dataset_id},
                    irreversible=True,
                    human_description=(
                        f'Create storage table "{full_id}" '
                        f"(stores {table_id.replace('staging_', '')} records for Grafana)"
                    ),
                )
            )
        else:
            entries.append(
                ChangeEntry(
                    resource_type=ResourceType.BQ_TABLE,
                    resource_id=full_id,
                    kind=ChangeKind.NO_CHANGE,
                    desired={"table_id": table_id},
                )
            )
    return entries


def _diff_secrets(
    sm_client: Any,
    project_id: str,
    desired_secrets: list[str],
) -> list[ChangeEntry]:
    """Compare desired Secret Manager secrets against existing ones.

    Args:
        sm_client: google.cloud.secretmanager.SecretManagerServiceClient.
        project_id: GCP project ID.
        desired_secrets: List of secret IDs that must exist.

    Returns:
        ChangeEntry list.
    """
    existing: set[str] = set()
    try:
        for secret in sm_client.list_secrets(request={"parent": f"projects/{project_id}"}):
            existing.add(secret.name.split("/")[-1])
    except Exception:
        pass

    entries: list[ChangeEntry] = []
    for secret_name in desired_secrets:
        if secret_name not in existing:
            entries.append(
                ChangeEntry(
                    resource_type=ResourceType.SECRET,
                    resource_id=secret_name,
                    kind=ChangeKind.CREATE,
                    desired={"name": secret_name},
                    human_description=(
                        f'Create secret slot "{secret_name}" '
                        "(you will add the value manually after approval)"
                    ),
                )
            )
        else:
            entries.append(
                ChangeEntry(
                    resource_type=ResourceType.SECRET,
                    resource_id=secret_name,
                    kind=ChangeKind.NO_CHANGE,
                    desired={"name": secret_name},
                )
            )
    return entries


# ── Public API ────────────────────────────────────────────────────────────────


def build_manifest(
    project_id: str,
    region: str,
    nexus_url: str,
    sa_email: str,
    scheduler_client: Any | None = None,
    bq_client: Any | None = None,
    sm_client: Any | None = None,
) -> InfraManifest:
    """Build a full diff manifest of required infrastructure changes.

    Compares DESIRED_SCHEDULER_JOBS, DESIRED_BQ_TABLES, and DESIRED_SECRETS
    against the live GCP state. Returns all entries (including NO_CHANGE) so
    the card can accurately say "N things already up to date".

    Args:
        project_id: GCP project ID.
        region: GCP region (e.g. ``"us-central1"``).
        nexus_url: Cloud Run URL for nexus-prime (used to build scheduler targets).
        sa_email: Service account email for OIDC auth on scheduler jobs.
        scheduler_client: Cloud Scheduler Admin API client. Created if None.
        bq_client: BigQuery client. Created if None.
        sm_client: Secret Manager client. Created if None.

    Returns:
        InfraManifest with all three resource types diffed.
    """
    if scheduler_client is None:
        import google.auth
        from googleapiclient.discovery import build as gapi_build

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        scheduler_client = gapi_build("cloudscheduler", "v1", credentials=creds)

    if bq_client is None:
        from google.cloud import bigquery

        bq_client = bigquery.Client(project=project_id)

    if sm_client is None:
        from google.cloud import secretmanager

        sm_client = secretmanager.SecretManagerServiceClient()

    parent = f"projects/{project_id}/locations/{region}"
    changes: list[ChangeEntry] = []
    changes.extend(_diff_scheduler_jobs(scheduler_client, parent, DESIRED_SCHEDULER_JOBS))
    changes.extend(_diff_bq_tables(bq_client, project_id, BQ_DATASET, DESIRED_BQ_TABLES))
    changes.extend(_diff_secrets(sm_client, project_id, DESIRED_SECRETS))

    return InfraManifest(
        proposal_id=str(uuid.uuid4()),
        project_id=project_id,
        region=region,
        nexus_url=nexus_url,
        sa_email=sa_email,
        changes=changes,
    )


def apply_manifest(
    manifest: InfraManifest,
    scheduler_client: Any | None = None,
    bq_client: Any | None = None,
    sm_client: Any | None = None,
) -> ApplyResult:
    """Apply all actionable changes from the manifest in safe order.

    Order:  secrets (additive, no data risk)
         →  BQ tables (CREATE TABLE IF NOT EXISTS, idempotent)
         →  scheduler jobs (upsert, idempotent)

    Each step is independent — a failure in one resource does not abort the
    others. All outcomes are recorded in ApplyResult.

    Args:
        manifest: The approved InfraManifest.
        scheduler_client: Cloud Scheduler Admin API client. Created if None.
        bq_client: BigQuery client. Created if None.
        sm_client: Secret Manager client. Created if None.

    Returns:
        ApplyResult with applied/failed lists and entries for scoped rollback.
    """
    if scheduler_client is None:
        import google.auth
        from googleapiclient.discovery import build as gapi_build

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        scheduler_client = gapi_build("cloudscheduler", "v1", credentials=creds)

    if bq_client is None:
        from google.cloud import bigquery

        bq_client = bigquery.Client(project=manifest.project_id)

    if sm_client is None:
        from google.cloud import secretmanager

        sm_client = secretmanager.SecretManagerServiceClient()

    result = ApplyResult()
    parent = f"projects/{manifest.project_id}/locations/{manifest.region}"

    # ── Step 1: Secrets ───────────────────────────────────────────────────────
    for entry in manifest.actionable:
        if entry.resource_type != ResourceType.SECRET:
            continue
        try:
            sm_client.create_secret(
                request={
                    "parent": f"projects/{manifest.project_id}",
                    "secret_id": entry.resource_id,
                    "secret": {"replication": {"automatic": {}}},
                }
            )
            result.applied.append(f"SECRET  created  {entry.resource_id}")
            result.applied_entries.append(entry)
        except Exception as exc:
            result.failed.append(f"SECRET  failed   {entry.resource_id}: {exc}")

    # ── Step 2: BigQuery tables ───────────────────────────────────────────────
    for entry in manifest.actionable:
        if entry.resource_type != ResourceType.BQ_TABLE:
            continue
        table_id = entry.desired.get("table_id", "")
        ddl_template = _BQ_DDL.get(table_id, "")
        if not ddl_template:
            result.failed.append(f"BQ_TABLE no DDL template for {table_id}")
            continue
        try:
            sql = ddl_template.format(project=manifest.project_id)
            bq_client.query(sql).result()
            result.applied.append(f"BQ_TABLE created  {entry.resource_id}")
            result.applied_entries.append(entry)
        except Exception as exc:
            result.failed.append(f"BQ_TABLE failed   {entry.resource_id}: {exc}")

    # ── Step 3: Cloud Scheduler jobs ─────────────────────────────────────────
    for entry in manifest.actionable:
        if entry.resource_type != ResourceType.SCHEDULER_JOB:
            continue
        job_id = entry.resource_id
        full_name = f"{parent}/jobs/{job_id}"
        target_url = manifest.nexus_url.rstrip("/") + entry.desired.get("path", "")
        body = {
            "name": full_name,
            "description": entry.desired.get("description", ""),
            "schedule": entry.desired.get("schedule", ""),
            "timeZone": "America/Argentina/Buenos_Aires",
            "httpTarget": {
                "uri": target_url,
                "httpMethod": "POST",
                "headers": {"Content-Type": "application/json"},
                "body": "",
                "oidcToken": {
                    "serviceAccountEmail": manifest.sa_email,
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
        try:
            if entry.kind == ChangeKind.CREATE:
                scheduler_client.projects().locations().jobs().create(
                    parent=parent, body=body
                ).execute()
                result.applied.append(f"SCHEDULER created  {job_id}")
            else:
                scheduler_client.projects().locations().jobs().patch(
                    name=full_name,
                    updateMask="schedule,timeZone,httpTarget,retryConfig,description",
                    body=body,
                ).execute()
                result.applied.append(f"SCHEDULER updated  {job_id}")
            result.applied_entries.append(entry)
        except Exception as exc:
            result.failed.append(f"SCHEDULER failed   {job_id}: {exc}")

    return result


def rollback_manifest(
    manifest: InfraManifest,
    apply_result: ApplyResult,
    scheduler_client: Any | None = None,
    sm_client: Any | None = None,
) -> list[str]:
    """Attempt to undo changes recorded in apply_result.

    Hard rules:
    - BigQuery tables are NEVER dropped (data may already be written).
    - Secrets with existing versions are NOT deleted (only newly created empty ones).
    - Scheduler jobs that were created are deleted; updated jobs are re-patched
      to their previous schedule.

    Args:
        manifest: The manifest that was applied.
        apply_result: Result from apply_manifest() — only applied_entries are targeted.
        scheduler_client: Cloud Scheduler Admin API client. Created if None.
        sm_client: Secret Manager client. Created if None.

    Returns:
        List of human-readable outcome strings (one per attempted rollback action).
    """
    if scheduler_client is None:
        import google.auth
        from googleapiclient.discovery import build as gapi_build

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        scheduler_client = gapi_build("cloudscheduler", "v1", credentials=creds)

    if sm_client is None:
        from google.cloud import secretmanager

        sm_client = secretmanager.SecretManagerServiceClient()

    parent = f"projects/{manifest.project_id}/locations/{manifest.region}"
    notes: list[str] = []

    for entry in apply_result.applied_entries:
        if entry.resource_type == ResourceType.SCHEDULER_JOB:
            job_id = entry.resource_id
            full_name = f"{parent}/jobs/{job_id}"
            if entry.kind == ChangeKind.CREATE:
                try:
                    scheduler_client.projects().locations().jobs().delete(name=full_name).execute()
                    notes.append(f"ROLLED BACK: deleted scheduler job {job_id}")
                except Exception as exc:
                    notes.append(f"ROLLBACK INCOMPLETE: could not delete {job_id}: {exc}")
            elif entry.kind == ChangeKind.UPDATE:
                old_schedule = entry.actual.get("schedule", "")
                if old_schedule:
                    try:
                        scheduler_client.projects().locations().jobs().patch(
                            name=full_name,
                            updateMask="schedule",
                            body={"name": full_name, "schedule": old_schedule},
                        ).execute()
                        notes.append(f"ROLLED BACK: restored {job_id} schedule → {old_schedule}")
                    except Exception as exc:
                        notes.append(f"ROLLBACK INCOMPLETE: could not restore {job_id}: {exc}")

        elif entry.resource_type == ResourceType.BQ_TABLE:
            # Hard rule: never drop tables.
            notes.append(
                f"ROLLBACK SKIPPED: table {entry.resource_id} cannot be auto-dropped. "
                "If empty, drop manually: "
                f"bq rm -f --table {entry.resource_id}"
            )

        elif entry.resource_type == ResourceType.SECRET:
            secret_name = entry.resource_id
            full_path = f"projects/{manifest.project_id}/secrets/{secret_name}"
            try:
                versions = list(sm_client.list_secret_versions(request={"parent": full_path}))
                if versions:
                    notes.append(
                        f"ROLLBACK SKIPPED: secret {secret_name} already has "
                        f"{len(versions)} version(s) — not deleted."
                    )
                else:
                    sm_client.delete_secret(request={"name": full_path})
                    notes.append(f"ROLLED BACK: deleted empty secret {secret_name}")
            except Exception as exc:
                notes.append(f"ROLLBACK INCOMPLETE: could not remove secret {secret_name}: {exc}")

    return notes


def run_health_checks(
    manifest: InfraManifest,
    scheduler_client: Any | None = None,
    bq_client: Any | None = None,
) -> tuple[bool, list[str]]:
    """Run targeted health checks on resources that were changed by the manifest.

    Only checks resource types that had actionable changes — skips NO_CHANGE
    entries entirely so the check is fast and focused.

    Args:
        manifest: The applied manifest.
        scheduler_client: Cloud Scheduler Admin API client. Created if None.
        bq_client: BigQuery client. Created if None.

    Returns:
        (all_passed: bool, notes: list[str]) — one note per checked resource.
    """
    if scheduler_client is None:
        import google.auth
        from googleapiclient.discovery import build as gapi_build

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        scheduler_client = gapi_build("cloudscheduler", "v1", credentials=creds)

    if bq_client is None:
        from google.cloud import bigquery

        bq_client = bigquery.Client(project=manifest.project_id)

    parent = f"projects/{manifest.project_id}/locations/{manifest.region}"
    notes: list[str] = []
    all_passed = True

    changed_types = {e.resource_type for e in manifest.actionable}

    if ResourceType.SCHEDULER_JOB in changed_types:
        for entry in manifest.actionable:
            if entry.resource_type != ResourceType.SCHEDULER_JOB:
                continue
            job_id = entry.resource_id
            full_name = f"{parent}/jobs/{job_id}"
            try:
                job = scheduler_client.projects().locations().jobs().get(name=full_name).execute()
                state = job.get("state", "UNKNOWN")
                if state in ("ENABLED", "PAUSED"):
                    notes.append(f"✅ Scheduler job {job_id} exists (state={state})")
                else:
                    notes.append(f"❌ Scheduler job {job_id} in unexpected state: {state}")
                    all_passed = False
            except Exception as exc:
                notes.append(f"❌ Scheduler job {job_id} not found after apply: {exc}")
                all_passed = False

    if ResourceType.BQ_TABLE in changed_types:
        for entry in manifest.actionable:
            if entry.resource_type != ResourceType.BQ_TABLE:
                continue
            table_id = entry.desired.get("table_id", "")
            try:
                bq_client.get_table(f"{manifest.project_id}.{BQ_DATASET}.{table_id}")
                notes.append(f"✅ BigQuery table {BQ_DATASET}.{table_id} exists")
            except Exception as exc:
                notes.append(f"❌ BigQuery table {BQ_DATASET}.{table_id} not found: {exc}")
                all_passed = False

    return all_passed, notes
