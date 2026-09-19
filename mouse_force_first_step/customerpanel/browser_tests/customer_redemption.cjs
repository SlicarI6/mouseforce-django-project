/* Isolated Stage 2D.3 browser verification; real Jamendo audio, fake benefits.
 * Start server.py with CUSTOMER_BROWSER_REDEMPTION_FIXTURE=1 and port 8767.
 */
const assert = require('node:assert/strict');
const path = require('node:path');
const os = require('node:os');
const fs = require('node:fs');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.CUSTOMER_BROWSER_BASE || 'http://127.0.0.1:8767';

(async () => {
  const browser = await chromium.launch({headless: true});
  const context = await browser.newContext({viewport: {width: 1440, height: 1100}});
  await context.addInitScript(() => {
    window.__visits = 0;
    window.addEventListener('customer:navigated', () => window.__visits++);
    window.WebSocket = class extends EventTarget { send() {} close() {} };
  });
  const page = await context.newPage();
  const errors = [];
  const purchases = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('request', r => { if (r.method() === 'POST' && /\/redeem\/$/.test(r.url())) purchases.push(r.url()); });
  async function scenario(name, action) { await action(); console.log('PASS: ' + name); }
  async function navigation(action) {
    const before = await page.evaluate(() => window.__visits);
    await action();
    await page.waitForFunction(n => window.__visits > n, before, {timeout: 35000});
  }
  async function navRewards() {
    await navigation(() => page.locator('.customer-secondary-nav a[href="/customer/rewards/"]').click());
  }
  async function audioContinues() {
    assert.equal(await page.evaluate(() => window.__audio === document.querySelector('.customer-music-audio')
      && window.__header === document.querySelector('body > header')
      && window.__audio.src === window.__track && !window.__audio.paused
      && window.__audio.currentTime >= window.__time && window.__audioEvents.length === 0), true);
    await page.evaluate(() => { window.__time = window.__audio.currentTime; });
  }
  async function fits() {
    const size = await page.locator('.customer-reward-action').boundingBox();
    assert.ok(size.x >= 0 && size.x + size.width <= page.viewportSize().width + 1);
    assert.equal(await page.locator('.customer-reward-action').evaluate(e => e.scrollWidth <= e.clientWidth + 1), true);
  }
  async function screenshot(name) {
    const file = path.join(os.tmpdir(), `mouseforce-stage2d3-${name}.png`);
    await page.locator('.customer-reward-action').screenshot({path: file});
    console.log('Screenshot: ' + file);
  }
  try {
    await page.request.get(base + '/__fixture__/?login=1');
    const fixtures = await (await page.request.post(base + '/__fixture__/', {form: {points: '1000'}})).json();
    const voucher = fixtures.digital_rewards.find(r => r.fulfillment_type === 'voucher').id;
    const external = fixtures.digital_rewards.find(r => r.fulfillment_type === 'external').id;
    await page.goto(base + '/customer/rewards/', {waitUntil: 'networkidle'});

    await scenario('Required browser-served source assets match the implementation', async () => {
      for (const asset of ['js/customer_reward_actions.js', 'js/customer_navigation.js', 'js/customer_page_lifecycle.js', 'css/customer_reward_actions.css']) {
        const response = await page.request.get(base + '/static/' + asset);
        assert.equal(response.status(), 200);
        const source = path.join(__dirname, '..', 'static', asset);
        assert.equal(await response.text(), fs.readFileSync(source, 'utf8'));
      }
    });
    await scenario('Music starts only on interaction using a real Jamendo track', async () => {
      await page.evaluate(() => {
        window.__audio = document.querySelector('.customer-music-audio');
        window.__header = document.querySelector('body > header');
        window.__audio.muted = true;
      });
      assert.equal(await page.evaluate(() => window.__audio.paused), true);
      await page.locator('.customer-music-play').click();
      await page.waitForFunction(() => !window.__audio.paused && window.__audio.currentTime > .3, null, {timeout: 60000});
      await page.evaluate(() => {
        window.__track = window.__audio.src;
        window.__time = window.__audio.currentTime;
        window.__audioEvents = [];
        for (const type of ['pause', 'emptied', 'loadstart']) window.__audio.addEventListener(type, () => window.__audioEvents.push(type));
      });
    });
    await scenario('Rewards → detail → confirmation preserves audio and uses authoritative costs', async () => {
      await navigation(() => page.locator(`a[href="/customer/rewards/${voucher}/"]`).click());
      await audioContinues();
      await navigation(() => page.locator('.reward-confirm-link').click());
      await audioContinues();
      assert.equal(await page.locator('#customer-page-top').getAttribute('data-page'), 'reward_confirm');
      assert.match(await page.locator('.reward-action-summary').textContent(), /1000 Points[\s\S]*100 Points[\s\S]*900 Points/);
      assert.equal(await page.locator('.customer-secondary-nav [aria-current="page"]').textContent(), 'Rewards');
      assert.equal(await page.locator('[data-reward-redeem]').count(), 1);
    });
    await scenario('Confirmation is readable on desktop, tablet and small mobile', async () => {
      for (const width of [1440, 768, 390, 320]) {
        await page.setViewportSize({width, height: 1100});
        await fits();
        const button = await page.locator('[data-reward-redeem] button').boundingBox();
        assert.ok(button.height >= 44 && button.width >= 44);
        if (width === 1440 || width === 390) await screenshot(`confirmation-${width}`);
      }
      await page.setViewportSize({width: 1440, height: 1100});
    });
    await scenario('Double submission guard and lost-response retry charge/allocate only once', async () => {
      let first = true;
      const endpoint = `**/customer/rewards/${voucher}/redeem/`;
      await page.route(endpoint, async route => {
        if (!first) { await route.continue(); return; }
        first = false;
        const response = await route.fetch(); // Commit succeeds, but simulate losing its response.
        assert.equal(response.status(), 200);
        await route.abort('failed');
      });
      await page.locator('[data-reward-redeem]').evaluate(form => {
        form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
        form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
      });
      await page.locator('[data-reward-error]:visible').waitFor();
      assert.equal(purchases.length, 1);
      const after = await (await page.request.get(base + '/__fixture__/')).json();
      assert.equal(after.redemptions, 1);
      assert.equal(after.points, 900);
      await navigation(() => page.locator('[data-reward-redeem] button').click());
      assert.equal(await page.locator('#customer-page-top').getAttribute('data-page'), 'redemption_result');
      assert.equal(purchases.length, 2);
      const retry = await (await page.request.get(base + '/__fixture__/')).json();
      assert.equal(retry.redemptions, 1);
      assert.equal(retry.points, 900);
      await page.unroute(endpoint);
      await audioContinues();
    });
    await scenario('Owned result reveals only on POST, with no-store and no private browser storage', async () => {
      assert.doesNotMatch(await page.content(), /BROWSER-TEST-VOUCHER-/);
      const responsePromise = page.waitForResponse(r => /\/reveal\/$/.test(r.url()) && r.request().method() === 'POST');
      await page.locator('[data-reward-reveal] button').click();
      const response = await responsePromise;
      assert.equal(response.status(), 200);
      assert.match(response.headers()['cache-control'], /no-store/);
      assert.equal(response.headers()['referrer-policy'], 'no-referrer');
      await page.locator('[data-reward-private]:visible code').waitFor();
      assert.match(await page.locator('[data-reward-private] code').textContent(), /^BROWSER-TEST-VOUCHER-/);
      assert.doesNotMatch(await page.evaluate(() => JSON.stringify([localStorage, sessionStorage, history.state])), /BROWSER-TEST-VOUCHER-|test-only-claim/);
      await page.evaluate(() => { window.__oldPrivate = document.querySelector('[data-reward-private]'); });
      for (const width of [1440, 768, 390, 320]) {
        await page.setViewportSize({width, height: 1100});
        await fits();
        if (width === 1440 || width === 390) await screenshot(`result-${width}`);
      }
      await page.setViewportSize({width: 1440, height: 1100});
    });
    await scenario('Back/Forward never resubmits, clears private DOM and preserves the playing audio', async () => {
      const count = purchases.length;
      for (let i = 0; i < 3; i++) { await navigation(() => page.goBack()); await audioContinues(); }
      assert.equal(await page.evaluate(() => window.__oldPrivate.childNodes.length), 0);
      for (let i = 0; i < 3; i++) { await navigation(() => page.goForward()); await audioContinues(); }
      assert.equal(purchases.length, count);
      assert.equal(await page.locator('[data-reward-private]').isHidden(), true);
    });
    await scenario('Changed offer requires review and never silently charges', async () => {
      await navRewards();
      await navigation(() => page.locator(`a[href="/customer/rewards/${voucher}/"]`).click());
      await navigation(() => page.locator('.reward-confirm-link').click());
      await page.request.post(base + '/__fixture__/', {form: {points: '900', reward_change: 'price'}});
      await page.locator('[data-reward-redeem] button').click();
      await page.locator('[data-reward-review]:visible').waitFor();
      assert.equal(await page.locator('[data-reward-redeem] button').isDisabled(), true);
      await navigation(() => page.locator('[data-reward-review]').click());
      assert.match(await page.locator('.reward-action-summary').textContent(), /120 Points/);
      const state = await (await page.request.get(base + '/__fixture__/')).json();
      assert.equal(state.redemptions, 1);
      await audioContinues();
    });
    await scenario('External redemption reveals the assigned private link safely and clears it on leave', async () => {
      await navRewards();
      await navigation(() => page.locator(`a[href="/customer/rewards/${external}/"]`).click());
      await navigation(() => page.locator('.reward-confirm-link').click());
      await navigation(() => page.locator('[data-reward-redeem] button').click());
      assert.doesNotMatch(await page.content(), /test-only-claim/);
      await page.locator('[data-reward-reveal] button').click();
      const link = page.locator('[data-reward-private]:visible a');
      await link.waitFor();
      assert.match(await link.getAttribute('href'), /^https:\/\/partner\.example\/test-only-claim\//);
      assert.equal(await link.getAttribute('rel'), 'noopener noreferrer');
      assert.equal(await link.getAttribute('referrerpolicy'), 'no-referrer');
      await page.evaluate(() => { window.__oldPrivate = document.querySelector('[data-reward-private]'); });
      await screenshot('external-result');
      await navRewards();
      assert.equal(await page.evaluate(() => window.__oldPrivate.childNodes.length), 0);
      assert.doesNotMatch(await page.content(), /test-only-claim|BROWSER-TEST-VOUCHER-/);
      await audioContinues();
    });
    assert.deepEqual(errors, []);
    console.log('PASS: No browser JavaScript errors. No audio pause, reload or restart during the complete flow.');
    console.log('Manual preview: ' + base + '/__fixture__/?login=1 then ' + base + '/customer/rewards/');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
