const puppeteer = require('/usr/lib/node_modules/puppeteer-core');
(async () => {
  const browser = await puppeteer.launch({
    executablePath: '/usr/bin/chromium',
    headless: 'new',
    args: ['--no-sandbox','--disable-setuid-sandbox']
  });
  const page = await browser.newPage();
  const apiCalls = [];
  page.on('request', req => {
    const u = req.url();
    if (u.includes('toornament') && (u.includes('api') || u.includes('/v1/') || u.includes('repository') || u.includes('stages') || u.includes('bracket') || u.includes('matches') || u.includes('participants'))) {
      apiCalls.push({url:u, method:req.method(), headers:req.headers()});
    }
  });
  page.on('response', async res => {
    const u = res.url();
    if (u.includes('api.') || u.includes('/v1/')) {
      let body='';
      try { body = (await res.text()).slice(0,300); } catch(e){}
      apiCalls.push({RESPONSE:true, url:u, status:res.status(), body});
    }
  });
  try {
    await page.goto('https://play.toornament.com/en_US/tournaments/2540134907084726271/stages', {waitUntil:'networkidle2', timeout:45000});
    await new Promise(r=>setTimeout(r,8000));
  } catch(e){ console.log('nav err:', e.message); }
  console.log('=== API/network calls ===');
  for (const c of apiCalls) {
    if (c.RESPONSE) {
      console.log(`\n[RESP ${c.status}] ${c.url}\n  body: ${c.body}`);
    } else {
      const h = c.headers;
      console.log(`\n[REQ ${c.method}] ${c.url}`);
      console.log(`  auth=${h.authorization||h.Authorization||'none'} x-api=${h['x-api-key']||h['X-Api-Key']||'none'}`);
    }
  }
  await browser.close();
})();
