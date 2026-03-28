"""
scripts/create_staging_tables.py — Run-once idempotent DDL to create the 4 BigQuery
staging tables used by the Grafana live dashboard sync (Phase 5, Step 8.1).

Each table mirrors a Google Sheets control-plane tab. All data columns are
STRING to avoid schema drift on Sheet column renames. The ``synced_at`` column
is TIMESTAMP and is added by the sync handler, not by Sheet data.

Usage:
    python scripts/create_staging_tables.py [--project <project_id>]

Prerequisites:
  - ADC configured:  gcloud auth application-default login
  - SA has roles/bigquery.dataEditor on the aos_logs dataset
  - .venv activated
  - Run from repo root
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from google.cloud import bigquery

DEFAULT_PROJECT = "morphic-gaos-prod"

# ── DDL statements — CREATE TABLE IF NOT EXISTS (idempotent) ─────────────────
# Column order matches the Sheet headers after normalization (strip, lower,
# spaces→underscores, /→underscore). All data columns are STRING.

_DDL_STATEMENTS: list[tuple[str, str]] = [
    (
        "staging_approvals",
        """\
CREATE TABLE IF NOT EXISTS `{project}.aos_logs.staging_approvals` (
    id                  STRING,
    agent_id            STRING,
    issue               STRING,
    trigger_reason      STRING,
    stopping_constraint STRING,
    iterations_run      STRING,
    total_cost_usd      STRING,
    proposed_code       STRING,
    status              STRING,
    timestamp           STRING,
    approved_by         STRING,
    approver_tier       STRING,
    code_sha256         STRING,
    priority            STRING,
    synced_at           TIMESTAMP
)""",
    ),
    (
        "staging_logs",
        """\
CREATE TABLE IF NOT EXISTS `{project}.aos_logs.staging_logs` (
    timestamp   STRING,
    agent_id    STRING,
    level       STRING,
    message     STRING,
    project_id  STRING,
    synced_at   TIMESTAMP
)""",
    ),
    (
        "staging_errors",
        """\
CREATE TABLE IF NOT EXISTS `{project}.aos_logs.staging_errors` (
    timestamp   STRING,
    agent_id    STRING,
    error_type  STRING,
    message     STRING,
    traceback   STRING,
    project_id  STRING,
    synced_at   TIMESTAMP
)""",
    ),
    (
        "staging_pending_knowledge",
        """\
CREATE TABLE IF NOT EXISTS `{project}.aos_logs.staging_pending_knowledge` (
    timestamp   STRING,
    agent_id    STRING,
    observation STRING,
    source      STRING,
    confidence  STRING,
    status      STRING,
    project_id  STRING,
    synced_at   TIMESTAMP
)""",
    ),
]


def _load_settings() -> dict:
    path = Path(__file__).parent.parent / "config" / "settings.yaml"
    if not path.exists():
        print("ERROR: config/settings.yaml not found. Run setup_workspace.py first.")
        sys.exit(1)
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create Grafana staging tables in BigQuery (idempotent)."
    )
    parser.add_argument(
        "--project",
        default=None,
        help="GCP project ID (default: gcp.project_id from settings.yaml)",
    )
    args = parser.parse_args()

    settings = _load_settings()
    project_id: str = args.project or settings.get("gcp", {}).get("project_id", DEFAULT_PROJECT)

    print(f"Project: {project_id}")
    print()

    client = bigquery.Client(project=project_id)

    failed = False
    for table_name, ddl_template in _DDL_STATEMENTS:
        sql = ddl_template.format(project=project_id)
        print(f"  {table_name} ... ", end="", flush=True)
        try:
            client.query(sql).result()
            print("ok")
        except Exception as exc:
            print(f"FAILED: {exc}")
            failed = True

    print()
    if failed:
        print("One or more tables failed to create. See errors above.")
        sys.exit(1)
    print("Done. All 4 staging tables are ready in aos_logs.")


if __name__ == "__main__":
    main()
