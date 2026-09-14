/* Run against browser_tests/server.py. Set PLAYWRIGHT_MODULE to an existing
 * Playwright installation; this adds no application/runtime dependency. */
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = 'http://127.0.0.1:8766';

(async () => {
  const browser = await chromium.launch({headless: true, args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream']});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  const page = await context.newPage();
  const errors = [];
  const claims = [];
  let passed = 0;
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  page.on('request', request => { if (request.url().includes('/points/claim-')) claims.push(request); });
  // WSGI fixtures exercise real notification HTTP endpoints; sockets are
  // instrumented here to check that navigation never duplicates shared ones.
  await context.addInitScript(() => {
    window.__documentToken = crypto.randomUUID();
    window.__navigations = 0;
    window.__sockets = [];
    window.addEventListener('customer:navigated', () => window.__navigations++);
    window.WebSocket = class extends EventTarget {
      constructor(url) { super(); this.url = url; this.readyState = 1; window.__sockets.push(this); }
      send() {}
      close() { this.readyState = 3; }
    };
  });
  async function scenario(name, fn) { await fn(); passed++; console.log(`PASS ${passed}: ${name}`); }
  async function fixture(data) { return (await page.request.post(`${base}/__fixture__/`, {form: data})).json(); }
  async function waitNavigation(action) {
    const before = await page.evaluate(() => window.__navigations);
    await action();
    await page.waitForFunction(count => window.__navigations > count, before, {timeout: 35000});
  }
  async function navigate(name) {
    await waitNavigation(() => name === 'dashboard'
      ? page.locator('.customer-info-back').click()
      : page.locator(`.customer-secondary-nav a[href="/customer/${name}/"]`).click());
    assert.equal(await page.locator('#customer-page-top').getAttribute('data-page'), name.replaceAll('-', '_'));
  }
  async function verifyIdentity() {
    assert.equal(await page.evaluate(() =>
      window.__originalAudio === document.querySelector('.customer-music-audio') &&
      window.__originalHeader === document.querySelector('body > header') &&
      window.__originalNav === document.querySelector('.customer-secondary-nav') &&
      window.__originalBell === document.querySelector('#bellBtn') &&
      window.__originalPlayer === document.querySelector('#customer-music-control')), true);
  }
  async function mediaState() {
    return page.evaluate(() => {
      const audio = document.querySelector('.customer-music-audio');
      return {src: audio.currentSrc, time: audio.currentTime, paused: audio.paused};
    });
  }
  try {
    await page.request.get(`${base}/__fixture__/?login=1`);
    await fixture({day: '0', feedback_reset: '1'});
    await page.goto(`${base}/customer/dashboard/`, {waitUntil: 'domcontentloaded'});
    await page.waitForFunction(() => typeof window.submitQuickFeedback === 'function');
    const documentToken = await page.evaluate(() => window.__documentToken);
    await page.evaluate(() => {
      window.__originalAudio = document.querySelector('.customer-music-audio');
      window.__originalHeader = document.querySelector('body > header');
      window.__originalNav = document.querySelector('.customer-secondary-nav');
      window.__originalBell = document.querySelector('#bellBtn');
      window.__originalPlayer = document.querySelector('#customer-music-control');
      window.__originalAudio.muted = true; // Real Jamendo stream, silent test machine.
    });
    await scenario('Source assets load, initial player does not autoplay', async () => {
      const state = await mediaState();
      assert.equal(state.src, '');
      assert.equal(state.paused, true);
      assert.equal(await page.locator('#points-total').textContent(), '0 Points');
      const served = await page.request.get(`${base}/static/js/customer_navigation.js`);
      assert.equal(served.status(), 200);
      assert.match(await served.text(), /Stage 1: only the explicitly registered/);
    });
    await scenario('Real Jamendo playback starts only after Play', async () => {
      await page.locator('.customer-music-play').click();
      await page.waitForFunction(() => {
        const audio = document.querySelector('.customer-music-audio');
        return !audio.paused && audio.currentTime > 1 && audio.readyState >= 3;
      }, null, {timeout: 40000});
      assert.match((await mediaState()).src, /storage\.jamendo\.com/);
    });
    await scenario('Same playing audio through Dashboard → News → Weather → Discounts → Offers → Dashboard', async () => {
      const before = await mediaState();
      await page.evaluate(() => {
        window.__audioInterruptions = [];
        for (const event of ['pause', 'emptied', 'loadstart'])
          window.__originalAudio.addEventListener(event, () => window.__audioInterruptions.push(event));
      });
      for (const name of ['news', 'weather', 'discounts', 'offers', 'dashboard']) {
        await navigate(name);
        await verifyIdentity();
        assert.equal((await mediaState()).src, before.src);
        assert.equal((await mediaState()).paused, false);
      }
      const after = await mediaState();
      assert.ok(after.time > before.time, `${before.time} -> ${after.time}`);
      assert.deepEqual(await page.evaluate(() => window.__audioInterruptions), []);
      assert.equal(await page.evaluate(() => window.__documentToken), documentToken);
      console.log(`  Continuous playback advanced ${before.time.toFixed(2)}s → ${after.time.toFixed(2)}s; no pause/load/empty events.`);
    });
    await scenario('Daily claim +10, duplicate blocked, authoritative state survives return', async () => {
      const before = claims.length;
      await page.locator('#points-daily').click();
      await page.waitForFunction(() => document.querySelector('#points-total').textContent === '10 Points');
      assert.equal(claims.length, before + 1);
      assert.equal(await page.locator('#points-daily').isDisabled(), true);
      const duplicate = await page.evaluate(async () => {
        const controls = document.querySelector('#points-controls');
        const response = await fetch(controls.dataset.dailyUrl, {method: 'POST', headers: {
          'X-CSRFToken': controls.querySelector('[name=csrfmiddlewaretoken]').value,
        }});
        return response.json();
      });
      assert.equal(duplicate.state.total_points, 10);
      await navigate('offers'); await navigate('dashboard');
      assert.equal(await page.locator('#points-total').textContent(), '10 Points');
      assert.match(await page.locator('#points-streak').textContent(), /1 Day in a Row/);
      assert.equal(await page.locator('#points-daily').isDisabled(), true);
    });
    await scenario('Day 7 manual bonus, duplicate prevention and day 8 no repeat', async () => {
      await fixture({day: '7', points: '70', ago: '1'});
      await navigate('offers'); await navigate('dashboard');
      assert.equal(await page.locator('#points-bonus-label').textContent(), 'Claim +35 Points');
      await page.locator('#points-bonus').click();
      await page.waitForFunction(() => document.querySelector('#points-total').textContent === '105 Points');
      assert.equal(await page.locator('#points-bonus').isDisabled(), true);
      await page.locator('#points-daily').click();
      await page.waitForFunction(() => document.querySelector('#points-total').textContent === '115 Points');
      assert.match(await page.locator('#points-streak').textContent(), /8 Days/);
    });
    await scenario('Day 14 manual bonus and next cycle without a duplicate bonus', async () => {
      await fixture({day: '14', points: '175', ago: '1', day7: '1'});
      await navigate('offers'); await navigate('dashboard');
      assert.equal(await page.locator('#points-bonus-label').textContent(), 'Claim +50 Points');
      await page.locator('#points-bonus').click();
      await page.waitForFunction(() => document.querySelector('#points-total').textContent === '225 Points');
      await page.locator('#points-daily').click();
      await page.waitForFunction(() => document.querySelector('#points-total').textContent === '235 Points');
      assert.match(await page.locator('#points-streak').textContent(), /1 Day in a Row/);
    });
    await scenario('Both FAQ accordions work with keyboard after repeated visits', async () => {
      for (let round = 0; round < 3; round++) {
        for (const [name, prefix] of [['how-points-work', 'points'], ['discounts', 'discounts']]) {
          await navigate(name);
          const button = page.locator(`.${prefix}-faq-question`).first();
          assert.equal(await button.getAttribute('aria-expanded'), 'false');
          await button.focus(); await button.press('Enter');
          assert.equal(await button.getAttribute('aria-expanded'), 'true');
          await button.press('Space');
          assert.equal(await button.getAttribute('aria-expanded'), 'false');
        }
      }
      await verifyIdentity();
    });
    await scenario('News categories, Featured Story, cards and external links', async () => {
      await navigate('news');
      for (const category of ['sports', 'world', 'gaming']) {
        await waitNavigation(() => page.locator(`.customer-news-categories a[href$="category=${category}"]`).click());
        assert.equal(new URL(page.url()).searchParams.get('category'), category);
        assert.match(await page.locator('.customer-news-categories [aria-current=page]').textContent(), new RegExp(category, 'i'));
        assert.match(await page.locator('#customer-news').textContent(), /Featured Story/);
        assert.match(await page.locator('#customer-news').textContent(), /Latest News/);
        assert.equal(await page.locator('.customer-news-link').count(), 6);
        assert.equal(await page.locator('.customer-news-link').first().getAttribute('target'), '_blank');
      }
    });
    await scenario('Weather GET city search, forecast, not-found and API fallback', async () => {
      await navigate('weather');
      for (const city of ['Bucharest', 'unknown', 'unavailable', 'Paris']) {
        await page.getByLabel('Search for a city', {exact: true}).fill(city);
        await waitNavigation(() => page.locator('.weather-search button').click());
        assert.equal(new URL(page.url()).searchParams.get('city'), city);
        if (city === 'unknown') assert.match(await page.locator('.weather-message').textContent(), /find that city/);
        else if (city === 'unavailable') assert.match(await page.locator('.weather-message').textContent(), /temporarily unavailable/);
        else {
          assert.equal(await page.locator('#weather-city-name').textContent(), city);
          assert.equal(await page.locator('.weather-day').count(), 7);
        }
      }
      await verifyIdentity();
    });
    await scenario('Back/Forward preserve query, title, active nav, scroll and audio', async () => {
      await page.evaluate(() => scrollTo({top: 400, behavior: 'instant'}));
      await page.waitForTimeout(200);
      const savedY = await page.evaluate(() => scrollY);
      const pausedBefore = (await mediaState()).paused;
      // Avoid Playwright scrolling the off-screen header into view before the click.
      await waitNavigation(() => page.evaluate(() => document.querySelector('.customer-secondary-nav a[href="/customer/offers/"]').click()));
      await waitNavigation(() => page.goBack());
      assert.equal(new URL(page.url()).searchParams.get('city'), 'Paris');
      assert.match(await page.title(), /Weather/);
      assert.equal((await page.locator('.customer-secondary-nav [aria-current=page]').textContent()).trim(), 'Weather');
      assert.ok(Math.abs(await page.evaluate(() => scrollY) - savedY) < 3, `Expected restored scroll ${savedY}, received ${await page.evaluate(() => scrollY)}`);
      await waitNavigation(() => page.goForward());
      assert.match(await page.title(), /Offers/);
      assert.equal((await mediaState()).paused, pausedBefore);
      await verifyIdentity();
    });
    await scenario('Notification bell, outside-close and single shared socket', async () => {
      for (const name of ['news', 'discounts', 'offers']) {
        await navigate(name);
        await page.locator('#bellBtn').click();
        await page.waitForFunction(() => !document.querySelector('#notificationDropdown').classList.contains('hidden'));
        await page.waitForFunction(() => document.querySelector('#notificationList').textContent.includes('Your account notification'));
        await page.locator('h1').click();
        assert.equal(await page.locator('#notificationDropdown').evaluate(el => el.classList.contains('hidden')), true);
        await page.locator('#bellBtn').click();
        // The existing absolutely positioned panel can overlap the bell when
        // populated. Exercise its unchanged toggle through keyboard activation.
        await page.locator('#bellBtn').focus();
        await page.locator('#bellBtn').press('Enter');
        assert.equal(await page.locator('#notificationDropdown').evaluate(el => el.classList.contains('hidden')), true);
      }
      assert.equal(await page.evaluate(() => window.__sockets.filter(s => s.url.includes('/ws/notifications/') && s.readyState === 1).length), 1);
    });
    await scenario('Feedback drafts, quick and expanded POST, rating and country', async () => {
      await navigate('dashboard');
      await page.locator('#quickMessage').fill('Saved draft');
      await navigate('offers'); await navigate('dashboard');
      assert.equal(await page.locator('#quickMessage').inputValue(), 'Saved draft');
      await page.locator('#sendButton').click();
      await page.waitForFunction(() => document.querySelector('#quickMessage').value === '');
      await page.locator('.expand-button').click();
      await page.locator('#feedbackInput').fill('Expanded feedback');
      await page.locator('#countryInput').fill('Romania');
      await page.locator('.star').nth(3).click();
      await page.locator('#expandedFeedback .send-button').click();
      await page.waitForFunction(() => document.querySelector('#feedbackInput').value === '');
      await page.locator('#expandedFeedback .chat-header button').click();
      const data = await (await page.request.get(`${base}/__fixture__/`)).json();
      assert.equal(data.feedback.length, 2);
      assert.deepEqual(data.feedback[1], {message: 'Expanded feedback', rating: 4, country: 'Romania'});
    });
    await scenario('Dashboard tabs, assistant, microphone cleanup on repeated navigation', async () => {
      await page.locator('.tab-btn[data-tab=events]').click();
      assert.equal(await page.locator('#events').evaluate(el => el.classList.contains('active')), true);
      await page.locator('#chat-toggle-btn').click();
      await page.waitForTimeout(1600);
      await page.locator('#chat-body button').filter({hasText: /^Yes$/}).click();
      assert.match(await page.locator('#chat-body').textContent(), /Technical Support/);
      await page.locator('#chat-close-btn').click();
      await page.locator('.expand-button').click();
      await page.locator('#startRecord').click();
      await page.waitForFunction(() => document.querySelector('#startRecord').disabled);
      await page.waitForTimeout(300);
      await page.locator('#stopRecord').click();
      await page.waitForFunction(() => document.querySelector('#audioInput').files.length === 1);
      await page.locator('#expandedFeedback .chat-header button').click();
      for (let i = 0; i < 3; i++) { await navigate('offers'); await navigate('dashboard'); }
      assert.equal(await page.locator('#audioInput').evaluate(el => el.files.length), 1);
      assert.equal(await page.evaluate(() => window.__sockets.filter(s => s.url.includes('/ws/chat/') && s.readyState === 1).length), 1);
      assert.ok(await page.locator('#vanta-background canvas').count() <= 1);
    });
    await scenario('Pause persists across navigation; Next, Previous and Close/reopen work', async () => {
      if (!(await mediaState()).paused) await page.locator('.customer-music-play').click();
      const paused = await mediaState();
      await navigate('offers'); await navigate('news');
      const after = await mediaState();
      assert.equal(after.paused, true);
      assert.ok(Math.abs(after.time - paused.time) < 0.1);
      await page.locator('.customer-music-play').click();
      await page.waitForFunction(() => !document.querySelector('.customer-music-audio').paused);
      const first = (await mediaState()).src;
      await page.locator('.customer-music-next').click();
      await page.waitForFunction(src => {
        const audio = document.querySelector('.customer-music-audio');
        return !audio.paused && audio.currentSrc !== src && audio.currentTime > 0;
      }, first, {timeout: 35000});
      await navigate('weather');
      await page.locator('.customer-music-previous').click();
      await page.waitForFunction(src => document.querySelector('.customer-music-audio').currentSrc === src && !document.querySelector('.customer-music-audio').paused, first);
      await page.locator('.customer-music-close').click();
      assert.equal(await page.locator('.customer-music-bar').isVisible(), false);
      assert.equal((await mediaState()).paused, true);
      await page.locator('.customer-music-toggle').click();
      assert.equal(await page.locator('.customer-music-bar').isVisible(), true);
      await verifyIdentity();
    });
    await scenario('Failed navigation keeps current content and supports retry', async () => {
      await page.route('**/customer/offers/', route => route.fulfill({status: 503, body: 'Unavailable'}), {times: 1});
      await page.locator('.customer-secondary-nav a[href="/customer/offers/"]').click();
      await page.locator('#customer-navigation-error').waitFor({state: 'visible'});
      assert.equal(await page.locator('#customer-page-top').getAttribute('data-page'), 'weather');
      assert.match(page.url(), /\/weather\//);
      await waitNavigation(() => page.locator('#customer-navigation-error button').click());
      assert.match(page.url(), /\/offers\//);
      await verifyIdentity();
    });
    await scenario('Failed Back restores the matching URL and preserves the shell', async () => {
      await page.route('**/customer/weather/', route => route.fulfill({status: 503, body: 'Unavailable'}), {times: 1});
      await page.goBack();
      await page.locator('#customer-navigation-error').waitFor({state: 'visible'});
      await page.waitForURL('**/customer/offers/');
      assert.equal(await page.locator('#customer-page-top').getAttribute('data-page'), 'offers');
      await verifyIdentity();
    });
    await scenario('Rapid navigation commits only the final destination', async () => {
      await waitNavigation(() => page.evaluate(() => {
        document.querySelector('.customer-secondary-nav a[href="/customer/news/"]').click();
        document.querySelector('.customer-secondary-nav a[href="/customer/discounts/"]').click();
      }));
      await page.waitForTimeout(300);
      assert.match(page.url(), /\/discounts\//);
      assert.equal(await page.locator('#customer-page-top').getAttribute('data-page'), 'discounts');
      await verifyIdentity();
    });
    await scenario('Navigating during a Daily POST waits without retrying or losing its result', async () => {
      await fixture({day: '0'});
      await navigate('dashboard');
      await page.route('**/customer/points/claim-daily/', async route => {
        await new Promise(resolve => setTimeout(resolve, 400));
        await route.continue();
      }, {times: 1});
      const before = claims.length;
      await page.locator('#points-daily').click();
      await navigate('offers');
      assert.equal(claims.length, before + 1);
      await navigate('dashboard');
      assert.equal(await page.locator('#points-total').textContent(), '10 Points');
      assert.equal(await page.locator('#points-daily').isDisabled(), true);
    });
    await scenario('Same-document anchors and their Back entry preserve audio', async () => {
      await page.evaluate(() => {
        const link = document.createElement('a');
        link.href = '#events'; document.body.append(link); link.click(); link.remove();
      });
      assert.equal(new URL(page.url()).hash, '#events');
      await page.goBack();
      await page.waitForURL('**/customer/dashboard/');
      await verifyIdentity();
      assert.equal(await page.evaluate(() => window.__documentToken), documentToken);
    });
    await scenario('Mobile/tablet navigation and page styles stay isolated', async () => {
      for (const width of [768, 390]) {
        await page.setViewportSize({width, height: 844});
        await navigate('news');
        const categories = await page.locator('.customer-news-categories').evaluate(el => ({overflow: getComputedStyle(el).overflowX, width: el.clientWidth, scroll: el.scrollWidth}));
        assert.equal(categories.overflow, 'auto');
        assert.ok(categories.scroll >= categories.width);
        await navigate('weather');
        const newsStyles = await page.locator('head [data-customer-page-style]').evaluateAll(nodes => nodes.some(node => node.textContent.includes('.customer-news-categories')));
        assert.equal(newsStyles, false);
        const box = await page.locator('#customer-music-control').boundingBox();
        assert.ok(box.x >= 0 && box.x + box.width <= width);
        await verifyIdentity();
      }
      await page.setViewportSize({width: 1440, height: 1000});
    });
    await scenario('Unsupported actions, ChatMe and modified/download links are not intercepted', async () => {
      const result = await page.evaluate(() => {
        const check = (href, options = {}, attrs = {}) => {
          const link = document.createElement('a'); link.href = href;
          Object.entries(attrs).forEach(([key, value]) => link.setAttribute(key, value));
          document.body.append(link);
          let intercepted;
          const after = event => { intercepted = event.defaultPrevented; event.preventDefault(); };
          window.addEventListener('click', after, {once: true});
          link.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, button: 0, ...options}));
          link.remove(); return intercepted;
        };
        return [check('/customer/chat/me/'), check('/customer/points/claim-daily/'),
          check('https://example.com/'), check('/customer/offers/', {ctrlKey: true}),
          check('/customer/offers/', {}, {download: 'offer'}), check('/customer/offers/', {}, {target: '_blank'})];
      });
      assert.deepEqual(result, [false, false, false, false, false, false]);
    });
    await scenario('No page script errors after repeated navigation', async () => { assert.deepEqual(errors, []); });
    await scenario('Logout resets music and remains a normal CSRF POST', async () => {
      await page.evaluate(() => {
        window.__logoutStopped = false;
        window.addEventListener('customer:session-ended', () => {
          window.__logoutStopped = document.querySelector('.customer-music-audio').paused;
        });
        // Observe and suppress only the browser default for this assertion.
        window.addEventListener('submit', event => event.preventDefault(), {once: true});
        document.querySelector('form[action*="logout"]').requestSubmit();
      });
      assert.equal(await page.evaluate(() => window.__logoutStopped), true);
      assert.equal(await page.locator('.customer-music-previous').isDisabled(), true);
      const response = await page.evaluate(async () => {
        const form = document.querySelector('form[action*="logout"]');
        return (await fetch(form.action, {method: 'POST', body: new FormData(form)})).status;
      });
      assert.ok(response < 400);
      assert.equal((await page.request.get(`${base}/customer/session/`)).status(), 401);
    });
    console.log(`RESULT: ${passed} browser scenarios passed.`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
