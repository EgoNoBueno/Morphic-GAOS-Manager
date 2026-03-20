"""tests/test_project_registry.py — Unit tests for tools/project_registry.py"""

from unittest.mock import patch

import pytest

from tools.project_registry import (
    ProjectArchivedError,
    ProjectNotFoundError,
    ProjectPausedError,
    ProjectRecord,
    ProjectRegistryParseError,
    get_active_project_ids,
    get_project,
    load_project_registry,
)

# ── Settings fixture ───────────────────────────────────────────────────────

SETTINGS_YAML = """\
gcp:
  project_id: test-project
  region: us-central1
sheet:
  workbook_id: spreadsheet-123
models:
  LOCAL_MODEL: ollama/llama3.1
  FAST_MODEL: gemini-2.0-flash
  DEEP_MODEL: gemini-2.0-pro
  LOCAL_MODEL_FALLBACK: gemini-2.0-flash
  LOCAL_MODEL_TIMEOUT_SECONDS: 2
projects:
  default:
    sheet_id: spreadsheet-123
    drive_folder_id: folder-abc
"""

_REGISTRY_ROWS = [
    {
        "project_id": "acme",
        "project_name": "Acme Corp",
        "status": "Active",
        "sheet_workbook_id": "wb-acme",
        "drive_folder_id": "drv-acme",
    },
    {
        "project_id": "northstar",
        "project_name": "North Star Inc",
        "status": "Paused",
        "sheet_workbook_id": "wb-north",
        "drive_folder_id": "drv-north",
    },
    {
        "project_id": "legacy",
        "project_name": "Legacy Co",
        "status": "Archived",
        "sheet_workbook_id": "wb-legacy",
        "drive_folder_id": "drv-legacy",
    },
]


@pytest.fixture(autouse=True)
def load_test_settings(tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(SETTINGS_YAML)
    import config

    config._reset_for_testing()
    config.load_settings(cfg)
    yield
    config._reset_for_testing()


# ── load_project_registry ──────────────────────────────────────────────────


class TestLoadProjectRegistry:
    def test_returns_list_of_project_records(self):
        with patch("tools.project_registry.get_all_records", return_value=_REGISTRY_ROWS):
            records = load_project_registry("test-project")

        assert len(records) == 3
        assert all(isinstance(r, ProjectRecord) for r in records)

    def test_project_ids_match_sheet_data(self):
        with patch("tools.project_registry.get_all_records", return_value=_REGISTRY_ROWS):
            records = load_project_registry("test-project")

        ids = [r.project_id for r in records]
        assert ids == ["acme", "northstar", "legacy"]

    def test_empty_registry_returns_empty_list(self):
        with patch("tools.project_registry.get_all_records", return_value=[]):
            records = load_project_registry("test-project")

        assert records == []

    def test_raises_parse_error_on_row_missing_required_field(self):
        bad_rows = [{"project_id": "x"}]  # missing project_name, status, etc.

        with patch("tools.project_registry.get_all_records", return_value=bad_rows):
            with pytest.raises(ProjectRegistryParseError):
                load_project_registry("test-project")

    def test_optional_fields_default_to_empty_string(self):
        rows = [
            {
                "project_id": "slim",
                "project_name": "Slim Project",
                "status": "Active",
                "sheet_workbook_id": "wb-slim",
                "drive_folder_id": "drv-slim",
                # no notes, owner_email, etc.
            }
        ]

        with patch("tools.project_registry.get_all_records", return_value=rows):
            records = load_project_registry("test-project")

        assert records[0].notes == ""
        assert records[0].owner_email == ""


# ── get_active_project_ids ─────────────────────────────────────────────────


class TestGetActiveProjectIds:
    def test_returns_only_active_project_ids(self):
        with patch("tools.project_registry.get_all_records", return_value=_REGISTRY_ROWS):
            active = get_active_project_ids("test-project")

        assert active == ["acme"]

    def test_returns_empty_when_no_active_projects(self):
        rows = [r for r in _REGISTRY_ROWS if r["status"] != "Active"]

        with patch("tools.project_registry.get_all_records", return_value=rows):
            active = get_active_project_ids("test-project")

        assert active == []

    def test_all_active_projects_are_returned(self):
        rows = [
            {**_REGISTRY_ROWS[0], "project_id": "alpha"},
            {**_REGISTRY_ROWS[0], "project_id": "beta"},
            {**_REGISTRY_ROWS[1], "project_id": "paused-one"},
        ]

        with patch("tools.project_registry.get_all_records", return_value=rows):
            active = get_active_project_ids("test-project")

        assert set(active) == {"alpha", "beta"}


# ── get_project ────────────────────────────────────────────────────────────


class TestGetProject:
    def test_returns_record_for_active_project(self):
        with patch("tools.project_registry.get_all_records", return_value=_REGISTRY_ROWS):
            record = get_project("acme", "test-project")

        assert record.project_id == "acme"
        assert record.status == "Active"

    def test_raises_project_not_found_for_unknown_id(self):
        with patch("tools.project_registry.get_all_records", return_value=_REGISTRY_ROWS):
            with pytest.raises(ProjectNotFoundError, match="unknown-slug"):
                get_project("unknown-slug", "test-project")

    def test_raises_project_paused_error_for_paused_project(self):
        with patch("tools.project_registry.get_all_records", return_value=_REGISTRY_ROWS):
            with pytest.raises(ProjectPausedError, match="northstar"):
                get_project("northstar", "test-project")

    def test_raises_project_archived_error_for_archived_project(self):
        with patch("tools.project_registry.get_all_records", return_value=_REGISTRY_ROWS):
            with pytest.raises(ProjectArchivedError, match="legacy"):
                get_project("legacy", "test-project")

    def test_drive_folder_id_is_returned_correctly(self):
        with patch("tools.project_registry.get_all_records", return_value=_REGISTRY_ROWS):
            record = get_project("acme", "test-project")

        assert record.drive_folder_id == "drv-acme"
