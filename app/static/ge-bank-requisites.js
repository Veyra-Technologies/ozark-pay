/* Identifier validation only: no exchange rate, tariff or money calculation. */
(() => {
  'use strict';
  document.querySelectorAll('form[data-ge-requisite]').forEach(form => {
    const method = form.elements.method;
    const input = form.elements.value;
    const suffix = form.elements.last4;
    const label = form.querySelector('[data-requisite-label]');
    const hint = form.querySelector('[data-requisite-hint]');
    const bank = form.querySelector('[data-local-bank-autocomplete]');
    let previous = method.value;
    let methodVersion = 0;
    const normalized = () => method.value === 'bank_account'
      ? input.value.replace(/\s/g, '').toUpperCase() : input.value.replace(/[\s-]/g, '');
    const checksum = value => {
      let total = 0;
      for (let i = value.length - 1, twice = false; i >= 0; i--, twice = !twice) {
        let digit = Number(value[i]);
        if (twice) { digit *= 2; if (digit > 9) digit -= 9; }
        total += digit;
      }
      return total % 10 === 0;
    };
    const validIban = value => {
      if (!/^GE[0-9]{2}[A-Z]{2}[0-9]{16}$/.test(value)) return false;
      let remainder = 0;
      for (const char of value.slice(4) + value.slice(0, 4)) {
        const digits = /[A-Z]/.test(char) ? String(char.charCodeAt(0) - 55) : char;
        for (const digit of digits) remainder = (remainder * 10 + Number(digit)) % 97;
      }
      return remainder === 1;
    };
    const validate = () => {
      const value = normalized();
      suffix.value = /^[0-9]{4}$/.test(value.slice(-4)) ? value.slice(-4) : '';
      input.setCustomValidity('');
      if (!value && !input.required) return;
      const valid = method.value === 'bank_account' ? validIban(value)
        : /^[0-9]{13,19}$/.test(value) && new Set(value).size > 1 && checksum(value);
      if (!valid) input.setCustomValidity(method.value === 'bank_account'
        ? 'Введите корректный грузинский IBAN (22 символа, GE и контрольные цифры)'
        : 'Введите корректный номер карты (13–19 цифр и контрольная цифра)');
    };
    const update = () => {
      const account = method.value === 'bank_account';
      if (previous !== method.value) {
        methodVersion++;
        input.value = ''; suffix.value = ''; input.required = true;
      }
      previous = method.value;
      label.textContent = account ? 'Номер банковского счёта' : 'Номер банковской карты';
      input.placeholder = account ? 'Введите номер банковского счёта / IBAN' : 'Введите номер карты';
      input.inputMode = account ? 'text' : 'numeric';
      input.setAttribute('autocapitalize', account ? 'characters' : 'off');
      hint.textContent = account ? 'Грузинский IBAN: 22 символа. Код банка должен соответствовать выбранному банку.'
        : '13–19 цифр. Пробелы и дефисы допустимы. Контрольная цифра проверяется автоматически.';
      validate();
    };
    method.addEventListener('change', update);
    input.addEventListener('input', validate);
    bank?.addEventListener('input', () => { input.setCustomValidity(''); validate(); });
    form.addEventListener('submit', event => {
      validate();
      if (!form.checkValidity()) { event.preventDefault(); form.reportValidity(); }
      else if (input.value) input.value = normalized();
    });
    update();
    if (form.dataset.geDetailsUrl) {
      const modal = form.closest('.modal');
      const button = document.querySelector(`[data-modal-open="${CSS.escape(modal.id)}"]`);
      button?.addEventListener('click', async () => {
        input.value = ''; input.required = false;
        const version = methodVersion;
        try {
          const response = await fetch(form.dataset.geDetailsUrl, {credentials:'same-origin', cache:'no-store',
            headers:{'X-Session-View': form.elements.session_view?.value || ''}});
          if (!response.ok) throw new Error('Не удалось загрузить реквизит');
          const data = await response.json();
          if (version === methodVersion && method.value === data.method) { input.value = data.value; validate(); }
        } catch (error) { hint.textContent = error.message; }
      });
      const close = () => { input.value = ''; input.setCustomValidity(''); };
      modal.querySelectorAll('[data-modal-close]').forEach(button => button.addEventListener('click', close));
    }
  });
})();
