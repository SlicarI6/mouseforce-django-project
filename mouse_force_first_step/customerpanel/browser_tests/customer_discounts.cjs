/* Run only with server.py's isolated PostgreSQL Discounts fixture. */
const assert = require('node:assert/strict');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.CUSTOMER_BROWSER_BASE || 'http://127.0.0.1:8783';

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
  const state = async () => (await page.request.get(`${base}/__fixture__/`)).json();
  const fixture = async values => (await page.request.post(`${base}/__fixture__/`, {form: values})).json();
  const dialog = page.locator('[data-deal-dialog]');
  let audioStarted = false;
  let passed = 0;
  async function scenario(name, run) { await run(); passed++; console.log(`PASS: ${name}`); }
  async function completedNavigation(action) {
    const count = await page.evaluate(() => window.__visits);
    await action();
    await page.waitForFunction(before => window.__visits > before, count, {timeout: 30000});
  }
  const navigate = url => completedNavigation(() => page.evaluate(url => window.CustomerNavigation.navigate(url), url));
  async function audioIdentity() {
    if (!audioStarted) return;
    assert.equal(await page.evaluate(() => window.__audio === document.querySelector('.customer-music-audio') && !window.__audio.paused && window.__audio.currentSrc === window.__audioSource), true);
    assert.deepEqual(await page.evaluate(() => window.__interruptions), []);
    assert.ok(await page.evaluate(() => window.__audio.currentTime > window.__initialTime));
  }
  async function search(query) {
    await page.locator('[data-discount-search] input[name=q]').fill(query);
    await page.waitForFunction(q => new URL(location.href).searchParams.get('q') === (q || null) && !document.querySelector('[data-discount-results]').hasAttribute('aria-busy'), query);
  }
  async function openModal() {
    await page.locator('[data-open-deal-unlock]').click();
    await dialog.locator('[data-deal-quote]').waitFor({state: 'visible'});
  }
  try {
    await page.request.get(`${base}/__fixture__/?login=1`);
    await fixture({points: '50', day: '7', ago: '0'});
    const deals = (await state()).discounts;
    const deal = brand => `/customer/discounts/${deals.find(item => item.brand === brand).id}/`;
    await page.goto(`${base}/customer/dashboard/`, {waitUntil: 'domcontentloaded'});
    await page.waitForFunction(() => !!window.CustomerNavigation);
    await scenario('Real Jamendo starts only after interaction', async () => {
      assert.equal(await page.locator('.customer-music-audio').evaluate(node => node.paused), true);
      await page.locator('.customer-music-audio').evaluate(node => { node.muted = true; });
      await page.locator('.customer-music-play').click();
      await page.waitForFunction(() => { const a = document.querySelector('.customer-music-audio'); return !a.paused && a.currentTime > 1 && a.readyState >= 3; }, null, {timeout: 60000});
      await page.evaluate(() => {
        window.__audio = document.querySelector('.customer-music-audio');
        window.__audioSource = window.__audio.currentSrc;
        window.__initialTime = window.__audio.currentTime;
        window.__interruptions = [];
        for (const event of ['pause', 'emptied', 'loadstart']) window.__audio.addEventListener(event, () => window.__interruptions.push(event));
      });
      audioStarted = true;
    });
    await scenario('Catalogue, preserved illustration/FAQ, source stylesheet and no paid payload', async () => {
      await completedNavigation(() => page.locator('.customer-secondary-nav a[href="/customer/discounts/"]').click());
      assert.equal(await page.locator('.deal-card').count(), 7);
      assert.match(await page.locator('.deal-catalogue-heading').textContent(), /Find deals from brands you like/);
      assert.equal(await page.locator('.discounts-legacy').count(), 1);
      const question = page.locator('.discounts-faq-question').first();
      await question.click(); assert.equal(await question.getAttribute('aria-expanded'), 'true');
      await question.click(); assert.equal(await question.getAttribute('aria-expanded'), 'false');
      assert.ok(!(await page.content()).includes('BROWSER-DEAL-CODE-'));
      assert.equal((await page.request.get(`${base}/static/css/customer_discounts.css`)).status(), 200);
      await audioIdentity();
    });
    await scenario('Responsive 3/2/1 cards, scrollable chips, no overflow, screenshots', async () => {
      for (const [width, columns] of [[1440, 3], [768, 2], [390, 1]]) {
        await page.setViewportSize({width, height: 1000});
        await page.evaluate(() => scrollTo({top: 0, behavior: 'instant'}));
        assert.equal(await page.locator('.deal-grid').evaluate(el => getComputedStyle(el).gridTemplateColumns.split(' ').length), columns);
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
        if (width === 390) assert.equal(await page.locator('.deal-categories').evaluate(el => el.scrollWidth > el.clientWidth && getComputedStyle(el).overflowX === 'auto'), true);
        const buttons = page.locator('.deal-card').first().locator('[data-vote-direction]');
        for (const button of await buttons.all()) {
          const box = await button.boundingBox();
          assert.ok(box.height >= 44 && box.x >= 0 && box.x + box.width <= width);
          assert.equal(await button.evaluate(el => el.scrollWidth <= el.clientWidth), true);
          assert.doesNotMatch(await button.textContent(), /Like|Dislike/);
          assert.match(await button.getAttribute('aria-label'), /Like|Dislike/);
        }
        const viewDeal = await page.locator('.deal-card').first().locator('.deal-view').boundingBox();
        assert.ok(viewDeal.height >= 44 && viewDeal.x + viewDeal.width <= width);
        assert.equal(await page.locator('.deal-card').first().locator('.deal-brand-placeholder svg').count(), 1);
        assert.ok(await page.locator('.deal-card-visual').first().evaluate(el => el.getBoundingClientRect().height < 110));
        assert.ok(await page.locator('.deal-ongoing-badge').count() > 0);
        await page.screenshot({path: path.join(os.tmpdir(), `mouseforce-discounts-${width}.png`)});
      }
      await page.setViewportSize({width: 1440, height: 1000});
    });
    await scenario('Live search keeps focus; category counts and deal types update', async () => {
      await search('weekend dining');
      assert.equal(await page.locator('.deal-card').count(), 1);
      assert.equal(await page.locator('[name=q]').evaluate(el => el === document.activeElement), true);
      assert.match(await page.locator('.deal-categories a').first().textContent(), /All\s+1/);
      await search('');
      await page.locator('[data-discount-search] select').selectOption('percent');
      await page.waitForFunction(() => location.search.includes('type=percent') && !document.querySelector('[data-discount-results]').hasAttribute('aria-busy'));
      assert.equal(await page.locator('.deal-card').count(), 2);
      await page.locator('.deal-categories a').filter({hasText: 'Beauty'}).click();
      await page.waitForFunction(() => location.search.includes('category=beauty') && !document.querySelector('[data-discount-results]').hasAttribute('aria-busy'));
      assert.equal(await page.locator('.deal-card').count(), 1);
      await audioIdentity();
    });
    await scenario('Filter Back/Forward restores URL, controls, cards and same audio', async () => {
      await completedNavigation(() => page.goBack());
      assert.equal(await page.locator('.deal-card').count(), 2);
      assert.equal(await page.locator('[name=type]').inputValue(), 'percent');
      await completedNavigation(() => page.goForward());
      assert.equal(await page.locator('.deal-card').count(), 1);
      await audioIdentity();
      await navigate('/customer/discounts/');
    });
    await scenario('Like/dislike counts, active states, remove, lost response retry; no charge', async () => {
      const card = page.locator('.deal-card').filter({hasText: 'Test Bistro'});
      const like = card.locator('[data-vote-direction="1"]');
      const dislike = card.locator('[data-vote-direction="-1"]');
      async function feedback(likes, dislikes, vote) {
        await page.waitForFunction(({likes, dislikes, vote}) => {
          const form = [...document.querySelectorAll('.deal-card')].find(el => el.textContent.includes('Test Bistro')).querySelector('[data-deal-vote]');
          const a = form.querySelector('[data-vote-direction="1"]');
          const b = form.querySelector('[data-vote-direction="-1"]');
          return !form.hasAttribute('aria-busy') && document.querySelector('[data-discounts-status]').textContent === ''
            && a.querySelector('[data-vote-count]').textContent === String(likes)
            && b.querySelector('[data-vote-count]').textContent === String(dislikes)
            && a.getAttribute('aria-pressed') === String(vote === 1) && b.getAttribute('aria-pressed') === String(vote === -1);
        }, {likes, dislikes, vote});
      }
      await like.click(); await feedback(1, 0, 1);
      await like.click(); await feedback(0, 0, 0);
      await dislike.click(); await feedback(0, 1, -1);
      await like.click(); await feedback(1, 0, 1);
      await dislike.click(); await feedback(0, 1, -1);
      await dislike.click(); await feedback(0, 0, 0);
      let lost = false;
      await page.route('**/discounts/*/vote/', async route => { if (!lost) { lost = true; await route.fetch(); await route.abort('failed'); } else await route.continue(); });
      await like.click();
      await page.locator('[data-discounts-status]').filter({hasText: 'could not be confirmed'}).waitFor();
      await like.click(); await feedback(1, 0, 1);
      await page.unroute('**/discounts/*/vote/');
      assert.equal((await state()).discount_votes.length, 1);
      assert.equal((await state()).points, 50);
    });
    await scenario('Detail feedback survives navigation, toggles with keyboard and keeps music', async () => {
      await navigate(deal('Test Bistro'));
      const like = page.locator('[data-vote-direction="1"]');
      const dislike = page.locator('[data-vote-direction="-1"]');
      assert.equal(await like.getAttribute('aria-pressed'), 'true');
      assert.equal(await like.locator('[data-vote-count]').textContent(), '1');
      await dislike.focus(); await page.keyboard.press('Enter');
      await page.waitForFunction(() => document.querySelector('[data-vote-direction="-1"]').getAttribute('aria-pressed') === 'true');
      assert.equal(await like.locator('[data-vote-count]').textContent(), '0');
      assert.equal(await dislike.locator('[data-vote-count]').textContent(), '1');
      await dislike.focus(); await page.keyboard.press('Space');
      await page.waitForFunction(() => document.querySelector('[data-vote-direction="-1"]').getAttribute('aria-pressed') === 'false');
      for (const width of [1440, 768, 390]) {
        await page.setViewportSize({width, height: 1000});
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
        const box = await dislike.boundingBox(); assert.ok(box.height >= 44 && box.x + box.width <= width);
      }
      await page.setViewportSize({width: 1440, height: 1000});
      await navigate('/customer/discounts/');
      assert.equal(await page.locator('.deal-card').filter({hasText: 'Test Bistro'}).locator('[data-vote-count]').allTextContents().then(values => values.join(',')), '0,0');
      assert.equal((await state()).points, 50);
      await audioIdentity();
    });
    await scenario('Free deal reveals immediately without Points or access receipt', async () => {
      await navigate(deal('Test Style'));
      assert.match(await page.locator('.deal-promo-code').textContent(), /BROWSER-DEAL-CODE-1/);
      assert.equal(await page.locator('[data-open-deal-unlock]').count(), 0);
      assert.equal((await state()).discount_access.length, 0);
      assert.equal((await state()).points, 50);
      await page.evaluate(() => { window.__oldBenefit = document.querySelector('[data-discount-benefit]'); });
      await navigate(deal('Test Bistro'));
      assert.equal(await page.evaluate(() => window.__oldBenefit.textContent), '');
    });
    await scenario('Paid details redact code/link; modal cancel, Escape and focus without charging', async () => {
      assert.ok(!(await page.content()).includes('BROWSER-DEAL-CODE-0'));
      assert.ok(!(await page.content()).includes('https://example.test/deals/0'));
      await openModal();
      assert.equal(await dialog.locator('[data-deal-quote-balance]').textContent(), '50 Points');
      assert.equal(await dialog.locator('[data-deal-quote-cost]').textContent(), '5 Points');
      assert.equal(await dialog.locator('[data-deal-quote-after]').textContent(), '45 Points');
      await page.screenshot({path: path.join(os.tmpdir(), 'mouseforce-discount-unlock-1440.png')});
      await page.keyboard.press('Escape');
      assert.equal(await dialog.isVisible(), false);
      assert.equal(await page.locator('[data-open-deal-unlock]').evaluate(el => el === document.activeElement), true);
      await openModal(); await dialog.locator('[data-deal-cancel]').click();
      assert.equal((await state()).points, 50);
      await audioIdentity();
    });
    await scenario('Double-click purchases exactly once, then reveals; navigation retains music', async () => {
      await openModal();
      await completedNavigation(() => dialog.locator('[data-deal-confirm]').evaluate(el => { el.click(); el.click(); }));
      assert.equal((await state()).points, 45);
      assert.equal((await state()).discount_access.length, 1);
      assert.equal(await page.locator('.deal-promo-code').textContent(), 'BROWSER-DEAL-CODE-0');
      assert.equal(await page.locator('[data-discount-benefit] a').getAttribute('rel'), 'noopener noreferrer');
      await audioIdentity();
      // The committed refresh is another history entry for the same detail URL.
      // Its first Back only restores scroll; the next Back fetches the prior page.
      const key = await page.evaluate(() => history.state.customerShell.key);
      await page.goBack();
      await page.waitForFunction(key => history.state.customerShell.key !== key, key);
      await completedNavigation(() => page.goBack());
      await completedNavigation(() => page.goForward());
      assert.equal(await page.locator('.deal-promo-code').textContent(), 'BROWSER-DEAL-CODE-0');
      assert.equal((await state()).points, 45);
    });
    await scenario('Lost purchase response retries return same access without second charge', async () => {
      await navigate(deal('Test Games'));
      await openModal();
      let lost = false;
      await page.route('**/discounts/*/unlock/', async route => { if (!lost) { lost = true; await route.fetch(); await route.abort('failed'); } else await route.continue(); });
      await dialog.locator('[data-deal-confirm]').click();
      await dialog.locator('[data-deal-message]').filter({hasText: 'could not confirm'}).waitFor();
      assert.equal((await state()).points, 35);
      await completedNavigation(() => dialog.locator('[data-deal-confirm]').click());
      await page.unroute('**/discounts/*/unlock/');
      assert.equal((await state()).points, 35);
      assert.equal((await state()).discount_access.length, 2);
      await audioIdentity();
    });
    await scenario('Changed price requires review; changed balance requires explicit fresh confirmation', async () => {
      await navigate(deal('Test Escape'));
      await fixture({points: '35', day: '7', discount_change: 'price', discount_brand: 'Test Escape'});
      await page.locator('[data-open-deal-unlock]').click();
      await dialog.locator('[data-deal-review]').waitFor({state: 'visible'});
      assert.equal(await dialog.locator('[data-deal-confirm]').isDisabled(), true);
      await completedNavigation(() => dialog.locator('[data-deal-review]').click());
      await openModal();
      await fixture({points: '36', day: '7'});
      await dialog.locator('[data-deal-confirm]').click();
      await page.waitForFunction(() => document.querySelector('[data-deal-quote-balance]').textContent === '36 Points');
      assert.equal((await state()).discount_access.length, 2);
      await dialog.locator('[data-deal-cancel]').click();
    });
    await scenario('Insufficient Points and mobile dialog remain readable and disabled', async () => {
      await fixture({points: '1', day: '7'});
      await navigate(deal('Test Journey'));
      await page.setViewportSize({width: 390, height: 844});
      await openModal();
      assert.equal(await dialog.locator('[data-deal-confirm]').isDisabled(), true);
      assert.match(await dialog.locator('[data-deal-message]').textContent(), /enough Points/);
      const box = await dialog.boundingBox(); assert.ok(box.x >= 0 && box.x + box.width <= 390);
      await page.screenshot({path: path.join(os.tmpdir(), 'mouseforce-discount-unlock-390.png')});
      await dialog.locator('[data-deal-cancel]').click();
      await page.evaluate(() => scrollTo(0,0));
      await page.screenshot({path: path.join(os.tmpdir(), 'mouseforce-discount-detail-390.png')});
      await page.setViewportSize({width: 1440, height: 1000});
    });
    await scenario('Expired purchase remains available only to owner, including saved deals', async () => {
      await fixture({points: '1', day: '7', discount_change: 'expire', discount_brand: 'Test Bistro'});
      await navigate('/customer/discounts/');
      assert.equal(await page.locator('.deal-card').filter({hasText: 'Test Bistro'}).count(), 0);
      await navigate('/customer/discounts/?access=unlocked');
      assert.equal(await page.locator('.deal-card').count(), 2);
      await navigate(deal('Test Bistro'));
      assert.match(await page.locator('.deal-archive-notice').textContent(), /no longer current/);
      assert.equal(await page.locator('.deal-promo-code').textContent(), 'BROWSER-DEAL-CODE-0');
      const other = await browser.newContext();
      await other.request.get(`${base}/__fixture__/?login=discount-other`);
      assert.equal((await other.request.get(base + deal('Test Bistro'))).status(), 404);
      const locked = await other.request.get(base + deal('Test Games'));
      assert.ok(!(await locked.text()).includes('BROWSER-DEAL-CODE-4'));
      await other.close();
    });
    await scenario('Repeated customer navigation preserves exact audio node/source/play position', async () => {
      for (const url of ['/customer/news/?category=world', '/customer/weather/?city=London', '/customer/discounts/', deal('Test Games'), '/customer/offers/', '/customer/dashboard/', '/customer/discounts/']) await navigate(url);
      await completedNavigation(() => page.goBack());
      await completedNavigation(() => page.goForward());
      await audioIdentity();
      console.log('Audio advanced continuously by seconds:', await page.evaluate(() => Math.round(window.__audio.currentTime - window.__initialTime)));
      assert.deepEqual(errors, []);
    });
    await scenario('Hard refresh and a new signed-in browser keep paid access without another charge', async () => {
      audioStarted = false;
      await page.goto(base + deal('Test Games'), {waitUntil: 'domcontentloaded'});
      assert.equal(await page.locator('.deal-promo-code').textContent(), 'BROWSER-DEAL-CODE-4');
      const fresh = await browser.newContext();
      await fresh.request.get(`${base}/__fixture__/?login=1`);
      const response = await fresh.request.get(base + deal('Test Games'));
      assert.match(await response.text(), /BROWSER-DEAL-CODE-4/);
      assert.equal((await state()).points, 1);
      await fresh.close();
    });
    console.log(`${passed} browser scenarios passed. Screenshots saved in the OS temporary directory.`);
  } catch (error) {
    await page.screenshot({path: path.join(os.tmpdir(), 'mouseforce-discounts-browser-failure.png')}).catch(() => {});
    console.error(error.stack);
    console.error('Page errors:', JSON.stringify(errors));
    process.exitCode = 1;
  } finally { await browser.close(); }
})();
