const { execSync } = require('child_process');
const globalRoot = execSync('npm root -g').toString().trim();
const { launch } = require(require('path').join(globalRoot, 'puppeteer-core'));
(async () => {
  const browser = await launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox', '--disable-gpu'] });
  const page = await browser.newPage();
  const errs = [];
  page.on('pageerror', e => errs.push(String(e)));
  async function ticks(pid) {
    await page.goto('http://localhost:8080/player/' + pid, { waitUntil: 'networkidle0' });
    await new Promise(r => setTimeout(r, 500));
    return page.evaluate(() => {
      const chart = Chart.getChart(document.getElementById('historyChart'));
      if (!chart) return null;
      return chart.scales.x.ticks.map(t => t.label).filter(l => l !== '');
    });
  }
  console.log('rapha (20yr) :', JSON.stringify(await ticks(2)));
  console.log('player1 (20yr):', JSON.stringify(await ticks(1)));
  console.log('player8834(2mo):', JSON.stringify(await ticks(8834)));
  console.log('player14888(2wk):', JSON.stringify(await ticks(14888)));
  console.log('player5070(1day):', JSON.stringify(await ticks(5070)));
  console.log('page errors:', errs.length ? errs : 'none');
  await browser.close();
})();
