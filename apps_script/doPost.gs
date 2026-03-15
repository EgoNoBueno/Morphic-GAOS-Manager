// apps_script/doPost.gs — HMAC-secured webhook entry point
// Receives incoming proposals from the Cloud Function and appends them
// to the Agent_Approvals sheet after passing three validation layers.
//
// Layer 1: HMAC-SHA256 signature verification (rejects tampered payloads)
// Layer 2: Schema validation (all required fields present, priority 1-5)
// Layer 3: project_id must exist as active in Project Registry tab

function doPost(e) {
  try {
    // Layer 1: HMAC signature check
    const secret = PropertiesService.getScriptProperties()
                     .getProperty('WEBHOOK_HMAC_SECRET');
    const receivedSig = e.parameter.signature || '';
    const body = e.postData.contents;
    const expectedSig = computeHmacSha256(secret, body);

    if (!secureCompare(receivedSig, expectedSig)) {
      logSecurityEvent_('HMAC_FAILURE', receivedSig);
      return jsonResponse_({ error: 'Unauthorized' }, 401);
    }

    // Layer 2: Schema validation
    const payload = JSON.parse(body);
    const validationError = validatePayload_(payload);
    if (validationError) {
      logSecurityEvent_('SCHEMA_INVALID', validationError);
      return jsonResponse_({ error: validationError }, 400);
    }

    // Layer 3: project_id must exist in Project Registry
    if (!isValidProject_(payload.project_id)) {
      logSecurityEvent_('INVALID_PROJECT', payload.project_id);
      return jsonResponse_({ error: 'Unknown project_id' }, 400);
    }

    // All checks passed — append to sheet
    appendProposal_(payload);
    return jsonResponse_({ status: 'accepted' }, 200);

  } catch (err) {
    logSecurityEvent_('DOPOST_ERROR', err.message);
    return jsonResponse_({ error: 'Internal error' }, 500);
  }
}

function computeHmacSha256(secret, message) {
  const rawKey = Utilities.newBlob(secret).getBytes();
  const rawMsg = Utilities.newBlob(message).getBytes();
  const sig = Utilities.computeHmacSha256Signature(rawMsg, rawKey);
  return Utilities.base64Encode(sig);
}

// Constant-time comparison to prevent timing attacks
function secureCompare(a, b) {
  if (a.length !== b.length) return false;
  let result = 0;
  for (let i = 0; i < a.length; i++) {
    result |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return result === 0;
}

function validatePayload_(p) {
  const required = ['message_id', 'correlation_id', 'project_id',
                    'source_agent', 'message_type', 'priority', 'payload'];
  for (const field of required) {
    if (!p[field]) return 'Missing required field: ' + field;
  }
  if (typeof p.priority !== 'number' || p.priority < 1 || p.priority > 5) {
    return 'priority must be integer 1-5';
  }
  return null; // valid
}
