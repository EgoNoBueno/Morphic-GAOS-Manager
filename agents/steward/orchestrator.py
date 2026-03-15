"""
agents/steward/orchestrator.py — Tier 2 Admin & HR Orchestrator

Steward manages calendar coordination, meeting scheduling, compliance
reminders, and employee onboarding tasks. It owns the Logs Sheet tab.

Construction spec: Docs/GAOS-Agent-Spec.md
Identity file:     Docs/agents/steward.md
Master spec:       Docs/GAOS-Manager-Spec.md §1.2
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# TODO (Phase 3): Implement Steward orchestrator.
#
# Required per Docs/GAOS-Agent-Spec.md:
#   - ADK Agent subclass
#   - LangGraph StateGraph with nodes:
#       plan, dispatch, collect, report, park, resume, escalate
#   - Pub/Sub topic:  agent.steward.events  (publish)
#   - Pub/Sub subscribe: agent.nexus-prime.events, agent.approvals.events
#   - Sheet write: Logs tab only
#   - Memory Bank: read/write (domain: admin)
#   - model = settings.models.LOCAL_MODEL for scheduling/formatting tasks;
#             FAST_MODEL for compliance and onboarding routing
#   - Agent boot sequence (Docs/GAOS-Agent-Spec.md §6)
#
# Business-specific Tier 3 task agents go in agents/steward/tasks/
# (gitignored — never committed to the public repo).
# Examples: calendar_coordinator.py, deadline_reminder.py, document_filer.py
# ---------------------------------------------------------------------------
