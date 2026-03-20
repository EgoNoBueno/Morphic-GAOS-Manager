"""
tools/vertex_search.py — Vertex AI Search (Discovery Engine) wrapper.

Provides read-side semantic search (Layer 5b) over the Knowledge/ Drive
corpus indexed by Vertex AI Search.  Indexing (Drive → Search sync) is
configured at the GCP project level — this module only performs queries.

Usage:
    from tools.vertex_search import query_playbooks, query_domain_knowledge

Spec: GAOS-Tools-Spec.md §15 · GAOS-Memory-Spec.md §3 (Layer 5b)
"""

from __future__ import annotations

import logging
from typing import Any

from config import get_settings

logger = logging.getLogger(__name__)


# ── Error types ───────────────────────────────────────────────────────────────


class VertexSearchError(Exception):
    """Unrecoverable Vertex AI Search API error."""


class DatastoreNotConfiguredError(Exception):
    """Required datastore ID is missing from settings."""


# ── Internal helpers ──────────────────────────────────────────────────────────


def _serving_config(gcp_project: str, location: str, datastore_id: str) -> str:
    """Build the full serving-config resource path for a Discovery Engine request."""
    return (
        f"projects/{gcp_project}/locations/{location}"
        f"/collections/default_collection/dataStores/{datastore_id}"
        f"/servingConfigs/default_config"
    )


def _extract_result(result: Any) -> dict[str, Any]:
    """Extract a plain dict from a Discovery Engine SearchResult protobuf."""
    doc = result.document
    data: dict[str, Any] = {}
    if doc.derived_struct_data:
        try:
            from google.protobuf.json_format import MessageToDict  # deferred

            data = MessageToDict(doc.derived_struct_data)
        except Exception:
            pass
    snippets = data.get("snippets", [])
    snippet_text = snippets[0].get("snippet", "") if snippets else ""
    return {
        "id": doc.id,
        "title": data.get("title", ""),
        "snippet": snippet_text,
        "link": data.get("link", ""),
    }


# ── Public API ────────────────────────────────────────────────────────────────


def search_knowledge(
    query: str,
    project_id: str,
    datastore_id: str,
    max_results: int = 5,
) -> list[dict[str, Any]]:
    """
    Run a semantic search against a Vertex AI Search datastore.

    Args:
        query:        Natural-language search query.
        project_id:   AOS project namespace (used to resolve GCP project ID).
        datastore_id: Vertex AI Search datastore short-form ID.
        max_results:  Maximum number of results to return (default 5).

    Returns:
        List of dicts with keys: ``id``, ``title``, ``snippet``, ``link``.
        Returns an empty list if the query is blank or yields no results.

    Raises:
        DatastoreNotConfiguredError: ``datastore_id`` is empty.
        VertexSearchError: Discovery Engine API failure.
    """
    if not query or not query.strip():
        return []

    if not datastore_id:
        raise DatastoreNotConfiguredError(
            "datastore_id is required — configure vertex_search.playbook_datastore_id "
            "or vertex_search.knowledge_datastore_id in settings.yaml."
        )

    settings = get_settings()
    gcp_project = settings.GCP_PROJECT_ID
    vs_cfg = getattr(settings, "vertex_search", None)
    location = vs_cfg.location if vs_cfg else "global"
    serving_cfg = _serving_config(gcp_project, location, datastore_id)

    try:
        from google.cloud import discoveryengine_v1 as discoveryengine  # deferred

        client = discoveryengine.SearchServiceClient()
        request = discoveryengine.SearchRequest(
            serving_config=serving_cfg,
            query=query.strip(),
            page_size=max_results,
        )
        response = client.search(request)
        return [_extract_result(r) for r in response]
    except DatastoreNotConfiguredError:
        raise
    except Exception as exc:
        raise VertexSearchError(f"search_knowledge failed: {exc}") from exc


def query_playbooks(
    query: str,
    project_id: str,
    max_results: int = 5,
) -> list[dict[str, Any]]:
    """
    Convenience wrapper: search the configured playbooks datastore.

    Uses ``settings.vertex_search.playbook_datastore_id``.

    Returns:
        Same format as ``search_knowledge``.

    Raises:
        DatastoreNotConfiguredError: ``playbook_datastore_id`` not set.
        VertexSearchError: API failure.
    """
    settings = get_settings()
    vs_cfg = getattr(settings, "vertex_search", None)
    ds_id = vs_cfg.playbook_datastore_id if vs_cfg else ""
    if not ds_id:
        raise DatastoreNotConfiguredError(
            "vertex_search.playbook_datastore_id is not set in settings.yaml."
        )
    return search_knowledge(query, project_id, ds_id, max_results)


def query_domain_knowledge(
    query: str,
    project_id: str,
    max_results: int = 5,
) -> list[dict[str, Any]]:
    """
    Convenience wrapper: search the configured general-knowledge datastore.

    Uses ``settings.vertex_search.knowledge_datastore_id``.

    Returns:
        Same format as ``search_knowledge``.

    Raises:
        DatastoreNotConfiguredError: ``knowledge_datastore_id`` not set.
        VertexSearchError: API failure.
    """
    settings = get_settings()
    vs_cfg = getattr(settings, "vertex_search", None)
    ds_id = vs_cfg.knowledge_datastore_id if vs_cfg else ""
    if not ds_id:
        raise DatastoreNotConfiguredError(
            "vertex_search.knowledge_datastore_id is not set in settings.yaml."
        )
    return search_knowledge(query, project_id, ds_id, max_results)
