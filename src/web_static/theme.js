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
