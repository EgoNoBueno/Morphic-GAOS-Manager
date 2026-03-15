# GAOS Tools Specification

**Morphic-G AOS** — Shared Tool Module API Reference

> This document defines the public API for every module in `tools/`. All agents call these functions; none may call the underlying Google SDK directly — the tool layer is the only permitted interface. This ensures consistent `project_id` scoping, batching, error handling, and retry behaviour across the entire system.
>
> **Prerequisites:** `config/settings.yaml` must be loaded before calling any tool. `get_secret()` must succeed for all secrets required by a tool before that tool is called. See `GAOS-Deploy-Spec.md` for provisioning steps.

---

## 1. Design Principles

| Principle | Rule |
|-----------|------|
| **`project_id` mandatory** | Every function that touches a Google service takes `project_id` as a required parameter. No function reads from a global or ambient project context. |
| **Raise, don't swallow** | Tools raise typed exceptions on failure. They do not catch and silently return `None`. The caller decides how to handle errors. |
| **No shared mutable state** | Tools are stateless pure functions. No module-level caches, no singletons, no thread-local state. |
| **Batch by default** | Sheet and Memory Bank tools provide batch variants. Single-row calls are convenience wrappers over the batch path. |
| **Retry on transient errors** | Rate limit (`429`) and server errors (`5xx`) are retried with exponential backoff up to 3 attempts before raising. All other errors raise immediately. |
| **No model calls** | Tools do not call any LLM. They are I/O wrappers only. Any intelligence is applied by the agent before or after the tool call. |

---

## 2. `tools/secrets.py`

Accessor for Google Secret Manager. Called during boot sequence before any other tool.

```python
from google.cloud import secretmanager
from google.api_core.exceptions import NotFound, PermissionDenied

def get_secret(secret_id: str, project_id: str) -> str:
    """
    Retrieve the latest version of a secret from Google Secret Manager.

    Args:
        secret_id:  The secret name as registered in Secret Manager
                    (e.g., "GEMINI_API_KEY").
        project_id: The GCP project that owns the secret.

    Returns:
        The secret value as a UTF-8 string.

    Raises:
        SecretNotFoundError:    Secret does not exist. Agent must log
                                STARTUP_FAILURE and exit.
        SecretAccessDenied:     Service account lacks
                                roles/secretmanager.secretAccessor for this
                                secret. Agent must log STARTUP_FAILURE and exit.
        SecretManagerError:     Unrecoverable API error.
    """
```

### Error Types

```python
class SecretNotFoundError(Exception):
    """Secret ID does not exist in Secret Manager."""

class SecretAccessDenied(Exception):
    """Caller's service account cannot access this secret."""

class SecretManagerError(Exception):
    """Unrecoverable Secret Manager API error."""
```

### Usage Pattern

```python
# In agent boot sequence — always fail fast
try:
    gemini_key = get_secret("GEMINI_API_KEY", settings.GCP_PROJECT_ID)
except (SecretNotFoundError, SecretAccessDenied) as e:
    logger.critical("STARTUP_FAILURE", extra={"reason": str(e)})
    sys.exit(1)
```

---

## 3. `tools/google_sheets.py`

All Sheets operations. Uses `gspread` with a service account credential loaded from Secret Manager at module init. Respects the 300 req/min project quota and 60 req/min per-user quota.

### Initialisation (module-level, called once per invocation)

```python
def init_sheets_client(project_id: str) -> None:
    """
    Authenticate gspread with the GSHEETS_SERVICE_ACCOUNT secret and
    open the workbook for this project. Must be called before any other
    function in this module.

    Raises:
        SecretNotFoundError: Propagated from get_secret().
        WorkbookNotFoundError: Spreadsheet ID for project_id is not in
                              settings.yaml under projects.<project_id>.sheet_id.
    """
```

### Core Functions

```python
def append_row(tab: str, row: dict, project_id: str) -> None:
    """
    Append one row to the named Sheet tab. Keys in `row` must match
    the tab's header row exactly (case-sensitive).

    Prefer batch_append_rows() when writing more than one row.

    Raises:
        TabNotFoundError:    Named tab does not exist in the workbook.
        RateLimitError:      Still failing after 3 retries with backoff.
        SheetsWriteError:    Unrecoverable write error.
    """

def batch_append_rows(tab: str, rows: list[dict], project_id: str) -> None:
    """
    Append multiple rows in a single API call (values_append).
    All rows must have identical key sets matching the tab header.

    Use this for all writes of 2+ rows — it counts as one API request
    regardless of row count.

    Raises:
        TabNotFoundError, RateLimitError, SheetsWriteError (same as append_row).
    """

def get_all_records(tab: str, project_id: str) -> list[dict]:
    """
    Return all rows in a tab as a list of dicts keyed by the header row.
    Empty rows are excluded. Result is not cached — call once per session
    and cache in working_memory if needed.

    Raises:
        TabNotFoundError, RateLimitError, SheetsReadError.
    """

def read_range(tab: str, a1_range: str, project_id: str) -> list[list]:
    """
    Return raw cell values for an A1-notation range (e.g., "A2:D50").
    Returns list of rows; each row is a list of cell values (str or empty str).

    Raises:
        TabNotFoundError, RateLimitError, SheetsReadError.
    """

def update_row(tab: str, row_index: int, updates: dict,
               project_id: str) -> None:
    """
    Update specific columns in an existing row. `row_index` is the
    1-based sheet row number (row 1 = header). `updates` is a dict of
    {column_header: new_value} — only named columns are written.

    Raises:
        TabNotFoundError, RowNotFoundError, RateLimitError, SheetsWriteError.
    """

def find_row(tab: str, column: str, value: str,
             project_id: str) -> dict | None:
    """
    Return the first row where `column` equals `value`, as a dict.
    Returns None if no matching row exists. Case-sensitive match.

    Raises:
        TabNotFoundError, RateLimitError, SheetsReadError.
    """

def find_rows(tab: str, column: str, value: str,
              project_id: str) -> list[dict]:
    """
    Return all rows where `column` equals `value`. Returns empty list
    if none match.
    """
```

### Error Types

```python
class TabNotFoundError(Exception):
    """Named tab does not exist in the workbook."""

class RowNotFoundError(Exception):
    """Row index is out of range for the given tab."""

class RateLimitError(Exception):
    """Sheets API quota exceeded after retry exhaustion."""

class SheetsReadError(Exception):
    """Unrecoverable read error."""

class SheetsWriteError(Exception):
    """Unrecoverable write error."""

class WorkbookNotFoundError(Exception):
    """Spreadsheet ID not found or inaccessible."""
```

### Tab Name Quoting Rule

The Sheets API rejects range strings that contain unquoted tab names with spaces (e.g., `Sales by Product!A2:D` returns 400 "Unable to parse range"). All tab names that contain spaces **must** be wrapped in single quotes inside the range string.

**Rule:** Store every tab name constant with the single quotes embedded:
```python
# Correct — single quotes are part of the string value
SALES_TAB   = "'Sales by Product'"
AD_TAB      = "'Ad Response/Spend/Recommendations'"
SHIPPING_TAB = "'Shipping and Receiving'"

# Then build range strings normally:
range_str = f"{SALES_TAB}!A2:D"   # → 'Sales by Product'!A2:D  ✓

# Wrong — the API will 400 on this
range_str = f"Sales by Product!A2:D"  # ✗
```

`google_sheets.py` must apply this quoting to the `tab` parameter internally for every function that builds a range string. Single-word tab names (e.g., `Accounting`, `Logs`) do not require quoting but quoting them is harmless.

### Rate Limit Compliance

All write calls are tracked against a module-level token bucket (300 req/min). If the bucket is empty, the call blocks up to 5 seconds before retrying. Agents that need to make many writes must use `batch_append_rows()` — a batch write counts as **1 request** regardless of row count.

---

## 4. `tools/pubsub.py`

Pub/Sub publisher for A2A messaging. Messages must conform to the `A2AMessage` schema (`GAOS-Manager-Spec.md` §10.2).

```python
from google.cloud import pubsub_v1
from pydantic import BaseModel

def publish(topic_name: str, message: A2AMessage,
            project_id: str) -> str:
    """
    Serialize and publish one A2AMessage to the named topic.

    `topic_name` is the short name without the full resource path
    (e.g., "agent/beacon/events"). The full topic path is constructed
    as: projects/<GCP_PROJECT_ID>/topics/<topic_name_with_slashes_as_dots>.

    Returns:
        message_id: The Pub/Sub-assigned message ID (string).

    Raises:
        TopicNotFoundError:   Topic does not exist; agent must create it
                              on boot (see agent boot sequence §6).
        PubSubPublishError:   Unrecoverable publish error.
    """

def ensure_topic_exists(topic_name: str, project_id: str) -> None:
    """
    Idempotent topic creation. Creates the topic if it does not exist.
    Safe to call on every boot. Used in the agent boot sequence (step 5).

    Raises:
        PubSubAdminError: Cannot create topic (permissions or quota error).
    """

def decode_push_message(envelope: dict) -> A2AMessage:
    """
    Decode a Pub/Sub push delivery envelope (as received by a Cloud Run
    HTTP handler) into a validated A2AMessage.

    The envelope format is:
        {
          "message": {
            "data": "<base64-encoded JSON>",
            "messageId": "...",
            ...
          },
          "subscription": "..."
        }

    Raises:
        MessageDecodeError:  Base64 or JSON decode failure.
        MessageValidationError: Decoded message fails A2AMessage schema validation.
    """
```

### Error Types

```python
class TopicNotFoundError(Exception):
    """Pub/Sub topic does not exist."""

class PubSubPublishError(Exception):
    """Unrecoverable publish error."""

class PubSubAdminError(Exception):
    """Cannot create or manage Pub/Sub topics."""

class MessageDecodeError(Exception):
    """Cannot decode the raw Pub/Sub push envelope."""

class MessageValidationError(Exception):
    """Decoded message does not satisfy A2AMessage schema."""
```

---

## 5. `tools/drive.py`

Google Drive accessor for the `Knowledge/` procedural knowledge folder. File paths are relative to the project's root `Knowledge/` folder (Drive folder ID stored in the Project Registry).

```python
def read_file(file_path: str, project_id: str) -> str:
    """
    Read a Markdown file from the Knowledge/ folder tree and return
    its full text content.

    `file_path` is relative to Knowledge/ root
    (e.g., "procedures/invoice_matching.md").

    Returns:
        File content as a UTF-8 string.

    Raises:
        KnowledgeFileNotFoundError: File does not exist at the given path.
        DriveReadError:             Unrecoverable Drive API error.
    """

def write_file(file_path: str, content: str, project_id: str) -> str:
    """
    Write (create or overwrite) a Markdown file in the Knowledge/ folder.
    Only callable by Nexus-Prime post-approval. Agents must not call this
    directly — use the knowledge update flow in GAOS-Memory-Spec.md §10.

    Returns:
        drive_file_id: The Google Drive file ID of the written file.

    Raises:
        DriveWriteError:     Unrecoverable write error.
        DrivePermissionError: Caller's service account lacks write access.
    """

def copy_file(source_path: str, dest_path: str, project_id: str) -> str:
    """
    Copy a file within the Knowledge/ tree (used for version archiving).
    Creates all intermediate folders if they do not exist.

    Returns:
        drive_file_id: The file ID of the newly created copy.

    Raises:
        KnowledgeFileNotFoundError, DriveWriteError.
    """

def list_folder(folder_path: str, project_id: str) -> list[str]:
    """
    Return a list of relative file paths under a given Knowledge/ subfolder.
    Useful for scanning available procedures or workflows.

    Raises:
        KnowledgeFolderNotFoundError, DriveReadError.
    """
```

### Error Types

```python
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
```

### Access Control Note

`write_file()` and `copy_file()` are write-access functions. The calling agent's service account must have `roles/drive.file` scoped to the `Knowledge/` folder. Only Nexus-Prime's service account has write access to this folder. Domain orchestrators have read-only access.

---

## 6. `tools/webhook_sender.py`

Sends HMAC-signed POST requests to the Apps Script `doPost` webhook endpoint. Used when an agent submits a proposal to the Approval Gate programmatically.

```python
import hashlib, hmac, json
from datetime import datetime, timezone

def post_to_webhook(payload: dict, project_id: str) -> None:
    """
    Sign the JSON payload with HMAC-SHA256 and POST it to the
    WEBHOOK_URL configured in Secret Manager.

    The function:
      1. Serializes payload to canonical JSON (sorted keys, no whitespace).
      2. Computes HMAC-SHA256(body, WEBHOOK_HMAC_SECRET).
      3. Attaches the hex digest as the X-AOS-Signature header.
      4. Adds X-AOS-Project-ID and X-AOS-Timestamp headers.
      5. POSTs to WEBHOOK_URL with Content-Type: application/json.

    If payload contains a `code` field, computes SHA-256 of the code
    value and adds it as `code_sha256` to the payload before signing.
    This value is written to col M of Agent_Approvals by the Apps Script.

    Raises:
        WebhookDeliveryError: Non-2xx response after retry exhaustion.
        WebhookTimeoutError:  Request timed out (10-second limit).
        SecretNotFoundError:  WEBHOOK_HMAC_SECRET or WEBHOOK_URL not found.
    """
```

### Error Types

```python
class WebhookDeliveryError(Exception):
    """Non-2xx HTTP response from the Apps Script endpoint."""

class WebhookTimeoutError(Exception):
    """Request timed out after 10 seconds."""
```

### Security Notes

- `WEBHOOK_HMAC_SECRET` and `WEBHOOK_URL` are always loaded from Secret Manager — never from `settings.yaml` or environment variables.
- The payload is serialized with `json.dumps(payload, sort_keys=True, separators=(',', ':'))` to ensure deterministic HMAC computation.
- `code_sha256` is computed **before** HMAC signing, so the hash covers the code content and is protected by the HMAC.
- Never log the raw `WEBHOOK_HMAC_SECRET` or the full `X-AOS-Signature` header.

---

## 7. `tools/project_registry.py`

Loads and validates the Project Registry tab. Called during agent boot sequence (step 4).

```python
from pydantic import BaseModel

class ProjectRecord(BaseModel):
    project_id: str          # Unique slug (e.g., "acme", "northstar")
    display_name: str        # Human-readable name
    status: str              # "Active" | "Paused" | "Archived"
    sheet_id: str            # Google Sheets workbook ID for this project
    drive_folder_id: str     # Knowledge/ root Drive folder ID
    pubsub_prefix: str       # Topic prefix (usually matches project_id)
    created_at: str          # ISO 8601 timestamp

def load_project_registry(project_id: str) -> list[ProjectRecord]:
    """
    Read the Project Registry tab from the control plane Sheet and
    return all ProjectRecord entries.

    The `project_id` argument here refers to the system-level GCP project
    (for Sheets authentication), not the AOS project namespace.

    Raises:
        TabNotFoundError:          Project Registry tab missing.
        ProjectRegistryParseError: A row fails ProjectRecord validation.
    """

def get_active_project_ids(project_id: str) -> list[str]:
    """
    Return a list of project_id strings where status == "Active".
    Used by agents to validate incoming task project_ids at boot.
    """

def get_project(project_id: str, system_project_id: str) -> ProjectRecord:
    """
    Return the ProjectRecord for a specific AOS project_id.

    Raises:
        ProjectNotFoundError: No row with this project_id exists.
        ProjectPausedError:   Project exists but status is "Paused".
        ProjectArchivedError: Project exists but status is "Archived".
    """
```

### Error Types

```python
class ProjectRegistryParseError(Exception):
    """A row in the Project Registry fails schema validation."""

class ProjectNotFoundError(Exception):
    """No project with the given project_id exists in the registry."""

class ProjectPausedError(Exception):
    """Project exists but is currently paused."""

class ProjectArchivedError(Exception):
    """Project exists but has been archived."""
```

---

## 8. `tools/memory.py`

Memory layer operations. **See `Docs/GAOS-Memory-Spec.md` for full schema definitions, confidence scoring, and the self-learning loop.** This section documents only the public function signatures.

```python
# Layer 2 — Episodic
def query_episodic(agent_id: str, project_id: str,
                   task_type: str, limit: int = 5) -> list[dict]:
    """Full implementation in GAOS-Memory-Spec.md §4."""

# Layer 3 — Observation Buffer
def flush_observations(observations: list[dict],
                        project_id: str) -> None:
    """Full implementation in GAOS-Memory-Spec.md §8."""

# Layer 4 — Semantic Memory
def load_domain_memory(agent_id: str, project_id: str) -> dict:
    """Full implementation in GAOS-Memory-Spec.md §6."""

def write_approved_memory(entry: "MemoryEntry",
                           project_id: str) -> str:
    """
    Nexus-Prime only. Full implementation in GAOS-Memory-Spec.md §6.

    Raises:
        UnauthorizedMemoryWrite: Caller is not Nexus-Prime's service account.
    """
```

### Restrictions

| Function | Tier 1 | Tier 2 | Tier 3 |
|----------|--------|--------|--------|
| `query_episodic` | ✅ | ✅ | ✗ |
| `flush_observations` | ✅ | ✅ | ✗ (returns observations in AgentOutput; orchestrator flushes) |
| `load_domain_memory` | ✅ | ✅ | ✗ |
| `write_approved_memory` | ✅ (Nexus-Prime only) | ✗ | ✗ |

---

## 9. Tool Usage Rules Summary

| Rule | Detail |
|------|--------|
| Always call `init_sheets_client()` before any Sheet operation | This runs once in the boot sequence; do not call per-task |
| Always use `batch_append_rows()` for ≥ 2 rows | Single-row convenience wrapper is one API call — batching is always preferred |
| Never call Drive `write_file()` from a domain orchestrator | Orchestrators propose Drive changes; Nexus-Prime applies them post-approval |
| Never call `write_approved_memory()` from any agent except Nexus-Prime | Unauthorized writes are logged as Priority-5 security events |
| Always propagate `project_id` into every tool call | There is no ambient project context — dropping it is a bug |
| Catch tool errors at the agent level | Do not retry inside a tool call; the tool raises after its own backoff. The agent decides whether to escalate or park. |

---

## 10. Reference Index

| Topic | Location |
|-------|----------|
| A2AMessage schema | `GAOS-Manager-Spec.md` §10.2 |
| Webhook HMAC threat model and test matrix | `GAOS-Manager-Spec.md` §15.2 |
| Approval Gate column definitions | `GAOS-Manager-Spec.md` §14 |
| Secret inventory | `GAOS-Manager-Spec.md` §15.1 |
| Sheets quota limits | `GAOS-Manager-Spec.md` §9.4 |
| Memory layer schemas and self-learning loop | `GAOS-Memory-Spec.md` |
| Agent boot sequence (tool call order) | `GAOS-Agent-Spec.md` §6 |
| Project Registry tab schema | `GAOS-Manager-Spec.md` §2 |
| Drive Knowledge/ folder structure | `GAOS-Memory-Spec.md` §7 |
