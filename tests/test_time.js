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
    await new Promise(r => setTimeout(r, 400));
    return page.evaluate(() => {
      const chart = Chart.getChart(document.getElementById('historyChart'));
      if (!chart) return { err: 'no chart' };
      const data = chart.data.datasets[0].data;
      const x = chart.scales.x;
      const seen = new Set();
      let dupX = 0;
      for (const p of data) { if (seen.has(p.x)) dupX++; seen.add(p.x); }
      let backward = 0;
      for (let i = 1; i < data.length; i++) if (data[i].x < data[i-1].x) backward++;
      return {
        points: data.length, uniqueX: seen.size, dupX, backward,
        axisMin: new Date(x.min).toISOString(), axisMax: new Date(x.max).toISOString(),
        ticks: x.ticks.map(t=>t.label).filter(l=>l!==''),
        legend: (chart.legend&&chart.legend.legendItems||[]).map(i=>i.text)
      };
    });
  }
  for (const pid of [923, 474, 2, 8834, 14888, 5070]) {
    const r = await inspect(pid);
    console.log(`player${pid}:`, JSON.stringify(r));
  }
  console.log('page errors:', errs.length ? errs : 'none');
  await browser.close();
})();
