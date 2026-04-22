"""
tools/google_search.py — Serper.dev Google Search wrapper for Scout's deep research.

Wraps the Serper.dev API (https://serper.dev) which proxies real Google Search results.
API key is read from GCP Secret Manager at call time — never stored in settings.yaml.

Free tier: 2,500 queries/month. Paid tiers from $50/mo for 50k queries.

Usage:
    from tools.google_search import search, research_topic
    results = search("loyalty program market trends 2026", project_id="morphic-gaos-prod")
    # Returns: [{"title": "...", "url": "...", "snippet": "...", "date": ""}, ...]

Spec: GAOS-Tools-Spec.md §17
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from tools import tracked

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://google.serper.dev/search"
_TIMEOUT_S = 10.0
_API_KEY_SECRET = "SERPER_API_KEY"  # pragma: allowlist secret

# ── Error types ──────────────────────────────────────────────────────────────


class GoogleSearchError(Exception):
    """Search API call failed, quota exceeded, or credentials unavailable."""


# ── Core search ──────────────────────────────────────────────────────────────


@tracked("google_search")
def search(
    query: str,
    project_id: str,
    num: int = 10,
) -> list[dict[str, Any]]:
    """
    Execute a single Google Custom Search query.

    Args:
        query:      Search query string.
        project_id: GCP project for Secret Manager access (API key + CX).
        num:        Max results to return (1–10; Google API hard cap is 10).

    Returns:
        List of dicts: ``[{title, url, snippet, date}, ...]``
        Returns empty list when no results found.

    Raises:
        GoogleSearchError: API error response, quota exceeded (429), or
                           credentials not available in Secret Manager.
    """
    if not query or not query.strip():
        return []

    from tools.secrets import SecretAccessDenied, SecretNotFoundError, get_secret

    try:
        api_key = get_secret(_API_KEY_SECRET, project_id)
    except (SecretNotFoundError, SecretAccessDenied) as exc:
        raise GoogleSearchError(f"search: credentials not available: {exc}") from exc

    num = max(1, min(num, 10))

    try:
        resp = httpx.post(
            _SEARCH_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query.strip(), "num": num},
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if status_code == 429:
            raise GoogleSearchError("search: daily quota exceeded (429)") from exc
        if status_code == 403:
            raise GoogleSearchError("search: API key invalid or quota exceeded (403)") from exc
        raise GoogleSearchError(f"search: HTTP {status_code}") from exc
    except httpx.TimeoutException as exc:
        raise GoogleSearchError(f"search: request timed out after {_TIMEOUT_S}s") from exc
    except httpx.ConnectError as exc:
        raise GoogleSearchError(f"search: network error: {exc}") from exc
    except ValueError as exc:
        raise GoogleSearchError(f"search: JSON decode failed: {exc}") from exc

    results: list[dict[str, Any]] = []
    for item in data.get("organic", []):
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "snippet": item.get("snippet", ""),
                "date": item.get("date", ""),
            }
        )

    return results


# ── Multi-query research ─────────────────────────────────────────────────────


@tracked("google_search")
def research_topic(
    queries: list[str],
    project_id: str,
    max_queries: int = 15,
) -> list[dict[str, Any]]:
    """
    Execute multiple Google Custom Search queries, deduplicating results by URL.

    Queries are executed in order up to ``max_queries``. Failed queries are
    logged and skipped — the remaining queries still execute. Results from all
    queries are merged into a single deduplicated list.

    Args:
        queries:     List of search query strings.
        project_id:  GCP project for Secret Manager access.
        max_queries: Hard cap on total queries executed (default 15).

    Returns:
        Deduplicated list of result dicts: ``[{title, url, snippet, date}, ...]``
        Empty list if all queries fail or the input list is empty.
    """
    if not queries:
        return []

    seen_urls: set[str] = set()
    results: list[dict[str, Any]] = []
    executed = 0

    for query in queries[:max_queries]:
        if executed >= max_queries:
            break
        if not query or not query.strip():
            continue
        try:
            batch = search(query, project_id)
            executed += 1
            for item in batch:
                url = item.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    results.append(item)
        except GoogleSearchError as exc:
            logger.warning(
                "research_topic: query %r failed — skipping: %s",
                query[:60],
                exc,
            )

    return results
