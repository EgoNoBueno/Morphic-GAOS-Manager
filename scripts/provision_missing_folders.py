"""
scripts/provision_missing_folders.py — Create missing Drive folders in an
already-provisioned Morphic-G AOS workspace.

Creates (idempotently):
  - Morphic-G AOS/Knowledge/playbooks/
  - Morphic-G AOS/Project_Incubator/

Prints the Project_Incubator/ folder ID so you can paste it into
config/settings.yaml under docs.blueprints_folder_id.

Prerequisites:
  - ADC configured: gcloud auth application-default login
  - .venv activated
  - Run from repo root: python scripts/provision_missing_folders.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import google.auth
import yaml
from googleapiclient.discovery import build

PROJECT = "morphic-gaos-prod"
DRIVE_ROOT_NAME = "Morphic-G AOS"
KNOWLEDGE_FOLDER_NAME = "Knowledge"
BLUEPRINTS_FOLDER_NAME = "Project_Incubator"
PLAYBOOKS_FOLDER_NAME = "playbooks"

SCOPES = ["https://www.googleapis.com/auth/drive"]


def _get_drive():
    creds, _ = google.auth.default(scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_folder(drive, name: str, parent_id: str | None = None) -> str | None:
    """Return the Drive ID of a folder by name (and optional parent), or None."""
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    result = drive.files().list(q=q, fields="files(id,name)", pageSize=5).execute()
    files = result.get("files", [])
    return files[0]["id"] if files else None


def get_or_create_folder(drive, name: str, parent_id: str) -> tuple[str, bool]:
    """Return (folder_id, created). If folder exists, return existing ID."""
    existing = find_folder(drive, name, parent_id)
    if existing:
        return existing, False
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = drive.files().create(body=metadata, fields="id").execute()
    return folder["id"], True


def load_settings() -> dict:
    """Load config/settings.yaml."""
    settings_path = Path(__file__).parent.parent / "config" / "settings.yaml"
    if not settings_path.exists():
        print("ERROR: config/settings.yaml not found. Run setup_workspace.py first.")
        sys.exit(1)
    with settings_path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    print("Authenticating via ADC...")
    drive = _get_drive()

    # Load the existing Knowledge/ folder ID from settings
    settings = load_settings()
    knowledge_id: str = settings["projects"]["default"]["drive_folder_id"]
    print(f"Using Knowledge/ folder ID from settings.yaml: {knowledge_id}")

    # Find the root folder (parent of Knowledge/) so we can create Project_Incubator/ beside it
    # Drive API: list parents of the Knowledge/ folder
    knowledge_meta = drive.files().get(fileId=knowledge_id, fields="parents").execute()
    root_id: str = knowledge_meta["parents"][0]
    print(f"Root (Morphic-G AOS/) folder ID: {root_id}")

    # 1. Knowledge/playbooks/
    print(f"\nEnsuring Knowledge/{PLAYBOOKS_FOLDER_NAME}/...")
    playbooks_id, created = get_or_create_folder(drive, PLAYBOOKS_FOLDER_NAME, knowledge_id)
    status = "created" if created else "already exists"
    print(f"  {PLAYBOOKS_FOLDER_NAME}/ ({status}): {playbooks_id}")

    # 2. Project_Incubator/ (sibling of Knowledge/)
    print(f"\nEnsuring {BLUEPRINTS_FOLDER_NAME}/...")
    blueprints_id, created = get_or_create_folder(drive, BLUEPRINTS_FOLDER_NAME, root_id)
    status = "created" if created else "already exists"
    print(f"  {BLUEPRINTS_FOLDER_NAME}/ ({status}): {blueprints_id}")

    # Done — print instructions
    print("\n" + "=" * 60)
    print("SUCCESS — add this to config/settings.yaml:")
    print("=" * 60)
    print(f'\ndocs:\n  blueprints_folder_id: "{blueprints_id}"')
    print("\n" + "=" * 60)
    print("NOTE: Permissions inherit from the root folder — no sharing needed.")


if __name__ == "__main__":
    main()
