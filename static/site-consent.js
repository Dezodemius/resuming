/* Global consent manager for optional analytics and browser preferences. */
(function () {
  'use strict';

  var banner = document.getElementById('site-consent-banner');
  if (!banner) return;

  var state = banner.dataset.state === 'analytics' || banner.dataset.state === 'necessary'
    ? banner.dataset.state : 'unknown';
  var metrikaId = banner.dataset.metrikaId;
  var status = banner.querySelector('.site-consent__status');
  var current = banner.querySelector('.site-consent__current');
  var closeButton = banner.querySelector('[data-site-consent-close]');
  var metrikaLoaded = false;
  var optionalStorageKeys = ['theme', 'lib_view'];
  var optionalConsentStorageKeys = ['ai_consent_rev'];
  var metrikaStorageKeys = ['yandex_metrika_callbacks2'];
  var metrikaCookieKeys = ['yandexuid', 'yuidss', 'ymex', 'yp', 'bh', 'is_gdpr'];

  function allows(category) {
    return state === 'analytics' && (category === 'analytics' || category === 'preferences');
  }

  function preferenceGet(key, fallback) {
    if (!allows('preferences') || optionalStorageKeys.indexOf(key) === -1) return fallback;
    try { return localStorage.getItem(key) || fallback; } catch (e) { return fallback; }
  }

  function preferenceSet(key, value) {
    if (!allows('preferences') || optionalStorageKeys.indexOf(key) === -1) return;
    try { localStorage.setItem(key, value); } catch (e) { /* private mode */ }
  }

  function isMetrikaKey(key) {
    return key.indexOf('_ym') === 0 || metrikaStorageKeys.indexOf(key) !== -1;
  }

  function expireCookie(name) {
    var parts = location.hostname.split('.');
    var domains = [''];
    for (var i = 0; i < parts.length - 1; i += 1) {
      var domain = parts.slice(i).join('.');
      domains.push(domain, '.' + domain);
    }
    domains.forEach(function (domain) {
      document.cookie = name + '=; Max-Age=0; Path=/; SameSite=Lax' +
        (domain ? '; Domain=' + domain : '');
    });
  }

  function clearOptionalState() {
    try {
      for (var i = localStorage.length - 1; i >= 0; i -= 1) {
        var key = localStorage.key(i);
        if (optionalStorageKeys.indexOf(key) !== -1 || optionalConsentStorageKeys.indexOf(key) !== -1 || isMetrikaKey(key)) localStorage.removeItem(key);
      }
    } catch (e) { /* storage may be unavailable */ }
    var cookies = document.cookie ? document.cookie.split(';') : [];
    cookies.forEach(function (item) {
      var name = item.split('=', 1)[0].trim();
      if (name.indexOf('_ym') === 0 || metrikaCookieKeys.indexOf(name) !== -1) expireCookie(name);
    });
    document.querySelectorAll('script[data-site-consent-metrika]').forEach(function (node) { node.remove(); });
    try { delete window.ym; } catch (e) { window.ym = undefined; }
  }

  function loadMetrika() {
    if (!allows('analytics') || !metrikaId || metrikaLoaded || document.querySelector('script[data-site-consent-metrika]')) return;
    metrikaLoaded = true;
    // Очередь создаётся только после явного выбора. Иначе даже вызовы целей,
    // случившиеся до загрузки tag.js, не попадут в Метрику.
    window.ym = window.ym || function () {
      (window.ym.a = window.ym.a || []).push(arguments);
    };
    window.ym.l = Date.now();
    var script = document.createElement('script');
    script.async = true;
    script.dataset.siteConsentMetrika = 'true';
    script.src = 'https://mc.yandex.ru/metrika/tag.js?id=' + encodeURIComponent(metrikaId);
    window.ym(Number(metrikaId), 'init', {
      ssr: true, webvisor: true, clickmap: true, ecommerce: 'dataLayer',
      accurateTrackBounce: true, trackLinks: true
    });
    document.head.appendChild(script);
  }

  function render() {
    banner.hidden = state !== 'unknown';
    if (closeButton) closeButton.hidden = state === 'unknown';
    if (current) {
      current.textContent = state === 'analytics'
        ? 'Сейчас аналитика разрешена. Вы можете изменить выбор.'
        : state === 'necessary'
          ? 'Сейчас включены только необходимые cookie. Вы можете изменить выбор.'
          : '';
    }
    if (allows('analytics')) {
      var theme = preferenceGet('theme', 'dark');
      if (theme === 'light') document.documentElement.setAttribute('data-theme', 'light');
      loadMetrika();
    }
  }

  function choose(choice) {
    if (choice !== 'analytics' && choice !== 'necessary') return Promise.resolve(false);
    status.textContent = '';
    banner.querySelectorAll('button[data-site-consent-choice]').forEach(function (button) { button.disabled = true; });
    return fetch('/api/site-consent', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({choice: choice})
    }).then(function (res) {
      if (!res.ok) throw new Error('save_failed');
      return res.json();
    }).then(function (body) {
      var wasAnalytics = state === 'analytics';
      state = body.choice === 'analytics' ? 'analytics' : 'necessary';
      if (state === 'necessary') clearOptionalState();
      render();
      window.dispatchEvent(new CustomEvent('siteconsentchange', {detail: {choice: state, rev: body.rev || ''}}));
      // Уже загруженный сторонний счётчик нельзя надёжно «выгрузить» из
      // страницы. После отзыва начинаем чистую страницу без него.
      if (wasAnalytics && state === 'necessary') window.location.reload();
      return true;
    }).catch(function () {
      status.textContent = 'Не удалось сохранить выбор. Попробуйте ещё раз.';
      return false;
    }).finally(function () {
      banner.querySelectorAll('button[data-site-consent-choice]').forEach(function (button) { button.disabled = false; });
    });
  }

  banner.querySelectorAll('button[data-site-consent-choice]').forEach(function (button) {
    button.addEventListener('click', function () { choose(button.dataset.siteConsentChoice); });
  });
  if (closeButton) closeButton.addEventListener('click', function () { banner.hidden = true; });
  document.addEventListener('click', function (event) {
    if (event.target.closest('[data-open-site-consent]')) {
      event.preventDefault();
      banner.hidden = false;
      var first = banner.querySelector('[data-site-consent-choice]');
      if (first) first.focus();
    }
  });

  window.SiteConsent = {
    rev: function () { return banner.dataset.rev || ''; },
    allows: allows,
    choose: choose,
    open: function () { banner.hidden = false; },
    close: function () { if (state !== 'unknown') banner.hidden = true; },
    state: function () { return state; },
    preferenceGet: preferenceGet,
    preferenceSet: preferenceSet,
    clearOptionalState: clearOptionalState
  };
  if (state === 'necessary') clearOptionalState();
  render();
})();
