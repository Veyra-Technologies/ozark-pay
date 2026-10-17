/* This form uses the same country policy as server validation. */
(() => {
  document.querySelectorAll('[data-merchant-catalog]').forEach(group => {
    const catalog = JSON.parse(group.dataset.merchantCatalog);
    const country = group.querySelector('[data-merchant-country]');
    const currency = group.querySelector('[data-merchant-currency]');
    const parser = group.querySelector('[data-merchant-parser]');
    const plans = group.querySelector('[data-merchant-plan]');
    const methods = group.querySelector('[data-merchant-methods]');
    country.value = group.dataset.currentCountry || '';
    const sync = (initial = false) => {
      const policy = catalog[country.value];
      currency.value = policy?.currency || '';
      parser.replaceChildren(...Object.entries(policy?.parsers || {}).map(([id,label]) => new Option(label,id)));
      plans.replaceChildren(new Option('Выберите тарифный план',''), ...(policy?.plans || []).map(plan =>
        new Option(`${plan.country} · ${plan.entity_id ? 'Индивидуальный' : 'Общий'} · v${plan.version} · ${plan.tiers.map(t => t.percent+'%').join(' / ')}`,plan.id)));
      methods.replaceChildren(...Object.entries(policy?.methods || {}).map(([id,label]) => new Option(label,id)));
      const chosen = initial ? JSON.parse(group.dataset.currentMethods) : [];
      [...methods.options].forEach(option => { option.selected = chosen.length ? chosen.includes(option.value) : true; });
      if (initial && group.dataset.currentPlan) plans.value = group.dataset.currentPlan;
      group.querySelector('[data-merchant-basis]').textContent = country.value === 'KZ'
        ? 'Номинал и ориентиры — KZT. Ступень комиссии и расчёты — BASE_USDT; обновление курса не меняет утверждённые границы.'
        : country.value === 'TJ' ? 'Диапазоны — TJS, минимум 10 TJS. Приём закрыт до подтверждения источника Rapira Green 1–3.' : country.value === 'RU' ? 'Диапазоны чеков и расчёт комиссии — RUB. Курс Rapira применяется по существующим правилам.' : '';
    };
    country.addEventListener('change', () => sync(false)); sync(true);
  });
})();
