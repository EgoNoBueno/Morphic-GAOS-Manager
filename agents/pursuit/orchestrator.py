"""
agents/pursuit/orchestrator.py — Tier 2 Sales Orchestrator

Pursuit manages CRM updates, lead follow-up sequences, quote generation,
and pipeline reporting. It owns the Sales by Product Sheet tab and
coordinates with Beacon (qualified leads) and Ledger (deal-closed invoicing).

Construction spec: Docs/GAOS-Agent-Spec.md
Identity file:     Docs/agents/pursuit.md
Master spec:       Docs/GAOS-Manager-Spec.md §1.2
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# TODO (Phase 3): Implement Pursuit orchestrator.
#
# Required per Docs/GAOS-Agent-Spec.md:
#   - ADK Agent subclass
#   - LangGraph StateGraph with nodes:
#       plan, dispatch, collect, report, park, resume, escalate
#   - Pub/Sub topic:  agent.pursuit.events  (publish)
#   - Pub/Sub subscribe: agent.nexus-prime.events, agent.beacon.events,
#                        agent.ledger.events, agent.foreman.events,
#                        agent.approvals.events
#   - Sheet write: Sales by Product tab only
#   - Memory Bank: read/write (domain: sales)
#   - model = settings.models.LOCAL_MODEL for lead scoring/formatting;
#             FAST_MODEL for routing and follow-up decisions
#   - Agent boot sequence (Docs/GAOS-Agent-Spec.md §6)
#
# Business-specific Tier 3 task agents go in agents/pursuit/tasks/
# (gitignored — never committed to the public repo).
# Examples: lead_scorer.py, followup_emailer.py, quote_builder.py
# ---------------------------------------------------------------------------
