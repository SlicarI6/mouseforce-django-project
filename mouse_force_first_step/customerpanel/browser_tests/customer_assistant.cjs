/* Run only against server.py with CUSTOMER_BROWSER_ASSISTANT_FIXTURE=1.
 * Provider, sockets and music are isolated test fixtures; no live account writes. */
const assert = require('node:assert/strict');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = 'http://127.0.0.1:8776';

(async () => {
  const browser = await chromium.launch({headless: true});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  const page = await context.newPage();
  const errors = [], requests = [];
  let passed = 0;
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    if (request.url().endsWith('/ask-openai/')) requests.push(request);
  });
  await context.addInitScript(() => {
    window.__documentToken = crypto.randomUUID();
    window.__navigations = 0;
    window.addEventListener('customer:navigated', () => window.__navigations++);
    window.WebSocket = class extends EventTarget {
      constructor() { super(); this.readyState = 1; }
      send() {}
      close() { this.readyState = 3; }
    };
  });
  // A silent test WAV, never used by the application or committed as a music file.
  const wav = Buffer.alloc(44 + 8000 * 2 * 120);
  wav.write('RIFF'); wav.writeUInt32LE(wav.length - 8, 4); wav.write('WAVEfmt ', 8);
  wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22);
  wav.writeUInt32LE(8000, 24); wav.writeUInt32LE(16000, 28);
  wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34); wav.write('data', 36);
  wav.writeUInt32LE(wav.length - 44, 40);
  await page.route('**/customer/music/track/**', route => route.fulfill({json: {
    track: {id: 'test-track', name: 'Test only', artist_name: 'Test fixture', audio_url: base + '/__test_audio.wav'},
  }}));
  await page.route('**/__test_audio.wav', route => route.fulfill({contentType: 'audio/wav', body: wav}));
  // Avoid unrelated external test reward image failures.
  await page.route('https://example.com/reward.png', route => route.fulfill({status: 204}));
  async function scenario(name, fn) { await fn(); console.log(`PASS ${++passed}: ${name}`); }
  async function navigate(action) {
    const before = await page.evaluate(() => window.__navigations);
    await action();
    await page.waitForFunction(value => window.__navigations > value, before);
  }
  async function open() {
    await page.locator('#chat-toggle-btn').click();
    await page.locator('#chat-box.visible').waitFor();
  }
  async function choose(privacy = 'No') {
    await page.locator('#chat-body').getByRole('button', {name: privacy, exact: true}).click();
    await page.locator('#chat-body').getByRole('button', {name: 'Product Advice', exact: true}).click();
    assert.equal(await page.locator('#chat-input').isEnabled(), true);
    assert.match(await page.locator('#chat-body').innerText(), /AI Assistant/);
  }
  async function ask(question) {
    await page.locator('#chat-input').fill(question);
    await page.locator('#chat-input').press('Enter');
    await page.waitForFunction(() => document.querySelector('#chat-box').getAttribute('aria-busy') === 'false');
  }
  async function stored() {
    return page.evaluate(() => Object.keys(sessionStorage).filter(k => k.startsWith('mouseforce:customer-assistant:')));
  }
  try {
    await page.request.get(base + '/__fixture__/?login=1');
    await page.goto(base + '/customer/dashboard/', {waitUntil: 'networkidle'});
    await page.waitForFunction(() => typeof window.sendChat === 'function');
    const documentToken = await page.evaluate(() => window.__documentToken);
    await scenario('Privacy link is real; Yes/No wording and original dark-blue widget remain', async () => {
      await open();
      assert.notEqual(await page.locator('#chat-body a').first().getAttribute('href'), '#');
      assert.match(await page.locator('#chat-body').innerText(), /OpenAI.*safety/);
      assert.doesNotMatch(await page.locator('#chat-body').innerText(), /90 days/);
      assert.equal((await page.request.get(await page.locator('#chat-body a').first().getAttribute('href'))).status(), 200);
    });
    await scenario('No permits temporary conversation; Enter calls CSRF-protected backend without delay', async () => {
      await choose();
      const started = Date.now();
      await ask('How do MouseForce Points work?');
      assert.ok(Date.now() - started < 5000);
      assert.match(await page.locator('#chat-body').innerText(), /Test assistant reply: How do MouseForce Points work/);
      assert.equal(requests.length, 1);
      assert.ok(requests[0].headers()['x-csrftoken']);
      assert.equal(requests[0].postDataJSON().privacy, 'temporary');
      assert.deepEqual(await stored(), []);
    });
    await scenario('Searched reply presents safe visible citations', async () => {
      await ask('Find me a good nightclub in London.');
      const source = page.locator('.chat-sources a');
      assert.equal(await source.getAttribute('href'), 'https://example.com/official');
      assert.equal(await source.getAttribute('target'), '_blank');
      assert.match(await source.getAttribute('rel'), /noopener/);
    });
    await scenario('No history survives close; Technical Support ends without pretending staff joined', async () => {
      await page.locator('[data-chat-minimize]').click();
      await open();
      assert.doesNotMatch(await page.locator('#chat-body').innerText(), /Test assistant reply/);
      await page.locator('#chat-body').getByRole('button', {name: 'No', exact: true}).click();
      await page.locator('#chat-body').getByRole('button', {name: 'Technical Support', exact: true}).click();
      assert.match(await page.locator('#chat-body').innerText(), /cannot confirm staff availability/);
      assert.equal(await page.locator('#chat-input').isDisabled(), true);
      const count = requests.length;
      await page.evaluate(() => window.sendChat());
      assert.equal(requests.length, count);
      await page.locator('#chat-body').getByRole('button', {name: 'Start new', exact: true}).click();
    });
    await scenario('Yes saves structured scoped state; provider HTML and unsafe sources remain inert', async () => {
      await choose('Yes');
      await page.route('**/customer/ask-openai/', route => route.fulfill({json: {
        response: '<img src=x onerror="window.__injected=true">',
        sources: [{url: 'javascript:alert(1)', title: 'Unsafe'}, {url: 'https://example.com/safe', title: 'Safe'}],
      }}));
      await ask('A safe rendering check');
      assert.match(await page.locator('#chat-body').innerText(), /<img src=x/);
      assert.equal(await page.locator('#chat-body img[src=x]').count(), 0);
      assert.equal(await page.locator('#chat-body a[href^="javascript:"]').count(), 0);
      assert.equal(await page.evaluate(() => !!window.__injected), false);
      assert.equal((await stored()).length, 1);
      assert.equal(await page.evaluate(() => sessionStorage.getItem('chat_content')), null);
      await page.unroute('**/customer/ask-openai/');
    });
    await scenario('Provider failures are friendly; duplicate sends create one request', async () => {
      await page.route('**/customer/ask-openai/', async route => {
        await new Promise(resolve => setTimeout(resolve, 300));
        await route.fulfill({status: 503, json: {error: 'The assistant is temporarily unavailable. Please try again shortly.'}});
      });
      const before = requests.length;
      await page.locator('#chat-input').fill('Please check a failure');
      await page.evaluate(() => { window.sendChat(); window.sendChat(); });
      await page.waitForFunction(() => document.querySelector('#chat-box').getAttribute('aria-busy') === 'false');
      assert.equal(requests.length, before + 1);
      assert.match(await page.locator('#chat-body').innerText(), /temporarily unavailable/);
      assert.doesNotMatch(await page.locator('#chat-body').innerText(), /undefined/);
      await page.unroute('**/customer/ask-openai/');
    });
    await scenario('Start new ignores an old pending reply', async () => {
      let release;
      const held = new Promise(resolve => { release = resolve; });
      await page.route('**/customer/ask-openai/', async route => {
        await held;
        await route.fulfill({json: {response: 'OLD PENDING REPLY', sources: []}}).catch(() => {});
      });
      await page.locator('#chat-input').fill('Old conversation');
      await page.locator('#chat-input').press('Enter');
      await page.locator('#chat-body').getByRole('button', {name: 'Start new', exact: true}).click();
      release();
      await page.waitForTimeout(250);
      assert.doesNotMatch(await page.locator('#chat-body').innerText(), /OLD PENDING REPLY|Old conversation/);
      assert.equal(await page.locator('#chat-input').isDisabled(), true);
      await page.unroute('**/customer/ask-openai/');
      await choose('Yes');
    });
    await scenario('Same audio keeps playing through pending AI, navigation and Back/Forward', async () => {
      await page.evaluate(() => { document.querySelector('.customer-music-audio').muted = true; });
      await page.locator('.customer-music-play').click();
      await page.waitForFunction(() => document.querySelector('.customer-music-audio').currentTime > 0.2);
      await page.evaluate(() => {
        window.__audio = document.querySelector('.customer-music-audio');
        window.__audioStart = window.__audio.currentTime;
        window.__interruptions = [];
        for (const name of ['pause', 'emptied', 'loadstart']) window.__audio.addEventListener(name, () => window.__interruptions.push(name));
      });
      let release;
      const held = new Promise(resolve => { release = resolve; });
      await page.route('**/customer/ask-openai/', async route => {
        await held;
        await route.fulfill({json: {response: 'REPLY AFTER NAVIGATION', sources: []}}).catch(() => {});
      });
      await page.locator('#chat-input').fill('Long pending question');
      await page.locator('#chat-input').press('Enter');
      await navigate(() => page.locator('.customer-secondary-nav a[href="/customer/news/"]').click());
      release();
      await page.unroute('**/customer/ask-openai/');
      for (const name of ['weather', 'discounts', 'rewards', 'offers']) {
        await navigate(() => page.locator(`.customer-secondary-nav a[href="/customer/${name}/"]`).click());
      }
      await navigate(() => page.locator('.customer-info-back').click());
      await open();
      assert.doesNotMatch(await page.locator('#chat-body').innerText(), /REPLY AFTER NAVIGATION/);
      assert.equal(await page.locator('#chat-input').isEnabled(), true);
      await page.locator('[data-chat-minimize]').click();
      await navigate(() => page.goBack());
      await navigate(() => page.goForward());
      assert.equal(await page.evaluate(() => window.__documentToken), documentToken);
      assert.equal(await page.evaluate(() => window.__audio === document.querySelector('.customer-music-audio') && !window.__audio.paused && window.__audio.currentTime > window.__audioStart), true);
      assert.deepEqual(await page.evaluate(() => window.__interruptions), []);
    });
    for (const width of [1440, 768, 390]) {
      await scenario(`Widget fits ${width}px with readable input and working minimize`, async () => {
        await page.setViewportSize({width, height: 900});
        await open();
        const box = await page.locator('#chat-box').boundingBox();
        assert.ok(box.x >= 0 && box.y >= 0 && box.x + box.width <= width + 1 && box.y + box.height <= 901, JSON.stringify(box));
        assert.ok((await page.locator('#chat-input').boundingBox()).width > 100);
        if (process.env.ASSISTANT_SCREENSHOTS) await page.screenshot({animations: 'disabled', path: path.join(process.env.ASSISTANT_SCREENSHOTS, `assistant-${width}.png`)});
        await page.locator('[data-chat-minimize]').click();
        assert.equal(await page.locator('#chat-box').isVisible(), false);
      });
    }
    await scenario('No chat is cleared on navigation; all three AI personas remain available', async () => {
      await page.setViewportSize({width: 1440, height: 1000});
      await open();
      for (const [random, persona] of [[0, 'Leon S.'], [0.5, 'Sofia W.'], [0.99, 'Charles M.']]) {
        await page.locator('#chat-body').getByRole('button', {name: 'Start new', exact: true}).click();
        await page.evaluate(value => { Math.random = () => value; }, random);
        await choose();
        assert.match(await page.locator('#chat-body').innerText(), new RegExp(persona.replaceAll('.', '\\.')));
      }
      assert.deepEqual(await stored(), []);
      await navigate(() => page.locator('.customer-secondary-nav a[href="/customer/news/"]').click());
      await navigate(() => page.goBack());
      await open();
      assert.equal(await page.locator('#chat-body').getByRole('button', {name: 'No', exact: true}).count(), 1);
      assert.doesNotMatch(await page.locator('#chat-body').innerText(), /How can I help/);
      await choose('Yes');
    });
    await scenario('No state is re-saved when session ends; no page JavaScript exceptions', async () => {
      assert.equal((await stored()).length, 1);
      await page.evaluate(() => window.dispatchEvent(new Event('customer:session-ended')));
      assert.deepEqual(await stored(), []);
      assert.deepEqual(errors, []);
    });
    console.log(`Completed ${passed} assistant browser scenarios.`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
