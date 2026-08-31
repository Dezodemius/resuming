/* Согласие на передачу персональных данных AI-провайдеру (ст. 12 152-ФЗ).
 *
 * Спрашивается один раз — перед первой генерацией, а не при входе: до неё
 * данные никуда не уходят. Ту же проверку делает сервер (403
 * consent_required), здесь только способ согласие дать: галочка защищает
 * лишь от честного браузера, данные наружу уносит ручка.
 *
 * Одна реализация на генератор и редактор: расхождение между страницами
 * означало бы два разных согласия вместо одного.
 *
 * Публичный интерфейс:
 *   AiConsent.ensure()  → Promise<bool>: true — согласие есть (было или дано
 *                         сейчас), false — модалку закрыли, начинать нельзя;
 *   AiConsent.reset()   → забыть отметку (сервер отказал вопреки ей);
 *   AiConsent.refused(res) → true, если это отказ по согласию.
 */
(function () {
  var modal = document.getElementById('modal-consent');
  if (!modal) return;

  var REV = modal.dataset.rev || '';
  var ANON_KEY = 'ai_consent_rev';
  var box = document.getElementById('consent-box');
  var btn = document.getElementById('btn-consent');
  var resolveWaiting = null;
  // null — ещё не спрашивали сервер; true/false — известное состояние.
  var known = null;
  var authed = false;

  function anonMark() {
    // У анонима аккаунта нет — отметка живёт в браузере. Приватный режим её
    // запрещает: тогда спросим ещё раз, а не пропустим молча.
    try { return localStorage.getItem(ANON_KEY) === REV; } catch (e) { return false; }
  }

  function saveAnonMark() {
    try { localStorage.setItem(ANON_KEY, REV); } catch (e) { /* приватный режим */ }
  }

  // Состояние берём у сервера: он единственный знает и про редакцию, и про
  // отметку зарегистрированного. Ответ кешируем на страницу.
  async function state() {
    if (known !== null) return known;
    try {
      var me = await (await fetch('/api/me')).json();
      authed = !!me.authenticated;
      known = authed ? !!me.ai_consent : anonMark();
    } catch (e) {
      known = false;   // не знаем — значит спросим, а не пропустим
    }
    return known;
  }

  function open() {
    box.checked = false;
    btn.disabled = true;
    modal.classList.add('on');
    return new Promise(function (resolve) { resolveWaiting = resolve; });
  }

  function finish(ok) {
    modal.classList.remove('on');
    var resolve = resolveWaiting;
    resolveWaiting = null;
    if (resolve) resolve(ok);
  }

  async function accept() {
    if (!box.checked) return;
    btn.disabled = true;
    if (!authed) {
      // У анонима согласие некуда записать на сервере — оно едет в самом
      // запросе генерации, а браузер помнит, что спрашивать больше не нужно.
      saveAnonMark();
      known = true;
      finish(true);
      return;
    }

    var res = null;
    try {
      res = await fetch('/api/consent', { method: 'POST' });
    } catch (e) { /* сеть — разбираем ниже вместе с ответом сервера */ }

    if (res && res.ok) {
      known = true;                       // согласие зарегистрированного хранит сервер
    } else {
      btn.disabled = false;
      if (window.toast) window.toast('Не удалось сохранить согласие — попробуйте ещё раз', 'err');
      return;
    }
    finish(true);
  }

  box.addEventListener('change', function () { btn.disabled = !box.checked; });
  btn.addEventListener('click', accept);
  Array.prototype.forEach.call(modal.querySelectorAll('[data-consent-close]'), function (el) {
    el.addEventListener('click', function () { finish(false); });
  });

  window.AiConsent = {
    rev: REV,
    ensure: async function () {
      if (await state()) return true;
      return open();
    },
    reset: function () {
      known = null;
      try { localStorage.removeItem(ANON_KEY); } catch (e) { /* приватный режим */ }
    },
    refused: function (res, body) {
      return res.status === 403 && body && body.error === 'consent_required';
    },
  };
})();
