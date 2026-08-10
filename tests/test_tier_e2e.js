// E2E: matches page tier filter dropdown + clicking tier filters.
const { execSync } = require('child_process');
const globalRoot = execSync('npm root -g').toString().trim();
const { launch } = require(require('path').join(globalRoot, 'puppeteer-core'));
(async () => {
  const browser = await launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox', '--disable-gpu'] });
  const page = await browser.newPage();
  const errs = [];
  page.on('pageerror', e => errs.push(String(e)));
  await page.goto('http://localhost:8080/matches?limit=100', { waitUntil: 'networkidle0' });

  // 1. Check tier dropdown exists
  const hasTierSelect = await page.evaluate(() => !!document.querySelector('select[name="tier"]'));
  console.log('tier dropdown present:', hasTierSelect);

  // 2. Check tier links exist and are clickable
  const tierLinks = await page.evaluate(() => {
    return Array.from(document.querySelectorAll('.tier-link')).map(a => a.getAttribute('href'));
  });
  console.log('tier links sample:', JSON.stringify(tierLinks.slice(0, 3)));

  // 3. Click the first tier link and verify filter applied
  if (tierLinks.length) {
    await page.click('.tier-link');
    await new Promise(r => setTimeout(r, 800));
    const url = page.url();
    const selected = await page.evaluate(() => {
      const sel = document.querySelector('select[name="tier"]');
      return sel ? sel.value : null;
    });
    const allTiers = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('.tier-link')).map(a => a.textContent.trim());
    });
    const unique = [...new Set(allTiers)];
    console.log('after click url:', url);
    console.log('tier select value:', selected);
    console.log('unique tiers in results:', JSON.stringify(unique));
  }
  console.log('page errors:', errs.length ? errs : 'none');
  await browser.close();
})();
