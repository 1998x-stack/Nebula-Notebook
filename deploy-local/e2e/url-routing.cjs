const { chromium } = require('playwright');
const BASE = process.env.NEBULA_BASE || 'http://127.0.0.1:8000';
const TITLE = 'URL routing test';
const UUID_PATH = /^\/notebook\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
(async () => {
  const browser = await chromium.launch({ headless:true, args:['--no-sandbox'] });
  const context = await browser.newContext({ viewport:{width:1440,height:1000} });
  const page = await context.newPage();
  const errors=[]; page.on('pageerror', e=>errors.push(String(e)));
  const serverErrors=[]; page.on('response', r=>{ if(r.status()>=500) serverErrors.push(`${r.status()} ${r.url()}`); });
  page.on('dialog', d=>d.accept());
  let createdId = null;
  try {
    await page.goto(BASE, { waitUntil:'domcontentloaded' });
    await page.waitForSelector('.login-card');
    await page.locator('input[type=email]').fill(process.env.ADMIN_EMAIL);
    await page.locator('input[type=password]').fill(process.env.ADMIN_PASSWORD);
    await page.locator('button[type=submit]').click();
    await page.waitForSelector('.dashboard');

    await page.getByRole('button', { name: /New notebook/i }).first().click();
    await page.locator('.modal input').first().fill(TITLE);
    await page.getByRole('button', { name: /Create notebook/i }).click();
    await page.waitForSelector('.editor-shell');
    await page.waitForFunction(() => document.body.innerText.includes('URL routing test'), { timeout: 20000 });
    const path = new URL(page.url()).pathname;
    console.log('create url', page.url());
    if (!UUID_PATH.test(path)) throw new Error(`create did not navigate to id url: ${page.url()}`);
    createdId = path.split('/').pop();

    await page.reload({ waitUntil:'domcontentloaded' });
    await page.waitForSelector('.editor-shell', { timeout: 20000 });
    await page.waitForFunction(() => document.body.innerText.includes('URL routing test'), { timeout: 20000 });
    if (new URL(page.url()).pathname !== path) throw new Error('reload lost notebook url');
    console.log('reload preserves url');

    await page.goBack();
    await page.waitForSelector('.dashboard', { timeout: 20000 });
    if (new URL(page.url()).pathname !== '/') throw new Error(`back should be dashboard: ${page.url()}`);
    await page.goForward();
    await page.waitForSelector('.editor-shell', { timeout: 20000 });
    await page.waitForFunction(() => document.body.innerText.includes('URL routing test'), { timeout: 20000 });
    if (new URL(page.url()).pathname !== path) throw new Error('forward lost notebook url');
    console.log('browser back/forward works');

    await page.locator('.bread').click();
    await page.waitForSelector('.dashboard', { timeout: 20000 });
    if (new URL(page.url()).pathname !== '/') throw new Error('breadcrumb did not return to /');

    await page.goto(BASE + path, { waitUntil:'domcontentloaded' });
    await page.waitForSelector('.editor-shell', { timeout: 20000 });
    await page.waitForFunction(() => document.body.innerText.includes('URL routing test'), { timeout: 20000 });
    console.log('direct deep link works');

    await page.goto(BASE + '/notebook/00000000-0000-0000-0000-000000000000', { waitUntil:'domcontentloaded' });
    await page.waitForSelector('.editor-loading', { timeout: 20000 });
    await page.waitForFunction(() => document.body.innerText.includes('Notebook not found'), { timeout: 20000 });
    await page.locator('.editor-loading button').click();
    await page.waitForSelector('.dashboard', { timeout: 20000 });
    if (new URL(page.url()).pathname !== '/') throw new Error('invalid id back did not return to /');
    console.log('missing notebook shows error + back');

    await page.goto(BASE + '/notebook/not-a-uuid', { waitUntil:'domcontentloaded' });
    await page.waitForSelector('.dashboard', { timeout: 20000 });
    if (new URL(page.url()).pathname !== '/') throw new Error(`malformed path not normalized: ${page.url()}`);
    console.log('malformed path normalized to /');

    await page.locator('.nb-card', { hasText: TITLE }).click();
    await page.waitForSelector('.editor-shell', { timeout: 20000 });
    await page.getByRole('button', { name: /Delete notebook/i }).click();
    await page.waitForSelector('.dashboard', { timeout: 20000 });
    if (new URL(page.url()).pathname !== '/') throw new Error('delete did not return to /');
    console.log('delete returns to /');

    await page.screenshot({ path:'../screenshots/17-url-routing.png', fullPage:true });
    console.log('pageerrors', errors, '5xx', serverErrors);
    const ok = errors.length===0 && serverErrors.length===0;
    console.log(ok ? 'PASS url routing' : 'FAIL url routing');
    await browser.close();
    process.exit(ok?0:1);
  } catch (err) {
    console.error('URL ROUTING FAIL', err);
    try { await page.screenshot({ path:'../screenshots/99-url-routing-failure.png', fullPage:true }); } catch {}
    try {
      const me = await context.request.get(BASE + '/api/auth/me').catch(()=>null);
      if (me && me.ok()) {
        const csrf = (await me.json()).csrf;
        const list = await context.request.get(BASE + '/api/notebooks');
        if (list.ok()) for (const nb of await list.json()) {
          if (nb.title === TITLE) await context.request.delete(BASE + '/api/notebooks/' + nb.id, { headers:{ Origin: BASE, 'X-CSRF-Token': csrf } }).catch(()=>{});
        }
      }
    } catch {}
    await browser.close();
    process.exit(1);
  }
})();
