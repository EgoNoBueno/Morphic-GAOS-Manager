# GAOS Memory Specification

**Morphic-G AOS** — Agent Memory Architecture, Self-Learning Loop, and Knowledge Management

> This document defines how agents in the Morphic-G AOS store, retrieve, learn, and evolve their knowledge. It covers all five memory layers, the two-phase knowledge approval model, confidence-based learning, and the structure of the procedural knowledge base (workflows, policies, procedures).
>
> **Prerequisites:** Agents must satisfy `GAOS-Agent-Spec.md` construction requirements. The Approval Gate mechanics referenced here are defined in `GAOS-Manager-Spec.md` §14. Memory Bank cost profile is in `GAOS-Manager-Spec.md` §9.4.

---

## 1. Design Principles

| Principle | Rule |
|-----------|------|
| **Human-approved knowledge** | No agent may promote a learning to long-term memory, update a workflow/policy/procedure, or modify its own guardrails without owner approval through the Approval Gate. |
| **Confidence threshold before proposal** | An observation must be corroborated enough times (confidence ≥ 0.70) before the agent proposes it as a knowledge update. Single observations enter the buffer only. |
| **Domain ownership** | Each Tier 2 orchestrator owns the knowledge in its domain. Cross-domain knowledge is owned by Nexus-Prime. Conflicts are resolved by the domain owner. |
| **`project_id` scoping** | Every memory read and write must include `project_id`. No agent accesses memory from a project it was not dispatched to. |
| **Batch reads, not per-call reads** | Memory Bank is pay-per-operation. Agents batch-load domain context at boot and cache it for the session. They do not query Memory Bank on every sub-task. |
| **Immutable history** | Memory entries are versioned, not deleted. Superseded entries are marked `active = FALSE`; their history is preserved in BigQuery for audit. |
| **Least-privilege writes** | Tier 3 sub-agents do not write to memory. They return observations in `AgentOutput`; the orchestrator decides whether to buffer them. |

---

## 2. Memory Architecture — Five Layers

```
┌─────────────────────────────────────────────────────────────────┐
│  Layer 1 — Working Memory        LangGraph StateGraph           │
│  (ephemeral, per-invocation, free)                              │
├─────────────────────────────────────────────────────────────────┤
│  Layer 2 — Episodic Memory       Cloud Logging + BigQuery       │
│  (recent task history, error fingerprints, 7–30 day TTL)       │
├─────────────────────────────────────────────────────────────────┤
│  Layer 3 — Observation Buffer    Google Sheets (Pending tab)    │
│  (candidate learnings below confidence threshold, 14 day TTL)  │
├─────────────────────────────────────────────────────────────────┤
│  Layer 4 — Semantic Memory       Vertex AI Memory Bank          │
│  (approved facts, patterns, rules — indefinite, versioned)     │
├─────────────────────────────────────────────────────────────────┤
│  Layer 5 — Procedural Knowledge  Google Drive (Markdown)        │
│  (approved workflows, policies, procedures — version-controlled)│
└─────────────────────────────────────────────────────────────────┘
```

| Layer | Store | Who writes | Who reads | TTL |
|-------|-------|-----------|-----------|-----|
| 1 — Working | LangGraph state | Agent (auto) | Same agent, same invocation | Invocation duration |
| 2 — Episodic | Cloud Logging / BigQuery | Agent (auto on task complete) | Any agent (batch query) | 7–30 days (see retention schedule) |
| 3 — Observation Buffer | `Pending_Knowledge` Sheet tab | Agent (proposes) | Nexus-Prime, Owner | 14 days — auto-expire if not actioned |
| 4 — Semantic | Vertex AI Memory Bank | Nexus-Prime (post-approval only) | All orchestrators (batch at boot) | Indefinite; versioned |
| 5 — Procedural | Google Drive (`Knowledge/`) | Nexus-Prime (post-approval only) | All agents (loaded as context) | Version-controlled; old versions archived |

---

## 3. Layer 1 — Working Memory (LangGraph State)

Working memory is the agent's in-flight scratchpad for a single Cloud Run invocation. It lives entirely in the LangGraph `StateGraph` and is never persisted between invocations.

### Schema

```python
from typing import TypedDict

class AgentWorkingMemory(TypedDict):
    task_id: str                    # Current task UUID
    project_id: str                 # Active project namespace
    current_objective: str          # What the agent is doing right now
    sub_task_results: list[dict]    # Collected outputs from Tier 3 sub-agents
    parked_proposals: list[str]     # Proposal IDs awaiting Approval Gate
    error_history: list[str]        # Error fingerprints seen this session
    memory_context: dict            # Semantic memory facts loaded at boot (cached)
    episodic_cache: dict            # Recent task outcomes keyed by task_type; loaded at boot
    observation_buffer: list[dict]  # Candidate learnings seen this session
    cost_usd: float                 # Running cost for this invocation
    iteration_count: int            # For evolution loops
```

### Rules

- `memory_context` is loaded **once at boot** from Layer 4 (Vertex AI Memory Bank) and cached for the entire invocation. It is **not refreshed mid-task** — stale-context risk is acceptable to avoid per-call Memory Bank costs.
- `observation_buffer` accumulates candidate learnings during the session. On invocation end, the agent calls `flush_observations()` which appends new observations to the `Pending_Knowledge` Sheet tab (de-duplicated by `content_hash`). Confidence incrementing and proposal triggering run as a separate nightly job, not inline during `flush_observations()`.
- Working memory is **lost on invocation end**. State that must survive restarts (parked proposals, open task IDs) is persisted to the `Agent_Approvals` Sheet and LangGraph's external checkpoint store before the invocation exits.

---

## 4. Layer 2 — Episodic Memory (Cloud Logging / BigQuery)

Episodic memory is the agent's autobiographical history — what tasks it ran, what errors it encountered, what outcomes resulted. It is written automatically as a side effect of every task, with no agent decision required.

### What is recorded

Every `AgentOutput` emits a Cloud Logging entry (see `GAOS-Manager-Spec.md` §13.2 for labels). The episodic store answers:

- *"Have I seen this error before?"* (no-progress detector uses this)
- *"What was the outcome last time I ran this task type?"*
- *"Which domain is generating the most evolution tasks?"*
- *"How much did last week's tasks cost?"*

### Episodic Query Pattern

Agents query episodic memory in two situations:

1. **At the start of a new task** — check for similar `task_type + agent_id` entries to surface relevant prior context before calling the LLM.
2. **Inside the evolution loop** — compare `error_fingerprint` to prior iterations (no-progress detection).

Queries are batch BigQuery reads, not Cloud Logging queries (BigQuery is cheaper at scale):

```python
# tools/memory.py
from google.cloud import bigquery
from config import get_settings

def query_episodic(agent_id: str, project_id: str,
                   task_type: str, limit: int = 5) -> list[dict]:
    """Return the N most recent outcomes for this agent + task_type."""
    settings = get_settings()
    gcp_project = settings.GCP_PROJECT_ID
    client = bigquery.Client(project=gcp_project)
    # Table reference uses .replace() — not an f-string — to avoid
    # accidental injection if gcp_project were ever user-supplied.
    sql = """
        SELECT task_id, status, result_summary, error_fingerprint,
               total_cost_usd, timestamp
        FROM `{gcp_project}.aos_logs.task_outcomes`
        WHERE agent_id = @agent_id
          AND task_type = @task_type
        ORDER BY timestamp DESC
        LIMIT @limit
    """.replace("{gcp_project}", gcp_project)
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("agent_id", "STRING", agent_id),
            bigquery.ScalarQueryParameter("task_type", "STRING", task_type),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
    )
    return [dict(row) for row in client.query(sql, job_config=job_config)]
```

---

## 5. Layer 3 — Observation Buffer (`Pending_Knowledge` Sheet Tab)

The Observation Buffer is the staging area for candidate learnings. An agent writes here when it notices something potentially worth remembering, but the item has not yet reached the confidence threshold needed for a formal proposal.

### `Pending_Knowledge` Sheet Schema

| Col | Field | Type | Description |
|-----|-------|------|-------------|
| A | `knowledge_id` | UUID | Unique identifier for this candidate entry |
| B | `content_hash` | string | SHA-256(agent_id + domain + content)[:16] — deduplication key used by `flush_observations()` |
| C | `agent_id` | string | Orchestrator that made the observation |
| D | `project_id` | string | Project namespace |
| E | `knowledge_type` | enum | `fact` / `pattern` / `rule` / `workflow_update` / `policy_update` / `procedure_update` / `guardrail_update` |
| F | `domain` | string | `accounting` / `marketing` / `sales` / `operations` / `admin` / `research` / `global` |
| G | `content` | string | The proposed knowledge in plain language |
| H | `evidence` | string | Comma-separated `task_id`s that support this observation |
| I | `confidence` | float | 0.0 – 1.0. Incremented each time a corroborating observation is added |
| J | `observation_count` | int | How many times this pattern has been independently observed |
| K | `status` | enum | `Buffered` / `Proposed` / `Approved` / `Rejected` / `Expired` / `Superseded` |
| L | `proposed_at` | datetime | When the entry was first buffered |
| M | `last_seen_at` | datetime | Timestamp of the most recent corroborating observation |
| N | `approved_by` | string | Email of owner who approved (blank until approved) |
| O | `approved_at` | datetime | Approval timestamp |
| P | `rejection_reason` | string | Owner's reason for rejection (blank if approved) |
| Q | `promoted_memory_id` | UUID | The Vertex AI Memory Bank ID this was promoted to (blank until approved) |

### Confidence Increment Rule

Each new corroborating observation (same `agent_id` + same `domain` + semantically matched `content`) applies this formula:

```
new_confidence = old_confidence + (1 - old_confidence) × 0.25
```

Starting at 0.0, observations accumulate:

| Observations | Confidence |
|---|---|
| 1 | 0.25 |
| 2 | 0.44 |
| 3 | 0.58 |
| 4 | 0.68 |
| **5** | **0.76** ← crosses 0.70 threshold → proposal triggered |

> **Threshold:** `confidence ≥ 0.70` triggers automatic proposal to the Approval Gate. Agents may not lower or override this threshold. The threshold exists to prevent noise from becoming policy.

### Auto-Expiry

Any entry with `status = Buffered` and `last_seen_at` older than **14 days** is auto-expired by the nightly archive job. Expired entries are moved to BigQuery for trend analysis (recurring expiry of the same pattern is a signal the agent's domain model is incomplete).

---

## 6. Layer 4 — Semantic Memory (Vertex AI Memory Bank)

Semantic memory is the long-term store of **approved, structured knowledge**. It is the only layer that agents actively consult when reasoning — everything above this layer is either ephemeral or staging.

### Memory Entry Schema

```python
from pydantic import BaseModel
from datetime import datetime

class MemoryEntry(BaseModel):
    memory_id: str               # UUID — stable across versions
    project_id: str
    agent_id: str                # Domain owner (e.g., "ledger", "beacon")
    knowledge_type: str          # "fact" | "pattern" | "rule" | "preference"
    domain: str                  # Business domain (aligns with orchestrator names)
    content: str                 # The knowledge, written as a clear declarative statement
    evidence: list[str]          # task_ids that supported approval
    confidence: float            # Final confidence score at approval time
    approved_by: str             # Owner email
    approved_at: datetime
    version: int                 # Starts at 1; increments on each approved update
    supersedes: str | None       # memory_id of the entry this replaces (None for v1)
    active: bool                 # False = deprecated/superseded — not loaded at boot
    tags: list[str]              # Free-form tags for retrieval (e.g., ["vendor", "payment", "net30"])
```

### Knowledge Types

| Type | Description | Example |
|------|-------------|---------|
| `fact` | A specific true statement about this business | `"Vendor: Acme Corp always invoices on the last Friday of the month."` |
| `pattern` | A recurring behavioural pattern with business impact | `"Leads from Google Ads convert 3× faster than Facebook Ads for this client."` |
| `rule` | A derived business rule the agent should apply | `"Any invoice over $500 requires a PO number before payment is authorised."` |
| `preference` | An operating preference expressed by the owner | `"Owner prefers email summaries in bullet-point format, not prose."` |

### Boot-Time Memory Load

At startup, each orchestrator calls `load_domain_memory()` to batch-fetch all active entries for its domain into `working_memory.memory_context`. This is the **only** Memory Bank read during a normal session.

> **Implementation note:** `tools/memory.py` uses `vertexai.preview.memory.MemoryBankClient`
> for agent-facing reads and writes. The BigQuery `memory_entries` table (provisioned in
> `GAOS-Deploy-Spec.md §7`) stores structured metadata that can be queried independently for
> audit, bulk analytics, and the archive pipeline. The two resources are complementary:
> - **`MemoryBankClient`** — used at boot (`load_domain_memory`) and on approval writes
>   (`write_approved_memory`); handles embedding and semantic indexing internally.
> - **BigQuery `memory_entries`** — used by the nightly archive job and for structured
>   queries (e.g., "how many active rules has Ledger accumulated?") without touching the
>   MemoryBank quota.

```python
# tools/memory.py
from config import get_settings
from vertexai.preview.memory import MemoryBankClient  # type: ignore[import-not-found]

# No module-level vertexai.init() — MemoryBankClient is initialised per-call
# using the project_id parameter so that multi-project deployments work correctly.


def load_domain_memory(agent_id: str, project_id: str) -> dict:
    """Batch-fetch all active memory entries for this agent's domain."""
    client = MemoryBankClient(project=project_id)
    entries = client.list(filters={
        "agent_id": agent_id,
        "active": True,
        "project_id": project_id,
    })
    context = {"fact": [], "pattern": [], "rule": [], "preference": []}
    for e in entries:
        context.setdefault(e.knowledge_type, []).append({
            "memory_id": e.memory_id,
            "content": e.content,
            "tags": e.tags,
        })
    return context


def query_memory_bank(query: str, corpus: str,
                      project_id: str, top_k: int = 5,
                      similarity_threshold: float = 0.80) -> list[dict]:
    """Semantic similarity search against a named corpus. Use mid-task for context enrichment.

    Args:
        query:   The search text (error fingerprint, knowledge content, etc.).
        corpus:  Corpus ID, e.g. ``"gaos-ledger"`` or ``"gaos-global"``.
    """
    client = MemoryBankClient(project=project_id)
    results = client.query(
        corpus=corpus,
        query=query,
        top_k=top_k,
    )
    return [
        {"memory_id": r.memory_id, "content": r.content,
         "similarity": r.similarity, "tags": getattr(r, "tags", [])}
        for r in results
        if r.similarity >= similarity_threshold
    ]


def write_approved_memory(entry: "MemoryEntry", project_id: str) -> str:
    """Write a newly approved memory entry. Called by Nexus-Prime only."""
    client = MemoryBankClient(project=project_id)
    if entry.supersedes:
        client.update(entry.supersedes, {"active": False})
    record = client.create(entry.model_dump())
    return record.memory_id
```

### Memory Conflict Resolution

If two orchestrators hold contradictory memories about the same stated fact (e.g., Ledger says "Vendor X payment terms are Net-30" and Pursuit says "Net-45"):

1. The **domain-owning agent** takes precedence (Ledger owns vendor payment terms).
2. The conflicting entry from the non-owner agent is automatically flagged with `active = FALSE` and a `MEMORY_CONFLICT` security event is logged.
3. Nexus-Prime publishes a Priority-3 `ALERT` so the owner can adjudicate.

---

## 7. Layer 5 — Procedural Knowledge (Google Drive)

Procedural knowledge is the human-readable operational documentation of the business — the *how* behind the *what*. It is stored as Markdown files in a structured Google Drive folder hierarchy and loaded as context by agents when relevant.

### Drive Folder Structure

```
Knowledge/                              ← Drive root folder (ID in Project Registry col E)
├── workflows/                          ← Step-by-step process documents
│   ├── ap_reconciliation.md
│   ├── lead_qualification.md
│   ├── order_fulfillment.md
│   ├── weekly_reporting.md
│   └── ...
├── policies/                           ← Business rules and constraints
│   ├── expense_approval_policy.md
│   ├── vendor_payment_terms.md
│   ├── data_retention_policy.md
│   ├── communications_policy.md
│   └── ...
├── procedures/                         ← How-to guides for specific sub-tasks
│   ├── invoice_matching.md
│   ├── lead_scoring_criteria.md
│   ├── inventory_reorder_trigger.md
│   └── ...
└── archive/                            ← Superseded versions (never deleted)
    ├── workflows/
    ├── policies/
    └── procedures/
```

### Document Metadata Header

Every file in the `Knowledge/` hierarchy must begin with a YAML front-matter block:

```yaml
---
title: "AP Reconciliation Workflow"
type: workflow                        # workflow | policy | procedure
domain: accounting                    # aligns with orchestrator domain
owner_agent: ledger
version: 3
last_updated: "2026-03-10T14:22:00Z"
approved_by: "owner@company.com"
supersedes_version: 2
status: active                        # active | archived
tags: [accounts-payable, reconciliation, monthly]
---
```

### Procedural Document Conventions

**Workflows** (`workflows/`) — describe a multi-step business process:
```markdown
## Workflow: AP Reconciliation

### Trigger
- Runs on the last business day of each month, or when Ledger detects > 5 unmatched invoices.

### Steps
1. Pull all open invoices from `Accounting` Sheet tab with status `Unpaid`.
2. Match each invoice against bank statement rows using `invoice_matching` procedure.
3. For unmatched invoices: flag with status `Review Required` and publish Priority-2 ALERT.
4. Generate monthly reconciliation summary and append to `Accounting` tab.
5. Archive reconciled rows to BigQuery (retention: 24 months).

### Success Condition
All invoices matched or flagged within 2 hours of trigger.

### Failure Escalation
If > 20% of invoices remain unmatched after 2 hours, escalate to Nexus-Prime (Priority-3).
```

**Policies** (`policies/`) — define rules an agent must enforce:
```markdown
## Policy: Expense Approval

### Rule
Any agent-proposed expenditure above $500 must be submitted as a
Priority-4 Approval Gate proposal before any commitment is made.

### Exceptions
- Recurring approved subscriptions in the `approved_subscriptions` tab: auto-approve.
- Emergency operational spend: Nexus-Prime may pre-approve up to $200; report retroactively.

### Enforcement
Ledger checks this policy before generating any payment instruction.
```

**Procedures** (`procedures/`) — describe how to perform a specific sub-task:
```markdown
## Procedure: Invoice Matching

### Input
`invoice_row`: dict with fields `vendor_name`, `amount`, `invoice_date`, `invoice_number`

### Steps
1. Query `bank_statement` tab for transactions within ±3 days of `invoice_date`.
2. Filter candidates where `abs(transaction_amount - invoice_amount) < 0.01`.
3. Score matches: +1 for vendor name match, +1 for amount exact, +1 for date within 1 day.
4. Accept match if score ≥ 2; flag for review if score = 1; reject if score = 0.

### Output
`match_status`: `matched` | `review_required` | `unmatched`
`matched_transaction_id`: string | None
```

---

## 8. Self-Learning Loop

This is the process by which an agent moves an observation from working memory through to an approved, permanent memory entry.

```
Agent notices pattern during task execution
    ↓
Does this already exist in working_memory.memory_context?
  YES → increment confidence on existing Pending_Knowledge entry
  NO  → create new Pending_Knowledge row with confidence = 0.25
    ↓
flush_observations() on invocation end
    ↓
confidence ≥ 0.70?
  NO  → entry stays in Pending_Knowledge (Buffered); re-evaluated next session
  YES → generate KnowledgeProposal + submit to Approval Gate
    ↓
Nexus-Prime enriches proposal with Memory Bank context
    ↓
Owner reviews in Agent_Approvals sheet
    ↓
APPROVED                          REJECTED
    ↓                                 ↓
Nexus-Prime calls                 Log rejection_reason
write_approved_memory()           Decrement confidence by 0.30
or updates Drive file             (agent recalibrates without zeroing)
    ↓
Update Pending_Knowledge
row → status = Approved
    ↓
Broadcast KNOWLEDGE_UPDATE
to all orchestrators via Pub/Sub
so they reload memory on next boot
```

### `flush_observations()` — End-of-Invocation Processing

```python
# tools/memory.py

def flush_observations(observations: list[dict], project_id: str) -> None:
    """
    Called once at end of every agent invocation.
    Appends new observations to the Pending_Knowledge Sheet tab.
    Entries whose content_hash already exists are silently skipped
    (deduplication). Does NOT call Memory Bank — only touches Sheets (free).

    Confidence incrementing and automatic proposal triggering are handled
    by the nightly archive job, not inline here.
    """
    if not observations:
        return

    from tools.google_sheets import batch_append_rows, find_row

    deduped = []
    for obs in observations:
        content_hash = obs.get("content_hash", "")
        if content_hash:
            existing = find_row("Pending_Knowledge", "content_hash", content_hash, project_id)
            if existing is not None:
                continue  # already buffered — skip
        deduped.append(obs)

    if deduped:
        batch_append_rows("Pending_Knowledge", deduped, project_id)
```

---

## 9. Knowledge Proposals — Approval Gate Integration

When confidence reaches threshold, the agent automatically generates a structured proposal and submits it to the standard Approval Gate. The proposal priority depends on what type of knowledge is being added or changed.

### Priority Mapping

| Knowledge Type | Approval Priority | Rationale |
|----------------|------------------|-----------|
| `fact` | 2 — Low | Operational detail; easily corrected if wrong |
| `pattern` | 2 — Low | Advisory; does not directly change agent behaviour |
| `rule` | 3 — Normal | Changes how agent makes decisions; moderate risk |
| `workflow_update` | 3 — Normal | Changes how work is done; reversible |
| `policy_update` | 4 — High | Changes business rules; significant impact |
| `procedure_update` | 3 — Normal | Changes sub-task execution; moderate risk |
| `guardrail_update` | 5 — Critical | Changes agent Do/Don't constraints; maximum risk |

### `KnowledgeProposal` Schema

```python
class KnowledgeProposal(BaseModel):
    proposal_id: str             # UUID — links to Agent_Approvals row
    knowledge_id: str            # Links to Pending_Knowledge row
    project_id: str
    agent_id: str
    knowledge_type: str          # from Pending_Knowledge
    domain: str
    priority: int                # Mapped per table above

    # For new memory entries
    proposed_content: str        # The new knowledge statement

    # For updates to existing entries — both are required for update proposals
    existing_memory_id: str | None    # memory_id being updated (None for new)
    existing_content: str | None      # Current content for diff display in Approval Gate

    # For procedural document updates
    drive_file_path: str | None       # e.g., "Knowledge/workflows/ap_reconciliation.md"
    proposed_diff: str | None         # Unified diff format of the proposed change

    evidence: list[str]          # task_ids supporting this knowledge
    confidence: float
    observation_count: int
    rationale: str               # Plain-language explanation of why this update is valuable
    approved_by: str | None      # Populated at approval time (email from Agent_Approvals col K)
```

### Approval Gate Column H for Knowledge Proposals

Column H (`Proposed Code`) in `Agent_Approvals` carries the full `KnowledgeProposal` JSON for knowledge proposals (not Python code). The code injection gates in `syncSkillsToVertex` skip non-code proposals (identified by `message_type = KNOWLEDGE_PROPOSAL`).

### What Nexus-Prime Does on Approval

```python
# nexus_prime/knowledge_handler.py

async def handle_knowledge_approval(proposal: KnowledgeProposal,
                                    project_id: str) -> None:
    if proposal.knowledge_type in ("fact", "pattern", "rule", "preference"):
        # Write to Vertex AI Memory Bank
        entry = MemoryEntry(
            memory_id=str(uuid4()),
            project_id=project_id,
            agent_id=proposal.agent_id,
            knowledge_type=proposal.knowledge_type,
            domain=proposal.domain,
            content=proposal.proposed_content,
            evidence=proposal.evidence,
            confidence=proposal.confidence,
            approved_by=proposal.approved_by,
            approved_at=datetime.utcnow(),
            version=1 if not proposal.existing_memory_id else _next_version(proposal.existing_memory_id),
            supersedes=proposal.existing_memory_id,
            active=True,
            tags=_extract_tags(proposal.proposed_content),
        )
        memory_id = write_approved_memory(entry, project_id)
        _update_pending_knowledge(proposal.knowledge_id, "Approved", memory_id)

    elif proposal.knowledge_type in ("workflow_update", "policy_update", "procedure_update"):
        # Apply diff to Google Drive Markdown file
        _apply_drive_update(
            project_id=project_id,
            file_path=proposal.drive_file_path,
            diff=proposal.proposed_diff,
            approved_by=proposal.approved_by,
        )
        _update_pending_knowledge(proposal.knowledge_id, "Approved", None)

    elif proposal.knowledge_type == "guardrail_update":
        # Update the agent's identity file in Docs/agents/
        _apply_identity_file_update(proposal)
        _update_pending_knowledge(proposal.knowledge_id, "Approved", None)

    # Broadcast KNOWLEDGE_UPDATE so all orchestrators know
    # to reload their memory_context on next boot
    publish_knowledge_update(project_id, proposal.domain)
```

---

## 10. Procedural Document Update Flow

When an orchestrator believes a workflow, policy, or procedure needs updating:

```
Agent notices a better approach during task execution
(e.g., a 3-step pattern that consistently outperforms the documented 6-step workflow)
    ↓
Adds observation to working_memory.observation_buffer
with knowledge_type = "workflow_update|policy_update|procedure_update"
    ↓
At invocation end, flush_observations() writes to Pending_Knowledge
    ↓
After threshold crossings, agent reads current Drive file content
and generates a unified diff using LOCAL_MODEL
    ↓
Constructs KnowledgeProposal with:
  - drive_file_path = "Knowledge/workflows/<filename>.md"
  - proposed_diff    = the unified diff
  - rationale        = plain-language justification
    ↓
Submits to Approval Gate (Priority per type table)
    ↓
Owner sees current content vs. proposed diff in Agent_Approvals col H
    ↓
APPROVED → Nexus-Prime applies diff to Drive file
           Archives previous version to Knowledge/archive/<type>/
           Updates YAML front-matter (version++, last_updated, approved_by)
REJECTED → Decrement confidence, log rejection_reason in Pending_Knowledge
```

### Version Archiving Rule

Before Nexus-Prime writes an approved update to a Drive file, it:

1. Copies the current file to `Knowledge/archive/<type>/<filename>_v<N>.md`
2. Applies the approved diff to the live file
3. Updates the YAML front-matter header (`version: N+1`, `last_updated`, `approved_by`, `supersedes_version: N`)

This means every previous version is available for audit. The archive folder is read-only to agents.

---

## 11. Guardrail Update Flow

Guardrail updates (`knowledge_type = guardrail_update`) modify an agent's Do/Don't identity file constraints. These are the highest-risk memory modifications in the system.

**Additional requirements beyond the standard approval flow:**

1. **Priority 5 mandatory** — no agent may submit a guardrail update at a lower priority.
2. **Nexus-Prime co-signature** — Nexus-Prime must add a note to the proposal row confirming it has reviewed the proposed change for conflict with system-level rules (Rules 1–5 in `GAOS-Manager-Spec.md` §1).
3. **Identity file diff visible in col H** — the exact lines being changed must be shown as a unified diff, not just a description.
4. **7-day cooling period** — after a guardrail update is approved, the change cannot be proposed again (in either direction) for 7 days. This prevents rapid oscillation.
5. **All active orchestrators re-broadcast** — Nexus-Prime broadcasts a `GUARDRAIL_UPDATE` message to all Tier 2 topics so they flush and reload their identity context on next boot.

---

## 12. Knowledge Retrieval Patterns

### Pattern 1 — Context-Augmented Task Start (primary pattern)

```python
# At the start of a task, before building the LLM prompt:
def build_task_context(agent_id: str, task_type: str,
                       working_memory: AgentWorkingMemory) -> str:
    """Assemble relevant memory into a context block for the prompt."""
    ctx = working_memory["memory_context"]

    # Filter to tags relevant to this task_type
    relevant_rules = [e for e in ctx["rule"]
                      if task_type in e.get("tags", [])]
    relevant_facts = [e for e in ctx["fact"]
                      if task_type in e.get("tags", [])]
    relevant_patterns = [e for e in ctx["pattern"]
                         if task_type in e.get("tags", [])]

    # Recent episodic context (BigQuery, batch-cached earlier)
    recent_outcomes = working_memory.get("episodic_cache", {}).get(task_type, [])

    return f"""
## Memory Context
### Rules in effect for {task_type}:
{_format_list(relevant_rules)}

### Known patterns:
{_format_list(relevant_patterns)}

### Relevant facts:
{_format_list(relevant_facts)}

### Recent outcomes (last 5 similar tasks):
{_format_list(recent_outcomes)}
"""
```

### Pattern 2 — Safety-Net Check (error handling)

When an agent catches an exception, it queries episodic memory for the same `error_fingerprint` before deciding how to respond:

```python
async def handle_error(error: Exception, task_id: str,
                       working_memory: AgentWorkingMemory) -> str:
    fingerprint = hashlib.md5(
        f"{type(error).__name__}:{str(error)[:100]}".encode()
    ).hexdigest()

    # Check episodic memory for prior occurrences
    prior = [e for e in working_memory["error_history"]
             if e == fingerprint]

    if len(prior) >= 2:
        # Seen this before — no-progress detector will fire; escalate
        return "escalate"
    else:
        # First or second encounter — try recovery
        working_memory["error_history"].append(fingerprint)
        return "retry"
```

### Pattern 3 — Workflow Lookup (procedure execution)

Before executing a complex multi-step process, the agent loads the relevant procedure document from the Drive context cache:

```python
def load_procedure(domain: str, procedure_name: str,
                   project_id: str) -> str:
    """Return procedure document content as a string."""
    path = f"Knowledge/procedures/{procedure_name}.md"
    return read_drive_file(path, project_id)
    # Loaded once per session; cached in working_memory
```

---

## 13. Memory Governance & Security

### Write Permissions Summary

| Actor | Layer 1 | Layer 2 | Layer 3 | Layer 4 | Layer 5 |
|-------|---------|---------|---------|---------|---------|
| Tier 3 sub-agent | ✗ | ✗ | ✗ | ✗ | ✗ |
| Tier 2 orchestrator | ✅ (own session) | ✅ (auto on task end) | ✅ (proposes via buffer) | ✗ | ✗ |
| Nexus-Prime | ✅ | ✅ | ✅ (manages all) | ✅ (post-approval only) | ✅ (post-approval only) |
| Owner (human) | ✗ (ephemeral) | ✗ (tamper-proof) | ✅ (approve/reject) | ✗ direct (via approval) | ✅ (direct edit always permitted) |

### Security Events

All of the following must be logged as `log_type: "memory_security"` Cloud Logging entries:

| Event | Trigger | Alert Priority |
|-------|---------|---------------|
| `MEMORY_CONFLICT` | Two active entries contradict each other | 3 |
| `KNOWLEDGE_PROPOSAL_REJECTED` | Owner rejects a proposal | 1 (informational) |
| `GUARDRAIL_UPDATE_PROPOSED` | Any agent proposes a guardrail change | 4 |
| `UNAUTHORIZED_MEMORY_WRITE` | Tier 3 agent or non-Nexus agent calls write_approved_memory | 5 (Critical) |
| `CONFIDENCE_MANIPULATION` | Observation count incremented more than once per task_id | 4 |
| `PENDING_KNOWLEDGE_EXPIRED_PATTERN` | Same content_hash has expired 3+ times without approval | 2 |

### Confidence Manipulation Prevention

An agent must not increment confidence for the same `task_id` more than once (padding evidence). `flush_observations()` deduplicates evidence by `task_id` before writing. Duplicate `task_id`s in the evidence column trigger a `CONFIDENCE_MANIPULATION` event.

---

## 14. Cost Profile

Memory operations have a direct cost impact via Vertex AI Memory Bank. The following rules keep memory costs within the §9.4 budget.

| Operation | Frequency | Cost | Rule |
|-----------|-----------|------|------|
| Memory Bank batch read at boot (`load_domain_memory`) | Once per agent invocation | ~$0.002–$0.005 per call (Vertex AI MemoryBank list op) | Cache in working_memory; do not re-query mid-session |
| Memory Bank semantic query (`query_memory_bank`) | Optional — mid-task only | ~$0.002–$0.005 per call | Only call when boot cache is insufficient for the task |
| Memory Bank write (new entry) | Only on approval | ~$0.005/write | ~5–10 writes/month expected within budget |
| Memory Bank write (supersede + new) | Only on approval of update | ~$0.010/cycle | Two operations: deactivate old, create new |
| BigQuery episodic query (`query_episodic`) | Once per task type per session | ~$0.00 (under free tier) | Batch by task type at boot; cache result |
| Drive file read (procedure load) | Once per procedure per session | $0.00 | Included in Google Workspace |
| Pending_Knowledge Sheet writes | Per `flush_observations()` call | $0.00 | Sheets API, free tier |

**Monthly memory cost estimate (Phase 1–4):** ~100 boot reads + ~20 semantic queries × ~$0.004 avg + ~10 writes × $0.005 = **~$0.55/month** — within the $0.90 budgeted in §9.4.

---

## 15. Data Retention

| Layer | Store | Retention | Archive |
|-------|-------|-----------|---------|
| Layer 1 — Working | LangGraph state | Invocation duration | Not persisted |
| Layer 2 — Episodic | Cloud Logging | 7 days (see spec §9.5) | BigQuery: 30 days post-deletion |
| Layer 3 — Observation Buffer | `Pending_Knowledge` Sheet | 14 days (Buffered); indefinite (Proposed/Approved) | BigQuery archive on expiry |
| Layer 4 — Semantic | Vertex AI Memory Bank | Indefinite (active entries) | Superseded entries: `active = FALSE`, retained for audit |
| Layer 5 — Procedural | Google Drive | Indefinite (current version) | Previous versions in `Knowledge/archive/` — never deleted |

---

## 16. Testing Requirements

### Memory Layer Tests

| # | Test | Pass Condition |
|---|------|----------------|
| M1 | Agent loads domain memory at boot; cache populated | `working_memory.memory_context` has > 0 entries (once seeded) |
| M2 | Same `task_id` appears twice in observation buffer; `flush_observations()` runs | Only one evidence entry in Pending_Knowledge; `CONFIDENCE_MANIPULATION` logged if second call detected |
| M3 | 5 corroborating observations for the same content_hash | Confidence reaches ≥ 0.70; `KnowledgeProposal` submitted to Approval Gate automatically |
| M4 | Entry stays Buffered for 15 days with no new observations | Auto-expired by archive job; status = `Expired`; BigQuery row written |
| M5 | Owner approves a `fact` proposal | Vertex AI Memory Bank entry created; `Pending_Knowledge` row → `Approved`; `KNOWLEDGE_UPDATE` Pub/Sub message published |
| M6 | Owner rejects a `policy_update` proposal | Confidence decremented by 0.30; rejection_reason logged; no Drive file modified |
| M7 | A guardrail_update is submitted at Priority 4 | System rejects it and logs error — Priority 5 is mandatory |
| M8 | Tier 3 sub-agent calls `write_approved_memory()` directly | `UNAUTHORIZED_MEMORY_WRITE` logged; write fails; Priority-5 alert published |
| M9 | Two active Memory Bank entries contradict each other | `MEMORY_CONFLICT` logged; non-owner entry marked `active = FALSE`; Priority-3 alert |
| M10 | Approved workflow_update proposal | Drive file updated; previous version archived in `Knowledge/archive/`; YAML version incremented |

---

## 17. Reference Index

| Topic | Location |
|-------|----------|
| Vertex AI Memory Bank overview | `GAOS-Manager-Spec.md` §12 |
| Memory Bank cost estimate | `GAOS-Manager-Spec.md` §9.4 |
| Data retention schedule | `GAOS-Manager-Spec.md` §9.5 |
| Approval Gate mechanics | `GAOS-Manager-Spec.md` §14 |
| RBAC (who can approve) | `GAOS-Manager-Spec.md` §15.3 |
| Agent boot sequence | `GAOS-Agent-Spec.md` §6 |
| Agent construction requirements | `GAOS-Agent-Spec.md` §2–4 |
| Self-evolution (code) loop | `GAOS-Manager-Spec.md` §13 |
| Evolution loop constraints | `GAOS-Manager-Spec.md` §13.1 |
| Project Registry (`project_id` scoping) | `GAOS-Manager-Spec.md` §2 |
| LangGraph state machine pattern | `GAOS-Agent-Spec.md` §3.2 |
| Model selection rules | `GAOS-Agent-Spec.md` §3.7 |
| Cloud Logging label standard | `GAOS-Manager-Spec.md` §13.2 |
