"""
tools/google_docs.py — Google Docs integration for Morphic-GAOS.

Provides:
  - create_document()     Create a new Google Doc, optionally in a Drive folder.
  - read_document()       Read the full plain-text content of a document.
  - append_content()      Append text to the end of an existing document.
  - list_comments()       List all comments on a document (for ITERATE_PLAN polling).

Authentication:
  Uses a service account with Docs + Drive scopes
  (settings.docs.service_account_key) or falls back to ADC.
  ADC is the preferred path for Cloud Run and local dev with oauth-client.json.

Spec: GAOS-Tools-Spec.md §16 · GAOS-Memory-Spec.md §3 (Layer 5b — Blueprint Factory)
"""

from __future__ import annotations

import logging
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import get_settings

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

_DOCS_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]

# ── Error types ──────────────────────────────────────────────────────────────


class DocsApiError(Exception):
    """Unrecoverable Google Docs or Drive API error."""


class DocumentNotFoundError(Exception):
    """The requested document does not exist or is not accessible."""


# ── Credential factory ────────────────────────────────────────────────────────


def _get_credentials() -> Any:
    """
    Return Google OAuth2 credentials for the Docs and Drive APIs.

    Resolution order:
    1. Service account key file (settings.docs.service_account_key) — local dev override.
    2. Domain-Wide Delegation (settings.docs.dwd_subject) — Cloud Run production path.
       Uses impersonated_credentials to impersonate the configured Workspace user so the
       SA can create Drive/Docs resources in that user's account.  Requires DWD to be
       configured in Google Workspace Admin > Security > API Controls > Domain-wide
       Delegation for the nexus-prime-sa OAuth client ID (106746764104037891386).
    3. ADC with explicit scopes — local dev fallback (gcloud application-default login).

    Returns:
        A google.oauth2 Credentials object with Docs + Drive scopes.
    """
    import google.auth
    from google.auth import iam as google_auth_iam
    from google.auth.transport import requests as google_auth_requests

    settings = get_settings()
    docs_cfg = getattr(settings, "docs", None)
    key_path: str = getattr(docs_cfg, "service_account_key", "") or ""
    dwd_subject: str = getattr(docs_cfg, "dwd_subject", "") or ""

    if key_path:
        log.warning("google_docs: credential path=key_file key=%s", key_path)
        return service_account.Credentials.from_service_account_file(key_path, scopes=_DOCS_SCOPES)

    if dwd_subject:
        log.warning("google_docs: credential path=DWD subject=%s", dwd_subject)
        # Cloud Run DWD: use IAM signBlob API to create a JWT with sub=workspace_user,
        # then exchange it for an access token.  impersonated_credentials.Credentials
        # does NOT honour the `subject` param — the token has no `sub` claim, so the
        # SA acts as itself (no Drive quota → 403).  The correct path is:
        #   1. ADC (Cloud Run SA) → refresh → iam.Signer (signBlob)
        #   2. service_account.Credentials(signer, subject=user) → DWD token
        # Requires: DWD configured in Workspace Admin for the SA's OAuth client ID,
        # and roles/iam.serviceAccountTokenCreator on itself.
        source_creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        request = google_auth_requests.Request()
        source_creds.refresh(request)

        gcp_project: str = getattr(getattr(settings, "gcp", None), "project_id", "") or ""
        sa_email = f"nexus-prime-sa@{gcp_project}.iam.gserviceaccount.com" if gcp_project else ""
        log.warning("google_docs: DWD sa_email=%s", sa_email)

        signer = google_auth_iam.Signer(
            request=request,
            credentials=source_creds,
            service_account_email=sa_email,
        )
        creds = service_account.Credentials(
            signer=signer,
            service_account_email=sa_email,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=_DOCS_SCOPES,
            subject=dwd_subject,
        )
        # Eagerly exchange the JWT assertion for an access token so any DWD config
        # failure surfaces immediately with a clear error (not as a 403 from the API).
        creds.refresh(request)
        log.warning("google_docs: DWD token obtained expiry=%s", creds.expiry)
        return creds

    log.warning("google_docs: credential path=ADC (no dwd_subject configured)")
    creds, _ = google.auth.default(scopes=_DOCS_SCOPES)
    return creds


def _get_docs_service() -> Any:
    """Build and return an authenticated Google Docs API v1 service client."""
    return build("docs", "v1", credentials=_get_credentials(), cache_discovery=False)


def _get_drive_service() -> Any:
    """Build and return an authenticated Google Drive API v3 service client."""
    return build("drive", "v3", credentials=_get_credentials(), cache_discovery=False)


# ── Internal helpers ──────────────────────────────────────────────────────────


def _extract_text(doc: dict[str, Any]) -> str:
    """
    Extract plain text from a Google Docs API document response dict.

    Traverses body.content → paragraph.elements → textRun.content and
    concatenates all text runs.  Non-paragraph structural elements (tables,
    section breaks) are skipped.

    Args:
        doc: Raw dict returned by ``documents().get().execute()``.

    Returns:
        Full document text as a single string (newlines preserved).
    """
    parts: list[str] = []
    for elem in doc.get("body", {}).get("content", []):
        if "paragraph" in elem:
            for pe in elem["paragraph"].get("elements", []):
                if "textRun" in pe:
                    parts.append(pe["textRun"].get("content", ""))
    return "".join(parts)


# ── Public API ────────────────────────────────────────────────────────────────


def create_document(
    title: str,
    project_id: str,
    folder_id: str | None = None,
    initial_content: str = "",
) -> str:
    """
    Create a new Google Doc and optionally place it in a Drive folder.

    Args:
        title:           Document title (required, non-empty).
        project_id:      AOS project namespace (for scoping and audit context).
        folder_id:       Drive folder ID to place the document in.  If ``None``,
                         uses ``settings.docs.blueprints_folder_id``; if that is
                         also empty the document is created in the account root.
        initial_content: Optional text to insert into the document body immediately
                         after creation.

    Returns:
        The document ID string of the newly created Google Doc.

    Raises:
        ValueError:             ``title`` is empty.
        DocsApiError:           Google Docs or Drive API failure.
    """
    if not title or not title.strip():
        raise ValueError("title must be a non-empty string")

    settings = get_settings()
    resolved_folder = folder_id or (
        getattr(getattr(settings, "docs", None), "blueprints_folder_id", "") or ""
    )

    try:
        docs_svc = _get_docs_service()
        doc = docs_svc.documents().create(body={"title": title}).execute()
        doc_id: str = doc["documentId"]

        if resolved_folder:
            drive_svc = _get_drive_service()
            drive_svc.files().update(
                fileId=doc_id,
                addParents=resolved_folder,
                removeParents="root",
                fields="id, parents",
            ).execute()

        if initial_content:
            docs_svc.documents().batchUpdate(
                documentId=doc_id,
                body={
                    "requests": [
                        {
                            "insertText": {
                                "text": initial_content,
                                "location": {"index": 1},
                            }
                        }
                    ]
                },
            ).execute()

        log.debug(
            "Created Google Doc '%s' (id=%s, project=%s, folder=%s)",
            title,
            doc_id,
            project_id,
            resolved_folder or "root",
        )
        return doc_id

    except HttpError as exc:
        raise DocsApiError(f"create_document failed for '{title}': {exc}") from exc


def read_document(doc_id: str, project_id: str) -> str:
    """
    Read the full plain-text content of a Google Doc.

    Args:
        doc_id:      The document ID.
        project_id:  AOS project namespace.

    Returns:
        The document body as a plain-text string (newlines preserved).
        Returns an empty string if the document has no text content.

    Raises:
        DocumentNotFoundError: The document does not exist or is not accessible.
        DocsApiError:          Google Docs API failure (non-404).
    """
    if not doc_id or not doc_id.strip():
        raise ValueError("doc_id must be a non-empty string")

    try:
        docs_svc = _get_docs_service()
        doc = docs_svc.documents().get(documentId=doc_id).execute()
        return _extract_text(doc)

    except HttpError as exc:
        if exc.resp.status == 404:
            raise DocumentNotFoundError(
                f"Document '{doc_id}' not found (project={project_id})"
            ) from exc
        raise DocsApiError(f"read_document failed for '{doc_id}': {exc}") from exc


def append_content(doc_id: str, content: str, project_id: str) -> None:
    """
    Append text to the end of an existing Google Doc.

    The text is inserted at the last valid index of the document body,
    preserving all existing content.

    Args:
        doc_id:      The document ID.
        content:     Text to append.  If empty, this is a no-op.
        project_id:  AOS project namespace.

    Raises:
        DocumentNotFoundError: The document does not exist or is not accessible.
        DocsApiError:          Google Docs API failure.
    """
    if not doc_id or not doc_id.strip():
        raise ValueError("doc_id must be a non-empty string")

    if not content:
        return

    try:
        docs_svc = _get_docs_service()
        doc = docs_svc.documents().get(documentId=doc_id).execute()
        body_content = doc.get("body", {}).get("content", [])
        end_index = body_content[-1]["endIndex"] - 1 if body_content else 1

        docs_svc.documents().batchUpdate(
            documentId=doc_id,
            body={
                "requests": [
                    {
                        "insertText": {
                            "text": content,
                            "location": {"index": end_index},
                        }
                    }
                ]
            },
        ).execute()

        log.debug("Appended %d chars to doc '%s' (project=%s)", len(content), doc_id, project_id)

    except HttpError as exc:
        if exc.resp.status == 404:
            raise DocumentNotFoundError(
                f"Document '{doc_id}' not found (project={project_id})"
            ) from exc
        raise DocsApiError(f"append_content failed for '{doc_id}': {exc}") from exc


def list_comments(doc_id: str, project_id: str) -> list[dict[str, Any]]:
    """
    List all comments on a Google Doc via the Drive API.

    Used by the ``doc-comment-poll`` Cloud Scheduler job to feed unresolved
    owner comments into the ``ITERATE_PLAN`` node in Nexus-Prime.

    Args:
        doc_id:      The document ID (same as the Drive file ID).
        project_id:  AOS project namespace.

    Returns:
        List of comment dicts, each with keys:
            ``id``         — comment resource ID
            ``content``    — comment text
            ``author``     — commenter's display name
            ``created_at`` — ISO 8601 timestamp string
            ``resolved``   — ``True`` if the comment thread is resolved

    Raises:
        DocumentNotFoundError: The document does not exist or is not accessible.
        DocsApiError:          Google Drive API failure.
    """
    if not doc_id or not doc_id.strip():
        raise ValueError("doc_id must be a non-empty string")

    try:
        drive_svc = _get_drive_service()
        response = (
            drive_svc.comments()
            .list(
                fileId=doc_id,
                fields="comments(id,content,author/displayName,createdTime,resolved)",
                includeDeleted=False,
            )
            .execute()
        )

        results: list[dict[str, Any]] = []
        for c in response.get("comments", []):
            results.append(
                {
                    "id": c.get("id", ""),
                    "content": c.get("content", ""),
                    "author": c.get("author", {}).get("displayName", ""),
                    "created_at": c.get("createdTime", ""),
                    "resolved": c.get("resolved", False),
                }
            )
        return results

    except HttpError as exc:
        if exc.resp.status == 404:
            raise DocumentNotFoundError(
                f"Document '{doc_id}' not found (project={project_id})"
            ) from exc
        raise DocsApiError(f"list_comments failed for '{doc_id}': {exc}") from exc
