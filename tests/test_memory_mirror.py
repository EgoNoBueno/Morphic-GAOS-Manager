"""tests/test_memory_mirror.py — Unit tests for tools/memory_mirror.py"""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from tools.memory_mirror import MemoryMirrorError, sync_to_atlas

# ── Fixtures ───────────────────────────────────────────────────────────────────

_BASE_YAML = """\
gcp:
  project_id: test-project
  region: us-central1
sheet:
  workbook_id: spreadsheet-123
models:
  LOCAL_MODEL: ollama/llama3
  FAST_MODEL: gemini-2.5-flash
  DEEP_MODEL: gemini-2.5-pro
  LOCAL_MODEL_FALLBACK: gemini-2.5-flash
  LOCAL_MODEL_TIMEOUT_SECONDS: 2
projects:
  default:
    sheet_id: spreadsheet-123
    drive_folder_id: folder-abc
"""

_YAML_WITH_ATLAS = (
    _BASE_YAML
    + """\
docs:
  service_account_key: ""
  blueprints_folder_id: ""
  knowledge_atlas_doc_id: "atlas-doc-id-123"
"""
)

_YAML_WITHOUT_ATLAS = (
    _BASE_YAML
    + """\
docs:
  service_account_key: ""
  blueprints_folder_id: ""
  knowledge_atlas_doc_id: ""
"""
)


@pytest.fixture()
def settings_with_atlas(tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(_YAML_WITH_ATLAS)
    import config

    config._reset_for_testing()
    config.load_settings(cfg)
    yield
    config._reset_for_testing()


@pytest.fixture()
def settings_without_atlas(tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(_YAML_WITHOUT_ATLAS)
    import config

    config._reset_for_testing()
    config.load_settings(cfg)
    yield
    config._reset_for_testing()


def _make_entry(**kwargs):
    """Build a minimal MemoryEntry for testing."""
    from models import MemoryEntry

    defaults = {
        "project_id": "test-project",
        "agent_id": "scout",
        "knowledge_type": "fact",
        "domain": "sales",
        "content": "Q4 bookings always spike the last three days of the quarter.",
        "confidence": 0.92,
        "approved_by": "nexus-prime",
        "approved_at": datetime(2026, 3, 20, 10, 0, 0, tzinfo=UTC),
        "tags": ["sales", "q4"],
    }
    defaults.update(kwargs)
    return MemoryEntry(**defaults)


# ── Happy path ─────────────────────────────────────────────────────────────────


def test_sync_to_atlas_happy_path(settings_with_atlas):
    """append_content is called with the correct atlas doc ID and formatted text."""
    entry = _make_entry()

    with patch("tools.memory_mirror.append_content") as mock_append:
        sync_to_atlas(entry)

    mock_append.assert_called_once()
    call_kwargs = mock_append.call_args
    assert call_kwargs.kwargs["doc_id"] == "atlas-doc-id-123"
    assert call_kwargs.kwargs["project_id"] == "test-project"
    appended_text: str = call_kwargs.kwargs["content"]
    assert entry.memory_id in appended_text
    assert "scout" in appended_text
    assert "92%" in appended_text
    assert "2026-03-20T10:00:00Z" in appended_text


def test_sync_to_atlas_includes_supersedes_marker(settings_with_atlas):
    """When entry.supersedes is set, both the Supersedes line and ⛔ marker appear."""
    old_id = "old-memory-id-abc"
    entry = _make_entry(supersedes=old_id)

    with patch("tools.memory_mirror.append_content") as mock_append:
        sync_to_atlas(entry)

    appended_text: str = mock_append.call_args.kwargs["content"]
    assert f"Supersedes: {old_id}" in appended_text
    assert f"⛔ SUPERSEDED: Entry {old_id} retired by {entry.memory_id}" in appended_text


def test_sync_to_atlas_no_supersedes_marker_when_not_set(settings_with_atlas):
    """When entry.supersedes is None, no ⛔ marker appears in the appended text."""
    entry = _make_entry(supersedes=None)

    with patch("tools.memory_mirror.append_content") as mock_append:
        sync_to_atlas(entry)

    appended_text: str = mock_append.call_args.kwargs["content"]
    assert "⛔" not in appended_text
    assert "Supersedes" not in appended_text


def test_sync_to_atlas_approved_at_none_renders_dash(settings_with_atlas):
    """When approved_at is None, 'Approved:' line renders '—' instead of crashing."""
    entry = _make_entry(approved_at=None)

    with patch("tools.memory_mirror.append_content") as mock_append:
        sync_to_atlas(entry)

    appended_text: str = mock_append.call_args.kwargs["content"]
    assert "Approved:   —" in appended_text


# ── Failure handling (non-blocking contract) ───────────────────────────────────


def test_sync_to_atlas_raises_memory_mirror_error_on_docs_api_failure(settings_with_atlas):
    """DocsApiError from append_content is wrapped as MemoryMirrorError."""
    from tools.google_docs import DocsApiError

    entry = _make_entry()

    with patch("tools.memory_mirror.append_content", side_effect=DocsApiError("Docs API down")):
        with pytest.raises(MemoryMirrorError, match="Atlas sync failed"):
            sync_to_atlas(entry)


def test_sync_to_atlas_raises_memory_mirror_error_on_document_not_found(settings_with_atlas):
    """DocumentNotFoundError from append_content is wrapped as MemoryMirrorError."""
    from tools.google_docs import DocumentNotFoundError

    entry = _make_entry()

    with patch(
        "tools.memory_mirror.append_content",
        side_effect=DocumentNotFoundError("Atlas doc not found"),
    ):
        with pytest.raises(MemoryMirrorError, match="Atlas sync failed"):
            sync_to_atlas(entry)


def test_sync_to_atlas_raises_memory_mirror_error_on_unexpected_exception(settings_with_atlas):
    """Unexpected exceptions from append_content are also wrapped as MemoryMirrorError."""
    entry = _make_entry()

    with patch(
        "tools.memory_mirror.append_content",
        side_effect=RuntimeError("unexpected network failure"),
    ):
        with pytest.raises(MemoryMirrorError, match="unexpected failure"):
            sync_to_atlas(entry)


# ── Config safety ──────────────────────────────────────────────────────────────


def test_sync_to_atlas_raises_when_atlas_doc_id_not_configured(settings_without_atlas):
    """MemoryMirrorError is raised with a helpful message when atlas_doc_id is empty."""
    entry = _make_entry()

    with pytest.raises(MemoryMirrorError, match="knowledge_atlas_doc_id is not configured"):
        sync_to_atlas(entry)


def test_sync_to_atlas_does_not_call_append_when_doc_id_missing(settings_without_atlas):
    """append_content is never called when the doc ID is missing."""
    entry = _make_entry()

    with patch("tools.memory_mirror.append_content") as mock_append:
        with pytest.raises(MemoryMirrorError):
            sync_to_atlas(entry)

    mock_append.assert_not_called()
