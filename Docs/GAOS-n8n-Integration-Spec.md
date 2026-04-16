# GAOS n8n Integration Specification

**Document type:** Architecture decision + deployment guide
**Status:** Draft — Option E (BI5: Daily Digital Performance Report) selected as pilot
**Last updated:** 2026-04-16

> **What this document covers:**
> Why n8n exists alongside GAOS, exactly where it fits in the ecosystem, how the two systems
> communicate, the deployment plan, and the criteria for selecting a first pilot workflow.

---

## 1. Decision and Rationale

### 1.1 The Gap n8n Fills

GAOS is built for **depth** — persistent memory, multi-step reasoning, approval gates, structured
logging, GCP-native cost tracking, and human-in-the-loop governance. This architecture makes GAOS
expensive (in development time) to connect to external SaaS tools. Every new third-party integration
requires a Python tool wrapper, a Secret Manager entry, unit tests, and a Cloud Run redeploy. That
overhead is justified when the integration requires reasoning. It is not justified when the workflow
is: *"trigger fires → move data → send a notification."*

The Automation Wish List has roughly 70% of its items in that second category. Flows that need
QuickBooks, DocuSign, Stripe, Slack, HubSpot, Jira, or LinkedIn have no business running through
a LangGraph `StateGraph`. They need connectors, not cognition.

n8n provides those connectors — 400+ pre-built, maintained by the n8n community, with visual
debugging and no redeploy cycle. **n8n is GAOS's execution layer for flows that do not need
a brain.**

### 1.2 What Was Explicitly Rejected

- **n8n as a replacement for GAOS** — GAOS handles security governance, the approval gate,
  multi-project data isolation, domain memory, and code evolution. n8n has no equivalent of any
  of these.
- **n8n as a message bus into GAOS** — Routing all n8n flows through the Pub/Sub bus into Nexus-
  Prime would create an unnecessary bottleneck and add latency to flows that never needed GAOS
  in the first place. Only Class 2 flows (see §2) touch the Pub/Sub boundary.
- **Gumloop as an alternative** — Gumloop and n8n serve the same niche. One platform, not two.
  n8n was selected for self-hosting capability and the size of its connector library.

---

## 2. Workflow Classification

Every item on the Automation Wish List belongs to one of three classes. The class determines
which system owns it.

### Class 1 — n8n Standalone (~70% of the Wish List)

These flows involve zero GAOS reasoning. n8n executes them end-to-end.

**Characteristics:**
- Trigger fires → data moves → email/Slack/CRM update
- No decision that requires domain memory or an LLM
- No action that requires the Approval Gate
- No access to GAOS BigQuery datasets

**Examples from the Wish List:**

| Item | Why Class 1 |
|------|------------|
| F1 — Receipt OCR → QuickBooks | Pure extract-transform-load; no reasoning needed |
| F3 — Late payment reminders | Date math + email template; no LLM required |
| F5 — Timesheet reminders | Scheduled ping; zero cognition |
| H2 — Employee onboarding checklist | Multi-step SaaS writes; no approval gate needed |
| H4 — Milestone alerts | Calendar lookup + notification |
| H5 — Candidate analyzer (sourcing) | Multi-step, but all within recruiting SaaS toolchain |
| C3 — CSAT survey trigger | Event → email; template-driven |
| A1 — Meeting transcription → Jira | Fireflies webhook + Jira API write |
| A3 — Calendar scheduling | Scheduling link + availability negotiation |
| A4 — CRM sync | Form webhook → multi-destination write |
| AD4 — Cross-channel ad report | API pulls + report compilation |
| V2 — Vendor renewal alert | Date check + email |
| IT2 — Security training nudge | Scheduled query + notification |
| EC1–EC4 — E-commerce lifecycle | Shopify/Stripe webhooks + email/SMS |
| PS4 — Renewal reminder sequence | Date-relative email sequence |
| PS5 — NPS / review request | Milestone event + survey send |

### Class 2 — n8n Normalizes → GAOS via Pub/Sub (~15% of the Wish List)

An external SaaS event requires GAOS reasoning, domain memory, or the Approval Gate. n8n
absorbs the messy inbound webhook, normalizes it to a clean `A2AMessage`-compatible JSON payload,
and publishes it to the appropriate GAOS Pub/Sub topic. GAOS handles everything after that.

The integration is a single n8n HTTP Request node pointing at the GCP Pub/Sub REST API.
No changes to GAOS orchestrator code are required — Pub/Sub already delivers to Nexus-Prime's
push endpoint.

**Examples from the Wish List:**

| Item | Why Class 2 (not standalone) |
|------|------------------------------|
| M1 — Lead scoring | Pursuit domain memory + ICP reasoning |
| F2 — Quote-to-Cash exceptions | Approval Gate required for invoices over threshold |
| F4 — Fraud anomaly | Approval Gate required before pausing a customer account |
| PS1/PS2 — Customer health + churn | Multi-signal reasoning + memory across interactions |
| CO2 — Contractor classification | Legal reasoning + Approval Gate before any action |
| DG1 — GDPR deletion request | Approval Gate is non-negotiable before PII deletion |

**n8n → GAOS message envelope (Class 2 flows):**

```json
{
  "messages": [{
    "data": "<base64-encoded A2AMessage JSON>",
    "attributes": {
      "source": "n8n",
      "workflow_id": "<n8n workflow name>",
      "message_type": "<TASK_HANDOFF | ALERT | VISION_SUBMITTED | ...>"
    }
  }]
}
```

The `A2AMessage` payload must include `source_agent: "n8n"`, `target_agent: "<domain agent>"`,
and a `message_type` from the existing `MessageType` enum in `models/__init__.py`. Do not
invent new message types for n8n flows — reuse what exists.

### Class 3 — GAOS Native (Already Running)

These are the ingest channels GAOS already owns natively. n8n has no role.

- Gmail watch → `GMAIL_NOTIFICATION` → `EMAIL_RECEIVED`
- Cloud Scheduler → `TTL_SWEEP` / `DAILY_SYNC` / `NIGHTLY_ARCHIVE`
- Google Chat DM → `CHAT_MESSAGE`
- Approval Sheet webhook → `APPROVAL_RESULT`

---

## 3. System Architecture Diagram

```
External World
   │
   ├─ SaaS webhooks (Stripe, DocuSign, HubSpot, Jira, LinkedIn...)
   │       │
   │       ▼
   │    ┌──────────────────────────────────────────────┐
   │    │              n8n (Cloud Run)                 │
   │    │                                              │
   │    │  Workflow canvas — 400+ SaaS connectors     │
   │    │  AI Agent node (for lightweight LLM tasks)  │
   │    │  Human-in-the-Loop node                     │
   │    │  Visual execution debugger                  │
   │    └──────────────────┬───────────────────────────┘
   │                       │
   │    Class 1 flows      │  Class 2 flows: normalized
   │    (no GAOS needed)   │  Pub/Sub message only
   │                       │
   │                       ▼
   │             ┌──────────────────┐
   │             │   GCP Pub/Sub    │
   │             │  (agent topics)  │
   │             └────────┬─────────┘
   │                      │
   │                      ▼
   │    ┌─────────────────────────────────────────────────────┐
   │    │                   GAOS                              │
   │    │                                                     │
   │    │  Nexus-Prime (Tier 1) ─── Approval Gate            │
   │    │       │                                             │
   │    │  ┌────┴─────────────────────────────────────────┐  │
   │    │  │  Ledger │ Beacon │ Pursuit │ Foreman │ Scout  │  │
   │    │  └──────────────────────────────────────────────┘  │
   │    │                                                     │
   │    │  BigQuery │ Secret Manager │ Drive │ Memory Bank   │
   │    └─────────────────────────────────────────────────────┘
   │
   └─ Gmail watch / Chat / Cloud Scheduler (GAOS native, no n8n)
```

**Key boundary rule:** n8n never reads from GAOS BigQuery datasets, never calls GAOS Cloud Run
endpoints directly, and never handles secrets that belong to GAOS service accounts. The only
permitted cross-boundary action is publishing to Pub/Sub for Class 2 flows.

---

## 4. Deployment Plan

### 4.1 Phase A — n8n Cloud (Pilot Validation)

**Goal:** Validate that n8n is the right fit before committing to infrastructure.

1. Create a free n8n Cloud account at [n8n.io](https://n8n.io).
2. Build the selected pilot workflow (see §5).
3. Run for 14 days with a human reviewing every execution.
4. Evaluate: Does n8n save meaningful development time vs. writing a Python tool wrapper?
5. If yes → proceed to Phase B. If no → reassess.

No GCP resources are created in Phase A. No integration with GAOS Pub/Sub.

### 4.2 Phase B — Self-Hosted on Cloud Run

Once the pilot validates the approach, migrate to self-hosted n8n in the same GCP project.

**Why self-hosted:**
- n8n Cloud free tier has an execution cap (5 active workflows, limited executions/month)
- Self-hosted in GCP keeps all data within the same security boundary as GAOS
- Persistent volume on Cloud Run Filestore preserves workflow state and credentials across restarts
- Total cost: ~$0–10/month depending on execution volume (Cloud Run scales to zero)

**Infrastructure:**

```yaml
Service name:     n8n
Region:           us-central1
CPU:              1 vCPU
Memory:           512 MiB
Min instances:    0   (scale-to-zero when idle)
Max instances:    2
Port:             5678
Volume mount:     /home/node/.n8n → Filestore (1 TiB minimum tier, ~$200/mo)
```

> ⚠️ **Cost note:** Filestore is the significant cost driver for self-hosted n8n on Cloud Run.
> An alternative is Cloud SQL (Postgres) for n8n's database backend, which costs ~$7/month
> on the db-f1-micro tier. This is the recommended approach — configure n8n with
> `DB_TYPE=postgresdb` environment variables pointing to a Cloud SQL instance.
> See n8n docs: [Postgres configuration](https://docs.n8n.io/hosting/configuration/supported-databases-settings/#postgresql).

**Recommended storage config (Cloud SQL instead of Filestore):**

```bash
# Create a Cloud SQL Postgres instance for n8n workflow/credential storage
gcloud sql instances create n8n-db \
    --database-version=POSTGRES_15 \
    --tier=db-f1-micro \
    --region=us-central1 \
    --project=morphic-gaos-prod

gcloud sql databases create n8n --instance=n8n-db --project=morphic-gaos-prod
gcloud sql users create n8n --instance=n8n-db --password=<from Secret Manager> \
    --project=morphic-gaos-prod
```

### 4.3 IAM Setup (Phase B only)

n8n needs a dedicated service account with minimal permissions. It must not share or inherit
GAOS service account credentials.

**Preferred: keyless authentication.** When n8n runs on Cloud Run (Phase B), the Cloud Run
service identity is `n8n-sa@morphic-gaos-prod.iam.gserviceaccount.com`. Cloud Run injects a
short-lived OIDC token automatically — no JSON key is needed. Use Application Default
Credentials (ADC) in n8n's GCP credential type with no explicit key file. This is the
required approach for Phase B Cloud Run deployments.

**Last resort: JSON key.** Only create a JSON key if the n8n deployment tier does not
support Workload Identity or ADC (e.g., n8n Cloud managed SaaS). Requirements:
- Scope: `pubsub.publisher` on the specific topic only — not project-wide.
- Lifetime: maximum 90 days. Set a calendar reminder for rotation before expiry.
- Storage: n8n credential vault only. Never commit to source control or include in logs.
- Rotation record: store the next rotation date in the workflow description field.
- Audit: run `gcloud iam service-accounts keys list --iam-account=n8n-sa@... --project=...`
  quarterly to confirm no stale keys exist.

```bash
PROJECT=morphic-gaos-prod

# Create dedicated n8n service account
gcloud iam service-accounts create n8n-sa \
    --display-name="n8n Workflow Runner" \
    --project=$PROJECT

# Class 2 flows only: publish to Pub/Sub agent topics
# Grant only on the specific topics n8n needs, not project-wide
gcloud pubsub topics add-iam-policy-binding agent.nexus-prime.events \
    --member="serviceAccount:n8n-sa@${PROJECT}.iam.gserviceaccount.com" \
    --role="roles/pubsub.publisher" \
    --project=$PROJECT

# n8n SA must NOT have:
# - BigQuery access (GAOS data is off-limits)
# - Secret Manager access (secrets are injected at deploy time via Cloud Run
#   secret references, not read by n8n at runtime — the deploying principal
#   needs secretmanager.versions.access, not the n8n SA itself)
# - Cloud Run invocation on any GAOS service
# - Any other GAOS IAM role
```

### 4.4 n8n Environment Variables (Cloud Run)

> **Note:** The `<from Secret Manager: ...>` references below are Cloud Run secret references
> (`--set-secrets`), not runtime API calls. Cloud Run injects these values at container start
> using the **deploying principal's** Secret Manager access. The n8n SA itself never calls the
> Secret Manager API.

```bash
N8N_HOST=0.0.0.0
N8N_PORT=5678
N8N_PROTOCOL=https
WEBHOOK_URL=https://n8n-<hash>-uc.a.run.app
N8N_ENCRYPTION_KEY=<from Secret Manager: n8n-encryption-key>
DB_TYPE=postgresdb
DB_POSTGRESDB_HOST=/cloudsql/morphic-gaos-prod:us-central1:n8n-db
DB_POSTGRESDB_DATABASE=n8n
DB_POSTGRESDB_USER=n8n
DB_POSTGRESDB_PASSWORD=<from Secret Manager: n8n-db-password>
```

---

## 5. Pilot Workflow Selection

The late payment reminder flow (F3) is excluded. The following options are the best
candidates for a first pilot, ordered by what they test and how fast they deliver value.

### Option A — Meeting Transcription → Jira (A1)
**What it tests:** Incoming webhook from Fireflies.ai → AI Agent node summarizes transcript →
Jira API creates action-item tickets for named participants.

**Why it's a good first pilot:**
- Tests the two most valuable n8n capabilities in one flow: webhook ingestion + AI Agent node
- High daily visibility — you'd see it working after every meeting
- Fireflies has a free tier; Jira has a free tier

**Risk:** Two third-party API integrations (Fireflies + Jira) means more setup. If either
changes their API, the flow breaks.

---

### Option B — Employee / Contractor Onboarding Checklist (H2)
**What it tests:** A contract-signed event (DocuSign webhook or manual trigger) → multi-step
parallel flow: create user in Google Workspace, assign Slack channel, send welcome email,
schedule 1:1 calendar invite, create Notion onboarding page.

**Why it's a good first pilot:**
- Tests n8n's multi-step, multi-destination capability — the core value proposition
- Immediately eliminates a painful manual process
- The trigger can be mocked as a manual button click for the pilot, so DocuSign isn't required
  on Day 1

**Risk:** More connectors = more setup time for the pilot.

---

### Option C — LinkedIn Lead Intelligence + CRM Write (M6)
**What it tests:** New CRM lead created → n8n calls LinkedIn API (or Phantombuster) to
scrape recent posts → AI Agent node drafts a hyper-personalized outreach email →
writes draft back to CRM record, notifies owner in Slack.

**Why it's a good first pilot:**
- Revenue-adjacent — directly supports sales outreach quality
- Tests the AI Agent node with real web data
- End-to-end in n8n, no GAOS Pub/Sub needed (pure Class 1)

**Risk:** LinkedIn scraping is fragile. Official LinkedIn API access requires OAuth approval.
Phantombuster adds cost. This is the hardest of the three to stand up reliably.

---

### Option D — Cross-Channel Ad Performance Report (AD4)
**What it tests:** Scheduled weekly trigger → pull spend/ROAS/CPC from Google Ads + Meta Ads
APIs → compile into a structured summary → send as a formatted Slack message or email digest.

**Why it's a good first pilot:**
- Pure scheduled data aggregation — the simplest possible n8n flow architecture
- Tests the Google Ads and Meta connectors, which are the most common paid-media integrations
- Zero risk of breaking anything; it's read-only
- If you run any paid ads, you'd use it immediately

**Risk:** Requires Google Ads and Meta Ads API credentials. If you aren't running paid ads yet,
this delivers zero value.

---

### ✅ Option E — Daily Digital Performance Report + KPI Tracker (BI5) — SELECTED PILOT

**What it tests:** Scheduled morning trigger → pull Google Analytics 4 (sessions, bounce rate,
avg. session duration, goal conversions, traffic source breakdown for the landing page) → write
raw metrics to a Google Sheet KPI tracker (rolling 7-day and 30-day baselines computed per
metric) → send a formatted daily digest via email with directional trend arrows vs. yesterday
and the 7-day average.

YouTube Data API v3 columns (views, watch time, subscriber delta, impressions CTR, top 5
videos) are **scaffolded in the KPI Sheet schema from Day 1** but the n8n nodes that populate
them are disabled until the YouTube channel exists. Activating Phase B requires enabling two
nodes and adding a Google YouTube credential — no structural changes to the workflow.

**Why this is the best first pilot:**
- GA4 is Google OAuth — n8n's Google nodes are the most battle-hardened in the library;
  no third-party credentials or approvals required beyond what you already have
- Entirely read-only — zero risk of breaking or writing to anything critical
- KPI Sheet write tests n8n's Sheets integration, which is the most reused capability across
  the entire wish list; validating it here pays dividends on every subsequent flow
- Produces immediate daily value from existing data — no channel required to start
- Clean two-phase upgrade path: Phase B (add YouTube) and Phase C (BI6 anomaly ALERT to
  GAOS Foreman) each require minimal changes to an already-running workflow

**n8n workflow name:** `BI5-analytics-daily-performance-report`

---

### BI5 Prerequisites Checklist

Complete all items before opening n8n Cloud.

#### Step 1 — Confirm GA4 is Collecting Data
- [ ] A Google Analytics 4 property exists for the landing page
- [ ] The GA4 measurement tag is firing on the landing page (verify in GA4 → Realtime view)
- [ ] You know the **GA4 Property ID** (format: `123456789` — found in GA4 Admin → Property Settings)
- [ ] At least 7 days of GA4 data exists so the 7-day baseline is meaningful on Day 1

#### Step 2 — Provision the KPI Tracker Sheet
Create a new Google Sheet (standalone, not the GAOS control Sheet) named **`GAOS-KPI-Tracker`**.
Add one tab named `daily_metrics` with these column headers in row 1:

| Col | Header | Active now? |
|-----|--------|-------------|
| A | `date` | ✅ |
| B | `ga_sessions` | ✅ |
| C | `ga_bounce_rate` | ✅ |
| D | `ga_avg_session_duration_s` | ✅ |
| E | `ga_organic_sessions` | ✅ |
| F | `ga_direct_sessions` | ✅ |
| G | `ga_referral_sessions` | ✅ |
| H | `ga_goal_completions` | ✅ |
| I | `baseline_7d_ga_sessions` | ✅ (computed) |
| J | `baseline_30d_ga_sessions` | ✅ (computed) |
| K | `yt_views` | ⏸ (Phase B) |
| L | `yt_watch_time_hrs` | ⏸ (Phase B) |
| M | `yt_subscriber_delta` | ⏸ (Phase B) |
| N | `yt_impressions_ctr` | ⏸ (Phase B) |
| O | `yt_top_video_id` | ⏸ (Phase B) |
| P | `baseline_7d_yt_views` | ⏸ (Phase B) |

Note the **Sheet ID** from the URL (`https://docs.google.com/spreadsheets/d/<SHEET_ID>/`).

#### Step 3 — Decide on Report Delivery
- [ ] **Email digest** — n8n sends a formatted HTML email to your address each morning
  (uses n8n's built-in Gmail node; requires Gmail OAuth credential)
- [ ] **Slack message** — n8n posts to a private Slack channel
  (requires a Slack OAuth app with `chat:write` scope)

Email is recommended for the pilot — fewer credentials to configure.

#### Step 4 — Create n8n Cloud Account
1. Go to [n8n.io](https://n8n.io) and create a free account
2. Start a new workflow named `BI5-analytics-daily-performance-report`
3. Add credentials: **Google Analytics** (OAuth2) and **Google Sheets** (OAuth2) and
   **Gmail** (OAuth2 or SMTP) — all three use the same Google account, so one OAuth
   consent covers all three

#### Step 5 — n8n OAuth Scopes Required
When authorizing the Google OAuth credential in n8n, confirm these scopes are granted:

| Scope | Purpose |
|-------|---------|
| `https://www.googleapis.com/auth/analytics.readonly` | GA4 Data API reads |
| `https://www.googleapis.com/auth/spreadsheets` | KPI Sheet writes |
| `https://www.googleapis.com/auth/gmail.send` | Morning digest email |
| `https://www.googleapis.com/auth/youtube.readonly` | Phase B only — add when channel exists |

> ⚠️ **Note:** n8n Cloud's Google OAuth app is pre-registered by n8n. You authorize
> access to your Google account via the standard OAuth consent screen — you do not
> need to create a GCP OAuth client for n8n Cloud. Self-hosted (Phase B) requires
> creating a GCP OAuth client manually.

#### Step 6 — Identify Your GA4 API Dimensions and Metrics
The GA4 Data API uses specific field names. Confirm these are available in your property
before building the n8n workflow:

```
Metrics:    sessions, bounceRate, averageSessionDuration,
            conversions, newUsers
Dimensions: sessionDefaultChannelGroup   (gives Organic, Direct, Referral, Paid)
Date range: yesterday (dateRange: { startDate: 'yesterday', endDate: 'yesterday' })
```

Verify your property returns data for these fields in the GA4 Explorer
(Explore → Free Form → add the metrics above).

---

**Recommendation:** Start with **Option E (BI5)**. It is the only option that is simultaneously
read-only, Google-native, immediately useful every day, and a direct test of the Sheets
integration that underpins half the wish list.

Options A–D remain valid for subsequent pilots after BI5 is proven. Option A (Meeting
Transcription) is recommended as the second pilot.

---

## 6. Naming and Governance

### 6.1 Workflow Naming Convention

All n8n workflows must follow this naming pattern:

```
<WishListID>-<domain>-<short-description>
```

Examples:
- `A1-admin-meeting-transcription`
- `H2-hr-onboarding-checklist`
- `F2-finance-quote-to-cash`
- `M1-marketing-lead-scoring`  ← Class 2: publishes to Pub/Sub

### 6.2 Class 2 Workflow Requirements

Any n8n workflow that publishes to a GAOS Pub/Sub topic must:

1. Include a `workflow_id` attribute in the Pub/Sub message (the workflow name above)
2. Log every publish event to a dedicated n8n execution log (built-in to n8n)
3. Include the `hop_count` field in the `A2AMessage` payload, starting at `0`
   (per Rule 25.4 — loop prevention)
4. Never publish to more than one GAOS topic per workflow execution
5. Be reviewed and tagged with `class-2` in n8n's workflow tags

### 6.3 Credential Management

n8n stores its own credentials internally (encrypted by `N8N_ENCRYPTION_KEY`). It must not
be given access to GAOS Secret Manager. Third-party API keys used exclusively by n8n
(e.g., LinkedIn API key, Fireflies webhook secret) live in n8n's credential vault, not in
GAOS Secret Manager — they have no business being in a system they'll never reach.

Exception: if a Class 2 workflow needs to publish to Pub/Sub, the **preferred** path is
keyless authentication — use Workload Identity Federation or the Cloud Run service account
identity (OIDC) so no JSON key is ever created or stored. Long-lived JSON service account
keys are a last resort only, permitted when keyless auth is technically infeasible for the
specific n8n deployment tier. If a JSON key must be stored: it lives in n8n's vault,
is scoped to `pubsub.publisher` on the specific topic only, must have a 90-day maximum
lifetime, must be rotated before expiry, and the rotation date must be recorded in the
workflow's description field. The key must never be committed to source control or logged.
See §4.3 for IAM setup and key management requirements.

---

## 7. Observability

n8n provides built-in execution logs for every workflow run. For Phase A (n8n Cloud), this
is sufficient. For Phase B (self-hosted), add:

- **Error alerting:** Configure n8n's built-in error workflow to send a Slack or email
  notification on any failed execution.
- **Execution metrics:** n8n exposes a Prometheus `/metrics` endpoint. Wire this to
  Cloud Monitoring via a scrape job if per-workflow execution counts become operationally
  relevant.
- **No GAOS BigQuery writes from n8n** — n8n execution history is not written to
  `aos_logs.api_call_log`. If cross-system audit trail is needed for a Class 2 flow,
  the downstream GAOS node is responsible for logging it.

---

## 8. Future: n8n → CrewAI → GAOS

As noted in Automation-Wish-List P3, CrewAI can eventually sit inside GAOS as a Tier 3
reasoning layer for tasks requiring dynamic subtask decomposition. When that ships, Class 2
flows will have a richer target: instead of publishing a raw `TASK_HANDOFF` to Nexus-Prime,
n8n can publish to a Foreman topic that triggers a CrewAI crew for the reasoning work.
The integration point does not change — only what GAOS does with the message after it arrives.

---

_Last updated: 2026-04-16_
