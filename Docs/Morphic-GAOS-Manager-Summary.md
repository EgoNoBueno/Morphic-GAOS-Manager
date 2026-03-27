# Morphic-G AOS — Project Summary

**What is this?**

Morphic-G AOS (Agent Operating System) is an intelligent workforce built entirely on Google's cloud ecosystem. Instead of one monolithic bot, it is a coordinated team of specialized AI agents that handle the day-to-day operations of a small business — accounting, marketing, sales, operations, admin, and research — running autonomously, learning from experience, and escalating to the human owner only when a decision genuinely requires one.

The system is designed to run at **minimal operating cost** by routing routine work to a free local AI model, reserving the paid cloud models for decisions that actually warrant them, and using Google's free-tier services wherever possible.

---

## Goals & Objectives

**Primary Goal:** Run all routine small-business operations (accounting, marketing, sales, ops, admin, research) autonomously through a coordinated team of specialized AI agents — escalating to the human owner *only* when a decision genuinely requires one.

**Cost Target:** Minimal operating expenses by routing routine work to a free local model (Ollama), using paid Gemini models only for genuine reasoning, and staying within Google's free-tier services everywhere else.

**Key Design Objectives:**

1. **Agent Hierarchy** — Nexus-Prime (GM) → 6 domain orchestrators (Ledger, Beacon, Pursuit, Foreman, Steward, Scout) → unlimited task agents. Owner sees only Tier 2 dashboards; Tier 3 is invisible unless something fails.

2. **No Custom UI** — The entire control plane is a Google Sheet: live agent status, approval queue, task logs, and business data. No terminal, no custom frontend.

3. **Human-in-the-loop Approval Gate** — Risky actions (code deploy, payments, outbound email) are parked until the owner clicks Approved/Rejected in the Sheet. The system continues other work while waiting.

4. **Gated Self-Evolution** — Agents can write new tools when they encounter gaps (5 iterations, 15-min TTL, $0.50 cap), but generated code passes two static analysis gates (pattern gate + import allowlist) and requires owner approval before deploying. No agent can deploy its own code unilaterally.

5. **Multi-Tenant** — One deployment manages multiple business projects; each gets isolated Sheet workbook, Drive folder, and Pub/Sub namespace via the Project Registry.

6. **Layered Memory** — Scratchpad → BigQuery (episodic) → Sheets staging buffer → Vertex AI Memory Bank (long-term). Agents propose learnings; Nexus-Prime promotes them to permanent memory only after owner approval.

**Current State (Phases 1–4 complete, Phase 5 complete):** All 7 orchestrators, the full tool layer (14 modules including `google_chat.py`, `vertex_search.py`, `google_docs.py`, and `google_search.py`), and a **496-test suite** are built and passing. Phases 1 through 4 are complete: all 7 Cloud Run services are deployed, the Approval Gate Chat-path E2E is validated (5 live approval proposals delivered via Google Chat), and the `think` node, vision hub, full approval loop, and two Phase 3 reactive routing nodes (`market_watchdog`, `roi_optimizer`) are live. Phase 4 is exiting — cost/security verification and the GAOS-Doctor checklist are the remaining tasks. Phase 5 — Grafana CEO dashboard — is complete: Grafana live on Cloud Run. Vertex Agent Engine remains future scope.

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
**Why it exists:** The majority of agent work is logging, formatting, and summarizing — tasks that a free local model handles perfectly. Reserving paid cloud models for genuine reasoning decisions reduces monthly costs by an order of magnitude.
**Resources required:** Ollama (local machine), Google Gemini API, `config/settings.yaml`.

#### A2A Communication Protocol
**What it is:** A standardized message envelope (`A2AMessage`) with typed fields — source agent, target agent, message type, priority, payload, project ID, correlation ID. All agent-to-agent communication goes through this envelope on Cloud Pub/Sub. The `MessageType` enum has 24 values covering the full operational surface: status updates, task routing, data exchange, alerts, escalation, approval flow (`APPROVAL_REQUEST` / `APPROVAL_RESULT`), knowledge promotion, self-evolution, project registry changes, system broadcasts, the Cloud Scheduler TTL sweep (`TTL_SWEEP`) and nightly archive (`NIGHTLY_ARCHIVE`), 8 Phase 2.5 conversation and intelligence types (`CHAT_MESSAGE`, `DAILY_SYNC`, `VISION_SUBMITTED`, `PLAN_REVIEW`, `COMMENT_RECEIVED`, `RESEARCH_MANDATE`, `SKILL_REQUEST`, `KNOWLEDGE_INJECTION`), and 2 Phase 3 reactive event types (`STOCK_INSUFFICIENT`, `DEAL_CLOSED`).
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
**What it is:** A four-tier decision hierarchy: Free First → Hybrid Second → Paid Only When Necessary → Never Pay for Idle. Every infrastructure choice is evaluated against this before committing — the result is low, predictable operating expenses.
**Why it exists:** Small business operators cannot justify high monthly bills for an AI system that mostly does routine logging. The architecture is explicitly designed to stay within Google's free tiers for everything except actual reasoning work.
**Resources required:** Cloud Run (scale-to-zero), Ollama (local), Cloud Scheduler, all free-tier Google services.

#### Governance & Security
**What it is:** A layered security architecture covering: secret management (Google Secret Manager), webhook authentication (HMAC-SHA256), approval access control (protected Sheet ranges + tier-based RBAC), code injection prevention (SHA-256 hash pinning + static analysis + import allowlist), and VPC sandboxing. **See `GAOS-Security-Policy.md`** for the canonical zero trust policy document that formalizes all of these controls.
**Why it exists:** The approval queue is the most sensitive surface in the system — an approved row deploys Python code to production. Every layer is designed to ensure that only legitimate, unmodified code, approved by the right person, ever reaches Vertex AI.
**Resources required:** Google Secret Manager, Google Apps Script (protected ranges, `onChange` trigger, `syncSkillsToVertex`), Vertex AI sandbox, Cloud Logging.

#### Development Roadmap (5 Phases)
**What it is:** A phased build plan. **Phase 1 is complete** — all 7 orchestrators, the `main.py` Cloud Run entry point, the core tool layer (`bigquery`, `webhook_sender`, `memory`, `project_registry`, `google_sheets`, `pubsub`, `secrets`), and a baseline test suite covering U1–U5 unit specs and S1–S4 static analysis gate.
**Phase 2.5 Steps 1–6 are complete (496 tests passing after Phase 3–4 additions):**
- Step 1: `tools/google_chat.py` + `POST /chat` (25 tests; commit `551f0ca`)
- Step 2: `handle_daily_sync()` + `POST /daily-sync` + `ChatConfig` (13 tests; commit `ed6140b`)
- Step 3: `tools/vertex_search.py` + Playbook schema + `write_playbook` node (22 tests; commit `d0f05b1`)
- Step 4: `tools/google_docs.py` + Blueprint Factory (29 tests; commit `d0f05b1`)
- Step 5: AppSheet Vision Hub + `VISION_SUBMITTED` handler + `doc-comment-poll` Scheduler job (30 tests; commit `a62c6cc`)
- Step 6: `tools/google_search.py` + Scout `_discover` recursive node + `KNOWLEDGE_INJECTION` protocol (24 tests; commit `7def85c`)

Step 7 (`ITERATE_PLAN` constraint compaction + `SKILL_REQUEST` approval flow) remains. Phase 2 (Ollama observability) and Phase 3 (think node, multimodal vision, full approval loop, Chat E2E) are **complete**. Phase 4 (production bootstrap, exit criteria validation, cost verification) is **complete** — exiting; cost/security verification and GAOS-Doctor checklist are the remaining tasks. Phase 5 (Grafana CEO dashboard) is **complete** — Grafana live on Cloud Run; Vertex Agent Engine is future scope. Three **Context Trio** files (`Docs/about-me.md`, `Docs/brand-voice.md`, `Docs/working-preferences.md`) were added and integrated into `_load_identity_file()` in `agents/__init__.py` — all 7 agents now automatically receive owner business context, brand voice, and operating rules appended to their system prompt at boot, with zero per-orchestrator changes required.
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

This document defines the public interface for fourteen tool modules. The design enforces consistent `project_id` scoping, batching, and error handling across the whole system.

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

#### `tools/memory.py`
**What it does:** Reads from and writes to Vertex AI Memory Bank for each agent's domain context. Agents batch-read at boot via `load_domain_memory()`, which applies a 32,000-character priority-ordered token budget guard (facts → preferences → patterns → rules) before returning context; `_truncated` and `_dropped_count` metadata keys are set if entries were dropped. `count_active_entries(agent_id, project_id)` returns the live count of active Memory Bank entries for an agent — used by `nightly_knowledge_promotion.py` to enforce per-agent entry caps before each write. Individual writes go through Nexus-Prime post-approval only.
**Why it exists:** Centralizes all long-term memory I/O so access patterns (boot budget guard, per-agent caps, approved writes only) are enforced consistently across every orchestrator.
**Resources required:** Vertex AI Memory Bank, `roles/aiplatform.user` IAM role.

#### `tools/bigquery.py`
**What it does:** Appends task-outcome rows, approval records, and cost summaries to BigQuery. Supports parameterized queries for episodic memory lookups.
**Why it exists:** BigQuery is the cold-storage backbone for audit logs and pattern detection. Centralizing all writes prevents schema drift across agents.
**Resources required:** BigQuery, `roles/bigquery.dataEditor` IAM role.

#### `tools/google_chat.py`
**What it does:** Posts messages and interactive approval cards to Google Chat spaces. Used by all orchestrators for status updates, the daily briefing, and approval-request cards.
**Why it exists:** Google Chat is the primary human-facing notification channel. A shared wrapper enforces consistent card format and error handling across agents.
**Added:** Phase 2.5 Step 1.
**Resources required:** Google Chat API, service account with Chat scope.

#### `tools/web_search.py`
**What it does:** Queries the DuckDuckGo Instant Answer API (no API key, no cost) and returns a plain-text snippet string. Used exclusively to inject current real-world context into `LOCAL_MODEL` (Ollama) prompts when `web_access=True` is passed to `_call_model()`. If the fetch fails for any reason, the original prompt is sent to Ollama unchanged — failure is always silent.
**Why it exists:** Gives the free local model access to current facts without spending any Gemini API budget. Gemini models already have live knowledge natively, so `web_access=True` is silently ignored for them.
**Resources required:** None — the DuckDuckGo public API requires no credentials.

#### `tools/vertex_search.py`
**What it does:** Queries the Vertex AI Search (Discovery Engine) corpus over the project's `Knowledge/` Drive folder. Returns semantically ranked procedural documents.
**Why it exists:** Gives agents fast semantic retrieval over their knowledge library without scanning Drive directly. The `write_playbook` node in all orchestrators indexes new playbooks here.
**Added:** Phase 2.5 Step 3.
**Resources required:** Vertex AI Search (Discovery Engine), `roles/discoveryengine.editor` IAM role.

#### `tools/google_docs.py`
**What it does:** Creates, reads, and appends content to Google Docs. Used exclusively by Nexus-Prime to generate and iterate on Blueprint Docs in the project Drive folder.
**Why it exists:** Blueprint Docs are the structured output of the vision workflow — they need to persist as editable Drive documents, not just Sheet rows.
**Added:** Phase 2.5 Step 4.
**Resources required:** Google Docs API, Google Drive API, service account with Docs + Drive scope.

#### `tools/google_search.py`
**What it does:** Calls the Google Custom Search JSON API v1. Exposes `search()` (single query) and `research_topic()` (multi-query with URL deduplication). Called exclusively from Scout's `_discover` node.
**Why it exists:** Enables Scout's recursive deep-research loop (`RESEARCH_MANDATE` → `_discover` → `KNOWLEDGE_INJECTION`) for gathering market intelligence with corroborated sourcing.
**Added:** Phase 2.5 Step 6.
**Key constraint:** Free tier is 100 queries/day; `max_queries_per_mandate` (default 15) caps each mandate at 15% of the daily quota.
**Resources required:** Google Custom Search API, `GOOGLE_SEARCH_API_KEY` and `GOOGLE_SEARCH_CX` in Secret Manager.

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
- Implements two **Phase 3 reactive routing nodes** (no LLM, no data mutation): **`market_watchdog`** — receives `STOCK_INSUFFICIENT` from Foreman and immediately publishes a priority-4 ALERT to Scout to find alternative sourcing; **`roi_optimizer`** — receives `DEAL_CLOSED` from Pursuit, computes gross margin `(revenue − cogs) / revenue`, and publishes a priority-3 `low_margin` ALERT to Beacon if margin falls below 20%.

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

### 9. `GAOS-Security-Policy.md` — Zero Trust Security Policy for Agentic Systems

*Organizational security policy governing all seven GAOS orchestrators, all tools, Apps Script components, and any generated or approved skills deployed to Vertex AI.*

**Derived from:** IBM/Anthropic guidance on architecting secure enterprise AI agents, mapped to the specific threat model and implementation of this system.

**Why a separate policy document:** Agentic systems require a distinct security posture from traditional software. Probabilistic outputs cannot be exhaustively reviewed at compile time; autonomous tool execution can chain calls the developer never anticipated; and a compromised agent amplifies attacks at machine speed. Zero Trust — never trust, always verify — is the correct mental model, applied to agents, their inputs, their tools, and their outputs.

**Coverage:** Non-human identity management (7 dedicated SAs, no shared credentials, no key files), principle of least privilege, prompt injection prevention (structural separation + provenance markers + injection signal detection), three static-analysis code gates (`validate_code_safety()` pattern gate + import allowlist + SHA-256 integrity pinning), human-in-the-loop Approval Gate, five-tier priority escalation ladder, data classification and retention policy, structured logging and reasoning trace requirements, network isolation (all 7 services `--no-allow-unauthenticated`), real-time alert thresholds (`HMAC_FAILURE`, `CODE_HASH_MISMATCH`, `APPROVAL_RBAC_BLOCK`, etc.), configuration drift detection, and incident response path (Priority-5 → Chat alert → SA revocation).

**Key gaps called out honestly:** Tool response provenance markers (§2.2) are currently a SHOULD for Phase 4, required for Phase 5. Access-pattern anomaly detection (§8.3) is currently manual.

---

### 10. `GAOS-Privacy-Spec.md` — Privacy & Data Sovereignty

*Frank assessment of what data the AOS stores and transmits, and a menu of mitigations with explicit effort/compliance labels.*

**Not a compliance certification** — an honest risk map. Each mitigation is labeled **Optional Enhancement**, **Recommended**, or **Required for Compliance** so the operator can choose the right level for their use case.

**Core distinction:** data *at rest* in GCP (owned by the operator's project, encrypted by default, never leaves GCP unless explicitly exported) versus data *processed* by external LLMs (sent to Google's inference infrastructure, subject to Google's data retention and use policies). These are different risks with different mitigations.

**Coverage:** encryption at rest and in transit, Vertex AI data residency options, VPC Service Controls for API boundary enforcement, minimizing LLM data exposure (prompt scrubbing, synthetic test data), the privacy trade-offs of the local Ollama model vs. Gemini API, and a section on what happens to data if the operator stops using the service.

---

### 11. `GAOS-Skill-Compliance-Spec.md` — External Skill Review Process

*Mandatory review checklist for any externally sourced Python module before it is integrated into the AOS environment.*

A "skill" in this context is any tool module (`tools/`), agent class, or supporting utility that was written outside this repository and is being considered for integration. This document defines what must be verified before that code is allowed to run inside the system.

**Relationship to the static analysis gate:** `GAOS-Manager-Spec.md §15.4` defines the automated AST-based gates that run at evolution time. This document covers the human-led review that happens *before* the automated gates — the questions a reviewer must answer that no static analysis tool can answer automatically (intent, data scope, behavior under failure).

**Review checklist covers:** module purpose and scope declaration; import inventory against the approved allowlist; absence of side effects at import time; behavior under partial failure; test coverage requirements; and the Approval Gate submission format used to promote a passing skill into the active tool layer.

---

### 12. Agent Identity Files (`Docs/agents/`)

*One Markdown file per domain orchestrator. This file is loaded verbatim as the agent's system prompt at the start of every session — it is who the agent thinks it is.*

Each file follows an eight-section template: Persona, Goal, Objectives, Resources, Specification, Guardrails, Escalation Rules, and Knowledge Sources. Below is a brief summary of each agent's role and what makes it distinct.

#### Ledger — Accounting Agent (`ledger.md`)
Tracks every financial transaction — invoices, expenses, payables, receivables, monthly P&L. It owns the `Accounting` Sheet tab and nothing else. It proposes payments but cannot execute them; every payment above $500 requires a Priority-4 approval.

*Key dependency:* Listens to Pursuit (deal-closed events trigger invoice creation) and Foreman (fulfillment-confirmed events close AR entries).

#### Beacon — Marketing Agent (`beacon.md`)
Monitors campaign performance across all channels, tracks ad spend against budget, and surfaces underperforming campaigns. It owns the Marketing-related Sheet tabs. Before committing any new spend, it must query Ledger for the available budget balance — it cannot authorize spending that Ledger has not confirmed is available. When Nexus-Prime's `roi_optimizer` detects a low-margin deal, it sends Beacon a `low_margin` ALERT; Beacon front-queues a `lead_source_roi_analysis` task to identify which lead source produced the low-margin customer.

*Key dependency:* Gets win/loss data from Pursuit for campaign ROI calculations. Gets market signals from Scout for targeting recommendations. Receives `low_margin` ALERTs from Nexus-Prime when deal margin falls below 20%.

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
Monitors competitors, tracks market trends, identifies sourcing alternatives, and detects supply disruptions. It owns the `Research Products` tab. It uses `FAST_MODEL` (not `LOCAL_MODEL`) because its work requires web-current knowledge. It routes market signals to Beacon and competitor alerts to Nexus-Prime — it does not author strategy itself.

Scout implements a recursive deep-research pipeline (Phase 2.5 Step 6): when it receives a `RESEARCH_MANDATE` Pub/Sub message, it routes to the `_discover` node, which generates LLM-driven search queries, calls `tools/google_search.py` up to `max_search_depth` levels (default 3, max 15 queries), and accumulates results. Findings corroborated across ≥ 5 independent sources (confidence ≥ 0.70) are held in the observation buffer and published as `KNOWLEDGE_INJECTION` events. If a `blueprint_doc_id` is present in the mandate payload, Scout appends a Section E Market Intelligence block directly to the Blueprint Doc.

*Key dependencies:* When Foreman detects a stockout, Nexus-Prime's `market_watchdog` node receives the `STOCK_INSUFFICIENT` event and immediately dispatches a priority-4 `stock_insufficient` ALERT to Scout — Scout finds alternative sourcing without waiting for a manual `RESEARCH_MANDATE`. Nexus-Prime also triggers proactive deep research by publishing `RESEARCH_MANDATE` — Scout replies with `KNOWLEDGE_INJECTION`.

---

### 13. Context Trio Files (`Docs/about-me.md`, `Docs/brand-voice.md`, `Docs/working-preferences.md`)

*Three owner-authored Markdown files that act as standing orders for every agent in the system — injected automatically into every agent's system prompt at boot via `_load_identity_file()`.*

AI agents are stateless by default. Every invocation is a blank slate unless context is deliberately injected. The Context Trio solves this without per-orchestrator wiring: `_load_identity_file()` in `agents/__init__.py` calls `_load_context_trio()` and appends the three files after the agent-specific identity text. All 7 orchestrators gain the context with zero code changes at the orchestrator level.

#### `about-me.md` — The Compass
**What it does:** Defines the owner's professional identity, core mission, business goals (table), and KPIs. Includes a "NOTE TO USER" stub prompting the owner to fill in niche-specific details.
**Why it exists:** Without it, agents optimize for a generic business — wrong industry assumptions, wrong success metrics, wrong risk tolerance. Every suggestion is filtered through *"does this solve a specific pain point?"* and *"does this deliver measurable value?"* only when this file is populated.

#### `brand-voice.md` — The Persona
**What it does:** Defines the **Transparent Champion** persona — a composite of Slack (human/plainspoken), Oatly (radically honest), and Nike (action-oriented). Includes a vocabulary table (use this, not that), a list of what to avoid, and agent instructions.
**Why it exists:** Without it, all agent-generated content — sales sequences, status updates, chat replies — defaults to corporate jargon. The three reference brands give agents a concrete, blend-able target rather than a vague tone descriptor.

#### `working-preferences.md` — The Constitution
**What it does:** Defines operational rules of engagement: architectural philosophy (modular, minimum complexity), economic discipline (low expenses standard, math-backed recommendations), autonomy and control policies, communication style (5 words not 10), and a Workflow Policies table (Search Before Build, Automate by Default, Atomic Execution, etc.).
**Why it exists:** Prevents agents from suggesting expensive, monolithic, or manual solutions. Overrides generic AI assistant defaults with the specific system and cost philosophy of this deployment.

**Integration point:** `_load_context_trio()` reads all three files gracefully — if any file is absent (e.g., in a stripped container or unit test environment), it skips that file without error. The agent falls back to the identity-only prompt.

---

## Document Index

| File | Purpose | Length |
|------|---------|--------|
| `Docs/GAOS-Manager-Spec.md` | Master system specification — architecture, security, deployment, roadmap | ~1,800 lines |
| `Docs/GAOS-Deploy-Spec.md` | Infrastructure provisioning & first-run guide — GCP, Sheets, Apps Script, Cloud Run | ~902 lines |
| `Docs/GAOS-Nexus-Prime-Spec.md` | Engineering construction requirements for the Tier 1 root orchestrator | ~834 lines |
| `Docs/GAOS-Onboarding-Spec.md` | Deployer first-run guide + end-user onboarding via Steward | ~638 lines |
| `Docs/GAOS-Memory-Spec.md` | Full memory architecture, five layers, self-learning loop | ~706 lines |
| `Docs/GAOS-Agent-Spec.md` | Engineering construction requirements for every agent tier | ~317 lines |
| `Docs/GAOS-Skill-Compliance-Spec.md` | External skill review process before AOS integration | ~298 lines |
| `Docs/GAOS-Tools-Spec.md` | Shared tool module API reference (`tools/` directory — 14 modules) | ~893 lines |
| `Docs/GAOS-Persona-Spec.md` | AOS soul ("The Strategic Architect"), `think` node spec, tone standard | ~297 lines |
| `Docs/GAOS-Security-Policy.md` | Zero trust security policy — identity, code gates, prompt injection, threat detection, incident response | ~400 lines |
| `Docs/GAOS-Privacy-Spec.md` | Cloud data exposure, privacy risk analysis, and mitigation strategies | ~260 lines |
| `Docs/GAOS-Project-Glossary.md` | Canonical glossary of all abbreviations and technical terms | ~157 lines |
| `Docs/GAOS-Doctor.md` | Health-check runbook for diagnosing deployment and runtime issues | ~55 lines |
| `.github/copilot-instructions.md` | Coding rules enforced during AI-assisted development sessions | ~452 lines |
| `Docs/about-me.md` | Context Trio — owner business context, priorities, and KPIs (The Compass) | ~67 lines |
| `Docs/brand-voice.md` | Context Trio — Transparent Champion brand voice standard (The Persona) | ~66 lines |
| `Docs/working-preferences.md` | Context Trio — operational rules of engagement, the Low Expenses Standard, workflow policies (The Constitution) | ~69 lines |
| `Docs/agents/nexus-prime.md` | Identity file — Nexus-Prime (Root Orchestrator / General Manager) | — |
| `Docs/agents/ledger.md` | Identity file — Ledger (Accounting Agent) | — |
| `Docs/agents/beacon.md` | Identity file — Beacon (Marketing Agent) | — |
| `Docs/agents/pursuit.md` | Identity file — Pursuit (Sales Agent) | — |
| `Docs/agents/foreman.md` | Identity file — Foreman (Operations Agent) | — |
| `Docs/agents/steward.md` | Identity file — Steward (Admin & HR Agent) | — |
| `Docs/agents/scout.md` | Identity file — Scout (Research Agent) | — |
| `main.py` | Cloud Run HTTP entry point — all 7 agents, selected by `AGENT_NAME` env var | — |
| `tests/` | 496-test suite — U1–U5 unit specs + S1–S4 static analysis + tool modules + Phases 2–3 + Phase 3 reactive routing + memory cap enforcement | — |

---

*This summary document is for orientation and onboarding. For authoritative implementation details, always refer to the linked specification documents.*
