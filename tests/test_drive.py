"""tests/test_drive.py — Unit tests for tools/drive.py"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

import config
from tools.drive import (
    DrivePermissionError,
    DriveReadError,
    DriveWriteError,
    KnowledgeFileNotFoundError,
    KnowledgeFolderNotFoundError,
    copy_file,
    list_folder,
    move_file,
    read_file,
    write_file,
)

# ── Settings ──────────────────────────────────────────────────────────────────

_SETTINGS_YAML = """\
gcp:
  project_id: test-project
  region: us-central1
sheet:
  workbook_id: spreadsheet-123
models:
  LOCAL_MODEL: ollama/llama3
  FAST_MODEL: gemini-2.0-flash
  DEEP_MODEL: gemini-2.0-pro
  LOCAL_MODEL_FALLBACK: gemini-2.0-flash
  LOCAL_MODEL_TIMEOUT_SECONDS: 2
projects:
  default:
    sheet_id: spreadsheet-123
    drive_folder_id: root-folder-id
  test-project:
    sheet_id: spreadsheet-123
    drive_folder_id: root-folder-id
"""

_PROJECT = "test-project"


@pytest.fixture(autouse=True)
def settings(tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(_SETTINGS_YAML)
    config._reset_for_testing()
    config.load_settings(cfg)
    yield
    config._reset_for_testing()


@pytest.fixture()
def mock_drive():
    """Patch ADC and Drive SDK; yield the mock service object."""
    svc = MagicMock()
    with (
        patch("google.auth.default", return_value=(MagicMock(), "test-project")),
        patch("tools.drive.build", return_value=svc),
    ):
        yield svc


@pytest.fixture()
def no_sleep():
    """Suppress time.sleep inside _retry_drive so retry tests finish instantly."""
    with patch("tools.drive.time.sleep"):
        yield


# ── Helpers ───────────────────────────────────────────────────────────────────


def _http_error(status: int) -> HttpError:
    resp = MagicMock()
    resp.status = str(status)
    return HttpError(resp, b"simulated error")


# ── read_file ─────────────────────────────────────────────────────────────────


class TestReadFile:
    def test_happy_path_returns_content(self, mock_drive):
        # _resolve_path: 2 list calls ("procedures", then "invoice.md")
        mock_drive.files.return_value.list.return_value.execute.side_effect = [
            {"files": [{"id": "folder-1", "name": "procedures"}]},
            {"files": [{"id": "file-1", "name": "invoice.md"}]},
        ]
        mock_drive.files.return_value.get_media.return_value.execute.return_value = (
            b"Invoice content"
        )

        result = read_file("procedures/invoice.md", _PROJECT)

        assert result == "Invoice content"

    def test_content_already_str_returned_as_is(self, mock_drive):
        mock_drive.files.return_value.list.return_value.execute.return_value = {
            "files": [{"id": "file-1", "name": "plain.md"}]
        }
        mock_drive.files.return_value.get_media.return_value.execute.return_value = (
            "Already a string"
        )

        assert read_file("plain.md", _PROJECT) == "Already a string"

    def test_file_not_found_raises(self, mock_drive):
        mock_drive.files.return_value.list.return_value.execute.return_value = {"files": []}

        with pytest.raises(KnowledgeFileNotFoundError):
            read_file("missing/file.md", _PROJECT)

    def test_path_traversal_raises(self, mock_drive):
        # _resolve_path returns None for paths containing ".."
        with pytest.raises(KnowledgeFileNotFoundError):
            read_file("../etc/passwd", _PROJECT)

    def test_drive_500_raises_read_error(self, mock_drive, no_sleep):
        mock_drive.files.return_value.list.return_value.execute.return_value = {
            "files": [{"id": "file-1", "name": "doc.md"}]
        }
        mock_drive.files.return_value.get_media.return_value.execute.side_effect = _http_error(500)

        with pytest.raises(DriveReadError):
            read_file("doc.md", _PROJECT)

    def test_drive_403_raises_permission_error(self, mock_drive):
        mock_drive.files.return_value.list.return_value.execute.return_value = {
            "files": [{"id": "file-1", "name": "doc.md"}]
        }
        mock_drive.files.return_value.get_media.return_value.execute.side_effect = _http_error(403)

        with pytest.raises(DrivePermissionError):
            read_file("doc.md", _PROJECT)

    def test_no_drive_folder_configured_raises(self, tmp_path):
        yaml_no_folder = """\
gcp:
  project_id: test-project
  region: us-central1
sheet:
  workbook_id: s123
models:
  LOCAL_MODEL: ollama/llama3
  FAST_MODEL: gemini-2.0-flash
  DEEP_MODEL: gemini-2.0-pro
  LOCAL_MODEL_FALLBACK: gemini-2.0-flash
  LOCAL_MODEL_TIMEOUT_SECONDS: 2
projects:
  test-project:
    sheet_id: s123
"""
        cfg = tmp_path / "no_folder.yaml"
        cfg.write_text(yaml_no_folder)
        config._reset_for_testing()
        config.load_settings(cfg)

        with (
            patch("google.auth.default", return_value=(MagicMock(), "test-project")),
            patch("tools.drive.build", return_value=MagicMock()),
            pytest.raises(KnowledgeFolderNotFoundError),
        ):
            read_file("doc.md", _PROJECT)

        config._reset_for_testing()


# ── write_file ────────────────────────────────────────────────────────────────


class TestWriteFile:
    def test_creates_new_file(self, mock_drive):
        # Call 1: _ensure_folder_path finds "procedures"
        # Call 2: _resolve_path finds no existing "doc.md"
        mock_drive.files.return_value.list.return_value.execute.side_effect = [
            {"files": [{"id": "folder-1"}]},
            {"files": []},
        ]
        mock_drive.files.return_value.create.return_value.execute.return_value = {
            "id": "new-file-id"
        }

        result = write_file("procedures/doc.md", "# Doc", _PROJECT)

        assert result == "new-file-id"
        mock_drive.files.return_value.create.assert_called_once()

    def test_updates_existing_file(self, mock_drive):
        # Call 1: _ensure_folder_path finds folder
        # Call 2: _resolve_path finds existing file
        mock_drive.files.return_value.list.return_value.execute.side_effect = [
            {"files": [{"id": "folder-1"}]},
            {"files": [{"id": "existing-id", "name": "doc.md"}]},
        ]
        mock_drive.files.return_value.update.return_value.execute.return_value = {
            "id": "existing-id"
        }

        result = write_file("procedures/doc.md", "# Updated", _PROJECT)

        assert result == "existing-id"
        mock_drive.files.return_value.update.assert_called_once()

    def test_write_error_on_500(self, mock_drive, no_sleep):
        # Top-level file: no _ensure_folder_path, just _resolve_path then create
        mock_drive.files.return_value.list.return_value.execute.return_value = {"files": []}
        mock_drive.files.return_value.create.return_value.execute.side_effect = _http_error(500)

        with pytest.raises(DriveWriteError):
            write_file("top_level.md", "content", _PROJECT)

    def test_permission_error_on_403(self, mock_drive):
        mock_drive.files.return_value.list.return_value.execute.return_value = {"files": []}
        mock_drive.files.return_value.create.return_value.execute.side_effect = _http_error(403)

        with pytest.raises(DrivePermissionError):
            write_file("top_level.md", "content", _PROJECT)

    def test_empty_content_written_successfully(self, mock_drive):
        mock_drive.files.return_value.list.return_value.execute.return_value = {"files": []}
        mock_drive.files.return_value.create.return_value.execute.return_value = {
            "id": "empty-file-id"
        }

        result = write_file("empty.md", "", _PROJECT)

        assert result == "empty-file-id"


# ── copy_file ─────────────────────────────────────────────────────────────────


class TestCopyFile:
    def test_happy_path(self, mock_drive):
        # Call 1: _resolve_path source "doc.md" → found
        # Call 2: _ensure_folder_path dest "archive" → found
        mock_drive.files.return_value.list.return_value.execute.side_effect = [
            {"files": [{"id": "src-file", "name": "doc.md"}]},
            {"files": [{"id": "archive-folder"}]},
        ]
        mock_drive.files.return_value.copy.return_value.execute.return_value = {"id": "copied-id"}

        result = copy_file("doc.md", "archive/doc.md", _PROJECT)

        assert result == "copied-id"

    def test_source_not_found_raises(self, mock_drive):
        mock_drive.files.return_value.list.return_value.execute.return_value = {"files": []}

        with pytest.raises(KnowledgeFileNotFoundError):
            copy_file("missing.md", "archive/missing.md", _PROJECT)

    def test_copy_error_on_500(self, mock_drive, no_sleep):
        # Source found; dest uses root (no subfolder)
        mock_drive.files.return_value.list.return_value.execute.return_value = {
            "files": [{"id": "src-file", "name": "doc.md"}]
        }
        mock_drive.files.return_value.copy.return_value.execute.side_effect = _http_error(500)

        with pytest.raises(DriveWriteError):
            copy_file("doc.md", "doc_copy.md", _PROJECT)

    def test_copy_error_on_403_raises_permission_error(self, mock_drive):
        mock_drive.files.return_value.list.return_value.execute.return_value = {
            "files": [{"id": "src-file", "name": "doc.md"}]
        }
        mock_drive.files.return_value.copy.return_value.execute.side_effect = _http_error(403)

        with pytest.raises(DrivePermissionError):
            copy_file("doc.md", "doc_copy.md", _PROJECT)


# ── list_folder ───────────────────────────────────────────────────────────────


class TestListFolder:
    def test_happy_path_returns_relative_paths(self, mock_drive):
        # Call 1: _resolve_path "procedures" → folder-1
        # Call 2: _collect_files contents of folder-1 (no nextPageToken → 1 page)
        mock_drive.files.return_value.list.return_value.execute.side_effect = [
            {"files": [{"id": "folder-1", "name": "procedures"}]},
            {
                "files": [
                    {"id": "f1", "name": "invoice.md", "mimeType": "text/plain"},
                    {"id": "f2", "name": "lead.md", "mimeType": "text/plain"},
                ]
            },
        ]

        result = list_folder("procedures", _PROJECT)

        assert "procedures/invoice.md" in result
        assert "procedures/lead.md" in result

    def test_folder_not_found_raises(self, mock_drive):
        mock_drive.files.return_value.list.return_value.execute.return_value = {"files": []}

        with pytest.raises(KnowledgeFolderNotFoundError):
            list_folder("nonexistent", _PROJECT)

    def test_empty_folder_returns_empty_list(self, mock_drive):
        mock_drive.files.return_value.list.return_value.execute.side_effect = [
            {"files": [{"id": "folder-1", "name": "empty_dir"}]},
            {"files": []},
        ]

        assert list_folder("empty_dir", _PROJECT) == []

    def test_permission_error_in_collect_raises(self, mock_drive):
        # _resolve_path succeeds; _collect_files hits 403 → DrivePermissionError
        mock_drive.files.return_value.list.return_value.execute.side_effect = [
            {"files": [{"id": "folder-1", "name": "procedures"}]},
            _http_error(403),
        ]

        with pytest.raises(DrivePermissionError):
            list_folder("procedures", _PROJECT)

    def test_subfolder_files_included_recursively(self, mock_drive):
        # procedures/ contains a subfolder "invoices/" which contains "jan.md"
        mock_drive.files.return_value.list.return_value.execute.side_effect = [
            {"files": [{"id": "folder-1", "name": "procedures"}]},  # _resolve_path
            {  # list procedures/ → one subfolder
                "files": [
                    {
                        "id": "sub-1",
                        "name": "invoices",
                        "mimeType": "application/vnd.google-apps.folder",
                    }
                ]
            },
            {  # list invoices/ → one file
                "files": [{"id": "f1", "name": "jan.md", "mimeType": "text/plain"}]
            },
        ]

        result = list_folder("procedures", _PROJECT)

        assert "procedures/invoices/jan.md" in result


# ── move_file ─────────────────────────────────────────────────────────────────


class TestMoveFile:
    """move_file(source_path, dest_folder_path, project_id, new_name=None)"""

    def test_happy_path_moves_and_returns_id(self, mock_drive):
        # _resolve_path for source (2 list calls: root → Inbound → photo.jpg)
        # _ensure_folder_path for dest (1 list call: Knowledge → Pictures exists)
        # files().get() for current parents
        # files().update() for the move
        mock_drive.files.return_value.list.return_value.execute.side_effect = [
            {"files": [{"id": "inbound-id", "name": "Inbound"}]},
            {"files": [{"id": "file-id", "name": "photo.jpg"}]},
            {"files": [{"id": "knowledge-id", "name": "Knowledge"}]},
            {"files": [{"id": "pictures-id", "name": "Pictures"}]},
        ]
        mock_drive.files.return_value.get.return_value.execute.return_value = {
            "parents": ["inbound-id"],
            "name": "photo.jpg",
        }
        mock_drive.files.return_value.update.return_value.execute.return_value = {"id": "file-id"}

        result = move_file("Inbound/photo.jpg", "Knowledge/Pictures", _PROJECT)

        assert result == "file-id"
        update_call = mock_drive.files.return_value.update.call_args
        assert update_call.kwargs["addParents"] == "pictures-id"
        assert update_call.kwargs["removeParents"] == "inbound-id"

    def test_rename_on_move(self, mock_drive):
        mock_drive.files.return_value.list.return_value.execute.side_effect = [
            {"files": [{"id": "inbound-id", "name": "Inbound"}]},
            {"files": [{"id": "file-id", "name": "photo.jpg"}]},
            {"files": [{"id": "knowledge-id", "name": "Knowledge"}]},
            {"files": [{"id": "pictures-id", "name": "Pictures"}]},
        ]
        mock_drive.files.return_value.get.return_value.execute.return_value = {
            "parents": ["inbound-id"],
            "name": "photo.jpg",
        }
        mock_drive.files.return_value.update.return_value.execute.return_value = {"id": "file-id"}

        move_file(
            "Inbound/photo.jpg", "Knowledge/Pictures", _PROJECT, new_name="2026-03-06_photo.jpg"
        )

        update_call = mock_drive.files.return_value.update.call_args
        assert update_call.kwargs["body"]["name"] == "2026-03-06_photo.jpg"

    def test_source_not_found_raises(self, mock_drive):
        mock_drive.files.return_value.list.return_value.execute.return_value = {"files": []}

        with pytest.raises(KnowledgeFileNotFoundError):
            move_file("Inbound/missing.jpg", "Knowledge/Pictures", _PROJECT)

    def test_drive_api_error_raises_write_error(self, mock_drive, no_sleep):
        mock_drive.files.return_value.list.return_value.execute.side_effect = [
            {"files": [{"id": "inbound-id"}]},
            {"files": [{"id": "file-id"}]},
            {"files": [{"id": "knowledge-id"}]},
            {"files": [{"id": "pictures-id"}]},
        ]
        mock_drive.files.return_value.get.return_value.execute.return_value = {
            "parents": ["inbound-id"],
            "name": "photo.jpg",
        }
        mock_drive.files.return_value.update.return_value.execute.side_effect = [
            _http_error(500),
            _http_error(500),
            _http_error(500),
        ]

        with pytest.raises(DriveWriteError):
            move_file("Inbound/photo.jpg", "Knowledge/Pictures", _PROJECT)

    def test_permission_error_on_move_raises(self, mock_drive):
        mock_drive.files.return_value.list.return_value.execute.side_effect = [
            {"files": [{"id": "inbound-id"}]},
            {"files": [{"id": "file-id"}]},
            {"files": [{"id": "knowledge-id"}]},
            {"files": [{"id": "pictures-id"}]},
        ]
        mock_drive.files.return_value.get.return_value.execute.return_value = {
            "parents": ["inbound-id"],
            "name": "photo.jpg",
        }
        mock_drive.files.return_value.update.return_value.execute.side_effect = _http_error(403)

        with pytest.raises(DrivePermissionError):
            move_file("Inbound/photo.jpg", "Knowledge/Pictures", _PROJECT)

    def test_dest_folder_created_when_missing(self, mock_drive):
        # _resolve_path succeeds, _ensure_folder_path must create missing subfolder
        mock_drive.files.return_value.list.return_value.execute.side_effect = [
            {"files": [{"id": "inbound-id"}]},
            {"files": [{"id": "file-id"}]},
            {"files": [{"id": "knowledge-id"}]},
            {"files": []},  # "NewFolder" does not exist yet → create it
        ]
        mock_drive.files.return_value.create.return_value.execute.return_value = {
            "id": "new-folder-id"
        }
        mock_drive.files.return_value.get.return_value.execute.return_value = {
            "parents": ["inbound-id"],
            "name": "report.docx",
        }
        mock_drive.files.return_value.update.return_value.execute.return_value = {"id": "file-id"}

        result = move_file("Inbound/report.docx", "Knowledge/NewFolder", _PROJECT)

        assert result == "file-id"
        # verify create was called for the missing folder
        mock_drive.files.return_value.create.assert_called_once()
