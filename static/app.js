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
    if (window.SiteConsent) window.SiteConsent.preferenceSet('theme', next);
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
    // Фото брать неоткуда: вход по почте и OAuth аватарку не отдают.
    el.textContent = initials(user.name || user.email);
  }

  // PDF-runtime приходит с внешнего CDN только после явного нажатия на
  // экспорт. Открытие страницы само по себе не создаёт внешнего запроса.
  var html2PdfPromise = null;
  function loadHtml2Pdf() {
    if (typeof window.html2pdf === 'function') return Promise.resolve(window.html2pdf);
    if (html2PdfPromise) return html2PdfPromise;
    html2PdfPromise = new Promise(function (resolve, reject) {
      var script = document.createElement('script');
      var timeout = setTimeout(function () {
        html2PdfPromise = null;
        script.remove();
        reject(new Error('Не удалось загрузить модуль PDF'));
      }, 20000);
      script.async = true;
      script.dataset.pdfRuntime = 'true';
      script.src = 'https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.1/html2pdf.bundle.min.js';
      script.onload = function () {
        clearTimeout(timeout);
        if (typeof window.html2pdf === 'function') resolve(window.html2pdf);
        else {
          html2PdfPromise = null;
          reject(new Error('Модуль PDF загрузился некорректно'));
        }
      };
      script.onerror = function () {
        clearTimeout(timeout);
        html2PdfPromise = null;
        reject(new Error('Не удалось загрузить модуль PDF'));
      };
      document.head.appendChild(script);
    });
    return html2PdfPromise;
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

  window.App = {
    toggleTheme: toggleTheme,
    setAvatar: setAvatar,
    initials: initials,
    loadHtml2Pdf: loadHtml2Pdf
  };
})();
