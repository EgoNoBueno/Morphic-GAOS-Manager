"""
scripts/setup_apps_script.py — Automated Apps Script deployment.

What this script does:
  1. Enables the Apps Script API in the GCP project (if not already enabled)
  2. Creates a bound Apps Script project on the spreadsheet
  3. Uploads all 5 .gs files from apps_script/
  4. Deploys the project as a Web App
  5. Opens the browser for the one-time OAuth consent (unavoidable)
  6. Stores WEBHOOK_URL in Secret Manager

After the one-time consent is complete, run this script again with --post-auth
to finalize: set Script Properties and install the onChange trigger.

Prerequisites:
  - ADC configured (§0.4)
  - .venv activated
  - config/settings.yaml populated (workbook_id)
  - Run from repo root: python scripts/setup_apps_script.py

Two-phase usage:
  Phase 1 (create + deploy):
    python scripts/setup_apps_script.py

  Phase 2 (after browser consent):
    python scripts/setup_apps_script.py --post-auth
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import google.auth
import yaml
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Config ────────────────────────────────────────────────────────────────────

PROJECT = "morphic-gaos-prod"
APPS_SCRIPT_DIR = Path(__file__).parent.parent / "apps_script"
SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"

# Order matters — helpers must be first so other files can call its functions
SCRIPT_FILES = [
    "helpers.gs",
    "doPost.gs",
    "onChangeApproval.gs",
    "syncSkillsToVertex.gs",
    "setup_protection.gs",
]

SCOPES = [
    "https://www.googleapis.com/auth/" + "script.projects",
    "https://www.googleapis.com/auth/" + "script.deployments",
    "https://www.googleapis.com/auth/" + "script.scriptapp",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/" + "drive",
    "https://www.googleapis.com/auth/cloud-platform",
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        sys.exit("config/settings.yaml not found — run setup_workspace.py first")
    with open(SETTINGS_PATH) as f:
        return yaml.safe_load(f)


def get_credentials():
    creds, _ = google.auth.default(scopes=SCOPES)
    if hasattr(creds, "refresh"):
        creds.refresh(Request())
    return creds


def enable_api(api: str) -> None:
    result = subprocess.run(
        ["gcloud", "services", "enable", api, "--project", PROJECT],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  Warning enabling {api}: {result.stderr.strip()}")
    else:
        print(f"  {api}: enabled")


def get_script_id_for_spreadsheet(script_service, spreadsheet_id: str) -> str | None:
    """Return the script ID if a bound project already exists."""
    try:
        resp = script_service.projects().list(
            scriptId=spreadsheet_id
        ).execute()
        projects = resp.get("projects", [])
        if projects:
            return projects[0]["scriptId"]
    except HttpError:
        pass
    return None


def create_bound_project(script_service, spreadsheet_id: str, title: str) -> str:
    """Create a new Apps Script project bound to the spreadsheet."""
    body = {
        "title": title,
        "parentId": spreadsheet_id,
    }
    resp = script_service.projects().create(body=body).execute()
    return resp["scriptId"]


def build_script_content() -> dict:
    """Read all .gs files and build the Apps Script content payload."""
    files = []
    for filename in SCRIPT_FILES:
        path = APPS_SCRIPT_DIR / filename
        if not path.exists():
            sys.exit(f"Missing script file: {path}")
        source = path.read_text(encoding="utf-8")
        files.append({
            "name": filename.replace(".gs", ""),
            "type": "SERVER_JS",
            "source": source,
        })
    # appsscript.json manifest — required for Web App deployment
    files.append({
        "name": "appsscript",
        "type": "JSON",
        "source": json.dumps({
            "timeZone": "America/Chicago",
            "dependencies": {},
            "webapp": {
                "executeAs": "USER_DEPLOYING",
                "access": "ANYONE_ANONYMOUS",
            },
            "exceptionLogging": "STACKDRIVER",
            "runtimeVersion": "V8",
            "oauthScopes": [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/" + "drive",
                "https://www.googleapis.com/auth/script.external_request",
                "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/cloud-platform",
            ],
        }, indent=2),
    })
    return {"files": files}


def deploy_web_app(script_service, script_id: str) -> tuple[str, str]:
    """Create a HEAD deployment and return (deployment_id, web_app_url)."""
    body = {
        "versionNumber": 0,  # HEAD
        "manifestFileName": "appsscript",
        "description": "Morphic-G AOS webhook + approval handler",
    }
    resp = script_service.projects().deployments().create(
        scriptId=script_id, body=body
    ).execute()
    deployment_id = resp["deploymentId"]
    web_app_url = (
        resp.get("entryPoints", [{}])[0]
        .get("webApp", {})
        .get("url", "")
    )
    return deployment_id, web_app_url


def store_secret(name: str, value: str) -> None:
    """Add a new version to an existing Secret Manager secret."""
    tmp = Path("tmp_secret_value.txt")
    tmp.write_text(value, encoding="utf-8")
    try:
        result = subprocess.run(
            ["gcloud", "secrets", "versions", "add", name,
             "--data-file", str(tmp), "--project", PROJECT],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            # Secret may not exist yet — create it first
            subprocess.run(
                ["gcloud", "secrets", "create", name,
                 "--project", PROJECT, "--replication-policy", "automatic"],
                capture_output=True, text=True
            )
            subprocess.run(
                ["gcloud", "secrets", "versions", "add", name,
                 "--data-file", str(tmp), "--project", PROJECT],
                check=True, capture_output=True, text=True
            )
        print(f"  Secret {name}: stored")
    finally:
        tmp.unlink(missing_ok=True)


def save_script_id(script_id: str) -> None:
    """Persist the script ID to settings.yaml for --post-auth phase."""
    with open(SETTINGS_PATH) as f:
        settings = yaml.safe_load(f)
    settings.setdefault("apps_script", {})["script_id"] = script_id
    with open(SETTINGS_PATH, "w") as f:
        yaml.dump(settings, f, default_flow_style=False, allow_unicode=True)


# ── Phase 1: create + upload + deploy ────────────────────────────────────────

def phase1() -> None:
    print("\n=== Phase 1: Create, upload, deploy ===\n")

    settings = load_settings()
    spreadsheet_id = settings["sheet"]["workbook_id"]

    print("Enabling APIs...")
    enable_api("script.googleapis.com")

    creds = get_credentials()
    script_service = build("script", "v1", credentials=creds)

    # Create or reuse bound project
    existing = get_script_id_for_spreadsheet(script_service, spreadsheet_id)
    if existing:
        script_id = existing
        print(f"Reusing existing script project: {script_id}")
    else:
        print("Creating bound Apps Script project...")
        script_id = create_bound_project(
            script_service, spreadsheet_id, "Morphic-G AOS — Control Plane"
        )
        print(f"Script project created: {script_id}")

    save_script_id(script_id)

    # Upload all .gs files
    print("Uploading script files...")
    content = build_script_content()
    script_service.projects().updateContent(
        scriptId=script_id, body=content
    ).execute()
    print(f"  Uploaded {len(SCRIPT_FILES)} files + appsscript.json manifest")

    # Deploy as Web App
    print("Creating Web App deployment...")
    try:
        deployment_id, web_app_url = deploy_web_app(script_service, script_id)
        print(f"  Deployment ID: {deployment_id}")
        if web_app_url:
            print(f"  Web App URL:   {web_app_url}")
            store_secret("WEBHOOK_URL", web_app_url)
            settings["apps_script"]["webhook_url"] = web_app_url
            settings["apps_script"]["deployment_id"] = deployment_id
            with open(SETTINGS_PATH, "w") as f:
                yaml.dump(settings, f, default_flow_style=False, allow_unicode=True)
        else:
            print("  ⚠  Web App URL not returned — script may need browser consent first")
            print("     Open this URL to authorize, then re-run with --post-auth:")
            print(f"     https://script.google.com/d/{script_id}/edit")
    except HttpError as e:
        if "UNAUTHENTICATED" in str(e) or "403" in str(e):
            print(f"\n  ⚠  Authorization required.")
            print(f"     Opening Apps Script editor in browser — click 'Allow'.")
            webbrowser.open(f"https://script.google.com/d/{script_id}/edit")
            print(f"\n     After authorizing, run:")
            print(f"       python scripts/setup_apps_script.py --post-auth")
            return
        raise

    print("\n✅ Phase 1 complete.")
    print("   Next: run --post-auth to set Script Properties and install trigger.")


# ── Phase 2: set properties + install trigger ─────────────────────────────────

def phase2() -> None:
    print("\n=== Phase 2: Script Properties + onChange trigger ===\n")

    settings = load_settings()
    script_id = settings.get("apps_script", {}).get("script_id")
    if not script_id:
        sys.exit("No script_id in settings.yaml — run Phase 1 first")

    # Fetch WEBHOOK_HMAC_SECRET from Secret Manager
    result = subprocess.run(
        ["gcloud", "secrets", "versions", "access", "latest",
         "--secret", "WEBHOOK_HMAC_SECRET", "--project", PROJECT],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        sys.exit("Could not read WEBHOOK_HMAC_SECRET from Secret Manager")
    hmac_secret = result.stdout.strip()

    webhook_url = settings.get("apps_script", {}).get("webhook_url", "")

    creds = get_credentials()
    script_service = build("script", "v1", credentials=creds)

    # Run setupProperties function via Apps Script API
    print("Setting Script Properties via Apps Script API...")
    props = {
        "WEBHOOK_HMAC_SECRET": hmac_secret,
        "VERTEX_AGENT_ENDPOINT": "",   # filled after Cloud Run deploy in §8
        "WEBHOOK_URL": webhook_url,
        "GCP_PROJECT": PROJECT,
    }
    run_body = {
        "function": "setupPropertiesFromApi_",
        "parameters": [props],
        "devMode": False,
    }
    try:
        resp = script_service.scripts().run(
            scriptId=script_id, body=run_body
        ).execute()
        if resp.get("error"):
            print(f"  ⚠  Script run error: {resp['error']}")
        else:
            print("  Script Properties set ✅")
    except HttpError as e:
        print(f"  ⚠  Could not run script remotely ({e}).")
        print("     Set these manually in Apps Script → Project Settings → Script Properties:")
        for k, v in props.items():
            print(f"     {k} = {v if k != 'WEBHOOK_HMAC_SECRET' else '(value from Secret Manager)'}")

    # Install onChange trigger by calling setupTrigger_ via API
    print("Installing onChange trigger...")
    trigger_body = {
        "function": "setupTrigger_",
        "parameters": [],
        "devMode": False,
    }
    try:
        resp = script_service.scripts().run(
            scriptId=script_id, body=trigger_body
        ).execute()
        if resp.get("error"):
            print(f"  ⚠  Trigger install error: {resp['error']}")
            print("     Install manually: Apps Script → Triggers → Add Trigger")
            print("     Function: onChangeApproval | Source: From spreadsheet | Event: On change")
        else:
            print("  onChange trigger installed ✅")
    except HttpError as e:
        print(f"  ⚠  Could not install trigger remotely ({e}).")
        print("     Install manually: Apps Script → Triggers → Add Trigger")
        print("     Function: onChangeApproval | Source: From spreadsheet | Event: On change")

    # Run setupProtections
    print("Running setupProtections (column/tab locks)...")
    prot_body = {
        "function": "setupProtections",
        "parameters": [],
        "devMode": False,
    }
    try:
        resp = script_service.scripts().run(
            scriptId=script_id, body=prot_body
        ).execute()
        if resp.get("error"):
            print(f"  ⚠  setupProtections error: {resp['error']}")
        else:
            print("  Column protections applied ✅")
    except HttpError as e:
        print(f"  ⚠  Could not run setupProtections remotely ({e}).")
        print("     Run manually: Apps Script editor → select setupProtections → Run")

    print("\n✅ Phase 2 complete.")
    print("   Remaining manual step: add owner row to Authorized Approvers tab (§4.3).")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deploy Apps Script project to the Morphic-G AOS spreadsheet"
    )
    parser.add_argument(
        "--post-auth", action="store_true",
        help="Phase 2: set Script Properties and install triggers (run after browser consent)"
    )
    args = parser.parse_args()

    if args.post_auth:
        phase2()
    else:
        phase1()


if __name__ == "__main__":
    main()
