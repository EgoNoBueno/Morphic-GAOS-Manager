// apps_script/setup_protection.gs — run ONCE from the Script Editor after setup
// Locks the Status, Proposed Code, and Code SHA-256 columns on Agent_Approvals,
// and locks the entire Authorized Approvers tab to owner-only edit.
//
// HOW TO RUN:
//   In the Apps Script editor, select "setupProtections" from the function
//   dropdown and click Run. You will be prompted to authorise permissions.
//   Run this exactly once after the spreadsheet is created.

function setupProtections() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const me = Session.getEffectiveUser();

  // Lock Status column (col I) on Agent_Approvals tab
  const approvals = ss.getSheetByName('Agent_Approvals');
  const statusCol = approvals.getRange('I:I');
  const p1 = statusCol.protect().setDescription('Status — owner only');
  p1.removeEditors(p1.getEditors());
  p1.addEditor(me);
  p1.setWarningOnly(false);

  // Lock entire Authorized Approvers tab
  const approversTab = ss.getSheetByName('Authorized Approvers');
  const p2 = approversTab.protect().setDescription('Approvers list — owner only');
  p2.removeEditors(p2.getEditors());
  p2.addEditor(me);
  p2.setWarningOnly(false);

  // Lock Proposed Code column (col H) — immutable after submission.
  // Post-submission edits are caught by the hash check in syncSkillsToVertex,
  // but locking prevents low-effort tampering.
  const codeCol = approvals.getRange('H:H');
  const p3 = codeCol.protect().setDescription('Proposed Code — immutable after submission');
  p3.removeEditors(p3.getEditors());
  p3.addEditor(me);
  p3.setWarningOnly(false);

  // Lock Code Hash column (col M) — tamper-evident seal.
  // If col M is editable, an attacker who edits col H could also update
  // the hash to match. Owner-only prevents this for non-owner actors.
  const hashCol = approvals.getRange('M:M');
  const p4 = hashCol.protect().setDescription('Code SHA-256 — owner only');
  p4.removeEditors(p4.getEditors());
  p4.addEditor(me);
  p4.setWarningOnly(false);

  Logger.log('Protections applied: I (Status), Authorized Approvers tab, H (Code), M (Hash).');
}
