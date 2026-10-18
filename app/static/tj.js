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
        result.textContent = `Сохранена версия ${data.version}. Приём TJ остаётся закрытым. Обновите страницу для просмотра индивидуальных условий.`;
      } catch (error) { result.textContent = error.message; }
      finally { button.disabled = false; }
    });
  });
})();

/* Personal grids are submitted inside the existing cabinet creation form. */
(() => {
  document.querySelectorAll('form[action$="/users/create"]').forEach(form => {
    const box = form.querySelector('[data-tj-inline]');
    if (!box) return;
    const rows = box.querySelector('[data-inline-tiers]');
    let inlineSequence = 0;
    const role = form.elements.role;
    const countryField = () => role.value === 'trader' ? form.elements.working_country : form.elements.merchant_country;
    let previous = '';
    const sync = () => {
      const merchantCountry = form.elements.merchant_country;
      const business = form.querySelector('[data-merchant-business]');
      if (business) {
        business.hidden = role.value === 'aggregator';
        form.elements.business_id.disabled = role.value !== 'merchant';
      }
      if (merchantCountry) {
        [...merchantCountry.options].forEach(option => {
          const excluded = role.value === 'aggregator' && option.value && !['RU','TJ'].includes(option.value);
          option.disabled = Boolean(excluded);option.hidden = Boolean(excluded);
        });
        if (merchantCountry.selectedOptions[0]?.disabled) {
          merchantCountry.value='';merchantCountry.dispatchEvent(new Event('change',{bubbles:true}));
        }
      }
      const country = countryField();
      const isTj = ['merchant','trader','aggregator'].includes(role.value) && country?.value === 'TJ';
      box.hidden = !isTj;
      box.querySelectorAll('input,button').forEach(input => { input.disabled = !isTj; });
      form.elements.tj_parser_confirmed.required = isTj;
      const plan = form.querySelector('[data-merchant-plan]');
      if (plan && isTj) {plan.disabled = true; plan.required = false;}
      const key = `${role.value}:${country?.value || ''}`;
      if (key !== previous) {
        if (role.value === 'aggregator') {
          form.elements.agg_min.value=isTj?'10':'100';
          form.elements.agg_max.value=isTj?'':'150000';
          form.elements.agg_min.placeholder=form.elements.agg_max.placeholder=isTj?'TJS':'RUB';
        }
        form.elements.tj_parser_confirmed.checked = false;
        [...rows.children].slice(1).forEach(row => row.remove());
        rows.querySelector('[data-tier-min]').value = '10';
        rows.querySelectorAll('[data-tier-percent],[data-tier-max]').forEach(field => {field.value='';});
        form.elements.tj_tiers.value='[]';
        const now=new Date();now.setMinutes(now.getMinutes()-now.getTimezoneOffset());
        box.querySelector('[data-inline-date]').value=now.toISOString().slice(0,16);
        window.OzarkSyncMoneyInputs?.(box);
        previous=key;
      }
    };
    form.addEventListener('change', event => {
      if (event.target.matches('[name=role],[data-work-country],[data-merchant-country]')) queueMicrotask(sync);
    });
    box.querySelector('[data-inline-add]').addEventListener('click', () => {
      const row=rows.firstElementChild.cloneNode(true);
      const sequence = ++inlineSequence;
      row.querySelectorAll('[data-money-view]').forEach(field => field.remove());
      row.querySelectorAll('input').forEach(field => {
        field.type='number';field.removeAttribute('data-money-original');field.value='';
        field.id=`tj-inline-${sequence}-${field.hasAttribute('data-tier-min')?'min':field.hasAttribute('data-tier-max')?'max':'percent'}`;
        const label=field.parentElement.querySelector('label');if(label)label.htmlFor=field.id;
      });
      rows.append(row);window.OzarkEnhance?.(row);
    });
    box.addEventListener('click', event => {
      const button=event.target.closest('[data-inline-remove]');
      if (button && rows.children.length>1) button.closest('[data-inline-tier]').remove();
    });
    form.addEventListener('submit', event => {
      if (box.hidden) return;
      const tiers=[...rows.children].map(row => ({min:row.querySelector('[data-tier-min]').value,
        max:row.querySelector('[data-tier-max]').value || null,percent:row.querySelector('[data-tier-percent]').value}));
      if (tiers.some(tier => tier.percent !== '') && tiers.some(tier => tier.percent === '' || tier.min === '')) {
        event.preventDefault();alert('Заполните комиссию и нижнюю границу каждого диапазона.');return;
      }
      form.elements.tj_tiers.value=JSON.stringify(tiers.every(tier=>tier.percent==='') ? [] : tiers);
      const date=box.querySelector('[data-inline-date]').value;
      if (tiers.some(tier=>tier.percent!=='' ) && !date) {event.preventDefault();alert('Укажите начало действия тарифа.');return;}
      form.elements.tj_effective_from.value=date ? new Date(date).toISOString() : '';
    });
    sync();
  });
})();
