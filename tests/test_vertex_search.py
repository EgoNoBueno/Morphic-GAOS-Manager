"""tests/test_vertex_search.py — Unit tests for tools/vertex_search.py"""

import sys
from unittest.mock import MagicMock, patch

import pytest

import config
from tools.vertex_search import (
    DatastoreNotConfiguredError,
    VertexSearchError,
    _extract_result,
    _serving_config,
    query_domain_knowledge,
    query_playbooks,
    search_knowledge,
)

# ── Settings fixtures ─────────────────────────────────────────────────────────

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

_YAML_WITH_VS = (
    _BASE_YAML
    + """\
vertex_search:
  location: global
  playbook_datastore_id: playbook-ds-001
  knowledge_datastore_id: knowledge-ds-001
"""
)


@pytest.fixture()
def settings_with_vs(tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(_YAML_WITH_VS)
    config._reset_for_testing()
    config.load_settings(cfg)
    yield
    config._reset_for_testing()


@pytest.fixture()
def settings_no_vs(tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(_BASE_YAML)
    config._reset_for_testing()
    config.load_settings(cfg)
    yield
    config._reset_for_testing()


# ── _serving_config ───────────────────────────────────────────────────────────


class TestServingConfig:
    def test_builds_correct_resource_path(self):
        result = _serving_config("my-project", "global", "my-ds")
        assert result == (
            "projects/my-project/locations/global"
            "/collections/default_collection/dataStores/my-ds"
            "/servingConfigs/default_config"
        )

    def test_uses_provided_location(self):
        result = _serving_config("proj", "us-central1", "ds123")
        assert "locations/us-central1" in result

    def test_embeds_datastore_id(self):
        result = _serving_config("proj", "global", "my-custom-ds")
        assert "dataStores/my-custom-ds" in result


# ── _extract_result ───────────────────────────────────────────────────────────


class TestExtractResult:
    def test_returns_expected_keys(self):
        mock_doc = MagicMock()
        mock_doc.id = "doc-1"
        mock_doc.derived_struct_data = None
        mock_result = MagicMock()
        mock_result.document = mock_doc

        extracted = _extract_result(mock_result)
        assert set(extracted.keys()) == {"id", "title", "snippet", "link"}

    def test_extracts_id(self):
        mock_doc = MagicMock()
        mock_doc.id = "doc-xyz"
        mock_doc.derived_struct_data = None
        mock_result = MagicMock()
        mock_result.document = mock_doc

        assert _extract_result(mock_result)["id"] == "doc-xyz"

    def test_returns_empty_strings_when_no_struct_data(self):
        mock_doc = MagicMock()
        mock_doc.id = "doc-2"
        mock_doc.derived_struct_data = None
        mock_result = MagicMock()
        mock_result.document = mock_doc

        result = _extract_result(mock_result)
        assert result["title"] == ""
        assert result["snippet"] == ""
        assert result["link"] == ""


# ── search_knowledge ──────────────────────────────────────────────────────────


class TestSearchKnowledge:
    def test_returns_empty_list_for_blank_query(self, settings_with_vs):
        assert search_knowledge("", "test-project", "some-ds") == []

    def test_returns_empty_list_for_whitespace_query(self, settings_with_vs):
        assert search_knowledge("   ", "test-project", "some-ds") == []

    def test_raises_when_datastore_id_empty(self, settings_with_vs):
        with pytest.raises(DatastoreNotConfiguredError):
            search_knowledge("find playbook", "test-project", "")

    def test_raises_vertex_search_error_on_api_failure(self, settings_with_vs):
        mock_client = MagicMock()
        mock_client.search.side_effect = Exception("Vertex unreachable")
        mock_de = MagicMock()
        mock_de.SearchServiceClient.return_value = mock_client
        mock_de.SearchRequest = MagicMock(return_value=MagicMock())

        with patch.dict(sys.modules, {"google.cloud.discoveryengine_v1": mock_de}):
            with pytest.raises(VertexSearchError, match="search_knowledge failed"):
                search_knowledge("test query", "test-project", "some-ds")

    def test_returns_list_of_dicts_on_success(self, settings_with_vs):
        mock_doc = MagicMock()
        mock_doc.id = "doc-123"
        mock_doc.derived_struct_data = None
        mock_result = MagicMock()
        mock_result.document = mock_doc
        mock_client = MagicMock()
        mock_client.search.return_value = [mock_result]
        mock_de = MagicMock()
        mock_de.SearchServiceClient.return_value = mock_client
        mock_de.SearchRequest = MagicMock(return_value=MagicMock())

        with patch.dict(sys.modules, {"google.cloud.discoveryengine_v1": mock_de}):
            results = search_knowledge("invoice automation", "test-project", "ds-001")

        assert len(results) == 1
        assert results[0]["id"] == "doc-123"

    def test_passes_query_to_search_request(self, settings_with_vs):
        mock_client = MagicMock()
        mock_client.search.return_value = []
        mock_de = MagicMock()
        mock_de.SearchServiceClient.return_value = mock_client
        captured: dict = {}

        def capture(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        mock_de.SearchRequest = MagicMock(side_effect=capture)

        with patch.dict(sys.modules, {"google.cloud.discoveryengine_v1": mock_de}):
            search_knowledge("test query", "test-project", "ds-001")

        assert captured.get("query") == "test query"

    def test_passes_page_size_to_search_request(self, settings_with_vs):
        mock_client = MagicMock()
        mock_client.search.return_value = []
        mock_de = MagicMock()
        mock_de.SearchServiceClient.return_value = mock_client
        captured: dict = {}

        def capture(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        mock_de.SearchRequest = MagicMock(side_effect=capture)

        with patch.dict(sys.modules, {"google.cloud.discoveryengine_v1": mock_de}):
            search_knowledge("query", "test-project", "ds-001", max_results=3)

        assert captured.get("page_size") == 3

    def test_strips_whitespace_from_query(self, settings_with_vs):
        mock_client = MagicMock()
        mock_client.search.return_value = []
        mock_de = MagicMock()
        mock_de.SearchServiceClient.return_value = mock_client
        captured: dict = {}

        def capture(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        mock_de.SearchRequest = MagicMock(side_effect=capture)

        with patch.dict(sys.modules, {"google.cloud.discoveryengine_v1": mock_de}):
            search_knowledge("  padded query  ", "test-project", "ds-001")

        assert captured.get("query") == "padded query"

    def test_returns_empty_list_when_api_returns_no_results(self, settings_with_vs):
        mock_client = MagicMock()
        mock_client.search.return_value = []
        mock_de = MagicMock()
        mock_de.SearchServiceClient.return_value = mock_client
        mock_de.SearchRequest = MagicMock(return_value=MagicMock())

        with patch.dict(sys.modules, {"google.cloud.discoveryengine_v1": mock_de}):
            results = search_knowledge("query", "test-project", "ds-001")

        assert results == []


# ── query_playbooks ───────────────────────────────────────────────────────────


class TestQueryPlaybooks:
    def test_raises_when_playbook_datastore_not_set(self, settings_no_vs):
        with pytest.raises(DatastoreNotConfiguredError, match="playbook_datastore_id"):
            query_playbooks("some query", "test-project")

    def test_delegates_with_correct_datastore_id(self, settings_with_vs):
        with patch("tools.vertex_search.search_knowledge") as mock_search:
            mock_search.return_value = [{"id": "p1"}]
            result = query_playbooks("Q3 launch", "test-project")

        mock_search.assert_called_once_with("Q3 launch", "test-project", "playbook-ds-001", 5)
        assert result == [{"id": "p1"}]

    def test_propagates_max_results(self, settings_with_vs):
        with patch("tools.vertex_search.search_knowledge") as mock_search:
            mock_search.return_value = []
            query_playbooks("query", "test-project", max_results=10)

        assert mock_search.call_args[0][3] == 10

    def test_returns_empty_list_on_no_results(self, settings_with_vs):
        with patch("tools.vertex_search.search_knowledge", return_value=[]):
            result = query_playbooks("no results", "test-project")
        assert result == []


# ── query_domain_knowledge ────────────────────────────────────────────────────


class TestQueryDomainKnowledge:
    def test_raises_when_knowledge_datastore_not_set(self, settings_no_vs):
        with pytest.raises(DatastoreNotConfiguredError, match="knowledge_datastore_id"):
            query_domain_knowledge("some query", "test-project")

    def test_delegates_with_correct_datastore_id(self, settings_with_vs):
        with patch("tools.vertex_search.search_knowledge") as mock_search:
            mock_search.return_value = []
            query_domain_knowledge("AP reconciliation", "test-project")

        mock_search.assert_called_once_with(
            "AP reconciliation", "test-project", "knowledge-ds-001", 5
        )

    def test_propagates_max_results(self, settings_with_vs):
        with patch("tools.vertex_search.search_knowledge") as mock_search:
            mock_search.return_value = []
            query_domain_knowledge("test", "test-project", max_results=7)

        assert mock_search.call_args[0][3] == 7
