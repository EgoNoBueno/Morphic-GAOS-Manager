# GAOS Deployment Specification

**Morphic-G AOS** — Infrastructure Provisioning & First-Run Guide

> **What is GAOS?**
> GAOS is a system of AI agents that help run a business automatically. Think of it like hiring 7 specialized AI employees — one handles accounting, one handles marketing, one handles sales, etc. They live in the cloud, talk to each other through a messaging system, and report through a shared Google Spreadsheet you use as a control panel.
>
> **What does this document do?**
> This is the complete step-by-step guide to building all the cloud infrastructure those agents need. You'll create accounts and permissions, databases, a messaging system, scheduled jobs, and finally deploy the code that makes everything run. Follow each section in order and complete the verification check before moving on.
>
> **Estimated time:** 2–3 hours for a first-time setup, 30 minutes for a repeat deployment.
> **Estimated monthly cost after setup:** Low — architecture uses free-tier services and scale-to-zero compute (see `GAOS-Manager-Spec.md` §9.4 for breakdown).

### Follow-This-Order Guide

> ⚠️ **Important:** Sections §19 and §20 appear in the middle of this document due to how the guide evolved, but they belong to **Phase 4** (the final phase). On a first deployment, skip them when you reach them and come back after §18.

Execute sections in this order:

| Step | Section | What you're building |
|------|---------|---------------------|
| 1 | §0 Prerequisites | Install tools, clone the repo, set up your Google credentials |
| 2 | §1 GCP Project | Create your Google Cloud project and turn on all required services |
| 3 | §2 IAM | Create the robot identities (service accounts) that run each agent |
| 4 | §3 Secret Manager | Store API keys securely — never in plain text in code |
| 5 | §4 Google Sheets | Provision the spreadsheet that serves as the system's control panel |
| 6 | §5 Pub/Sub | Set up the message queue that agents use to talk to each other |
| 7 | §6 Drive | Set up the knowledge folder where agents store and read documents |
| 8 | §7 BigQuery | Create the databases for logs, memory, and analytics |
| 9 | §8 `settings.yaml` | Fill in the central config file with IDs from previous steps |
| 10 | §9 Cloud Run | Deploy all 7 agents as live web services in the cloud |
| 11 | §10 Cloud Scheduler | Set up automated recurring tasks (nightly archive, daily briefing, etc.) |
| 12 | §11 Cloud Logging | Configure how long logs are kept |
| 13 | §12 Vertex AI | Create the AI memory banks for each agent domain |
| 14 | §13–18 | Smoke tests and phase completion checklists |
| — | §19–20 | *(Phase 4 only — skip on first pass)* Exit criteria and bootstrap runbook |
| — | §21–22 | Post-launch optimization queue (start only after real production traffic) |

---

## 0. Prerequisites

Before starting, confirm the following are in place on your local machine.

### 0.1 Required Accounts

| Account | Purpose | Notes |
|---------|---------|-------|
| Google account (personal or Workspace) | Owns all GCP resources and the Google Sheet | Must be an account you control and trust |
| GitHub account | Source control | Repo already created: `EgoNoBueno/Morphic-GAOS-Manager` |

> **PowerShell version:** All PowerShell snippets in this guide require **PowerShell 7+** (`pwsh`). PS 5.1 (`powershell.exe`) is not supported — it lacks the `?.` null-conditional operator and the `&&` pipeline chain operator used in several steps. Verify with `$PSVersionTable.PSVersion`; if the major version is less than 7, install from <https://aka.ms/powershell>.

### 0.2 Required Local Tools

> **In plain English:** These are the programs your computer needs before you start. Think of them as your toolkit — each one does a different job during setup.
> - **gcloud** — Google's command-line tool for managing cloud resources
> - **gh** — GitHub's command-line tool (you'll use it to set up CI/CD secrets)
> - **python** — the programming language the agents are written in
> - **uv** — a fast Python package manager (installs dependencies much faster than pip)
> - **git** — version control (you already have this)
> - **ollama** — runs AI models locally on your machine

Install these before proceeding:

```powershell
# Verify each tool after installing
$PSVersionTable.PSVersion  # Must be 7.x — install from https://aka.ms/powershell if not
gcloud --version        # Google Cloud CLI — https://cloud.google.com/sdk/docs/install
gh --version            # GitHub CLI (already confirmed installed)
python --version        # Python 3.11+
uv --version            # uv package manager — https://docs.astral.sh/uv/getting-started/installation/
git --version           # Already confirmed installed
ollama --version        # Ollama — https://ollama.com/download (Windows)
```

### 0.3 Clone and Bootstrap Repo

> **In plain English:** This downloads the project code to your computer and sets up an isolated Python environment (a private copy of Python just for this project, so it doesn't conflict with anything else you have installed).

```powershell
# If not already done
git clone https://github.com/EgoNoBueno/Morphic-GAOS-Manager.git
cd Morphic-GAOS-Manager

# IMPORTANT: Create your config file from the template
# Do this before running any other scripts — many scripts read this file
Copy-Item config\settings.yaml.template config\settings.yaml

# Create Python environment and install all dependencies (including dev tools)
uv venv
uv pip install -e ".[dev]"
```

> **Note:** All package versions are pinned in `pyproject.toml`. `[dev]` adds `pytest`, `pytest-cov`, `ruff`, and `mypy`. Omit `[dev]` in a production-only environment: `uv pip install -e .`. Use `google-genai>=1.0.0` (`google.genai.Client()` API) — **not** `google-generativeai`. The `google-generativeai` package is EOL: it imports with a `FutureWarning` and the `v1beta` endpoint it targets no longer serves models like `gemini-1.5-pro`, returning 404. The `google-genai` package is the official successor and is what `google-adk` expects.

### 0.4 Application Default Credentials (ADC) Setup

> **In plain English:** Google needs to know who *you* are when your local scripts talk to Google Cloud. Instead of storing a password in a file (which is a security risk), we use "Application Default Credentials" — you log in once via a browser, and Google saves a secure token on your computer. After that, all your local scripts automatically authenticate as you. On Cloud Run (when things are deployed), the agents prove their identity using their service accounts instead.

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
SCOPES="${SCOPES},${_G}/auth/script.scriptapp,${_G}/auth/chat.spaces.readonly"
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

### 0.5 GAOS-Doctor Health Runbook

GAOS-Doctor is a manual health-check runbook — not a CLI tool. Refer to `Docs/GAOS-Doctor.md` for the step-by-step checklist of system health verifications (Sheet connectivity, Pub/Sub topics, Secret Manager access, Cloud Run `/health` endpoints, Vertex AI corpora, etc.).

Run through the GAOS-Doctor checklist whenever you suspect a configuration drift or after any infrastructure change.

> ⚠️ **Warning — `settings.yaml` must have a named entry for every `project_id` used:** `init_sheets_client(project_id)` resolves the workbook by doing an exact key lookup of `projects.<project_id>.sheet_id` in `settings.yaml`. **It does not consult `projects.default` as a fallback** — there is no fallback mechanism. If `projects.<project_id>` is absent, `init_sheets_client` raises `WorkbookNotFoundError` immediately, even if `projects.default` exists with a valid `sheet_id`. Every distinct `project_id` passed to `init_sheets_client` must have its own entry. For the system project, add a `projects.morphic-gaos-prod` block with the same `sheet_id` and `drive_folder_id` values as `projects.default` — this is an explicit copy, not an automatic alias.

### 0.6 Google Sheets Control Plane

> **In plain English:** The entire GAOS system is controlled through one Google Spreadsheet. Each of its 14 tabs is like a department's desk — one for accounting records, one for the approval queue, one for logs, etc. You don't build this by hand; a script creates it for you in §4.

The GAOS control plane is a **single** Google Sheets workbook with a 14-tab schema — not a separate "Dashboard" spreadsheet. The workbook is provisioned by `scripts/setup_workspace.py` in §4.1 and its ID is stored in `settings.yaml` under `sheet.workbook_id`.

The 14 tabs are: `Project Registry`, `Accounting`, `Inventory`, `Contacts`, `Leads`, `Scheduling`, `Agent_Approvals`, `Authorized_Approvers`, `Logs`, `Error_Logs`, `Observability`, `Research`, `Tasks`, `Proposals`. No additional spreadsheet needs to be created.

> ⚠️ **Warning — Project Registry `status` must be lowercase `'active'`:** The Apps Script `isValidProject_()` helper in `helpers.gs` checks `data[i][2] === 'active'` with a strict equality comparison. Capitalised values like `'Active'` or `'ACTIVE'` silently fail this check and cause the webhook to return `400 Unknown project_id` even when the project row exists in the tab. Always write `status = 'active'` (lowercase) when adding or updating Project Registry rows programmatically.

---

## 1. GCP Project Setup

> **In plain English:** Google Cloud Platform (GCP) is like renting a private slice of Google's worldwide datacenter. A "project" is your isolated workspace inside it — all your databases, servers, and services live together under one project so you can manage costs and permissions in one place. You'll create one project called `morphic-gaos-prod` and attach a billing account (credit card) to pay for server time. Most of this system stays within Google's free tier.

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

> **In plain English:** When humans use Google Cloud, they log in with an email and password. But AI agents running in the cloud need their own identity to access services. A "service account" is like a robot employee ID — it has a name, a set of permissions (what it's allowed to do), and Google enforces those limits automatically. We give each agent its own service account so they can only access *exactly* what they need and nothing more. If one agent ever has a bug or is compromised, the damage is contained.

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

# CI/CD deployer service account (used by GitHub Actions — no human login)
gcloud iam service-accounts create deployer-sa \
  --display-name="OpenTofu CI/CD Deployer"
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

# ── CI/CD Deployer SA — used by GitHub Actions OpenTofu pipeline (see §9.3) ──
# ⚠️  Prerequisite: the Artifact Registry repo "cloud-run-source-deploy" and the
#    GCS bucket "gs://morphic-gaos-tfstate" must already exist before running the
#    two resource-scoped bindings below. Both are created in §9.3 steps 1 and 2.
#    Run §9.3 steps 1–2 first, then return here to complete these bindings.
# Cloud Run admin: create/update services
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:deployer-sa@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/run.admin"
# Artifact Registry: push container images (requires repo created in §9.3 step 2)
gcloud artifacts repositories add-iam-policy-binding cloud-run-source-deploy \
  --location=us-central1 --project=$PROJECT \
  --member="serviceAccount:deployer-sa@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/artifactregistry.writer"
# GCS: read/write OpenTofu state in the tfstate bucket (requires bucket created in §9.3 step 1)
gcloud storage buckets add-iam-policy-binding gs://morphic-gaos-tfstate \
  --member="serviceAccount:deployer-sa@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"
# Project-level IAM admin: required when TF manages project-level IAM bindings
# (e.g. roles/bigquery.dataEditor on agent SAs). Without this, apply returns 403
# on every google_project_iam_member resource.
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:deployer-sa@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/resourcemanager.projectIamAdmin" \
  --condition=None
# Service account admin: required to create new SAs via TF (e.g. grafana-sa).
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:deployer-sa@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountAdmin" \
  --condition=None
# Secret Manager admin: required to bind Secret Manager IAM for new SAs via TF.
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:deployer-sa@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/secretmanager.admin" \
  --condition=None
# actAs: deployer-sa must be allowed to assign each agent SA to its Cloud Run
# service. Bound per-SA (not project-level) per the principle of least privilege.
for agent in nexus-prime ledger beacon pursuit foreman steward scout grafana; do
  gcloud iam service-accounts add-iam-policy-binding \
    ${agent}-sa@${PROJECT}.iam.gserviceaccount.com \
    --member="serviceAccount:deployer-sa@${PROJECT}.iam.gserviceaccount.com" \
    --role="roles/iam.serviceAccountUser" \
    --project=$PROJECT
done
```

> ⚠️ **Warning — `roles/iam.serviceAccountUser` must be scoped per-SA, not project-wide.**
> A project-level binding lets the deployer impersonate any SA in the project, including
> those with elevated permissions. Always bind on the individual agent SA resource.

> ⚠️ **Warning — Partial apply leaves orphaned GCP resources that cause 409 on retry.** If an apply fails mid-run (e.g. due to missing permissions), GCP creates successful resources but TF state doesn't record them. The next apply tries to create them again and gets `409 alreadyExists`. Fix: add an `import {}` block in `main.tf` to adopt the existing resource. The block is idempotent — safe to leave permanently. Example: `infra/main.tf` contains an import block for `google_service_account.grafana` added after the 2026-04-02 partial-apply incident.

### 2.3 Service Account Identity

No JSON key files are needed. Service account identity is supplied at runtime:

- **Local development:** `gcloud auth application-default login` (see §0.4) provides ADC credentials. The tool layer calls `google.auth.default()` which picks these up automatically.
- **Cloud Run:** Each Cloud Run service is deployed with `--service-account=<agent>-sa@${PROJECT}.iam.gserviceaccount.com`. The GCE metadata server provides credentials — no key file required.

**Verification:** `gcloud iam service-accounts list` — 8 service accounts listed.

---

## 3. Secret Manager

> **In plain English:** API keys, passwords, and other sensitive values should *never* be stored in code or config files — if you accidentally push code to GitHub, they'd be publicly visible. Google Secret Manager is a secure vault: you store sensitive values there once, and agents fetch them at runtime using their service account identity. Even if someone reads your code, they can't see the actual key values.

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
# Note: the pipe below uses bash syntax. Run this in bash (or Cloud Shell), not PowerShell.
# PowerShell alternative: use scripts/setup_secrets.py which handles this interactively.
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

### 3.3 Local Ollama Tunneling (localtunnel)

The repository provides a small automation to expose a local Ollama server to the internet and keep the `OLLAMA_HOST` Secret Manager secret up-to-date. This is provided as an operator convenience for local development when Cloud Run instances need to reach a developer's local model.

- Scripts:
  - `scripts/start_ollama_tunnel.py` — a Python watchdog that spawns `npx localtunnel`, validates the tunnel by probing `/api/tags`, and updates `OLLAMA_HOST` in Secret Manager. It can run once (`--once`) or as a persistent watchdog (default).
  - `scripts/register_ollama_tunnel_task.ps1` — Windows helper to register the watchdog as a Scheduled Task (auto-start at login).

- Requirements:
  - Node.js (provides `npx`) available on PATH.
  - A Python virtual environment with dependencies (see `pyproject.toml`) and access to Google Cloud credentials that can write to Secret Manager.
  - Ollama running locally on the port used (default: `11434`).

- Quick test (interactive, creates a log):
```powershell
& .\.venv\Scripts\python.exe .\scripts\start_ollama_tunnel.py --port 11434 --project morphic-gaos-prod --once --log-file .\logs\ollama-tunnel.log
Get-Content .\logs\ollama-tunnel.log -Tail 200
```

- Run persistent watchdog (foreground):
```powershell
& .\.venv\Scripts\python.exe .\scripts\start_ollama_tunnel.py --port 11434 --project morphic-gaos-prod --log-file .\logs\ollama-tunnel.log
```

- Register for login persistence (Windows, requires elevation):
```powershell
Start-Process pwsh -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','scripts\register_ollama_tunnel_task.ps1' -Verb RunAs
```

- Notes and caveats:
  - The agents include a small bypass header for loca.lt hosts: requests to hosts containing `.loca.lt` include `Bypass-Tunnel-Reminder: true` so the public localtunnel service responds with the API JSON and not an HTML challenge page.
  - Scheduled Tasks run without a full interactive profile; the watchdog now injects common Node.js locations into `PATH` automatically but ensure Node.js is installed either system-wide (`C:\Program Files\nodejs`) or the user's npm bin (`%APPDATA%\npm`).
  - The registration helper registers the task to run `python.exe` (not `pythonw.exe`) so logs are written to disk; if you prefer a hidden window, adjust `register_ollama_tunnel_task.ps1` accordingly.
  - Security: localtunnel exposes a publicly reachable URL. Only use for development and understand the exposure surface — do not use for sensitive production data.

Verify the secret was updated:
```powershell
gcloud secrets versions access latest --secret=OLLAMA_HOST --project=morphic-gaos-prod
```

> ⚠️ **Warning — per-secret IAM bindings may silently fail to propagate:** After running §3.2, verify each critical secret's bindings were actually written before deploying:
> ```powershell
> gcloud secrets get-iam-policy GEMINI_API_KEY --project=morphic-gaos-prod
> ```
> If the output shows `ROLE  MEMBERS` with no rows, the binding was never applied (possible IAM propagation race or a silent CLI failure). Symptom in Cloud Run logs: `Permission 'secretmanager.versions.access' denied for resource '…/GEMINI_API_KEY/versions/latest'` and a 500 response.
>
> **Fix:** Grant `roles/secretmanager.secretAccessor` at the project level as a fallback:
> ```powershell
> $PROJECT = "morphic-gaos-prod"
> foreach ($agent in @("ledger","beacon","pursuit","foreman","steward","scout")) {
>     gcloud projects add-iam-policy-binding $PROJECT `
>       --member="serviceAccount:${agent}-sa@${PROJECT}.iam.gserviceaccount.com" `
>       --role="roles/secretmanager.secretAccessor" --condition=None
> }
> ```
> (nexus-prime-sa already has access via `roles/editor`.) This is less granular than per-secret bindings but guarantees all agents can access all secrets.

---

## 4. Google Sheets Workbook

> **In plain English:** The GAOS control panel is a single Google Spreadsheet with 14 tabs — one for each area of the business (accounting, sales, logs, the approval queue, etc.). You view and interact with the whole system through this one spreadsheet. You don't build it by hand; a setup script creates all 14 tabs with the right column headers automatically.

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

> **Prerequisite — Update `SPREADSHEET_ID_` in `helpers.gs` first:**
> The file `apps_script/helpers.gs` contains a hardcoded constant `SPREADSHEET_ID_` that must match *your* spreadsheet ID before the script is uploaded. Open `apps_script/helpers.gs` in VS Code, find the line that sets this constant, and replace its value with the spreadsheet ID that `setup_workspace.py` printed in §4.1:
> ```javascript
> const SPREADSHEET_ID_ = "YOUR_SPREADSHEET_ID_HERE";
> ```
> If this value is wrong, every webhook call will silently fail with a 500 error because `doPost` opens the wrong (or nonexistent) spreadsheet.

Run the setup script — it creates the bound project, uploads all `.gs` files,
deploys the Web App, and stores `WEBHOOK_URL` in Secret Manager:

```powershell
python scripts/setup_apps_script.py
```

**Phase 1** (create + upload + deploy) runs fully. At the end it opens the
Apps Script editor in your browser for a **one-time OAuth consent click**.
After clicking Allow, the web app is live and `WEBHOOK_URL` is in Secret Manager.

> **Phase 2 (Script Properties, trigger, protections) must be completed manually.**
> The `scripts.run()` API returns HTTP 403 for this project and cannot be automated.
> `--post-auth` will print the correct values in its fallback output (useful for
> copy-paste) but will not succeed at setting them. Complete the following steps
> in the Apps Script editor instead:
>
> ⚠️ **Warning — bound-script Execution API 403:** The Apps Script project is created
> with `parentId: spreadsheet_id` (bound to the Sheet). Bound scripts inherit the
> spreadsheet's auto-assigned GCP project, which differs from `morphic-gaos-prod`.
> The `scripts.run()` Execution API cannot cross that project boundary with ADC
> credentials — it returns 403 permanently regardless of scopes, editor consent,
> or re-auth. Do not spend time debugging this; the three Phase 2 tasks must
> always be completed manually in the Apps Script editor.

**Step 1 — Set Script Properties**

> ⚠️ **Get the `VERTEX_AGENT_ENDPOINT` URL from §9.2**, not from this table. It is the nexus-prime Cloud Run URL with `/sync` appended. It is unique to your deployment.

(Apps Script editor → Project Settings → Script Properties):

| Key | Value |
|-----|-------|
| `WEBHOOK_HMAC_SECRET` | Value of `gcloud secrets versions access latest --secret=WEBHOOK_HMAC_SECRET --project=morphic-gaos-prod` |
| `WEBHOOK_URL` | Value of `gcloud secrets versions access latest --secret=WEBHOOK_URL --project=morphic-gaos-prod` |
| `VERTEX_AGENT_ENDPOINT` | Your nexus-prime Cloud Run URL + `/sync` — get it from §9.2 |
| `GCP_PROJECT` | `morphic-gaos-prod` |

> ⚠️ **Key names are case-sensitive and must use underscores, not hyphens.**
> `WEBHOOK_HMAC_SECRET` and `GCP_PROJECT` are correct. `WEBHOOK_HMAC-SECRET`
> or `GCP_PROPERTY` will silently return `null` and cause `doPost` to throw
> `Internal error (500)` on every request.

**Step 2 — Run `setupProtections`** (function dropdown → `setupProtections` → Run). Locks Status, Proposed Code, and Code SHA-256 columns + entire Authorized Approvers tab to owner-only edit.

**Step 3 — Install the `onEdit` trigger** (Triggers → Add Trigger → Function: `onChangeApproval` | Event type: **On edit**).

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

> ⚠️ **Warning — run `setupProtections()` exactly once.** The function creates new protection objects on every call — running it twice creates duplicate rules (15 duplicate protections were found on 2026-04-03 after multiple runs: 5 each for Status, Code, and Hash columns). If you accidentally ran it more than once, use the Sheets API or the Google Sheets UI (Data → Protect sheets and ranges) to delete the duplicates, keeping one of each.
>
> ⚠️ **Warning — nexus-prime SA must be an editor on the Status (col I) protection.** Without explicit SA access to col I, the `promote` node's `update_row("Agent_Approvals", ..., {"Status": "Deployed"})` call fails with `400: You are trying to edit a protected cell`. As of 2026-04-03, `setup_protection.gs` adds the SA (`nexus-prime-sa@morphic-gaos-prod.iam.gserviceaccount.com`) via `p1.addEditor(SA_EMAIL)` in `setupProtections()`. **If you ran an older version that did not include the SA**, run `patchStatusProtectionForSA()` (also in `setup_protection.gs`) from the Apps Script editor — it adds the SA to all existing Status protections without recreating them. Alternatively, run `python scripts/_patch_sheet_protection.py` which does the same via the Sheets API.

### 4.6 Install the `onEdit` Trigger

Install manually in the Apps Script editor (see §4.4 Step 3):
Triggers → Add Trigger → Function: `onChangeApproval` | Event type: **On edit**.

> ⚠️ **Warning — must use `onEdit`, not `onChange`:** The `onChangeApproval` handler uses `e.range`, which is only present on `onEdit` events. Installing as `onChange` causes a silent failure: `e.range` is `undefined`, `range.getColumn()` throws, and the trigger does nothing. The error only appears in the Apps Script execution log — the cell value stays changed and no Logs entry is written.
>
> **If you previously installed an `onChange` trigger**, delete it in the Triggers panel and reinstall as `onEdit`.

> ⚠️ **Warning — never use `e.source` in the trigger handler:** `e.source` is unreliable for installable `onEdit` triggers and may be `undefined`, causing `TypeError: Cannot read properties of undefined (reading 'source')` on line 1 of the handler. Always use `e.range.getSheet()` instead — it is always populated on `onEdit` events. Also guard the entire function with `if (!e || !e.range) return;` as the first line.
>
> Similarly, `Session.getActiveUser().getEmail()` may return an empty string for installable triggers in some Google Workspace configurations. Fall back to `Session.getEffectiveUser().getEmail()` — use: `Session.getActiveUser().getEmail() || Session.getEffectiveUser().getEmail()`.
>
> **Automation:** Re-upload all `.gs` files after any local edit using `python scripts/setup_apps_script.py --push` — no copy-paste into the Apps Script editor required.
>
> ⚠️ **Warning — `--push` requires `script.projects` in ADC scopes.** The Apps Script push internally calls `projects.updateContent` which requires the `https://www.googleapis.com/auth/script.projects` scope. If ADC was last refreshed without the full scope list (e.g., a basic `gcloud auth application-default login`), `--push` fails with a 403. Fix: re-run the ADC login from §6.2 using `--client-id-file=oauth-client.json --scopes="...script.projects..."` (full scope list is in §6.2). Discovered 2026-04-03.

### 4.7 Deploy the Webhook as a Web App

Handled automatically by Phase 1. The Web App URL is stored in Secret Manager
as `WEBHOOK_URL` and in `config/settings.yaml` under `apps_script.webhook_url`.

**Verification (Sheet):**
- Manually change a `Status` cell in `Agent_Approvals` to `Approved` — an entry should appear in the `Logs` tab (from `logApprovalEvent_`).
- Change a cell without being in the `Authorized Approvers` tab — the cell should revert to `Pending` and a `NOT_IN_APPROVERS_LIST` entry should appear.

---

## 5. Cloud Pub/Sub

> **In plain English:** Pub/Sub (short for Publish/Subscribe) is Google's messaging service — it's how agents talk to each other without needing to know each other's addresses. Think of it like a bulletin board system: an agent "publishes" a message to a named "topic", and any agent that has "subscribed" to that topic receives the message. Messages are delivered reliably even if the receiving agent is temporarily offline or busy.

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

**Step D — Create all 23 subscriptions:**

> ⚠️ **Replace the URLs below with your own.** The URLs in the `URLS` array are from the original deployment and will not work for a new project. Get your actual URLs after completing §9 by running:
> ```bash
> gcloud run services list --region=us-central1 --project=morphic-gaos-prod --format="table(metadata.name,status.url)"
> ```
> Also note: `declare -A` (bash associative arrays) requires **bash 4+**. On macOS the default shell is zsh — run these commands in `bash` explicitly, or use the Cloud Shell in the GCP console.

```bash
PROJECT=morphic-gaos-prod
BASE="projects/${PROJECT}/topics"
PUSH_SA="pubsub-push-sa@${PROJECT}.iam.gserviceaccount.com"

# Replace each URL with the output of: gcloud run services describe <agent> --region=us-central1 --format='value(status.url)'
declare -A URLS=(
  [nexus-prime]="https://YOUR-NEXUS-PRIME-URL.run.app"
  [ledger]="https://YOUR-LEDGER-URL.run.app"
  [beacon]="https://YOUR-BEACON-URL.run.app"
  [pursuit]="https://YOUR-PURSUIT-URL.run.app"
  [foreman]="https://YOUR-FOREMAN-URL.run.app"
  [steward]="https://YOUR-STEWARD-URL.run.app"
  [scout]="https://YOUR-SCOUT-URL.run.app"
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
  "scout.sub.foreman:scout:agent.foreman.events" \
  "scout.sub.nexus-prime:scout:agent.nexus-prime.events"; do
    IFS=: read name subscriber topic <<< "$sub_agent_topic"
    gcloud pubsub subscriptions create $name \
      --topic="${BASE}/${topic}" \
      --push-endpoint="${URLS[$subscriber]}/pubsub" \
      --push-auth-service-account=$PUSH_SA \
      --ack-deadline=60 --project=$PROJECT
done
```

**Verification:** `gcloud pubsub subscriptions list --project=$PROJECT --format="table(name,pushConfig.pushEndpoint)"` — 23 subscriptions listed, all pointing to `*.run.app/pubsub`.

---

## 6. Google Drive — Knowledge/ Folder

> **In plain English:** Agents need a place to store and read documents — things like policies, procedures, and workflows that describe how the business operates. The Knowledge/ folder in Google Drive serves this purpose. The workspace setup script already created this folder in §4.1, so this section mostly verifies it and seeds it with placeholder documents.

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

**Automated:** Use `scripts/_seed_knowledge_files.py` — idempotent, creates all 13 files, skips any that already exist.

```powershell
# Prerequisites: ADC refreshed with Drive scope (see warning below)
python scripts/_seed_knowledge_files.py
```

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

> ⚠️ **Warning — ADC Drive scope:** Standard `gcloud auth application-default login` only grants `cloud-platform` scope. The Drive API requires explicit OAuth scopes. Refresh ADC with:
> ```powershell
> # Run in a standalone PowerShell window (not VS Code terminal — browser redirect fails there)
> $s = "https://www.googleapis.com/auth/"
> gcloud auth application-default login `
>   --client-id-file=oauth-client.json `
>   --scopes="${s}spreadsheets,${s}drive,${s}script.projects,${s}script.deployments,${s}script.scriptapp,${s}chat.spaces.readonly,${s}cloud-platform"
> ```
>
> ⚠️ **Warning — script.scriptapp and chat.spaces.readonly required:** The `setup_apps_script.py --post-auth` command calls `scripts.run()` (requires `script.scriptapp`) and discovers the owner DM space (requires `chat.spaces.readonly`). Both must be in the `--scopes` list above. If you used the old scope set (without these), re-auth with the command above before running `--post-auth`.

> ⚠️ **Warning — Drive API must be enabled in `morphic-gaos-prod`:** `oauth-client.json` belongs to `morphic-gaos-prod`. If you get a 403 `accessNotConfigured` error from the Drive API, run:
> ```powershell
> gcloud services enable drive.googleapis.com --project=morphic-gaos-prod
> ```

### 6.3 Service Account Access

Already handled by `scripts/setup_workspace.py` — all 7 SAs were granted writer
access to the root `Morphic-G AOS/` folder, which the Knowledge/ subfolder inherits.

To verify: open the `Morphic-G AOS` folder in Drive → right-click → Share → confirm
all SA emails appear with Editor access.

---

## 7. BigQuery

> **In plain English:** BigQuery is Google's large-scale database service. GAOS uses it to store logs, memory entries, and historical analytics — data that accumulates over months. Unlike the Google Spreadsheet (which is the live, human-readable control panel), BigQuery is built for permanently storing millions of rows and answering questions like "what did this agent cost over the last 30 days?" Think of the Spreadsheet as the dashboard and BigQuery as the archive.

> **Windows note:** The `bq` CLI fails on Windows (Python `absl.flags` conflict in the bundled
> Cloud SDK). Use the Python approach below for all BigQuery provisioning.

### 7.1 Create Dataset and Tables

> **Automated option (recommended):** Instead of running the individual Python blocks below, use the all-in-one script:
> ```powershell
> python scripts/create_staging_tables.py
> ```
> This creates the dataset and all 7 tables in one shot. The manual Python blocks below are kept for reference and for creating or recreating individual tables.

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

Then create all 7 tables (the `gaos_agents` table is auto-created by the BQ log sink — see §Log Sink below):

| Table | Partition | TTL | Purpose |
|-------|-----------|-----|---------|
| `task_outcomes` | `log_date` | 30 days | Episodic memory source — every task result |
| `evolution_tasks` | `log_date` | 365 days | Self-evolution loop outcomes |
| `approval_history` | `log_date` | 730 days | Full approval gate history |
| `observability_weekly` | — | indefinite | Weekly summary archive |
| `memory_entries` | — | indefinite | Structured metadata for Vertex AI RAG entries |
| `monologue_frames` | `timestamp` | 90 days | Think node reasoning traces (`GAOS-Persona-Spec.md` §4) |
| `agent_checkpoints` | `timestamp` | 30 days | Phoenix recovery — SHA-256-pinned agent state snapshots (`tools/phoenix.py`) |
| `gaos_agents` | `timestamp` (date-partitioned, auto) | 7 days (Log Explorer retention) | **Auto-created by Cloud Logging BQ sink** — structured `_log_cloud()` entries exported from Cloud Logging. Primary source for Grafana Live Log/Error feeds and `handle_archive()` distillation. Do not create manually. |

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
# Written by Nexus-Prime's think node before each output-producing decision.
# IAM: nexus-prime-sa already holds roles/bigquery.dataEditor on the dataset
#       — no additional table-level grants required.
from google.cloud import bigquery
client = bigquery.Client(project='morphic-gaos-prod')
table = bigquery.Table(
    'morphic-gaos-prod.aos_logs.monologue_frames',
    schema=[
        bigquery.SchemaField('task_id', 'STRING'),
        bigquery.SchemaField('project_id', 'STRING'),
        bigquery.SchemaField('knowledge_gap_detected', 'BOOL'),
        bigquery.SchemaField('knowledge_gap_description', 'STRING'),
        bigquery.SchemaField('partial_result_available', 'BOOL'),
        bigquery.SchemaField('response_mode', 'STRING'),   # Direct|Reframe|Research|Tactical
        bigquery.SchemaField('reasoning_summary', 'STRING'),
        bigquery.SchemaField('timestamp', 'TIMESTAMP'),    # UTC event time
    ],
)
table.time_partitioning = bigquery.TimePartitioning(
    field='timestamp',                # partition on logical event time, not ingestion time
    expiration_ms=90 * 86_400_000,
)
client.create_table(table, exists_ok=True)
print('monologue_frames: created')
```

```python
# Schema: agent_checkpoints
# Written by tools/phoenix.py save_checkpoint() after every major milestone.
# Read by phoenix_recover() to restore state after corruption.
# Rows are SHA-256-pinned — tampered rows are skipped at load time.
from google.cloud import bigquery
client = bigquery.Client(project='morphic-gaos-prod')
table = bigquery.Table(
    'morphic-gaos-prod.aos_logs.agent_checkpoints',
    schema=[
        bigquery.SchemaField('agent_id', 'STRING'),
        bigquery.SchemaField('project_id', 'STRING'),
        bigquery.SchemaField('timestamp', 'TIMESTAMP'),
        bigquery.SchemaField('state_json', 'STRING'),
        bigquery.SchemaField('checkpoint_hash', 'STRING'),
        bigquery.SchemaField('is_valid', 'BOOL'),
    ],
)
table.time_partitioning = bigquery.TimePartitioning(
    field='timestamp',
    expiration_ms=30 * 86_400_000,   # 30-day rolling window
)
client.create_table(table, exists_ok=True)
print('agent_checkpoints: created')
```

> ⚠️ **Note — `state_json` is never re-executed:** The checkpoint blob is a serialized `dict`, not executable code. `phoenix_recover()` deserializes and hash-verifies it before use. The `is_valid` flag is always written as `True`; rows with a hash mismatch are silently skipped by `load_checkpoint()` rather than deleted, preserving the full audit trail.

> ⚠️ **Warning — `exists_ok=True` will NOT alter an existing table:** If `monologue_frames` already exists with `timestamp STRING` and ingestion-time partitioning, `create_table(..., exists_ok=True)` silently succeeds without applying any schema changes. The new schema only takes effect on a fresh table.

> ⚠️ **Warning — breaking change for existing deployments:** Switching from `STRING` to `TIMESTAMP` and from ingestion-time to field-based partitioning is a non-backward-compatible change. Existing rows written with a plain ISO 8601 string into a `STRING` column will not be compatible with the new `TIMESTAMP` schema without a migration.

**Migration path for existing deployments:**

> ⚠️ **Suspend writes before Steps 2–4.** Any rows inserted into `monologue_frames` while the migration is in progress will be lost — they land in the old table, which is replaced in Step 4. Before proceeding past Step 1, scale Nexus-Prime to zero:
> ```bash
> gcloud run services update nexus-prime --region us-central1 --project=morphic-gaos-prod --min-instances 0 --max-instances 0
> ```
> Restore after Step 4 completes:
> ```bash
> gcloud run services update nexus-prime --region us-central1 --project=morphic-gaos-prod --min-instances 1 --max-instances 3
> ```

> ⚠️ **90-day partition TTL on `monologue_frames_new`.** `bq cp` in Step 4 preserves table metadata including the partition expiration. Any rows whose `CAST(timestamp AS TIMESTAMP)` falls outside the 90-day retention window will be silently dropped by BigQuery on the next partition sweep. Two options:
> - **Filter the INSERT (recommended for large tables):** restrict Step 2 to recent data only — rows older than 90 days have already expired and do not need migration:
>   ```sql
>   WHERE CAST(timestamp AS TIMESTAMP) >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 DAY)
>   ```
> - **Temporarily remove the TTL:** omit `expiration_ms` when creating `monologue_frames_new` in Step 1, run Steps 2–4, then reapply the TTL after the migration:
>   ```bash
>   bq update --time_partitioning_expiration 7776000 morphic-gaos-prod:aos_logs.monologue_frames
>   ```
>   (`7776000` seconds = 90 days)

**Pre-flight: validate CAST and partition assignment with a sample row**

Run this before the full INSERT to confirm that `CAST(timestamp AS TIMESTAMP)` produces valid values and that BigQuery routes them to the expected partitions:

```sql
-- Insert 5 sample rows and verify partition assignment.
INSERT INTO `morphic-gaos-prod.aos_logs.monologue_frames_new`
SELECT
  task_id,
  project_id,
  knowledge_gap_detected,
  knowledge_gap_description,
  partial_result_available,
  response_mode,
  reasoning_summary,
  CAST(timestamp AS TIMESTAMP) AS timestamp
FROM `morphic-gaos-prod.aos_logs.monologue_frames`
LIMIT 5;

-- Verify the 5 rows landed in the correct partition (date should match the source timestamp).
SELECT timestamp, DATE(timestamp) AS partition_date
FROM `morphic-gaos-prod.aos_logs.monologue_frames_new`
ORDER BY timestamp DESC
LIMIT 5;
```

If any `partition_date` is `NULL` or unexpected, abort: the source `timestamp` strings are not ISO 8601 UTC and need remediation before the full copy. Truncate `monologue_frames_new` and fix the source data:

```sql
TRUNCATE TABLE `morphic-gaos-prod.aos_logs.monologue_frames_new`;
```

```sql
-- Step 1: Create the replacement table with the correct schema (run the script above with a temp name).
--         Replace 'monologue_frames_new' in the script, then run it.

-- Step 2: Copy existing data, casting the STRING timestamp to TIMESTAMP.
--         To skip rows older than 90 days (already expired under the new TTL), add the WHERE clause below.
INSERT INTO `morphic-gaos-prod.aos_logs.monologue_frames_new`
SELECT
  task_id,
  project_id,
  knowledge_gap_detected,
  knowledge_gap_description,
  partial_result_available,
  response_mode,
  reasoning_summary,
  CAST(timestamp AS TIMESTAMP) AS timestamp
FROM `morphic-gaos-prod.aos_logs.monologue_frames`
WHERE CAST(timestamp AS TIMESTAMP) >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 DAY);

-- Step 3: Verify row counts match (counts may differ if the 90-day filter was applied).
SELECT COUNT(*) FROM `morphic-gaos-prod.aos_logs.monologue_frames`;
SELECT COUNT(*) FROM `morphic-gaos-prod.aos_logs.monologue_frames_new`;

-- Step 4: BigQuery does not support ALTER TABLE ... RENAME TO.
--         Use the bq CLI to copy the new table over the old name, then remove the temp table.
--         Run these two commands in a terminal (not in the BigQuery console SQL editor):
```

```bash
# Copy monologue_frames_new → monologue_frames (overwrites the destination).
bq cp --force \
  morphic-gaos-prod:aos_logs.monologue_frames_new \
  morphic-gaos-prod:aos_logs.monologue_frames

# Remove the temporary table now that the copy is complete.
bq rm --force morphic-gaos-prod:aos_logs.monologue_frames_new
```

**Rollback path:**

| Failure point | Recovery action |
|---|---|
| Step 2 INSERT fails mid-run | `monologue_frames` is untouched. Truncate `monologue_frames_new`, diagnose the CAST failure, then restart from Step 2. |
| Step 4 `bq cp` fails | `monologue_frames` is untouched (copy target is a different name). `monologue_frames_new` still holds the migrated data. Retry `bq cp` after resolving the error. |
| Step 4 `bq rm` fails after successful `bq cp` | Migration is complete — `monologue_frames` now has the correct schema. Run `bq rm --force morphic-gaos-prod:aos_logs.monologue_frames_new` manually to clean up. |
| `monologue_frames` was already overwritten and data is corrupt | Restore from BigQuery table snapshots (if enabled) or re-backfill from Cloud Logging exports. The original data was written as structured log entries via `_log_cloud` — the BigQuery sink for `aos_logs` can be used as a secondary source. |

> **Note:** Steps 2–4 require `roles/bigquery.dataEditor` on the dataset and `roles/bigquery.dataViewer` on the source table. No IAM changes to existing service accounts are required for the migration itself.

**Writer compatibility — TIMESTAMP-compatible values:**

`insert_rows_json` accepts both Python `datetime` objects and ISO 8601 strings for `TIMESTAMP` columns. All writers sending to `monologue_frames` must use one of the following formats:

| Format | Example | Notes |
|--------|---------|-------|
| ISO 8601 UTC string | `"2026-03-20T10:30:00.123456+00:00"` or `"2026-03-20T10:30:00Z"` | `utcnow_iso()` emits the `+00:00` form; the `Z` suffix is an equivalent UTC representation and also accepted — no code change needed |
| Python `datetime` (timezone-aware) | `datetime.now(timezone.utc)` | Automatically serialized by the BQ client |

`MonologueFrame.timestamp` is typed as `str` and populated via `utcnow_iso()` (`datetime.now(timezone.utc).isoformat()`), which produces `"2026-03-20T10:30:00.123456+00:00"` — a valid ISO 8601 UTC timestamp that BigQuery accepts for `TIMESTAMP` columns through `insert_rows_json`. No changes to `models/__init__.py` or `agents/nexus_prime/orchestrator.py` are required.

**Verification:** In BigQuery console, expand `aos_logs` dataset — 7 tables listed.

---

## 8. `config/settings.yaml`

> **In plain English:** `settings.yaml` is the central config file — think of it as the master list of settings that every agent reads at startup. It tells agents which Google Cloud project to use, which AI models to call, which spreadsheet to write to, what Pub/Sub topics exist, and more. You fill it in once and it never changes unless your infrastructure changes. **It does not store secrets** — those live in Secret Manager.

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
  # REQUIRED: named entry for the system project ID (must match gcp.project_id exactly).
  # Copy the same IDs from projects.default — this is an explicit copy, NOT an automatic alias.
  # Without this block, any call using project_id="morphic-gaos-prod" will throw WorkbookNotFoundError.
  morphic-gaos-prod:
    sheet_id: "<your-spreadsheet-id>"              # same value as projects.default.sheet_id
    drive_folder_id: "<your-knowledge-folder-id>"  # same value as projects.default.drive_folder_id

models:
  LOCAL_MODEL: "ollama/llama3"
  LOCAL_MODEL_FALLBACK: "gemini-2.5-flash"
  LOCAL_MODEL_TIMEOUT_SECONDS: 90
  FAST_MODEL: "gemini-2.5-flash"
  DEEP_MODEL: "gemini-2.5-pro"
  FAST_MODEL_INPUT_PRICE_PER_M: 0.15    # USD per 1M input tokens — update when switching model versions
  FAST_MODEL_OUTPUT_PRICE_PER_M: 0.60   # USD per 1M output tokens
  DEEP_MODEL_INPUT_PRICE_PER_M: 1.25    # USD per 1M input tokens
  DEEP_MODEL_OUTPUT_PRICE_PER_M: 10.00  # USD per 1M output tokens

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

# Google Docs — Blueprint Factory + Knowledge Atlas (Memory Mirror)
docs:
  service_account_key: ""      # Leave empty to use ADC (recommended)
  blueprints_folder_id: ""     # Drive folder ID for Blueprint Docs (Project_Incubator/)
  knowledge_atlas_doc_id: ""   # Google Doc ID for the Knowledge Atlas
                               # See §17 for one-time setup steps.
```

---

## 8.1 Cloud Logging → BigQuery Sink (`gaos-logs-bq-sink`)

The `gaos_agents` BQ table is auto-created by a Cloud Logging export sink — do not create it manually.

### What it does

Every `_log_cloud()` call writes a structured entry to Cloud Logging under log name `projects/morphic-gaos-prod/logs/gaos-agents`. The sink exports those entries to `aos_logs.gaos_agents` as a date-partitioned BigQuery table. Grafana's Live Log Feed and Live Error Feed panels read from this table directly, and `handle_archive()` queries it for distillation and weekly summaries.

### One-time provisioning

```powershell
# Create the sink (run once per project)
gcloud logging sinks create gaos-logs-bq-sink `
    "bigquery.googleapis.com/projects/morphic-gaos-prod/datasets/aos_logs" `
    --project=morphic-gaos-prod `
    --log-filter='resource.type="cloud_run_revision" AND logName="projects/morphic-gaos-prod/logs/gaos-agents"' `
    --use-partitioned-tables

# Grant the sink's service account write access to the dataset
# (get the SA email from the sink describe output)
$SINK_SA = $(gcloud logging sinks describe gaos-logs-bq-sink `
    --project=morphic-gaos-prod `
    --format="value(writerIdentity)")

gcloud projects add-iam-policy-binding morphic-gaos-prod `
    --member="$SINK_SA" `
    --role="roles/bigquery.dataEditor"
```

### Verify

```powershell
gcloud logging sinks describe gaos-logs-bq-sink --project=morphic-gaos-prod
# Confirm: destination matches aos_logs dataset, writerIdentity is populated
```

After the first `_log_cloud()` call, BigQuery auto-creates `aos_logs.gaos_agents` with a `timestamp` partition column. No manual schema creation required.

### IAM summary

| SA | Role | Why |
|----|------|-----|
| `service-975461050387@gcp-sa-logging.iam.gserviceaccount.com` | `roles/bigquery.dataEditor` | Sink writer — writes log entries to `gaos_agents` |
| `grafana-sa` | `roles/bigquery.dataViewer` (already granted) | Grafana reads `gaos_agents` for log panels |
| `nexus-prime-sa` | `roles/bigquery.jobUser` (already granted) | `handle_archive()` queries `gaos_agents` |

> ⚠️ **`staging_logs` and `staging_errors` superseded:** These two tables (populated by the gaos-sheets-sync job from the Sheets Logs/Error Logs tabs) always contained 0 rows because `_log_cloud()` never writes to Sheets — it writes to Cloud Logging only. The Grafana panels that previously used them have been migrated to `gaos_agents`. The `staging_logs` and `staging_errors` tables remain in the dataset but are no longer read by any panel.

---

## 9. Cloud Run Services

> **In plain English:** Cloud Run is Google's serverless hosting platform. You give it a container image (a packaged, self-contained snapshot of your code and all its dependencies), and Google runs it as a web service. The main advantage: you only pay while the service is handling requests, and Google scales it up or down automatically with demand. Each of the 7 GAOS agents gets its own Cloud Run service, and they all share the same codebase — an environment variable (`AGENT_NAME`) tells the code which agent to behave as.

### 9.1 Build and Deploy Each Agent

> **OpenTofu supersession:** The IaC pipeline in §9.3 supersedes the manual Cloud Run
> deployment loop in this section (Step 2 onward). The prerequisites block and Step 1
> (image build) remain here for reference and one-time bootstrap. All other provisioning
> steps (Secrets, BQ, Sheets) in §§3–8 remain authoritative and are not managed by OpenTofu.

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
    --timeout 300s \
    --concurrency 1 \
    --min-instances 0 \
    --max-instances 5 \
    --no-allow-unauthenticated \
    --set-env-vars AGENT_NAME=${agent}
done
```

> **Note:** Secrets (`GEMINI_API_KEY`, etc.) are **not** injected as environment variables. Each agent fetches secrets at boot via the Secret Manager API using its service account identity. `--set-secrets` is not needed.

> ⚠️ **Warning — 60s timeout causes `CancelledError` on LLM calls:** The original spec used `--timeout 60s`. Gemini API calls can take 30–120s under load, and LangGraph graph execution that chains multiple tool calls can exceed 60s easily. Symptoms: `asyncio.exceptions.CancelledError` in Cloud Logging, HTTP 500 from the `/pubsub` endpoint, and agents silently failing to complete tasks even though Pub/Sub delivery succeeded. **Use `--timeout 300s`** (shown above). If you need to update already-deployed services:
> ```powershell
> foreach ($agent in @("nexus-prime","ledger","beacon","pursuit","foreman","steward","scout")) {
>     gcloud run services update $agent --region=us-central1 --project=morphic-gaos-prod --timeout=300
> }
> ```

Each service exposes fifteen endpoints:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/pubsub` | POST | Pub/Sub push subscription delivery |
| `/ttl-sweep` | POST | Cloud Scheduler hourly TTL sweep (Nexus-Prime only) |
| `/sync` | POST | Apps Script approval callback (Nexus-Prime only) |
| `/archive` | POST | Cloud Scheduler nightly archive sweep (Nexus-Prime only) |
| `/daily-sync` | POST | Cloud Scheduler 6 AM morning briefing (Nexus-Prime only) |
| `/sheets-sync` | POST | Cloud Scheduler 5-min Sheets → BigQuery staging sync (Nexus-Prime only) |
| `/chat` | POST | Google Chat push events — messages and card callbacks (Nexus-Prime only) |
| `/vision` | POST | Owner-submitted project vision (Nexus-Prime only) |
| `/poll-comments` | POST | Doc comment poll trigger (Nexus-Prime only) |
| `/infra-provision` | POST | Trigger infrastructure diff + proposal card (Nexus-Prime only) |
| `/gmail-webhook` | POST | Gmail Pub/Sub push notification — enqueues for async processing (Nexus-Prime only) |
| `/daily-digest` | POST | Cloud Scheduler 6 AM email digest (Nexus-Prime only) |
| `/gmail-renew-watch` | POST | Renew Gmail watch subscription — called every 23h by Cloud Scheduler (Nexus-Prime only) |
| `/log-sink` | POST | Cloud Logging error-sink — receives structured log alerts and dispatches an email to the owner (Nexus-Prime only) |
| `/health` | GET | Liveness probe — always returns `{"status":"ok"}` |

All POST endpoints require a `Bearer` token in the `Authorization` header. Cloud Run ingress validates the OIDC token before the request reaches the handler; the handler check is defense-in-depth only.

> ⚠️ **Warning — Cloud Run image must be rebuilt after every code change:** Cloud Run does not auto-deploy from source. After any change to `main.py`, agent orchestrators, or `tools/`, the image must be rebuilt and all 7 services redeployed:
> ```powershell
> $PROJECT="morphic-gaos-prod"
> $IMAGE="us-central1-docker.pkg.dev/$PROJECT/cloud-run-source-deploy/gaos-agent:latest"
> gcloud builds submit --tag $IMAGE --project=$PROJECT .
> foreach ($agent in @("nexus-prime","ledger","beacon","pursuit","foreman","steward","scout")) {
>   gcloud run deploy $agent --image $IMAGE --region us-central1 --project $PROJECT `
>     --service-account "${agent}-sa@${PROJECT}.iam.gserviceaccount.com" `
>     --memory 512Mi --cpu 1 --timeout 300s --concurrency 1 `
>     --min-instances 0 --max-instances 5 --no-allow-unauthenticated `
>     --set-env-vars AGENT_NAME=$agent --quiet
> }
> ```
> **Lesson learned:** After Phase 2.5 Steps 3–6 added `/vision`, `/poll-comments`, `/daily-sync`, and `/chat` endpoints, the production Cloud Run services were still running revision `00001` (pre-Phase 2.5 code). `/poll-comments` returned 404 and the Scheduler's `doc-comment-poll` job was failing every 5 minutes as a result. Always rebuild and redeploy after significant feature additions.

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

> ⚠️ **The URL table below is from the original deployment and will not match yours.** After running the command above, copy your actual service URLs — they will look different. Update §5.2 with your URLs before creating Pub/Sub subscriptions.

> ⚠️ **The PowerShell health check below also uses hardcoded URLs.** Replace the `$url` line with your actual URL pattern before running it.

Actual URLs for the original `morphic-gaos-prod` deployment (reference only — yours will differ):

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
- Value: your nexus-prime Cloud Run URL + `/sync` (from the `gcloud run services list` output above)

For `morphic-gaos-prod` this was set to `https://nexus-prime-975461050387.us-central1.run.app/sync` on 2026-04-03 (confirmed in §19 4c checklist). For a new deployment, substitute your own URL.

**Verification (Windows PowerShell):**
```powershell
# Requires gcloud on PATH. If the command is not found, run:
#   (Get-Command gcloud -ErrorAction SilentlyContinue)?.Source
# or locate gcloud.cmd under your Cloud SDK install and use its full path instead.
# --include-email is required when calling identity-token with ADC user credentials
$token = gcloud auth print-identity-token --include-email
foreach ($agent in @('nexus-prime','ledger','beacon','pursuit','foreman','steward','scout')) {
  $url = "https://YOUR-${agent}-URL.run.app/health"  # replace with your actual URL
  $resp = Invoke-RestMethod -Uri $url -Headers @{Authorization="Bearer $token"}
  Write-Host "${agent}: $($resp.status)"
}
```
Expected: `ok` for each of the 7 services.

---

### 9.3 Infrastructure as Code (OpenTofu)

The `infra/main.tf` blueprint and `.github/workflows/deploy.yml` pipeline supersede the
manual Cloud Run deployment loop in §9.1 Step 2. All other provisioning steps
(Secrets, BigQuery, Sheets — §§3–8) remain authoritative and are not managed by OpenTofu.

**One-time bootstrap** — run these commands manually before the first CI run:

```bash
PROJECT=morphic-gaos-prod

# 1. Create the GCS bucket for OpenTofu state (must exist before `tofu init` runs)
gcloud storage buckets create gs://morphic-gaos-tfstate \
  --location=us-central1 --project=$PROJECT

# 2. Create Artifact Registry Docker repository (must exist before first docker push)
gcloud artifacts repositories create cloud-run-source-deploy \
  --repository-format=docker \
  --location=us-central1 \
  --project=$PROJECT

# 3. Configure Workload Identity Federation (no long-lived credentials)
PROJECT_NUMBER=$(gcloud projects describe $PROJECT --format='value(projectNumber)')

gcloud iam workload-identity-pools create github-actions \
  --location=global \
  --display-name="GitHub Actions" \
  --project=$PROJECT

gcloud iam workload-identity-pools providers create-oidc github-oidc \
  --location=global \
  --workload-identity-pool=github-actions \
  --display-name="GitHub OIDC" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="attribute.repository=='EgoNoBueno/Morphic-GAOS-Manager'" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --project=$PROJECT

gcloud iam service-accounts add-iam-policy-binding \
  deployer-sa@${PROJECT}.iam.gserviceaccount.com \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-actions/attribute.repository/EgoNoBueno/Morphic-GAOS-Manager" \
  --project=$PROJECT

# Store the provider resource name as GitHub Secret WIF_PROVIDER:
echo "projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-actions/providers/github-oidc"
```

---

## 19. Phase 4 Exit Criteria Checklist

> ⚠️ **First-deployment reading note:** This section tracks what has already been completed on the *original* production deployment. It appears here due to document history, but logically belongs after §18. If you are setting up GAOS for the first time, **skip this section now** and continue with §10 (Cloud Scheduler). Return here after your own system passes §18 to track your Phase 4 completion.

Phase 4 is complete — and the system is **production-ready** — when **every item** below is checked:

### 4a — Infrastructure Bootstrap

- [x] GCS state bucket created: `gs://morphic-gaos-tfstate` — created 2026-03-21, versioning enabled
- [x] Artifact Registry repo created: `cloud-run-source-deploy` — already existed (306MB)
- [x] `deployer-sa` service account created and all IAM bindings applied: `roles/run.admin` (project), `roles/artifactregistry.writer` (AR repo), `roles/storage.objectAdmin` (tfstate bucket), `roles/iam.serviceAccountUser` on all 7 agent SAs, `roles/resourcemanager.projectIamAdmin` (project), `roles/iam.serviceAccountAdmin` (project), `roles/secretmanager.admin` (project) — last 3 added 2026-04-02 when TF began managing project IAM bindings and new SAs
- [x] Workload Identity Federation pool `github-actions` + OIDC provider `github-oidc` created; `attribute.repository` condition locks to `EgoNoBueno/Morphic-GAOS-Manager` only; `roles/iam.workloadIdentityUser` binding applied to `deployer-sa`
- [x] `WIF_PROVIDER` and `WIF_SERVICE_ACCOUNT` GitHub Secrets set — verified via `gh secret list`
- [x] `SETTINGS_YAML` GitHub Secret set — base64-encoded `config/settings.yaml` written before the Docker `build` step so containers have full config at runtime; added 2026-03-23 after confirming CI builds were missing settings.yaml (Drive folder IDs, model aliases, etc.)
- [x] `production` GitHub Environment created with `EgoNoBueno` as required reviewer — configured via GitHub API 2026-03-21

### 4b — CI/CD Pipeline Validation

- [x] First push to `master` triggers the `build` job successfully (Docker image pushed to AR) — run `23376208619`, 2026-03-21
- [x] `plan` job runs `tofu plan -out=tfplan` with zero errors; artifact uploaded — plan completed in 17s
- [x] `apply` job gated behind `production` environment approval; after approval, all 7 Cloud Run services deploy at the correct revision — 7 imported, 0 added, 7 changed, 0 destroyed
- [x] `gcloud run services list --region=us-central1 --project=$PROJECT` shows all 7 services with state `Ready` — verified 2026-03-21
- [x] `GET /health` returns HTTP 200 for all 7 services — verified 2026-03-21 with OIDC token
- [x] **Grafana CEO Dashboard infrastructure deployed** — `grafana-sa`, BQ viewer/jobUser IAM bindings, Secret Manager accessor, `deployer_actAs_grafana`, and `google_cloud_run_v2_service.grafana` all in TF state — apply run `23944096299`, 2026-04-03. Required 4 partial-apply iterations due to cascading 409s on orphaned resources (see §2.2 import block warning).

### 4c — Production Wiring

> *URLs in this section are from the original deployment. Your deployment URLs will be different — use your `gcloud run services list` output.*

- [x] All Pub/Sub push subscriptions confirmed: 23 subscriptions exist with OIDC push auth; `pubsub-push-sa` has `roles/run.invoker` on all 7 services; Pub/Sub service agent granted `roles/iam.serviceAccountTokenCreator` — 2026-03-21. URLs use `*-975461050387.us-central1.run.app` (confirmed valid alias per `run.googleapis.com/urls` annotation — both URL formats work)
  > *(Note: 2026-04-02 — 8 `/pubsub` push subscriptions were found to be missing `--push-auth-service-account` (OIDC). All re-configured; a 9th Gmail subscription was already correct. All 25 active subscriptions verified with OIDC auth as of 2026-04-03.)*
  > *(Note: 2026-04-21 — `scout.sub.nexus-prime` added so Scout receives `RESEARCH_MANDATE` messages published to `agent.nexus-prime.events`. Required for MC1–MC3 marketing channel research mandates. Now 23 subscriptions total.)*
- [x] `VERTEX_AGENT_ENDPOINT` Script Property in Apps Script updated via Apps Script editor → Project Settings → Script Properties — set to `https://nexus-prime-975461050387.us-central1.run.app/sync` — 2026-04-03
- [x] `CLOUD_RUN_URL` environment variable on `nexus-prime` — **set automatically by CI/CD pipeline** (`Wire CLOUD_RUN_URL on nexus-prime` step in apply job reads TF output `nexus_prime_url` and updates the service in-place)
- [x] `settings.yaml` `chat.owner_space` set to `spaces/jbpdpSAAAAE` — confirmed 2026-03-21 via `Select-String owner_space config/settings.yaml`

### 4d — Live End-to-End Validation

- [x] **Email pipeline E2E:** Sent test email from `denton.hess@gmail.com` to monitored inbox — `compose_reply` sent reply (sent_id=`19d530ea9f5fb966`); second Gmail notification (from the outbound reply) dropped cleanly at terminal node with no loop — dedup gate and own-address exclusion both confirmed working — 2026-04-03.
  *(Re-verified 2026-04-16 after three-bug fix — see warning below. Test produced exactly 1 reply; no duplicates. Deployed revision: `nexus-prime-00099-lpx`.)*
- [x] **Approval Gate `/sync` E2E:** `scripts/_sync_e2e_test.py` — POST to `/sync`, hash check passes, `promote` node writes `Deployed` to `Agent_Approvals` row — **PASS 2026-04-03**. Root cause of prior failures: (1) `promote` node had bare `except: pass` on all exception paths — fixed by adding `_log_cloud` throughout; (2) `Agent_Approvals` Status column (col I) protection locked to owner only — SA added via Sheets API (`scripts/_patch_sheet_protection.py`).
- [x] **Vision blueprint E2E:** `scripts/_vision_e2e_test.py` — `vision_blueprint` called directly, DWD credentials working, Blueprint Doc created in Drive — **PASS 2026-04-03** (tokens_used=2354, cost_usd=0.0).

> ⚠️ **Bug fixed 2026-04-16 — Duplicate reply storm (three-bug chain):** A production incident produced 48 duplicate replies from a single inbound email. Root cause was a chain of three bugs that combined under load:
> 1. **Stale watermark:** `process_gmail_notification` guard `if new_history_id and not skipped_ids:` refused to advance `gmail_last_history_id` when any fetched message returned 404 (outbound replies are archived-immediately and 404 on re-fetch). This caused every subsequent Gmail push notification to replay ~100 old messages.
> 2. **Sheets 429 storm:** Replaying 100 messages per notification at full concurrency hammered the Sheets read quota — all Sheets calls began returning 429.
> 3. **Fail-open guards:** Both the idempotency guard (§13.1a) and pre-send status lock (§13.7) used `except: pass` / `except: log warning`, so 429 errors were invisible — all concurrent `compose_reply` tasks bypassed both guards and sent.
> **Fixes:** (1) watermark always advances when `new_history_id` exists; (2) idempotency guard fails closed (`return state` on any exception); (3) pre-send status lock fails closed. Queue purged via `gcloud pubsub subscriptions seek`. See `GAOS-Email-Pipeline-Spec.md §10.7`, `§13.1a`, `§13.7`.
- [x] **Nightly archive job:** Force-run the `nightly-archive` Cloud Scheduler job → `POST /archive` returns HTTP 200 → `NIGHTLY_ARCHIVE complete: 0 rows archived` logged (no rows aged ≥30 days yet); confirmed 2026-03-21. *(BQ timestamp parse bug + `approval_history` schema mismatch fixed same session — see bug note below.)*
- [x] **TTL sweep job:** Force-run `ttl-sweep` → `POST /ttl-sweep` returns HTTP 200; confirmed 2026-03-21 via Cloud Logging.
- [x] **Daily kickoff job:** Force-run `daily-kickoff` → `DAILY_SYNC complete: 2 logs, 0 errors, 5 pending approvals` logged + briefing card dispatched to `spaces/jbpdpSAAAAE`; confirmed 2026-03-21.
- [x] **Doc comment poll:** Force-run triggered `POST /poll-comments` → `poll_comments complete: 1 published, 0 errors, 1 docs polled`; confirmed 2026-03-21.

> ⚠️ **Bug fixed 2026-03-21 — Archive BQ timestamp + schema:** The nightly archive job was silently failing to insert rows into BigQuery due to two issues: (1) Google Sheets stores timestamps as `M/D/YYYY H:MM:SS` but BQ TIMESTAMP requires `YYYY-MM-DD HH:MM:SS` — `_parse_ts` now handles both formats and all `bq_rows` use `_parse_ts(...).isoformat()` instead of the raw string; (2) `aos_logs.approval_history` was missing `issue` (STRING) and `code_sha256` (STRING) columns — added via Python BQ client `update_table`. Fix is in `agents/nexus_prime/orchestrator.py`.

### 4e — Cost + Security Verification

- [x] Cloud Billing dashboard reviewed — **$1.83 net charged for Apr 1–20, 2026** (only Gemini API). Gross infrastructure spend was **$82.43** ($77.20 Cloud Run + $2.05 AR + $1.83 Gemini API + $1.07 Secret Manager + $0.28 Scheduler), all offset by GCP starter credits. ✅ 2026-04-20
  > ⚠️ **Warning — credits mask real run rate:** The net $1.83 is not representative of steady-state cost. Gross Cloud Run spend of ~$77/20 days (~$115/month) reflects two significant incident storms this month (89k Pub/Sub fault loop + 48-reply duplicate storm). Once credits are exhausted, the $10/month budget alert will need revisiting. Track gross spend trend, not net, to spot cost anomalies early.
- [x] Budget alert configured at $10/month threshold in GCP Billing console — `SL10 Cloud Dev Budget` at $10/month with 50%/90%/100% thresholds; confirmed 2026-03-21 via `gcloud billing budgets list`
- [x] Cloud Logging retention set to 7 days for `projects/morphic-gaos-prod/logs/` — `_Default` bucket: 7 days; confirmed 2026-03-21 via `gcloud logging buckets describe _Default`
- [x] All 7 Cloud Run services confirm `--no-allow-unauthenticated` — no `allUsers` IAM binding on any service; verified 2026-03-21 via `gcloud run services get-iam-policy` on all 7

> ⚠️ **Note:** `GET /health` is intended for Cloud Run's internal liveness probe (authenticated via the SA identity), not for unauthenticated external pinging. If you need to test health locally, add an OIDC token via SA impersonation: `gcloud auth print-identity-token --impersonate-service-account=nexus-prime-sa@morphic-gaos-prod.iam.gserviceaccount.com --audiences=<SERVICE_URL>` — user ADC cannot produce ID tokens (only service account credentials or metadata server can).

### 4f — GAOS-Doctor Runbook

- [x] Run the full GAOS-Doctor checklist (`scripts/gaos_doctor.py`) and confirm all health checks pass — **33/33 passed 2026-03-21**: Sheet connectivity ✅, all 8 Pub/Sub topics ✅, 23 subscriptions active ✅, all 6 secrets accessible ✅, all 7 Cloud Run `/health` endpoints HTTP 200 ✅, all 7 Vertex AI RAG corpora indexed ✅
  > *(Re-run 2026-04-03 after Phase 4 deploy and OIDC/timeout fixes: **41 OK, 1 WARN, 0 FAIL** — GAOS-Doctor expanded to 42 checks across 8 groups. 1 WARN = transient Cloud Logging error in prior 1h, not a pattern.)*

> **Note:** The GAOS-Doctor runbook is implemented as `scripts/gaos_doctor.py` (not yet the CLI described in `Docs/GAOS-Doctor.md`). Run with: `.venv\Scripts\python.exe scripts/gaos_doctor.py`

---

## 20. Phase 4 Bootstrap Runbook

One-time commands to go from a Phase 3 code-complete state to a fully deployed Phase 4 production system. Run in order. All commands are PowerShell-compatible.

```powershell
# ── Setup variables ──────────────────────────────────────────────────────
$PROJECT = "morphic-gaos-prod"
$REGION  = "us-central1"
$REPO    = "cloud-run-source-deploy"
$SA      = "deployer-sa"

# ── Step 1: GCS Terraform state bucket ──────────────────────────────────
gsutil mb -p $PROJECT -l $REGION gs://morphic-gaos-tfstate/
gsutil versioning set on gs://morphic-gaos-tfstate/

# ── Step 2: Artifact Registry repo ──────────────────────────────────────
gcloud artifacts repositories create $REPO `
  --repository-format=docker --location=$REGION --project=$PROJECT

# ── Step 3: Deployer service account + IAM ───────────────────────────────
gcloud iam service-accounts create $SA `
  --display-name="GAOS CI/CD Deployer" --project=$PROJECT

# Cloud Run admin (deploy all services)
gcloud projects add-iam-policy-binding $PROJECT `
  --member="serviceAccount:${SA}@${PROJECT}.iam.gserviceaccount.com" `
  --role="roles/run.admin"

# AR writer (push images) — scoped to the repo resource, not the project
gcloud artifacts repositories add-iam-policy-binding $REPO `
  --location=$REGION --project=$PROJECT `
  --member="serviceAccount:${SA}@${PROJECT}.iam.gserviceaccount.com" `
  --role="roles/artifactregistry.writer"

# GCS state bucket admin (tofu state read/write)
gsutil iam ch "serviceAccount:${SA}@${PROJECT}.iam.gserviceaccount.com:roles/storage.objectAdmin" `
  gs://morphic-gaos-tfstate/

# actAs each per-agent SA (required to set SA identity on Cloud Run deploys)
foreach ($agent in @("nexus-prime","ledger","beacon","pursuit","foreman","steward","scout")) {
  gcloud iam service-accounts add-iam-policy-binding `
    "${agent}-sa@${PROJECT}.iam.gserviceaccount.com" `
    --role="roles/iam.serviceAccountUser" `
    --member="serviceAccount:${SA}@${PROJECT}.iam.gserviceaccount.com" `
    --project=$PROJECT
}

# ── Step 4: Workload Identity Federation ────────────────────────────────
$PROJECT_NUMBER = (gcloud projects describe $PROJECT --format="value(projectNumber)")

gcloud iam workload-identity-pools create github-actions `
  --location=global --display-name="GitHub Actions" --project=$PROJECT

gcloud iam workload-identity-pools providers create-oidc github-oidc `
  --location=global `
  --workload-identity-pool=github-actions `
  --display-name="GitHub OIDC" `
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" `
  --attribute-condition="attribute.repository=='EgoNoBueno/Morphic-GAOS-Manager'" `
  --issuer-uri="https://token.actions.githubusercontent.com" `
  --project=$PROJECT

gcloud iam service-accounts add-iam-policy-binding `
  "${SA}@${PROJECT}.iam.gserviceaccount.com" `
  --role="roles/iam.workloadIdentityUser" `
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-actions/attribute.repository/EgoNoBueno/Morphic-GAOS-Manager" `
  --project=$PROJECT

# Print the two values to paste as GitHub Secrets
Write-Host "WIF_PROVIDER = projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-actions/providers/github-oidc"
Write-Host "WIF_SERVICE_ACCOUNT = ${SA}@${PROJECT}.iam.gserviceaccount.com"

# ── Step 5: GitHub Secrets (via gh CLI) ─────────────────────────────────
# Replace <WIF_PROVIDER_VALUE> with the output from Step 4
gh secret set WIF_PROVIDER --body "projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-actions/providers/github-oidc" --repo EgoNoBueno/Morphic-GAOS-Manager
gh secret set WIF_SERVICE_ACCOUNT --body "${SA}@${PROJECT}.iam.gserviceaccount.com" --repo EgoNoBueno/Morphic-GAOS-Manager

# SETTINGS_YAML — base64-encoded config/settings.yaml so CI can write it into
# the Docker build context before `docker build`. Must be re-set whenever
# settings.yaml changes (e.g. new Drive folder IDs, model aliases, etc.).
# On Windows (PowerShell):
$b64 = [Convert]::ToBase64String([System.IO.File]::ReadAllBytes("config\settings.yaml"))
gh secret set SETTINGS_YAML --body $b64 --repo EgoNoBueno/Morphic-GAOS-Manager

# ── Step 6: GitHub Environment ──────────────────────────────────────────
# Manual — GitHub UI only:
# Settings → Environments → New environment → name: "production"
# Add yourself as a required reviewer → Save protection rules

# ── Step 7: Trigger the pipeline ────────────────────────────────────────
git push origin master   # triggers build → plan → (manual gate) → apply

# ── Step 8: Verify all 7 services deployed ──────────────────────────────
gcloud run services list --region=$REGION --project=$PROJECT
```

**After the apply job succeeds:**

1. Read the 7 Cloud Run service URLs from the `service_urls` output printed by `tofu apply`, or run: `gcloud run services list --region=us-central1 --project=$PROJECT`
2. Update Pub/Sub push subscription endpoints (§5.2) to the new URLs.
3. Set `VERTEX_AGENT_ENDPOINT` in Apps Script Script Properties.
4. `CLOUD_RUN_URL` on nexus-prime is **wired automatically** by the `Wire CLOUD_RUN_URL on nexus-prime` step at the end of the apply job — no manual action required.
5. Run the Phase 4 exit criteria checklist (§19).

> ⚠️ **Warning — `tofu init` required before first `tofu plan`:** The CI/CD pipeline runs `tofu init` automatically in the `plan` job. If you want to run Terraform locally before pushing, run `tofu init -backend-config="bucket=morphic-gaos-tfstate"` from the `infra/` directory first. The `-backend-config` is not hardcoded in `main.tf` so it can be overridden for different environments.

> ⚠️ **Note — attribute-condition scope:** The `--attribute-condition` locks this WIF provider to
> tokens issued for `EgoNoBueno/Morphic-GAOS-Manager` only. A fork cannot impersonate `deployer-sa`
> even if it triggers a workflow — the condition is evaluated server-side by Google before the token
> is issued. The IAM binding further restricts impersonation to the specific `attribute.repository`
> principal set, not to the pool as a whole.

**Create the GitHub Environment (one-time, via GitHub UI):**

1. Repository → Settings → Environments → New environment → name: `production`
2. Enable "Required reviewers" → add yourself
3. Save

This environment is the human approval gate. After the Plan job completes, GitHub pauses
the Apply job and sends a review notification. Click "Approve and deploy" to proceed.

**Artifact retention:** `tfplan` artifacts expire after 3 days. A plan older than 3 days
cannot be applied — push a new commit to regenerate. This prevents stale plans from
deploying configuration that no longer matches the current infrastructure state.

**Workflow summary:**

| Trigger | Job | Action |
|---------|-----|--------|
| `push` to `master` | `build` | `docker build` + `docker push` to Artifact Registry (SHA tag) |
| after `build` | `plan` | `tofu plan -var="image_tag=<sha>" -out=tfplan` → upload artifact (3 days) |
| after `plan` + human approval | `apply` | Download `tfplan` → `tofu apply tfplan` |

---

## 10. Cloud Scheduler Jobs

Cloud Scheduler delivers requests via OIDC. The Nexus-Prime service account issues the token; it must also have `roles/run.invoker` on the nexus-prime Cloud Run service so the token is accepted.

> **Preferred provisioning method:** Use `scripts/provision_schedulers.py` instead of the manual `gcloud` commands in §10.2 and §10.3. It resolves the nexus-prime Cloud Run URL dynamically (no hardcoded URL), creates or patches the two scheduled jobs (`gaos-archive` and `gaos-daily-sync`) idempotently, and grants the IAM invoker binding automatically.
>
> ```powershell
> python scripts/provision_schedulers.py [--project <project_id>]
> ```
>
> The `gcloud` commands below are retained as reference for manual individual operations and for §10.1 and §10.4 which the script does not cover.

**Grant Nexus-Prime SA invoker rights on its own service (manual — the script handles this automatically):**

```bash
PROJECT=morphic-gaos-prod
gcloud run services add-iam-policy-binding nexus-prime \
  --region=us-central1 --project=$PROJECT \
  --member="serviceAccount:nexus-prime-sa@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

### 10.1 TTL Sweep Job (hourly)

Scans `Agent_Approvals` tab for proposals older than their TTL and re-notifies or auto-rejects them.

> ⚠️ **Replace `NP_URL` with your actual nexus-prime Cloud Run URL** (from §9.2). Use `scripts/provision_schedulers.py` to avoid this manual substitution.

```bash
NP_URL="https://YOUR-NEXUS-PRIME-URL.run.app"   # replace with your URL from §9.2
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
NP_URL="https://YOUR-NEXUS-PRIME-URL.run.app"   # replace with your URL from §9.2
gcloud scheduler jobs create http gaos-archive \
  --location=us-central1 \
  --schedule="0 2 * * *" \
  --uri="${NP_URL}/archive" \
  --oidc-service-account-email="nexus-prime-sa@morphic-gaos-prod.iam.gserviceaccount.com" \
  --project=morphic-gaos-prod
```

> ⚠️ **Warning — hardcoded URL:** The `NP_URL` above is the URL for the `morphic-gaos-prod` deployment. On a new project it will be different. Use `scripts/provision_schedulers.py` instead — it resolves the URL dynamically from the Cloud Run Admin API.

**Verification:** In Cloud Scheduler console, both jobs appear with state `ENABLED`. Run each manually by clicking **Force run** — both should return HTTP 200. `ttl-sweep` hits `POST /ttl-sweep`; `gaos-archive` hits `POST /archive` (implemented in Phase 2 Item 3 — archives aged Sheet rows to BigQuery).

### 10.3 Daily Kickoff Job (6:00 AM daily) — *Phase 2.5 Step 2*

Triggers Nexus-Prime's morning briefing. Nexus-Prime queries overnight Logs, Error Logs, and pending Agent_Approvals rows, then posts a briefing Chat card to the owner's space configured in `settings.yaml` under `chat.owner_space`.

**Prerequisite:** Set `chat.owner_space` in `settings.yaml` to the owner's DM space resource name (e.g. `spaces/AAAAXXXXXXX`). Find this value in any inbound `/chat` event payload under `event.space.name`, or in the Google Chat API console.

```bash
NP_URL="https://YOUR-NEXUS-PRIME-URL.run.app"   # replace with your URL from §9.2
gcloud scheduler jobs create http gaos-daily-sync \
  --location=us-central1 \
  --schedule="0 6 * * *" \
  --uri="${NP_URL}/daily-sync" \
  --oidc-service-account-email="nexus-prime-sa@morphic-gaos-prod.iam.gserviceaccount.com" \
  --project=morphic-gaos-prod
```

> ⚠️ **Warning — hardcoded URL:** Same as §10.2 — use `scripts/provision_schedulers.py` for new deployments to avoid URL drift.

### 10.4 Doc Comment Poll Job (every 5 minutes) — *Phase 2.5*

Polls open Blueprint Google Docs for new owner comments so Nexus-Prime can process `COMMENT_RECEIVED` constraint updates without waiting for a manual trigger.

```bash
NP_URL="https://YOUR-NEXUS-PRIME-URL.run.app"   # replace with your URL from §9.2
gcloud scheduler jobs create http doc-comment-poll \
  --location=us-central1 \
  --schedule="*/5 * * * *" \
  --uri="${NP_URL}/poll-comments" \
  --oidc-service-account-email="nexus-prime-sa@morphic-gaos-prod.iam.gserviceaccount.com" \
  --project=morphic-gaos-prod
```

**Verification:** Both Phase 2.5 jobs appear with state `ENABLED` in the Scheduler console. Force-run `gaos-daily-sync` — a morning briefing card appears in the owner's Chat space (`POST /daily-sync` returns HTTP 200). Force-run `doc-comment-poll` — `POST /poll-comments` returns HTTP 200.

### 10.5 Gmail Watch Renewal Job (every 23 hours) — *Phase 5*

Renews the Gmail Pub/Sub watch subscription before it expires (Gmail maximum is 7 days). Running every 23 hours ensures the watch is always current, even if a single renewal fails.

```bash
NP_URL="https://YOUR-NEXUS-PRIME-URL.run.app"   # replace with your URL from §9.2
gcloud scheduler jobs create http gmail-renew-watch \
  --location=us-central1 \
  --schedule="0 */23 * * *" \
  --uri="${NP_URL}/gmail-renew-watch" \
  --oidc-service-account-email="nexus-prime-sa@morphic-gaos-prod.iam.gserviceaccount.com" \
  --project=morphic-gaos-prod
```

**Verification:** The `gmail-renew-watch` job appears with state `ENABLED` in the Scheduler console. Force-run the job — `POST /gmail-renew-watch` returns HTTP 200 and Cloud Logging shows `gmail-renew-watch: watch renewed, expires_at=<timestamp>`.

---

### 10.6 Gmail Pub/Sub Watch — One-Time Initial Setup

> **In plain English:** Before the renewal job (§10.5) can do anything, Gmail must be told to publish notifications to a Pub/Sub topic whenever a new email arrives. This is a one-time setup. Once the initial "watch" is registered, the renewal job keeps it alive indefinitely. Without this step, Nexus-Prime never receives Gmail events.

**Prerequisites — Authentication Model:**

> **In plain English:** Nexus-Prime does **not** use its own service account to access Gmail. It uses a stored OAuth2 refresh token that belongs to the Gmail account you want to monitor. You run a script once, sign in to Gmail in a browser, and the script captures a long-lived refresh token. That token is stored encrypted in Secret Manager. Every time Cloud Run needs to call Gmail, it fetches the token from Secret Manager, exchanges it for a short-lived access token, and calls the Gmail API — no further human action required unless you deliberately revoke access or need to rotate the key.

**Auth model: stored user OAuth2 refresh token — not Domain-Wide Delegation (DWD) and not service account impersonation.** `nexus-prime-sa` does not hold Gmail credentials. Credentials belong to the Gmail account owner and are stored in Secret Manager.

**What is stored:** Secret Manager secret `GMAIL_OAUTH_CREDENTIALS` — a JSON object:
```json
{"client_id": "...", "client_secret": "...", "refresh_token": "..."}
```
`client_id` and `client_secret` come from the OAuth 2.0 client you created in GCP Console (APIs & Services → Credentials → OAuth 2.0 Client IDs). `refresh_token` is produced during the one-time OAuth browser flow run in Step 5 below.

**How Cloud Run loads it:** At every Gmail API call, `tools/gmail.py:_load_credentials()` calls `get_secret("GMAIL_OAUTH_CREDENTIALS", project_id)`, parses the JSON, and constructs a `google.oauth2.credentials.Credentials` object. Google's auth library automatically exchanges the refresh token for a short-lived access token — no manual rotation is needed on any schedule.

**When the refresh token can become invalid:**
- The Gmail account owner revokes access at <https://myaccount.google.com/permissions>
- The token is unused for **6 months** (Google auto-revokes idle refresh tokens)
- The GCP OAuth app is in **Testing** status (not published) — Testing tokens expire after 7 days regardless of use; publish the app to production to remove this limit

> ⚠️ **Watch renewal ≠ credential rotation.** The `gmail-renew-watch` Scheduler job (§10.5) renews the *Gmail Pub/Sub watch subscription*, which Google expires every 7 days. This is completely separate from the OAuth refresh token in `GMAIL_OAUTH_CREDENTIALS` — the token stays valid indefinitely under normal conditions.

**To rotate the refresh token** (e.g., security key rotation or recovery from a revoked token):
1. Revoke the current grant: go to <https://myaccount.google.com/permissions> and remove the GAOS app entry. This step is required — without it the OAuth flow re-uses the existing session and does not issue a new `refresh_token`.
2. Re-run `scripts/setup_gmail_oauth.py --project morphic-gaos-prod`.
3. Store the new JSON blob printed by the script into Secret Manager:
   ```powershellOpt
   # Paste the JSON output from setup_gmail_oauth.py between the quotes below
   $newJson = '{"client_id": "...", "client_secret": "...", "refresh_token": "..."}'
   $newJson | gcloud secrets versions add GMAIL_OAUTH_CREDENTIALS `
     --data-file=- --project=morphic-gaos-prod
   ```
4. Verify: force-run the `gmail-renew-watch` Scheduler job — it must return HTTP 200. A 401 from Gmail means the secret version was not stored correctly.

> ⚠️ **`client_secrets.json` must exist before Step 5.** Download it from GCP Console → APIs & Services → Credentials → your OAuth 2.0 Client ID → Download JSON. Place it in the repo root as `client_secrets.json`. It is excluded from git (`.gitignore`). Without it, `setup_gmail_oauth.py` exits immediately.

**Step 1 — Enable the Gmail API:**

```bash
gcloud services enable gmail.googleapis.com --project=morphic-gaos-prod
```

**Step 2 — Create the Gmail inbox notification topic:**

```bash
gcloud pubsub topics create gmail.nexus-prime.inbox --project=morphic-gaos-prod
```

**Step 3 — Grant Gmail permission to publish to the topic:**
Gmail uses the fixed service account `gmail-api-push@system.gserviceaccount.com` to send notifications. It must be a publisher on your topic:

```bash
gcloud pubsub topics add-iam-policy-binding gmail.nexus-prime.inbox \
  --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
  --role="roles/pubsub.publisher" \
  --project=morphic-gaos-prod
```

**Step 4 — Create the subscription (push to nexus-prime):**
Do this after §9.2 gives you the nexus-prime Cloud Run URL:

```bash
PUSH_SA="pubsub-push-sa@morphic-gaos-prod.iam.gserviceaccount.com"
NP_URL="https://YOUR-NEXUS-PRIME-URL.run.app"      # replace with §9.2 output

gcloud pubsub subscriptions create nexus-prime.sub.gmail \
  --topic="projects/morphic-gaos-prod/topics/gmail.nexus-prime.inbox" \
  --push-endpoint="${NP_URL}/gmail-webhook" \
  --push-auth-service-account=$PUSH_SA \
  --ack-deadline=60 --project=morphic-gaos-prod
```

**Step 5 — Run the one-time OAuth2 flow and register the initial watch:**

This script opens a browser tab for Gmail sign-in, captures the refresh token, prints the JSON blob to store in Secret Manager, creates the `GAOS-Tasks` Gmail label if missing, and registers the initial `watch()` call.

```powershell
# Requires: client_secrets.json in repo root (see Prerequisites above)
# Opens a browser tab — sign in as the Gmail account you want to monitor.
python scripts/setup_gmail_oauth.py --project morphic-gaos-prod
```

When the script finishes, it prints:
- The `GMAIL_OAUTH_CREDENTIALS` JSON — store this in Secret Manager immediately (see the rotation procedure in Prerequisites above).
- Three `settings.yaml` values to update: `monitored_address`, `label_id`, `pubsub_topic`.
- A reminder to also store `GMAIL_AUTHORIZED_SENDERS` in Secret Manager.

> ⚠️ **Warning — `GMAIL_AUTHORIZED_SENDERS` must never contain the outbound send-from alias:**
> The system sends email from `settings.gmail.sender_address` (e.g. `aos@sl10repairtechs.com`).
> If that address appears in `GMAIL_AUTHORIZED_SENDERS`, every outbound reply triggers an inbound
> processing cycle, which sends another reply — an infinite loop. The 2026-04-02 incident produced
> ~89,000 Pub/Sub faults and 18 unwanted emails from this exact misconfiguration.
> **Correct value:** only real human inboxes — e.g. `dhess@sl10repairtechs.com,denton.hess@gmail.com`.
> No outbound alias, no service account address. See Rule 26.1 in `AI-Autocoding-Rules.md`.

**Verification:** After running the script, check Cloud Logging — you should see `gmail-renew-watch: watch registered` or a similar confirmation. Force-run the `gmail-renew-watch` Scheduler job — it should return HTTP 200.

> ⚠️ **Gmail watch expires after 7 days.** If the Cloud Scheduler renewal job fails (or doesn't exist yet), the watch will silently expire and Nexus-Prime stops receiving email events. The `gmail-renew-watch` job (§10.5) prevents this by renewing every 23 hours. Ensure that job is deployed and healthy before relying on Gmail-based workflows.

---

### 10.7 Sheets → BigQuery Staging Sync (every 5 minutes)

Keeps the `bigquery_staging` dataset in sync with the live Google Sheet tabs so Grafana dashboards reflect near-real-time data.

| Field | Value |
|-------|-------|
| **Job name** | `gaos-sheets-sync` |
| **Schedule** | `*/5 * * * *` (every 5 minutes) |
| **Endpoint** | `POST /sheets-sync` |
| **Agent** | Nexus-Prime only |
| **Provisioned by** | `scripts/provision_schedulers.py` |

**What it does:** Reads each configured Sheet tab, normalizes headers, and calls `replace_rows()` to overwrite the matching staging table in BigQuery. Logs `sheets-sync: <tab> → <table> (<N> rows)` per tab.

**Verification:**
```powershell
gcloud scheduler jobs describe gaos-sheets-sync --project=morphic-gaos-prod --location=us-central1
# Force a run:
gcloud scheduler jobs run gaos-sheets-sync --project=morphic-gaos-prod --location=us-central1
```

Check Cloud Logging for `sheets-sync:` entries confirming rows written.

---

### 10.8 Daily Digest Email (6:00 AM PST daily)

Sends a daily system health and activity summary email to the owner at 6 AM PST.

| Field | Value |
|-------|-------|
| **Job name** | `gaos-daily-digest` |
| **Schedule** | `0 14 * * *` (14:00 UTC = 6:00 AM PST / 7:00 AM PDT) |
| **Endpoint** | `POST /daily-digest` |
| **Agent** | Nexus-Prime only |
| **Provisioned by** | `scripts/provision_schedulers.py` |

**What it does:** Reads System_State, Main Control Plane, Email Inbox, Logs, Error Logs, Agent_Approvals, and the `api_call_log` BQ table. Formats a structured digest via `FAST_MODEL` and sends it via Gmail. Subject: `GAOS Daily Digest — <date>`.

**Verification:**
```powershell
gcloud scheduler jobs describe gaos-daily-digest --project=morphic-gaos-prod --location=us-central1
# Force a run:
gcloud scheduler jobs run gaos-daily-digest --project=morphic-gaos-prod --location=us-central1
```

Check the owner inbox for the digest email and Cloud Logging for `daily-digest:` confirmation.

> ⚠️ **Outbound cap applies.** The digest send is subject to Rule 26 flood guard (`settings.outbound.flood_threshold`). If more than 10 emails were sent in the prior 60 minutes, the digest will be skipped and logged at ERROR.

---

### 10.9 GAOS-Doctor Daily Run (7:00 AM PT daily)

Runs `scripts/gaos_doctor.py` as a **Cloud Run Job** and emails the health report to `settings.gmail.alert_address` after checks complete.

| Field | Value |
|-------|-------|
| **Job name** | `gaos-doctor` (Cloud Run Job) |
| **Scheduler job name** | `gaos-doctor-daily` |
| **Schedule** | `0 7 * * *` (7:00 AM PT, `America/Los_Angeles`) |
| **Container** | Built from `Dockerfile.doctor`; sets `DOCTOR_SEND_REPORT=1` |
| **Provisioned by** | `scripts/provision_doctor_job.py` |

> ⚠️ **`oauthToken` not `oidcToken`:** The Scheduler job triggers the Cloud Run Jobs v2 API (`POST …/jobs/gaos-doctor:run`), not an HTTP service endpoint. Cloud Scheduler must use `oauthToken` (scope: `cloud-platform`) for the Jobs API. Using `oidcToken` returns 403.

**Deploy:**
```powershell
# 1. Build and push the doctor image
gcloud builds submit `
  --tag us-central1-docker.pkg.dev/morphic-gaos-prod/cloud-run-source-deploy/gaos-doctor:latest `
  --dockerfile=Dockerfile.doctor .

# 2. Provision job + scheduler (idempotent)
python scripts/provision_doctor_job.py --project morphic-gaos-prod

# 3. Verify: force a manual run
gcloud run jobs execute gaos-doctor --region=us-central1 --project=morphic-gaos-prod
```

**What it does:** Runs all ~50 GAOS-Doctor checks and emails a structured plain-text report. Subject: `GAOS-Doctor <date>: N OK  N WARN  N FAIL`. Full design in `Docs/GAOS-Doctor.md`.

**Verification:**
```powershell
gcloud scheduler jobs describe gaos-doctor-daily --project=morphic-gaos-prod --location=us-central1
gcloud run jobs describe gaos-doctor --region=us-central1 --project=morphic-gaos-prod
```

---

## 11. Cloud Logging — Retention Configuration

> **In plain English:** Every time an agent does something, it writes a log entry. Cloud Logging stores all these entries. By default, Google keeps them for 30 days, but that generates more storage than we need and can trigger costs. We reduce retention to 7 days to stay within Google's free tier (50 GB/month).

Reduce Cloud Logging retention from the default 30 days to 7 days to stay within the free tier (50 GB/month ingestion).

1. Go to Cloud Logging → **Log Storage** (or Logs Router in newer UI)
2. Edit the `_Default` log bucket
3. Set retention period to **7 days**
4. Save

This is a manual one-time step; there is no `gcloud` CLI command for bucket retention changes.

---

## 12. Vertex AI Memory Bank

> **In plain English:** Each agent has long-term memory stored in Vertex AI RAG (Retrieval-Augmented Generation). Think of it like a searchable filing cabinet: when an agent needs to recall something it learned in the past, it searches its memory bank and gets back the most relevant entries. Each business domain (accounting, sales, etc.) has its own separate memory bank called a "corpus" so agents don't mix up information from different areas.

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
| 0 | Unit test suite | `pytest` | Full suite passes — 0 failures, 0 errors |
| 1 | Sheet write | `python -c "from tools.google_sheets import init_sheets_client, append_row; import datetime; init_sheets_client('default'); append_row('Logs', {'timestamp': datetime.datetime.utcnow().isoformat(), 'level': 'SMOKE_TEST', 'source': 'smoke', 'message': 'phase 1 test'}, 'default')"` | Row appears in `Logs` tab |
| 2 | Sheet read | `python -c "from tools.google_sheets import init_sheets_client, get_all_records; init_sheets_client('default'); rows = get_all_records('Project Registry', 'default'); print(f'{len(rows)} rows')"` | Prints `0 rows` (or more once project rows are added) |
| 3 | Pub/Sub publish | `python -c "from tools.pubsub import publish; from models import A2AMessage; ..."` sending a test message to `agent.nexus-prime.events` | Message ID returned; no exception |
| 4 | Approval trigger | Run `python scripts/smoke_test_4.py` — script appends a throwaway row, prompts you to type `Approved` in the UI, then polls for K/L stamp and Logs entry | K (Approved By) and L (Approver Tier) populate; APPROVAL entry appears in Logs tab ✅ |
| 5 | Secret access | `python -c "from tools.secrets import get_secret; v = get_secret('GEMINI_API_KEY', 'morphic-gaos-prod'); print(v[:8] + '...')"` | First 8 chars of API key printed |
| 6+7+8tests | Webhook HMAC (all 8 cases) | Run `python scripts/smoke_test_6_7.py` — automated; runs all 8 webhook test cases from `GAOS-Manager-Spec.md §14` (valid payload, tampered sig, missing sig, bad schema, bad project_id, priority OOB, empty body, replay) | `8/8 tests passed` printed; cleanup note shows smoke rows to delete from `Agent_Approvals` |

> ⚠️ **Warning — APIs must be enabled in `morphic-gaos-prod`:** All API calls from `oauth-client.json` are billed/quota-tracked against `morphic-gaos-prod`. If you get a 403 `accessNotConfigured` error during any smoke test, run:
> ```powershell
> gcloud services enable sheets.googleapis.com drive.googleapis.com pubsub.googleapis.com secretmanager.googleapis.com bigquery.googleapis.com script.googleapis.com --project=morphic-gaos-prod
> ```

---

## 14. Phase 1 Exit Criteria Checklist

Phase 1 is complete — and Phase 2 (Ollama integration) may begin — when **every item** below is checked:

- [x] `tools/google_sheets.py` appends a row and reads a cell value without errors
- [x] All 332 unit tests pass (`pytest` — green, 0 failures)
- [x] Apps Script `onEdit` trigger fires on Status cell change — stamps Approved By (col K), Approver Tier (col L), and writes an APPROVAL entry to the Logs tab ✅ (smoke test 4 passed 2026-03-17)
- [x] Local Python subscriber receives the Pub/Sub push and prints the proposal ID and new status ✅ (`scripts/smoke_test_pubsub_sub.py` — PASS 2026-03-18)
- [x] Cloud Scheduler TTL sweep job exists and can be triggered manually (HTTP 200 response)
- [x] Cloud Scheduler nightly archive job exists with state `ENABLED` — `POST /archive` implemented in Phase 2 Item 3 (returns HTTP 200)
- [x] **[Phase 2.5]** Google Chat App created in Google Cloud console; Chat API enabled; `nexus-prime-sa` added to the Chat space as the bot identity (authenticated via service account ADC — no Secret Manager token required)
- ❌ **[Phase 2.5 — ABANDONED 2026-03-30]** Google Chat end-to-end delivery: never successfully delivered a message from mobile after ~2 weeks. Failure summary: (1) `CLOUD_RUN_URL` env var missing from initial deploy → fixed; (2) `chat@system.gserviceaccount.com` not in `roles/run.invoker` → fixed; (3) Chat exhausts 2–3 retries in ~60s — IAM propagation took longer, retry budget spent; (4) tunnel URL instability caused stale `OLLAMA_HOST` → fallback Gemini calls hitting 429; (5) stale Cloud Run image (6 days old) deployed during active testing. Full post-mortem in `GAOS-Nexus-Prime-Spec.md §3.2 chat_respond` warning block. **Replaced by Gmail polling — see §10.6.**
- [x] **[Phase 2.5]** Vertex AI Search datastore created and indexed against Drive `Knowledge/` folder; datastore ID stored in `settings.yaml`
- [x] **[Phase 2.5]** Google Custom Search Engine created; CSE ID and API key stored in Secret Manager as `GOOGLE_SEARCH_CX` and `GOOGLE_SEARCH_API_KEY`
- ❌ **[Phase 2.5 — NOT YET DOCUMENTED]** AppSheet app deployed and connected to the Google Sheets workbook (`Agent_Approvals` + `Project Registry` tabs at minimum) — setup instructions have not been written. **Skip this item.** No AppSheet setup is required for core system function; this is an optional mobile UI layer that may be documented in a future phase.
- [x] **[Phase 2.5]** Cloud Scheduler `gaos-daily-sync` job created (6 AM daily, `POST /daily-sync`, returns HTTP 200)
- [x] **[Phase 2.5]** Cloud Scheduler `doc-comment-poll` job created (every 5 minutes, `POST /poll-comments`, returns HTTP 200)
- [x] **[Phase 2.5]** `POST /chat` endpoint returns HTTP 200; Nexus-Prime responds in Chat thread within 10 seconds — ⚠️ endpoint works (JWT verifies, routes correctly) but Google Chat never reliably delivered messages from mobile. See abandonment note above.
- ❌ **[Phase 2.5 — ABANDONED]** Approval Gate Chat-path validated end-to-end: Chat card button tap → `APPROVAL_RESULT` published to Pub/Sub → Nexus-Prime resumes the parked task → `Agent_Approvals` row updated + audit row written to Logs tab — blocked by Google Chat delivery failure. Will be re-evaluated with Gmail-based approval path.
- [x] All webhook smoke tests passing: `python scripts/smoke_test_6_7.py` prints `8/8 tests passed` ✅ (2026-03-18)
- [x] All 8 individual webhook test cases from `GAOS-Manager-Spec.md §14` confirmed via smoke_test_6_7.py output ✅ (2026-03-18)
- [x] `setupProtections()` has been run; Status/Code/Hash columns are locked to owner
- [x] Authorized Approvers tab has at least one row (owner, tier 5, active=TRUE)
- [x] `settings.yaml` is complete with correct `workbook_id`, `knowledge_folder_id`, and model aliases
- [x] `WEBHOOK_URL` secret is populated in Secret Manager
- [x] BigQuery dataset and 7 tables created with TTL partitioning
- [x] All Knowledge/ seed files created in Drive
- [x] All 7 Cloud Run services deployed and returning `{"status":"ok"}` on `/health`
- [x] All 22 Pub/Sub push subscriptions created with OIDC auth (`pubsub-push-sa`)
- [x] Vertex AI RAG corpora created — all 7 `memory_bank.corpora` entries in `settings.yaml` are non-empty
- [x] Cloud Logging retention reduced to 7 days

---

## 15. Phase 2 Exit Criteria Checklist

Phase 2 is complete — and Phase 3 (multi-agent orchestration) may begin — when **every item** below is checked:

- [x] Ollama installed locally (`ollama list` shows at least one model) ✅ (`llama3:latest`, `qwen2.5-coder:7b` — confirmed 2026-03-18)
- [x] `LOCAL_MODEL` alias in `settings.yaml` resolves to an installed Ollama model (`ollama/llama3`) ✅ (2026-03-18)
- [x] `LOCAL_MODEL_TIMEOUT_SECONDS` set high enough to avoid false Gemini fallback (`90`) ✅ (2026-03-18 → initially `30`; later raised to `90` after continued CPU-only timeouts)
- [x] `_call_model()` routes to Ollama when `web_access=False` and model is `LOCAL_MODEL` ✅ (confirmed via direct API call, HTTP 200)
- [x] `scripts/observability_loop.py --once` completes with no errors and appends a `SYSTEM_THOUGHTS` row to the Logs tab ✅ (2026-03-18 — 49 rows sampled, `ollama/llama3`)
- [x] All 332 unit tests still passing after Ollama integration ✅ (2026-03-18)
- [x] Knowledge Atlas Google Doc created in Drive and `docs.knowledge_atlas_doc_id` set in `settings.yaml` ✅ (see §17)

> ⚠️ **Warning — OLLAMA_HOST secret has trailing `\r\n`:** `get_secret('OLLAMA_HOST', ...)` returns `'http://localhost:11434\r\n'`. Fixed in `agents/__init__.py` with `.strip().rstrip("/")` on the host value. If symptoms reappear (httpx raises `Invalid non-printable ASCII character in URL, '\r'`), verify the fix is in place or update the secret via `echo -n 'http://localhost:11434' | gcloud secrets versions add OLLAMA_HOST --data-file=- --project=...`.

> ⚠️ **Warning — Windows charmap blocks Unicode in model responses:** On Windows, `sys.stdout` defaults to cp1252. If a model response contains non-ASCII characters (e.g. `→`, `—`), any `print()` of that text raises `UnicodeEncodeError`. Fix: add the following guard at script startup (after the `sys` import). All scripts that print model output or Unicode symbols must include this.
>
> ```python
> if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
>     sys.stdout.reconfigure(encoding="utf-8", errors="replace")
> ```
>
> Additionally, all `open()` calls in scripts must pass `encoding="utf-8"` explicitly. The pytest suite injects `PYTHONUTF8=1` automatically via `pyproject.toml` (`pytest-env` plugin). For interactive terminal sessions add `$env:PYTHONUTF8 = "1"` to your PowerShell profile.

---

## 16. Reference Index

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
| Knowledge Atlas (Memory Mirror) | `GAOS-Tools-Spec.md` §18 · §17 (this doc) |
| Nexus-Prime construction requirements | `GAOS-Nexus-Prime-Spec.md` |
| Phase 3 exit criteria | §18 (this doc) |
| Phase 4 exit criteria + bootstrap runbook | §19–20 (this doc) |

---

## 17. Knowledge Atlas Google Doc (Memory Mirror)

The **Knowledge Atlas** is the human-readable glass-box view of every approved `MemoryEntry` in Vertex AI Memory Bank. Each time Nexus-Prime auto-promotes a knowledge candidate, `tools.memory_mirror.sync_to_atlas()` appends a structured text block to this doc. When an entry supersedes an earlier one, a `⛔ SUPERSEDED` audit marker is appended so the full retirement history is visible.

> **Why a Google Doc and not a Sheet?** The Atlas is designed for owner **reading**, not querying. A Google Doc is shareable, searchable with Cmd+F, and opens in a browser without any API setup. It is an audit trail, not a database.

### Setup (one-time, manual)

**Step 1 — Create the document:**

1. Open Google Drive in the account that owns the GAOS project.
2. Navigate to the `Knowledge/` root folder (the same Drive folder whose ID is in `settings.yaml → projects.default.drive_folder_id`).
3. Create a new Google Doc: **+ New → Google Docs → Blank document**.
4. Name it: **GAOS Knowledge Atlas — \<project_name\>** (e.g. `GAOS Knowledge Atlas — Morphic-G`).
5. Add a header line at the top of the document body:

```
GAOS Knowledge Atlas
Approved memory entries are appended automatically by Nexus-Prime.
---
```

**Step 2 — Copy the document ID:**

The document ID is the long string in the URL between `/d/` and `/edit`:

```
https://docs.google.com/document/d/<DOCUMENT_ID>/edit
```

**Step 3 — Paste the ID into `settings.yaml`:**

```yaml
docs:
  service_account_key: ""
  blueprints_folder_id: ""
  knowledge_atlas_doc_id: "1AbCdEfGhIjKlMnOpQrStUvWxYz"   # ← paste here
```

**Step 4 — Verify (optional but recommended):**

Run the following in a Python shell (with the venv active) to confirm the doc is accessible:

```powershell
python -c "
from config import get_settings; from tools.google_docs import read_document
s = get_settings()
print(read_document(s.docs.knowledge_atlas_doc_id, s.GCP_PROJECT_ID)[:200])
"
```

Expected output: the first 200 characters of the document body (the header you added in Step 1).

> ⚠️ **Do not leave `knowledge_atlas_doc_id` empty in production.** If it is empty, every knowledge auto-promotion logs a `WARNING` and the Atlas entry is skipped. The Vertex AI write still succeeds — but the Atlas will be incomplete. Set the ID before the first `KNOWLEDGE_CANDIDATE` message is processed.

### Verification Command

```powershell
# After pasting the doc ID into settings.yaml:
python -c "
from config import get_settings
s = get_settings()
print('Atlas doc ID:', s.docs.knowledge_atlas_doc_id or '[NOT SET]')
"
```

Expected: prints the document ID string (not `[NOT SET]`).

---

## 18. Phase 3 Exit Criteria Checklist

Phase 3 is complete — and Phase 4 (production validation) may begin — when **every item** below is checked:

- [x] `think` node implemented in Nexus-Prime LangGraph: `ESCALATION`, `EVOLUTION_REQUEST`, and `KNOWLEDGE_CANDIDATE` message types all route through `think` before proceeding to `diagnose` or `knowledge_review` ✅ (2026-03-19)
- [x] `MonologueFrame` Pydantic model defined in `models/__init__.py`; `think` node writes a structured frame to the `aos_logs.monologue_frames` BigQuery table on every invocation ✅ (2026-03-19)
- [x] Tactical mode override implemented in `think`: when `priority >= 4`, `response_mode` is forced to `"Tactical"` and the fact propagated into all subsequent node prompts ✅ (2026-03-19)
- [x] `vision_blueprint` node implemented: owner submits an image via `/chat` → DEEP_MODEL Gemini multimodal extracts vision text → blueprint Google Doc generated and link posted in the Chat thread ✅ (2026-03-20)
- [x] `handle_skill_request` node implemented: inbound path sends `send_skill_import_card()` + writes `Agent_Approvals` row + parks `proposal_id`; resolution path routes `Approved` → `SKILL_REQUEST` back to requesting agent, `Rejected` → `ALERT` ✅ (2026-03-18)
- [x] `iterate_plan` + `_run_compaction` implemented: constraint list auto-compacted via `DEEP_MODEL` when estimate exceeds 70 % of context window; compacted result replaces constraints in working memory ✅ (2026-03-18)
- [x] `handle_poll_comments` implemented: polls Google Docs for resolved comments authored by the owner; for each qualifying comment dispatches `KNOWLEDGE_CANDIDATE` to Nexus-Prime ✅ (2026-03-20)
- [x] `tools/memory_mirror.py` implemented: `sync_to_atlas()` appends each approved `MemoryEntry` to the Knowledge Atlas Google Doc; `⛔ SUPERSEDED` audit marker appended when `entry.supersedes` is set ✅ (2026-03-20)
- [x] Google Chat Interactive Hub complete:
  - `send_approval_card()` sends 3-section card (Context + optional Reasoning + Decision buttons) ✅
  - `_verify_chat_jwt()` verifies Google-signed JWT (`chat@system.gserviceaccount.com`) on every `/chat` request ✅
  - `/chat` `CARD_CLICKED` handler routes `"approve"/"reject"` → `APPROVAL_RESULT` and `"skill_approve"/"skill_reject"` → `SKILL_REQUEST` (resolved) to Nexus-Prime ✅
  - (2026-03-20)
  - ⚠️ **Google Chat delivery abandoned 2026-03-30** — the `/chat` endpoint itself is correct and deployed, but Google Chat never reliably delivered messages from mobile. Full post-mortem: `GAOS-Nexus-Prime-Spec.md §3.2 chat_respond`. `chat@system.gserviceaccount.com` has `roles/run.invoker` and `CLOUD_RUN_URL` is set (revision `00057`). Pivoting to Gmail polling.
- ❌ **[ABANDONED]** Approval Gate Chat-path validated end-to-end: Chat card button tap → `APPROVAL_RESULT` published to Pub/Sub — blocked by Chat delivery failure. Will be re-evaluated with Gmail.
- [x] `_call_model()` extended with `image_bytes: bytes | None = None`; multimodal content sent as `[Part.from_bytes(...), Part.from_text(...)]` for Gemini; Ollama + `image_bytes` logs `WARNING` and strips bytes ✅ (2026-03-20)
- [x] OpenTofu IaC blueprint (`infra/main.tf`) and push-to-deploy CI/CD pipeline (`.github/workflows/deploy.yml`) committed to repo ✅ (2026-03-20)
- [x] WIF (Workload Identity Federation) replaces long-lived `GCP_SA_KEY` in CI/CD; `id-token: write` permission set; `attribute.repository` condition scopes access to this repo only ✅ (2026-03-20)
- [x] All 408 unit tests passing — zero regressions from all Phase 3 additions ✅ (2026-03-20)
- [x] **[Requires GCP]** One-time OpenTofu bootstrap: `morphic-gaos-tfstate` GCS bucket created, `cloud-run-source-deploy` Artifact Registry repo created, `deployer-sa` created with required IAM bindings, WIF pool + OIDC provider created, `WIF_PROVIDER` and `WIF_SERVICE_ACCOUNT` GitHub Secrets set → first `tofu plan` + `tofu apply` deploys all 7 Cloud Run services ✅ (2026-03-21 — all confirmed in §19 4a)
- ❌ **[ABANDONED — see §14]** Approval Gate Chat-path validated end-to-end: Chat card button tap → `APPROVAL_RESULT` published to Pub/Sub → Nexus-Prime resumes the parked task → `Agent_Approvals` row updated + audit row written to Logs tab. Blocked by Google Chat delivery failure (2026-03-30 post-mortem in `GAOS-Nexus-Prime-Spec.md §3.2`). The `/sync`-path Approval Gate E2E was validated instead — PASS 2026-04-03 (see §19 §4d).

---

## 21. Turnkey Deployment Roadmap

This section tracks the five gaps between the current state of the project and a
fully turnkey, one-command deployment experience (comparable to Nemoclaw or Abacus.ai
container deployments). Each gap is classified by whether it should be worked now
(active development) or at Phase 4 exit (pre-ship).

### Gap Assessment

| Gap | Description | Scripts/Files | Status |
|-----|-------------|---------------|--------|
| **Gap 3** | Secret provisioning is manual `gcloud` commands | `scripts/setup_secrets.py` | ✅ **Done** — 2026-03-23 |
| **Gap 4** | `setup_workspace.py` prints IDs but user copies them into `settings.yaml` by hand | `scripts/setup_workspace.py` | ✅ **Done** — 2026-03-23 |
| **Gap 1** | No CI/CD pipeline — build, push, and `tofu apply` are manual | `.github/workflows/` | ✅ Done (Phase 4) |
| **Gap 2** | Bootstrap requires manual pre-steps (GCS bucket, GCP APIs) | `scripts/bootstrap.py` | ✅ **Done** — 2026-03-26 |
| **Gap 5** | No pre-built image in a public registry | CI/CD output | Phase 4 exit (depends on Gap 1) |

### Target new-deployment sequence (all gaps closed)

```
1. gcloud auth application-default login
2. python scripts/setup_workspace.py          # Drive/Sheets → auto-writes config/settings.yaml
3. python scripts/setup_secrets.py            # interactively provisions all Secret Manager secrets
4. tofu init && tofu apply -var="image_tag=latest"   # all 7 agents live
```

### Gap 3 — `scripts/setup_secrets.py`

Auto-provisions all Secret Manager secrets and per-secret IAM bindings.

- Secrets requiring user input (GEMINI_API_KEY, GOOGLE_SEARCH_API_KEY, GOOGLE_SEARCH_CX): read
  via `getpass.getpass()` — never echoed to terminal or shell history.
- OLLAMA_HOST: prompted with default `http://localhost:11434`.
- WEBHOOK_HMAC_SECRET: auto-generated via `secrets.token_hex(32)`.
- WEBHOOK_URL: skipped — created by `scripts/setup_apps_script.py`.
- Idempotent: skips any secret that already has a version.
- IAM bindings: applied using the Secret Manager SDK `set_iam_policy` call.

Run:
```powershell
python scripts/setup_secrets.py --project morphic-gaos-prod
```

### Gap 4 — `setup_workspace.py` auto-write

`setup_workspace.py` now auto-writes `config/settings.yaml` from the template
after creating Drive/Sheets resources:
- Replaces `<your-spreadsheet-id>` with the live spreadsheet ID.
- Replaces `<your-drive-folder-id>` with the live Knowledge/ folder ID.
- Skips the write if `config/settings.yaml` already exists (prints a notice).
- Pass `--overwrite` to replace an existing `settings.yaml`.

The spec instructions in §4.1 no longer require a manual copy step — the script
handles it. The printout of IDs is retained for reference.

### Gap 2 — `scripts/bootstrap.py`

Automates the full Phase 4 Bootstrap Runbook (§20) from a blank GCP project to a
CI/CD-ready state. Run once before the first `git push origin master`.

Steps automated (all idempotent — safe to re-run):
- Enables 14 required GCP APIs via `gcloud services enable`
- Creates the GCS Terraform state bucket `morphic-gaos-tfstate` (versioning on)
- Creates the `cloud-run-source-deploy` Artifact Registry Docker repo
- Creates the `deployer-sa` CI/CD service account
- Applies all IAM bindings: `roles/run.admin` (project), `roles/artifactregistry.writer` (AR
  repo), `roles/storage.objectAdmin` (tfstate bucket), `roles/iam.serviceAccountUser` on all 7
  per-agent service accounts
- Creates the `github-actions` WIF pool and `github-oidc` OIDC provider, scoped to this repo
- Applies the `roles/iam.workloadIdentityUser` binding on `deployer-sa`
- Sets `WIF_PROVIDER`, `WIF_SERVICE_ACCOUNT`, and `SETTINGS_YAML` GitHub Secrets via `gh` CLI
  (skipped gracefully if `gh` is not installed)

Run:
```powershell
python scripts/bootstrap.py --project morphic-gaos-prod
```

See `scripts/bootstrap.py` docstring for full prerequisites and arg reference.

### Gap 5 — Phase 4 exit task

- **Gap 5 (pre-built image):** Publish `gaos-agent:latest` to a public or
  customer-accessible registry in the CI/CD apply job. Depends on Gap 1 (done).

---

## 20. Infrastructure Provisioner

**Purpose:** Self-service GCP resource provisioning for Cloud Scheduler jobs, BigQuery
staging tables, and Secret Manager secrets — without the owner ever running `gcloud`.

**Workflow:**
```
CLI (plan)  →  Chat card (Approve/Reject)  →  Nexus-Prime (apply)
              ↓                                      ↓
         ApprovalProposal row               health check → rollback if failed
         in Agent_Approvals                  → result Chat card
```

**Entry points:**
- `scripts/provision_infra.py` — CLI plan phase. Run locally or from CI.
- `POST /infra-provision` on nexus-prime — triggers `handle_infra_plan()`.
- CARD_CLICKED `infra_approve`/`infra_reject` → `handle_infra_provision()` graph node.

**Core module:** `tools/infra_provision.py`

| Export | Purpose |
|--------|--------|
| `DESIRED_SCHEDULER_JOBS` | Authoritative list of Cloud Scheduler jobs |
| `DESIRED_BQ_TABLES` | Authoritative list of BigQuery staging tables |
| `DESIRED_SECRETS` | Authoritative list of Secret Manager secrets |
| `build_manifest()` | Diff desired vs actual GCP state → `InfraManifest` |
| `apply_manifest()` | Apply actionable entries in safe order (secrets → BQ → scheduler) |
| `rollback_manifest()` | Undo applied entries; BQ tables are **never** dropped |
| `run_health_checks()` | Targeted checks on changed resources only |

**Manifest storage:** `InfraManifest.to_json()` is stored in `ApprovalProposal.proposed_code`
in the `Agent_Approvals` sheet — no new GCP resources required.

> ⚠️ **Warning — manifest stored in proposed_code column:** `handle_infra_provision()` reads the manifest JSON from `ApprovalProposal.proposed_code` (column H of `Agent_Approvals`). If that cell is manually edited after the card is sent, the SHA-256 hash will mismatch and the apply will be rejected. Never edit the JSON in that column directly.

**Safe apply order:** secrets (additive) → BQ tables (`CREATE TABLE IF NOT EXISTS`) → schedulers (upsert).

**Rollback hard rules:**
- BigQuery tables: **never dropped**. Notes include a manual `bq rm` command.
- Secrets: only deleted if zero versions exist.
- Scheduler CREATE → delete; Scheduler UPDATE → re-patch to prior schedule from `ChangeEntry.actual`.

**To trigger a plan:**
```powershell
.venv\Scripts\python.exe scripts\provision_infra.py --project morphic-gaos-prod --space spaces/<id>
# --dry-run prints diff without sending card
```

---

## 22. Post-Launch Optimization Queue

Deferred improvements that require real production traffic data before
implementation. **Do not start any item below until the data gate is met.**
All three items depend on §22.1 (CPMA panel) being live first.

### 22.1 CPMA Panel — Prerequisite for Everything Else

**What:** A BigQuery view + Grafana panel computing Cost Per Meaningful Action
(`SUM(cost_usd) / COUNT(*)`) grouped by `(task_type, task_subtype)` and
`project_id`. Uses the existing `aos_logs.task_outcomes` table.

**Data gate:** ≥ 50 completed rows per `message_type` in `task_outcomes`.
Run this query to check readiness:

```sql
SELECT message_type, COUNT(*) AS n
FROM aos_logs.task_outcomes
GROUP BY 1
ORDER BY 2 DESC
```

Proceed when every message type you care about is ≥ 50 and the p95 cost has
been stable (< 20% week-over-week change) for two consecutive weeks. Expected
timeline: 2 weeks post-launch with 3+ active projects.

**Implementation notes:**
- Partition the view with `WHERE log_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)` — Grafana must never scan the full unbounded table.
- Add a `cost_estimated: bool` column to `task_outcomes` before launch; set it `True` whenever the pricing lookup in `_call_model` falls back to a default. CPMA panels must filter or flag estimated rows.
- Group by `(task_type, task_subtype)` not just `task_type` — `task_type` maps to `message_type.value` (a protocol concept). `task_subtype` carries the business-action granularity needed for a meaningful CPMA.

---

### 22.2 Per-Task Spend Guard

**What:** A hard budget check before each `_call_model` call in
`agents/nexus_prime/orchestrator.py` (and any other orchestrator that
multi-steps LLM calls). Raises a typed exception when accumulated
`cost_usd` for the current task exceeds a per-message-type threshold,
then calls `record()` before exiting to guarantee a `task_outcomes` row
with `status="budget_exceeded"`.

**Data gate:** CPMA panel live (§22.1) + at least 5 observed runs of the
most expensive message type (`EVOLUTION_REQUEST` with full Write-Test-Refine
cycles) under real production load. Set thresholds at 3× the observed p95
per message type. Do not guess thresholds — a flat threshold fires constantly
on expensive tasks and never on cheap ones.

**Implementation notes:**
- Track spend at the `_call_model` wrapper level, not in `think()` state. The in-process counter must increment regardless of call success or failure — a zero-cost error return must not blind the guard.
- `settings.budget.per_task_usd` must be a `dict[str, float]` keyed by `MessageType.value` with a `"default"` fallback, matching the existing pattern of `settings.memory.max_active_entries`.
- On guard trigger: call `record(state)` before raising so the task completes with a terminal row. Never leave the agent in a state that appears permanently stuck to monitoring.

---

### 22.3 Anthropic Prompt Caching

**What:** Structure the `think()` system prompt as two explicit blocks —
`[STATIC: identity + rules]` and `[DYNAMIC: context trio]` — and mark the
static block with `cache_control` for the 90% cache-read discount on
repeated Claude calls (5-minute TTL, refreshed on hit). Write premium is
25% on the first call; subsequent reads cost $0.30/1M vs $3.00/1M standard.

**Data gate:** Average daily Anthropic-model calls > 200 (≈ 1 per 7 minutes,
creating enough 5-minute windows with 2+ calls to make the math work). Check:

```sql
SELECT COUNT(*) AS daily_anthropic_calls
FROM aos_logs.task_outcomes
WHERE log_date = CURRENT_DATE()
  AND model_alias LIKE '%claude%'  -- or however model alias is logged
```

Also gate on static prompt token count: measure with `tiktoken` before any
implementation. If the static block is under ~2,000 tokens, the 25% write
surcharge exceeds the 90% read discount at GAOS's call volume — skip caching
entirely until volume justifies it.

**Implementation notes:**
- Prompt architecture change must precede any code change: static block first, dynamic Context Trio second. This separation is a prerequisite regardless of caching.
- Add `_supports_prompt_caching(model_alias: str) -> bool` helper — returns `True` only for aliases resolving to Anthropic models. Gate `cache_control` injection on this check; Gemini and Ollama calls are unaffected.
- The 5-minute TTL covers a single `think()` call comfortably. Multi-node tasks with inter-node gaps > 4 minutes will miss the cache on subsequent calls — accept this and note that caching only helps `think()` (the highest-cost call), not the full OODA chain.
