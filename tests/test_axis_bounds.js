const { execSync } = require('child_process');
const globalRoot = execSync('npm root -g').toString().trim();
const { launch } = require(require('path').join(globalRoot, 'puppeteer-core'));
(async () => {
  const browser = await launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox', '--disable-gpu'] });
  const page = await browser.newPage();
  const errs = [];
  page.on('pageerror', e => errs.push(String(e)));
  async function inspect(pid) {
    await page.goto('http://localhost:8080/player/' + pid, { waitUntil: 'networkidle0' });
    await new Promise(r => setTimeout(r, 500));
    return page.evaluate(() => {
      const chart = Chart.getChart(document.getElementById('historyChart'));
      if (!chart) return { err: 'no chart' };
      const x = chart.scales.x;
      const data = chart.data.datasets[0].data;
      return {
        dataMin: new Date(data[0].x).toISOString().slice(0,10),
        dataMax: new Date(data[data.length-1].x).toISOString().slice(0,10),
        axisMin: new Date(x.min).toISOString().slice(0,10),
        axisMax: new Date(x.max).toISOString().slice(0,10),
        ticks: x.ticks.map(t => t.label).filter(l => l !== '')
      };
    });
  }
  for (const pid of [474, 2, 8834, 14888, 5070]) {
    const r = await inspect(pid);
    console.log(`player${pid}: data ${r.dataMin}..${r.dataMax} | axis ${r.axisMin}..${r.axisMax} | ticks ${JSON.stringify(r.ticks)}`);
  }
  console.log('page errors:', errs.length ? errs : 'none');
  await browser.close();
})();
