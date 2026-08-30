/* Безопасная отправка подписанных сервером полей на платёжную страницу Robokassa. */
(function () {
  'use strict';

  var ROBOKASSA_ACTION = 'https://auth.robokassa.ru/Merchant/Index.aspx';

  function submitRobokassaPayment(payload) {
    if (!payload || payload.action !== ROBOKASSA_ACTION || payload.method !== 'POST') {
      throw new Error('Некорректный адрес платёжной формы');
    }
    if (!payload.fields || typeof payload.fields !== 'object' || Array.isArray(payload.fields)) {
      throw new Error('Не получены данные платёжной формы');
    }
    ['MerchantLogin', 'OutSum', 'InvId', 'Description', 'SignatureValue', 'Receipt'].forEach(function (name) {
      if (payload.fields[name] === null || payload.fields[name] === undefined || payload.fields[name] === '') {
        throw new Error('Платёжная форма заполнена не полностью');
      }
    });

    var form = document.createElement('form');
    form.method = 'POST';
    form.action = ROBOKASSA_ACTION;
    form.acceptCharset = 'UTF-8';
    form.style.display = 'none';

    Object.keys(payload.fields).forEach(function (name) {
      var value = payload.fields[name];
      if (value === null || value === undefined) return;
      var input = document.createElement('input');
      input.type = 'hidden';
      input.name = name;
      input.value = String(value);
      form.appendChild(input);
    });

    document.body.appendChild(form);
    form.submit();
  }

  window.submitRobokassaPayment = submitRobokassaPayment;
})();
