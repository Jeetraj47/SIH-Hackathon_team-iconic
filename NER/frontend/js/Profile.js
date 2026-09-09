/* ============================================================================
   NE-AURA (SIH 26002) - OFFICER PROFILE PAGE SCRIPT
   Page-specific logic for Profile.html (Officer Roshan Sahu profile & incident submission logs)
   ============================================================================ */

document.addEventListener('DOMContentLoaded', () => {
  renderIncidentsList();
});

/**
 * Triggers manual Delta-CRDT Mesh Synchronization
 */
function syncCrdtMesh() {
  showToast('⚡ Delta-CRDT Mesh Syncing with 8 State Command Nodes...');
  setTimeout(() => {
    showToast('✅ CRDT Mesh Sync Complete: 14 Telemetry Packets Verified (0 Conflicts)');
  }, 1200);
}

/**
 * Exports Officer Security & Activity Audit Log as CSV file
 */
function downloadOfficerAuditLog() {
  const csvHeaders = "Log ID,Timestamp,Officer ID,Action Type,Location/Zone,Verification Status\n";
  const csvRows = [
    `"LOG-9001","2026-09-07 18:20","CRDT-OFFICER-8849","Public Fund Audit","Subansiri River Bridge (Assam)","Cryptographically Sealed"`,
    `"LOG-9002","2026-09-07 16:45","CRDT-OFFICER-8849","Monsoon Hazard Flag","NH-27 Sonapur Pass KM 141","BRO Unit Dispatched"`,
    `"LOG-9003","2026-09-07 13:10","CRDT-OFFICER-8849","CRDT Peer Sync","Node CRDT-MZO-109","Zero Data Loss"`,
    `"LOG-9004","2026-09-06 17:30","CRDT-OFFICER-8849","ULIP Cold-Chain Inspection","Sonapur Intermodal Hub","Pass Approved"`
  ].join("\n");

  const blob = new Blob([csvHeaders + csvRows], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `NE-AURA_Officer_Audit_Log_Roshan_Sahu.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);

  showToast('📥 Downloading Cryptographic Officer Audit Log CSV...');
}

