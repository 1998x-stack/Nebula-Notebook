const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ headless: true, args:['--no-sandbox'] });
  const page = await browser.newPage({ viewport:{width:1500,height:1100} });
  const errors=[]; page.on('pageerror', e=>errors.push(String(e)));
  await page.goto('http://127.0.0.1:8000', { waitUntil:'domcontentloaded' });
  await page.waitForSelector('.login-card');
  await page.locator('input[type=email]').fill(process.env.ADMIN_EMAIL);
  await page.locator('input[type=password]').fill(process.env.ADMIN_PASSWORD);
  await page.locator('button[type=submit]').click();
  await page.waitForSelector('.dashboard');
  await page.waitForTimeout(1200);

  const home = {
    folderCards: await page.locator('.folder-card h3').allInnerTexts(),
    folderMeta: await page.locator('.folder-card p').allInnerTexts(),
    sidebarFolders: await page.locator('.nav-group .nav-item').allInnerTexts(),
    rootNotebooks: await page.locator('.nb-card h3').allInnerTexts(),
  };
  await page.screenshot({ path:'../screenshots/22-home-with-folder.png', fullPage:false });

  await page.getByRole('button', { name: 'Open folder sklearn-tutorials' }).click();
  await page.waitForFunction(() => document.querySelector('.breadcrumb .current')?.textContent.includes('sklearn-tutorials'));
  const inside = {
    crumb: (await page.locator('.breadcrumb').innerText()).replace(/\n/g,' '),
    notebooks: await page.locator('.nb-card h3').allInnerTexts(),
  };
  await page.screenshot({ path:'../screenshots/23-folder-contents.png', fullPage:false });

  // open the math-heavy notebook straight from the folder
  await page.getByRole('button', { name: /Open notebook 04/ }).click();
  await page.waitForSelector('.katex', { timeout:30000 });
  const math = {
    katex: await page.locator('.katex').count(),
    display: await page.locator('.katex-display').count(),
    err: await page.locator('.katex-error').count(),
  };
  await page.screenshot({ path:'../screenshots/24-math-in-notebook.png', fullPage:false });
  console.log('HOME', JSON.stringify(home));
  console.log('INSIDE', JSON.stringify(inside));
  console.log('MATH', JSON.stringify(math), 'pageerrors', errors.length, errors.slice(0,2));
  await browser.close();
  const ok = home.folderCards.includes('sklearn-tutorials')
          && home.folderMeta.some(m=>m.includes('9 notebook'))
          && inside.notebooks.length === 9
          && math.katex > 0 && math.err === 0 && errors.length === 0;
  console.log(ok ? 'PASS final ui check' : 'FAIL final ui check');
  process.exit(ok?0:1);
})();
