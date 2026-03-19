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
from datetime import datetime, timezone

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

# Ensure stdout handles full Unicode (e.g. model responses with em-dashes, arrows)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agents import _call_model
from config import get_settings
from tools.google_sheets import append_row, get_all_records, init_sheets_client


_AGENT_ID = "observability-loop"
_LOG_LEVEL = "SYSTEM_THOUGHTS"
_MAX_LOG_ROWS = 50      # rows read for context; keeps prompt small
_MAX_SUMMARY_CHARS = 400


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        print(f"[{_utcnow()}] SYSTEM_THOUGHTS appended ({len(sample)} rows sampled, model={model_used}):")
        print(f"  -> {thought[:120]}{'...' if len(thought) > 120 else ''}")
    except Exception as exc:
        print(f"[{_utcnow()}] ERROR appending to Logs: {exc}")


def _agent_id_with_model(model: str) -> str:
    """Compact agent_id string that records which model actually ran."""
    short = model.replace("ollama/", "").replace("-", "")[:16]
    return f"{_AGENT_ID}/{short}"


def main() -> None:
    parser = argparse.ArgumentParser(description="GAOS Phase 2 observability loop")
    parser.add_argument("--interval", type=int, default=5,
                        help="Poll interval in minutes (default: 5)")
    parser.add_argument("--project", default="morphic-gaos-prod",
                        help="GAOS project_id (default: morphic-gaos-prod)")
    parser.add_argument("--once", action="store_true",
                        help="Run one cycle and exit")
    args = parser.parse_args()

    settings = get_settings()
    project_id = args.project
    interval_s = args.interval * 60

    init_sheets_client(project_id)

    print(f"=== GAOS Observability Loop ===")
    print(f"Project  : {project_id}")
    print(f"Model    : {settings.models.LOCAL_MODEL}")
    print(f"Fallback : {settings.models.LOCAL_MODEL_FALLBACK}")
    print(f"Interval : {args.interval} min")
    print(f"Mode     : {'single cycle' if args.once else 'continuous'}")
    print()

    if args.once:
        _run_cycle(project_id, settings)
        return

    print("Press Ctrl-C to stop.\n")
    try:
        while True:
            _run_cycle(project_id, settings)
            print(f"  [sleeping {args.interval} min...]\n")
            time.sleep(interval_s)
    except KeyboardInterrupt:
        print(f"\n[{_utcnow()}] Observability loop stopped.")


if __name__ == "__main__":
    main()
