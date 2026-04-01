"""
agents/steward/archivist/orchestrator.py — Tier 3 Sub-Agent

Archivist: File organization and taxonomy agent, dispatched by Steward.

Receives a batch of up to 50 unclassified Drive file metadata records,
classifies each one via LOCAL_MODEL, and returns a structured migration
proposal to Steward for Approval Gate submission. This agent performs
**no writes** — all execution is delegated back to the orchestrator.

Identity file: Docs/agents/Archivist.md
Spec:          GAOS-Agent-Spec.md §4
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agents import _call_model, _load_identity_file, _log_cloud

# ── Agent constants ──────────────────────────────────────────────────────────

_AGENT_ID = "archivist"
_BATCH_LIMIT = 50
_CONFIDENCE_THRESHOLD = 0.80
_ACTIVE_ZONES = {"Projects/", "Knowledge/", "Inbound/"}
_NAMING_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}_[A-Za-z0-9_-]+_.+$")


# ── Typed schemas (§2.2 Input/Output Purity — extra="forbid") ───────────────


class FileRecord(BaseModel):
    """Metadata for one unclassified Drive file."""

    model_config = ConfigDict(extra="forbid")

    file_id: str
    name: str
    mime_type: str
    current_path: str
    size_bytes: int = 0
    sha256: str = ""  # Pre-computed by Steward at dispatch time


class ArchivistContext(BaseModel):
    """Typed context block for ArchivistInput."""

    model_config = ConfigDict(extra="forbid")

    files: list[FileRecord]
    taxonomy_hint: str = ""  # Optional: contents of Knowledge/Taxonomy_Master.md


class ArchivistInput(BaseModel):
    """Concrete input schema for the Archivist sub-agent."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    project_id: str
    instruction: str
    context: ArchivistContext


class MoveProposal(BaseModel):
    """A single approved-move or rename-proposal entry."""

    model_config = ConfigDict(extra="forbid")

    file_id: str
    original_name: str
    proposed_name: str
    source_path: str
    destination_path: str
    classification: str  # e.g., "Invoice", "Strategy", "Blueprint"
    project_id_tag: str
    confidence: float
    sha256: str  # Must be verified by Steward before executing the move


class ArchivistResult(BaseModel):
    """Structured output from the Archivist classification pass."""

    model_config = ConfigDict(extra="forbid")

    approved_moves: list[MoveProposal]
    ambiguous_files: list[str]  # file_ids that could not be classified at threshold
    duplicate_candidates: list[list[str]]  # Groups of file_ids that appear identical
    files_processed: int
    cost_usd: float


class ArchivistOutput(BaseModel):
    """Concrete output schema for the Archivist sub-agent."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    project_id: str
    agent_id: str
    status: Literal["success", "escalated", "failed"]
    result: ArchivistResult | dict[str, Any] = Field(default_factory=dict)
    cost_usd: float = 0.0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ── Internal helpers ─────────────────────────────────────────────────────────


def _validate_path(path: str) -> bool:
    """Return True only if path is within an authorised active zone."""
    # Reject path traversal attempts
    if ".." in path:
        return False
    return any(path.startswith(zone) for zone in _ACTIVE_ZONES)


def _is_naming_compliant(name: str) -> bool:
    """Return True if the filename already follows YYYY-MM-DD_Project_Desc convention."""
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return bool(_NAMING_PATTERN.match(stem))


def _build_classification_prompt(
    file_record: FileRecord,
    taxonomy_hint: str,
    project_id: str,
) -> str:
    """Build the LOCAL_MODEL classification prompt for one file."""
    hint_block = f"\nTaxonomy reference:\n{taxonomy_hint[:800]}\n" if taxonomy_hint else ""
    return (
        f"You are a file classification assistant for project '{project_id}'.\n"
        f"Classify the following file and respond in JSON only.\n"
        f"{hint_block}\n"
        f"File name: {file_record.name}\n"
        f"MIME type: {file_record.mime_type}\n"
        f"Current path: {file_record.current_path}\n\n"
        f"Respond with exactly this JSON schema (no markdown, no explanation):\n"
        '{{"document_type": "<Invoice|Strategy|Blueprint|Policy|Playbook|Reference|Other>", '
        '"project_id_tag": "<project_slug>", '
        '"topic_folder": "<destination relative path under Projects/ or Knowledge/>", '
        '"proposed_name": "<YYYY-MM-DD_Project_Description.ext>", '
        '"confidence": <0.0-1.0>}}'
    )


def _sha256_name(name: str) -> str:
    """Return sha256 hex of a file name string — used as duplicate fingerprint."""
    return hashlib.sha256(name.encode()).hexdigest()


# ── Core execution logic (single functional node — §4.7) ────────────────────


def _classify_batch(
    files: list[FileRecord],
    taxonomy_hint: str,
    project_id: str,
    task_id: str,
) -> tuple[list[MoveProposal], list[str], list[list[str]], float]:
    """
    Classify a batch of files with LOCAL_MODEL.

    Returns (approved_moves, ambiguous_ids, duplicate_groups, total_cost_usd).
    """
    from config import get_settings

    model = get_settings().models.LOCAL_MODEL
    approved: list[MoveProposal] = []
    ambiguous: list[str] = []
    total_cost = 0.0

    # Duplicate detection: group files by normalised name hash
    name_index: dict[str, list[str]] = {}
    for f in files:
        key = _sha256_name(f.name.lower().strip())
        name_index.setdefault(key, []).append(f.file_id)
    duplicate_groups = [ids for ids in name_index.values() if len(ids) > 1]

    for file_rec in files:
        # Skip files outside active zones
        if not _validate_path(file_rec.current_path):
            _log_cloud(
                _AGENT_ID,
                project_id,
                "task",
                task_id,
                f"Skipped file outside active zone: {file_rec.current_path}",
                "WARNING",
            )
            ambiguous.append(file_rec.file_id)
            continue

        prompt = _build_classification_prompt(file_rec, taxonomy_hint, project_id)
        try:
            resp = _call_model(prompt, model=model)
            cost_usd = resp.cost_usd
            total_cost += cost_usd

            raw = resp.text.strip()
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw)

            data = json.loads(raw)
            confidence = float(data.get("confidence", 0.0))

            if confidence < _CONFIDENCE_THRESHOLD:
                ambiguous.append(file_rec.file_id)
                _log_cloud(
                    _AGENT_ID,
                    project_id,
                    "task",
                    task_id,
                    f"AMBIGUOUS ({confidence:.0%}): {file_rec.name}",
                    "INFO",
                )
                continue

            destination = data.get("topic_folder", "Inbound/").rstrip("/")
            proposed_name = data.get("proposed_name", file_rec.name)

            # Validate destination is within active zones
            if not _validate_path(destination + "/"):
                ambiguous.append(file_rec.file_id)
                continue

            approved.append(
                MoveProposal(
                    file_id=file_rec.file_id,
                    original_name=file_rec.name,
                    proposed_name=proposed_name,
                    source_path=file_rec.current_path,
                    destination_path=f"{destination}/{proposed_name}",
                    classification=data.get("document_type", "Other"),
                    project_id_tag=data.get("project_id_tag", project_id),
                    confidence=confidence,
                    sha256=file_rec.sha256,
                )
            )
            _log_cloud(
                _AGENT_ID,
                project_id,
                "task",
                task_id,
                f"Classified ({confidence:.0%}): {file_rec.name} → {destination}/{proposed_name}",
                "INFO",
            )

        except json.JSONDecodeError as exc:
            _log_cloud(
                _AGENT_ID,
                project_id,
                "task",
                task_id,
                f"JSON parse failed for {file_rec.name}: {exc}",
                "WARNING",
            )
            ambiguous.append(file_rec.file_id)
        except RuntimeError as exc:
            # LOCAL_MODEL unavailable — escalate immediately
            raise RuntimeError(f"LOCAL_MODEL unavailable: {exc}") from exc

    return approved, ambiguous, duplicate_groups, total_cost


# ── Public entry point ───────────────────────────────────────────────────────


async def run(agent_input: ArchivistInput) -> ArchivistOutput:
    """
    Execute one Archivist classification pass.

    Classifies up to 50 Drive file records and returns a structured
    migration proposal. Performs no writes — all moves are returned
    to Steward for Approval Gate submission.

    Args:
        agent_input: Typed ArchivistInput with file batch and project context.

    Returns:
        ArchivistOutput with status "success", "escalated", or "failed".
    """
    task_id = agent_input.task_id
    project_id = agent_input.project_id

    _log_cloud(
        _AGENT_ID,
        project_id,
        "task",
        task_id,
        f"Archivist started — {len(agent_input.context.files)} files to classify",
        "INFO",
    )

    files = agent_input.context.files[:_BATCH_LIMIT]
    taxonomy_hint = agent_input.context.taxonomy_hint

    try:
        approved, ambiguous, duplicates, cost_usd = _classify_batch(
            files, taxonomy_hint, project_id, task_id
        )
    except RuntimeError as exc:
        # LOCAL_MODEL unavailable — graceful degradation per §4.4
        _log_cloud(
            _AGENT_ID,
            project_id,
            "task",
            task_id,
            f"Escalating: LOCAL_MODEL unavailable — {exc}",
            "ERROR",
        )
        return ArchivistOutput(
            task_id=task_id,
            project_id=project_id,
            agent_id=_AGENT_ID,
            status="escalated",
            result={"reason": "LOCAL_MODEL_UNAVAILABLE", "detail": str(exc)},
            cost_usd=0.0,
        )
    except Exception as exc:
        _log_cloud(
            _AGENT_ID,
            project_id,
            "task",
            task_id,
            f"Unexpected failure: {exc}",
            "ERROR",
        )
        return ArchivistOutput(
            task_id=task_id,
            project_id=project_id,
            agent_id=_AGENT_ID,
            status="failed",
            result={"error": str(exc)},
            cost_usd=0.0,
        )

    result = ArchivistResult(
        approved_moves=approved,
        ambiguous_files=ambiguous,
        duplicate_candidates=duplicates,
        files_processed=len(files),
        cost_usd=cost_usd,
    )

    _log_cloud(
        _AGENT_ID,
        project_id,
        "task",
        task_id,
        (
            f"Archivist complete — {len(approved)} moves proposed, "
            f"{len(ambiguous)} ambiguous, {len(duplicates)} duplicate groups"
        ),
        "INFO",
    )

    return ArchivistOutput(
        task_id=task_id,
        project_id=project_id,
        agent_id=_AGENT_ID,
        status="success",
        result=result,
        cost_usd=cost_usd,
    )


# ── ADK Agent class (§2.1) ───────────────────────────────────────────────────

try:
    from google.adk import Agent  # noqa: F401

    class ArchivistAgent(Agent):
        """Tier 3 sub-agent: Drive file classification and migration proposal."""

        name: str = _AGENT_ID
        description: str = (
            "Classifies unorganised Drive files and returns structured move proposals to Steward."
        )
        model: str = (
            __import__("config", fromlist=["get_settings"]).get_settings().models.LOCAL_MODEL
        )
        instruction: str = _load_identity_file(_AGENT_ID)
        tools: list[Any] = []  # Read-only: uses _call_model + drive metadata; no ADK tool calls

except ImportError:
    # ADK not available in test/CI environments — module is still importable
    ArchivistAgent = None  # type: ignore[assignment,misc]
