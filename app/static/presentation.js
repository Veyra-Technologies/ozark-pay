/* Display precision is independent of storage, input payloads and calculations. */
(() => {
  'use strict';
  function decimal(value) {
    const match = String(value ?? '0').replaceAll(' ', '').replace(',', '.').match(/^(-?)(\d+)(?:\.(\d*))?$/);
    if (!match) return String(value ?? '—');
    let cents = BigInt(match[2]) * 100n + BigInt((match[3] || '').padEnd(2, '0').slice(0, 2));
    if (Number((match[3] || '')[2] || 0) >= 5) cents++;
    const digits = cents.toString().padStart(3, '0');
    return (match[1] && cents ? '-' : '') + digits.slice(0, -2).replace(/\B(?=(\d{3})+(?!\d))/g, ' ') + ',' + digits.slice(-2);
  }
  window.OzarkDecimal = decimal;
  function syncInputs(root = document) {
    root.querySelectorAll('[data-money-original]').forEach(field => {
      const visible = field.nextElementSibling;
      if (visible?.dataset.moneyView === 'true' && document.activeElement !== visible) visible.value = field.value ? decimal(field.value) : '';
    });
  }
  window.OzarkSyncMoneyInputs = syncInputs;
  const moneyName = /(?:amount|balance|hold$|percent|^rate(?:_|$)|fee(?:_|$)|^requested_usdt$|min_check|max_check|daily_limit|monthly_limit)/;
  function inputs(root = document) {
    root.querySelectorAll('input[name]').forEach(field => {
      if (!moneyName.test(field.name) || field.hasAttribute('data-money-original') || ['hidden','checkbox','radio'].includes(field.type) || field.name.endsWith('_id')) return;
      if (!['number','text'].includes(field.type)) return;
      const visible = field.cloneNode(false), original = {min:field.min,max:field.max};
      visible.type = 'text'; visible.removeAttribute('name'); visible.removeAttribute('step'); visible.removeAttribute('min'); visible.removeAttribute('max'); visible.removeAttribute('pattern'); visible.removeAttribute('data-commission-rate'); visible.inputMode = 'decimal'; visible.dataset.moneyView = 'true';
      field.removeAttribute('id'); field.type = 'hidden'; field.dataset.moneyOriginal = 'true'; field.after(visible);
      visible.value = field.value ? decimal(field.value) : '';
      visible.addEventListener('input', () => {
        const raw = visible.value.trim().replaceAll(' ', '').replace(',', '.');
        let error = raw && !/^-?\d+(?:\.\d+)?$/.test(raw) ? 'Введите число, например 100,12' : '';
        if (!error && raw && original.min && Number(raw) < Number(original.min)) error = `Минимум ${decimal(original.min)}`;
        if (!error && raw && field.max && Number(raw) > Number(field.max)) error = `Максимум ${decimal(field.max)}`;
        visible.setCustomValidity(error); field.value = raw; field.dispatchEvent(new Event('input', {bubbles:true}));
      });
      visible.addEventListener('blur', () => { if (visible.checkValidity()) syncInputs(root); });
    });
  }
  function formatText(root = document.body) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = []; while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      if (!node.parentElement || node.parentElement.closest('script,style,pre,code,textarea,input,option,.mono,.cell-mono,[data-raw-value]')) continue;
      const text = node.nodeValue;
      let formatted = text.replace(/(?<![\w.:-])(-?\d+(?:[ ]\d{3})*(?:[.,]\d+)?)(\s*(?:RUB|USDT|AMD|GEL|₽|%))(?!\w)/g, (_, number, unit) => decimal(number) + unit);
      if (/^\s*-?\d+[.,]\d{3,}\s*$/.test(formatted)) formatted = decimal(formatted.trim());
      if (formatted !== text) node.nodeValue = formatted;
    }
  }
  const labels = {'Merchant':'Мерчант','Executor':'Исполнитель','Status':'Статус','Date':'Дата','Amount':'Сумма','Actions':'Действия','Action':'Действие','Rate':'Курс','Merchant fee':'Комиссия мерчанта','Executor fee':'Комиссия исполнителя','Created':'Создан','Created at':'Создан','Last used at':'Использован','Network':'Сеть','Mode':'Режим','From':'Начало','To':'Окончание','Type':'Тип','Currency':'Валюта','Operation / external':'Операция','Operation / settlement':'Статус','Создано / завершено':'Дата','Маржа / доход':'Доход','Merchant rate / fee':'Комиссия мерчанта','Executor rate / fee':'Комиссия исполнителя'};
  let recordNumber = 0;
  function tableDetails(root = document) {
    // Polling replaces a tbody, so include its owning table as well as descendants.
    const tables = new Set(root.querySelectorAll('table'));
    if (root instanceof Element && root.closest('table')) tables.add(root.closest('table'));
    tables.forEach(table => {
      const header = [...(table.rows[0]?.cells || [])]; if (!header.some(c => c.tagName === 'TH')) return;
      // Give both desktop tables and mobile cards a single, explicit header group.
      if (!table.tHead) table.createTHead().append(header[0].parentElement);
      header.forEach(c => { if (labels[c.textContent.trim()]) c.textContent = labels[c.textContent.trim()]; });
      const names = header.map(c => c.textContent.trim());
      if (!table.dataset.compacted && !table.classList.contains('operational-table') && header.length > 7) {
        const hidden = names.map((name,i) => /(?:ledger|rule|version|secret|ID|external|хэш|hash|destination|последний|правило)/i.test(name) ? i : -1).filter(i=>i>=0);
        const candidates = names.map((name,i)=>i).filter(i=>!hidden.includes(i));
        while(candidates.length > 6) hidden.push(candidates.splice(5,1)[0]);
        const extra = document.createElement('th'); extra.textContent = 'Подробности'; table.rows[0].append(extra);
        for (const row of [...table.rows].slice(1)) {
          const cells = [...row.cells]; if (cells.length !== header.length) { if(cells.length === 1) cells[0].colSpan = header.length-hidden.length+1; continue; }
          const td = row.insertCell(), button = document.createElement('button'), dialog = document.createElement('div'), box = document.createElement('div'), title = document.createElement('h2'), dl = document.createElement('dl');
          dialog.className = 'modal'; dialog.id = `table-record-${++recordNumber}`; box.className = 'modalbox modalbox-wide'; title.textContent = 'Подробности записи'; dl.className = 'profile-data';
          hidden.forEach(i => { const dt = document.createElement('dt'), dd = document.createElement('dd'); dt.textContent = names[i]; dd.className = cells[i].className; while(cells[i].firstChild) dd.append(cells[i].firstChild); cells[i].remove(); dl.append(dt,dd); });
          button.type = 'button'; button.className = 'secondary'; button.textContent = 'Подробнее'; button.addEventListener('click',()=>{ dialog.classList.add('active'); window.OzarkWorkspace?.focusDialog(dialog); });
          const close = button.cloneNode(); close.textContent = 'Закрыть'; close.addEventListener('click',()=>{ dialog.classList.remove('active'); window.OzarkWorkspace?.restoreFocus(dialog); });
          box.append(title,dl,close); dialog.append(box); document.body.append(dialog); td.append(button);
        }
        hidden.forEach(i => header[i].remove()); table.dataset.compacted = 'true';
      }
      const currentHeaderCells = [...table.rows[0].cells];
      const currentHeaders = currentHeaderCells.map(c=>c.textContent.trim());
      const amountColumn = name => /(?:сумма|баланс|доступно|холд|оборот|доход|комиссия|курс|начислен|процент|списание|прибыль|резерв|долг|ставка|usdt|rub|amd|gel|%|gross|accrual|available|frozen|debt)/i.test(name);
      const amountColumns = currentHeaders.map((name, i) => amountColumn(name) &&
        ![...table.rows].slice(1).some(row => row.cells[i]?.querySelector('form,input,select,textarea')));
      currentHeaderCells.forEach((cell,i) => { cell.scope = 'col'; cell.classList.toggle('amount-cell', amountColumns[i]); });
      for(const row of [...table.rows].slice(1)) [...row.cells].forEach((cell,i)=>{
        if (cell.colSpan > 1) { cell.dataset.label = ''; return; }
        cell.dataset.label = currentHeaders[i] || 'Действие';
        cell.classList.toggle('amount-cell', amountColumns[i]);
        if(/^(?:IP|IP-адрес)$/.test(currentHeaders[i])) cell.classList.add('ip-address');
        if (/^(?:дата|время|создан|создана|создано|использован)(?:$|\s)/i.test(currentHeaders[i])) cell.classList.add('date-cell');
      });
      table.classList.add('compact-table');
    });
    root.querySelectorAll('td small,td .compact-note,td .muted').forEach(node=>{if(!node.children.length && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(node.textContent.trim())) node.classList.add('cell-mono');});
    // Technical values are readable in full on demand, never letter-by-letter columns.
    root.querySelectorAll('td .cell-mono,td.mono,td.cell-mono,td .cell-ellipsis,td.cell-wrap').forEach(node=>{
      if(node.dataset.shortened || node.closest('.modal') || node.querySelector('button,input')) return;
      const raw=node.title || node.textContent.trim();
      const technical = node.matches('.cell-mono,.mono,.cell-ellipsis');
      if(raw.length < (technical ? 24 : 140)) return;
      node.dataset.shortened='true'; node.title=raw;
      const identifier = node.matches('.cell-mono,.mono') || !/\s/.test(raw);
      node.textContent=identifier ? raw.slice(0,8)+'…'+raw.slice(-4) :
        raw.length > 80 ? raw.slice(0,80).replace(/\s+\S*$/, '')+'…' : raw;
      const b=document.createElement('button'); b.type='button'; b.className='text-link'; b.textContent=technical ? node.textContent : 'Детали'; b.setAttribute('aria-label', 'Полное значение: '+node.textContent); b.addEventListener('click',()=>{
        const d=document.createElement('dialog'); d.className='value-dialog'; const title=document.createElement('h2'); title.textContent='Полное значение'; const pre=document.createElement('pre'); pre.textContent=raw; const copy=document.createElement('button'); copy.type='button'; copy.className='secondary'; copy.textContent='Копировать';copy.onclick=()=>navigator.clipboard.writeText(raw);const close=document.createElement('button');close.type='button';close.className='secondary';close.textContent='Закрыть';close.onclick=()=>{d.close();d.remove();};const actions=document.createElement('div');actions.className='actions';actions.append(copy,close);d.append(title,pre,actions);d.addEventListener('cancel',()=>d.remove());document.body.append(d);d.showModal(); }); if(technical) { node.textContent=''; node.append(b); } else if(node.tagName === 'TD') node.append(b); else node.after(b);
    });
  }
  function enhance(root = document) { inputs(root); formatText(root === document ? document.body : root); tableDetails(root); }
  window.OzarkEnhance = enhance;
  enhance();
  document.querySelectorAll('.profile-content form[method=post]').forEach(form=>{const field=document.createElement('input');field.type='hidden';field.name='return_to';field.value=location.pathname+location.search;form.append(field);});
  document.querySelectorAll('[data-list-search],[data-list-status]').forEach(control=>control.addEventListener('input',()=>{
    const id=control.dataset.listSearch||control.dataset.listStatus, query=document.querySelector(`[data-list-search="${id}"]`).value.toLowerCase(),status=document.querySelector(`[data-list-status="${id}"]`).value;
    document.querySelectorAll(`#${id} tr[data-list-state]`).forEach(row=>{row.hidden=!row.textContent.toLowerCase().includes(query)||(status && status!==row.dataset.listState);});
  }));
  const role=document.querySelector('form[action$="/users/create"] select[name=role]');
  function roleFields(){document.querySelectorAll('[data-role-fields]').forEach(group=>{const shown=group.dataset.roleFields.split(' ').includes(role.value);group.hidden=!shown;group.querySelectorAll('input,select').forEach(field=>{field.disabled=!shown;field.required=shown&&!field.hasAttribute('data-optional');});});}
  if(role){role.addEventListener('change',roleFields);roleFields();}
  const revealActive=()=>document.querySelectorAll('.nav-section').forEach(group=>{if(group.querySelector('.active')) group.open=true;});
  revealActive();window.addEventListener('hashchange',revealActive);
  document.querySelectorAll('.nav-section [data-target]').forEach(button=>button.addEventListener('click',revealActive));
})();
