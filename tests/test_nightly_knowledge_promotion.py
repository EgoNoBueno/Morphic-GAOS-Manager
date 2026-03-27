"""
tests/test_nightly_knowledge_promotion.py — Unit tests for
scripts/nightly_knowledge_promotion.py.

Covers all three sweeps and the helper functions.  All GCP calls
(Sheets, BigQuery, Memory Bank) are patched at the SDK boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from scripts.nightly_knowledge_promotion import (
    _increment_confidence,
    _parse_dt,
    run_confidence_sweep,
    run_expiry_sweep,
    run_promotion_sweep,
)
from tools.memory import MemoryBankError

# ── Settings fixture ───────────────────────────────────────────────────────

SETTINGS_YAML = """\
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
  LOCAL_MODEL_TIMEOUT_SECONDS: 30
projects:
  default:
    sheet_id: spreadsheet-123
    drive_folder_id: folder-abc
"""


@pytest.fixture(autouse=True)
def load_test_settings(tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(SETTINGS_YAML)
    import config

    config._reset_for_testing()
    config.load_settings(cfg)
    yield
    config._reset_for_testing()


# ── Row builder helpers ────────────────────────────────────────────────────

_OLD_TS = (datetime.now(UTC) - timedelta(days=20)).isoformat()
_FRESH_TS = (datetime.now(UTC) - timedelta(days=1)).isoformat()
_NOW_TS = datetime.now(UTC).isoformat()

PROJECT = "morphic-gaos-prod"


def _buffered_row(
    knowledge_id: str = "kid-001",
    confidence: float = 0.0,
    observation_count: int = 0,
    last_seen_at: str = _FRESH_TS,
    status: str = "Buffered",
) -> dict:
    return {
        "knowledge_id": knowledge_id,
        "content_hash": "abc123",
        "agent_id": "scout",
        "project_id": PROJECT,
        "knowledge_type": "fact",
        "domain": "market",
        "content": "Test insight about the market.",
        "evidence": "task-1,task-2",
        "confidence": confidence,
        "observation_count": observation_count,
        "status": status,
        "proposed_at": "",
        "last_seen_at": last_seen_at,
        "approved_by": "",
        "approved_at": "",
        "rejection_reason": "",
        "promoted_memory_id": "",
    }


def _approved_row(
    knowledge_id: str = "kid-100",
    promoted_memory_id: str = "",
) -> dict:
    return {
        "knowledge_id": knowledge_id,
        "content_hash": "def456",
        "agent_id": "foreman",
        "project_id": PROJECT,
        "knowledge_type": "pattern",
        "domain": "operations",
        "content": "Approved operational insight.",
        "evidence": "task-10",
        "confidence": 0.85,
        "observation_count": 5,
        "status": "Approved",
        "proposed_at": _OLD_TS,
        "last_seen_at": _OLD_TS,
        "approved_by": "owner@example.com",
        "approved_at": _NOW_TS,
        "rejection_reason": "",
        "promoted_memory_id": promoted_memory_id,
    }


# ── TestHelpers ────────────────────────────────────────────────────────────


class TestHelpers:
    def test_increment_confidence_from_zero(self):
        result = _increment_confidence(0.0)
        assert abs(result - 0.25) < 1e-9

    def test_increment_confidence_from_quarter(self):
        # 0.25 + (1 - 0.25) * 0.25 = 0.25 + 0.1875 = 0.4375
        result = _increment_confidence(0.25)
        assert abs(result - 0.4375) < 1e-9

    def test_increment_confidence_approaches_but_never_exceeds_one(self):
        conf = 0.0
        for _ in range(100):
            conf = _increment_confidence(conf)
        assert conf < 1.0
        assert conf > 0.99

    def test_parse_dt_valid_iso_string(self):
        ts = "2024-01-15T10:30:00+00:00"
        result = _parse_dt(ts)
        assert result is not None
        assert result.tzinfo is not None

    def test_parse_dt_naive_string_treated_as_utc(self):
        ts = "2024-01-15T10:30:00"
        result = _parse_dt(ts)
        assert result is not None
        assert result.tzinfo == UTC

    def test_parse_dt_empty_string_returns_none(self):
        assert _parse_dt("") is None

    def test_parse_dt_invalid_returns_none(self):
        assert _parse_dt("not-a-date") is None


# ── TestRunExpirySweep ────────────────────────────────────────────────────


class TestRunExpirySweep:
    def test_expires_old_buffered_row(self):
        rows = [_buffered_row(knowledge_id="kid-old", last_seen_at=_OLD_TS)]

        with (
            patch("scripts.nightly_knowledge_promotion.update_row") as mock_update,
            patch("tools.bigquery.insert_row") as mock_bq,
        ):
            expired = run_expiry_sweep(rows, PROJECT)

        assert expired == 1
        mock_update.assert_called_once_with(
            "Pending_Knowledge", "kid-old", {"status": "Expired"}, PROJECT
        )
        assert mock_bq.call_count == 1
        bq_args = mock_bq.call_args[0]
        assert bq_args[0] == "aos_logs.expired_observations"
        assert bq_args[1]["knowledge_id"] == "kid-old"

    def test_skips_fresh_buffered_row(self):
        rows = [_buffered_row(knowledge_id="kid-fresh", last_seen_at=_FRESH_TS)]

        with (
            patch("scripts.nightly_knowledge_promotion.update_row") as mock_update,
            patch("tools.bigquery.insert_row") as mock_bq,
        ):
            expired = run_expiry_sweep(rows, PROJECT)

        assert expired == 0
        mock_update.assert_not_called()
        mock_bq.assert_not_called()

    def test_skips_non_buffered_rows(self):
        rows = [
            _buffered_row(knowledge_id="kid-proposed", status="Proposed", last_seen_at=_OLD_TS),
            _approved_row(knowledge_id="kid-approved"),
        ]

        with (
            patch("scripts.nightly_knowledge_promotion.update_row") as mock_update,
            patch("tools.bigquery.insert_row") as mock_bq,
        ):
            expired = run_expiry_sweep(rows, PROJECT)

        assert expired == 0
        mock_update.assert_not_called()
        mock_bq.assert_not_called()

    def test_bq_error_is_nonfatal_row_still_counted(self):
        rows = [_buffered_row(knowledge_id="kid-bqfail", last_seen_at=_OLD_TS)]

        with (
            patch("scripts.nightly_knowledge_promotion.update_row"),
            patch(
                "tools.bigquery.insert_row",
                side_effect=RuntimeError("BQ down"),
            ),
        ):
            expired = run_expiry_sweep(rows, PROJECT)

        assert expired == 1  # counted because sheet was updated successfully

    def test_sheet_error_skips_row(self):
        rows = [_buffered_row(knowledge_id="kid-sheetfail", last_seen_at=_OLD_TS)]

        with (
            patch(
                "scripts.nightly_knowledge_promotion.update_row",
                side_effect=RuntimeError("Sheet down"),
            ),
            patch("tools.bigquery.insert_row") as mock_bq,
        ):
            expired = run_expiry_sweep(rows, PROJECT)

        assert expired == 0  # not counted — sheet update failed
        mock_bq.assert_not_called()

    def test_dry_run_does_not_write(self):
        rows = [_buffered_row(knowledge_id="kid-dryrun", last_seen_at=_OLD_TS)]

        with (
            patch("scripts.nightly_knowledge_promotion.update_row") as mock_update,
            patch("tools.bigquery.insert_row") as mock_bq,
        ):
            expired = run_expiry_sweep(rows, PROJECT, dry_run=True)

        assert expired == 1  # counted for reporting totals
        mock_update.assert_not_called()
        mock_bq.assert_not_called()

    def test_multiple_rows_mixed(self):
        rows = [
            _buffered_row(knowledge_id="kid-old-1", last_seen_at=_OLD_TS),
            _buffered_row(knowledge_id="kid-fresh-1", last_seen_at=_FRESH_TS),
            _buffered_row(knowledge_id="kid-old-2", last_seen_at=_OLD_TS),
        ]

        with (
            patch("scripts.nightly_knowledge_promotion.update_row"),
            patch("tools.bigquery.insert_row"),
        ):
            expired = run_expiry_sweep(rows, PROJECT)

        assert expired == 2


# ── TestRunConfidenceSweep ────────────────────────────────────────────────


class TestRunConfidenceSweep:
    def test_increments_below_threshold_no_proposal(self):
        # Starting at 0.0 → 0.25 — still below 0.70
        rows = [_buffered_row(knowledge_id="kid-low", confidence=0.0, observation_count=1)]

        with (
            patch("scripts.nightly_knowledge_promotion.update_row") as mock_update,
            patch("tools.google_sheets.append_row") as mock_append,
        ):
            incremented, proposed = run_confidence_sweep(rows, PROJECT)

        assert incremented == 1
        assert proposed == 0

        call_args = mock_update.call_args[0]
        updates = call_args[2]
        assert abs(updates["confidence"] - 0.25) < 0.001
        assert updates["observation_count"] == 2
        assert "status" not in updates  # not proposed yet
        mock_append.assert_not_called()

    def test_crosses_threshold_creates_proposal(self):
        # Starting at 0.68: 0.68 + (1-0.68)*0.25 = 0.68 + 0.08 = 0.76 → ≥ 0.70
        rows = [_buffered_row(knowledge_id="kid-thresh", confidence=0.68, observation_count=4)]

        with (
            patch("scripts.nightly_knowledge_promotion.update_row") as mock_update,
            patch("tools.google_sheets.append_row") as mock_append,
        ):
            incremented, proposed = run_confidence_sweep(rows, PROJECT)

        assert incremented == 1
        assert proposed == 1

        updates = mock_update.call_args[0][2]
        assert updates["status"] == "Proposed"
        assert "proposed_at" in updates
        assert updates["confidence"] >= 0.70

        # Agent_Approvals row should be appended
        assert mock_append.call_count == 1
        append_args = mock_append.call_args[0]
        assert append_args[0] == "Agent_Approvals"
        row_dict = append_args[1]
        assert row_dict["Trigger Reason"] == "KNOWLEDGE_THRESHOLD"
        assert "KNOWLEDGE ENTRY" in row_dict["Proposed Code"]

    def test_skips_non_buffered_rows(self):
        rows = [
            _buffered_row(knowledge_id="kid-proposed", status="Proposed", confidence=0.5),
            _approved_row(knowledge_id="kid-approved"),
        ]

        with (
            patch("scripts.nightly_knowledge_promotion.update_row") as mock_update,
            patch("tools.google_sheets.append_row") as mock_append,
        ):
            incremented, proposed = run_confidence_sweep(rows, PROJECT)

        assert incremented == 0
        assert proposed == 0
        mock_update.assert_not_called()
        mock_append.assert_not_called()

    def test_append_error_is_nonfatal(self):
        # Entry crosses threshold but Agent_Approvals append fails
        rows = [_buffered_row(knowledge_id="kid-appendfail", confidence=0.68)]

        with (
            patch("scripts.nightly_knowledge_promotion.update_row"),
            patch(
                "tools.google_sheets.append_row",
                side_effect=RuntimeError("Sheet write failed"),
            ),
        ):
            incremented, proposed = run_confidence_sweep(rows, PROJECT)

        # proposed count still incremented — the proposal was attempted
        assert incremented == 1
        assert proposed == 1

    def test_sheet_update_error_skips_row(self):
        rows = [_buffered_row(knowledge_id="kid-updatefail", confidence=0.0)]

        with (
            patch(
                "scripts.nightly_knowledge_promotion.update_row",
                side_effect=RuntimeError("Sheet down"),
            ),
            patch("tools.google_sheets.append_row") as mock_append,
        ):
            incremented, proposed = run_confidence_sweep(rows, PROJECT)

        assert incremented == 0
        assert proposed == 0
        mock_append.assert_not_called()

    def test_dry_run_does_not_write(self):
        rows = [_buffered_row(knowledge_id="kid-dryconf", confidence=0.68)]

        with (
            patch("scripts.nightly_knowledge_promotion.update_row") as mock_update,
            patch("tools.google_sheets.append_row") as mock_append,
        ):
            incremented, proposed = run_confidence_sweep(rows, PROJECT, dry_run=True)

        assert incremented == 1
        assert proposed == 1
        mock_update.assert_not_called()
        mock_append.assert_not_called()

    def test_formula_precision(self):
        # 0.0 → 0.25 exactly
        rows = [_buffered_row(confidence=0.0)]

        with (
            patch("scripts.nightly_knowledge_promotion.update_row") as mock_update,
            patch("tools.google_sheets.append_row"),
        ):
            run_confidence_sweep(rows, PROJECT)

        updates = mock_update.call_args[0][2]
        assert updates["confidence"] == round(0.25, 4)
        assert updates["observation_count"] == 1


# ── TestRunPromotionSweep ─────────────────────────────────────────────────


class TestRunPromotionSweep:
    def test_promotes_approved_row(self):
        rows = [_approved_row(knowledge_id="kid-promo", promoted_memory_id="")]

        with (
            patch(
                "scripts.nightly_knowledge_promotion.write_approved_memory",
                return_value="mem-xyz",
            ) as mock_write,
            patch("scripts.nightly_knowledge_promotion.update_row") as mock_update,
        ):
            promoted = run_promotion_sweep(rows, PROJECT)

        assert promoted == 1
        assert mock_write.call_count == 1
        # Verify the MemoryEntry passed to write_approved_memory
        entry_arg = mock_write.call_args[0][0]
        assert entry_arg.domain == "operations"
        assert entry_arg.agent_id == "foreman"
        assert entry_arg.project_id == PROJECT
        assert "task-10" in entry_arg.evidence

        # Verify promoted_memory_id is written back to the Sheet
        mock_update.assert_called_once_with(
            "Pending_Knowledge", "kid-promo", {"promoted_memory_id": "mem-xyz"}, PROJECT
        )

    def test_skips_already_promoted_row(self):
        rows = [_approved_row(knowledge_id="kid-done", promoted_memory_id="mem-already")]

        with (
            patch("scripts.nightly_knowledge_promotion.write_approved_memory") as mock_write,
            patch("scripts.nightly_knowledge_promotion.update_row") as mock_update,
        ):
            promoted = run_promotion_sweep(rows, PROJECT)

        assert promoted == 0
        mock_write.assert_not_called()
        mock_update.assert_not_called()

    def test_skips_non_approved_rows(self):
        rows = [
            _buffered_row(knowledge_id="kid-buf"),
            _buffered_row(knowledge_id="kid-prop", status="Proposed"),
        ]

        with (
            patch("scripts.nightly_knowledge_promotion.write_approved_memory") as mock_write,
            patch("scripts.nightly_knowledge_promotion.update_row") as mock_update,
        ):
            promoted = run_promotion_sweep(rows, PROJECT)

        assert promoted == 0
        mock_write.assert_not_called()
        mock_update.assert_not_called()

    def test_memory_bank_error_skips_row(self):
        rows = [_approved_row(knowledge_id="kid-membankfail")]

        with (
            patch(
                "scripts.nightly_knowledge_promotion.write_approved_memory",
                side_effect=MemoryBankError("Vertex down"),
            ),
            patch("scripts.nightly_knowledge_promotion.update_row") as mock_update,
        ):
            promoted = run_promotion_sweep(rows, PROJECT)

        assert promoted == 0
        mock_update.assert_not_called()

    def test_unexpected_error_skips_row(self):
        rows = [_approved_row(knowledge_id="kid-expterr")]

        with (
            patch(
                "scripts.nightly_knowledge_promotion.write_approved_memory",
                side_effect=RuntimeError("Something exploded"),
            ),
            patch("scripts.nightly_knowledge_promotion.update_row") as mock_update,
        ):
            promoted = run_promotion_sweep(rows, PROJECT)

        assert promoted == 0
        mock_update.assert_not_called()

    def test_dry_run_does_not_write(self):
        rows = [_approved_row(knowledge_id="kid-drypro")]

        with (
            patch("scripts.nightly_knowledge_promotion.write_approved_memory") as mock_write,
            patch("scripts.nightly_knowledge_promotion.update_row") as mock_update,
        ):
            promoted = run_promotion_sweep(rows, PROJECT, dry_run=True)

        assert promoted == 1  # counted for totals
        mock_write.assert_not_called()
        mock_update.assert_not_called()

    def test_multiple_approved_multiple_promoted(self):
        rows = [
            _approved_row(knowledge_id="kid-a"),
            _approved_row(knowledge_id="kid-b", promoted_memory_id="mem-already"),
            _approved_row(knowledge_id="kid-c"),
        ]

        mem_ids = iter(["mem-a1", "mem-c1"])

        with (
            patch(
                "scripts.nightly_knowledge_promotion.write_approved_memory",
                side_effect=lambda entry, pid: next(mem_ids),
            ),
            patch("scripts.nightly_knowledge_promotion.update_row") as mock_update,
        ):
            promoted = run_promotion_sweep(rows, PROJECT)

        assert promoted == 2
        assert mock_update.call_count == 2

    def test_promotion_sweep_enforces_cap(self):
        """When at cap, oldest same-type entry is deactivated before write."""
        import sys

        rows = [_approved_row(knowledge_id="kid-cap")]
        # _approved_row uses agent_id="foreman", knowledge_type="pattern"
        # Default cap for foreman (absent from test settings) = 150.
        # count_active_entries returns 200 >= 150 → triggers eviction.

        lru_entry = MagicMock()
        lru_entry.memory_id = "mem-lru-001"

        fake_client_instance = MagicMock()
        fake_client_instance.list.return_value = [lru_entry]
        fake_client_cls = MagicMock(return_value=fake_client_instance)
        fake_vertexai = MagicMock()
        fake_vertexai.MemoryBankClient = fake_client_cls

        with (
            patch(
                "scripts.nightly_knowledge_promotion.count_active_entries",
                return_value=200,
            ),
            patch.dict(sys.modules, {"vertexai.preview.memory": fake_vertexai}),
            patch(
                "scripts.nightly_knowledge_promotion.write_approved_memory",
                return_value="mem-new-001",
            ) as mock_write,
            patch("scripts.nightly_knowledge_promotion.update_row"),
        ):
            promoted = run_promotion_sweep(rows, PROJECT)

        assert promoted == 1
        # Oldest entry was deactivated before the new write
        fake_client_instance.update.assert_called_once_with("mem-lru-001", {"active": False})
        mock_write.assert_called_once()

    def test_promotion_sweep_skips_cap_on_dry_run(self):
        """Cap enforcement is skipped entirely when dry_run=True."""
        rows = [_approved_row(knowledge_id="kid-drycap")]

        with (
            patch(
                "scripts.nightly_knowledge_promotion.count_active_entries",
            ) as mock_count,
            patch("scripts.nightly_knowledge_promotion.write_approved_memory") as mock_write,
            patch("scripts.nightly_knowledge_promotion.update_row"),
        ):
            promoted = run_promotion_sweep(rows, PROJECT, dry_run=True)

        assert promoted == 1
        mock_count.assert_not_called()
        mock_write.assert_not_called()
