"""
tools/drive.py — Google Drive accessor for Morphic-G AOS.

Provides read and write access to the Knowledge/ procedural-knowledge
folder tree. File paths are relative to the project's root Knowledge/
folder (Drive folder ID stored in the Project Registry).

Only Nexus-Prime's service account has write access. Domain orchestrators
call read_file() and list_folder() only.

Spec: GAOS-Tools-Spec.md §5
"""
from __future__ import annotations

import io
import time
from typing import Any

from googleapiclient.discovery import build  # type: ignore[import-untyped]
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]
from googleapiclient.http import MediaIoBaseUpload  # type: ignore[import-untyped]
import google.auth
import json

from config import get_settings
from tools.secrets import get_secret


# ── Error types ──────────────────────────────────────────────────────────────


class KnowledgeFileNotFoundError(Exception):
    """File does not exist at the given path."""


class KnowledgeFolderNotFoundError(Exception):
    """Folder does not exist at the given path."""


class DriveReadError(Exception):
    """Unrecoverable Drive read error."""


class DriveWriteError(Exception):
    """Unrecoverable Drive write error."""


class DrivePermissionError(Exception):
    """Service account lacks required Drive permissions."""


# ── Internal helpers ──────────────────────────────────────────────────────────


def _build_service(project_id: str) -> Any:
    """Return an authenticated Drive v3 service resource."""
    settings = get_settings()
    sa_json = get_secret("GDRIVE_SERVICE_ACCOUNT", settings.GCP_PROJECT_ID)
    sa_info = json.loads(sa_json)
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _get_drive_root(project_id: str) -> str:
    """Return the root Knowledge/ folder ID for this project."""
    settings = get_settings()
    project = settings.get_project(project_id)
    if project is None or not project.drive_folder_id:
        raise KnowledgeFolderNotFoundError(
            f"No drive_folder_id configured for project '{project_id}'."
        )
    return project.drive_folder_id


def _resolve_path(service: Any, root_folder_id: str, path: str) -> str | None:
    """
    Walk a slash-delimited relative path under root_folder_id and return
    the Drive file/folder ID of the final component, or None if not found.
    """
    parts = [p for p in path.strip("/").split("/") if p]
    current_id = root_folder_id
    for part in parts:
        query = (
            f"'{current_id}' in parents "
            f"and name = '{part}' "
            f"and trashed = false"
        )
        resp = service.files().list(q=query, fields="files(id, name)").execute()
        files = resp.get("files", [])
        if not files:
            return None
        current_id = files[0]["id"]
    return current_id


def _ensure_folder_path(service: Any, root_folder_id: str, path: str) -> str:
    """
    Ensure all folders in `path` exist under root_folder_id, creating
    them as needed. Returns the ID of the final (deepest) folder.
    """
    parts = [p for p in path.strip("/").split("/") if p]
    current_id = root_folder_id
    for part in parts:
        query = (
            f"'{current_id}' in parents "
            f"and name = '{part}' "
            f"and mimeType = 'application/vnd.google-apps.folder' "
            f"and trashed = false"
        )
        resp = service.files().list(q=query, fields="files(id)").execute()
        files = resp.get("files", [])
        if files:
            current_id = files[0]["id"]
        else:
            folder_meta = {
                "name": part,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [current_id],
            }
            created = service.files().create(body=folder_meta, fields="id").execute()
            current_id = created["id"]
    return current_id


def _retry_drive(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Retry on Drive 429/5xx up to 3 times with exponential backoff."""
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return fn(*args, **kwargs)
        except HttpError as exc:
            code = int(exc.resp.status)
            if code == 429 or code >= 500:
                last_exc = exc
                time.sleep(2 ** attempt)
                continue
            if code == 403:
                raise DrivePermissionError(
                    f"Service account lacks Drive permissions: {exc}"
                ) from exc
            raise DriveReadError(f"Drive API error {code}: {exc}") from exc
    raise DriveReadError(
        f"Drive API still failing after 3 retries: {last_exc}"
    ) from last_exc


# ── Public API ────────────────────────────────────────────────────────────────


def read_file(file_path: str, project_id: str) -> str:
    """
    Read a Markdown file from the Knowledge/ folder tree.

    Args:
        file_path:  Relative to Knowledge/ root (e.g., "procedures/invoice.md").
        project_id: AOS project namespace.

    Returns:
        File content as a UTF-8 string.

    Raises:
        KnowledgeFileNotFoundError, DriveReadError, DrivePermissionError.
    """
    service = _build_service(project_id)
    root = _get_drive_root(project_id)
    file_id = _resolve_path(service, root, file_path)
    if file_id is None:
        raise KnowledgeFileNotFoundError(
            f"File not found in Knowledge/: {file_path}"
        )
    try:
        content = _retry_drive(
            service.files().get_media(fileId=file_id).execute
        )
        return content.decode("utf-8") if isinstance(content, bytes) else content
    except HttpError as exc:
        raise DriveReadError(f"Failed to read '{file_path}': {exc}") from exc


def write_file(file_path: str, content: str, project_id: str) -> str:
    """
    Create or overwrite a Markdown file in the Knowledge/ folder tree.

    Only callable by Nexus-Prime post-approval.

    Args:
        file_path:  Relative to Knowledge/ root.
        content:    UTF-8 Markdown text.
        project_id: AOS project namespace.

    Returns:
        drive_file_id: The Google Drive file ID of the written file.

    Raises:
        DriveWriteError, DrivePermissionError.
    """
    service = _build_service(project_id)
    root = _get_drive_root(project_id)

    parts = file_path.rstrip("/").rsplit("/", 1)
    folder_path = parts[0] if len(parts) == 2 else ""
    file_name = parts[-1]

    parent_id = _ensure_folder_path(service, root, folder_path) if folder_path else root

    media = MediaIoBaseUpload(
        io.BytesIO(content.encode("utf-8")),
        mimetype="text/markdown",
        resumable=False,
    )

    # Check if file already exists under parent_id
    existing_id = _resolve_path(service, parent_id, file_name)
    try:
        if existing_id:
            result = _retry_drive(
                service.files().update(
                    fileId=existing_id,
                    media_body=media,
                    fields="id",
                ).execute
            )
        else:
            file_meta = {"name": file_name, "parents": [parent_id]}
            result = _retry_drive(
                service.files().create(
                    body=file_meta,
                    media_body=media,
                    fields="id",
                ).execute
            )
        return result["id"]
    except HttpError as exc:
        raise DriveWriteError(f"Failed to write '{file_path}': {exc}") from exc


def copy_file(source_path: str, dest_path: str, project_id: str) -> str:
    """
    Copy a file within the Knowledge/ tree (used for version archiving).

    Creates all intermediate destination folders if they do not exist.

    Returns:
        drive_file_id: The file ID of the newly created copy.

    Raises:
        KnowledgeFileNotFoundError, DriveWriteError, DrivePermissionError.
    """
    service = _build_service(project_id)
    root = _get_drive_root(project_id)

    source_id = _resolve_path(service, root, source_path)
    if source_id is None:
        raise KnowledgeFileNotFoundError(
            f"Source file not found: {source_path}"
        )

    dest_parts = dest_path.rstrip("/").rsplit("/", 1)
    dest_folder_path = dest_parts[0] if len(dest_parts) == 2 else ""
    dest_name = dest_parts[-1]
    dest_parent_id = (
        _ensure_folder_path(service, root, dest_folder_path)
        if dest_folder_path
        else root
    )

    try:
        result = _retry_drive(
            service.files().copy(
                fileId=source_id,
                body={"name": dest_name, "parents": [dest_parent_id]},
                fields="id",
            ).execute
        )
        return result["id"]
    except HttpError as exc:
        raise DriveWriteError(
            f"Failed to copy '{source_path}' → '{dest_path}': {exc}"
        ) from exc


def list_folder(folder_path: str, project_id: str) -> list[str]:
    """
    Return relative file paths under a Knowledge/ subfolder.

    Args:
        folder_path: Relative to Knowledge/ root (e.g., "procedures").
        project_id:  AOS project namespace.

    Returns:
        List of relative file paths (e.g., ["procedures/invoice.md"]).

    Raises:
        KnowledgeFolderNotFoundError, DriveReadError, DrivePermissionError.
    """
    service = _build_service(project_id)
    root = _get_drive_root(project_id)

    folder_id = _resolve_path(service, root, folder_path) if folder_path else root
    if folder_id is None:
        raise KnowledgeFolderNotFoundError(
            f"Folder not found in Knowledge/: {folder_path}"
        )

    results: list[str] = []
    _collect_files(service, folder_id, folder_path.rstrip("/"), results)
    return results


def _collect_files(service: Any, folder_id: str, prefix: str, out: list[str]) -> None:
    """Recursively collect relative file paths under folder_id."""
    query = f"'{folder_id}' in parents and trashed = false"
    resp = service.files().list(
        q=query, fields="files(id, name, mimeType)"
    ).execute()
    for item in resp.get("files", []):
        rel = f"{prefix}/{item['name']}" if prefix else item["name"]
        if item["mimeType"] == "application/vnd.google-apps.folder":
            _collect_files(service, item["id"], rel, out)
        else:
            out.append(rel)
