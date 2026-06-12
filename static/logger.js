/* Резюмирую.рф — клиентский логгер.
 * Все fetch-запросы автоматически пишутся в консоль браузера:
 *   [api#N] → METHOD url            — запрос ушёл
 *   [api#N] ← METHOD url STATUS ms  — ответ получен
 *   [api#N] ✗ METHOD url ms         — сетевая ошибка
 * Плюс window.appLog(op, phase, extra) для логов операций уровня UI.
 */
(function () {
  'use strict';

  var ts = function () { return new Date().toISOString().slice(11, 23); };

  window.appLog = function (op, phase, extra) {
    var line = '[' + ts() + '] [' + op + '] ' + phase;
    if (extra !== undefined) console.info(line, extra);
    else console.info(line);
  };
  window.appError = function (op, msg, extra) {
    var line = '[' + ts() + '] [' + op + '] ОШИБКА: ' + msg;
    if (extra !== undefined) console.error(line, extra);
    else console.error(line);
  };

  var origFetch = window.fetch.bind(window);
  var seq = 0;
  window.fetch = function (input, init) {
    var id = ++seq;
    var method = (init && init.method) || 'GET';
    var url = typeof input === 'string' ? input : (input && input.url) || String(input);
    var t0 = performance.now();
    console.info('[' + ts() + '] [api#' + id + '] → ' + method + ' ' + url);
    return origFetch(input, init).then(function (res) {
      var ms = Math.round(performance.now() - t0);
      var line = '[' + ts() + '] [api#' + id + '] ← ' + method + ' ' + url + ' ' + res.status + ' (' + ms + 'ms)';
      if (res.ok) console.info(line);
      else console.warn(line);
      return res;
    }, function (err) {
      var ms = Math.round(performance.now() - t0);
      console.error('[' + ts() + '] [api#' + id + '] ✗ ' + method + ' ' + url + ' (' + ms + 'ms)', err);
      throw err;
    });
  };

  window.addEventListener('error', function (e) {
    console.error('[' + ts() + '] [js] необработанная ошибка:', e.message, e.filename + ':' + e.lineno);
  });
  window.addEventListener('unhandledrejection', function (e) {
    console.error('[' + ts() + '] [js] необработанный promise rejection:', e.reason);
  });

  console.info('[' + ts() + '] [logger] клиентский логгер активен — все API-запросы логируются');
})();
