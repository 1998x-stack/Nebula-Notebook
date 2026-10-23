const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ headless: true, args:['--no-sandbox'] });
  const page = await browser.newPage({ viewport:{width:1440,height:1000} });
  await page.goto('http://127.0.0.1:8000', { waitUntil:'domcontentloaded' });
  await page.waitForSelector('.login-card');
  await page.locator('input[type=email]').fill(process.env.ADMIN_EMAIL);
  await page.locator('input[type=password]').fill(process.env.ADMIN_PASSWORD);
  await page.locator('button[type=submit]').click();
  await page.waitForSelector('.dashboard');
  await page.screenshot({ path:'../screenshots/09-handover-dashboard.png', fullPage:true });
  await page.locator('.nb-card').first().click();
  await page.waitForSelector('.editor-shell');
  await page.screenshot({ path:'../screenshots/10-handover-editor.png', fullPage:true });
  console.log('handover screenshots saved');
  await browser.close();
})();
