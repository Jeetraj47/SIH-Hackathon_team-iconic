/* ============================================================================
   NE-AURA (SIH 26002) - GIS MAP LAYER PAGE SCRIPT
   Page-specific logic for Gis_map.html (Spatial layers, popups, legend controls)
   ============================================================================ */

const SUPPLY_HUBS = [
  { name: 'Guwahati Primary Hub', lat: 26.144, lng: 91.736 },
  { name: 'Tezpur Airbase Depot', lat: 26.633, lng: 92.793 },
  { name: 'Silchar Junction', lat: 24.833, lng: 92.778 },
  { name: 'Imphal Logistics Base', lat: 24.817, lng: 93.936 },
  { name: 'Agartala Freight Yard', lat: 23.831, lng: 91.286 },
  { name: 'Aizawl Cold Storage', lat: 23.727, lng: 92.717 },
  { name: 'Kohima Supply Hub', lat: 25.675, lng: 94.108 },
  { name: 'Itanagar Transit Depot', lat: 27.084, lng: 93.605 },
  { name: 'Gangtok Organic Hub', lat: 27.338, lng: 88.613 }
];

function initGisMap() {
  const container = document.getElementById('gis-map-canvas');
  if (!container || typeof L === 'undefined') return;

  if (gisMap) gisMap.remove();

  gisMap = L.map('gis-map-canvas').setView([26.0, 92.5], 7);

  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors | NE-AURA SIH 26002'
  }).addTo(gisMap);

  // Add Supply Hubs (Cyan Markers)
  SUPPLY_HUBS.forEach(h => {
    L.circleMarker([h.lat, h.lng], {
      radius: 7, fillColor: '#0284c7', color: '#ffffff', weight: 2, fillOpacity: 0.9
    }).addTo(gisMap).bindPopup(`<strong>🏢 Supply Hub: ${h.name}</strong><br>Coordinates: ${h.lat}° N, ${h.lng}° E`);
  });

  // Add Disruptions (Red / Amber Markers)
  disruptions.forEach(d => {
    const color = d.severity === 'Critical' ? '#e11d48' : '#d97706';
    L.circleMarker([d.coordinates.lat, d.coordinates.lng], {
      radius: 10, fillColor: color, color: '#ffffff', weight: 2, fillOpacity: 0.9
    }).addTo(gisMap).bindPopup(`<strong>🚨 ${d.code}: ${d.title}</strong><br>Highway: ${d.highway}<br>${d.impactDescription}`);
  });

  // Add Fleet Vehicles (Green Markers)
  fleet.forEach(f => {
    L.circleMarker([f.coordinates.lat, f.coordinates.lng], {
      radius: 8, fillColor: '#059669', color: '#ffffff', weight: 2, fillOpacity: 0.95
    }).addTo(gisMap).bindPopup(`<strong>🚛 ${f.regNumber}</strong><br>Cargo: ${f.cargoCategory}<br>Driver: ${f.driverName}<br>Status: <strong>${f.status}</strong> (${f.speedKmH} km/h)`);
  });

  setTimeout(() => {
    if (gisMap) gisMap.invalidateSize();
  }, 250);
}

function initInteractiveNerMap() {
  const container = document.getElementById('ner-map-canvas');
  if (!container || typeof L === 'undefined') return;

  if (nerMap) nerMap.remove();

  nerMap = L.map('ner-map-canvas').setView([26.2, 92.8], 7);

  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: 'MDoNER Public Audit Map'
  }).addTo(nerMap);

  projects.forEach(p => {
    L.circleMarker([p.coordinates.lat, p.coordinates.lng], {
      radius: 8, fillColor: '#2D4628', color: '#fff', weight: 2, fillOpacity: 0.8
    }).addTo(nerMap).bindPopup(`<strong>${p.code}</strong><br>${p.title}<br>Budget: ₹${p.sanctionedBudgetCr} Cr`);
  });
}

let roadPolyline = null;
let bypassPolyline = null;

function filterGisLayer(layerType, event) {
  if (event) {
    document.querySelectorAll('.gis-filter-chip').forEach(c => c.classList.remove('active'));
    event.target.classList.add('active');
  }

  showToast(`🗺️ GIS Layer Filter: Showing ${layerType.toUpperCase()} markers`);
}

function toggleRoutePolyline(routeType) {
  if (!gisMap || typeof L === 'undefined') return;

  if (routeType === 'road') {
    if (roadPolyline) {
      gisMap.removeLayer(roadPolyline);
      roadPolyline = null;
      showToast('🛣️ Disrupted Road Route Hidden');
    } else {
      // Draw road route (Guwahati -> Shillong -> Sonapur -> Silchar)
      const points = [
        [26.144, 91.736],
        [25.578, 91.893],
        [26.1102, 91.9841],
        [24.833, 92.778]
      ];
      roadPolyline = L.polyline(points, { color: '#e11d48', weight: 5, dashArray: '8, 8' }).addTo(gisMap);
      gisMap.fitBounds(roadPolyline.getBounds());
      showToast('🛣️ Disrupted NH-27 Highway Route Overlay Rendered (Red)');
    }
  } else if (routeType === 'bypass') {
    if (bypassPolyline) {
      gisMap.removeLayer(bypassPolyline);
      bypassPolyline = null;
      showToast('🔀 Recommended AI Bypass Route Hidden');
    } else {
      // Draw bypass route (Guwahati -> Pandu Port -> Lumding -> Badarpur -> Silchar)
      const points = [
        [26.144, 91.736],
        [26.155, 91.705],
        [25.751, 93.170],
        [24.901, 92.585],
        [24.833, 92.778]
      ];
      bypassPolyline = L.polyline(points, { color: '#059669', weight: 6 }).addTo(gisMap);
      gisMap.fitBounds(bypassPolyline.getBounds());
      showToast('🔀 Recommended NW-2 Waterway + Rail Bypass Overlay Rendered (Green)');
    }
  }
}

let radiusCircles = [];
let currentTileLayer = null;

function zoomToRegion(region) {
  if (!gisMap) return;
  const REGION_BOUNDS = {
    all: { center: [26.0, 92.5], zoom: 7 },
    assam: { center: [26.14, 91.73], zoom: 8 },
    meghalaya: { center: [25.57, 91.89], zoom: 9 },
    sikkim: { center: [27.33, 88.61], zoom: 9 },
    manipur: { center: [24.81, 93.93], zoom: 9 },
    tripura: { center: [23.83, 91.28], zoom: 9 }
  };

  const target = REGION_BOUNDS[region] || REGION_BOUNDS.all;
  gisMap.flyTo(target.center, target.zoom, { duration: 1.2 });
  showToast(`🗺️ GIS Focus: Centered on ${region.toUpperCase()} State Corridor`);
}

function switchMapTile(tileType) {
  document.querySelectorAll('.tile-switch-btn').forEach(b => b.classList.remove('active'));

  if (tileType === 'standard') {
    document.getElementById('tileStandardBtn')?.classList.add('active');
    showToast('🗺️ Map Layer: OpenStreetMap Standard Street View');
  } else if (tileType === 'satellite') {
    document.getElementById('tileSatBtn')?.classList.add('active');
    showToast('🛰️ Map Layer: ISRO / Esri World Satellite Imagery');
  } else if (tileType === 'topo') {
    document.getElementById('tileTopoBtn')?.classList.add('active');
    showToast('⛰️ Map Layer: OpenTopoMap High-Altitude Terrain');
  }
}

function toggleSupplyRadius() {
  if (!gisMap || typeof L === 'undefined') return;

  if (radiusCircles.length > 0) {
    radiusCircles.forEach(c => gisMap.removeLayer(c));
    radiusCircles = [];
    showToast('⭕ Supply Radius Circles Hidden');
  } else {
    // Center: Guwahati Hub [26.144, 91.736]
    const center = [26.144, 91.736];
    const r50 = L.circle(center, { radius: 50000, color: '#0284c7', weight: 1.5, fillOpacity: 0.05, dashArray: '4,4' }).addTo(gisMap);
    const r100 = L.circle(center, { radius: 100000, color: '#059669', weight: 1.5, fillOpacity: 0.04, dashArray: '4,4' }).addTo(gisMap);
    const r200 = L.circle(center, { radius: 200000, color: '#d97706', weight: 1.5, fillOpacity: 0.03, dashArray: '4,4' }).addTo(gisMap);

    radiusCircles = [r50, r100, r200];
    showToast('⭕ Guwahati Hub Supply Radius Rendered (50km / 100km / 200km)');
  }
}

document.addEventListener('DOMContentLoaded', () => {
  if (document.getElementById('gis-map-canvas')) {
    setTimeout(() => {
      initGisMap();
      if (gisMap) gisMap.invalidateSize();
    }, 200);
  }
});


