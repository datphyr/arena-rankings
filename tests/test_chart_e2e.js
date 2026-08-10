// E2E test: player page rating-history chart updates in place when the game filter changes.
// History is always all-time (no period filter).
// puppeteer-core is installed GLOBALLY (npm install -g puppeteer-core); resolve it from the global path.
const { execSync } = require('child_process');
const globalRoot = execSync('npm root -g').toString().trim();
const { launch } = require(require('path').join(globalRoot, 'puppeteer-core'));
(async () => {
  const browser = await launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox', '--disable-gpu'] });
  const page = await browser.newPage();
  const pageErrors = [];
  const fetches = [];
  page.on('pageerror', e => pageErrors.push(String(e)));
  page.on('request', r => { if (r.url().includes('/api/player/')) fetches.push(r.url()); });

  await page.goto('http://localhost:8080/player/2/rapha', { waitUntil: 'networkidle0' });
  await new Promise(r => setTimeout(r, 1500));

  const getCount = () => page.evaluate(() => {
    const c = document.getElementById('historyChart');
    const inst = c && (window.Chart && Chart.getChart && Chart.getChart(c));
    return inst ? inst.data.labels.length : -1;
  });

  // No period select should exist
  const hasPeriod = await page.evaluate(() => !!document.querySelector('select[name="period"]'));

  const allCount = await getCount(); // 586 (all games)
  await page.select('select[name="game"]', 'Quake Champions');
  await new Promise(r => setTimeout(r, 2000));
  const qcCount = await getCount();   // 314
  await page.select('select[name="game"]', '');
  await new Promise(r => setTimeout(r, 2000));
  const backAll = await getCount();   // 586

  console.log('has period select:', hasPeriod);
  console.log('all:', allCount, '| QC:', qcCount, '| back to all:', backAll);
  console.log('fetches:', fetches);
  console.log('page errors:', pageErrors.length ? pageErrors : 'none');

  const ok =
    !hasPeriod &&
    allCount === 586 &&
    qcCount === 314 &&
    backAll === 586 &&
    fetches.length === 2 &&
    pageErrors.length === 0;
  console.log(ok ? 'PASS' : 'FAIL');
  await browser.close();
  process.exit(ok ? 0 : 1);
})();
