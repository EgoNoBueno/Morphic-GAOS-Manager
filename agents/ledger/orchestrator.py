"""
agents/ledger/orchestrator.py — Tier 2 Accounting Orchestrator

Ledger tracks all money in/out of the business: invoices, expenses,
reconciliation, and P&L summaries. It owns the `Accounting` Sheet tab
and proposes payments via the Approval Gate.

Construction spec: Docs/GAOS-Agent-Spec.md
Identity file:     Docs/agents/ledger.md
Master spec:       Docs/GAOS-Manager-Spec.md §1.2
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# TODO (Phase 3): Implement Ledger orchestrator.
#
# Required per Docs/GAOS-Agent-Spec.md:
#   - ADK Agent subclass
#   - LangGraph StateGraph with nodes:
#       plan, dispatch, collect, report, park, resume, escalate
#   - Pub/Sub topic:  agent.ledger.events  (publish)
#   - Pub/Sub subscribe: agent.nexus-prime.events, agent.pursuit.events,
#                        agent.foreman.events, agent.approvals.events
#   - Sheet write: Accounting tab only
#   - Memory Bank: read/write (domain: accounting)
#   - model = settings.models.LOCAL_MODEL for classification/formatting;
#             FAST_MODEL for routing decisions
#   - Agent boot sequence (Docs/GAOS-Agent-Spec.md §6)
#
# Business-specific Tier 3 task agents go in agents/ledger/tasks/
# (gitignored — never committed to the public repo).
# Examples: invoice_parser.py, expense_classifier.py, pl_reporter.py
# ---------------------------------------------------------------------------
