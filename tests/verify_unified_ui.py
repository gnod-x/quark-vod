"""Browser regression with isolated HTTP fixtures; no real model or cloud writes."""
import json
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'evidence'/'ui'
OUT.mkdir(parents=True,exist_ok=True)
HTML=(ROOT/'app/quark_vod/static/admin.html').read_text()
ITEM={'id':42,'media_type':'tv','title':'测试剧集','year':'2026','overview':'用于验证季集、版本、片源选择和任务状态的隔离片目。'}
TOKEN='isolated-browser-fixture'
results=[]
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
    for width in (1440,390):
        context=browser.new_context(viewport={'width':width,'height':1000 if width>500 else 844},reduced_motion='reduce')
        page=context.new_page();errors=[];calls=[];saved=[];conflict=[False];expired=[False];empty_season=[False];hold_add=[False];pending_add=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        settings={'version':1,'revision':0,'source':'defaults','enabled':False,'timezone':'Asia/Shanghai','ai':{'enabled':False,'base_url':'','primary_model':'','fallback_models':[],'reasoning':{'probe':'none','subtitle':'low','people':'low','plot':'high','filename':'low'}},'tasks':{'ai_night_worker':{'enabled':False,'schedules':['0 23 * * *'],'timeout_seconds':16200}}}
        def route(r):
            req=r.request;path=urlsplit(req.url).path
            if path=='/services/vod/admin':return r.fulfill(status=200,body=HTML,content_type='text/html')
            assert path.startswith('/services/vod/'),path
            suffix=path[len('/services/vod'):]
            if suffix=='/health':return r.fulfill(json={'version':'isolated-fixture'})
            assert req.headers.get('authorization')=='Bearer '+TOKEN,(path,req.headers)
            assert TOKEN not in req.url
            calls.append((suffix,req.method,req.post_data_json if req.post_data else None))
            if expired[0]:return r.fulfill(status=401,json={'error':'unauthorized'})
            if suffix=='/api/admin/overview':return r.fulfill(json={'stats':{},'capability':{},'strm_summary':{}})
            if suffix=='/api/admin/quota':return r.fulfill(json={'total_gb':0,'quota_limit_gb':100})
            if suffix=='/api/admin/automation':
                if req.method=='PUT':
                    body=req.post_data_json;saved.append(body)
                    if conflict[0]:return r.fulfill(status=409,json={'error':'automation_revision_conflict'})
                    settings.update(body);settings['revision']+=1;settings['source']='server'
                return r.fulfill(json=settings)
            if suffix=='/api/admin/automation/jobs':return r.fulfill(json={'jobs':[{'id':'ai_night_worker','label':'夜间维护','description':'检查片源、字幕和资料。'}]})
            if suffix=='/api/admin/automation/runs':return r.fulfill(json={'runs':[],'worker':{'status':'not_started'}})
            if suffix.startswith('/api/admin/cinema/'):
                op=suffix.rsplit('/',1)[-1]
                data={
                    'discovery':{'code':0,'saved':[],'watching':[],'trending_error':'未配置 TMDB，可先搜索作品。','trending':[]},
                    'search':{'code':0,'data':[ITEM]},
                    'catalogue':{'code':0,'item':ITEM,'seasons':[{'number':1,'name':'第一季'},{'number':2,'name':'第二季'}],'season':(req.post_data_json or {}).get('season',1),'episodes':[{'number':2,'name':'第二集','aired':True},{'number':3,'name':'第三集','aired':True},{'number':4,'name':'待播','aired':False}]},
                    'sources':{'code':0,'partial':False,'candidates':[{'token':'a'*24,'name':'测试剧集 S02E03 HDR','size':2000000000,'quality':{'level':'4k'}}]},
                    'files':{'code':0,'files':[{'token':'b'*24,'name':'S02E03.mkv','size':2000000000,'quality':{'level':'4k'}}]},
                    'add':{'code':0,'job':'j'*24,'msg':'已排队'},'replace':{'code':0,'job':'k'*24,'msg':'换源已排队'},
                    'subscribe':{'code':0,'msg':'已订阅本季自动追更'},
                    'job':{'code':0,'state':'ready','message':'文件已保存，入口已激活','play_url':'/play?cat=test&t=fixture','jellyfin_url':'http://jellyfin.invalid/web/index.html#!/details?id=fixture','item':ITEM}
                }.get(op)
                if op=='catalogue' and empty_season[0] and (req.post_data_json or {}).get('season')==1:
                    data={**data,'episodes':[{'number':1,'name':'待播','aired':False}]}
                if op=='add' and hold_add[0]:
                    pending_add.append(r);return
                if data is not None:return r.fulfill(json=data)
            raise AssertionError('unexpected route '+path)
        page.route('**/*',route)
        page.goto('http://quark.test/services/vod/admin#cinema');page.locator('#token').fill(TOKEN);page.locator('#connect button').click();page.locator('#workspace').wait_for(state='visible')
        assert page.locator('#tab-cinema').get_attribute('aria-selected')=='true'
        assert page.locator('#panel-cinema').is_visible()
        assert TOKEN not in page.url
        page.locator('#tab-automation').click();page.wait_for_function("document.querySelector('#automationFields').disabled===false")
        assert not page.locator('#schedulerEnabled').is_checked()
        assert page.locator('#automationPrimary').input_value()==''
        page.locator('#automationPrimary').fill('fixture-model');page.locator('#automationBaseUrl').fill('https://ai.invalid/v1');page.locator('#automationFallback').fill('fallback-one\nfallback-two')
        page.locator('#automationSave').click();page.wait_for_function("document.querySelector('#automationMessage').textContent.includes('已保存')")
        assert saved[-1]['ai']['fallback_models']==['fallback-one','fallback-two']
        assert set(saved[-1])=={'revision','enabled','timezone','ai','tasks'}
        assert not saved[-1]['enabled'] and not saved[-1]['ai']['enabled']
        conflict[0]=True;page.locator('#automationPrimary').fill('unsaved-model');page.locator('#automationSave').click();page.wait_for_function("document.querySelector('#automationMessage').textContent.includes('已保留')")
        assert page.locator('#automationPrimary').input_value()=='unsaved-model'
        page.locator('#tab-cinema').click();page.locator('#tab-automation').click();assert page.locator('#automationPrimary').input_value()=='unsaved-model'
        page.screenshot(path=str(OUT/f'automation-{width}.png'),full_page=True)
        page.locator('#tab-cinema').click();page.wait_for_function("document.querySelector('#cinemaMessage').textContent.includes('选择作品')")
        page.locator('#cinemaKeyword').fill('测试');page.locator('#cinemaSearch button').click();page.locator('.cinema-poster').first.click();page.locator('#cinemaTitle').wait_for(state='visible')
        page.wait_for_function("document.querySelector('#cinemaSeason').options.length===2")
        page.locator('#cinemaSeason').select_option('2');page.wait_for_timeout(100)
        page.locator('#cinemaVariant').select_option('hdr');page.wait_for_timeout(100)
        page.get_by_role('button',name='第 3 集 第三集',exact=True).click();page.locator('#cinemaSourcesButton').click();page.locator('#cinemaSources .cinema-source').wait_for(state='visible')
        assert next(v for suffix,method,v in reversed(calls) if suffix.endswith('/sources'))['episode']==3
        page.get_by_role('button',name='展开包内文件',exact=True).click();page.locator('#cinemaFileList .cinema-source').wait_for(state='visible')
        page.locator('#cinemaFileList').get_by_role('button',name='立即点播',exact=True).click();page.wait_for_function("document.querySelector('#cinemaTaskMessage').textContent.includes('入口已激活')")
        assert next(v for suffix,method,v in reversed(calls) if suffix.endswith('/add'))=={'token':'b'*24}
        page.locator('#cinemaSources').get_by_role('button',name='换用此源',exact=True).click();page.locator('#cinemaReason').select_option('wrong_audio');page.locator('#cinemaReplace').click();page.wait_for_timeout(100)
        assert next(v for suffix,method,v in reversed(calls) if suffix.endswith('/replace'))['reason']=='wrong_audio'
        page.locator('#cinemaSubscribe').click();page.wait_for_function("document.querySelector('#cinemaSourceMessage').textContent.includes('已订阅')")
        page.screenshot(path=str(OUT/f'cinema-{width}.png'),full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),width
        assert page.evaluate('localStorage.length')==0
        assert page.evaluate('Object.values(sessionStorage).every(v=>!v.includes('+json.dumps(TOKEN)+'))')
        # A season with no aired episode disables source actions, not changing the season.
        empty_season[0]=True
        page.locator('#cinemaSeason').select_option('1');page.wait_for_function("document.querySelector('#cinemaSourcesButton').disabled")
        page.locator('#cinemaSeason').select_option('2');page.wait_for_function("document.querySelector('#cinemaSourcesButton').disabled===false")
        # A late add response must not restore a task or session after logout.
        page.locator('#cinemaSourcesButton').click();page.locator('#cinemaSources .cinema-source').wait_for(state='visible')
        hold_add[0]=True;page.locator('#cinemaSources').get_by_role('button',name='立即点播',exact=True).click();page.wait_for_timeout(100)
        assert pending_add
        page.locator('#disconnect').click();pending_add.pop().fulfill(json={'code':0,'job':'z'*24,'msg':'delayed'})
        page.wait_for_timeout(100);assert page.locator('#cinemaTask').is_hidden();assert page.locator('#workspace').is_hidden()
        assert page.locator('#token').input_value()==''
        # Reconnect and verify an expired session still clears the interface.
        hold_add[0]=False;empty_season[0]=False;page.locator('#token').fill(TOKEN);page.locator('#connect button').click();page.locator('#workspace').wait_for(state='visible')
        page.locator('#cinemaKeyword').fill('测试');page.locator('#cinemaSearch button').click();page.locator('.cinema-poster').first.click();page.wait_for_function("document.querySelector('#cinemaSourcesButton').disabled===false")
        page.locator('#cinemaSourcesButton').click();page.locator('#cinemaSources .cinema-source').wait_for(state='visible');page.locator('#cinemaSources').get_by_role('button',name='立即点播',exact=True).click();page.wait_for_function("document.querySelector('#cinemaTaskMessage').textContent.includes('入口已激活')")
        page.locator('#cinemaTask').wait_for(state='visible');expired[0]=True;page.locator('#cinemaRetryStatus').click();page.locator('#connection').wait_for(state='visible');assert page.locator('#token').input_value()==''
        assert page.locator('#cinemaWork').is_hidden()
        assert not errors,errors
        results.append({'width':width,'checks':'auth,proxy-prefix,empty,settings-save,conflict-preserves-input,season-episode-variant,file-selection,replace,subscribe,job-ready,unaired-season,late-response-logout,logout,no-overflow,no-token-storage','api_calls':len(calls),'page_errors':errors})
        context.close()
    browser.close()
(OUT/'browser-fixtures.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
print(json.dumps(results,ensure_ascii=False,indent=2))
