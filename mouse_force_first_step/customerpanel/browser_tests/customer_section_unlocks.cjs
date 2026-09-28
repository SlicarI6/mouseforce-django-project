/* Isolated server.py with CUSTOMER_BROWSER_UNLOCK_FIXTURE=1 and digital fixtures. */
const assert = require('node:assert/strict');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.CUSTOMER_BROWSER_BASE || 'http://127.0.0.1:8781';

(async () => {
  const browser = await chromium.launch({headless: true});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  await context.addInitScript(() => {
    window.__visits = 0;
    window.addEventListener('customer:navigated', () => window.__visits++);
    window.WebSocket = class extends EventTarget { send() {} close() {} };
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const dialog = page.locator('#customer-section-dialog');
  const confirm = dialog.locator('[data-unlock-confirm]');
  const cancel = dialog.locator('[data-unlock-cancel]');
  const state = async () => (await page.request.get(`${base}/__fixture__/`)).json();
  async function setPoints(points) {
    await page.request.post(`${base}/__fixture__/`, {form: {points: String(points)}});
  }
  async function scenario(name, run) { await run(); console.log(`PASS: ${name}`); }
  async function completedNavigation(action) {
    const before = await page.evaluate(() => window.__visits);
    await action();
    await page.waitForFunction(count => window.__visits > count, before, {timeout: 40000});
  }
  async function clickSection(section) {
    await page.locator(`.customer-secondary-nav a[href="/customer/${section}/"]`).click();
  }
  async function modal(section) {
    await clickSection(section);
    await dialog.waitFor({state: 'visible'});
    await dialog.locator('[data-unlock-summary]').waitFor({state: 'visible'});
    assert.match(await dialog.locator('h2').textContent(), /^Unlock /);
  }
  async function buy(section) {
    const before = (await state()).points;
    await modal(section);
    await completedNavigation(() => confirm.click());
    assert.equal((await state()).points, before - 10);
    assert.equal(await page.locator(`.customer-secondary-nav a[href="/customer/${section}/"] .customer-section-lock`).count(), 0);
  }
  async function audioIdentity() {
    assert.equal(await page.evaluate(() => window.__audio === document.querySelector('.customer-music-audio') && !window.__audio.paused), true);
    assert.deepEqual(await page.evaluate(() => window.__interruptions), []);
  }
  try {
    await page.request.get(`${base}/__fixture__/?login=1`);
    await setPoints(9);
    await page.goto(`${base}/customer/dashboard/`, {waitUntil: 'domcontentloaded'});
    await page.waitForFunction(() => !!window.CustomerNavigation && !!window.CustomerSectionAccess);
    await scenario('Five locked links; How Points Work and Dashboard stay free', async () => {
      assert.equal(await page.locator('.customer-section-lock').count(), 5);
      assert.equal(await page.locator('[href="/customer/how-points-work/"] .customer-section-lock').count(), 0);
      await completedNavigation(() => clickSection('how-points-work'));
      assert.equal(await dialog.isVisible(), false);
      await completedNavigation(() => page.locator('.customer-info-back').click());
      assert.equal((await state()).points, 9);
    });
    await scenario('Insufficient Points shows the required message without entering or charging', async () => {
      await modal('rewards');
      assert.equal(await confirm.isDisabled(), true);
      assert.match(await dialog.locator('[data-unlock-message]').textContent(), /You don’t have enough Points/);
      assert.match(page.url(), /\/customer\/dashboard\/$/);
      await cancel.click();
      assert.equal((await state()).section_unlocks.length, 0);
    });
    await scenario('Direct URLs and reward actions are protected before an unlock', async () => {
      for (const route of ['rewards/', 'discounts/', 'offers/', 'news/?category=world', 'weather/?city=London', 'redemptions/', 'rewards/requests/']) {
        const response = await page.request.get(`${base}/customer/${route}`);
        assert.equal(response.status(), 403);
        assert.match(await response.text(), /data-section-gate=/);
      }
    });
    await scenario('Real Jamendo audio starts only after interaction', async () => {
      assert.equal(await page.locator('.customer-music-audio').evaluate(audio => audio.paused), true);
      await page.evaluate(() => { document.querySelector('.customer-music-audio').muted = true; });
      await page.locator('.customer-music-play').click();
      await page.waitForFunction(() => {
        const audio = document.querySelector('.customer-music-audio');
        return !audio.paused && audio.currentTime > 1 && audio.readyState >= 3;
      }, null, {timeout: 60000});
      await page.evaluate(() => {
        window.__audio = document.querySelector('.customer-music-audio');
        window.__initialTime = window.__audio.currentTime;
        window.__initialSource = window.__audio.currentSrc;
        window.__interruptions = [];
        for (const event of ['pause', 'emptied', 'loadstart']) window.__audio.addEventListener(event, () => window.__interruptions.push(event));
      });
    });
    await setPoints(50);
    await scenario('Cancel and Escape preserve page, URL, history, balance and playing audio', async () => {
      const previous = page.url();
      const history = await page.evaluate(() => window.history.length);
      await modal('rewards');
      assert.equal(await dialog.locator('[data-unlock-balance]').textContent(), '50 Points');
      assert.equal(await dialog.locator('[data-unlock-after]').textContent(), '40 Points');
      await page.screenshot({path: path.join(os.tmpdir(), 'mouseforce-section-unlock-1440.png')});
      await page.keyboard.press('Escape');
      assert.equal(page.url(), previous);
      assert.equal(await page.evaluate(() => window.history.length), history);
      assert.equal((await state()).points, 50);
      await audioIdentity();
    });
    await scenario('Double click buys one permanent Rewards unlock and updates the balance immediately', async () => {
      await modal('rewards');
      await completedNavigation(() => confirm.evaluate(button => { button.click(); button.click(); }));
      assert.equal((await state()).points, 40);
      assert.equal((await state()).section_unlocks.length, 1);
      assert.match(await page.locator('#customer-section-notice').textContent(), /Rewards unlocked permanently/);
      assert.equal(await page.locator('.rewards-balance strong').textContent(), '40 Points');
      await audioIdentity();
    });
    await scenario('Rewards detail, confirmation, redeem, result, reveal, history and requests share one access fee', async () => {
      await setPoints(300);
      const voucher = (await state()).digital_rewards.find(item => item.fulfillment_type === 'voucher');
      await completedNavigation(() => page.evaluate(url => window.CustomerNavigation.navigate(url), `/customer/rewards/${voucher.id}/`));
      await completedNavigation(() => page.locator('a[href$="/confirm/"]').click());
      await completedNavigation(() => page.locator('[data-reward-redeem] button[type="submit"]').click());
      assert.equal((await state()).points, 200); // Only the individual reward's 100 Points.
      await page.locator('[data-reward-reveal] button[type="submit"]').click();
      await page.locator('[data-reward-private] code').waitFor({state: 'visible'});
      await page.evaluate(() => { window.__privateBox = document.querySelector('[data-reward-private]'); });
      await completedNavigation(() => page.evaluate(() => window.CustomerNavigation.navigate('/customer/redemptions/')));
      assert.equal(await page.evaluate(() => window.__privateBox.textContent), '');
      for (const url of ['/customer/rewards/requests/', '/customer/rewards/requests/new/', '/customer/rewards/']) {
        await completedNavigation(() => page.evaluate(url => window.CustomerNavigation.navigate(url), url));
      }
      assert.equal((await state()).section_unlocks.length, 1);
      assert.equal((await state()).points, 200);
      await audioIdentity();
    });
    await scenario('Changed balance requires fresh review instead of silently charging', async () => {
      await modal('news');
      await setPoints(210);
      await confirm.click();
      await page.waitForFunction(() => document.querySelector('[data-unlock-balance]').textContent === '210 Points');
      assert.equal((await state()).section_unlocks.length, 1);
      assert.match(await dialog.locator('[data-unlock-message]').textContent(), /balance has changed/);
      await completedNavigation(() => confirm.click());
      assert.equal((await state()).points, 200);
      await completedNavigation(() => page.locator('.customer-news-categories a[href*="gaming"]').click());
      assert.match(page.url(), /category=gaming/);
      await audioIdentity();
    });
    await scenario('Discounts unlock and FAQ retain persistent navigation', async () => {
      await buy('discounts');
      const first = page.locator('.discounts-faq-question').first();
      await first.click();
      assert.equal(await first.getAttribute('aria-expanded'), 'true');
      await audioIdentity();
    });
    await scenario('Mobile/tablet modal fits, retains focus and keeps audio playing', async () => {
      for (const width of [390, 768]) {
        await page.setViewportSize({width, height: 844});
        await modal('weather');
        const bounds = await dialog.boundingBox();
        assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width + 1);
        assert.equal(await page.evaluate(() => document.getElementById('customer-section-dialog').contains(document.activeElement)), true);
        await page.screenshot({path: path.join(os.tmpdir(), `mouseforce-section-unlock-${width}.png`)});
        await page.keyboard.press('Escape');
        await audioIdentity();
      }
      await page.setViewportSize({width: 1440, height: 1000});
      await buy('weather');
      await page.locator('.weather-search input[name="city"]').fill('London');
      await completedNavigation(() => page.locator('.weather-search button').click());
      assert.match(page.url(), /city=London/);
    });
    await scenario('Lost purchase response retries without a second deduction', async () => {
      const before = (await state()).points;
      let lost = false;
      await page.route('**/customer/sections/offers/unlock/', async route => {
        if (route.request().method() === 'POST' && !lost) {
          lost = true;
          await route.fetch(); // Commit, then simulate a lost response.
          await route.abort('failed');
        } else await route.continue();
      });
      await modal('offers');
      await confirm.click();
      await page.waitForFunction(() => document.querySelector('[data-unlock-message]').textContent.includes('Please retry'));
      assert.equal((await state()).points, before - 10);
      await completedNavigation(() => confirm.click());
      assert.equal((await state()).points, before - 10);
      assert.equal((await state()).section_unlocks.length, 5);
      assert.equal(await page.locator('.customer-section-lock').count(), 0);
      await audioIdentity();
    });
    await scenario('Back/Forward and free page visits never re-charge or recreate audio', async () => {
      const before = (await state()).points;
      await completedNavigation(() => page.goBack());
      assert.match(page.url(), /weather\/\?city=London/);
      await completedNavigation(() => page.goForward());
      assert.match(page.url(), /offers\/$/);
      await completedNavigation(() => clickSection('how-points-work'));
      await completedNavigation(() => page.locator('.customer-info-back').click());
      assert.equal((await state()).points, before);
      assert.equal(await page.locator('#points-total').textContent(), `${before} Points`);
      await audioIdentity();
      assert.equal(await page.evaluate(() => window.__audio.currentSrc === window.__initialSource && window.__audio.currentTime > window.__initialTime), true);
    });
    assert.deepEqual(errors, []);
    console.log('ALL SECTION UNLOCK BROWSER SCENARIOS PASSED');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
