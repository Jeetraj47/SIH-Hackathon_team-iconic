/**
 * SIH 2026 — Highway Safety Dashboard
 * Interactive real-time dashboard connecting to the backend API
 */

const API_BASE = 'http://localhost:5000/api';

// ─── State ──────────────────────────────────────────────────
let state = {
  token: null,
  user: null,
  locations: [],
  meshNodes: [],
  meshStats: null,
  incidents: [],
  incidentStats: null,
};

// ─── Toast Notification ─────────────────────────────────────
function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  const icons = { success: '✅', error: '❌', info: 'ℹ️', warning: '⚠️' };
  toast.innerHTML = `<span>${icons[type] || ''}</span> ${message}`;
  container.appendChild(toast);
  setTimeout(() => {
    toast.classList.add('removing');
    setTimeout(() => toast.remove(), 300);
  }, 3500);
}

// ─── API Helpers ────────────────────────────────────────────
async function api(path, method = 'GET', body = null) {
  const opts = {
    method,
    headers: {
      'Content-Type': 'application/json',
    },
  };
  if (state.token) {
    opts.headers.Authorization = `Bearer ${state.token}`;
  }
  if (body) {
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(`${API_BASE}${path}`, opts);
  return resp.json();
}

// ─── Auth: Auto-login ───────────────────────────────────────

async function autoLogin() {
  const res = await api('/auth/login', 'POST', {
    email: 'admin_dash@sih.com',
    password: 'admin123'
  });

  if (res.success) {
    state.token = res.data.token;
    state.user = res.data.user;

    const userName = document.getElementById('user-name');

    if (userName) {
      userName.textContent = state.user.name;
    }

    showToast(
      `Welcome back, ${state.user.name}!`,
      'success'
    );

    return true;
  }

  console.error('Login failed:', res);

  showToast(
    res.error || 'Authentication failed!',
    'error'
  );

  return false;
}

// ─── Seed Demo Data ─────────────────────────────────────────
async function seedDemoData() {
  const users = [
    { name: 'Rajesh Kumar', email: 'rajesh@sih.com', password: 'pass123', role: 'driver', vehicleId: 'MH12XY4567', phone: '9876543210' },
    { name: 'Priya Sharma', email: 'priya@sih.com', password: 'pass123', role: 'driver', vehicleId: 'MH14AB1234', phone: '9876543211' },
    { name: 'Amit Patel', email: 'amit@sih.com', password: 'pass123', role: 'official', phone: '9876543212' },
    { name: 'Sanjay Singh', email: 'sanjay@sih.com', password: 'pass123', role: 'driver', vehicleId: 'MH43CD5678', phone: '9876543213' },
    { name: 'Neha Gupta', email: 'neha@sih.com', password: 'pass123', role: 'official', phone: '9876543214' },
  ];

  const tokens = [];

  for (const u of users) {
    let r = await api('/auth/login', 'POST', { email: u.email, password: u.password });
    if (!r.success) {
      r = await api('/auth/register', 'POST', u);
    }
    if (r.success) tokens.push({ token: r.data.token, user: r.data.user });
  }

  const locations = [
    { longitude: 72.8777, latitude: 19.0760, speed: 72, heading: 200, source: 'gps' },
    { longitude: 73.0600, latitude: 18.9500, speed: 85, heading: 195, source: 'gps' },
    { longitude: 73.3200, latitude: 18.7900, speed: 65, heading: 210, source: 'gps' },
    { longitude: 73.7200, latitude: 18.5700, speed: 90, heading: 190, source: 'mesh' },
    { longitude: 73.8567, latitude: 18.5204, speed: 0, heading: 0, source: 'gps' },
  ];

  for (let i = 0; i < tokens.length; i++) {
    const oldToken = state.token;
    state.token = tokens[i].token;
    await api('/location/update', 'PUT', locations[i]);
    state.token = oldToken;
  }

  await api('/location/update', 'PUT', {
    longitude: 72.8300, latitude: 19.1100, speed: 0, heading: 0, source: 'gps',
  });

  const meshNodes = [
    { nodeId: 'GW-NH48-KM100', name: 'NH-48 Gateway KM 100', type: 'gateway', longitude: 72.90, latitude: 19.10, highway: 'NH-48' },
    { nodeId: 'SN-NH48-KM105', name: 'NH-48 Sensor KM 105', type: 'sensor', longitude: 72.93, latitude: 19.08, highway: 'NH-48' },
    { nodeId: 'RL-NH48-KM110', name: 'NH-48 Relay KM 110', type: 'relay', longitude: 72.96, latitude: 19.05, highway: 'NH-48' },
    { nodeId: 'SN-NH48-KM120', name: 'NH-48 Sensor KM 120', type: 'sensor', longitude: 73.05, latitude: 18.95, highway: 'NH-48' },
    { nodeId: 'GW-NH48-KM130', name: 'NH-48 Gateway KM 130', type: 'gateway', longitude: 73.15, latitude: 18.88, highway: 'NH-48' },
    { nodeId: 'SN-NH48-KM140', name: 'NH-48 Sensor KM 140', type: 'sensor', longitude: 73.30, latitude: 18.80, highway: 'NH-48' },
    { nodeId: 'RL-NH48-KM150', name: 'NH-48 Relay KM 150', type: 'relay', longitude: 73.45, latitude: 18.72, highway: 'NH-48' },
    { nodeId: 'GW-NH44-KM200', name: 'NH-44 Gateway KM 200', type: 'gateway', longitude: 77.59, latitude: 12.97, highway: 'NH-44' },
    { nodeId: 'SN-NH44-KM210', name: 'NH-44 Sensor KM 210', type: 'sensor', longitude: 77.61, latitude: 13.05, highway: 'NH-44' },
    { nodeId: 'RL-NH44-KM220', name: 'NH-44 Relay KM 220', type: 'relay', longitude: 77.63, latitude: 13.12, highway: 'NH-44' },
    { nodeId: 'SN-NH66-KM050', name: 'NH-66 Sensor KM 50', type: 'sensor', longitude: 73.00, latitude: 19.20, highway: 'NH-66' },
    { nodeId: 'GW-NH66-KM060', name: 'NH-66 Gateway KM 60', type: 'gateway', longitude: 73.05, latitude: 19.30, highway: 'NH-66' },
  ];

  for (const n of meshNodes) {
    await api('/mesh/register', 'POST', n);
  }

  const nodesRes = await api('/mesh/nodes');
  if (nodesRes.success && nodesRes.data.nodes) {
    for (const node of nodesRes.data.nodes) {
      const battery = 40 + Math.random() * 60;
      const signal = -30 - Math.random() * 60;
      await api(`/mesh/nodes/${node._id}/heartbeat`, 'PUT', {
        batteryLevel: Math.round(battery),
        signalStrength: Math.round(signal),
      });
    }
  }

  const incidents = [
    { title: 'Major Pothole Near Panvel Junction', description: 'Deep crater-like pothole spanning 3 feet on left lane. Multiple vehicles damaged.', category: 'pothole', severity: 'high', longitude: 73.10, latitude: 18.99, highway: 'NH-48 KM 115', affectedLanes: 1 },
    { title: 'Multi-vehicle Collision on Express Highway', description: 'Three trucks and two cars involved. Highway blocked for emergency response.', category: 'accident', severity: 'critical', longitude: 73.20, latitude: 18.85, highway: 'NH-48 KM 125', affectedLanes: 3 },
    { title: 'Landslide After Heavy Rain at Lonavala', description: 'Rocks and debris blocking partial road surface. One lane still passable.', category: 'landslide', severity: 'high', longitude: 73.40, latitude: 18.75, highway: 'NH-48 KM 95', affectedLanes: 1 },
    { title: 'Waterlogging Near Toll Plaza', description: 'Standing water depth of about 1 foot after overnight rain. Traffic moving slowly.', category: 'flood', severity: 'medium', longitude: 73.30, latitude: 18.82, highway: 'NH-48 KM 135', affectedLanes: 2 },
    { title: 'Fallen Tree Blocking Lane', description: 'Large banyan tree uprooted and blocking the leftmost lane after strong winds.', category: 'debris', severity: 'medium', longitude: 72.95, latitude: 19.05, highway: 'NH-48 KM 108', affectedLanes: 1 },
    { title: 'Construction Zone Speed Limit', description: 'Road widening work in progress. Speed limit reduced to 40 kmph for next 2km.', category: 'construction', severity: 'low', longitude: 73.55, latitude: 18.68, highway: 'NH-48 KM 160', affectedLanes: 1 },
    { title: 'Signal Failure at Bypass Intersection', description: 'Traffic signal not working at major intersection. Manual traffic management in place.', category: 'signal_failure', severity: 'medium', longitude: 73.85, latitude: 18.52, highway: 'NH-48 KM 180', affectedLanes: 0 },
  ];

  for (let i = 0; i < incidents.length; i++) {
    const tkn = i < tokens.length ? tokens[i].token : state.token;
    const oldToken = state.token;
    state.token = tkn;
    await api('/incidents', 'POST', incidents[i]);
    state.token = oldToken;
  }

  const incRes = await api('/incidents');
  if (incRes.success && incRes.data.incidents) {
    const list = incRes.data.incidents;
    if (list[0]) await api(`/incidents/${list[0]._id}/verify`, 'PUT');
    if (list[1]) await api(`/incidents/${list[1]._id}/verify`, 'PUT');
    if (list[2]) await api(`/incidents/${list[2]._id}/verify`, 'PUT');
    if (list[1]) await api(`/incidents/${list[1]._id}`, 'PUT', { status: 'in_progress' });
    if (list[5]) await api(`/incidents/${list[5]._id}/resolve`, 'PUT');
  }
}

// ─── Data Fetching ──────────────────────────────────────────
async function fetchAllData() {
  const [locRes, meshRes, meshStatsRes, incRes, incStatsRes] = await Promise.all([
    api('/location/all?online=true'),
    api('/mesh/nodes'),
    api('/mesh/stats'),
    api('/incidents'),
    api('/incidents/stats'),
  ]);

  if (locRes.success) state.locations = locRes.data.locations;
  if (meshRes.success) state.meshNodes = meshRes.data.nodes;
  if (meshStatsRes.success) state.meshStats = meshStatsRes.data;
  if (incRes.success) state.incidents = incRes.data.incidents;
  if (incStatsRes.success) state.incidentStats = incStatsRes.data;
}

// ─── Render: Overview Tab ───────────────────────────────────
function renderOverview() {
  const onlineUsers = state.locations.length;
  const activeNodes = state.meshStats?.overview?.active ?? 0;
  const activeIncidents =
    (state.incidentStats?.overview?.reported ?? 0) +
    (state.incidentStats?.overview?.verified ?? 0) +
    (state.incidentStats?.overview?.inProgress ?? 0);
  const criticalCount = state.incidents.filter(
    (i) => i.severity === 'critical' && i.status !== 'resolved' && i.status !== 'dismissed'
  ).length;

  animateNumber('kpi-users-val', onlineUsers);
  animateNumber('kpi-mesh-val', activeNodes);
  animateNumber('kpi-incidents-val', activeIncidents);
  animateNumber('kpi-critical-val', criticalCount);

  renderCategoryChart();
  renderSeverityChart();
  renderHighwayChart();
  renderRecentCritical();
}

function animateNumber(elementId, target) {
  const el = document.getElementById(elementId);
  if (!el) return;
  const current = parseInt(el.textContent) || 0;
  if (current === target) { el.textContent = target; return; }
  const duration = 600;
  const start = performance.now();
  function step(timestamp) {
    const progress = Math.min((timestamp - start) / duration, 1);
    const eased = 1 - Math.pow(1 - progress, 3);
    el.textContent = Math.round(current + (target - current) * eased);
    if (progress < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

// ─── Charts (Canvas-based) ──────────────────────────────────
function renderCategoryChart() {
  const canvas = document.getElementById('chart-category');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const data = state.incidentStats?.byCategory ?? [];
  if (data.length === 0) return;

  const colors = ['#ef4444', '#f97316', '#f59e0b', '#22c55e', '#3b82f6', '#8b5cf6', '#ec4899', '#64748b'];
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  const maxVal = Math.max(...data.map((d) => d.count), 1);
  const barWidth = Math.min(40, (w - 60) / data.length - 10);
  const chartH = h - 60;
  const startX = 40;

  data.forEach((item, i) => {
    const barH = (item.count / maxVal) * chartH * 0.85;
    const x = startX + i * (barWidth + 12);
    const y = chartH - barH + 10;

    const grad = ctx.createLinearGradient(x, y, x, chartH + 10);
    grad.addColorStop(0, colors[i % colors.length]);
    grad.addColorStop(1, colors[i % colors.length] + '40');
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.roundRect(x, y, barWidth, barH, [4, 4, 0, 0]);
    ctx.fill();

    ctx.fillStyle = '#f1f5f9';
    ctx.font = 'bold 11px Inter, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(item.count, x + barWidth / 2, y - 6);

    ctx.fillStyle = '#64748b';
    ctx.font = '9px Inter, sans-serif';
    const label = (item._id || 'unknown').replace('_', ' ');
    ctx.fillText(label.charAt(0).toUpperCase() + label.slice(1, 8), x + barWidth / 2, chartH + 26);
  });
}

function renderSeverityChart() {
  const canvas = document.getElementById('chart-severity');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const data = state.incidentStats?.bySeverity ?? [];
  if (data.length === 0) return;

  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  const total = data.reduce((s, d) => s + d.count, 0);
  if (total === 0) return;

  const cx = w / 2;
  const cy = h / 2;
  const r = Math.min(w, h) / 2 - 40;
  const innerR = r * 0.55;

  const colorMap = { critical: '#ef4444', high: '#f97316', medium: '#f59e0b', low: '#22c55e' };
  let angle = -Math.PI / 2;
  const order = ['critical', 'high', 'medium', 'low'];
  const sorted = order.map((sev) => data.find((d) => d._id === sev)).filter(Boolean);

  sorted.forEach((item) => {
    const sliceAngle = (item.count / total) * 2 * Math.PI;
    const color = colorMap[item._id] || '#64748b';

    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, r, angle, angle + sliceAngle);
    ctx.closePath();
    ctx.fill();

    const midAngle = angle + sliceAngle / 2;
    const lx = cx + Math.cos(midAngle) * (r * 0.78);
    const ly = cy + Math.sin(midAngle) * (r * 0.78);
    ctx.fillStyle = '#fff';
    ctx.font = 'bold 11px Inter, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(`${item.count}`, lx, ly);

    angle += sliceAngle;
  });

  ctx.fillStyle = '#111827';
  ctx.beginPath();
  ctx.arc(cx, cy, innerR, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = '#f1f5f9';
  ctx.font = 'bold 24px Inter, sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(total, cx, cy - 6);
  ctx.fillStyle = '#64748b';
  ctx.font = '10px Inter, sans-serif';
  ctx.fillText('TOTAL', cx, cy + 12);
}

function renderHighwayChart() {
  const canvas = document.getElementById('chart-highway');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const data = state.incidentStats?.byHighway ?? [];
  if (data.length === 0) return;

  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  const maxVal = Math.max(...data.map((d) => d.count), 1);
  const barH = Math.min(28, (h - 40) / data.length - 6);
  const chartW = w - 160;
  const startY = 20;

  data.forEach((item, i) => {
    const barW = (item.count / maxVal) * chartW * 0.85;
    const y = startY + i * (barH + 8);
    const x = 130;

    ctx.fillStyle = 'rgba(255,255,255,0.03)';
    ctx.beginPath();
    ctx.roundRect(x, y, chartW * 0.85, barH, 4);
    ctx.fill();

    const grad = ctx.createLinearGradient(x, y, x + barW, y);
    grad.addColorStop(0, '#6366f1');
    grad.addColorStop(1, '#22d3ee');
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.roundRect(x, y, barW, barH, 4);
    ctx.fill();

    if (item.activeCount > 0) {
      ctx.fillStyle = '#ef4444';
      ctx.beginPath();
      ctx.arc(x + barW + 14, y + barH / 2, 8, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = '#fff';
      ctx.font = 'bold 8px Inter, sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(item.activeCount, x + barW + 14, y + barH / 2);
    }

    ctx.fillStyle = '#94a3b8';
    ctx.font = '11px Inter, sans-serif';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    ctx.fillText(item._id || 'Unknown', x - 8, y + barH / 2);

    ctx.fillStyle = '#f1f5f9';
    ctx.font = 'bold 10px Inter, sans-serif';
    ctx.textAlign = 'left';
    ctx.fillText(item.count, x + barW + (item.activeCount > 0 ? 28 : 8), y + barH / 2);
  });
}

// ─── Render: Recent Critical ────────────────────────────────
function renderRecentCritical() {
  const container = document.getElementById('recent-critical');
  const critical = state.incidents
    .filter((i) => (i.severity === 'critical' || i.severity === 'high') && i.status !== 'resolved' && i.status !== 'dismissed')
    .slice(0, 8);

  if (critical.length === 0) {
    container.innerHTML = `<div class="empty-state"><div class="empty-icon">✅</div><div class="empty-text">No critical incidents right now</div></div>`;
    return;
  }

  container.innerHTML = critical
    .map((inc) => {
      const icon = getCategoryIcon(inc.category);
      const time = timeAgo(inc.createdAt);
      return `
      <div class="activity-item">
        <div class="activity-icon">${icon}</div>
        <div class="activity-info">
          <div class="activity-title">${escapeHtml(inc.title)}</div>
          <div class="activity-meta">${inc.highway || 'Unknown location'} • ${time}</div>
        </div>
        <span class="activity-badge badge-${inc.severity}">${inc.severity}</span>
      </div>`;
    })
    .join('');
}

// ─── Render: Fleet Tracking (Sidebar only, Mapbox handles map) ──
function renderFleet() {
  const listEl = document.getElementById('fleet-list');
  if (!listEl) return;
  
  if (state.locations.length === 0) {
    listEl.innerHTML = `<div class="empty-state"><div class="empty-icon">📍</div><div class="empty-text">No users online</div></div>`;
  } else {
    listEl.innerHTML = state.locations
      .map((loc) => {
        const user = loc.user || {};
        const role = user.role || 'unknown';
        const dotColor = role === 'admin' ? 'var(--accent-purple)' : role === 'official' ? 'var(--accent-green)' : 'var(--accent-blue)';
        const coords = loc.location?.coordinates || [0, 0];
        return `
        <div class="fleet-item">
          <div class="fleet-item-dot" style="background:${dotColor};color:${dotColor}"></div>
          <div class="fleet-item-info">
            <div class="fleet-item-name">${escapeHtml(user.name || 'Unknown')}</div>
            <div class="fleet-item-meta">${role.toUpperCase()} ${user.vehicleId ? '• ' + user.vehicleId : ''} • [${coords[0].toFixed(3)}, ${coords[1].toFixed(3)}]</div>
          </div>
          <div class="fleet-item-speed">${loc.speed || 0} km/h</div>
        </div>`;
      })
      .join('');
  }
}

// ─── Render: Mesh Network ───────────────────────────────────
function renderMesh() {
  const s = state.meshStats?.overview ?? {};
  animateNumber('mesh-total', s.total ?? 0);
  animateNumber('mesh-active', s.active ?? 0);
  animateNumber('mesh-inactive', s.inactive ?? 0);
  animateNumber('mesh-maintenance', s.maintenance ?? 0);
  const batteryEl = document.getElementById('mesh-battery');
  const signalEl = document.getElementById('mesh-signal');
  if(batteryEl) batteryEl.textContent = `${Math.round(s.avgBattery ?? 0)}%`;
  if(signalEl) signalEl.textContent = `${Math.round(s.avgSignal ?? 0)} dBm`;

  renderMeshNodes();
  renderMeshHighwayChart();

  const typeFilter = document.getElementById('mesh-filter-type');
  const statusFilter = document.getElementById('mesh-filter-status');
  if (typeFilter) typeFilter.onchange = renderMeshNodes;
  if (statusFilter) statusFilter.onchange = renderMeshNodes;
}

function renderMeshNodes() {
  const container = document.getElementById('mesh-nodes-grid');
  if (!container) return;
  
  const typeFilter = document.getElementById('mesh-filter-type')?.value || '';
  const statusFilter = document.getElementById('mesh-filter-status')?.value || '';

  let nodes = state.meshNodes;
  if (typeFilter) nodes = nodes.filter((n) => n.type === typeFilter);
  if (statusFilter) nodes = nodes.filter((n) => n.status === statusFilter);

  if (nodes.length === 0) {
    container.innerHTML = `<div class="empty-state"><div class="empty-icon">🔗</div><div class="empty-text">No nodes found</div></div>`;
    return;
  }

  container.innerHTML = nodes
    .map((node) => {
      const battery = node.batteryLevel ?? 100;
      const batteryColor = battery > 60 ? '#10b981' : battery > 30 ? '#f59e0b' : '#ef4444';
      const signal = node.signalStrength ?? 0;
      return `
      <div class="node-card type-${node.type}">
        <div class="node-header">
          <span class="node-name">${escapeHtml(node.name)}</span>
          <span class="node-status ${node.status}">${node.status}</span>
        </div>
        <div class="node-id">${node.nodeId}</div>
        <div class="node-details">
          <div class="node-detail"><span class="node-detail-label">Type:</span> <span class="node-detail-val">${node.type}</span></div>
          <div class="node-detail"><span class="node-detail-label">Highway:</span> <span class="node-detail-val">${node.highway}</span></div>
          <div class="node-detail">
            <span class="node-detail-label">Battery:</span>
            <span class="battery-bar">
              <span class="battery-fill"><span class="battery-fill-inner" style="width:${battery}%;background:${batteryColor}"></span></span>
              <span class="node-detail-val">${battery}%</span>
            </span>
          </div>
          <div class="node-detail"><span class="node-detail-label">Signal:</span> <span class="node-detail-val">${signal} dBm</span></div>
        </div>
      </div>`;
    })
    .join('');
}

function renderMeshHighwayChart() {
  const canvas = document.getElementById('chart-mesh-highway');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const data = state.meshStats?.byHighway ?? [];
  if (data.length === 0) return;

  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  const maxVal = Math.max(...data.map((d) => d.count), 1);
  const barH = 30;
  const startY = 20;

  data.forEach((item, i) => {
    const barW = (item.count / maxVal) * (w - 200) * 0.85;
    const y = startY + i * (barH + 12);
    const x = 100;

    ctx.fillStyle = 'rgba(255,255,255,0.03)';
    ctx.beginPath();
    ctx.roundRect(x, y, (w - 200) * 0.85, barH, 4);
    ctx.fill();

    const grad = ctx.createLinearGradient(x, y, x + barW, y);
    grad.addColorStop(0, '#a855f7');
    grad.addColorStop(1, '#22d3ee');
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.roundRect(x, y, barW, barH, 4);
    ctx.fill();

    ctx.fillStyle = '#10b981';
    ctx.font = 'bold 10px Inter, sans-serif';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.fillText(`${item.activeCount} active`, x + barW + 10, y + barH / 2);

    ctx.fillStyle = '#94a3b8';
    ctx.font = '12px Inter, sans-serif';
    ctx.textAlign = 'right';
    ctx.fillText(item._id || 'Unknown', x - 10, y + barH / 2);

    ctx.fillStyle = '#fff';
    ctx.font = 'bold 12px Inter, sans-serif';
    ctx.textAlign = 'center';
    if (barW > 30) {
      ctx.fillText(item.count, x + barW / 2, y + barH / 2);
    }
  });
}

// ─── Render: Incidents ──────────────────────────────────────
function renderIncidents() {
  const s = state.incidentStats?.overview ?? {};
  animateNumber('stat-reported', s.reported ?? 0);
  animateNumber('stat-verified', s.verified ?? 0);
  animateNumber('stat-inprogress', s.inProgress ?? 0);
  animateNumber('stat-resolved', s.resolved ?? 0);
  animateNumber('stat-dismissed', s.dismissed ?? 0);

  renderIncidentList();
  renderIncidentMap();

  const catFilter = document.getElementById('inc-filter-category');
  const sevFilter = document.getElementById('inc-filter-severity');
  if (catFilter) catFilter.onchange = renderIncidentList;
  if (sevFilter) sevFilter.onchange = renderIncidentList;
}

function renderIncidentList() {
  const container = document.getElementById('incidents-list');
  if (!container) return;
  
  const catFilter = document.getElementById('inc-filter-category')?.value || '';
  const sevFilter = document.getElementById('inc-filter-severity')?.value || '';

  let list = state.incidents;
  if (catFilter) list = list.filter((i) => i.category === catFilter);
  if (sevFilter) list = list.filter((i) => i.severity === sevFilter);

  if (list.length === 0) {
    container.innerHTML = `<div class="empty-state"><div class="empty-icon">🚨</div><div class="empty-text">No incidents found</div></div>`;
    return;
  }

  container.innerHTML = list
    .map((inc) => {
      const icon = getCategoryIcon(inc.category);
      const time = timeAgo(inc.createdAt);
      const reporter = inc.reporter?.name || 'Unknown';
      return `
      <div class="incident-item">
        <div class="incident-sev-bar ${inc.severity}"></div>
        <div class="incident-category-icon">${icon}</div>
        <div class="incident-info">
          <div class="incident-title">${escapeHtml(inc.title)}</div>
          <div class="incident-meta">
            <span>📍 ${inc.highway || 'No highway'}</span>
            <span>👤 ${escapeHtml(reporter)}</span>
            <span>🕐 ${time}</span>
            ${inc.affectedLanes ? `<span>🚗 ${inc.affectedLanes} lane(s)</span>` : ''}
          </div>
        </div>
        <div class="incident-badges">
          <span class="incident-badge badge-${inc.severity}">${inc.severity}</span>
          <span class="incident-badge badge-status-${inc.status}">${inc.status.replace('_', ' ')}</span>
        </div>
      </div>`;
    })
    .join('');
}

function renderIncidentMap() {
  const canvas = document.getElementById('canvas-incidents');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  const active = state.incidents.filter((i) => i.status !== 'resolved' && i.status !== 'dismissed');
  if (active.length === 0) return;

  const coords = active.map((i) => i.location?.coordinates || [0, 0]);
  let minLng = Math.min(...coords.map((c) => c[0])) - 0.15;
  let maxLng = Math.max(...coords.map((c) => c[0])) + 0.15;
  let minLat = Math.min(...coords.map((c) => c[1])) - 0.1;
  let maxLat = Math.max(...coords.map((c) => c[1])) + 0.1;

  if (maxLng - minLng < 0.5) { minLng -= 0.25; maxLng += 0.25; }
  if (maxLat - minLat < 0.3) { minLat -= 0.15; maxLat += 0.15; }

  const toX = (lng) => 40 + ((lng - minLng) / (maxLng - minLng)) * (w - 80);
  const toY = (lat) => h - 30 - ((lat - minLat) / (maxLat - minLat)) * (h - 60);

  ctx.strokeStyle = 'rgba(99, 102, 241, 0.05)';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 8; i++) {
    const gx = 40 + (i / 8) * (w - 80);
    ctx.beginPath(); ctx.moveTo(gx, 10); ctx.lineTo(gx, h - 20); ctx.stroke();
    const gy = 10 + (i / 8) * (h - 30);
    ctx.beginPath(); ctx.moveTo(40, gy); ctx.lineTo(w - 40, gy); ctx.stroke();
  }

  const severityColors = { critical: '#ef4444', high: '#f97316', medium: '#f59e0b', low: '#22c55e' };

  active.forEach((inc) => {
    const c = inc.location?.coordinates || [0, 0];
    const x = toX(c[0]);
    const y = toY(c[1]);
    const color = severityColors[inc.severity] || '#64748b';
    const size = inc.severity === 'critical' ? 12 : inc.severity === 'high' ? 10 : 8;

    const glow = ctx.createRadialGradient(x, y, 0, x, y, size + 10);
    glow.addColorStop(0, color + '40');
    glow.addColorStop(1, 'transparent');
    ctx.fillStyle = glow;
    ctx.beginPath();
    ctx.arc(x, y, size + 10, 0, Math.PI * 2);
    ctx.fill();

    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(x, y, size, 0, Math.PI * 2);
    ctx.fill();

    ctx.font = `${size}px sans-serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(getCategoryIcon(inc.category), x, y);

    ctx.fillStyle = '#f1f5f9';
    ctx.font = 'bold 8px Inter, sans-serif';
    ctx.textAlign = 'center';
    const label = inc.title.length > 25 ? inc.title.substring(0, 22) + '...' : inc.title;
    ctx.fillText(label, x, y - size - 6);
  });
}

// ─── Utility Functions ──────────────────────────────────────
function getCategoryIcon(category) {
  const icons = {
    accident: '🚗', pothole: '🕳️', landslide: '⛰️', flood: '🌊',
    debris: '🌳', construction: '🚧', signal_failure: '🚦', other: '❓',
  };
  return icons[category] || '❓';
}

function timeAgo(dateStr) {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function escapeHtml(str) {
  const d = document.createElement('div');
  d.textContent = str || '';
  return d.innerHTML;
}

// ─── Tab Navigation ─────────────────────────────────────────
function initNav() {
  const navBtns = document.querySelectorAll('.nav-btn');
  navBtns.forEach((btn) => {
    btn.addEventListener('click', () => {
      const tabId = btn.dataset.tab;

      navBtns.forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');

      document.querySelectorAll('.tab-content').forEach((t) => t.classList.remove('active'));
      const tab = document.getElementById(`tab-${tabId}`);
      if (tab) tab.classList.add('active');

      // Re-render active tab data
      switch (tabId) {
        case 'overview': renderOverview(); break;
        case 'location': renderFleet(); break;
        case 'mesh': renderMesh(); break;
        case 'incidents': renderIncidents(); break;
      }
    });
  });
}

// ─── Mapbox Navigation & Incident State ────────────────────────
let mapboxMap;
let mapboxMarkers = [];

let navigationMap = null;
let navigationDirections = null;

function initMapbox() {

  mapboxgl.accessToken =
    'pk.eyJ1IjoiYW5raXRrdWp1ciIsImEiOiJjbXVjNGJjYmEwM3gxMnlzaHZ0dW5hZGY4In0.sb04HbaoGHpIbPGnCeRbOA';

  const container = document.getElementById('navigation-map');
  if (!container) return;

  navigationMap = new mapboxgl.Map({
    container: 'navigation-map',

    style: 'mapbox://styles/mapbox/streets-v12',

    center: [85.3240, 23.3441], // Ranchi

    zoom: 9
  });



  navigationMap.addControl(
    new mapboxgl.NavigationControl(),
    'top-right'
  );

  navigationMap.on('load', () => {

    plotNavigationIncidents();

    // Important when opening a map inside a modal
    setTimeout(() => {
      navigationMap.resize();
    }, 100);

  });

}
function openNavigation() {
  const modal = document.getElementById('navigation-modal');

  if (!modal) {
    console.error('navigation-modal not found');
    return;
  }

  // Show the navigation window
  modal.classList.remove('hidden');

  // Create the map
  if (!navigationMap) {
    initMapbox();
  } else {
    setTimeout(() => {
      navigationMap.resize();
    }, 200);
  }
}


function closeNavigation() {

  const modal = document.getElementById('navigation-modal');

  if (!modal) return;

  modal.classList.add('hidden');

}


function plotMapboxIncidents() {
  if (!mapboxMap) return;

  // Clear old markers
  mapboxMarkers.forEach(marker => marker.remove());
  mapboxMarkers = [];

  const activeIncidents = state.incidents.filter(i => i.status !== 'resolved' && i.status !== 'dismissed');

  activeIncidents.forEach(inc => {
    const coords = inc.location?.coordinates;
    if (!coords) return;

    const color = inc.severity === 'critical' ? '#ef4444' : 
                  inc.severity === 'high' ? '#f97316' : 
                  inc.severity === 'medium' ? '#f59e0b' : '#22c55e';

    const popup = new mapboxgl.Popup({ offset: 25 }).setHTML(`
      <div style="color: #111827; padding: 4px; font-family: sans-serif;">
        <h4 style="margin: 0 0 4px 0; font-size: 14px;">${getCategoryIcon(inc.category)} ${inc.title}</h4>
        <p style="margin: 0; font-size: 12px;"><strong>Severity:</strong> ${inc.severity.toUpperCase()}</p>
        <p style="margin: 4px 0 0 0; font-size: 12px;">${inc.description}</p>
      </div>
    `);

    const marker = new mapboxgl.Marker({ color: color })
      .setLngLat(coords)
      .setPopup(popup)
      .addTo(mapboxMap);

    mapboxMarkers.push(marker);
  });
}
function initNavigationButton() {

  const openBtn =
    document.getElementById('open-navigation-btn');

  const closeBtn =
    document.getElementById('close-navigation-btn');

  if (openBtn) {
    openBtn.addEventListener('click', openNavigation);
  }

  if (closeBtn) {
    closeBtn.addEventListener('click', closeNavigation);
  }

}

// ─── Main Init ──────────────────────────────────────────────
async function init() {
  try {
    // Login
    const loggedIn = await autoLogin();
    if (!loggedIn) return;

    // Check if we need to seed data
    const checkRes = await api('/incidents');
    if (checkRes.success && (checkRes.data.incidents?.length ?? 0) < 3) {
      showToast('Seeding demo data...', 'info');
      await seedDemoData();
      showToast('Demo data ready!', 'success');
    }

    // Fetch all data
    await fetchAllData();

    // Initialize nav
    initNav();
    initNavigationButton();

    renderOverview();
    renderFleet();
    renderMesh();
    renderIncidents();

// Hide loading overlay
    document.getElementById('loading-overlay').classList.add('hidden');
    document.getElementById('app').classList.remove('hidden');

    // Auto-refresh every 30 seconds
    setInterval(async () => {
      await fetchAllData();
      
      const activeTab = document.querySelector('.nav-btn.active')?.dataset?.tab;
      switch (activeTab) {
        case 'overview': renderOverview(); break;
        case 'location': renderFleet(); break;
        case 'mesh': renderMesh(); break;
        case 'incidents': renderIncidents(); break;
      }
      
    }, 30000);
  } catch (err) {
    console.error('Init error:', err);
    showToast('Failed to initialize dashboard', 'error');
  }
}

// Start Application
document.addEventListener('DOMContentLoaded', init);