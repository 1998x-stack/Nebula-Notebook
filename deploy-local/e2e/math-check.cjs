const { chromium } = require('playwright');
(async () => {
  const nb = process.argv[2];
  const browser = await chromium.launch({ headless: true, args:['--no-sandbox'] });
  const page = await browser.newPage({ viewport:{width:1440,height:1200} });
  const errors=[]; page.on('pageerror', e=>errors.push(String(e)));
  await page.goto('http://127.0.0.1:8000', { waitUntil:'domcontentloaded' });
  await page.waitForSelector('.login-card');
  await page.locator('input[type=email]').fill(process.env.ADMIN_EMAIL);
  await page.locator('input[type=password]').fill(process.env.ADMIN_PASSWORD);
  await page.locator('button[type=submit]').click();
  await page.waitForSelector('.dashboard');
  await page.goto('http://127.0.0.1:8000/notebook/'+nb, { waitUntil:'domcontentloaded' });
  await page.waitForSelector('.markdown-preview', { timeout:30000 });
  await page.waitForSelector('.katex', { timeout:30000 }).catch(()=>{});
  const info = await page.evaluate(() => ({
    mdCells: document.querySelectorAll('.markdown-preview').length,
    katex: document.querySelectorAll('.katex').length,
    katexDisplay: document.querySelectorAll('.katex-display').length,
    katexError: document.querySelectorAll('.katex-error').length,
    sample: [...document.querySelectorAll('.katex-display')].slice(0,1).map(n=>n.textContent.slice(0,70)),
  }));
  await page.screenshot({ path:'../screenshots/20-math-render.png', fullPage:false });
  console.log('MATH', JSON.stringify(info), 'pageerrors', errors.length, errors.slice(0,2));
  await browser.close();
  process.exit(info.katex > 0 && info.katexError === 0 && errors.length === 0 ? 0 : 1);
})();
