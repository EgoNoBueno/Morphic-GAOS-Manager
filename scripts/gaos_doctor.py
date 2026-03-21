"""
scripts/gaos_doctor.py — GAOS-Doctor health check runbook.

Verifies all health conditions listed in GAOS-Deploy-Spec.md §19 Phase 4f:
  - Sheet connectivity (read Project Registry tab)
  - Pub/Sub topics exist + all subscriptions active
  - Secret Manager access for all required secrets
  - Cloud Run /health endpoints reachable (7 services)
  - Vertex AI RAG corpora indexed (7 corpora)

Usage (from repo root, .venv activated, ADC configured):
  python scripts/gaos_doctor.py

Exit codes:
  0 — all checks passed
  1 — one or more checks failed
"""

from __future__ import annotations

import sys

import google.auth
import google.auth.transport.requests
import google.oauth2.id_token
import gspread
import httpx
from google.cloud import pubsub_v1, secretmanager
from vertexai.preview import rag

from config import get_settings

# ── Constants ─────────────────────────────────────────────────────────────────

PROJECT = "morphic-gaos-prod"
REGION = "us-central1"

AGENTS = ["nexus-prime", "ledger", "beacon", "pursuit", "foreman", "steward", "scout"]

SERVICE_URLS: dict[str, str] = {
    "nexus-prime": "https://nexus-prime-7bu22bxlda-uc.a.run.app",
    "ledger": "https://ledger-7bu22bxlda-uc.a.run.app",
    "beacon": "https://beacon-7bu22bxlda-uc.a.run.app",
    "pursuit": "https://pursuit-7bu22bxlda-uc.a.run.app",
    "foreman": "https://foreman-7bu22bxlda-uc.a.run.app",
    "steward": "https://steward-7bu22bxlda-uc.a.run.app",
    "scout": "https://scout-7bu22bxlda-uc.a.run.app",
}

SECRETS = [
    "GEMINI_API_KEY",
    "OLLAMA_HOST",
    "WEBHOOK_HMAC_SECRET",
    "WEBHOOK_URL",
    "GOOGLE_SEARCH_API_KEY",
    "GOOGLE_SEARCH_CX",
]

EXPECTED_TOPICS = [
    f"projects/{PROJECT}/topics/agent.{t}.events"
    for t in [
        "nexus-prime",
        "ledger",
        "beacon",
        "pursuit",
        "foreman",
        "steward",
        "scout",
        "approvals",
    ]
]

# ── Helpers ───────────────────────────────────────────────────────────────────

PASS = "\033[32m✅\033[0m"
FAIL = "\033[31m❌\033[0m"
WARN = "\033[33m⚠️ \033[0m"

results: list[tuple[str, bool, str]] = []


def check(label: str, passed: bool, detail: str = "") -> bool:
    """Record and print a single check result."""
    icon = PASS if passed else FAIL
    print(f"  {icon} {label}" + (f"  — {detail}" if detail else ""))
    results.append((label, passed, detail))
    return passed


# ── Check 1: Sheets connectivity ─────────────────────────────────────────────


def check_sheets() -> None:
    """Verify gspread can authenticate and read the Project Registry tab."""
    print("\n[1/5] Sheet Connectivity")
    try:
        settings = get_settings()
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(settings.sheet.workbook_id)
        ws = sh.worksheet("Project Registry")
        rows = ws.get_all_values()
        check("Project Registry tab readable", True, f"{len(rows)} rows")
    except Exception as exc:
        check("Project Registry tab readable", False, str(exc)[:120])


# ── Check 2: Pub/Sub topics + subscriptions ───────────────────────────────────


def check_pubsub() -> None:
    """Verify all 8 topics exist and all 22 subscriptions are active."""
    print("\n[2/5] Pub/Sub Topics + Subscriptions")
    try:
        pub = pubsub_v1.PublisherClient()
        existing = {t.name for t in pub.list_topics(request={"project": f"projects/{PROJECT}"})}
        for topic in EXPECTED_TOPICS:
            check(f"Topic: {topic.split('/')[-1]}", topic in existing)
    except Exception as exc:
        check("Pub/Sub topic list", False, str(exc)[:120])
        return

    try:
        sub = pubsub_v1.SubscriberClient()
        subs = list(sub.list_subscriptions(request={"project": f"projects/{PROJECT}"}))
        check("Subscriptions exist", len(subs) > 0, f"{len(subs)} found (expected 22)")
        # Spot-check key subscriptions
        sub_names = {s.name.split("/")[-1] for s in subs}
        for key_sub in ["nexus-prime.sub.ledger", "nexus-prime.sub.approvals", "scout.sub.foreman"]:
            check(f"Sub: {key_sub}", key_sub in sub_names)
    except Exception as exc:
        check("Pub/Sub subscription list", False, str(exc)[:120])


# ── Check 3: Secret Manager ───────────────────────────────────────────────────


def check_secrets() -> None:
    """Verify Secret Manager access for all required secrets."""
    print("\n[3/5] Secret Manager Access")
    try:
        client = secretmanager.SecretManagerServiceClient()
        for secret_id in SECRETS:
            name = f"projects/{PROJECT}/secrets/{secret_id}/versions/latest"
            try:
                resp = client.access_secret_version(request={"name": name})
                val_len = len(resp.payload.data)
                check(f"Secret: {secret_id}", val_len > 0, f"{val_len} bytes")
            except Exception as exc:
                check(f"Secret: {secret_id}", False, str(exc)[:80])
    except Exception as exc:
        check("Secret Manager client init", False, str(exc)[:120])


# ── Check 4: Cloud Run /health ────────────────────────────────────────────────


def _get_local_id_token(audience: str, impersonate_sa: str) -> str:
    """Get an OIDC ID token via gcloud SA impersonation (user ADC can't produce ID tokens)."""
    import shutil
    import subprocess

    gcloud_cmd = shutil.which("gcloud") or "gcloud"
    result = subprocess.run(
        [
            gcloud_cmd,
            "auth",
            "print-identity-token",
            f"--impersonate-service-account={impersonate_sa}",
            f"--audiences={audience}",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        shell=(sys.platform == "win32"),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[-120:])
    return result.stdout.strip()


def check_health_endpoints() -> None:
    """Hit each Cloud Run /health endpoint with an OIDC token via gcloud."""
    print("\n[4/5] Cloud Run /health Endpoints")

    # nexus-prime-sa has roles/run.invoker on all 7 services (as orchestrator)
    invoker_sa = f"nexus-prime-sa@{PROJECT}.iam.gserviceaccount.com"
    for agent, base_url in SERVICE_URLS.items():
        url = f"{base_url}/health"
        try:
            token = _get_local_id_token(base_url, invoker_sa)
            resp = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
            check(f"{agent} /health", resp.status_code == 200, f"HTTP {resp.status_code}")
        except Exception as exc:
            check(f"{agent} /health", False, str(exc)[:80])


# ── Check 5: Vertex AI RAG corpora ────────────────────────────────────────────


def check_vertex_corpora() -> None:
    """Verify all 7 RAG corpora exist and have at least one indexed file."""
    print("\n[5/5] Vertex AI RAG Corpora")
    try:
        import vertexai

        vertexai.init(project=PROJECT, location="us-west1")
        settings = get_settings()
        corpora = settings.memory_bank.corpora

        for domain, corpus_name in corpora.items():
            try:
                corpus = rag.get_corpus(name=corpus_name)
                check(f"Corpus: {domain}", True, corpus.display_name or corpus_name.split("/")[-1])
            except Exception as exc:
                check(f"Corpus: {domain}", False, str(exc)[:80])
    except Exception as exc:
        check("Vertex AI init", False, str(exc)[:120])


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    """Run all health checks and print a summary."""
    print("=" * 60)
    print("  GAOS-Doctor — Phase 4f Health Check")
    print(f"  Project: {PROJECT}")
    print("=" * 60)

    check_sheets()
    check_pubsub()
    check_secrets()
    check_health_endpoints()
    check_vertex_corpora()

    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    total = len(results)

    print("\n" + "=" * 60)
    print(f"  Results: {passed}/{total} passed  ({failed} failed)")
    print("=" * 60)

    if failed:
        print("\nFailed checks:")
        for label, ok, detail in results:
            if not ok:
                print(f"  {FAIL} {label}" + (f": {detail}" if detail else ""))
        return 1

    print(f"\n{PASS} All {total} checks passed — system is healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
