// apps_script/syncSkillsToVertex.gs — three-gate code deployment pipeline
// Triggered via custom menu: 🤖 Agent OS > Sync Approved Skills
// A row is only sent to Vertex AI if all three gates pass.
// Gate 1: Code integrity — SHA-256 of col H must match col M (set at submission)
// Gate 2: Static analysis — no dangerous patterns, no unapproved imports
// Gate 3: Deploy to Vertex AI Agent Engine via REST

function syncSkillsToVertex() {
  const ss = getSpreadsheet_();
  const sheet = ss.getSheetByName('Agent_Approvals');
  const data = sheet.getDataRange().getValues();

  // Dangerous Python patterns — any match blocks deployment
  const DANGEROUS = [
    /\bos\.system\b/, /\bsubprocess\b/, /\beval\s*\(/,
    /\bexec\s*\(/, /\b__import__\s*\(/, /\bcompile\s*\(/,
    /\bpickle\b/, /\bctypes\b/, /\bimportlib\b/, /\bsocket\b/,
  ];

  // Only these top-level module names may appear in import statements
  const ALLOWED_IMPORTS = [
    'google', 'vertexai', 'langchain', 'pydantic', 'datetime',
    'json', 're', 'math', 'typing', 'collections', 'itertools',
    'functools', 'logging', 'gspread',
  ];

  for (let i = 1; i < data.length; i++) {
    // Columns (0-based): A=0, B=1, H=7, I=8, M=12, N=13
    const id       = data[i][0];
    const code     = data[i][7];  // col H: Proposed Code
    const status   = data[i][8];  // col I: Status
    const codeHash = data[i][12]; // col M: code_sha256
    const priority = data[i][13]; // col N: Priority

    if (status !== 'Approved') continue;

    const codeStr = String(code);

    // ── Gate 1: Code integrity ─────────────────────────────────────
    // Recompute SHA-256 of col H and compare to the hash stored in
    // col M at submission time. Any edit after proposal submission
    // (by anyone, including the owner) will mismatch here.
    const recomputed = computeSha256_(codeStr);
    if (recomputed !== String(codeHash).trim()) {
      logSecurityEvent_('CODE_HASH_MISMATCH',
        'Proposal ' + id + ': col H was edited after submission — deployment blocked');
      publishCriticalAlert_(id, priority || 5, 'CODE_INJECTION_ATTEMPT',
        'Code tampered after approval submission');
      sheet.getRange(i + 1, 9).setValue('BLOCKED_TAMPERED');
      continue;
    }

    // ── Gate 2: Static analysis ────────────────────────────────────
    // Scan for dangerous built-ins and unapproved imports.
    const violation = staticAnalysis_(codeStr, DANGEROUS, ALLOWED_IMPORTS);
    if (violation) {
      logSecurityEvent_('STATIC_ANALYSIS_BLOCK',
        'Proposal ' + id + ': ' + violation);
      sheet.getRange(i + 1, 9).setValue('BLOCKED_STATIC');
      continue;
    }

    // ── Gate 3: Deploy to Vertex AI Agent Engine ───────────────────
    try {
      const token = ScriptApp.getOAuthToken();
      const endpoint = PropertiesService.getScriptProperties()
                         .getProperty('VERTEX_AGENT_ENDPOINT');
      if (!endpoint) {
        logSecurityEvent_('DEPLOY_SKIPPED',
          'Proposal ' + id + ': VERTEX_AGENT_ENDPOINT not configured');
        continue;
      }
      const resp = UrlFetchApp.fetch(endpoint, {
        method: 'POST',
        headers: {
          'Authorization': 'Bearer ' + token,
          'Content-Type': 'application/json',
        },
        payload: JSON.stringify({ proposal_id: id, code: codeStr }),
        muteHttpExceptions: true,
      });
      if (resp.getResponseCode() === 200) {
        sheet.getRange(i + 1, 9).setValue('DEPLOYED');
      } else {
        logSecurityEvent_('DEPLOY_ERROR',
          'Proposal ' + id + ': HTTP ' + resp.getResponseCode());
      }
    } catch (err) {
      logSecurityEvent_('DEPLOY_ERROR', 'Proposal ' + id + ': ' + err.message);
    }
  }
}

// SHA-256 of a string using Apps Script built-ins (no external libraries)
function computeSha256_(text) {
  const bytes = Utilities.newBlob(text).getBytes();
  const hash  = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, bytes);
  return hash.map(function(b) {
    return ('0' + (b & 0xff).toString(16)).slice(-2);
  }).join('');
}

// Returns a violation description string or null if the code is clean
function staticAnalysis_(code, patterns, allowedImports) {
  for (const re of patterns) {
    if (re.test(code)) return 'Blocked pattern: ' + re.source;
  }
  const importRe = /^(?:import|from)\s+([\w]+)/gm;
  let m;
  while ((m = importRe.exec(code)) !== null) {
    if (!allowedImports.includes(m[1])) return 'Unapproved import: ' + m[1];
  }
  return null;
}

// Note: The URLs mentioned in this section are dynamically generated based on the specific Google Cloud project and deployment configuration. They will differ for each new deployment.

// Register the custom menu so users can trigger sync from the Sheet UI
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('🤖 Agent OS')
    .addItem('Sync Approved Skills', 'syncSkillsToVertex')
    .addToUi();
}
