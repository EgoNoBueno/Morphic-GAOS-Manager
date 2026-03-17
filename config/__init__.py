"""
config/__init__.py — Settings loader for Morphic-G AOS.

Loads config/settings.yaml once per process and returns a validated
Settings object. All tool modules import get_settings() from here.

Usage:
    from config import get_settings
    settings = get_settings()
    project_id = settings.GCP_PROJECT_ID
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

# ── Schema models ─────────────────────────────────────────────────────────


class GCPConfig(BaseModel):
    project_id: str
    region: str = "us-central1"


class SheetConfig(BaseModel):
    workbook_id: str


class ModelAliases(BaseModel):
    LOCAL_MODEL: str
    FAST_MODEL: str
    DEEP_MODEL: str
    LOCAL_MODEL_FALLBACK: str
    LOCAL_MODEL_TIMEOUT_SECONDS: int = 2


class MemoryBankConfig(BaseModel):
    region: str = "us-central1"
    corpora: dict[str, str] = Field(default_factory=dict)


class PubSubConfig(BaseModel):
    all_topics: list[str] = Field(default_factory=list)


class ProjectConfig(BaseModel):
    sheet_id: str = ""
    drive_folder_id: str = ""


class AppsScriptConfig(BaseModel):
    script_id: str = ""
    deployment_id: str = ""
    webhook_url: str = ""


class ChatConfig(BaseModel):
    owner_space: str = ""  # spaces/<id> — owner DM for morning briefings


class VertexSearchConfig(BaseModel):
    location: str = "global"
    playbook_datastore_id: str = ""    # Vertex AI Search datastore for Knowledge/playbooks/
    knowledge_datastore_id: str = ""   # Vertex AI Search datastore for general Knowledge/


class Settings(BaseModel):
    gcp: GCPConfig
    sheet: SheetConfig
    models: ModelAliases
    memory_bank: MemoryBankConfig = Field(default_factory=MemoryBankConfig)
    pubsub: PubSubConfig = Field(default_factory=PubSubConfig)
    projects: dict[str, ProjectConfig] = Field(default_factory=dict)
    apps_script: AppsScriptConfig = Field(default_factory=AppsScriptConfig)
    chat: ChatConfig = Field(default_factory=ChatConfig)
    vertex_search: VertexSearchConfig = Field(default_factory=VertexSearchConfig)

    @property
    def GCP_PROJECT_ID(self) -> str:
        return self.gcp.project_id

    def get_project(self, project_id: str) -> Optional[ProjectConfig]:
        return self.projects.get(project_id)


# ── Module-level singleton ─────────────────────────────────────────────────

_settings: Optional[Settings] = None

_DEFAULT_PATH = Path(__file__).parent / "settings.yaml"


def load_settings(path: Optional[Path] = None) -> Settings:
    """
    Load and validate settings.yaml. Caches the result for the process lifetime.

    Args:
        path: Override path to the YAML file (used in tests).

    Raises:
        FileNotFoundError: settings.yaml does not exist.
        ValueError:        YAML is missing required fields.
    """
    global _settings
    resolved = path or _DEFAULT_PATH
    if not resolved.exists():
        raise FileNotFoundError(
            f"settings.yaml not found at '{resolved}'. "
            "Copy config/settings.yaml.template to config/settings.yaml and fill in your values."
        )
    with resolved.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    _settings = Settings.model_validate(raw)
    return _settings


def get_settings() -> Settings:
    """Return the cached Settings instance, loading from disk if needed."""
    if _settings is None:
        return load_settings()
    return _settings


def _reset_for_testing() -> None:
    """Clear the cached singleton. Only call this from tests."""
    global _settings
    _settings = None
