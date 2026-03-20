"""
tools/project_registry.py — Project Registry loader for Morphic-G AOS.

Reads and validates the Project Registry Sheet tab during agent boot.
The Project Registry maps AOS project namespaces to their infrastructure:
Sheet workbook, Drive folder, and Pub/Sub topic prefix.

Spec: GAOS-Tools-Spec.md §7
"""

from __future__ import annotations

from pydantic import BaseModel, ValidationError

from tools.google_sheets import get_all_records

# ── Error types ──────────────────────────────────────────────────────────────


class ProjectRegistryParseError(Exception):
    """A row in the Project Registry fails schema validation."""


class ProjectNotFoundError(Exception):
    """No project with the given project_id exists in the registry."""


class ProjectPausedError(Exception):
    """Project exists but is currently paused."""


class ProjectArchivedError(Exception):
    """Project exists but has been archived."""


# ── Schema ───────────────────────────────────────────────────────────────────


class ProjectRecord(BaseModel):
    """
    One row in the Project Registry Sheet tab.

    Column headers match setup_workspace.py HEADERS["Project Registry"] exactly:
        project_id | project_name | status | sheet_workbook_id |
        drive_folder_id | budget_ceiling_usd | owner_email | created_date | notes
    """

    project_id: str  # Unique slug (e.g., "acme", "northstar")
    project_name: str  # Human-readable display name
    status: str  # "Active" | "Pending" | "Paused" | "Archived"
    sheet_workbook_id: str  # Google Sheets workbook ID for this project
    drive_folder_id: str  # Knowledge/ root Drive folder ID
    budget_ceiling_usd: str = ""  # Monthly LLM spend ceiling (blank = no limit)
    owner_email: str = ""  # Google account to notify on escalations
    created_date: str = ""  # ISO 8601 date the project was registered
    notes: str = ""  # Free-text context for Nexus-Prime


# ── Tab name ─────────────────────────────────────────────────────────────────

_TAB = "Project Registry"


# ── Public API ───────────────────────────────────────────────────────────────


def load_project_registry(project_id: str) -> list[ProjectRecord]:
    """
    Read the Project Registry tab from the control-plane Sheet and
    return all ProjectRecord entries.

    Args:
        project_id: The *system-level* AOS project_id used for Sheets auth
                    (typically the single project that owns the control-plane
                    Sheet). Not an AOS namespace being looked up.

    Returns:
        All rows as a list of ProjectRecord.

    Raises:
        TabNotFoundError:          Project Registry tab missing.
        ProjectRegistryParseError: A row fails ProjectRecord validation.
    """
    rows = get_all_records(_TAB, project_id)
    records: list[ProjectRecord] = []
    for i, row in enumerate(rows, start=2):  # row 1 = header
        try:
            records.append(ProjectRecord(**row))
        except (ValidationError, TypeError) as exc:
            raise ProjectRegistryParseError(
                f"Project Registry row {i} failed validation: {exc}"
            ) from exc
    return records


def get_active_project_ids(project_id: str) -> list[str]:
    """
    Return all project_id strings where status == "Active".
    Used by agents to validate incoming task project_ids at boot.
    """
    return [r.project_id for r in load_project_registry(project_id) if r.status == "Active"]


def get_project(project_id: str, system_project_id: str) -> ProjectRecord:
    """
    Return the ProjectRecord for a specific AOS project_id.

    Args:
        project_id:        The AOS namespace to look up.
        system_project_id: The system-level project_id for Sheets auth.

    Raises:
        ProjectNotFoundError: No row with this project_id exists.
        ProjectPausedError:   Project exists but status is "Paused".
        ProjectArchivedError: Project exists but status is "Archived".
    """
    records = load_project_registry(system_project_id)
    for record in records:
        if record.project_id == project_id:
            if record.status == "Paused":
                raise ProjectPausedError(f"Project '{project_id}' is currently paused.")
            if record.status == "Archived":
                raise ProjectArchivedError(f"Project '{project_id}' has been archived.")
            return record
    raise ProjectNotFoundError(f"No project with project_id='{project_id}' found in the registry.")
