# GAOS Onboarding Specification

**Morphic-G AOS** — New Deployer Setup Guide, Interactive Onboarding Script, and End-User Onboarding via Steward

> This document covers two distinct onboarding scenarios:
> - **§1–§4 — Deployer Onboarding:** A first-time operator standing up the AOS for the first time. Includes the complete service sign-up sequence, an interactive onboarding script (`tools/onboarding.py`), and a readiness checklist.
> - **§5 — End-User Onboarding (Steward Agent):** Adding a new employee or stakeholder to an already-running AOS instance. This is an ongoing operational task owned by the Steward agent.
>
> **Relationship to other documents:** `GAOS-Deploy-Spec.md` is the low-level infrastructure provisioning reference. This document is the *human-facing guide* that wraps it — the onboarding script automates the `GAOS-Deploy-Spec.md` steps wherever possible and validates the results. Complete this document first, then use the Deploy Spec for any steps the script cannot handle automatically.

---

## 1. Service Sign-Up Sequence

Complete these registrations in order. Later steps depend on earlier ones. The total time for account creation (excluding waiting periods) is approximately **25 minutes** for a new operator with nothing set up, or **5 minutes** for someone who already has a Google account and GitHub.

Each service lists exactly what you need to collect — these values feed directly into the onboarding script in §3.

---

### Step 1 — Google Account (5 min)

**What it is:** The identity that owns all GCP resources and the Google Sheet. This is the master account for the entire AOS.

**Sign up:** [accounts.google.com](https://accounts.google.com)

**Recommendation:** Use a **dedicated Google Workspace or Gmail account** for the AOS — not your personal account. This isolates the AOS data from your personal Google Drive, limits blast radius if credentials are compromised, and makes it easier to hand off or audit.

**What to collect:**
```
OWNER_EMAIL=your-aos-account@gmail.com
```

**Cost:** Free (Gmail) or included in Google Workspace subscription.

---

### Step 2 — Google Cloud Platform Project (10 min)

**What it is:** The GCP project is the billing and IAM boundary for every cloud service the AOS uses. Everything in the system — Pub/Sub, BigQuery, Secret Manager, Cloud Run, Vertex AI — lives inside this project.

**Sign up / sign in:** [console.cloud.google.com](https://console.cloud.google.com)

**Steps:**
1. Log in with the account from Step 1.
2. Click the project dropdown → **New Project**.
3. Project name: `Morphic GAOS` (display name)
4. Project ID: `morphic-gaos-prod` (or any globally unique ID — you cannot change this later)
5. Click **Create**.
6. Enable billing: **Billing → Link a billing account**. A credit card is required even though estimated monthly cost is ≈ $0.55. GCP will not charge you until you exceed the free tier.
7. Navigate to **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID**
   - Application type: **Desktop app**
   - Download the JSON file as `oauth-client.json` (stored locally, never committed)

**Billing alert (strongly recommended):** Set a budget alert at $10/month so you're notified if costs spike unexpectedly:
- **Billing → Budgets & Alerts → Create Budget**
- Amount: $10
- Alert thresholds: 50%, 90%, 100%

**What to collect:**
```
GCP_PROJECT_ID=morphic-gaos-prod
GCP_REGION=us-central1          # or europe-west1 for EU data residency
BILLING_ACCOUNT_ID=             # from: gcloud billing accounts list
```

**Cost:** Free to create; pay-as-you-go billing. See `GAOS-Manager-Spec.md §9.4` for the ≈ $0.55/month estimate.

---

### Step 3 — Gemini API Key (5 min)

**What it is:** The API key used to call Gemini models (`FAST_MODEL` and `DEEP_MODEL`). This is the most commonly misconfigured credential in the stack — the source matters.

> **Critical:** Get this key from **console.cloud.google.com → APIs & Services → Credentials** inside YOUR GCP project. Do **not** use an API key from `aistudio.google.com`. AI Studio keys live in Google's shared project and hit a `limit: 0` quota ceiling even when your billing account has credits — see `GAOS-Deploy-Spec.md §3.1`.

**Steps:**
1. In your GCP project: **APIs & Services → Enabled APIs → Generative Language API** — enable it if not already enabled.
2. **APIs & Services → Credentials → Create Credentials → API Key**
3. Click **Restrict Key**:
   - API restrictions: **Generative Language API** only
   - Application restrictions: **None** (for local dev; set to IP restriction in production)
4. Copy the key value — you will only see it once at creation.

**What to collect:**
```
GEMINI_API_KEY=AIza...
```

**Cost:** Billed per token. See `GAOS-Manager-Spec.md §9.4` for estimates — typically ~$1.10/month at normal AOS load (Flash ~$0.10 + Pro ~$1.00 including think node and diagnostics).

---

### Step 4 — Ollama (Local LLM, 5 min install + model download time)

**What it is:** The local inference engine for `LOCAL_MODEL`. Runs entirely on your machine — no API calls, no token charges, complete privacy for any task routed to it.

**Download:** [ollama.com/download](https://ollama.com/download) — select Windows.

**Steps:**
1. Run the installer.
2. Open PowerShell and pull the recommended model:
   ```powershell
   ollama pull llama3.1          # 16GB RAM minimum
   # ollama pull mistral         # Alternative for GPU machines
   # ollama pull llama3.3:70b    # For Local-First privacy topology (48GB+ VRAM)
   ```
3. Register Ollama as a Windows Service (run PowerShell as Administrator):
   ```powershell
   nssm install OllamaService "C:\Users\$env:USERNAME\AppData\Local\Programs\Ollama\ollama.exe" "serve"
   nssm set OllamaService Start SERVICE_AUTO_START
   nssm set OllamaService AppRestartDelay 3000
   Start-Service OllamaService
   ```
4. Verify: `Invoke-RestMethod http://localhost:11434/api/tags` — returns JSON with the model listed.

> **NSSM (Non-Sucking Service Manager)** is the recommended tool for running Ollama as a Windows service. Install it from [nssm.cc](https://nssm.cc/download) or via `winget install nssm`.

**What to collect:**
```
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3.1
```

**Cost:** Free. Electricity only.

---

### Step 5 — GitHub Account (2 min if not already set up)

**What it is:** Source control for the AOS codebase. The repo `EgoNoBueno/Morphic-GAOS-Manager` is already created.

**Sign up:** [github.com](https://github.com) — if you don't already have an account.

**Steps (local setup):**
```powershell
gh auth login   # Authenticate GitHub CLI
git clone https://github.com/EgoNoBueno/Morphic-GAOS-Manager.git
cd Morphic-GAOS-Manager
```

**Cost:** Free for public repos; free tier includes private repos.

---

### Step 6 — Google Cloud CLI + Python Environment (5 min)

**Install tools:**
```powershell
# Google Cloud CLI
# Download: https://cloud.google.com/sdk/docs/install (Windows installer)
# After install, run:
gcloud init
gcloud auth application-default login `
  --client-id-file=oauth-client.json `
  --scopes="https://www.googleapis.com/auth/spreadsheets,https://www.googleapis.com/auth/drive.file,https://www.googleapis.com/auth/cloud-platform"

# uv Python package manager
# Install: https://docs.astral.sh/uv/getting-started/installation/
# Or via PowerShell:
irm https://astral.sh/uv/install.ps1 | iex

# Bootstrap Python environment
uv venv
uv pip install google-cloud-secret-manager google-cloud-pubsub gspread pydantic `
               "google-adk>=1.0.0" langgraph google-cloud-bigquery google-cloud-logging `
               google-cloud-aiplatform "google-genai>=1.0.0"
```

---

## 2. What to Have Ready Before Running the Onboarding Script

The onboarding script (`tools/onboarding.py`) automates most of the `GAOS-Deploy-Spec.md` steps. Before running it, have these values ready:

| Value | Source | Example |
|-------|--------|---------|
| `OWNER_EMAIL` | Your Google account | `you@gmail.com` |
| `GCP_PROJECT_ID` | Created in Step 2 | `morphic-gaos-prod` |
| `GCP_REGION` | Step 2 — US or EU | `us-central1` |
| `GEMINI_API_KEY` | Created in Step 3 | `AIza...` |
| `OLLAMA_HOST` | Step 4 | `http://localhost:11434` |
| `OLLAMA_MODEL` | Step 4 | `llama3.1` |
| `oauth-client.json` path | Downloaded in Step 2 | `C:\path\to\oauth-client.json` |
| `PROJECT_NAME` | Your business or project name | `Acme Retail` |
| Monthly budget ceiling (USD) | Your decision | `5.00` |
| Privacy topology | See `GAOS-Privacy-Spec.md §5` | `standard` / `local-first` |

---

## 3. Onboarding Script — `tools/onboarding.py`

> ⚠️ **Status — Planned, Not Yet Implemented:** `tools/onboarding.py` is the target design for a full interactive CLI wizard. It does not exist yet. For now, use `scripts/setup_workspace.py` for Drive/Sheets provisioning (Step 3 of the manual sequence) and complete the remaining steps in `GAOS-Deploy-Spec.md` manually. This section documents the intended implementation.

The onboarding script is an interactive CLI wizard that:
1. Checks that all required tools are installed and reachable
2. Collects configuration values interactively
3. Runs all GCP provisioning steps from `GAOS-Deploy-Spec.md §1–§12`
4. Populates `config/settings.yaml`
5. Stores all secrets in Secret Manager
6. Runs the smoke tests from `GAOS-Deploy-Spec.md §13`
7. Produces a readiness report

```python
"""
tools/onboarding.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Interactive onboarding wizard for Morphic-G AOS.
Run once to provision all GCP resources and generate settings.yaml.

Usage:
    python tools/onboarding.py [--resume] [--dry-run]

Options:
    --resume    Skip steps already marked complete in .onboarding_state.json
    --dry-run   Print all commands without executing them
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from datetime import datetime


# ── State persistence ─────────────────────────────────────────────────────────
STATE_FILE = Path(".onboarding_state.json")

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"completed_steps": [], "config": {}}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))

def mark_done(state: dict, step: str):
    if step not in state["completed_steps"]:
        state["completed_steps"].append(step)
    save_state(state)

def is_done(state: dict, step: str) -> bool:
    return step in state["completed_steps"]


# ── Console helpers ───────────────────────────────────────────────────────────
def header(text: str):
    width = 60
    print(f"\n{'━' * width}")
    print(f"  {text}")
    print(f"{'━' * width}")

def ok(text: str):      print(f"  ✓  {text}")
def warn(text: str):    print(f"  ⚠  {text}")
def err(text: str):     print(f"  ✗  {text}")
def info(text: str):    print(f"     {text}")

def ask(prompt: str, default: str = "") -> str:
    display = f" [{default}]" if default else ""
    value = input(f"  → {prompt}{display}: ").strip()
    return value if value else default

def ask_secret(prompt: str) -> str:
    """Prompt for a secret value — input not echoed."""
    import getpass
    return getpass.getpass(f"  → {prompt} (hidden): ").strip()

def confirm(prompt: str) -> bool:
    answer = input(f"  → {prompt} [Y/n]: ").strip().lower()
    return answer in ("", "y", "yes")


# ── Tool availability checks ──────────────────────────────────────────────────

REQUIRED_TOOLS = {
    "gcloud":  "Google Cloud CLI — https://cloud.google.com/sdk/docs/install",
    "git":     "Git — https://git-scm.com/downloads/win",
    "python":  "Python 3.11+ — https://python.org/downloads",
    "uv":      "uv package manager — https://docs.astral.sh/uv",
    "ollama":  "Ollama — https://ollama.com/download",
}

def check_prerequisites(state: dict) -> bool:
    header("STEP 1 — Prerequisite Check")
    if is_done(state, "prerequisites"):
        ok("Prerequisites already verified — skipping")
        return True

    all_ok = True
    for tool, install_url in REQUIRED_TOOLS.items():
        if shutil.which(tool):
            ok(f"{tool} found")
        else:
            err(f"{tool} not found — install from: {install_url}")
            all_ok = False

    # Check Ollama is actually running
    try:
        import urllib.request
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2)
        ok("Ollama service reachable at http://localhost:11434")
    except Exception:
        warn("Ollama installed but not running. Start it with: ollama serve")
        warn("Or register as Windows service — see GAOS-Onboarding-Spec.md §1 Step 4")
        all_ok = False

    if all_ok:
        mark_done(state, "prerequisites")
    return all_ok


# ── Configuration collection ──────────────────────────────────────────────────

def collect_config(state: dict) -> dict:
    header("STEP 2 — Configuration")
    if is_done(state, "config_collected"):
        ok("Configuration already collected — skipping")
        return state["config"]

    info("Enter values for your deployment. Press Enter to accept defaults.")
    print()

    cfg = {}
    cfg["project_id"]        = ask("GCP Project ID", "morphic-gaos-prod")
    cfg["project_name"]      = ask("Human-readable project name", "Morphic GAOS")
    cfg["region"]            = ask("GCP Region (us-central1 or europe-west1 for GDPR)", "us-central1")
    cfg["owner_email"]       = ask("Owner Google account email")
    cfg["budget_usd"]        = ask("Monthly budget ceiling (USD)", "5.00")
    cfg["ollama_host"]       = ask("Ollama host", "http://localhost:11434")
    cfg["ollama_model"]      = ask("Ollama model", "llama3.1")

    print()
    info("Privacy topology options (see GAOS-Privacy-Spec.md):")
    info("  standard    — Gemini cloud LLM for complex reasoning (default, ≈$0.55/month)")
    info("  local-first — All LLM inference on local Ollama (requires 48GB+ VRAM)")
    cfg["privacy_topology"]  = ask("Privacy topology", "standard")

    # Secrets — not stored in settings.yaml, go to Secret Manager
    print()
    info("The following values will be stored in Secret Manager — not in any file.")
    cfg["gemini_api_key"]    = ask_secret("Gemini API Key (from GCP Console → APIs & Services → Credentials)")

    # HMAC secret — generate automatically
    import secrets as _secrets
    cfg["webhook_hmac"]      = _secrets.token_hex(32)
    ok(f"WEBHOOK_HMAC_SECRET generated automatically")

    # Model aliases — stored in settings.yaml; set here so smoke tests can reference them
    local_model = f"ollama/{cfg['ollama_model']}"
    if cfg["privacy_topology"] == "local-first":
        cfg["fast_model"] = local_model
        cfg["deep_model"] = local_model
    else:
        cfg["fast_model"] = "gemini-2.5-flash"
        cfg["deep_model"] = "gemini-2.5-pro"

    state["config"] = cfg
    mark_done(state, "config_collected")
    save_state(state)
    return cfg


# ── GCP provisioning ──────────────────────────────────────────────────────────

def run(cmd: str, dry_run: bool = False) -> tuple[int, str]:
    """Run a shell command. Returns (exit_code, output)."""
    if dry_run:
        print(f"     [DRY RUN] {cmd}")
        return 0, ""
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


def provision_gcp(cfg: dict, state: dict, dry_run: bool):
    header("STEP 3 — GCP Setup")
    if is_done(state, "gcp_provisioned"):
        ok("GCP already provisioned — skipping")
        return

    project = cfg["project_id"]
    region  = cfg["region"]

    info("Enabling required APIs (this takes ~60 seconds)...")
    apis = " ".join([
        "sheets.googleapis.com", "drive.googleapis.com",
        "pubsub.googleapis.com", "secretmanager.googleapis.com",
        "run.googleapis.com", "cloudscheduler.googleapis.com",
        "bigquery.googleapis.com", "logging.googleapis.com",
        "monitoring.googleapis.com", "aiplatform.googleapis.com",
        "cloudresourcemanager.googleapis.com",
        "gmail.googleapis.com",          # Gmail push watch + inbox polling
        "chat.googleapis.com",           # Google Chat bot
        "docs.googleapis.com",           # Google Docs (Blueprint Factory)
        "discoveryengine.googleapis.com", # Vertex AI Search
        "customsearch.googleapis.com",   # Google Custom Search (Scout)
    ])
    code, out = run(f"gcloud services enable {apis} --project={project}", dry_run)
    if code != 0:
        err(f"API enable failed: {out}")
        sys.exit(1)
    ok("APIs enabled")

    info("Creating service accounts...")
    agents = ["nexus-prime", "ledger", "beacon", "pursuit", "foreman", "steward", "scout", "apps-script"]
    for agent in agents:
        run(f'gcloud iam service-accounts create {agent}-sa '
            f'--display-name="{agent} AOS Agent" --project={project}', dry_run)
    ok("Service accounts created")

    mark_done(state, "gcp_provisioned")
    save_state(state)


def provision_secrets(cfg: dict, state: dict, dry_run: bool):
    header("STEP 4 — Secret Manager")
    if is_done(state, "secrets_provisioned"):
        ok("Secrets already provisioned — skipping")
        return

    project = cfg["project_id"]

    secrets = {
        "GEMINI_API_KEY":          cfg["gemini_api_key"],
        "OLLAMA_HOST":             cfg["ollama_host"],
        "WEBHOOK_HMAC_SECRET":     cfg["webhook_hmac"],
    }

    for name, value in secrets.items():
        # Create secret
        run(f'gcloud secrets create {name} --project={project}', dry_run)
        # Add version — pipe value via stdin to avoid shell history exposure
        if not dry_run:
            proc = subprocess.run(
                f'gcloud secrets versions add {name} --data-file=- --project={project}',
                shell=True, input=value, capture_output=True, text=True
            )
            if proc.returncode != 0:
                err(f"Failed to set secret {name}: {proc.stderr}")
            else:
                ok(f"Secret stored: {name}")
        else:
            print(f"     [DRY RUN] echo -n '<value>' | gcloud secrets versions add {name} ...")


    mark_done(state, "secrets_provisioned")
    save_state(state)


def write_settings_yaml(cfg: dict, state: dict):
    header("STEP 5 — settings.yaml")
    if is_done(state, "settings_yaml_written"):
        ok("settings.yaml already written — skipping")
        return

    fast_model  = cfg["fast_model"]   # Set in collect_config() from privacy_topology
    deep_model  = cfg["deep_model"]
    local_model = f"ollama/{cfg['ollama_model']}"

    if cfg["privacy_topology"] == "local-first":
        local_fallback = "null  # Local-First: no cloud fallback"
    else:
        local_fallback = fast_model

    settings_content = f"""# config/settings.yaml
# Auto-generated by tools/onboarding.py on {datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}
# Safe to commit — all secrets are in Secret Manager, not here.

gcp:
  project_id: "{cfg['project_id']}"
  region: "{cfg['region']}"

sheet:
  workbook_id: ""    # Template sheet cloned when provisioning new projects (GAOS-Deploy-Spec.md §4)

projects:
  default:
    sheet_id: ""            # Runtime workbook for the default project — set to the same ID as sheet.workbook_id in single-project deployments
    drive_folder_id: ""     # Fill in after creating Drive folder (GAOS-Deploy-Spec.md §6)

models:
  LOCAL_MODEL: "{local_model}"
  LOCAL_MODEL_FALLBACK: "{local_fallback}"
  LOCAL_MODEL_TIMEOUT_SECONDS: 90  # Seconds to wait for Ollama before raising RuntimeError; 90 s accommodates CPU-only inference on low-end hardware
  FAST_MODEL: "{fast_model}"
  DEEP_MODEL: "{deep_model}"

memory_bank:
  region: "us-central1"    # Change to us-west1 if corpora were created there
  corpora: {{}}              # Populated by scripts/_create_corpora.py

pubsub:
  max_hop_count: 5  # A2A loop prevention — drop messages with hop_count >= this value (Rule 25.4)
  all_topics:
    - agent.nexus-prime.events
    - agent.ledger.events
    - agent.beacon.events
    - agent.pursuit.events
    - agent.foreman.events
    - agent.steward.events
    - agent.scout.events
    - agent.approvals.events

apps_script:
  timezone: America/Los_Angeles
  script_id: ""           # Fill in after Apps Script deploy (GAOS-Deploy-Spec.md §4.4)
  deployment_id: ""       # Fill in after Apps Script deploy
  # webhook_url is intentionally absent — stored in Secret Manager as WEBHOOK_URL

chat:
  owner_space: ""         # spaces/<id> — owner DM for morning briefings
  service_account_key: "" # Optional path to SA key JSON; leave blank for ADC

docs:
  blueprints_folder_id: ""      # Drive folder ID for Blueprint Docs
  knowledge_atlas_doc_id: ""    # Pre-create in Drive; paste doc ID here (GAOS-Tools-Spec.md §18)
  service_account_key: ""       # Optional path to SA key JSON; leave blank for ADC

gmail:
  monitored_address: ""         # Inbox watched by Gmail push — fill in your address
  sender_address: ""            # "Send mail as" alias for outbound email
  alert_address: ""             # Must NOT equal monitored_address
  label_id: ""
  pubsub_topic: "projects/{cfg['project_id']}/topics/gmail-notifications"
  max_results: 50

outbound:
  max_emails_per_task: 3        # Hard cap per single task execution (Rule 26.2)
  max_publishes_per_task: 10    # Hard cap on Pub/Sub publishes per task (Rule 26.2)
  flood_window_minutes: 60      # Rolling window for flood detection (Rule 26.3)
  flood_threshold: 10           # Max emails in window before abort (Rule 26.3)

memory:
  max_boot_chars: 32000
  max_active_entries:
    nexus-prime: 200
    ledger: 200
    beacon: 150
    pursuit: 150
    foreman: 150
    steward: 100
    scout: 100

# Note: code_safety.allowed_imports is NOT configured here.
# The import allowlist is hardcoded in agents/__init__.py._ALLOWED_IMPORTS.
"""

    Path("config/settings.yaml").write_text(settings_content)
    ok("config/settings.yaml written")

> **Why two sheet fields?** `sheet.workbook_id` is the **template spreadsheet** — Nexus-Prime clones
> it when provisioning each new project (`_clone_project_sheet()`). `projects.<id>.sheet_id` is the
> **runtime workbook** that `tools/google_sheets.py` uses for every read/write operation. In a
> single-project deployment both fields hold the same spreadsheet ID; in multi-project setups the
> template remains pristine while each project's `sheet_id` points to its own clone.
>
> **Sync rule:** If you ever rotate or replace the spreadsheet, update **both** fields together.
> `setup_workspace.py` always prints both lines side-by-side to reinforce this.

    mark_done(state, "settings_yaml_written")
    save_state(state)


# ── Smoke tests ───────────────────────────────────────────────────────────────

def run_smoke_tests(cfg: dict, state: dict):
    header("STEP 6 — Smoke Tests")
    if is_done(state, "smoke_tests_passed"):
        ok("Smoke tests already passed — skipping")
        return

    results = {}

    # Test 1: gcloud auth
    code, out = run("gcloud auth application-default print-access-token")
    results["gcloud_auth"] = code == 0
    (ok if results["gcloud_auth"] else err)(f"ADC auth: {'OK' if results['gcloud_auth'] else 'FAILED — run: gcloud auth application-default login'}")

    # Test 2: Gemini API key reachable
    try:
        import google.genai as genai
        client = genai.Client(api_key=cfg["gemini_api_key"])
        resp = client.models.generate_content(
            model=cfg["fast_model"],  # Use configured model alias — never hardcode version strings
            contents="Reply with the single word: READY"
        )
        gemini_ok = "READY" in (resp.text or "")
        results["gemini_api"] = gemini_ok
        (ok if gemini_ok else err)(f"Gemini API key: {'OK' if gemini_ok else 'FAILED — check key source (must be from GCP Console, not AI Studio)'}")
    except Exception as e:
        results["gemini_api"] = False
        err(f"Gemini API: FAILED — {e}")

    # Test 3: Ollama reachable
    try:
        import urllib.request
        urllib.request.urlopen(f"{cfg['ollama_host']}/api/tags", timeout=2)
        results["ollama"] = True
        ok("Ollama: reachable")
    except Exception:
        results["ollama"] = False
        warn("Ollama: not reachable (non-blocking — FAST_MODEL fallback will activate)")

    # Test 4: Secret Manager
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{cfg['project_id']}/secrets/GEMINI_API_KEY/versions/latest"
        val = client.access_secret_version(request={"name": name})
        results["secret_manager"] = bool(val.payload.data)
        ok(f"Secret Manager: GEMINI_API_KEY readable")
    except Exception as e:
        results["secret_manager"] = False
        err(f"Secret Manager: FAILED — {e}")

    all_critical = results.get("gcloud_auth") and results.get("gemini_api") and results.get("secret_manager")
    if all_critical:
        mark_done(state, "smoke_tests_passed")
        save_state(state)

    return results


# ── Readiness report ──────────────────────────────────────────────────────────

def print_readiness_report(state: dict, smoke_results: dict):
    header("READINESS REPORT")

    manual_steps = [
        ("Create Google Sheet workbook and add tab structure",
         "GAOS-Deploy-Spec.md §4.1–§4.3"),
        ("Deploy Apps Script (doPost.gs, onChangeApproval.gs, syncSkillsToVertex.gs)",
         "GAOS-Deploy-Spec.md §4.4"),
        ("Fill in sheet.workbook_id in config/settings.yaml",
         "After §4.1"),
        ("Create Google Drive Knowledge/ folder and seed files",
         "GAOS-Deploy-Spec.md §6"),
        ("Fill in drive.knowledge_folder_id in config/settings.yaml",
         "After §6.1"),
        ("Create BigQuery tables with TTL partitioning",
         "GAOS-Deploy-Spec.md §7.2"),
        ("Create Vertex AI Memory Bank corpora (7 domains)",
         "GAOS-Deploy-Spec.md §12.1"),
        ("Build and deploy Cloud Run services for all 7 agents",
         "GAOS-Deploy-Spec.md §9"),
        ("Update Pub/Sub subscription push endpoints after Cloud Run deploy",
         "GAOS-Deploy-Spec.md §9.2"),
        ("Deploy Apps Script as Web App and store URL in Secret Manager",
         "GAOS-Deploy-Spec.md §4.7"),
        ("Set Cloud Logging retention to 7 days",
         "GAOS-Deploy-Spec.md §11"),
        ("Add yourself to Authorized Approvers tab in the Sheet",
         "GAOS-Deploy-Spec.md §4.3"),
        ("Create seed Knowledge/ files in Google Drive",
         "GAOS-Deploy-Spec.md §6.2"),
        ("Run full smoke tests from GAOS-Deploy-Spec.md §13",
         "GAOS-Deploy-Spec.md §13"),
    ]

    completed = state.get("completed_steps", [])
    print()
    ok(f"Automated steps completed: {len(completed)}")
    print()
    info("The following steps require manual action in Google Cloud Console / Sheets:")
    print()
    for i, (step, ref) in enumerate(manual_steps, 1):
        print(f"  {i:2}. {step}")
        print(f"       → {ref}")
    print()
    info("State saved to .onboarding_state.json")
    info("Run 'python tools/onboarding.py --resume' to continue after manual steps.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Morphic-G AOS Onboarding Wizard")
    parser.add_argument("--resume",  action="store_true", help="Skip already-completed steps")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running")
    args = parser.parse_args()

    print("\n  Morphic-G AOS — Onboarding Wizard")
    print(f"  {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    if args.dry_run:
        warn("DRY RUN mode — no changes will be made")

    state = load_state() if args.resume else {"completed_steps": [], "config": {}}

    if not check_prerequisites(state):
        err("Fix the above issues before continuing.")
        sys.exit(1)

    cfg = collect_config(state)
    provision_gcp(cfg, state, args.dry_run)
    provision_secrets(cfg, state, args.dry_run)
    write_settings_yaml(cfg, state)
    smoke_results = run_smoke_tests(cfg, state)
    print_readiness_report(state, smoke_results)


if __name__ == "__main__":
    main()
```

---

## 4. Operator Readiness Checklist

Use this checklist after running the onboarding script. Check each item before starting the first agent.

### Automated by the script ✓

> ⚠️ **`tools/onboarding.py` is not yet implemented.** All items below are manual until the script is built. Use `scripts/setup_workspace.py` for Drive/Sheets provisioning; complete the rest per `GAOS-Deploy-Spec.md`.

- [ ] All prerequisite tools installed and reachable
- [ ] GCP APIs enabled
- [ ] 8 service accounts created
- [ ] `GEMINI_API_KEY`, `OLLAMA_HOST`, `WEBHOOK_HMAC_SECRET` in Secret Manager
- [ ] `config/settings.yaml` populated

### Manual steps required
- [ ] Google Sheet workbook created, all tabs added (`GAOS-Deploy-Spec.md §4.1–§4.3`)
- [ ] `sheet.workbook_id` filled into `config/settings.yaml`
- [ ] Apps Script deployed (`doPost.gs`, `onChangeApproval.gs`, `syncSkillsToVertex.gs`)
- [ ] Your email added to `Authorized Approvers` tab (Tier 5)
- [ ] Apps Script Web App deployed and URL stored in Secret Manager + Script Properties
- [ ] Google Drive `Knowledge/` folder created with subfolder structure
- [ ] `drive.knowledge_folder_id` filled into `config/settings.yaml`
- [ ] Seed knowledge files created (policies/, procedures/, workflows/)
- [ ] BigQuery `aos_logs` dataset and 9 tables created (`GAOS-Deploy-Spec.md §7`): 6 Grafana staging tables via `scripts/create_staging_tables.py`, plus `task_outcomes`, `agent_checkpoints`, and `monologue_frames` via separate Python snippets in §7
- [ ] Vertex AI Memory Bank — 7 corpora created (global + 6 domains)
- [ ] All 7 agent Cloud Run services deployed
- [ ] Pub/Sub subscription push endpoints updated with Cloud Run URLs
- [ ] Cloud Scheduler jobs created (TTL sweep + nightly archive)
- [ ] Cloud Logging retention reduced to 7 days
- [ ] Full smoke tests from `GAOS-Deploy-Spec.md §13` all pass
- [ ] `Project Registry` tab populated with `default` project row
- [ ] First Nexus-Prime boot observed on the `Main Control Plane` tab

---

## 5. End-User Onboarding — Steward Agent Procedure

This section covers adding a **new employee or stakeholder** to an already-running AOS instance. This is an ongoing operational task owned by the Steward agent, not a one-time deploy step.

### 5.1 Trigger

End-user onboarding begins when a row appears in the `Steward_Onboarding_Queue` Sheet tab. This tab is populated either manually by the owner or automatically when Steward detects an onboarding event in connected HR systems.

**`Steward_Onboarding_Queue` headers:**
```
onboarding_id | full_name | role | email | department | start_date | access_tier | status | assigned_agent | notes
```

**`access_tier`** maps to the existing `Authorized Approvers` tier system:
| Tier | Role | Approval permissions |
|------|------|---------------------|
| 1 | Read-only viewer | None — dashboard view only |
| 2 | Contributor | Can trigger agent tasks; no approval authority |
| 3 | Manager | Can approve Priority 1–2 proposals |
| 4 | Senior Manager | Can approve Priority 1–3 proposals |
| 5 | Owner | Full approval authority |

### 5.2 Steward Onboarding Workflow

When Steward detects a new row in `Steward_Onboarding_Queue`:

```
[Detect new row] ──► [Validate fields] ──► [Provision access]
                             │
                    [Missing required field?]
                             │
                   [Publish ESCALATION to Nexus-Prime]

[Provision access]:
  1. Add email + tier to Authorized_Approvers tab (Nexus-Prime approval required)
  2. Share the project's Google Sheet workbook with new user (view or edit per tier)
  3. Send welcome email with dashboard URL and orientation checklist
  4. Set status = "Complete" in Steward_Onboarding_Queue
  5. Log to Logs tab with onboarding_id
```

**Rule:** Step 1 (adding to `Authorized_Approvers`) requires a Priority-3 Approval Gate proposal. Steward cannot promote a user to an approval tier without owner sign-off. Tier 1 and 2 (view/contribute access only) can be provisioned autonomously.

### 5.3 Welcome Email Template

Steward generates this email using `LOCAL_MODEL`, personalized with the user's name, role, and project name. It is submitted as an Approval Gate proposal before sending — communications policy requires owner review for any outbound email.

```
Subject: Welcome to the Morphic-G AOS — [Project Name]

Hi [Name],

You have been added to the [Project Name] AOS dashboard with [Access Tier] access.

Your dashboard: [Google Sheet URL]

What you can see:
[Tier-appropriate summary — e.g., "You can view all business unit tabs and the
Main Control Plane. You cannot modify agent configurations or approve proposals."]

To request a task from the AOS, add a row to the Task Queue tab with your request
in the "Instruction" column and set Status to "Pending".

Questions? Reply to this email — your request will be routed to the appropriate agent.

— Morphic-G AOS (Steward)
```

### 5.4 Offboarding

When a user's `active` field in `Authorized_Approvers` is set to `FALSE`:

1. Steward revokes Sheet sharing within one processing cycle
2. Any `Pending` approval proposals authored by that user are flagged for re-assignment
3. Steward logs the offboarding event to the `Logs` tab with timestamp
4. If the offboarded user had any Priority 4–5 approval authority, Steward publishes a Priority-2 INFO to Nexus-Prime so the owner can confirm their approval authority has been covered

---

## 6. Ongoing Credential Maintenance

API keys and service account credentials are not set-and-forget. Set a recurring reminder for each of the following:

| Item | Frequency | Action |
|------|-----------|--------|
| Gemini API key rotation | Every 90 days | Generate new key in GCP Console → update `GEMINI_API_KEY` in Secret Manager (`gcloud secrets versions add`) → old version auto-expires after 24h grace |
| Service account key audit | Every 90 days | `gcloud iam service-accounts keys list` — delete any keys older than 90 days |
| GCP IAM permissions audit | Every 6 months | Review roles in `GAOS-Deploy-Spec.md §2.2` — remove any permissions added ad hoc |
| Ollama model update | Monthly | `ollama pull llama3.1` — pulls latest version if available |
| `settings.yaml` model alias review | When Google releases new Gemini version | Follow the Model Versioning Policy in `GAOS-Manager-Spec.md §11.1` |
| Budget alert check | Monthly | Review GCP Billing → actual vs. budget |

### API Key Rotation — Zero-Downtime Procedure

Secret Manager supports multiple active versions. Use this sequence for zero-downtime rotation:

```bash
PROJECT=morphic-gaos-prod

# 1. Add new version (old version stays active)
echo -n "<new-api-key>" | gcloud secrets versions add GEMINI_API_KEY \
  --data-file=- --project=$PROJECT

# 2. Verify new version works (run smoke test)
python scripts/smoke_test_4.py   # verify Gemini API key works

# 3. Disable old version (does not delete — keeps it for audit)
OLD_VERSION=$(gcloud secrets versions list GEMINI_API_KEY \
  --project=$PROJECT --format="value(name)" | tail -1)
gcloud secrets versions disable $OLD_VERSION --secret=GEMINI_API_KEY --project=$PROJECT

# 4. Destroy old version after 7-day audit hold
# gcloud secrets versions destroy $OLD_VERSION --secret=GEMINI_API_KEY --project=$PROJECT
```

---

## Reference Index

| Topic | Document | Section |
|-------|----------|---------|
| Infrastructure provisioning (low-level) | `GAOS-Deploy-Spec.md` | §1–§13 |
| Privacy topology options | `GAOS-Privacy-Spec.md` | §4–§5 |
| Zero trust security policy (identity, gates, incidents) | `GAOS-Security-Policy.md` | All |
| Model alias definitions | `GAOS-Manager-Spec.md` | §11 |
| Approval Gate mechanics | `GAOS-Manager-Spec.md` | §14 |
| Steward identity file | `Docs/agents/steward.md` | All |
| Authorized Approvers tier definitions | `GAOS-Manager-Spec.md` | §14 |
| Secret Manager setup detail | `GAOS-Deploy-Spec.md` | §3 |
| Smoke tests | `GAOS-Deploy-Spec.md` | §13 |
