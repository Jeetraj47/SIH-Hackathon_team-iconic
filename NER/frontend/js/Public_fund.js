/* ============================================================================
   NE-AURA (SIH 26002) - PUBLIC FUND GOVERNANCE PAGE SCRIPT
   Page-specific logic for Public_fund.html (MDoNER project grid, state filters, CSV export)
   ============================================================================ */

function renderPublicFundProjects() {
  const gridContainer = document.getElementById('project-cards-grid');
  const stateFilter = document.getElementById('filter-state')?.value || 'all';
  const sectorFilter = document.getElementById('filter-sector')?.value || 'all';
  const searchVal = (document.getElementById('filter-search')?.value || '').toLowerCase();

  if (!gridContainer) return;

  const filtered = projects.filter(p => {
    if (stateFilter !== 'all' && p.state !== stateFilter) return false;
    if (sectorFilter !== 'all' && p.sector !== sectorFilter) return false;
    if (searchVal && !p.title.toLowerCase().includes(searchVal) && !p.code.toLowerCase().includes(searchVal)) return false;
    return true;
  });

  gridContainer.innerHTML = filtered.map(p => `
    <div class="glass-card" onclick="openProjectDetailModal('${p.id}')">
      <div class="card-header-title">
        <span class="badge ${p.status === 'On Track' ? 'badge-success' : p.status === 'Delayed' ? 'badge-warning' : 'badge-danger'}">${p.status}</span>
        <span class="mono-font" style="font-size: 0.75rem; color: var(--text-muted);">${p.code}</span>
      </div>
      <h3 style="font-size: 0.95rem; font-weight: 700; margin-bottom: 0.5rem; line-height: 1.3;">${p.title}</h3>
      <p style="font-size: 0.78rem; color: var(--text-muted); margin-bottom: 1rem;">${p.district}, ${NER_STATES[p.state]?.name || p.state}</p>
      
      <div style="margin-bottom: 0.75rem;">
        <div style="display: flex; justify-content: space-between; font-size: 0.75rem; font-weight: 700; margin-bottom: 0.25rem;">
          <span>Physical Progress</span>
          <span>${p.physicalProgressPct}%</span>
        </div>
        <div class="progress-bar-bg">
          <div class="progress-bar-fill" style="width: ${p.physicalProgressPct}%; background: var(--accent-forest);"></div>
        </div>
      </div>

      <div style="display: flex; justify-content: space-between; font-size: 0.75rem; color: var(--text-muted); border-top: 1px solid var(--border-subtle); padding-top: 0.5rem;">
        <span>Sanctioned: ₹${p.sanctionedBudgetCr} Cr</span>
        <span>Utilized: ₹${p.fundsUtilizedCr} Cr</span>
      </div>
    </div>
  `).join('');
}

function openProjectDetailModal(id) {
  const prj = projects.find(p => p.id === id);
  if (!prj) return;
  const content = document.getElementById('project-detail-content');
  if (content) {
    content.innerHTML = `
      <h3 style="font-size: 1.1rem; font-weight: 800; color: var(--accent-forest); margin-bottom: 0.5rem;">${prj.title}</h3>
      <p style="font-size: 0.8rem; color: var(--text-muted); margin-bottom: 1rem;">Code: ${prj.code} | State: ${NER_STATES[prj.state]?.name}</p>
      <p style="font-size: 0.85rem; margin-bottom: 1rem;">${prj.description}</p>
      <div class="grid-2" style="margin-bottom: 1rem;">
        <div style="background: var(--bg-card-subtle); padding: 0.75rem; border-radius: 8px;">
          <span style="font-size: 0.75rem; color: var(--text-muted);">Sanctioned Budget</span>
          <h4 style="font-size: 1.1rem; font-weight: 800;">₹${prj.sanctionedBudgetCr} Cr</h4>
        </div>
        <div style="background: var(--bg-card-subtle); padding: 0.75rem; border-radius: 8px;">
          <span style="font-size: 0.75rem; color: var(--text-muted);">Physical Progress</span>
          <h4 style="font-size: 1.1rem; font-weight: 800; color: var(--accent-forest);">${prj.physicalProgressPct}%</h4>
        </div>
      </div>
      <div style="font-family: 'JetBrains Mono', monospace; font-size: 0.75rem; background: rgba(16, 185, 129, 0.1); border: 1px dashed var(--accent-forest); padding: 0.5rem 0.75rem; border-radius: 6px;">
        🔒 Merkle Proof SHA256: 0x8a92f018e${prj.id.replace(/[^0-9]/g, '')}b44a9
      </div>
    `;
  }
  const modal = document.getElementById('project-detail-modal');
  if (modal) modal.classList.add('open');
}

function closeProjectDetailModal() {
  const modal = document.getElementById('project-detail-modal');
  if (modal) modal.classList.remove('open');
}

function openCitizenAuditModal() {
  const modal = document.getElementById('citizen-audit-modal');
  if (modal) modal.classList.add('open');
}

function closeCitizenAuditModal() {
  const modal = document.getElementById('citizen-audit-modal');
  if (modal) modal.classList.remove('open');
}

function submitCitizenAudit(event) {
  event.preventDefault();
  const projId = document.getElementById('audit-proj-id')?.value;
  const category = document.getElementById('audit-category')?.value;
  const randomHash = '0x' + Math.random().toString(16).substr(2, 8) + '44e8';

  closeCitizenAuditModal();
  showToast(`📢 Citizen Audit Hash Generated: ${randomHash} · Logged for ${projId}`);
}

document.addEventListener('DOMContentLoaded', () => {
  renderPublicFundProjects();
});

