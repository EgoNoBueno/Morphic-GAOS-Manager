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

> ⚠️ **Warning — Tab names with spaces must be single-quoted in range strings:** The Sheets API v4 requires tab names that contain spaces (or special characters) to be wrapped in single quotes when used in A1 notation range strings. Omitting the quotes causes a `400 "Unable to parse range"` error even though the tab name itself is valid.
>
> ```python
> # ❌ Wrong — produces 400 if tab name contains a space
> range_str = f"Raw Data!C2:C"
>
> # ✅ Correct — embed the single quotes in the constant
> RAW_TAB = "'Raw Data'"      # note the inner single quotes
> range_str = f"{RAW_TAB}!C2:C"   # → 'Raw Data'!C2:C
> ```
>
> **Reference:** [Google Sheets API concepts — A1 notation](https://developers.google.com/workspace/sheets/api/guides/concepts#a1_notation): *"Single quotes are required for sheet names with spaces or special characters."*

### Initialisation (module-level, called once per invocation)

```python
def init_sheets_client(project_id: str) -> None:
    """
    Authenticate gspread via Application Default Credentials (ADC) and
    open the workbook for this project. Must be called before any other
    function in this module.

    On Cloud Run, ADC resolves to the attached service account identity.
    Locally, ADC is provided by `gcloud auth application-default login`.

    Raises:
        WorkbookNotFoundError: Spreadsheet ID for project_id is not in
                              settings.yaml under projects.<project_id>.sheet_id,
                              or the spreadsheet is inaccessible.
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

def get_all_records_with_row_numbers(
        tab: str, project_id: str) -> list[tuple[int, dict]]:
    """
    Same as `get_all_records` but returns (1-based-row-number, record) pairs.
    Row 1 is always the header; data starts at row 2. The row number is the
    physical sheet row number as used by `delete_rows()`.

    Use this instead of `get_all_records` whenever you need to delete specific
    rows after reading — otherwise row numbers shift after each deletion.

    Raises:
        TabNotFoundError, RateLimitError, SheetsReadError.
    """

def delete_rows(tab: str, row_numbers: list[int], project_id: str) -> None:
    """
    Delete the given 1-based row numbers from `tab`. Rows are deleted in
    descending order so that earlier deletions do not shift the numbers of
    later rows.

    Row numbers ≤ 1 (the header) are silently skipped.

    Rate-limited via the shared token bucket (same 300 req/min budget).

    Raises:
        TabNotFoundError, RateLimitError, SheetsWriteError.
    """

def read_range(tab: str, a1_range: str, project_id: str) -> list[list]:
    """
    Return raw cell values for an A1-notation range (e.g., "A2:D50").
    Returns list of rows; each row is a list of cell values (str or empty str).

    Raises:
        TabNotFoundError, RateLimitError, SheetsReadError.
    """

def update_row(tab: str, row_index: int | str, updates: dict,
               project_id: str) -> None:
    """
    Update specific columns in an existing row.

    `row_index` can be:
    - **int**: 1-based sheet row number (row 1 = header).
    - **str**: A value to look up in the first column (``ID``) of the tab.
      Useful when the caller knows the UUID primary key but not the row number.

    `updates` is a dict of {column_header: new_value} — only named columns
    are written.

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

def publish(topic_name: str, message: A2AMessage) -> str:
    """
    Serialize and publish one A2AMessage to the named topic.

    The GCP project is taken from ``settings.GCP_PROJECT_ID`` — it is fixed
    infrastructure and does not vary per GAOS ``project_id``.

    ``topic_name`` is the dot-delimited short name without the full
    resource path (e.g., ``"agent.nexus-prime.events"``). The full topic
    path is constructed as: ``projects/<GCP_PROJECT_ID>/topics/<topic_name>``.

    Note: Slash-delimited names (``"agent/beacon/events"``) are accepted
    for backward compatibility — slashes are replaced with dots
    internally — but all new call sites must use dot-delimited names.

    Returns:
        message_id: The Pub/Sub-assigned message ID (string).

    Raises:
        TopicNotFoundError:   Topic does not exist; agent must create it
                              on boot (see agent boot sequence §6).
        PubSubPublishError:   Unrecoverable publish error.
    """

def ensure_topic_exists(topic_name: str) -> None:
    """
    Idempotent topic creation. Creates the topic if it does not exist.
    Safe to call on every boot. Used in the agent boot sequence (step 5).
    GCP project is taken from ``settings.GCP_PROJECT_ID``.

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

def write_playbook(doc: "PlaybookDoc", body: str, project_id: str) -> str:
    """
    Write a Playbook Markdown document to ``Knowledge/playbooks/`` in Drive.

    Generates YAML front-matter from ``doc`` and prepends it to ``body``
    before calling ``write_file()``. The filename is derived from
    ``doc.project_id`` and a URL-safe slug of ``doc.title``.

    Args:
        doc:        PlaybookDoc instance — provides all front-matter fields
                    (title, domain, owner_agent, version, project_id,
                    created_from_vision, last_updated, approved_by, status, tags).
                    **Defined in** ``models/__init__.py``; import as
                    ``from models import PlaybookDoc``.
        body:       Markdown body text (Objective, Milestones, Constraints, etc.).
        project_id: AOS project namespace.

    Returns:
        drive_file_id: The Google Drive file ID of the written playbook.

    Raises:
        DriveWriteError, DrivePermissionError.
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

    If payload contains a `Proposed Code` or `code` field, computes SHA-256
    of that value and adds it as `code_sha256` to the payload before signing.
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
    """Non-2xx HTTP response from the Apps Script endpoint after retries."""

class WebhookTimeoutError(Exception):
    """Request timed out after 10 seconds."""

class WebhookURLError(Exception):
    """Webhook URL is invalid or resolves to a private/internal address."""
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
    project_id: str                # Unique slug (e.g., "acme", "northstar")
    project_name: str              # Human-readable display name
    status: str                    # "Active" | "Pending" | "Paused" | "Archived"
    sheet_workbook_id: str         # Google Sheets workbook ID for this project
    drive_folder_id: str           # Knowledge/ root Drive folder ID
    budget_ceiling_usd: str = ""  # Monthly LLM spend ceiling (blank = no limit)
    owner_email: str = ""         # Google account to notify on escalations
    created_date: str = ""        # ISO 8601 date the project was registered
    notes: str = ""               # Free-text context for Nexus-Prime

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

# Semantic search across a Memory Bank corpus
def query_memory_bank(query: str, corpus: str, project_id: str,
                      top_k: int = 5,
                      similarity_threshold: float = 0.80) -> list[dict]:
    """
    Perform a semantic similarity search against a Vertex AI Memory Bank corpus.

    Args:
        query:                The text query (e.g., an error fingerprint).
        corpus:               Corpus ID (e.g., "gaos-ledger").
        project_id:           GCP project that owns the Memory Bank.
        top_k:                Maximum number of results to return.
        similarity_threshold: Minimum similarity score [0.0–1.0] to include.

    Returns:
        List of matching memory dicts (content, memory_id, similarity, tags).

    Raises:
        MemoryBankError: Unrecoverable Vertex AI API error.
    """

# Layer 3 — Observation Buffer
def flush_observations(observations: list[dict],
                        project_id: str) -> None:
    """Full implementation in GAOS-Memory-Spec.md §8."""

# Layer 4 — Semantic Memory
def load_domain_memory(agent_id: str, project_id: str) -> dict:
    """
    Batch-fetch all active memory entries for this agent's domain from the
    Vertex AI Memory Bank. Called once per agent boot.

    Returns a dict grouped by knowledge_type:
    ``{"fact": [...], "pattern": [...], "rule": [...], "preference": []}``,
    plus two metadata keys:

    - ``_truncated`` (bool): True if the 32,000-character boot budget was exceeded.
    - ``_dropped_count`` (int): Number of entries dropped to fit the budget.

    When truncation occurs, entries are dropped in reverse-priority order:
    facts are kept first, then preferences, then patterns, then rules.
    ``warnings.warn`` (RuntimeWarning) is fired on truncation.

    Full design: GAOS-Memory-Spec.md §6 and §6.2.

    Raises:
        MemoryBankError: Vertex AI API failure.
    """

def count_active_entries(agent_id: str, project_id: str) -> int:
    """
    Return the number of active Memory Bank entries for this agent.

    Used by the nightly promotion sweep to enforce per-agent size caps
    before each write (see GAOS-Memory-Spec.md §6.1).

    Args:
        agent_id:   The orchestrator whose entries are counted.
        project_id: GCP project that owns the Memory Bank.

    Returns:
        Count of entries where ``active = True`` for this agent.

    Raises:
        MemoryBankError: Vertex AI API failure.
    """

def write_approved_memory(entry: "MemoryEntry",
                           project_id: str) -> str:
    """
    Nexus-Prime only. Full implementation in GAOS-Memory-Spec.md §6.

    Raises:
        UnauthorizedMemoryWrite: Caller is not Nexus-Prime's service account.
    """

class MemoryBankError(Exception):
    """Unrecoverable Vertex AI Memory Bank API error."""
```

### Restrictions

| Function | Tier 1 | Tier 2 | Tier 3 |
|----------|--------|--------|--------|
| `query_episodic` | ✅ | ✅ | ✗ |
| `flush_observations` | ✅ | ✅ | ✗ (returns observations in AgentOutput; orchestrator flushes) |
| `load_domain_memory` | ✅ | ✅ | ✗ |
| `count_active_entries` | ✅ (Nexus-Prime / nightly sweep) | ✗ | ✗ |
| `write_approved_memory` | ✅ (Nexus-Prime only) | ✗ | ✗ |
| `query_memory_bank` | ✅ (Nexus-Prime only) | ✗ | ✗ |

---

## 9. `tools/bigquery.py`

BigQuery writer for cold-storage logging and analytics. All log writes from agents go through this module — no agent may call the BigQuery SDK directly.

```python
def insert_row(table_ref: str, row: dict, project_id: str = "") -> None:
    """
    Stream one row into a BigQuery table.

    Args:
        table_ref:  Unqualified ``dataset.table``
                    (e.g., "aos_logs.task_outcomes") or fully qualified
                    ``project.dataset.table``.
        row:        Dict keyed by column name. Values must be JSON-serialisable.
        project_id: Unused — present for API symmetry with other tools.
                    The GCP project is always read from settings.GCP_PROJECT_ID.

    Raises:
        BigQueryInsertError: API call failed after 3 retries.
        BigQueryRowError:    BigQuery rejected the row (schema mismatch or
                             invalid value).
    """

def insert_rows(table_ref: str, rows: list[dict], project_id: str = "") -> None:
    """
    Stream multiple rows into a BigQuery table in a single API call.

    Prefer this over calling ``insert_row()`` in a loop for batches of 2+ rows.
    Empty ``rows`` list is a no-op.

    Raises:
        BigQueryInsertError: API call failed after 3 retries.
        BigQueryRowError:    One or more rows were rejected.
    """

def replace_rows(
    table_ref: str,
    rows: list[dict],
    project_id: str = "",
) -> None:
    """
    Full-replace the contents of a BigQuery table via TRUNCATE + streaming INSERT.

    Issues ``TRUNCATE TABLE`` to clear the table, then streams all rows via
    ``insert_rows()``. If ``rows`` is empty the table is truncated and the
    function returns immediately.

    ``TRUNCATE TABLE`` is used instead of ``DELETE FROM … WHERE TRUE`` because
    BQ blocks DML DELETE on tables with rows in the streaming buffer (< ~90 min
    old). TRUNCATE bypasses this restriction.

    > ⚠️ **Warning — BQ streaming buffer blocks DML DELETE:** Using
    > ``DELETE FROM … WHERE TRUE`` on a streaming-insert table will raise
    > ``400 UPDATE or DELETE … would affect rows in the streaming buffer`` if
    > any rows were streamed in the last ~90 minutes. Always use
    > ``TRUNCATE TABLE`` for full-table clears on streaming tables.

    Args:
        table_ref:  Unqualified ``dataset.table`` or fully qualified
                    ``project.dataset.table``.
        rows:       List of row dicts keyed by column name.
        project_id: Unused (present for API symmetry).

    Raises:
        BigQueryInsertError: The TRUNCATE query failed.
        BigQueryRowError:    One or more rows were rejected during streaming insert.
    """
```

### Error Types

```python
class BigQueryInsertError(Exception):
    """Streaming insert failed after retry exhaustion."""

class BigQueryRowError(Exception):
    """BigQuery rejected one or more rows (schema mismatch or invalid value)."""
```

### Usage Pattern

```python
# Standard task outcome log — used by every agent after task completion
from tools.bigquery import insert_row

insert_row("aos_logs.task_outcomes", {
    "task_id":        task_id,
    "project_id":     project_id,
    "agent_id":       "ledger",
    "task_type":      "invoice_matching",
    "status":         "success",
    "result_summary": "Matched 12 invoices",
    "total_cost_usd": 0.004,
    "timestamp":      datetime.now(UTC).isoformat(),
})
```

### Table Reference

All standard tables live in the `aos_logs` dataset provisioned in `GAOS-Deploy-Spec.md §7`. Always use the short `dataset.table` form — the GCP project is injected automatically from `settings.GCP_PROJECT_ID`.

---

## 10. `agents/__init__.py` — LLM Routing Helper

All LLM calls in the system go through `_call_model()` in `agents/__init__.py`. This is not a tool module (it does not live in `tools/`) but it is the shared abstraction for model access and must be treated as a first-class interface.

### Signature

```python
def _call_model(
    prompt: str,
    model: str,
    system_prompt: str = "",
    parse_json: bool = False,
    web_access: bool = False,
) -> ModelResponse:
```

### Routing Logic

| `model` value | Route | Notes |
|---------------|-------|-------|
| starts with `ollama/` | `_call_model_ollama()` → local Ollama server | Increments fallback counter on `httpx.TimeoutException`, `httpx.ConnectError`, or `httpx.HTTPStatusError`; **raises `RuntimeError`** — no Gemini fallback. Callers must catch and handle gracefully; must NOT re-invoke `_call_model()` with a Gemini alias. |
| any other string | `_call_model_gemini()` → `google.genai` (AI Studio) | Raises `RuntimeError` immediately if `GEMINI_API_KEY` is unavailable; catches `ResourceExhausted` (429) with a `WARNING` log then re-raises |

### Ollama Call Details

- **Host:** fetched from Secret Manager as `OLLAMA_HOST` at call-time; defaults to `http://localhost:11434` if the secret fetch fails (intentional local-dev fallback)
- **Tunnel:** `OLLAMA_HOST` points to a **Cloudflare Tunnel** URL (`https://<tunnel-id>.cfargotunnel.com`) — a permanent, stable URL that never changes. Set up once via `scripts/setup_cloudflare_tunnel.py`. No loca.lt, no watchdog process, no Secret Manager drift.
- **Timeout:** `LOCAL_MODEL_TIMEOUT_SECONDS` from `settings.yaml` (default: 90 seconds)
- **On failure:** on `httpx.TimeoutException`, `httpx.ConnectError`, or `httpx.HTTPStatusError`, the function emits a `logger.error` (exception type, host URL, model name, failure count) then **raises `RuntimeError`** — the Gemini fallback is permanently disabled. No Gemini tokens are spent. The failure counter (`get_ollama_fallback_count()`) still increments so operations can detect repeated failures.

> ⚠️ **Fallback is disabled:** The Gemini fallback that existed in early development (auto-routing to `LOCAL_MODEL_FALLBACK` on Ollama timeout) was permanently removed. All Ollama failures now surface as `RuntimeError` so the caller decides how to handle them. Callers that handle the error gracefully (e.g. `chat_respond` returns a user-facing apology string) must catch `Exception` and respond appropriately — they must NOT re-call `_call_model()` with a Gemini alias as a substitute.

> ⚠️ **Tunnel architecture — loca.lt replaced by Cloudflare Tunnel:** The original loca.lt tunnel was unreliable (subdomain theft, HTML challenge page, requires a running watchdog process). Cloudflare Tunnel provides a UUID-based permanent URL, runs as a Windows service (no terminal needed), and never requires a Secret Manager update after initial setup. To set up or migrate: `python scripts/setup_cloudflare_tunnel.py --project morphic-gaos-prod`. See `scripts/start_ollama_tunnel.py` (kept as fallback) and the deprecated `scripts/register_ollama_tunnel_task.ps1`.

- **Streaming:** always disabled (`stream=False`) — agents process complete responses, not token streams

### `web_access` Parameter

When `web_access=True` and the model is an `ollama/` alias, `_call_model` prepends DuckDuckGo Instant Answer results to the prompt before sending to Ollama. This gives the local model access to current real-world data without incurring Gemini API costs.

- Web results are fetched via `tools.web_search.web_search(prompt)` (see `tools/web_search.py`)
- If the web fetch fails for any reason, Ollama still receives the original prompt — failure is silent
- `web_access=True` is silently ignored when the model is a Gemini alias (Gemini has live knowledge natively)
- **Do not use** `web_access=True` in high-frequency loops or with prompts containing customer data (the query string is sent to DuckDuckGo)

### `ModelResponse` Fields

```python
@dataclass
class ModelResponse:
    text: str           # raw response text
    cost_usd: float     # always 0.0 — per-call cost calculation is not implemented; actual spend is tracked via GCP billing (see GAOS-Manager-Spec.md §9.4)
    tokens_used: int    # total tokens from usage_metadata (0 for Ollama); tracked for usage monitoring
    data: dict          # parsed JSON if parse_json=True, else {}
```

### `validate_code_safety()`

Static-analysis gate for agent-generated code. Called by Nexus-Prime before submitting any code to the Approval Gate.

```python
def validate_code_safety(code: str) -> dict[str, Any]:
    """
    Gate 1 + 2 combined static-analysis check.

    Gate 1 (Pattern): scans for blocked built-ins (os.system, subprocess.*,
      pickle.loads, eval, exec, etc.).
    Gate 2 (Import): every import must be on _ALLOWED_IMPORTS.

    Returns:
        {"passed": bool, "reason": str}
        On SyntaxError, passed=False with the parse error message.
    """
```

Failure is a hard stop — code is not submitted, not retried. See `.github/copilot-instructions.md §4` for the full gate contract.

### Ollama Fallback Telemetry

Two module-level functions expose the session-scoped Ollama-to-Gemini fallback counter:

```python
def get_ollama_fallback_count() -> int:
    """
    Return the number of times the system has fallen back from Ollama to Gemini
    in the current process lifetime. Thread-safe.
    """

def reset_ollama_fallback_count() -> None:
    """Reset the fallback counter to zero. Useful in tests and observability loops."""
```

The counter accumulates across all agents in the same process. It is not persisted across restarts — for cross-restart tracking, read the counter in the observability loop and write it to the Logs tab before resetting.

### Utility Helpers

```python
def utcnow_iso() -> str:
    """Return current UTC time as an ISO 8601 string. Used in log entries."""

def utcnow_date() -> str:
    """Return current UTC date as YYYY-MM-DD string. Used in heartbeats."""
```

### `AgentState` Enum

Added in Phase 2 Chapter 8 (OpenClaw Paradigm). Formalises the agent OODA loop as a typed state machine.

```python
class AgentState(StrEnum):
    """Named states for the agent OODA loop (StrEnum for JSON serialisation)."""
    INIT        = "INIT"
    PLANNING    = "PLANNING"
    EXECUTION   = "EXECUTION"
    OBSERVATION = "OBSERVATION"
    HEALING     = "HEALING"
    SYNTHESIS   = "SYNTHESIS"
    ESCALATION  = "ESCALATION"
    IDLE        = "IDLE"
    COMPLETED   = "COMPLETED"
```

### `log_state_transition()`

Emits a structured `_log_cloud` entry whenever an agent transitions between OODA states. Designed to be called at node boundaries inside orchestrators.

```python
def log_state_transition(
    agent_id: str,
    project_id: str,
    task_id: str,
    from_state: AgentState | str,
    to_state: AgentState | str,
    reason: str = "",
) -> None:
    """
    Emit a structured state-transition log entry.

    Args:
        agent_id:   Agent identifier (e.g. "nexus-prime").
        project_id: Project namespace.
        task_id:    Current task ID for correlation.
        from_state: The state the agent is leaving (``AgentState`` or plain string).
        to_state:   The state the agent is entering (``AgentState`` or plain string).
        reason:     Optional human-readable reason string.
    """
```

Log type is `"state_transition"`. Extra fields (`from_state`, `to_state`, `reason`) appear under `extra` in the structured log payload and are queryable in Cloud Logging.

### `validate_output_coherence()`

Offl-model semantic gate. Runs on `LOCAL_MODEL` (Ollama) and degrades gracefully when Ollama is unavailable. Added in Phase 2 Chapter 8.

```python
def validate_output_coherence(
    goal: str,
    output: str,
    agent_id: str,
    project_id: str,
) -> dict[str, Any]:
    """
    Ask LOCAL_MODEL whether *output* coherently achieves *goal*.

    Args:
        goal:       The original task objective or instruction.
        output:     The agent output to evaluate.
        agent_id:   Calling agent identifier (for log correlation).
        project_id: Project namespace.

    Returns:
        {"passed": bool, "confidence": float, "reason": str}
        On Ollama unavailability, returns {"passed": True, "confidence": 0.0,
        "reason": "coherence check skipped — Ollama unavailable"} so the
        offline check never blocks the main execution path.
    """
```

> ⚠️ **Only use with `LOCAL_MODEL`.** `web_access=True` is silently ignored and `validate_output_coherence` never receives it; the check is purely prompt-based and runs fully offline. Coherence failures emit a `WARNING` log — they are advisory, not a hard stop.

---

## 11. `tools/google_chat.py`

Google Chat integration for human↔agent communication. Added in Phase 2.5 Step 1.

> **Spec reference:** `GAOS-Manager-Spec.md §2.5 (Phase 2.5 — Conversation Layer)`

```python
from googleapiclient.discovery import build

def send_message(space_name: str, text: str) -> dict:
    """
    Send a plain-text message to a Google Chat space.

    Args:
        space_name: The Chat space resource name, e.g. ``spaces/XXXXXXXXX``.
        text:       Plain-text body (≤ 4096 characters; truncated automatically).

    Returns:
        The Chat API Message resource dict.

    Raises:
        ChatConfigError:   space_name is empty.
        ChatDeliveryError: Chat API returned a non-2xx response.
    """

def send_threaded_reply(space_name: str, thread_key: str, text: str) -> dict:
    """
    Send a reply that stays inside a specific thread using a developer-chosen thread key.

    Falls back to creating a new thread if no existing thread with the given key exists.
    Preferred function for Chat responses — keeps all messages in a single conversation
    thread rather than creating a new top-level thread per reply.

    Args:
        space_name: Chat space resource name (e.g. ``spaces/XXXXXXXXX``).
        thread_key: A stable developer-chosen string identifying the thread.
            Good values: the original ``message_name`` from the inbound Chat event,
            or computed keys like ``f"approval-{proposal_id}"``.
        text:       Plain-text message body (≤ 4096 characters; truncated if longer).

    Returns:
        The Chat API Message resource dict.

    Raises:
        ChatConfigError:   space_name or thread_key is empty.
        ChatDeliveryError: Chat API returned an HTTP error.
    """

def send_reply_in_thread(space_name: str, thread_name: str, text: str) -> dict:
    """
    Send a reply in an existing Chat thread using the server-assigned thread resource name.

    Unlike ``send_threaded_reply()`` which uses a developer-assigned ``threadKey``,
    this function references the thread by its server-assigned resource name
    (e.g. ``"spaces/XXXXX/threads/YYYYY"``).

    Use this whenever the inbound Chat event includes ``message.thread.name``
    (available in ``parse_chat_event()`` as the ``thread_name`` key).

    Args:
        space_name:  Chat space resource name (e.g. ``spaces/XXXXXXXXX``).
        thread_name: Server-assigned thread resource name
                     (e.g. ``"spaces/XXXXX/threads/YYYYY"``).
        text:        Plain-text message body (≤ 4096 characters; truncated if longer).

    Returns:
        The Chat API Message resource dict.

    Raises:
        ChatConfigError:   space_name or thread_name is empty.
        ChatDeliveryError: Chat API returned an HTTP error.
    """

def send_card(space_name: str, card: dict) -> dict:
    """
    Send a Card v2 message to a Chat space.

    Args:
        space_name: Chat space resource name.
        card:       Fully-formed Card v2 dict (must have ``header`` and at least one ``section``).

    Returns:
        The Chat API Message resource dict.

    Raises:
        ChatConfigError, ChatDeliveryError.
    """

def send_approval_card(
    space_name: str,
    proposal_id: str,
    agent_id: str,
    issue_summary: str,
    proposed_action: str,
    priority: int,
    cost_usd: float,
    doc_url: str = "",
    reasoning_summary: str = "",
) -> dict:
    """
    Post an Approve / Reject interactive card to the owner's Chat space.

    Button clicks return CARD_CLICKED events to ``POST /chat`` with
    ``action.actionMethodName`` set to ``"approve"`` or ``"reject"``.

    Raises:
        ChatConfigError:   space_name or proposal_id is empty.
        ChatDeliveryError: Chat API returned an error.
    """

def send_skill_import_card(
    space_name: str,
    proposal_id: str,
    agent_id: str,
    package_name: str,
    reason: str,
    pypi_url: str = "",
) -> dict:
    """
    Post a Skill Import approval card requesting permission to ``pip install`` a package.

    Button clicks return CARD_CLICKED events with ``action_name`` set to
    ``"skill_approve"`` or ``"skill_reject"``.

    Raises:
        ChatConfigError, ChatDeliveryError.
    """

def send_infra_proposal_card(
    space_name: str,
    proposal_id: str,
    change_lines: list[str],
    irreversible_warning: str = "",
) -> dict:
    """
    Post an infrastructure change proposal card for owner approval.

    Designed for non-technical readers — uses plain language throughout.
    Button clicks deliver ``actionMethodName: "infra_approve"`` or
    ``"infra_reject"`` to ``POST /chat``.

    Args:
        space_name:           Chat space resource name.
        proposal_id:          Manifest ``proposal_id`` (stored in Agent_Approvals).
        change_lines:         Human-readable list of proposed changes (one per entry).
        irreversible_warning: Non-empty string triggers a ⚠️ warning section.
            Should concisely explain what cannot be undone automatically.

    Returns:
        The Chat API Message resource dict.

    Raises:
        ChatConfigError:   space_name or proposal_id is empty.
        ChatDeliveryError: Chat API returned an error.
    """

def parse_chat_event(body: dict) -> dict:
    """
    Validate and parse an inbound Google Chat push payload.

    Chat delivers event types: ``MESSAGE``, ``CARD_CLICKED``,
    ``ADDED_TO_SPACE``, ``REMOVED_FROM_SPACE``.

    Returns:
        Normalised dict with keys: ``event_type``, ``space_name``,
        ``sender_email``, ``text`` (or empty str), ``action_name``
        (for CARD_CLICKED), ``parameters`` (``dict[str, str]`` keyed
        by parameter key), ``message_name`` (Chat message resource name).

    Raises:
        ChatEventParseError: body is missing required fields.
    """
```

### Authentication

Uses a Google service account with `chat.bot` scope. Key path is loaded from `settings.chat.service_account_key`; falls back to ADC (used automatically on Cloud Run).

### Error Types

```python
class ChatDeliveryError(Exception):
    """Chat API returned a non-2xx response."""

class ChatConfigError(Exception):
    """Chat is not configured (missing space or credentials)."""

class ChatEventParseError(Exception):
    """Inbound Chat event body is missing required fields."""
```


### Settings Required

Add to `config/settings.yaml` under the `chat:` key:

```yaml
chat:
  owner_space: "spaces/XXXXXXXXX"   # Owner's DM space resource name
  service_account_key: ""           # Optional path to SA key JSON; leave blank for ADC
```

### Usage Rule

Only Nexus-Prime calls `send_approval_card()`, `send_skill_import_card()`, or `send_infra_proposal_card()`. Domain orchestrators may not post Chat messages directly.

---

## 12. `tools/web_search.py`

Lightweight DuckDuckGo Instant Answer API wrapper for prepending real-world context to `LOCAL_MODEL` (Ollama) prompts. No API key required; no cost.

> **Used internally by:** `agents/__init__.py` `_call_model()` when `web_access=True`.

```python
def web_search(query: str, max_results: int = 5) -> str:
    """
    Query DuckDuckGo Instant Answer API and return a formatted snippet string.

    Args:
        query:       Natural-language search query.
        max_results: Maximum related-topic snippets to include (default 5).

    Returns:
        Multi-line string of AbstractText + up to max_results RelatedTopics.
        Returns empty string on any failure — this function never raises.
    """
```

### Behaviour
- Returns `""` on network timeout (5-second limit), HTTP error, or empty results — the caller always gets a string.
- `web_access=True` is silently ignored for Gemini models (they have live knowledge).
- **Never use** `web_access=True` in high-frequency loops or with prompts containing customer PII (the query is sent to DuckDuckGo's public API).

---

## 13. Tool Usage Rules Summary

| Rule | Detail |
|------|--------|
| Always call `init_sheets_client()` before any Sheet operation | This runs once in the boot sequence; do not call per-task |
| Always use `batch_append_rows()` for ≥ 2 rows | Single-row convenience wrapper is one API call — batching is always preferred |
| Never call Drive `write_file()` from a domain orchestrator | Orchestrators propose Drive changes; Nexus-Prime applies them post-approval |
| Never call `write_approved_memory()` from any agent except Nexus-Prime | Unauthorized writes are logged as Priority-5 security events |
| Always propagate `project_id` into every tool call | There is no ambient project context — dropping it is a bug |
| Catch tool errors at the agent level | Do not retry inside a tool call; the tool raises after its own backoff. The agent decides whether to escalate or park. |
| Never call `httpx` or any Google SDK directly from an agent | Use the tool layer and `_call_model()` — direct SDK calls bypass scoping, error handling, and fallback logic. |
| Wrap Sheets/BQ writes with `cb_check` / `cb_success` / `cb_failure` | At minimum, protect `append_row("Agent_Approvals")` and `insert_row("aos_logs.*")` calls — these are the highest-blast-radius failure points. |
| Never call `cb_failure` inside a `CircuitOpenError` handler | The call was never attempted; recording a failure only delays HALF_OPEN recovery. |
| `save_checkpoint` is best-effort on writes — never block on it | The BQ write inside `save_checkpoint` is swallowed (logged as WARNING). However, validation and serialization failures still raise — call it **outside** the circuit-breaker try block so a bad state dict does not trigger `cb_failure` on a write that succeeded. |

---

## 14. Reference Index

| Topic | Location |
|-------|----------|
| A2AMessage schema | `GAOS-Manager-Spec.md` §10.2 |
| MessageType registry (all 29 types) | `GAOS-Manager-Spec.md` §10.2 |
| Webhook HMAC threat model and test matrix | `GAOS-Manager-Spec.md` §15.2 |
| Approval Gate column definitions | `GAOS-Manager-Spec.md` §14 |
| Secret inventory | `GAOS-Manager-Spec.md` §15.1 |
| Sheets quota limits | `GAOS-Manager-Spec.md` §9.4 |
| Memory layer schemas and self-learning loop | `GAOS-Memory-Spec.md` |
| Agent boot sequence (tool call order) | `GAOS-Agent-Spec.md` §7 |
| Circuit breaker full API | §19 (this doc) |
| Phoenix recovery full API | §20 (this doc) |
| `agent_checkpoints` BQ table creation | `GAOS-Deploy-Spec.md` §7 |
| AgentState enum + log_state_transition | §10 (this doc) |
| Project Registry tab schema | `GAOS-Manager-Spec.md` §2 |
| Drive Knowledge/ folder structure | `GAOS-Memory-Spec.md` §7 |
| Chat settings (`chat.owner_space`, `chat.service_account_key`) | `GAOS-Deploy-Spec.md` §10.3 |
| Vertex AI Search settings (`vertex_search.*`) | `GAOS-Deploy-Spec.md` §10 |
| Google Docs settings (`docs.*`) | `config/settings.yaml.template` — `docs:` block |
| Blueprint Factory (Vision → Doc) | `Docs/agents/nexus-prime.md` — `VISION_SUBMITTED` handler |
| Google Search settings (`google_search.*`) | `config/settings.yaml.template` — `google_search:` block |
| Gmail tool API | §22 (this doc) |
| Gmail settings (`gmail.*`) | `config/settings.yaml` — `gmail:` block |

---

## 15. `tools/vertex_search.py`

Vertex AI Search (Discovery Engine) wrapper for Layer 5b semantic retrieval over the Drive Knowledge/ corpus. Added in Phase 2.5 Step 3.

> **Spec reference:** `GAOS-Memory-Spec.md §3 (Layer 5b — Vertex AI Search retrieval layer)`

```python
def search_knowledge(
    query: str,
    project_id: str,
    datastore_id: str,
    max_results: int = 5,
) -> list[dict[str, Any]]:
    """
    Run a semantic search against a Vertex AI Search datastore.

    Returns:
        List of dicts with keys: ``id``, ``title``, ``snippet``, ``link``.
        Returns an empty list for blank queries or when the API returns no results.

    Raises:
        DatastoreNotConfiguredError: ``datastore_id`` is empty.
        VertexSearchError:           Discovery Engine API failure.
    """

def query_playbooks(query: str, project_id: str, max_results: int = 5) -> list[dict[str, Any]]:
    """Convenience wrapper: search the configured playbooks datastore.
    Uses ``settings.vertex_search.playbook_datastore_id``."""

def query_domain_knowledge(query: str, project_id: str, max_results: int = 5) -> list[dict[str, Any]]:
    """Convenience wrapper: search the configured general-knowledge datastore.
    Uses ``settings.vertex_search.knowledge_datastore_id``."""
```

### Error Types

```python
class VertexSearchError(Exception):
    """Unrecoverable Vertex AI Search API error."""

class DatastoreNotConfiguredError(Exception):
    """Required datastore ID is missing from settings."""
```

### Settings Required

```yaml
vertex_search:
  location: "global"
  playbook_datastore_id: ""   # Short-form ID from Vertex AI Search console
  knowledge_datastore_id: ""  # Second datastore for general Knowledge/ files
```

### Usage Rule

Only Nexus-Prime and Scout call `query_playbooks()` / `query_domain_knowledge()`.
Domain orchestrators receive retrieved context via Nexus-Prime task responses — they do
not call Vertex Search directly.

---

## 16. `tools/google_docs.py`

Google Docs + Drive API wrapper for the Blueprint Factory. Added in Phase 2.5 Step 4.

> **Spec reference:** `GAOS-Memory-Spec.md §3 (Blueprint Factory)` · `Docs/agents/nexus-prime.md — VISION_SUBMITTED handler`

```python
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
        project_id:      AOS project namespace.
        folder_id:       Drive folder ID.  If ``None``, uses
                         ``settings.docs.blueprints_folder_id``; if that is also
                         empty, the document is created in the account root.
        initial_content: Optional text inserted at index 1 immediately after creation.

    Returns:
        The document ID string.

    Raises:
        ValueError:   ``title`` is empty.
        DocsApiError: Google Docs or Drive API failure.
    """

def read_document(doc_id: str, project_id: str) -> str:
    """
    Read the full plain-text content of a Google Doc.

    Returns:
        Document body as a plain-text string (newlines preserved).
        Empty string if the document has no text.

    Raises:
        DocumentNotFoundError: Document does not exist or is inaccessible.
        DocsApiError:          Google Docs API failure (non-404).
    """

def append_content(doc_id: str, content: str, project_id: str) -> None:
    """
    Append text to the end of an existing Google Doc.

    No-op if ``content`` is empty.  Text is inserted at the last valid body index.

    Raises:
        DocumentNotFoundError: Document does not exist or is inaccessible.
        DocsApiError:          Google Docs API failure.
    """

def list_comments(doc_id: str, project_id: str) -> list[dict[str, Any]]:
    """
    List all comments on a Google Doc (via the Drive API).

    Used by the ``doc-comment-poll`` Cloud Scheduler job to feed owner comments
    into the ``ITERATE_PLAN`` node in Nexus-Prime.

    Returns:
        List of dicts with keys:
            ``id``         — comment resource ID
            ``content``    — comment text
            ``author``     — commenter's display name
            ``created_at`` — ISO 8601 timestamp string
            ``resolved``   — ``True`` if the thread is resolved

    Raises:
        DocumentNotFoundError: Document does not exist or is inaccessible.
        DocsApiError:          Google Drive API failure.
    """
```

### Error Types

```python
class DocsApiError(Exception):
    """Unrecoverable Google Docs or Drive API error."""

class DocumentNotFoundError(Exception):
    """The requested document does not exist or is not accessible."""
```

### Authentication

Uses a service account with `documents` + `drive` scopes. Key path is read from
`settings.docs.service_account_key`; falls back to ADC (used automatically on Cloud Run
and in local dev with `oauth-client.json`).

### Settings Required

```yaml
docs:
  service_account_key: ""      # Optional path to SA key JSON; leave empty for ADC
  blueprints_folder_id: ""     # Default Drive folder ID for Blueprint Docs
  knowledge_atlas_doc_id: ""   # Google Doc ID for the Knowledge Atlas (Memory Mirror)
```

### Usage Rule

Only Nexus-Prime calls `create_document()`, `append_content()`, and `list_comments()`.
Domain orchestrators do not interact with Google Docs directly — they submit task results
to Nexus-Prime which applies Blueprint updates post-approval.

---

## 17. `tools/google_search.py`

Google Custom Search JSON API v1 wrapper for Scout's deep research. Added in Phase 2.5 Step 6.

```python
def search(
    query: str,
    project_id: str,
    num: int = 10,
) -> list[dict[str, Any]]:
    """
    Execute a single Google Custom Search query.

    Returns:
        List of dicts: [{title, url, snippet, date}, ...]

    Raises:
        GoogleSearchError: API error, quota exceeded (429/403), or credentials unavailable.
    """

def research_topic(
    queries: list[str],
    project_id: str,
    max_queries: int = 15,
) -> list[dict[str, Any]]:
    """
    Execute multiple queries, deduplicating results by URL.
    Failed queries are skipped — remaining queries still execute.
    """
```

### Error Types

| Exception | Trigger |
|-----------|---------|
| `GoogleSearchError` | HTTP 429/403, network failure, JSON decode error, missing Secret Manager credentials |

### Authentication

Credentials are fetched from GCP Secret Manager at call time — never embedded in settings.yaml. The Secret Manager secret names are hardcoded module constants:

- `GOOGLE_SEARCH_API_KEY` — Custom Search JSON API key
- `GOOGLE_SEARCH_CX` — Programmable Search Engine ID (CX)

### Settings Required

```yaml
google_search:
  max_search_depth: 3          # Recursive query depth cap (read by Scout's _discover node)
  max_queries_per_mandate: 15  # Hard cap on total queries per RESEARCH_MANDATE
```

### Usage Rule

`tools/google_search.py` is called exclusively from Scout's `_discover` node. No other orchestrator calls it directly. Nexus-Prime triggers Scout via a `RESEARCH_MANDATE` Pub/Sub message — it does not call the search tool itself.

> ⚠️ **Rate limit:** Free tier is 100 queries/day. `max_queries_per_mandate` (default 15) ensures a single mandate never exceeds 15% of the daily quota. Monitor usage in GCP Console → APIs & Services → Google Custom Search API.

---

## 18. `tools/memory_mirror.py`

Mirrors every approved `MemoryEntry` to a human-readable Google Doc called the **Knowledge Atlas** — a plain-text glass-box view of all knowledge the system has promoted to Vertex AI Memory Bank. Added in Phase 2.5 Step 7 (Memory Mirror).

> **Spec reference:** `GAOS-Memory-Spec.md §6 (Layer 4 — Semantic Memory)` · `Docs/GAOS-Nexus-Prime-Spec.md — knowledge_review node`

```python
def sync_to_atlas(entry: MemoryEntry, supersession_reason: str | None = None) -> None:
    """
    Append an approved MemoryEntry to the Knowledge Atlas Google Doc.

    Each entry is formatted as a structured text block (ID, agent, domain,
    type, content, confidence, approval timestamp, tags). When
    ``entry.supersedes`` is set, the block opens with a ⛔ SUPERSEDED header
    placed *before* the standard fields so a reviewer scanning the doc sees
    the retirement notice immediately, followed by the ``supersession_reason``
    explaining why the old entry was retired.

    The Atlas doc must be pre-created in Google Drive — this function never
    auto-creates it. Copy the document ID into
    ``settings.docs.knowledge_atlas_doc_id``.

    Args:
        entry: The approved MemoryEntry to mirror.
        supersession_reason: One-sentence LLM-provided explanation of why the
            old entry is being retired. Defaults to "(no reason provided)".

    Raises:
        MemoryMirrorError: ``knowledge_atlas_doc_id`` is not configured, or
            the Docs API call failed.  Callers (knowledge_review node) must
            catch this and log a WARNING — it must never block the Vertex
            AI write path.
    """
```

### Error Types

```python
class MemoryMirrorError(Exception):
    """
    Raised when the Atlas sync fails.  The Vertex AI write always wins —
    callers must catch this, log a WARNING, and continue.
    """
```

### Entry Format (appended per approved MemoryEntry)

**Standard entry (no supersession):**
```
---
ID:         <memory_id UUID>
Agent:      <agent_id>
Domain:     <domain>
Type:       <knowledge_type>
Content:    <content>
Confidence: 92%
Approved:   2026-03-20T10:00:00Z
Tags:       sales, q4
```

**Supersession entry (`entry.supersedes` is set) — ⛔ header appears first:**
```
---
⛔ SUPERSEDED by <new_memory_id>
Retires:    <old_memory_id>
Reason:     <supersession_reason from LLM>

ID:         <new_memory_id>
Agent:      <agent_id>
Domain:     <domain>
Type:       <knowledge_type>
Content:    <content>
Confidence: 92%
Approved:   2026-03-20T10:00:00Z
Tags:       sales, q4
```

### Settings Required

```yaml
docs:
  knowledge_atlas_doc_id: ""   # Google Doc ID for the Knowledge Atlas
                               # Pre-create the doc in Drive; paste its ID here.
                               # Leave empty to disable mirroring (Vertex write
                               # still succeeds — a WARNING is logged instead).
```

### Usage Rule

`sync_to_atlas()` is called exclusively from `knowledge_review` in Nexus-Prime's orchestrator, immediately after `write_approved_memory()`. It runs inside its own nested `try/except` — `MemoryMirrorError` is logged as a `WARNING` and never propagates. The Vertex AI write path is always primary.

> ⚠️ **Pre-create the Atlas doc manually.** Do not set `knowledge_atlas_doc_id` to an empty string and expect auto-creation — `sync_to_atlas()` raises `MemoryMirrorError` immediately if the ID is missing. Auto-creation during cold start risks duplicate Atlas documents if multiple agents boot simultaneously. Create the doc once in Google Drive, note its document ID from the URL (`/d/<ID>/edit`), and paste it into `settings.yaml`.

---

## 19. `tools/circuit_breaker.py`

In-process CLOSED → OPEN → HALF_OPEN state machine that prevents agents from hammering dead external dependencies. Added in Phase 2 Chapter 8 (OpenClaw Paradigm §8.3).

> **Design rationale:** Cloud Run `workers=1` (single-process) — state is held in a module-level dict keyed by `(agent_id, resource_key)`. No cross-instance coordination is needed; state resets on cold start, which is acceptable (the circuit simply begins closed again and re-opens if the dependency is still dead).

```python
from tools.circuit_breaker import (
    check as cb_check,
    record_failure as cb_failure,
    record_success as cb_success,
    CircuitOpenError,
)
```

### State Machine

| State | Condition | Behaviour |
|-------|-----------|-----------|
| `CLOSED` | Default; failure count < threshold | Calls pass through normally |
| `OPEN` | Failure count ≥ threshold | `check()` raises `CircuitOpenError` immediately; no call attempted |
| `HALF_OPEN` | Cooldown period elapsed after OPEN | One probe call allowed; failure → back to OPEN, success → CLOSED |

Default threshold: **3 failures**. Default cooldown: **300 seconds**. Both are configurable per call-site.

### Public API

```python
def check(agent_id: str, resource_key: str) -> None:
    """
    Assert the circuit is CLOSED or HALF_OPEN for (agent_id, resource_key).

    Raises:
        CircuitOpenError: Circuit is OPEN and cooldown has not elapsed.
    """

def record_failure(agent_id: str, resource_key: str) -> None:
    """
    Record one failure for (agent_id, resource_key).
    Increments the failure counter; transitions CLOSED → OPEN when threshold reached.
    """

def record_success(agent_id: str, resource_key: str) -> None:
    """
    Record a successful call for (agent_id, resource_key).
    Resets failure counter; transitions HALF_OPEN → CLOSED.
    """

def get_state(agent_id: str, resource_key: str) -> str:
    """Return the current state string: "CLOSED", "OPEN", or "HALF_OPEN"."""

def reset(agent_id: str, resource_key: str) -> None:
    """Manually reset the circuit to CLOSED. Useful in tests."""

def reset_all() -> None:
    """Reset all circuits to CLOSED. Intended for test teardown only."""
```

### Error Types

```python
class CircuitOpenError(Exception):
    """
    Raised by check() when the circuit is OPEN and the cooldown has not elapsed.
    The caller should log a WARNING and skip the call — do not record a failure
    (the call was never attempted).
    """
```

### Canonical Usage Pattern

```python
from tools.circuit_breaker import check as cb_check, record_failure as cb_failure, \
    record_success as cb_success, CircuitOpenError

try:
    cb_check("nexus-prime", "google-sheets")
    append_row("Agent_Approvals", row, project_id)
    cb_success("nexus-prime", "google-sheets")
except CircuitOpenError:
    _log_cloud(..., "Circuit open — call skipped", "WARNING")
    # Do NOT call cb_failure here — the call was never made
except Exception as exc:
    cb_failure("nexus-prime", "google-sheets")
    _log_cloud(..., f"Call failed: {exc}", "ERROR")
```

> ⚠️ **Do not call `cb_failure` on `CircuitOpenError`.** When the circuit is already OPEN, incrementing the failure counter is redundant and can cause the cooldown timer to reset, indefinitely deferring recovery. Only `record_failure` when the underlying API call was actually attempted and failed.

### Current Wire-Up Points (Nexus-Prime)

| Node | Resource Key | Call site |
|------|-------------|-----------|
| `propose_gate` | `"google-sheets"` | `append_row("Agent_Approvals", ...)` |
| `record` | `"bigquery"` | `insert_row("aos_logs.task_outcomes", ...)` |

---

## 20. `tools/phoenix.py`

Checkpoint and recovery for agent working state. Implements the Phoenix Pattern from OpenClaw Paradigm §8.11: when corrupted state is detected, the agent restores from the last known-good snapshot rather than attempting in-place repair. Added in Phase 2 Chapter 8.

> **Persistence:** Checkpoints are written to BigQuery `aos_logs.agent_checkpoints` (30-day TTL, `timestamp` partition). See `GAOS-Deploy-Spec.md §7` for table creation. Writes are **best-effort and non-fatal** — a checkpoint failure never blocks the agent's main execution path.

> **Security:** Every checkpoint row is SHA-256-pinned (`checkpoint_hash` = SHA-256 of the serialized state JSON). `load_checkpoint()` recomputes the hash for every candidate row and silently skips any row where `stored_hash ≠ computed_hash`. This prevents a tampered `Agent_Approvals`-style attack on the `state_json` column.

```python
from tools.phoenix import save_checkpoint, load_checkpoint, phoenix_recover, validate_state
```

### Public API

```python
def validate_state(state: dict[str, Any]) -> dict[str, Any]:
    """
    Check that *state* contains required fields and is within size limits.

    Required fields: ``agent_id``, ``project_id``.
    Size limit: 512 KB serialized.

    Returns:
        {"valid": bool, "reason": str}   # reason is empty on success
    """

def save_checkpoint(agent_id: str, project_id: str, state: dict[str, Any]) -> str:
    """
    Validate, serialize, hash-pin, and write *state* to aos_logs.agent_checkpoints.

    The BQ write is best-effort — a write failure is caught internally and logged
    as WARNING without re-raising. Validation and serialization failures, however,
    do raise — callers must handle them separately from the surrounding circuit-
    breaker try block.

    Args:
        agent_id:   Agent identifier (used as the BQ row key).
        project_id: Project namespace.
        state:      Agent working state dict to snapshot.

    Returns:
        The SHA-256 hexdigest of the serialized state.

    Raises:
        CheckpointCorruptedError:     State failed validation — not saved.
        CheckpointSerializationError: State is not JSON-serializable.
    """

def load_checkpoint(agent_id: str, project_id: str) -> dict[str, Any] | None:
    """
    Load and hash-verify the most recent valid checkpoint for *agent_id*.

    Queries the 5 most recent rows ordered by timestamp DESC. Rows with a
    SHA-256 hash mismatch or malformed JSON are silently skipped (logged as
    WARNING). A BQ query failure is also swallowed and returns None.
    This function never raises.

    Returns:
        The first hash-verified state dict, or None if no valid checkpoint
        exists (all rows skipped, BQ unreachable, or no rows found).
    """

def phoenix_recover(
    agent_id: str,
    project_id: str,
    current_state: dict[str, Any],
) -> dict[str, Any]:
    """
    Validate *current_state*; if invalid, restore from the last checkpoint.

    Args:
        agent_id:      Agent identifier.
        project_id:    Project namespace.
        current_state: The state dict to validate.

    Returns:
        *current_state* unchanged if valid, or the restored checkpoint dict.

    Raises:
        CheckpointCorruptedError: Current state is invalid AND no valid
            checkpoint exists in BigQuery.
    """
```

### Error Types

```python
class CheckpointCorruptedError(Exception):
    """
    Active state failed validation and no restorable checkpoint exists.
    The caller must log CRITICAL, set hard_stop_triggered=True, and return state.
    """

class CheckpointSerializationError(Exception):
    """State dict cannot be serialized to JSON. Raised only by internal helpers."""
```

### Canonical Usage Pattern

```python
from tools.phoenix import save_checkpoint, CheckpointCorruptedError, CheckpointSerializationError

# save_checkpoint must be called OUTSIDE the circuit-breaker try block.
# Its BQ write is internally swallowed, but validation/serialization errors
# still raise — having it inside except Exception would trigger cb_failure
# even when the BQ write it's checkpointing succeeded.
try:
    cb_check("nexus-prime", "bigquery")
    insert_row("aos_logs.task_outcomes", outcome)
    cb_success("nexus-prime", "bigquery")
except CircuitOpenError:
    _log_cloud(..., "BigQuery circuit open", "WARNING")
except Exception:
    cb_failure("nexus-prime", "bigquery")
else:
    # Only checkpoint if the write succeeded.
    try:
        save_checkpoint("nexus-prime", state.get("project_id", ""), dict(state))
    except (CheckpointCorruptedError, CheckpointSerializationError) as exc:
        _log_cloud(..., f"Checkpoint skipped (invalid state): {exc}", "WARNING")
```

### Current Wire-Up Points (Nexus-Prime)

| Node | When | Behaviour |
|------|------|-----------|
| `record` | After successful `insert_row("aos_logs.task_outcomes", ...)` | Snapshots full working state; non-fatal |

### Required BigQuery Table

See `GAOS-Deploy-Spec.md §7` for the `agent_checkpoints` table creation script. Schema:

| Column | Type | Notes |
|--------|------|-------|
| `agent_id` | STRING | |
| `project_id` | STRING | |
| `timestamp` | TIMESTAMP | Partition key; 30-day TTL |
| `state_json` | STRING | Serialized dict — never executed |
| `checkpoint_hash` | STRING | SHA-256 of `state_json` |
| `is_valid` | BOOL | Always `True` on write; hash mismatch rows skipped at load |

---

## 21. `tools/infra_provision.py`

Infrastructure drift detection, controlled apply, health verification, and scoped rollback for the three GCP resource types managed outside OpenTofu by Morphic-GAOS: **Cloud Scheduler jobs**, **BigQuery staging tables**, and **Secret Manager secret slots**. Added in Phase 4 alongside the `POST /infra-provision` endpoint and `send_infra_proposal_card()`.

> **Spec reference:** `GAOS-Deploy-Spec.md §20 (Infrastructure Provisioner)`

### Desired State Registry

Three module-level constants define the single source of truth for every managed resource. Import from here — never duplicate the lists elsewhere.

| Constant | Type | Description |
|----------|------|-------------|
| `DESIRED_SCHEDULER_JOBS` | `list[dict]` | Cloud Scheduler job defs: `id`, `schedule`, `path`, `description` |
| `DESIRED_BQ_TABLES` | `list[str]` | Staging table names in `aos_logs` dataset |
| `DESIRED_SECRETS` | `list[str]` | Secret Manager secret IDs that must exist |
| `BQ_DATASET` | `str` | Dataset name constant — `"aos_logs"` |

### Data Model

```python
class ChangeKind(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    NO_CHANGE = "NO_CHANGE"

class ResourceType(StrEnum):
    SCHEDULER_JOB = "SCHEDULER_JOB"
    BQ_TABLE = "BQ_TABLE"
    SECRET = "SECRET"

@dataclass
class ChangeEntry:
    """Single resource diff result. Serializes to/from JSON for Agent_Approvals."""
    resource_type: ResourceType
    resource_id: str
    kind: ChangeKind
    desired: dict[str, Any]
    actual: dict[str, Any]
    irreversible: bool         # True for BQ_TABLE creates — tables are never auto-dropped
    human_description: str     # Plain-language label shown in the Chat approval card

@dataclass
class InfraManifest:
    """Full diff manifest produced by build_manifest(). Serialises to JSON for storage."""
    proposal_id: str
    project_id: str
    region: str
    nexus_url: str
    sa_email: str
    changes: list[ChangeEntry]
    # Computed properties: .actionable (filters NO_CHANGE), .has_changes, .has_irreversible

@dataclass
class ApplyResult:
    """Outcome of apply_manifest()."""
    applied: list[str]
    failed: list[str]
    applied_entries: list[ChangeEntry]   # Scoped input for rollback_manifest()
    # Computed property: .success = not bool(failed)
```

### Public API

```python
def build_manifest(
    project_id: str,
    region: str,
    nexus_url: str,
    sa_email: str,
    scheduler_client: Any | None = None,
    bq_client: Any | None = None,
    sm_client: Any | None = None,
) -> InfraManifest:
    """
    Diff desired vs actual GCP state across all three resource types.

    Returns InfraManifest with ALL entries including NO_CHANGE so the Chat
    card can accurately say "N changes, M already up to date".
    GCP clients are auto-created via ADC if not provided.
    """

def apply_manifest(
    manifest: InfraManifest,
    scheduler_client: Any | None = None,
    bq_client: Any | None = None,
    sm_client: Any | None = None,
) -> ApplyResult:
    """
    Apply actionable changes in safe dependency order:
    secrets → BQ tables → scheduler jobs.
    Each resource is independent — one failure does not abort the rest.
    """

def rollback_manifest(
    manifest: InfraManifest,
    apply_result: ApplyResult,
    scheduler_client: Any | None = None,
    sm_client: Any | None = None,
) -> list[str]:
    """
    Attempt to undo changes recorded in apply_result.applied_entries.
    Hard rules: BQ tables are NEVER dropped; secrets with existing
    versions are NOT deleted; scheduler jobs are deleted (CREATE)
    or re-patched to their previous schedule (UPDATE).
    Returns list of human-readable outcome strings.
    """

def run_health_checks(
    manifest: InfraManifest,
    scheduler_client: Any | None = None,
    bq_client: Any | None = None,
) -> tuple[bool, list[str]]:
    """
    Verify all changed resources exist after apply_manifest().
    Skips NO_CHANGE entries — checks only modified resource types.
    Returns (all_passed: bool, notes: list[str]).
    """
```

### Workflow Integration

Called from two entry points — never directly from an orchestrator event loop:

| Entry Point | Phase | Actions |
|-------------|-------|---------|
| `scripts/provision_infra.py` (CLI, local or CI) | PLAN | `build_manifest()` → write `Agent_Approvals` row → `send_infra_proposal_card()` |
| `handle_infra_provision()` in nexus_prime | APPLY | Read manifest JSON from approval row → `apply_manifest()` → `run_health_checks()` → rollback + result card on failure |

### Apply Order Rationale

Secrets are created first (additive, no data risk). BQ tables use `CREATE TABLE IF NOT EXISTS` DDL (idempotent). Scheduler jobs are upserted last so their OIDC targets (Nexus URL) and BigQuery dependencies exist before the jobs can run.

### Rollback Hard Rule

BigQuery tables created during apply are **never** auto-dropped — data may already be written. `rollback_manifest()` emits a `ROLLBACK SKIPPED` line with a manual `bq rm` command for the operator.

### Test Coverage

`tests/test_infra_provision.py` — covers: `build_manifest` (all NO_CHANGE, partial CREATE, multi-kind), `apply_manifest` (happy path, partial failure), `rollback_manifest` (scheduler CREATE rollback, scheduler UPDATE rollback, BQ skip, secret with versions skip), `run_health_checks` (pass, fail, mixed).

---

## 22. `tools/gmail.py`

Gmail API integration for inbox polling, thread context retrieval, outbound email sending, and push watch registration. Added in Phase 3 for email-based communication.

> **Spec reference:** `Docs/email-comm-plan.md`

```python
def get_gmail_service(project_id: str) -> Any:
    """
    Build and return an authenticated Gmail API service object.

    Fetches OAuth2 credentials from Secret Manager (``GMAIL_OAUTH_CREDENTIALS``
    secret) on the first call per process. The service is cached in a
    per-project module-level dict (``dict[str, tuple[Any, float]]``) keyed by
    ``project_id`` and protected by a ``threading.Lock`` (``_gmail_svc_lock``).
    Two projects can coexist in cache simultaneously without thrashing.
    The lock is acquired only during dict read/write — it is released before
    the slow service build — so I/O never blocks concurrent calls for other
    projects. The cache resets on Cloud Run cold start. Credential expiry is
    handled automatically by google-auth token refresh.

    Args:
        project_id: GCP project that owns the GMAIL_OAUTH_CREDENTIALS secret.

    Returns:
        A ``googleapiclient.discovery.Resource`` for the Gmail v1 API.

    Raises:
        GmailAuthError: Credentials missing, malformed, or OAuth scope insufficient.
    """

def fetch_new_messages(
    project_id: str,
    history_id: str,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    """
    Fetch all messages added since ``history_id`` using the Gmail history delta API.

    Calls ``users.history.list(startHistoryId=history_id)`` to retrieve only
    the delta since the last processed ID. Extracts ``text/plain`` body parts
    only; never parses ``text/html``.

    **HTTP 410 Gone handling:** When ``history.list`` returns 410 the watermark
    is too old (purged by Gmail). The function calls ``getProfile(userId="me")``
    to obtain a fresh ``historyId`` and returns ``([], fresh_historyId, [])``
    without raising. This advances the watermark so the caller never replays
    the same 410 on the next notification (Rule 29). If ``getProfile`` also
    fails, ``WatermarkRecoveryError`` is raised.

    Args:
        project_id: GCP project for credential and secret lookup.
        history_id: The ``historyId`` from the last successfully processed
            notification. Use the initial watch ``historyId`` as the seed.

    Returns:
        A tuple of ``(messages, new_history_id, skipped_ids)`` where:
        - ``messages`` is a list of dicts with keys: ``message_id``, ``thread_id``,
          ``from_addr``, ``subject``, ``body``, ``received_at`` (ISO-8601 UTC),
          ``message_id_header``.
        - ``new_history_id`` is the latest ``historyId`` from the API response.
        - ``skipped_ids`` is a list of Gmail message IDs that were permanently
          unavailable (404 after 3 retries) and silently skipped.

    Raises:
        GmailAuthError:         Credential fetch failed.
        GmailAPIError:          Gmail API returned a non-retryable error.
        WatermarkRecoveryError: ``history.list`` returned 410 AND the
                                ``getProfile`` fallback also failed.
    """

> ⚠️ **Warning — HTTP 410 watermark loop:** If ``history.list`` returns 410
> and the caller does NOT advance the watermark, every subsequent Pub/Sub
> notification replays the same 410 — a self-perpetuating 100% error loop
> (Rule 29). ``fetch_new_messages`` handles 410 internally and always returns
> a fresh ``new_history_id``. Callers must persist ``new_history_id``
> unconditionally — never skip the persist on empty message lists.

def get_thread_context(
    project_id: str,
    thread_id: str,
    max_messages: int = 3,
) -> list[dict[str, Any]]:
    """
    Return the last ``max_messages`` exchanges in a Gmail thread.

    Fetches the full thread and extracts the last N messages as plain text,
    oldest first. Used by ``process_gmail_notification`` to give the LLM
    conversation context before composing a reply.

    Args:
        project_id:   GCP project for credential lookup.
        thread_id:    The Gmail thread ID to retrieve.
        max_messages: Maximum messages to return (default 3).

    Returns:
        List of message dicts (same shape as fetch_new_messages output),
        ordered oldest → newest.

    Raises:
        GmailAuthError, GmailAPIError.
    """

def mark_as_read(project_id: str, message_id: str) -> None:
    """
    Remove the UNREAD label from a Gmail message.

    Called after each successfully processed message to prevent reprocessing
    if the watch fires again before the historyId is updated.

    Args:
        project_id: GCP project for credential lookup.
        message_id: The Gmail message ID to mark as read.

    Raises:
        GmailAuthError, GmailAPIError.
    """

def send_email(
    project_id: str,
    to: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
    in_reply_to: str | None = None,
    from_addr: str | None = None,
) -> str:
    """
    Send an email via the Gmail API.

    Sets ``In-Reply-To`` and ``References`` headers when ``in_reply_to``
    is provided, keeping the reply coherent in the recipient's inbox client.

    Args:
        project_id:  GCP project for credential lookup.
        to:          Recipient email address.
        subject:     Email subject line.
        body:        Plain-text email body.
        thread_id:   Gmail thread ID to reply within.
        in_reply_to: ``Message-ID`` header value of the message being replied to.
        from_addr:   Override ``From`` header. Must be a verified "Send mail as"
                     alias. Use ``settings.gmail.sender_address``.

    Returns:
        The Gmail message ID of the sent message.

    Raises:
        GmailAuthError, GmailAPIError.
    """

def setup_watch(project_id: str, topic_name: str, label_id: str) -> tuple[str, str]:
    """
    Register or renew a Gmail push watch.

    Gmail watch() publishes a Pub/Sub notification to ``topic_name`` whenever
    a message arrives in INBOX. The watch expires every 7 days — renewal is
    mandatory. Call at deploy time and renew via Cloud Scheduler
    (POST /gmail-renew-watch, every 23 hours).

    .. note::
       The ``label_id`` parameter is accepted for API compatibility but is
       currently unused — the implementation always watches ``INBOX``.
       See ``email-comm-plan.md`` for the design rationale.

    Args:
        project_id: GCP project for credential lookup.
        topic_name: Fully-qualified Pub/Sub topic resource name.
        label_id:   Gmail label ID (accepted but unused; INBOX is always watched).

    Returns:
        A tuple of ``(expiration_ms_str, history_id_str)``.

    Raises:
        GmailAuthError, GmailAPIError.
    """
```

### Authentication

Uses OAuth2 credentials stored as the `GMAIL_OAUTH_CREDENTIALS` secret in GCP Secret Manager. The secret is a JSON blob containing `client_id`, `client_secret`, and `refresh_token`. Generated via `scripts/setup_gmail_oauth.py`.

Required Gmail scopes: `gmail.modify` + `gmail.send`.

### Error Types

```python
class GmailAuthError(Exception):
    """Secret missing, token invalid, or OAuth scope insufficient."""

class GmailAPIError(Exception):
    """Gmail REST API returned a non-retryable error."""

class WatermarkRecoveryError(Exception):
    """
    history.list returned HTTP 410 Gone AND the getProfile fallback also failed.
    The caller must NOT persist the stale watermark — implement backoff and
    retry. If the error persists, manual intervention is required to obtain
    a valid historyId (e.g. re-register the Gmail push watch).
    """
```

### Settings Required

Add to `config/settings.yaml` under the `gmail:` key:

```yaml
gmail:
  monitored_address: 'dhess@sl10repairtechs.com'
  sender_address: 'aos@sl10repairtechs.com'
  label_id: 'Label_6'
  pubsub_topic: 'projects/morphic-gaos-prod/topics/gmail-notifications'
  max_results: 50
```

### Usage Rule

Only Nexus-Prime calls Gmail tool functions. Domain orchestrators must not interact with the Gmail API directly — email handling flows through the `GMAIL_NOTIFICATION` → `EMAIL_RECEIVED` Pub/Sub event chain.

### Test Coverage

`tests/test_gmail.py` — 13 tests covering: happy path (fetch, send, mark_as_read, setup_watch), Secret Manager failure, Gmail API failure, empty/invalid input, thread context retrieval, 410 watermark reset (`test_fetch_new_messages_410_watermark_reset`), 410 + getProfile failure → `WatermarkRecoveryError` (`test_fetch_new_messages_410_getprofile_failure`), message 404 after max retries → `skipped_ids` (`test_fetch_new_messages_404_skipped`).
