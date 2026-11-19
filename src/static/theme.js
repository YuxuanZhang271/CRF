// Apply the preference before the stylesheet paints; theme changes never send commands.
(() => {
  const root = document.documentElement;
  const preferenceKey = 'abml-color-theme';
  const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
  let preferredTheme;
  try {
    preferredTheme = localStorage.getItem(preferenceKey);
  } catch (_) { /* Color switching also works when browser storage is blocked. */ }
  if (!['light', 'dark'].includes(preferredTheme)) preferredTheme = null;

  function applyTheme(theme) {
    root.dataset.theme = theme;
    const button = document.getElementById('theme-toggle');
    if (button) {
      button.setAttribute('aria-pressed', String(theme === 'light'));
      button.title = `Switch to ${theme === 'light' ? 'dark' : 'light'} mode`;
    }
  }

  applyTheme(preferredTheme || (systemTheme.matches ? 'dark' : 'light'));
  document.addEventListener('DOMContentLoaded', () => {
    applyTheme(root.dataset.theme);
    document.getElementById('theme-toggle').addEventListener('click', () => {
      preferredTheme = root.dataset.theme === 'light' ? 'dark' : 'light';
      applyTheme(preferredTheme);
      try {
        localStorage.setItem(preferenceKey, preferredTheme);
      } catch (_) { /* Keep the chosen mode for this page even without persistence. */ }
    });
  });
  systemTheme.addEventListener('change', event => {
    if (!preferredTheme) applyTheme(event.matches ? 'dark' : 'light');
  });
})();
