/* Isolated Stage 2D.4 verification; fictional delivery details, real audio. */
const assert = require('node:assert/strict');
const path = require('node:path');
const os = require('node:os');
const fs = require('node:fs');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.CUSTOMER_BROWSER_BASE || 'http://127.0.0.1:8769';

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
  const posts = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (request.method() === 'POST' && /\/redeem\/$/.test(request.url())) posts.push(request.url()); });
  async function scenario(name, action) { await action(); console.log('PASS: ' + name); }
  async function navigate(action) {
    const before = await page.evaluate(() => window.__visits);
    await action();
    await page.waitForFunction(n => window.__visits > n, before, {timeout: 35000});
  }
  async function audioContinues() {
    assert.equal(await page.evaluate(() => window.__audio === document.querySelector('.customer-music-audio')
      && window.__audio.src === window.__track && !window.__audio.paused
      && window.__audio.currentTime >= window.__time && window.__audioEvents.length === 0), true);
    await page.evaluate(() => { window.__time = window.__audio.currentTime; });
  }
  async function fitScreens(name) {
    for (const width of [1440, 768, 390, 320]) {
      await page.setViewportSize({width, height: 1100});
      const root = page.locator('.customer-reward-action');
      const box = await root.boundingBox();
      assert.ok(box.x >= 0 && box.x + box.width <= width + 1);
      assert.equal(await root.evaluate(e => e.scrollWidth <= e.clientWidth + 1), true);
      for (const input of await root.locator('input:not([type=hidden]), textarea, button').all()) {
        const bounds = await input.boundingBox();
        assert.ok(bounds.height >= 44 && bounds.x + bounds.width <= width + 1);
      }
      if (width === 1440 || width === 390) {
        const filename = path.join(os.tmpdir(), `mouseforce-stage2d4-${name}-${width}.png`);
        await root.screenshot({path: filename});
        console.log('Screenshot: ' + filename);
      }
    }
    await page.setViewportSize({width: 1440, height: 1100});
  }
  const details = {recipient_name: 'Private Browser Tester', contact_email: 'private-browser@example.test',
    address_line_1: '321 Fictional Browser Lane', city: 'London', postal_code: 'AB12 3CD', country_code: 'GB'};
  try {
    await page.request.get(base + '/__fixture__/?login=1');
    const fixture = await (await page.request.post(base + '/__fixture__/', {form: {points: '1000'}})).json();
    const physical = fixture.fulfillment_rewards.find(r => r.fulfillment_type === 'physical').id;
    const manual = fixture.fulfillment_rewards.find(r => r.fulfillment_type === 'manual').id;
    await page.goto(base + '/customer/rewards/', {waitUntil: 'networkidle'});
    await scenario('Browser-served assets match source files', async () => {
      for (const asset of ['js/customer_reward_actions.js', 'css/customer_reward_actions.css']) {
        const response = await page.request.get(base + '/static/' + asset);
        assert.equal(response.status(), 200);
        assert.equal(await response.text(), fs.readFileSync(path.join(__dirname, '..', 'static', asset), 'utf8'));
      }
    });
    await scenario('Real Jamendo music starts only after interaction', async () => {
      await page.evaluate(() => {
        window.__audio = document.querySelector('.customer-music-audio');
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
    await scenario('Physical detail and confirmation preserve music and collect only required details', async () => {
      await navigate(() => page.locator(`a[href="/customer/rewards/${physical}/"]`).click());
      await audioContinues();
      await navigate(() => page.locator('.reward-confirm-link').click());
      await audioContinues();
      assert.equal(await page.locator('[name="contact_phone"], [name="region"], [name="request_details"]').count(), 0);
      assert.match(await page.locator('.reward-action-summary').textContent(), /1000 Points[\s\S]*100 Points[\s\S]*900 Points/);
      await fitScreens('physical-confirmation');
    });
    await scenario('Server field errors preserve entered details without spending', async () => {
      for (const [name, value] of Object.entries(details)) await page.locator(`[name="${name}"]`).fill(value);
      await page.locator('[name="contact_email"]').fill('invalid-private-email');
      await page.locator('[data-reward-redeem]').evaluate(form => form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})));
      await page.locator('[name="contact_email"][aria-invalid="true"]').waitFor();
      assert.match(await page.locator('[data-field-error="contact_email"]').textContent(), /valid email/);
      assert.equal(await page.locator('[name="address_line_1"]').inputValue(), details.address_line_1);
      const state = await (await page.request.get(base + '/__fixture__/')).json();
      assert.equal(state.fulfillments, 0);
      assert.equal(state.points, 1000);
      await page.locator('[name="contact_email"]').fill(details.contact_email);
    });
    await scenario('Lost-response retry reserves stock and creates fulfillment exactly once', async () => {
      let first = true;
      const endpoint = `**/customer/rewards/${physical}/redeem/`;
      await page.route(endpoint, async route => {
        if (!first) return route.continue();
        first = false;
        const response = await route.fetch();
        assert.equal(response.status(), 200);
        await route.abort('failed');
      });
      await page.evaluate(() => { window.__oldForm = document.querySelector('[data-reward-redeem]'); });
      await page.locator('[data-reward-redeem]').evaluate(form => {
        form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
        form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
      });
      await page.waitForFunction(() => document.querySelector('[data-reward-error]').textContent.includes('Try this same request'));
      assert.equal(posts.length, 2); // One validation request, one purchase.
      await navigate(() => page.locator('[data-reward-redeem] button').click());
      await page.unroute(endpoint);
      const state = await (await page.request.get(base + '/__fixture__/')).json();
      assert.equal(state.points, 900);
      assert.equal(state.fulfillments, 1);
      assert.equal(state.fulfillment_rewards.find(r => r.id === physical).stock_remaining, 1);
      assert.match(await page.locator('.customer-reward-action').textContent(), /Request received — awaiting fulfillment/);
      assert.equal(await page.evaluate(() => [...window.__oldForm.querySelectorAll('[data-fulfillment-input]')].every(e => !e.value)), true);
      await audioContinues();
      await fitScreens('physical-result');
    });
    await scenario('Private delivery DOM is scrubbed, Back/Forward preserves audio and never resubmits', async () => {
      assert.doesNotMatch(await page.evaluate(() => JSON.stringify([history.state, localStorage, sessionStorage])), /Private Browser|private-browser|Fictional Browser/);
      await page.evaluate(() => { window.__private = document.querySelector('[data-fulfillment-private]'); });
      const count = posts.length;
      await navigate(() => page.goBack());
      assert.equal(await page.evaluate(() => window.__private.textContent), '');
      await audioContinues();
      await navigate(() => page.goForward());
      await audioContinues();
      assert.equal(posts.length, count);
      assert.match(await page.locator('[data-fulfillment-private]').textContent(), /Fictional Browser Lane/);
    });
    await scenario('Manual reward requests contact/phone and details without a shipping address', async () => {
      await navigate(() => page.locator('.customer-secondary-nav a[href="/customer/rewards/"]').click());
      await navigate(() => page.locator(`a[href="/customer/rewards/${manual}/"]`).click());
      await navigate(() => page.locator('.reward-confirm-link').click());
      assert.equal(await page.locator('[name="address_line_1"], [name="country_code"], [name="region"]').count(), 0);
      await page.locator('[name="recipient_name"]').fill(details.recipient_name);
      await page.locator('[name="contact_email"]').fill(details.contact_email);
      await page.locator('[name="contact_phone"]').fill('+44 1234 567890');
      await page.locator('[name="request_details"]').fill('Fictional request: afternoon appointment.');
      await fitScreens('manual-confirmation');
      await navigate(() => page.locator('[data-reward-redeem] button').click());
      assert.match(await page.locator('.customer-reward-action').textContent(), /Awaiting manual processing/);
      const state = await (await page.request.get(base + '/__fixture__/')).json();
      assert.equal(state.points, 800);
      assert.equal(state.fulfillments, 2);
      await audioContinues();
      await fitScreens('manual-result');
    });
    await scenario('Repeated navigation and mobile Back/Forward retain the same playing audio', async () => {
      await page.setViewportSize({width: 390, height: 844});
      for (let i = 0; i < 2; i++) {
        await navigate(() => page.goBack());
        await audioContinues();
        await navigate(() => page.goForward());
        await audioContinues();
      }
      assert.deepEqual(errors, []);
    });
    console.log('All Stage 2D.4 browser scenarios passed.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
