"""
models/__init__.py — Shared Pydantic schemas for Morphic-G AOS.

All agent-to-agent communication uses A2AMessage. The Approval Gate
uses ApprovalProposal. Both are validated Pydantic models — no raw
dicts cross module boundaries.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field


# ── Message type registry ──────────────────────────────────────────────────


class MessageType(str, Enum):
    STATUS_UPDATE     = "STATUS_UPDATE"    # Routine heartbeat / objective update
    TASK_HANDOFF      = "TASK_HANDOFF"     # Pass work to another orchestrator
    TASK_COMPLETE     = "TASK_COMPLETE"    # Task finished; no further action needed
    DATA_REQUEST      = "DATA_REQUEST"     # Request a data payload (awaits DATA_RESPONSE)
    DATA_RESPONSE     = "DATA_RESPONSE"    # Reply to a DATA_REQUEST
    ALERT             = "ALERT"            # Anomaly or error needing manager awareness
    ESCALATION        = "ESCALATION"       # Requires human decision via Approval Gate
    EVOLUTION_REQUEST = "EVOLUTION_REQUEST" # Code evolution cycle requested
    APPROVAL_RESULT   = "APPROVAL_RESULT"  # Human responded to a proposal
    KNOWLEDGE_CANDIDATE = "KNOWLEDGE_CANDIDATE"  # New observation for knowledge review
    NEW_PROJECT       = "NEW_PROJECT"      # Project Registry change detected
    BROADCAST         = "BROADCAST"        # Nexus-Prime → All: system-wide directive


# ── A2AMessage ─────────────────────────────────────────────────────────────


class A2AMessage(BaseModel):
    """
    Standard envelope for all agent-to-agent Pub/Sub messages.
    Defined in GAOS-Manager-Spec.md §10.2.
    """

    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str | None = None                   # Links all messages for one task
    project_id: str                              # Must match a row in Project Registry
    source_agent: str                            # e.g. "beacon"
    target_agent: str                            # e.g. "pursuit" | "nexus-prime" | "broadcast"
    message_type: MessageType
    priority: int = Field(ge=1, le=5)        # 1 (low) → 5 (critical)
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    requires_ack: bool = False


# ── ApprovalProposal ───────────────────────────────────────────────────────


class ApprovalStatus(str, Enum):
    PENDING = "Pending"
    APPROVED = "Approved"
    REJECTED = "Rejected"
    NEEDS_REVISION = "Needs Revision"


class ApprovalProposal(BaseModel):
    """
    Proposal row schema for the Agent_Approvals Sheet tab.
    Column order mirrors the header row defined in GAOS-Deploy-Spec.md §4.3.

    ID | Agent ID | Issue | Trigger Reason | Stopping Constraint |
    Iterations Run | Total Cost USD | Proposed Code | Status | Timestamp |
    Approved By | Approver Tier | code_sha256
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str
    issue: str
    trigger_reason: str
    stopping_constraint: str = ""
    iterations_run: int = 0
    total_cost_usd: float = 0.0
    proposed_code: str = ""
    status: ApprovalStatus = ApprovalStatus.PENDING
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    approved_by: str = ""
    approver_tier: int = 0
    code_sha256: str = ""

    def to_sheet_row(self) -> dict[str, Any]:
        """Serialize to a dict keyed by the Agent_Approvals header row."""
        return {
            "ID": self.id,
            "Agent ID": self.agent_id,
            "Issue": self.issue,
            "Trigger Reason": self.trigger_reason,
            "Stopping Constraint": self.stopping_constraint,
            "Iterations Run": self.iterations_run,
            "Total Cost USD": round(self.total_cost_usd, 4),
            "Proposed Code": self.proposed_code,
            "Status": self.status.value,
            "Timestamp": self.timestamp.isoformat(),
            "Approved By": self.approved_by,
            "Approver Tier": self.approver_tier,
            "code_sha256": self.code_sha256,
        }


# ── AgentInput / AgentOutput ────────────────────────────────────────────────────────

class AgentInput(BaseModel):
    """
    Standard input envelope for all agents.
    Defined in GAOS-Agent-Spec.md §2.2.
    """

    task_id: str                # UUID — links all log entries for one task
    project_id: str             # Project namespace — never omit
    instruction: str            # Natural language task description
    context: dict[str, Any] = Field(default_factory=dict)  # Structured task context


class AgentOutput(BaseModel):
    """
    Standard output envelope for all agents.
    Defined in GAOS-Agent-Spec.md §2.2.
    """

    task_id: str
    project_id: str
    agent_id: str               # Matches Agent.name
    status: Literal["success", "escalated", "failed"]
    result: dict[str, Any] = Field(default_factory=dict)  # Task-specific output
    cost_usd: float = 0.0       # Accumulated model cost for this task
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ── AgentWorkingMemory ────────────────────────────────────────────────────────────


class AgentWorkingMemory(TypedDict):
    """
    LangGraph state schema shared by all orchestrator agents (Tier 1 + Tier 2).
    Defined in GAOS-Memory-Spec.md §3; extended by NexusPrimeWorkingMemory
    in GAOS-Nexus-Prime-Spec.md §2.
    """
    # Core task context
    task_id: str                         # Current task UUID
    project_id: str                      # Active project namespace
    current_objective: str               # What the agent is doing right now

    # Sub-task tracking
    sub_task_results: list[dict]         # Collected outputs from Tier 3 sub-agents
    parked_proposals: list[str]          # Proposal IDs awaiting Approval Gate
    error_history: list[str]             # Error fingerprints seen this session

    # Memory layers (loaded once at boot — not refreshed mid-task)
    memory_context: dict                 # Layer 4 semantic facts cached at boot
    episodic_cache: dict                 # Layer 2 recent outcomes cached at boot
    observation_buffer: list[dict]       # Layer 3 candidate learnings this session

    # Cost and loop tracking
    cost_usd: float                      # Running cost for this invocation
    iteration_count: int                 # Evolution loop iteration (see §13.1)
    step_count: int                      # LangGraph step counter
    tokens_used: int                     # Token consumption this invocation

    # Event processing
    incoming_message: A2AMessage | None  # Current message being handled
    messages: list                       # LangGraph message log

    # Guard flags
    hard_stop_triggered: bool            # True if a hard stop constraint fired
    evolution_triggered: bool            # True if Write-Test-Refine loop is active


# ── MemoryEntry ───────────────────────────────────────────────────────────────────


class MemoryEntry(BaseModel):
    """
    A single approved knowledge entry in the Vertex AI Memory Bank.
    Defined in GAOS-Memory-Spec.md §6.
    """

    memory_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    agent_id: str               # Domain owner (e.g., "ledger", "beacon")
    knowledge_type: str         # "fact" | "pattern" | "rule" | "preference"
    domain: str                 # Business domain (aligns with orchestrator names)
    content: str                # The knowledge, as a clear declarative statement
    evidence: list[str] = Field(default_factory=list)  # task_ids that supported approval
    confidence: float = 0.0
    approved_by: str = ""
    approved_at: datetime | None = None
    version: int = 1            # Starts at 1; increments on each approved update
    supersedes: str | None = None  # memory_id of the entry this replaces
    active: bool = True
    tags: list[str] = Field(default_factory=list)


# ── KnowledgeProposal ────────────────────────────────────────────────────────────────


class KnowledgeProposal(BaseModel):
    """
    Proposal written to Agent_Approvals for a knowledge change.
    Defined in GAOS-Memory-Spec.md §9.
    """

    proposal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    knowledge_id: str           # Links to Pending_Knowledge row
    project_id: str
    agent_id: str
    knowledge_type: str
    domain: str
    priority: int = Field(ge=1, le=5)

    # For new memory entries
    proposed_content: str = ""

    # For updates to existing entries (both required for update proposals)
    existing_memory_id: str | None = None
    existing_content: str | None = None

    # For procedural document updates
    drive_file_path: str | None = None
    proposed_diff: str | None = None

    evidence: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    observation_count: int = 0
