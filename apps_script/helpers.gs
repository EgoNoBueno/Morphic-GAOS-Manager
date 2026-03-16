// apps_script/helpers.gs — shared utilities used by all other script files
//
// Also contains setup helpers called remotely by scripts/setup_apps_script.py:
//   setupPropertiesFromApi_(props)  — sets Script Properties from a key/value object
//   setupTrigger_()                 — installs the onChange trigger programmatically
// All functions in this file are available to every other .gs file in the project.

// Spreadsheet ID — used by getSpreadsheet_() so doPost (web-app context)
// can access the sheet; SpreadsheetApp.getActiveSpreadsheet() returns null
// for standalone scripts called outside an interactive session.
const SPREADSHEET_ID_ = '1O0GA48SIJtyKPOZku8sV9li71p1KRgbJoTyhfXoooH4';

/** Returns the control-plane spreadsheet, safe to call from any context. */
function getSpreadsheet_() {
  return SpreadsheetApp.openById(SPREADSHEET_ID_);
}

/**
 * Returns a JSON ContentService response.
 * Note: Apps Script Web Apps always return HTTP 200 to the caller;
 * statusCode is included in the body for the client to inspect.
 */
function jsonResponse_(obj, statusCode) {
  const body = Object.assign({ statusCode: statusCode }, obj);
  return ContentService
    .createTextOutput(JSON.stringify(body))
    .setMimeType(ContentService.MimeType.JSON);
}

/**
 * Appends a validated proposal payload to the Agent_Approvals sheet.
 *
 * Column layout (matches setup_workspace.py HEADERS + col N added here):
 *   A=ID, B=Agent ID, C=Issue, D=Trigger Reason, E=Stopping Constraint,
 *   F=Iterations Run, G=Total Cost USD, H=Proposed Code, I=Status,
 *   J=Timestamp, K=Approved By, L=Approver Tier, M=code_sha256, N=Priority
 */
function appendProposal_(payload) {
  const ss = getSpreadsheet_();
  const sheet = ss.getSheetByName('Agent_Approvals');
  const inner = payload.payload || {};
  sheet.appendRow([
    payload.message_id,                         // A: ID
    payload.source_agent,                        // B: Agent ID
    inner.issue || payload.correlation_id || '', // C: Issue
    inner.trigger_reason || payload.message_type || '', // D: Trigger Reason
    inner.stopping_constraint || '',             // E: Stopping Constraint
    inner.total_iterations || '',                // F: Iterations Run
    inner.cost_usd || '',                        // G: Total Cost USD
    inner.proposed_code || '',                   // H: Proposed Code
    'Pending',                                   // I: Status
    new Date(),                                  // J: Timestamp
    '',                                          // K: Approved By
    '',                                          // L: Approver Tier
    inner.code_sha256 || '',                     // M: code_sha256
    payload.priority,                            // N: Priority
  ]);
}

/**
 * Returns true if project_id matches an active row in the Project Registry tab.
 */
function isValidProject_(projectId) {
  const ss = getSpreadsheet_();
  const tab = ss.getSheetByName('Project Registry');
  if (!tab) return false;
  const data = tab.getDataRange().getValues();
  for (let i = 1; i < data.length; i++) {
    // col A = project_id (0), col C = status (2)
    if (String(data[i][0]) === String(projectId) && data[i][2] === 'active') {
      return true;
    }
  }
  return false;
}

/**
 * Returns the priority (integer 1–5) for the proposal in the given row.
 * Priority is stored in column N (index 14, 1-based). Defaults to 3.
 */
function getPriorityFromProposal_(sheet, row) {
  const val = sheet.getRange(row, 14).getValue();
  const parsed = parseInt(val);
  return (isNaN(parsed) || parsed < 1 || parsed > 5) ? 3 : parsed;
}

/**
 * Logs a security event row to the Logs tab.
 * Signature matches the inline definition in the spec so both files work.
 */
function logSecurityEvent_(type, detail) {
  const ss = getSpreadsheet_();
  const log = ss.getSheetByName('Logs') || ss.insertSheet('Logs');
  log.appendRow([new Date(), 'SECURITY', type, String(detail)]);
}

/**
 * Publishes a security alert to agent.approvals.events via Pub/Sub REST.
 * Falls back to logging only if the OAuth token lacks pubsub scope.
 */
function publishAlert_(proposalId, email, reason) {
  logSecurityEvent_('APPROVAL_ALERT',
    'proposal=' + proposalId + ' approver=' + email + ' reason=' + reason);
  try {
    const token = ScriptApp.getOAuthToken();
    const project = 'morphic-gaos-prod';
    const topic = 'agent.approvals.events';
    const message = {
      proposalId: proposalId,
      email: email,
      reason: reason,
      timestamp: new Date().toISOString(),
    };
    const body = {
      messages: [{
        data: Utilities.base64Encode(JSON.stringify(message)),
        attributes: { event_type: 'APPROVAL_RBAC_BLOCK' },
      }],
    };
    UrlFetchApp.fetch(
      'https://pubsub.googleapis.com/v1/projects/' + project + '/topics/' + topic + ':publish',
      {
        method: 'POST',
        headers: {
          'Authorization': 'Bearer ' + token,
          'Content-Type': 'application/json',
        },
        payload: JSON.stringify(body),
        muteHttpExceptions: true,
      }
    );
  } catch (err) {
    logSecurityEvent_('PUBLISH_ALERT_ERROR', err.message);
  }
}

/**
 * Publishes a critical security alert (e.g. code tamper detected) to
 * agent.approvals.events. Used by syncSkillsToVertex when Gate 1 fails.
 */
function publishCriticalAlert_(proposalId, priority, alertType, message) {
  logSecurityEvent_(alertType,
    'proposal=' + proposalId + ' priority=' + priority + ' ' + message);
  try {
    const token = ScriptApp.getOAuthToken();
    const project = 'morphic-gaos-prod';
    const topic = 'agent.approvals.events';
    const body = {
      messages: [{
        data: Utilities.base64Encode(JSON.stringify({
          proposalId: proposalId,
          priority: priority,
          alertType: alertType,
          message: message,
          timestamp: new Date().toISOString(),
        })),
        attributes: { event_type: alertType, priority: String(priority) },
      }],
    };
    UrlFetchApp.fetch(
      'https://pubsub.googleapis.com/v1/projects/' + project + '/topics/' + topic + ':publish',
      {
        method: 'POST',
        headers: {
          'Authorization': 'Bearer ' + token,
          'Content-Type': 'application/json',
        },
        payload: JSON.stringify(body),
        muteHttpExceptions: true,
      }
    );
  } catch (err) {
    logSecurityEvent_('PUBLISH_CRITICAL_ALERT_ERROR', err.message);
  }
}

// ── Remote setup helpers (called by scripts/setup_apps_script.py) ─────────────

/**
 * Sets Script Properties from a plain key/value object.
 * Called remotely via the Apps Script API during automated setup.
 * Empty string values are stored as-is (placeholder for later).
 */
function setupPropertiesFromApi_(props) {
  const sp = PropertiesService.getScriptProperties();
  for (const key in props) {
    if (Object.prototype.hasOwnProperty.call(props, key)) {
      sp.setProperty(key, String(props[key]));
    }
  }
  Logger.log('Script Properties set: ' + Object.keys(props).join(', '));
}

/**
 * Installs the onChangeApproval trigger programmatically.
 * Idempotent — skips installation if an onChange trigger already exists.
 * Called remotely via the Apps Script API during automated setup.
 */
function setupTrigger_() {
  const ss = getSpreadsheet_();
  const triggers = ScriptApp.getUserTriggers(ss);
  for (const t of triggers) {
    if (t.getHandlerFunction() === 'onChangeApproval' &&
        t.getEventType() === ScriptApp.EventType.ON_CHANGE) {
      Logger.log('onChange trigger already installed — skipping');
      return;
    }
  }
  ScriptApp.newTrigger('onChangeApproval')
    .forSpreadsheet(ss)
    .onChange()
    .create();
  Logger.log('onChange trigger installed for onChangeApproval');
}
