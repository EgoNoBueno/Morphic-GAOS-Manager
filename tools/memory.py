"""
tools/memory.py — Memory layer operations for Morphic-G AOS.

Provides the four memory-layer public functions used by agents at boot and
during task execution. The full schema definitions, confidence scoring,
and self-learning loop are documented in GAOS-Memory-Spec.md.

Access control:
  - query_episodic, flush_observations, load_domain_memory: Tier 1 + Tier 2 agents.
  - write_approved_memory, query_memory_bank: Nexus-Prime (Tier 1) only.

Spec: GAOS-Tools-Spec.md §8 + GAOS-Memory-Spec.md
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from config import get_settings

if TYPE_CHECKING:
    from models import MemoryEntry


# ── Error types ──────────────────────────────────────────────────────────────


class MemoryBankError(Exception):
    """Unrecoverable Vertex AI Memory Bank API error."""


class UnauthorizedMemoryWrite(Exception):
    """Caller is not Nexus-Prime's service account."""


# ── Layer 2 — Episodic (BigQuery) ─────────────────────────────────────────────


def query_episodic(
    agent_id: str,
    project_id: str,
    task_type: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """
    Return the N most recent task outcomes for this agent + task_type.
    Queries BigQuery ``aos_logs.task_outcomes`` — not Cloud Logging.

    Used at the start of a new task to surface relevant prior context
    before calling the LLM, and inside the evolution loop for no-progress
    detection.

    Full implementation: GAOS-Memory-Spec.md §4.

    Raises:
        MemoryBankError: BigQuery query failure.
    """
    from google.cloud import bigquery  # deferred — not needed at import time

    settings = get_settings()
    gcp_project = settings.GCP_PROJECT_ID
    client = bigquery.Client(project=gcp_project)
    sql = """
        SELECT task_id, status, result_summary, error_fingerprint,
               total_cost_usd, timestamp
        FROM `{gcp_project}.aos_logs.task_outcomes`
        WHERE agent_id = @agent_id
          AND task_type = @task_type
        ORDER BY timestamp DESC
        LIMIT @limit
    """.replace("{gcp_project}", gcp_project)
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("agent_id", "STRING", agent_id),
            bigquery.ScalarQueryParameter("task_type", "STRING", task_type),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
    )
    try:
        return [dict(row) for row in client.query(sql, job_config=job_config)]
    except Exception as exc:
        raise MemoryBankError(f"query_episodic failed: {exc}") from exc


# ── Layer 3 — Observation Buffer (Sheets) ────────────────────────────────────


def flush_observations(observations: list[dict[str, Any]], project_id: str) -> None:
    """
    Write buffered observations to the ``Pending_Knowledge`` Sheet tab.
    Applies deduplication by ``content_hash`` before writing.

    Full implementation: GAOS-Memory-Spec.md §8.

    Raises:
        MemoryBankError: Write failure after retry exhaustion.
    """
    if not observations:
        return

    from tools.google_sheets import batch_append_rows, find_row  # deferred

    deduped: list[dict[str, Any]] = []
    for obs in observations:
        content_hash = obs.get("content_hash", "")
        if content_hash:
            existing = find_row("Pending_Knowledge", "content_hash", content_hash, project_id)
            if existing is not None:
                continue  # already buffered — skip
        deduped.append(obs)

    if deduped:
        try:
            batch_append_rows("Pending_Knowledge", deduped, project_id)
        except Exception as exc:
            raise MemoryBankError(f"flush_observations failed: {exc}") from exc


# ── Layer 4 — Semantic Memory (Vertex AI Memory Bank) ────────────────────────


def load_domain_memory(agent_id: str, project_id: str) -> dict[str, list[dict[str, Any]]]:
    """
    Batch-fetch all active memory entries for this agent's domain from the
    Vertex AI Memory Bank. Called once per agent boot.

    Returns a dict grouped by knowledge_type:
    ``{"fact": [...], "pattern": [...], "rule": [...], "preference": [...]}``.

    Full implementation: GAOS-Memory-Spec.md §6.

    Raises:
        MemoryBankError: Vertex AI API failure.
    """
    try:
        from vertexai.preview.memory import MemoryBankClient  # type: ignore[import-not-found]
    except ImportError as exc:
        raise MemoryBankError(
            "vertexai.preview.memory is not available. "
            "Install google-cloud-aiplatform with the preview extras."
        ) from exc

    try:
        client = MemoryBankClient(project=project_id)
        entries = client.list(
            filters={
                "agent_id": agent_id,
                "active": True,
                "project_id": project_id,
            }
        )
        context: dict[str, list[dict[str, Any]]] = {
            "fact": [],
            "pattern": [],
            "rule": [],
            "preference": [],
        }
        for e in entries:
            bucket = context.setdefault(e.knowledge_type, [])
            bucket.append(
                {
                    "memory_id": e.memory_id,
                    "content": e.content,
                    "tags": e.tags,
                }
            )
        return context
    except Exception as exc:
        raise MemoryBankError(f"load_domain_memory failed: {exc}") from exc


def write_approved_memory(entry: MemoryEntry, project_id: str) -> str:
    """
    Write a newly approved memory entry to the Vertex AI Memory Bank.
    Called by Nexus-Prime only after approval.

    If ``entry.supersedes`` is set, the old entry is marked inactive first.

    Returns:
        memory_id: The Vertex AI-assigned memory ID.

    Raises:
        UnauthorizedMemoryWrite: Should be enforced at the agent level.
        MemoryBankError:         Vertex AI API failure.
    """
    try:
        from vertexai.preview.memory import MemoryBankClient  # type: ignore[import-not-found]
    except ImportError as exc:
        raise MemoryBankError(
            "vertexai.preview.memory is not available. "
            "Install google-cloud-aiplatform with the preview extras."
        ) from exc

    try:
        client = MemoryBankClient(project=project_id)
        if entry.supersedes:
            client.update(entry.supersedes, {"active": False})
        record = client.create(entry.model_dump())
        return record.memory_id
    except Exception as exc:
        raise MemoryBankError(f"write_approved_memory failed: {exc}") from exc


def query_memory_bank(
    query: str,
    corpus: str,
    project_id: str,
    top_k: int = 5,
    similarity_threshold: float = 0.80,
) -> list[dict[str, Any]]:
    """
    Perform a semantic similarity search against a Vertex AI Memory Bank corpus.

    Used by Nexus-Prime in ``diagnose`` (search by error fingerprint) and
    ``knowledge_review`` (duplicate detection).

    Args:
        query:                Text to search for.
        corpus:               Corpus ID (e.g., "gaos-ledger").
        project_id:           GCP project that owns the Memory Bank.
        top_k:                Maximum number of results to return.
        similarity_threshold: Minimum similarity score [0.0–1.0] to include
                              in the results.

    Returns:
        List of dicts: ``[{content, memory_id, similarity, tags}, ...]``
        sorted by similarity descending.

    Raises:
        MemoryBankError: Vertex AI API failure.
    """
    try:
        from vertexai.preview.memory import MemoryBankClient  # type: ignore[import-not-found]
    except ImportError as exc:
        raise MemoryBankError(
            "vertexai.preview.memory is not available. "
            "Install google-cloud-aiplatform with the preview extras."
        ) from exc

    try:
        client = MemoryBankClient(project=project_id)
        results = client.query(
            corpus=corpus,
            query=query,
            top_k=top_k,
        )
        return [
            {
                "memory_id": r.memory_id,
                "content": r.content,
                "similarity": r.similarity,
                "tags": getattr(r, "tags", []),
            }
            for r in results
            if r.similarity >= similarity_threshold
        ]
    except Exception as exc:
        raise MemoryBankError(f"query_memory_bank failed: {exc}") from exc
