/* Резюмирую.рф — общий рантайм каркаса (тема, аватар, мелочи) */
(function () {
  'use strict';

  // ── Тема ──────────────────────────────────────────────────────────────
  // Начальное значение ставится встроенным скриптом в <head> (до отрисовки),
  // здесь — переключение по кнопке и синхронизация иконки.
  function currentTheme() {
    return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  }
  function syncThemeIcon() {
    var btn = document.getElementById('theme-btn');
    if (btn) btn.textContent = currentTheme() === 'light' ? '☀️' : '🌙';
  }
  function toggleTheme() {
    var next = currentTheme() === 'light' ? 'dark' : 'light';
    if (next === 'light') document.documentElement.setAttribute('data-theme', 'light');
    else document.documentElement.removeAttribute('data-theme');
    try { localStorage.setItem('theme', next); } catch (e) {}
    syncThemeIcon();
  }

  // ── Аватар пользователя ───────────────────────────────────────────────
  function initials(name) {
    var parts = String(name || '').trim().split(/\s+/).filter(Boolean);
    if (!parts.length) return 'U';
    if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
    return (parts[0][0] + parts[1][0]).toUpperCase();
  }
  function setAvatar(user) {
    var el = document.getElementById('app-avatar');
    if (!el || !user) return;
    if (user.photo) {
      el.innerHTML = '<img src="' + user.photo + '" alt="">';
    } else {
      el.textContent = initials(user.name || user.email);
    }
  }

  function init() {
    syncThemeIcon();
    var btn = document.getElementById('theme-btn');
    if (btn) btn.addEventListener('click', toggleTheme);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.App = { toggleTheme: toggleTheme, setAvatar: setAvatar, initials: initials };
})();
