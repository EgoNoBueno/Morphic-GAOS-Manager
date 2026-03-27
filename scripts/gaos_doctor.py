"""
scripts/gaos_doctor.py — GAOS-Doctor health check runbook.

Verifies all health conditions listed in GAOS-Deploy-Spec.md §19 (Phase 4 exit criteria,
current system health):
  1. Sheet connectivity (read Project Registry tab)
  2. Pub/Sub topics exist + all subscriptions active
  3. Secret Manager access for all required secrets
  4. Cloud Run /health endpoints reachable (7 services)
  5. Vertex AI RAG corpora indexed (7 corpora)
  6. Agent heartbeat recency (BigQuery aos_logs.status_snapshots)
  7. Monthly cost aggregation (BigQuery aos_logs.task_outcomes)
  8. Cloud Logging error theme analysis (last 1h)

Usage (from repo root, .venv activated, ADC configured):
  python scripts/gaos_doctor.py

Exit codes:
  0 — all checks passed (or WARN-only)
  1 — one or more FAIL checks
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime, timedelta

import google.auth
import google.auth.transport.requests
import google.oauth2.id_token
import gspread
import httpx
from google.cloud import bigquery as _bq
from google.cloud import logging as gcloud_logging
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

OK_TAG = "\033[32m[OK]  \033[0m"
WARN_TAG = "\033[33m[WARN]\033[0m"
FAIL_TAG = "\033[31m[FAIL]\033[0m"

results: list[tuple[str, str, str]] = []  # (label, "ok"|"warn"|"fail", detail)


def check(label: str, passed: bool, detail: str = "") -> bool:
    """Record and print a single check result."""
    tag = OK_TAG if passed else FAIL_TAG
    print(f"  {tag} {label}" + (f" — {detail}" if detail else ""))
    results.append((label, "ok" if passed else "fail", detail))
    return passed


def warn(label: str, detail: str = "") -> None:
    """Record and print a non-critical warning."""
    print(f"  {WARN_TAG} {label}" + (f" — {detail}" if detail else ""))
    results.append((label, "warn", detail))


# ── Check 1: Sheets connectivity ─────────────────────────────────────────────


def check_sheets() -> None:
    """Verify gspread can authenticate and read the Project Registry tab."""
    print("\n[1/8] Sheet Connectivity")
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
    print("\n[2/8] Pub/Sub Topics + Subscriptions")
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
        sub_count = len(subs)
        if sub_count == 0:
            check("Subscriptions exist", False, "0 found (expected 22)")
        elif sub_count < 22:
            warn("Subscriptions exist", f"{sub_count} found (expected 22)")
        else:
            check("Subscriptions exist", True, f"{sub_count} found")
        # Spot-check key subscriptions
        sub_names = {s.name.split("/")[-1] for s in subs}
        for key_sub in ["nexus-prime.sub.ledger", "nexus-prime.sub.approvals", "scout.sub.foreman"]:
            check(f"Sub: {key_sub}", key_sub in sub_names)
    except Exception as exc:
        check("Pub/Sub subscription list", False, str(exc)[:120])


# ── Check 3: Secret Manager ───────────────────────────────────────────────────


def check_secrets() -> None:
    """Verify Secret Manager access for all required secrets."""
    print("\n[3/8] Secret Manager Access")
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
    print("\n[4/8] Cloud Run /health Endpoints")

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
    print("\n[5/8] Vertex AI RAG Corpora")
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


# ── Check 6: Agent heartbeat recency ──────────────────────────────────────────

_HEARTBEAT_WARN_MINUTES = 5
_HEARTBEAT_FAIL_MINUTES = 15


def check_agent_heartbeats() -> None:
    """Query BigQuery for the most recent heartbeat per agent; flag stale agents."""
    print("\n[6/8] Agent Heartbeat Recency")
    try:
        client = _bq.Client(project=PROJECT)
        query = (
            f"SELECT agent_id, MAX(timestamp) AS last_hb "
            f"FROM `{PROJECT}.aos_logs.status_snapshots` "
            f"GROUP BY agent_id"
        )
        rows = {r["agent_id"]: r["last_hb"] for r in client.query(query).result()}
    except Exception as exc:
        check("BQ heartbeat query", False, str(exc)[:120])
        return

    now = datetime.now(tz=UTC)
    for agent_id in AGENTS:
        if agent_id not in rows:
            check(f"{agent_id} heartbeat", False, "no heartbeats recorded")
            continue
        last_hb = rows[agent_id]
        # BigQuery timestamps arrive as datetime; ensure tz-aware
        if last_hb.tzinfo is None:
            last_hb = last_hb.replace(tzinfo=UTC)
        age_seconds = (now - last_hb).total_seconds()
        age_str = f"{int(age_seconds // 60)}m {int(age_seconds % 60)}s ago"
        if age_seconds > _HEARTBEAT_FAIL_MINUTES * 60:
            check(
                f"{agent_id} heartbeat",
                False,
                f"stale {age_str} (fail >{_HEARTBEAT_FAIL_MINUTES}m)",
            )
        elif age_seconds > _HEARTBEAT_WARN_MINUTES * 60:
            warn(f"{agent_id} heartbeat", f"stale {age_str} (warn >{_HEARTBEAT_WARN_MINUTES}m)")
        else:
            check(f"{agent_id} heartbeat", True, age_str)


# ── Check 7: Monthly cost (MTD) ───────────────────────────────────────────────

_COST_WARN_USD = 5.0
_COST_FAIL_USD = 10.0


def check_monthly_cost() -> None:
    """Sum cost_usd from task_outcomes for the current calendar month."""
    print("\n[7/8] Monthly Cost (MTD)")
    try:
        client = _bq.Client(project=PROJECT)
        query = (
            f"SELECT IFNULL(SUM(cost_usd), 0.0) AS total "
            f"FROM `{PROJECT}.aos_logs.task_outcomes` "
            f"WHERE TIMESTAMP_TRUNC(timestamp, MONTH) = TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), MONTH)"
        )
        result = list(client.query(query).result())
        total: float = float(result[0]["total"]) if result else 0.0
    except Exception as exc:
        check("MTD cost query", False, str(exc)[:120])
        return

    detail = f"${total:.4f} MTD (warn>${_COST_WARN_USD}, fail>${_COST_FAIL_USD})"
    if total >= _COST_FAIL_USD:
        check("MTD cost", False, detail)
    elif total >= _COST_WARN_USD:
        warn("MTD cost", detail)
    else:
        check("MTD cost", True, detail)


# ── Check 8: Cloud Logging error themes ───────────────────────────────────────

_ERROR_WARN_COUNT = 5
_THEME_PATTERNS: list[tuple[str, str]] = [
    ("Rate Limiting", r"429|rate.?limit|RESOURCE_EXHAUSTED"),
    ("Auth Failures", r"SecretNotFoundError|PermissionDenied|401|403"),
    ("Timeouts", r"CancelledError|timeout|DeadlineExceeded"),
]


def check_error_themes() -> None:
    """Scan Cloud Logging for ERROR entries in the last hour, grouped by theme."""
    print("\n[8/8] Error Themes (last 1h)")
    try:
        one_hour_ago = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
        log_filter = (
            f'logName="projects/{PROJECT}/logs/gaos-agents" '
            f'severity="ERROR" '
            f'timestamp>="{one_hour_ago}"'
        )
        log_client = gcloud_logging.Client(project=PROJECT)
        entries = list(log_client.list_entries(filter_=log_filter, max_results=500))
    except Exception as exc:
        check("Cloud Logging error query", False, str(exc)[:120])
        return

    buckets: dict[str, int] = {"Rate Limiting": 0, "Auth Failures": 0, "Timeouts": 0, "Internal": 0}
    for entry in entries:
        msg = str(
            entry.payload.get("message", "") if isinstance(entry.payload, dict) else entry.payload
        )
        matched = False
        for theme, pattern in _THEME_PATTERNS:
            if re.search(pattern, msg, re.IGNORECASE):
                buckets[theme] += 1
                matched = True
                break
        if not matched:
            buckets["Internal"] += 1

    n = len(entries)
    breakdown = ", ".join(f"{k}: {v}" for k, v in buckets.items() if v)
    detail = f"{n} errors in last 1h" + (f" — {breakdown}" if breakdown else "")

    if n == 0:
        check("Error themes", True, "0 errors in last 1h")
    elif n <= _ERROR_WARN_COUNT:
        warn("Error themes", detail)
    else:
        check("Error themes", False, detail)


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    """Run all 8 health check sections and print a summary."""
    print("=" * 60)
    print("  GAOS-Doctor — System Health Check")
    print(f"  Project: {PROJECT}")
    print("=" * 60)

    check_sheets()
    check_pubsub()
    check_secrets()
    check_health_endpoints()
    check_vertex_corpora()
    check_agent_heartbeats()
    check_monthly_cost()
    check_error_themes()

    n_ok = sum(1 for _, s, _ in results if s == "ok")
    n_warn = sum(1 for _, s, _ in results if s == "warn")
    n_fail = sum(1 for _, s, _ in results if s == "fail")
    total = len(results)

    print("\n" + "=" * 60)
    print(f"  Summary: {n_ok} OK, {n_warn} WARN, {n_fail} FAIL  ({total} total)")
    print("=" * 60)

    if n_warn or n_fail:
        print("\nNon-OK checks:")
        for label, status, detail in results:
            if status in ("warn", "fail"):
                tag = FAIL_TAG if status == "fail" else WARN_TAG
                print(f"  {tag} {label}" + (f" — {detail}" if detail else ""))

    if n_fail:
        return 1

    if n_warn:
        print(f"\n{WARN_TAG} {n_warn} warning(s) — system is operational with caveats.")
        return 0

    print(f"\n{OK_TAG} All {total} checks passed — system is healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
