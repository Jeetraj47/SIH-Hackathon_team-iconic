/* ============================================================================
   NE-AURA (SIH 26002) - RESOURCES & OPEN DATA PAGE SCRIPT
   Page-specific logic for resource.html (Dataset downloads & API specifications)
   ============================================================================ */

const API_PAYLOAD_SAMPLES = {
  'stgcn-risk': {
    "status": 200,
    "endpoint": "/api/v1/routes/stgcn-risk",
    "corridor": "NH-27 Sonapur Pass",
    "stgcn_risk_score": 0.84,
    "disruption_category": "Landslide / Mudslide",
    "recommended_bypass": "NW-2 Waterway Barge via Pandu Terminal",
    "est_clearance_hrs": 4.5
  },
  'ulip-vahan': {
    "status": 200,
    "endpoint": "/api/v1/ulip/vahan-record",
    "registration_no": "AS-01-GC-9012",
    "owner_name": "Northeast Intermodal Logistics Pvt Ltd",
    "vehicle_class": "Heavy Goods Truck (6-Axle Reefer)",
    "fastag_status": "Active (Sonapur Toll Pass)",
    "fitness_valid_until": "2028-11-15"
  },
  'crdt-mesh': {
    "status": 200,
    "endpoint": "/api/v1/crdt/mesh-state",
    "node_id": "CRDT-OFFICER-8849",
    "peer_count": 12,
    "state_vector": "0x8f3a921b44c0",
    "pending_offline_payloads": 0,
    "sync_latency_ms": 12
  }
};

function filterDatasets(category, btnEl) {
  const cards = document.querySelectorAll('#dataset-cards-grid .resource-item-card');
  const chipBtns = document.querySelectorAll('.filter-chip-btn');

  chipBtns.forEach(btn => btn.classList.remove('active'));
  if (btnEl) btnEl.classList.add('active');

  cards.forEach(card => {
    if (category === 'all' || card.getAttribute('data-cat') === category) {
      card.style.display = 'flex';
    } else {
      card.style.display = 'none';
    }
  });
}

function loadApiPlaygroundPreview() {
  const select = document.getElementById('api-endpoint-select');
  const codeBox = document.getElementById('api-response-code');
  if (!select || !codeBox) return;

  const key = select.value;
  const sample = API_PAYLOAD_SAMPLES[key] || API_PAYLOAD_SAMPLES['stgcn-risk'];
  codeBox.textContent = JSON.stringify(sample, null, 2);
}

function executeApiPlaygroundCall() {
  showToast('🚀 Executing API Endpoint Query... (200 OK - 14ms)');
  loadApiPlaygroundPreview();
}

function copyCurlCommand() {
  const select = document.getElementById('api-endpoint-select');
  const endpointUrl = select ? select.options[select.selectedIndex].text.split(' ')[1] : '/api/v1/routes/stgcn-risk';
  const curl = `curl -X GET "https://api.ne-aura.gov.in${endpointUrl}" -H "Authorization: Bearer SHA256-NER-2026-X89"`;

  navigator.clipboard?.writeText(curl);
  showToast('📋 Copied cURL Command to Clipboard!');
}

function filterEmergencyDirectory() {
  const query = (document.getElementById('emergency-search')?.value || '').toLowerCase();
  const rows = document.querySelectorAll('#emergency-table tbody tr');

  rows.forEach(row => {
    const text = row.textContent.toLowerCase();
    row.style.display = text.includes(query) ? '' : 'none';
  });
}

document.addEventListener('DOMContentLoaded', () => {
  loadApiPlaygroundPreview();
});

