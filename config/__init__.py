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
    max_hop_count: int = 5  # A2A loop prevention — messages exceeding this hop count are dropped


class ProjectConfig(BaseModel):
    sheet_id: str = ""
    drive_folder_id: str = ""


class AppsScriptConfig(BaseModel):
    script_id: str = ""
    deployment_id: str = ""
    webhook_url: str = ""
    timezone: str = "America/Los_Angeles"


class ChatConfig(BaseModel):
    owner_space: str = ""  # spaces/<id> — owner DM for morning briefings


class VertexSearchConfig(BaseModel):
    location: str = "global"
    playbook_datastore_id: str = ""  # Vertex AI Search datastore for Knowledge/playbooks/
    knowledge_datastore_id: str = ""  # Vertex AI Search datastore for general Knowledge/


class DocsConfig(BaseModel):
    service_account_key: str = ""  # Path to SA key JSON; leave empty to use ADC
    blueprints_folder_id: str = ""  # Default Drive folder ID for Blueprint Docs
    knowledge_atlas_doc_id: str = ""  # Google Doc ID for the Knowledge Atlas (Memory Mirror)
    dwd_subject: str = ""  # Workspace user email to impersonate via DWD (Cloud Run path)


class GoogleSearchConfig(BaseModel):
    api_key_secret: str = "GOOGLE_SEARCH_API_KEY"  # Secret Manager key name
    cx_secret: str = "GOOGLE_SEARCH_CX"  # Secret Manager CX name
    max_search_depth: int = 3  # Recursive query depth per mandate
    max_queries_per_mandate: int = 15  # Hard cap on queries per RESEARCH_MANDATE


class MemoryConfig(BaseModel):
    max_active_entries: dict[str, int] = Field(default_factory=dict)
    max_boot_chars: int = 32_000


class GmailConfig(BaseModel):
    monitored_address: str = ""  # Inbox watched by Gmail push
    sender_address: str = ""  # Authorised reply-from address
    alert_address: str = (
        ""  # Destination for system error alerts — must NOT equal monitored_address
    )
    label_id: str = ""  # Gmail label ID (e.g. Label_6)
    pubsub_topic: str = ""  # Full topic path: projects/<pid>/topics/...
    max_results: int = 50
    trigger_keyword: str = (
        ""  # If set, only process emails whose subject contains this string (case-insensitive)
    )


class OutboundConfig(BaseModel):
    max_emails_per_task: int = Field(
        default=3, gt=0
    )  # Hard cap per single task execution (Rule 26.2)
    max_publishes_per_task: int = Field(
        default=10, gt=0
    )  # Hard cap on Pub/Sub publishes per task (Rule 26.2)
    flood_window_minutes: int = Field(
        default=60, gt=0
    )  # Rolling window for flood detection (Rule 26.3)
    flood_threshold: int = Field(default=10, gt=0)  # Max emails in window before abort (Rule 26.3)


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
    docs: DocsConfig = Field(default_factory=DocsConfig)
    google_search: GoogleSearchConfig = Field(default_factory=GoogleSearchConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    gmail: GmailConfig = Field(default_factory=GmailConfig)
    outbound: OutboundConfig = Field(default_factory=OutboundConfig)

    @property
    def GCP_PROJECT_ID(self) -> str:
        return self.gcp.project_id

    def get_project(self, project_id: str) -> ProjectConfig | None:
        return self.projects.get(project_id)


# ── Module-level singleton ─────────────────────────────────────────────────

_settings: Settings | None = None

_DEFAULT_PATH = Path(__file__).parent / "settings.yaml"


def load_settings(path: Path | None = None) -> Settings:
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
