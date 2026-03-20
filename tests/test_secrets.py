"""tests/test_secrets.py — Unit tests for tools/secrets.py"""

from unittest.mock import patch

import pytest

from tools.secrets import (
    SecretAccessDenied,
    SecretManagerError,
    SecretNotFoundError,
    get_secret,
)


@pytest.fixture()
def mock_sm_client():
    """Patch secretmanager.SecretManagerServiceClient for all tests."""
    with patch("tools.secrets.secretmanager.SecretManagerServiceClient") as cls:
        yield cls.return_value


class TestGetSecret:
    def test_returns_secret_value(self, mock_sm_client):
        mock_sm_client.access_secret_version.return_value.payload.data = b"my-api-key-12345"
        result = get_secret("GEMINI_API_KEY", "morphic-gaos-prod")
        assert result == "my-api-key-12345"
        mock_sm_client.access_secret_version.assert_called_once_with(
            request={"name": "projects/morphic-gaos-prod/secrets/GEMINI_API_KEY/versions/latest"}
        )

    def test_raises_secret_not_found(self, mock_sm_client):
        from google.api_core.exceptions import NotFound

        mock_sm_client.access_secret_version.side_effect = NotFound("not found")
        with pytest.raises(SecretNotFoundError, match="GEMINI_API_KEY"):
            get_secret("GEMINI_API_KEY", "morphic-gaos-prod")

    def test_raises_secret_access_denied(self, mock_sm_client):
        from google.api_core.exceptions import PermissionDenied

        mock_sm_client.access_secret_version.side_effect = PermissionDenied("permission denied")
        with pytest.raises(SecretAccessDenied, match="GEMINI_API_KEY"):
            get_secret("GEMINI_API_KEY", "morphic-gaos-prod")

    def test_raises_secret_manager_error_on_unexpected(self, mock_sm_client):
        mock_sm_client.access_secret_version.side_effect = RuntimeError("unexpected network error")
        with pytest.raises(SecretManagerError, match="unexpected"):
            get_secret("SOME_SECRET", "morphic-gaos-prod")

    def test_builds_correct_resource_path(self, mock_sm_client):
        mock_sm_client.access_secret_version.return_value.payload.data = b"val"
        get_secret("MY_SECRET", "my-project-123")
        call_args = mock_sm_client.access_secret_version.call_args
        assert (
            call_args.kwargs["request"]["name"]
            == "projects/my-project-123/secrets/MY_SECRET/versions/latest"
        )
