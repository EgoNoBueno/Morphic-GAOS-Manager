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
from typing import Any

from pydantic import BaseModel, Field


# ── Message type registry ──────────────────────────────────────────────────


class MessageType(str, Enum):
    STATUS_UPDATE = "STATUS_UPDATE"   # Routine heartbeat / objective update
    TASK_HANDOFF = "TASK_HANDOFF"     # Pass work to another orchestrator
    DATA_REQUEST = "DATA_REQUEST"     # Request a data payload (awaits DATA_RESPONSE)
    DATA_RESPONSE = "DATA_RESPONSE"   # Reply to a DATA_REQUEST
    ALERT = "ALERT"                   # Anomaly or error needing manager awareness
    ESCALATION = "ESCALATION"         # Requires human decision via Approval Gate
    BROADCAST = "BROADCAST"           # Nexus-Prime → All: system-wide directive


# ── A2AMessage ─────────────────────────────────────────────────────────────


class A2AMessage(BaseModel):
    """
    Standard envelope for all agent-to-agent Pub/Sub messages.
    Defined in GAOS-Manager-Spec.md §10.2.
    """

    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str                          # Must match a row in Project Registry
    source_agent: str                        # e.g. "beacon"
    target_agent: str                        # e.g. "pursuit" | "nexus-prime" | "broadcast"
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
