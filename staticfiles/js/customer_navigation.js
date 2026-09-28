/* Stage 1: only the explicitly registered customer GET pages use this shell. */
(() => {
  'use strict';
  const config = document.getElementById('customer-shell-config');
  const top = document.getElementById('customer-page-top');
  const main = document.getElementById('customer-page-main');
  if (!config || !top || !main || !window.CustomerPages) return;
  const pageNames = ['dashboard', 'discounts', 'how_points_work', 'rewards', 'offers', 'news', 'weather'];
  const routes = new Map(pageNames.map(name => [new URL(config.dataset[name], location.href).pathname, name]));
  const discountsPath = new URL(config.dataset.discounts, location.href).pathname;
  const rewardsPath = new URL(config.dataset.rewards, location.href).pathname;
  const redemptionsPath = new URL(config.dataset.redemptions, location.href).pathname.replace(/[^/]+\/$/, '');
  const uuidPath = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
  function pageFor(pathname) {
    if (routes.has(pathname)) return routes.get(pathname);
    if (pathname.startsWith(discountsPath) && new RegExp(`^${uuidPath}/$`).test(pathname.slice(discountsPath.length))) return 'discounts';
    if (pathname === redemptionsPath) return 'redemption_history';
    const suffix = pathname.startsWith(rewardsPath) ? pathname.slice(rewardsPath.length) : '';
    if (suffix === 'requests/') return 'reward_requests';
    if (suffix === 'requests/new/') return 'reward_request_new';
    if (new RegExp(`^requests/${uuidPath}/$`).test(suffix)) return 'reward_request_detail';
    if (new RegExp(`^requests/${uuidPath}/confirm/$`).test(suffix)) return 'reward_confirm';
    if (new RegExp(`^${uuidPath}/$`).test(suffix)) return 'reward_detail';
    if (new RegExp(`^${uuidPath}/confirm/$`).test(suffix)) return 'reward_confirm';
    const result = pathname.startsWith(redemptionsPath) ? pathname.slice(redemptionsPath.length) : '';
    return new RegExp(`^${uuidPath}/$`).test(result) ? 'redemption_result' : null;
  }
  function sectionFor(pathname) {
    const page = pageFor(pathname);
    if (['discounts', 'rewards', 'offers', 'news', 'weather'].includes(page)) return page;
    return page?.startsWith('reward_') || page?.startsWith('redemption_') ? 'rewards' : null;
  }
  const session = top.dataset.session;
  const pages = window.CustomerPages;
  const errorBox = document.getElementById('customer-navigation-error');
  const positions = new Map();
  let generation = 0;
  let controller = null;
  let currentURL = location.href;
  let entry = {index: 0, key: crypto.randomUUID()};
  let restoring = false;
  let stopped = false;
  let retryURL = null;
  let checkingSession = false;
  const initial = pages.prepare(top.dataset.page).then(() => {
    if (!stopped) pages.mount(top.dataset.page);
  });

  // Legacy non-customer Dashboard rendering stays unchanged; no shell navigation.
  if (!session || !pageFor(location.pathname)) return;
  history.scrollRestoration = 'manual';
  history.replaceState({...history.state, customerShell: entry}, '', location.href);

  function supported(url) { return url.origin === location.origin && !!pageFor(url.pathname); }
  function remember() {
    positions.set(entry.key, [scrollX, scrollY]);
    if (positions.size > 100) positions.delete(positions.keys().next().value);
  }
  function shutdown() {
    if (stopped) return;
    stopped = true;
    generation += 1;
    controller?.abort();
    window.dispatchEvent(new Event('customer:session-ended'));
  }
  function leaveSession(url) {
    shutdown();
    location.assign(url);
  }
  async function checkSession() {
    if (stopped || checkingSession) return;
    checkingSession = true;
    try {
      const response = await fetch(config.dataset.sessionUrl, {credentials: 'same-origin', cache: 'no-store'});
      if (response.status === 401 || response.status === 403) return leaveSession(location.href);
      if (response.ok && (await response.json()).session !== session) leaveSession(location.href);
    } catch (_) { /* A temporary network failure does not stop an authorized stream. */ }
    finally { checkingSession = false; }
  }

  async function stageStyles(doc, signal) {
    const nodes = [...doc.head.querySelectorAll('[data-customer-page-style]')];
    const staged = [];
    try {
      await Promise.all(nodes.map(node => {
        if (node.tagName !== 'STYLE' && !(node.tagName === 'LINK' && node.rel === 'stylesheet')) {
          throw new Error('Unexpected page asset');
        }
        const copy = document.importNode(node, true);
        copy.media = 'not all';
        copy.dataset.customerStaged = '';
        staged.push(copy);
        if (copy.tagName === 'STYLE') { document.head.append(copy); return; }
        const url = new URL(node.getAttribute('href'), location.href);
        if (url.origin !== location.origin || !url.pathname.startsWith('/static/')) throw new Error('Unexpected stylesheet');
        copy.href = url.href;
        return new Promise((resolve, reject) => {
          const finish = error => {
            clearTimeout(timer);
            signal.removeEventListener('abort', cancel);
            copy.onload = copy.onerror = null;
            error ? reject(error) : resolve();
          };
          const cancel = () => finish(new DOMException('Aborted', 'AbortError'));
          const timer = setTimeout(() => finish(new Error('Stylesheet timeout')), 12000);
          copy.onload = () => finish();
          copy.onerror = () => finish(new Error('Stylesheet unavailable'));
          signal.addEventListener('abort', cancel, {once: true});
          document.head.append(copy);
          if (signal.aborted) cancel();
        });
      }));
      return staged;
    } catch (error) {
      staged.forEach(node => node.remove());
      throw error;
    }
  }

  function importContent(region) {
    const fragment = document.createDocumentFragment();
    for (const node of region.childNodes) fragment.append(document.importNode(node, true));
    // The only scripts retained in a region are inert server JSON state.
    fragment.querySelectorAll('script').forEach(script => {
      if (script.type !== 'application/json') script.remove();
    });
    return fragment;
  }

  function waitFor(work, signal) {
    // Cancelling navigation does not cancel or retry a submitted mutation.
    // Its page scope stays alive to receive the authoritative response.
    return new Promise((resolve, reject) => {
      const cancel = () => reject(new DOMException('Aborted', 'AbortError'));
      signal.addEventListener('abort', cancel, {once: true});
      Promise.resolve(work).then(resolve, reject).finally(() => signal.removeEventListener('abort', cancel));
      if (signal.aborted) cancel();
    });
  }

  async function navigate(destination, targetEntry = null) {
    const url = new URL(destination, location.href);
    if (stopped || !supported(url)) return;
    const operation = ++generation;
    controller?.abort();
    const request = new AbortController();
    controller = request;
    let timeout = null; // Time spent reviewing a purchase is not a navigation timeout.
    const oldEntry = entry;
    function restoreLockedHistory() {
      if (targetEntry && targetEntry.index !== oldEntry.index) {
        restoring = true;
        history.go(oldEntry.index - targetEntry.index);
        targetEntry = null;
      }
    }
    let styles = [];
    errorBox.hidden = true;
    main.setAttribute('aria-busy', 'true');
    main.inert = true;
    top.inert = true;
    remember();
    try {
      await waitFor(initial, request.signal);
      await waitFor(pages.beforeLeave(), request.signal);
      await waitFor(window.CustomerSectionAccess.settle(), request.signal);
      if (operation !== generation || stopped) return;
      const section = sectionFor(url.pathname);
      if (section && !window.CustomerSectionAccess.unlocked(section)) {
        restoreLockedHistory();
        if (!await waitFor(window.CustomerSectionAccess.ensure(section), request.signal)) return;
      }
      if (operation !== generation || stopped) return;
      timeout = setTimeout(() => request.abort(), 25000);
      const loadPage = () => fetch(url.href, {
        credentials: 'same-origin', cache: 'no-store', signal: request.signal,
        headers: {'X-Customer-Navigation': '1', 'Accept': 'text/html'},
      });
      let response = await loadPage();
      // Access denial is not an expired session; keep the shell/audio alive.
      if (section && response.status === 403 && response.headers.get('X-Customer-Section-Locked') === section) {
        clearTimeout(timeout);
        restoreLockedHistory();
        if (!await waitFor(window.CustomerSectionAccess.ensure(section, true), request.signal)) return;
        timeout = setTimeout(() => request.abort(), 25000);
        response = await loadPage();
      }
      const finalURL = new URL(response.url);
      if (response.status === 401 || response.status === 403 || (response.redirected && !supported(finalURL))) {
        return leaveSession(url.href);
      }
      if (!response.ok || !supported(finalURL) || !response.headers.get('Content-Type')?.includes('text/html')) {
        throw new Error('Page unavailable');
      }
      const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
      const nextTop = doc.getElementById('customer-page-top');
      const nextMain = doc.getElementById('customer-page-main');
      if (!nextTop || !nextMain || nextTop.dataset.page !== pageFor(finalURL.pathname)) throw new Error('Unsupported page');
      if (nextTop.dataset.session !== session) return leaveSession(url.href);
      styles = await stageStyles(doc, request.signal);
      await waitFor(pages.prepare(nextTop.dataset.page), request.signal);
      // A user may have submitted a claim while the destination was fetching.
      await waitFor(pages.beforeLeave(), request.signal);
      if (operation !== generation || stopped) return;
      if (request.signal.aborted) throw new DOMException('Aborted', 'AbortError');
      const nextContent = importContent(nextMain);
      const nextIntro = importContent(nextTop);
      pages.unmount();
      document.head.querySelectorAll('[data-customer-page-style]:not([data-customer-staged])').forEach(node => node.remove());
      styles.forEach(node => { delete node.dataset.customerStaged; node.media = ''; });
      styles = [];
      top.replaceChildren(nextIntro);
      main.replaceChildren(nextContent);
      top.dataset.page = nextTop.dataset.page;
      document.title = doc.title;
      document.querySelectorAll('.customer-secondary-nav a[href]').forEach(link => {
        const activePath = ['reward_detail', 'reward_confirm', 'redemption_result', 'redemption_history', 'reward_requests', 'reward_request_new', 'reward_request_detail'].includes(nextTop.dataset.page) ? rewardsPath : nextTop.dataset.page === 'discounts' ? discountsPath : url.pathname;
        if (new URL(link.href).pathname === activePath) link.setAttribute('aria-current', 'page');
        else link.removeAttribute('aria-current');
      });
      pages.mount(nextTop.dataset.page);
      window.CustomerSectionAccess.adopt(doc);
      main.inert = false;
      top.inert = false;
      if (targetEntry) entry = targetEntry;
      else {
        entry = {index: oldEntry.index + 1, key: crypto.randomUUID()};
        history.pushState({customerShell: entry}, '', url.href);
      }
      currentURL = url.href;
      const heading = main.querySelector('h1, h2');
      if (heading) {
        heading.setAttribute('tabindex', '-1');
        heading.focus({preventScroll: true});
      }
      const saved = targetEntry && positions.get(targetEntry.key);
      const anchor = url.hash && document.getElementById(decodeURIComponent(url.hash.slice(1)));
      if (saved) scrollTo({left: saved[0], top: saved[1], behavior: 'instant'});
      else if (anchor) anchor.scrollIntoView();
      else scrollTo({top: 0, left: 0, behavior: 'instant'});
      window.dispatchEvent(new CustomEvent('customer:navigated', {detail: {page: nextTop.dataset.page}}));
    } catch (error) {
      if (operation !== generation || stopped) return;
      retryURL = url.href;
      errorBox.hidden = false;
      // Failed Back/Forward must not leave the URL describing different content.
      if (targetEntry && targetEntry.index !== oldEntry.index) {
        restoring = true;
        history.go(oldEntry.index - targetEntry.index);
      }
    } finally {
      clearTimeout(timeout);
      styles.forEach(node => node.remove());
      if (operation === generation) {
        main.removeAttribute('aria-busy');
        main.inert = false;
        top.inert = false;
      }
    }
  }

  // Page actions may navigate to a server-returned GET result. The same route
  // whitelist applies; this API cannot submit purchases or reveal requests.
  function updateDiscountQuery(destination, replace = false) {
    const url = new URL(destination, location.href);
    if (stopped || url.origin !== location.origin || url.pathname !== discountsPath || location.pathname !== discountsPath) return;
    remember();
    if (!replace) entry = {index: entry.index + 1, key: crypto.randomUUID()};
    history[replace ? 'replaceState' : 'pushState']({customerShell: entry}, '', url.href);
    currentURL = url.href;
  }
  window.CustomerNavigation = Object.freeze({navigate, updateDiscountQuery, endSession: () => leaveSession(location.href)});

  document.addEventListener('click', event => {
    if (stopped || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest('a[href]');
    if (!link || link.hasAttribute('download') || (link.target && link.target !== '_self') || link.hasAttribute('data-no-customer-navigation')) return;
    const url = new URL(link.href, location.href);
    if (!supported(url)) return;
    if (url.pathname === location.pathname && url.search === location.search && (url.hash || link.getAttribute('href').startsWith('#'))) {
      event.preventDefault();
      remember();
      entry = {index: entry.index + 1, key: crypto.randomUUID()};
      history.pushState({customerShell: entry}, '', url.href);
      currentURL = url.href;
      let anchor = null;
      try { anchor = document.getElementById(decodeURIComponent(url.hash.slice(1))); } catch (_) {}
      if (anchor) anchor.scrollIntoView(); else scrollTo({top: 0, behavior: 'instant'});
      return;
    }
    event.preventDefault(); // Do not stop propagation: notification outside-click stays intact.
    navigate(url.href);
  });
  document.addEventListener('submit', event => {
    const form = event.target;
    // Logout remains an ordinary CSRF-protected POST and full document departure.
    if (new URL(form.action).pathname === new URL(config.dataset.logoutUrl, location.href).pathname) {
      shutdown();
      logoutChannel?.postMessage({type: 'logout', session});
      return;
    }
    if (stopped || event.defaultPrevented || form.method.toLowerCase() !== 'get' || form.target) return;
    const url = new URL(form.action);
    if (!supported(url) || !form.matches('.weather-search')) return;
    event.preventDefault();
    url.search = new URLSearchParams(new FormData(form, event.submitter || undefined)).toString();
    navigate(url.href);
  });
  window.addEventListener('popstate', event => {
    if (restoring) { restoring = false; return; }
    const target = event.state?.customerShell;
    if (!target || !supported(new URL(location.href))) { leaveSession(location.href); return; }
    const previous = new URL(currentURL);
    if (previous.pathname === location.pathname && previous.search === location.search) {
      generation += 1;
      controller?.abort();
      remember();
      entry = target;
      currentURL = location.href;
      const saved = positions.get(target.key);
      if (saved) scrollTo({left: saved[0], top: saved[1], behavior: 'instant'});
      main.inert = false;
      top.inert = false;
      main.removeAttribute('aria-busy');
      return;
    }
    navigate(location.href, target);
  });
  window.addEventListener('scroll', remember, {passive: true});
  window.addEventListener('focus', checkSession);
  errorBox.querySelector('button').addEventListener('click', () => { if (retryURL) navigate(retryURL); });
  const logoutChannel = 'BroadcastChannel' in window ? new BroadcastChannel('customer-shell-session') : null;
  if (logoutChannel) logoutChannel.onmessage = event => {
    if (event.data?.type === 'logout' && event.data.session === session) shutdown();
  };
  const sessionTimer = setInterval(() => {
    if (!document.querySelector('.customer-music-audio').paused) checkSession();
  }, 60000);
  window.addEventListener('pagehide', () => {
    stopped = true;
    controller?.abort();
    pages.unmount();
    clearInterval(sessionTimer);
    logoutChannel?.close();
  });
  // A BFCache restoration after leaving the shell is a genuine document return.
  window.addEventListener('pageshow', event => { if (event.persisted) location.reload(); });
})();
