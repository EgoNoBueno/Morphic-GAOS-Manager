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

from typing import Any

from google.api_core import retry
from google.api_core.exceptions import GoogleAPICallError, RetryError
from google.cloud import bigquery

from config import get_settings

# ── Error types ──────────────────────────────────────────────────────────────


class BigQueryInsertError(Exception):
    """Streaming insert failed after retry exhaustion."""


class BigQueryRowError(Exception):
    """BigQuery rejected one or more rows (schema mismatch or invalid value)."""


# ── Internal helpers ──────────────────────────────────────────────────────────

_client_cache: dict[str, bigquery.Client] = {}


def _get_client(gcp_project: str) -> bigquery.Client:
    """Return a cached bigquery.Client for *gcp_project*, creating it on first use."""
    if gcp_project not in _client_cache:
        _client_cache[gcp_project] = bigquery.Client(project=gcp_project)
    return _client_cache[gcp_project]


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
        BigQueryInsertError: API call failed after retrying transient errors.
        BigQueryRowError:    BigQuery rejected the row (schema mismatch).
    """
    settings = get_settings()
    gcp_project = settings.GCP_PROJECT_ID
    full_ref = _full_table_ref(table_ref, gcp_project)

    client = _get_client(gcp_project)

    @retry.Retry(
        predicate=retry.if_transient_error, initial=1.0, maximum=4.0, multiplier=2.0, deadline=10.0
    )
    def _insert() -> None:
        errors = client.insert_rows_json(full_ref, [row])
        if errors:
            raise BigQueryRowError(f"BigQuery rejected row for '{full_ref}': {errors}")

    try:
        _insert()
    except BigQueryRowError:
        raise
    except (GoogleAPICallError, RetryError) as exc:
        raise BigQueryInsertError(f"BigQuery insert into '{full_ref}' failed: {exc}") from exc


def insert_rows(
    table_ref: str,
    rows: list[dict[str, Any]],
    project_id: str = "",
    row_ids: list[str] | None = None,
) -> None:
    """Stream multiple rows into a BigQuery table in a single API call.

    Prefer this over calling insert_row() in a loop for batches of ≥ 2 rows.

    Args:
        table_ref:  Unqualified ``dataset.table`` or fully qualified ``project.dataset.table``.
        rows:       List of dicts keyed by column name. Values must be JSON-serialisable.
        project_id: Unused (present for API symmetry with other tools). GCP project is
                    always read from settings.GCP_PROJECT_ID.
        row_ids:    Optional list of stable, unique ``insertId`` strings (one per row).
                    When supplied, BigQuery uses these as deduplication keys for at-least-
                    once delivery — re-sending the same ``insertId`` within the dedup
                    window (currently ~1 minute) will not create duplicate rows. Pass a
                    deterministic key (e.g. SHA-256 of the row's natural key) when the
                    caller may retry, such as the nightly archive job.

    Raises:
        BigQueryInsertError: API call failed after retrying transient errors.
        BigQueryRowError:    One or more rows were rejected.
    """
    if not rows:
        return

    settings = get_settings()
    gcp_project = settings.GCP_PROJECT_ID
    full_ref = _full_table_ref(table_ref, gcp_project)

    client = _get_client(gcp_project)

    @retry.Retry(
        predicate=retry.if_transient_error, initial=1.0, maximum=4.0, multiplier=2.0, deadline=10.0
    )
    def _insert_batch() -> None:
        errors = client.insert_rows_json(full_ref, rows, row_ids=row_ids)
        if errors:
            raise BigQueryRowError(f"BigQuery rejected rows for '{full_ref}': {errors}")

    try:
        _insert_batch()
    except BigQueryRowError:
        raise
    except (GoogleAPICallError, RetryError) as exc:
        raise BigQueryInsertError(f"BigQuery batch insert into '{full_ref}' failed: {exc}") from exc


def query_rows(
    sql: str,
    project_id: str = "",
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Run a parameterised SELECT query and return results as a list of row dicts.

    Args:
        sql:        Standard SQL. Use @param_name syntax for named parameters.
        project_id: GCP project to query against. Defaults to ``settings.GCP_PROJECT_ID``
            when empty or omitted.
        params:     Optional named query parameters. Values must be str, int, float, or bool.

    Returns:
        A list of row dicts keyed by column name. Empty list if no rows match.

    Raises:
        BigQueryInsertError: Query failed due to an API or network error.
    """
    settings = get_settings()
    gcp_project = project_id if project_id else settings.GCP_PROJECT_ID
    client = _get_client(gcp_project)

    bq_params: list[bigquery.ScalarQueryParameter] = []
    for name, value in (params or {}).items():
        if isinstance(value, bool):
            bq_params.append(bigquery.ScalarQueryParameter(name, "BOOL", value))
        elif isinstance(value, int):
            bq_params.append(bigquery.ScalarQueryParameter(name, "INT64", value))
        elif isinstance(value, float):
            bq_params.append(bigquery.ScalarQueryParameter(name, "FLOAT64", value))
        else:
            bq_params.append(bigquery.ScalarQueryParameter(name, "STRING", str(value)))

    job_config = bigquery.QueryJobConfig(query_parameters=bq_params)
    try:
        job = client.query(sql, job_config=job_config)
        return [dict(row) for row in job.result()]
    except (GoogleAPICallError, RetryError) as exc:
        raise BigQueryInsertError(f"BigQuery query failed: {exc}") from exc
