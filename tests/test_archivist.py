"""
tests/test_archivist.py — Unit tests for agents/steward/archivist/orchestrator.py

Covers GAOS-Agent-Spec.md §9.1 requirements (U1–U5) for Tier 3 sub-agents.
All GCP and model calls are mocked at the SDK boundary.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from agents import ModelResponse
from agents.steward.archivist.orchestrator import (
    ArchivistContext,
    ArchivistInput,
    ArchivistOutput,
    FileRecord,
    _is_naming_compliant,
    _validate_path,
    run,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

_PROJECT_ID = "test-project"
_TASK_ID = "task-1234"

_GOOD_FILE = FileRecord(
    file_id="file-abc",
    name="Q1_Budget_Draft.pdf",
    mime_type="application/pdf",
    current_path="Inbound/Q1_Budget_Draft.pdf",
    size_bytes=1024,
    sha256="abc123",
)

_GOOD_CLASSIFICATION = {
    "document_type": "Strategy",
    "project_id_tag": _PROJECT_ID,
    "topic_folder": "Projects/finance",
    "proposed_name": "2026-03-31_test-project_Q1_Budget_Draft.pdf",
    "confidence": 0.92,
}


def _make_input(files: list[FileRecord] | None = None) -> ArchivistInput:
    return ArchivistInput(
        task_id=_TASK_ID,
        project_id=_PROJECT_ID,
        instruction="Classify and propose moves for the provided files.",
        context=ArchivistContext(
            files=files or [_GOOD_FILE],
            taxonomy_hint="",
        ),
    )


def _mock_model_response(payload: dict) -> ModelResponse:
    """Return a mock ModelResponse with the given JSON payload as .text."""
    resp = ModelResponse(text=json.dumps(payload), cost_usd=0.001, tokens_used=150)
    return resp


# ── Path / naming helpers ─────────────────────────────────────────────────────


def test_validate_path_accepts_active_zones():
    assert _validate_path("Inbound/foo.pdf") is True
    assert _validate_path("Projects/acme/report.pdf") is True
    assert _validate_path("Knowledge/procedures/filing.md") is True


def test_validate_path_rejects_traversal():
    assert _validate_path("../etc/passwd") is False
    assert _validate_path("Inbound/../secrets.txt") is False


def test_validate_path_rejects_unknown_zone():
    assert _validate_path("Accounting/ledger.xlsx") is False
    assert _validate_path("Desktop/random.docx") is False


def test_is_naming_compliant_passes_valid_pattern():
    assert _is_naming_compliant("2026-03-31_acme_Q1-Budget.pdf") is True


def test_is_naming_compliant_fails_non_compliant():
    assert _is_naming_compliant("Q1 Budget Draft.pdf") is False
    assert _is_naming_compliant("Untitled.docx") is False


# ── U1: Valid input produces typed ArchivistOutput ────────────────────────────


@pytest.mark.asyncio
async def test_u1_valid_input_returns_typed_output():
    """U1 — Valid AgentInput produces typed AgentOutput with expected structure."""
    agent_input = _make_input()

    with (
        patch("agents.steward.archivist.orchestrator._log_cloud"),
        patch(
            "agents.steward.archivist.orchestrator._call_model",
            return_value=_mock_model_response(_GOOD_CLASSIFICATION),
        ),
        patch("config.get_settings") as mock_settings,
    ):
        mock_settings.return_value.models.LOCAL_MODEL = "ollama/llama3"
        result = await run(agent_input)

    assert isinstance(result, ArchivistOutput)
    assert result.task_id == _TASK_ID
    assert result.project_id == _PROJECT_ID
    assert result.agent_id == "archivist"
    assert result.status == "success"
    assert hasattr(result.result, "approved_moves")
    assert hasattr(result.result, "ambiguous_files")
    assert result.cost_usd >= 0.0


# ── U2: project_id appears in every log call ─────────────────────────────────


@pytest.mark.asyncio
async def test_u2_project_id_in_all_log_calls():
    """U2 — project_id is forwarded to every _log_cloud call."""
    agent_input = _make_input()
    log_calls: list[tuple] = []

    def capture_log(agent_id, project_id, *args, **kwargs):
        log_calls.append((agent_id, project_id))

    with (
        patch("agents.steward.archivist.orchestrator._log_cloud", side_effect=capture_log),
        patch(
            "agents.steward.archivist.orchestrator._call_model",
            return_value=_mock_model_response(_GOOD_CLASSIFICATION),
        ),
        patch("config.get_settings") as mock_settings,
    ):
        mock_settings.return_value.models.LOCAL_MODEL = "ollama/llama3"
        await run(agent_input)

    assert len(log_calls) > 0, "Expected at least one _log_cloud call"
    for agent_id, project_id in log_calls:
        assert project_id == _PROJECT_ID, (
            f"Expected project_id='{_PROJECT_ID}' in log call, got '{project_id}'"
        )
        assert agent_id == "archivist"


# ── U3: Model alias from settings — no hardcoded version string ───────────────


def test_u3_no_hardcoded_model_version():
    """U3 — No literal Gemini version string in agent code."""
    from pathlib import Path

    source = (
        Path(__file__).parent.parent / "agents" / "steward" / "archivist" / "orchestrator.py"
    ).read_text(encoding="utf-8")

    forbidden_patterns = [
        "gemini-1.5",
        "gemini-2.0",
        "gemini-2.5",
        "gemini-pro",
        "gemini-flash",
    ]
    for pattern in forbidden_patterns:
        assert pattern not in source, (
            f"Hardcoded model version string '{pattern}' found in archivist/orchestrator.py"
        )


# ── U4: LOCAL_MODEL unavailable → status="escalated", no continuation ─────────


@pytest.mark.asyncio
async def test_u4_local_model_unavailable_returns_escalated():
    """U4 — When LOCAL_MODEL raises RuntimeError, agent returns escalated, not failed."""
    agent_input = _make_input()

    with (
        patch("agents.steward.archivist.orchestrator._log_cloud"),
        patch(
            "agents.steward.archivist.orchestrator._call_model",
            side_effect=RuntimeError("Ollama unreachable"),
        ),
        patch("config.get_settings") as mock_settings,
    ):
        mock_settings.return_value.models.LOCAL_MODEL = "ollama/llama3"
        result = await run(agent_input)

    assert result.status == "escalated"
    assert isinstance(result.result, dict)
    assert result.result["reason"] == "LOCAL_MODEL_UNAVAILABLE"


# ── U5: Files outside active zones return ambiguous, not failed ───────────────


@pytest.mark.asyncio
async def test_u5_files_outside_active_zone_marked_ambiguous():
    """U5 — Files with paths outside active zones are returned as ambiguous."""
    out_of_zone_file = FileRecord(
        file_id="file-zzz",
        name="secret_ledger.xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        current_path="Accounting/Ledger/secret_ledger.xlsx",
        size_bytes=512,
        sha256="deadbeef",
    )
    agent_input = _make_input(files=[out_of_zone_file])

    with (
        patch("agents.steward.archivist.orchestrator._log_cloud"),
        patch("agents.steward.archivist.orchestrator._call_model") as mock_model,
        patch("config.get_settings") as mock_settings,
    ):
        mock_settings.return_value.models.LOCAL_MODEL = "ollama/llama3"
        result = await run(agent_input)

    # _call_model should never be called for an out-of-zone file
    mock_model.assert_not_called()
    assert result.status == "success"
    from agents.steward.archivist.orchestrator import ArchivistResult

    assert isinstance(result.result, ArchivistResult)
    assert "file-zzz" in result.result.ambiguous_files
    assert result.result.approved_moves == []


# ── Batch limit enforcement ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_limit_capped_at_50():
    """Archivist silently caps input to 50 files per spec."""
    files = [
        FileRecord(
            file_id=f"file-{i:03d}",
            name=f"doc_{i}.pdf",
            mime_type="application/pdf",
            current_path=f"Inbound/doc_{i}.pdf",
        )
        for i in range(75)
    ]
    agent_input = _make_input(files=files)

    with (
        patch("agents.steward.archivist.orchestrator._log_cloud"),
        patch(
            "agents.steward.archivist.orchestrator._call_model",
            return_value=_mock_model_response(_GOOD_CLASSIFICATION),
        ),
        patch("config.get_settings") as mock_settings,
    ):
        mock_settings.return_value.models.LOCAL_MODEL = "ollama/llama3"
        result = await run(agent_input)

    from agents.steward.archivist.orchestrator import ArchivistResult

    assert isinstance(result.result, ArchivistResult)
    assert result.result.files_processed == 50


# ── Low-confidence classification → ambiguous ─────────────────────────────────


@pytest.mark.asyncio
async def test_low_confidence_file_goes_to_ambiguous():
    """Files classified below the 80% threshold are returned as ambiguous."""
    low_conf = dict(_GOOD_CLASSIFICATION)
    low_conf["confidence"] = 0.55
    agent_input = _make_input()

    with (
        patch("agents.steward.archivist.orchestrator._log_cloud"),
        patch(
            "agents.steward.archivist.orchestrator._call_model",
            return_value=_mock_model_response(low_conf),
        ),
        patch("config.get_settings") as mock_settings,
    ):
        mock_settings.return_value.models.LOCAL_MODEL = "ollama/llama3"
        result = await run(agent_input)

    assert result.status == "success"
    from agents.steward.archivist.orchestrator import ArchivistResult

    assert isinstance(result.result, ArchivistResult)
    assert _GOOD_FILE.file_id in result.result.ambiguous_files
    assert result.result.approved_moves == []


# ── Malformed JSON from model → ambiguous, not failed ─────────────────────────


@pytest.mark.asyncio
async def test_malformed_model_json_marks_file_ambiguous():
    """If LOCAL_MODEL returns invalid JSON, the file is ambiguous — agent does not fail."""
    from agents import ModelResponse

    bad_resp = ModelResponse(text="not json at all {{", cost_usd=0.0, tokens_used=10)
    agent_input = _make_input()

    with (
        patch("agents.steward.archivist.orchestrator._log_cloud"),
        patch(
            "agents.steward.archivist.orchestrator._call_model",
            return_value=bad_resp,
        ),
        patch("config.get_settings") as mock_settings,
    ):
        mock_settings.return_value.models.LOCAL_MODEL = "ollama/llama3"
        result = await run(agent_input)

    assert result.status == "success"
    from agents.steward.archivist.orchestrator import ArchivistResult

    assert isinstance(result.result, ArchivistResult)
    assert _GOOD_FILE.file_id in result.result.ambiguous_files


# ── Duplicate detection ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_files_detected():
    """Files with identical lowercased names are flagged as duplicate candidates."""
    dup_a = FileRecord(
        file_id="file-dup-a",
        name="Report Final.pdf",
        mime_type="application/pdf",
        current_path="Inbound/Report Final.pdf",
    )
    dup_b = FileRecord(
        file_id="file-dup-b",
        name="Report Final.pdf",
        mime_type="application/pdf",
        current_path="Projects/acme/Report Final.pdf",
    )
    agent_input = _make_input(files=[dup_a, dup_b])

    with (
        patch("agents.steward.archivist.orchestrator._log_cloud"),
        patch(
            "agents.steward.archivist.orchestrator._call_model",
            return_value=_mock_model_response(_GOOD_CLASSIFICATION),
        ),
        patch("config.get_settings") as mock_settings,
    ):
        mock_settings.return_value.models.LOCAL_MODEL = "ollama/llama3"
        result = await run(agent_input)

    from agents.steward.archivist.orchestrator import ArchivistResult

    assert isinstance(result.result, ArchivistResult)
    assert len(result.result.duplicate_candidates) >= 1
    dup_group = result.result.duplicate_candidates[0]
    assert "file-dup-a" in dup_group
    assert "file-dup-b" in dup_group
