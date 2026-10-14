/* Shared progressive enhancement. Login deliberately does not load this file. */
(() => {
  'use strict';
  const body = document.body;
  const nav = document.querySelector('#workspace-nav');
  const menu = document.querySelector('.menu-toggle');
  const closeMenu = () => { body.classList.remove('nav-open'); menu?.setAttribute('aria-expanded', 'false'); };
  menu?.addEventListener('click', () => { const opened = body.classList.toggle('nav-open'); menu.setAttribute('aria-expanded', String(opened)); if (opened) nav?.querySelector('[data-target]')?.focus(); });
  document.querySelector('.menu-dismiss')?.addEventListener('click', () => { closeMenu(); menu?.focus(); });
  document.querySelector('.nav-shade')?.addEventListener('click', closeMenu);
  const toast = document.createElement('div'); toast.className = 'toast'; toast.hidden = true; toast.setAttribute('role', 'status'); body.append(toast);
  let toastTimer;
  function notify(message) { toast.textContent = message; toast.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { toast.hidden = true; }, 2600); }
  const focusOrigins = new WeakMap();
  function focusDialog(dialog) {
    focusOrigins.set(dialog, document.activeElement);
    dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true');
    const heading = dialog.querySelector('h2');
    if (heading) { heading.id ||= `${dialog.id}-heading`; dialog.setAttribute('aria-labelledby', heading.id); }
    const box = dialog.querySelector('.modalbox'); if (box) box.tabIndex = -1;
    (dialog.querySelector('[data-modal-close],input:not([type=hidden]),button') || box)?.focus();
  }
  function restoreFocus(dialog) { focusOrigins.get(dialog)?.focus(); }
  window.OzarkWorkspace = { focusDialog, restoreFocus };
  document.addEventListener('keydown', (event) => {
    const dialog = document.querySelector('.modal.active');
    if (event.key === 'Escape') {
      if (dialog) { event.preventDefault(); dialog.classList.remove('active'); restoreFocus(dialog); }
      else if (body.classList.contains('nav-open')) { closeMenu(); menu?.focus(); }
    }
    const scope = dialog || (body.classList.contains('nav-open') ? nav : null);
    if (event.key !== 'Tab' || !scope) return;
    const controls = [...scope.querySelectorAll('button:not(:disabled),a[href],input:not([type=hidden]):not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex="0"]')].filter(n => n.getClientRects().length);
    if (!controls.length) return;
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }, true);
  document.querySelectorAll('.modal').forEach(dialog => {
    dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true');
    if (!dialog.querySelector('[data-modal-close]')) {
      const close = document.createElement('button'); close.className = 'secondary mobile-close'; close.type = 'button'; close.textContent = 'Закрыть';
      close.addEventListener('click', () => { dialog.classList.remove('active'); restoreFocus(dialog); }); dialog.querySelector('.modalbox')?.prepend(close);
    }
  });
  // TeamLead shares the shell but has no legacy cabinet script.
  function navigate(id) {
    if (typeof window.activateSection === 'function') { window.activateSection(id, true); return; }
    const target = document.getElementById(id); if (!target?.classList.contains('section')) return;
    document.querySelectorAll('.section').forEach(n => n.classList.toggle('active', n === target));
    document.querySelectorAll('.nav [data-target]').forEach(n => { const active = n.dataset.target === id; n.classList.toggle('active', active); n.setAttribute('aria-current', active ? 'page' : 'false'); });
    history.replaceState(null, '', `#${id}`); closeMenu(); window.scrollTo({top:0});
  }
  if (body.classList.contains('teamlead-workspace')) {
    nav?.querySelectorAll('[data-target]').forEach(n => n.addEventListener('click', () => navigate(n.dataset.target)));
    const requested = new URLSearchParams(location.search).get('section');
    navigate(requested === 'security' ? 'sec9' : (location.hash.slice(1) || 'sec1'));
  }
  document.querySelectorAll('[data-workspace-target]').forEach(n => n.addEventListener('click', e => { e.preventDefault(); navigate(n.dataset.workspaceTarget); }));
  window.addEventListener('hashchange', () => navigate(location.hash.slice(1)));
  // Keep every data table inside its own scroll region, never the viewport.
  document.querySelectorAll('table').forEach(table => {
    if (!table.parentElement.matches('.table-wrap,.table')) { const wrap = document.createElement('div'); wrap.className = 'table-wrap'; table.before(wrap); wrap.append(table); }
    const wrap = table.parentElement; wrap.tabIndex = 0; wrap.setAttribute('role', 'region'); wrap.setAttribute('aria-label', 'Таблица. Прокрутка по горизонтали при необходимости');
  });
  // Associate legacy form labels without changing names, actions, CSRF or permissions.
  let fieldNumber = 0;
  document.querySelectorAll('input:not([type=hidden]),select,textarea').forEach(field => {
    if (field.labels?.length || field.getAttribute('aria-label')) return;
    const label = field.previousElementSibling;
    if (label?.tagName === 'LABEL') { field.id ||= `workspace-field-${++fieldNumber}`; label.htmlFor = field.id; }
    else { field.setAttribute('aria-label', field.placeholder || field.name || 'Поле формы'); }
  });
  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-workspace-copy]'); if (!button) return;
    const source = document.getElementById(button.dataset.workspaceCopy); if (!source) return;
    try { await navigator.clipboard.writeText(source.textContent); notify('Скопировано'); } catch { notify('Копирование недоступно. Выделите текст в блоке.'); source.focus(); }
  });
  const filters = document.querySelector('[data-webhook-filters]');
  if (filters) {
    const rows = [...document.querySelectorAll('[data-webhook-row]')];
    const search = filters.querySelector('[data-webhook-search]'), status = filters.querySelector('[data-webhook-status]'), from = filters.querySelector('[data-webhook-from]'), to = filters.querySelector('[data-webhook-to]');
    const empty = document.createElement('p'); empty.className = 'empty-state'; empty.textContent = 'Нет событий по выбранным фильтрам. Измените условия поиска.'; empty.hidden = true;
    document.querySelector('.webhook-table')?.parentElement.after(empty);
    function filter() {
      const term = search.value.trim().toLowerCase(); let shown = 0;
      rows.forEach(row => { const match = (!term || row.dataset.search.toLowerCase().includes(term)) && (!status.value || row.dataset.status === status.value) && (!from.value || row.dataset.date >= from.value) && (!to.value || row.dataset.date <= to.value); row.hidden = !match; if (match) shown++; });
      document.querySelector('[data-webhook-count]').textContent = `${shown} из ${rows.length} событий · последние 300 записей`;
      empty.hidden = shown > 0 || rows.length === 0;
    }
    filters.addEventListener('input', filter); filters.addEventListener('change', filter);
    document.querySelector('[data-webhook-reset]')?.addEventListener('click', () => { [search,status,from,to].forEach(n => { n.value = ''; }); filter(); search.focus(); });
  }
  const quoteForm = document.querySelector('[data-teamlead-withdrawal]');
  if (quoteForm) {
    const amount = quoteForm.elements.requested_usdt, output = quoteForm.querySelector('[data-quote-result]'), submit = quoteForm.querySelector('[type=submit]');
    let requestNumber = 0, debounce, quoteRunning = false, quoteStopped = false;
    async function refreshQuote(options = {}) {
      if (quoteStopped || (options.automatic && (quoteRunning || debounce))) return;
      quoteRunning = true;
      const sequence = ++requestNumber;
      submit.disabled = true; output.setAttribute('aria-busy', 'true'); output.textContent = 'Получаем актуальный расчёт…';
      try {
        const url = new URL(quoteForm.dataset.quoteUrl, location.origin);
        if (options.maximum) url.searchParams.set('requested_usdt', 'max');
        else if (amount.value) url.searchParams.set('requested_usdt', amount.value);
        const response = await fetch(url, {credentials:'same-origin', redirect:'manual', headers: {'Accept':'application/json', 'x-session-view':quoteForm.dataset.sessionView || ''}, cache:'no-store'});
        if (response.type === 'opaqueredirect' || [401,403,409].includes(response.status)) { quoteStopped = true; throw new Error('Сессия изменилась. Откройте кабинет заново.'); }
        const result = await response.json(); if (sequence !== requestNumber) return;
        if (!response.ok || !result.available) throw new Error(result.reason || 'Нет актуального расчёта. Повторите позже.');
        if (options.maximum) amount.value = result.requested_usdt || '';
        amount.max = result.max_usdt;
        window.OzarkSyncMoneyInputs?.();
        quoteForm.querySelector('[data-fill-max]').dataset.maximum = result.max_usdt;
        const maximumNode = document.querySelector('[data-teamlead-max]');
        if (maximumNode) { maximumNode.textContent = `${window.OzarkDecimal(result.max_usdt)} USDT`; maximumNode.classList.remove('unavailable'); }
        const maximumNote = document.querySelector('[data-teamlead-max-note]');
        if (maximumNote) maximumNote.textContent = `Комиссия ${window.OzarkDecimal(result.fee_usdt)} USDT учтена`;
        const details = document.querySelector('[data-teamlead-quote-details]');
        if (details) {
          details.hidden = false;
          details.textContent = `Курс: 1 USDT = ${window.OzarkDecimal(result.rate)} RUB · ${result.source} · Комиссия: ${window.OzarkDecimal(result.fee_usdt)} USDT / ${window.OzarkDecimal(result.fee_rub)} RUB · Получен: ${result.fetched_at.replace('T', ' ').slice(0, 19)} UTC`;
        }
        const lines = amount.value ? [
          `Получите: ${window.OzarkDecimal(result.requested_usdt)} USDT`, `Комиссия: ${window.OzarkDecimal(result.fee_usdt)} USDT (${window.OzarkDecimal(result.fee_rub)} RUB)`,
          `Всего будет зарезервировано: ${window.OzarkDecimal(result.total_rub)} RUB`, `Курс: 1 USDT = ${window.OzarkDecimal(result.rate)} RUB · ${result.source}`,
          ...(result.affordable ? [] : ['Недостаточно доступных средств. Уменьшите сумму.'])
        ] : [`Можно запросить до ${window.OzarkDecimal(result.max_usdt)} USDT`, `Комиссия ${window.OzarkDecimal(result.fee_usdt)} USDT уже учтена. Курс: 1 USDT = ${window.OzarkDecimal(result.rate)} RUB.`];
        output.replaceChildren(...lines.map(text => { const line=document.createElement('div'); line.textContent=text; return line; }));
        submit.disabled = !amount.value || !result.affordable;
      } catch(error) {
        if (sequence !== requestNumber) return;
        output.textContent = `Расчёт USDT недоступен. ${error.message}`;
        const maximumNode = document.querySelector('[data-teamlead-max]');
        if (maximumNode) { maximumNode.textContent = 'Расчёт USDT недоступен'; maximumNode.classList.add('unavailable'); }
        const maximumNote = document.querySelector('[data-teamlead-max-note]');
        if (maximumNote) maximumNote.textContent = error.message;
        const details = document.querySelector('[data-teamlead-quote-details]');
        if (details) details.hidden = true;
        quoteForm.querySelector('[data-fill-max]').dataset.maximum = '';
      }
      finally { quoteRunning = false; if (sequence === requestNumber) output.setAttribute('aria-busy', 'false'); }
    }
    amount.addEventListener('input', () => { ++requestNumber; submit.disabled = true; clearTimeout(debounce); debounce = setTimeout(() => { debounce=null; refreshQuote(); }, 350); });
    quoteForm.querySelector('[data-refresh-quote]').addEventListener('click', refreshQuote);
    quoteForm.querySelector('[data-fill-max]').addEventListener('click', () => { clearTimeout(debounce); debounce=null; refreshQuote({maximum: true}); });
    refreshQuote({automatic:true});
    setInterval(() => refreshQuote({automatic:true}), 15000);
    quoteForm.addEventListener('submit', event => { if (submit.disabled) event.preventDefault(); });
  }
  document.querySelectorAll('form').forEach(form => form.addEventListener('submit', event => {
    if (event.defaultPrevented || form.method.toLowerCase() !== 'post') return;
    // Preserve the submitting button's name/value and the original action.
    const button = event.submitter;
    if (button) { setTimeout(() => { if (!event.defaultPrevented) { button.setAttribute('aria-busy','true'); button.textContent='Выполняется…'; } }, 0); }
  }));
})();
