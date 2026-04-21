"""
scripts/observability_loop.py — Phase 2 Ollama Observability Loop.

Runs continuously, polling the Logs tab every N minutes and using
LOCAL_MODEL (Ollama) to generate a "System Thoughts" summary row,
which is appended back to the Logs tab under level='SYSTEM_THOUGHTS'.

Usage:
    python scripts/observability_loop.py [--interval 5] [--project morphic-gaos-prod]

Arguments:
    --interval   Poll interval in minutes (default: 5)
    --project    GAOS project_id to read/write (default: morphic-gaos-prod)
    --once       Run one cycle then exit (useful for smoke-testing)

Exit:
    Ctrl-C to stop. Logs every cycle to stdout and to the Logs Sheet tab.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

# Ensure stdout handles full Unicode (e.g. model responses with em-dashes, arrows)
import io as _io

if isinstance(sys.stdout, _io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8")

from agents import _call_model
from config import get_settings
from tools.google_sheets import append_row, get_all_records, init_sheets_client

_AGENT_ID = "observability-loop"
_LOG_LEVEL = "SYSTEM_THOUGHTS"
_MAX_LOG_ROWS = 50  # rows read for context; keeps prompt small
_MAX_SUMMARY_CHARS = 400

# ── Known push subscriptions and their expected Cloud Run service names ───────────────
#
# Format: {subscription_name: service_name_prefix}
# The check resolves the live Cloud Run URL for each service at runtime and
# compares it against the subscription's pushConfig.pushEndpoint.
# A mismatch means the subscription is pointing at an old (stale) URL and
# messages will silently 404 — as happened 2026-03-31 after nexus-prime redeploy.
#
# Rule 27.3: stale-URL detection must run in the observability loop.
_NEXUS_SUB_SUFFIXES: list[str] = [
    "nexus-prime.sub.events",
    "nexus-prime.sub.approvals",
    "nexus-prime.sub.ledger",
    "nexus-prime.sub.beacon",
    "nexus-prime.sub.pursuit",
    "nexus-prime.sub.foreman",
    "nexus-prime.sub.steward",
    "nexus-prime.sub.scout",
]


def _get_known_push_subs(project_id: str) -> dict[str, str]:
    """Return the push-subscription-name → service-name mapping for project_id."""
    return {
        f"projects/{project_id}/subscriptions/{suffix}": "nexus-prime"
        for suffix in _NEXUS_SUB_SUFFIXES
    }


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _check_pubsub_endpoint_staleness(project_id: str) -> None:
    """Rule 27.3 — warn if any push subscription points at a stale Cloud Run URL.

    Resolves the live nexus-prime Cloud Run URL via the Cloud Run Admin API and
    compares it against each known push subscription's pushConfig.pushEndpoint.
    Logs a WARNING for any mismatch so the operator can run provision_schedulers.py
    to re-point the subscription.

    Args:
        project_id: GCP project ID (used for Cloud Run and Pub/Sub API calls).
    """
    try:
        import google.auth
        from google.cloud import pubsub_v1
        from googleapiclient.discovery import build

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])

        # Resolve the live URL for nexus-prime via Cloud Run Admin API
        run_client = build("run", "v2", credentials=creds, cache_discovery=False)
        name = f"projects/{project_id}/locations/us-central1/services/nexus-prime"
        try:
            svc = run_client.projects().locations().services().get(name=name).execute()
            live_url = svc.get("uri", "").rstrip("/")
        except Exception as exc:
            print(f"[{_utcnow()}] PUBSUB-STALE: could not resolve nexus-prime URL — {exc}")
            return

        subscriber = pubsub_v1.SubscriberClient(credentials=creds)
        known_push_subs = _get_known_push_subs(project_id)
        any_stale = False
        for sub_name, _service in known_push_subs.items():
            try:
                sub = subscriber.get_subscription(request={"subscription": sub_name})
                endpoint = sub.push_config.push_endpoint.rstrip("/")
                # Normalise: strip /pubsub suffix when present so we compare base URLs.
                # When the suffix is absent (non-standard endpoint), compare as-is and
                # log a debug note so non-standard configs are visible.
                if "/pubsub" in endpoint:
                    endpoint_base = endpoint.rsplit("/pubsub", 1)[0]
                else:
                    endpoint_base = endpoint
                    print(
                        f"[{_utcnow()}] DEBUG PUBSUB-STALE: {sub_name.split('/')[-1]} "
                        f"endpoint has no /pubsub suffix — comparing full URL: {endpoint}"
                    )
                if live_url and endpoint_base != live_url:
                    print(
                        f"[{_utcnow()}] WARNING PUBSUB-STALE: {sub_name.split('/')[-1]} "
                        f"→ {endpoint} (expected prefix {live_url}) — run provision_schedulers.py"
                    )
                    any_stale = True
            except Exception as exc:
                print(f"[{_utcnow()}] PUBSUB-STALE: could not check {sub_name} — {exc}")
        if not any_stale:
            print(
                f"[{_utcnow()}] PUBSUB-STALE: all {len(known_push_subs)} push endpoints up to date"
            )
    except Exception as exc:
        print(f"[{_utcnow()}] PUBSUB-STALE check failed — {exc}")


def _run_cycle(project_id: str, settings) -> None:
    """Read recent Logs, call Ollama, append a SYSTEM_THOUGHTS row."""
    # ── 1. Read recent activity ───────────────────────────────────────────────
    try:
        rows = get_all_records("Logs", project_id)
    except Exception as exc:
        print(f"[{_utcnow()}] ERROR reading Logs tab: {exc}")
        return

    recent = [r for r in rows if r.get("level") != _LOG_LEVEL]
    sample = recent[-_MAX_LOG_ROWS:]

    if not sample:
        print(f"[{_utcnow()}] No non-SYSTEM_THOUGHTS log rows found — skipping cycle.")
        return

    # ── 2. Build prompt ───────────────────────────────────────────────────────
    entries = "\n".join(
        f"  [{r.get('timestamp', '?')}] {r.get('agent_id', '?')} [{r.get('level', '?')}]: "
        f"{r.get('message', '')[:120]}"
        for r in sample
    )
    prompt = (
        f"You are an observability assistant for an AI agent system. "
        f"Review the following {len(sample)} recent log entries and write a single concise "
        f"paragraph (max 3 sentences, under {_MAX_SUMMARY_CHARS} characters) summarizing "
        f"the current system health, active agents, and any notable patterns or concerns.\n\n"
        f"Log entries:\n{entries}\n\n"
        f"System Thoughts:"
    )

    # ── 3. Call LOCAL_MODEL (Ollama) ──────────────────────────────────────────
    try:
        resp = _call_model(prompt, model=settings.models.LOCAL_MODEL)
        thought = resp.text.strip()[:_MAX_SUMMARY_CHARS]
        model_used = settings.models.LOCAL_MODEL
        if resp.cost_usd > 0:
            model_used = settings.models.LOCAL_MODEL_FALLBACK + " (fallback)"
    except Exception as exc:
        print(f"[{_utcnow()}] ERROR calling model: {exc}")
        return

    if not thought:
        print(f"[{_utcnow()}] Empty model response — skipping append.")
        return

    # ── 4. Append to Logs tab ─────────────────────────────────────────────────
    row = {
        "timestamp": _utcnow(),
        "agent_id": _agent_id_with_model(model_used),
        "level": _LOG_LEVEL,
        "message": thought,
        "project_id": project_id,
    }
    try:
        append_row("Logs", row, project_id)
        print(
            f"[{_utcnow()}] SYSTEM_THOUGHTS appended ({len(sample)} rows sampled, model={model_used}):"
        )
        print(f"  -> {thought[:120]}{'...' if len(thought) > 120 else ''}")
    except Exception as exc:
        print(f"[{_utcnow()}] ERROR appending to Logs: {exc}")


def _agent_id_with_model(model: str) -> str:
    """Compact agent_id string that records which model actually ran."""
    short = model.replace("ollama/", "").replace("-", "")[:16]
    return f"{_AGENT_ID}/{short}"


def main() -> None:
    parser = argparse.ArgumentParser(description="GAOS Phase 2 observability loop")
    parser.add_argument(
        "--interval", type=int, default=5, help="Poll interval in minutes (default: 5)"
    )
    parser.add_argument(
        "--project",
        default="morphic-gaos-prod",
        help="GAOS project_id (default: morphic-gaos-prod)",
    )
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    args = parser.parse_args()

    settings = get_settings()
    project_id = args.project
    interval_s = args.interval * 60

    init_sheets_client(project_id)

    print("=== GAOS Observability Loop ===")
    print(f"Project  : {project_id}")
    print(f"Model    : {settings.models.LOCAL_MODEL}")
    print(f"Fallback : {settings.models.LOCAL_MODEL_FALLBACK}")
    print(f"Interval : {args.interval} min")
    print(f"Mode     : {'single cycle' if args.once else 'continuous'}")
    print()

    if args.once:
        _run_cycle(project_id, settings)
        _check_pubsub_endpoint_staleness(project_id)
        return

    print("Press Ctrl-C to stop.\n")
    try:
        while True:
            _run_cycle(project_id, settings)
            _check_pubsub_endpoint_staleness(project_id)
            print(f"  [sleeping {args.interval} min...]\n")
            time.sleep(interval_s)
    except KeyboardInterrupt:
        print(f"\n[{_utcnow()}] Observability loop stopped.")


if __name__ == "__main__":
    main()
