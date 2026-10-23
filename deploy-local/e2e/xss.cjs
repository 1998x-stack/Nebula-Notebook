const { chromium } = require('playwright');
const BASE = process.env.NEBULA_BASE || 'http://127.0.0.1:8000';
(async () => {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.login-card');
  await page.locator('input[type=email]').fill(process.env.ADMIN_EMAIL);
  await page.locator('input[type=password]').fill(process.env.ADMIN_PASSWORD);
  await page.locator('button[type=submit]').click();
  await page.waitForSelector('.dashboard');
  await page.getByRole('button', { name: /New notebook/i }).first().click();
  await page.locator('.modal input').first().fill('XSS sanitizer check');
  await page.getByRole('button', { name: /Create notebook/i }).click();
  await page.waitForSelector('.editor-shell');
  await page.getByRole('button', { name: /^Connect/ }).click();
  await page.waitForFunction(() => document.querySelector('.runtime-mode')?.textContent.includes('Kernel ready'), { timeout: 90000 });
  await page.locator('.cell .run-cell').first().click();
  await page.getByRole('button', { name: /^Code/ }).first().click();
  await page.locator('.cm-content').last().click();
  await page.keyboard.type(`from IPython.display import HTML\nHTML('<b id="safe-bold">safe</b><img src=x onerror="window.__xss=1"><script>window.__xss=2</script><iframe src="https://example.com"></iframe>')`);
  await page.locator('.cell .run-cell').last().click();
  await page.waitForSelector('#safe-bold', { timeout: 30000 });
  await page.waitForTimeout(1500);
  const result = await page.evaluate(() => ({
    xss: window.__xss,
    bold: !!document.querySelector('#safe-bold'),
    onerror: [...document.querySelectorAll('.output-html img')].map(i => i.getAttribute('onerror')),
    scripts: document.querySelectorAll('.output-html script').length,
    iframes: document.querySelectorAll('.output-html iframe').length,
  }));
  console.log('SANITIZER', JSON.stringify(result), 'pageerrors', errors.length);
  await page.screenshot({ path: 'deploy-local/screenshots/08-xss-output.png', fullPage: true });
  const ok = result.xss === undefined && result.bold === true && result.onerror.every(x => x === null) && result.scripts === 0 && result.iframes === 0 && errors.length === 0;
  console.log(ok ? 'PASS xss sanitizer' : 'FAIL xss sanitizer');
  await browser.close();
  process.exit(ok ? 0 : 1);
})();
