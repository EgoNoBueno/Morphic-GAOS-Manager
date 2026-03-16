"""
tools/web_search.py — Lightweight web search for LOCAL_MODEL (Ollama) context injection.

Uses the DuckDuckGo Instant Answer API — no API key required, no cost.
Results are returned as a formatted string ready to prepend to an LLM prompt.

Usage:
    from tools.web_search import web_search
    snippets = web_search("current price of steel per ton 2026")
    # Returns a multi-line string of top results, or "" on failure.
"""
from __future__ import annotations

import logging

import httpx

_DDG_URL = "https://api.duckduckgo.com/"
_RELATED_MAX = 5
_TIMEOUT_S = 5.0

logger = logging.getLogger(__name__)


def web_search(query: str, max_results: int = _RELATED_MAX) -> str:
    """
    Query DuckDuckGo Instant Answer API and return a formatted snippet string.

    Args:
        query:       Natural-language search query.
        max_results: Maximum number of related-topic snippets to include (default 5).

    Returns:
        Formatted string with AbstractText + up to max_results RelatedTopics,
        or empty string if the request fails or returns no results.
    """
    if not query or not query.strip():
        return ""

    params = {
        "q": query.strip(),
        "format": "json",
        "no_html": "1",
        "no_redirect": "1",
        "skip_disambig": "1",
    }

    try:
        resp = httpx.get(_DDG_URL, params=params, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPStatusError) as exc:
        logger.warning("web_search: request failed (%s) — returning empty", type(exc).__name__)
        return ""
    except ValueError:
        logger.warning("web_search: JSON decode failed — returning empty")
        return ""

    lines: list[str] = []

    abstract = data.get("AbstractText", "").strip()
    abstract_source = data.get("AbstractSource", "")
    if abstract:
        source_tag = f" [{abstract_source}]" if abstract_source else ""
        lines.append(f"Summary{source_tag}: {abstract}")

    answer = data.get("Answer", "").strip()
    if answer:
        lines.append(f"Direct answer: {answer}")

    for topic in data.get("RelatedTopics", [])[:max_results]:
        # RelatedTopics entries are dicts with "Text" or nested "Topics"
        text = topic.get("Text", "").strip()
        if text:
            lines.append(f"- {text}")

    return "\n".join(lines)
