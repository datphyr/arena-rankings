const { execSync } = require('child_process');
const globalRoot = execSync('npm root -g').toString().trim();
const { launch } = require(require('path').join(globalRoot, 'puppeteer-core'));
(async () => {
  const browser = await launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox', '--disable-gpu'] });
  const page = await browser.newPage();
  const errs = [];
  page.on('pageerror', e => errs.push(String(e)));
  async function check(url, label) {
    await page.goto('http://localhost:8080' + url, { waitUntil: 'networkidle0' });
    await new Promise(r => setTimeout(r, 300));
    const body = await page.evaluate(() => document.body.innerText);
    const comma = body.match(/\d{4}-\d{2}-\d{2}, \d{2}:\d{2}/g) || [];
    const space = body.match(/\d{4}-\d{2}-\d{2} \d{2}:\d{2}/g) || [];
    console.log(`${label}: comma=${comma.length} space=${space.length}`);
    if (comma.length) console.log('  samples:', comma.slice(0,3));
  }
  await check('/', 'home');
  await check('/player/923', 'player923');
  await check('/matches', 'matches');
  await check('/tournaments', 'tournaments');
  await check('/h2h?p1=clawz&p2=agent', 'h2h');
  await check('/leaderboard?sort=peak', 'leaderboard');
  // chart legend + tooltip
  await page.goto('http://localhost:8080/player/923', { waitUntil: 'networkidle0' });
  await new Promise(r => setTimeout(r, 500));
  const chart = await page.evaluate(() => {
    const c = Chart.getChart(document.getElementById('historyChart'));
    if (!c) return { err: 'no chart' };
    const legend = (c.legend && c.legend.legendItems || []).map(i => i.text);
    return { legend };
  });
  console.log('chart legend:', JSON.stringify(chart));
  console.log('page errors:', errs.length ? errs : 'none');
  await browser.close();
})();
