const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE = process.env.NEBULA_BASE || 'http://127.0.0.1:8000';
const EMAIL = process.env.ADMIN_EMAIL;
const PASSWORD = process.env.ADMIN_PASSWORD;
const SHOT_DIR = path.resolve(__dirname, '..', 'screenshots');
fs.mkdirSync(SHOT_DIR, { recursive: true });

const results = [];
function check(name, cond, detail = '') {
  results.push({ name, ok: !!cond, detail });
  console.log(`${cond ? '  PASS' : '  FAIL'}  ${name}${cond ? '' : ' ' + detail}`);
}

(async () => {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
  const page = await context.newPage();
  const pageErrors = [];
  const consoleErrors = [];
  page.on('pageerror', e => pageErrors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });

  try {
    await page.goto(BASE, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.login-card', { timeout: 20000 });
    await page.screenshot({ path: path.join(SHOT_DIR, '01-login.png'), fullPage: true });

    await page.locator('input[type=email]').fill(EMAIL);
    await page.locator('input[type=password]').fill(PASSWORD);
    await page.locator('button[type=submit]').click();
    await page.waitForSelector('.dashboard', { timeout: 20000 });
    await page.screenshot({ path: path.join(SHOT_DIR, '02-dashboard.png'), fullPage: true });
    check('UI login + dashboard', true);

    // Administration modal: create the user we will share with.
    const suffix = Date.now().toString(36);
    const viewerEmail = `e2e-viewer-${suffix}@test.local`;
    const viewerPassword = 'E2eViewerPassword-123!';
    await page.getByRole('button', { name: /Administration/ }).click();
    await page.waitForSelector('.modal');
    await page.locator('.admin-form input[type=email]').fill(viewerEmail);
    await page.locator('.admin-form input[type=password]').fill(viewerPassword);
    await page.locator('.admin-form button[type=submit]').click();
    await page.waitForFunction(
      (email) => [...document.querySelectorAll('.scroll-list .list-line span')].some(el => el.textContent.includes(email)),
      viewerEmail, { timeout: 15000 });
    await page.screenshot({ path: path.join(SHOT_DIR, '03-admin.png'), fullPage: true });
    check('UI administration creates user', true);
    await page.locator('.modal-head .icon-btn').click();

    // Create notebook.
    await page.getByRole('button', { name: /New notebook/i }).first().click();
    await page.waitForSelector('.modal');
    const title = `E2E notebook ${suffix}`;
    await page.locator('.modal input').first().fill(title);
    await page.getByRole('button', { name: /Create notebook/i }).click();
    await page.waitForSelector('.editor-shell', { timeout: 20000 });
    check('UI creates and opens notebook', true);

    // Connect CPU runtime and wait for kernel-ready indicator.
    await page.getByRole('button', { name: /^Connect/ }).click();
    await page.waitForFunction(() => {
      const el = document.querySelector('.runtime-mode');
      return el && el.textContent.includes('Kernel ready');
    }, { timeout: 90000 });
    check('UI connects CPU runtime and kernel channel', true);

    // Run the default code cell.
    await page.locator('.cell .run-cell').first().click();
    await page.waitForFunction(() => document.body.innerText.includes('Hello, Nebula Notebook!'), { timeout: 45000 });
    check('UI executes default cell and renders stdout', true);

    // Add a new code cell, type a new expression and run it.
    await page.getByRole('button', { name: /^Code/ }).first().click();
    const editors = page.locator('.cm-content');
    const lastEditor = editors.nth(await editors.count() - 1);
    await lastEditor.click();
    await page.keyboard.type('sum(range(10))');
    await page.locator('.cell .run-cell').last().click();
    await page.waitForFunction(() => {
      const nodes = [...document.querySelectorAll('.output-text')];
      return nodes.some(n => n.textContent.trim() === '45');
    }, { timeout: 45000 });
    check('UI edits and executes a new code cell', true);
    await page.screenshot({ path: path.join(SHOT_DIR, '04-editor-run.png'), fullPage: true });

    // Autosave.
    await page.waitForFunction(() => {
      const el = document.querySelector('.save-state');
      return el && el.textContent.includes('All changes saved');
    }, { timeout: 30000 });
    check('UI autosave reaches saved state', true);

    // Workspace files: upload a CSV from the Files dialog.
    const uploadPath = path.join(SHOT_DIR, 'e2e-data.csv');
    fs.writeFileSync(uploadPath, 'x,y\n1,2\n');
    await page.getByRole('button', { name: /^Files/ }).click();
    await page.waitForSelector('.modal');
    await page.setInputFiles('.modal input[type=file]', uploadPath);
    await page.waitForFunction(() => document.body.innerText.includes('e2e-data.csv'), { timeout: 20000 });
    await page.screenshot({ path: path.join(SHOT_DIR, '05-files.png'), fullPage: true });
    check('UI workspace upload lists file', true);
    await page.locator('.modal-head .icon-btn').click();

    // Share with the viewer account.
    await page.getByRole('button', { name: /^Share/ }).click();
    await page.waitForSelector('.modal');
    await page.locator('.share-form input[type=email]').fill(viewerEmail);
    await page.locator('.share-form select').selectOption('viewer');
    await page.locator('.modal').getByRole('button', { name: /^Share$/ }).click();
    await page.waitForFunction((email) => document.body.innerText.includes(email), viewerEmail, { timeout: 20000 });
    await page.screenshot({ path: path.join(SHOT_DIR, '06-share.png'), fullPage: true });
    check('UI shares notebook with viewer role', true);
    await page.locator('.modal-head .icon-btn').click();

    // Export downloads a valid ipynb.
    const downloadPromise = page.waitForEvent('download', { timeout: 20000 });
    await page.getByRole('button', { name: /^Export/ }).click();
    const download = await downloadPromise;
    const exportPath = path.join(SHOT_DIR, 'exported.ipynb');
    await download.saveAs(exportPath);
    const exported = JSON.parse(fs.readFileSync(exportPath, 'utf8'));
    check('UI export downloads valid nbformat document', exported.nbformat === 4 && exported.cells.length >= 2);

    // Reload: the document and saved output persist from SQLite.
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.dashboard', { timeout: 20000 });
    await page.locator('.nb-card', { hasText: title }).click();
    await page.waitForSelector('.editor-shell', { timeout: 20000 });
    await page.waitForFunction(() => document.body.innerText.includes('45'), { timeout: 20000 });
    check('UI reload shows persisted notebook content and output', true);

    // Second context: viewer opens the shared notebook read-only.
    const viewerContext = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const viewer = await viewerContext.newPage();
    await viewer.goto(BASE, { waitUntil: 'domcontentloaded' });
    await viewer.waitForSelector('.login-card');
    await viewer.locator('input[type=email]').fill(viewerEmail);
    await viewer.locator('input[type=password]').fill(viewerPassword);
    await viewer.locator('button[type=submit]').click();
    await viewer.waitForSelector('.dashboard', { timeout: 20000 });
    await viewer.locator('.nb-card', { hasText: title }).click();
    await viewer.waitForSelector('.editor-shell', { timeout: 20000 });
    await viewer.waitForFunction(() => document.body.innerText.includes('Read-only notebook'), { timeout: 15000 });
    const runDisabled = await viewer.locator('.run-cell').first().isDisabled();
    check('viewer UI is read-only and cannot run cells', runDisabled);
    await viewer.screenshot({ path: path.join(SHOT_DIR, '07-viewer.png'), fullPage: true });
    await viewerContext.close();

    // Cleanup: delete the notebook via the owner UI.
    page.on('dialog', d => d.accept());
    await page.getByRole('button', { name: /Delete notebook/i }).click();
    await page.waitForSelector('.dashboard', { timeout: 20000 });
    check('UI deletes notebook', true);

    check('no uncaught page errors', pageErrors.length === 0, pageErrors.join(' | '));
    const relevantConsole = consoleErrors.filter(t => !/401|403|favicon|Failed to load resource/i.test(t));
    check('no unexpected console errors', relevantConsole.length === 0, relevantConsole.join(' | '));

    const failed = results.filter(r => !r.ok).length;
    console.log(`\nSUMMARY ${results.length - failed}/${results.length} passed`);
    process.exitCode = failed ? 1 : 0;
  } catch (err) {
    console.error('E2E ERROR', err);
    try { await page.screenshot({ path: path.join(SHOT_DIR, '99-failure.png'), fullPage: true }); } catch {}
    process.exitCode = 2;
  } finally {
    await browser.close();
  }
})();
