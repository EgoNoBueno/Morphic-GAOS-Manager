"""tests/test_bigquery.py — Unit tests for tools/bigquery.py"""

from unittest.mock import MagicMock, patch

import pytest
from google.api_core.exceptions import GoogleAPICallError

from tools.bigquery import (
    BigQueryInsertError,
    BigQueryRowError,
    _full_table_ref,
    insert_row,
    insert_rows,
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


@pytest.fixture(autouse=True)
def load_test_settings(tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(SETTINGS_YAML)
    import config

    config._reset_for_testing()
    config.load_settings(cfg)
    yield
    config._reset_for_testing()


# ── _full_table_ref ────────────────────────────────────────────────────────


class TestFullTableRef:
    def test_unqualified_ref_is_prefixed_with_project(self):
        assert _full_table_ref("dataset.table", "my-project") == "my-project.dataset.table"

    def test_fully_qualified_ref_is_unchanged(self):
        assert _full_table_ref("other.dataset.table", "my-project") == "other.dataset.table"

    def test_two_dot_ref_is_treated_as_qualified(self):
        ref = "proj.ds.tbl"
        assert _full_table_ref(ref, "ignored") == ref


# ── insert_row ─────────────────────────────────────────────────────────────


class TestInsertRow:
    def test_happy_path_calls_insert_rows_json_once(self):
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = []

        with patch("tools.bigquery.bigquery.Client", return_value=mock_client):
            insert_row("dataset.my_table", {"col": "val"})

        mock_client.insert_rows_json.assert_called_once_with(
            "test-project.dataset.my_table", [{"col": "val"}]
        )

    def test_raises_bigquery_row_error_when_rows_rejected(self):
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = [{"index": 0, "errors": ["bad value"]}]

        with patch("tools.bigquery.bigquery.Client", return_value=mock_client):
            with pytest.raises(BigQueryRowError, match="rejected row"):
                insert_row("dataset.my_table", {"col": "bad"})

    def test_raises_bigquery_insert_error_after_three_api_failures(self):
        mock_client = MagicMock()
        mock_client.insert_rows_json.side_effect = GoogleAPICallError("gateway timeout")

        with patch("tools.bigquery.bigquery.Client", return_value=mock_client):
            with patch("tools.bigquery.time.sleep"):  # skip backoff
                with pytest.raises(BigQueryInsertError, match="3 retries"):
                    insert_row("dataset.my_table", {"col": "val"})

        assert mock_client.insert_rows_json.call_count == 3

    def test_row_error_is_not_retried(self):
        """Schema / value rejection is not transient — never retry."""
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = [{"errors": ["schema mismatch"]}]

        with patch("tools.bigquery.bigquery.Client", return_value=mock_client):
            with pytest.raises(BigQueryRowError):
                insert_row("dataset.my_table", {"col": "val"})

        assert mock_client.insert_rows_json.call_count == 1

    def test_project_id_kwarg_is_ignored_gcp_project_from_settings_used(self):
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = []

        with patch("tools.bigquery.bigquery.Client", return_value=mock_client) as MockCls:
            insert_row("dataset.tbl", {"k": "v"}, project_id="caller-project")

        # Client is always constructed with the GCP project from settings
        MockCls.assert_called_once_with(project="test-project")


# ── insert_rows ────────────────────────────────────────────────────────────


class TestInsertRows:
    def test_empty_list_is_a_no_op(self):
        with patch("tools.bigquery.bigquery.Client") as MockClient:
            insert_rows("dataset.my_table", [])

        MockClient.assert_not_called()

    def test_batch_insert_calls_insert_rows_json_with_all_rows(self):
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = []
        rows = [{"a": 1}, {"a": 2}]

        with patch("tools.bigquery.bigquery.Client", return_value=mock_client):
            insert_rows("dataset.my_table", rows)

        mock_client.insert_rows_json.assert_called_once_with("test-project.dataset.my_table", rows)

    def test_batch_raises_bigquery_row_error_on_rejection(self):
        mock_client = MagicMock()
        mock_client.insert_rows_json.return_value = [{"errors": ["bad"]}]

        with patch("tools.bigquery.bigquery.Client", return_value=mock_client):
            with pytest.raises(BigQueryRowError):
                insert_rows("dataset.my_table", [{"a": 1}])

    def test_batch_raises_insert_error_after_three_api_failures(self):
        mock_client = MagicMock()
        mock_client.insert_rows_json.side_effect = GoogleAPICallError("timeout")

        with patch("tools.bigquery.bigquery.Client", return_value=mock_client):
            with patch("tools.bigquery.time.sleep"):
                with pytest.raises(BigQueryInsertError):
                    insert_rows("dataset.my_table", [{"a": 1}])

        assert mock_client.insert_rows_json.call_count == 3
