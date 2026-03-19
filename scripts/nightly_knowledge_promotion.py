"""
scripts/nightly_knowledge_promotion.py — Layer 3 → Layer 4 nightly promotion job.

Three sweeps run in sequence on every execution:

  1. Expiry sweep     — mark Buffered entries older than 14 days as Expired
                        and archive them to BigQuery aos_logs.expired_observations.
  2. Confidence sweep — apply one confidence increment to every remaining
                        Buffered entry; promote entries that cross the 0.70
                        threshold to Proposed and append a row to Agent_Approvals.
  3. Promotion sweep  — for each Approved entry whose promoted_memory_id is
                        still empty, build a MemoryEntry and write it to the
                        Vertex AI Memory Bank.

Usage:
    python scripts/nightly_knowledge_promotion.py --project morphic-gaos-prod
    python scripts/nightly_knowledge_promotion.py --project morphic-gaos-prod --dry-run

Spec: GAOS-Memory-Spec.md §5 (Layer 3), §6 (Layer 4)
"""
from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, ".")

from config import get_settings
from models import ApprovalProposal, MemoryEntry
from tools.google_sheets import (
    get_all_records,
    init_sheets_client,
    update_row,
)
from tools.memory import MemoryBankError, write_approved_memory
from tools.secrets import get_secret

# ── Constants ─────────────────────────────────────────────────────────────────

_CONFIDENCE_THRESHOLD = 0.70
_EXPIRY_DAYS = 14


# ── Helpers ───────────────────────────────────────────────────────────────────


def _increment_confidence(current: float) -> float:
    """Apply one observation cycle: new = old + (1 − old) × 0.25."""
    return current + (1.0 - current) * 0.25


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: str) -> datetime | None:
    """
    Parse an ISO 8601 datetime string. Timezone-naive strings are treated as UTC.
    Returns None if the string is empty or unparseable.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


# ── Sweep 1 — Expiry ──────────────────────────────────────────────────────────


def run_expiry_sweep(
    rows: list[dict[str, Any]],
    project_id: str,
    dry_run: bool = False,
) -> int:
    """
    Mark Buffered entries whose last_seen_at is older than _EXPIRY_DAYS as Expired.
    Archives each expired row to BigQuery aos_logs.expired_observations.

    Args:
        rows:       All rows loaded from Pending_Knowledge.
        project_id: Project namespace.
        dry_run:    If True, print what would happen without writing anything.

    Returns:
        Number of rows expired this sweep.
    """
    from tools.bigquery import insert_row  # deferred

    cutoff = datetime.now(timezone.utc) - timedelta(days=_EXPIRY_DAYS)
    expired = 0

    for row in rows:
        if row.get("status") != "Buffered":
            continue

        last_seen = _parse_dt(str(row.get("last_seen_at", "")))
        if last_seen is None or last_seen >= cutoff:
            continue

        knowledge_id = str(row.get("knowledge_id", ""))
        print(f"  [expiry] Expiring {knowledge_id[:12]}… (last_seen={last_seen.date()})")

        if not dry_run:
            try:
                update_row(
                    "Pending_Knowledge",
                    knowledge_id,
                    {"status": "Expired"},
                    project_id,
                )
            except Exception as exc:
                print(f"  [expiry] WARNING — sheet update failed for {knowledge_id}: {exc}")
                continue

            try:
                insert_row(
                    "aos_logs.expired_observations",
                    {
                        "knowledge_id": knowledge_id,
                        "content_hash": str(row.get("content_hash", "")),
                        "agent_id": str(row.get("agent_id", "")),
                        "domain": str(row.get("domain", "")),
                        "content": str(row.get("content", ""))[:1000],
                        "observation_count": row.get("observation_count", 0),
                        "last_seen_at": str(row.get("last_seen_at", "")),
                        "expired_at": _utcnow(),
                        "project_id": project_id,
                    },
                    project_id,
                )
            except Exception as exc:
                # Non-fatal — sheet is already updated; BQ archive is best-effort.
                print(f"  [expiry] WARNING — BQ archive failed for {knowledge_id}: {exc}")

        expired += 1

    return expired


# ── Sweep 2 — Confidence ──────────────────────────────────────────────────────


def run_confidence_sweep(
    rows: list[dict[str, Any]],
    project_id: str,
    dry_run: bool = False,
) -> tuple[int, int]:
    """
    Apply one confidence increment to every remaining Buffered entry.
    Entries that cross 0.70 are promoted to Proposed and written to Agent_Approvals.

    Args:
        rows:       All rows loaded from Pending_Knowledge (post-expiry-sweep).
        project_id: Project namespace.
        dry_run:    If True, print what would happen without writing anything.

    Returns:
        (incremented, proposed) — count of rows updated and count promoted.
    """
    from tools.google_sheets import append_row  # deferred

    incremented = 0
    proposed = 0

    for row in rows:
        if row.get("status") != "Buffered":
            continue

        knowledge_id = str(row.get("knowledge_id", ""))

        try:
            old_confidence = float(row.get("confidence", 0.0))
        except (ValueError, TypeError):
            old_confidence = 0.0

        try:
            old_count = int(row.get("observation_count", 0))
        except (ValueError, TypeError):
            old_count = 0

        new_confidence = _increment_confidence(old_confidence)
        new_count = old_count + 1
        crosses_threshold = new_confidence >= _CONFIDENCE_THRESHOLD

        print(
            f"  [confidence] {knowledge_id[:12]}… "
            f"conf {old_confidence:.3f} → {new_confidence:.3f} "
            f"(obs {new_count})"
            + (" → PROPOSE" if crosses_threshold else "")
        )

        updates: dict[str, Any] = {
            "confidence": round(new_confidence, 4),
            "observation_count": new_count,
        }
        if crosses_threshold:
            updates["status"] = "Proposed"
            updates["proposed_at"] = _utcnow()

        if not dry_run:
            try:
                update_row("Pending_Knowledge", knowledge_id, updates, project_id)
            except Exception as exc:
                print(f"  [confidence] WARNING — sheet update failed for {knowledge_id}: {exc}")
                continue

        incremented += 1

        if crosses_threshold:
            proposal = ApprovalProposal(
                id=str(uuid.uuid4()),
                agent_id=str(row.get("agent_id", "unknown")),
                issue=(
                    f"Knowledge proposal [{row.get('domain', '')}/"
                    f"{row.get('knowledge_type', '')}]: "
                    f"{str(row.get('content', ''))[:200]}"
                ),
                trigger_reason="KNOWLEDGE_THRESHOLD",
                stopping_constraint=f"confidence >= {_CONFIDENCE_THRESHOLD} after {new_count} cycles",
                iterations_run=new_count,
                total_cost_usd=0.0,
                proposed_code=(
                    f"[KNOWLEDGE ENTRY]\n"
                    f"domain        : {row.get('domain', '')}\n"
                    f"knowledge_type: {row.get('knowledge_type', '')}\n"
                    f"agent_id      : {row.get('agent_id', '')}\n"
                    f"knowledge_id  : {knowledge_id}\n\n"
                    f"{row.get('content', '')}"
                ),
            )
            if not dry_run:
                try:
                    append_row("Agent_Approvals", proposal.to_sheet_row(), project_id)
                except Exception as exc:
                    print(
                        f"  [confidence] WARNING — Agent_Approvals append failed "
                        f"for {knowledge_id}: {exc}"
                    )
            proposed += 1

    return incremented, proposed


# ── Sweep 3 — Promotion ───────────────────────────────────────────────────────


def run_promotion_sweep(
    rows: list[dict[str, Any]],
    project_id: str,
    dry_run: bool = False,
) -> int:
    """
    Promote Approved entries (with no promoted_memory_id) to the Vertex AI Memory Bank.
    Called after owner sets status = Approved in the Pending_Knowledge tab.

    Args:
        rows:       All rows loaded from Pending_Knowledge (post-confidence-sweep).
        project_id: Project namespace.
        dry_run:    If True, print what would happen without writing anything.

    Returns:
        Number of entries promoted this sweep.
    """
    promoted = 0

    for row in rows:
        if row.get("status") != "Approved":
            continue
        if row.get("promoted_memory_id"):
            continue  # already promoted in a previous run

        knowledge_id = str(row.get("knowledge_id", ""))
        print(f"  [promotion] Promoting {knowledge_id[:12]}…")

        try:
            confidence = float(row.get("confidence", 0.0))
        except (ValueError, TypeError):
            confidence = 0.0

        approved_at = _parse_dt(str(row.get("approved_at", ""))) or datetime.now(timezone.utc)

        entry = MemoryEntry(
            project_id=project_id,
            agent_id=str(row.get("agent_id", "")),
            knowledge_type=str(row.get("knowledge_type", "fact")),
            domain=str(row.get("domain", "global")),
            content=str(row.get("content", "")),
            evidence=[
                t.strip()
                for t in str(row.get("evidence", "")).split(",")
                if t.strip()
            ],
            confidence=confidence,
            approved_by=str(row.get("approved_by", "")),
            approved_at=approved_at,
        )

        if not dry_run:
            try:
                memory_id = write_approved_memory(entry, project_id)
                update_row(
                    "Pending_Knowledge",
                    knowledge_id,
                    {"promoted_memory_id": memory_id},
                    project_id,
                )
                print(f"  [promotion] Written as memory_id={memory_id}")
            except MemoryBankError as exc:
                print(
                    f"  [promotion] WARNING — Memory Bank write failed "
                    f"for {knowledge_id}: {exc}"
                )
                continue
            except Exception as exc:
                print(f"  [promotion] WARNING — Unexpected error for {knowledge_id}: {exc}")
                continue
        else:
            print(f"  [promotion] DRY RUN — would promote {knowledge_id}")

        promoted += 1

    return promoted


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Nightly knowledge promotion: "
            "Layer 3 (Pending_Knowledge) → Layer 4 (Vertex AI Memory Bank)."
        )
    )
    parser.add_argument(
        "--project",
        default="morphic-gaos-prod",
        help="GCP project / AOS project_id to operate on.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read Sheet and print what would happen without writing anything.",
    )
    args = parser.parse_args()
    project_id = args.project
    dry_run = args.dry_run

    get_settings()  # initialise settings singleton; fail fast if settings.yaml is missing
    get_secret("GEMINI_API_KEY", project_id)  # fail fast if credentials absent

    print("=== Nightly Knowledge Promotion ===")
    print(f"Project  : {project_id}")
    print(f"Dry run  : {dry_run}")
    print()

    init_sheets_client(project_id)
    rows = get_all_records("Pending_Knowledge", project_id)
    buffered_count = sum(1 for r in rows if r.get("status") == "Buffered")
    approved_count = sum(1 for r in rows if r.get("status") == "Approved")
    print(f"Rows loaded : {len(rows)} total — {buffered_count} Buffered, {approved_count} Approved")
    print()

    # Sweep 1 — expiry (must run before confidence sweep so threshold check
    # is not applied to stale entries that should be retired instead)
    print("── Sweep 1: Expiry ──────────────────────────────────────────────")
    expired = run_expiry_sweep(rows, project_id, dry_run)
    print(f"  Total expired : {expired}")
    print()

    # Reload to pick up status changes from expiry sweep before proceeding.
    if not dry_run and expired > 0:
        rows = get_all_records("Pending_Knowledge", project_id)

    # Sweep 2 — confidence increment
    print("── Sweep 2: Confidence ──────────────────────────────────────────")
    incremented, proposed = run_confidence_sweep(rows, project_id, dry_run)
    print(f"  Total incremented : {incremented}  |  proposed : {proposed}")
    print()

    # Reload so promotion sweep sees rows whose status just changed to Approved.
    if not dry_run and (incremented > 0 or proposed > 0):
        rows = get_all_records("Pending_Knowledge", project_id)

    # Sweep 3 — promotion
    print("── Sweep 3: Promotion ───────────────────────────────────────────")
    promoted = run_promotion_sweep(rows, project_id, dry_run)
    print(f"  Total promoted : {promoted}")
    print()

    print(f"Done. expired={expired}  proposed={proposed}  promoted={promoted}")


if __name__ == "__main__":
    main()
