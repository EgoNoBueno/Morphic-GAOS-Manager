"""
tools/bigquery.py — BigQuery writer for Morphic-G AOS.

Provides a single insert_row() function used by agents to write task
outcomes and error logs to BigQuery. All writes are best-effort streaming
inserts — the caller should not retry on transient errors; the tool
handles backoff internally.

Table references use the format: "dataset.table"
(e.g., "aos_logs.task_outcomes"). The GCP project is inferred from
settings.GCP_PROJECT_ID.
"""
from __future__ import annotations

import time
from typing import Any

from google.cloud import bigquery
from google.api_core.exceptions import GoogleAPICallError

from config import get_settings


# ── Error types ──────────────────────────────────────────────────────────────


class BigQueryInsertError(Exception):
    """Streaming insert failed after retry exhaustion."""


class BigQueryRowError(Exception):
    """BigQuery rejected one or more rows (schema mismatch or invalid value)."""


# ── Internal helpers ──────────────────────────────────────────────────────────


def _full_table_ref(table_ref: str, gcp_project: str) -> str:
    """Return a fully qualified ``project.dataset.table`` reference."""
    if table_ref.count(".") == 2:
        return table_ref  # already fully qualified
    return f"{gcp_project}.{table_ref}"


# ── Public API ────────────────────────────────────────────────────────────────


def insert_row(table_ref: str, row: dict[str, Any], project_id: str = "") -> None:
    """
    Stream one row into a BigQuery table.

    Args:
        table_ref:  Unqualified ``dataset.table`` or fully qualified
                    ``project.dataset.table``.
        row:        Dict keyed by column name. Values must be JSON-serialisable.
        project_id: Unused (present for API symmetry with other tools). The GCP
                    project is always read from settings.GCP_PROJECT_ID.

    Raises:
        BigQueryInsertError: API call failed after 3 retries.
        BigQueryRowError:    BigQuery rejected the row (schema mismatch).
    """
    settings = get_settings()
    gcp_project = settings.GCP_PROJECT_ID
    full_ref = _full_table_ref(table_ref, gcp_project)

    client = bigquery.Client(project=gcp_project)

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            errors = client.insert_rows_json(full_ref, [row])
            if errors:
                raise BigQueryRowError(
                    f"BigQuery rejected row for '{full_ref}': {errors}"
                )
            return
        except BigQueryRowError:
            raise
        except GoogleAPICallError as exc:
            last_exc = exc
            time.sleep(2 ** attempt)

    raise BigQueryInsertError(
        f"BigQuery insert into '{full_ref}' failed after 3 retries: {last_exc}"
    ) from last_exc


def insert_rows(table_ref: str, rows: list[dict[str, Any]], project_id: str = "") -> None:
    """
    Stream multiple rows into a BigQuery table in a single API call.

    Prefer this over calling insert_row() in a loop for batches of ≥ 2 rows.

    Raises:
        BigQueryInsertError: API call failed after 3 retries.
        BigQueryRowError:    One or more rows were rejected.
    """
    if not rows:
        return

    settings = get_settings()
    gcp_project = settings.GCP_PROJECT_ID
    full_ref = _full_table_ref(table_ref, gcp_project)

    client = bigquery.Client(project=gcp_project)

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            errors = client.insert_rows_json(full_ref, rows)
            if errors:
                raise BigQueryRowError(
                    f"BigQuery rejected rows for '{full_ref}': {errors}"
                )
            return
        except BigQueryRowError:
            raise
        except GoogleAPICallError as exc:
            last_exc = exc
            time.sleep(2 ** attempt)

    raise BigQueryInsertError(
        f"BigQuery batch insert into '{full_ref}' failed after 3 retries: {last_exc}"
    ) from last_exc
