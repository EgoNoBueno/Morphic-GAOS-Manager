# GAOS Privacy & Data Sovereignty Specification

**Morphic-G AOS** — Cloud Data Exposure, Privacy Risk Analysis, and Mitigation Strategies

> This document is a frank assessment of what data the AOS stores and transmits, who can access it, and what options exist to reduce exposure. Each mitigation is clearly labeled as **Optional Enhancement**, **Recommended**, or **Required for Compliance** so you can choose the right level for your use case.
>
> **This is not a compliance certification.** It is an honest map of the risk surface and a menu of tools to reduce it.  
> **Prerequisites:** `GAOS-Deploy-Spec.md` (infrastructure), `GAOS-Manager-Spec.md` §9 (cost profile), `GAOS-Agent-Spec.md` §2.2 (`AgentInput` schema).

---

## 1. Where Your Data Lives

The first step is distinguishing between data *you store* in GCP and data *processed* by an external LLM. These are different risks with different mitigations.

### 1.1 Data at Rest — Stored in Your GCP Project

| Data | Service | Notes |
|------|---------|-------|
| Business operational data (invoices, inventory, sales, HR) | Google Sheets | Owned by your GCP project; encrypted at rest by default |
| Agent reasoning traces, task history, cost logs | BigQuery (`aos_logs`) | Owned by your GCP project; encrypted at rest by default |
| Business rules, approved patterns | Vertex AI Memory Bank | Owned by your GCP project |
| Procedural docs, workflows, skill files | Google Drive (`Knowledge/`) | Owned by your GCP project |
| API keys, service account credentials | Secret Manager | Encrypted by default; access is per-secret IAM |
| MonologueFrame reasoning records | BigQuery (`aos_logs.monologue_frames`) | Includes task context fed to LLM at inference time; table schema defined in `GAOS-Persona-Spec.md` §4.2 — not yet in Deploy Spec §7 provisioning |

**Key point:** All of this data is isolated in *your* GCP project. It is not commingled with other GCP customers. Google's [Data Processing Addendum](https://cloud.google.com/terms/data-processing-addendum) governs it — not Google's consumer privacy policy.

### 1.2 Data in Transit — Sent to an External LLM

This is the more sensitive category. Every time a `DEEP_MODEL` or `FAST_MODEL` call fires, the system constructs a prompt that contains:

- The user's instruction (which may reference client names, financial figures, HR matters)
- `episodic_cache` — recent task history with outcomes
- `memory_context` — approved business rules (which may include pricing, vendor terms, margins)

That payload leaves your GCP project boundary and travels to **Google's Vertex AI inference endpoint**. The response comes back, but the payload was processed on Google's infrastructure.

`LOCAL_MODEL` (Ollama) calls never leave your machine. This distinction is the foundation of every mitigation strategy in this document.

| Model | Data leaves your control? | Governed by |
|-------|--------------------------|-------------|
| `LOCAL_MODEL` (Ollama) | **No** — inference on your hardware | No third party |
| `FAST_MODEL` (Gemini via Vertex AI) | **Yes** — sent to Google's infrastructure | GCP DPA |
| `DEEP_MODEL` (Gemini via Vertex AI) | **Yes** — sent to Google's infrastructure | GCP DPA |

---

## 2. What Protections Already Exist

Before identifying gaps, it is important to be accurate about what the current architecture already provides.

### 2.1 Your GCP Project = Your Isolation Boundary

Unlike consumer apps (Gemini on gemini.google.com, Google Workspace Consumer), data handled through a GCP project covered by a billing account is subject to Google's **Cloud Data Processing Addendum**. The key terms:

- Google does **not** use your customer data to train its models.
- Your data is not shared with other Google customers.
- You can request data deletion and receive confirmation.

This is materially different from pasting sensitive data into a consumer chatbot.

### 2.2 Per-Agent Least-Privilege IAM

Each of the 7 service accounts has only the permissions its role requires (see `GAOS-Deploy-Spec.md §2.2`). A compromised `beacon-sa` cannot read Ledger's financial data. An injection attack on the Scout agent cannot write to the `Agent_Approvals` tab.

### 2.3 Encryption at Rest and in Transit

All GCP services in this stack (BigQuery, Secret Manager, Cloud Logging, Drive, Sheets) encrypt data at rest by default using Google-managed keys. All communication is over TLS 1.2+.

### 2.4 Ollama Is Already in the Stack for Privacy-Sensitive Work

`LOCAL_MODEL` was designed precisely for tasks where privacy matters: observability logging, data formatting, log summarization. The hybrid routing table in `GAOS-Manager-Spec.md §5` gives you a per-task lever to route to Ollama. The gap is that this routing is currently based on *task complexity*, not *data sensitivity*.

### 2.5 Secret Manager — No Credentials on Disk

Service account keys are uploaded to Secret Manager and deleted from disk during deploy. No credentials are stored in the repo or in `.env` files that are committed.

---

## 3. The Gaps

| Gap | Risk Level | Impact |
|-----|-----------|--------|
| No policy defining which *data classifications* are allowed to reach a cloud LLM | High | Confidential data (client PII, financials) can be included in a Gemini prompt today with no gate |
| No PII/sensitive-data scrubbing before prompts are assembled | Medium | Entity data reaches the cloud LLM in the clear |
| Google holds the default encryption keys (GMEK) for BigQuery and Memory Bank | Medium | Google can theoretically access data at rest under legal compulsion |
| No GCP region constraint — data residency is unspecified | Medium | Relevant for GDPR (EU residents) or regulated industries |
| No VPC Service Controls perimeter | Low–Medium | Exfiltration path exists if a service account is compromised |
| Vertex AI Memory Bank stores approved business rules | Low | Rules themselves are usually not sensitive; risk increases if memory contains client-specific data |

---

## 4. Mitigation Options

Mitigations are organized into three tiers by implementation effort. None is mandatory unless your use case specifically requires it. Pick the tier that matches your sensitivity level.

---

### Tier 1 — Low Effort, High Impact

These can be implemented in a single session with no new GCP services.

#### Option A — `data_classification` Field on `AgentInput` *(Optional Enhancement)*

Add an optional `data_classification` field to the base `AgentInput` schema. When set, it overrides the normal model selection rules and forces local-only inference for confidential or restricted data. When omitted, the system behaves exactly as it does today — no change to existing agents.

```python
from typing import Literal, Optional
from pydantic import BaseModel

class AgentInput(BaseModel):
    task_id: str
    project_id: str
    instruction: str
    context: dict

    # ── Optional Privacy Field ──────────────────────────────────────────────
    # When set, overrides model selection rules for this task.
    # Omit entirely if not needed — system defaults to normal model routing.
    #
    # "public"       → No restrictions. Normal model selection applies.
    # "internal"     → Internal business data. Normal model selection applies.
    # "confidential" → Client names, financial details, HR data.
    #                  Forces LOCAL_MODEL. If LOCAL_MODEL unreachable,
    #                  task is PARKED (never falls back to cloud).
    # "restricted"   → Legally sensitive (PII, regulated data, privileged).
    #                  Forces LOCAL_MODEL. No fallback. Hard stop if unavailable.
    data_classification: Optional[Literal["public", "internal", "confidential", "restricted"]] = None
```

The enforcement rule lives in the agent boot utility (one function, shared by all agents):

```python
def resolve_model(input: AgentInput, agent_default_model: str) -> str:
    """
    Returns the model alias to use for this task.
    If data_classification is set and sensitive, enforces local-only.
    """
    sensitive = {"confidential", "restricted"}
    if input.data_classification in sensitive:
        return "LOCAL_MODEL"   # Ollama — never leaves the machine
    return agent_default_model
```

The hard-stop behavior for `"restricted"` tasks when Ollama is unavailable:

```python
def resolve_model_or_park(input: AgentInput, agent_default_model: str) -> str | None:
    """Returns model alias, or None if the task must be parked."""
    if input.data_classification == "restricted":
        if not _is_ollama_reachable():
            _park_task(input.task_id, reason="LOCAL_MODEL unavailable; restricted data cannot use cloud LLM")
            return None
        return "LOCAL_MODEL"
    return resolve_model(input, agent_default_model)
```

**What this buys you:** A hard, auditable boundary. Every task with a classification of `confidential` or `restricted` is provably local-only — visible in BigQuery logs via the `data_classification` column. No cloud LLM touches it.

**What it costs:** One optional field on each `AgentInput` instance that needs it. Agents that never handle sensitive data do not change at all.

---

#### Option B — Prompt Sanitization Before Cloud LLM Calls *(Optional Enhancement)*

A light pre-processing step that strips recognizable sensitive patterns from the prompt before it leaves the machine. The original data remains in working memory for tool execution; only the sanitized version goes to Gemini.

```python
import re

# Patterns to redact before sending to a cloud LLM
_REDACT_PATTERNS = [
    (re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'), "[EMAIL]"),
    (re.compile(r'\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b'), "[PHONE]"),
    (re.compile(r'\$[\d,]+(?:\.\d{2})?'), "[AMOUNT]"),
    (re.compile(r'\b\d{9}\b'), "[POTENTIAL_SSN]"),           # 9-digit numbers
    (re.compile(r'\b4[0-9]{12}(?:[0-9]{3})?\b'), "[CARD]"),  # Visa pattern
]

def sanitize_for_cloud_llm(text: str) -> str:
    """
    Redacts recognizable PII and financial patterns before sending to a cloud LLM.
    Call this on prompt strings, not on tool outputs or working memory.
    """
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
```

This is imperfect — named entities (client names, company names) are hard to redact without an NER model — but it eliminates the most common accidental exposures (email addresses in CRM data, dollar amounts in financial tasks, phone numbers in HR data).

---

#### Option C — GCP Data Region Constraint *(Recommended for EU/regulated users)*

Set a single region for all GCP services in `settings.yaml`. This ensures data at rest never leaves that geography, which is a GDPR requirement for EU residents and often required for regulated industries (healthcare, finance).

```yaml
# settings.yaml
gcp:
  project_id: morphic-gaos-prod
  region: us-central1    # United States
  # region: europe-west1 # For GDPR compliance (EU data residency)
  # region: asia-east1   # For APAC
```

Enforce this at provisioning time with a GCP Org Policy:

```bash
# Restrict resource location to a single region (run once, as project owner)
gcloud resource-manager org-policies set-policy \
  --project=morphic-gaos-prod \
  constraints/gcp.resourceLocations \
  --policy='{"constraint":"constraints/gcp.resourceLocations","listPolicy":{"allowedValues":["in:us-locations"]}}'
```

---

### Tier 2 — Medium Effort, Structural Improvement

These require additional GCP configuration but no code changes.

#### Option D — Customer-Managed Encryption Keys (CMEK) *(Optional Enhancement)*

By default, Google manages the encryption keys for all GCP storage services (BigQuery, Cloud Storage, Secret Manager). With CMEK, *you* generate and control the key in Google Cloud KMS. Google cannot access your data at rest without your key — including in response to a legal request directed at Google.

```bash
# Create a KMS keyring and key for the AOS
gcloud kms keyrings create morphic-gaos-keyring \
  --location=us-central1 --project=morphic-gaos-prod

gcloud kms keys create morphic-gaos-data-key \
  --location=us-central1 \
  --keyring=morphic-gaos-keyring \
  --purpose=encryption \
  --project=morphic-gaos-prod

# Apply to BigQuery dataset
bq update --dataset \
  --default_kms_key="projects/morphic-gaos-prod/locations/us-central1/keyRings/morphic-gaos-keyring/cryptoKeys/morphic-gaos-data-key" \
  morphic-gaos-prod:aos_logs
```

**Trade-off:** If you lose access to the KMS key, your data is unrecoverable. You are responsible for key rotation and backup. GCP provides [key rotation policies](https://cloud.google.com/kms/docs/key-rotation) and [Cloud HSM](https://cloud.google.com/kms/docs/hsm) for hardware-backed keys.

---

#### Option E — VPC Service Controls Perimeter *(Optional Enhancement)*

VPC Service Controls creates a security perimeter around your GCP APIs. Even if a service account credential is compromised, data cannot be exfiltrated to an IP outside the perimeter.

```bash
# Create an access policy (one per organization)
gcloud access-context-manager policies create \
  --organization=<ORG_ID> --title="Morphic GAOS Policy"

# Create the service perimeter
gcloud access-context-manager perimeters create morphic-gaos-perimeter \
  --policy=<POLICY_ID> \
  --title="GAOS Data Perimeter" \
  --resources="projects/<PROJECT_NUMBER>" \
  --restricted-services="bigquery.googleapis.com,storage.googleapis.com,secretmanager.googleapis.com,aiplatform.googleapis.com"
```

**Trade-off:** Requires a GCP Organization (not available on personal free-tier accounts). Adds meaningful operational complexity — any new GCP service or external integration must be explicitly granted perimeter access.

---

### Tier 3 — Architectural Alternatives

These are full topology changes for the highest-sensitivity use cases. They are more work but provide qualitatively stronger guarantees.

#### Option F — Local-First Topology *(Alternative Architecture)*

The architecture already supports this. Replace `DEEP_MODEL` and `FAST_MODEL` with a locally hosted high-quality model via Ollama. Gemini is never called. The only cloud services used are Sheets, Drive, BigQuery, and Pub/Sub — none of which receive LLM prompts.

```yaml
# settings.yaml — Local-First configuration
models:
  LOCAL_MODEL: ollama/llama3.3:70b      # Primary reasoning model (local)
  FAST_MODEL:  ollama/llama3.3:70b      # Same — no cloud LLM at all
  DEEP_MODEL:  ollama/llama3.3:70b      # Same — no cloud LLM at all
  LOCAL_MODEL_FALLBACK: null            # No cloud fallback — hard stop if Ollama down
```

**Hardware requirement:** A model capable of replacing `DEEP_MODEL` needs at minimum:
- `llama3.1:70b` — requires ~48GB VRAM (e.g., 2× RTX 4090, or an A100)
- `mistral-large` via Ollama — similar requirements
- A purpose-built mini-workstation (e.g., Mac Studio with 192GB unified memory) runs 70B models comfortably and costs ~$3,500

**What you give up:** Speed. Cloud Gemini inference is substantially faster than local 70B inference on consumer hardware, especially for long context. For a small business with moderate task volume, the latency is usually acceptable.

**What you gain:** Zero data leaves your premises for inference. Sheets, Drive, BigQuery, and Pub/Sub still use the cloud, but they handle structured data (rows, files, messages) — not raw LLM prompts carrying your business context.

---

#### Option G — Hybrid Private Topology *(Alternative Architecture)*

A middle path: keep `LOCAL_MODEL` for logging, formatting, and summarization (as today). Deploy a self-hosted mid-tier model (e.g., `llama3.1:8b` or `gemma2:9b`) for `FAST_MODEL` tasks. Reserve Gemini `DEEP_MODEL` **only** for the Approval Gate proposal formatting — the one task where Gemini's reasoning quality most clearly earns its privacy cost.

```yaml
# settings.yaml — Hybrid Private configuration
models:
  LOCAL_MODEL:  ollama/llama3.1:8b      # Free, local — logging, formatting
  FAST_MODEL:   ollama/llama3.1:8b      # Free, local — routing, moderate tasks
  DEEP_MODEL:   gemini-2.0-flash        # Cloud — Approval Gate proposals only
  LOCAL_MODEL_FALLBACK: gemini-2.0-flash
```

This topology reduces cloud LLM exposure to a small, well-defined category of tasks (complex reasoning for approval proposals) where the business context passed to Gemini is typically already at an abstracted, non-sensitive level (error fingerprints, code diffs, proposal summaries).

---

## 5. Decision Guide

Use this table to choose the right level for your situation.

| Use Case | Recommended Configuration |
|----------|--------------------------|
| Personal project, no client data | Default architecture — no changes needed |
| Small business, client names and financials in the system | Option A (`data_classification` on sensitive tasks) + Option C (region constraint) |
| Business with EU customers (GDPR applies) | Option A + Option C (`europe-west1`) + Option D (CMEK) |
| Regulated industry (healthcare, legal, finance) | Option F (Local-First) or Option G (Hybrid Private) + Option D + Option E |
| Maximum privacy, no cloud LLM ever | Option F (Local-First) with `LOCAL_MODEL_FALLBACK: null` |

---

## 6. What Google's DPA Actually Says

For GCP-covered projects, Google's [Cloud Data Processing Addendum](https://cloud.google.com/terms/data-processing-addendum) includes these material commitments:

- **No training on customer data.** Vertex AI does not use your prompts or responses to train foundation models.
- **Data deletion.** You can request deletion; Google confirms within contractual SLA.
- **Sub-processor disclosure.** Google publishes the list of sub-processors who may handle data.
- **Audit rights.** Enterprise customers can request audit reports (SOC 2 Type II, ISO 27001).

This is the baseline. It means using Vertex AI for LLM inference is categorically different from pasting data into a consumer chatbot — but it is not the same as running everything locally.

---

## 7. The Honest Trade-Off Summary

| Approach | Privacy Strength | Cost | Complexity |
|----------|-----------------|------|------------|
| Default (Gemini cloud LLM) | Moderate — GCP DPA applies | ~$1.50/month | Lowest |
| + `data_classification` routing (Option A) | Good — sensitive tasks never hit cloud LLM | No added cost | Low — one field, one function |
| + Prompt sanitization (Option B) | Good addition | No added cost | Low |
| + CMEK (Option D) | Strong at-rest protection | ~$0/month (KMS free tier) | Medium |
| Hybrid Private (Option G) | Strong — most tasks local | Hardware cost only | Medium |
| Local-First (Option F) | Maximum | Hardware ($2,500–$3,500 one-time) | Medium-High |

The **highest return on investment** for most small businesses is **Option A** (the optional `data_classification` field). It costs nothing, requires no new infrastructure, and creates an auditable, enforceable boundary for the specific tasks that actually carry sensitive data — without changing how any other task operates.

---

## Reference Index

| Topic | Document | Section |
|-------|----------|---------|
| `AgentInput` schema (base) | `GAOS-Agent-Spec.md` | §2.2 |
| Model selection rules | `GAOS-Manager-Spec.md` | §11 |
| Ollama hybrid routing and fallback | `GAOS-Manager-Spec.md` | §5 |
| IAM service account setup | `GAOS-Deploy-Spec.md` | §2 |
| Secret Manager setup | `GAOS-Deploy-Spec.md` | §3 |
| GCP region configuration | `GAOS-Deploy-Spec.md` | §8 |
| `think` node and MonologueFrame logging | `GAOS-Persona-Spec.md` | §4 |
