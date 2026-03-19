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
    """Full implementation in GAOS-Memory-Spec.md §6."""

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
| starts with `ollama/` | `_call_model_ollama()` → local Ollama server | Falls back to `LOCAL_MODEL_FALLBACK` on timeout or connection error |
| any other string | `_call_model_gemini()` → `google.genai` | Falls back to ADC / Vertex AI if `GEMINI_API_KEY` is unavailable |

### Ollama Call Details

- **Host:** fetched from Secret Manager as `OLLAMA_HOST` at call-time; defaults to `http://localhost:11434` if the secret fetch fails (intentional local-dev fallback)
- **Timeout:** `LOCAL_MODEL_TIMEOUT_SECONDS` from `settings.yaml` (default: 2 seconds)
- **Fallback:** on `httpx.TimeoutException` or `httpx.ConnectError`, automatically retries via `_call_model_gemini()` with the `LOCAL_MODEL_FALLBACK` alias — the caller never sees the error
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
    cost_usd: float     # estimated cost (0.0 for Ollama; token-based estimate for Gemini)
    tokens_used: int    # total tokens (0 for Ollama)
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

Failure is a hard stop — code is not submitted, not retried. See `AI-Autocoding-Rules.md §4` for the full gate contract.

### Utility Helpers

```python
def utcnow_iso() -> str:
    """Return current UTC time as an ISO 8601 string. Used in log entries."""

def utcnow_date() -> str:
    """Return current UTC date as YYYY-MM-DD string. Used in heartbeats."""
```

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
    """Inbound Chat push payload is missing required fields."""
```

### Settings Required

Add to `config/settings.yaml` under the `chat:` key:

```yaml
chat:
  owner_space: "spaces/XXXXXXXXX"   # Owner's DM space resource name
  service_account_key: ""           # Optional path to SA key JSON; leave blank for ADC
```

### Usage Rule

Only Nexus-Prime calls `send_approval_card()` or `send_skill_import_card()`. Domain orchestrators may not post Chat messages directly.

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

---

## 14. Reference Index

| Topic | Location |
|-------|----------|
| A2AMessage schema | `GAOS-Manager-Spec.md` §10.2 |
| MessageType registry (all 22 types) | `GAOS-Manager-Spec.md` §10.2 |
| Webhook HMAC threat model and test matrix | `GAOS-Manager-Spec.md` §15.2 |
| Approval Gate column definitions | `GAOS-Manager-Spec.md` §14 |
| Secret inventory | `GAOS-Manager-Spec.md` §15.1 |
| Sheets quota limits | `GAOS-Manager-Spec.md` §9.4 |
| Memory layer schemas and self-learning loop | `GAOS-Memory-Spec.md` |
| Agent boot sequence (tool call order) | `GAOS-Agent-Spec.md` §7 |
| Project Registry tab schema | `GAOS-Manager-Spec.md` §2 |
| Drive Knowledge/ folder structure | `GAOS-Memory-Spec.md` §7 |
| Chat settings (`chat.owner_space`, `chat.service_account_key`) | `GAOS-Deploy-Spec.md` §10.3 |
| Vertex AI Search settings (`vertex_search.*`) | `GAOS-Deploy-Spec.md` §10 |
| Google Docs settings (`docs.*`) | `config/settings.yaml.template` — `docs:` block |
| Blueprint Factory (Vision → Doc) | `Docs/agents/nexus-prime.md` — `VISION_SUBMITTED` handler |
| Google Search settings (`google_search.*`) | `config/settings.yaml.template` — `google_search:` block |

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
