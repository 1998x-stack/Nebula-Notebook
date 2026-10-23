const { chromium } = require('playwright');
const BASE = process.env.NEBULA_BASE || 'http://127.0.0.1:8000';
(async () => {
  const browser = await chromium.launch({ headless:true, args:['--no-sandbox'] });
  const context = await browser.newContext({ viewport:{width:1440,height:1000} });
  const page = await context.newPage();
  const errors=[]; page.on('pageerror', e=>errors.push(String(e)));
  page.on('dialog', d=>d.accept());
  await page.goto(BASE, { waitUntil:'domcontentloaded' });
  await page.waitForSelector('.login-card');
  await page.locator('input[type=email]').fill(process.env.ADMIN_EMAIL);
  await page.locator('input[type=password]').fill(process.env.ADMIN_PASSWORD);
  await page.locator('button[type=submit]').click();
  await page.waitForSelector('.dashboard');
  const title = 'Transient output check';
  await page.getByRole('button', { name: /New notebook/i }).first().click();
  await page.locator('.modal input').first().fill(title);
  await page.getByRole('button', { name: /Create notebook/i }).click();
  await page.waitForSelector('.editor-shell');
  const connect = page.locator('.runtime-controls button.primary');
  await connect.waitFor({ state:'visible', timeout:30000 });
  await page.waitForFunction(() => { const b=document.querySelector('.runtime-controls button.primary'); return b && !b.disabled; }, { timeout:60000 });
  await connect.click();
  await page.waitForFunction(() => document.querySelector('.runtime-mode')?.textContent.includes('Kernel ready'), { timeout:90000 });
  await page.getByRole('button', { name: /^Code/ }).first().click();
  await page.locator('.cm-content').last().click();
  await page.keyboard.type("%matplotlib inline\nimport matplotlib.pyplot as plt\nplt.plot([1,2,3])\nplt.show()");
  await page.locator('.cell .run-cell').last().click();
  await page.waitForSelector('.output-image img', { timeout:45000 });
  await page.waitForFunction(() => document.querySelector('.save-state')?.textContent.includes('All changes saved'), { timeout:30000 });
  const list = await context.request.get(`${BASE}/api/notebooks`);
  const nb = (await list.json()).find(n => n.title === title);
  const docResp = await context.request.get(`${BASE}/api/notebooks/${nb.id}`);
  const doc = (await docResp.json()).document;
  let found=null;
  for (const cell of doc.cells) {
    for (const out of (cell.outputs || [])) {
      if (out.data && out.data['image/png']) found=out;
    }
  }
  console.log('saved version', (await docResp.json()).version, 'image output keys', found && Object.keys(found), 'transient?', found && ('transient' in found));
  console.log('save state', await page.locator('.save-state').innerText(), 'pageerrors', errors.length);
  await page.screenshot({ path:'../screenshots/15-transient-fixed.png', fullPage:true });
  const ok = found && !('transient' in found) && errors.length===0 && (await page.locator('.save-state').innerText()).includes('All changes saved');
  await page.getByRole('button', { name: /Delete notebook/i }).click();
  await page.waitForSelector('.dashboard', { timeout:20000 });
  await browser.close();
  console.log(ok ? 'PASS transient output stripped + autosave' : 'FAIL transient output');
  process.exit(ok?0:1);
})();
