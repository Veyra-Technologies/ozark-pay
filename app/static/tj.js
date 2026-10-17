/* Administrative tariff inputs only; no financial rates/calculations in the UI. */
(() => {
  'use strict';
  document.querySelectorAll('[data-aggregator-country]').forEach(country => {
    const form=country.closest('form');
    const tariff=form.querySelector('[data-agg-tariff]');
    const sync=reset => {
      const tj=country.value==='TJ';
      form.querySelectorAll('[data-agg-currency]').forEach(label => {label.textContent=tj?'TJS':'RUB';});
      tariff.hidden=!tj;
      tariff.querySelector('select').disabled=!tj;
      tariff.querySelector('select').required=tj;
      if(reset){
        form.elements.min_payment_amount.value=tj?'10':'100';
        form.elements.max_payment_amount.value=tj?'':'150000';
        form.elements.commission_percent.value=tj?'':'0';
        window.OzarkSyncMoneyInputs?.(form);
      }
    };
    country.addEventListener('change',()=>sync(true));sync(false);
  });
  document.querySelectorAll('[data-tj-tariff]').forEach((form, formIndex) => {
    let tierSequence = 0;
    const date = new Date();
    date.setMinutes(date.getMinutes()-date.getTimezoneOffset());
    form.elements.effective_from.value = date.toISOString().slice(0,16);
    const rows = form.querySelector('[data-tj-tier-rows]');
    form.querySelector('[data-tj-add]').addEventListener('click', () => {
      const row = rows.firstElementChild.cloneNode(true);
      // Presentation replaces numeric inputs with a hidden value and an editor.
      // Cloning those editors would lose their input listeners.
      row.querySelectorAll('[data-money-view]').forEach(input => input.remove());
      row.querySelectorAll('[data-money-original]').forEach(input => {
        input.type = 'number'; input.removeAttribute('data-money-original');
      });
      const sequence = ++tierSequence;
      row.querySelectorAll('input').forEach(input => {
        input.value = '';
        input.id = `tj-tier-${formIndex}-${sequence}-${input.name}`;
        const label = input.parentElement.querySelector('label');
        if (label) label.htmlFor = input.id;
      });
      rows.append(row);
      window.OzarkEnhance?.(row);
    });
    rows.addEventListener('click', event => {
      if (event.target.closest('[data-tj-remove]') && rows.children.length > 1)
        event.target.closest('[data-tj-tier]').remove();
    });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const result = form.querySelector('[data-tj-result]');
      const button = form.querySelector('[type=submit]');
      button.disabled = true;
      try {
        const payload = {party:form.elements.party.value, entity_id:form.elements.entity_id.value || null,
          effective_from:new Date(form.elements.effective_from.value).toISOString(),
          tiers:[...rows.children].map(row => ({min:row.querySelector('[name=minimum]').value,
            max:row.querySelector('[name=maximum]').value || null, percent:row.querySelector('[name=percent]').value}))};
        const response = await fetch(form.dataset.url,{method:'POST',credentials:'same-origin',
          headers:{'Content-Type':'application/json','X-CSRF-Token':form.elements.csrf_token.value},body:JSON.stringify(payload)});
        const data = await response.json();
        if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Не удалось сохранить тариф.');
        result.textContent = `Сохранена версия ${data.version}. Приём TJ остаётся закрытым. Обновите страницу для выбора тарифа.`;
      } catch (error) { result.textContent = error.message; }
      finally { button.disabled = false; }
    });
  });
})();
