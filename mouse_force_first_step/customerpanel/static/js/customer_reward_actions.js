/* Owned reward actions. No private values or tokens enter browser storage/history. */
(() => {
  'use strict';
  window.CustomerRewardActions = scope => {
    const root = scope.document.querySelector('.customer-reward-action');
    if (!root) return;
    const form = root.querySelector('[data-reward-redeem], [data-reward-reveal], [data-reward-request]');
    const redeem = form?.hasAttribute('data-reward-redeem');
    const requestReward = form?.hasAttribute('data-reward-request');
    const button = form?.querySelector('button[type="submit"]');
    const originalLabel = button?.textContent;
    const error = root.querySelector('[data-reward-error]');
    const review = root.querySelector('[data-reward-review]');
    const privateBox = root.querySelector('[data-reward-private]');
    root.querySelectorAll('[data-fulfillment-placeholder]').forEach(label => {
      const input = scope.document.getElementById(label.htmlFor);
      if (input?.tagName === 'TEXTAREA') input.placeholder = label.dataset.fulfillmentPlaceholder;
    });
    let pending = false;
    let needsReview = false;

    function clearPrivate() {
      if (privateBox) {
        privateBox.querySelectorAll('a').forEach(link => link.removeAttribute('href'));
        privateBox.replaceChildren();
        privateBox.hidden = true;
      }
    }
    function scrubPage() {
      clearPrivate();
      root.querySelectorAll('[data-fulfillment-private]').forEach(node => node.replaceChildren());
      root.querySelectorAll('[data-fulfillment-input]').forEach(input => {
        input.value = ''; input.removeAttribute('value');
        if (input.tagName === 'TEXTAREA') input.textContent = '';
      });
      const token = form?.elements.namedItem('confirmation_token');
      if (token) { token.value = ''; token.removeAttribute('value'); }
    }
    scope.cleanup(scrubPage);
    // Also scrub a document placed in the browser's back/forward cache.
    scope.on(window, 'pagehide', scrubPage);
    if (!form) return;

    async function submit() {
      pending = true;
      button.disabled = true;
      button.textContent = requestReward ? 'Submitting your request…' : redeem ? 'Confirming your reward…' : 'Revealing…';
      form.setAttribute('aria-busy', 'true');
      error.hidden = true;
      if (review) review.hidden = true;
      clearPrivate();
      root.querySelectorAll('[data-field-error]').forEach(node => { node.textContent = ''; });
      root.querySelectorAll('[data-fulfillment-input]').forEach(input => input.removeAttribute('aria-invalid'));
      try {
        const response = await scope.fetch(form.action, {
          method: 'POST', credentials: 'same-origin', cache: 'no-store', referrerPolicy: 'no-referrer',
          headers: {'Accept': 'application/json'}, body: new FormData(form),
        });
        if (!scope.alive) return null;
        if (!response.headers.get('Content-Type')?.includes('application/json')) {
          throw new Error('Please check your connection or refresh this page before trying again.');
        }
        const data = await response.json();
        if (!scope.alive) return null;
        if (!response.ok) {
          if (data.field_errors) {
            let firstInvalid;
            for (const node of root.querySelectorAll('[data-field-error]')) {
              const messages = data.field_errors[node.dataset.fieldError];
              if (!Array.isArray(messages)) continue;
              node.textContent = messages.map(item => item.message).join(' ');
              const input = form.elements.namedItem(node.dataset.fieldError);
              if (input) {
                input.setAttribute('aria-invalid', 'true');
                input.setAttribute('aria-describedby', node.id);
                firstInvalid ||= input;
              }
            }
            firstInvalid?.focus();
          }
          if (review && data.review_url) {
            const url = new URL(data.review_url, location.href);
            if (url.origin === location.origin) {
              review.href = url.href;
              review.hidden = false;
              needsReview = true;
            }
          }
          throw new Error(data.error || 'This request could not be completed. Please try again.');
        }
        if (redeem || requestReward) {
          const destination = new URL(data.redirect_url, location.href);
          if (destination.origin !== location.origin) throw new Error('Please refresh to check your reward.');
          return destination.href;
        }
        if (!privateBox || !['code', 'claim_link'].includes(data.kind) || typeof data.value !== 'string') {
          throw new Error('Your benefit is temporarily unavailable. Please try again.');
        }
        if (data.kind === 'code') {
          const code = document.createElement('code');
          code.textContent = data.value;
          privateBox.append(code);
        } else {
          const url = new URL(data.value);
          if (url.protocol !== 'https:' || url.username || url.password) throw new Error('Your benefit is temporarily unavailable.');
          const link = document.createElement('a');
          link.href = url.href;
          link.textContent = 'Open your private partner offer';
          link.target = '_blank';
          link.rel = 'noopener noreferrer';
          link.referrerPolicy = 'no-referrer';
          link.dataset.noCustomerNavigation = '';
          privateBox.append(link);
        }
        privateBox.hidden = false;
        button.textContent = 'Reveal again';
      } catch (problem) {
        if (scope.alive && problem.name !== 'AbortError') {
          error.textContent = problem instanceof TypeError
            ? 'We could not confirm the result. Try this same request again; it will not spend Points twice.'
            : problem.message;
          error.hidden = false;
        }
      } finally {
        pending = false;
        if (scope.alive) {
          button.disabled = needsReview;
          button.textContent = needsReview ? 'Review required' : originalLabel;
          form.removeAttribute('aria-busy');
        }
      }
      return null;
    }

    scope.on(form, 'submit', event => {
      event.preventDefault();
      if (pending || needsReview) return;
      // Resolve the tracked POST before navigating: navigation waits for pending
      // mutations. Keeping the same token makes a lost-response retry idempotent.
      scope.track(submit()).then(destination => {
        if (!destination || !scope.alive) return;
        button.disabled = true;
        button.textContent = requestReward ? 'Opening your request…' : 'Opening your reward…';
        window.CustomerNavigation.navigate(destination);
      });
    });
  };
})();
