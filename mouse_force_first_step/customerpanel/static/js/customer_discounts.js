/* Discounts has one explicit lifecycle; the shared shell/audio is never replaced. */
window.CustomerDiscounts = function (scope) {
  'use strict';
  const root = scope.document.querySelector('[data-discounts-page]');
  if (!root) return;
  const status = root.querySelector('[data-discounts-status]');
  const session = document.getElementById('customer-page-top').dataset.session;
  const busyVotes = new WeakSet();
  let filterGeneration = 0;
  let searchTimer;
  let searchHistory = false;

  async function readJSON(response) {
    if (response.status === 401 || (response.status === 403 && !response.headers.get('X-Customer-Section-Locked'))) {
      window.CustomerNavigation.endSession();
      throw new Error('Please sign in again.');
    }
    if (!response.headers.get('Content-Type')?.includes('application/json')) throw new Error('Unable to load this request. Please try again.');
    const data = await response.json();
    if (data.session && data.session !== session) {
      window.CustomerNavigation.endSession();
      throw new Error('Please sign in again.');
    }
    if (data.error === 'section_locked') throw new Error('Please unlock the Discounts section first.');
    return data;
  }

  function setPoints(state) {
    window.dispatchEvent(new CustomEvent('customer:points-state', {detail: state}));
    const balance = root.querySelector('[data-deal-balance]');
    if (balance) balance.textContent = `${state.total_points} Points`;
  }

  scope.on(root, 'submit', event => {
    const form = event.target.closest('[data-deal-vote]');
    if (!form) return;
    event.preventDefault();
    if (busyVotes.has(form) || !event.submitter) return;
    const value = event.submitter.value;
    const body = new FormData(form);
    body.set('value', value);
    busyVotes.add(form);
    const buttons = [...form.querySelectorAll('button')];
    buttons.forEach(button => { button.disabled = true; });
    form.setAttribute('aria-busy', 'true');
    scope.track((async () => {
      try {
        const response = await scope.fetch(form.action, {method: 'POST', credentials: 'same-origin', cache: 'no-store', headers: {Accept: 'application/json'}, body});
        const data = await readJSON(response);
        if (!scope.alive) return;
        if (!response.ok) throw new Error(data.message || 'Unable to update your vote.');
        buttons.forEach(button => {
          const direction = Number(button.dataset.voteDirection);
          const selected = data.vote === direction;
          const label = direction === 1 ? 'like' : 'dislike';
          const count = direction === 1 ? data.likes : data.dislikes;
          button.querySelector('[data-vote-count]').textContent = count;
          button.value = selected ? '0' : String(direction);
          button.setAttribute('aria-pressed', String(selected));
          button.setAttribute('aria-label', `${selected ? `Remove ${label}` : `${direction === 1 ? 'Like' : 'Dislike'} this deal`}, ${count} ${label}s`);
        });
        status.textContent = '';
      } catch (error) {
        if (scope.alive && error.name !== 'AbortError') status.textContent = 'Your vote could not be confirmed. Try the same choice again.';
      } finally {
        busyVotes.delete(form);
        buttons.forEach(button => { button.disabled = false; });
        form.removeAttribute('aria-busy');
      }
    })());
  });

  const search = root.querySelector('[data-discount-search]');
  const results = root.querySelector('[data-discount-results]');
  if (search && results) {
    const input = search.elements.q;
    function searchURL() {
      const url = new URL(search.action);
      url.search = new URLSearchParams(new FormData(search)).toString();
      return url;
    }
    async function filter(url, replace) {
      const operation = ++filterGeneration;
      results.setAttribute('aria-busy', 'true');
      results.inert = true;
      status.textContent = 'Updating deals…';
      try {
        await scope.settle();
        if (!scope.alive || operation !== filterGeneration) return;
        const response = await scope.fetch(url, {credentials: 'same-origin', cache: 'no-store', headers: {Accept: 'application/json', 'X-Discount-Results': '1'}});
        const data = await readJSON(response);
        if (!scope.alive || operation !== filterGeneration) return;
        if (!response.ok || typeof data.html !== 'string') throw new Error('Unable to update these results.');
        results.innerHTML = data.html; // Only the server's named, script-free results fragment.
        const canonical = new URL(data.url, location.href);
        if (canonical.origin !== location.origin || canonical.pathname !== new URL(search.action).pathname) throw new Error('Unexpected results URL.');
        search.elements.category.value = canonical.searchParams.get('category') || 'all';
        window.CustomerNavigation.updateDiscountQuery(canonical.href, replace);
        status.textContent = `${data.count} deal${data.count === 1 ? '' : 's'}`;
      } catch (error) {
        if (scope.alive && operation === filterGeneration && error.name !== 'AbortError') status.textContent = 'Results could not be updated. Please search again.';
      } finally {
        if (operation === filterGeneration) { results.inert = false; results.removeAttribute('aria-busy'); }
      }
    }
    scope.on(search, 'submit', event => {
      event.preventDefault(); clearTimeout(searchTimer); searchHistory = false; filter(searchURL(), false);
    });
    scope.on(input, 'input', () => {
      clearTimeout(searchTimer);
      searchTimer = scope.timeout(() => { filter(searchURL(), searchHistory); searchHistory = true; }, 300);
    });
    scope.on(input, 'blur', () => { searchHistory = false; });
    scope.on(search.elements.type, 'change', () => { clearTimeout(searchTimer); searchHistory = false; filter(searchURL(), false); });
    scope.on(root, 'click', event => {
      const link = event.target.closest('a[data-discount-filter]');
      if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault(); clearTimeout(searchTimer); searchHistory = false; filter(new URL(link.href), false);
    });
    scope.on(results, 'focusin', event => {
      const link = event.target.closest('.deal-categories a');
      if (link) link.parentElement.scrollLeft = Math.max(0, link.offsetLeft - link.parentElement.offsetLeft - 12);
    });
  }

  const dialog = root.querySelector('[data-deal-dialog]');
  const opener = root.querySelector('[data-open-deal-unlock]');
  let quote = null;
  let pending = false;
  let quoteGeneration = 0;
  if (dialog && opener) {
    const form = dialog.querySelector('form');
    const confirm = form.querySelector('[data-deal-confirm]');
    const cancel = form.querySelector('[data-deal-cancel]');
    const message = form.querySelector('[data-deal-message]');
    const review = form.querySelector('[data-deal-review]');
    async function loadQuote(note = '') {
      const operation = ++quoteGeneration;
      quote = null; confirm.disabled = true; review.hidden = true;
      message.textContent = note || 'Checking your Points…';
      try {
        const response = await scope.fetch(form.dataset.quoteUrl, {credentials: 'same-origin', cache: 'no-store', headers: {Accept: 'application/json'}});
        const data = await readJSON(response);
        if (!scope.alive || operation !== quoteGeneration || !dialog.open) return;
        if (data.error === 'offer_changed') { message.textContent = data.message; review.hidden = false; return; }
        if (!response.ok) throw new Error('This deal is no longer available.');
        if (data.allowed) { dialog.close(); window.CustomerNavigation.navigate(location.href); return; }
        quote = data; setPoints(data.state);
        form.querySelector('[data-deal-quote]').hidden = false;
        form.querySelector('[data-deal-quote-balance]').textContent = `${data.balance} Points`;
        form.querySelector('[data-deal-quote-cost]').textContent = `${data.cost} Points`;
        form.querySelector('[data-deal-quote-after]').textContent = data.balance_after === null ? '—' : `${data.balance_after} Points`;
        confirm.textContent = `Unlock for ${data.cost} Points`;
        confirm.disabled = !data.can_unlock || pending;
        message.textContent = data.can_unlock ? note : `You don’t have enough Points. You need ${data.cost} Points to unlock this deal.`;
      } catch (error) { if (scope.alive && dialog.open) message.textContent = 'Unable to check this deal. Close the dialog and try again.'; }
    }
    function close() { if (!pending) { quoteGeneration++; quote = null; dialog.close(); opener.focus({preventScroll: true}); } }
    scope.on(opener, 'click', () => { form.querySelector('[data-deal-quote]').hidden = true; dialog.showModal(); loadQuote(); });
    scope.on(cancel, 'click', close);
    scope.on(dialog, 'cancel', event => { event.preventDefault(); close(); });
    scope.on(form, 'submit', event => {
      event.preventDefault();
      if (pending || !quote?.can_unlock) return;
      const body = new URLSearchParams({confirmation_token: quote.token, csrfmiddlewaretoken: form.elements.csrfmiddlewaretoken.value});
      pending = true; confirm.disabled = true; cancel.disabled = true; message.textContent = 'Unlocking…';
      scope.track((async () => {
        try {
          const response = await scope.fetch(form.action, {method: 'POST', credentials: 'same-origin', cache: 'no-store', headers: {Accept: 'application/json'}, body});
          const data = await readJSON(response);
          if (!scope.alive) return null;
          if (response.ok) {
            setPoints(data.state);
            message.textContent = data.message;
            return data.redirect_url;
          }
          if (['balance_changed', 'expired_confirmation', 'insufficient_points'].includes(data.error)) {
            await loadQuote(data.message);
          } else {
            message.textContent = data.message || 'Please retry; you will not be charged twice.';
            if (['offer_changed', 'unavailable', 'invalid_confirmation'].includes(data.error)) { quote = null; review.hidden = false; }
          }
        } catch (error) { if (scope.alive) message.textContent = 'We could not confirm access. Please retry; you will not be charged twice.'; }
        finally { pending = false; cancel.disabled = false; confirm.disabled = !quote?.can_unlock; }
        return null;
      })()).then(destination => {
        if (!destination || !scope.alive) return;
        dialog.close();
        const url = new URL(destination, location.href);
        if (url.origin === location.origin) window.CustomerNavigation.navigate(url.href);
      });
    });
  }

  function scrub() { root.querySelectorAll('[data-discount-benefit]').forEach(node => node.replaceChildren()); quote = null; }
  scope.beforeLeave = () => {
    filterGeneration++; quoteGeneration++; clearTimeout(searchTimer);
    if (results) { results.inert = false; results.removeAttribute('aria-busy'); }
    if (dialog?.open) dialog.close();
  };
  scope.cleanup(() => { clearTimeout(searchTimer); dialog?.close(); scrub(); });
  scope.on(window, 'pagehide', scrub);
};
