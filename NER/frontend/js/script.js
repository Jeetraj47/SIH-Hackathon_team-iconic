/* ============================================================================
   NE-AURA (SIH 26002) - CORE SHARED DATA & UTILITY ENGINE
   Compact Shared State, Global Utility Methods, and Telemetry Engine
   ============================================================================ */

// 1. DATASETS & STATE
const NER_STATES = {
  arunachal: { id: 'arunachal', name: 'Arunachal Pradesh', capital: 'Itanagar', districtsCount: 26, activeProjectsCount: 24, totalSanctionedCr: 1248.5, totalReleasedCr: 980.2, totalUtilizedCr: 795.4, flagColor: '#0284c7', shortCode: 'AR', description: 'Border connectivity, hill-top solar microgrids, cold-chain agri.' },
  assam: { id: 'assam', name: 'Assam', capital: 'Dispur', districtsCount: 35, activeProjectsCount: 48, totalSanctionedCr: 3410.8, totalReleasedCr: 2750.4, totalUtilizedCr: 2210.6, flagColor: '#16a34a', shortCode: 'AS', description: 'Brahmaputra flood mitigation, riverine bridges, rural tap water.' },
  manipur: { id: 'manipur', name: 'Manipur', capital: 'Imphal', districtsCount: 16, activeProjectsCount: 19, totalSanctionedCr: 890.3, totalReleasedCr: 680.5, totalUtilizedCr: 490.2, flagColor: '#ea580c', shortCode: 'MN', description: 'Rural healthcare tele-clinics, tribal horticultural market hubs.' },
  meghalaya: { id: 'meghalaya', name: 'Meghalaya', capital: 'Shillong', districtsCount: 12, activeProjectsCount: 22, totalSanctionedCr: 960.0, totalReleasedCr: 740.3, totalUtilizedCr: 615.8, flagColor: '#0d9488', shortCode: 'ML', description: 'Sustainable eco-tourism corridors, organic spice processing units.' },
  mizoram: { id: 'mizoram', name: 'Mizoram', capital: 'Aizawl', districtsCount: 11, activeProjectsCount: 18, totalSanctionedCr: 745.2, totalReleasedCr: 590.1, totalUtilizedCr: 485.6, flagColor: '#8b5cf6', shortCode: 'MZ', description: 'Border trade routes, slope stabilization, gravity-fed village water.' },
  nagaland: { id: 'nagaland', name: 'Nagaland', capital: 'Kohima', districtsCount: 16, activeProjectsCount: 21, totalSanctionedCr: 820.7, totalReleasedCr: 615.0, totalUtilizedCr: 492.3, flagColor: '#e11d48', shortCode: 'NL', description: 'Organic king chilli value chains, rural tech incubators.' },
  sikkim: { id: 'sikkim', name: 'Sikkim', capital: 'Gangtok', districtsCount: 6, activeProjectsCount: 15, totalSanctionedCr: 630.4, totalReleasedCr: 520.8, totalUtilizedCr: 470.1, flagColor: '#d97706', shortCode: 'SK', description: '100% Organic state logistics, glacial lake flood warning towers.' },
  tripura: { id: 'tripura', name: 'Tripura', capital: 'Agartala', districtsCount: 8, activeProjectsCount: 20, totalSanctionedCr: 785.6, totalReleasedCr: 640.2, totalUtilizedCr: 552.7, flagColor: '#059669', shortCode: 'TR', description: 'Industrial bamboo cluster parks, Indo-Bangla connectivity corridors.' }
};

let projects = [
  {
    id: 'prj-as-01', code: 'NER-NESIDS-AS-104', title: 'Majuli Island Flood-Resilient Elevated Rural Road & Culvert Network',
    description: '32 km climate-resilient elevated road network connecting 42 flood-prone riverine Mishing tribal villages in Majuli river island.',
    state: 'assam', district: 'Majuli', blockOrVillage: 'Garamur & Jengraimukh', sector: 'Connectivity & Roads', scheme: 'NESIDS (Roads)',
    implementingAgency: 'Public Works Roads Department (PWRD), Assam', sanctionedBudgetCr: 142.5, fundsReleasedCr: 114.0, fundsUtilizedCr: 98.4,
    physicalProgressPct: 78, financialProgressPct: 80, sanctionDate: '2023-04-15', targetCompletionDate: '2026-11-30', status: 'On Track',
    contractor: 'Brahmaputra Infrastructure & Marine Works JV', utilizationCertificateSubmitted: true, coordinates: { lat: 26.96, lng: 94.21 }
  },
  {
    id: 'prj-ar-02', code: 'NER-DEVINE-AR-209', title: 'Tawang-Dirang High Altitude Solar Micro-Grid & Cold Storage Facility',
    description: '5 MW decentralized solar micro-grids with battery storage for 850 border farming families.',
    state: 'arunachal', district: 'Tawang', blockOrVillage: 'Kitpi & Jang Circle', sector: 'Clean Energy & Micro-grids', scheme: 'PM-DevINE',
    implementingAgency: 'Arunachal Pradesh Energy Development Agency (APEDA)', sanctionedBudgetCr: 88.0, fundsReleasedCr: 70.4, fundsUtilizedCr: 35.2,
    physicalProgressPct: 42, financialProgressPct: 80, sanctionDate: '2023-08-10', targetCompletionDate: '2026-12-15', status: 'Delayed',
    contractor: 'Himalayan Solar Green Energy Ltd.', utilizationCertificateSubmitted: false, coordinates: { lat: 27.58, lng: 91.86 }
  },
  {
    id: 'prj-ml-03', code: 'NER-JJM-ML-331', title: 'East Khasi Hills Gravity-Flow Piped Drinking Water Project',
    description: 'Providing functional tap connections to 110 perched hill hamlets tapping perennial spring sources.',
    state: 'meghalaya', district: 'East Khasi Hills', blockOrVillage: 'Mawkynrew', sector: 'Drinking Water & Sanitation', scheme: 'Jal Jeevan Mission',
    implementingAgency: 'PHED Meghalaya', sanctionedBudgetCr: 64.2, fundsReleasedCr: 51.3, fundsUtilizedCr: 48.6,
    physicalProgressPct: 91, financialProgressPct: 80, sanctionDate: '2023-01-20', targetCompletionDate: '2026-10-31', status: 'On Track',
    contractor: 'Shillong Hill Hydraulics', utilizationCertificateSubmitted: true, coordinates: { lat: 25.32, lng: 91.88 }
  },
  {
    id: 'prj-tr-04', code: 'NER-BAMBOO-TR-412', title: 'Gomati Modern Bamboo Processing & Board Hub',
    description: 'Common facility center for mechanized slicing and export-grade bamboo mat board manufacturing.',
    state: 'tripura', district: 'Gomati', blockOrVillage: 'Kakraban', sector: 'Agriculture & Bamboo Livelihood', scheme: 'National Bamboo Mission',
    implementingAgency: 'Tripura Bamboo Mission', sanctionedBudgetCr: 45.8, fundsReleasedCr: 36.6, fundsUtilizedCr: 32.8,
    physicalProgressPct: 86, financialProgressPct: 80, sanctionDate: '2023-06-18', targetCompletionDate: '2026-09-30', status: 'On Track',
    contractor: 'Tripura Industrial Infra', utilizationCertificateSubmitted: true, coordinates: { lat: 23.53, lng: 91.48 }
  },
  {
    id: 'prj-nl-05', code: 'NER-DEVINE-NL-508', title: 'Mokokchung Specialty Healthcare & Telemedicine Center',
    description: '60-bed secondary hospital with pediatric ICU, dialysis center, and ISRO satellite telemedicine uplink.',
    state: 'nagaland', district: 'Mokokchung', blockOrVillage: 'Changtongya', sector: 'Healthcare & Telemedicine', scheme: 'PM-DevINE',
    implementingAgency: 'Health & Family Welfare Nagaland', sanctionedBudgetCr: 52.4, fundsReleasedCr: 41.9, fundsUtilizedCr: 21.0,
    physicalProgressPct: 35, financialProgressPct: 80, sanctionDate: '2023-09-05', targetCompletionDate: '2026-10-31', status: 'Critical Attention',
    contractor: 'Naga Hill Engineers', utilizationCertificateSubmitted: false, coordinates: { lat: 26.54, lng: 94.68 }
  },
  {
    id: 'prj-sk-07', code: 'NER-NESIDS-SK-719', title: 'Pakyong Multi-Modal Agro-Logistics Park',
    description: 'Integrated hub with automated ozone cleaning and packaging for organic cardamom & Sikkim oranges.',
    state: 'sikkim', district: 'Pakyong', blockOrVillage: 'Rhenock', sector: 'Agriculture & Bamboo Livelihood', scheme: 'NESIDS (Other)',
    implementingAgency: 'SIMFED Sikkim', sanctionedBudgetCr: 58.5, fundsReleasedCr: 46.8, fundsUtilizedCr: 44.2,
    physicalProgressPct: 95, financialProgressPct: 80, sanctionDate: '2022-11-12', targetCompletionDate: '2026-09-25', status: 'Completed',
    contractor: 'Kanchenjunga Cold Storage', utilizationCertificateSubmitted: true, coordinates: { lat: 27.24, lng: 88.58 }
  }
];

let disruptions = [
  { id: 'dis-101', code: 'DIS-101', title: 'Major Landslide at Sonapur Tunnel Pass', highway: 'NH-27 / NH-6', location: 'Sonapur Corridor, Jaintia Hills', district: 'East Jaintia Hills', state: 'meghalaya', type: 'Landslide', severity: 'Critical', edgeRiskScore: 0.89, status: 'Active', impactDescription: '420m slope failure triggered by heavy rain. Both lanes severed.', coordinates: { lat: 25.138, lng: 92.368 }, affectedVehiclesCount: 142 },
  { id: 'dis-102', code: 'DIS-102', title: 'Monsoon Flash Flood Inundation', highway: 'NH-37', location: 'Kaziranga Belt', district: 'Golaghat', state: 'assam', type: 'Monsoon Flash Flood', severity: 'Critical', edgeRiskScore: 0.82, status: 'Active', impactDescription: 'Brahmaputra overflow submerged 1.2km highway.', coordinates: { lat: 26.584, lng: 93.171 }, affectedVehiclesCount: 88 },
  { id: 'dis-103', code: 'DIS-103', title: 'Teesta River Bridge Scour', highway: 'NH-10', location: 'Coronation Bridge Approach', district: 'Kalimpong', state: 'sikkim', type: 'Bridge Shear Defect', severity: 'Warning', edgeRiskScore: 0.74, status: 'Under Clearance', impactDescription: 'Pier footing scoured. Vehicles over 16T restricted.', coordinates: { lat: 26.883, lng: 88.472 }, affectedVehiclesCount: 46 }
];

let fleet = [
  { id: 'flt-01', code: 'TRK-NER-901', regNumber: 'AS-01-HC-9920', cargoCategory: 'Essential Vaccines & Medicines', origin: 'Guwahati Hub', destination: 'Silchar Medical College', driverName: 'Biren Gogoi', driverPhone: '+91 94350 12891', coordinates: { lat: 25.82, lng: 92.15 }, destinationCoordinates: { lat: 24.833, lng: 92.778 }, speedKmH: 42, status: 'Rerouted by AI', etaHours: 5.4, temperatureC: 4.2, fuelPct: 78 },
  { id: 'flt-02', code: 'TRK-NER-902', regNumber: 'ML-05-AB-1234', cargoCategory: 'GI Lakadong Turmeric', origin: 'Jowai Agricultural Co-op', destination: 'Guwahati Airport', driverName: 'Kyrshan Kharbuli', driverPhone: '+91 98620 44321', coordinates: { lat: 25.45, lng: 92.18 }, destinationCoordinates: { lat: 26.144, lng: 91.736 }, speedKmH: 35, status: 'Delayed by Landslide', etaHours: 9.2, temperatureC: 28.5, fuelPct: 62 },
  { id: 'flt-03', code: 'TRK-NER-903', regNumber: 'MN-01-C-8877', cargoCategory: 'Medical Oxygen', origin: 'Tezpur Airbase Depot', destination: 'JNIMS Hospital Imphal', driverName: 'Thoiba Singh', driverPhone: '+91 89740 55102', coordinates: { lat: 26.12, lng: 93.45 }, destinationCoordinates: { lat: 24.817, lng: 93.936 }, speedKmH: 52, status: 'In Transit', etaHours: 6.8, temperatureC: -182.0, fuelPct: 85 },
  { id: 'flt-04', code: 'TRK-NER-904', regNumber: 'TR-01-AX-4500', cargoCategory: 'PDS Rice/Grains', origin: 'FCI Depot Jogighopa', destination: 'Agartala Central Godown', driverName: 'Subhas Debbarma', driverPhone: '+91 94361 77892', coordinates: { lat: 24.15, lng: 91.95 }, destinationCoordinates: { lat: 23.831, lng: 91.286 }, speedKmH: 48, status: 'In Transit', etaHours: 7.5, temperatureC: 24.0, fuelPct: 54 }
];

let incidentReports = [
  { id: 'INC-2026-0881', category: 'Landslide', highway: 'NH-27 Sonapur Pass', district: 'East Jaintia Hills', state: 'meghalaya', severity: 'Critical', fieldNotes: 'Heavy rock mudslide blocking both carriage directions. BRO excavator on site.', timestamp: '2026-09-07 08:30 IST', status: 'Synced with GIS Server', reportedBy: 'Officer Roshan Sahu' },
  { id: 'INC-2026-0882', category: 'Bridge Degradation', highway: 'NH-10 Teesta River', district: 'Kalimpong', state: 'sikkim', severity: 'Warning', fieldNotes: 'Scour detected around bridge pier #3. 16-ton limit active.', timestamp: '2026-09-07 07:15 IST', status: 'Synced with GIS Server', reportedBy: 'Tenzing Lepcha' }
];

const ULIP_DATABASE = {
  'AS-01-HC-9920': {
    owner: 'Assam State Cold-Chain Logistics Ltd.', model: 'Tata Prima 2830.K Cryo-Insulated', weight: '28.0 Tons', permit: 'All-India National Goods (Hill Endorsed)', fitness: '2028-04-15', BS: 'BS-VI OBD-II Stage 2',
    driver: 'Biren Gogoi', license: 'AS-01-2015-0049281', hillCertified: 'YES', bloodGroup: 'B+ Positive',
    fastagToll: 'Nazirakhat Toll Plaza (NH-27)', fastagTime: '2026-09-07 04:42 IST', fastagStatus: 'ACTIVE & VERIFIED', balance: '₹4,850.00',
    foisRake: 'RAKE-BG-8839-GHY (Guwahati Yard)', foisStatus: 'Scheduled Intermodal Ro-Ro'
  },
  'ML-05-AB-1234': {
    owner: 'Jaintia Organic Spice Farmers Producer Co.', model: 'Ashok Leyland Ecomet 1215', weight: '12.0 Tons', permit: 'Meghalaya State Carrier Permit', fitness: '2027-09-30', BS: 'BS-VI',
    driver: 'Kyrshan Kharbuli', license: 'ML-05-2018-0091823', hillCertified: 'YES', bloodGroup: 'O+ Positive',
    fastagToll: 'Umiam Toll Gate, Shillong Bypass', fastagTime: '2026-09-07 03:15 IST', fastagStatus: 'ACTIVE & VERIFIED', balance: '₹1,920.00',
    foisRake: 'RAKE-BG-1092-JOW (Dhubri River Port)', foisStatus: 'Standby for River Barge Swap'
  }
};

const ARRHENIUS_COMMODITIES = [
  { name: 'GI Lakadong Turmeric (Curcumin 7.5%)', Ea: 68.4, baseHours: 168, refTemp: 18.0, storage: 'Dark, low-humidity solar cold store (12°C - 18°C)' },
  { name: 'GI Tripura Queen Pineapple', Ea: 74.2, baseHours: 96, refTemp: 12.0, storage: 'Controlled Atmosphere Reefer (8°C - 12°C)' },
  { name: 'GI Naga King Chilli (Bhut Jolokia)', Ea: 62.1, baseHours: 120, refTemp: 15.0, storage: 'Ventilated Solar Chamber with RH 85% (12°C - 16°C)' },
  { name: 'GI Khasi Mandarin Orange', Ea: 70.8, baseHours: 144, refTemp: 10.0, storage: 'Cold Vault (5°C - 9°C, 90% RH)' }
];

let isLandslideSimulatedActive = true;
let isDarkMode = false;
let currentLanguage = 'en';
let gisMap = null;
let nerMap = null;

// 2. CORE TELEMETRY INITIALIZER
document.addEventListener('DOMContentLoaded', () => {
  initGpsTimer();
});

// 3. LANDSLIDE SIMULATION EMERGENCY TOGGLE
function toggleLandslideSimulation() {
  isLandslideSimulatedActive = !isLandslideSimulatedActive;

  disruptions = disruptions.map(d => {
    if (d.id === 'dis-101') {
      return {
        ...d,
        status: isLandslideSimulatedActive ? 'Active' : 'Under Clearance',
        severity: isLandslideSimulatedActive ? 'Critical' : 'Warning',
        edgeRiskScore: isLandslideSimulatedActive ? 0.89 : 0.45,
        impactDescription: isLandslideSimulatedActive
          ? 'EMERGENCY: 420m slope failure triggered. Dual carriage lanes severed. ST-GCN multi-modal bypass engaged.'
          : 'BRO clearance dozers operational. Single-lane convoy pilot active.'
      };
    }
    return d;
  });

  fleet = fleet.map(v => {
    if (v.regNumber === 'AS-01-HC-9920') {
      return {
        ...v,
        status: isLandslideSimulatedActive ? 'Rerouted by AI' : 'In Transit',
        speedKmH: isLandslideSimulatedActive ? 42 : 55
      };
    }
    return v;
  });

  const swapBanner = document.getElementById('multiModalSwapBanner');
  if (swapBanner) {
    if (isLandslideSimulatedActive) {
      swapBanner.style.display = 'block';
      swapBanner.innerHTML = `
        <div class="glass-card" style="border: 2px solid var(--accent-terracotta); background: linear-gradient(135deg, rgba(226, 114, 91, 0.08), rgba(45, 70, 40, 0.08)); padding: 1.25rem;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.75rem;">
            <h3 style="font-size: 1.05rem; font-weight: 800; color: var(--accent-terracotta);">🚨 EMERGENCY MULTI-MODAL SWAP ENGAGED (NH-27 Landslide Breach)</h3>
            <span class="badge badge-danger">ST-GCN RADR Automated Reroute</span>
          </div>
          <div class="grid-3" style="font-size: 0.82rem;">
            <div style="background: var(--bg-card); padding: 0.85rem; border-radius: var(--radius-md); border-left: 4px solid var(--accent-sky);">
              <strong>Stage 1: NW-2 River Barge</strong>
              <p style="color: var(--text-muted); font-size: 0.76rem; margin-top: 0.2rem;">Pandu IWT Waterway Terminal → Dhubri Port</p>
            </div>
            <div style="background: var(--bg-card); padding: 0.85rem; border-radius: var(--radius-md); border-left: 4px solid var(--accent-violet);">
              <strong>Stage 2: FOIS Rail Siding</strong>
              <p style="color: var(--text-muted); font-size: 0.76rem; margin-top: 0.2rem;">Jogighopa MMLP Rake #883 (Broad Gauge)</p>
            </div>
            <div style="background: var(--bg-card); padding: 0.85rem; border-radius: var(--radius-md); border-left: 4px solid var(--accent-emerald);">
              <strong>Stage 3: Last-Mile Drone Corridor</strong>
              <p style="color: var(--text-muted); font-size: 0.76rem; margin-top: 0.2rem;">Heavy Lift Staging to Imphal & Aizawl Passes</p>
            </div>
          </div>
        </div>
      `;
    } else {
      swapBanner.style.display = 'none';
    }
  }

  showToast(
    isLandslideSimulatedActive
      ? '🚨 NH-27 Sonapur Landslide Triggered! Automated ST-GCN RADR Intermodal Reroute active.'
      : '✅ Sonapur Landslide cleared. Restoring standard corridor flow.'
  );

  if (typeof initGisMap === 'function' && document.getElementById('gis-map-canvas')) initGisMap();
  if (typeof renderFleetTable === 'function') renderFleetTable();
}

// 4. GPS TELEMETRY TIMER
function initGpsTimer() {
  setInterval(() => {
    fleet = fleet.map(v => {
      if (v.status === 'Delayed by Landslide') return v;
      const latDiff = (v.destinationCoordinates.lat - v.coordinates.lat) * 0.005;
      const lngDiff = (v.destinationCoordinates.lng - v.coordinates.lng) * 0.005;
      const speedJitter = Math.floor((Math.random() - 0.5) * 4);
      return {
        ...v,
        speedKmH: Math.max(25, Math.min(65, v.speedKmH + speedJitter)),
        fuelPct: Math.max(10, v.fuelPct - 0.02),
        coordinates: {
          lat: Number((v.coordinates.lat + latDiff).toFixed(5)),
          lng: Number((v.coordinates.lng + lngDiff).toFixed(5))
        }
      };
    });
    if (typeof renderFleetTable === 'function') renderFleetTable();
  }, 4000);
}

// 5. GLOBAL UI HELPERS & NAVIGATION MODALS
function openDriverModal() { window.location.href = 'Driver_view.html'; }
function closeDriverModal() {
  const modal = document.getElementById('driver-modal');
  if (modal) modal.classList.remove('open');
}

function openIncidentModal() {
  const modal = document.getElementById('incident-modal');
  if (modal) modal.classList.add('open');
}
function closeIncidentModal() {
  const modal = document.getElementById('incident-modal');
  if (modal) modal.classList.remove('open');
}

function toggleVoiceWidget() {
  const panel = document.getElementById('voice-assistant-panel');
  if (panel) panel.classList.toggle('active');
}

function showToast(msg) {
  const toast = document.getElementById('live-toast');
  if (!toast) return;
  const msgEl = document.getElementById('toast-msg');
  if (msgEl) msgEl.innerText = msg;
  toast.style.display = 'flex';
  setTimeout(() => { toast.style.display = 'none'; }, 5000);
}

function toggleTheme() {
  isDarkMode = !isDarkMode;
  document.body.classList.toggle('dark-theme', isDarkMode);
}

function exportCsv() {
  const headers = ['Project Code', 'Title', 'State', 'District', 'Sanctioned Budget (Cr)', 'Utilized (Cr)', 'Status'];
  const rows = projects.map(p => [`"${p.code}"`, `"${p.title.replace(/"/g, '""')}"`, `"${p.state}"`, `"${p.district}"`, p.sanctionedBudgetCr, p.fundsUtilizedCr, `"${p.status}"`]);
  const csvContent = 'data:text/csv;charset=utf-8,' + [headers.join(','), ...rows.map(r => r.join(','))].join('\n');
  const encodedUri = encodeURI(csvContent);
  const link = document.createElement('a');
  link.setAttribute('href', encodedUri);
  link.setAttribute('download', `NE_AURA_Audit_Data_${new Date().toISOString().slice(0, 10)}.csv`);
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}

function exportPdf() {
  window.print();
}

// 6. THREE DOTS DROPDOWN MENU HANDLERS
function toggleThreeDotsMenu(e) {
  if (e) e.stopPropagation();
  const dropdown = document.getElementById('three-dots-dropdown');
  if (dropdown) dropdown.classList.toggle('show');
}

function closeThreeDotsMenu() {
  const dropdown = document.getElementById('three-dots-dropdown');
  if (dropdown) dropdown.classList.remove('show');
}

document.addEventListener('click', (e) => {
  const container = document.querySelector('.three-dots-container');
  if (container && !container.contains(e.target)) {
    closeThreeDotsMenu();
  }
});
