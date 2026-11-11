const fs=require('fs'),path=require('path'),assert=require('assert');
const {chromium}=require(process.env.PW_PACKAGE || 'playwright-core');
const ROOT=path.resolve(process.env.TEST_ROOT || path.join(__dirname,'..')),OUT=path.resolve(process.env.TEST_OUT || path.join(ROOT,'.browser-results'));fs.mkdirSync(OUT,{recursive:true});
const result={engine:'chromium',sandbox:true,viewports:[],externalRequests:[],unhandled:[],cases:[],scope:'Actual captured HTML from accepted app, local exact assets, synthetic API responses; not live backend/provider E2E'};
let browser;
const mime=p=>p.endsWith('.css')?'text/css':p.endsWith('.js')?'application/javascript':p.endsWith('.svg')?'image/svg+xml':p.endsWith('.ttf')?'font/ttf':'text/html; charset=utf-8';
(async()=>{try{
 browser=await chromium.launch({headless:true,chromiumSandbox:true,...(process.env.CHROMIUM_EXECUTABLE?{executablePath:process.env.CHROMIUM_EXECUTABLE}:{}),timeout:8000,args:['--disable-gpu','--renderer-process-limit=1','--num-raster-threads=1','--disable-background-networking','--disable-extensions','--disable-crash-reporter','--disable-breakpad']});
 result.version=browser.version();
 for(const viewport of [{width:1440,height:900},{width:390,height:844}]){
  const ctx=await browser.newContext({viewport});const page=await ctx.newPage();page.setDefaultTimeout(1500);let scenario='anonymous';
  page.on('pageerror',e=>result.unhandled.push(String(e)));
  await page.route('**/*',async route=>{
   const u=new URL(route.request().url());if(u.origin!=='http://127.0.0.1:8000'){result.externalRequests.push(u.origin+u.pathname);return route.abort();}
   let body={},status=200,p=u.pathname;
   if(p.startsWith('/api/')||p==='/identify'){
    if(p==='/api/me')body=scenario==='owner'?{ok:true,recognized:true,verified_player:true,owner_tag:'#PYLQGRJC',selected_list_id:1}:{ok:true,recognized:false};
    else if(p==='/api/lists')body={ok:true,lists:[{id:1,name:'Fictional private list',is_system:false,is_favorite:false}],selected_list_id:1};
    else if(p==='/api/lists/1/players')body={ok:true,players:[]};
    else if(p.endsWith('/meta'))body={ok:true,list_id:7,owner_tag:'#QYLGCRJP'};
    else if(p.includes('/shared/'))body={ok:true,list:{id:7,name:'Fictional public list',owner_tag:'#QYLGCRJP'},players:[]};
    else if(p.includes('/player-daily-stats/'))body={overall:{wins:0,losses:0,draws:0,total:0,win_rate:0},brawlers:[],modes:[],types:[],per_day:[]};
    else if(p==='/api/auth/email/start')body={challenge_id:'synthetic-challenge'};
    else if(p==='/api/auth/email/verify'){status=400;body={detail:'Synthetic invalid code'};}
    else if(p==='/identify'){status=401;body={detail:'Log in with your verified email first.'};}
    else body={ok:true,players:[],stages:[],data:[]};
    return route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
   }
   let file;
   if(p.startsWith('/static/')){
    file=path.join(ROOT,p);if(!fs.existsSync(file)&&/.(png|webp|jpg|jpeg|ico)$/i.test(p))file=path.join(ROOT,'static/starrlabs',/lab-|background/.test(p)?'neutral-lab.svg':/cream-enamel|teal-metal|crt-glass/.test(p)?'neutral-texture.svg':'neutral-emblem.svg');
   }else{const name=p==='/'?'index':p.startsWith('/player/')?'player':p.slice(1);file=path.join(ROOT,'.smoke-rendered',name+'.html');}
   if(!file.startsWith(ROOT)||!fs.existsSync(file))return route.fulfill({status:404,body:'Not found'});
   return route.fulfill({status:200,contentType:mime(file),body:fs.readFileSync(file)});
  });
  for(const [name,url] of [['index','/'],['login','/login'],['help','/how-it-works'],['about','/us'],['event','/event'],['player','/player/PYLQGRJC']]){
   await page.goto('http://127.0.0.1:8000'+url,{waitUntil:'load'});await page.evaluate(()=>document.fonts.ready);
   const layout=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,title:document.title,body:document.body.innerText.length,broken:[...document.images].filter(i=>!i.complete||!i.naturalWidth).map(i=>i.src)}));
   result.cases.push({viewport:viewport.width,name,...layout});assert(layout.body>40,name+' missing content');assert(layout.scroll<=layout.width+2,name+' overflow '+layout.scroll);assert(!layout.broken.length,name+' broken images');
   if(name==='index'||name==='player')await page.screenshot({path:path.join(OUT,name+'-'+viewport.width+'.png')});
   if(name==='player'){assert(await page.evaluate(()=>typeof Chart==='function'),'local chart missing');await page.locator('#periodFilter').selectOption('7');}
   if(name==='login'){
    await page.locator('#login-email').fill('fictional@example.invalid');await page.locator('#email-step button').click();await page.locator('#code-step').waitFor({state:'visible'});assert(await page.locator('#login-code').evaluate(e=>document.activeElement===e));
    await page.locator('#login-code').fill('000000');await page.locator('#code-step .email-auth-primary').click();await page.waitForFunction(()=>document.querySelector('#email-auth-status').textContent==='Synthetic invalid code');await page.locator('#use-other-email').click();assert(await page.locator('#email-step').isVisible());
    result.cases.push({viewport:viewport.width,name:'mocked-email-form-transitions',passed:true});
   }
  }
  scenario='owner';await page.goto('http://127.0.0.1:8000/');await page.locator('#lists-toolbar').waitFor({state:'visible'});assert((await page.locator('#list-select').innerText()).includes('Fictional private list'));result.cases.push({viewport:viewport.width,name:'owner-restoration',passed:true});
  scenario='anonymous';await page.goto('http://127.0.0.1:8000/?share=synthetic-public');await page.waitForFunction(()=>document.querySelector('#shared-text').textContent.includes('Fictional public list'));assert(!(await page.locator('#lists-toolbar').isVisible()));result.cases.push({viewport:viewport.width,name:'anonymous-public-share',passed:true});
  result.viewports.push(viewport);await ctx.close();
 }
 assert(!result.unhandled.length,JSON.stringify(result.unhandled));assert(!result.externalRequests.length,JSON.stringify(result.externalRequests));result.passed=true;
}catch(e){result.error=String(e).slice(0,12000);result.passed=false;process.exitCode=1;}finally{await browser?.close();fs.writeFileSync(path.join(OUT,'browser.json'),JSON.stringify(result,null,2));console.log(JSON.stringify({passed:result.passed,cases:result.cases.length,error:result.error}));}})();
