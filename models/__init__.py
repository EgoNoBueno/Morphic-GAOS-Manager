"""
models/__init__.py — Shared Pydantic schemas for Morphic-G AOS.

All agent-to-agent communication uses A2AMessage. The Approval Gate
uses ApprovalProposal. Both are validated Pydantic models — no raw
dicts cross module boundaries.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field

# ── Message type registry ──────────────────────────────────────────────────


class MessageType(StrEnum):
    STATUS_UPDATE = "STATUS_UPDATE"  # Routine heartbeat / objective update
    TASK_HANDOFF = "TASK_HANDOFF"  # Pass work to another orchestrator
    TASK_COMPLETE = "TASK_COMPLETE"  # Task finished; no further action needed
    DATA_REQUEST = "DATA_REQUEST"  # Request a data payload (awaits DATA_RESPONSE)
    DATA_RESPONSE = "DATA_RESPONSE"  # Reply to a DATA_REQUEST
    ALERT = "ALERT"  # Anomaly or error needing manager awareness
    ESCALATION = "ESCALATION"  # Requires human decision via Approval Gate
    EVOLUTION_REQUEST = "EVOLUTION_REQUEST"  # Code evolution cycle requested
    APPROVAL_REQUEST = "APPROVAL_REQUEST"  # Agent requests human approval (Approval Gate)
    APPROVAL_RESULT = "APPROVAL_RESULT"  # Human responded to a proposal
    KNOWLEDGE_CANDIDATE = "KNOWLEDGE_CANDIDATE"  # New observation for knowledge review
    NEW_PROJECT = "NEW_PROJECT"  # Project Registry change detected
    BROADCAST = "BROADCAST"  # Nexus-Prime → All: system-wide directive
    TTL_SWEEP = "TTL_SWEEP"  # Cloud Scheduler: sweep stale proposals
    NIGHTLY_ARCHIVE = "NIGHTLY_ARCHIVE"  # Cloud Scheduler: nightly Sheet → BigQuery archive
    # ── Phase 2.5 — Conversation Layer ────────────────────────────────────
    CHAT_MESSAGE = "CHAT_MESSAGE"  # Inbound message from Google Chat (owner → Nexus-Prime)
    DAILY_SYNC = "DAILY_SYNC"  # Cloud Scheduler: 6 AM morning briefing trigger
    VISION_SUBMITTED = "VISION_SUBMITTED"  # Owner submitted a project vision (Chat or AppSheet)
    PLAN_REVIEW = "PLAN_REVIEW"  # Owner commented on a Blueprint Doc constraint
    COMMENT_RECEIVED = "COMMENT_RECEIVED"  # Doc comment poll detected a new owner comment
    RESEARCH_MANDATE = "RESEARCH_MANDATE"  # Nexus-Prime → Scout: deep structured research request
    SKILL_REQUEST = "SKILL_REQUEST"  # Agent requests owner approval to install a new library
    KNOWLEDGE_INJECTION = (
        "KNOWLEDGE_INJECTION"  # Scout: corroborated market intelligence (≥5 sources)
    )
    # ── Phase 3 — Reactive cross-domain routing ────────────────────────────
    STOCK_INSUFFICIENT = "STOCK_INSUFFICIENT"  # Foreman: stockout detected → Nexus-Prime dispatches Scout sourcing pivot
    DEAL_CLOSED = "DEAL_CLOSED"  # Pursuit: deal closed → Nexus-Prime checks margin → dispatches Beacon ROI analysis
    # ── Infrastructure Provisioner (Infra Provisioner §20) ─────────────────
    INFRA_PROVISION_APPROVED = "INFRA_PROVISION_APPROVED"  # Owner approved infra change card
    INFRA_PROVISION_REJECTED = "INFRA_PROVISION_REJECTED"  # Owner rejected infra change card


# ── A2AMessage ─────────────────────────────────────────────────────────────


class A2AMessage(BaseModel):
    """
    Standard envelope for all agent-to-agent Pub/Sub messages.
    Defined in GAOS-Manager-Spec.md §10.2.
    """

    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str | None = None  # Links all messages for one task
    project_id: str  # Must match a row in Project Registry
    source_agent: str  # e.g. "beacon"
    target_agent: str  # e.g. "pursuit" | "nexus-prime" | "broadcast"
    message_type: MessageType
    priority: int = Field(ge=1, le=5)  # 1 (low) → 5 (critical)
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = (
        Field(  # datetime — published via Pub/Sub; model_dump_json() handles serialization
            default_factory=lambda: datetime.now(UTC)
        )
    )
    requires_ack: bool = False


# ── ApprovalProposal ───────────────────────────────────────────────────────


class ApprovalStatus(StrEnum):
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
    timestamp: datetime = Field(  # datetime — to_sheet_row() calls .isoformat() explicitly; not written to BQ directly
        default_factory=lambda: datetime.now(UTC)
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

    task_id: str  # UUID — links all log entries for one task
    project_id: str  # Project namespace — never omit
    instruction: str  # Natural language task description
    context: dict[str, Any] = Field(default_factory=dict)  # Structured task context


class AgentOutput(BaseModel):
    """
    Standard output envelope for all agents.
    Defined in GAOS-Agent-Spec.md §2.2.
    """

    task_id: str
    project_id: str
    agent_id: str  # Matches Agent.name
    status: Literal["success", "escalated", "failed"]
    result: dict[str, Any] = Field(default_factory=dict)  # Task-specific output
    cost_usd: float = 0.0  # Accumulated model cost for this task
    timestamp: datetime = Field(  # datetime — Pydantic-only; not written to BigQuery directly
        default_factory=lambda: datetime.now(UTC)
    )


# ── AgentWorkingMemory ────────────────────────────────────────────────────────────


class AgentWorkingMemory(TypedDict):
    """
    LangGraph state schema shared by all orchestrator agents (Tier 1 + Tier 2).
    Defined in GAOS-Memory-Spec.md §3; extended by NexusPrimeWorkingMemory
    in GAOS-Nexus-Prime-Spec.md §2.
    """

    # Core task context
    task_id: str  # Current task UUID
    project_id: str  # Active project namespace
    current_objective: str  # What the agent is doing right now

    # Sub-task tracking
    sub_task_results: list[dict]  # Collected outputs from Tier 3 sub-agents
    parked_proposals: list[str]  # Proposal IDs awaiting Approval Gate
    error_history: list[str]  # Error fingerprints seen this session

    # Memory layers (loaded once at boot — not refreshed mid-task)
    memory_context: dict  # Layer 4 semantic facts cached at boot
    episodic_cache: dict  # Layer 2 recent outcomes cached at boot
    observation_buffer: list[dict]  # Layer 3 candidate learnings this session

    # Cost and loop tracking
    cost_usd: float  # Running cost for this invocation
    iteration_count: int  # Evolution loop iteration (see §13.1)
    step_count: int  # LangGraph step counter
    tokens_used: int  # Token consumption this invocation

    # Event processing
    incoming_message: A2AMessage | None  # Current message being handled
    messages: list  # LangGraph message log

    # Internal timing (set at boot, used for elapsed-time logging)
    _started_at: float

    # Guard flags
    hard_stop_triggered: bool  # True if a hard stop constraint fired
    evolution_triggered: bool  # True if Write-Test-Refine loop is active


# ── MemoryEntry ───────────────────────────────────────────────────────────────────


class MemoryEntry(BaseModel):
    """
    A single approved knowledge entry in the Vertex AI Memory Bank.
    Defined in GAOS-Memory-Spec.md §6.
    """

    memory_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    agent_id: str  # Domain owner (e.g., "ledger", "beacon")
    knowledge_type: str  # "fact" | "pattern" | "rule" | "preference"
    domain: str  # Business domain (aligns with orchestrator names)
    content: str  # The knowledge, as a clear declarative statement
    evidence: list[str] = Field(default_factory=list)  # task_ids that supported approval
    confidence: float = 0.0
    approved_by: str = ""
    approved_at: datetime | None = None
    version: int = 1  # Starts at 1; increments on each approved update
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
    knowledge_id: str  # Links to Pending_Knowledge row
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


# ── MonologueFrame ────────────────────────────────────────────────────────────────


class MonologueFrame(BaseModel):
    """
    Structured pre-response reasoning record written by Nexus-Prime's ``think``
    node before every output-producing decision (diagnose, knowledge_review).
    Logged to BigQuery ``aos_logs.monologue_frames``.
    Defined in GAOS-Nexus-Prime-Spec.md §3.2.
    """

    task_id: str
    project_id: str
    knowledge_gap_detected: bool
    knowledge_gap_description: str
    partial_result_available: bool
    response_mode: Literal["Research", "Direct", "Reframe", "Tactical"]
    reasoning_summary: str
    timestamp: str = Field(  # str (not datetime) — model_dump() feeds insert_rows_json(); datetime is not JSON-serializable
        default_factory=lambda: datetime.now(UTC).isoformat()
    )


# ── PlaybookDoc ───────────────────────────────────────────────────────────────────


class PlaybookDoc(BaseModel):
    """
    Schema for a Playbook document written to Knowledge/playbooks/.
    Defined in GAOS-Memory-Spec.md §7.4 — Playbook Document Schema.
    Generated by Nexus-Prime (or a domain orchestrator) after a
    VISION_SUBMITTED event and committed to Drive post-approval.
    """

    title: str
    domain: str  # Aligns with orchestrator (e.g. "ledger")
    owner_agent: str  # Agent that authored the playbook
    project_id: str
    version: int = 1
    created_from_vision: str = ""  # task_id or Chat submission reference
    last_updated: str = (
        Field(  # str (not datetime) — written to Drive as JSON; datetime is not JSON-serializable
            default_factory=lambda: datetime.now(UTC).isoformat()
        )
    )
    approved_by: str = ""
    status: str = "draft"  # draft | active | archived
    tags: list[str] = Field(default_factory=list)
