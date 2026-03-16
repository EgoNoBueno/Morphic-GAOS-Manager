# Morphic-G AOS — Project Summary

**What is this?**

Morphic-G AOS (Agent Operating System) is an intelligent workforce built entirely on Google's cloud ecosystem. Instead of one monolithic bot, it is a coordinated team of specialized AI agents that handle the day-to-day operations of a small business — accounting, marketing, sales, operations, admin, and research — running autonomously, learning from experience, and escalating to the human owner only when a decision genuinely requires one.

The system is designed to run for roughly **$2.50 per month** in cloud costs by routing routine work to a free local AI model, reserving the paid cloud models for decisions that actually warrant them, and using Google's free-tier services wherever possible.

---

## Theory of Operation

Think of Morphic-G AOS like a well-run office with a clear chain of command.

**Nexus-Prime** is the general manager. It does not do department work itself — it watches over the whole business, routes jobs to the right team, and is the only entity that can authorize changes to how the system operates.

**Six domain orchestrators** (Ledger, Beacon, Pursuit, Foreman, Steward, Scout) are the department heads. Each owns a slice of the business and manages its own team of task agents underneath. They write status updates to a shared Google Sheet so the owner can see what every department is doing at a glance.

**Task agents** (Tier 3) are the worker bees. They do one thing — parse an invoice, score a lead, check a shipment status — and hand the result back to their orchestrator. They never talk to the owner directly.

**The human owner** interacts with the system through a Google Sheet and an approval queue. When an agent wants to do something risky — deploy new code, commit a payment, send an email — it writes a proposal to the `Agent_Approvals` tab and waits. The owner clicks Approved or Rejected, and the agent picks up where it left off. The owner never has to watch a terminal or read logs.

**Communication between agents** travels through Google Cloud Pub/Sub, not direct function calls. Each agent publishes events to its own topic; other agents subscribe to the topics they need. This means a restart, a crash, or a cold-start on Cloud Run never loses a message — Pub/Sub holds it until the subscriber is ready.

**Memory** is layered: fast scratchpad for the current task, recent history in BigQuery for pattern recognition, a staging buffer for candidate learnings, approved long-term facts in Vertex AI Memory Bank, and version-controlled procedure documents in Google Drive. Agents propose new learnings; Nexus-Prime promotes them to permanent memory only after the human owner approves.

**Self-improvement** is built in but gated. When an agent hits a task it cannot do, it writes and tests a Python solution (max 5 iterations, 15 min, $0.50 cost cap), then submits it for human review. Before the proposal ever reaches the Approval Queue, the code passes a two-gate AST-based static analysis: the **pattern gate** blocks dangerous call patterns (`os.system`, `subprocess.*`, `pickle.loads`, `eval`, `exec`) and the **import gate** validates every `import` and `from … import` against an explicit allowlist using exact module-boundary matching. No agent can deploy its own code unilaterally.

---

## Specification Files

### 1. `GAOS-Manager-Spec.md` — The Master Blueprint

*The authoritative source for how the entire system is designed, why every decision was made, and what the full implementation looks like.*

This is the largest document (over 1,690 lines). It defines the system from top to bottom. Every other spec file links back to it.

#### Agent Hierarchy
**What it is:** A three-tier structure — Nexus-Prime at the top, six domain orchestrators in the middle, unlimited task agents at the bottom.
**Why it exists:** Keeps human oversight focused. The owner only needs to watch the Tier 2 dashboard tabs. Tier 3 activity stays invisible unless something goes wrong.
**Resources required:** Google ADK (Agent Development Kit), Cloud Run, Cloud Pub/Sub.

#### Google Sheets Control Plane
**What it is:** A Google Sheet that serves as the operational dashboard — live agent status, the approval queue, task logs, and business unit data.
**Why it exists:** No custom UI to build or maintain. The owner already knows Google Sheets. Every "thought" an agent has is logged here in plain language.
**Resources required:** Google Sheets API (`gspread`), Google Apps Script (for triggers and the approval webhook).

#### Project Registry
**What it is:** A tab in the master Sheet workbook where each row defines one business project (a client account, a business unit, a separate operation). Each project gets its own Sheet workbook, Drive folder, and Pub/Sub topic namespace.
**Why it exists:** Makes the system multi-tenant. One deployment can manage multiple businesses or business units without their data ever mixing.
**Resources required:** Google Sheets, Google Drive, Cloud Pub/Sub.

#### Event-Driven Approval Gate
**What it is:** When an agent needs human sign-off, it writes a proposal row to the `Agent_Approvals` tab, publishes an **`APPROVAL_REQUEST`** message to Pub/Sub, and **parks the task** — continuing other work in the meantime. The moment the owner changes the Status cell, an Apps Script `onChange` trigger fires, delivering an **`APPROVAL_RESULT`** message via Pub/Sub. Nexus-Prime matches the correlation ID, unparks the task, and execution continues from where it stopped. Proposals that go unanswered are handled by a Cloud Scheduler job that fires a **`TTL_SWEEP`** message to Nexus-Prime once per hour; Nexus-Prime re-notifies the owner and auto-rejects proposals that have exceeded 2× their priority deadline.
**Why it exists:** A polling approach (the agent repeatedly checking the sheet) wastes API quota, blocks the agent's work queue, and loses its place if the agent restarts. The event-driven model is cheaper, faster, and more resilient.
**Resources required:** Cloud Pub/Sub, Google Apps Script (`onChange` trigger), Cloud Scheduler (`TTL_SWEEP` message).

#### Proposal Priority & TTL
**What it is:** Five priority levels (1 = routine, 5 = critical), each with its own response deadline. A Cloud Scheduler job sweeps hourly and re-notifies the owner when proposals go unanswered, then auto-rejects them at 2× their deadline.
**Why it exists:** Prevents the approval queue from silently filling up with stale proposals. Every proposal either gets a human decision or is cleanly closed.
**Resources required:** Cloud Scheduler, Cloud Pub/Sub.

#### Hybrid LLM Strategy
**What it is:** Three model tiers — `LOCAL_MODEL` (free, runs on your local machine via Ollama), `FAST_MODEL` (Gemini Flash, fast and cheap), `DEEP_MODEL` (Gemini Pro, for serious reasoning). All three are referenced by alias in `settings.yaml`; no version strings appear in code.
**Why it exists:** The majority of agent work is logging, formatting, and summarizing — tasks that a free local model handles perfectly. Reserving paid cloud models for genuine reasoning decisions cuts monthly costs from ~$50 to ~$2.50.
**Resources required:** Ollama (local machine), Google Gemini API, `config/settings.yaml`.

#### A2A Communication Protocol
**What it is:** A standardized message envelope (`A2AMessage`) with typed fields — source agent, target agent, message type, priority, payload, project ID, correlation ID. All agent-to-agent communication goes through this envelope on Cloud Pub/Sub. The `MessageType` enum has 14 values covering the full operational surface: status updates, task routing, data exchange, alerts, escalation, approval flow (`APPROVAL_REQUEST` / `APPROVAL_RESULT`), knowledge promotion, self-evolution, project registry changes, system broadcasts, and the Cloud Scheduler TTL sweep (`TTL_SWEEP`).
**Why it exists:** Standardization means any agent can be replaced or updated without changing the messaging layer. The `correlation_id` links related messages across a multi-step workflow for audit traceability.
**Resources required:** Cloud Pub/Sub, Pydantic.

#### Cross-Domain Workflow Policies (5 Policies)
**What it is:** Explicit rules for how agents collaborate across department boundaries. For example, Policy 1 (Lead-to-Revenue) defines the exact sequence: Beacon passes a qualified lead to Pursuit, Pursuit closes the deal and notifies Ledger, Ledger generates the invoice. No agent steps outside its lane.
**Why it exists:** Without explicit policies, agents produce contradictory states (e.g., Pursuit quotes a product Foreman has flagged as out of stock). The policies are governance decisions that can only be changed through the Approval Gate.
**Resources required:** Cloud Pub/Sub, Agent_Approvals Sheet tab.

#### Self-Evolution Protocol (Write-Test-Refine Loop)
**What it is:** When an agent encounters a task it has no tool for, it enters a Write-Test-Refine loop (max 5 iterations, 15-minute TTL, $0.50 cost cap, no-progress detector). Before the result is submitted to the Approval Gate it passes two code safety gates: a **pattern gate** (AST walk for blocked call patterns: `os.system`, `subprocess.*`, `pickle.loads`, `eval`, `exec`, etc.) and an **import gate** (every `import` and `from … import` is checked against the approved module allowlist using exact module-boundary matching, so `import requests` is blocked even though `re` is in the allowlist). Code that fails either gate never reaches the queue — the loop logs a hard stop. The submitted code is then SHA-256 pinned so post-submission edits to the Sheet are detectable at deploy time.
**Why it exists:** The alternative is a static system that needs a developer every time a new data source or API appears. Self-evolution keeps the system growing without constant manual intervention, while the constraints and approval gate ensure it never goes off the rails.
**Resources required:** `agents/__init__.py` (`validate_code_safety`, `_run_evolution_loop`), Cloud Pub/Sub, Agent_Approvals Sheet tab.

#### Data Retention & Archive Policy
**What it is:** A nightly Apps Script job that summarizes and archives aged rows from the Sheet to BigQuery, then deletes the raw rows. BigQuery tables have native TTL policies that automatically purge old data. Each data type has a defined "hot" (Sheet), "cold" (BigQuery), and "gone" lifecycle.
**Why it exists:** An unbounded Sheet fills up, slows down, and eventually costs money. Keeping only current state in the Sheet and historical analysis in BigQuery means both stay performant and within free-tier limits.
**Resources required:** Google Apps Script, BigQuery, Ollama (for weekly summarization before deletion).

#### Cost Strategy
**What it is:** A four-tier decision hierarchy: Free First → Hybrid Second → Paid Only When Necessary → Never Pay for Idle. Every infrastructure choice is evaluated against this before committing. Estimated monthly cost for a single operator during Phases 1–4 is ≈ $2.50.
**Why it exists:** Small business operators cannot justify $50–$200/month for an AI system that mostly does routine logging. The architecture is explicitly designed to stay within Google's free tiers for everything except actual reasoning work.
**Resources required:** Cloud Run (scale-to-zero), Ollama (local), Cloud Scheduler, all free-tier Google services.

#### Governance & Security
**What it is:** A layered security architecture covering: secret management (Google Secret Manager), webhook authentication (HMAC-SHA256), approval access control (protected Sheet ranges + tier-based RBAC), code injection prevention (SHA-256 hash pinning + static analysis + import allowlist), and VPC sandboxing.
**Why it exists:** The approval queue is the most sensitive surface in the system — an approved row deploys Python code to production. Every layer is designed to ensure that only legitimate, unmodified code, approved by the right person, ever reaches Vertex AI.
**Resources required:** Google Secret Manager, Google Apps Script (protected ranges, `onChange` trigger, `syncSkillsToVertex`), Vertex AI sandbox, Cloud Logging.

#### Development Roadmap (5 Phases)
**What it is:** A phased build plan. **Phase 1 is complete** — all 7 orchestrators, the `main.py` Cloud Run entry point, the full tool layer (`bigquery`, `webhook_sender`, `memory`, `project_registry`, `google_sheets`, `pubsub`, `secrets`), and a 151-test suite covering U1–U5 unit specs and S1–S4 static analysis gate. Phase 2 (Ollama observability), Phase 3 (Gemini + full approval loop), Phase 4 (full validation, exit criteria), Phase 5 (Grafana CEO dashboard, future).
**Why it exists:** Building everything at once is how you end up with a broken system that is impossible to debug. Each phase has explicit exit criteria that must all be true before moving to the next.
**Resources required:** Phases 1–4: Cloud Run, Cloud Pub/Sub, Sheets, Ollama, Gemini. Phase 5 (future): Grafana on Cloud Run, Vertex AI Agent Engine (optional upgrade).

---

### 2. `GAOS-Agent-Spec.md` — How to Build an Agent

*The engineering manual every developer reads before writing a single line of agent code.*

While the master spec defines what the system does, this document defines how each agent must be constructed. It is the counterpart to the master spec, not a summary of it.

#### Tier Obligations Table
**What it is:** A single table showing exactly which requirements apply to each tier. Nexus-Prime, orchestrators, and task agents have different obligations for LangGraph, Pub/Sub, Sheet access, Memory Bank, and more.
**Why it exists:** Without a clear table, developers accidentally over-engineer task agents (adding Pub/Sub wiring they don't need) or under-engineer orchestrators (skipping the approval gate integration).

#### Universal Requirements
**What it is:** Requirements that every agent at every tier must satisfy — ADK `Agent` class structure, typed Pydantic input/output schemas, `project_id` forwarded to every tool call, structured Cloud Logging labels, and cost tracking in every `AgentOutput`.
**Why it exists:** Consistency. An agent that drops `project_id` from a sub-call or returns untyped `dict` data breaks the system's ability to audit, route, and scope correctly.
**Resources required:** Google ADK, Pydantic, Cloud Logging.

#### LangGraph State Machine (Tier 2)
**What it is:** Each orchestrator must declare a LangGraph `StateGraph` with a minimum of seven named nodes: `plan`, `dispatch`, `collect`, `report`, `park`, `resume`, `escalate`. The `park` → `resume` cycle is the human-in-the-loop pattern for the approval gate.
**Why it exists:** LangGraph manages stateful, long-running workflows across Pub/Sub events and agent restarts. Without it, an orchestrator has no way to park a task and resume it correctly when an approval arrives minutes or hours later.
**Resources required:** LangGraph, Cloud Pub/Sub, LangGraph checkpoint store.

#### Agent Boot Sequence
**What it is:** A seven-step ordered startup procedure every orchestrator must follow: load identity file → load settings → load secrets → read Project Registry → connect Pub/Sub → write IDLE heartbeat → begin event loop. Any step failure causes a clean exit with a `STARTUP_FAILURE` log event.
**Why it exists:** A partially-started agent that skips secret loading or Project Registry validation will produce incorrect, potentially cross-project behavior. Fail-fast is safer than trying to operate in a degraded state.
**Resources required:** Google Secret Manager, Google Sheets (Project Registry tab), Cloud Pub/Sub, Cloud Logging.

#### Completion Checklists
**What it is:** A per-tier checklist of ~17 items for Tier 2 and ~10 items for Tier 3. An agent is not considered deployable until every item is checked.
**Why it exists:** Checklists prevent the classic "it works on my machine" deployment where half the requirements were implemented and half were skipped because the basic happy-path tests passed.

#### Testing Requirements
**What it is:** Five universal unit tests, six Tier 2 integration tests, and four static analysis tests. Each has a specific pass condition, not just "it runs without errors."
**Why it exists:** An agent can start, post a heartbeat, and appear healthy while still silently dropping `project_id`, hardcoding a model version, or failing to halt on the iteration cap. The specific tests catch these failure modes explicitly.
**Resources required:** pytest, mock assertions on tools, Vertex AI Code Execution sandbox (for static analysis tests).

---

### 3. `GAOS-Memory-Spec.md` — How Agents Learn and Remember

*The full architecture for agent memory — from what an agent knows right now to what the business has learned over years of operation.*

#### Five-Layer Memory Model
**What it is:** Memory is organized into five separate layers, each with a different purpose, cost, and lifetime.

| Layer | Where it lives | Lifetime |
|-------|---------------|----------|
| Working Memory | LangGraph state | One task invocation |
| Episodic Memory | Cloud Logging + BigQuery | 7–30 days |
| Observation Buffer | Google Sheets (Pending_Knowledge tab) | 14 days |
| Semantic Memory | Vertex AI Memory Bank | Indefinite, versioned |
| Procedural Knowledge | Google Drive (Markdown files) | Version-controlled |

**Why it exists:** Using a single store for everything is either too expensive (querying Memory Bank on every sub-task) or too lossy (keeping everything in working memory that disappears on restart). Each layer is matched to the access pattern it needs to serve.
**Resources required:** LangGraph, Cloud Logging, BigQuery, Google Sheets, Vertex AI Memory Bank, Google Drive.

#### Confidence-Gated Learning
**What it is:** An agent cannot propose a new learning until it has seen the same pattern corroborated enough times to push the confidence score above 0.70. Single observations go into the buffer only.
**Why it exists:** Prevents one-off anomalies from polluting long-term memory. A vendor payment being late once is not a pattern. Five times is.
**Resources required:** Google Sheets (Pending_Knowledge tab), Vertex AI Memory Bank.

#### Knowledge Approval Flow
**What it is:** When an observation reaches the confidence threshold, the agent automatically submits a `KnowledgeProposal` through the standard Approval Gate. Only after the owner approves does Nexus-Prime write the new fact to the Vertex AI Memory Bank or update the relevant procedure file in Google Drive.
**Why it exists:** Long-term memory is the most durable way to change agent behavior permanently. It must have the same human oversight as code changes — an agent should not be able to rewrite its own instructions without the owner knowing.
**Resources required:** Agent_Approvals Sheet tab, Cloud Pub/Sub, Vertex AI Memory Bank, Google Drive.

#### Procedural Knowledge (Workflows, Policies, Procedures)
**What it is:** Human-readable Markdown files stored in the `Knowledge/` folder on Google Drive. Agents load these as context (e.g., `Knowledge/workflows/ap_reconciliation.md`). When an update is needed, the agent proposes a diff; Nexus-Prime applies it post-approval and archives the old version.
**Why it exists:** Structured facts in the Memory Bank answer "what happened" questions. Procedures answer "how to do this" questions. The two stores are complementary — both are needed for a fully context-aware agent.
**Resources required:** Google Drive (`tools/drive.py`), Nexus-Prime service account (write-only), domain orchestrator service accounts (read-only).

---

### 4. `GAOS-Tools-Spec.md` — The Agent Toolbox

*The API reference for every shared function agents are allowed to call. No agent touches a Google SDK directly — it goes through these wrappers.*

This document defines the public interface for six tool modules. The design enforces consistent `project_id` scoping, batching, and error handling across the whole system.

#### `tools/secrets.py`
**What it does:** Fetches secrets from Google Secret Manager by name. The only module allowed to touch Secret Manager directly.
**Why it exists:** Centralizing secret access means any change to how secrets are loaded (rotation, new store, different auth) only touches one file.
**Resources required:** Google Secret Manager, `roles/secretmanager.secretAccessor` IAM role.

#### `tools/google_sheets.py`
**What it does:** All Sheet operations — appending rows, batch-writing multiple rows in one API call, reading ranges, finding rows by column value, and updating specific cells.
**Why it exists:** Every agent writes to the Sheet. Without a shared wrapper, each agent re-implements rate limit handling, credential management, and batching independently — and differently.
**Key rule:** Use `batch_append_rows()` for two or more rows. A batch write counts as one API request regardless of row count, keeping the system well within the 300 req/min quota.
**Resources required:** Google Sheets API, `gspread`, service account with Sheet access.

#### `tools/pubsub.py`
**What it does:** Publishes `A2AMessage` objects to Pub/Sub topics, creates topics idempotently on boot, and decodes incoming push-delivery envelopes.
**Why it exists:** Ensures every outbound message is validated against the `A2AMessage` schema before it hits the wire. An invalid message sent to Pub/Sub would be silently accepted by the topic but then fail at the receiving agent.
**Resources required:** Cloud Pub/Sub, Pydantic (`A2AMessage`).

#### `tools/drive.py`
**What it does:** Reads and writes Markdown files in the project's `Knowledge/` Drive folder. Write access is restricted to Nexus-Prime's service account only.
**Why it exists:** Gives agents access to their procedure library without direct SDK calls. The access control boundary is enforced at the tool level — calling `write_file()` from a domain orchestrator raises `DrivePermissionError`.
**Resources required:** Google Drive API, `roles/drive.file` IAM scope (Nexus-Prime write, orchestrators read).

#### `tools/webhook_sender.py`
**What it does:** Signs a proposal payload with HMAC-SHA256 and POSTs it to the Apps Script `doPost` webhook. Computes `code_sha256` of any attached code before signing, so the hash is protected within the HMAC envelope.
**Why it exists:** The webhook endpoint is public-facing. Every call must be signed so the Apps Script can verify it came from a legitimate agent and was not tampered with in transit.
**Resources required:** Google Secret Manager (`WEBHOOK_HMAC_SECRET`, `WEBHOOK_URL`), Apps Script Web App endpoint.

#### `tools/project_registry.py`
**What it does:** Loads the Project Registry tab, validates project IDs, and distinguishes between active, paused, and archived projects.
**Why it exists:** Every agent must validate the `project_id` it receives before doing any work. Centralizing this across all agents means the validation logic only needs to be correct once.
**Resources required:** Google Sheets (Project Registry tab).

---

### 5. `GAOS-Deploy-Spec.md` — Infrastructure Provisioning Guide

*Step-by-step instructions for standing up every GCP and Google Workspace resource the system requires.*

This is the operational counterpart to the master blueprint. Where `GAOS-Manager-Spec.md` defines what the system does, this document explains how to build the environment it runs in. Each section ends with a verification step — the guide explicitly says not to proceed until the previous step passes.

**Coverage:** GCP project configuration, service account creation, IAM assignments, Secret Manager setup, Cloud Pub/Sub topic and subscription provisioning, Cloud Run service deployment, Apps Script deployment and `onChange` trigger wiring, BigQuery dataset creation, and Cloud Scheduler job setup. Includes a Phase 1 pre-deployment checklist and the full HMAC test matrix (8 tests) for webhook security validation.

**Resources required:** Every GCP service in the system; Google Cloud CLI (`gcloud`); GitHub CLI for CI/CD setup.

---

### 6. `GAOS-Nexus-Prime-Spec.md` — Nexus-Prime Construction Specification

*Engineering construction requirements for the Tier 1 root orchestrator — the general manager of the entire AOS.*

This is the construction counterpart to the behavioral description in `GAOS-Manager-Spec.md §1`. It defines everything specific to Nexus-Prime that is not covered by the universal requirements in `GAOS-Agent-Spec.md`.

**Key extensions beyond the standard agent spec:**
- Subscribes to **all 7 domain topics** plus `agent.approvals.events` — the only agent with full-system visibility.
- Owns the complete **Approval Gate lifecycle**: receives `APPROVAL_REQUEST` messages, writes proposal rows to the `Agent_Approvals` tab, matches `APPROVAL_RESULT` callbacks by correlation ID, and unparks the waiting orchestrator.
- Handles **project namespace initialization**: when a new row appears in the Project Registry with status `Active`, Nexus-Prime creates the Sheet tabs, Drive folder, and Pub/Sub topics for the new project.
- Owns the `/sync` endpoint on the Cloud Run service — the Apps Script `syncSkillsToVertex` calls this endpoint to promote an approved skill to Vertex AI after the owner's approval.
- Defines the **cross-domain conflict resolution** policy: when two orchestrators publish contradictory states (e.g., Pursuit quotes a product Foreman has suspended), Nexus-Prime is the arbitrator.

**Resources required:** Google ADK, LangGraph, all 8 Pub/Sub topics, Google Sheets (all tabs), Vertex AI Memory Bank (write access), Google Drive (write access via Nexus-Prime service account).

---

### 7. `GAOS-Onboarding-Spec.md` — Onboarding Guide

*Two-part guide: first-time deployer setup and ongoing end-user onboarding via Steward.*

**Part 1 — Deployer Onboarding (§1–§4):** A human-facing setup guide that wraps `GAOS-Deploy-Spec.md`. It walks a new operator through the complete service sign-up sequence (Google account, GCP project, GitHub, Gemini API, Vertex AI, Ollama), then hands off to an interactive onboarding script (`tools/onboarding.py`) that automates the `GAOS-Deploy-Spec.md` steps wherever possible and validates the results. Includes a readiness checklist the operator must clear before the system is considered live.

**Part 2 — End-User Onboarding (§5):** The operational workflow Steward runs when a new employee or stakeholder is added to a running AOS instance. Covers Sheet access provisioning, RBAC tier assignment (writing the new approver row to the `Authorized Approvers` tab), and the orientation message sequence Steward sends to the new user.

**Why it exists as a separate document:** The Deploy Spec is a low-level technical reference. This guide is written for the operator — it provides context, explains why each step matters, and handles the inevitable "what if this goes wrong" scenarios that a pure reference doc does not cover.

---

### 8. `GAOS-Persona-Spec.md` — The Strategic Architect

*Defines the AOS behavioral identity, internal monologue architecture, and tone standard that every agent inherits.*

Every agent in the hierarchy inherits the **Strategic Architect** soul when formulating any user-visible output. The persona is not cosmetic — it drives specific decision branches in the LangGraph state machine.

**Three behavioral archetypes:**
| Archetype | Source | Decision rule |
|-----------|--------|--------------|
| **The Huang Effect** | Relentless efficiency | If a faster or cleaner alternative exists (`efficiency_score < 0.60`), surface it before complying. Never silently execute a slow path. |
| **The Nadella Mindset** | "Learn-it-all" over "know-it-all" | When a knowledge gap or API failure is detected, announce research in progress and provide the best available partial result. Never say "I can't." |
| **The Nassetta Touch** | Anticipatory service | Look exactly two steps ahead. Every output should proactively surface the next likely need, formatted for the user's actual workflow tool. |

**The `think` node (§4):** A mandatory pre-response reasoning step inserted before any node that produces user-visible output. Uses `DEEP_MODEL` to classify the request into one of four response modes — **Direct**, **Reframe**, **Research**, or **Tactical** — before generating any reply. The monologue is stored in Working Memory and logged to BigQuery, making it auditable and improvable.

**Weekly Review Loop (§5):** A regularly scheduled Nexus-Prime task that reads from the Observation Buffer, surfaces recurring patterns in agent behavior, and proposes system-level improvements through the standard Approval Gate.

---

### 9. `GAOS-Privacy-Spec.md` — Privacy & Data Sovereignty

*Frank assessment of what data the AOS stores and transmits, and a menu of mitigations with explicit effort/compliance labels.*

**Not a compliance certification** — an honest risk map. Each mitigation is labeled **Optional Enhancement**, **Recommended**, or **Required for Compliance** so the operator can choose the right level for their use case.

**Core distinction:** data *at rest* in GCP (owned by the operator's project, encrypted by default, never leaves GCP unless explicitly exported) versus data *processed* by external LLMs (sent to Google's inference infrastructure, subject to Google's data retention and use policies). These are different risks with different mitigations.

**Coverage:** encryption at rest and in transit, Vertex AI data residency options, VPC Service Controls for API boundary enforcement, minimizing LLM data exposure (prompt scrubbing, synthetic test data), the privacy trade-offs of the local Ollama model vs. Gemini API, and a section on what happens to data if the operator stops using the service.

---

### 10. `GAOS-Skill-Compliance-Spec.md` — External Skill Review Process

*Mandatory review checklist for any externally sourced Python module before it is integrated into the AOS environment.*

A "skill" in this context is any tool module (`tools/`), agent class, or supporting utility that was written outside this repository and is being considered for integration. This document defines what must be verified before that code is allowed to run inside the system.

**Relationship to the static analysis gate:** `GAOS-Manager-Spec.md §15.4` defines the automated AST-based gates that run at evolution time. This document covers the human-led review that happens *before* the automated gates — the questions a reviewer must answer that no static analysis tool can answer automatically (intent, data scope, behavior under failure).

**Review checklist covers:** module purpose and scope declaration; import inventory against the approved allowlist; absence of side effects at import time; behavior under partial failure; test coverage requirements; and the Approval Gate submission format used to promote a passing skill into the active tool layer.

---

### 11. Agent Identity Files (`Docs/agents/`)

*One Markdown file per domain orchestrator. This file is loaded verbatim as the agent's system prompt at the start of every session — it is who the agent thinks it is.*

Each file follows an eight-section template: Persona, Goal, Objectives, Resources, Specification, Guardrails, Escalation Rules, and Knowledge Sources. Below is a brief summary of each agent's role and what makes it distinct.

#### Ledger — Accounting Agent (`ledger.md`)
Tracks every financial transaction — invoices, expenses, payables, receivables, monthly P&L. It owns the `Accounting` Sheet tab and nothing else. It proposes payments but cannot execute them; every payment above $500 requires a Priority-4 approval.

*Key dependency:* Listens to Pursuit (deal-closed events trigger invoice creation) and Foreman (fulfillment-confirmed events close AR entries).

#### Beacon — Marketing Agent (`beacon.md`)
Monitors campaign performance across all channels, tracks ad spend against budget, and surfaces underperforming campaigns. It owns the Marketing-related Sheet tabs. Before committing any new spend, it must query Ledger for the available budget balance — it cannot authorize spending that Ledger has not confirmed is available.

*Key dependency:* Gets win/loss data from Pursuit for campaign ROI calculations. Gets market signals from Scout for targeting recommendations.

#### Pursuit — Sales Agent (`pursuit.md`)
Manages the sales pipeline from lead scoring through quote generation to deal close. It owns the `Sales by Product` tab. It drafts emails and quotes but cannot send anything — all outbound communications require an approval. It watches Foreman's stock alerts and must suspend quoting any product that drops below threshold.

*Key dependency:* Publishes deal-closed events that trigger both Ledger (invoice) and Foreman (fulfillment). Receives stock alerts from Foreman.

#### Foreman — Operations Agent (`foreman.md`)
Keeps inventory stocked and shipments moving. It owns the `Shipping and Receiving` tab. It proposes purchase orders but cannot commit to one without Priority-3 approval. The moment a stockout is detected, it immediately publishes an alert so Pursuit stops quoting the affected product.

*Key dependency:* Triggers Pursuit's stock suspension via the stock-insufficient alert. Receives deal-closed events from Pursuit to begin fulfillment.

#### Steward — Admin & HR Agent (`steward.md`)
Manages compliance deadlines, meeting scheduling, employee onboarding tracking, and document filing. It owns the `Logs` tab. It has write access to Google Drive but only for filing completed documents — it cannot author or modify policy files. Calendar invites require Priority-2 approval.

*Key notes:* A missed compliance deadline (today's date past the due date) triggers the highest possible alert — Priority-5, immediate.

#### Scout — Research Agent (`scout.md`)
Monitors competitors, tracks market trends, identifies sourcing alternatives, and detects supply disruptions. It owns the `Research Products` tab. It uses `FAST_MODEL` (not `LOCAL_MODEL`) because its work requires web-current knowledge via Vertex AI Search. It routes market signals to Beacon and competitor alerts to Nexus-Prime — it does not author strategy itself.

*Key dependency:* When Foreman detects a stockout, Scout is automatically tasked to find alternative sourcing options within 48 hours.

---

## Glossary of Terms

**A2A (Agent-to-Agent) Protocol**
The standardized communication layer between agents. All messages use the `A2AMessage` Pydantic schema and travel through Cloud Pub/Sub. No agent talks to another agent directly.

**Approval Gate**
The human review checkpoint in the system. When an agent wants to deploy code, make a payment, send a communication, or change system behavior, it writes a proposal to the `Agent_Approvals` Sheet tab and waits for the owner's approval before proceeding.

**BigQuery**
Google's cloud data warehouse. Used in GAOS as the cold storage layer for historical logs and task outcomes. Agents query it for episodic memory (past task outcomes) but write to it only through the nightly archive job.

**Cloud Run**
Google's serverless container runtime. Agents run on Cloud Run and scale to zero when idle — the system pays only for actual execution time, not idle time.

**Cloud Scheduler**
A cron-like service that triggers jobs on a schedule. Used in GAOS for the nightly archive job, the hourly TTL sweep of unanswered proposals, and other time-based agent wakes.

**Confidence Threshold**
The minimum confidence score (0.70 by default) an observation must reach before the system automatically submits it as a knowledge proposal. Confidence rises as multiple independent observations corroborate the same pattern.

**Correlation ID**
A UUID that links all Pub/Sub messages in a single multi-step workflow (e.g., the sequence of messages in a Lead-to-Revenue flow). Used for audit tracing.

**DEEP_MODEL**
The alias for the highest-capability paid cloud model (Gemini Pro or equivalent). Reserved for approval gate proposals, system re-architecture decisions, and multi-agent conflict resolution. Never used for routine tasks.

**Domain Orchestrator**
A Tier 2 agent that owns a specific business domain (accounting, marketing, sales, operations, admin, research). Each orchestrator has its own Sheet tab, Pub/Sub topic, and team of Tier 3 task agents.

**Episodic Memory**
The agent's autobiographical history — which tasks ran, what errors occurred, what the outcomes were. Stored automatically in Cloud Logging and queryable from BigQuery. Used for pattern detection and the no-progress detector.

**Evolution Task**
A Write-Test-Refine session where an agent discovers a capability gap and writes Python code in the Vertex AI sandbox to address it. The result (new skill) is always submitted through the Approval Gate before deployment.

**FAST_MODEL**
The alias for the speed-optimized paid cloud model (Gemini Flash or equivalent). Used for moderate reasoning, chat interactions, API response parsing, and as the fallback when the local model is unavailable.

**gspread**
The Python library used to interact with the Google Sheets API. All Sheet reads and writes in GAOS go through `tools/google_sheets.py`, which uses gspread under the hood.

**HMAC-SHA256**
A cryptographic signing algorithm. GAOS uses it to sign every webhook request from a Cloud agent to the Apps Script approval endpoint. The Apps Script verifies the signature before processing the request, preventing unauthorized injections.

**IAM (Identity and Access Management)**
Google Cloud's permission system. Each agent has its own service account with the minimum permissions it needs and no more. Nexus-Prime's service account cannot read the Vertex AI credentials; the Sheets service account cannot read Pub/Sub credentials.

**Identity File**
A Markdown file in `Docs/agents/` that defines an agent's persona, goal, objectives, resources, operational scope, guardrails, escalation rules, and knowledge sources. This file is loaded as the agent's system prompt at the start of every session — it is the agent's instructions for how to behave.

**Knowledge Proposal**
A structured submission to the Approval Gate asking Nexus-Prime to promote an agent observation (or a procedure update) to permanent memory. Nothing enters long-term memory without one.

**LangGraph**
The orchestration framework that manages stateful, multi-step agent workflows. Orchestrators declare a `StateGraph` with named nodes (plan, dispatch, collect, park, resume, etc.) and LangGraph manages transitions between them, including persisting state across Pub/Sub events.

**LOCAL_MODEL**
The alias for the free local model (Ollama with Llama or Mistral). Used for logging, formatting, summarization, data classification, and any task where cloud LLM quality is not required. Costs only electricity.

**Nexus-Prime**
The Tier 1 root agent — the general manager of the entire AOS. It does not do domain work itself. It oversees all orchestrators, handles cross-domain conflicts, manages knowledge approval, initializes new projects, and is the only agent that can authorize changes to how the system operates.

**No-Progress Detector**
A safety mechanism in the Write-Test-Refine loop. If the error fingerprint on iteration N is identical to iteration N-1, the loop stops immediately — further attempts will not help without human intervention.

**Observation Buffer**
The Tier 3 memory layer. Agent learnings that have not yet reached the confidence threshold are held here (in the `Pending_Knowledge` Sheet tab) for up to 14 days while the system waits for corroborating instances. If they expire without reaching threshold, they are discarded.

**Ollama**
An open-source tool that runs large language models locally on the host machine. GAOS uses it as `LOCAL_MODEL` for zero-cost tasks. It is configured as a Windows Service to auto-start on boot and restart on crash.

**`project_id`**
A short string slug (e.g., `acme-retail`) that identifies one business project namespace. Every agent action — Sheet write, Pub/Sub publish, Memory Bank read, Cloud Log entry — must include the `project_id` to ensure data from different projects never mixes.

**Project Registry**
A tab in the master Google Sheet workbook that lists every active AOS project. Nexus-Prime reads this on startup and monitors it for new entries. Adding a row with status `Active` automatically triggers a new project initialization.

**Protected Range**
A Google Sheets feature that prevents specific cells from being edited by anyone except the owner. Used on the `Status` column (col I), `Proposed Code` column (col H), and the `Code SHA-256` column (col M) in the `Agent_Approvals` tab — and on the entire `Authorized Approvers` tab.

**Pub/Sub (Cloud Pub/Sub)**
Google's managed message-passing service. Used as the nervous system of GAOS — all agent-to-agent communication, approval notifications, and system-wide broadcasts travel through Pub/Sub topics and subscriptions.

**RBAC (Role-Based Access Control)**
The permission system governing who may approve proposals. Each person in the `Authorized Approvers` Sheet tab has a tier (1–5). They may only approve proposals at or below their tier level. Attempts by unauthorized or insufficient-tier users to approve proposals are reverted and logged as security events.

**Semantic Memory**
The Tier 4 memory layer. Approved facts, patterns, and business rules that agents use as context for decision-making. Stored in Vertex AI Memory Bank. Written only by Nexus-Prime post-approval; read by all orchestrators at boot.

**Service Account**
A Google Identity account used by a software service (not a human). Each agent has its own service account with the minimum GCP permissions it needs. Service account JSON keys are stored in Google Secret Manager.

**Static Analysis**
A two-gate code review performed by `validate_code_safety()` in `agents/__init__.py` before any agent-written code reaches the Approval Queue. **Gate 1 (pattern gate):** walks the AST to block dangerous call patterns (`os.system`, `subprocess.*`, `pickle.loads`, `eval`, `exec`, `__import__`, etc.). **Gate 2 (import gate):** checks every `import` and `from … import` against the approved module allowlist using exact module-boundary matching (e.g. `import requests` is blocked even though `re` is allowlisted — a substring check would pass it). Code that fails either gate is hard-stopped before it reaches the queue. Separately, `syncSkillsToVertex` (Apps Script) performs a final hash-consistency check at deploy time.

**Steward**
The Admin & HR domain orchestrator. Manages compliance deadlines, meeting scheduling, onboarding tracking, and document filing.

**Task Agent (Tier 3)**
A stateless, single-purpose agent that performs one unit of work and returns a result to its orchestrator. It does not use LangGraph, does not publish to Pub/Sub, and does not interact with the human dashboard. It escalates by returning `status: "escalated"` in its output.

**TTL (Time-to-Live)**
The maximum time before something is considered stale and acted upon. Used for: unanswered approval proposals (each priority has a TTL), observation buffer entries (14 days), Cloud Logging entries (7 days), and BigQuery table partitions (varies by data type).

**Vertex AI Code Execution**
A sandboxed Python execution environment from Google. Used by agents during the Write-Test-Refine self-evolution loop to safely run and test code without risking the production environment. Network-isolated by Google.

**Vertex AI Memory Bank**
Google's managed vector memory service. Stores long-term semantic facts for agents. Agents batch-read their domain context from the Memory Bank at boot and cache it for the session — they do not query it per-task, as each operation has a cost.

**Working Memory**
The Tier 1 memory layer. The agent's in-flight scratchpad for a single Cloud Run invocation — current task ID, sub-task results, running cost, observation buffer, parked proposals. Lives in LangGraph state and is lost when the invocation ends.

**Write-Test-Refine Loop**
See *Evolution Task*.

---

## Document Index

| File | Purpose | Length |
|------|---------|--------|
| `Docs/GAOS-Manager-Spec.md` | Master system specification — architecture, security, deployment, roadmap | ~1,691 lines |
| `Docs/GAOS-Deploy-Spec.md` | Infrastructure provisioning & first-run guide — GCP, Sheets, Apps Script, Cloud Run | ~998 lines |
| `Docs/GAOS-Nexus-Prime-Spec.md` | Engineering construction requirements for the Tier 1 root orchestrator | ~945 lines |
| `Docs/GAOS-Onboarding-Spec.md` | Deployer first-run guide + end-user onboarding via Steward | ~827 lines |
| `Docs/GAOS-Memory-Spec.md` | Full memory architecture, five layers, self-learning loop | ~841 lines |
| `Docs/GAOS-Agent-Spec.md` | Engineering construction requirements for every agent tier | ~402 lines |
| `Docs/GAOS-Skill-Compliance-Spec.md` | External skill review process before AOS integration | ~419 lines |
| `Docs/GAOS-Tools-Spec.md` | Shared tool module API reference (`tools/` directory) | ~659 lines |
| `Docs/GAOS-Persona-Spec.md` | AOS soul ("The Strategic Architect"), `think` node spec, tone standard | ~360 lines |
| `Docs/GAOS-Privacy-Spec.md` | Cloud data exposure, privacy risk analysis, and mitigation strategies | ~367 lines |
| `Docs/agents/nexus-prime.md` | Identity file — Nexus-Prime (Root Orchestrator / General Manager) | — |
| `Docs/agents/ledger.md` | Identity file — Ledger (Accounting Agent) | — |
| `Docs/agents/beacon.md` | Identity file — Beacon (Marketing Agent) | — |
| `Docs/agents/pursuit.md` | Identity file — Pursuit (Sales Agent) | — |
| `Docs/agents/foreman.md` | Identity file — Foreman (Operations Agent) | — |
| `Docs/agents/steward.md` | Identity file — Steward (Admin & HR Agent) | — |
| `Docs/agents/scout.md` | Identity file — Scout (Research Agent) | — |
| `main.py` | Cloud Run HTTP entry point — all 7 agents, selected by `AGENT_NAME` env var | — |
| `tests/` | 151-test suite — U1–U5 unit specs + S1–S4 static analysis gate + tool modules | — |

---

*This summary document is for orientation and onboarding. For authoritative implementation details, always refer to the linked specification documents.*
