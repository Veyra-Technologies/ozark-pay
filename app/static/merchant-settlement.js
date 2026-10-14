(() => {
  'use strict';
  const approvedForms = new WeakSet();
  const dialog = document.createElement('dialog');
  dialog.className = 'value-dialog';
  dialog.id = 'settlementConfirmDialog';
  dialog.setAttribute('aria-labelledby','settlementConfirmHeading');
  const heading = document.createElement('h2');
  heading.id = 'settlementConfirmHeading'; heading.textContent = 'Подтверждение Settle';
  const description = document.createElement('p');
  description.style.overflowWrap = 'anywhere';
  const actions = document.createElement('div'); actions.className = 'modal-actions';
  const cancel = document.createElement('button'); cancel.type='button'; cancel.className='secondary'; cancel.textContent='Отмена';
  const confirm = document.createElement('button'); confirm.type='button'; confirm.className='primary'; confirm.textContent='Подтвердить действие';
  actions.append(cancel,confirm); dialog.append(heading,description,actions); document.body.append(dialog);
  let pendingForm, submitter;
  cancel.addEventListener('click',()=>dialog.close());
  dialog.addEventListener('close',()=>{ pendingForm=undefined; submitter=undefined; });
  document.addEventListener('submit',event=>{
    const target=event.target.closest('form[data-settlement-confirm]');
    if (!target) return;
    if (approvedForms.has(target)) { approvedForms.delete(target); return; }
    event.preventDefault();
    pendingForm=target; submitter=event.submitter;
    description.textContent=target.dataset.settlementConfirm;
    confirm.disabled=false; dialog.showModal(); cancel.focus();
  });
  confirm.addEventListener('click',()=>{
    const target=pendingForm, button=submitter;
    if (!target || !target.reportValidity()) return;
    confirm.disabled=true; dialog.close(); approvedForms.add(target);
    target.requestSubmit(button || undefined);
  });
  const form = document.querySelector('[data-settlement-form]');
  if (!form) return;
  const amount = form.querySelector('[data-settlement-amount]');
  const address = form.querySelector('[data-settlement-address]');
  const total = form.querySelector('[data-settlement-total]');
  const fee = form.querySelector('[data-settlement-fee]');
  const equivalent = form.querySelector('[data-settlement-equivalent]');
  const result = form.querySelector('[data-settlement-result]');
  const submit = form.querySelector('[data-settlement-submit]');
  const expected = form.querySelector('[data-settlement-expected]');
  const display = value => window.OzarkDecimal(value);
  let sequence = 0, timer, preview;
  function confirmation() {
    if (!preview) return;
    form.dataset.settlementConfirm = `Создать заявку: мерчанту ${display(preview.amount_usdt)} USDT, комиссия ${display(preview.fee_usdt)} USDT сверх выплаты, резерв ${display(preview.reserve_amount)} ${preview.balance_currency}. Сеть TRON (TRC20). Полный адрес: ${address.value.trim()}. Проверьте адрес: криптовалютный перевод необратим.`;
  }
  async function update() {
    const current = ++sequence;
    preview = undefined;
    expected.value = '';
    submit.disabled = true;
    if (!amount.value || Number(amount.value) <= 0) { total.value = '—'; result.textContent = 'Введите сумму выплаты.'; return; }
    result.textContent = 'Проверка суммы…';
    try {
      const url = new URL(form.dataset.previewUrl, location.origin);
      url.searchParams.set('amount_usdt',amount.value);
      const response = await fetch(url, {credentials:'same-origin',cache:'no-store',headers:{accept:'application/json','x-session-view':form.dataset.sessionView || ''}});
      const data = await response.json();
      if (current !== sequence) return;
      if (!response.ok || !data.rate_available || data.reserve_amount === undefined) {
        total.value='—'; result.textContent='Расчёт временно недоступен. Обновите кабинет и проверьте доступность курса.'; return;
      }
      preview = data;
      expected.value = data.reserve_amount;
      fee.value = `${display(data.fee_usdt)} USDT`;
      total.value = `${display(data.reserve_amount)} ${data.balance_currency}`;
      equivalent.textContent = data.balance_currency === 'RUB'
        ? `Мерчант получает ${display(data.amount_usdt)} USDT. Выплата: ${display(data.payout_balance_amount)} RUB; комиссия: ${display(data.fee_balance_amount)} RUB. Курс фиксируется при создании заявки.`
        : `Мерчант получает ${display(data.amount_usdt)} USDT. Комиссия ${display(data.fee_usdt)} USDT списывается сверх выплаты.`;
      submit.disabled = !data.can_request || data.has_active_request;
      result.textContent = data.has_active_request ? 'Сначала завершите текущую заявку.' : data.can_request ? 'Полная стоимость покрывается доступным балансом.' : 'Недостаточно доступного баланса с учётом комиссии.';
      confirmation();
    } catch (_) { if (current === sequence) { total.value='—'; result.textContent='Не удалось проверить сумму. Обновите кабинет.'; } }
  }
  amount.addEventListener('input', () => { clearTimeout(timer); submit.disabled=true; ++sequence; timer=setTimeout(update,150); });
  address.addEventListener('input',confirmation);
})();
