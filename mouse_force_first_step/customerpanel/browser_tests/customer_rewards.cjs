/* Run against browser_tests/server.py with PLAYWRIGHT_MODULE set, as for
 * customer_shell.cjs. Uses an isolated customer and real Jamendo playback. */
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.CUSTOMER_BROWSER_BASE || 'http://127.0.0.1:8766';

(async () => {
  const browser = await chromium.launch({headless: true});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  await context.addInitScript(() => {
    window.__visits = 0;
    window.addEventListener('customer:navigated', () => window.__visits++);
    // The isolated WSGI fixture has no WebSocket server.
    window.WebSocket = class extends EventTarget { send() {} close() {} };
  });
  const page = await context.newPage();
  // Only the public image fixture is intercepted; audio uses real Jamendo.
  await page.route('https://example.com/reward.png', route => route.fulfill({
    contentType: 'image/png', body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a/8sAAAAASUVORK5CYII=', 'base64'),
  }));
  const errors = [];
  const requests = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => requests.push(request));
  const visibleCards = () => page.locator('[data-reward-card]:visible');
  async function scenario(name, fn) { await fn(); console.log(`PASS: ${name}`); }
  async function navigation(action) {
    const before = await page.evaluate(() => window.__visits);
    await action();
    await page.waitForFunction(count => window.__visits > count, before, {timeout: 35000});
  }
  async function visit(name) {
    await navigation(() => name === 'dashboard'
      ? page.locator('.customer-info-back').click()
      : page.locator(`.customer-secondary-nav a[href="/customer/${name}/"]`).click());
  }
  async function category(name) { await page.locator(`.rewards-categories [data-category="${name}"]`).click(); }
  async function availability(name) { await page.locator(`[data-availability="${name}"]`).click(); }
  async function identity() {
    assert.equal(await page.evaluate(() => window.__audio === document.querySelector('.customer-music-audio')
      && window.__header === document.querySelector('body > header')
      && window.__nav === document.querySelector('.customer-secondary-nav')), true);
  }
  try {
    await page.request.get(`${base}/__fixture__/?login=1`);
    await page.request.post(`${base}/__fixture__/`, {form: {day: '0', points: '260', rewards_active: '1'}});
    await page.goto(`${base}/customer/offers/`, {waitUntil: 'networkidle'});
    await page.evaluate(() => {
      window.__audio = document.querySelector('.customer-music-audio');
      window.__header = document.querySelector('body > header');
      window.__nav = document.querySelector('.customer-secondary-nav');
      window.__audio.muted = true;
    });
    await scenario('Rewards opens through the existing shell with fresh balance and scoped styles', async () => {
      await visit('rewards');
      await identity();
      assert.equal(await page.title(), 'Rewards | MouseForce');
      assert.equal(await page.locator('#customer-page-top').getAttribute('data-page'), 'rewards');
      assert.equal(await page.locator('.customer-secondary-nav [aria-current="page"]').textContent(), 'Rewards');
      assert.equal(await page.locator('.rewards-balance strong').textContent(), '260 Points');
      assert.equal(await visibleCards().count(), 7);
      assert.equal(await page.locator('[data-reward-card][data-available="true"]').count(), 2);
      assert.match(await page.locator('[data-reward-card][data-category="beauty"]').textContent(), /You need 240 more points/);
      assert.equal(await page.locator('[data-rewards-filters]').isVisible(), true);
    });
    await scenario('All categories filter locally, with keyboard and combined availability filtering', async () => {
      const before = requests.length;
      for (const slug of ['beauty', 'food-drink', 'travel', 'entertainment', 'tech-gaming', 'shopping', 'experiences']) {
        await category(slug);
        assert.equal(await visibleCards().count(), 1);
        assert.equal(await visibleCards().getAttribute('data-category'), slug);
        assert.equal(await page.locator(`.rewards-categories [data-category="${slug}"]`).getAttribute('aria-pressed'), 'true');
      }
      await category('all');
      await availability('available');
      assert.equal(await visibleCards().count(), 2);
      await category('beauty');
      assert.equal(await visibleCards().count(), 0);
      assert.equal(await page.locator('[data-rewards-empty]').isVisible(), true);
      const button = page.locator('.rewards-categories [data-category="food-drink"]');
      await button.focus(); await button.press('Enter');
      assert.equal(await visibleCards().count(), 1);
      await page.locator('[data-availability="all"]').focus();
      await page.locator('[data-availability="all"]').press('Space');
      assert.equal(await page.locator('[data-availability="all"]').getAttribute('aria-pressed'), 'true');
      assert.equal(requests.length, before, 'Filters must make no requests');
      await category('all');
    });
    await scenario('Desktop, tablet and mobile grids fit; mobile pills scroll horizontally', async () => {
      for (const [width, columns] of [[1440, 3], [768, 2], [390, 1], [320, 1]]) {
        await page.setViewportSize({width, height: 1000});
        assert.equal(await page.locator('.rewards-grid').evaluate(el => getComputedStyle(el).gridTemplateColumns.split(' ').length), columns);
        assert.equal(await page.locator('#customer-rewards').evaluate(el => el.getBoundingClientRect().right <= innerWidth), true);
        for (const card of await visibleCards().all()) {
          const box = await card.boundingBox();
          assert.ok(box.x >= 0 && box.x + box.width <= width + 1);
        }
        if (width <= 390) {
          assert.equal(await page.locator('.rewards-categories').evaluate(el => el.scrollWidth > el.clientWidth && getComputedStyle(el).overflowX === 'auto'), true);
          await page.locator('.rewards-categories [data-category="experiences"]').focus();
          assert.ok(await page.locator('.rewards-categories').evaluate(el => el.scrollLeft) > 0);
        }
      }
      await page.setViewportSize({width: 1440, height: 1000});
    });
    await scenario('Repeated visits clean up filters, refresh balance and remove Rewards styles on exit', async () => {
      for (let round = 0; round < 3; round++) {
        await visit('offers');
        assert.equal(await page.locator('head [data-customer-page-style]').evaluateAll(nodes => nodes.some(node => node.textContent.includes('#customer-rewards'))), false);
        await visit('rewards');
        await category('beauty');
        assert.equal(await visibleCards().count(), 1);
        await identity();
      }
      await visit('offers');
      await page.request.post(`${base}/__fixture__/`, {form: {day: '0', points: '500'}});
      await visit('rewards');
      assert.equal(await visibleCards().count(), 7);
      assert.equal(await page.locator('.rewards-balance strong').textContent(), '500 Points');
      assert.equal(await page.locator('[data-reward-card][data-available="true"]').count(), 3);
    });
    await scenario('Back and Forward preserve URL, title, active link and the same audio element', async () => {
      await visit('offers');
      await navigation(() => page.goBack());
      assert.equal(new URL(page.url()).pathname, '/customer/rewards/');
      assert.equal(await page.title(), 'Rewards | MouseForce');
      assert.equal(await page.locator('.customer-secondary-nav [aria-current="page"]').textContent(), 'Rewards');
      await category('travel');
      assert.equal(await visibleCards().count(), 1);
      await navigation(() => page.goForward());
      assert.equal(new URL(page.url()).pathname, '/customer/offers/');
      await identity();
    });
    await scenario('Reward detail opens persistently, links to confirmation without spending, and supports Back/Forward', async () => {
      await visit('rewards');
      const card = page.locator('[data-reward-card][data-category="food-drink"]');
      const detailURL = await card.locator('.reward-link').getAttribute('href');
      await navigation(() => card.locator('.reward-link').click());
      assert.equal(new URL(page.url()).pathname, detailURL);
      assert.equal(await page.locator('#customer-page-top').getAttribute('data-page'), 'reward_detail');
      assert.equal(await page.locator('.customer-secondary-nav [aria-current="page"]').textContent(), 'Rewards');
      assert.equal(await page.locator('.reward-confirm-link').getAttribute('href'), detailURL + 'confirm/');
      assert.equal(await page.locator('.reward-partner-link').getAttribute('target'), '_blank');
      assert.equal(await page.locator('.reward-partner-link').getAttribute('rel'), 'noopener noreferrer');
      assert.equal(await page.locator('#customer-reward-detail form').count(), 0);
      await identity();
      for (const width of [1440, 768, 390, 320]) {
        await page.setViewportSize({width, height: 1000});
        assert.equal(await page.locator('#customer-reward-detail').evaluate(el => el.getBoundingClientRect().right <= innerWidth), true);
        assert.equal(await page.locator('.reward-detail-hero').evaluate(el => el.scrollWidth <= el.clientWidth + 1), true);
      }
      await page.screenshot({path: `${process.env.TEMP}/customer-reward-detail-mobile.png`, fullPage: true});
      await page.setViewportSize({width: 1440, height: 1000});
      await page.screenshot({path: `${process.env.TEMP}/customer-reward-detail-desktop.png`, fullPage: true});
      await navigation(() => page.goBack());
      assert.equal(new URL(page.url()).pathname, '/customer/rewards/');
      await navigation(() => page.goForward());
      assert.equal(new URL(page.url()).pathname, detailURL);
      await navigation(() => page.locator('.reward-back').click());
      await identity();
      await page.goto(`${base}${detailURL}`, {waitUntil: 'networkidle'});
      await page.evaluate(() => {
        window.__audio = document.querySelector('.customer-music-audio');
        window.__header = document.querySelector('body > header');
        window.__nav = document.querySelector('.customer-secondary-nav');
        window.__audio.muted = true;
      });
      await navigation(() => page.locator('.reward-back').click());
      await identity();
      const before = await page.evaluate(() => window.__visits);
      const href = await page.locator('.reward-link').first().getAttribute('href');
      const popupReady = context.waitForEvent('page');
      await page.locator('.reward-link').first().click({modifiers: ['Control']});
      const popup = await popupReady;
      await popup.waitForURL(`${base}${href}`);
      assert.equal(await page.evaluate(() => window.__visits), before);
      await popup.close();
    });
    await scenario('Real music continues entering and leaving Rewards without pause or media reload', async () => {
      await page.locator('.customer-music-play').click();
      await page.waitForFunction(() => !window.__audio.paused && window.__audio.currentTime > 1, null, {timeout: 45000})
        .catch(async () => { throw new Error(`Playback unavailable: ${await page.locator('.customer-music-status').textContent()}`); });
      const before = await page.evaluate(() => {
        window.__interruptions = [];
        for (const event of ['pause', 'emptied', 'loadstart']) window.__audio.addEventListener(event, () => window.__interruptions.push(event));
        return {src: window.__audio.currentSrc, time: window.__audio.currentTime};
      });
      for (const name of ['rewards', 'news', 'rewards', 'weather', 'rewards', 'discounts', 'rewards', 'how-points-work', 'rewards', 'dashboard', 'rewards', 'offers']) {
        await visit(name);
        if (name === 'rewards') {
          await navigation(() => page.locator('.reward-link').first().click());
          await identity();
          assert.equal(await page.evaluate(() => window.__audio.paused), false);
          await navigation(() => page.locator('.reward-back').click());
        }
        await identity();
        assert.equal(await page.evaluate(() => window.__audio.paused), false);
        assert.equal(await page.evaluate(() => window.__audio.currentSrc), before.src);
      }
      const after = await page.evaluate(() => window.__audio.currentTime);
      assert.ok(after > before.time);
      assert.deepEqual(await page.evaluate(() => window.__interruptions), []);
      console.log(`  Same track advanced from ${before.time.toFixed(2)}s to ${after.toFixed(2)}s.`);
      await page.locator('.customer-music-play').click();
    });
    await scenario('Direct Rewards load initializes filters; no errors or reward action requests', async () => {
      await page.goto(`${base}/customer/rewards/`, {waitUntil: 'networkidle'});
      await category('all');
      await availability('available');
      assert.equal(await visibleCards().count(), 3);
      assert.deepEqual(errors, []);
      assert.equal(requests.filter(request => /points\/claim-|redeem/.test(request.url())).length, 0);
      assert.equal(await page.locator('[data-reward-card] button, [data-reward-card] form').count(), 0);
      assert.equal(await page.locator('[data-reward-card] .reward-link').count(), 7);
      await page.screenshot({path: `${process.env.TEMP}/customer-rewards-desktop.png`, fullPage: true});
      await page.setViewportSize({width: 390, height: 844});
      await page.screenshot({path: `${process.env.TEMP}/customer-rewards-mobile.png`, fullPage: true});
    });
    await scenario('Empty database catalogue remains clean after persistent navigation', async () => {
      await page.request.post(`${base}/__fixture__/`, {form: {points: '500', rewards_active: '0'}});
      await visit('offers'); await visit('rewards');
      assert.equal(await visibleCards().count(), 0);
      assert.match(await page.locator('#customer-rewards').textContent(), /New rewards are on the way/);
      assert.equal(await page.locator('[data-rewards-empty]').isVisible(), false);
      await page.request.post(`${base}/__fixture__/`, {form: {points: '500', rewards_active: '1'}});
      await visit('offers'); await visit('rewards');
      assert.equal(await visibleCards().count(), 7);
    });
    console.log('9 Rewards browser scenarios passed.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
