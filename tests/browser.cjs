// Run with Playwright available in NODE_PATH. No network sources are needed.
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const files = new Set(['index.html','data_updated.js','current_data.js','current_view.js','map_paths.js']);
const server = http.createServer((req,res) => {
  const name = req.url === '/' ? 'index.html' : req.url.slice(1);
  if (!files.has(name)) {res.writeHead(404); res.end(); return;}
  res.setHeader('Content-Type', name.endsWith('.html') ? 'text/html; charset=utf-8' : 'text/javascript; charset=utf-8');
  res.end(fs.readFileSync(path.join(root,name)));
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser = await chromium.launch({headless:true, ...(process.env.BROWSER_PATH ? {executablePath:process.env.BROWSER_PATH} : {})});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[]; page.on('pageerror',e=>errors.push(e.message));
    await page.route('https://fonts.googleapis.com/**',r=>r.abort());
    await page.goto('http://127.0.0.1:'+server.address().port);
    await page.locator('#mapSvg path').first().waitFor();
    assert.equal(await page.locator('#modeSeg [data-mode=baseline]').getAttribute('aria-pressed'),'true');
    await page.locator('#refToggle').click();
    const sourceLinks = await page.locator('#srcList a').evaluateAll(els=>els.map(e=>e.href));
    assert(sourceLinks.includes('https://lddcatalog.ldd.go.th/dataset/ldd_21_04'));
    assert(sourceLinks.includes('https://catalog.disaster.go.th/dataset/b3f55bb3-0f35-4fda-b41c-f337aaf4f605'));
    assert(sourceLinks.every(url=>!url.includes('%C2%B7')));
    await page.locator('#refToggle').click();
    fs.mkdirSync(path.join(root,'tests/screenshots'),{recursive:true});
    await page.screenshot({path:path.join(root,'tests/screenshots/baseline.png')});
    await page.locator('#modeSeg [data-mode=current]').click();
    assert(await page.locator('#mapTabContent').isHidden());
    assert(await page.locator('#currentTabContent').isVisible());
    assert((await page.locator('#currentTabContent article').count())>=2);
    await page.locator('#hazardSeg [data-hazard=drought]').click();
    assert((await page.locator('#currentTabContent').innerText()).includes('ไม่พบรายงานที่ตรงตัวกรอง'));
    await page.locator('#hazardSeg [data-hazard=all]').click();
    await page.screenshot({path:path.join(root,'tests/screenshots/current.png')});
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(root,'tests/screenshots/mobile.png')});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1));
    await page.locator('#modeSeg [data-mode=baseline]').click();
    assert(await page.locator('#mapTabContent').isVisible());
    // Unsafe remote text must remain text, and expired items must disappear.
    await page.evaluate(()=>{
      const fixture={checkedAt:new Date().toISOString(),sources:[],directory:[],events:[
        {title:'<img src=x onerror="window.BAD=true">',url:'javascript:alert(1)',publishedAt:new Date().toISOString(),
          hazards:['eq'],mentionedProvinces:[],note:'test'},
        {title:'EXPIRED_EVENT',url:'https://example.com',publishedAt:'2000-01-01T00:00:00Z',hazards:['eq'],mentionedProvinces:[],note:'test'}]};
      renderCurrent(document.getElementById('currentTabContent'),fixture,{hazard:'all',query:'',region:'',regionMap:{}});
    });
    assert.equal(await page.locator('#currentTabContent img').count(),0);
    assert.equal(await page.locator('#currentTabContent a').count(),0);
    assert(!(await page.locator('#currentTabContent').textContent()).includes('EXPIRED_EVENT'));
    assert.deepEqual(errors,[]);
    // Execute n8n summary code with representative failure/success responses.
    const flow=JSON.parse(fs.readFileSync(path.join(root,'n8n/natcat-daily-update.json'),'utf8'));
    assert.equal(flow.settings.timezone,'Asia/Bangkok');
    assert.equal(flow.nodes[0].parameters.rule.interval[0].field,'days');
    const code=flow.nodes.find(n=>n.type==='n8n-nodes-base.code').parameters.jsCode;
    const run=input=>vm.runInNewContext('(function(){'+code+'})()',{$input:{first:()=>({json:input})},Date,JSON});
    assert.equal(run({checkedAt:new Date().toISOString(),status:'ok',baselineStatus:'ok'})[0].json.needsAttention,false);
    assert.equal(run({error:'timeout'})[0].json.needsAttention,true);
    assert.equal(run({checkedAt:'2000-01-01',status:'ok'})[0].json.needsAttention,true);
    assert.equal(run({checkedAt:new Date().toISOString(),status:'ok',review:{status:'pending_review',changeCount:1}})[0].json.needsAttention,true);
    console.log('PASS: desktop/mobile modes, source links, filters, expiry, XSS, and n8n daily summary');
  } finally { await browser.close(); server.close(); }
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
