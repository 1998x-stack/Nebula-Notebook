const { chromium } = require('playwright');

// Verifies the dashboard UX work: independently scrolling panes, collapsible
// sidebar, list/grid views with sorting, and URL-routed folders (deep link +
// browser history).
(async () => {
  const browser = await chromium.launch({ headless: true, args:['--no-sandbox'] });
  const context = await browser.newContext({ viewport:{width:1500,height:900} });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));

  await page.goto('http://127.0.0.1:8000', { waitUntil:'domcontentloaded' });
  await page.waitForSelector('.login-card');
  await page.locator('input[type=email]').fill(process.env.ADMIN_EMAIL);
  await page.locator('input[type=password]').fill(process.env.ADMIN_PASSWORD);
  await page.locator('button[type=submit]').click();
  await page.waitForSelector('.dashboard');
  await page.waitForTimeout(1000);

  const results = {};

  // 1) the page itself must not scroll; the panes scroll on their own
  results.docScrollable = await page.evaluate(() => document.documentElement.scrollHeight > window.innerHeight + 2);
  results.dashboardIsScroller = await page.evaluate(() => {
    const d = document.querySelector('.dashboard');
    return getComputedStyle(d).overflowY === 'auto' && getComputedStyle(document.querySelector('.sidebar')).overflow !== 'visible';
  });

  // 2) collapsible sidebar (persisted)
  const width = () => page.locator('.sidebar').evaluate(n => Math.round(n.getBoundingClientRect().width));
  results.wide = await width();
  await page.getByRole('button', { name: 'Collapse sidebar' }).click();
  await page.waitForTimeout(350);
  results.collapsed = await width();
  results.collapsePersisted = await page.evaluate(() => localStorage.getItem('nebula:nav-collapsed'));
  await page.getByRole('button', { name: 'Expand sidebar' }).click();
  await page.waitForTimeout(300);
  results.expanded = await width();

  // 3) folder navigation is URL-driven and history-aware
  const rootUrl = page.url();
  await page.getByRole('button', { name: 'Open folder sklearn-tutorials' }).click();
  await page.waitForFunction(() => location.pathname.startsWith('/folder/'));
  results.folderUrl = page.url();
  results.urlChanged = page.url() !== rootUrl;
  results.crumbInFolder = (await page.locator('.breadcrumb').innerText()).replace(/\n/g,' ');

  const readRows = async () => page.locator('.item-row').evaluateAll(rows => rows.map(r => ({
    kind: r.dataset.kind,
    name: r.querySelector('.item-name').innerText,
    size: r.querySelector('.item-size').innerText,
  })));
  const toBytes = s => { const [v,u] = s.split(' '); const n = parseFloat(v) || 0;
    return n * (u === 'GB' ? 1e9 : u === 'MB' ? 1e6 : u === 'KB' ? 1e3 : 1); };

  // 4) list view + sorting by name (9 real notebooks, 00..08)
  await page.getByRole('button', { name: 'List view' }).click();
  await page.waitForSelector('.item-row');
  await page.locator('.sort-control select').selectOption('name-asc');
  await page.waitForTimeout(300);
  results.sortedByName = await readRows();
  await page.locator('.sort-control select').selectOption('size-desc');
  await page.waitForTimeout(300);
  results.sortedBySize = await readRows();
  await page.screenshot({ path:'../screenshots/25-list-view.png' });

  // 5) grid view still works and keeps the URL
  await page.getByRole('button', { name: 'Grid view' }).click();
  await page.waitForSelector('.nb-card');
  results.gridIsUrlStable = page.url().includes('/folder/');

  // 6) history: back to the root, forward into the folder again
  await page.goBack();
  await page.waitForFunction(() => location.pathname === '/', null, { timeout:20000 });
  results.afterBack = { url: page.url(), crumb: (await page.locator('.breadcrumb').innerText()).replace(/\n/g,' ') };
  await page.goForward();
  await page.waitForFunction(() => location.pathname.startsWith('/folder/'), null, { timeout:20000 });
  results.afterForward = page.url();
  await page.screenshot({ path:'../screenshots/26-folder-url.png' });

  // 7) cold start at the folder URL in a second tab of the same session
  const cold = await context.newPage();
  cold.on('pageerror', e => errors.push('cold: ' + String(e)));
  const resp = await cold.goto(results.folderUrl, { waitUntil:'domcontentloaded' });
  await cold.waitForSelector('.dashboard', { timeout:20000 });
  await cold.waitForTimeout(1200);
  results.deepLink = await cold.evaluate(() => ({
    status: true,
    path: location.pathname,
    crumb: (document.querySelector('.breadcrumb')?.innerText || '').replace(/\n/g,' '),
    notebooks: document.querySelectorAll('[data-kind="notebook"]').length,
  }));
  results.deepLinkHttpStatus = resp.status();
  await cold.close();

  const names = results.sortedByName.map(r => r.name);
  const namesAscending = names.every((v,i,a) => i === 0 || a[i-1].localeCompare(v) <= 0);
  const sizesDescending = results.sortedBySize.map(r => toBytes(r.size)).every((v,i,a) => i === 0 || a[i-1] >= v);
  const expectedOrder = ['00 ·','01 ·','02 ·','03 ·','04 ·','05 ·','06 ·','07 ·','08 ·']
    .every((prefix, i) => namesAscending && results.sortedByName[i].name.startsWith(prefix));

  console.log(JSON.stringify(results, null, 1));
  console.log('pageerrors', errors.length, errors.slice(0, 3));
  const ok = results.docScrollable === false
    && results.dashboardIsScroller === true
    && results.collapsed < results.wide && results.collapsePersisted === '1' && results.expanded === results.wide
    && results.urlChanged && results.folderUrl.includes('/folder/')
    && results.crumbInFolder.includes('sklearn-tutorials')
    && results.sortedByName.length === 9 && namesAscending && expectedOrder
    && sizesDescending
    && results.gridIsUrlStable
    && results.afterBack.url.endsWith('/') && results.afterBack.crumb === 'My notebooks'
    && results.afterForward.includes('/folder/')
    && results.deepLinkHttpStatus === 200 && results.deepLink.crumb.includes('sklearn-tutorials')
    && results.deepLink.notebooks === 9
    && errors.length === 0;
  console.log(ok ? 'PASS ux check' : 'FAIL ux check');
  await browser.close();
  process.exit(ok ? 0 : 1);
})();
