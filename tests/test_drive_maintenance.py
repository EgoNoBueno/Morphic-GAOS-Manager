"""
tests/test_drive_maintenance.py — Unit tests for agents/steward/tasks/drive_maintenance.py

Covers happy path, Drive scan failure, Archivist failure, requires_approval flag,
and project_id forwarding requirements (GAOS-Agent-Spec.md §9.1 U2).
All GCP and model calls are mocked at the SDK boundary.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from agents.steward.archivist.orchestrator import ArchivistOutput
from models import AgentInput

# ── Fixtures ──────────────────────────────────────────────────────────────────

_PROJECT_ID = "test-project"
_TASK_ID = "task-dm-001"


def _make_input(context: dict[str, Any] | None = None) -> AgentInput:
    return AgentInput(
        task_id=_TASK_ID,
        project_id=_PROJECT_ID,
        instruction="Run drive maintenance.",
        context=context or {},
    )


def _archivist_output(
    status: str = "success", approved_moves: list | None = None
) -> ArchivistOutput:
    """Build a mock ArchivistOutput."""
    from datetime import UTC, datetime

    from agents.steward.archivist.orchestrator import ArchivistOutput, ArchivistResult

    result = ArchivistResult(
        approved_moves=approved_moves or [],
        ambiguous_files=[],
        duplicate_candidates=[],
        files_processed=1,
        cost_usd=0.002,
    )
    out = ArchivistOutput(
        task_id="archivist-task-1",
        project_id=_PROJECT_ID,
        agent_id="archivist",
        status=status,  # type: ignore[arg-type]
        result=result,
        cost_usd=0.002,
        timestamp=datetime.now(UTC),
    )
    return out


def _sample_move() -> dict:
    return {
        "file_id": "abc123",
        "original_name": "invoice.pdf",
        "proposed_name": "2026-03-31_test-project_Invoice.pdf",
        "source_path": "Inbound/invoice.pdf",
        "destination_path": "Projects/test-project/2026-03-31_test-project_Invoice.pdf",
        "classification": "Invoice",
        "project_id_tag": _PROJECT_ID,
        "confidence": 0.92,
        "sha256": "deadbeef",
    }


# ── Empty Inbound ─────────────────────────────────────────────────────────────


_MODULE = "agents.steward.tasks.drive_maintenance"


def test_empty_inbound_returns_success_no_approval():
    from agents.steward.tasks.drive_maintenance import run

    with (
        patch(f"{_MODULE}._log_cloud"),
        patch(f"{_MODULE}.list_folder", return_value=[]),
    ):
        out = run(_make_input())

    assert out.status == "success"
    assert out.project_id == _PROJECT_ID
    assert out.agent_id == "steward"
    assert not out.result.get("requires_approval")
    assert "empty" in out.result.get("message", "").lower()


# ── Drive scan failure ────────────────────────────────────────────────────────


def test_drive_scan_failure_returns_failed():
    from agents.steward.tasks.drive_maintenance import run
    from tools.drive import DriveReadError

    with (
        patch(f"{_MODULE}._log_cloud"),
        patch(
            f"{_MODULE}.list_folder",
            side_effect=DriveReadError("network error"),
        ),
    ):
        out = run(_make_input())

    assert out.status == "failed"
    assert "network error" in out.result.get("error", "")


def test_folder_not_found_returns_failed():
    from agents.steward.tasks.drive_maintenance import run
    from tools.drive import KnowledgeFolderNotFoundError

    with (
        patch(f"{_MODULE}._log_cloud"),
        patch(
            f"{_MODULE}.list_folder",
            side_effect=KnowledgeFolderNotFoundError("Inbound not found"),
        ),
    ):
        out = run(_make_input())

    assert out.status == "failed"


# ── Archivist failure propagation ─────────────────────────────────────────────


def test_archivist_failed_returns_escalated():
    from agents.steward.tasks.drive_maintenance import run

    failed_out = _archivist_output(status="failed")

    with (
        patch(f"{_MODULE}._log_cloud"),
        patch(f"{_MODULE}.list_folder", return_value=["Inbound/report.pdf"]),
        patch(f"{_MODULE}.read_file", side_effect=Exception("no file")),
        patch(f"{_MODULE}._archivist_run", AsyncMock(return_value=failed_out)),
    ):
        out = run(_make_input())

    assert out.status == "escalated"
    assert out.project_id == _PROJECT_ID


def test_archivist_escalated_returns_escalated():
    from agents.steward.tasks.drive_maintenance import run

    escalated_out = _archivist_output(status="escalated")

    with (
        patch(f"{_MODULE}._log_cloud"),
        patch(f"{_MODULE}.list_folder", return_value=["Inbound/report.pdf"]),
        patch(f"{_MODULE}.read_file", side_effect=Exception),
        patch(f"{_MODULE}._archivist_run", AsyncMock(return_value=escalated_out)),
    ):
        out = run(_make_input())

    assert out.status == "escalated"


# ── Move proposals → requires_approval ───────────────────────────────────────


def test_approved_moves_set_requires_approval_true():
    from agents.steward.archivist.orchestrator import MoveProposal
    from agents.steward.tasks.drive_maintenance import run

    move = MoveProposal(**_sample_move())
    success_out = _archivist_output(status="success", approved_moves=[move])

    with (
        patch(f"{_MODULE}._log_cloud"),
        patch(f"{_MODULE}.list_folder", return_value=["Inbound/invoice.pdf"]),
        patch(f"{_MODULE}.read_file", side_effect=Exception),
        patch(f"{_MODULE}._archivist_run", AsyncMock(return_value=success_out)),
    ):
        out = run(_make_input())

    assert out.status == "success"
    assert out.result.get("requires_approval") is True
    assert out.result.get("task_type") == "drive_maintenance"
    assert "approved_moves" in out.result
    assert out.cost_usd == success_out.cost_usd


def test_no_approved_moves_requires_approval_false():
    from agents.steward.tasks.drive_maintenance import run

    success_out = _archivist_output(status="success", approved_moves=[])

    with (
        patch(f"{_MODULE}._log_cloud"),
        patch(f"{_MODULE}.list_folder", return_value=["Inbound/mystery.bin"]),
        patch(f"{_MODULE}.read_file", side_effect=Exception),
        patch(f"{_MODULE}._archivist_run", AsyncMock(return_value=success_out)),
    ):
        out = run(_make_input())

    assert out.status == "success"
    assert out.result.get("requires_approval") is False


# ── U2: project_id forwarded to Archivist ─────────────────────────────────────


def test_u2_project_id_forwarded_to_archivist():
    """U2 — project_id from AgentInput must appear in the ArchivistInput passed to archivist."""
    from agents.steward.tasks.drive_maintenance import run

    captured: list[Any] = []

    async def _fake_archivist(ai):
        captured.append(ai)
        return _archivist_output(status="success", approved_moves=[])

    with (
        patch(f"{_MODULE}._log_cloud"),
        patch(f"{_MODULE}.list_folder", return_value=["Inbound/doc.pdf"]),
        patch(f"{_MODULE}.read_file", side_effect=Exception),
        patch(f"{_MODULE}._archivist_run", side_effect=_fake_archivist),
    ):
        run(_make_input())

    assert len(captured) == 1
    assert captured[0].project_id == _PROJECT_ID


# ── Taxonomy hint from context ────────────────────────────────────────────────


def test_taxonomy_hint_from_context_skips_drive_read():
    """If taxonomy_hint is in context, read_file should not be called."""
    from agents.steward.tasks.drive_maintenance import run

    success_out = _archivist_output(status="success")

    mock_read = MagicMock()
    with (
        patch(f"{_MODULE}._log_cloud"),
        patch(f"{_MODULE}.list_folder", return_value=["Inbound/file.pdf"]),
        patch(f"{_MODULE}.read_file", mock_read),
        patch(f"{_MODULE}._archivist_run", AsyncMock(return_value=success_out)),
    ):
        run(_make_input(context={"taxonomy_hint": "Invoice: financial documents\nContract: legal"}))

    mock_read.assert_not_called()


# ── FileRecord construction ───────────────────────────────────────────────────


def test_file_records_derive_name_from_path():
    """Verify FileRecord.name is the basename extracted from the path."""
    from agents.steward.tasks.drive_maintenance import run

    success_out = _archivist_output(status="success")
    captured: list[Any] = []

    async def _fake_archivist(ai):
        captured.append(ai.context.files)
        return success_out

    with (
        patch(f"{_MODULE}._log_cloud"),
        patch(
            f"{_MODULE}.list_folder",
            return_value=["Inbound/subdir/quarterly-report.xlsx"],
        ),
        patch(f"{_MODULE}.read_file", side_effect=Exception),
        patch(f"{_MODULE}._archivist_run", side_effect=_fake_archivist),
    ):
        run(_make_input())

    assert len(captured) == 1
    record = captured[0][0]
    assert record.name == "quarterly-report.xlsx"
    assert record.current_path == "Inbound/subdir/quarterly-report.xlsx"
    assert record.mime_type != ""
