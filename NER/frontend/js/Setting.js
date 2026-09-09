/* ============================================================================
   NE-AURA (SIH 26002) - PLATFORM SETTINGS PAGE SCRIPT
   Page-specific logic for Setting.html (Theme mode toggle, language selector, cache reset)
   ============================================================================ */

function handleLanguageChange(e) {
  if (e && e.target) {
    changeLanguage(e.target.value);
  }
}

function resetPlatformSettings() {
  if (confirm('Are you sure you want to reset all platform preferences to default?')) {
    localStorage.clear();
    showToast('⚙️ Platform settings reset to default.');
    setTimeout(() => { window.location.href = 'index.html'; }, 1000);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const langSelect = document.getElementById('langSelect');
  if (langSelect) {
    langSelect.addEventListener('change', handleLanguageChange);
  }
});
