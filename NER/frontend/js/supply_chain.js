/* ============================================================================
   NE-AURA (SIH 26002) - SUPPLY CHAIN & SPOILAGE PAGE SCRIPT
   Page-specific logic for supply_chain.html (Arrhenius PINN Calculator & Accessibility charts)
   ============================================================================ */

function initArrheniusCalculator() {
  const select = document.getElementById('arrhenius-commodity-select');
  const tempInput = document.getElementById('arrhenius-temp-input');
  const humidityInput = document.getElementById('arrhenius-humidity-input');
  const vibeInput = document.getElementById('arrhenius-vibe-input');

  if (!select || !tempInput) return;

  const update = () => {
    const comName = select.value;
    const tempC = parseFloat(tempInput.value) || 28;
    const humidityPct = parseFloat(humidityInput?.value || 75);
    const vibeG = parseFloat(vibeInput?.value || 0.8);

    const com = ARRHENIUS_COMMODITIES.find(c => c.name === comName) || ARRHENIUS_COMMODITIES[0];

    const R = 8.314;
    const refK = com.refTemp + 273.15;
    const tempK = tempC + 273.15;
    const exponent = (com.Ea * 1000 / R) * ((1 / refK) - (1 / tempK));
    
    // Multi-factor kinetic degradation equation: k(T, H, v)
    const baseThermalRate = Math.exp(exponent);
    const humidityFactor = 1 + 0.004 * Math.max(0, humidityPct - 60);
    const vibeFactor = 1 + 0.15 * Math.max(0, vibeG - 0.5);
    
    const totalSpoilageFactor = baseThermalRate * humidityFactor * vibeFactor;
    const adjustedLifeHours = Math.max(1, (com.baseHours / totalSpoilageFactor).toFixed(1));

    const shelfRes = document.getElementById('arrhenius-shelf-result');
    const factorRes = document.getElementById('arrhenius-factor-result');
    const idealRes = document.getElementById('arrhenius-ideal-storage');

    if (shelfRes) shelfRes.innerText = `${adjustedLifeHours} Hours`;
    if (factorRes) factorRes.innerText = `${totalSpoilageFactor.toFixed(2)}x Kinetic Rate Multiplier`;
    if (idealRes) idealRes.innerText = com.storage;
  };

  select.addEventListener('change', update);
  tempInput.addEventListener('input', update);
  if (humidityInput) humidityInput.addEventListener('input', update);
  if (vibeInput) vibeInput.addEventListener('input', update);

  update();
}

function dispatchReeferPod() {
  showToast('❄️ Emergency Reefer Cryo-Pod Dispatched to Pandu River Hub (ETA 25 mins)');
}

document.addEventListener('DOMContentLoaded', () => {
  initArrheniusCalculator();
});

