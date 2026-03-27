"""
tools/phoenix.py — Phoenix checkpoint and recovery for Morphic-GAOS agents.

Implements the Phoenix Pattern from §8.11 of the OpenClaw Paradigm Book:
when an agent encounters corrupted working state, it restores from the last
known-good checkpoint rather than attempting in-place repair.

Checkpoints are persisted to BigQuery (aos_logs.agent_checkpoints) and loaded
at recovery time. The full lifecycle is:

  1. save_checkpoint()  — Snapshot working state after every major milestone.
  2. validate_state()   — Check a state dict for required fields and size limits.
  3. load_checkpoint()  — Retrieve the latest hash-verified checkpoint from BQ.
  4. phoenix_recover()  — Detect corruption; if found, restore from checkpoint.

Spec: Phoenix Pattern — §8.11, OpenClaw Paradigm Book.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel

from tools.bigquery import insert_row, query_rows

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_CHECKPOINT_TABLE = "aos_logs.agent_checkpoints"

# Fields that must be present and non-empty in a valid checkpoint.
_REQUIRED_STATE_FIELDS = ("agent_id", "project_id")

# Hard limit on serialized state size to prevent unbounded BQ row payloads.
_MAX_STATE_BYTES = 512_000  # 512 KB


# ── Error types ───────────────────────────────────────────────────────────────


class CheckpointCorruptedError(Exception):
    """Active state failed validation and no restorable checkpoint exists."""


class CheckpointSerializationError(Exception):
    """State dict cannot be serialized to JSON."""


# ── Internal helpers ──────────────────────────────────────────────────────────


def _json_default(obj: Any) -> Any:
    """Custom JSON serializer for types not natively handled by json.dumps.

    Handles Pydantic models, datetime/date, and Enum instances losslessly.
    Raises CheckpointSerializationError for any other unsupported type so
    silent data loss via str() is never silently introduced.

    Args:
        obj: The object that json.dumps could not serialize.

    Returns:
        A JSON-safe representation of *obj*.

    Raises:
        CheckpointSerializationError: *obj* is of an unsupported type.
    """
    if isinstance(obj, BaseModel):
        return obj.model_dump()
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    raise CheckpointSerializationError(
        f"State contains a value of type '{type(obj).__name__}' that cannot be "
        f"checkpoint-serialized. Convert it to a JSON-safe type before saving."
    )


def _serialize_state(state: dict[str, Any]) -> str:
    """Serialize *state* to a deterministic JSON string.

    Args:
        state: The agent working state dict.

    Returns:
        Canonical JSON string (sorted keys, no extra whitespace).

    Raises:
        CheckpointSerializationError: If the dict contains an unsupported type
            or is otherwise not JSON-serializable.
    """
    try:
        return json.dumps(state, sort_keys=True, default=_json_default)
    except CheckpointSerializationError:
        raise
    except (TypeError, ValueError) as exc:
        raise CheckpointSerializationError(f"State is not JSON-serializable: {exc}") from exc


def _compute_hash(serialized: str) -> str:
    """Return the SHA-256 hexdigest of *serialized*."""
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ── Public API ────────────────────────────────────────────────────────────────


def validate_state(state: dict[str, Any]) -> dict[str, Any]:
    """
    Validate that *state* is logically consistent and contains required fields.

    Args:
        state: The agent working state dict to validate.

    Returns:
        A dict with keys ``valid`` (bool) and ``reason`` (str, empty on success).
    """
    if not isinstance(state, dict):
        return {"valid": False, "reason": "State is not a dict."}

    for field in _REQUIRED_STATE_FIELDS:
        if not state.get(field):
            return {"valid": False, "reason": f"Required field missing or empty: '{field}'."}

    try:
        serialized = _serialize_state(state)
    except CheckpointSerializationError as exc:
        return {"valid": False, "reason": str(exc)}

    if len(serialized.encode("utf-8")) > _MAX_STATE_BYTES:
        return {
            "valid": False,
            "reason": f"State exceeds max size ({_MAX_STATE_BYTES} bytes).",
        }

    return {"valid": True, "reason": ""}


def save_checkpoint(agent_id: str, project_id: str, state: dict[str, Any]) -> str:
    """
    Persist a snapshot of *state* to BigQuery as a recoverable checkpoint.

    The checkpoint is only written if the state passes validation. The write
    is best-effort — a BQ failure is logged but does not raise, to avoid
    blocking the agent's main execution path.

    Args:
        agent_id:   The agent identifier (e.g., "nexus-prime").
        project_id: The AOS project namespace.
        state:      The agent working state dict to checkpoint.

    Returns:
        The SHA-256 hexdigest of the serialized state.

    Raises:
        CheckpointCorruptedError:     State failed validation — not saved.
        CheckpointSerializationError: State is not JSON-serializable.
    """
    check = validate_state(state)
    if not check["valid"]:
        raise CheckpointCorruptedError(
            f"State failed validation before checkpoint — refusing to save: {check['reason']}"
        )

    serialized = _serialize_state(state)
    state_hash = _compute_hash(serialized)

    row: dict[str, Any] = {
        "agent_id": agent_id,
        "project_id": project_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "state_json": serialized,
        "checkpoint_hash": state_hash,
        "is_valid": True,
    }

    try:
        insert_row(_CHECKPOINT_TABLE, row, project_id)
    except Exception as exc:
        logger.warning(
            "Phoenix checkpoint write failed for %s/%s (non-fatal): %s",
            agent_id,
            project_id,
            exc,
        )

    return state_hash


def load_checkpoint(agent_id: str, project_id: str) -> dict[str, Any] | None:
    """
    Load the most recent valid checkpoint for *agent_id* from BigQuery.

    Each candidate is hash-verified before deserialisation. Hash mismatches
    and malformed JSON are skipped; the next candidate is tried.

    Args:
        agent_id:   The agent identifier.
        project_id: The AOS project namespace.

    Returns:
        The deserialized state dict, or None if no valid checkpoint exists.
    """
    from config import get_settings

    settings = get_settings()
    gcp_project = settings.GCP_PROJECT_ID

    sql = f"""
        SELECT state_json, checkpoint_hash
        FROM `{gcp_project}.{_CHECKPOINT_TABLE}`
        WHERE agent_id = @agent_id
          AND project_id = @project_id
          AND is_valid = TRUE
        ORDER BY timestamp DESC
        LIMIT 5
    """
    try:
        rows = query_rows(sql, project_id, params={"agent_id": agent_id, "project_id": project_id})
    except Exception as exc:
        logger.warning("Phoenix checkpoint load failed for %s/%s: %s", agent_id, project_id, exc)
        return None

    for row in rows:
        state_json: str = row.get("state_json", "")
        stored_hash: str = row.get("checkpoint_hash", "")
        computed_hash = _compute_hash(state_json)

        if computed_hash != stored_hash:
            logger.warning(
                "Phoenix: hash mismatch for %s/%s — skipping (tampered or corrupt).",
                agent_id,
                project_id,
            )
            continue

        try:
            return json.loads(state_json)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "Phoenix: malformed checkpoint JSON for %s/%s — skipping: %s",
                agent_id,
                project_id,
                exc,
            )

    return None


def phoenix_recover(
    agent_id: str,
    project_id: str,
    current_state: dict[str, Any],
) -> dict[str, Any]:
    """
    Validate *current_state*. If corrupted, load and return the last known-good checkpoint.

    This is the primary entry point for the Phoenix Pattern. Call it when an
    agent suspects its working state has been corrupted — for example, after an
    unhandled exception or a failed tool call that may have left state partially
    updated.

    Args:
        agent_id:      The agent identifier.
        project_id:    The AOS project namespace.
        current_state: The agent's current working state dict.

    Returns:
        *current_state* if valid, or the restored checkpoint state if not.

    Raises:
        CheckpointCorruptedError: Current state is corrupted and no restorable
                                  checkpoint exists in BigQuery.
    """
    check = validate_state(current_state)
    if check["valid"]:
        return current_state

    logger.warning(
        "Phoenix: state corruption detected for %s/%s — reason: %s. Attempting recovery.",
        agent_id,
        project_id,
        check["reason"],
    )

    restored = load_checkpoint(agent_id, project_id)
    if restored is None:
        raise CheckpointCorruptedError(
            f"State corrupted for {agent_id}/{project_id} and no restorable checkpoint found. "
            f"Reason: {check['reason']}"
        )

    logger.info(
        "Phoenix: successfully restored state for %s/%s from checkpoint.",
        agent_id,
        project_id,
    )
    return restored
