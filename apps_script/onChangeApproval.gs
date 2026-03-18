// apps_script/onChangeApproval.gs — RBAC approval gate
// Fires on every Sheet edit via the onChange trigger.
// When Status column (col I) on Agent_Approvals changes to Approved/Rejected,
// validates the approver's identity and tier before allowing the change to stand.

function onChangeApproval(e) {
  // Guard: e or e.range may be undefined in edge cases (e.g. trigger misconfiguration).
  if (!e || !e.range) return;

  const range = e.range;
  const sheet = range.getSheet();               // reliable for onEdit events
  if (sheet.getName() !== 'Agent_Approvals') return;

  const col = range.getColumn();
  const STATUS_COL = 9; // Column I
  if (col !== STATUS_COL) return;

  const newStatus = range.getValue();
  if (newStatus !== 'Approved' && newStatus !== 'Rejected') return;

  const row = range.getRow();
  const proposalId = sheet.getRange(row, 1).getValue();
  const priority = getPriorityFromProposal_(sheet, row);
  // getActiveUser() returns '' for installable triggers in some Workspace configs;
  // fall back to getEffectiveUser() (the account that authorised the trigger).
  const approverEmail = Session.getActiveUser().getEmail()
                     || Session.getEffectiveUser().getEmail();

  // Look up approver in Authorized Approvers tab
  const approver = getApprover_(approverEmail);

  if (!approver) {
    revertAndAlert_(sheet, row, proposalId, approverEmail,
                    'NOT_IN_APPROVERS_LIST');
    return;
  }
  if (!approver.active) {
    revertAndAlert_(sheet, row, proposalId, approverEmail,
                    'APPROVER_INACTIVE');
    return;
  }
  if (approver.tier < priority) {
    revertAndAlert_(sheet, row, proposalId, approverEmail,
                    'TIER_INSUFFICIENT: tier=' + approver.tier + ' priority=' + priority);
    return;
  }

  // Authorised — stamp approver identity onto the row
  sheet.getRange(row, 11).setValue(approverEmail);    // Col K: Approved By
  sheet.getRange(row, 12).setValue(approver.tier);    // Col L: Approver Tier
  logApprovalEvent_(proposalId, approverEmail, approver.tier, newStatus);
}

function getApprover_(email) {
  const ss = getSpreadsheet_();
  const tab = ss.getSheetByName('Authorized Approvers');
  if (!tab) return null;
  const data = tab.getDataRange().getValues();
  for (let i = 1; i < data.length; i++) { // skip header row
    if (data[i][0] === email) {
      return { email: data[i][0], tier: data[i][2], active: data[i][3] };
    }
  }
  return null;
}

function revertAndAlert_(sheet, row, proposalId, email, reason) {
  sheet.getRange(row, 9).setValue('Pending'); // revert Status to Pending
  logSecurityEvent_('APPROVAL_RBAC_BLOCK',
    proposalId + ' | ' + email + ' | ' + reason);
  publishAlert_(proposalId, email, reason);
}

function logApprovalEvent_(proposalId, email, tier, status) {
  const ss = getSpreadsheet_();
  const log = ss.getSheetByName('Logs');
  log.appendRow([new Date(), 'APPROVAL', proposalId, email, tier, status]);
}
