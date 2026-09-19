/* Stage 2D.6: only isolated fake benefits and fictional delivery details. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.CUSTOMER_BROWSER_BASE || 'http://127.0.0.1:8775';
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
  async function scenario(name, test) {await test(); console.log('PASS: ' + name);}
  async function navigate(action) {
    const before = await page.evaluate(() => window.__visits);
    await action();
    await page.waitForFunction(n => window.__visits > n, before, {timeout: 30000});
  }
  async function visit(url) {await navigate(() => page.evaluate(url => window.CustomerNavigation.navigate(url), base + url));}
  async function music() {
    assert.equal(await page.evaluate(() => window.__audio === document.querySelector('.customer-music-audio')
      && window.__audio.src === window.__src && !window.__audio.paused && window.__audio.currentTime >= window.__position
      && window.__audioEvents.length === 0), true);
    await page.evaluate(() => {window.__position = window.__audio.currentTime;});
  }
  async function state() {return (await page.request.get(base + '/__fixture__/')).json();}
  async function fits(name) {
    for (const width of [1440, 768, 390, 320]) {
      await page.setViewportSize({width, height: 1100});
      const root = page.locator('.customer-reward-action');
      const box = await root.boundingBox();
      assert.ok(box.x >= 0 && box.x + box.width <= width + 1);
      assert.equal(await root.evaluate(e => e.scrollWidth <= e.clientWidth + 1), true);
      if ([1440, 390].includes(width)) await root.screenshot({path: path.join(os.tmpdir(), `mouseforce-stage2d6-${name}-${width}.png`)});
    }
    await page.setViewportSize({width: 1440, height: 1100});
  }
  async function purchase(reward, details={}) {
    await visit(`/customer/rewards/${reward}/confirm/`);
    for (const [name, value] of Object.entries(details)) await page.locator(`[name="${name}"]`).fill(value);
    await navigate(() => page.getByRole('button', {name: 'Redeem for 100 Points'}).click());
    await music();
    return new URL(page.url()).pathname.split('/').filter(Boolean).at(-1);
  }
  async function refund(id, reason='unusable', stock='none') {
    const url = base + `/admin/customerpanel/redemption/${id}/refund/`;
    await staff.goto(url);
    await staff.locator('[name="reason"]').selectOption(reason);
    await staff.locator('[name="stock_action"]').selectOption(stock);
    await staff.locator('[name="internal_note"]').fill('BROWSER STAFF REFUND NOTE: verified fictional case.');
    await staff.locator('[name="approved"]').check();
    if (stock !== 'none') await staff.locator('[name="stock_verified"]').check();
    const data = await staff.locator('form[autocomplete="off"]').evaluate(form => Object.fromEntries(new FormData(form)));
    const response = staff.waitForResponse(r => r.request().method() === 'POST' && r.url() === url);
    await staff.locator('input[type="submit"]').click();
    assert.equal((await response).status(), 302);
    await staff.getByText(/Refunded: 100 Points/).waitFor();
    return {url, data};
  }
  try {
    await page.request.get(base + '/__fixture__/?login=1');
    await staff.request.get(base + '/__fixture__/?login=staff');
    const fixture = await (await page.request.post(base + '/__fixture__/', {form: {points: '1000'}})).json();
    const voucher = fixture.digital_rewards.find(r => r.fulfillment_type === 'voucher').id;
    const external = fixture.digital_rewards.find(r => r.fulfillment_type === 'external').id;
    const physical = fixture.fulfillment_rewards.find(r => r.fulfillment_type === 'physical').id;
    await page.goto(base + '/customer/rewards/', {waitUntil: 'networkidle'});
    await scenario('Source assets available; music starts only after user interaction', async () => {
      for (const asset of ['js/customer_navigation.js', 'js/customer_page_lifecycle.js', 'js/customer_reward_actions.js', 'css/customer_reward_actions.css']) {
        const response = await page.request.get(base + '/static/' + asset);
        assert.equal(await response.text(), fs.readFileSync(path.join(__dirname, '..', 'static', asset), 'utf8'));
      }
      await page.evaluate(() => {window.__audio = document.querySelector('.customer-music-audio'); window.__audio.muted = true;});
      assert.equal(await page.evaluate(() => window.__audio.paused), true);
      await page.locator('.customer-music-play').click();
      await page.waitForFunction(() => !window.__audio.paused && window.__audio.currentTime > .3, null, {timeout: 60000});
      await page.evaluate(() => {
        window.__src = window.__audio.src; window.__position = window.__audio.currentTime; window.__audioEvents = [];
        for (const name of ['pause', 'emptied', 'loadstart']) window.__audio.addEventListener(name, () => window.__audioEvents.push(name));
      });
    });
    await scenario('Rewards to empty history keeps music and responsive layout', async () => {
      await navigate(() => page.getByRole('link', {name: 'Redemption history', exact: true}).click());
      assert.match(await page.locator('#customer-redemption-history').textContent(), /Your redeemed rewards will appear here/);
      await fits('empty-history'); await music();
    });
    let digitalId;
    await scenario('History never shows a revealed voucher and navigation clears its private DOM', async () => {
      digitalId = await purchase(voucher);
      await page.getByRole('button', {name: 'Reveal voucher code'}).click();
      await page.locator('[data-reward-private]:not([hidden]) code').waitFor();
      const privateValue = await page.locator('[data-reward-private] code').textContent();
      await page.evaluate(() => {window.__privateNode = document.querySelector('[data-reward-private]');});
      await navigate(() => page.getByRole('link', {name: 'Redemption history', exact: true}).click());
      assert.doesNotMatch(await page.content(), new RegExp(privateValue));
      assert.equal(await page.evaluate(() => window.__privateNode.textContent), '');
      await navigate(() => page.goBack()); await navigate(() => page.goForward()); await music();
      await fits('history');
    });
    await scenario('Staff digital refund and lost-response retry credit once and never recycle code', async () => {
      const {url, data} = await refund(digitalId);
      const retry = await staff.request.post(url, {form: data});
      assert.equal(retry.status(), 200);
      const current = await state();
      assert.equal(current.points, 1000); assert.equal(current.assigned_codes, 1); assert.equal(current.refund_events, 1);
      assert.equal(current.refunds[0].stock_restored_at, null);
      await visit('/customer/redemptions/');
      assert.match(await page.locator('#customer-redemption-history').textContent(), /Refunded.*100 Points/);
      assert.doesNotMatch(await page.content(), /BROWSER STAFF REFUND NOTE/);
      await navigate(() => page.getByRole('link', {name: 'View redemption', exact: true}).click());
      assert.match(await page.locator('.reward-action-notice').textContent(), /Refunded.*100 Points were returned/);
      assert.match(await page.locator('.reward-action-summary').textContent(), /Balance after redemption[\s\S]*900 Points/);
      assert.equal(await page.locator('[data-reward-reveal]').count(), 0);
      const csrf = (await context.cookies()).find(c => c.name === 'csrftoken').value;
      const reveal = await page.request.post(base + `/customer/redemptions/${digitalId}/reveal/`, {form: {csrfmiddlewaretoken: csrf}});
      assert.equal(reveal.status(), 409);
      await fits('refunded-result'); await music();
    });
    await scenario('Verified physical cancellation restores one unit; private delivery DOM is scrubbed', async () => {
      const id = await purchase(physical, {recipient_name: 'Fictional Return Tester', contact_email: 'return@example.test',
        address_line_1: '123 Fictional Lane', city: 'London', postal_code: 'AB12 3CD', country_code: 'GB'});
      await page.evaluate(() => {window.__delivery = document.querySelector('[data-fulfillment-private]');});
      await visit('/customer/redemptions/');
      assert.equal(await page.evaluate(() => window.__delivery.textContent), '');
      assert.doesNotMatch(await page.content(), /123 Fictional Lane|return@example.test/);
      const {url, data} = await refund(id, 'cancellation', 'cancelled');
      await staff.request.post(url, {form: data});
      const current = await state();
      assert.equal(current.points, 1000); assert.equal(current.refund_events, 2);
      assert.equal(current.fulfillment_rewards.find(r => r.id === physical).stock_remaining, 2);
      assert.ok(current.refunds.find(r => r.id === id).stock_restored_at);
      await music();
    });
    await scenario('Refunded external entitlement remains assigned and history never reveals the link', async () => {
      const id = await purchase(external);
      await page.getByRole('button', {name: 'Reveal partner link'}).click();
      await page.locator('[data-reward-private]:not([hidden]) a').waitFor();
      const link = await page.locator('[data-reward-private] a').getAttribute('href');
      await page.evaluate(() => {window.__privateLink = document.querySelector('[data-reward-private]');});
      await visit('/customer/redemptions/');
      assert.equal(await page.evaluate(() => window.__privateLink.querySelector('a')), null);
      assert.ok(!(await page.content()).includes(link));
      await refund(id);
      const current = await state();
      assert.equal(current.points, 1000); assert.equal(current.assigned_codes, 2); assert.equal(current.refund_events, 3);
      await visit('/customer/redemptions/');
      await fits('refunded-history');
    });
    await scenario('History/result Back/Forward retains original audio and never repeats a charge/refund', async () => {
      for (let i = 0; i < 3; i++) {
        await navigate(() => page.getByRole('link', {name: 'View redemption', exact: true}).first().click());
        await navigate(() => page.goBack()); await navigate(() => page.goForward());
        await visit('/customer/redemptions/'); await music();
      }
      assert.equal((await state()).points, 1000);
      assert.equal((await state()).refund_events, 3);
      assert.deepEqual(errors, []);
    });
    console.log('7 Stage 2D.6 browser scenarios passed.');
  } finally {await browser.close();}
})().catch(error => {console.error(error.message); process.exitCode = 1;});
