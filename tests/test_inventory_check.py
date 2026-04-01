"""
tests/test_inventory_check.py — Unit tests for agents/foreman/tasks/inventory_check.py

Covers: happy path (all stocked), stockout detection, empty tab, sheet read
failure, and project_id forwarding.
All GCP calls are mocked at the SDK boundary.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from models import AgentInput

_PROJECT_ID = "test-project"
_TASK_ID = "task-inv-001"


def _make_input(context: dict[str, Any] | None = None) -> AgentInput:
    return AgentInput(
        task_id=_TASK_ID,
        project_id=_PROJECT_ID,
        instruction="Check inventory levels.",
        context=context or {},
    )


def _sheet_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return rows


class TestInventoryCheckHappyPath:
    def test_all_stocked_returns_success(self):
        from agents.foreman.tasks.inventory_check import run

        rows = [
            {
                "sku": "SKU-001",
                "description": "Widget A",
                "quantity_on_hand": 50,
                "reorder_threshold": 10,
            },
            {
                "sku": "SKU-002",
                "description": "Widget B",
                "quantity_on_hand": 20,
                "reorder_threshold": 5,
            },
        ]
        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.get_all_records", return_value=rows),
        ):
            out = run(_make_input())

        assert out.status == "success"
        assert out.result["checked"] == 2
        assert out.result["depleted_skus"] == []

    def test_stockout_returns_stockout_status(self):
        from agents.foreman.tasks.inventory_check import run

        rows = [
            {
                "sku": "SKU-001",
                "description": "Widget A",
                "quantity_on_hand": 2,
                "reorder_threshold": 10,
            },
            {
                "sku": "SKU-002",
                "description": "Widget B",
                "quantity_on_hand": 20,
                "reorder_threshold": 5,
            },
        ]
        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.get_all_records", return_value=rows),
        ):
            out = run(_make_input())

        assert out.status == "stockout"
        assert len(out.result["depleted_skus"]) == 1
        assert out.result["depleted_skus"][0]["sku"] == "SKU-001"
        assert out.result["depleted_skus"][0]["shortfall"] == 8

    def test_multiple_depleted_skus_all_returned(self):
        from agents.foreman.tasks.inventory_check import run

        rows = [
            {"sku": "A", "quantity_on_hand": 0, "reorder_threshold": 5},
            {"sku": "B", "quantity_on_hand": 1, "reorder_threshold": 10},
            {"sku": "C", "quantity_on_hand": 100, "reorder_threshold": 5},
        ]
        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.get_all_records", return_value=rows),
        ):
            out = run(_make_input())

        assert out.status == "stockout"
        assert len(out.result["depleted_skus"]) == 2
        skus = [d["sku"] for d in out.result["depleted_skus"]]
        assert "A" in skus and "B" in skus

    def test_empty_tab_returns_success_with_zero_checked(self):
        from agents.foreman.tasks.inventory_check import run

        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.get_all_records", return_value=[]),
        ):
            out = run(_make_input())

        assert out.status == "success"
        assert out.result["checked"] == 0


class TestInventoryCheckThreshold:
    def test_threshold_override_from_context(self):
        from agents.foreman.tasks.inventory_check import run

        rows = [{"sku": "SKU-1", "quantity_on_hand": 8, "reorder_threshold": 5}]
        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.get_all_records", return_value=rows),
        ):
            # threshold_override=10 means qty=8 is below threshold
            out = run(_make_input({"threshold_override": 10}))

        assert out.status == "stockout"

    def test_default_threshold_used_when_column_missing(self):
        from agents.foreman.tasks.inventory_check import _DEFAULT_THRESHOLD, run

        rows = [{"sku": "SKU-X", "quantity_on_hand": _DEFAULT_THRESHOLD - 1}]
        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.get_all_records", return_value=rows),
        ):
            out = run(_make_input())

        assert out.status == "stockout"

    def test_rows_without_sku_are_skipped(self):
        from agents.foreman.tasks.inventory_check import run

        rows = [
            {"sku": "", "quantity_on_hand": 0, "reorder_threshold": 5},  # skipped
            {"sku": "REAL-SKU", "quantity_on_hand": 50, "reorder_threshold": 5},
        ]
        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.get_all_records", return_value=rows),
        ):
            out = run(_make_input())

        assert out.status == "success"
        assert out.result["checked"] == 2  # both rows counted, only non-blank sku evaluated


class TestInventoryCheckFailures:
    def test_sheet_read_failure_returns_escalated(self):
        from agents.foreman.tasks.inventory_check import run

        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch(
                "tools.google_sheets.get_all_records", side_effect=Exception("Sheet unavailable")
            ),
        ):
            out = run(_make_input())

        assert out.status == "escalated"
        assert "Sheet read failed" in out.result["error"]

    def test_project_id_forwarded_to_sheet_call(self):
        from agents.foreman.tasks.inventory_check import run

        captured: list[str] = []

        def _capture_get(tab: str, pid: str) -> list:
            captured.append(pid)
            return []

        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.get_all_records", side_effect=_capture_get),
        ):
            run(_make_input())

        assert captured == [_PROJECT_ID]

    def test_cost_usd_always_zero(self):
        from agents.foreman.tasks.inventory_check import run

        with (
            patch("tools.google_sheets.init_sheets_client"),
            patch("tools.google_sheets.get_all_records", return_value=[]),
        ):
            out = run(_make_input())

        assert out.cost_usd == 0.0
