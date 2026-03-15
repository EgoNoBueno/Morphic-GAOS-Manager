# GAOS Deployment Specification

**Morphic-G AOS** — Infrastructure Provisioning & First-Run Guide

> This document is the step-by-step guide for standing up every Google Cloud and Google Workspace resource the system requires. Follow the sections in order. Each section ends with a verification step — do not proceed to the next section until verification passes.
>
> **Estimated time:** 2–3 hours for a first-time setup, 30 minutes for a repeat deployment.
> **Estimated monthly cost after setup:** ≈ $1.50 (see `GAOS-Manager-Spec.md` §9.4 for breakdown).

---

## 0. Prerequisites

Before starting, confirm the following are in place on your local machine.

### 0.1 Required Accounts

| Account | Purpose | Notes |
|---------|---------|-------|
| Google account (personal or Workspace) | Owns all GCP resources and the Google Sheet | Must be an account you control and trust |
| GitHub account | Source control | Repo already created: `EgoNoBueno/Morphic-GAOS-Manager` |

### 0.2 Required Local Tools

Install these before proceeding:

```powershell
# Verify each tool after installing
gcloud --version        # Google Cloud CLI — https://cloud.google.com/sdk/docs/install
gh --version            # GitHub CLI (already confirmed installed)
python --version        # Python 3.11+
uv --version            # uv package manager — https://docs.astral.sh/uv/getting-started/installation/
git --version           # Already confirmed installed
ollama --version        # Ollama — https://ollama.com/download (Windows)
```

### 0.3 Clone and Bootstrap Repo

```powershell
# If not already done
git clone https://github.com/EgoNoBueno/Morphic-GAOS-Manager.git
cd Morphic-GAOS-Manager

# Create Python environment and install dependencies (Phase 1 — minimal)
uv venv
uv pip install google-cloud-secret-manager google-cloud-pubsub gspread pydantic \
               google-adk langgraph google-cloud-bigquery google-cloud-logging \
               google-cloud-aiplatform \
               "google-genai>=1.0.0"
```

> **SDK note:** Use `google-genai>=1.0.0` (`google.genai.Client()` API) — **not** `google-generativeai`. The `google-generativeai` package is EOL: it imports with a `FutureWarning` and the `v1beta` endpoint it targets no longer serves models like `gemini-1.5-pro`, returning 404. The `google-genai` package is the official successor and is what `google-adk` expects.

### 0.4 Application Default Credentials (ADC) Setup

All local development runs use ADC — **no service account key file on disk**. `GOOGLE_APPLICATION_CREDENTIALS` must NOT be set in your environment or `.env` file. If that variable exists (even pointing at a missing file), `google-auth` skips ADC entirely and fails silently.

```powershell
# Verify the variable is not set — remove it if it is
$env:GOOGLE_APPLICATION_CREDENTIALS   # should return nothing

# Step 1: Create an OAuth 2.0 Desktop Client ID in YOUR GCP project
# Go to: console.cloud.google.com/apis/credentials → Create Credentials → OAuth client ID
# Application type: Desktop app
# Download the JSON file and save as oauth-client.json (gitignored)

# Step 2: Log in with your Desktop client and request the Sheets + Drive scopes
# The DEFAULT gcloud client ID BLOCKS the spreadsheets scope — you MUST use your own.
gcloud auth application-default login `
  --client-id-file=oauth-client.json `
  --scopes="https://www.googleapis.com/auth/spreadsheets,https://www.googleapis.com/auth/drive.file,https://www.googleapis.com/auth/cloud-platform"

# Verify ADC is working
gcloud auth application-default print-access-token
```

> **What this does:** All Google Cloud libraries (`google-auth`, `gspread`, `google-genai`, `google-cloud-pubsub`) automatically pick up the credential written to `~/.config/gcloud/application_default_credentials.json`. You do not need a service account key file locally. Service account keys are only used by Cloud Run at runtime (loaded from Secret Manager — see §3).

### 0.5 Local Ollama Setup

```powershell
# Pull the recommended model (16GB RAM minimum; for GPU use mistral)
ollama pull llama3.1

# Register Ollama as a Windows Service so it starts on boot and restarts on crash
# Run PowerShell as Administrator:
nssm install OllamaService "C:\Users\$env:USERNAME\AppData\Local\Programs\Ollama\ollama.exe" "serve"
nssm set OllamaService Start SERVICE_AUTO_START
nssm set OllamaService AppRestartDelay 3000
Start-Service OllamaService

# Verify
Invoke-RestMethod http://localhost:11434/api/tags
```

---

## 1. GCP Project Setup

### 1.1 Create the Project

```bash
# Choose a unique project ID — this becomes your GCP_PROJECT_ID in settings.yaml
gcloud projects create morphic-gaos-prod --name="Morphic GAOS"
gcloud config set project morphic-gaos-prod

# Link billing account (required for Cloud Run, Secret Manager, Vertex AI)
gcloud billing accounts list
gcloud billing projects link morphic-gaos-prod --billing-account=<BILLING_ACCOUNT_ID>
```

### 1.2 Enable APIs

Enable all required APIs in a single command:

```bash
gcloud services enable \
  sheets.googleapis.com \
  drive.googleapis.com \
  pubsub.googleapis.com \
  secretmanager.googleapis.com \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  bigquery.googleapis.com \
  logging.googleapis.com \
  monitoring.googleapis.com \
  aiplatform.googleapis.com \
  cloudresourcemanager.googleapis.com
```

**Verification:** `gcloud services list --enabled` — all 11 services must appear.

---

## 2. IAM Service Accounts

Create one service account per agent. Each gets only the permissions its role requires. Never reuse a service account between agents.

### 2.1 Create Service Accounts

```bash
# Nexus-Prime — most privileged (manages all other agents)
gcloud iam service-accounts create nexus-prime-sa \
  --display-name="Nexus-Prime AOS Manager"

# Domain orchestrators
for agent in ledger beacon pursuit foreman steward scout; do
  gcloud iam service-accounts create ${agent}-sa \
    --display-name="${agent^} Domain Orchestrator"
done

# Apps Script service account (for Sheets + Drive access from Apps Script)
gcloud iam service-accounts create apps-script-sa \
  --display-name="Apps Script Sheets/Drive Integration"
```

### 2.2 Assign IAM Roles

```bash
PROJECT=morphic-gaos-prod

# ── Nexus-Prime ────────────────────────────────────────────────────────────
# Full Pub/Sub admin, BigQuery writer, Drive write, Sheets, Memory Bank, Logging
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:nexus-prime-sa@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/pubsub.admin"
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:nexus-prime-sa@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/bigquery.dataEditor"
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:nexus-prime-sa@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/logging.logWriter"
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:nexus-prime-sa@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user"

# ── Domain orchestrators (all share these base roles) ─────────────────────
for agent in ledger beacon pursuit foreman steward scout; do
  SA="${agent}-sa@${PROJECT}.iam.gserviceaccount.com"
  gcloud projects add-iam-policy-binding $PROJECT \
    --member="serviceAccount:${SA}" --role="roles/pubsub.publisher"
  gcloud projects add-iam-policy-binding $PROJECT \
    --member="serviceAccount:${SA}" --role="roles/pubsub.subscriber"
  gcloud projects add-iam-policy-binding $PROJECT \
    --member="serviceAccount:${SA}" --role="roles/logging.logWriter"
  gcloud projects add-iam-policy-binding $PROJECT \
    --member="serviceAccount:${SA}" --role="roles/bigquery.dataViewer"
done

# ── Apps Script SA: Sheets + Drive ────────────────────────────────────────
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:apps-script-sa@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/sheets.editor"
```

### 2.3 Generate and Download Service Account Keys

```bash
# Create keys directory (gitignored)
mkdir -p config/keys

for sa in nexus-prime ledger beacon pursuit foreman steward scout apps-script; do
  gcloud iam service-accounts keys create "config/keys/${sa}-sa.json" \
    --iam-account="${sa}-sa@${PROJECT}.iam.gserviceaccount.com"
done
```

> **Warning:** These JSON files are highly sensitive. They are excluded from git by `.gitignore`. Never commit them. Upload them to Secret Manager in the next step and then delete the local copies.

**Verification:** `gcloud iam service-accounts list` — 8 service accounts listed.

---

## 3. Secret Manager

Populate all secrets before any agent code runs. The agent boot sequence calls `get_secret()` for every secret in its inventory and fails fast if any are missing.

### 3.1 Create Secrets

```bash
PROJECT=morphic-gaos-prod

# API credentials
# ⚠️  IMPORTANT: Get this key from console.cloud.google.com/apis/credentials
#    inside YOUR GCP project — NOT from aistudio.google.com.
#    AI Studio keys live in Google's shared project and are not covered by your
#    GCP billing account. Using a Studio key causes 429 RESOURCE_EXHAUSTED
#    errors with "limit: 0" even when billing is enabled and credits are available.
gcloud secrets create GEMINI_API_KEY --project=$PROJECT
echo -n "<your-gemini-api-key>" | \
  gcloud secrets versions add GEMINI_API_KEY --data-file=- --project=$PROJECT

# Service account JSON keys (upload file content as secret value)
gcloud secrets create GSHEETS_SERVICE_ACCOUNT --project=$PROJECT
gcloud secrets versions add GSHEETS_SERVICE_ACCOUNT \
  --data-file=config/keys/apps-script-sa.json --project=$PROJECT

gcloud secrets create PUBSUB_SERVICE_ACCOUNT --project=$PROJECT
gcloud secrets versions add PUBSUB_SERVICE_ACCOUNT \
  --data-file=config/keys/nexus-prime-sa.json --project=$PROJECT

gcloud secrets create BIGQUERY_SERVICE_ACCOUNT --project=$PROJECT
gcloud secrets versions add BIGQUERY_SERVICE_ACCOUNT \
  --data-file=config/keys/nexus-prime-sa.json --project=$PROJECT

gcloud secrets create VERTEX_SERVICE_ACCOUNT --project=$PROJECT
gcloud secrets versions add VERTEX_SERVICE_ACCOUNT \
  --data-file=config/keys/nexus-prime-sa.json --project=$PROJECT

# Ollama host (local machine LAN IP or loopback)
gcloud secrets create OLLAMA_HOST --project=$PROJECT
echo -n "http://localhost:11434" | \
  gcloud secrets versions add OLLAMA_HOST --data-file=- --project=$PROJECT

# HMAC secret for webhook (generate a random 32-byte hex string)
python -c "import secrets; print(secrets.token_hex(32))" | \
  gcloud secrets versions add WEBHOOK_HMAC_SECRET --data-file=- --project=$PROJECT 2>/dev/null || \
  (gcloud secrets create WEBHOOK_HMAC_SECRET --project=$PROJECT && \
   python -c "import secrets; print(secrets.token_hex(32))" | \
   gcloud secrets versions add WEBHOOK_HMAC_SECRET --data-file=- --project=$PROJECT)

# WEBHOOK_URL — add after Apps Script is deployed in Section 5
# gcloud secrets create WEBHOOK_URL --project=$PROJECT
# echo -n "<apps-script-web-app-url>" | \
#   gcloud secrets versions add WEBHOOK_URL --data-file=- --project=$PROJECT
```

### 3.2 Grant Per-Secret Access (Least-Privilege)

```bash
PROJECT=morphic-gaos-prod

# GEMINI_API_KEY — all orchestrators need it
for agent in nexus-prime ledger beacon pursuit foreman steward scout; do
  gcloud secrets add-iam-policy-binding GEMINI_API_KEY \
    --member="serviceAccount:${agent}-sa@${PROJECT}.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor" --project=$PROJECT
done

# GSHEETS_SERVICE_ACCOUNT — all agents need Sheet access
for agent in nexus-prime ledger beacon pursuit foreman steward scout; do
  gcloud secrets add-iam-policy-binding GSHEETS_SERVICE_ACCOUNT \
    --member="serviceAccount:${agent}-sa@${PROJECT}.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor" --project=$PROJECT
done

# PUBSUB_SERVICE_ACCOUNT — all agents publish/subscribe
for agent in nexus-prime ledger beacon pursuit foreman steward scout; do
  gcloud secrets add-iam-policy-binding PUBSUB_SERVICE_ACCOUNT \
    --member="serviceAccount:${agent}-sa@${PROJECT}.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor" --project=$PROJECT
done

# BIGQUERY_SERVICE_ACCOUNT — nexus-prime and archive job only
gcloud secrets add-iam-policy-binding BIGQUERY_SERVICE_ACCOUNT \
  --member="serviceAccount:nexus-prime-sa@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" --project=$PROJECT

# VERTEX_SERVICE_ACCOUNT — nexus-prime only (evolution sandbox, Memory Bank writes)
gcloud secrets add-iam-policy-binding VERTEX_SERVICE_ACCOUNT \
  --member="serviceAccount:nexus-prime-sa@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" --project=$PROJECT

# OLLAMA_HOST — all agents (fallback routing)
for agent in nexus-prime ledger beacon pursuit foreman steward scout; do
  gcloud secrets add-iam-policy-binding OLLAMA_HOST \
    --member="serviceAccount:${agent}-sa@${PROJECT}.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor" --project=$PROJECT
done

# WEBHOOK_HMAC_SECRET — nexus-prime only (webhook sender)
gcloud secrets add-iam-policy-binding WEBHOOK_HMAC_SECRET \
  --member="serviceAccount:nexus-prime-sa@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" --project=$PROJECT
```

### 3.3 Delete local key files

```bash
Remove-Item config/keys/*.json -Force
```

**Verification:** `gcloud secrets list --project=$PROJECT` — 7 secrets listed (WEBHOOK_URL will be added after Apps Script deploy).

---

## 4. Google Sheets Workbook

### 4.1 Create the Master Workbook

1. Open [Google Sheets](https://sheets.google.com) and create a new blank spreadsheet.
2. Name it **`Morphic-G AOS — Control Plane`**.
3. Copy the Spreadsheet ID from the URL: `https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit`
4. Save this ID — it goes into `settings.yaml` as `SHEET_ID`.

### 4.2 Create All Required Tabs

Create tabs in this order (right-click tab bar → Insert sheet):

| Tab Name | Owner | Purpose |
|----------|-------|---------|
| `Project Registry` | Nexus-Prime | One row per AOS project namespace |
| `Agent_Approvals` | Nexus-Prime | Approval Gate proposal queue |
| `Authorized Approvers` | Owner | RBAC tier definitions |
| `Accounting` | Ledger | Financial transactions, invoices, P&L |
| `Marketing` | Beacon | Campaign performance, ad spend |
| `Sales by Product` | Pursuit | CRM leads, pipeline, quotes |
| `Sales Graphs` | Beacon | Chart data (read by dashboard) |
| `Ad Response/Spend/Recommendations` | Beacon | Ad platform data |
| `Shipping and Receiving` | Foreman | Inventory, shipments, vendor SLAs |
| `Logs` | Steward | Compliance, onboarding, admin tasks |
| `Error Logs` | Nexus-Prime | Weekly evolution summary rows |
| `Research Products` | Scout | Market intelligence, competitor data |
| `Pending_Knowledge` | Nexus-Prime | Observation buffer (Layer 3 memory) |
| `Memory Repository Size` | Nexus-Prime | Memory Bank usage tracking |

### 4.3 Add Header Rows

Add these header rows to tabs that agents write to. Row 1 is always the header.

**`Agent_Approvals` headers (row 1, columns A–M):**
```
ID | Agent ID | Issue | Trigger Reason | Stopping Constraint | Iterations Run | Total Cost USD | Proposed Code | Status | Timestamp | Approved By | Approver Tier | code_sha256
```

**`Authorized Approvers` headers (row 1, columns A–F):**
```
email | name | tier | active | added_date | notes
```
Then add your own row in row 2:
```
your@email.com | Your Name | 5 | TRUE | 2026-03-14 | Owner
```

**`Project Registry` headers (row 1, columns A–I):**
```
project_id | project_name | status | sheet_workbook_id | drive_folder_id | budget_ceiling_usd | owner_email | created_date | notes
```
Add the first project row immediately (this workbook itself — `project_id` = `default`):
```
default | Default Project | Active | <this-spreadsheet-id> | <drive-folder-id — fill in after §6> | 5.00 | your@email.com | 2026-03-14 |
```

### 4.4 Deploy Apps Script

1. In the Sheet: **Extensions → Apps Script**
2. Create three script files. Paste in the full implementations from `GAOS-Manager-Spec.md`:
   - `doPost.gs` — from §14 (HMAC webhook handler)
   - `onChangeApproval.gs` — from §14 (RBAC approval handler)
   - `syncSkillsToVertex.gs` — from §14 (three-gate code deploy)
   - `setup_protection.gs` — from §14 (run once to lock columns)
3. Add `WEBHOOK_HMAC_SECRET` to **Script Properties** (Project Settings → Script Properties):
   - Key: `WEBHOOK_HMAC_SECRET`
   - Value: the same 32-byte hex value stored in Secret Manager
4. Add `VERTEX_AGENT_ENDPOINT` to Script Properties:
   - Value: placeholder for now — fill in after Cloud Run deploy in §8

### 4.5 Run Protection Setup

In the Apps Script editor, select `setupProtections` and click **Run**. This locks:
- Column I (Status) on `Agent_Approvals` → owner only
- Column H (Proposed Code) on `Agent_Approvals` → owner only
- Column M (code_sha256) on `Agent_Approvals` → owner only
- Entire `Authorized Approvers` tab → owner only

### 4.6 Install the `onChange` Trigger

Apps Script → **Triggers** (clock icon) → Add Trigger:
- Function: `onChangeApproval`
- Event source: From spreadsheet
- Event type: On change
- Save

### 4.7 Deploy the Webhook as a Web App

Apps Script → **Deploy → New deployment**:
- Type: Web app
- Execute as: **Me**
- Who has access: **Anyone**
- Click Deploy — copy the Web App URL

Store the URL in Secret Manager:
```bash
echo -n "<web-app-url>" | \
  gcloud secrets versions add WEBHOOK_URL --data-file=- --project=morphic-gaos-prod
```

Also store it in Script Properties:
- Key: `WEBHOOK_URL`
- Value: the same URL

**Verification (Sheet):**
- Manually change a `Status` cell in `Agent_Approvals` to `Approved` — an entry should appear in the `Logs` tab (from `logApprovalEvent_`).
- Change a cell without being in the `Authorized Approvers` tab — the cell should revert to `Pending` and a `NOT_IN_APPROVERS_LIST` entry should appear.

---

## 5. Cloud Pub/Sub

Create all topics and subscriptions. Agents create their own topics idempotently at boot, but pre-creating them here ensures Nexus-Prime can subscribe to all orchestrator topics before any orchestrator has run.

### 5.1 Create Topics

```bash
PROJECT=morphic-gaos-prod

for topic in \
  agent.nexus-prime.events \
  agent.ledger.events \
  agent.beacon.events \
  agent.pursuit.events \
  agent.foreman.events \
  agent.steward.events \
  agent.scout.events \
  agent.approvals.events; do
    gcloud pubsub topics create $topic --project=$PROJECT
done
```

> **Naming note:** Pub/Sub topic names cannot contain `/`. Use `.` as the separator in GCP resources; the `A2AMessage` schema uses `/` as a display convention only.

### 5.2 Create Subscriptions

```bash
PROJECT=morphic-gaos-prod
BASE="projects/${PROJECT}/topics"

# Nexus-Prime subscribes to all orchestrator topics
for topic in ledger beacon pursuit foreman steward scout; do
  gcloud pubsub subscriptions create nexus-prime.sub.${topic} \
    --topic="${BASE}/agent.${topic}.events" \
    --push-endpoint="<nexus-prime-cloud-run-url>/pubsub" \
    --ack-deadline=60 \
    --project=$PROJECT
done

# Approvals subscription — all agents subscribe to this
for agent in nexus-prime ledger beacon pursuit foreman steward scout; do
  gcloud pubsub subscriptions create ${agent}.sub.approvals \
    --topic="${BASE}/agent.approvals.events" \
    --push-endpoint="<${agent}-cloud-run-url>/pubsub" \
    --ack-deadline=60 \
    --project=$PROJECT
done

# Cross-domain subscriptions per §10.1
# Pursuit subscribes to ledger + beacon + foreman
gcloud pubsub subscriptions create pursuit.sub.ledger \
  --topic="${BASE}/agent.ledger.events" \
  --push-endpoint="<pursuit-cloud-run-url>/pubsub" \
  --ack-deadline=60 --project=$PROJECT

gcloud pubsub subscriptions create pursuit.sub.beacon \
  --topic="${BASE}/agent.beacon.events" \
  --push-endpoint="<pursuit-cloud-run-url>/pubsub" \
  --ack-deadline=60 --project=$PROJECT

gcloud pubsub subscriptions create pursuit.sub.foreman \
  --topic="${BASE}/agent.foreman.events" \
  --push-endpoint="<pursuit-cloud-run-url>/pubsub" \
  --ack-deadline=60 --project=$PROJECT

# Ledger subscribes to pursuit + foreman
gcloud pubsub subscriptions create ledger.sub.pursuit \
  --topic="${BASE}/agent.pursuit.events" \
  --push-endpoint="<ledger-cloud-run-url>/pubsub" \
  --ack-deadline=60 --project=$PROJECT

gcloud pubsub subscriptions create ledger.sub.foreman \
  --topic="${BASE}/agent.foreman.events" \
  --push-endpoint="<ledger-cloud-run-url>/pubsub" \
  --ack-deadline=60 --project=$PROJECT

# Foreman subscribes to pursuit
gcloud pubsub subscriptions create foreman.sub.pursuit \
  --topic="${BASE}/agent.pursuit.events" \
  --push-endpoint="<foreman-cloud-run-url>/pubsub" \
  --ack-deadline=60 --project=$PROJECT

# Beacon subscribes to pursuit + scout
gcloud pubsub subscriptions create beacon.sub.pursuit \
  --topic="${BASE}/agent.pursuit.events" \
  --push-endpoint="<beacon-cloud-run-url>/pubsub" \
  --ack-deadline=60 --project=$PROJECT

gcloud pubsub subscriptions create beacon.sub.scout \
  --topic="${BASE}/agent.scout.events" \
  --push-endpoint="<beacon-cloud-run-url>/pubsub" \
  --ack-deadline=60 --project=$PROJECT

# Scout subscribes to foreman
gcloud pubsub subscriptions create scout.sub.foreman \
  --topic="${BASE}/agent.foreman.events" \
  --push-endpoint="<scout-cloud-run-url>/pubsub" \
  --ack-deadline=60 --project=$PROJECT
```

> **Note:** Push endpoint URLs contain the Cloud Run service URLs. These are created in §8. Update subscription push endpoints after Cloud Run deploy using:
> `gcloud pubsub subscriptions modify-push-config <sub-name> --push-endpoint=<url>`

**Verification:** `gcloud pubsub topics list --project=$PROJECT` — 8 topics listed.

---

## 6. Google Drive — Knowledge/ Folder

### 6.1 Create Folder Structure

1. In Google Drive, create a top-level folder: **`Morphic-G AOS — Knowledge`**
2. Inside it, create these subfolders:

```
Knowledge/
├── workflows/         # Multi-step process documents
├── procedures/        # Step-by-step instructions for specific tasks
├── policies/          # Rules agents must follow
└── archive/           # Old versions of updated documents
```

3. Copy the folder ID from the URL of the root `Knowledge/` folder and add it to the `default` row in the `Project Registry` tab (column E).

### 6.2 Create Seed Knowledge Files

Create these placeholder files now — agents will reference them from day one. These can be brief initially; agents will propose updates as they learn.

**`policies/expense_approval_policy.md`** — spending thresholds requiring approval  
**`policies/vendor_payment_terms.md`** — standard vendor payment terms  
**`policies/data_retention_policy.md`** — data handling rules  
**`policies/communications_policy.md`** — outbound communication rules  
**`policies/research_policy.md`** — allowed research methods and citation requirements  
**`procedures/invoice_matching.md`** — how to match invoices to bank entries  
**`procedures/lead_scoring_criteria.md`** — lead qualification criteria  
**`procedures/inventory_reorder_trigger.md`** — reorder threshold definitions per SKU  
**`procedures/document_filing.md`** — where different document types are filed  
**`procedures/competitive_intelligence.md`** — competitor monitoring methodology  
**`workflows/ap_reconciliation.md`** — month-end accounts payable reconciliation  
**`workflows/order_fulfillment.md`** — deal-to-delivery sequence  
**`workflows/weekly_reporting.md`** — weekly summary generation process  

### 6.3 Share with Service Accounts

Grant the appropriate service accounts access to the `Knowledge/` folder:

- **Nexus-Prime SA** (`nexus-prime-sa@...`): Editor (can write post-approval)
- **All orchestrator SAs** (`ledger-sa@...`, `beacon-sa@...`, etc.): Viewer (read-only)

In Google Drive: right-click `Knowledge/` folder → Share → add each service account email.

---

## 7. BigQuery

### 7.1 Create Dataset

```bash
bq mk --dataset \
  --description "AOS cold log storage and historical analytics" \
  --location US \
  morphic-gaos-prod:aos_logs
```

### 7.2 Create Tables with TTL Partitioning

```sql
-- Run in BigQuery console (https://console.cloud.google.com/bigquery)
-- or via bq CLI

-- Task outcomes (episodic memory source)
CREATE TABLE `morphic-gaos-prod.aos_logs.task_outcomes`
(
  task_id STRING,
  project_id STRING,
  agent_id STRING,
  task_type STRING,
  status STRING,
  error_fingerprint STRING,
  cost_usd FLOAT64,
  duration_seconds FLOAT64,
  timestamp TIMESTAMP,
  log_date DATE
)
PARTITION BY log_date
OPTIONS (partition_expiration_days = 30);

-- Evolution task outcomes
CREATE TABLE `morphic-gaos-prod.aos_logs.evolution_tasks`
(
  task_id STRING,
  project_id STRING,
  agent_id STRING,
  trigger_reason STRING,
  total_iterations INT64,
  stopping_constraint STRING,
  error_fingerprint STRING,
  total_duration_seconds FLOAT64,
  total_cost_usd FLOAT64,
  escalated BOOL,
  local_fallback BOOL,
  timestamp TIMESTAMP,
  log_date DATE
)
PARTITION BY log_date
OPTIONS (partition_expiration_days = 365);

-- Approval gate history
CREATE TABLE `morphic-gaos-prod.aos_logs.approval_history`
(
  proposal_id STRING,
  project_id STRING,
  agent_id STRING,
  priority INT64,
  status STRING,
  approved_by STRING,
  approver_tier INT64,
  cost_usd FLOAT64,
  timestamp TIMESTAMP,
  log_date DATE
)
PARTITION BY log_date
OPTIONS (partition_expiration_days = 730);

-- Observability log (weekly summaries — raw deleted from Sheet after 7 days)
CREATE TABLE `morphic-gaos-prod.aos_logs.observability_weekly`
(
  week STRING,
  project_id STRING,
  tasks_started INT64,
  tasks_succeeded INT64,
  tasks_escalated INT64,
  top_constraint STRING,
  top_error STRING,
  total_cost_usd FLOAT64,
  archived_at TIMESTAMP
)
OPTIONS (expiration_timestamp = NULL);  -- indefinite; manually managed
```

**Verification:** In BigQuery console, expand `aos_logs` dataset — 4 tables listed with partition configuration visible.

---

## 8. `config/settings.yaml`

Create this file before writing any agent code. It is the single source of truth for model aliases, project IDs, and service configuration.

```yaml
# config/settings.yaml
# Do NOT commit secrets here — all sensitive values go in Secret Manager.
# This file is safe to commit.

gcp:
  project_id: "morphic-gaos-prod"
  region: "us-central1"

sheet:
  workbook_id: "<paste-your-spreadsheet-id-here>"

drive:
  knowledge_folder_id: "<paste-your-knowledge-folder-id-here>"

models:
  LOCAL_MODEL: "ollama/llama3.1"
  LOCAL_MODEL_FALLBACK: "gemini-2.0-flash"
  LOCAL_MODEL_TIMEOUT_SECONDS: 2
  FAST_MODEL: "gemini-2.0-flash"
  DEEP_MODEL: "gemini-2.0-pro"

pubsub:
  topic_prefix: "agent"       # Topics follow: agent.<name>.events
  ack_deadline_seconds: 60

bigquery:
  dataset: "aos_logs"

logging:
  retention_days: 7           # Set in Log Explorer — reduces free tier consumption

cost:
  monthly_budget_alert_usd: 5.00

code_safety:
  allowed_imports:
    - google
    - vertexai
    - langchain
    - pydantic
    - datetime
    - json
    - re
    - math
    - typing
    - collections
    - itertools
    - functools
    - logging
    - gspread
```

---

## 9. Cloud Run Services

### 9.1 Build and Deploy Each Agent

Each agent is packaged as a Cloud Run service. The directory structure per agent:

```
src/agents/tier2/beacon/
├── agent.py          # ADK Agent class
├── Dockerfile
└── requirements.txt  # or pyproject.toml if using uv
```

**Dockerfile template (all agents use this pattern):**

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Copy the full Docs/agents/ directory — identity files must be in the image
COPY Docs/ ./Docs/
COPY config/settings.yaml ./config/settings.yaml
COPY src/agents/tier2/<name>/ ./
COPY tools/ ./tools/

RUN pip install --no-cache-dir \
    google-adk langgraph google-cloud-pubsub google-cloud-secret-manager \
    gspread pydantic google-cloud-logging google-cloud-aiplatform

CMD ["python", "agent.py"]
```

**Deploy command (run once per agent after Dockerfile is ready):**

```bash
PROJECT=morphic-gaos-prod
REGION=us-central1

for agent in nexus-prime ledger beacon pursuit foreman steward scout; do
  gcloud run deploy ${agent} \
    --source src/agents/tier2/${agent}/ \
    --region $REGION \
    --project $PROJECT \
    --service-account ${agent}-sa@${PROJECT}.iam.gserviceaccount.com \
    --memory 512Mi \
    --cpu 1 \
    --timeout 60s \
    --concurrency 10 \
    --min-instances 0 \
    --max-instances 5 \
    --no-allow-unauthenticated \
    --set-secrets "GEMINI_API_KEY=GEMINI_API_KEY:latest,GSHEETS_SERVICE_ACCOUNT=GSHEETS_SERVICE_ACCOUNT:latest,PUBSUB_SERVICE_ACCOUNT=PUBSUB_SERVICE_ACCOUNT:latest,OLLAMA_HOST=OLLAMA_HOST:latest"
done
```

### 9.2 Update Pub/Sub Subscription Endpoints

After Cloud Run deploys, get each service URL and update the subscriptions:

```bash
for agent in nexus-prime ledger beacon pursuit foreman steward scout; do
  URL=$(gcloud run services describe $agent \
    --region=us-central1 --project=$PROJECT \
    --format='value(status.url)')
  echo "${agent}: ${URL}"
done
```

Copy each URL and update subscriptions using the modify-push-config commands from §5.2.

Also update `VERTEX_AGENT_ENDPOINT` in Apps Script properties with the Nexus-Prime Cloud Run URL + `/sync`.

---

## 10. Cloud Scheduler Jobs

### 10.1 TTL Sweep Job (hourly)

Scans `Agent_Approvals` tab for proposals older than their TTL and re-notifies or auto-rejects them.

```bash
gcloud scheduler jobs create http ttl-sweep \
  --location=us-central1 \
  --schedule="0 * * * *" \
  --uri="<nexus-prime-cloud-run-url>/ttl-sweep" \
  --oidc-service-account-email="nexus-prime-sa@morphic-gaos-prod.iam.gserviceaccount.com" \
  --project=morphic-gaos-prod
```

### 10.2 Nightly Archive Job (2:00 AM daily)

Summarizes and moves aged Sheet rows to BigQuery.

```bash
gcloud scheduler jobs create http nightly-archive \
  --location=us-central1 \
  --schedule="0 2 * * *" \
  --uri="<nexus-prime-cloud-run-url>/archive" \
  --oidc-service-account-email="nexus-prime-sa@morphic-gaos-prod.iam.gserviceaccount.com" \
  --project=morphic-gaos-prod
```

**Verification:** In Cloud Scheduler console, both jobs appear. Run each manually by clicking **Force run** — they should return HTTP 200 (even on a stub endpoint).

---

## 11. Cloud Logging — Retention Configuration

Reduce Cloud Logging retention from the default 30 days to 7 days to stay within the free tier (50 GB/month ingestion).

1. Go to Cloud Logging → **Log Storage** (or Logs Router in newer UI)
2. Edit the `_Default` log bucket
3. Set retention period to **7 days**
4. Save

This is a manual one-time step; there is no `gcloud` CLI command for bucket retention changes.

---

## 12. Vertex AI Memory Bank

### 12.1 Create Memory Bank Corpora

One corpus per domain. Nexus-Prime has one global corpus.

```bash
# Run in Cloud Shell or via Python client
python << 'EOF'
from google.cloud import aiplatform
aiplatform.init(project="morphic-gaos-prod", location="us-central1")

domains = ["global", "accounting", "marketing", "sales", 
           "operations", "admin", "research"]
for domain in domains:
    # Create a RAG corpus per domain
    corpus = aiplatform.rag.create_corpus(
        display_name=f"gaos-{domain}",
        description=f"Morphic-G AOS semantic memory — {domain} domain"
    )
    print(f"{domain}: {corpus.name}")
EOF
```

Save the corpus resource names — add them to `settings.yaml` under `memory_bank.corpora` after creation.

---

## 13. Smoke Tests

Phase 1 is complete when all of the following pass. Run them in order.

| # | Test | How to run | Expected result |
|---|------|-----------|-----------------|
| 1 | Sheet write | Run `python -c "from tools.google_sheets import init_sheets_client, append_row; init_sheets_client('default'); append_row('Logs', {'timestamp': '2026-03-14', 'agent_id': 'test', 'message': 'smoke test'}, 'default')"` | Row appears in `Logs` tab |
| 2 | Sheet read | Run `python -c "from tools.google_sheets import init_sheets_client, get_all_records; init_sheets_client('default'); print(get_all_records('Project Registry', 'default'))"` | Returns list with the `default` project row |
| 3 | Pub/Sub publish | Run `python -c "from tools.pubsub import publish; from models import A2AMessage; ..."` sending a test message to `agent.nexus-prime.events` | Message ID returned; no exception |
| 4 | Pub/Sub receive | Manually trigger the Apps Script `onChange` by editing a Status cell | Local Python subscriber (or Cloud Run log) shows the event within 5 seconds |
| 5 | Secret access | Run `python -c "from tools.secrets import get_secret; print(get_secret('GEMINI_API_KEY', 'morphic-gaos-prod'))"` | API key printed (first 8 chars only — enough to confirm it's not empty) |
| 6 | Webhook HMAC | POST a correctly signed payload to the Apps Script Web App URL | HTTP 200; row appears in `Agent_Approvals` |
| 7 | Webhook rejection | POST with a tampered signature | HTTP 401; `HMAC_FAILURE` appears in `Logs` tab |

Run all 8 webhook-specific tests from `GAOS-Manager-Spec.md §14` after test 7 passes.

---

## 14. Phase 1 Exit Criteria Checklist

Phase 1 is complete — and Phase 2 (Ollama integration) may begin — when **every item** below is checked:

- [ ] `tools/google_sheets.py` appends a row and reads a cell value without errors
- [ ] Apps Script `onChange` trigger fires on Status cell change and publishes to `agent.approvals.events`
- [ ] Local Python subscriber receives the Pub/Sub push and prints the proposal ID and new status
- [ ] Cloud Scheduler TTL sweep job exists and can be triggered manually (HTTP 200 response)
- [ ] Cloud Scheduler nightly archive job exists and can be triggered manually (HTTP 200 response)
- [ ] All 7 smoke tests above are passing
- [ ] All 8 webhook tests from `GAOS-Manager-Spec.md §14` are passing
- [ ] `setupProtections()` has been run; Status/Code/Hash columns are locked to owner
- [ ] Authorized Approvers tab has at least one row (owner, tier 5, active=TRUE)
- [ ] `settings.yaml` is complete with correct `workbook_id`, `knowledge_folder_id`, and model aliases
- [ ] `WEBHOOK_URL` secret is populated in Secret Manager
- [ ] BigQuery dataset and 4 tables created with TTL partitioning
- [ ] All Knowledge/ seed files created in Drive
- [ ] Cloud Logging retention reduced to 7 days

---

## 15. Reference Index

| Topic | Location |
|-------|----------|
| Secret inventory (full list) | `GAOS-Manager-Spec.md` §15.1 |
| Webhook HMAC threat model and test matrix | `GAOS-Manager-Spec.md` §15.2 |
| Approval RBAC and `onChange` handler code | `GAOS-Manager-Spec.md` §15.3 |
| Code injection prevention + `syncSkillsToVertex` code | `GAOS-Manager-Spec.md` §15.4 |
| Pub/Sub topic topology | `GAOS-Manager-Spec.md` §10.1 |
| Cost estimates and free tier limits | `GAOS-Manager-Spec.md` §9.4 |
| Data retention schedule | `GAOS-Manager-Spec.md` §9.5 |
| Agent boot sequence | `GAOS-Agent-Spec.md` §6 |
| Tool module API reference | `GAOS-Tools-Spec.md` |
| Memory Bank corpus usage | `GAOS-Memory-Spec.md` §6 |
| Nexus-Prime construction requirements | `GAOS-Nexus-Prime-Spec.md` |
