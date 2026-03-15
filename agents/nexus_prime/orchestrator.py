"""
agents/nexus_prime/orchestrator.py — Tier 1 Root Agent

Nexus-Prime is the AOS general manager. It does not perform domain work
directly — it routes tasks to Tier 2 orchestrators, owns the approval gate,
and governs the self-evolution protocol.

Construction spec: Docs/GAOS-Agent-Spec.md
Identity file:     Docs/agents/nexus-prime.md
Master spec:       Docs/GAOS-Manager-Spec.md §1
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# TODO (Phase 3): Implement Nexus-Prime orchestrator.
#
# Required per Docs/GAOS-Agent-Spec.md:
#   - ADK Agent subclass
#   - LangGraph StateGraph with nodes:
#       plan, dispatch, collect, report, park, resume, escalate
#   - Pub/Sub subscribe to ALL orchestrator topics + agent.approvals.events
#   - Sheet write access: global control plane tabs
#   - Memory Bank: read/write (global + all domains)
#   - Approval Gate: submit proposals + process approvals
#   - Self-evolution: Write-Test-Refine loop (Vertex AI sandbox)
#   - model = settings.models.DEEP_MODEL
#   - Agent boot sequence (Docs/GAOS-Agent-Spec.md §6)
#
# Business-specific Tier 3 task agents go in agents/nexus_prime/tasks/
# (gitignored — never committed to the public repo).
# ---------------------------------------------------------------------------
