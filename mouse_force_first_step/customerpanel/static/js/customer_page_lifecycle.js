/* Explicit page lifecycles: fetched HTML never supplies executable scripts. */
(() => {
  'use strict';
  let active = null;
  let dashboardDraft = {};
  const assets = new Map();

  function scriptOnce(url, ready) {
    if (ready()) return Promise.resolve();
    if (!assets.has(url)) {
      assets.set(url, new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = url;
        const timer = setTimeout(() => { script.remove(); reject(new Error('Asset timeout')); }, 12000);
        script.onload = () => { clearTimeout(timer); resolve(); };
        script.onerror = () => { clearTimeout(timer); script.remove(); reject(new Error('Asset unavailable')); };
        document.head.append(script);
      }).catch(error => { assets.delete(url); throw error; }));
    }
    return assets.get(url);
  }

  function createScope(top, main) {
    const abort = new AbortController();
    const timers = new Set();
    const pending = new Set();
    const cleanups = [];
    // Keep references to this visit's nodes, not the persistent host elements.
    // Late callbacks must never find identically named controls on a later visit.
    const roots = [...top.children, ...main.children];
    const find = selector => {
      for (const root of roots) {
        const match = root.matches(selector) ? root : root.querySelector(selector);
        if (match) return match;
      }
      return null;
    };
    const scope = {
      alive: true,
      document: {
        getElementById: id => find(`#${CSS.escape(id)}`),
        querySelector: find,
        querySelectorAll: selector => roots.flatMap(root => [
          ...(root.matches(selector) ? [root] : []), ...root.querySelectorAll(selector),
        ]),
        createElement: (...args) => document.createElement(...args),
        get cookie() { return document.cookie; },
        body: document.body,
      },
      on(target, type, handler, options = {}) {
        target?.addEventListener(type, handler, {...options, signal: abort.signal});
      },
      cleanup(fn) { cleanups.push(fn); },
      expose(name, fn) {
        const previous = window[name];
        window[name] = fn;
        cleanups.push(() => {
          if (window[name] === fn) {
            if (previous === undefined) delete window[name]; else window[name] = previous;
          }
        });
      },
      timeout(fn, delay) {
        if (!scope.alive) return null;
        const timer = setTimeout(() => { timers.delete(timer); if (scope.alive) fn(); }, delay);
        timers.add(timer);
        return timer;
      },
      fetch(url, options = {}) {
        // A POST is never retried or assumed undone by aborting navigation.
        return fetch(url, {...options, signal: abort.signal});
      },
      track(promise) {
        pending.add(promise);
        promise.finally(() => pending.delete(promise)).catch(() => {});
        return promise;
      },
      async settle() {
        while (pending.size) await Promise.allSettled([...pending]);
      },
      dispose() {
        scope.alive = false;
        abort.abort();
        timers.forEach(clearTimeout);
        cleanups.reverse().forEach(fn => fn());
      },
    };
    return scope;
  }

  function faq(scope, prefix) {
    scope.document.querySelectorAll(`.${prefix}-faq-question`).forEach(button => {
      const answer = scope.document.getElementById(button.getAttribute('aria-controls'));
      scope.on(button, 'click', () => {
        const expanded = button.getAttribute('aria-expanded') !== 'true';
        button.setAttribute('aria-expanded', String(expanded));
        answer.setAttribute('aria-hidden', String(!expanded));
        answer.inert = !expanded;
        button.querySelector(`.${prefix}-faq-icon`).textContent = expanded ? '−' : '+';
      });
    });
  }

  window.CustomerPages = {
    async prepare(page) {
      if (page !== 'dashboard') return;
      const config = document.getElementById('customer-shell-config').dataset;
      // These are the same two existing Dashboard dependencies, loaded once.
      // A decorative effect failure must not prevent navigation or Points use.
      try {
        await scriptOnce(config.threeUrl, () => !!window.THREE);
        await scriptOnce(config.vantaUrl, () => !!window.VANTA?.CLOUDS);
      } catch (_) { /* The existing CSS background remains available. */ }
    },
    mount(page) {
      const scope = createScope(document.getElementById('customer-page-top'), document.getElementById('customer-page-main'));
      active = scope;
      if (page === 'dashboard') window.CustomerDashboard(scope, dashboardDraft);
      if (page === 'discounts') faq(scope, 'discounts');
      if (page === 'how_points_work') faq(scope, 'points');
      if (page === 'news') {
        const bar = scope.document.querySelector('#customer-news .customer-news-categories');
        if (bar) {
          const reveal = link => { bar.scrollLeft = Math.max(0, link.offsetLeft - (bar.clientWidth - link.offsetWidth) / 2); };
          const selected = bar.querySelector('[aria-current="page"]');
          if (selected) reveal(selected);
          scope.on(bar, 'focusin', event => { if (event.target.matches('a')) reveal(event.target); });
        }
      }
    },
    async beforeLeave() {
      const scope = active;
      if (!scope) return;
      await scope.settle();
      await scope.beforeLeave?.();
      await scope.settle();
    },
    unmount() { active?.dispose(); active = null; },
  };
  window.addEventListener('customer:session-ended', () => {
    window.CustomerPages.unmount();
    dashboardDraft = {};
  });
})();
