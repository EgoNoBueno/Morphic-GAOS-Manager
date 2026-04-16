# GAOS Voice Input Specification

*Last updated: 2026-04-16*

> **Status: PLANNED — Not yet implemented**
>
> This document captures the complete architecture for adding voice input to
> Nexus-Prime. No new endpoints or tools exist yet. Sections §1–§3 describe
> what works today with zero code. Section §4 defines the `/speak` endpoint
> build plan. Proceed to §4 only after reading §2 (Google Chat diagnosis
> requirement).

---

## Table of Contents

1. [What Works Today — Zero-Code Path](#1-what-works-today--zero-code-path)
2. [Google Chat Status and Diagnosis Gate](#2-google-chat-status-and-diagnosis-gate)
3. [Architecture Overview](#3-architecture-overview)
4. [Option B — `/speak` Endpoint Build Plan](#4-option-b--speak-endpoint-build-plan)
5. [Authentication](#5-authentication)
6. [Client Setup — iOS Shortcut](#6-client-setup--ios-shortcut)
7. [Client Setup — Android Tasker](#7-client-setup--android-tasker)
8. [Response Delivery](#8-response-delivery)
9. [Secrets and Config](#9-secrets-and-config)
10. [Security Constraints](#10-security-constraints)
11. [Testing the Endpoint](#11-testing-the-endpoint)
12. [Future: Text-to-Speech Reply](#12-future-text-to-speech-reply)

---

## 1. What Works Today — Zero-Code Path

Voice input via Gmail is already functional with no changes to the codebase:

1. Open the native email app on any phone.
2. Use the phone's built-in voice dictation (Siri, Gboard mic, Samsung keyboard) to
   compose the message body.
3. Send to the monitored Gmail inbox (`settings.yaml → gmail.monitored_address`) with
   **"Kenny"** in the subject line (current `trigger_keyword`).
4. GAOS processes the email as `EMAIL_RECEIVED` through the full pipeline:
   `handle_gmail_webhook → process_gmail_notification → compose_reply`.
5. Reply arrives in the monitored inbox within 15–60 seconds.

**What it costs:** Nothing. **What it requires:** Nothing new.

**Limitation:** Round-trip is async (15–60 seconds). No instant response. Suitable for
requests where latency is acceptable.

---

## 2. Google Chat Status and Diagnosis Gate

> ⚠️ **Read this before building `/speak`.** Building a new endpoint is unnecessary if
> the Chat delivery issue can be fixed with configuration.

### Current State

The Google Chat integration was **abandoned on 2026-03-30** after messages never
reliably delivered from mobile over ~2 weeks of testing. The full post-mortem is in
`GAOS-Chat-Dev-Reference.md §1` and `GAOS-Deploy-Spec.md §14`.

**What still works:**
- The `/chat` endpoint is live, deployed, and JWT-verified.
- `CARD_CLICKED` events (Approval Gate buttons) deliver correctly.
- `CHAT_MESSAGE` routing through `think → chat_respond` is fully wired in the
  LangGraph orchestrator.

**What was broken:** Text message delivery from mobile. The symptom was that
messages sent from the Chat mobile app never triggered a push event to the `/chat`
endpoint. Desktop delivery may have worked inconsistently.

### Diagnosis Requirement

**Before building `/speak`**, spend 30 minutes on this diagnosis. The broken behavior
may be a configuration issue, not a Google Chat API limitation:

| Check | How to verify |
|-------|--------------|
| Chat App is configured as a Bot (not just webhook) | Cloud Console → Chat API → Configuration → App type = **Bot** |
| App URL matches `CLOUD_RUN_URL` exactly (including `https://`, no trailing slash) | Cloud Console → Chat API → Configuration → Bot URL |
| The Chat app is authorized for "Direct Messages" and "Spaces" | Configuration → Visibility/Functionality |
| The App is added to a DM space with the owner's account | Open Google Chat → Start DM → search the app by name |
| A test message from desktop triggers a `/chat` log entry in Cloud Logging | `gcloud logging read 'resource.labels.service_name="nexus-prime" AND httpRequest.requestUrl=~"/chat"'` |
| A test message from mobile triggers a `/chat` log entry | Same query — if desktop hits but mobile doesn't, the app is configured but DM push is broken on mobile specifically |

If desktop delivers and mobile doesn't: this is likely a Google Chat mobile push
delivery issue specific to direct-message bots on the free Google Workspace tier. In
that case, build `/speak`.

If neither delivers: fix the Chat app URL or bot configuration before proceeding.

---

## 3. Architecture Overview

Three options exist in order of increasing build cost:

| Option | Description | Works Today? | Build Cost |
|--------|-------------|:---:|--------|
| **A — Gmail voice** | Dictate email → send to monitored inbox | ✅ Yes | None |
| **B — `/speak` endpoint** | iOS Shortcut/Tasker → HMAC POST → Cloud Run → LangGraph | ❌ No | ~40 lines + Shortcut setup |
| **C — Restore Chat** | Fix Google Chat mobile delivery | ❌ No | Diagnosis first, then 0–2 hours |

### Option B Data Flow

```
Phone microphone
      │  Native device speech-to-text (Siri, Gboard, Samsung, etc.)
      │  No new mobile app required — dictated text as plain string
      ▼
iOS Shortcut / Android Tasker HTTP action
      │  POST https://<CLOUD_RUN_URL>/speak
      │  Header: X-GAOS-Signature: sha256=<hmac_hex>
      │  Body: {"text": "...", "project_id": "morphic-gaos-prod"}
      ▼
main.py  @app.post("/speak")
      │  _verify_voice_hmac(request)       ← constant-time HMAC check
      │  Build A2AMessage(type=CHAT_MESSAGE, payload={text, sender, space})
      ▼
_get_agent().run(envelope)
      │  NexusPrimeAgent LangGraph
      │  boot → monitor → route → think → chat_respond
      ▼
Response delivery (see §8)
```

---

## 4. Option B — `/speak` Endpoint Build Plan

### 4.1 New endpoint in `main.py`

Add after the existing `/chat` handler block. The endpoint must:

1. Verify the HMAC signature before touching the body (fail-closed, 401 if invalid).
2. Build an `A2AMessage` with `MessageType.CHAT_MESSAGE`.
3. Call `_get_agent().run(envelope)` — identical to how `/chat` does it.
4. Return `{"status": "ok"}` once the graph completes (or an async ACK pattern for
   slow responses — see §4.3).

```python
@app.post("/speak")
async def speak_handler(request: Request) -> Response:
    """Voice input gateway — HMAC-signed POST from iOS Shortcut or Android Tasker.

    Accepts a plain-text dictated message, verifies the HMAC-SHA256 signature,
    builds a CHAT_MESSAGE envelope, and runs the full LangGraph pipeline.

    Required header:
        X-GAOS-Signature: sha256=<hex digest of body using VOICE_HMAC_SECRET>

    Body (JSON):
        text       (str)  — Dictated message text
        project_id (str)  — GCP project ID (e.g. "morphic-gaos-prod")

    Returns:
        {"status": "ok", "reply": "<agent response>"}
    """
    body_bytes = await request.body()
    if len(body_bytes) > 8192:
        raise HTTPException(status_code=413, detail="Request body exceeds 8192-byte limit")
    _verify_voice_hmac(request, body_bytes)

    try:
        data = json.loads(body_bytes)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    text = data.get("text", "").strip()
    project_id = data.get("project_id", _get_project_id())

    if not text:
        raise HTTPException(status_code=400, detail="'text' field is required")

    envelope = A2AMessage(
        message_type=MessageType.CHAT_MESSAGE,
        sender_id="voice-input",
        project_id=project_id,
        payload={
            "text": text,
            "sender_email": settings.gmail.monitored_address,
            "space_name": "voice",       # sentinel — no real Chat space
            "message_name": "",
        },
    )

    agent = _get_agent()
    result_state = await agent.run(envelope)

    reply = result_state.get("chat_reply", "") if result_state else ""
    return JSONResponse(content={"status": "ok", "reply": reply})
```

### 4.2 HMAC verification helper

Add alongside `_verify_chat_jwt()` in `main.py`:

```python
def _verify_voice_hmac(request: Request, body_bytes: bytes) -> None:
    """Verify X-GAOS-Signature header using VOICE_HMAC_SECRET from Secret Manager.

    Performs a constant-time comparison to prevent timing attacks.

    Raises:
        HTTPException(401): Signature header missing, malformed, or digest mismatch.
    """
    import hmac as _hmac
    import hashlib

    sig_header = request.headers.get("X-GAOS-Signature", "")
    if not sig_header.startswith("sha256="):
        raise HTTPException(status_code=401, detail="Missing or malformed X-GAOS-Signature")

    provided_hex = sig_header[len("sha256="):]
    project_id = _get_project_id()

    try:
        secret = get_secret("VOICE_HMAC_SECRET", project_id)
    except Exception as exc:
        log.error("VOICE_HMAC_SECRET fetch failed: %s", exc)
        raise HTTPException(status_code=503, detail="Auth service unavailable") from exc

    expected = _hmac.new(
        secret.encode("utf-8"),
        body_bytes,
        hashlib.sha256,
    ).hexdigest()

    if not _hmac.compare_digest(provided_hex, expected):
        raise HTTPException(status_code=401, detail="HMAC signature mismatch")
```

### 4.3 Response timeout note

`chat_respond` calls `FAST_MODEL`, which typically returns in 3–8 seconds. The `/speak`
endpoint can safely block-await within Cloud Run's default 60-second request timeout.

If the request body triggers a `DEEP_MODEL` path (e.g. a research or evolution request),
implement the async ACK pattern: return `{"status": "queued"}` immediately and
deliver the reply via email to `monitored_address`. The Shortcut/Tasker client can
poll or simply accept that heavier requests reply via email.

---

## 5. Authentication

The `/speak` endpoint uses **HMAC-SHA256 request signing** — the same scheme as
`tools/webhook_sender.py`.

### How it works

1. The mobile client (Shortcut/Tasker) holds `VOICE_HMAC_SECRET` as a local variable.
2. Before each request, the client computes `HMAC-SHA256(request_body, secret)` and
   sends the hex digest as `X-GAOS-Signature: sha256=<hex>`.
3. The server fetches `VOICE_HMAC_SECRET` from Secret Manager and computes the same
   digest independently. Constant-time comparison prevents timing attacks.
4. Any mismatch → 401. The body is never processed.

### Why not Google JWT (like `/chat`)?

Google JWT requires a Google identity issuer. A mobile shortcut has no Google service
account — it's a personal device action. HMAC is the correct choice for device-to-service
authentication where the client cannot hold a service account credential.

### VOICE_HMAC_SECRET provisioning

```powershell
# Generate a 256-bit secret (32 bytes hex = 64 chars)
$SECRET = -join ((1..32) | ForEach-Object { [byte][System.Security.Cryptography.RandomNumberGenerator]::GetInt32(256) } |
    ForEach-Object { $_.ToString("x2") })

# Store in Secret Manager
echo $SECRET | gcloud secrets create VOICE_HMAC_SECRET `
    --project=morphic-gaos-prod `
    --data-file=-

# Grant Cloud Run SA access
gcloud secrets add-iam-policy-binding VOICE_HMAC_SECRET `
    --project=morphic-gaos-prod `
    --member="serviceAccount:<cloud-run-sa>@morphic-gaos-prod.iam.gserviceaccount.com" `
    --role="roles/secretmanager.secretAccessor"
```

> ⚠️ **Never log or print `VOICE_HMAC_SECRET`.** Store it only in Secret Manager (server
> side) and as a local variable inside the iOS Shortcut or Tasker profile (client side).
> Do not commit it to any file, env var in Dockerfile, or settings.yaml.

---

## 6. Client Setup — iOS Shortcut

Create a shortcut named **"Hey GAOS"** (or any trigger phrase you prefer).

### Shortcut action sequence

| Step | Action | Configuration |
|------|--------|--------------|
| 1 | **Dictate Text** | Language: English. Prompt: "What do you need?" |
| 2 | **Set Variable** `DictatedText` | Value: Dictation Result |
| 3 | **Text** (build JSON body) | `{"text": "[DictatedText]", "project_id": "morphic-gaos-prod"}` |
| 4 | **Set Variable** `RequestBody` | Value: Text result from step 3 |
| 5 | **Run Script Over SSH** or **Get Contents of URL** | See HMAC note below |
| 6 | **Get Dictionary Value** | Key: `reply` from the URL response |
| 7 | **Speak Text** *(optional)* | Speak the reply aloud |
| 8 | **Show Notification** | Body: `reply` value |

### HMAC signing limitation

iOS Shortcuts has no native HMAC action. Two options:

**Option 1 — Use a signing proxy (recommended):**
Run a tiny local FastAPI or Flask server on a Mac that stays on (or a Raspberry Pi)
that computes the HMAC and forwards to Cloud Run. The Shortcut POSTs unsigned to the
proxy (`http://192.168.x.x:9000/sign-and-send`); the proxy adds the header and
forwards to Cloud Run.

**Option 2 — Layer Cloud Armor IP allowlist on top of HMAC:**
If your home/office has a static IP, add a Cloud Armor policy restricting `/speak`
to that CIDR range as a second layer of defense. **Do not remove HMAC** — Section 10
prohibits any alternative auth path on `/speak`. The IP allowlist reduces the attack
surface but does not replace cryptographic request signing. Less portable but adds
defense-in-depth with zero change to the HMAC verification logic.

**Option 3 — Use a Scriptable app action:**
[Scriptable](https://scriptable.app/) runs JavaScript inside Shortcuts. You can call
`CryptoKit` equivalent logic from a Scriptable script to compute the HMAC and include
it in the URL request headers. Sample script available at implementation time.

### Siri voice trigger

After building the shortcut: Settings → Accessibility → Siri → then say
*"Hey Siri, Hey GAOS"* (or the shortcut name you chose). Siri runs the shortcut.

---

## 7. Client Setup — Android Tasker

Tasker can compute HMAC natively via the **JavaScriptlet** action using Android's
`javax.crypto` library.

### Task action sequence

| Step | Action | Configuration |
|------|--------|--------------|
| 1 | **Say To Me** (input prompt) or **Google Assistant** voice trigger | Returns spoken text as `%SIRI_TEXT` variable (via AutoVoice plugin) |
| 2 | **JavaScriptlet** | Compute HMAC-SHA256 of `JSON.stringify({text: ..., project_id: ...})` using stored secret — see script below |
| 3 | **HTTP Request** | URL: `https://<CLOUD_RUN_URL>/speak`, Method: POST, Headers: `X-GAOS-Signature: sha256=%hmac_hex`, Body: JSON from step 2 |
| 4 | **Variable Set** `%reply` | From HTTP response body → `.reply` field |
| 5 | **Say** *(optional)* | Speak `%reply` |
| 6 | **Notify** | Flash notification with content of `%reply` |

### Tasker JavaScriptlet for HMAC

```javascript
// Tasker JavaScriptlet action — runs in Android JS engine
var text = local.voice_text;   // set from AutoVoice or prior variable
var projectId = "morphic-gaos-prod";
var secret = local.hmac_secret; // store in Tasker local variable, not hardcoded

var body = JSON.stringify({ text: text, project_id: projectId });

// Android javax.crypto
var key = new java.lang.String(secret).getBytes("UTF-8");
var mac = javax.crypto.Mac.getInstance("HmacSHA256");
mac.init(new javax.crypto.spec.SecretKeySpec(key, "HmacSHA256"));
var rawHmac = mac.doFinal(new java.lang.String(body).getBytes("UTF-8"));

// Convert byte array to hex string
var hexChars = "0123456789abcdef";
var hex = "";
for (var i = 0; i < rawHmac.length; i++) {
    var b = rawHmac[i] & 0xFF;
    hex += hexChars.charAt(b >> 4) + hexChars.charAt(b & 0x0F);
}

local.hmac_hex = hex;
local.request_body = body;
```

Then use `%hmac_hex` in the HTTP Request header and `%request_body` as the POST body.

---

## 8. Response Delivery

When `/speak` completes, the reply is returned in the HTTP response body
(`{"status": "ok", "reply": "..."}`). The client reads it directly.

| Response latency | Cause | Client behavior |
|-----------------|-------|-----------------|
| < 10 seconds | FAST_MODEL path | Show inline in Shortcut/Tasker |
| 10–30 seconds | Complex reasoning, memory query | Show inline — still within timeout |
| > 30 seconds | DEEP_MODEL or slow BQ query | Endpoint should return `{"status": "queued"}` and deliver reply via email |

**Email fallback for heavy requests:**
If `chat_respond` detects the response will require `DEEP_MODEL`, it should
set `state["voice_reply_via_email"] = True` and route the finished reply to
`send_email()` → monitored inbox. The `/speak` handler then returns
`{"status": "queued", "reply": "Working on it — reply coming via email."}`.

This is not yet implemented. For Phase 1 of `/speak`, all requests use FAST_MODEL
and return synchronously.

---

## 9. Secrets and Config

### New secret required

| Secret Name | Purpose | Where stored |
|-------------|---------|-------------|
| `VOICE_HMAC_SECRET` | Signs and verifies `/speak` requests | Secret Manager (`morphic-gaos-prod`) |

### No new `settings.yaml` keys required

The `/speak` endpoint uses `gmail.monitored_address` as the sender identity sentinel
and reads `VOICE_HMAC_SECRET` directly from Secret Manager at request time. No new
settings fields are needed.

### Endpoint URL

The Cloud Run URL is already in `CLOUD_RUN_URL` (used by Chat JWT verification).
`/speak` appends to the same base URL. No new Cloud Run service or deployment is needed.

---

## 10. Security Constraints

| Constraint | Rule |
|-----------|------|
| HMAC check is fail-closed | A missing or invalid `X-GAOS-Signature` header returns 401 before any body processing |
| Constant-time comparison | Use `hmac.compare_digest()` — never `==` on hex strings (timing attack) |
| Secret never logged | `VOICE_HMAC_SECRET` must not appear in Cloud Logging, even on errors |
| Body size limit | Reject requests where `Content-Length > 8192` bytes — dictated input is never that long |
| Rate limit awareness | The `/speak` endpoint calls `_get_agent().run()` which invokes an LLM. Per Rule 25, the orchestrator already has retry/cost guards. No additional rate limiter is needed at the HTTP layer initially, but monitor for abuse. |
| No unauthenticated endpoint | Do not add a SKIP_VOICE_HMAC env var equivalent. Unlike `/chat` (which has a Google-controlled JWT chain), `/speak` has no alternative auth path. |

---

## 11. Testing the Endpoint

### Smoke test — curl (PS 7+) or Invoke-WebRequest

```powershell
# 1. Compute HMAC on local machine for a test body
$SECRET = (gcloud secrets versions access latest --secret=VOICE_HMAC_SECRET --project=morphic-gaos-prod)
$BODY = '{"text": "What is the current system status?", "project_id": "morphic-gaos-prod"}'
$KEY = [System.Text.Encoding]::UTF8.GetBytes($SECRET)
$MSG = [System.Text.Encoding]::UTF8.GetBytes($BODY)
$HMAC = [System.Security.Cryptography.HMACSHA256]::new($KEY)
$HEX = ($HMAC.ComputeHash($MSG) | ForEach-Object { $_.ToString("x2") }) -join ""

# 2. POST to the endpoint
$TOKEN = gcloud auth print-identity-token
Invoke-WebRequest `
    -Uri "https://<CLOUD_RUN_URL>/speak" `
    -Method POST `
    -Headers @{
        "Authorization" = "Bearer $TOKEN"
        "X-GAOS-Signature" = "sha256=$HEX"
        "Content-Type" = "application/json"
    } `
    -Body $BODY
```

Expected response: `{"status": "ok", "reply": "<Nexus-Prime response text>"}`

### What to verify

| Check | Expected |
|-------|---------|
| Valid HMAC → 200 | `{"status": "ok", "reply": "..."}` |
| Missing header → 401 | `{"detail": "Missing or malformed X-GAOS-Signature"}` |
| Wrong secret → 401 | `{"detail": "HMAC signature mismatch"}` |
| Empty `text` field → 400 | `{"detail": "'text' field is required"}` |
| Response content | Nexus-Prime reply in plain text, no Markdown (Chat formatting rules apply) |

---

## 12. Future: Text-to-Speech Reply

The current design returns text. If you want GAOS to speak the reply aloud:

- **iOS:** Add a **Speak Text** action at the end of the Shortcut using the `reply`
  value. Siri's voice reads the response. No server-side changes needed.
- **Android:** Tasker's **Say** action reads `%reply` aloud using the system TTS engine.
- **Server-side TTS (Phase 4+):** The `/speak` endpoint could optionally return an
  audio URL by calling Google Cloud Text-to-Speech API, writing the MP3 to a
  signed GCS URL, and including it in the response. This is an upgrade, not a
  requirement for the initial build.

---

## Implementation Checklist

When the decision is made to build `/speak`, complete these steps in order:

- [ ] Diagnose Google Chat mobile delivery failure (§2) — confirm Chat is definitively broken before proceeding
- [ ] Provision `VOICE_HMAC_SECRET` in Secret Manager (§5)
- [ ] Grant Cloud Run SA `secretmanager.secretAccessor` on `VOICE_HMAC_SECRET`
- [ ] Add `_verify_voice_hmac()` helper to `main.py` alongside `_verify_chat_jwt()`
- [ ] Add `POST /speak` endpoint to `main.py` (§4.1)
- [ ] Update `main.py` module docstring to include `/speak` in the endpoint list
- [ ] Smoke test with PowerShell (§11)
- [ ] Set up iOS Shortcut or Android Tasker profile (§6 or §7)
- [ ] Update this doc: change status from PLANNED to ACTIVE
