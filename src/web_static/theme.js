// Manual light/dark theme toggle.
// - Follows the OS setting by default (no data-theme attr on <html>).
// - A saved choice in localStorage overrides the OS setting.
// - The header button cycles light <-> dark and persists the choice.
(function () {
  var STORAGE_KEY = 'arena-theme';
  var root = document.documentElement;
  var btn = document.getElementById('theme-toggle');
  var icon = btn ? btn.querySelector('.theme-toggle-icon') : null;

  function systemPrefersLight() {
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
  }

  function currentTheme() {
    var saved = null;
    try { saved = localStorage.getItem(STORAGE_KEY); } catch (e) {}
    if (saved === 'light' || saved === 'dark') return saved;
    return systemPrefersLight() ? 'light' : 'dark';
  }

  function apply(theme) {
    // data-theme="light"/"dark" forces a mode; removing it falls back to OS.
    if (theme === 'light' || theme === 'dark') {
      root.setAttribute('data-theme', theme);
    } else {
      root.removeAttribute('data-theme');
    }
    if (icon) icon.textContent = theme === 'dark' ? '🌙' : '☀️';
    // Notify any theme-aware components (e.g. the rating chart) to re-read
    // their colors, since Chart.js captures CSS vars at build time.
    document.dispatchEvent(new CustomEvent('themechange', { detail: { theme: theme } }));
  }

  function init() {
    var saved = null;
    try { saved = localStorage.getItem(STORAGE_KEY); } catch (e) {}
    if (saved === 'light' || saved === 'dark') {
      apply(saved);
    } else {
      apply(systemPrefersLight() ? 'light' : 'dark');
    }
  }

  if (btn) {
    btn.addEventListener('click', function () {
      var next = currentTheme() === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem(STORAGE_KEY, next); } catch (e) {}
      apply(next);
    });
  }

  // If the user hasn't chosen manually, keep following OS changes live.
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', function (e) {
      var saved = null;
      try { saved = localStorage.getItem(STORAGE_KEY); } catch (err) {}
      if (saved !== 'light' && saved !== 'dark') {
        apply(e.matches ? 'light' : 'dark');
      }
    });
  }

  init();
})();

// Noise strength slider — controls the film-grain overlay opacity.
// Slider value is a percent (0-100); maps to opacity 0-1.0. Persisted.
(function () {
  var STORAGE_KEY = 'arena-noise';
  var DEFAULT_PCT = 50; // 0.50 opacity — user's chosen default
  var slider = document.getElementById('noise-slider');
  var valEl = document.getElementById('noise-val');
  var root = document.documentElement;

  function apply(pct) {
    var opacity = (pct / 100).toFixed(3);
    root.style.setProperty('--noise-opacity', opacity);
    if (valEl) valEl.textContent = pct + '%';
  }

  function init() {
    var saved = null;
    try { saved = localStorage.getItem(STORAGE_KEY); } catch (e) {}
    var pct = DEFAULT_PCT;
    if (saved !== null && !isNaN(parseInt(saved, 10))) {
      pct = Math.max(0, Math.min(100, parseInt(saved, 10)));
    }
    if (slider) slider.value = pct;
    apply(pct);
  }

  if (slider) {
    slider.addEventListener('input', function () {
      var pct = parseInt(slider.value, 10);
      apply(pct);
      try { localStorage.setItem(STORAGE_KEY, String(pct)); } catch (e) {}
    });
  }

  init();
})();
