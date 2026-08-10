// E2E: player page shows main name + dimmed least-used aliases in brackets.
const { execSync } = require('child_process');
const globalRoot = execSync('npm root -g').toString().trim();
const { launch } = require(require('path').join(globalRoot, 'puppeteer-core'));
(async () => {
  const browser = await launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox', '--disable-gpu'] });
  const page = await browser.newPage();
  const errs = [];
  page.on('pageerror', e => errs.push(String(e)));
  await page.goto('http://localhost:8080/player/1175/davjs', { waitUntil: 'networkidle0' });
  await new Promise(r => setTimeout(r, 800));
  const res = await page.evaluate(() => {
    const h1 = document.querySelector('.player-head h1');
    const alias = document.querySelector('.player-aliases');
    if (!alias) return { hasAlias: false };
    const cs = getComputedStyle(alias);
    return {
      hasAlias: true,
      text: alias.textContent,
      color: cs.color,
      fontWeight: cs.fontWeight,
      fontSize: cs.fontSize,
    };
  });
  console.log('aliases element:', JSON.stringify(res));
  console.log('page errors:', errs.length ? errs : 'none');
  await browser.close();
})();
