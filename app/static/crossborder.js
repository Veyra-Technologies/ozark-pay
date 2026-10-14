/* Assigned profiles and server-calculated quotes. No provider credentials/math. */
(() => {
  'use strict';
  const money = value => value == null ? '—' : window.OzarkDecimal(value);
  const time = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('ru-RU') : '—';
  document.querySelectorAll('[data-working-profile-fields]').forEach(group => {
    const country = group.querySelector('[data-work-country]');
    const currency = group.querySelector('[data-work-currency]');
    const source = group.querySelector('[data-work-source]');
    function sync() {
      const selected = country.selectedOptions[0];
      currency.value = selected?.dataset.currency || '';
      const option = document.createElement('option');
      option.value = selected?.dataset.source || '';
      option.textContent = country.value === 'GE' ? 'Binance · GEL/TBC · информационный' : option.value === 'xe' ? 'XE · среднерыночный' : option.value === 'rapira' ? 'Rapira · ask' : 'Сначала выберите страну';
      source.replaceChildren(option);
      const method = group.querySelector('[data-trader-tariff-method]');
      if (method) {
        const choices = JSON.parse(method.dataset.methodCatalog)[country.value] || [];
        method.replaceChildren(...(choices.length ? choices : [{value:'',label:'Приём пока не подключён'}]).map(item => new Option(item.label,item.value)));
      }
    }
    country.addEventListener('change', sync); sync();
  });
  const requests = new Map();
  document.querySelectorAll('[data-merchant-parser-setting]').forEach(form => {
    const country = form.querySelector('[data-parser-country]');
    const field = form.querySelector('[data-parser-field]');
    const parser = form.querySelector('[data-parser-select]');
    const direction = form.querySelector('[data-parser-direction]');
    const submit = form.querySelector('[data-parser-submit]');
    const help = form.querySelector('[data-parser-help]');
    function syncParser() {
      parser.replaceChildren();
      direction.value = '';
      submit.disabled = true;
      if (!country.value) {
        field.hidden = true; parser.disabled = true;
        help.textContent = 'После выбора страны появятся совместимые с ролью, валютой и направлением источники.';
        return;
      }
      field.hidden = false;
      if (country.value !== 'GE') {
        const option = document.createElement('option');
        option.value = ''; option.textContent = 'Для этой страны нет нового совместимого парсера';
        parser.append(option); parser.disabled = true;
        help.textContent = 'Армения в этой доработке не изменяется.';
        return;
      }
      const option = document.createElement('option');
      option.value = 'binance_p2p_kaspi_buy_100000_avg10';
      option.textContent = 'Binance P2P · Kaspi Bank · Покупка USDT · 100 000 ₸ · Среднее 10';
      parser.append(option); parser.disabled = false; direction.value = 'kz-ge'; submit.disabled = false;
      help.textContent = 'KZT за 1 USDT для стороны мерчанта. Курс получателя в GEL берётся отдельно из XE.';
    }
    country.addEventListener('change', syncParser);
    syncParser();
  });
  async function read(url) {
    if (requests.has(url)) return requests.get(url);
    const task = fetch(url, {credentials:'same-origin', cache:'no-store', headers:{Accept:'application/json'}})
      .then(async r => { if (!r.ok) throw new Error('unavailable'); return r.json(); });
    requests.set(url, task);
    try { return await task; } finally { requests.delete(url); }
  }
  function quoteText(data, root) {
    const rate=root.querySelector('[data-work-rate]'), status=root.querySelector('[data-work-status]');
    const provider=root.querySelector('[data-work-provider]'), fetched=root.querySelector('[data-work-fetched]');
    if(rate) rate.textContent = data.available ? money(data.rate) : '—';
    if(status) status.textContent = data.reason;
    if(provider) provider.textContent = time(data.provider_timestamp);
    if(fetched) fetched.textContent = time(data.fetched_at);
    root.dataset.stale = String(!data.available);
  }
  let polling = false;
  async function refresh() {
    if (document.hidden || polling) return;
    polling = true;
    try {
      await Promise.all([...document.querySelectorAll('[data-work-quote]')].map(async root => {
        try { quoteText(await read(root.dataset.workQuote), root); }
        catch { const status=root.querySelector('[data-work-status]'); if(status) status.textContent = 'Нет подтверждения свежести курса'; const rate=root.querySelector('[data-work-rate]'); if(rate) rate.textContent='—'; root.dataset.stale = 'true'; }
      }));
      const status = document.querySelector('[data-xe-status-url]');
      if (status) {
        try {
          const data = await read(status.dataset.xeStatusUrl);
          status.querySelector('[data-xe-status]').textContent = data.reason;
          document.querySelectorAll('[data-xe-readiness]').forEach(n => n.textContent = data.available ? 'Готовы' : 'Не готовы');
          const quoteNodes = [];
          if (data.bundle) {
            for (const currency of ['KZT','AMD','GEL']) {
              const span = document.createElement('span');
              span.textContent = `${money(data.bundle.rates[currency])} ${currency} за 1 USDT`;
              quoteNodes.push(span);
            }
            const date = document.createElement('span');
            date.textContent = `Время XE: ${time(data.bundle.provider_timestamp)} · Получен: ${time(data.bundle.fetched_at)}`;
            quoteNodes.push(date);
          }
          status.querySelector('[data-xe-quotes]').replaceChildren(...quoteNodes);
        } catch {
          status.querySelector('[data-xe-status]').textContent = 'Нет подтверждения свежести XE';
          document.querySelectorAll('[data-xe-readiness]').forEach(n => n.textContent = 'Не готовы');
        }
      }
      const binance = document.querySelector('[data-binance-status-url]');
      if (binance) {
        try {
          const data = await read(binance.dataset.binanceStatusUrl);
          binance.querySelector('[data-binance-status]').textContent = data.reason;
          const state = binance.querySelector('[data-binance-state]');
          state.textContent = data.available ? 'Актуален' : data.status === 'stale' ? 'Устарел' : 'Недоступен';
          state.className = `status-pill ${data.available ? 'status-ok' : 'status-wait'}`;
          document.querySelectorAll('[data-binance-readiness]').forEach(n => n.textContent = data.available ? 'Готова' : 'Не готова');
          const nodes = [];
          if (data.quote) {
            const rate = document.createElement('span');
            rate.textContent = `${money(data.quote.rate)} KZT за 1 USDT`;
            const count = document.createElement('span');
            count.textContent = `${data.quote.ad_count} объявлений`;
            const fetched = document.createElement('span');
            fetched.textContent = `Получен: ${time(data.quote.fetched_at)}`;
            nodes.push(rate, count, fetched);
          } else if (data.actual_count != null) {
            const count = document.createElement('span');
            count.textContent = `Подходящих объявлений: ${data.actual_count} из 10`;
            nodes.push(count);
          }
          binance.querySelector('[data-binance-quote]').replaceChildren(...nodes);
        } catch {
          binance.querySelector('[data-binance-status]').textContent = 'Нет подтверждения свежести Binance P2P';
          binance.querySelector('[data-binance-state]').textContent = 'Недоступен';
          document.querySelectorAll('[data-binance-readiness]').forEach(n => n.textContent = 'Не готова');
        }
      }
    } finally { polling = false; }
  }
  document.querySelectorAll('[data-work-provider],[data-work-fetched]').forEach(n => n.textContent = time(n.textContent));
  if (document.querySelector('[data-work-quote],[data-xe-status-url],[data-binance-status-url]')) { refresh(); setInterval(refresh, 30000); document.addEventListener('visibilitychange', refresh); }
  document.querySelectorAll('[data-crossborder-preview]').forEach(form => {
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const result = form.querySelector('[data-crossborder-result]'), button = form.querySelector('button[type=submit]');
      result.hidden = false; result.textContent = 'Проверяем котировки и назначенные тарифы…'; button.disabled = true;
      try {
        const response = await fetch(form.action, {method:'POST', credentials:'same-origin', headers:{Accept:'application/json'}, body:new FormData(form)});
        const data = await response.json();
        if (!response.ok || !data.available) { result.textContent = data.reason || 'Расчёт недоступен. Проверьте сессию и исходные данные.'; return; }
        const s = data.snapshot;
        result.replaceChildren();
        const title = document.createElement('h3'); title.textContent = 'Предпросмотр зафиксирован';
        const route = document.createElement('p'); route.textContent = `${money(s.pay_amount)} KZT → ${money(s.usdt_equivalent)} USDT → ${money(s.receive_amount)} ${s.receive_currency}`;
        const fees = document.createElement('dl'); fees.className = 'profile-data';
        for (const [label, value] of [['Комиссия мерчанта',s.merchant_fee_usdt],['Комиссия трейдера',s.trader_fee_usdt],['К расчёту мерчанту',s.merchant_receivable_usdt],['Доход площадки',s.platform_income_usdt]]) {
          const dt=document.createElement('dt'), dd=document.createElement('dd'); dt.textContent=label; dd.textContent=`${money(value)} USDT`; fees.append(dt,dd);
        }
        const validity=document.createElement('p'); validity.textContent=`Действует до ${time(s.expires_at)}. Платёж и резерв не созданы.`;
        const details=document.createElement('details'), summary=document.createElement('summary'), pre=document.createElement('pre');
        summary.textContent='Полный снимок: котировки, тарифы и точность'; pre.textContent=JSON.stringify(s,null,2); details.append(summary,pre);
        const link=document.createElement('a'); link.href=form.action.replace(/\/quote$/,`/quotes/${encodeURIComponent(data.quote_id)}`); link.textContent='Открыть сохранённый расчёт';
        result.append(title,route,fees,validity,details,link);
        // Market polling updates only rates; a fixed preview is never recalculated.
        setTimeout(()=>{validity.textContent=`Срок расчёта истёк (${time(s.expires_at)}). Для нового расчёта нажмите «Рассчитать предпросмотр».`;},Math.max(0,Date.parse(s.expires_at)-Date.now()));
      } catch { result.textContent='Не удалось получить расчёт. Повторите запрос.'; }
      finally { button.disabled=false; }
    });
  });
})();
