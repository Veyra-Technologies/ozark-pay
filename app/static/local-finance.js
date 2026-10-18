/* Only local-time presentation; all financial decisions remain on the server. */
(() => {
  'use strict';
  document.addEventListener('submit', event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.matches('[data-local-confirmation],[data-local-evidence]')) return;
    const local = form.querySelector('[data-local-time]');
    const iso = form.querySelector('[data-local-time-iso]');
    const date = new Date(local.value);
    if (!local.value || Number.isNaN(date.getTime())) {
      event.preventDefault(); local.reportValidity(); return;
    }
    iso.value = date.toISOString();
  });
})();
