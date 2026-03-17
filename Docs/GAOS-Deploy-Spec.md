# GAOS Deployment Specification

**Morphic-G AOS** — Infrastructure Provisioning & First-Run Guide

> This document is the step-by-step guide for standing up every Google Cloud and Google Workspace resource the system requires. Follow the sections in order. Each section ends with a verification step — do not proceed to the next section until verification passes.
>
> **Estimated time:** 2–3 hours for a first-time setup, 30 minutes for a repeat deployment.
> **Estimated monthly cost after setup:** ≈ $2.50 (see `GAOS-Manager-Spec.md` §9.4 for breakdown).

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

# Create Python environment and install all dependencies (including dev tools)
uv venv
uv pip install -e ".[dev]"
```

> **Note:** All package versions are pinned in `pyproject.toml`. `[dev]` adds `pytest`, `pytest-cov`, `ruff`, and `mypy`. Omit `[dev]` in a production-only environment: `uv pip install -e .`. Use `google-genai>=1.0.0` (`google.genai.Client()` API) — **not** `google-generativeai`. The `google-generativeai` package is EOL: it imports with a `FutureWarning` and the `v1beta` endpoint it targets no longer serves models like `gemini-1.5-pro`, returning 404. The `google-genai` package is the official successor and is what `google-adk` expects.

### 0.4 Application Default Credentials (ADC) Setup

All local development runs use ADC — **no service account key file on disk**. `GOOGLE_APPLICATION_CREDENTIALS` must NOT be set in your environment or `.env` file. If that variable exists (even pointing at a missing file), `google-auth` skips ADC entirely and fails silently.

```powershell
# Verify the variable is not set — remove it if it is
$env:GOOGLE_APPLICATION_CREDENTIALS   # should return nothing

# Step 1: Configure OAuth Consent Screen in YOUR GCP project
# Go to: console.cloud.google.com → APIs & Services → OAuth consent screen
# User type: External → Create
# App name: morphic-g-aos  (use this exact name)
# Support email: your GCP account email → Save and Continue
# Skip Scopes step → add yourself as a Test User → Back to Dashboard

# Step 2: Create an OAuth 2.0 Desktop Client ID
# APIs & Services → Credentials → + Create Credentials → OAuth client ID
# Application type: Desktop app  |  Name: morphic-gaos-desktop
# Download the JSON file and save as oauth-client.json (gitignored)

# Step 3: Log in with your Desktop client and request the required scopes
# Do NOT use --no-browser — Desktop clients require a redirect_uri which only
# the browser flow provides. Without it Google returns: Error 400 invalid_request.
# The DEFAULT gcloud client ID BLOCKS the spreadsheets scope — you MUST use your own.
_G="https://www.googleapis.com"
SCOPES="${_G}/auth/spreadsheets,${_G}/auth/drive"
SCOPES="${SCOPES},${_G}/auth/script.projects,${_G}/auth/script.deployments"
SCOPES="${SCOPES},${_G}/auth/cloud-platform"
gcloud auth application-default login --client-id-file=oauth-client.json --scopes="$SCOPES"

# Step 4: Set the quota project (gcloud login drops this field every time)
# The credentials file is at:
#   Windows: %APPDATA%\gcloud\application_default_credentials.json
#   Linux/Mac: ~/.config/gcloud/application_default_credentials.json
# Add "quota_project_id": "morphic-gaos-prod" to the JSON, or run:
gcloud auth application-default set-quota-project morphic-gaos-prod

# Step 5: Verify ADC is working
python -c "import google.auth; from google.auth.transport.requests import Request; creds, proj = google.auth.default(scopes=['https://www.googleapis.com' + '/auth/spreadsheets']); creds.refresh(Request()); print('ADC OK — project:', proj)"
```

> **Note:** Every time you re-run `gcloud auth application-default login`, it overwrites the credentials file and drops `quota_project_id`. Re-run Step 4 after any re-login.

> **What this does:** All Google Cloud libraries (`google-auth`, `gspread`, `google-genai`, `google-cloud-pubsub`) automatically pick up the credential written to `~/.config/gcloud/application_default_credentials.json`. You do not need a service account key file locally. On Cloud Run, the attached service account identity provides credentials automatically — see §2.3.

### 0.5 GAOS-Doctor Setup

The GAOS-Doctor tool is a critical utility for maintaining the health and consistency of the GAOS system. Follow these steps to set up and integrate GAOS-Doctor:

1. **Install GAOS-Doctor**:
   Ensure the `gaos-doctor` CLI tool is installed and accessible in your environment. This tool is included in the Morphic-GAOS-Manager repository and can be installed as part of the Python dependencies.

   ```powershell
   uv pip install -e ".[dev]"
   ```

2. **Verify Installation**:
   Run the following command to confirm the tool is installed correctly:

   ```powershell
   gaos-doctor --version
   ```

3. **Run Initial Health Check**:
   Execute a full system health check to ensure all prerequisites are met:

   ```powershell
   gaos-doctor --deep
   ```

4. **Integrate with CI/CD**:
   Add `gaos-doctor` to your CI/CD pipeline to automate health checks during deployments. Example:

   ```yaml
   - name: Run GAOS-Doctor
     run: |
       gaos-doctor --non-interactive
   ```

5. **Documentation**:
   Refer to `Docs/GAOS-Doctor.md` for detailed usage instructions and advanced options.

### 0.6 Google Sheets Dashboard Setup

The Google Sheets dashboard is a critical component for monitoring the health and performance of the GAOS system. Follow these steps to configure the dashboard:

1. **Create the Dashboard**:
   - Open Google Sheets and create a new spreadsheet named `GAOS Dashboard`.
   - Share the sheet with the service account email used by the GAOS system.

2. **Add Monitoring Sections**:
   - **Agent Status Overview**:
     - Create a sheet tab named `Agent Status`.
     - Add columns: `Agent Name`, `Current Status`, `Last Activity`, `Pending Tasks`.
   - **System Alerts**:
     - Create a sheet tab named `System Alerts`.
     - Add columns: `Alert Type`, `Component`, `Timestamp`, `Resolution Status`.
   - **Task Queue Metrics**:
     - Create a sheet tab named `Task Queue`.
     - Add columns: `Task ID`, `Assigned Agent`, `Task Type`, `Status`, `Time in Queue`.
   - **Resource Utilization**:
     - Create a sheet tab named `Resource Utilization`.
     - Add columns: `Resource Type`, `Usage`, `Quota Limit`, `% Utilized`.
   - **Approval Queue**:
     - Create a sheet tab named `Approval Queue`.
     - Add columns: `Request ID`, `Request Type`, `Submitted By`, `Status`, `Timestamp`.
   - **Error Logs**:
     - Create a sheet tab named `Error Logs`.
     - Add columns: `Error ID`, `Component`, `Error Message`, `Timestamp`, `Resolution Notes`.
   - **Performance Metrics**:
     - Create a sheet tab named `Performance Metrics`.
     - Add columns: `Metric Name`, `Current Value`, `Threshold`, `Status`.
   - **System Uptime**:
     - Create a sheet tab named `System Uptime`.
     - Add columns: `Component`, `Uptime Percentage`, `Last Downtime`, `Downtime Duration`.

3. **Integrate with GAOS System**:
   - Use the `google-sheets` tool module to write data to the appropriate tabs.
   - Schedule periodic updates using Cloud Scheduler to ensure real-time data.

4. **Add Visualizations**:
   - Create charts for task trends, resource utilization, and error frequency.
   - Use conditional formatting to highlight critical statuses (e.g., errors, high resource usage).

5. **Verify Setup**:
   - Ensure all tabs are populated with test data from the GAOS system.
   - Validate that the service account has the necessary permissions to update the sheet.

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
  cloudresourcemanager.googleapis.com \
  chat.googleapis.com \
  customsearch.googleapis.com \
  discoveryengine.googleapis.com
```

**Verification:** `gcloud services list --enabled` — all 14 services must appear.

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
    --member="serviceAccount:${SA}" --role="roles/bigquery.dataEditor"
done

# ── Apps Script SA ────────────────────────────────────────────────────────
# No project-level IAM needed. Sheets and Drive access for apps-script-sa is
# granted at the file level when setup_workspace.py shares the root Drive folder
# with all service account emails (see §4.1). No gcloud command required here.
```

### 2.3 Service Account Identity

No JSON key files are needed. Service account identity is supplied at runtime:

- **Local development:** `gcloud auth application-default login` (see §0.4) provides ADC credentials. The tool layer calls `google.auth.default()` which picks these up automatically.
- **Cloud Run:** Each Cloud Run service is deployed with `--service-account=<agent>-sa@${PROJECT}.iam.gserviceaccount.com`. The GCE metadata server provides credentials — no key file required.

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
#
# ⚠️  Before creating the key, enable the Generative Language API in the SAME
#    project the key will be created in:
#    gcloud services enable generativelanguage.googleapis.com --project=$PROJECT
#
# ⚠️  Do NOT paste the key value into the terminal or this chat — it will be
#    logged. Instead, save it to a temp file in VS Code, then run:
#      gcloud secrets versions add GEMINI_API_KEY --data-file=tmp_key.txt --project=$PROJECT
#      Remove-Item tmp_key.txt   # (PowerShell) or: rm tmp_key.txt
gcloud secrets create GEMINI_API_KEY --project=$PROJECT
# Save key to tmp_key.txt in VS Code editor, then:
# gcloud secrets versions add GEMINI_API_KEY --data-file=tmp_key.txt --project=$PROJECT
# Remove-Item tmp_key.txt

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

# WEBHOOK_URL — created automatically by scripts/setup_apps_script.py Phase 1
# No manual step needed here; the script creates the secret and stores the URL.

# Google Custom Search — Scout's KNOWLEDGE_INJECTION deep research (Phase 2.5 Step 6)
# Get the API key from: console.cloud.google.com/apis/credentials
# Get the CX (Search Engine ID) from: programmablesearchengine.google.com
gcloud secrets create GOOGLE_SEARCH_API_KEY --project=$PROJECT
# Save key to tmp_key.txt in VS Code editor, then:
# gcloud secrets versions add GOOGLE_SEARCH_API_KEY --data-file=tmp_key.txt --project=$PROJECT
# Remove-Item tmp_key.txt
gcloud secrets create GOOGLE_SEARCH_CX --project=$PROJECT
# Save CX to tmp_cx.txt in VS Code editor, then:
# gcloud secrets versions add GOOGLE_SEARCH_CX --data-file=tmp_cx.txt --project=$PROJECT
# Remove-Item tmp_cx.txt
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

# GOOGLE_SEARCH_API_KEY / GOOGLE_SEARCH_CX — scout only (KNOWLEDGE_INJECTION research)
for secret in GOOGLE_SEARCH_API_KEY GOOGLE_SEARCH_CX; do
  gcloud secrets add-iam-policy-binding $secret \
    --member="serviceAccount:scout-sa@${PROJECT}.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor" --project=$PROJECT
done
```

**Verification:** `gcloud secrets list --project=$PROJECT` — 6 secrets listed (WEBHOOK_URL added by setup_apps_script.py Phase 1).

---

## 4. Google Sheets Workbook

### 4.1 Run the Workspace Setup Script

Sections 4 and 6 are fully automated. A single script creates the Drive folder
structure, the spreadsheet (all 14 tabs + headers), the Knowledge/ subfolders,
and shares everything with all service accounts.

```powershell
# Prerequisites: ADC configured (§0.4), .venv activated
python scripts/setup_workspace.py
```

The script creates this structure in Google Drive:

```
Morphic-G AOS/                       ← project root folder
├── Morphic-G AOS — Control Plane    ← spreadsheet (14 tabs + headers)
└── Knowledge/                       ← §6 knowledge folder
    ├── workflows/
    ├── procedures/
    ├── policies/
    └── archive/
```

When it finishes it prints the IDs needed for `config/settings.yaml`:

```
============================================================
SUCCESS — add these to config/settings.yaml:
============================================================
  sheet.workbook_id:                <spreadsheet-id>
  projects.default.sheet_id:        <spreadsheet-id>
  projects.default.drive_folder_id: <knowledge-folder-id>
============================================================
```

Copy those values into `config/settings.yaml` (created from the template — see §0.3).

### 4.2 Tab Reference

The script creates all 14 tabs with headers. For reference:

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

### 4.3 Add Your Owner Row to Authorized Approvers

The script creates the tab and header row. Add your owner row manually in row 2:

```
your@email.com | Your Name | 5 | TRUE | <today> | Owner
```

### 4.4 Deploy Apps Script (Automated)

Run the setup script — it creates the bound project, uploads all `.gs` files,
deploys the Web App, and stores `WEBHOOK_URL` in Secret Manager:

```powershell
python scripts/setup_apps_script.py
```

**Phase 1** (create + upload + deploy) runs fully. At the end it opens the
Apps Script editor in your browser for a **one-time OAuth consent click**.
After clicking Allow, the web app is live and `WEBHOOK_URL` is in Secret Manager.

> **Phase 2 (Script Properties, trigger, protections) must be completed manually.**
> The `scripts.run()` API requires interactive OAuth credentials — it cannot be
> called with ADC or service account tokens. Attempting `--post-auth` will return
> HTTP 403. Complete the following steps in the Apps Script editor instead:

**Step 1 — Set Script Properties** (Apps Script editor → Project Settings → Script Properties):

| Key | Value |
|-----|-------|
| `WEBHOOK_HMAC_SECRET` | Value of `gcloud secrets versions access latest --secret=WEBHOOK_HMAC_SECRET --project=morphic-gaos-prod` |
| `WEBHOOK_URL` | Value of `gcloud secrets versions access latest --secret=WEBHOOK_URL --project=morphic-gaos-prod` |
| `VERTEX_AGENT_ENDPOINT` | `https://nexus-prime-975461050387.us-central1.run.app/sync` |
| `GCP_PROJECT` | `morphic-gaos-prod` |

> ⚠️ **Key names are case-sensitive and must use underscores, not hyphens.**
> `WEBHOOK_HMAC_SECRET` and `GCP_PROJECT` are correct. `WEBHOOK_HMAC-SECRET`
> or `GCP_PROPERTY` will silently return `null` and cause `doPost` to throw
> `Internal error (500)` on every request.

**Step 2 — Run `setupProtections`** (function dropdown → `setupProtections` → Run). Locks Status, Proposed Code, and Code SHA-256 columns + entire Authorized Approvers tab to owner-only edit.

**Step 3 — Install the `onChange` trigger** (Triggers → Add Trigger → Function: `onChangeApproval` | Event type: On change).

> **`doPost` web-app context note:** `SpreadsheetApp.getActiveSpreadsheet()` returns
> `null` when called from a deployed web-app `doPost` endpoint (as opposed to an
> interactive editor session). All spreadsheet access in `helpers.gs` uses
> `getSpreadsheet_()` (which calls `openById(SPREADSHEET_ID_)`) so the web-app
> context works correctly. The `SPREADSHEET_ID_` constant is hardcoded in
> `helpers.gs` and matches `sheet.workbook_id` in `settings.yaml`.

### 4.5 Run Protection Setup

Run `setupProtections` manually in the Apps Script editor (see §4.4 Step 2).
The function locks:
- Column I (Status) on `Agent_Approvals` → owner only
- Column H (Proposed Code) on `Agent_Approvals` → owner only
- Column M (code_sha256) on `Agent_Approvals` → owner only
- Entire `Authorized Approvers` tab → owner only

### 4.6 Install the `onChange` Trigger

Install manually in the Apps Script editor (see §4.4 Step 3):
Triggers → Add Trigger → Function: `onChangeApproval` | Event type: On change.

### 4.7 Deploy the Webhook as a Web App

Handled automatically by Phase 1. The Web App URL is stored in Secret Manager
as `WEBHOOK_URL` and in `config/settings.yaml` under `apps_script.webhook_url`.

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

> **Do this step after §9 (Cloud Run deploy).** Push subscriptions require real Cloud Run URLs. Create topics now (§5.1), then return here once the 7 services are deployed and you have their URLs.

Subscriptions use **OIDC push authentication** — Pub/Sub signs each delivery request with a service account token that Cloud Run verifies. This requires a dedicated push SA and invoker grants on every service.

**Step A — Create the push service account:**

```bash
PROJECT=morphic-gaos-prod
gcloud iam service-accounts create pubsub-push-sa \
  --display-name="Pub/Sub Push Auth" --project=$PROJECT
```

**Step B — Grant it `roles/run.invoker` on every Cloud Run service:**

```bash
PROJECT_NUM=$(gcloud projects describe morphic-gaos-prod --format='value(projectNumber)')
PUSH_SA="pubsub-push-sa@${PROJECT}.iam.gserviceaccount.com"

for agent in nexus-prime ledger beacon pursuit foreman steward scout; do
  gcloud run services add-iam-policy-binding $agent \
    --region=us-central1 --project=$PROJECT \
    --member="serviceAccount:${PUSH_SA}" \
    --role="roles/run.invoker"
done
```

**Step C — Also grant the Pub/Sub service agent token creator rights** (required for OIDC token generation):

```bash
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:service-${PROJECT_NUM}@gcp-sa-pubsub.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountTokenCreator"
```

**Step D — Create all 22 subscriptions:**

```bash
PROJECT=morphic-gaos-prod
BASE="projects/${PROJECT}/topics"
PUSH_SA="pubsub-push-sa@${PROJECT}.iam.gserviceaccount.com"

declare -A URLS=(
  [nexus-prime]="https://nexus-prime-975461050387.us-central1.run.app"
  [ledger]="https://ledger-975461050387.us-central1.run.app"
  [beacon]="https://beacon-975461050387.us-central1.run.app"
  [pursuit]="https://pursuit-975461050387.us-central1.run.app"
  [foreman]="https://foreman-975461050387.us-central1.run.app"
  [steward]="https://steward-975461050387.us-central1.run.app"
  [scout]="https://scout-975461050387.us-central1.run.app"
)

# Nexus-Prime subscribes to all orchestrator topics
for topic in ledger beacon pursuit foreman steward scout; do
  gcloud pubsub subscriptions create nexus-prime.sub.${topic} \
    --topic="${BASE}/agent.${topic}.events" \
    --push-endpoint="${URLS[nexus-prime]}/pubsub" \
    --push-auth-service-account=$PUSH_SA \
    --ack-deadline=60 --project=$PROJECT
done

# All agents subscribe to approvals
for agent in nexus-prime ledger beacon pursuit foreman steward scout; do
  gcloud pubsub subscriptions create ${agent}.sub.approvals \
    --topic="${BASE}/agent.approvals.events" \
    --push-endpoint="${URLS[$agent]}/pubsub" \
    --push-auth-service-account=$PUSH_SA \
    --ack-deadline=60 --project=$PROJECT
done

# Cross-domain subscriptions
for sub_agent_topic in \
  "pursuit.sub.ledger:pursuit:agent.ledger.events" \
  "pursuit.sub.beacon:pursuit:agent.beacon.events" \
  "pursuit.sub.foreman:pursuit:agent.foreman.events" \
  "ledger.sub.pursuit:ledger:agent.pursuit.events" \
  "ledger.sub.foreman:ledger:agent.foreman.events" \
  "foreman.sub.pursuit:foreman:agent.pursuit.events" \
  "beacon.sub.pursuit:beacon:agent.pursuit.events" \
  "beacon.sub.scout:beacon:agent.scout.events" \
  "scout.sub.foreman:scout:agent.foreman.events"; do
    IFS=: read name subscriber topic <<< "$sub_agent_topic"
    gcloud pubsub subscriptions create $name \
      --topic="${BASE}/${topic}" \
      --push-endpoint="${URLS[$subscriber]}/pubsub" \
      --push-auth-service-account=$PUSH_SA \
      --ack-deadline=60 --project=$PROJECT
done
```

**Verification:** `gcloud pubsub subscriptions list --project=$PROJECT --format="table(name,pushConfig.pushEndpoint)"` — 22 subscriptions listed, all pointing to `*.run.app/pubsub`.

---

## 6. Google Drive — Knowledge/ Folder

### 6.1 Folder Structure

The `scripts/setup_workspace.py` script (run in §4.1) already created the full
folder tree and shared it with all service accounts. No manual steps needed here.

```
Morphic-G AOS/              ← root folder (shared: all 7 SAs — writer)
└── Knowledge/              ← drive_folder_id in settings.yaml
    ├── workflows/           # Multi-step process documents
    ├── procedures/          # Step-by-step instructions for specific tasks
    ├── policies/            # Rules agents must follow
    └── archive/             # Old versions of updated documents
```

The Knowledge/ folder ID was printed by the setup script and is already in
`config/settings.yaml` under `projects.default.drive_folder_id`.

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

### 6.3 Service Account Access

Already handled by `scripts/setup_workspace.py` — all 7 SAs were granted writer
access to the root `Morphic-G AOS/` folder, which the Knowledge/ subfolder inherits.

To verify: open the `Morphic-G AOS` folder in Drive → right-click → Share → confirm
all SA emails appear with Editor access.

---

## 7. BigQuery

> **Windows note:** The `bq` CLI fails on Windows (Python `absl.flags` conflict in the bundled
> Cloud SDK). Use the Python approach below for all BigQuery provisioning.

### 7.1 Create Dataset and Tables (automated)

All BigQuery infrastructure is provisioned by a single Python command:

```powershell
# Prerequisites: .venv activated, ADC configured
python -c "
from google.cloud import bigquery
client = bigquery.Client(project='morphic-gaos-prod')
ds = bigquery.Dataset('morphic-gaos-prod.aos_logs')
ds.location = 'US'
ds.description = 'AOS cold log storage and historical analytics'
client.create_dataset(ds, exists_ok=True)
print('Dataset ready')
"
```

Then create all 6 tables:

| Table | Partition | TTL | Purpose |
|-------|-----------|-----|---------|
| `task_outcomes` | `log_date` | 30 days | Episodic memory source — every task result |
| `evolution_tasks` | `log_date` | 365 days | Self-evolution loop outcomes |
| `approval_history` | `log_date` | 730 days | Full approval gate history |
| `observability_weekly` | — | indefinite | Weekly summary archive |
| `memory_entries` | — | indefinite | Structured metadata for Vertex AI RAG entries |
| `monologue_frames` | `log_date` | 90 days | Think node reasoning traces (`GAOS-Persona-Spec.md` §4) |

The `memory_entries` table holds the structured `MemoryEntry` metadata (agent_id, knowledge_type,
active flag, version, etc.) so that `load_domain_memory()` can filter by agent and active status
without querying Vertex AI. The Vertex AI RAG corpus stores the raw text for semantic retrieval.

```python
# Schema: memory_entries
from google.cloud import bigquery
client = bigquery.Client(project='morphic-gaos-prod')
table = bigquery.Table(
    'morphic-gaos-prod.aos_logs.memory_entries',
    schema=[
        bigquery.SchemaField('memory_id', 'STRING'),
        bigquery.SchemaField('project_id', 'STRING'),
        bigquery.SchemaField('agent_id', 'STRING'),
        bigquery.SchemaField('knowledge_type', 'STRING'),
        bigquery.SchemaField('domain', 'STRING'),
        bigquery.SchemaField('content', 'STRING'),
        bigquery.SchemaField('confidence', 'FLOAT64'),
        bigquery.SchemaField('version', 'INT64'),
        bigquery.SchemaField('supersedes', 'STRING'),
        bigquery.SchemaField('active', 'BOOL'),
        bigquery.SchemaField('tags', 'STRING'),       # JSON array as string
        bigquery.SchemaField('approved_by', 'STRING'),
        bigquery.SchemaField('approved_at', 'TIMESTAMP'),
    ],
)
client.create_table(table, exists_ok=True)
print('memory_entries: created')
```

```python
# Schema: monologue_frames
from google.cloud import bigquery
client = bigquery.Client(project='morphic-gaos-prod')
table = bigquery.Table(
    'morphic-gaos-prod.aos_logs.monologue_frames',
    schema=[
        bigquery.SchemaField('frame_id', 'STRING'),
        bigquery.SchemaField('project_id', 'STRING'),
        bigquery.SchemaField('task_id', 'STRING'),
        bigquery.SchemaField('response_mode', 'STRING'),
        bigquery.SchemaField('reasoning_summary', 'STRING'),
        bigquery.SchemaField('log_date', 'DATE'),
        bigquery.SchemaField('created_at', 'TIMESTAMP'),
    ],
)
table.time_partitioning = bigquery.TimePartitioning(
    field='log_date', expiration_ms=90 * 86_400_000,
)
client.create_table(table, exists_ok=True)
print('monologue_frames: created')
```

**Verification:** In BigQuery console, expand `aos_logs` dataset — 6 tables listed.

---

## 8. `config/settings.yaml`

Create this file before writing any agent code. It is the single source of truth for model aliases, project IDs, and service configuration.

```yaml
# config/settings.yaml
# Do NOT commit secrets here — all sensitive values go in Secret Manager.
# This file is safe to commit.
# See config/settings.yaml.template for a blank starter.

gcp:
  project_id: "morphic-gaos-prod"
  region: "us-central1"

sheet:
  workbook_id: "<your-spreadsheet-id>"   # from setup_workspace.py output

projects:
  default:
    sheet_id: "<your-spreadsheet-id>"
    drive_folder_id: "<your-knowledge-folder-id>"  # from setup_workspace.py output

models:
  LOCAL_MODEL: "ollama/llama3.1"
  LOCAL_MODEL_FALLBACK: "gemini-2.0-flash"
  LOCAL_MODEL_TIMEOUT_SECONDS: 2
  FAST_MODEL: "gemini-2.0-flash"
  DEEP_MODEL: "gemini-2.0-pro"

pubsub:
  all_topics:                   # Nexus-Prime validates these exist at boot
    - "agent.nexus-prime.events"
    - "agent.ledger.events"
    - "agent.beacon.events"
    - "agent.pursuit.events"
    - "agent.foreman.events"
    - "agent.steward.events"
    - "agent.scout.events"
    - "agent.approvals.events"

bigquery:
  dataset: "aos_logs"

# Vertex AI RAG corpora — populated automatically by scripts/_create_corpora.py
# Region note: us-central1/us-east1/us-east4 restricted for new projects;
# use us-west1 or see https://cloud.google.com/vertex-ai/generative-ai/docs/rag-engine/rag-overview#supported-regions
memory_bank:
  region: "us-west1"
  corpora:
    global: ""        # projects/<num>/locations/us-west1/ragCorpora/<id>
    accounting: ""
    marketing: ""
    sales: ""
    operations: ""
    admin: ""
    research: ""

# Apps Script — populated automatically by scripts/setup_apps_script.py Phase 1
apps_script:
  script_id: ""
  deployment_id: ""
  webhook_url: ""
```

---

## 9. Cloud Run Services

### 9.1 Build and Deploy Each Agent

All 7 agents share a single codebase and a single Dockerfile at the project root. `main.py` is the FastAPI entry point for every service. The `AGENT_NAME` environment variable tells it which orchestrator to instantiate.

> **`--concurrency 1` is mandatory.** LangGraph maintains in-memory graph state. Allowing multiple concurrent requests on one instance would corrupt state across invocations. The `CMD` in the Dockerfile enforces `--workers 1` for the same reason — do not override this.

**Prerequisites — run once before first deploy:**

```bash
PROJECT=morphic-gaos-prod
PROJECT_NUM=$(gcloud projects describe $PROJECT --format='value(projectNumber)')
COMPUTE_SA="${PROJECT_NUM}-compute@developer.gserviceaccount.com"

# Enable required APIs
gcloud services enable \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  storage.googleapis.com \
  storage-component.googleapis.com \
  --project=$PROJECT

# Create the Artifact Registry Docker repo (Cloud Build pushes images here)
gcloud artifacts repositories create cloud-run-source-deploy \
  --repository-format=docker \
  --location=us-central1 \
  --project=$PROJECT

# Cloud Build workers run as the Compute Engine default SA — grant it storage + registry access
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:${COMPUTE_SA}" --role="roles/storage.objectAdmin"
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:${COMPUTE_SA}" --role="roles/artifactregistry.writer"
```

**Dockerfile (project root — committed to repo):**

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
```

All dependencies come from `pyproject.toml` via `pip install .`. `config/settings.yaml` is included in the image via `.gcloudignore` (which intentionally does not exclude it, unlike `.gitignore`).

**Step 1 — Build the image once** (this takes ~5 minutes):

```bash
IMAGE="us-central1-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy/gaos-agent:latest"
gcloud builds submit --tag $IMAGE --project=$PROJECT .
```

**Step 2 — Deploy all 7 services from the pre-built image** (fast — no rebuild):

```bash
IMAGE="us-central1-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy/gaos-agent:latest"
REGION=us-central1

for agent in nexus-prime ledger beacon pursuit foreman steward scout; do
  gcloud run deploy ${agent} \
    --image $IMAGE \
    --region $REGION \
    --project $PROJECT \
    --service-account ${agent}-sa@${PROJECT}.iam.gserviceaccount.com \
    --memory 512Mi \
    --cpu 1 \
    --timeout 60s \
    --concurrency 1 \
    --min-instances 0 \
    --max-instances 5 \
    --no-allow-unauthenticated \
    --set-env-vars AGENT_NAME=${agent}
done
```

> **Note:** Secrets (`GEMINI_API_KEY`, etc.) are **not** injected as environment variables. Each agent fetches secrets at boot via the Secret Manager API using its service account identity. `--set-secrets` is not needed.

Each service exposes seven endpoints:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/pubsub` | POST | Pub/Sub push subscription delivery |
| `/ttl-sweep` | POST | Cloud Scheduler hourly TTL sweep (Nexus-Prime only) |
| `/sync` | POST | Apps Script approval callback (Nexus-Prime only) |
| `/archive` | POST | Cloud Scheduler nightly archive sweep (Nexus-Prime only) |
| `/daily-sync` | POST | Cloud Scheduler 6 AM morning briefing (Nexus-Prime only) |
| `/chat` | POST | Google Chat push events — messages and card callbacks (Nexus-Prime only) |
| `/health` | GET | Liveness probe — always returns `{"status":"ok"}` |

All POST endpoints require a `Bearer` token in the `Authorization` header. Cloud Run ingress validates the OIDC token before the request reaches the handler; the handler check is defense-in-depth only.

### 9.2 Post-Deploy Wiring

**Get the deployed service URLs:**

```bash
for agent in nexus-prime ledger beacon pursuit foreman steward scout; do
  URL=$(gcloud run services describe $agent \
    --region=us-central1 --project=$PROJECT \
    --format='value(status.url)')
  echo "${agent}: ${URL}"
done
```

Actual URLs for this deployment:

| Service | URL |
|---------|-----|
| nexus-prime | `https://nexus-prime-975461050387.us-central1.run.app` |
| ledger | `https://ledger-975461050387.us-central1.run.app` |
| beacon | `https://beacon-975461050387.us-central1.run.app` |
| pursuit | `https://pursuit-975461050387.us-central1.run.app` |
| foreman | `https://foreman-975461050387.us-central1.run.app` |
| steward | `https://steward-975461050387.us-central1.run.app` |
| scout | `https://scout-975461050387.us-central1.run.app` |

**Now create Pub/Sub subscriptions (§5.2)** — they require Cloud Run URLs and must be created after this step.

**Update `VERTEX_AGENT_ENDPOINT` in Apps Script:**

Set this Script Property in the Apps Script editor (Project Settings → Script Properties):
- Key: `VERTEX_AGENT_ENDPOINT`
- Value: `https://nexus-prime-975461050387.us-central1.run.app/sync`

This was completed as part of §4.4 Step 1 if that section was followed. If not already set, add it now.

**Verification (Windows PowerShell):**
```powershell
# --include-email is required when calling identity-token with ADC user credentials
foreach ($agent in @('nexus-prime','ledger','beacon','pursuit','foreman','steward','scout')) {
  $url = "https://${agent}-975461050387.us-central1.run.app/health"
  $token = & "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd" auth print-identity-token --include-email
  $resp = Invoke-RestMethod -Uri $url -Headers @{Authorization="Bearer $token"}
  Write-Host "${agent}: $($resp.status)"
}
```
Expected: `ok` for each of the 7 services.

---

## 10. Cloud Scheduler Jobs

Cloud Scheduler delivers requests via OIDC. The Nexus-Prime service account issues the token; it must also have `roles/run.invoker` on the nexus-prime Cloud Run service so the token is accepted.

**Grant Nexus-Prime SA invoker rights on its own service:**

```bash
PROJECT=morphic-gaos-prod
gcloud run services add-iam-policy-binding nexus-prime \
  --region=us-central1 --project=$PROJECT \
  --member="serviceAccount:nexus-prime-sa@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

### 10.1 TTL Sweep Job (hourly)

Scans `Agent_Approvals` tab for proposals older than their TTL and re-notifies or auto-rejects them.

```bash
NP_URL="https://nexus-prime-975461050387.us-central1.run.app"
gcloud scheduler jobs create http ttl-sweep \
  --location=us-central1 \
  --schedule="0 * * * *" \
  --uri="${NP_URL}/ttl-sweep" \
  --oidc-service-account-email="nexus-prime-sa@morphic-gaos-prod.iam.gserviceaccount.com" \
  --project=morphic-gaos-prod
```

### 10.2 Nightly Archive Job (2:00 AM daily)

Summarizes and moves aged Sheet rows to BigQuery.

```bash
NP_URL="https://nexus-prime-975461050387.us-central1.run.app"
gcloud scheduler jobs create http nightly-archive \
  --location=us-central1 \
  --schedule="0 2 * * *" \
  --uri="${NP_URL}/archive" \
  --oidc-service-account-email="nexus-prime-sa@morphic-gaos-prod.iam.gserviceaccount.com" \
  --project=morphic-gaos-prod
```

**Verification:** In Cloud Scheduler console, both jobs appear with state `ENABLED`. Run each manually by clicking **Force run** — both should return HTTP 200. `ttl-sweep` hits `POST /ttl-sweep`; `nightly-archive` hits `POST /archive` (implemented in Phase 2 Item 3 — archives aged Sheet rows to BigQuery).

### 10.3 Daily Kickoff Job (6:00 AM daily) — *Phase 2.5 Step 2*

Triggers Nexus-Prime's morning briefing. Nexus-Prime queries overnight Logs, Error Logs, and pending Agent_Approvals rows, then posts a briefing Chat card to the owner's space configured in `settings.yaml` under `chat.owner_space`.

**Prerequisite:** Set `chat.owner_space` in `settings.yaml` to the owner's DM space resource name (e.g. `spaces/AAAAXXXXXXX`). Find this value in any inbound `/chat` event payload under `event.space.name`, or in the Google Chat API console.

```bash
NP_URL="https://nexus-prime-975461050387.us-central1.run.app"
gcloud scheduler jobs create http daily-kickoff \
  --location=us-central1 \
  --schedule="0 6 * * *" \
  --uri="${NP_URL}/daily-sync" \
  --oidc-service-account-email="nexus-prime-sa@morphic-gaos-prod.iam.gserviceaccount.com" \
  --project=morphic-gaos-prod
```

### 10.4 Doc Comment Poll Job (every 5 minutes) — *Phase 2.5*

Polls open Blueprint Google Docs for new owner comments so Nexus-Prime can process `COMMENT_RECEIVED` constraint updates without waiting for a manual trigger.

```bash
NP_URL="https://nexus-prime-975461050387.us-central1.run.app"
gcloud scheduler jobs create http doc-comment-poll \
  --location=us-central1 \
  --schedule="*/5 * * * *" \
  --uri="${NP_URL}/poll-comments" \
  --oidc-service-account-email="nexus-prime-sa@morphic-gaos-prod.iam.gserviceaccount.com" \
  --project=morphic-gaos-prod
```

**Verification:** Both Phase 2.5 jobs appear with state `ENABLED` in the Scheduler console. Force-run `daily-kickoff` — a morning briefing card appears in the owner's Chat space (`POST /daily-sync` returns HTTP 200). Force-run `doc-comment-poll` — `POST /poll-comments` returns HTTP 200.

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

> **Region note:** `us-central1`, `us-east1`, and `us-east4` are capacity-restricted for new
> projects. Use `us-west1` (or another supported region from the
> [RAG Engine docs](https://cloud.google.com/vertex-ai/generative-ai/docs/rag-engine/rag-overview#supported-regions)).
> Set `memory_bank.region` in `settings.yaml` to match.

> **Package note:** Use `vertexai.rag` (from the `google-cloud-aiplatform` package) — **not**
> `google.cloud.aiplatform.rag`. The `aiplatform.rag` attribute doesn't exist in the installed
> version; `vertexai.rag` is the correct import path.

```powershell
# Automated — creates all 7 corpora and writes corpus names to settings.yaml
python scripts/_create_corpora.py
```

The script (`scripts/_create_corpora.py`) uses `vertexai.rag.create_corpus()` and writes
all corpus resource names to `memory_bank.corpora` in `settings.yaml` automatically.

**Verification:** `settings.yaml` `memory_bank.corpora` section has non-empty values for all
7 domains.

---

## 13. Smoke Tests

Phase 1 is complete when all of the following pass. Run them in order.

| # | Test | How to run | Expected result |
|---|------|-----------|-----------------|
| 0 | Unit test suite | `pytest` | 320 tests pass, 0 failures |
| 1 | Sheet write | `python -c "from tools.google_sheets import init_sheets_client, append_row; import datetime; init_sheets_client('default'); append_row('Logs', {'timestamp': datetime.datetime.utcnow().isoformat(), 'level': 'SMOKE_TEST', 'source': 'smoke', 'message': 'phase 1 test'}, 'default')"` | Row appears in `Logs` tab |
| 2 | Sheet read | `python -c "from tools.google_sheets import init_sheets_client, get_all_records; init_sheets_client('default'); rows = get_all_records('Project Registry', 'default'); print(f'{len(rows)} rows')"` | Prints `0 rows` (or more once project rows are added) |
| 3 | Pub/Sub publish | `python -c "from tools.pubsub import publish; from models import A2AMessage; ..."` sending a test message to `agent.nexus-prime.events` | Message ID returned; no exception |
| 4 | Pub/Sub receive | Manually trigger the Apps Script `onChange` by editing a Status cell | Local Python subscriber (or Cloud Run log) shows the event within 5 seconds |
| 5 | Secret access | `python -c "from tools.secrets import get_secret; v = get_secret('GEMINI_API_KEY', 'morphic-gaos-prod'); print(v[:8] + '...')"` | First 8 chars of API key printed |
| 6 | Webhook HMAC | POST a correctly signed payload to the Apps Script Web App URL | `statusCode: 200`; row appears in `Agent_Approvals` |
| 7 | Webhook rejection | POST with a tampered signature | `statusCode: 401`; `HMAC_FAILURE` appears in `Logs` tab |

Run all 8 webhook-specific tests from `GAOS-Manager-Spec.md §14` after test 7 passes.

---

## 14. Phase 1 Exit Criteria Checklist

Phase 1 is complete — and Phase 2 (Ollama integration) may begin — when **every item** below is checked:

- [ ] `tools/google_sheets.py` appends a row and reads a cell value without errors
- [ ] All 320 unit tests pass (`pytest` — green, 0 failures)
- [ ] Apps Script `onChange` trigger fires on Status cell change and publishes to `agent.approvals.events`
- [ ] Local Python subscriber receives the Pub/Sub push and prints the proposal ID and new status
- [ ] Cloud Scheduler TTL sweep job exists and can be triggered manually (HTTP 200 response)
- [x] Cloud Scheduler nightly archive job exists with state `ENABLED` — `POST /archive` implemented in Phase 2 Item 3 (returns HTTP 200)
- [x] **[Phase 2.5]** Google Chat App created in Google Cloud console; Chat API enabled; `nexus-prime-sa` added to the Chat space as the bot identity (authenticated via service account ADC — no Secret Manager token required)
- [x] **[Phase 2.5]** Vertex AI Search datastore created and indexed against Drive `Knowledge/` folder; datastore ID stored in `settings.yaml`
- [x] **[Phase 2.5]** Google Custom Search Engine created; CSE ID and API key stored in Secret Manager as `GOOGLE_SEARCH_CX` and `GOOGLE_SEARCH_API_KEY`
- [ ] **[Phase 2.5]** AppSheet app deployed and connected to the Google Sheets workbook (`Agent_Approvals` + `Project Registry` tabs at minimum)
- [x] **[Phase 2.5]** Cloud Scheduler `daily-kickoff` job created (6 AM daily, `POST /sync`, returns HTTP 200)
- [x] **[Phase 2.5]** Cloud Scheduler `doc-comment-poll` job created (every 5 minutes, `POST /poll-comments`, returns HTTP 200)
- [x] **[Phase 2.5]** `POST /chat` endpoint returns HTTP 200; Nexus-Prime responds in Chat thread within 10 seconds
- [ ] **[Phase 2.5]** Approval Gate Chat-path validated end-to-end: Chat card Approve tap → `APPROVAL_RESULT` published → Nexus-Prime resumes parked task → Sheet audit row written
- [ ] All 7 smoke tests above are passing
- [ ] All 8 webhook tests from `GAOS-Manager-Spec.md §14` are passing
- [ ] `setupProtections()` has been run; Status/Code/Hash columns are locked to owner
- [ ] Authorized Approvers tab has at least one row (owner, tier 5, active=TRUE)
- [ ] `settings.yaml` is complete with correct `workbook_id`, `knowledge_folder_id`, and model aliases
- [ ] `WEBHOOK_URL` secret is populated in Secret Manager
- [ ] BigQuery dataset and 6 tables created with TTL partitioning
- [ ] All Knowledge/ seed files created in Drive
- [ ] All 7 Cloud Run services deployed and returning `{"status":"ok"}` on `/health`
- [ ] All 22 Pub/Sub push subscriptions created with OIDC auth (`pubsub-push-sa`)
- [ ] Vertex AI RAG corpora created — all 7 `memory_bank.corpora` entries in `settings.yaml` are non-empty
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
