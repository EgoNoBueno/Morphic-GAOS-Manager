"""
tools/memory_mirror.py — Memory Mirror: mirrors approved MemoryEntry records to a
human-readable Google Doc ("Knowledge Atlas").

Every approved entry is appended as a structured text block. When an entry supersedes
an earlier one, a ⛔ SUPERSEDED audit marker is included so the Atlas retains a
permanent record of which entry retired which.

The Atlas doc must be pre-created in Google Drive. Paste its ID into settings.yaml
under ``docs.knowledge_atlas_doc_id``. This module never auto-creates the doc —
auto-creation during cold start risks duplicates if multiple agents boot simultaneously.

Usage (from knowledge_review node):
    try:
        from tools.memory_mirror import sync_to_atlas
        sync_to_atlas(entry)
    except Exception as exc:
        _log_cloud(..., f"Atlas sync failed (non-fatal): {exc}", "WARNING")

Spec: GAOS-Memory-Spec.md §Layer 4 · GAOS-Tools-Spec.md §18
"""

from __future__ import annotations

from models import MemoryEntry
from tools.google_docs import DocsApiError, DocumentNotFoundError, append_content

# ── Error types ───────────────────────────────────────────────────────────────


class MemoryMirrorError(Exception):
    """
    Raised when the Atlas sync fails.

    Callers (knowledge_review node) must catch this and log a WARNING — it must
    never block the primary Vertex AI write path. The Vertex write always wins.
    """


# ── Entry format ──────────────────────────────────────────────────────────────

# Prepended when this entry supersedes an older one — appears before the fields
# so a human reviewer sees the retirement notice immediately.
_SUPERSESSION_HEADER = "⛔ SUPERSEDED by {new_id}\nRetires:    {old_id}\nReason:     {reason}\n\n"

_ENTRY_FIELDS = (
    "ID:         {memory_id}\n"
    "Agent:      {agent_id}\n"
    "Domain:     {domain}\n"
    "Type:       {knowledge_type}\n"
    "Content:    {content}\n"
    "Confidence: {confidence:.0%}\n"
    "Approved:   {approved_at}\n"
    "Tags:       {tags}\n"
)


# ── Public API ────────────────────────────────────────────────────────────────


def sync_to_atlas(entry: MemoryEntry, supersession_reason: str | None = None) -> None:
    """Mirror an approved MemoryEntry to the Knowledge Atlas Google Doc.

    Appends a structured text block for the new entry. When ``entry.supersedes``
    is set, the block opens with a ``⛔ SUPERSEDED by <new_id>`` header — placed
    before the standard fields so a reviewer scanning the doc sees the retirement
    notice immediately, then the ``supersession_reason`` explaining why the old
    entry was retired.

    The Atlas doc ID is read from ``settings.docs.knowledge_atlas_doc_id``.
    The doc must be pre-created manually in Google Drive — this function will
    never auto-create it.

    Args:
        entry: The approved MemoryEntry to mirror. Must have ``memory_id`` set.
        supersession_reason: One-sentence explanation of why the old entry is
            being retired, as returned by the LLM. Defaults to
            ``"(no reason provided)"`` when omitted or ``None``.

    Raises:
        MemoryMirrorError: Atlas doc ID is not configured in settings.yaml, or
            the Google Docs API call failed. Callers must catch this and log a
            WARNING — it must never block the primary Vertex AI write.
    """
    from config import get_settings

    settings = get_settings()
    atlas_doc_id: str = getattr(settings.docs, "knowledge_atlas_doc_id", "") or ""

    if not atlas_doc_id:
        raise MemoryMirrorError(
            "knowledge_atlas_doc_id is not configured in settings.docs. "
            "Create the Atlas doc in Google Drive, paste its document ID into "
            "config/settings.yaml under docs.knowledge_atlas_doc_id, then restart."
        )

    approved_at_str = entry.approved_at.strftime("%Y-%m-%dT%H:%M:%SZ") if entry.approved_at else "—"

    fields = _ENTRY_FIELDS.format(
        memory_id=entry.memory_id,
        agent_id=entry.agent_id,
        domain=entry.domain,
        knowledge_type=entry.knowledge_type,
        content=entry.content,
        confidence=entry.confidence,
        approved_at=approved_at_str,
        tags=", ".join(entry.tags) if entry.tags else "—",
    )

    if entry.supersedes:
        reason = supersession_reason or "(no reason provided)"
        header = _SUPERSESSION_HEADER.format(
            new_id=entry.memory_id,
            old_id=entry.supersedes,
            reason=reason,
        )
        text = "\n---\n" + header + fields
    else:
        text = "\n---\n" + fields

    try:
        append_content(doc_id=atlas_doc_id, content=text, project_id=entry.project_id)
    except (DocsApiError, DocumentNotFoundError) as exc:
        raise MemoryMirrorError(
            f"Atlas sync failed for memory_id={entry.memory_id}: {exc}"
        ) from exc
    except Exception as exc:
        raise MemoryMirrorError(
            f"Atlas sync unexpected failure for memory_id={entry.memory_id}: {exc}"
        ) from exc
