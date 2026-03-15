"""
agents/scout/orchestrator.py — Tier 2 Research Orchestrator

Scout handles market research, competitor monitoring, trend analysis,
and product sourcing recommendations. It owns the Research Products
Sheet tab (via the Logs tab group).

Construction spec: Docs/GAOS-Agent-Spec.md
Identity file:     Docs/agents/scout.md
Master spec:       Docs/GAOS-Manager-Spec.md §1.2
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# TODO (Phase 3): Implement Scout orchestrator.
#
# Required per Docs/GAOS-Agent-Spec.md:
#   - ADK Agent subclass
#   - LangGraph StateGraph with nodes:
#       plan, dispatch, collect, report, park, resume, escalate
#   - Pub/Sub topic:  agent.scout.events  (publish)
#   - Pub/Sub subscribe: agent.nexus-prime.events, agent.approvals.events
#   - Sheet write: Research Products tab only
#   - Memory Bank: read/write (domain: research)
#   - model = settings.models.LOCAL_MODEL for summarization/extraction;
#             FAST_MODEL for trend routing and prioritization
#   - Agent boot sequence (Docs/GAOS-Agent-Spec.md §6)
#
# Business-specific Tier 3 task agents go in agents/scout/tasks/
# (gitignored — never committed to the public repo).
# Examples: web_scraper.py, trend_summarizer.py, competitor_price_monitor.py
# ---------------------------------------------------------------------------
