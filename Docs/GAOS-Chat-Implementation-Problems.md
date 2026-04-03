# GAOS Integration Problems — Morphic-GAOS

Production failures encountered during GAOS Chat and Gmail integration development.
Organized by failure class. Each entry has root cause, symptom, and exact fix.

_Last updated: 2026-04-03 — Phase 4_

> ⚠️ **Note — Google Chat delivery abandoned 2026-03-30.** The `/chat` endpoint
> remains deployed and card-button callbacks still function, but Google Chat never
> reliably delivered messages from mobile. The primary communication channel is now
> Gmail. Entries in this document apply to the full GAOS integration stack, not
> Chat exclusively. See `GAOS-Chat-Dev-Reference.md` for the full post-mortem.

---

## Infinite-Loop / Self-Amplifying Failures

### 1. STATUS_UPDATE self-loop saturates all Cloud Run instances

**Root cause:** `record()` published every log message to `agent.nexus-prime.events`.
`nexus-prime.sub.events` is a push subscription on that same topic. Every message
completion triggered a delivery → `agent.run()` → `record()` → publish → repeat at
~25 req/sec.

**Symptom:** All 5 Cloud Run instances at 429 "no available instance", Sheets write
quota exhausted within minutes, zero `/chat` log entries responding.

**Fix:**
```python
if not (msg and msg.message_type == MessageType.STATUS_UPDATE):
    publish(topic, msg)
```

**Fixed:** 2026-03-22 in `agents/nexus_prime/orchestrator.py`.

---

### 2. HTTP 204 + JSON body causes uvicorn crash and Pub/Sub retry storm

**Root cause:** `JSONResponse(content={...}, status_code=204)` sends a body on a
No-Content response. uvicorn raises `RuntimeError: Response content longer than
Content-Length` and closes the TCP connection. Pub/Sub treats this as delivery
failure and retries at ~1 req/sec indefinitely.

**Symptom:** The same Pub/Sub message ID appearing in Cloud Logging every second.
Handler appears to execute but the message never ACKs. Instances stuck.

**Fix:** `return Response(status_code=204)` — no `content=` argument, no `JSONResponse`.
Any 204 path must have zero body.

**Fixed:** 2026-03-21 in `/pubsub` handler in `main.py`.

---

### 3. Gmail outbound reply triggers infinite inbound loop (2026-04-02)

**Root cause:** `GMAIL_AUTHORIZED_SENDERS` contained `aos@sl10repairtechs.com` — the
same address the system sends from. When a test email was sent from the monitored
inbox, an auto-reply arrived from `aos@`. It passed the authorized-senders gate,
triggered a new `compose_reply` run, which sent another reply, which triggered
another auto-reply — indefinitely. The code-layer `_own_addresses` check existed
but was not yet deployed in the active Cloud Run revision at the time.

**Symptom:** ~89,000 Pub/Sub faults and 18 outbound emails before manual
intervention. Cloud Logging showed `EMAIL_RECEIVED` messages at burst rate. No
exception was raised — the loop looked like normal high-volume processing.

**Impact scale:** A 25-turn loop at ~$0.003/reply = ~$0.075. At Pub/Sub fanout rates
this reached saturation in under 3 minutes.

**Fix — two independent layers required:**

1. **Code layer** — `_own_addresses` check runs *before* the authorized-senders
   gate in `process_gmail` (per Rule 26.1):
   ```python
   _own_addresses = {settings.gmail.monitored_address, settings.gmail.sender_address}
   if from_addr in _own_addresses:
       continue  # exits before authorized_senders check
   ```
2. **Config layer** — remove `aos@sl10repairtechs.com` from `GMAIL_AUTHORIZED_SENDERS`.
   The secret must only contain real human inboxes. Correct value:
   `dhess@sl10repairtechs.com,denton.hess@gmail.com`.

Neither layer alone is sufficient: a code-only guard is bypassed by misconfiguration;
a config-only guard is bypassed by code regression. Both must pass simultaneously
for a loop to form.

**Fixed:** 2026-04-02 — removed `aos@` from secret; code-layer guard deployed.

---

## Auth Failures That Present as Silent 403s

### 4. `impersonated_credentials.Credentials(subject=...)` silently ignores `subject`

**Root cause:** The generated token has no `sub` claim — the SA acts as itself with
no Drive quota → 403 on every Docs/Drive call. This is a known limitation of
`google.auth.impersonated_credentials`.

**Symptom:** 403 `PERMISSION_DENIED` on Docs/Drive API calls even though DWD is
configured and the SA has the correct IAM roles.

**Fix:** Use the correct DWD path: `google.auth.iam.Signer` (signBlob) +
`service_account.Credentials(signer, subject=user)`. Requires
`roles/iam.serviceAccountTokenCreator` on the SA itself, plus DWD configured in
Google Workspace Admin → Security → API Controls → Domain-wide Delegation.

**Fixed:** 2026-03-24 in `tools/google_docs.py`.

---

### 5. `iamcredentials.googleapis.com` must be enabled — error message is misleading

**Root cause:** `google.auth.iam.Signer` calls the IAM Credentials API (`signBlob`).
If the API is not enabled on the project, the call returns `403 SERVICE_DISABLED`.

**Misleading detail:** The error message references the quota project number, not
the SA's project — when they differ, this sends you looking in the wrong place.

**Fix:** Enable `iamcredentials.googleapis.com` on both the SA's project AND the
ADC quota project (if different):
```powershell
gcloud services enable iamcredentials.googleapis.com --project=morphic-gaos-prod
```

**Fixed:** 2026-03-24.

---

### 6. Chat JWT audience must include the `/chat` path

**Root cause:** `_verify_chat_jwt()` was verifying against the base `CLOUD_RUN_URL`.
Google Chat signs JWTs with an audience of `{service_url}/chat` — including the path.

**Symptom:** Every inbound Chat event returns 401. JWT verification passes locally
(where `SKIP_CHAT_JWT_VERIFY=true`) but fails in Cloud Run.

**Fix:**
```python
# Correct
audience = f"{service_url}/chat"   # not just service_url
```

**Fixed:** 2026-03-21 in `main.py`.

---

### 7. Pub/Sub push subscriptions missing OIDC auth

**Root cause:** Pub/Sub push subscriptions to Cloud Run require an OIDC service
account token so Cloud Run's IAM check accepts the request. Subscriptions created
without `--push-auth-service-account` send unauthenticated requests — every push
returns 401, which Pub/Sub treats as a delivery failure and retries at ~1 req/sec.

**Symptom:** Cloud Logging flooded with 401s at ~1/sec per subscription. The 8
`/pubsub` subscriptions (one per agent × `/pubsub`) generated the flood while the
2 Gmail subs (provisioned later, with OIDC) were healthy. The subscriptions appeared
green in `gcloud pubsub subscriptions list` — no health indicator distinguishes
"delivering 401" from "delivering 200".

**Misleading detail:** The subscription list output shows no `serviceAccountEmail`
field when OIDC is absent — it does not show an error. The only observable signal is
the 401 rate in Cloud Logging.

**Fix:** Re-configure each affected subscription:
```powershell
gcloud pubsub subscriptions modify-push-config <subscription-name> `
    --push-auth-service-account=pubsub-push-sa@morphic-gaos-prod.iam.gserviceaccount.com `
    --project=morphic-gaos-prod
```

Verify after applying:
```powershell
gcloud pubsub subscriptions describe <subscription-name> | Select-String "serviceAccountEmail"
```

**Prevention:** `scripts/provision_infra.py` (or Terraform) must set
`--push-auth-service-account` on every push subscription at creation time. Any
subscription without this field is misconfigured.

**Fixed:** 2026-04-02 — all 8 `/pubsub` subscriptions re-configured with OIDC.

---

## Silent Config Failures (No Error, Wrong Behavior)

### 8. Pydantic silently drops unknown YAML fields

**Root cause:** Pydantic `BaseModel` ignores YAML keys that have no matching field
declaration. The value is present in `settings.yaml` and correctly stored in Secret
Manager, but at runtime the field reads as its default (usually `""`). No warning,
no error.

**Symptom:** Config looks correct everywhere you check it. Runtime shows `""`.
In the DWD case: `dwd_subject` always empty → DWD branch never entered → ADC fallback
→ SA acts as itself → 403.

**Fix:** Every new key added to `settings.yaml` must have a matching typed field in
the relevant config class in `config/__init__.py`:
```python
# ❌ Wrong
class DocsConfig(BaseModel):
    scope: str = ""

# ✅ Correct
class DocsConfig(BaseModel):
    scope: str = ""
    dwd_subject: str = ""   # must declare or Pydantic silently drops the YAML value
```

**Diagnostic:** Add `log.warning("tool: credential path=<branch>")` at each branch
entry — one Cloud Logging line reveals which path Cloud Run actually took.

**Fixed:** 2026-03-24 in `config/__init__.py`.

---

### 9. `dict.get("key", default)` does not fall back on empty string

**Root cause:** `initial_state` initialized `project_id=""`. `.get("project_id", settings.GCP_PROJECT_ID)`
returns `""` — the empty string IS the value. The default is only used when the key
is absent.

**Symptom:** All calls that need a `project_id` downstream receive an empty string
and fail silently or with a misleading error.

**Fix:**
```python
# ❌ Wrong
project_id = state.get("project_id", settings.GCP_PROJECT_ID)

# ✅ Correct
project_id = state.get("project_id") or settings.GCP_PROJECT_ID
```

**Fixed:** 2026-03-21 in `boot()` in the orchestrator.

---

### 10. `GOOGLE_APPLICATION_CREDENTIALS` env var silently overrides ADC

**Root cause:** If this env var is set in `.env` or the shell (even to a nonexistent
file path), `google-auth` skips `gcloud auth application-default login` credentials
entirely. No error is raised — you just get the wrong credentials.

**Symptom:** Local ADC works when the var is absent; all auth fails when it is set.
Error messages point to credential issues, not to env var interference.

**Fix:** Remove `GOOGLE_APPLICATION_CREDENTIALS` from `.env` and the shell environment
when using ADC locally. The var should only be set deliberately for service account
key file auth.

---

### 11. `publish()` silently drops messages when called with a third positional argument

**Root cause:** `publish(topic, msg)` reads `project_id` internally from
`settings.GCP_PROJECT_ID`. Five orchestrators were calling `publish(topic, msg, pid)`
— the extra positional arg raises `TypeError`, which was swallowed by a broad
`except Exception: pass`. All Pub/Sub messages from those 5 services were silently
dropped.

**Symptom:** Agents appear to run normally (logs are written) but no A2A messages
are ever delivered between agents. Only Nexus-Prime (which had the correct 2-arg
call) was publishing successfully.

**Fix:** `publish(topic, msg)` — no third argument. The function takes exactly 2 args.

**Fixed:** 2026-03-23 across 5 orchestrators (14 call sites).

---

## Cloud Run Environment Traps

### 12. `--timeout 60s` causes `CancelledError` on LLM calls

**Root cause:** Gemini API + LangGraph graph traversal can exceed 60s under load or
during cold start. When the Cloud Run request timeout fires, the async task is
cancelled mid-flight.

**Symptom:** `asyncio.exceptions.CancelledError` in Cloud Logging — not a timeout
error, which makes it hard to diagnose. Agents silently fail tasks with no reply
sent to the originating space.

**Fix:** Use `--timeout 300s` on all agent services:
```powershell
gcloud run services update nexus-prime --timeout=300 --region=us-central1
```

---

### 13. `gcloud run deploy --source` with a pinned traffic revision does not auto-route

**Root cause:** If any traffic is pinned to a specific revision (`nexus-prime-00050=100`),
a new deploy creates the revision but leaves traffic on the old pinned revision.

**Symptom:** Deploy succeeds, Cloud Run shows a new revision, but the live service is
still running the old code. Takes several requests to notice.

**Fix:** After every `gcloud run deploy --source`, verify traffic routing:
```powershell
gcloud run services describe nexus-prime --region=us-central1 --format="value(status.traffic)"
# If not pointing at the new revision:
gcloud run services update-traffic nexus-prime --to-latest --region=us-central1
```

---

### 14. `MemorySaver` (LangGraph in-process checkpoint) wiped on every cold start

**Root cause:** `MemorySaver` stores graph state in process RAM. Cloud Run scales to
zero after ~15 minutes of inactivity and between request scaling events. Every cold
start is a blank slate.

**Current impact:** Low — `chat_respond` is stateless by design; context is loaded
fresh from Sheets/Vertex per request. `MemorySaver` only holds `parked_proposals`
and `system_state_summary` from the `boot` node.

**Future risk:** Any multi-turn Chat flow that assumes state persists between user
messages will silently lose context mid-conversation. See GAOS-Chat-Dev-Reference.md
§18 for durable checkpointer options (BigQuery, Firestore, Redis).

---

## Dependency / API Mismatches

### 15. `google-generativeai` is EOL — model calls return 404

**Root cause:** The Gemini Python SDK split. `google-generativeai` (old package) routes
to the `v1beta` endpoint, which has dropped support for several model versions
(`gemini-1.5-pro`, etc.). Imports work; calls fail at runtime.

**Symptom:** 404 on model calls with a version that was working before. The old
package imports with `FutureWarning` but the error only surfaces on the first
API call.

**Fix:** Migrate to `google-genai>=1.0.0` with the new `genai.Client()` API:
```python
# ❌ Old
import google.generativeai as genai
model = genai.GenerativeModel("gemini-1.5-pro")

# ✅ New
from google import genai
client = genai.Client()
response = client.models.generate_content(model="gemini-1.5-pro", ...)
```

Full migration notes in `GAOS-Deploy-Spec.md §0.3`.

---

### 16. AI Studio API keys bill to Google's shared project, not yours

**Root cause:** API keys created at `aistudio.google.com` are scoped to a Google-owned
shared project — your GCP billing credits and quota allocations do not apply to them.

**Symptom:** `429 RESOURCE_EXHAUSTED` with `limit: 0` even with billing enabled and
quota visible in Cloud Console. The key works in AI Studio but fails in code that
runs against your project's quota.

**Fix:** Create the API key inside your own GCP project at
`console.cloud.google.com/apis/credentials`, not at AI Studio. Full setup in
`GAOS-Deploy-Spec.md §3.1`.

---

## The Meta-Pattern

The most damaging failures above were **silent** — no exception raised, no log
emitted, just wrong behavior downstream. The most effective single countermeasure
across all of them:

> **Add explicit branch-marker log lines at every credential path, config branch,
> and publish call site.** A `log.warning("module: path=<branch>")` at each entry
> point makes the actual runtime path observable in Cloud Logging without needing
> to reproduce locally or add a deploy cycle.

The second most effective countermeasure: **broad `except Exception: pass` blocks
are production bugs.** Every swallowed exception in this list masked a real failure
for hours or longer. At minimum: log the exception type and the context before
continuing.
