# Project Modifications Scratchpad

**Purpose:** Working document to capture all ideas before any spec changes are made.
**Status:** ✅ Ideas capture complete — modification plan drafted below. Ready for owner review.

---

## MODIFICATION PLAN

### Resolved Design Decisions

**Chat vs Sheet primary approval surface:** ✅ **Option A selected.** Chat is the primary approval surface. Owner taps Approve/Reject on a Chat card. Sheet `Agent_Approvals` tab is a read-only audit trail — rows still written there for logging, but the `onChange` trigger is NOT the approval source of truth. Apps Script `onChange` is retained only for logging; Nexus-Prime acts on Chat callback only.

**Concern #1 — Phase numbering:** ✅ Resolved. New additions are **Phase 2.5**. Original Phase 3 (Approval Gate / Gemini Integration) keeps its number. Sequence: Phase 2 (complete) → **Phase 2.5 (Innovation Upgrade, this work)** → Phase 3 (Approval Gate) → Phase 4 (Validation) → Phase 5 (Grafana).

**Concern #2 — `KNOWLEDGE_INJECTION` governance bypass:** ✅ Resolved. Scout's Market Intelligence follows the standard 5-observation confidence gate (no bypass). Tagged `knowledge_type = "market_intel"` to distinguish from internally observed patterns. Owner still approves before Layer 4 promotion. Web-sourced data is never more trusted than lived business experience.

**Concern #3 — `ITERATE_PLAN` token limit creep:** ✅ Resolved. Compaction scheme defined below.

**Concern #4 — AppSheet timing:** ✅ Resolved. AppSheet configuration merged into Step 5 (Vision Hub) — available from day one of that step. Step 7 slot removed.

**Concern #5 — Test suite scope:** ✅ Acknowledged. ~30–40 new tests expected. Planned alongside each step, not deferred.

---

### `ITERATE_PLAN` Constraint Compaction Scheme

**Trigger:** When the active constraint count reaches **5**, before the next Blueprint re-generation, Nexus-Prime runs a compaction pass.

**Compaction algorithm:**
```
1. COLLECT: Pull all N active constraints from Blueprint state
   e.g.:
     C1: "Make the tone more professional"
     C2: "The loyalty program must be invite-only"
     C3: "Budget cap is $5,000"
     C4: "No automated discounts — concierge style only"
     C5: "Include a referral component"

2. SUMMARIZE: Single Gemini Flash call (cheap, not Pro):
   Prompt: "Compress these {N} design constraints into one concise
            'Design Constraints' paragraph without losing any requirement."
   Output: "This loyalty program must be invite-only and concierge-style
            (no automated discounts), with a referral component, capped at
            $5,000 budget, written in a professional tone."

3. REPLACE: Active constraint stack → single compacted paragraph
   label: "COMPACTED_CONSTRAINTS (from {N} reviewed comments)"

4. ARCHIVE: All N original constraints written to BigQuery
   table: aos_logs.blueprint_constraints
   fields: blueprint_id, constraint_text, comment_author,
           comment_timestamp, compacted_at
   (Preserved for audit; never deleted)

5. RESUME: Blueprint re-generation runs with compacted paragraph
   instead of N individual lines — prompt length stays bounded
```

**Compaction cap:** Max active constraints before compaction = **5**. After compaction, new constraints accumulate again from 1. A second compaction at 5 new constraints produces "COMPACTED_CONSTRAINTS v2" — prior compacted paragraph is treated as a single constraint in the new batch. Maximum effective prompt growth per Blueprint: ~3 compacted paragraphs regardless of total comment count.

**Cost:** One extra Gemini Flash call per compaction event (~$0.001). Negligible.

---

### Build Sequence (ordered by dependency)

**Sequence tweak applied:** Vertex Search (Step 3) now precedes Google Docs (Step 4) — you can't build an institutional Blueprint Doc before the search layer that populates it exists.

```
Phase 3 additions (Human Interface Layer)
├── ✅ Step 1: tools/google_chat.py + POST /chat + CHAT_MESSAGE + SKILL_REQUEST  [DONE — commit 551f0ca]
│            └── 25 tests passing. Chat API v1, ADC auth, send_message/send_card/send_approval_card/
│                send_skill_import_card/parse_chat_event. POST /chat routes CARD_CLICKED →
│                APPROVAL_RESULT or SKILL_REQUEST; MESSAGE → CHAT_MESSAGE; lifecycle → ACK.
├── ✅ Step 2: POST /daily-sync + handle_daily_sync() + DAILY_SYNC + daily-kickoff scheduler job  [DONE — commit ed6140b]
│            └── 13 tests passing. Queries Logs/Error Logs/Agent_Approvals (24 h window), composes
│                Chat Card v2 briefing, sends to settings.chat.owner_space. ChatConfig added
│                to config/__init__.py + settings.yaml.template. NOTE: separate endpoint
│                /daily-sync (not /sync) — docs corrected in commit a988dc6.
├── ✅ Step 3 ► DONE: tools/vertex_search.py + Playbook schema + write_playbook ritual in all orchestrators
│            └── 22 tests passing. search_knowledge, query_playbooks, query_domain_knowledge. VertexSearchConfig
│                added to config/__init__.py + settings.yaml.template. NOTE: write_playbook in orchestrators
│                deferred to Phase 3 (Approval Gate) when the Playbook Drive folder is live.
└── ✅ Step 4 ► DONE: tools/google_docs.py (Docs API wrapper — create, read, append, list comments)
             └── 29 tests passing. create_document, read_document, append_content, list_comments. DocsConfig
                 added to config/__init__.py + settings.yaml.template. §15 + §16 added to GAOS-Tools-Spec.md.

Phase 3.5 additions (Innovation Interface Layer)
├── ✅ Step 5: Vision workflow — Project_Incubator tab + VISION_SUBMITTED + PLAN_REVIEW +  [DONE — iterate_plan, _run_compaction, handle_poll_comments]
│            │   COMMENT_RECEIVED + Blueprint Doc generator in Nexus-Prime +
│            │   ITERATE_PLAN node + Doc comment polling (Cloud Scheduler 5-min job)
│            └── ✅ DONE: 30 tests passing. vision_blueprint node, iterate_plan node,
│                _run_compaction, handle_poll_comments. POST /vision + /poll-comments.
│                NexusPrimeWorkingMemory: active_blueprints, blueprint_constraints fields.
│                Route table updated. build_nexus_prime_graph() updated.
│                Depends on: Steps 1, 3, 4
├── ✅ Step 6: tools/google_search.py + RESEARCH_MANDATE + Scout _discover node (recursive loop)
│            │   + Step 6.1: KNOWLEDGE_INJECTION — Scout writes Market Intelligence tags
│            │               to Vertex AI Memory Bank (Layer 4) after every discovery loop
│            └── ✅ DONE: 24 tests passing. _discover (recursive Google Custom Search, max depth 3,
│                        max 15 queries), _inject_knowledge (KNOWLEDGE_INJECTION publish +
│                        Section E Blueprint Doc append), _route_after_boot (RESEARCH_MANDATE routing),
│                        GoogleSearchConfig in config/__init__.py + settings.yaml.template,
│                        tools/google_search.py (search, research_topic, GoogleSearchError),
│                        _initial_state() updated for Pub/Sub envelope decoding.
│                        Depends on: Step 5 (injects into Blueprint Section E)
└── Step 7: AppSheet app (no backend code — config only, wires to Project_Incubator tab)
             └── Depends on: Step 5 (tab must exist first)

Phase 5 additions (Grafana Live Dashboard — Sheets → BQ Staging Sync)
├── Step 8.1: scripts/create_staging_tables.py — run-once DDL; CREATE TABLE IF NOT EXISTS for
│            4 new BQ staging tables in aos_logs: staging_approvals, staging_logs,
│            staging_errors, staging_pending_knowledge. All columns STRING + synced_at TIMESTAMP.
├── Step 8.2: replace_rows(table_ref, rows, project_id) in tools/bigquery.py
│            DELETE FROM WHERE TRUE → streaming insert. Raises BigQueryInsertError on DELETE fail.
│            Empty rows = clear table only. Covered by new tests in tests/test_sheets_sync.py.
├── Step 8.3: handle_sheets_sync(project_id) in agents/nexus_prime/orchestrator.py
│            Loops SYNC_TABS (4 pairs). Per-tab failures non-fatal (WARNING + continue).
│            Normalizes headers (lowercase, spaces → underscores). Adds synced_at timestamp.
│            Returns row counts dict.
├── Step 8.4: POST /sheets-sync in main.py
│            Nexus-Prime only. Follows /archive pattern. Update module docstring.
├── Step 8.5: gaas-sheets-sync Cloud Scheduler job in scripts/provision_schedulers.py
│            Schedule: */5 * * * * (every 5 min). Path: /sheets-sync. Cost: +$0.10/month.
├── Step 8.6: 5 new panels in dashboard/grafana/dashboards/ceo-overview.json (IDs 10–14)
│            Live Approval Queue (table), Pending Approvals stat, Pending Knowledge stat,
│            Live Log Feed (last 50), Live Error Feed (last 20). Datasource: bigquery-morphic-gaos.
│            Existing panels untouched.
└── Step 8.7: tests/test_sheets_sync.py (new file) — 5 tests: SS1 (happy path, 4 tabs),
             SS2 (TabNotFoundError non-fatal), SS3 (BQ failure non-fatal + continue),
             SS4 (empty rows), SS5 (header normalization).
             Mock boundaries: get_all_records and replace_rows — not the BQ SDK directly.
```

---

### Full Component List

| Step | New File / Component | Type | Cost Impact | Status |
|---|---|---|---|---|
| 1 | `tools/google_chat.py` | Python tool | $0 — Workspace included | ✅ |
| 1 | `POST /chat` in `main.py` | Endpoint | $0 | ✅ |
| 1 | `CHAT_MESSAGE` in `MessageType` enum | Enum value | $0 | ✅ |
| 1 | Interactive Approve/Reject cards | Chat API feature | $0 | ✅ |
| 1 | **`SKILL_REQUEST` in `MessageType` enum** | Enum value | $0 | ✅ |
| 1 | **Skill Import Chat card** ("I need library X — Approve?") | Chat API feature | $0 | ✅ |
| 2 | `POST /daily-sync` in `main.py` | Endpoint | $0 | ✅ |
| 2 | `handle_daily_sync()` in nexus_prime orchestrator | Logic | $0 | ✅ |
| 2 | `ChatConfig` + `chat.owner_space` in settings | Config | $0 | ✅ |
| 2 | `DAILY_SYNC` in `MessageType` enum | Enum value | $0 | ✅ |
| 2 | `daily-kickoff` Cloud Scheduler job | GCP resource | $0 (3rd free job) | ⏳ wire in GCP |
| 3 | `tools/vertex_search.py` | Python tool | $0 (≤1,000 queries/month free) | ✅ |
| 3 | Playbook Markdown schema | Template | $0 | ✅ |
| 3 | `write_playbook` node in all 6 orchestrators | Logic change | $0 | ⏳ Phase 3 |
| 3 | 2× Vertex AI Search datastores | GCP resource | $0 free tier | ⏳ wire in GCP |
| 4 | `tools/google_docs.py` | Python tool | $0 — Workspace included | ✅ |
| 5 | `Project_Incubator` Sheet tab | Sheet tab | $0 | ✅ |
| 5 | `VISION_SUBMITTED` + `PLAN_REVIEW` + `COMMENT_RECEIVED` in `MessageType` | Enum values | $0 | ✅ |
| 5 | Blueprint Doc generator in Nexus-Prime | Logic | Minor LLM cost (Gemini Pro call per Blueprint) | ✅ |
| 5 | **`ITERATE_PLAN` node in Nexus-Prime LangGraph** | Logic | Minor LLM cost (re-run on each comment batch) | ✅ |
| 5 | Doc comment polling Scheduler job | GCP resource | $0 (4th job = $0.10/month) | ⏳ wire in GCP |
| 6 | `tools/google_search.py` | Python tool | $0 free tier (≤100 queries/day) | ✅ |
| 6 | `RESEARCH_MANDATE` in `MessageType` | Enum value | $0 | ✅ |
| 6 | `_discover` node in Scout LangGraph | Logic change | Minor cost (~$0.13/mandate max) | ✅ |
| 6 | **Step 6.1: `KNOWLEDGE_INJECTION` — Scout writes Market Intelligence to Memory Bank** | Logic | Minor Memory Bank write cost | ✅ |
| 6 | `SCOUT_MAX_SEARCH_DEPTH`, `SCOUT_MAX_QUERIES_PER_MANDATE` in `settings.yaml` | Config | $0 | ✅ |
| 6 | `GOOGLE_SEARCH_API_KEY`, `GOOGLE_SEARCH_CX` secrets | Secret Manager | $0 | ⏳ wire in GCP |
| 7 | AppSheet app | No-code config | $0 — Workspace included | — |
| 8.1 | `scripts/create_staging_tables.py` | Run-once DDL script | $0 | ⏳ |
| 8.2 | `replace_rows()` in `tools/bigquery.py` | Function | $0 | ⏳ |
| 8.3 | `handle_sheets_sync()` in nexus_prime orchestrator | Handler | $0 | ⏳ |
| 8.4 | `POST /sheets-sync` in `main.py` | Endpoint | $0 | ⏳ |
| 8.5 | `gaos-sheets-sync` Cloud Scheduler job | GCP resource | $0.10/month | ⏳ |
| 8.6 | 5 new Grafana panels in `ceo-overview.json` | Dashboard | $0 | ⏳ |
| 8.7 | `tests/test_sheets_sync.py` | 5 tests | $0 | ⏳ |

**Revised monthly cost estimate:** ~$2.55–$2.65/month (adds one $0.10 Cloud Scheduler job + occasional Blueprint LLM calls; well within target).

---

### Spec Documents That Need Updating

| Document | What Changes |
|---|---|
| `GAOS-Manager-Spec.md` | New Section: Google Chat Interface; update Section 2 (Dashboard); update Section 17 checklist; add Phase 3 + Phase 3.5 items to Section 16 roadmap; update cost table |
| `GAOS-Memory-Spec.md` | Add Layer 5b: Vertex AI Search (retrieval layer over Drive); add Playbook schema; add `write_playbook` to orchestrator completion requirements |
| `GAOS-Deploy-Spec.md` | Add: Chat app registration steps, Vertex AI Search datastore provisioning, Google Custom Search Engine setup, AppSheet connection steps, new Scheduler jobs |
| `models/__init__.py` | Add new `MessageType` values: `CHAT_MESSAGE`, `DAILY_SYNC`, `VISION_SUBMITTED`, `PLAN_REVIEW`, `COMMENT_RECEIVED`, `RESEARCH_MANDATE`, `SKILL_REQUEST`, `KNOWLEDGE_INJECTION` |
| `Docs/agents/*.md` | Add `write_playbook` to completion requirements for all 6 orchestrators |
| New: `tools/google_chat.py` | — |
| New: `tools/google_docs.py` | — |
| New: `tools/vertex_search.py` | — |
| New: `tools/google_search.py` | — |
| `GAOS-Manager-Spec.md` | Add Phase 5 (Grafana live sync); add `POST /sheets-sync` endpoint; update cost table (+$0.10/month) |
| `GAOS-Deploy-Spec.md` | Add `gaos-sheets-sync` Scheduler job step; add `create_staging_tables.py` run-once step; add Phase 5 exit checklist items |
| New: `scripts/create_staging_tables.py` | — |
| New: `tests/test_sheets_sync.py` | — |

---

### Three Missing "Connective Tissue" Items

#### Item A: `KNOWLEDGE_INJECTION` — Scout → Memory Bank Bridge (Step 6.1)

**What's missing:** After Scout's recursive discovery loop completes, the findings currently only flow into the Blueprint Doc. They need to also be written to **Vertex AI Memory Bank (Layer 4)** as tagged Market Intelligence entries so future projects in the same category automatically retrieve them — without Scout having to re-run the search.

**The mechanism:**
- After `_discover` node completes, Scout calls a new `_inject_knowledge` node
- Findings are structured as `MemoryEntry` records with `knowledge_type = "pattern"` and tags matching the mandate topic (e.g., `["loyalty-program", "competitor-intel", "2026"]`)
- These are submitted as high-confidence observations (confidence = 0.80, evidence = current mandate ID) — bypassing the 5-corroboration threshold since they represent fresh external research, not internal pattern detection
- Standard approval gate still applies before they reach permanent Layer 4 storage
- **Result:** Next Vision on a similar topic → Nexus-Prime's boot-time memory load already contains Scout's prior findings — no re-research needed

**New component:** `KNOWLEDGE_INJECTION` `MessageType` — Scout publishes this to Nexus-Prime after each mandate, containing structured memory candidates for review and approval.

#### Item B: `ITERATE_PLAN` Node — Blueprint Feedback Loop (Step 5.1)

**What's missing:** The comment polling job detects new Doc comments, but there's no defined logic for *what Nexus-Prime does with them*. Simply re-reading the Doc is not enough — the comments must be parsed as **constraints** and fed back into the Blueprint Generator as updated system instructions.

**The mechanism:**
```
Doc comment polling fires (every 5 min)
    → New unresolved comment found (e.g., "Make the tone more professional")
    → Pub/Sub publishes COMMENT_RECEIVED to Nexus-Prime
    → Nexus-Prime enters ITERATE_PLAN node:
          1. Retrieve original Blueprint prompt + all prior constraints
          2. Append new comment as an additional constraint:
             "CONSTRAINT: Make the tone more professional"
          3. Re-run Blueprint Generator (Gemini Pro call) with full constraint stack
          4. Update the Google Doc in-place (append revised sections, mark old ones as [REVISED])
          5. Mark comment as resolved in Docs API
          6. Update Project_Incubator row status → 'Blueprint Iterating'
    → Owner sees updated Doc in ~5 minutes
```

**Important:** Each iteration appends constraints — it does NOT discard prior constraints. The constraint stack is cumulative. This prevents the agent from "forgetting" earlier feedback when processing new comments.

**New component:** `ITERATE_PLAN` node in Nexus-Prime's LangGraph `StateGraph`. Sits between the `plan` node and the `dispatch` node — only reachable while a Blueprint is in active review.

#### Item C: `SKILL_REQUEST` — Library Installation Approval Card (Step 1 addition)

**What's missing:** The Write-Test-Refine loop (Section 13) handles code generation and sandbox testing, but if the agent's generated code requires a Python library not currently in `pyproject.toml`, the loop currently has no clean escalation path for *package installation* specifically. It would fail at import, hit the error fingerprint, and escalate as a generic failure.

**The mechanism:**
- During the Write-Test-Refine loop, if a `ModuleNotFoundError` is caught on a library that is not in the import allowlist, the agent does NOT iterate — it immediately publishes a `SKILL_REQUEST` to Nexus-Prime
- Nexus-Prime sends a specific Chat card to the owner:
  ```
  🔧 Skill Import Request
  Agent: Scout
  Task: Build SEO analysis tool
  Required library: 'seo-master' (v2.1.0)
  Source: PyPI — https://pypi.org/project/seo-master/
  Risk: Unknown library — not on current allowlist.
  [✅ Approve & Add to Allowlist]  [❌ Reject]
  ```
- On approval: library added to `pyproject.toml`, added to import allowlist, Write-Test-Refine loop resumes from the failed iteration
- On rejection: loop logs a hard stop with reason `skill_request_rejected`; escalation written to `Agent_Approvals`

**Security note:** This card must display the exact PyPI URL and version. The owner is approving a specific version, not an open-ended install. The static analysis import gate is updated to include the newly approved library only after owner approval — not before.

**New component:** `SKILL_REQUEST` `MessageType` + Skill Import Chat card template + `approved_libraries` list added to `settings.yaml` as a runtime-updateable allowlist extension.

---

## Topic 1: Google Chat App — Conversational Interface With Nexus-Prime

**Owner's requirement:** A chat box (mobile + desktop) where the owner can have a real conversation with Nexus-Prime. Described as **imperative** — not optional.

**Identified solution:** Google Chat App (Chatbot) — already included in Google Workspace Business, zero additional cost.

**How it works:**
- Owner opens a DM with "Nexus-Prime" in Google Chat (phone or desktop)
- Message is POSTed to `main.py` Cloud Run endpoint via Google Chat event subscription
- `main.py` routes to Nexus-Prime
- Nexus-Prime replies via Chat API — response appears in thread

**New components required:**
- `tools/google_chat.py` — send messages and interactive cards via Chat API
- `POST /chat` endpoint in `main.py` — receives incoming Chat webhook events
- `CHAT_MESSAGE` message type added to `MessageType` enum
- Interactive Approve/Reject button cards on approval proposals (rendered in Chat)
- New spec section: Google Chat as the primary human interface

**Open design decision — not yet resolved:**
> Should the Sheet approval tab stay as **primary** (Chat = convenience alias), or should Chat become **primary** (Sheet = read-only audit trail)?
> This determines whether the Apps Script `onChange` trigger remains the approval source of truth.

---

## Topic 2: AppSheet + The Full "Innovation Interface" / "Vision to Blueprint" Workflow

**Owner's elaborated requirement:** A complete end-to-end workflow that takes a high-level "Vision" (not a task) and turns it into an approved, institutionally-informed project plan before any execution begins.

### The Full Workflow

```
Owner enters Vision (AppSheet form or Chat)
       ↓
Written to Project_Incubator Sheet tab
       ↓
Pub/Sub → Nexus-Prime wakes up
       ↓
Nexus-Prime runs Vertex AI Search over Drive Playbooks (Look-Back)
       ↓
Nexus-Prime creates Google Docs Blueprint:
   Section A: Goal (from Vision input)
   Section B: Institutional Context (from Search results)
   Section C: Proposed Agent Stack (which Tier 2 agents are needed)
   Section D: Approval Table (specific actions needing permission)
       ↓
Link sent to owner (via Chat or email)
       ↓
Owner reads, comments, edits in the Doc  ← COLLABORATIVE LOOP
       ↓
Nexus-Prime watches for comments → iterates Blueprint
       ↓
Owner signals approval ("Go" in Chat, or flags Doc as approved)
       ↓
Normal execution begins — agents dispatched, Approval Gate governs code
       ↓
Project completes → Foreman writes Playbook Doc to Drive → indexed by Vertex AI Search
```

### What's Genuinely New (Not Yet in Spec)

| New Component | Description |
|---|---|
| `Project_Incubator` Sheet tab | New tab — captures Vision inputs. Columns: Vision, Project Type, Owner Notes, Status (`Incubating`/`Blueprint Ready`/`Approved`/`Active`) |
| Vision vs Task distinction | Current spec only handles Tasks. Visions are upstream — higher-level, unstructured, no immediate execution |
| `VISION_SUBMITTED` message type | New `MessageType` enum value — triggers the Blueprint workflow in Nexus-Prime |
| `tools/google_docs.py` | Create/append/read/list-comments on Google Docs via Docs API |
| Blueprint Doc creation | Nexus-Prime generates a structured Google Doc from Vision + Search results |
| **Google Doc comment watching** | Nexus-Prime polls for new comments on an active Blueprint Doc and reacts to them — **this requires polling** (Docs API has no push/webhook for comments). Polling interval: ~5 min via Cloud Scheduler. |
| Playbook ritual | Foreman (and other orchestrators) write a structured Playbook Doc to Drive on project completion |
| `tools/vertex_search.py` | Query Vertex AI Search over a Drive corpus (see Topic 3) |
| Audit trail | Each Blueprint Doc itself becomes the next Lesson Learned — self-reinforcing loop |

### AppSheet vs Google Chat — They Are NOT Mutually Exclusive

| Surface | Best For |
|---|---|
| **AppSheet** | Structured Vision capture: dropdowns (Project Type), photo uploads (inspiration images), voice-to-text while driving, offline drafting |
| **Google Chat** | Freeform conversation, quick idea capture, approval signals ("Go"), status questions |

Both write to `Project_Incubator`. AppSheet is the rich mobile form; Chat is the quick conversational entry. **Recommendation: support both input paths to the same tab.**

### Technical Considerations

- **Google Docs comment watching:** No push API exists for Doc comments. Must poll via Docs API `comments.list` endpoint. Suggested: Cloud Scheduler fires every 5 minutes, checks all Docs with status `Blueprint Active`, publishes a `COMMENT_RECEIVED` event to Pub/Sub if new comments found.
- **AppSheet inclusion in Workspace Business:** ✅ Confirmed — included at no extra cost.
- **Docs API quota:** 300 requests/min per project — polling 5-min intervals across a handful of active Blueprint Docs is well within limits.

**Status: NEW — high value. Spans Topics 2, 3, and 4. See also Topic 3 (Vertex AI Search) and Topic 4 (Google Docs) for component details.**

---

## Topic 3: Vertex AI Search — The "Hippocampus" / Institutional Knowledge Engine

**Owner's elaborated requirement:** Transform Google Drive from a "digital attic" into a searchable institutional brain. Semantic search over Drive Playbooks/docs, automatically indexed, queried by agents before any new project begins. Context injection — agent finds *your* successful past examples and injects them into its prompt before acting.

### How It Fits the Existing Memory Architecture

The spec already defines a 5-layer memory model in `GAOS-Memory-Spec.md`. **Layer 5 (Procedural / Google Drive) is the gap:**

```
Layer 1 — Working Memory       LangGraph state          ✅ Implemented
Layer 2 — Episodic Memory      BigQuery                 ✅ Implemented
Layer 3 — Observation Buffer   Sheets (Pending tab)     ✅ Implemented
Layer 4 — Semantic Memory      Vertex AI Memory Bank    ✅ Implemented
Layer 5 — Procedural Knowledge Google Drive (Markdown)  ⚠️  WRITE-ONLY — no search tool
                                                              ← Vertex AI Search fills this gap
```

Agents write Markdown procedures to Drive today — but no agent can efficiently **search** that content. Vertex AI Search adds the retrieval layer that makes Layer 5 actually useful at runtime.

### The Automated Librarian Workflow

```
Project closes
    → Foreman triggers Post-Mortem task
    → Agent writes Playbook Markdown to Drive: Knowledge/Playbooks/<project_id>_<date>.md
    → Vertex AI Search crawler indexes it (hourly crawl or Drive change notification)

New project starts
    → Scout's first "thought" node: query Vertex AI Search
      "Retrieve all Playbooks related to [topic]"
    → Top results injected into Nexus-Prime's prompt BEFORE any planning begins
    → Nexus-Prime builds Blueprint with institutional context already embedded
```

### Per-Agent Search Scope

| Agent | Search Domain | What It Looks For |
|---|---|---|
| **Nexus-Prime** | Full corpus (all domains) | Cross-project patterns; anything relevant to a new Vision |
| **Ledger** | `Knowledge/Accounting/` | Past tax filing patterns, expense categorization precedents |
| **Beacon** | `Knowledge/Marketing/` | Past campaign results, highest-performing headlines/emails |
| **Pursuit** | `Knowledge/Sales/` | Deal patterns, pricing history, client preferences |
| **Foreman** | `Knowledge/Operations/` | Vendor handling, fulfillment patterns |
| **Steward** | `Knowledge/Admin/` | Scheduling preferences, compliance precedents |
| **Scout** | Full corpus (research) | Any prior research on the given topic |

**Data Scoping note:** The spec already has per-project Drive folder isolation (`drive_folder_id` in Project Registry). Two Vertex AI Search datastores should be created:
- `aos-public-knowledge` — general business playbooks (all agents can read)
- `aos-restricted-knowledge` — private financial/strategic docs (Nexus-Prime only)

### What's Genuinely New (Not Yet in Spec)

| New Component | Description |
|---|---|
| `tools/vertex_search.py` | Query Vertex AI Search datastores; returns ranked document snippets with source URLs |
| Playbook schema | Standardized Markdown template every orchestrator must use at project close: Goal, Outcome, What Worked, What Failed, Owner Preferences Observed, Tags |
| Playbook-writing step | New mandatory node in orchestrator completion flow: `write_playbook` → Drive before closing any project |
| Context injection pattern | Standard pattern for prepending search results to LLM prompt: `[INSTITUTIONAL CONTEXT]\n{results}\n[END CONTEXT]\n{original_prompt}` |
| Scout search node | New first node in Scout's `plan` phase: always query Vertex AI Search before any web search |
| Two Vertex AI Search datastores | `aos-public-knowledge` and `aos-restricted-knowledge` (setup in Deploy Spec) |

### Cost

- Vertex AI Search (Discovery Engine): **first 1,000 queries/month free**
- For a single-operator setup running a few projects/week: estimated **~50–100 queries/month** — stays in free tier indefinitely
- Drive crawl: free (uses Drive API, already authorized)

### Integration Point With Topic 2 (Blueprint)

Context injection is what makes the Blueprint Doc (Topic 2) *institutional* rather than generic. The flow is:

```
Vision submitted → Nexus-Prime searches Drive (Topic 3)
                 → Injects results into Blueprint generator (Topic 4)
                 → Blueprint Section B: "Institutional Context" is populated automatically
```

Without Vertex AI Search, Blueprint Section B would be empty. Topics 2, 3, and 4 are tightly coupled — they should be spec'd and built as one workflow unit.

**Status: NEW — fills the Layer 5 retrieval gap. Required for the Innovation Interface to work. Tightly coupled with Topics 2 and 4.**

---

## Topic 4: Google Docs Dynamic Templates — Strategy Doc / Blueprint Factory

**Owner's idea:** When a vague idea comes in, Nexus-Prime creates a "Strategy Doc" in Google Docs — a step-by-step plan written in plain language — before any execution starts. Owner reads it, comments, adjusts. Agent doesn't start until the plan is approved.

**Analysis vs current spec:**

The current spec has two approval surfaces:
- `Agent_Approvals` Sheet tab — designed for code patches and risky actions (structured row)
- `onChange` trigger — fires when a Sheet status dropdown changes

**What's missing:** There is no concept of a **planning artifact** — a human-readable Google Doc that lays out a project plan and waits for narrative feedback/comment before execution begins. This is different from the approval gate (which approves specific code) — this is upstream of that, at the project design level.

**What's genuinely new here:**
- A new `PLAN_REVIEW` message type in the `MessageType` enum
- `tools/google_docs.py` — create/append/read Google Docs via Docs API
- A "Strategy Doc" step in Nexus-Prime's project kickoff flow: idea received → Doc created → owner reviews → approval triggers execution
- This fits naturally as a **higher-priority gate** upstream of the Approval Gate — plan approval before any tool use begins

**Cost:** Google Docs API is included in Workspace — $0.

**Status: NEW — needs spec work. High value. Connects directly to the Chat interface (Topic 1): owner submits idea in Chat → Nexus-Prime creates Strategy Doc → sends link back in Chat: "Here's my plan. Review and reply 'Go' to start."**

---

## Topic 5: Vertex AI Workbench — The "Skill Forge" / Lab

**Owner's idea:** A "Test Kitchen" / coding lab where agents experiment with new tools before touching production. Safe sandbox for building skills for new projects.

**Analysis vs current spec:**

**This is already fully spec'd** — Section 13 defines the entire Write-Test-Refine loop using **Vertex AI Code Execution Sandbox**. Agents write Python, run it in the secure sandbox, iterate up to 5 times, then propose via the Approval Gate. The sandbox is network-isolated by Google.

**Vertex AI Workbench** (the owner's term) is a different Google product — it's a managed Jupyter notebook environment for data scientists. We are NOT using Workbench; we're using Vertex AI Code Execution, which is the right choice for an automated agent loop.

**What the owner is describing IS already the spec.** The sandbox metaphor ("test kitchen") matches exactly.

**Action:** None needed on the technical side. The spec language can be updated to use the "Test Kitchen" / "Skill Forge" metaphor in layman's descriptions so the owner recognizes it as the same thing.

**Status: ALREADY IMPLEMENTED in spec (Section 13). No new components needed.**

---

## Topic 6: Pub/Sub — The "Nervous System" / Intercom

**Owner's idea:** Agents communicate instantly via Pub/Sub so approvals trigger immediate action across all agents.

**Analysis vs current spec:**

**This is already fully built and deployed.** Pub/Sub is the backbone of the entire A2A communication layer (Section 10). The `tools/pubsub.py` tool exists, all 7 orchestrators use it, and `main.py` has the Cloud Run push endpoint. It is the PA system metaphor the owner described.

**Status: ALREADY IMPLEMENTED — fully operational. No changes needed.**

## Topic 7: Recursive "Depth-First" Web Search — Scout's Discovery Engine

**Owner's requirement:** Scout doesn't just do one search and stop. It does a broad sweep, then dives deep on each result, then re-triggers itself when it discovers unknown terms/competitors. Live market intelligence injected into the Blueprint Doc under a "Market Intelligence" section.

### What's Already Built

`tools/web_search.py` **already exists** — built in Phase 2 Item 1. It uses the DuckDuckGo Instant Answer API (no API key, no cost). It returns a snippet string for context injection into Ollama prompts.

**However**, DuckDuckGo Instant Answer has significant limitations for this use case:
- Returns only the "Instant Answer" box + related topics — not full web search results
- No pagination, no depth, no structured competitive data
- Fine for simple context injection; **not sufficient for competitive intelligence**

### Google Search API vs DuckDuckGo — The Real Question

| | DuckDuckGo (current) | Google Search API (Custom Search JSON API) |
|---|---|---|
| **Cost** | Free | Free tier: 100 queries/day; $5 per 1,000 after that |
| **Result depth** | Instant Answer only | Full 10-result pages, pagination, site: filtering |
| **Structured data** | No | Yes — title, snippet, URL, metadata per result |
| **Recursive loops** | Too limited | Viable — each result URL can seed next query |
| **For competitive intel** | Insufficient | Correct tool |

**Recommendation:** Keep DuckDuckGo for quick Ollama context injection (already works). Add Google Custom Search JSON API for Scout's structured research and recursive loops. Two tools, two purposes.

### The Recursive Loop Design

```
Scout receives Discovery Mandate (from Nexus-Prime, rooted in a Vision)
    ↓
Step A — Broad Sweep: search("top 5 competitors in [niche]")
    → Returns 5 competitor names + URLs
    ↓
Step B — Deep Dive loop (for each competitor):
    search("[competitor] pricing 2026")
    search("[competitor] customer complaints")
    search("[competitor] marketing strategy")
    ↓
Step C — Gap Analysis:
    Compare all findings against Vertex AI Search results (institutional knowledge)
    LLM synthesizes: "3 things competitors do wrong that we can exploit"
    ↓
Step D — Unknown Unknown Detection:
    Any new term found that wasn't in the original query → appended to search queue
    Loop re-triggers with new term (max depth: configurable, default: 2 levels)
    ↓
Step E — Blueprint injection:
    Adds "Market Intelligence" section to Blueprint Doc:
      - Option A: Standard approach
      - Option B: Innovative approach based on discovered gap
```

### Loop Safety Constraints

The recursive loop needs the same hard stops as the Write-Test-Refine loop — an unbounded search loop is a cost and time risk:

| Constraint | Limit |
|---|---|
| Max recursion depth | 2 levels by default (configurable in `settings.yaml`) |
| Max total queries per mandate | 25 queries (prevents runaway cost) |
| Time-to-live | 10 minutes (fits Cloud Run 60-min ceiling; most mandates complete in 2–3 min) |
| Cost cap | $0.13 (25 queries × $5/1,000 = $0.125, rounds to ~$0.13) |

At 100 free queries/day, typical usage (2–3 Visions/week × ~15 queries each) stays in the free tier.

### What's Genuinely New (Not Yet in Spec)

| New Component | Description |
|---|---|
| `tools/google_search.py` | Google Custom Search JSON API wrapper — structured results, pagination, site: filtering |
| `RESEARCH_MANDATE` message type | New `MessageType` — Nexus-Prime sends this to Scout for Innovation projects; distinct from routine `TASK_HANDOFF` |
| Recursive search loop in Scout | New `_discover` node in Scout's LangGraph StateGraph — implements depth-first loop with hard stops |
| Market Intelligence section | Blueprint Doc Section E: competitive options (Option A / Option B) added by Scout |
| `settings.yaml` additions | `SCOUT_MAX_SEARCH_DEPTH`, `SCOUT_MAX_QUERIES_PER_MANDATE` |
| Google Custom Search Engine ID + API key | New secret: `GOOGLE_SEARCH_API_KEY`, `GOOGLE_SEARCH_CX` in Secret Manager |

### Relationship to Existing `tools/web_search.py`

`web_search.py` (DuckDuckGo) stays exactly as-is — it serves a different purpose (Ollama context injection, no key needed). `tools/google_search.py` is a separate, richer tool used only by Scout for structured research mandates.

**Status: NEW — extends Scout significantly. Requires new tool, new message type, new LangGraph node, and Cloud Scheduler or Pub/Sub trigger for the recursive loop. Tightly coupled with Topic 2 (Blueprint Doc — Market Intelligence section).**

---

## Topic 8: The Proactive Sync Loop — Daily Business Heartbeat

**Owner's requirement:** A 5-step autonomous cycle — Wake Up → Remember → Do Work → Fix Problems → Get Permission — that runs even when the owner doesn't touch the system. Described as running on Vertex AI Agent Engine.

### What's Already Fully Spec'd (Steps 2–5 Are Done)

| Step | Owner's Description | Spec Status |
|---|---|---|
| Wake Up | Cloud Scheduler trigger | ⚠️ Partial — see below |
| Remember | Memory Bank morning audit | ✅ Section 12 — boot-time memory load already defined |
| Do Work | A2A task delegation via Pub/Sub | ✅ Section 10 — fully built and operational |
| Fix Problems | Vertex AI Sandbox / Write-Test-Refine | ✅ Section 13 — fully spec'd with all hard stops |
| Get Permission | Approval Gate | ✅ Section 14 — fully spec'd and built |

### What's Actually New: A Dedicated Daily Kickoff Job

The spec currently defines **2 Cloud Scheduler jobs**:
- `ttl-sweep` — hourly, scans stale approval proposals
- `nightly-archive` — 2:00 AM daily, archives aged Sheet rows to BigQuery

**What's missing:** A dedicated **daily proactive kickoff** that tells Nexus-Prime "business is open, run your morning audit and dispatch agents for the day." The spec mentions "Proactive Trigger | Cloud Scheduler + Cloud Run" as a component but never defines the actual job, schedule, or handler.

**New component needed:** `daily-kickoff` Cloud Scheduler job → `POST /sync` endpoint on Nexus-Prime Cloud Run → Nexus-Prime loads memory, checks unresolved tasks, dispatches each Tier 2 orchestrator with a `BROADCAST` message to start their day.

### ⚠️ Critical Cost Flag: Vertex AI Agent Engine Is NOT the Runtime

The owner described this loop running on **Vertex AI Agent Engine**. This is explicitly excluded from Phase 1–4 in the spec:

> *"Vertex AI Agent Engine is explicitly excluded from Phase 1–4 to stay within budget."*
> Cost: **$50–$135/month per always-on container** — would immediately blow the $2.50/month target.

**The Sync Loop runs on Cloud Run (scale-to-zero), not Agent Engine.** Cloud Scheduler POSTs to the Cloud Run endpoint, which wakes up, runs the sync, and scales back to zero. Cost: effectively $0 (well within the 2M free invocations/month on Cloud Run).

Vertex AI Agent Engine remains the **Phase 5+ upgrade path** — only relevant if always-on, sub-second cold-start response becomes necessary, and only after the business value justifies the cost. That decision goes through the Approval Gate as a Priority-4 proposal.

### What's Genuinely New (Not Yet in Spec)

| New Component | Description |
|---|---|
| `daily-kickoff` Cloud Scheduler job | `0 6 * * *` (6:00 AM daily) → `POST /sync` on Nexus-Prime Cloud Run |
| `POST /sync` endpoint in `main.py` | Auth-gated (scheduler SA only); triggers Nexus-Prime morning audit + dispatch |
| `DAILY_SYNC` message type | New `MessageType` enum value — Nexus-Prime sends this to each Tier 2 orchestrator to begin their day |
| Morning audit sequence in Nexus-Prime | Load memory → check unresolved tasks → check parked proposals → dispatch Tier 2 agents → send Chat summary to owner |
| Chat "Morning Briefing" message | After daily sync, Nexus-Prime sends a plain-language summary to owner via Google Chat: what's pending, what's running today, any escalations needed |
| Cost budget update | Cloud Scheduler count goes from 2 → 3 jobs (still in free tier — first 3 jobs are free) |

### The "Morning Briefing" Chat Message (Ties Topics 1 + 8 Together)

```
6:00 AM — Cloud Scheduler fires
    → Nexus-Prime runs morning audit
    → Dispatches all Tier 2 agents
    → Sends owner a Chat message:

"☀️ Good morning. Here's your daily briefing:
• 2 approval requests pending (Beacon wants to launch Tuesday campaign, Ledger needs PO sign-off)
• Pursuit has 3 new leads from yesterday — scoring in progress
• Scout running research mandate: 'AI loyalty programs' (started 6:02 AM)
• No errors overnight.
Reply 'status' anytime for an update."
```

**Status: MOSTLY ALREADY BUILT — the components exist. What's new is: (1) `daily-kickoff` scheduler job + `POST /sync` endpoint, (2) `DAILY_SYNC` message type, (3) morning Chat briefing. Agent Engine is NOT part of this — stays on Cloud Run.**

---

## Topic 9: Grafana Live Operational Dashboard — Sheets → BQ Staging Sync

**Problem:** Grafana already deployed (Phase 5 complete). Gap: Grafana queries BigQuery, which is only refreshed at 2 AM by `handle_archive()`. Live operational data (pending approvals, active logs, errors, pending knowledge) is in Google Sheets and is up to 24 hours stale in the dashboard.

**Solution:** A new `POST /sheets-sync` endpoint in Nexus-Prime reads 4 operational Sheet tabs and does a full-replace into 4 new BigQuery staging tables every 5 minutes. Grafana gets 5 new panels that query the staging tables instead of the nightly archive tables.

### Root Cause of Staleness

```
Grafana queries BigQuery (aos_logs.approval_history)
    ↑
BigQuery only updated at 2 AM by handle_archive()
    ↑ 24-hour gap
Live data lives in Google Sheets (Agent_Approvals, Logs, Error Logs, Pending_Knowledge)
```

### Solution Architecture

```
Cloud Scheduler */5 * * * *
    → POST /sheets-sync (Nexus-Prime Cloud Run)
    → handle_sheets_sync(project_id):
          get_all_records("Agent_Approvals") → replace_rows(staging_approvals)
          get_all_records("Logs")             → replace_rows(staging_logs)
          get_all_records("Error Logs")       → replace_rows(staging_errors)
          get_all_records("Pending_Knowledge")→ replace_rows(staging_pending_knowledge)
    → Grafana queries staging_* tables
    → Live data lag: ≤ 5 minutes
```

### Staging Tables (new, dataset: aos_logs)

| Table | Source Tab | Key Columns |
|---|---|---|
| `staging_approvals` | `Agent_Approvals` | All headers verbatim (STRING) + `synced_at TIMESTAMP` |
| `staging_logs` | `Logs` | All headers + `synced_at TIMESTAMP` |
| `staging_errors` | `Error Logs` | All headers + `synced_at TIMESTAMP` |
| `staging_pending_knowledge` | `Pending_Knowledge` | All headers + `synced_at TIMESTAMP` |

All columns STRING (avoids schema drift if Sheet columns change). No partition key — tables are tiny, always full-replace.

### `replace_rows()` — New BQ Primitive

No `replace_rows()` exists in `tools/bigquery.py` — only `insert_row()`, `insert_rows()`, `query_rows()`. New function:
- `DELETE FROM table WHERE TRUE` via `client.query().result()` (DML)
- Streaming insert via `insert_rows_json`
- Raises `BigQueryInsertError` on DELETE failure; proceeds to insert only if DELETE succeeded
- If `rows` is empty: runs DELETE (clears table) and returns — no insert
- Full docstring required (Rule 17)

### New Grafana Panels (IDs 10–14)

| Panel ID | Type | Title | Staging Table Queried |
|---|---|---|---|
| 10 | table | Live Approval Queue | `staging_approvals ORDER BY timestamp ASC LIMIT 100` |
| 11 | stat | Pending Approvals (live) | `staging_approvals WHERE LOWER(status) IN ('pending', '')` |
| 12 | stat | Pending Knowledge (live) | `staging_pending_knowledge WHERE LOWER(status) IN ('pending', '')` |
| 13 | table | Live Log Feed (last 50) | `staging_logs ORDER BY timestamp DESC LIMIT 50` |
| 14 | table | Live Error Feed (last 20) | `staging_errors ORDER BY timestamp DESC LIMIT 20` |

Datasource uid `bigquery-morphic-gaos` (existing — no changes to `provisioning/datasources/bigquery.yaml`).

### Cost

- 4th Cloud Scheduler job: **$0.10/month** (first 3 are free)
- Sheets API: 4 reads × 12/hr = 48 calls/hr — well within 300 req/min budget
- BQ DML (DELETE): free for the amounts involved
- **Total impact:** +$0.10/month → revised estimate ~$2.65/month

### Verification Steps

1. `python scripts/create_staging_tables.py` → 4 tables in `morphic-gaos-prod.aos_logs`
2. Force-POST `/sheets-sync` → HTTP 200 with `{"staging_approvals": 5, ...}`
3. `SELECT COUNT(*) FROM morphic-gaos-prod.aos_logs.staging_approvals` in BQ → matches Sheet row count
4. Open Grafana → panels 10–14 populated; existing panels unaffected
5. `python scripts/provision_schedulers.py` → `gaos-sheets-sync` in Scheduler console
6. Wait 10 min → edit Sheet row → Grafana reflects change within 5 min
7. `pytest tests/test_sheets_sync.py` → 5 passing; full pytest suite → 0 failures

**Status: PLANNED — implementation plan complete. Awaiting execution. See `/memories/session/plan.md` for full spec.**

---

## Notes

- Owner has Google Workspace Business account (AppSheet included, Google Chat included)
- Cost target remains ~$2.50/month — all changes must be evaluated against this
- Vertex AI Agent Engine explicitly excluded from Phase 1–4 — $50–$135/month; deferred to Phase 5+
- Phase 5 (Grafana) live dashboard plan complete — 7 build steps designed, not yet implemented. See Topic 9.
- No spec changes will be made until the full picture is captured here

---

## Agent Backlog — Low-Activity Tasks to Automate or Delegate

Captured 2026-04-01. Each row is a task category, the planned resolution, and any agent to build.

| Task | Plan | Agent / Notes |
|------|------|---------------|
| **Filing** | Automate | New agent: receives documents, organizes, files, and indexes them. No other responsibility. |
| **Organizing** | Automate | New agent: general-purpose organizing agent (scope TBD — likely overlaps with Filing agent; evaluate whether one agent covers both). |
| **Admin** | TBD | Scope unclear — needs definition before an agent can be designed. |
| **Bank Deposits** | Automate via Stripe | Stripe API integration — no new agent needed; existing tooling or a Stripe webhook handler. |
| **Cleaning** | Human | Hire housekeeper (daily). Not automatable. |
| **Research** | Automate | Agent to run research queries, aggregate results, and surface summaries for owner review. |
| **Writing Tasks** | Automate | Agent to draft all written content (emails, docs, copy). Owner approval queue. |
| **Edit Website** | Automate | Agent to make website edits from structured instructions. |
| **Inventory Management** | Human PA | Human PA manages physical supplies. Not automatable. |
| **Sending Emails** | Automate (with approval) | Agent drafts all outbound emails. Owner approves via existing approval queue before send. |
| **Marketing Phone Calls** | Automate | Agent handles outbound marketing call setup and scheduling. |
| **Video Editing** | Agent | AI-assisted video editing agent. |
| **Graphics** | AI | AI-generated graphics (no persistent agent needed — trigger on demand). |
| **Shopping** | Agent (with approval) | Agent sources and proposes purchases. Human approves before any transaction. |
| **Data Entry** | Agent | Agent handles all structured data entry tasks. |
| **Cold Calling** | AI | AI-driven outbound cold calling. |
| **Social Media** | Agent | Agent manages posts, scheduling, and engagement drafts. |

**Next step:** Define scope for Admin and Organizing agents, then spec the Filing agent first (clearest scope, highest reuse potential).

---

*Last updated: 2026-04-01*

test_api_key = 'AKIAIOSFODNN7EXAMPLE'
