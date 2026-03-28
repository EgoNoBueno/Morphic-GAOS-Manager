"""
tests/test_infra_provision.py — Unit tests for tools/infra_provision.py

Coverage:
  IP1 — Happy path: diff has 1 CREATE scheduler → manifest produced correctly
  IP2 — No-op diff: all resources already exist → has_changes is False
  IP3 — apply_manifest: secrets step succeeds, BQ step fails → ApplyResult logged correctly
  IP4 — apply_manifest: full apply succeeds
  IP5 — rollback_manifest: BQ hard rule (never drop tables)
  IP6 — rollback_manifest: newly created empty secret is deleted
  IP7 — rollback_manifest: scheduler CREATE → job deleted
  IP8 — rollback_manifest: scheduler UPDATE → old schedule restored
  IP9 — run_health_checks: scheduler state ENABLED → passes
  IP10 — run_health_checks: scheduler not found after apply → fails
  IP11 — run_health_checks: BQ table exists → passes
  IP12 — run_health_checks: BQ table missing → fails
  IP13 — InfraManifest round-trips through to_json / from_json
  IP14 — ChangeEntry round-trips through to_dict / from_dict
"""

from __future__ import annotations

from unittest.mock import MagicMock

from tools.infra_provision import (
    DESIRED_BQ_TABLES,
    DESIRED_SCHEDULER_JOBS,
    DESIRED_SECRETS,
    ApplyResult,
    ChangeEntry,
    ChangeKind,
    InfraManifest,
    ResourceType,
    _diff_bq_tables,
    _diff_scheduler_jobs,
    _diff_secrets,
    apply_manifest,
    build_manifest,
    rollback_manifest,
    run_health_checks,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

PROJECT = "morphic-gaos-test"
REGION = "us-central1"
NEXUS_URL = "https://nexus-prime-test.run.app"
SA_EMAIL = "nexus-prime@morphic-gaos-test.iam.gserviceaccount.com"
PARENT = f"projects/{PROJECT}/locations/{REGION}"


def _make_manifest(**overrides) -> InfraManifest:
    """Convenience constructor with sensible test defaults."""
    defaults = {
        "proposal_id": "test-proposal-id-1234",
        "project_id": PROJECT,
        "region": REGION,
        "nexus_url": NEXUS_URL,
        "sa_email": SA_EMAIL,
        "changes": [],
    }
    defaults.update(overrides)
    return InfraManifest(**defaults)


def _make_scheduler_entry(
    job_id: str = "gaos-archive",
    kind: ChangeKind = ChangeKind.CREATE,
    old_schedule: str = "",
    desired_schedule: str = "0 2 * * *",
) -> ChangeEntry:
    return ChangeEntry(
        resource_type=ResourceType.SCHEDULER_JOB,
        resource_id=job_id,
        kind=kind,
        desired={"id": job_id, "schedule": desired_schedule, "path": "/archive"},
        actual={"schedule": old_schedule} if old_schedule else {},
        human_description=f"Register scheduled job {job_id}",
    )


def _make_bq_entry(
    table_id: str = "staging_logs", kind: ChangeKind = ChangeKind.CREATE
) -> ChangeEntry:
    return ChangeEntry(
        resource_type=ResourceType.BQ_TABLE,
        resource_id=f"aos_logs.{table_id}",
        kind=kind,
        desired={"table_id": table_id, "dataset_id": "aos_logs"},
        irreversible=True,
        human_description=f"Create storage table aos_logs.{table_id}",
    )


def _make_secret_entry(
    secret_name: str = "GEMINI_API_KEY", kind: ChangeKind = ChangeKind.CREATE
) -> ChangeEntry:
    return ChangeEntry(
        resource_type=ResourceType.SECRET,
        resource_id=secret_name,
        kind=kind,
        desired={"name": secret_name},
        human_description=f"Create secret slot {secret_name}",
    )


# ── IP1: build_manifest — happy path with one missing scheduler ───────────────


class TestBuildManifest:
    def test_ip1_diff_produces_create_for_missing_scheduler(self):
        """IP1: Scheduler missing from GCP → ChangeKind.CREATE in result."""
        mock_sched = MagicMock()
        # GCP returns no existing jobs
        mock_sched.projects.return_value.locations.return_value.jobs.return_value.list.return_value.execute.return_value = {
            "jobs": []
        }

        mock_bq = MagicMock()
        dataset_ref = MagicMock()
        mock_bq.dataset.return_value = dataset_ref
        mock_bq.list_tables.return_value = []  # all tables missing too

        mock_sm = MagicMock()
        mock_sm.list_secrets.return_value = []  # all secrets missing

        manifest = build_manifest(
            project_id=PROJECT,
            region=REGION,
            nexus_url=NEXUS_URL,
            sa_email=SA_EMAIL,
            scheduler_client=mock_sched,
            bq_client=mock_bq,
            sm_client=mock_sm,
        )

        scheduler_changes = [
            c for c in manifest.changes if c.resource_type == ResourceType.SCHEDULER_JOB
        ]
        assert all(c.kind == ChangeKind.CREATE for c in scheduler_changes)
        assert len(scheduler_changes) == len(DESIRED_SCHEDULER_JOBS)
        assert manifest.has_changes is True

    def test_ip2_no_op_when_all_resources_exist(self):
        """IP2: All desired resources present → has_changes is False."""
        mock_sched = MagicMock()
        existing_jobs = [
            {"name": f"{PARENT}/jobs/{j['id']}", "schedule": j["schedule"]}
            for j in DESIRED_SCHEDULER_JOBS
        ]
        mock_sched.projects.return_value.locations.return_value.jobs.return_value.list.return_value.execute.return_value = {
            "jobs": existing_jobs
        }

        mock_bq = MagicMock()
        # Return one mock table per desired table
        existing_tbls = []
        for t in DESIRED_BQ_TABLES:
            tbl = MagicMock()
            tbl.table_id = t
            existing_tbls.append(tbl)
        mock_bq.dataset.return_value = MagicMock()
        mock_bq.list_tables.return_value = existing_tbls

        mock_sm = MagicMock()
        existing_secrets = []
        for s in DESIRED_SECRETS:
            sec = MagicMock()
            sec.name = f"projects/{PROJECT}/secrets/{s}"
            existing_secrets.append(sec)
        mock_sm.list_secrets.return_value = existing_secrets

        manifest = build_manifest(
            project_id=PROJECT,
            region=REGION,
            nexus_url=NEXUS_URL,
            sa_email=SA_EMAIL,
            scheduler_client=mock_sched,
            bq_client=mock_bq,
            sm_client=mock_sm,
        )

        assert manifest.has_changes is False
        assert all(c.kind == ChangeKind.NO_CHANGE for c in manifest.changes)


# ── IP3 / IP4: apply_manifest ─────────────────────────────────────────────────


class TestApplyManifest:
    def test_ip3_partial_failure_recorded(self):
        """IP3: Secret succeeds, BQ query raises → failed list contains BQ entry."""
        entry_secret = _make_secret_entry("WEBHOOK_URL")
        entry_bq = _make_bq_entry("staging_logs")
        manifest = _make_manifest(changes=[entry_secret, entry_bq])

        mock_sm = MagicMock()
        mock_sm.create_secret.return_value = MagicMock()

        mock_bq = MagicMock()
        mock_bq.query.side_effect = RuntimeError("BQ quota exceeded")

        mock_sched = MagicMock()

        result = apply_manifest(
            manifest,
            scheduler_client=mock_sched,
            bq_client=mock_bq,
            sm_client=mock_sm,
        )

        applied_ids = [a for a in result.applied if "WEBHOOK_URL" in a]
        failed_ids = [f for f in result.failed if "staging_logs" in f or "BQ_TABLE" in f]

        assert applied_ids, "Secret apply should succeed"
        assert failed_ids, "BQ apply should fail"
        assert result.success is False
        # Rollback scope: only applied_entries (secret), not the failed BQ table
        applied_entry_types = {e.resource_type for e in result.applied_entries}
        assert ResourceType.SECRET in applied_entry_types
        assert ResourceType.BQ_TABLE not in applied_entry_types

    def test_ip4_full_apply_success(self):
        """IP4: All three resource types applied successfully → success True."""
        entries = [
            _make_secret_entry("GEMINI_API_KEY"),
            _make_bq_entry("staging_approvals"),
            _make_scheduler_entry("gaos-archive", kind=ChangeKind.CREATE),
        ]
        manifest = _make_manifest(changes=entries)

        mock_sm = MagicMock()
        mock_bq = MagicMock()
        query_result = MagicMock()
        mock_bq.query.return_value.result.return_value = query_result

        mock_sched = MagicMock()
        mock_sched.projects.return_value.locations.return_value.jobs.return_value.create.return_value.execute.return_value = {}

        result = apply_manifest(
            manifest,
            scheduler_client=mock_sched,
            bq_client=mock_bq,
            sm_client=mock_sm,
        )

        assert result.success is True
        assert len(result.failed) == 0
        assert len(result.applied_entries) == 3


# ── IP5–IP8: rollback_manifest ────────────────────────────────────────────────


class TestRollbackManifest:
    def test_ip5_bq_table_never_dropped(self):
        """IP5: BQ table in applied_entries → rollback note says ROLLBACK SKIPPED."""
        entry = _make_bq_entry("staging_errors")
        manifest = _make_manifest(changes=[entry])
        apply_result = ApplyResult(
            applied=["BQ_TABLE created  aos_logs.staging_errors"],
            applied_entries=[entry],
        )

        mock_sched = MagicMock()
        mock_sm = MagicMock()

        notes = rollback_manifest(
            manifest, apply_result, scheduler_client=mock_sched, sm_client=mock_sm
        )

        assert any("ROLLBACK SKIPPED" in n and "staging_errors" in n for n in notes)
        # Confirm no delete call was made on BQ client (not even injected)
        mock_sm.delete_secret.assert_not_called()

    def test_ip6_empty_secret_is_deleted(self):
        """IP6: Newly created secret with 0 versions → deleted during rollback."""
        entry = _make_secret_entry("GEMINI_API_KEY")
        manifest = _make_manifest(changes=[entry])
        apply_result = ApplyResult(
            applied=["SECRET  created  GEMINI_API_KEY"],
            applied_entries=[entry],
        )

        mock_sched = MagicMock()
        mock_sm = MagicMock()
        mock_sm.list_secret_versions.return_value = []  # no versions → safe to delete

        notes = rollback_manifest(
            manifest, apply_result, scheduler_client=mock_sched, sm_client=mock_sm
        )

        full_path = f"projects/{PROJECT}/secrets/GEMINI_API_KEY"
        mock_sm.delete_secret.assert_called_once_with(request={"name": full_path})
        assert any("ROLLED BACK" in n and "GEMINI_API_KEY" in n for n in notes)

    def test_ip6b_secret_with_versions_is_skipped(self):
        """IP6b: Secret with existing versions → NOT deleted, note says ROLLBACK SKIPPED."""
        entry = _make_secret_entry("GEMINI_API_KEY")
        manifest = _make_manifest(changes=[entry])
        apply_result = ApplyResult(
            applied=["SECRET  created  GEMINI_API_KEY"],
            applied_entries=[entry],
        )

        mock_sched = MagicMock()
        mock_sm = MagicMock()
        mock_sm.list_secret_versions.return_value = [MagicMock()]  # 1 version exists

        notes = rollback_manifest(
            manifest, apply_result, scheduler_client=mock_sched, sm_client=mock_sm
        )

        mock_sm.delete_secret.assert_not_called()
        assert any("ROLLBACK SKIPPED" in n and "GEMINI_API_KEY" in n for n in notes)

    def test_ip7_created_scheduler_job_deleted(self):
        """IP7: Scheduler CREATE rolled back → job deleted from GCP."""
        entry = _make_scheduler_entry("gaos-archive", kind=ChangeKind.CREATE)
        manifest = _make_manifest(changes=[entry])
        apply_result = ApplyResult(
            applied=["SCHEDULER created  gaos-archive"],
            applied_entries=[entry],
        )

        mock_sched = MagicMock()
        mock_sched.projects.return_value.locations.return_value.jobs.return_value.delete.return_value.execute.return_value = {}
        mock_sm = MagicMock()

        notes = rollback_manifest(
            manifest, apply_result, scheduler_client=mock_sched, sm_client=mock_sm
        )

        full_name = f"{PARENT}/jobs/gaos-archive"
        mock_sched.projects.return_value.locations.return_value.jobs.return_value.delete.assert_called_once_with(
            name=full_name
        )
        assert any("ROLLED BACK" in n and "gaos-archive" in n for n in notes)

    def test_ip8_updated_scheduler_job_restored(self):
        """IP8: Scheduler UPDATE rolled back → previous schedule re-patched."""
        old_schedule = "0 3 * * *"
        entry = _make_scheduler_entry(
            "gaos-archive",
            kind=ChangeKind.UPDATE,
            old_schedule=old_schedule,
            desired_schedule="0 2 * * *",
        )
        manifest = _make_manifest(changes=[entry])
        apply_result = ApplyResult(
            applied=["SCHEDULER updated  gaos-archive"],
            applied_entries=[entry],
        )

        mock_sched = MagicMock()
        mock_sched.projects.return_value.locations.return_value.jobs.return_value.patch.return_value.execute.return_value = {}
        mock_sm = MagicMock()

        notes = rollback_manifest(
            manifest, apply_result, scheduler_client=mock_sched, sm_client=mock_sm
        )

        patch_call = mock_sched.projects.return_value.locations.return_value.jobs.return_value.patch
        patch_call.assert_called_once()
        call_kwargs = patch_call.call_args.kwargs
        assert call_kwargs["body"]["schedule"] == old_schedule
        assert any(old_schedule in n for n in notes)


# ── IP9–IP12: run_health_checks ───────────────────────────────────────────────


class TestRunHealthChecks:
    def test_ip9_scheduler_enabled_passes(self):
        """IP9: Scheduler job state=ENABLED → check passes."""
        entry = _make_scheduler_entry("gaos-archive", kind=ChangeKind.CREATE)
        manifest = _make_manifest(changes=[entry])

        mock_sched = MagicMock()
        mock_sched.projects.return_value.locations.return_value.jobs.return_value.get.return_value.execute.return_value = {
            "state": "ENABLED"
        }
        mock_bq = MagicMock()

        ok, notes = run_health_checks(manifest, scheduler_client=mock_sched, bq_client=mock_bq)

        assert ok is True
        assert any("✅" in n and "gaos-archive" in n for n in notes)

    def test_ip10_scheduler_missing_after_apply_fails(self):
        """IP10: Scheduler get() raises after apply → check fails."""
        entry = _make_scheduler_entry("gaos-archive", kind=ChangeKind.CREATE)
        manifest = _make_manifest(changes=[entry])

        mock_sched = MagicMock()
        mock_sched.projects.return_value.locations.return_value.jobs.return_value.get.return_value.execute.side_effect = RuntimeError(
            "404 not found"
        )
        mock_bq = MagicMock()

        ok, notes = run_health_checks(manifest, scheduler_client=mock_sched, bq_client=mock_bq)

        assert ok is False
        assert any("❌" in n and "gaos-archive" in n for n in notes)

    def test_ip11_bq_table_exists_passes(self):
        """IP11: BQ get_table succeeds → check passes."""
        entry = _make_bq_entry("staging_approvals", kind=ChangeKind.CREATE)
        manifest = _make_manifest(changes=[entry])

        mock_sched = MagicMock()
        mock_bq = MagicMock()
        mock_bq.get_table.return_value = MagicMock()

        ok, notes = run_health_checks(manifest, scheduler_client=mock_sched, bq_client=mock_bq)

        assert ok is True
        assert any("✅" in n and "staging_approvals" in n for n in notes)

    def test_ip12_bq_table_missing_fails(self):
        """IP12: BQ get_table raises → check fails."""
        entry = _make_bq_entry("staging_approvals", kind=ChangeKind.CREATE)
        manifest = _make_manifest(changes=[entry])

        mock_sched = MagicMock()
        mock_bq = MagicMock()
        mock_bq.get_table.side_effect = RuntimeError("Table not found")

        ok, notes = run_health_checks(manifest, scheduler_client=mock_sched, bq_client=mock_bq)

        assert ok is False
        assert any("❌" in n and "staging_approvals" in n for n in notes)

    def test_no_op_manifest_skips_all_checks(self):
        """NO_CHANGE entries → no scheduler/BQ API calls made."""
        entry = ChangeEntry(
            resource_type=ResourceType.SCHEDULER_JOB,
            resource_id="gaos-archive",
            kind=ChangeKind.NO_CHANGE,
            desired={"id": "gaos-archive", "schedule": "0 2 * * *"},
        )
        manifest = _make_manifest(changes=[entry])

        mock_sched = MagicMock()
        mock_bq = MagicMock()

        ok, notes = run_health_checks(manifest, scheduler_client=mock_sched, bq_client=mock_bq)

        assert ok is True
        assert notes == []
        mock_sched.projects.assert_not_called()
        mock_bq.get_table.assert_not_called()


# ── IP13 / IP14: serialization round-trips ────────────────────────────────────


class TestSerialization:
    def test_ip13_infra_manifest_round_trip(self):
        """IP13: InfraManifest → to_json() → from_json() preserves all fields."""
        entry = _make_scheduler_entry(
            "gaos-daily-sync", kind=ChangeKind.UPDATE, old_schedule="0 7 * * *"
        )
        manifest = _make_manifest(changes=[entry])

        raw = manifest.to_json()
        restored = InfraManifest.from_json(raw)

        assert restored.proposal_id == manifest.proposal_id
        assert restored.project_id == manifest.project_id
        assert restored.region == manifest.region
        assert restored.nexus_url == manifest.nexus_url
        assert restored.sa_email == manifest.sa_email
        assert len(restored.changes) == 1
        restored_entry = restored.changes[0]
        assert restored_entry.resource_type == ResourceType.SCHEDULER_JOB
        assert restored_entry.kind == ChangeKind.UPDATE
        assert restored_entry.actual["schedule"] == "0 7 * * *"

    def test_ip14_change_entry_round_trip(self):
        """IP14: ChangeEntry → to_dict() → from_dict() preserves all fields."""
        entry = ChangeEntry(
            resource_type=ResourceType.BQ_TABLE,
            resource_id="aos_logs.staging_errors",
            kind=ChangeKind.CREATE,
            desired={"table_id": "staging_errors", "dataset_id": "aos_logs"},
            irreversible=True,
            human_description="Create storage table staging_errors",
        )

        d = entry.to_dict()
        restored = ChangeEntry.from_dict(d)

        assert restored.resource_type == ResourceType.BQ_TABLE
        assert restored.resource_id == "aos_logs.staging_errors"
        assert restored.kind == ChangeKind.CREATE
        assert restored.irreversible is True
        assert restored.human_description == "Create storage table staging_errors"
        assert restored.desired["table_id"] == "staging_errors"


# ── Private diff helpers ──────────────────────────────────────────────────────


class TestDiffHelpers:
    def test_diff_scheduler_treats_list_failure_as_all_create(self):
        """If GCP list() raises, all desired jobs come back as CREATE."""
        mock_sched = MagicMock()
        mock_sched.projects.return_value.locations.return_value.jobs.return_value.list.return_value.execute.side_effect = RuntimeError(
            "network error"
        )

        result = _diff_scheduler_jobs(mock_sched, PARENT, DESIRED_SCHEDULER_JOBS)

        assert all(e.kind == ChangeKind.CREATE for e in result)
        assert len(result) == len(DESIRED_SCHEDULER_JOBS)

    def test_diff_bq_missing_table_produces_create(self):
        """BQ table absent from dataset → CREATE entry with irreversible=True."""
        mock_bq = MagicMock()
        mock_bq.list_tables.return_value = []

        result = _diff_bq_tables(mock_bq, PROJECT, "aos_logs", ["staging_logs"])

        assert len(result) == 1
        assert result[0].kind == ChangeKind.CREATE
        assert result[0].irreversible is True

    def test_diff_secrets_present_secret_produces_no_change(self):
        """Secret already in SM → NO_CHANGE entry."""
        mock_sm = MagicMock()
        existing = MagicMock()
        existing.name = f"projects/{PROJECT}/secrets/GEMINI_API_KEY"
        mock_sm.list_secrets.return_value = [existing]

        result = _diff_secrets(mock_sm, PROJECT, ["GEMINI_API_KEY"])

        assert len(result) == 1
        assert result[0].kind == ChangeKind.NO_CHANGE

    def test_diff_scheduler_update_when_schedule_differs(self):
        """Scheduler exists with different schedule → UPDATE entry."""
        mock_sched = MagicMock()
        mock_sched.projects.return_value.locations.return_value.jobs.return_value.list.return_value.execute.return_value = {
            "jobs": [
                {
                    "name": f"{PARENT}/jobs/gaos-archive",
                    "schedule": "0 3 * * *",  # differs from desired "0 2 * * *"
                }
            ]
        }

        desired = [{"id": "gaos-archive", "schedule": "0 2 * * *", "path": "/archive"}]
        result = _diff_scheduler_jobs(mock_sched, PARENT, desired)

        assert len(result) == 1
        assert result[0].kind == ChangeKind.UPDATE
        assert result[0].actual["schedule"] == "0 3 * * *"
