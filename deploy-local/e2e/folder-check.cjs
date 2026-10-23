const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ headless: true, args:['--no-sandbox'] });
  const page = await browser.newPage({ viewport:{width:1500,height:1200} });
  const errors=[]; page.on('pageerror', e=>errors.push(String(e)));
  await page.goto('http://127.0.0.1:8000', { waitUntil:'domcontentloaded' });
  await page.waitForSelector('.login-card');
  await page.locator('input[type=email]').fill(process.env.ADMIN_EMAIL);
  await page.locator('input[type=password]').fill(process.env.ADMIN_PASSWORD);
  await page.locator('button[type=submit]').click();
  await page.waitForSelector('.dashboard');
  // 1) 新建文件夹
  await page.getByRole('button', { name: /New folder/i }).first().click();
  const tmpName = 'tmp-' + Date.now();
  await page.locator('.modal input').first().fill(tmpName);
  await page.getByRole('button', { name: 'Create folder' }).click();
  await page.waitForFunction(n => [...document.querySelectorAll('.folder-card h3')].some(h=>h.textContent===n), tmpName);
  const cards = await page.locator('.folder-card h3').allInnerTexts();
  // 2) 进入 sklearn-tutorials 文件夹
  await page.getByRole("button", { name: "Open folder sklearn-tutorials" }).click();
  await page.waitForSelector('.breadcrumb .current');
  const crumb = await page.locator('.breadcrumb').innerText();
  const titles = await page.locator('.nb-card h3').allInnerTexts();
  await page.screenshot({ path:'../screenshots/21-folder-view.png', fullPage:false });
  // 3) 经侧栏回到根目录
  await page.locator('.sidebar .nav-item').first().click();
  await page.waitForFunction(() => document.querySelector('.breadcrumb .current')?.textContent.includes('My notebooks'));
  // 4) 删除测试文件夹（含确认框）
  page.on('dialog', d=>d.accept());
  await page.getByRole('button', { name: 'Delete folder ' + tmpName }).click();
  await page.waitForFunction(n => ![...document.querySelectorAll('.folder-card h3')].some(h => h.textContent === n), tmpName);
  const after = await page.locator('.folder-card h3').allInnerTexts();
  console.log('FOLDERS', JSON.stringify({tmpName, cards, crumb: crumb.replace(/\n/g,' '), titlesInFolder: titles, afterDelete: after}));
  console.log('pageerrors', errors.length, errors.slice(0,2));
  await browser.close();
  const ok = after.length === 1 && after[0] === 'sklearn-tutorials' && titles.length === 1 && errors.length === 0;
  console.log(ok ? 'PASS folder UI' : 'FAIL folder UI');
  process.exit(ok ? 0 : 1);
})();
