/* Stage 2D.5: real request/review/acceptance views, isolated inventory and audio. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.CUSTOMER_BROWSER_BASE || 'http://127.0.0.1:8772';

(async () => {
  const browser = await chromium.launch({headless: true});
  const context = await browser.newContext({viewport: {width: 1440, height: 1100}});
  await context.addInitScript(() => {
    window.__visits = 0;
    window.addEventListener('customer:navigated', () => window.__visits++);
    window.WebSocket = class extends EventTarget {send() {} close() {}};
  });
  const page = await context.newPage();
  const staffContext = await browser.newContext();
  const staff = await staffContext.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  async function scenario(label, test) { await test(); console.log('PASS: ' + label); }
  async function navigate(action) {
    const previous = await page.evaluate(() => window.__visits);
    await action();
    await page.waitForFunction(n => window.__visits > n, previous, {timeout: 30000});
  }
  async function continuous() {
    assert.equal(await page.evaluate(() => window.__audio === document.querySelector('.customer-music-audio')
      && window.__audio.src === window.__track && !window.__audio.paused
      && window.__audio.currentTime >= window.__position && window.__audioEvents.length === 0), true);
    await page.evaluate(() => {window.__position = window.__audio.currentTime;});
  }
  async function state() { return (await page.request.get(base + '/__fixture__/')).json(); }
  async function fits(name) {
    for (const width of [1440, 768, 390, 320]) {
      await page.setViewportSize({width, height: 1100});
      const root = page.locator('.customer-reward-action');
      const bounds = await root.boundingBox();
      assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width + 1);
      assert.equal(await root.evaluate(e => e.scrollWidth <= e.clientWidth + 1), true);
      for (const field of await root.locator('input:not([type=hidden]), textarea, select, button').all()) {
        const box = await field.boundingBox();
        assert.ok(box.height >= 44 && box.x >= 0 && box.x + box.width <= width + 1);
      }
      if (width === 390 || width === 1440) await root.screenshot({path: path.join(os.tmpdir(), `mouseforce-stage2d5-${name}-${width}.png`)});
    }
    await page.setViewportSize({width: 1440, height: 1100});
  }
  try {
    await page.request.get(base + '/__fixture__/?login=1');
    const fixture = await (await page.request.post(base + '/__fixture__/', {form: {points: '1000'}})).json();
    const manual = fixture.fulfillment_rewards.find(r => r.fulfillment_type === 'manual').id;
    await page.goto(base + '/customer/rewards/', {waitUntil: 'networkidle'});
    await scenario('Required browser assets match current application sources', async () => {
      for (const asset of ['js/customer_reward_actions.js', 'css/customer_reward_actions.css', 'js/customer_navigation.js', 'js/customer_page_lifecycle.js']) {
        const response = await page.request.get(base + '/static/' + asset);
        assert.equal(response.status(), 200);
        assert.equal(await response.text(), fs.readFileSync(path.join(__dirname, '..', 'static', asset), 'utf8'));
      }
    });
    await scenario('Real Jamendo playback starts only after interaction', async () => {
      await page.evaluate(() => {window.__audio = document.querySelector('.customer-music-audio'); window.__audio.muted = true;});
      assert.equal(await page.evaluate(() => window.__audio.paused), true);
      await page.locator('.customer-music-play').click();
      await page.waitForFunction(() => !window.__audio.paused && window.__audio.currentTime > .3, null, {timeout: 60000});
      await page.evaluate(() => {
        window.__track = window.__audio.src; window.__position = window.__audio.currentTime; window.__audioEvents = [];
        for (const name of ['pause', 'emptied', 'loadstart']) window.__audio.addEventListener(name, () => window.__audioEvents.push(name));
      });
    });
    await scenario('Rewards to request form preserves music and fits desktop/tablet/mobile', async () => {
      await navigate(() => page.getByRole('link', {name: 'Request a reward', exact: true}).click());
      await continuous(); await fits('form');
    });
    let requestUrl, requestId;
    await scenario('CSRF enforced; request submission and duplicate retry have zero spending', async () => {
      await page.locator('[name="title"]').fill('A private browser reward request');
      await page.locator('[name="description"]').fill('A fictional weekend experience. Browser-only private description.');
      await page.locator('[name="category"]').selectOption('experiences');
      const data = await page.locator('[data-reward-request]').evaluate(form => Object.fromEntries(new FormData(form)));
      const noCSRF = {...data}; delete noCSRF.csrfmiddlewaretoken;
      assert.equal((await page.request.post(base + '/customer/rewards/requests/new/', {form: noCSRF})).status(), 403);
      await page.evaluate(() => {window.__oldForm = document.querySelector('[data-reward-request]');});
      await navigate(() => page.getByRole('button', {name: 'Submit request', exact: true}).click());
      await continuous();
      assert.match(await page.locator('.customer-reward-action').textContent(), /Your request has been submitted for review/);
      requestUrl = new URL(page.url()).pathname;
      requestId = requestUrl.split('/').filter(Boolean).at(-1);
      assert.equal(await page.evaluate(() => window.__oldForm.elements.description.value), '');
      const retry = await page.request.post(base + '/customer/rewards/requests/new/', {form: data, headers: {Accept: 'application/json'}});
      assert.equal(retry.status(), 200);
      const current = await state();
      assert.equal(current.points, 1000); assert.equal(current.redemptions, 0);
      assert.equal(current.reward_requests.length, 1);
      assert.equal(current.fulfillment_rewards.find(r => r.id === manual).stock_remaining, 2);
      await fits('pending');
    });
    await scenario('Admin review approves restricted reward without spending or reserving', async () => {
      await staff.request.get(base + '/__fixture__/?login=staff');
      await staff.goto(base + `/admin/customerpanel/rewardrequest/${requestId}/review/`);
      await staff.locator('[name=status]').selectOption('approved');
      await staff.locator('[name=approved_reward]').selectOption(manual);
      await staff.locator('[name=staff_response]').fill('An offer for your review.');
      await staff.locator('[name=internal_notes]').fill('BROWSER STAFF ONLY SECRET');
      await staff.locator('input[type=submit]').click();
      await staff.waitForURL('**/admin/customerpanel/rewardrequest/');
      const current = await state();
      assert.equal(current.points, 1000); assert.equal(current.redemptions, 0);
      assert.equal(current.reward_requests[0].approved_reward_id, manual);
      assert.equal(current.fulfillment_rewards.find(r => r.id === manual).stock_remaining, 2);
    });
    await scenario('Back/Forward refreshes approved offer without private notes or audio interruption', async () => {
      await navigate(() => page.getByRole('link', {name: 'My reward requests', exact: true}).click());
      await navigate(() => page.goBack());
      await continuous();
      assert.match(await page.locator('.customer-reward-action').textContent(), /Review and confirm offer/);
      assert.doesNotMatch(await page.content(), /BROWSER STAFF ONLY SECRET/);
      await navigate(() => page.goForward()); await continuous();
      await navigate(() => page.getByRole('link', {name: 'A private browser reward request'}).click());
      await fits('approved');
    });
    let postData;
    await scenario('Explicit confirmation remains required and reuses existing redemption form', async () => {
      await page.evaluate(() => {window.__privateRequest = document.querySelector('[data-fulfillment-private]');});
      await navigate(() => page.getByRole('link', {name: 'Review and confirm offer'}).click());
      await continuous();
      assert.equal(await page.evaluate(() => window.__privateRequest.textContent), '');
      assert.equal((await state()).redemptions, 0);
      await fits('confirmation');
      for (const [name, value] of Object.entries({recipient_name: 'Browser Requester', contact_email: 'requester@example.test', contact_phone: '+441234567890', request_details: 'Fictional fulfillment note'})) {
        await page.locator(`[name="${name}"]`).fill(value);
      }
      postData = await page.locator('[data-reward-redeem]').evaluate(form => Object.fromEntries(new FormData(form)));
      await navigate(() => page.getByRole('button', {name: 'Redeem for 100 Points'}).click());
      await continuous();
    });
    await scenario('One acceptance, one fulfillment and same result on POST retry', async () => {
      const resultPath = new URL(page.url()).pathname;
      const retry = await page.request.post(base + `/customer/rewards/${manual}/redeem/`, {form: postData, headers: {Accept: 'application/json'}});
      assert.equal(retry.status(), 200);
      assert.deepEqual(await retry.json(), {redirect_url: resultPath, replayed: true});
      const current = await state();
      assert.equal(current.points, 900); assert.equal(current.redemptions, 1); assert.equal(current.fulfillments, 1);
      assert.ok(current.reward_requests[0].redemption_id);
      assert.equal(current.fulfillment_rewards.find(r => r.id === manual).stock_remaining, 1);
      await fits('result');
    });
    await scenario('Accepted request and result survive repeated navigation with continuous music', async () => {
      for (let i = 0; i < 2; i++) {
        await navigate(() => page.evaluate(url => window.CustomerNavigation.navigate(url), base + requestUrl));
        assert.match(await page.locator('.customer-reward-action').textContent(), /Reward accepted/);
        await navigate(() => page.getByRole('link', {name: 'View result', exact: true}).click());
        await navigate(() => page.goBack()); await navigate(() => page.goForward());
        await continuous();
      }
      assert.deepEqual(errors, []);
    });
    console.log('9 Stage 2D.5 browser scenarios passed.');
  } finally {await browser.close();}
})().catch(error => {console.error(error.message); process.exitCode = 1;});
