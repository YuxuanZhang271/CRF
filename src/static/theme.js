// Apply the preference before the stylesheet paints; theme changes never send commands.
(() => {
  const root = document.documentElement;
  const preferenceKey = 'abml-color-theme';
  const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
  let preferredTheme;
  try {
    preferredTheme = localStorage.getItem(preferenceKey);
  } catch (_) { /* Color switching also works when browser storage is blocked. */ }
  function setPreference(value) {
    preferredTheme = ['light', 'dark'].includes(value) ? value : null;
    applyTheme(preferredTheme || (systemTheme.matches ? 'dark' : 'light'));
  }

  function applyTheme(theme) {
    root.dataset.theme = theme;
    const button = document.getElementById('theme-toggle');
    if (button) {
      button.setAttribute('aria-pressed', String(theme === 'light'));
      button.title = `Switch to ${theme === 'light' ? 'dark' : 'light'} mode`;
    }
  }

  setPreference(preferredTheme);
  document.addEventListener('DOMContentLoaded', () => {
    applyTheme(root.dataset.theme);
    document.getElementById('theme-toggle')?.addEventListener('click', () => {
      preferredTheme = root.dataset.theme === 'light' ? 'dark' : 'light';
      applyTheme(preferredTheme);
      try {
        localStorage.setItem(preferenceKey, preferredTheme);
      } catch (_) { /* Keep the chosen mode for this page even without persistence. */ }
    });
  });
  window.addEventListener('storage', event => {
    if (event.storageArea === localStorage && (event.key === preferenceKey || event.key === null)) {
      setPreference(event.newValue);
    }
  });
  systemTheme.addEventListener('change', event => {
    if (!preferredTheme) applyTheme(event.matches ? 'dark' : 'light');
  });
})();
