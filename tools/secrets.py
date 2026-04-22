"""
tools/secrets.py — Google Secret Manager accessor.

Called during the agent boot sequence (step 3) before any other tool.
All secrets are fetched by name from the configured GCP project.
Fails fast on missing or inaccessible secrets — never returns None.

Spec: GAOS-Tools-Spec.md §2
"""

from __future__ import annotations

from google.api_core.exceptions import NotFound, PermissionDenied
from google.cloud import secretmanager

from tools import tracked

# ── Error types ────────────────────────────────────────────────────────────


class SecretNotFoundError(Exception):
    """Secret ID does not exist in Secret Manager."""


class SecretAccessDenied(Exception):
    """Caller's service account cannot access this secret."""


class SecretManagerError(Exception):
    """Unrecoverable Secret Manager API error."""


# ── Public API ─────────────────────────────────────────────────────────────


@tracked("secret_manager")
def get_secret(secret_id: str, project_id: str) -> str:
    """
    Retrieve the latest version of a secret from Google Secret Manager.

    Args:
        secret_id:  The secret name as registered in Secret Manager
                    (e.g. "GEMINI_API_KEY").
        project_id: The GCP project that owns the secret.

    Returns:
        The secret value as a UTF-8 string.

    Raises:
        SecretNotFoundError:  Secret does not exist. Agent must log
                              STARTUP_FAILURE and exit.
        SecretAccessDenied:   Service account lacks
                              roles/secretmanager.secretAccessor for this
                              secret. Agent must log STARTUP_FAILURE and exit.
        SecretManagerError:   Unrecoverable API error.
    """
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    try:
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8").strip()
    except NotFound as exc:
        raise SecretNotFoundError(
            f"Secret '{secret_id}' not found in project '{project_id}'. "
            "Run the provisioning steps in GAOS-Deploy-Spec.md §3."
        ) from exc
    except PermissionDenied as exc:
        raise SecretAccessDenied(
            f"Permission denied accessing secret '{secret_id}' in project "
            f"'{project_id}'. Ensure the service account has "
            "roles/secretmanager.secretAccessor for this secret."
        ) from exc
    except Exception as exc:
        raise SecretManagerError(f"Unexpected error accessing secret '{secret_id}': {exc}") from exc
