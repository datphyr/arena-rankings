// E2E: autocomplete shows all unique aliases of a player (no dedup by player_id).
const { execSync } = require('child_process');
const globalRoot = execSync('npm root -g').toString().trim();
const { launch } = require(require('path').join(globalRoot, 'puppeteer-core'));
(async () => {
  const browser = await launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox', '--disable-gpu'] });
  const page = await browser.newPage();
  const errs = [];
  page.on('pageerror', e => errs.push(String(e)));
  await page.goto('http://localhost:8080/matches', { waitUntil: 'networkidle0' });
  const input = await page.$('input[data-autocomplete="player"]');
  if (!input) { console.log('no player autocomplete input found'); await browser.close(); return; }
  await input.type('dav');
  await new Promise(r => setTimeout(r, 600));
  const items = await page.evaluate(() => {
    return Array.from(document.querySelectorAll('.ac-item .ac-name')).map(e => e.textContent);
  });
  console.log('autocomplete items for "dav":', JSON.stringify(items));
  const hasDavis = items.includes('davis');
  const hasDavjs = items.includes('davjs');
  console.log('has davis:', hasDavis, '| has davjs:', hasDavjs, '| both (no dedup):', hasDavis && hasDavjs);
  console.log('page errors:', errs.length ? errs : 'none');
  await browser.close();
})();
