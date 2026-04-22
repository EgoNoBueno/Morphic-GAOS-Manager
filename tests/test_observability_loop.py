"""
tests/test_observability_loop.py — Unit tests for _check_pubsub_endpoint_staleness in
scripts/observability_loop.py, specifically the auto-repair remedial action added 2026-04-21.

All GCP calls are mocked at the SDK boundary.

Tests:
  _check_pubsub_endpoint_staleness() — auto-repair behaviour
    test_stale_endpoint_triggers_modify_push_config
    test_stale_endpoint_repair_success_prints_repaired
    test_stale_endpoint_repair_failure_sets_stale_flag
    test_fresh_endpoint_does_not_call_modify_push_config
    test_live_url_resolution_failure_returns_early
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _load_obs_loop() -> ModuleType:
    """Load observability_loop.py, stubbing out the module-level GAOS imports."""
    spec = importlib.util.spec_from_file_location(
        "observability_loop",
        Path(__file__).parent.parent / "scripts" / "observability_loop.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    with (
        patch("agents._call_model"),
        patch("config.get_settings"),
        patch("tools.google_sheets.append_row"),
        patch("tools.google_sheets.get_all_records"),
        patch("tools.google_sheets.init_sheets_client"),
    ):
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="session")
def obs_module():
    """Session-scoped fixture that loads observability_loop.py with GCP imports stubbed."""
    return _load_obs_loop()


# ---------------------------------------------------------------------------
# Helpers — build mock GCP clients
# ---------------------------------------------------------------------------


def _make_sub_with_endpoint(endpoint: str) -> MagicMock:
    sub = MagicMock()
    sub.push_config.push_endpoint = endpoint
    return sub


def _make_gcp_mocks(
    live_url: str,
    sub_endpoint: str = "",
    prebuilt_subscription: MagicMock | None = None,
):
    """Return (mock_auth, mock_build, mock_sub_cls, mock_subscriber) for a single subscription.

    mock_auth:       patched google.auth.default returning dummy credentials.
    mock_build:      patched googleapiclient.discovery.build returning a Cloud Run client
                     whose services().get() resolves to *live_url*.
    mock_sub_cls:    patched pubsub_v1.SubscriberClient class whose constructor returns
                     mock_subscriber.
    mock_subscriber: the SubscriberClient instance; get_subscription() returns either
                     *prebuilt_subscription* (when supplied) or a MagicMock with
                     push_endpoint set to *sub_endpoint*.
    """
    mock_creds = MagicMock()
    mock_auth = MagicMock(return_value=(mock_creds, None))

    mock_run_svc = MagicMock()
    mock_run_svc.get.return_value.execute.return_value = {"uri": live_url}
    mock_run_loc = MagicMock()
    mock_run_loc.services.return_value = mock_run_svc
    mock_run_proj = MagicMock()
    mock_run_proj.locations.return_value = mock_run_loc
    mock_run_client = MagicMock()
    mock_run_client.projects.return_value = mock_run_proj
    mock_build = MagicMock(return_value=mock_run_client)

    sub = (
        prebuilt_subscription
        if prebuilt_subscription is not None
        else _make_sub_with_endpoint(sub_endpoint)
    )
    mock_subscriber = MagicMock()
    mock_subscriber.get_subscription.return_value = sub
    mock_sub_cls = MagicMock(return_value=mock_subscriber)

    return mock_auth, mock_build, mock_sub_cls, mock_subscriber


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCheckPubsubEndpointStaleness:
    """Auto-repair behaviour in _check_pubsub_endpoint_staleness."""

    def test_stale_endpoint_triggers_modify_push_config(self, capsys, obs_module):
        """When a subscription points at a stale URL, modify_push_config is called."""
        live = "https://nexus-prime-new-abc123.a.run.app"
        old = "https://nexus-prime-OLD.a.run.app/pubsub"

        mock_auth, mock_build, mock_sub_cls, mock_subscriber = _make_gcp_mocks(live, old)

        with (
            patch("google.auth.default", mock_auth),
            patch("googleapiclient.discovery.build", mock_build),
            patch("google.cloud.pubsub_v1.SubscriberClient", mock_sub_cls),
        ):
            obs_module._check_pubsub_endpoint_staleness("morphic-gaos-prod")

        mock_subscriber.modify_push_config.assert_called()
        call_kwargs = mock_subscriber.modify_push_config.call_args.kwargs
        push_cfg = call_kwargs["request"]["push_config"]
        # push_config is now a PushConfig object (not a bare dict) so that
        # existing fields like oidc_token are preserved on the subscription.
        assert push_cfg.push_endpoint == f"{live}/pubsub"

    def test_stale_endpoint_repair_preserves_oidc_token(self, obs_module):
        """Repair must not wipe the oidc_token already set on the subscription."""
        from google.pubsub_v1.types import PushConfig

        live = "https://nexus-prime-new-abc123.a.run.app"
        old = "https://nexus-prime-OLD.a.run.app/pubsub"

        # Build a real PushConfig with an OIDC token already configured.
        original_push_cfg = PushConfig(
            push_endpoint=old,
            oidc_token=PushConfig.OidcToken(
                service_account_email="nexus-prime-sa@morphic-gaos-prod.iam.gserviceaccount.com",
                audience=live,
            ),
        )

        sub = MagicMock()
        sub.push_config = original_push_cfg
        mock_auth, mock_build, mock_sub_cls, mock_subscriber = _make_gcp_mocks(
            live, prebuilt_subscription=sub
        )

        with (
            patch("google.auth.default", mock_auth),
            patch("googleapiclient.discovery.build", mock_build),
            patch("google.cloud.pubsub_v1.SubscriberClient", mock_sub_cls),
        ):
            obs_module._check_pubsub_endpoint_staleness("morphic-gaos-prod")

        call_kwargs = mock_subscriber.modify_push_config.call_args.kwargs
        sent_cfg = call_kwargs["request"]["push_config"]
        # Endpoint updated to the live URL.
        assert sent_cfg.push_endpoint == f"{live}/pubsub"
        # OIDC token carried over from the original push_config.
        assert (
            sent_cfg.oidc_token.service_account_email
            == "nexus-prime-sa@morphic-gaos-prod.iam.gserviceaccount.com"
        )

    def test_stale_endpoint_repair_success_prints_repaired(self, capsys, obs_module):
        """A successful repair prints 'REPAIRED' so the operator knows what happened."""
        live = "https://nexus-prime-new-abc123.a.run.app"
        old = "https://nexus-prime-OLD.a.run.app/pubsub"

        mock_auth, mock_build, mock_sub_cls, mock_subscriber = _make_gcp_mocks(live, old)

        with (
            patch("google.auth.default", mock_auth),
            patch("googleapiclient.discovery.build", mock_build),
            patch("google.cloud.pubsub_v1.SubscriberClient", mock_sub_cls),
        ):
            obs_module._check_pubsub_endpoint_staleness("morphic-gaos-prod")

        out = capsys.readouterr().out
        assert "REPAIRED" in out
        assert "REPAIR FAILED" not in out

    def test_stale_endpoint_repair_failure_sets_stale_flag(self, capsys, obs_module):
        """If modify_push_config raises, REPAIR FAILED is printed (manual action needed)."""
        live = "https://nexus-prime-new-abc123.a.run.app"
        old = "https://nexus-prime-OLD.a.run.app/pubsub"

        mock_auth, mock_build, mock_sub_cls, mock_subscriber = _make_gcp_mocks(live, old)
        mock_subscriber.modify_push_config.side_effect = RuntimeError("permission denied")

        with (
            patch("google.auth.default", mock_auth),
            patch("googleapiclient.discovery.build", mock_build),
            patch("google.cloud.pubsub_v1.SubscriberClient", mock_sub_cls),
        ):
            obs_module._check_pubsub_endpoint_staleness("morphic-gaos-prod")

        out = capsys.readouterr().out
        assert "REPAIR FAILED" in out
        # "all endpoints up to date" must NOT appear — there's a genuine unresolved stale sub
        assert "up to date" not in out

    def test_fresh_endpoint_does_not_call_modify_push_config(self, obs_module):
        """A subscription already pointing at the live URL triggers no repair call."""
        live = "https://nexus-prime-current.a.run.app"
        current = f"{live}/pubsub"

        mock_auth, mock_build, mock_sub_cls, mock_subscriber = _make_gcp_mocks(live, current)

        with (
            patch("google.auth.default", mock_auth),
            patch("googleapiclient.discovery.build", mock_build),
            patch("google.cloud.pubsub_v1.SubscriberClient", mock_sub_cls),
        ):
            obs_module._check_pubsub_endpoint_staleness("morphic-gaos-prod")

        mock_subscriber.modify_push_config.assert_not_called()

    def test_live_url_resolution_failure_returns_early(self, capsys, obs_module):
        """If the Cloud Run API call fails, the function returns without checking subs."""
        mock_creds = MagicMock()
        mock_auth = MagicMock(return_value=(mock_creds, None))

        mock_run_svc = MagicMock()
        mock_run_svc.get.return_value.execute.side_effect = RuntimeError("403 forbidden")
        mock_run_loc = MagicMock()
        mock_run_loc.services.return_value = mock_run_svc
        mock_run_proj = MagicMock()
        mock_run_proj.locations.return_value = mock_run_loc
        mock_run_client = MagicMock()
        mock_run_client.projects.return_value = mock_run_proj
        mock_build = MagicMock(return_value=mock_run_client)

        mock_subscriber = MagicMock()
        mock_sub_cls = MagicMock(return_value=mock_subscriber)

        with (
            patch("google.auth.default", mock_auth),
            patch("googleapiclient.discovery.build", mock_build),
            patch("google.cloud.pubsub_v1.SubscriberClient", mock_sub_cls),
        ):
            obs_module._check_pubsub_endpoint_staleness("morphic-gaos-prod")

        out = capsys.readouterr().out
        assert "could not resolve" in out
        mock_subscriber.get_subscription.assert_not_called()
