/* Server owns all exchange-rate, tariff and accounting arithmetic. */
(() => {
  'use strict';
  const parserByCountry = {
    RU: ['rapira', 'Rapira · USDT/RUB · askPrice'],
    KZ: ['binance_p2p_kaspi_buy_100000_avg10', 'Binance P2P · USDT/KZT · Kaspi Bank'],
    GE: ['xe', 'XE · местный справочный курс GEL'],
  };
  document.querySelectorAll('[data-merchant-country-fields]:not([data-merchant-catalog])').forEach(group => {
    const country = group.querySelector('[data-merchant-country]');
    const parser = group.querySelector('[data-merchant-parser]');
    const sync = () => {
      const option = document.createElement('option');
      [option.value, option.textContent] = parserByCountry[country.value] || ['', 'Сначала выберите страну'];
      parser.replaceChildren(option);
    };
    country.addEventListener('change', sync); sync();
  });
  const root = document.querySelector('[data-crossborder-live]');
  if (!root) return;
  document.addEventListener('click', event => {
    const link = event.target.closest('[data-open-operation]');
    if (!link) return;
    const panel = document.getElementById(link.dataset.openOperation);
    if (panel) { event.preventDefault(); panel.open = true; panel.scrollIntoView({block:'center'}); }
  });
  const requestedPanel = document.getElementById(window.location.hash.slice(1));
  if (requestedPanel?.matches('details[id^="cb-"]')) requestedPanel.open = true;
  const base = root.dataset.base;
  const token = root.querySelector('[name=csrf_token]').value;
  const tariff = root.querySelector('[data-live-tariff]');
  function cloneTier(row) {
    const copy = row.cloneNode(true);
    copy.querySelectorAll('[id]').forEach(input => {
      const previous = input.id;
      input.id = `tariff-${crypto.randomUUID()}`;
      copy.querySelectorAll('label').forEach(label => { if (label.htmlFor === previous) label.htmlFor = input.id; });
    });
    return copy;
  }
  function reviewDecision(message) {
    return new Promise(resolve => {
      const dialog = document.createElement('dialog'); dialog.className = 'panel';
      dialog.setAttribute('aria-label', 'Подтверждение действия');
      const text = document.createElement('p'); text.textContent = message;
      const yes = document.createElement('button'); yes.type = 'button'; yes.className = 'primary'; yes.textContent = 'Подтверждаю действие';
      const no = document.createElement('button'); no.type = 'button'; no.className = 'secondary'; no.textContent = 'Вернуться';
      const finish = result => { dialog.close(); dialog.remove(); resolve(result); };
      yes.addEventListener('click', () => finish(true)); no.addEventListener('click', () => finish(false));
      dialog.addEventListener('cancel', event => { event.preventDefault(); finish(false); });
      dialog.append(text, yes, no); root.append(dialog); dialog.showModal(); no.focus();
    });
  }
  const applyFilter = () => {
    const query = root.querySelector('[data-operation-search]')?.value.toLocaleLowerCase() || '';
    const status = root.querySelector('[data-operation-status]')?.value || '';
    root.querySelectorAll('[data-operation-row]').forEach(row => {
      row.hidden = !(row.dataset.search.toLocaleLowerCase().includes(query) && (!status || row.dataset.status === status));
    });
  };
  root.querySelector('[data-operation-search]')?.addEventListener('input', applyFilter);
  root.querySelector('[data-operation-status]')?.addEventListener('change', applyFilter);
  root.querySelectorAll('[data-assignment-owner]').forEach(select => {
    const sync = () => {
      const form = select.form, option = select.selectedOptions[0];
      const country = option?.dataset.country, merchant = select.value.startsWith('merchant:');
      const choices = merchant ? [parserByCountry[country]] : [['binance_p2p_tbc_buy_500_avg10', 'Binance · USDT/GEL · TBC Bank'], parserByCountry.KZ];
      form.elements.parser_id.replaceChildren(...choices.filter(Boolean).map(([value,label]) => new Option(label,value)));
      form.elements.purpose.value = merchant && country !== 'GE' ? 'financial' : 'informational';
    };
    select.addEventListener('change', sync); sync();
  });
  const localTime = () => {
    const value = new Date();
    return new Date(value.getTime() - value.getTimezoneOffset() * 60000).toISOString().slice(0,16);
  };
  // Receipt time is evidence supplied by the participant, not the page-open time.
  root.querySelectorAll('input[type=datetime-local][name=effective_from]').forEach(input => { input.value = localTime(); });
  function show(target, value) {
    const pre = document.createElement('pre');
    pre.textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
    target.replaceChildren(pre);
  }
  function tierRows(tiers) {
    if (!tariff) return;
    const holder = tariff.querySelector('[data-tier-rows]');
    const prototype = cloneTier(holder.querySelector('[data-tier-row]'));
    holder.replaceChildren();
    tiers.forEach(tier => {
      const row = cloneTier(prototype);
      row.querySelector('[data-tier-min]').value = tier.min;
      row.querySelector('[data-tier-max]').value = tier.max || '';
      row.querySelector('[data-tier-percent]').value = tier.percent;
      holder.append(row);
    });
  }
  function filterEntities() {
    if (!tariff) return;
    const selected = tariff.elements.party.value;
    [...tariff.elements.entity_id.options].forEach(option => { option.hidden = Boolean(option.dataset.party && option.dataset.party !== selected); });
    if (tariff.elements.entity_id.selectedOptions[0]?.hidden) tariff.elements.entity_id.value = '';
  }
  if (tariff) {
    filterEntities();
    tariff.elements.party.addEventListener('change', () => {   filterEntities(); });
    tariff.querySelector('[data-tier-add]').addEventListener('click', () => {
      const holder = tariff.querySelector('[data-tier-rows]');
      const last = holder.lastElementChild;
      const next = cloneTier(last);
      next.querySelector('[data-tier-min]').value = last.querySelector('[data-tier-max]').value;
      next.querySelector('[data-tier-max]').value = '';
      holder.append(next);
    });
    tariff.addEventListener('click', event => {
      if (event.target.matches('[data-tier-remove]') && tariff.querySelectorAll('[data-tier-row]').length > 1) event.target.closest('[data-tier-row]').remove();
    });
  }
  root.querySelectorAll('[data-copy-tariff]').forEach(button => button.addEventListener('click', () => {
    const plan = JSON.parse(button.dataset.copyTariff);
    tariff.elements.party.value = plan.party;
    filterEntities();
    tariff.elements.entity_id.value = plan.entity_id || '';
    tariff.elements.is_active.value = String(plan.active);
    tariff.elements.effective_from.value = localTime();
    tierRows(plan.tiers);
    
    
    tariff.closest('details').open = true;
    tariff.scrollIntoView({block:'center'});
  }));
  root.querySelectorAll('[data-live-card]').forEach(button => button.addEventListener('click', async () => {
    const target = button.parentElement.querySelector('[data-live-card-result]');
    try {
      const response = await fetch(`${base}/operations/${encodeURIComponent(button.dataset.liveCard)}/requisite`, {credentials:'same-origin', cache:'no-store'});
      if (!response.ok) throw new Error('Реквизиты недоступны');
      const data = await response.json();
      target.textContent = `${data.bank} · ${data.beneficiary} · ${data.method_label || ''} · ${data.requisite || data.card} · ${data.currency}`;
    } catch (error) { target.textContent = error.message; }
  }));
  root.querySelectorAll('[data-live-action]').forEach(form => {
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const action = form.dataset.liveAction;
      const target = form.querySelector('[data-live-result]');
      const button = form.querySelector('button[type=submit]');
      const payload = Object.fromEntries(new FormData(form));
      if (action === 'assignment' && payload.owner) {
        [payload.owner_type, payload.owner_id] = payload.owner.split(':'); delete payload.owner;
      }
      if (action === 'merchant-settings') { payload.methods = new FormData(form).getAll('methods'); }
      if (action === 'confirm') payload.bank_credit_confirmed = payload.bank_credit_confirmed === 'true';
      if (payload.is_active) payload.is_active = payload.is_active === 'true';
      for (const key of ['ttl_seconds', 'refresh_seconds']) if (payload[key]) payload[key] = Number(payload[key]);
      for (const key of ['effective_from', 'received_at']) if (payload[key]) payload[key] = new Date(payload[key]).toISOString();
      if (['tariff','ru-tariff'].includes(action)) {
        payload.entity_id = payload.entity_id || null;
        payload.tiers = [...form.querySelectorAll('[data-tier-row]')].map(row => ({
          min: row.querySelector('[data-tier-min]').value,
          max: row.querySelector('[data-tier-max]').value || null,
          percent: row.querySelector('[data-tier-percent]').value,
        }));
        payload.reference = null;
        delete payload.local_per_usdt; delete payload.reference_date;
        payload.approve_reference = false;
        if (action === 'ru-tariff') for (const key of ['party','payment_method','effective_from','is_active','reference','approve_reference']) delete payload[key];
      }
      if (['confirm','cancel','refund'].includes(action)) {
        // Preserve the key for retries of this exact submitted payload.
        const fingerprint = JSON.stringify(payload);
        if (form.dataset.eventPayload !== fingerprint) {
          form.dataset.eventKey = crypto.randomUUID(); form.dataset.eventPayload = fingerprint;
        }
        payload.event_key = form.dataset.eventKey;
      }
      if (['confirm','cancel','refund','country'].includes(action) && !await reviewDecision({
        confirm:'Подтвердить фактическое поступление GEL и выполнить расчёт USDT?',
        cancel:'Подтверждаете отсутствие поступления и освобождение резерва?',
        refund:'Сторнировать расчёт USDT по указанному основанию?',
        country:'Страна этого кабинета будет зафиксирована навсегда. Подтвердить?',
      }[action])) return;
      button.disabled = true;
      show(target, 'Запрос выполняется…');
      try {
        const response = await fetch(`${base}/${action}`, {method:'POST', credentials:'same-origin',
          headers:{'Content-Type':'application/json', Accept:'application/json', 'X-CSRF-Token':token}, body:JSON.stringify(payload)});
        const data = await response.json();
        if (!response.ok) {
          show(target, data.detail?.reason || data.detail || 'Операция недоступна');
          return;
        }
        if (['test-parser','parser-history','assignment-history'].includes(action)) {
          show(target, data);
        } else {
          show(target, 'Сохранено. Обновляем историю…');
          window.location.reload();
        }
      } catch (error) {
        show(target, 'Не удалось подтвердить результат. Повторите тот же запрос: идентификатор операции сохранён.');
      } finally { button.disabled = false; }
    });
  });
})();
