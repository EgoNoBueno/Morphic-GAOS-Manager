"""tests/test_phoenix.py — Unit tests for tools/phoenix.py"""

from unittest.mock import patch

import pytest

from tools.phoenix import (
    CheckpointCorruptedError,
    _compute_hash,
    _serialize_state,
    load_checkpoint,
    phoenix_recover,
    save_checkpoint,
    validate_state,
)

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

_AGENT_ID = "nexus-prime"
_PROJECT_ID = "test-project"
_VALID_STATE = {"agent_id": _AGENT_ID, "project_id": _PROJECT_ID, "task_id": "abc-123"}


@pytest.fixture(autouse=True)
def load_test_settings(tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(SETTINGS_YAML)
    import config

    config._reset_for_testing()
    config.load_settings(cfg)
    yield
    config._reset_for_testing()


# ── validate_state ────────────────────────────────────────────────────────────


class TestValidateState:
    def test_valid_state_passes(self):
        result = validate_state(_VALID_STATE)
        assert result["valid"] is True
        assert result["reason"] == ""

    def test_missing_agent_id_fails(self):
        result = validate_state({"project_id": _PROJECT_ID})
        assert result["valid"] is False
        assert "agent_id" in result["reason"]

    def test_missing_project_id_fails(self):
        result = validate_state({"agent_id": _AGENT_ID})
        assert result["valid"] is False
        assert "project_id" in result["reason"]

    def test_empty_agent_id_fails(self):
        result = validate_state({"agent_id": "", "project_id": _PROJECT_ID})
        assert result["valid"] is False
        assert "agent_id" in result["reason"]

    def test_non_dict_input_fails(self):
        result = validate_state("not a dict")  # type: ignore[arg-type]
        assert result["valid"] is False
        assert "not a dict" in result["reason"]

    def test_none_input_fails(self):
        result = validate_state(None)  # type: ignore[arg-type]
        assert result["valid"] is False

    def test_oversized_state_fails(self):
        huge = {"agent_id": _AGENT_ID, "project_id": _PROJECT_ID, "blob": "x" * 600_000}
        result = validate_state(huge)
        assert result["valid"] is False
        assert "max size" in result["reason"]

    def test_extra_fields_are_permitted(self):
        state = {**_VALID_STATE, "foo": "bar", "nested": {"deep": True}}
        result = validate_state(state)
        assert result["valid"] is True


# ── save_checkpoint ───────────────────────────────────────────────────────────


class TestSaveCheckpoint:
    def test_save_writes_correct_row_to_bigquery(self):
        with patch("tools.phoenix.insert_row") as mock_insert:
            returned_hash = save_checkpoint(_AGENT_ID, _PROJECT_ID, _VALID_STATE)

        mock_insert.assert_called_once()
        call_args = mock_insert.call_args[0]
        assert call_args[0] == "aos_logs.agent_checkpoints"
        row = call_args[1]
        assert row["agent_id"] == _AGENT_ID
        assert row["project_id"] == _PROJECT_ID
        assert row["is_valid"] is True
        assert row["checkpoint_hash"] == returned_hash

    def test_save_returns_sha256_hexdigest(self):
        with patch("tools.phoenix.insert_row"):
            result = save_checkpoint(_AGENT_ID, _PROJECT_ID, _VALID_STATE)
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_save_rejects_corrupted_state_before_writing(self):
        with patch("tools.phoenix.insert_row") as mock_insert:
            with pytest.raises(CheckpointCorruptedError, match="validation"):
                save_checkpoint(_AGENT_ID, _PROJECT_ID, {"agent_id": ""})
        mock_insert.assert_not_called()

    def test_save_is_non_fatal_when_bigquery_is_down(self):
        """BQ write failure must not raise — checkpoint is best-effort."""
        with patch("tools.phoenix.insert_row", side_effect=RuntimeError("bq down")):
            # Must not raise.
            save_checkpoint(_AGENT_ID, _PROJECT_ID, _VALID_STATE)

    def test_save_state_json_is_deterministic(self):
        """Same state must always produce the same hash."""
        with patch("tools.phoenix.insert_row"):
            h1 = save_checkpoint(_AGENT_ID, _PROJECT_ID, _VALID_STATE)
            h2 = save_checkpoint(_AGENT_ID, _PROJECT_ID, _VALID_STATE)
        assert h1 == h2


# ── load_checkpoint ───────────────────────────────────────────────────────────


def _make_valid_row(state: dict) -> dict:
    """Build a BQ row dict that will pass hash verification."""
    serialized = _serialize_state(state)
    return {
        "state_json": serialized,
        "checkpoint_hash": _compute_hash(serialized),
        "is_valid": True,
    }


class TestLoadCheckpoint:
    def test_load_returns_deserialized_state(self):
        row = _make_valid_row(_VALID_STATE)
        with patch("tools.phoenix.query_rows", return_value=[row]):
            result = load_checkpoint(_AGENT_ID, _PROJECT_ID)
        assert result == _VALID_STATE

    def test_load_returns_none_when_no_rows(self):
        with patch("tools.phoenix.query_rows", return_value=[]):
            result = load_checkpoint(_AGENT_ID, _PROJECT_ID)
        assert result is None

    def test_load_returns_none_on_bigquery_failure(self):
        with patch("tools.phoenix.query_rows", side_effect=RuntimeError("bq down")):
            result = load_checkpoint(_AGENT_ID, _PROJECT_ID)
        assert result is None

    def test_load_skips_row_with_hash_mismatch(self):
        row = {"state_json": '{"agent_id": "x"}', "checkpoint_hash": "badhash"}
        with patch("tools.phoenix.query_rows", return_value=[row]):
            result = load_checkpoint(_AGENT_ID, _PROJECT_ID)
        assert result is None

    def test_load_skips_malformed_json_row(self):
        bad_json = "{not: valid json"
        row = {"state_json": bad_json, "checkpoint_hash": _compute_hash(bad_json)}
        with patch("tools.phoenix.query_rows", return_value=[row]):
            result = load_checkpoint(_AGENT_ID, _PROJECT_ID)
        assert result is None

    def test_load_falls_back_to_second_row_when_first_has_hash_mismatch(self):
        bad_row = {"state_json": '{"agent_id": "tampered"}', "checkpoint_hash": "bad"}
        good_row = _make_valid_row(_VALID_STATE)
        with patch("tools.phoenix.query_rows", return_value=[bad_row, good_row]):
            result = load_checkpoint(_AGENT_ID, _PROJECT_ID)
        assert result == _VALID_STATE

    def test_load_queries_with_correct_agent_and_project_params(self):
        with patch("tools.phoenix.query_rows", return_value=[]) as mock_query:
            load_checkpoint(_AGENT_ID, _PROJECT_ID)
        mock_query.assert_called_once()
        call_params = mock_query.call_args.kwargs.get("params") or mock_query.call_args[1].get(
            "params", {}
        )
        assert call_params.get("agent_id") == _AGENT_ID
        assert call_params.get("project_id") == _PROJECT_ID


# ── phoenix_recover ───────────────────────────────────────────────────────────


class TestPhoenixRecover:
    def test_returns_current_state_when_valid(self):
        result = phoenix_recover(_AGENT_ID, _PROJECT_ID, _VALID_STATE)
        assert result is _VALID_STATE

    def test_restores_from_checkpoint_when_state_is_corrupted(self):
        corrupted = {"agent_id": "", "project_id": _PROJECT_ID}
        with patch("tools.phoenix.load_checkpoint", return_value=_VALID_STATE) as mock_load:
            result = phoenix_recover(_AGENT_ID, _PROJECT_ID, corrupted)
        assert result == _VALID_STATE
        mock_load.assert_called_once_with(_AGENT_ID, _PROJECT_ID)

    def test_raises_when_state_corrupted_and_no_checkpoint_available(self):
        corrupted = {"agent_id": "", "project_id": _PROJECT_ID}
        with patch("tools.phoenix.load_checkpoint", return_value=None):
            with pytest.raises(CheckpointCorruptedError, match="no restorable checkpoint"):
                phoenix_recover(_AGENT_ID, _PROJECT_ID, corrupted)

    def test_does_not_call_load_checkpoint_when_state_is_valid(self):
        with patch("tools.phoenix.load_checkpoint") as mock_load:
            phoenix_recover(_AGENT_ID, _PROJECT_ID, _VALID_STATE)
        mock_load.assert_not_called()

    def test_corruption_error_message_contains_agent_id(self):
        corrupted = {"agent_id": "", "project_id": _PROJECT_ID}
        with patch("tools.phoenix.load_checkpoint", return_value=None):
            with pytest.raises(CheckpointCorruptedError, match=_AGENT_ID):
                phoenix_recover(_AGENT_ID, _PROJECT_ID, corrupted)
