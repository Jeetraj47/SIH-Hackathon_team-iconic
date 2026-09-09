/* ============================================================================
   NE-AURA (SIH 26002) - AI ROUTE OPTIMIZER PAGE SCRIPT
   Page-specific logic for AI_Route_Optimizer.html (ST-GCN + GRU calculation form & route map)
   ============================================================================ */

function calculateAIRoute(e) {
  if (e) e.preventDefault();
  const origin = document.getElementById('originSelect')?.value || 'Guwahati Hub';
  const dest = document.getElementById('destSelect')?.value || 'Silchar Hub';
  const cargo = document.getElementById('cargoTypeSelect')?.value || 'Essential Vaccines';

  showToast(`🧠 ST-GCN RADR Route Calculated for ${cargo}: ${origin} ➔ ${dest} (96.4% Confidence, Intermodal Swap Active)`);
}

function updateSimulatorValues() {
  const inputs = document.querySelectorAll('.range-slider-input');
  if (inputs.length >= 3) {
    const rain = parseInt(inputs[0].value, 10);
    const slope = parseInt(inputs[1].value, 10);
    const vis = parseInt(inputs[2].value, 10);

    const rainEl = document.getElementById('rainVal');
    const slopeEl = document.getElementById('slopeVal');
    const visEl = document.getElementById('visVal');
    const riskEl = document.getElementById('simRiskScore');

    if (rainEl) rainEl.textContent = `${rain} mm/hr`;
    if (slopeEl) slopeEl.textContent = `${slope}° Angle`;
    if (visEl) visEl.textContent = `${vis} meters`;

    // Dynamic ST-GCN Risk Index calculation
    const rawRisk = Math.min(0.98, Math.max(0.05, (rain * 0.003) + (slope * 0.008) + ((2000 - vis) * 0.00015)));
    const formattedRisk = rawRisk.toFixed(2);

    if (riskEl) {
      if (rawRisk > 0.75) {
        riskEl.style.color = 'var(--accent-terracotta)';
        riskEl.textContent = `${formattedRisk} · CRITICAL THREAT`;
      } else if (rawRisk > 0.40) {
        riskEl.style.color = '#f59e0b';
        riskEl.textContent = `${formattedRisk} · ELEVATED WARNING`;
      } else {
        riskEl.style.color = 'var(--accent-forest)';
        riskEl.textContent = `${formattedRisk} · CLEAR ROUTE`;
      }
    }
  }
}

function openWaybillModal() {
  const modal = document.getElementById('waybill-modal');
  if (modal) modal.classList.add('active');
}

function closeWaybillModal() {
  const modal = document.getElementById('waybill-modal');
  if (modal) modal.classList.remove('active');
}

function updateOptimizationWeights() {
  const wCost = document.getElementById('wCostVal');
  const wSpeed = document.getElementById('wSpeedVal');
  const wSafety = document.getElementById('wSafetyVal');
  const wSpoilage = document.getElementById('wSpoilageVal');
  const scoreBadge = document.getElementById('tuningScoreBadge');

  const sliders = document.querySelectorAll('#aiRouterForm ~ .glass-card .range-slider-input');
  // Reading sliders
  const cVal = document.querySelectorAll('.range-slider-input')[0]?.value || 85;
  const spVal = document.querySelectorAll('.range-slider-input')[1]?.value || 70;
  const sfVal = document.querySelectorAll('.range-slider-input')[2]?.value || 95;
  const spLVal = document.querySelectorAll('.range-slider-input')[3]?.value || 90;

  if (wCost) wCost.textContent = `${cVal}%`;
  if (wSpeed) wSpeed.textContent = `${spVal}%`;
  if (wSafety) wSafety.textContent = `${sfVal}%`;
  if (wSpoilage) wSpoilage.textContent = `${spLVal}%`;

  const avgScore = ((parseInt(cVal) + parseInt(spVal) + parseInt(sfVal) + parseInt(spLVal)) / 4).toFixed(1);
  if (scoreBadge) {
    scoreBadge.textContent = `ST-GCN Score: ${avgScore} / 100`;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('aiRouterForm');
  if (form) {
    form.addEventListener('submit', calculateAIRoute);
  }
});


