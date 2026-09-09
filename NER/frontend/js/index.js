/* ============================================================================
   NE-AURA (SIH 26002) - EXECUTIVE DASHBOARD PAGE SCRIPT
   Page-specific logic for index.html (KPIs, Alert Stream & GIS Map overview)
   ============================================================================ */

document.addEventListener('DOMContentLoaded', () => {
  if (document.getElementById('gis-map-canvas')) {
    setTimeout(initGisMap, 200);
  }
});
