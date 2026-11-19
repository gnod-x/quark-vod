/* Deterministic tests of the actual portal scripts using a minimal DOM fixture.
 * No browser layout, production HTTP requests, saves or cloud access.
 * Run with Node, or invoke verifyPortalUi({ 'api.js': ..., ... }) in V8.
 */
'use strict';

async function verifyPortalUi(sources) {
  const passed = [];
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const test = async (name, run) => { await run(); passed.push(name); };

  function fixture() {
    const elements = new Map();
    function element() {
      return {value:'', textContent:'', innerHTML:'', disabled:false, style:{},
        children:[], isConnected:true,
        replaceChildren() { this.children=[]; this.innerHTML=''; this.textContent=''; },
        appendChild(child) { this.children.push(child); }};
    }
    const document = {
      getElementById(id) { if (!elements.has(id)) elements.set(id,element()); return elements.get(id); },
      createElement:element,
      addEventListener() {}
    };
    const timers = [];
    const environment = new Function('document','window','setTimeout','clearTimeout','fetch',
      'AbortController','console',
      sources['api.js']+'\n'+sources['ui.js']+'\n'+sources['app.js']+'\nreturn {App,API,UI};');
    const context = environment(document, {location:{hostname:'portal.test'}},
      (callback,delay) => { timers.push({delay,callback}); if (delay===3000) callback(); return timers.length; },
      () => {}, (...args) => context.fetch(...args), class {constructor(){this.signal={};} abort(){}},
      {warn(){},error(){}});
    context.fetch = () => { throw new Error('unexpected HTTP request'); };
    context.document=document; context.element=element; context.timers=timers;
    return context;
  }

  await test('all three production scripts parse in V8', () => {
    for (const name of ['api.js','ui.js','app.js']) {
      assert(typeof sources[name]==='string', 'missing source '+name);
      new Function(sources[name]);
    }
    fixture();
  });

  await test('catalogue escapes untrusted titles and poster attributes', async () => {
    const {App,API,document} = fixture();
    document.getElementById('vodSearchInput').value='sample';
    API.searchVod=async () => ({code:0,data:[{id:1,media_type:'tv',title:'<script>bad</script>',
      original_title:'A&B',overview:'<img src=x>',poster:'javascript:alert(1)',year:'2026'}]});
    await App.handleVodSearch();
    const html=document.getElementById('vodResultList').innerHTML;
    assert(html.includes('&lt;script&gt;bad&lt;/script&gt;') && !html.includes('<script>'), 'unsafe title');
    assert(html.includes('A&amp;B') && html.includes('&lt;img src=x&gt;'), 'unsafe original title/overview');
    assert(html.includes('src="/favicon.png"') && !html.includes('javascript:'), 'unsafe poster');
    assert(!document.getElementById('btnVodSearch').disabled, 'search remains disabled');
  });

  await test('empty catalogue distinguishes catalogue from sources', async () => {
    const {App,API,document} = fixture();
    document.getElementById('vodSearchInput').value='empty';
    API.searchVod=async () => ({code:0,data:[]});
    await App.handleVodSearch();
    assert(document.getElementById('vodResultList').textContent.includes('没有匹配作品'), 'missing empty state');
  });

  await test('an old catalogue response cannot overwrite a newer search', async () => {
    const {App,API,document} = fixture(); let release;
    API.searchVod=keyword => keyword==='old' ? new Promise(resolve=>{release=resolve;}) : Promise.resolve({code:0,data:[]});
    document.getElementById('vodSearchInput').value='old';
    const old=App.handleVodSearch();
    document.getElementById('vodSearchInput').value='new';
    await App.handleVodSearch();
    release({code:0,data:[{title:'stale'}]}); await old;
    assert(App.vodItems.length===0, 'old response replaced current result');
  });

  await test('saved candidate is actionable without a share check; locked labels are escaped', async () => {
    const {App,API,document} = fixture();
    App.vodItems=[{id:1,media_type:'tv'}];
    document.getElementById('vodSeason_0').value='2';
    document.getElementById('vodEpisode_0').value='3';
    let request;
    API.vodSources=async payload => {request=payload;return {code:0,saved:true,partial:true,season:2,episode:3,
      nodes:[{node:'one',pages:1},{node:'one',pages:2},{node:'bad',pages:0}],
      candidates:[{name:'saved',reuse_saved:true,quality:{label:'已保存'},sources:['本机']},
                  {name:'<script>bad</script>',locked:true,quality:{label:'4K 待实测'},sources:['A&B']}]};};
    await App.findVodSources(0);
    const html=document.getElementById('vodSources_0').innerHTML;
    assert(request.season===2 && request.episode===3, 'incorrect episode');
    assert(/id="vodCheck_0_0" hidden/.test(html), 'saved check should be hidden');
    assert(/id="vodAdd_0_0" onclick=/.test(html), 'saved action should be enabled');
    assert(/id="vodAdd_0_1" disabled/.test(html), 'unchecked action should be disabled');
    assert(html.includes('点播已存视频') && html.includes('已审核片源'), 'missing saved/locked action');
    assert(html.includes('&lt;script&gt;') && !html.includes('<script>') && html.includes('A&amp;B'), 'unescaped source');
    const message=document.getElementById('vodMessage_0').textContent;
    assert(message.includes('1 个响应节点') && message.includes('部分节点') && message.includes('优先复用'), 'misleading source summary');
  });

  await test('empty source results do not claim the work has no resource', async () => {
    const {App,API,document} = fixture(); App.vodItems=[{id:1,media_type:'movie'}];
    API.vodSources=async () => ({code:0,candidates:[],nodes:[]});
    await App.findVodSources(0);
    assert(document.getElementById('vodSources_0').textContent.includes('不能据此判断'), 'overstated no-source result');
  });

  await test('share inspection enables add only on success', async () => {
    const {App,API,document} = fixture(); App.vodSources={0:[{token:'fixture'}]};
    API.inspectVod=async token => {assert(token==='fixture','wrong token');return {code:0,
      file:{name:'<video>.mkv',size:1024**3,quality:{label:'4K·待实测'}}};};
    await App.checkVodSource(0,0);
    assert(!document.getElementById('vodAdd_0_0').disabled, 'successful inspection did not enable add');
    assert(document.getElementById('vodFile_0_0').textContent.includes('<video>.mkv · 1.00 GiB'), 'file details missing');
    API.inspectVod=async () => ({code:1,msg:'分享不可读'});
    await App.checkVodSource(0,0);
    assert(document.getElementById('vodAdd_0_0').disabled, 'failed inspection enabled add');
    assert(!document.getElementById('vodCheck_0_0').disabled, 'inspection retry disabled');
  });

  for (const state of ['ready','preserved']) await test('queued task reaches '+state+' with an accurate Jellyfin link', async () => {
    const {App,API,document} = fixture(); App.vodSources={0:[{token:'fixture'}]};
    let calls=0;
    API.addVod=async payload => {assert(payload.token==='fixture','wrong add token');return {code:0,job:'job',msg:'已排队'};};
    API.vodJob=async job => {assert(job==='job','wrong job');calls++;return calls===1?{code:0,state:'checking'}:
      {code:0,state,message:state==='preserved'?'保留原入口；未验证当前可读性':'已激活',refresh_ok:false};};
    await App.addVodSource(0,0);
    const message=document.getElementById('vodFile_0_0'), button=document.getElementById('vodAdd_0_0');
    assert(calls===2 && message.children.length===1, 'missing task polling/link');
    const link=message.children[0];
    assert(link.href==='http://portal.test:8097' && link.rel==='noopener', 'incorrect Jellyfin link');
    assert(button.textContent===(state==='ready'?'播放入口已就绪':'已有播放入口'), 'misleading task label');
    assert(message.textContent.includes(state==='ready'?'刷新暂未确认':'未验证当前可读性'), 'missing qualification');
  });

  await test('failed transfer offers retry without showing success', async () => {
    const {App,API,document} = fixture(); App.vodSources={0:[{token:'fixture'}]};
    API.addVod=async () => ({code:0,job:'job',msg:'queued'});
    API.vodJob=async () => ({code:0,state:'failed',message:'上游暂时失败'});
    await App.addVodSource(0,0);
    const message=document.getElementById('vodFile_0_0');
    assert(!document.getElementById('vodAdd_0_0').disabled, 'retry disabled');
    assert(message.textContent==='上游暂时失败' && message.children.length===0, 'false success');
  });

  await test('polling budget exhaustion resumes the same job', async () => {
    const {App,API,element} = fixture(); let calls=0;
    API.vodJob=async () => {calls++;return {code:0,state:'preparing'};};
    const message=element(),button=element(); button.disabled=true;
    await App.pollVodJob('job',message,button);
    assert(calls===100 && !button.disabled && typeof button.onclick==='function', 'missing resume behavior');
    API.vodJob=async job => {assert(job==='job','resumed another job');return {code:0,state:'preserved',message:'已有入口'};};
    await button.onclick();
    assert(message.children.length===1, 'resume failed');
  });

  await test('HTTP failures and aborts reach the UI without a false success', async () => {
    const context=fixture(); const {API}=context; let captured;
    context.fetch=async (endpoint,options) => {captured={endpoint,options};return {ok:true,
      headers:{get:()=> 'application/json'},json:async()=>({code:0})};};
    await API.addVod({token:'fixture'});
    assert(captured.endpoint==='/api/vod/add' && captured.options.method==='POST', 'incorrect add request');
    assert(JSON.parse(captured.options.body).token==='fixture', 'incorrect request body');
    context.fetch=async () => ({ok:false,status:503,json:async()=>({msg:'暂不可读'})});
    try {await API.vodJob('job');throw new Error('accepted HTTP failure');}
    catch(error) {assert(error.message==='暂不可读','lost HTTP error');}
    context.fetch=async () => {const error=new Error('abort');error.name='AbortError';throw error;};
    try {await API.searchVod('sample');throw new Error('accepted abort');}
    catch(error) {assert(error.message.includes('已提交的转存会继续执行'),'misleading timeout');}
  });

  return {engine:'JavaScript/V8',scope:'production scripts with mocked DOM/API; no browser layout or production requests',
    tests:passed,count:passed.length,all_passed:true};
}

if (typeof module!=='undefined') {
  module.exports={verifyPortalUi};
  if (require.main===module) {
    const fs=require('fs'),path=require('path');
    const root=path.resolve(__dirname,'../../hometheater/web/js');
    const sources=Object.fromEntries(['api.js','ui.js','app.js'].map(name=>[name,fs.readFileSync(path.join(root,name),'utf8')]));
    verifyPortalUi(sources).then(report=>console.log(JSON.stringify(report,null,2))).catch(error=>{console.error(error);process.exitCode=1;});
  }
}
