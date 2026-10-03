'use strict';
const ThemeUI = (() => {
  const key = 'biandengbao:appearance';
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  const valid = value => ['system', 'light', 'dark'].includes(value);
  let preference = 'system';
  try { const saved = localStorage.getItem(key); if (valid(saved)) preference = saved; } catch {}
  function apply() {
    const theme = preference === 'system' ? media.matches ? 'dark' : 'light' : preference;
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    document.querySelector('meta[name=theme-color]')?.setAttribute('content', theme === 'dark' ? '#212121' : '#ffffff');
    document.querySelectorAll('[name=appearance]').forEach(input => { input.checked = input.value === preference; });
    document.querySelectorAll('[data-appearance-button]').forEach(button => {
      const label = '外观：' + ({ system: '跟随系统', light: '浅色', dark: '深色' })[preference];
      button.title = label; button.setAttribute('aria-label', label);
    });
  }
  function set(value) {
    if (!valid(value)) return;
    preference = value;
    try { localStorage.setItem(key, value); } catch {}
    apply();
  }
  media.addEventListener('change', () => { if (preference === 'system') apply(); });
  window.addEventListener('storage', event => {
    if (event.key !== key && event.key !== null) return;
    preference = valid(event.newValue) ? event.newValue : 'system';
    apply();
  });
  apply();
  return { set, apply, preference: () => preference };
})();
