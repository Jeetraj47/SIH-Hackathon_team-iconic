/* ============================================================================
   NE-AURA (SIH 26002) - LIVE FLEET & ULIP PAGE SCRIPT
   Page-specific logic for Live_fleet.html (GPS Fleet table & ULIP Sandbox lookup)
   ============================================================================ */

function lookupUlipRecord(regNum) {
  const record = ULIP_DATABASE[regNum] || ULIP_DATABASE['AS-01-HC-9920'];
  const display = document.getElementById('ulip-record-display');
  if (!display) return;

  display.innerHTML = `
    <div class="grid-2">
      <div class="glass-card" style="padding: 1rem;">
        <h4 style="color: var(--accent-forest); font-size: 0.85rem; font-weight: 800; margin-bottom: 0.5rem;">🚘 VAHAN 4.0 (Vehicle Registry)</h4>
        <p style="font-size: 0.8rem;"><strong>Owner:</strong> ${record.owner}</p>
        <p style="font-size: 0.8rem;"><strong>Model:</strong> ${record.model}</p>
        <p style="font-size: 0.8rem;"><strong>Gross Weight:</strong> ${record.weight}</p>
        <p style="font-size: 0.8rem;"><strong>Permit:</strong> ${record.permit}</p>
      </div>
      <div class="glass-card" style="padding: 1rem;">
        <h4 style="color: var(--accent-forest); font-size: 0.85rem; font-weight: 800; margin-bottom: 0.5rem;">🪪 SARATHI (Driver License Verification)</h4>
        <p style="font-size: 0.8rem;"><strong>Driver:</strong> ${record.driver}</p>
        <p style="font-size: 0.8rem;"><strong>License #:</strong> ${record.license}</p>
        <p style="font-size: 0.8rem;"><strong>Hill Endorsed:</strong> ${record.hillCertified}</p>
        <p style="font-size: 0.8rem;"><strong>Blood Group:</strong> ${record.bloodGroup}</p>
      </div>
      <div class="glass-card" style="padding: 1rem;">
        <h4 style="color: var(--accent-forest); font-size: 0.85rem; font-weight: 800; margin-bottom: 0.5rem;">💳 FASTag (Toll Gate Telemetry)</h4>
        <p style="font-size: 0.8rem;"><strong>Last Toll:</strong> ${record.fastagToll}</p>
        <p style="font-size: 0.8rem;"><strong>Passage Time:</strong> ${record.fastagTime}</p>
        <p style="font-size: 0.8rem;"><strong>RFID Tag:</strong> ${record.fastagStatus}</p>
        <p style="font-size: 0.8rem;"><strong>Tag Balance:</strong> ${record.balance}</p>
      </div>
      <div class="glass-card" style="padding: 1rem;">
        <h4 style="color: var(--accent-forest); font-size: 0.85rem; font-weight: 800; margin-bottom: 0.5rem;">🚆 FOIS (Rail Rake Transit Sync)</h4>
        <p style="font-size: 0.8rem;"><strong>Rake ID:</strong> ${record.foisRake}</p>
        <p style="font-size: 0.8rem;"><strong>Freight Status:</strong> ${record.foisStatus}</p>
      </div>
    </div>
  `;
}

function renderFleetTable(dataToRender) {
  const tbody = document.getElementById('fleet-table-body');
  if (!tbody) return;

  const dataset = dataToRender || fleet;
  tbody.innerHTML = dataset.map(f => `
    <tr>
      <td class="mono-font" style="font-weight: 700;">${f.regNumber}</td>
      <td>${f.cargoCategory}</td>
      <td>${f.driverName}</td>
      <td><span class="speed-gauge-pill">${f.speedKmH} km/h</span></td>
      <td><span class="badge ${f.status === 'In Transit' ? 'badge-success' : f.status === 'Rerouted by AI' ? 'badge-info' : 'badge-warning'}">${f.status}</span></td>
      <td>
        <button class="btn btn-outline btn-sm" onclick="lookupUlipRecord('${f.regNumber}'); openUlipDetailModal('${f.regNumber}');">Verify ULIP Record</button>
      </td>
    </tr>
  `).join('');
}

function filterFleetTable(category, event) {
  if (event) {
    document.querySelectorAll('.fleet-filter-chip').forEach(c => c.classList.remove('active'));
    event.target.classList.add('active');
  }

  if (category === 'all') {
    renderFleetTable(fleet);
  } else if (category === 'rerouted') {
    renderFleetTable(fleet.filter(f => f.status === 'Rerouted by AI'));
  } else if (category === 'cryo') {
    renderFleetTable(fleet.filter(f => f.cargoCategory.toLowerCase().includes('vaccine') || f.cargoCategory.toLowerCase().includes('oxygen') || f.cargoCategory.toLowerCase().includes('spoilage')));
  } else if (category === 'heavy') {
    renderFleetTable(fleet.filter(f => f.regNumber.includes('HC') || f.regNumber.includes('GC')));
  } else if (category === 'barge') {
    renderFleetTable(fleet.filter(f => f.status.includes('Waterway') || f.status.includes('Rerouted')));
  }
}

function searchFleetTable() {
  const query = document.getElementById('fleetSearchInput')?.value?.toLowerCase() || '';
  const filtered = fleet.filter(f => f.regNumber.toLowerCase().includes(query) || f.driverName.toLowerCase().includes(query) || f.cargoCategory.toLowerCase().includes(query));
  renderFleetTable(filtered);
}

function openUlipDetailModal(regNum) {
  const modal = document.getElementById('ulip-detail-modal');
  const body = document.getElementById('ulipModalBody');
  const record = ULIP_DATABASE[regNum] || ULIP_DATABASE['AS-01-HC-9920'];

  if (modal && body) {
    body.innerHTML = `
      <div style="background: var(--bg-card-subtle); padding: 1rem; border-radius: var(--radius-md); margin-bottom: 1rem;">
        <div style="font-size: 1.1rem; font-weight: 800; color: var(--accent-forest);">Vehicle Registration: ${regNum}</div>
        <div>Owner: <strong>${record.owner}</strong> | Model: <strong>${record.model}</strong></div>
        <div>Permit: <span class="badge badge-success">${record.permit}</span></div>
      </div>
      <div class="grid-2">
        <div>
          <strong>Driver Verification (SARATHI):</strong>
          <div>Driver: ${record.driver}</div>
          <div>License: ${record.license}</div>
          <div>Hill Endorsement: <span class="badge badge-info">${record.hillCertified}</span></div>
        </div>
        <div>
          <strong>FASTag RFID Telemetry:</strong>
          <div>Toll Gate: ${record.fastagToll}</div>
          <div>Pass Time: ${record.fastagTime}</div>
          <div>Tag Balance: ₹${record.balance}</div>
        </div>
      </div>
    `;
    modal.classList.add('active');
  }
}

function downloadFleetAuditCSV() {
  const headers = ['Reg Number', 'Cargo Category', 'Driver', 'Speed KmH', 'Status'];
  const rows = fleet.map(f => [f.regNumber, f.cargoCategory, f.driverName, f.speedKmH, f.status]);
  const csvContent = 'data:text/csv;charset=utf-8,' + [headers.join(','), ...rows.map(e => e.join(','))].join('\n');

  const encodedUri = encodeURI(csvContent);
  const link = document.createElement('a');
  link.setAttribute('href', encodedUri);
  link.setAttribute('download', 'NE-AURA_ULIP_Live_Fleet_Audit_Report.csv');
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  showToast('📥 Downloading Live Fleet ULIP Telematics Audit Report (CSV)...');
}

document.addEventListener('DOMContentLoaded', () => {
  renderFleetTable();
  lookupUlipRecord('AS-01-HC-9920');
});


