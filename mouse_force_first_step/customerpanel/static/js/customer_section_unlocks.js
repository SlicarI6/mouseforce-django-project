/* Shared, persistent unlock dialog. Balances/access come only from Django. */
(() => {
  'use strict';
  const config = document.getElementById('customer-shell-config');
  const dialog = document.getElementById('customer-section-dialog');
  if (!config || !dialog) return;
  const session = document.getElementById('customer-page-top')?.dataset.session;
  const form = dialog.querySelector('form');
  const confirm = form.querySelector('[data-unlock-confirm]');
  const cancel = form.querySelector('[data-unlock-cancel]');
  const message = form.querySelector('[data-unlock-message]');
  const summary = form.querySelector('[data-unlock-summary]');
  const title = document.getElementById('section-unlock-title');
  const description = document.getElementById('section-unlock-description');
  const notice = document.getElementById('customer-section-notice');
  const labels = {discounts: 'Discounts', rewards: 'Rewards', offers: 'Offers', news: 'News', weather: 'Weather'};
  const paths = new Map(Object.keys(labels).map(key => [new URL(config.dataset[key], location.href).pathname, key]));
  const insufficient = 'You don’t have enough Points. You need 10 Points to unlock this section.';
  let state = null;
  let current = null;
  let quote = null;
  let generation = 0;
  let pending = null;
  let stopped = false;
  let toastTimer;
  let refreshPromise;
  const unlocked = section => !!state?.unlocked.includes(section);
  const endpoint = section => config.dataset.unlockUrl.replace('SECTION', encodeURIComponent(section));

  function update(next) {
    if (!next || !Array.isArray(next.unlocked) || !next.points || stopped) return;
    if (next.session && next.session !== session) {
      window.CustomerNavigation?.endSession();
      return;
    }
    state = next;
    document.querySelectorAll('.customer-secondary-nav a[href]').forEach(link => {
      const section = paths.get(new URL(link.href).pathname);
      if (!section) return; // How Points Work is always free.
      link.querySelector('.customer-section-lock')?.remove();
      if (!unlocked(section)) {
        const badge = document.createElement('span');
        badge.className = 'customer-section-lock';
        badge.setAttribute('aria-label', 'Locked: 10 Points once');
        badge.title = 'Unlock once for 10 Points';
        const icon = document.createElement('span');
        icon.setAttribute('aria-hidden', 'true');
        icon.textContent = '🔒';
        const cost = document.createElement('span');
        cost.textContent = '10 Points';
        badge.append(icon, cost);
        link.append(badge);
      }
    });
    window.dispatchEvent(new CustomEvent('customer:points-state', {detail: next.points}));
  }

  async function json(response) {
    if (response.status === 401 || response.status === 403) {
      window.CustomerNavigation?.endSession();
      throw new Error('Please sign in again.');
    }
    if (!response.headers.get('Content-Type')?.includes('application/json')) throw new Error('Please try again in a moment.');
    return response.json();
  }

  function finish(allowed) {
    const previous = current;
    current = null;
    quote = null;
    generation += 1;
    if (dialog.open) dialog.close();
    previous?.resolve(allowed);
    if (previous?.focus?.isConnected) previous.focus.focus({preventScroll: true});
  }

  function success(text) {
    notice.textContent = text;
    notice.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { notice.hidden = true; }, 6000);
  }

  async function loadQuote(section, reviewMessage = '') {
    const operation = ++generation;
    confirm.disabled = true;
    message.textContent = reviewMessage || 'Checking your Points…';
    try {
      const response = await fetch(endpoint(section), {credentials: 'same-origin', cache: 'no-store', headers: {Accept: 'application/json'}});
      const data = await json(response);
      if (operation !== generation || stopped) return;
      if (!response.ok) throw new Error('Unable to check your Points. Please try again.');
      update(data.state);
      if (!current || stopped) return;
      if (data.unlocked) { finish(true); return; }
      quote = data;
      summary.hidden = false;
      form.querySelector('[data-unlock-balance]').textContent = `${data.balance} Points`;
      form.querySelector('[data-unlock-after]').textContent = data.balance_after === null ? '—' : `${data.balance_after} Points`;
      message.textContent = data.can_unlock ? reviewMessage : insufficient;
      confirm.disabled = !data.can_unlock || !!pending;
    } catch (error) {
      if (operation === generation && current) message.textContent = error.message;
    }
  }

  function ensure(section, force = false) {
    if (stopped || !Object.hasOwn(labels, section)) return Promise.resolve(false);
    if (!force && unlocked(section)) return Promise.resolve(true);
    if (current?.section === section) return current.promise;
    if (current) finish(false);
    const focus = document.activeElement;
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    current = {section, resolve, promise, focus};
    quote = null;
    title.textContent = `Unlock ${labels[section]}?`;
    description.textContent = `Unlock ${labels[section]} for 10 Points. This is a one-time payment and you’ll have permanent access.`;
    summary.hidden = true;
    confirm.disabled = true;
    cancel.disabled = false;
    confirm.textContent = 'Unlock for 10 Points';
    dialog.showModal();
    loadQuote(section);
    return promise;
  }

  form.addEventListener('submit', event => {
    event.preventDefault();
    if (pending || !current || !quote?.can_unlock) return;
    const section = current.section;
    const body = new URLSearchParams({confirmation_token: quote.token});
    confirm.disabled = true;
    cancel.disabled = true;
    confirm.textContent = 'Unlocking…';
    message.textContent = '';
    form.setAttribute('aria-busy', 'true');
    // A submitted mutation is never cancelled or silently repeated by navigation.
    pending = (async () => {
      try {
        const response = await fetch(endpoint(section), {
          method: 'POST', credentials: 'same-origin', cache: 'no-store', body,
          headers: {Accept: 'application/json', 'X-CSRFToken': form.elements.csrfmiddlewaretoken.value},
        });
        const data = await json(response);
        if (stopped) return;
        if (data.state) update(data.state);
        if (response.ok && data.unlocked) {
          success(data.message);
          finish(true);
        } else if (['balance_changed', 'expired_confirmation', 'invalid_confirmation', 'insufficient_points'].includes(data.error)) {
          await loadQuote(section, data.message); // A changed quote requires another explicit click.
        } else {
          message.textContent = data.message || 'We could not confirm the unlock. Please retry; you will not be charged twice.';
        }
      } catch (_) {
        if (!stopped) message.textContent = 'We could not confirm the unlock. Please retry; you will not be charged twice.';
      } finally {
        pending = null;
        form.removeAttribute('aria-busy');
        cancel.disabled = false;
        confirm.textContent = 'Unlock for 10 Points';
        confirm.disabled = !current || !quote?.can_unlock;
      }
    })();
  });
  cancel.addEventListener('click', () => { if (!pending) finish(false); });
  dialog.addEventListener('cancel', event => { event.preventDefault(); if (!pending) finish(false); });
  dialog.addEventListener('close', () => { if (current && !pending) finish(false); });
  dialog.addEventListener('click', event => {
    const rect = dialog.getBoundingClientRect();
    if (!pending && event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) finish(false);
  });

  async function refresh() {
    if (!session || stopped || pending || current || refreshPromise) return refreshPromise;
    refreshPromise = (async () => {
      try {
        const response = await fetch(config.dataset.sectionAccessUrl, {credentials: 'same-origin', cache: 'no-store'});
        const data = await json(response);
        if (response.ok && !pending && !current) update(data);
      } catch (_) { /* The next confirmation always checks the server again. */ }
      finally { refreshPromise = null; }
    })();
    return refreshPromise;
  }
  function adopt(doc) {
    const node = doc.getElementById('customer-section-state');
    if (node) { try { update(JSON.parse(node.textContent)); } catch (_) {} }
  }
  adopt(document);
  window.CustomerSectionAccess = Object.freeze({ensure, unlocked, update, adopt, refresh, settle: () => pending || Promise.resolve()});
  window.addEventListener('focus', refresh);
  document.addEventListener('submit', event => {
    if (!event.target.matches('[data-section-fallback]')) return;
    event.preventDefault();
    const section = event.target.closest('[data-section-gate]').dataset.sectionGate;
    const destination = event.target.elements.next.value;
    ensure(section, true).then(allowed => {
      if (allowed) {
        if (window.CustomerNavigation) window.CustomerNavigation.navigate(destination);
        else location.assign(destination);
      }
    });
  });
  window.addEventListener('customer:session-ended', () => { stopped = true; finish(false); clearTimeout(toastTimer); notice.hidden = true; });
})();
