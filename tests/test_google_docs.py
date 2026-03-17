"""tests/test_google_docs.py — Unit tests for tools/google_docs.py"""
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from tools.google_docs import (
    DocumentNotFoundError,
    DocsApiError,
    _extract_text,
    append_content,
    create_document,
    list_comments,
    read_document,
)

# ── Settings fixtures ──────────────────────────────────────────────────────────

_BASE_YAML = """\
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

_YAML_WITH_DOCS = _BASE_YAML + """\
docs:
  service_account_key: ""
  blueprints_folder_id: blueprints-folder-xyz
"""

_YAML_WITH_SA_KEY = _BASE_YAML + """\
docs:
  service_account_key: /fake/path/key.json
  blueprints_folder_id: ""
"""


@pytest.fixture()
def settings_with_docs(tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(_YAML_WITH_DOCS)
    import config
    config._reset_for_testing()
    config.load_settings(cfg)
    yield
    config._reset_for_testing()


@pytest.fixture()
def settings_base(tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(_BASE_YAML)
    import config
    config._reset_for_testing()
    config.load_settings(cfg)
    yield
    config._reset_for_testing()


@pytest.fixture()
def settings_with_sa_key(tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(_YAML_WITH_SA_KEY)
    import config
    config._reset_for_testing()
    config.load_settings(cfg)
    yield
    config._reset_for_testing()


# ── Helpers ────────────────────────────────────────────────────────────────────


def _http_error(status: int) -> HttpError:
    """Build a minimal HttpError with the given HTTP status code."""
    resp = MagicMock()
    resp.status = status
    return HttpError(resp=resp, content=b"error")


def _mock_docs_svc(doc_id: str = "doc-abc", title: str = "Blueprint") -> MagicMock:
    """Return a MagicMock wired to return a minimal Docs API response."""
    svc = MagicMock()
    svc.documents.return_value.create.return_value.execute.return_value = {
        "documentId": doc_id,
        "title": title,
    }
    svc.documents.return_value.get.return_value.execute.return_value = {
        "documentId": doc_id,
        "title": title,
        "body": {
            "content": [
                {
                    "paragraph": {
                        "elements": [{"textRun": {"content": "Hello world\n"}}]
                    },
                    "endIndex": 12,
                },
                {"endIndex": 13},
            ]
        },
    }
    svc.documents.return_value.batchUpdate.return_value.execute.return_value = {}
    return svc


def _mock_drive_svc() -> MagicMock:
    """Return a MagicMock wired to return minimal Drive API responses."""
    svc = MagicMock()
    svc.files.return_value.update.return_value.execute.return_value = {
        "id": "doc-abc",
        "parents": ["blueprints-folder-xyz"],
    }
    svc.comments.return_value.list.return_value.execute.return_value = {
        "comments": [
            {
                "id": "comment-1",
                "content": "Fix the budget section",
                "author": {"displayName": "Alice"},
                "createdTime": "2026-03-16T10:00:00Z",
                "resolved": False,
            }
        ]
    }
    return svc


# ── _extract_text ──────────────────────────────────────────────────────────────


class TestExtractText:
    def test_extracts_text_from_paragraphs(self):
        doc = {
            "body": {
                "content": [
                    {
                        "paragraph": {
                            "elements": [
                                {"textRun": {"content": "Hello "}},
                                {"textRun": {"content": "world\n"}},
                            ]
                        }
                    }
                ]
            }
        }
        assert _extract_text(doc) == "Hello world\n"

    def test_skips_non_paragraph_elements(self):
        doc = {
            "body": {
                "content": [
                    {"sectionBreak": {}},
                    {
                        "paragraph": {
                            "elements": [{"textRun": {"content": "Line\n"}}]
                        }
                    },
                ]
            }
        }
        assert _extract_text(doc) == "Line\n"

    def test_returns_empty_string_for_empty_body(self):
        assert _extract_text({}) == ""
        assert _extract_text({"body": {"content": []}}) == ""

    def test_skips_elements_without_text_run(self):
        doc = {
            "body": {
                "content": [
                    {
                        "paragraph": {
                            "elements": [
                                {"inlineObjectElement": {}},
                                {"textRun": {"content": "Text"}},
                            ]
                        }
                    }
                ]
            }
        }
        assert _extract_text(doc) == "Text"


# ── create_document ────────────────────────────────────────────────────────────


class TestCreateDocument:
    def test_returns_doc_id_on_success(self, settings_base):
        docs_svc = _mock_docs_svc(doc_id="new-doc-id")
        with (
            patch("tools.google_docs._get_docs_service", return_value=docs_svc),
            patch("tools.google_docs._get_drive_service"),
        ):
            result = create_document("My Blueprint", "proj-1")
        assert result == "new-doc-id"

    def test_moves_to_folder_when_folder_id_provided(self, settings_base):
        docs_svc = _mock_docs_svc()
        drive_svc = _mock_drive_svc()
        with (
            patch("tools.google_docs._get_docs_service", return_value=docs_svc),
            patch("tools.google_docs._get_drive_service", return_value=drive_svc),
        ):
            create_document("Blueprint", "proj-1", folder_id="explicit-folder")
        drive_svc.files.return_value.update.assert_called_once()
        call_kwargs = drive_svc.files.return_value.update.call_args.kwargs
        assert call_kwargs["addParents"] == "explicit-folder"

    def test_uses_settings_blueprints_folder_when_no_folder_id(self, settings_with_docs):
        docs_svc = _mock_docs_svc()
        drive_svc = _mock_drive_svc()
        with (
            patch("tools.google_docs._get_docs_service", return_value=docs_svc),
            patch("tools.google_docs._get_drive_service", return_value=drive_svc),
        ):
            create_document("Blueprint", "proj-1")
        drive_svc.files.return_value.update.assert_called_once()
        call_kwargs = drive_svc.files.return_value.update.call_args.kwargs
        assert call_kwargs["addParents"] == "blueprints-folder-xyz"

    def test_no_folder_move_when_no_folder_configured(self, settings_base):
        docs_svc = _mock_docs_svc()
        drive_svc = _mock_drive_svc()
        with (
            patch("tools.google_docs._get_docs_service", return_value=docs_svc),
            patch("tools.google_docs._get_drive_service", return_value=drive_svc),
        ):
            create_document("Blueprint", "proj-1")
        drive_svc.files.return_value.update.assert_not_called()

    def test_inserts_initial_content_when_provided(self, settings_base):
        docs_svc = _mock_docs_svc()
        with (
            patch("tools.google_docs._get_docs_service", return_value=docs_svc),
            patch("tools.google_docs._get_drive_service"),
        ):
            create_document("Blueprint", "proj-1", initial_content="# Objective\n...")
        docs_svc.documents.return_value.batchUpdate.assert_called_once()

    def test_raises_value_error_for_empty_title(self, settings_base):
        with pytest.raises(ValueError, match="non-empty"):
            create_document("", "proj-1")

    def test_raises_docs_api_error_on_http_failure(self, settings_base):
        docs_svc = MagicMock()
        docs_svc.documents.return_value.create.return_value.execute.side_effect = _http_error(500)
        with (
            patch("tools.google_docs._get_docs_service", return_value=docs_svc),
            patch("tools.google_docs._get_drive_service"),
        ):
            with pytest.raises(DocsApiError, match="create_document"):
                create_document("Blueprint", "proj-1")


# ── read_document ──────────────────────────────────────────────────────────────


class TestReadDocument:
    def test_returns_text_content(self, settings_base):
        docs_svc = _mock_docs_svc()
        with patch("tools.google_docs._get_docs_service", return_value=docs_svc):
            text = read_document("doc-abc", "proj-1")
        assert text == "Hello world\n"

    def test_raises_document_not_found_on_404(self, settings_base):
        docs_svc = MagicMock()
        docs_svc.documents.return_value.get.return_value.execute.side_effect = _http_error(404)
        with patch("tools.google_docs._get_docs_service", return_value=docs_svc):
            with pytest.raises(DocumentNotFoundError):
                read_document("missing-doc", "proj-1")

    def test_raises_docs_api_error_on_non_404_http_failure(self, settings_base):
        docs_svc = MagicMock()
        docs_svc.documents.return_value.get.return_value.execute.side_effect = _http_error(500)
        with patch("tools.google_docs._get_docs_service", return_value=docs_svc):
            with pytest.raises(DocsApiError):
                read_document("doc-abc", "proj-1")

    def test_raises_value_error_for_empty_doc_id(self, settings_base):
        with pytest.raises(ValueError, match="non-empty"):
            read_document("", "proj-1")

    def test_returns_empty_string_for_doc_with_no_text(self, settings_base):
        docs_svc = MagicMock()
        docs_svc.documents.return_value.get.return_value.execute.return_value = {
            "documentId": "empty-doc",
            "body": {"content": []},
        }
        with patch("tools.google_docs._get_docs_service", return_value=docs_svc):
            text = read_document("empty-doc", "proj-1")
        assert text == ""


# ── append_content ─────────────────────────────────────────────────────────────


class TestAppendContent:
    def test_appends_text_at_correct_index(self, settings_base):
        docs_svc = _mock_docs_svc()
        with patch("tools.google_docs._get_docs_service", return_value=docs_svc):
            append_content("doc-abc", "New paragraph\n", "proj-1")
        # batchUpdate should have been called once (for the insertText)
        docs_svc.documents.return_value.batchUpdate.assert_called_once()
        requests = docs_svc.documents.return_value.batchUpdate.call_args.kwargs["body"]["requests"]
        assert requests[0]["insertText"]["text"] == "New paragraph\n"
        assert requests[0]["insertText"]["location"]["index"] == 12  # endIndex(13) - 1

    def test_no_op_when_content_is_empty(self, settings_base):
        docs_svc = _mock_docs_svc()
        with patch("tools.google_docs._get_docs_service", return_value=docs_svc):
            append_content("doc-abc", "", "proj-1")
        docs_svc.documents.return_value.batchUpdate.assert_not_called()

    def test_raises_document_not_found_on_404(self, settings_base):
        docs_svc = MagicMock()
        docs_svc.documents.return_value.get.return_value.execute.side_effect = _http_error(404)
        with patch("tools.google_docs._get_docs_service", return_value=docs_svc):
            with pytest.raises(DocumentNotFoundError):
                append_content("missing-doc", "content", "proj-1")

    def test_raises_docs_api_error_on_api_failure(self, settings_base):
        docs_svc = MagicMock()
        docs_svc.documents.return_value.get.return_value.execute.side_effect = _http_error(500)
        with patch("tools.google_docs._get_docs_service", return_value=docs_svc):
            with pytest.raises(DocsApiError):
                append_content("doc-abc", "text", "proj-1")

    def test_raises_value_error_for_empty_doc_id(self, settings_base):
        with pytest.raises(ValueError, match="non-empty"):
            append_content("", "text", "proj-1")


# ── list_comments ──────────────────────────────────────────────────────────────


class TestListComments:
    def test_returns_list_of_comment_dicts(self, settings_base):
        drive_svc = _mock_drive_svc()
        with patch("tools.google_docs._get_drive_service", return_value=drive_svc):
            comments = list_comments("doc-abc", "proj-1")
        assert len(comments) == 1
        c = comments[0]
        assert c["id"] == "comment-1"
        assert c["content"] == "Fix the budget section"
        assert c["author"] == "Alice"
        assert c["created_at"] == "2026-03-16T10:00:00Z"
        assert c["resolved"] is False

    def test_returns_empty_list_when_no_comments(self, settings_base):
        drive_svc = MagicMock()
        drive_svc.comments.return_value.list.return_value.execute.return_value = {"comments": []}
        with patch("tools.google_docs._get_drive_service", return_value=drive_svc):
            comments = list_comments("doc-abc", "proj-1")
        assert comments == []

    def test_returns_empty_list_when_key_missing_from_response(self, settings_base):
        drive_svc = MagicMock()
        drive_svc.comments.return_value.list.return_value.execute.return_value = {}
        with patch("tools.google_docs._get_drive_service", return_value=drive_svc):
            comments = list_comments("doc-abc", "proj-1")
        assert comments == []

    def test_raises_document_not_found_on_404(self, settings_base):
        drive_svc = MagicMock()
        drive_svc.comments.return_value.list.return_value.execute.side_effect = _http_error(404)
        with patch("tools.google_docs._get_drive_service", return_value=drive_svc):
            with pytest.raises(DocumentNotFoundError):
                list_comments("missing-doc", "proj-1")

    def test_raises_docs_api_error_on_api_failure(self, settings_base):
        drive_svc = MagicMock()
        drive_svc.comments.return_value.list.return_value.execute.side_effect = _http_error(403)
        with patch("tools.google_docs._get_drive_service", return_value=drive_svc):
            with pytest.raises(DocsApiError, match="list_comments"):
                list_comments("doc-abc", "proj-1")

    def test_raises_value_error_for_empty_doc_id(self, settings_base):
        with pytest.raises(ValueError, match="non-empty"):
            list_comments("", "proj-1")


# ── Credential loading ─────────────────────────────────────────────────────────


class TestCredentials:
    def test_uses_service_account_key_when_configured(self, settings_with_sa_key):
        with (
            patch(
                "tools.google_docs.service_account.Credentials.from_service_account_file"
            ) as mock_sa,
            patch("tools.google_docs.build") as mock_build,
        ):
            mock_sa.return_value = MagicMock()
            mock_build.return_value = MagicMock()
            from tools.google_docs import _get_docs_service, _DOCS_SCOPES
            _get_docs_service()
        mock_sa.assert_called_once_with("/fake/path/key.json", scopes=_DOCS_SCOPES)

    def test_falls_back_to_adc_when_no_key_configured(self, settings_base):
        with (
            patch("google.auth.default") as mock_adc,
            patch("tools.google_docs.build") as mock_build,
        ):
            from tools.google_docs import _get_docs_service
            mock_adc.return_value = (MagicMock(), None)
            mock_build.return_value = MagicMock()
            _get_docs_service()
        mock_adc.assert_called_once()
