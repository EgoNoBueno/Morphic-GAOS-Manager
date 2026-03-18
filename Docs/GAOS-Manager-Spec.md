# Morphic-G AOS
## *An Adaptive, Self-Learning, Self Evolving Agent Operating System Built Using The Google Tool Stack*

**Morphic G AOS** is a dynamic, self-evolving agentic operating system built exclusively on the Google ecosystem to serve as the intelligent backbone of small business operations. By weaving together the power of Gemini, Vertex AI, and the entire Google Workspace suite, Morphic G deploys a fleet of autonomous agents that don't just execute tasks—they learn from every interaction and adapt to the specific needs of the business owner. Whether it's managing client communications in Gmail, optimizing inventory in Sheets, or drafting strategy in Docs, the system constantly refines its own workflows, transforming standard productivity tools into a proactive, self-improving workforce that grows alongside the enterprise.

---

## 1. AOS Manager

Oversees all components of the AOS. Considered a Root Agent. This manager doesn't do the grunt work; it oversees a hierarchy of specialized sub-agents, manages your business rules, and communicates directly with you through your Google Sheets dashboard.

### Manager Persona: "Nexus-Prime"

- **Goal:** Oversee all business operations and ensure agents are following the rules.
- **Rule 1:** If a worker agent fails, the Manager must diagnose the error.
- **Rule 2:** If a new skill is needed, the Manager must oversee the "Write-Test-Refine" loop in the Vertex AI Sandbox.
- **Rule 3:** Never deploy new code without sending a proposal to the `Agent_Approvals` sheet.
- **Rule 4:** All actions must be scoped to a `project_id`. On startup, read the **Project Registry** tab to load the list of active projects. Never mix data, messages, or logs between projects.
- **Rule 5:** If a new row appears in the Project Registry with status `Active`, initialize that project's namespace (Sheet workbook, Drive folder, Pub/Sub topic prefix) before dispatching any agents to it.

---

### 1.1 Agent Hierarchy

The AOS operates on a **3-tier model**. Human monitoring is focused at **Tier 1 and Tier 2** only. Tier 3 sub-agents execute silently under their orchestrator's supervision.

```
Tier 1 — Root Agent
└── Nexus-Prime (AOS Manager)
        ↓  delegates domain work  ↓
Tier 2 — Domain Orchestrators  [Human-Monitored via Dashboard]
├── Ledger       (Accounting Agent)
├── Beacon       (Marketing Agent)
├── Pursuit      (Sales Agent)
├── Foreman      (Operations Agent)
├── Steward      (Admin & HR Agent)
└── Scout        (Research Agent)
        ↓  spawns task runners  ↓
Tier 3 — Task Agents  [Supervised by Orchestrator Only]
    (Invoice parser, email drafter, ad reporter, etc.)
```

**Monitoring rule:** Each Tier 2 orchestrator posts its status, current objective, and any errors to the Main Control Plane sheet. Tier 3 activity is logged to the orchestrator's own Business Unit tab but does not surface to Nexus-Prime unless an error or approval is required.

---

### 1.2 Domain Orchestrator Definitions

#### Ledger — Accounting Agent
- **Responsibility:** Accounts receivable/payable, expense categorization, P&L summaries, invoice tracking.
- **Reports to:** Nexus-Prime
- **Dashboard tab:** Accounting
- **Example Tier 3 agents:** Invoice Parser, Expense Classifier, Cash Flow Reporter

#### Beacon — Marketing Agent
- **Responsibility:** Campaign planning, ad spend monitoring, content scheduling, marketing performance analysis.
- **Reports to:** Nexus-Prime
- **Dashboard tab:** Marketing (Sales by Product, Sales Graphs, Ad Response/Spend/Recommendations)
- **Example Tier 3 agents:** Ad Spend Monitor, Social Post Scheduler, Campaign Analyst

#### Pursuit — Sales Agent
- **Responsibility:** CRM updates, lead follow-up sequences, quote generation, pipeline reporting.
- **Reports to:** Nexus-Prime
- **Dashboard tab:** Marketing → Sales by Product
- **Example Tier 3 agents:** Lead Scorer, Follow-up Emailer, Quote Builder

#### Foreman — Operations Agent
- **Responsibility:** Order fulfillment, inventory levels, shipping coordination, vendor communications.
- **Reports to:** Nexus-Prime
- **Dashboard tab:** Shipping and Receiving
- **Example Tier 3 agents:** Inventory Monitor, Shipment Tracker, Vendor Notifier

#### Steward — Admin & HR Agent
- **Responsibility:** Calendar management, meeting scheduling, compliance reminders, employee onboarding tasks.
- **Reports to:** Nexus-Prime
- **Dashboard tab:** Logs
- **Example Tier 3 agents:** Calendar Coordinator, Deadline Reminder, Document Filer

#### Scout — Research Agent
- **Responsibility:** Market research, competitor monitoring, trend analysis, product sourcing recommendations.
- **Reports to:** Nexus-Prime
- **Dashboard tab:** Logs → Research Products
- **Example Tier 3 agents:** Web Scraper, Trend Summarizer, Competitor Price Monitor

#### Domain Orchestrator Summary

| Agent | Domain | Primary Responsibilities | Dashboard Tab |
|-------|--------|--------------------------|---------------|
| **Ledger** | Accounting & Finance | Accounts receivable/payable, expense categorization, P&L summaries, invoice tracking | Accounting |
| **Beacon** | Marketing | Campaign planning, ad spend monitoring, content scheduling, marketing performance analysis | Marketing (Sales by Product, Sales Graphs, Ad Response/Spend) |
| **Pursuit** | Sales | CRM updates, lead follow-up sequences, quote generation, pipeline reporting | Marketing → Sales by Product |
| **Foreman** | Operations | Order fulfillment, inventory levels, shipping coordination, vendor communications | Shipping and Receiving |
| **Steward** | Admin & HR | Calendar management, meeting scheduling, compliance reminders, employee onboarding | Logs |
| **Scout** | Research | Market research, competitor monitoring, trend analysis, product sourcing recommendations | Logs → Research Products |

---

### 1.3 Agent Construction Requirements

> **See [`Docs/GAOS-Agent-Spec.md`](GAOS-Agent-Spec.md) for the full agent construction specification.**

That document defines:
- Tier obligations table (what each tier must implement)
- Universal requirements (ADK class, Pydantic schemas, `project_id` scoping, logging, cost tracking)
- Tier 2 — Orchestrator requirements (identity file, LangGraph state machine, Pub/Sub wiring, dashboard heartbeat, Approval Gate integration, self-evolution capability, model selection rules)
- Tier 3 — Sub-agent requirements (stateless design, tool scope, model selection, `project_id` inheritance)
- Naming conventions
- Agent boot sequence
- Per-agent completion checklists (Tier 2 and Tier 3)
- Testing requirements (unit, integration, static analysis)
- Reference index back to relevant sections of this document

Every new agent — whether building a named Tier 2 orchestrator or a Tier 3 task runner — must satisfy all applicable requirements in `GAOS-Agent-Spec.md` before being added to the Implementation Checklist in Section 17.

---

## 2. The Manager's Dashboard (Google Sheets Control Plane)

The AOS Manager uses your Google Sheet as its "eyes and ears" for human interaction.

- **Observability:** The Manager logs every "thought" and "action" into a sheet so you can see exactly what it's doing 24/7.
- **The Approval Gate:** When the Manager wants to evolve the system, it adds a row to your sheet with the ID (e.g., `NEX-TEST-001`), the issue, and the proposed code.
- **The "Okay" Button:** The Manager is programmed to **stop and wait** until it sees the status change to "Approved" in the spreadsheet.

### Control Plane Sheet Headers

| Column | Description |
|--------|-------------|
| **Timestamp** | When the action was logged |
| **Agent Thought** | Where the Manager explains its logic (Observability) |
| **ID** | The unique task ID (e.g., `NEX-TEST-001`) |
| **Proposed Action/Code** | The code or change the Manager wants to make |
| **Status** | Approval Gate dropdown: `Pending`, `Approved`, `Rejected` |

### Dashboard Sections

- **Project Selector:** Dropdown to filter the entire dashboard by `project_id` — or view consolidated across all projects.
- **Main Control Plane:** Centralized view for all agent activities.
- **The Approval Gate:** Dedicated section for human-in-the-loop validation.
- **Observability Log:** Real-time feed of agent "thoughts" and logic.
- **Task Queue:** List of objectives with priority levels.
- **System Health Monitor:** Tracking API usage, token costs, and hardware performance.
- **Global State Key-Value Store:** For long-term variables and config settings.
- **Archive/Audit Trail:** Completed tasks and aged log rows are archived to BigQuery by the nightly job (see Section 9.5). Keeps the Sheet under ~10,000 rows permanently.
- **Manual Override Switch:** A master "pause" button for all operations.

> **Future — Phase 5 (Grafana Dashboard):** The Google Sheets control plane is the operational interface for Phases 1–4. In Phase 5 it will be supplemented by a **Grafana** web dashboard that provides a fully coded, version-controlled CEO-facing UI. Dashboard layout will be defined as JSON files in `dashboard/grafana/` and deployed programmatically. Sheets remain the agent data store; Grafana reads from them as a data source. See Phase 5 in Section 16.

### Business Unit Reporting Tabs

- Accounting
- Marketing
  - Sales by Product
  - Sales Graphs
  - Ad Response / Ad Spend / Marketing Recommendations
- Shipping and Receiving
- Website Metrics
- Logs
  - Error Logs
  - Research Products
  - Memory Repository Size

### Project Registry Tab

The master Sheet workbook contains a **Project Registry** tab. Nexus-Prime reads this on startup and monitors it for changes. Each row defines one project namespace.

| Column | Field | Description |
|--------|-------|-------------|
| A | `project_id` | Short unique slug, e.g., `acme-retail`, `greenleaf-mktg` |
| B | `project_name` | Human-readable name |
| C | `status` | `Active` / `Paused` / `Archived` |
| D | `sheet_workbook_id` | Google Sheets workbook ID for this project's data |
| E | `drive_folder_id` | Google Drive folder ID for this project's knowledge base |
| F | `budget_ceiling_usd` | Monthly LLM spend ceiling for this project |
| G | `owner_email` | Google account to notify on escalations |
| H | `created_date` | ISO date the project was registered |
| I | `notes` | Free-text context for Nexus-Prime |

**Adding a new project:** Add a row with status `Active`. Nexus-Prime detects the new entry via the `onChange` trigger, initializes the namespace, and begins dispatching agents.

**Pausing a project:** Change status to `Paused`. Nexus-Prime publishes a `BROADCAST` to all agents scoped to that `project_id` to suspend activity.

**Each project gets its own:**
- Google Sheets workbook (business unit tabs cloned from template)
- Google Drive folder (knowledge base, skills log, pending repairs)
- Pub/Sub topic namespace prefix: `<project_id>/agent/<agent_name>/events`

---

## 3. The Manager's "Brain" Abilities

### A. The "Logger" Ability

The Manager uses the **Google Sheets API** (`gspread`). Every time the LLM completes a "chain of thought," it calls a function to `append_row` to your spreadsheet.

### B. The "Approval" Loop

The approval mechanism is **event-driven**, not polling-based. The agent never blocks waiting for a cell value. It parks the proposal in its LangGraph state store, continues other work, and resumes only when notified via Pub/Sub.

#### Happy Path — Event-Driven Flow

1. **Post:** Agent writes the proposal row to `Agent_Approvals` sheet and publishes a `TASK_HANDOFF` to `agent/approvals/events` Pub/Sub topic, then **parks the task** in LangGraph state and moves on.
2. **Trigger:** An Apps Script `onChange` trigger fires the moment the Status cell changes.
3. **Notify:** Apps Script publishes a message to `agent/approvals/events` with the proposal ID and new status (`Approved` or `Rejected`).
4. **Resume:** The agent's Pub/Sub subscriber receives the push, looks up the parked task by proposal ID, and resumes execution.
5. **Execute or Halt:** Proceeds only if status strictly equals `"Approved"`. On `"Rejected"`, logs the outcome and closes the task.

#### Dead-Letter / No-Response — TTL Sweep

A **Cloud Scheduler job runs hourly** and scans for `Pending` rows older than their TTL. If no human has responded, the scheduler re-notifies and eventually auto-rejects. TTL is set by proposal priority:

| Priority | Proposal Type | TTL | On Expiry Action |
|----------|--------------|-----|-----------------|
| 5 — Critical | System re-architecture | 1 hour | Re-notify immediately; auto-reject at 2× TTL |
| 4 — High | Self-healing patch | 4 hours | Re-notify; escalate to Nexus-Prime |
| 3 — Normal | New skill proposal | 24 hours | Re-notify |
| 2 — Low | Config / routine change | 72 hours | Re-notify |
| 1 — Info | Routine log action | 7 days | Silent auto-reject |

**On 2× TTL with no response:** Scheduler marks the row `Stale`, publishes an `ESCALATION` to Nexus-Prime, and auto-rejects the proposal. The agent receives the rejection via Pub/Sub and closes the task cleanly.

#### Why Not Polling

- Polling consumes Sheets API quota (60 reads/min/user) continuously for every pending proposal
- A restarted agent loses its place in the polling loop; Pub/Sub retains messages for up to 7 days, so approval events survive restarts
- Polling blocks the agent's event queue; the parked-task model lets the agent remain productive while waiting
- No new services required — Apps Script `onChange`, Pub/Sub, and Cloud Scheduler are all already in the stack

---

## 4. API Limits & Cost Reference (2026)

### Speed Limits (API Quotas)

| Request Type | Limit (Per Project) | Limit (Per User) |
|-------------|---------------------|------------------|
| Read Requests | 300 per minute | 60 per minute |
| Write Requests | 300 per minute | 60 per minute |

> **Mitigation:** Use batching — collect multiple "thoughts" and write them in a single API call to avoid hitting the 60 writes/minute limit.

### Size Limits

- **10 Million Cells** max per spreadsheet (including all tabs).
- **Practical limit:** Sheets become sluggish past ~100,000 rows.
- **Cell capacity:** Up to 50,000 characters per cell.

### Cost Factor

- **Google Sheets API:** 100% free (no charge per read/write).
- **Real cost:** Input/Output tokens for the AI model (`DEEP_MODEL` or `FAST_MODEL` — see Section 11).
- **Apps Script hidden cost:** 90 min/day (free), 6 hours/day (Workspace).

---

## 5. Hybrid LLM Strategy: Gemini + Ollama

Route tasks based on "intelligence requirements":

| Feature | Paid API (Gemini) | Local LLM (Ollama) |
|---------|------------------|-------------------|
| **Cost** | ~$0.01–$0.10 per complex task | $0.00 (electricity only) |
| **Privacy** | Data sent to cloud | 100% Private (local) |
| **Speed** | Instant (Cloud GPU) | Dependent on RAM/GPU |
| **Best For** | Final Approvals / High Logic | Constant 24/7 Logging / Data Cleaning |

- **`DEEP_MODEL` (Paid — Gemini):** Complex reasoning, final system changes, Approval Gate proposals.
- **`FAST_MODEL` (Paid — Gemini):** Moderate reasoning, chat interface, routing, fast lookups.
- **`LOCAL_MODEL` (Free — Ollama):** Constant observability logging, log summarization, formatting data for Google Sheet.

### Ollama Availability & Fallback

Ollama runs on the local machine and is not guaranteed to be available 24/7 (machine sleep, process crash, restart). The system uses a **two-tier fallback** — no extra hardware required:

```
Step 1: Ping LOCAL_MODEL (Ollama) — 2 second timeout
    ↓ reachable → use LOCAL_MODEL (free)
    ↓ unreachable →
Step 2: Route to LOCAL_MODEL_FALLBACK (FAST_MODEL / Gemini Flash)
    ↓ log task with local_fallback=true
```

`FAST_MODEL` as fallback costs ~$0.001–$0.01 per logging/summarization task — negligible in practice since it only activates when the local machine is unavailable. Frequent `local_fallback=true` entries in the logs are the signal to invest in an always-on secondary device.

#### Ollama Process Reliability (Windows)

Ollama must be configured as a **Windows Service** so it auto-starts on boot and auto-restarts on crash — this eliminates the most common availability failure without any additional hardware:

```powershell
# Register Ollama as a Windows Service (run once, as Administrator)
nssm install OllamaService "C:\Users\<user>\AppData\Local\Programs\Ollama\ollama.exe" "serve"
nssm set OllamaService Start SERVICE_AUTO_START
nssm set OllamaService AppRestartDelay 3000
Start-Service OllamaService
```

### Recommended Local Models

- **16GB RAM:** Llama 3.1 8B
- **Dedicated GPU:** Mistral or Gemma 2

---

## 6. Recommended Project Structure

```
Morphic-GAOS-Manager/
├── agents/                       # One package per agent
│   ├── nexus_prime/
│   │   ├── __init__.py
│   │   └── orchestrator.py       # Root AOS Manager (Tier 1 — DEEP_MODEL)
│   ├── ledger/
│   │   ├── __init__.py
│   │   ├── orchestrator.py       # Accounting Agent (Tier 2)
│   │   └── tasks/                # Tier 3 task runners (one module per task)
│   ├── beacon/
│   │   ├── orchestrator.py       # Marketing Agent (Tier 2)
│   │   └── tasks/
│   ├── pursuit/
│   │   ├── orchestrator.py       # Sales Agent (Tier 2)
│   │   └── tasks/
│   ├── foreman/
│   │   ├── orchestrator.py       # Operations Agent (Tier 2)
│   │   └── tasks/
│   ├── steward/
│   │   ├── orchestrator.py       # Admin & HR Agent (Tier 2)
│   │   └── tasks/
│   └── scout/
│       ├── orchestrator.py       # Research Agent (Tier 2)
│       └── tasks/
├── tools/                        # Shared tool modules all agents import
│   ├── __init__.py
│   ├── google_sheets.py          # Read/Write logic for Dashboard
│   ├── project_registry.py       # Load/watch Project Registry tab
│   ├── pubsub.py                 # Publish A2A messages
│   ├── secrets.py                # get_secret() wrapper for Secret Manager
│   ├── bigquery.py               # Cold log writes and archive queries
│   ├── drive.py                  # Knowledge/ folder read/write
│   ├── memory.py                 # Vertex AI Memory Bank read/write
│   ├── webhook_sender.py         # HMAC-signed Approval Gate proposals
│   ├── google_chat.py            # Google Chat card + message sender (Phase 2.5 Step 1)
│   ├── web_search.py             # DuckDuckGo Instant Answer API — free, no key, Ollama context injection only
│   ├── vertex_search.py          # Vertex AI Search over Drive Knowledge/ corpus (Phase 2.5 Step 3)
│   ├── google_docs.py            # Google Docs create/append/comment — Blueprint Factory (Phase 2.5 Step 4)
│   └── google_search.py          # Google Custom Search API — Scout _discover node (Phase 2.5 Step 6)
├── models/                       # Pydantic schemas (A2AMessage, MemoryEntry, etc.)
├── config/
│   ├── settings.yaml             # Model aliases, GCP IDs, topic list (committed)
│   └── settings.yaml.template    # Blank starter template
├── Docs/
│   ├── agents/                   # Per-agent identity files (system prompt headers)
│   │   ├── nexus-prime.md
│   │   ├── ledger.md, beacon.md, pursuit.md …
│   ├── about-me.md               # Context Trio — owner business context (The Compass)
│   ├── brand-voice.md            # Context Trio — Transparent Champion brand voice (The Persona)
│   ├── working-preferences.md    # Context Trio — operational rules, cost ceiling (The Constitution)
│   └── *.md                      # Spec and architecture docs
├── apps_script/                  # Apps Script source (.gs files deployed via API)
│   ├── doPost.gs
│   ├── onChangeApproval.gs
│   ├── helpers.gs
│   ├── setup_protection.gs
│   └── syncSkillsToVertex.gs
├── scripts/                      # One-time setup and seeding scripts
│   ├── setup_workspace.py
│   ├── setup_apps_script.py
│   ├── _create_corpora.py
│   └── _seed_knowledge.py
├── tests/
├── main.py                       # FastAPI entry point — AGENT_NAME env var selects orchestrator
├── Dockerfile
└── pyproject.toml
```

---

## 7. Suggested Tech Stack (2026 Standard)

| Component | Technology |
|-----------|-----------|
| **Orchestration** | LangGraph (stateful loops + Human-in-the-loop) |
| **Package Manager** | `uv` (faster/cleaner than pip) |
| **Local LLM** | Ollama — Llama 3.1 or Mistral |
| **Cloud LLM** | `FAST_MODEL` (speed) / `DEEP_MODEL` (complex) — resolved from `settings.yaml` |
| **Sheets Integration** | `gspread` |
| **Data Validation** | Pydantic |
| **Agent Framework** | Google ADK (Agent Development Kit) |

---

## 8. Full AOS Component Stack

| Component | Google Service | Purpose |
|-----------|---------------|---------|
| **Logic/Reasoning** | `DEEP_MODEL` + Google ADK | Thinking and Planning |
| **Execution** | Vertex AI Sandbox | "Hands" that write/run code |
| **Messaging** | Cloud Pub/Sub | Nervous System for alerts |
| **Control Plane** | Google Sheets | Human Dashboard — current state only |
| **Long-term Archive** | BigQuery | Cold log storage, historical queries, Grafana data source |
| **Memory** | Vertex AI Memory Bank | Long-term recall of business rules |
| **Runtime (Phase 1–4)** | Cloud Run (event-driven, scale-to-zero) | Agents wake on Pub/Sub push; pay per invocation only |
| **Runtime (Phase 5+)** | Vertex AI Agent Engine *(future)* | Managed stateful runtime — adds ~$50–$135/month; deferred until justified |
| **Proactive Trigger** | Cloud Scheduler + Cloud Run | Timed/cron agent wake-up events |
| **Skills Library** | Agent Garden / Tools Hub | MCP connectors + pre-built tools |
| **Messaging Interface** | Google Chat Multi-Channel Hub | Human↔Agent + Agent↔Agent comms |
| **Knowledge Base** | Google Drive (Markdown) | Procedures, workflows, business rules |
| **Multi-Agent Protocol** | Agent-to-Agent (A2A) | Standardizes agent communication |

---

## 9. Cost Strategy & Zero-Cost Design Principles

A core constraint of Morphic-G AOS is to minimize operational cost to zero where reasonably possible, while preserving capability. Every infrastructure decision should be evaluated against this hierarchy before committing:

### 9.1 Cost Tier Hierarchy

| Tier | Principle | Examples |
|------|-----------|---------|
| **Free First** | Use free-tier or zero-marginal-cost services by default | Google Sheets API, Google Drive, Apps Script (free tier), Cloud Pub/Sub (first 10GB/month free), Google ADK |
| **Hybrid Second** | Route work to free/local infrastructure when quality is sufficient | Ollama (local LLM) for logging, summarization, formatting; local Python runner for lightweight tasks |
| **Paid Only When Necessary** | Spend tokens only for tasks that genuinely require cloud LLM reasoning | `DEEP_MODEL` for Approval Gate proposals, complex planning, high-stakes decisions |
| **Never Pay for Idle** | Do not run always-on paid compute; use event-driven / serverless patterns | Cloud Run (pay per invocation), Cloud Scheduler triggers, Pub/Sub push — not always-on VMs |

### 9.2 Service-Level Cost Decisions

| Component | Chosen Service | Cost | Rationale |
|-----------|---------------|------|-----------|
| Control Plane | Google Sheets API | Free | Zero per-call charge; already in Google Workspace |
| Agent Messaging | Cloud Pub/Sub | Free (under 10GB/mo) | Event-driven; no idle cost |
| Proactive Triggers | Cloud Scheduler + Cloud Run | Near-zero | Serverless; pay only per execution |
| Observability Logging | Ollama (local) | $0 (electricity) | High-frequency writes don't need cloud LLM quality |
| Routine Summarization | Ollama (local) | $0 | Formatting and log compression; local model sufficient |
| Complex Reasoning | `FAST_MODEL` / `DEEP_MODEL` (paid) | ~$0.01–$0.10/task | Reserved for decisions requiring full LLM capability |
| Knowledge Base | Google Drive (Markdown) | Free | Included in Google Workspace |
| Secrets Management | Google Secret Manager | Free (first 6 secrets/mo) | Replaces insecure `.env` in production |
| Code Execution Sandbox | Vertex AI Code Execution | Pay-per-use | Only invoked during Write-Test-Refine loop |
| Long-term Memory | Vertex AI Memory Bank | Pay-per-use | Invoked on retrieval/write, not continuously |

### 9.3 LLM Routing Rule (Cost Enforcement)

Before any task is sent to a paid cloud model, the routing logic must check:

1. **Can `LOCAL_MODEL` handle this?** — If the task is logging, formatting, summarizing, or classifying structured data, route to local model.
2. **Does it need `FAST_MODEL`?** — If the task needs current knowledge, fast inference, or moderate reasoning, use the fast alias (cheaper).
3. **Does it need `DEEP_MODEL`?** — Only if the task involves: system re-architecture, Approval Gate proposals, multi-agent conflict resolution, or high-stakes business decisions.

No agent may default to `DEEP_MODEL` without explicit justification in its task routing logic.

### 9.4 Free Tier Limits & Paid-Tier Cost Estimates

#### Free Tier Limits (do not exceed without budget review)

| Service | Free Tier Limit | Consequence if Exceeded |
|---------|----------------|-------------------------|
| Apps Script | 90 min/day (free) / 6 hrs/day (Workspace) | Webhook and sync jobs stop running |
| Cloud Pub/Sub | 10 GB/month data volume | $0.04/GB overage |
| Cloud Run | 2M requests/month + 360,000 GB-seconds CPU | Per-request and per-GB-second charges begin |
| Google Sheets API | 300 req/min (project), 60 req/min (user) | `429` rate limit errors; use batching |
| Vertex AI Memory Bank | Charged per operation | Design agents to batch memory reads/writes |
| **BigQuery** | 10 GB storage + 1 TB queries/month free | Per-GB charges beyond free tier; TTL policies prevent accumulation |
| **Cloud Logging** | 50 GB/month ingestion free; 30-day default retention | Reduce retention to 7 days in Log Explorer to stay within free tier |

#### Paid-Tier Cost Estimates (worst-case monthly, Phase 1–4 load)

Assumes: 1 operator, 6 Tier 2 agents, ~50 approval proposals/month, ~10 evolution tasks/month.
Runtime is **Cloud Run (scale-to-zero)** — Vertex AI Agent Engine is explicitly excluded from Phase 1–4 to stay within budget.
Volume includes the `think` node (`GAOS-Persona-Spec.md` §4) and weekly friction audit (`GAOS-Persona-Spec.md` §5) which add ~150K DEEP_MODEL input tokens/month. Routing decisions use FAST_MODEL per §9.3.

| Service | Unit Price | Est. Monthly Volume | Est. Monthly Cost |
|---------|-----------|---------------------|-------------------|
| **Cloud Pub/Sub** | $0.04/GB after 10 GB free | ~50 MB messages | **$0.00** (under free tier) |
| **Cloud Run** (agent runtime + triggers) | $0.000024/vCPU-sec + $0.0000025/GB-sec | ~500 agent invocations × 2 s × 0.25 vCPU + 2 scheduled jobs | **~$0.01** |
| **Cloud Scheduler** | $0.10/job/month (first 3 free) | 3 jobs (TTL sweep + archive + daily-kickoff); 4th job `doc-comment-poll` = $0.10/month (Phase 2.5 Step 5) | **$0.00** (3 jobs within free tier); **$0.10** when Phase 2.5 Step 5 is deployed |
| **Gemini Flash (`FAST_MODEL`)** | ~$0.075/1M input + ~$0.30/1M output tokens | ~700K in + 150K out (routing, Scout synthesis, fallback tasks) | **~$0.10** |
| **Gemini Pro (`DEEP_MODEL`)** | ~$1.25/1M input + ~$5.00/1M output tokens | ~400K in + 100K out (approvals, think node, diagnostics, weekly review) | **~$1.00** |
| **Vertex AI Code Execution** | ~$0.001/session | ~10 sessions (evolution tasks) | **~$0.01** |
| **Vertex AI Memory Bank** | ~$0.005/write, ~$0.002/read | ~100 writes + 300 reads (7-agent boot cycles) | **~$1.10** |
| **Google Secret Manager** | Free first 6 secrets; $0.06/10K accesses | 6 secrets, ~1K accesses | **$0.00** |
| **BigQuery** | $0.02/GB storage after 10 GB free | ~1 GB cold archive | **$0.00** (under free tier) |
| **Vertex AI Agent Engine** | ~$50–$135/month (1 vCPU / 2 GB, always-on) | **Not used in Phase 1–4** | **$0.00** *(deferred)* |
| **Total (Phase 1–4)** | | | **≈ $2.50/month** |

> **Budget trigger:** If monthly spend exceeds **$5.00**, Nexus-Prime publishes a Priority-4 `ALERT`. Review and adjust this threshold after three months of live operation based on actual usage data.

> **Vertex AI Agent Engine upgrade path (Phase 5+):** If orchestrators need persistent in-memory state across days (e.g., active LangGraph graphs that cannot afford cold-start latency), evaluate migrating to Agent Engine. Budget **$50–$135/month per always-on container**. Only justifiable once the business value of eliminating cold-start offsets the cost. Document the decision as a Priority-4 proposal through the Approval Gate before enabling.

---

### 9.5 Data Retention & Archive Policy

Unbounded log accumulation wastes storage, slows the Sheet, and eventually costs money. The policy below enforces a **summarize-before-delete** approach: raw rows are compressed into aggregate summaries before removal so the insight is kept without the noise.

#### Core Rule: Hot vs. Cold vs. Gone

| Tier | Store | Contents | Who manages it |
|------|-------|----------|----------------|
| **Hot** | Google Sheets | Current state only — pending approvals, active tasks, live KPIs, project registry | Agents read/write directly |
| **Cold** | BigQuery | Summarized historical data within retention window | Nightly Apps Script archive job |
| **Gone** | Deleted | Raw rows and cold data past TTL | BigQuery native TTL policies (zero-code) |

#### Retention Schedule

| Data | Sheet retention | BigQuery retention | Notes |
|------|----------------|-------------------|-------|
| Observability log (raw agent thoughts) | 7 days | 30 days | Summarized weekly before deletion |
| Per-iteration evolution logs | 7 days | 14 days | Only useful while debugging a live issue |
| Evolution task outcome summaries | 30 days | 12 months | Recurring error pattern analysis |
| Approval Gate history | 90 days | 24 months | Audit trail |
| Business unit daily rows | 90 days | Daily rows deleted; monthly aggregates kept indefinitely | Trend data matters; daily granularity does not |
| Error logs | 30 days | 6 months | Pattern detection window |
| `local_fallback` / Ollama health events | 30 days | 6 months | Reliability trending |
| Cloud Logging entries | N/A | 7 days (set in Log Explorer) | Google default is 30 days; reduce to save free tier quota |

#### Nightly Archive Job (Cloud Scheduler → Nexus-Prime `/archive` endpoint)

A **Cloud Scheduler job** (`nightly-archive`, `0 2 * * *`) POSTs to the Nexus-Prime Cloud Run service at **2:00 AM daily**. The `/archive` handler in the Nexus-Prime orchestrator performs all steps:

1. **Summarize:** `LOCAL_MODEL` (Ollama / fallback Flash) generates one weekly aggregate row per log type (e.g., "Week of 2026-W12: 847 observability entries, top agent: beacon, 3 evolution tasks, $0.34 spent") and writes it to the Cold tier in BigQuery.
2. **Archive:** Rows older than their Sheet retention threshold are moved to BigQuery.
3. **Delete:** Rows moved successfully are deleted from the Sheet.
4. **Report:** Appends one summary row to the **Archive/Audit Trail** Sheet tab: timestamp, rows archived per tab, current Sheet row counts.
5. **Alert:** If any Sheet tab exceeds 50,000 rows after the job (indicating a failure), publishes an `ALERT` to Nexus-Prime.

#### BigQuery TTL Configuration (set once at table creation)

```sql
-- Example: set partition expiry on the task_outcomes table (30-day retention)
ALTER TABLE `morphic-gaos-prod.aos_logs.task_outcomes`
SET OPTIONS (partition_expiration_days = 30);
```

Each BigQuery table has a native TTL set to its retention window. No ongoing maintenance. No code. Rows older than the TTL are automatically deleted by Google at zero cost.

#### Sheet Health Guardrail

The nightly job logs current row counts per tab. If any tab exceeds **25,000 rows** (half the sluggish threshold), Nexus-Prime publishes an `ALERT` — the archive job may be failing or a log source is unexpectedly high-volume.

---

## 10. A2A Communication Protocol

All communication between Tier 2 orchestrators and Nexus-Prime travels through **Cloud Pub/Sub** using a defined message envelope. This section specifies the topic topology, message schema, routing rules, and the cross-domain workflow policies that govern how agents collaborate.

### 10.1 Pub/Sub Topic Topology

Each orchestrator owns a single outbound topic. Nexus-Prime subscribes to all of them. Orchestrators subscribe to each other **only where a cross-domain workflow requires it** (see Section 10.3).

> **Topic naming convention:** GCP Pub/Sub topic names cannot contain `/`. In all GCP resources and code, use `.` as the separator (e.g. `agent.nexus-prime.events`). The `/` notation in the table below is a human-readable display convention only.

| Topic | Owner | Subscribers |
|-------|-------|-------------|
| `agent/nexus-prime/events` | Nexus-Prime | All Tier 2 orchestrators |
| `agent/ledger/events` | Ledger | Nexus-Prime, Pursuit |
| `agent/beacon/events` | Beacon | Nexus-Prime, Pursuit |
| `agent/pursuit/events` | Pursuit | Nexus-Prime, Ledger, Foreman |
| `agent/foreman/events` | Foreman | Nexus-Prime, Pursuit, Ledger |
| `agent/steward/events` | Steward | Nexus-Prime |
| `agent/scout/events` | Scout | Nexus-Prime, Beacon |

**Rule:** An orchestrator must **never** write directly to another orchestrator's topic. All cross-agent triggers are published to the sender's own topic and consumed by the subscriber.

### 10.2 Standard Message Envelope

Every Pub/Sub message must conform to this Pydantic schema:

```python
class A2AMessage(BaseModel):
    message_id: str        # UUID — unique per message
    correlation_id: str    # Links related messages in a workflow chain
    project_id: str        # Namespace — must match a row in the Project Registry
    source_agent: str      # e.g., "beacon"
    target_agent: str      # e.g., "pursuit" | "nexus-prime" | "broadcast"
    message_type: str      # See message type registry below
    priority: int          # 1 (low) to 5 (critical)
    payload: dict          # Type-specific content
    timestamp: datetime
    requires_ack: bool     # If True, receiver must publish a receipt message
```

#### Message Type Registry

| `message_type` | Direction | Meaning |
|----------------|-----------|--------|
| `STATUS_UPDATE` | Any → Nexus-Prime | Routine heartbeat / objective update |
| `TASK_HANDOFF` | Any → Any | Passing a unit of work to another orchestrator |
| `TASK_COMPLETE` | Any → Nexus-Prime | Task finished; no further action needed |
| `DATA_REQUEST` | Any → Any | Requesting a data payload (awaits `DATA_RESPONSE`) |
| `DATA_RESPONSE` | Any → Requester | Reply to a `DATA_REQUEST` |
| `ALERT` | Any → Nexus-Prime | Anomaly or error requiring manager awareness |
| `ESCALATION` | Any → Nexus-Prime | Requires human decision via Approval Gate |
| `EVOLUTION_REQUEST` | Any → Nexus-Prime | Code evolution cycle initiated by an orchestrator |
| `APPROVAL_REQUEST` | Any → Nexus-Prime | Agent requests human approval via Approval Gate |
| `APPROVAL_RESULT` | Apps Script → Nexus-Prime | Human responded to a proposal (`Approved` / `Rejected`) |
| `KNOWLEDGE_CANDIDATE` | Any → Nexus-Prime | New observation promoted to human review |
| `NEW_PROJECT` | Nexus-Prime internal | Project Registry change detected — initialize namespace |
| `BROADCAST` | Nexus-Prime → All | System-wide directive or config change |
| `TTL_SWEEP` | Cloud Scheduler → Nexus-Prime | Hourly scan for proposals past their TTL |
| `NIGHTLY_ARCHIVE` | Cloud Scheduler → Nexus-Prime | 2 AM archive of aged Sheet rows to BigQuery |
| — *Phase 2.5* — | | |
| `CHAT_MESSAGE` | Google Chat → Nexus-Prime | Inbound owner message via Google Chat |
| `DAILY_SYNC` | Cloud Scheduler → Nexus-Prime | 6 AM morning briefing trigger |
| `VISION_SUBMITTED` | Owner → Nexus-Prime | Owner submitted a project vision (Chat or AppSheet) |
| `PLAN_REVIEW` | Owner → Nexus-Prime | Owner commented on a Blueprint Doc constraint |
| `COMMENT_RECEIVED` | Poll job → Nexus-Prime | Doc comment poll detected a new owner comment |
| `RESEARCH_MANDATE` | Nexus-Prime → Scout | Deep structured research request |
| `SKILL_REQUEST` | Any → Nexus-Prime | Agent requests approval to install a Python package |
| `KNOWLEDGE_INJECTION` | Scout → Nexus-Prime | Corroborated market intelligence (≥ 5 sources) |

### 10.3 Cross-Domain Workflow Policies

These policies define the **triggers, actors, and rules** for every workflow that crosses agent boundaries. They are governance decisions — they may only be modified through the Approval Gate.

#### Policy 1: Lead-to-Revenue (Beacon → Pursuit → Ledger)

| Step | Actor | Action |
|------|-------|--------|
| 1 | Beacon | Publishes `TASK_HANDOFF` with qualified lead data to `agent/beacon/events` |
| 2 | Pursuit | Receives handoff, creates CRM record, begins follow-up sequence |
| 3 | Pursuit | On deal close, publishes `TASK_HANDOFF` with deal details to `agent/pursuit/events` |
| 4 | Ledger | Receives deal closed event, generates invoice, opens AR entry |
| 5 | Ledger | Publishes `STATUS_UPDATE` confirming invoice created |

**Rule:** Pursuit must not generate an invoice directly. All billing actions are owned by Ledger.

#### Policy 2: Campaign Budget Authorization (Beacon ↔ Ledger)

| Step | Actor | Action |
|------|-------|--------|
| 1 | Beacon | Before launching any campaign, publishes `DATA_REQUEST` for available budget to `agent/beacon/events` |
| 2 | Ledger | Responds with `DATA_RESPONSE` containing approved budget ceiling |
| 3 | Beacon | If proposed spend ≤ budget ceiling → proceeds autonomously |
| 4 | Beacon | If proposed spend > budget ceiling → publishes `ESCALATION` to Nexus-Prime for human approval |

**Rule:** Beacon may never commit ad spend exceeding the budget figure returned by Ledger without explicit Approval Gate sign-off.

#### Policy 3: Order-to-Cash (Pursuit → Foreman → Ledger)

| Step | Actor | Action |
|------|-------|--------|
| 1 | Pursuit | Deal closed → publishes `TASK_HANDOFF` with order spec to `agent/pursuit/events` |
| 2 | Foreman | Receives order, checks inventory, coordinates fulfillment |
| 3 | Foreman | Publishes `STATUS_UPDATE` (shipped) or `ALERT` (stock insufficient) |
| 4 | Ledger | On shipped confirmation, marks AR entry as fulfilled |

**Rule:** If Foreman publishes a stock-insufficient `ALERT`, Pursuit must pause the deal and notify Nexus-Prime before committing a delivery date to the client.

#### Policy 4: Intelligence Routing (Scout → Beacon / Nexus-Prime)

| Step | Actor | Action |
|------|-------|--------|
| 1 | Scout | Completes research task, classifies finding as `MARKET_SIGNAL` or `COMPETITOR_ALERT` |
| 2 | Scout | Routes `MARKET_SIGNAL` via `TASK_HANDOFF` to Beacon for campaign consideration |
| 3 | Scout | Routes `COMPETITOR_ALERT` via `ESCALATION` to Nexus-Prime for strategic review |

**Rule:** Scout does not define strategy. It delivers data and routes it to the correct consumer. It does not author cross-agent policies or governance rules.

#### Policy 5: Inventory Threshold Alert (Foreman → Pursuit)

| Step | Actor | Action |
|------|-------|--------|
| 1 | Foreman | Detects stock level below configured threshold |
| 2 | Foreman | Publishes `ALERT` to `agent/foreman/events` |
| 3 | Pursuit | Receives alert, pauses quoting for affected products |
| 4 | Pursuit | Publishes `STATUS_UPDATE` to Nexus-Prime confirming quoting paused |

**Rule:** Pursuit must not quote a product that Foreman has flagged as below threshold until Foreman publishes a restocked `STATUS_UPDATE`.

### 10.4 Escalation and Conflict Resolution Rules

- **Priority 5 (Critical):** Any agent may publish a Priority 5 `ESCALATION` directly to Nexus-Prime, which immediately surfaces it to the Approval Gate sheet and pauses the sending agent.
- **Conflict between orchestrators:** If two orchestrators publish contradictory state (e.g., Pursuit quotes a product Foreman has flagged), Nexus-Prime is the arbiter. It publishes a `BROADCAST` directive resolving the conflict.
- **No direct agent-to-agent commands:** One orchestrator cannot instruct another to take an action. It can only publish data/events and let the subscribing orchestrator's own policy logic respond.
- **All cross-domain messages are logged** to the Observability Log sheet tab with their `message_id` and `correlation_id` for full audit traceability.

---

## 11. Tiered LLM Model Routing

All model references in code and agent instructions use **role aliases**, never hardcoded version strings. The actual model identifiers live exclusively in `config/settings.yaml` and are resolved at runtime. To upgrade to a new Gemini release, update `settings.yaml` only — no code or spec changes required.

### Model Alias Definitions (`config/settings.yaml`)

```yaml
models:
  FAST_MODEL: "gemini-2.0-flash"        # Speed-optimised cloud model
  DEEP_MODEL: "gemini-2.0-pro"          # High-capability cloud model
  LOCAL_MODEL: "ollama/llama3.1"        # Local zero-cost model (primary)
  LOCAL_MODEL_FALLBACK: "gemini-2.0-flash"  # Cloud fallback when Ollama unreachable
  LOCAL_MODEL_TIMEOUT_SECONDS: 2        # Ping timeout before switching to fallback
```

### `DEEP_MODEL` — The Executive Kernel
- Handles "Big Picture" reasoning.
- Used when: re-architecting, complex planning, high-stakes decisions, Approval Gate proposals.
- Example: "Re-brand my entire marketing strategy" → `DEEP_MODEL` analyzes full architecture.

### `FAST_MODEL` — The Shell / I/O Controller
- Built for speed and low cost.
- Handles: chat interface, simple file lookups, routing commands to `DEEP_MODEL` when needed.

### `LOCAL_MODEL` — The Free Worker
- Zero marginal cost (electricity only).
- Handles: observability logging, log summarization, weekly dashboard summaries, data formatting.
- **Fallback:** If unreachable within `LOCAL_MODEL_TIMEOUT_SECONDS`, routes to `LOCAL_MODEL_FALLBACK` and sets `local_fallback=true` in the log entry.

### 11.1 Model Versioning Policy

When Google releases a new Gemini version or a better local model becomes available:

1. **Evaluate** the candidate model in the Vertex AI Sandbox against a benchmark task set; log results to `SKILLS_LOG.md`.
2. **Update** the relevant alias in `config/settings.yaml` (e.g., change `DEEP_MODEL` from `gemini-2.0-pro` to `gemini-3.0-pro`).
3. **Validate** with a dry-run using the Phase 4 staging process.
4. **Promote** through the standard Approval Gate — model upgrades are treated as a **Priority 2 config change** (72-hour TTL).
5. **Log** the change to the Audit Trail sheet tab with the old and new model identifier, benchmark results, and approval ID.

**Rule:** No agent may pin a specific model version string in its own code. All model references must use an alias from `settings.yaml`.

---

## 12. Vertex AI Memory Bank

Operates on a **Record → Retrieve → Refine** cycle using vector embeddings.

- **Contextual Mirror:** When a new task arrives, the agent checks memory for similar past challenges.
- **Safety Net:** If a logic path failed previously, the agent sees that "red flag" and chooses a different route.
- **Stateful Memory:** Transforms the AI from a temporary contractor into a seasoned employee who remembers preferences, past mistakes, and specific business logic.

> **Full memory architecture, self-learning loop, confidence scoring, knowledge proposal schemas, procedural document structure, and governance rules are defined in [`Docs/GAOS-Memory-Spec.md`](GAOS-Memory-Spec.md).**
>
> Key design decisions:
> - Five-layer model: Working (LangGraph) → Episodic (BigQuery) → Observation Buffer (Sheets) → Semantic (Memory Bank) → Procedural (Google Drive)
> - Agents *propose* learnings; all promotions to long-term memory require owner approval through the standard Approval Gate
> - Confidence threshold of **0.70** (5+ corroborating observations) before a proposal is auto-submitted
> - Workflows, policies, and procedures are version-controlled Markdown files in `Knowledge/` on Google Drive; agents propose diffs, Nexus-Prime applies them post-approval
> - Guardrail updates are Priority 5 mandatory — the highest tier of human oversight in the system

---

## 13. Self-Evolution Protocol ("Write-Test-Refine" Loop)

When the agent encounters a task it has no tool for:

1. **Write:** Agent writes a Python script in the Vertex AI Code Execution sandbox.
2. **Test:** Runs the script in the secure environment.
3. **Refine:** Debugs and iterates until successful — subject to the constraints in Section 13.1.
4. **Save:** Stores the new skill to Cloud Storage / `SKILLS_LOG.md`.
5. **Propose:** Sends a proposal to the Approval Gate before deploying.

### 13.1 Loop Constraints (Iteration Limits & TTL)

An unbounded refinement loop is a cost and reliability risk. Every Write-Test-Refine task is subject to all four of the following hard constraints simultaneously — whichever triggers first stops the loop.

| Constraint | Limit | Rationale |
|------------|-------|-----------|
| **Iteration cap** | 5 iterations max | Beyond 5, the agent is missing context or capability only a human can provide |
| **Warning threshold** | Warn at iteration 3 | Surfaces an `ALERT` to the dashboard so the user is aware before the cap is hit |
| **Time-to-live (TTL)** | 15 minutes total | Prevents blocking the agent's event queue; fits within Cloud Run's 60-min ceiling |
| **Cost cap** | $0.50 per evolution task | ~5 Gemini Pro + sandbox calls; if projected spend exceeds this, escalate before next iteration |
| **No-progress detector** | Stop immediately | If the error fingerprint on iteration N is identical to iteration N-1, stop regardless of count — further attempts will not help |

#### Iteration State Schema

Each iteration must record its state so the no-progress detector and escalation log have a complete picture:

```python
class EvolutionIteration(BaseModel):
    task_id: str            # Links all iterations for one evolution task
    iteration_number: int
    error_fingerprint: str  # Hash of the exception type + message (for no-progress detection)
    code_written: str
    test_output: str
    succeeded: bool
    timestamp: datetime
    cumulative_cost_usd: float
```

#### On Any Constraint Trigger — Mandatory Escalation

When the iteration cap, TTL, cost cap, or no-progress detector fires, the agent **must**:

1. **Save** the full iteration log to `PENDING_REPAIRS/<task_id>/` in Cloud Storage.
2. **Publish** an `ESCALATION` message (priority 4) to Nexus-Prime via Pub/Sub, including a plain-language summary of what was attempted and where it failed.
3. **Surface** the escalation to the `Agent_Approvals` sheet with status `Pending` so the human can review partial work and decide whether to provide additional context, reject, or hand off to a developer.
4. **Halt** — the originating agent must not retry the same task autonomously until the escalation is resolved.

### 13.2 Evolution Task Logging

Every evolution task — whether it succeeds or hits a constraint — must emit one structured log event to **Cloud Logging** on completion. This is the primary source for frequency analysis, cost tracking, and identifying systemic capability gaps.

#### `EvolutionTaskOutcome` Log Schema

```python
class EvolutionTaskOutcome(BaseModel):
    task_id: str                  # UUID — unique per evolution task
    project_id: str               # Project namespace this task belongs to
    agent_id: str                 # Which orchestrator triggered it (e.g., "ledger", "beacon")
    trigger_reason: str           # Plain-language description of the capability gap
    total_iterations: int         # How many Write-Test-Refine cycles ran
    stopping_constraint: str      # "success" | "iteration_cap" | "ttl" | "cost_cap" | "no_progress"
    error_fingerprint: str        # Recurring error hash — populated if stopping_constraint == "no_progress"
    total_duration_seconds: float
    total_cost_usd: float
    escalated: bool
    local_fallback: bool          # True if LOCAL_MODEL was unavailable and FAST_MODEL was used
    timestamp: datetime
```

#### Per-Iteration Log (written inside the loop)

Each iteration also writes a lighter `EvolutionIteration` entry (defined in 13.1) to Cloud Logging so individual step failures are queryable independently of the task-level summary.

#### Cloud Logging Labels

All evolution log entries must include these resource labels so they are filterable in Log Explorer and Cloud Monitoring:

```
agent_id: "<agent_name>"
project_id: "<project_slug>"
log_type: "evolution_task"
stopping_constraint: "<constraint_name>"
escalated: "true" | "false"
local_fallback: "true" | "false"
```

#### Weekly Dashboard Summary (Error Logs Sheet Tab)

Ollama (local, zero-cost) runs a weekly summarization job that queries Cloud Logging and appends one row to the **Error Logs** sheet tab in the format:

| Week | Tasks Started | Succeeded | Escalated | Top Constraint | Top Recurring Error | Total Cost |
|------|--------------|-----------|-----------|---------------|--------------------|-----------|
| 2026-W12 | 7 | 4 | 2 | no_progress | `AuthError: 401` | $0.34 |

This gives the CEO a running record of agent reliability trends without requiring Cloud Monitoring access.

### Self-Healing Recovery Loop (Example)

**Scenario:** Legacy weather API returns `410 Gone`.

1. **Detect:** Agent catches the exception, enters Diagnostic Mode.
2. **Research:** Uses `google_search` to find new API documentation.
3. **Write:** Drafts a patch script in the sandbox. *(Iteration 1 begins — TTL clock starts)*
4. **Verify:** Runs `code_interpreter.run_test(new_code)`.
5. **Refine:** If test fails, checks no-progress detector, increments iteration count, repeats from step 3.
6. **Stage:** On success (or constraint trigger), saves to `PENDING_REPAIRS/` in Cloud Storage.
7. **Notify:** Sends push notification — *"Nexus-01 has drafted a fix. Review and Deploy?"* (or escalation notice if constrained).
8. **Deploy:** On approval, `syncSkillsToVertex` passes all three code-injection gates and the new skill is registered via the Cloud Run agent's tool registry. Cloud Run's next invocation picks up the updated code from `ACTIVE_SKILLS/` in Cloud Storage.

---

## 14. Approval Gate Architecture

### Flow

```
Agent detects issue / capability gap
    ↓
Writes proposal row to Agent_Approvals sheet
    ↓
Publishes TASK_HANDOFF to Pub/Sub: agent/approvals/events
    ↓
Agent parks task in LangGraph state store → continues other work
    ↓
[HAPPY PATH] User changes Status cell → "Approved" or "Rejected"
Apps Script onChange trigger fires
    ↓
Apps Script publishes approval event to Pub/Sub: agent/approvals/events
    ↓
Agent Pub/Sub subscriber receives push → resumes parked task
    ↓
On "Approved": syncSkillsToVertex passes all 3 code-injection gates
    ↓
Cloud Storage: PENDING_SKILLS/ → ACTIVE_SKILLS/
    ↓
Cloud Run agent picks up new skill on next cold-start invocation
    ↓
[Phase 5+] Vertex AI Agent Engine hot-swap (if Agent Engine adopted)

[NO-RESPONSE PATH] Cloud Scheduler hourly TTL sweep
    ↓
Proposal older than TTL → re-notify user
    ↓
Proposal older than 2× TTL → mark Stale, publish ESCALATION to Nexus-Prime
    ↓
Auto-reject: agent receives rejection via Pub/Sub, closes task cleanly
```

### Agent_Approvals Sheet Columns

| Column | Value |
|--------|-------|
| A | ID (Unique Proposal ID) |
| B | Agent ID (Which orchestrator raised the proposal) |
| C | Issue (What the agent is trying to fix) |
| D | Trigger Reason (Capability gap description) |
| E | Stopping Constraint (`iteration_cap` / `ttl` / `cost_cap` / `no_progress` / `success`) |
| F | Iterations Run |
| G | Total Cost USD |
| H | Proposed Code (Python script from Sandbox — **protected range; immutable after submission**) |
| I | Status (`Pending` / `Approved` / `Rejected` / `Stale` / `DEPLOYED` / `BLOCKED_TAMPERED` / `BLOCKED_STATIC`) |
| J | Timestamp |
| K | Approved By (email — stamped by `onChangeApproval`) |
| L | Approver Tier (integer — stamped by `onChangeApproval`) |
| M | `code_sha256` (SHA-256 of col H at proposal submission — **protected range; tamper-evident**) |

### Apps Script: `doPost` Webhook

Receives incoming proposals from the Cloud Function and appends them to the sheet. **All requests must pass three validation layers before any sheet write occurs.** An unauthenticated `doPost` endpoint can be exploited to inject malicious code into the approval queue — see Section 15.2 for the full threat model.

#### Layer 1: HMAC-SHA256 Signature Verification

The Cloud Function signs every request body with a shared secret stored in Secret Manager (`WEBHOOK_HMAC_SECRET`). The `doPost` function recomputes the signature and rejects mismatches with HTTP 401 before reading any payload content.

```javascript
// apps_script/doPost.gs
function doPost(e) {
  try {
    // Layer 1: HMAC signature check
    const secret = PropertiesService.getScriptProperties()
                     .getProperty('WEBHOOK_HMAC_SECRET');
    const receivedSig = e.parameter.signature || '';
    const body = e.postData.contents;
    const expectedSig = computeHmacSha256(secret, body);

    if (!secureCompare(receivedSig, expectedSig)) {
      logSecurityEvent_('HMAC_FAILURE', receivedSig);
      return jsonResponse_({ error: 'Unauthorized' }, 401);
    }

    // Layer 2: Schema validation
    const payload = JSON.parse(body);
    const validationError = validatePayload_(payload);
    if (validationError) {
      logSecurityEvent_('SCHEMA_INVALID', validationError);
      return jsonResponse_({ error: validationError }, 400);
    }

    // Layer 3: project_id must exist in Project Registry
    if (!isValidProject_(payload.project_id)) {
      logSecurityEvent_('INVALID_PROJECT', payload.project_id);
      return jsonResponse_({ error: 'Unknown project_id' }, 400);
    }

    // All checks passed — append to sheet
    appendProposal_(payload);
    return jsonResponse_({ status: 'accepted' }, 200);

  } catch (err) {
    logSecurityEvent_('DOPOST_ERROR', err.message);
    return jsonResponse_({ error: 'Internal error' }, 500);
  }
}

function computeHmacSha256(secret, message) {
  const rawKey = Utilities.newBlob(secret).getBytes();
  const rawMsg = Utilities.newBlob(message).getBytes();
  const sig = Utilities.computeHmacSha256Signature(rawMsg, rawKey);
  return Utilities.base64Encode(sig);
}

// Constant-time comparison to prevent timing attacks
function secureCompare(a, b) {
  if (a.length !== b.length) return false;
  let result = 0;
  for (let i = 0; i < a.length; i++) {
    result |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return result === 0;
}

function validatePayload_(p) {
  const required = ['message_id','correlation_id','project_id',
                    'source_agent','message_type','priority','payload'];
  for (const field of required) {
    if (!p[field]) return `Missing required field: ${field}`;
  }
  if (typeof p.priority !== 'number' || p.priority < 1 || p.priority > 5) {
    return 'priority must be integer 1-5';
  }
  return null; // valid
}

// NOTE: getSpreadsheet_() (defined in helpers.gs) must be used instead of
// getActiveSpreadsheet() — the latter returns null in a deployed web-app
// doPost context (standalone script, not bound to a spreadsheet).
function logSecurityEvent_(type, detail) {
  const ss = getSpreadsheet_();
  const log = ss.getSheetByName('Logs') || ss.insertSheet('Logs');
  log.appendRow([new Date(), 'SECURITY', type, String(detail)]);
}
```

#### Cloud Function: Signing the Request

```python
# tools/webhook_sender.py
import hmac, hashlib, base64, requests
from tools.secrets import get_secret

def post_to_webhook(payload: dict, webhook_url: str, project_id: str):
    # If this is a code proposal, pin the SHA-256 hash of the code
    # before the body is serialised and signed. The hash travels inside
    # the HMAC envelope so any post-transit tampering with either the
    # code or the hash will invalidate the signature at the doPost layer,
    # and any post-write Sheet edit will be caught at deploy time.
    inner = payload.get("payload", {})
    if "proposed_code" in inner:
        inner["code_sha256"] = hashlib.sha256(
            inner["proposed_code"].encode()
        ).hexdigest()

    secret = get_secret("WEBHOOK_HMAC_SECRET", project_id)
    body = json.dumps(payload)
    sig = base64.b64encode(
        hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    ).decode()
    resp = requests.post(
        webhook_url,
        data=body,
        params={"signature": sig},
        headers={"Content-Type": "application/json"},
        timeout=10
    )
    resp.raise_for_status()
    return resp.json()
```

#### Webhook Testing Requirements

Before Phase 3 begins, all of the following tests must pass:

| # | Test | Expected Result |
|---|------|-----------------|
| 1 | POST with valid HMAC signature + valid payload | HTTP 200; row appended to sheet |
| 2 | POST with tampered signature (1 char changed) | HTTP 401; no row written; security event logged |
| 3 | POST with missing signature parameter | HTTP 401; no row written; security event logged |
| 4 | POST with valid signature but missing `project_id` field | HTTP 400; no row written |
| 5 | POST with valid signature but `project_id` not in registry | HTTP 400; no row written |
| 6 | POST with valid signature but `priority` out of range (0 or 6) | HTTP 400; no row written |
| 7 | POST with empty body | HTTP 400 or 500; no row written; no unhandled exception |
| 8 | Replay attack: send identical valid request twice | Second request accepted (HMAC alone doesn't prevent replay — see note) |

> **Replay attack note (Test 8):** Basic HMAC does not prevent a valid request being resent. If replay protection is required, add a `nonce` field to the payload and store seen nonces in a short-lived Sheet tab or Cloud Logging check. Defer this to Phase 3+ unless threat model requires it.

#### Secrets Required for Webhook

Add to Secret Inventory (Section 15.1):

| Secret Name | Value | Used By |
|-------------|-------|---------|
| `WEBHOOK_HMAC_SECRET` | Random 32-byte hex string | Cloud Function (sign) + Apps Script (verify) |

Generate with: `python -c "import secrets; print(secrets.token_hex(32))"`

### Apps Script: `syncSkillsToVertex`

Triggered via custom menu **🤖 Agent OS > Sync Approved Skills**. Before deploying any code, this function runs **three sequential gates**. A row is only sent to Vertex if all three pass; otherwise the row status is set to `BLOCKED_TAMPERED` or `BLOCKED_STATIC` and a security event is logged.

```javascript
// apps_script/syncSkillsToVertex.gs
function syncSkillsToVertex() {
  // NOTE: getSpreadsheet_() (defined in helpers.gs) must be used here.
  // getActiveSpreadsheet() returns null in a deployed web-app context.
  const ss = getSpreadsheet_();
  const sheet = ss.getSheetByName('Agent_Approvals');
  const data = sheet.getDataRange().getValues();

  // Dangerous Python patterns — any match blocks deployment
  const DANGEROUS = [
    /\bos\.system\b/, /\bsubprocess\b/, /\beval\s*\(/,
    /\bexec\s*\(/, /\b__import__\s*\(/, /\bcompile\s*\(/,
    /\bpickle\b/, /\bctypes\b/, /\bimportlib\b/, /\bsocket\b/,
  ];

  // Only these top-level module names may appear in import statements
  const ALLOWED_IMPORTS = [
    'google', 'vertexai', 'langchain', 'pydantic', 'datetime',
    'json', 're', 'math', 'typing', 'collections', 'itertools',
    'functools', 'logging', 'gspread',
  ];

  for (let i = 1; i < data.length; i++) {
    // Columns: A=0 … H=7, I=8, J=9, K=10, L=11, M=12
    const [id, agentId, , , , , , code, status, , , , codeHash] = data[i];
    if (status !== 'Approved') continue;

    const codeStr = String(code);

    // ── Gate 1: Code integrity ─────────────────────────────────────
    // Recompute SHA-256 of col H and compare to the hash stored in
    // col M at submission time. Any edit to the cell after the proposal
    // was written (by anyone, including the owner) will mismatch here.
    const recomputed = computeSha256_(codeStr);
    if (recomputed !== String(codeHash).trim()) {
      logSecurityEvent_('CODE_HASH_MISMATCH',
        `Proposal ${id}: col H was edited after submission — deployment blocked`);
      publishCriticalAlert_(id, 5, 'CODE_INJECTION_ATTEMPT',
        'Code tampered after approval submission');
      sheet.getRange(i + 1, 9).setValue('BLOCKED_TAMPERED');
      continue;
    }

    // ── Gate 2: Static analysis ────────────────────────────────────
    // Scan for dangerous built-ins and unapproved imports.
    // This catches malicious code that the agent itself may have
    // written (e.g., via prompt injection into the LLM).
    const violation = staticAnalysis_(codeStr, DANGEROUS, ALLOWED_IMPORTS);
    if (violation) {
      logSecurityEvent_('STATIC_ANALYSIS_BLOCK',
        `Proposal ${id}: ${violation}`);
      sheet.getRange(i + 1, 9).setValue('BLOCKED_STATIC');
      continue;
    }

    // ── Gate 3: Deploy to Vertex AI Agent Engine ───────────────────
    try {
      const token = ScriptApp.getOAuthToken();
      const endpoint = PropertiesService.getScriptProperties()
                         .getProperty('VERTEX_AGENT_ENDPOINT');
      const resp = UrlFetchApp.fetch(endpoint, {
        method: 'POST',
        headers: {
          'Authorization': 'Bearer ' + token,
          'Content-Type': 'application/json',
        },
        payload: JSON.stringify({ proposal_id: id, code: codeStr }),
        muteHttpExceptions: true,
      });
      if (resp.getResponseCode() === 200) {
        sheet.getRange(i + 1, 9).setValue('DEPLOYED');
      } else {
        logSecurityEvent_('DEPLOY_ERROR',
          `Proposal ${id}: HTTP ${resp.getResponseCode()}`);
      }
    } catch (err) {
      logSecurityEvent_('DEPLOY_ERROR', `Proposal ${id}: ${err.message}`);
    }
  }
}

// SHA-256 of a string using Apps Script built-ins (no external libs)
function computeSha256_(text) {
  const bytes = Utilities.newBlob(text).getBytes();
  const hash  = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, bytes);
  return hash.map(b => ('0' + (b & 0xff).toString(16)).slice(-2)).join('');
}

// Returns a violation string or null if clean
function staticAnalysis_(code, patterns, allowedImports) {
  for (const re of patterns) {
    if (re.test(code)) return `Blocked pattern: ${re.source}`;
  }
  const importRe = /^(?:import|from)\s+([\w]+)/gm;
  let m;
  while ((m = importRe.exec(code)) !== null) {
    if (!allowedImports.includes(m[1])) return `Unapproved import: ${m[1]}`;
  }
  return null;
}
```

### Authorized Approvers Tab

A dedicated tab in the master Sheet workbook defines who may approve proposals at each priority tier. The `onChange` handler reads this tab on every status change. The tab is itself protected to owner-only edit.

| Column | Field | Description |
|--------|-------|-------------|
| A | `email` | Google account email of the approver |
| B | `name` | Display name |
| C | `tier` | Integer 1–5 — maximum priority level this person may approve |
| D | `active` | `TRUE` / `FALSE` — set to FALSE to revoke without deleting the row |
| E | `added_date` | ISO date added |
| F | `notes` | e.g., "Owner", "Technical Lead", "Temp contractor — low-risk only" |

**Tier meanings:**

| Tier | Can Approve Priorities | Intended Role |
|------|----------------------|---------------|
| 5 | 1, 2, 3, 4, 5 | Owner only |
| 4 | 1, 2, 3, 4 | Technical Lead |
| 3 | 1, 2, 3 | Senior team member |
| 2 | 1, 2 | Trusted team member |
| 1 | 1 | Limited delegate |

### Approval Gate RBAC: `onChange` Handler

The `onChange` trigger fires on every Sheet edit. When the Status column (col I) of the `Agent_Approvals` tab changes to `Approved` or `Rejected`, the handler validates the approver's identity and authority before allowing the change to stand.

```javascript
// apps_script/onChangeApproval.gs
function onChangeApproval(e) {
  const sheet = e.source.getActiveSheet();
  if (sheet.getName() !== 'Agent_Approvals') return;

  const range = e.range;
  const col = range.getColumn();
  const STATUS_COL = 9; // Column I
  if (col !== STATUS_COL) return;

  const newStatus = range.getValue();
  if (newStatus !== 'Approved' && newStatus !== 'Rejected') return;

  const row = range.getRow();
  const proposalId = sheet.getRange(row, 1).getValue();
  const priority = getPriorityFromProposal_(sheet, row);
  const approverEmail = Session.getActiveUser().getEmail();

  // Look up approver in Authorized Approvers tab
  const approver = getApprover_(approverEmail);

  if (!approver) {
    revertAndAlert_(sheet, row, proposalId, approverEmail,
                    'NOT_IN_APPROVERS_LIST');
    return;
  }
  if (!approver.active) {
    revertAndAlert_(sheet, row, proposalId, approverEmail,
                    'APPROVER_INACTIVE');
    return;
  }
  if (approver.tier < priority) {
    revertAndAlert_(sheet, row, proposalId, approverEmail,
                    `TIER_INSUFFICIENT: tier=${approver.tier} priority=${priority}`);
    return;
  }

  // Authorised — stamp approver identity onto the row
  sheet.getRange(row, 11).setValue(approverEmail);    // Col K: Approved By
  sheet.getRange(row, 12).setValue(approver.tier);    // Col L: Approver Tier
  logApprovalEvent_(proposalId, approverEmail, approver.tier, newStatus);
}

function getApprover_(email) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const tab = ss.getSheetByName('Authorized Approvers');
  if (!tab) return null;
  const data = tab.getDataRange().getValues();
  for (let i = 1; i < data.length; i++) { // skip header
    if (data[i][0] === email) {
      return { email: data[i][0], tier: data[i][2], active: data[i][3] };
    }
  }
  return null;
}

function revertAndAlert_(sheet, row, proposalId, email, reason) {
  sheet.getRange(row, 9).setValue('Pending'); // revert Status to Pending
  logSecurityEvent_('APPROVAL_RBAC_BLOCK', `${proposalId} | ${email} | ${reason}`);
  // Publish alert to Nexus-Prime via UrlFetchApp → Pub/Sub
  publishAlert_(proposalId, email, reason);
}

function logApprovalEvent_(proposalId, email, tier, status) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const log = ss.getSheetByName('Logs');
  log.appendRow([new Date(), 'APPROVAL', proposalId, email, tier, status]);
}
```

### Protected Range Setup (run once, manually)

Execute once after Sheet creation to lock the Status column and the Authorized Approvers tab to owner-only edit:

```javascript
// apps_script/setup_protections.gs — run once from Script Editor
function setupProtections() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const me = Session.getEffectiveUser();

  // Lock Status column (col I) on Agent_Approvals tab
  const approvals = ss.getSheetByName('Agent_Approvals');
  const statusCol = approvals.getRange('I:I');
  const p1 = statusCol.protect().setDescription('Status — owner only');
  p1.removeEditors(p1.getEditors());
  p1.addEditor(me);
  p1.setWarningOnly(false);

  // Lock entire Authorized Approvers tab
  const approversTab = ss.getSheetByName('Authorized Approvers');
  const p2 = approversTab.protect().setDescription('Approvers list — owner only');
  p2.removeEditors(p2.getEditors());
  p2.addEditor(me);
  p2.setWarningOnly(false);

  // Lock Proposed Code column (col H) — immutable after submission.
  // Post-submission edits are caught by the hash check in syncSkillsToVertex,
  // but locking the column prevents accidental or low-effort tampering.
  const codeCol = approvals.getRange('H:H');
  const p3 = codeCol.protect().setDescription('Proposed Code — immutable after submission');
  p3.removeEditors(p3.getEditors());
  p3.addEditor(me);
  p3.setWarningOnly(false);

  // Lock Code Hash column (col M) — tamper-evident seal.
  // If col M is editable, an attacker who edits col H could also update
  // the hash to match. Owner-only prevents this for non-owner actors.
  const hashCol = approvals.getRange('M:M');
  const p4 = hashCol.protect().setDescription('Code SHA-256 — owner only');
  p4.removeEditors(p4.getEditors());
  p4.addEditor(me);
  p4.setWarningOnly(false);

  Logger.log('Protections applied: I (Status), Authorized Approvers tab, H (Code), M (Hash).');
}
```

---

## 15. Governance & Security

| Control | Implementation |
|---------|---------------|
| **Secrets Management** | Google Secret Manager — see Section 15.1 |
| **Webhook Security** | HMAC-SHA256 signed requests + schema validation + project_id check — see Section 15.2 |
| **Approval RBAC** | Protected Range + tier-based `onChange` allowlist + Authorized Approvers tab — see Section 15.3 |
| **User Sign-In** | Google Identity / IAM Service Accounts per agent |
| **Resource Monitoring** | Cloud Monitoring + budget alerts |
| **Privilege Control** | Fine-grained IAM roles (least-privilege) |
| **Environment Isolation** | VPC Sandboxing (Vertex Agent Engine) |
| **Error Logging** | Cloud Logging (tamper-proof audit trail) — structured labels on all evolution and A2A events |
| **Evolution Frequency Monitoring** | Cloud Monitoring dashboard — query by `agent_id`, `stopping_constraint`, `escalated` labels |
| **Weekly CEO Summary** | Ollama summarization job → Error Logs sheet tab (see Section 13.2) |
| **Rollback** | Vertex AI Versioned Agent Deployments |
| **Code Injection Prevention** | SHA-256 hash pinning at submission + protected col H/M + static analysis + import allowlist before Vertex deploy — see Section 15.4 |
| **Auth for Deployment** | `ScriptApp.getOAuthToken()` — only the Google account owner can push to production |

### VPC Security Layers

| Feature | Real-World Equivalent | What It Does |
|---------|----------------------|-------------|
| VPC | The Gated Community | Keeps digital assets off public internet |
| Subnet | Individual Yards | Divides space so "Guest House" can't see "Main Vault" |
| Sandboxing | The Testing Lab | Safe room for running experimental code |
| Security Groups | The Bouncer | Controls exactly who is allowed in/out |

### 15.1 Secrets Management Policy

Storing API keys in `.env` files is acceptable **only during local development**. Any secret that touches a cloud service, a shared machine, or a Git repository must be stored in **Google Secret Manager**. This policy is non-negotiable and applies to all environments from Phase 1 onward.

#### Secret Inventory

| Secret Name | Value | Used By |
|-------------|-------|---------|
| `GEMINI_API_KEY` | Gemini API key | All agents using `FAST_MODEL` / `DEEP_MODEL` |
| `OLLAMA_HOST` | Local Ollama endpoint URL (e.g. `http://localhost:11434`) | `LOCAL_MODEL` routing |
| `WEBHOOK_HMAC_SECRET` | Random 32-byte hex string | Nexus-Prime (`webhook_sender.py`) + Apps Script (`doPost`) |
| `WEBHOOK_URL` | Apps Script Web App URL | Nexus-Prime (`webhook_sender.py`) |
| `GOOGLE_SEARCH_API_KEY` | Google Custom Search JSON API key | Scout (`tools/google_search.py` — `_discover` node, Phase 2.5 Step 6) |
| `GOOGLE_SEARCH_CX` | Custom Search Engine ID (CX) | Scout (`tools/google_search.py` — `_discover` node, Phase 2.5 Step 6) |

> **No JSON key files in Secret Manager.** All Google service access (Sheets, Pub/Sub, BigQuery, Drive, Vertex AI) is handled via service account identity — each Cloud Run service runs as its own SA and calls `google.auth.default()`. Service account JSON keys are not created, stored, or injected anywhere in this system. See `GAOS-Deploy-Spec.md §2`.
| Environment | Secret Source | `.env` allowed? |
|-------------|--------------|----------------|
| Local dev (your machine) | `.env` file — never committed to Git | Yes, `.env` in `.gitignore` |
| Cloud Run | Google Secret Manager — fetched at boot via `get_secret()` using service account identity; **not** injected as env vars | No |
| Apps Script | `PropertiesService.getScriptProperties()` — Google's built-in secret store for Apps Script | No |
| CI/CD pipeline | Secret Manager accessed via Workload Identity Federation | No |

**Rule:** `.env` must be listed in `.gitignore` from day one. A pre-commit hook must be configured to block any commit containing strings matching known key patterns (e.g., `AIza`, `-----BEGIN`).

#### Accessing Secrets in Python (Cloud Runtime)

```python
from google.cloud import secretmanager

def get_secret(secret_id: str, project_id: str) -> str:
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")

# Usage
gemini_key = get_secret("GEMINI_API_KEY", settings.GCP_PROJECT_ID)
```

#### Accessing Secrets in Apps Script

```javascript
// Store once via Script Editor > Project Settings > Script Properties
const props = PropertiesService.getScriptProperties();
const hmacSecret = props.getProperty('WEBHOOK_HMAC_SECRET');
const webhookUrl  = props.getProperty('WEBHOOK_URL');
```

#### IAM Rule for Secret Access

Each agent's service account is granted `roles/secretmanager.secretAccessor` **only for the specific secrets it needs** — not blanket access to all secrets in the project. For example, only Nexus-Prime's SA can read `WEBHOOK_HMAC_SECRET`; all agents can read `GEMINI_API_KEY` and `OLLAMA_HOST`. Least-privilege enforced at the secret level.

#### Secret Rotation Policy

- Rotate all API keys every **90 days** or immediately on suspected compromise.
- Use Secret Manager **versions** — never overwrite a secret value; add a new version and deprecate the old one.
- Rotation triggers a Priority 2 `BROADCAST` from Nexus-Prime to all agents to reload their cached credentials.

### 15.2 Webhook Security Policy

The `doPost` Apps Script webhook is the external entry point for the approval queue. An unauthenticated endpoint allows anyone with the URL to inject arbitrary code into the approval pipeline. The three-layer defense defined in Section 14 (`doPost` implementation) is mandatory from Phase 1 onward.

#### Threat Model

| Threat | Attack Vector | Layer That Stops It |
|--------|--------------|--------------------|
| Unauthenticated injection | POST from unknown source | Layer 1: HMAC signature |
| Payload tampering in transit | Modified request body | Layer 1: HMAC invalidated by any change |
| Malformed payload crashing sheet logic | Missing/invalid fields | Layer 2: Schema validation |
| Cross-project data poisoning | Valid signature, wrong project | Layer 3: Project Registry check |
| Timing attack on signature compare | Statistical analysis of response times | `secureCompare()` constant-time function |
| Replay attack | Resend of a valid previous request | Deferred — add nonce if threat model requires |

#### Operational Rules

1. `WEBHOOK_HMAC_SECRET` must be stored in both Secret Manager (Cloud Function side) and `PropertiesService` (Apps Script side). **Never the same value as any other secret.**
2. The Apps Script Web App must be deployed as **"Execute as: Me"** and **"Who has access: Anyone"** — this is required for the Cloud Function to reach it, but the HMAC layer is what restricts it to authorised callers only.
3. Security events (`HMAC_FAILURE`, `SCHEMA_INVALID`, `INVALID_PROJECT`) logged to the Logs sheet tab must also be forwarded to Cloud Logging via a secondary Apps Script call for tamper-proof audit trail.
4. If `HMAC_FAILURE` events exceed **5 in any 10-minute window**, Nexus-Prime must publish a Priority 5 `ESCALATION` — this pattern indicates an active probing or replay attack.
5. The webhook URL must never be committed to the repository, logged in plaintext, or included in any Sheet cell. Treat it as a secret: store in Secret Manager as `WEBHOOK_URL`.

#### Phase 1 Pre-Deployment Checklist

- [ ] `WEBHOOK_HMAC_SECRET` created in Secret Manager and `PropertiesService`
- [ ] `WEBHOOK_URL` stored in Secret Manager
- [ ] All 8 webhook tests from Section 14 passing
- [ ] Security event logging verified (row appears in Logs tab on auth failure)
- [ ] `.gitignore` confirmed to exclude `.env` and any local test files containing secrets
- [ ] Pre-commit hook installed to block accidental secret commits

### 15.3 Approval RBAC Policy

The Approval Gate is the highest-impact human interaction point in the system — an approval deploys code to production. Access to the Status column must be restricted to verified, authorised individuals with authority appropriate to the proposal's risk level.

#### Three-Layer Defence

| Layer | Mechanism | Stops |
|-------|-----------|-------|
| 1 | **Protected Range** on Status column | Any non-owner from physically editing the cell |
| 2 | **`onChange` allowlist + tier check** | Delegates approving above their authority; anyone added to Sheet with edit rights |
| 3 | **Authorizer stamped on row** (cols K, L) | Audit trail — every approval is attributed to a named, tiered individual |

#### Priority-to-Tier Requirement

| Proposal Priority | Required Approver Tier | Rationale |
|------------------|----------------------|-----------|
| 5 — Critical | 5 (Owner only) | System re-architecture; maximum blast radius |
| 4 — High | 4+ | Self-healing patches; code runs on Vertex |
| 3 — Normal | 4+ | New skills deployed to production |
| 2 — Low | 2+ | Config changes; low deployment risk |
| 1 — Info | 1+ | Routine log actions; minimal risk |

#### RBAC Security Events

All blocked approval attempts are logged as security events with reason code:

| Reason Code | Meaning |
|-------------|--------|
| `NOT_IN_APPROVERS_LIST` | Email not found in Authorized Approvers tab |
| `APPROVER_INACTIVE` | Approver exists but `active = FALSE` |
| `TIER_INSUFFICIENT` | Approver tier < proposal priority |

If `APPROVAL_RBAC_BLOCK` events exceed **3 in any 30-minute window**, Nexus-Prime publishes a Priority 4 `ALERT` — this may indicate a misconfigured team member or an insider threat attempt.

#### RBAC Testing Requirements

| # | Test | Expected Result |
|---|------|-----------------|
| 1 | Owner (tier 5) approves a priority 5 proposal | Accepted; cols K/L stamped; logged |
| 2 | Non-listed email edits Status to Approved | Reverted to Pending; `NOT_IN_APPROVERS_LIST` logged; alert published |
| 3 | Tier-2 approver approves a priority-4 proposal | Reverted to Pending; `TIER_INSUFFICIENT` logged |
| 4 | Inactive approver (active=FALSE) approves any proposal | Reverted to Pending; `APPROVER_INACTIVE` logged |
| 5 | Owner approves a priority-2 proposal | Accepted; cols K/L stamped |
| 6 | Owner runs `setupProtections()` then non-owner tries to edit Status | Cell is greyed out / edit blocked by Sheets before `onChange` even fires |

#### Phase 1 Pre-Deployment Checklist (RBAC)

- [ ] `setupProtections()` run; Status column and Authorized Approvers tab locked to owner
- [ ] Authorized Approvers tab populated with at least one row (owner, tier 5, active=TRUE)
- [ ] `onChangeApproval` trigger installed (Apps Script > Triggers > onChange)
- [ ] All 6 RBAC tests above passing
- [ ] Cols K and L verified to populate correctly on a test approval

### 15.4 Code Injection Prevention Policy

The `Agent_Approvals` sheet is the last human checkpoint before agent-written Python executes on Vertex AI. An attacker who can modify col H after the code was sandbox-tested — or an LLM that was prompt-injected into writing malicious code — could deploy arbitrary payloads to production. This section defines the three-gate defence that prevents this.

#### Threat Model

| Threat | Vector | Gate that Stops It |
|--------|--------|--------------------|
| **Post-approval cell edit** | Attacker/insider edits col H after human approves but before `syncSkillsToVertex` runs | Gate 1: hash mismatch → `BLOCKED_TAMPERED` + Priority-5 alert |
| **Prompt injection → malicious code** | LLM writes `os.system()` or `subprocess` call in the generated Python | Gate 2: static analysis blocklist → `BLOCKED_STATIC` |
| **Unapproved dependency** | Agent imports `requests` to exfiltrate data to a non-Google URL | Gate 2: import allowlist → `BLOCKED_STATIC` |
| **Hash spoofing** | Attacker edits both col H and col M to forge a consistent hash | Partially mitigated: col M is owner-only protected. Full mitigation is out-of-band hash storage (Phase 3+). |
| **Sandbox bypass** | Code passes static analysis but exploits a Vertex AI sandbox escape | Vertex AI sandboxes are network-isolated by Google. Not in scope for application-layer controls. |

#### Three Gates — Mandatory Pass Order

All three gates execute in `syncSkillsToVertex` before any code reaches Vertex:

| Gate | Check | Block Status | Alert Priority |
|------|-------|-------------|----------------|
| **1 — Integrity** | SHA-256 of col H == col M | `BLOCKED_TAMPERED` | 5 (Critical) |
| **2 — Static Analysis** | No dangerous built-in patterns (see blocklist below) | `BLOCKED_STATIC` | 4 (High) |
| **2b — Import Allowlist** | All `import`/`from` statements use approved modules only | `BLOCKED_STATIC` | 4 (High) |
| **3 — Deploy** | All gates passed; POST to Vertex Agent Engine | `DEPLOYED` | — |

#### Static Analysis Blocklist

Any occurrence of the following in the proposed code blocks deployment:

| Pattern | Risk |
|---------|------|
| `os.system` | Arbitrary shell command execution |
| `subprocess` | Shell execution; process spawning |
| `eval(` | Dynamic code evaluation |
| `exec(` | Dynamic code execution |
| `__import__(` | Dynamic module loading bypass |
| `compile(` | Runtime code compilation |
| `pickle` | Arbitrary object deserialisation (code execution) |
| `ctypes` | Direct memory / OS access |
| `importlib` | Dynamic import bypass |
| `socket` | Raw network access outside Google APIs |

#### Import Allowlist

Only the following top-level module names are permitted in `import` and `from … import` statements:

`google`, `vertexai`, `langchain`, `pydantic`, `datetime`, `json`, `re`, `math`, `typing`, `collections`, `itertools`, `functools`, `logging`, `gspread`

> **Updating the allowlist:** Any addition requires a Priority-4 owner approval proposal first. The allowlist is stored in `settings.yaml` under `code_safety.allowed_imports` so it can be updated without modifying Apps Script.

#### Testing Requirements

| # | Test | Expected Result |
|---|------|-----------------|
| 1 | Approved row with correct col M hash | Passes Gate 1; proceeds to Gate 2 |
| 2 | Manually edit col H after submission (requires owner); run sync | `CODE_HASH_MISMATCH` logged; row → `BLOCKED_TAMPERED`; Priority-5 alert published |
| 3 | Submit code containing `os.system('rm -rf /')` | Gate 2 blocks; row → `BLOCKED_STATIC`; `STATIC_ANALYSIS_BLOCK` logged |
| 4 | Submit code with `import requests` (not on allowlist) | Gate 2b blocks; `Unapproved import: requests` logged |
| 5 | Submit clean code using only allowlisted imports | All gates pass; row → `DEPLOYED` |
| 6 | `setupProtections()` run; non-owner tries to edit col H | Sheets blocks the edit before `onChange` or `syncSkillsToVertex` can see it |

#### Phase 1 Pre-Deployment Checklist (Code Injection Prevention)

- [ ] `setupProtections()` updated and re-run to include col H and col M protections
- [ ] `VERTEX_AGENT_ENDPOINT` added to Script Properties
- [ ] `syncSkillsToVertex` replaced with gated implementation above
- [ ] `post_to_webhook` updated to compute and include `code_sha256` in the payload
- [ ] All 6 code injection tests above passing
- [ ] `settings.yaml` contains `code_safety.allowed_imports` list

---

## 16. Development Roadmap

### Phase 1: The "Heartbeat" (Connectivity)
- Create `tools/google_sheets.py` using `gspread`.
- **Goal:** Python script successfully adds a row to Manager's Dashboard and reads a specific cell's status.
- **Why first:** If the Sheet connection is flaky, the AI will fail.

#### Phase 1 Exit Criteria
Phase 1 is complete when **all** of the following are true:

1. `tools/google_sheets.py` successfully appends a row and reads a cell value without errors.
2. The Apps Script `onChange` trigger fires and publishes a message to the `agent/approvals/events` Pub/Sub topic when a Status cell is changed manually.
3. A local Python subscriber receives the Pub/Sub push and prints the proposal ID and new status.
4. The Cloud Scheduler TTL sweep job exists and can be triggered manually for testing.
5. **Doc split completed:** `GAOS-Manager-Spec.md` has been decomposed into the `Docs/architecture/`, `Docs/agents/`, `Docs/operations/`, and `Docs/roadmap.md` structure (see Section 18.1). The monolith file becomes the index only. No agent loads the full spec as a system prompt.

### Phase 2: The "Observability" Loop (Ollama Integration)
- Set up a background process that uses **Ollama** to summarize workspace activity.
- **Output:** Script appends "System Thoughts" to Google Sheet every few minutes.

### Phase 2.5: The "Conversation Layer" (Chat + Vision Hub)

Inserted between Phase 2 and Phase 3. All items below must be deployed and verified before Phase 3 begins.

#### Goals
- Replace Sheet-dropdown approval with a Google Chat card (Option A: Chat is the single source of truth for approval events).
- Add a conversational `POST /chat` endpoint so the owner can interact with Nexus-Prime directly.
- Introduce a daily morning briefing via `POST /daily-sync` (Cloud Scheduler, 6 AM daily).
- Add a **Blueprint Factory** — Nexus-Prime converts Chat Vision submissions into structured Google Docs project blueprints.
- Integrate **Vertex AI Search** over the Drive `Knowledge/` corpus so agents can retrieve procedural memory (Layer 5 was write-only before this step).
- Add an **AppSheet** Vision Hub UI as a structured alternative submission path (merged into this step).
- Enable Scout's **recursive web search** (`_discover` node) and wire it to the `KNOWLEDGE_INJECTION` protocol.
- Add the **ITERATE_PLAN** constraint compaction node to Nexus-Prime's LangGraph graph.

#### Phase 2.5 Build Sequence

| Step | Deliverable | New file(s) | Status |
|------|------------|-------------|--------|
| 1 | `tools/google_chat.py` + `POST /chat` endpoint + Skill Import card | `tools/google_chat.py`, `tests/test_google_chat.py` | ✅ Complete |
| 2 | `POST /daily-sync` endpoint + daily morning briefing card + `daily-kickoff` Scheduler job | `tests/test_daily_sync.py` | ✅ Complete |
| 3 | `tools/vertex_search.py` + Playbook schema + `write_playbook` node (all 6 orchestrators) | `tools/vertex_search.py` | ✅ (`write_playbook` ⏳ Phase 3) |
| 4 | `tools/google_docs.py` + Blueprint Factory (Nexus-Prime `ITERATE_PLAN` node) | `tools/google_docs.py` | ✅ |
| 5 | AppSheet Vision Hub + `VISION_SUBMITTED` handler + `doc-comment-poll` Scheduler job | `tests/test_vision_workflow.py` | ✅ Complete (`doc-comment-poll` ⏳ wire in GCP) |
| 6 | Scout `_discover` recursive node + `tools/google_search.py` + `KNOWLEDGE_INJECTION` | `tools/google_search.py`, `tests/test_scout_discover.py` | ✅ Complete |
| 7 | `ITERATE_PLAN` constraint compaction node + `SKILL_REQUEST` approval flow | — | — |

#### Phase 2.5 Exit Criteria

1. `POST /chat` returns HTTP 200; Nexus-Prime responds in the Chat thread.
2. `POST /daily-sync` returns HTTP 200; a morning briefing card is posted to the owner's Chat space.
3. `daily-kickoff` Scheduler job fires at 6 AM and hits `POST /daily-sync` (manual Force Run passes).
4. Approval Gate: owner taps **Approve** on a Chat card → `APPROVAL_RESULT` published → Nexus-Prime resumes the parked task. Sheet row is written as audit trail only.
5. A Vision submission via Chat (`VISION_SUBMITTED`) results in a structured Google Doc blueprint in the project Drive folder.
6. Vertex AI Search returns ≥ 1 procedural document for a domain keyword query.
7. Scout `_discover` runs a 3-depth recursive search; findings are buffered correctly as `KNOWLEDGE_INJECTION` candidates with `knowledge_type = "market_intel"`.
8. `ITERATE_PLAN` compaction triggers when `constraints` list reaches 5 items; original constraints are archived to BigQuery `aos_logs.blueprint_constraints`.
9. All new tests pass (`pytest`, 0 failures).
10. Monthly cost projection (from 7-day test run) remains below **$3.00/month** at equivalent production load.

---

### Phase 3: The "Approval Gate" (Gemini Integration)
- Program Supervisor Agent to:
  1. Detect a problem.
  2. Write a proposal row to the `Agent_Approvals` sheet and publish a `TASK_HANDOFF` to `agent/approvals/events`.
  3. **Park the task** in LangGraph state — agent continues other work.
  4. Resume only when the Apps Script `onChange` push arrives via Pub/Sub with status `"Approved"` or `"Rejected"`.
  5. Cloud Scheduler TTL sweep handles the no-response case (see Section 3.B).

### Phase 4: The Validation ("First Run")
1. Run connectivity test — verify a row appears in the sheet.
2. Manually change status to `Approved`.
3. Click **🤖 Agent OS > Sync**.
4. Verify the "Approved" signal reaches Vertex.

#### Phase 4 Exit Criteria

Phase 4 is complete when **all** of the following are true:

1. Nexus-Prime publishes at least one `TASK` message and a Tier 2 orchestrator returns a `COMPLETED` reply within 60 seconds — confirmed in Cloud Logging.
2. A Priority-4 or Priority-5 proposal triggers the full Approval Gate loop end-to-end: Pub/Sub push received → row written to `Agent_Approvals` → `onChangeApproval` fires → RBAC check passes → confirmation event published back to Nexus-Prime.
3. At least one Tier 2 orchestrator completes a self-evolution loop: `EvolutionIteration` rows written, Write-Test-Refine cycle completes, `EvolutionTaskOutcome` row logged with `convergence_achieved = TRUE`.
4. All three hard stops (iteration cap, TTL, cost cap) are individually verified to halt the evolution loop when triggered artificially in a test run.
5. Ollama fallback verified end-to-end: stop the local Ollama service → confirm `LOCAL_MODEL_FALLBACK` (Gemini Flash) takes over → `local_fallback = true` appears in the `EvolutionTaskOutcome` log row.
6. `post_to_webhook` HMAC validation passes all 8 tests in the test matrix (Section 14).
7. Estimated monthly cost — projected from actual Cloud Logging `DEEP_MODEL` and `FAST_MODEL` call counts over the 7-day test run — is below **$5.00/month** at equivalent production load.

### Phase 5: CEO Dashboard (Grafana) — *Future*
> **Status: Placeholder — implement after Phase 4 is stable.**

- Deploy **Grafana** (self-hosted on Cloud Run or local machine) as the visual CEO dashboard layer.
- Connect Grafana to the Google Sheets data source via the Sheets API connector.
- Define all dashboard panels as JSON in `dashboard/grafana/` — version-controlled alongside the codebase.
- Panels to build (minimum viable dashboard):
  - Agent status tiles (one per Tier 2 orchestrator — green/amber/red)
  - Approval Gate queue (pending count + oldest item age)
  - Evolution task frequency chart (by agent, by stopping constraint)
  - Weekly cost tracker (total USD spent on `FAST_MODEL` / `DEEP_MODEL`)
  - `local_fallback` frequency (Ollama availability health)
  - Business unit KPIs (revenue, pipeline, ad spend — fed from domain orchestrator tabs)
- Sheet conditional formatting script (`dashboard/setup_sheet_formatting.gs`) runs once on deploy to keep the raw Sheet readable when accessed directly.

**Why Grafana over Looker Studio:** Looker Studio has no meaningful programmatic API — reports must be built by hand in the UI and cannot be version-controlled. Grafana dashboards are fully defined in JSON, deployable via API, and live in the repo alongside the agents that feed them.

---

## 17. Implementation Checklist

| Component | Google Service | Status |
|-----------|---------------|--------|
| Logic/Reasoning | Gemini 2 + Google ADK | [x] |
| Code Execution | Vertex AI Sandbox | [ ] |
| Messaging | Cloud Pub/Sub | [x] |
| Proactive Trigger | Cloud Scheduler | [x] |
| Human Interface | Google Sheets + Apps Script | [x] |
| Conversational Interface | Google Chat App | [x] |
| Vision Hub | AppSheet (Business Workspace) | [x] |
| Blueprint Factory | Google Docs API | [x] |
| Institutional Knowledge Retrieval | Vertex AI Search (over Drive corpus) | [x] |
| Structured Research | Google Custom Search API | [x] |
| Memory | Vertex AI Memory Bank | [x] |
| Runtime (Phase 1–4) | Cloud Run (scale-to-zero, event-driven) | [x] |
| Runtime (Phase 5+) | Vertex AI Agent Engine *(deferred — see §9.4)* | [ ] |
| Knowledge Base | Google Drive | [x] |
| Skills Library | Agent Garden / MCP Tools | [ ] |
| **Nexus-Prime (Tier 1)** | Google ADK Root Agent | [x] |
| **Ledger — Accounting Agent (Tier 2)** | Google ADK + Sheets | [x] |
| **Beacon — Marketing Agent (Tier 2)** | Google ADK + Sheets | [x] |
| **Pursuit — Sales Agent (Tier 2)** | Google ADK + CRM tools | [x] |
| **Foreman — Operations Agent (Tier 2)** | Google ADK + Sheets | [x] |
| **Steward — Admin Agent (Tier 2)** | Google ADK + Calendar | [x] |
| **Scout — Research Agent (Tier 2)** | Google ADK + Search | [x] |

---

## 18. Key Implementation Principles

### 18.1 Documentation Split Policy

Once Phase 1 exit criteria are met, `GAOS-Manager-Spec.md` must be decomposed into the following structure. The master file becomes a ~150-line index and overview; agents load only the files relevant to their role.

```
Docs/
├── GAOS-Manager-Spec.md      # Index only: overview, component stack, agent hierarchy
├── architecture/
│   ├── a2a-protocol.md       # Section 10: A2A, message envelopes, workflow policies
│   ├── llm-routing.md        # Section 11: model aliases, versioning policy
│   └── approval-gate.md      # Section 14: flow, sheet columns, Apps Script
├── agents/
│   ├── nexus-prime.md        # Identity: persona, rules, tools, guardrails
│   ├── ledger.md
│   ├── beacon.md
│   ├── pursuit.md
│   ├── foreman.md
│   ├── steward.md
│   └── scout.md
├── operations/
│   ├── self-evolution.md     # Section 13: loop constraints, logging schema
│   ├── governance.md         # Section 15: security, IAM, VPC
│   ├── cost-strategy.md      # Section 9: cost tiers, free tier limits
│   └── memory-spec.md        # GAOS-Memory-Spec: full memory architecture reference
└── roadmap.md                # Sections 16–17: phases, checklist
```

**Agent loading rule:** Each agent loads only its own identity file plus the architecture files it acts on. Nexus-Prime loads `a2a-protocol.md` and `approval-gate.md`. Domain orchestrators (Ledger, Beacon, etc.) load their own identity file only. No agent loads the full `GAOS-Manager-Spec.md`.

**Context Trio auto-append:** `_load_identity_file()` in `agents/__init__.py` automatically appends the three Context Trio files (`Docs/about-me.md`, `Docs/brand-voice.md`, `Docs/working-preferences.md`) to every agent's system prompt after the agent-specific identity text. This provides owner business context, brand voice, and operating rules to all 7 agents without per-orchestrator changes. The trio is loaded via `_load_context_trio()` and falls back gracefully if any file is absent.

- **Use Pydantic for Data:** Define what an "Action" looks like so the AI always sends `ID`, `Code`, and `Description` in the exact format the Sheet expects.
- **The "State" is the Sheet (current state only):** The Sheet holds *live* state — pending approvals, active task queue, project registry, current KPIs. Do not store historical log data in the Sheet. If the system restarts, the agent reads the Sheet to know where it left off. Historical context (past decisions, recurring errors, trends) comes from BigQuery or Cloud Logging, not from scrolling Sheet rows. See Section 9.5 for retention thresholds.
- **Start with "Dry Runs":** Before giving `executor.py` permission to run code, have it write the "Proposed Command" to the sheet for review.
- **Batch Sheet Writes:** Collect multiple log entries and write them in a single API call to stay under rate limits.
- **Agent Identity Files (`agents.md`):** Each agent has a persona, goal, objectives, resources, specification, guardrails (Do/Don't), history, and knowledge definition.

### 18.2 Agent Identity File Template

Every Tier 2 orchestrator and Nexus-Prime has a corresponding file at `Docs/agents/<agent-name>.md`. This is the canonical structure each file must follow. The file is loaded as the system prompt header for every ADK session via `_load_identity_file()` in `agents/__init__.py`, which also appends the Context Trio automatically (see §18.1 Context Trio auto-append above). Nexus-Prime's identity file is `Docs/agents/nexus-prime.md`; its governance rules are defined in Section 1 of this document.

```markdown
# <Agent Name> — Identity File
<!-- Example: Beacon — Marketing Agent -->

## Persona
<One sentence describing who this agent "is". Written in first person.>
Example: "I am Beacon, the marketing intelligence agent for [Company]. I track campaign performance, monitor ad spend, and surface growth opportunities."

## Goal
<Single measurable primary objective.>
Example: "Ensure marketing spend drives measurable pipeline growth; surface any campaign with negative ROI within 24 hours."

## Objectives (Ongoing)
- <Short recurring task 1>
- <Short recurring task 2>
- <Short recurring task 3>

## Resources
| Resource | Type | Access |
|----------|------|--------|
| `<Domain>` Sheet tab | Google Sheets | Read/Write |
| `agent/<name>/events` | Cloud Pub/Sub | Publish |
| `agent/nexus-prime/events` | Cloud Pub/Sub | Subscribe |

## Specification
<Concise description of this agent's operational scope: what decisions it makes, what it does NOT touch.>

## Guardrails

**Do:**
- <Allowed action 1>
- <Allowed action 2>

**Don't:**
- Never approve your own proposals.
- Never write to Sheet tabs owned by another orchestrator.
- Never call `DEEP_MODEL` for tasks classifiable as logging, formatting, or summarization.

## Escalation Rules
| Condition | Action |
|-----------|--------|
| Domain KPI deviates >20% from baseline | Publish Priority-3 ALERT to Nexus-Prime |
| External API returns 5xx three times in a row | Pause task; publish Priority-4 ALERT |
| Evolution loop hits any hard stop | Log `EvolutionTaskOutcome`; publish Priority-2 INFO |

## Knowledge Sources
- `Docs/architecture/a2a-protocol.md`
- `Docs/agents/<name>.md` (this file)

## History
<!-- Auto-populated by the system; do not edit manually -->
Last updated: <ISO timestamp>
Last evolution task: <task ID or "none">
```

**Convention:** File name is the agent's lowercase short name (`beacon.md`, `ledger.md`, `pursuit.md`, `foreman.md`, `steward.md`, `scout.md`). Files live in `Docs/agents/` and are loaded individually — never the full spec.
