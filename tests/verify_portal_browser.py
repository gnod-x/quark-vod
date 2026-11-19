"""Real browser interaction against local assets with deterministic API fixtures.
No production writes. Desktop/mobile, source checks, queued status and HTML escaping.
Requires playwright and a temporary Chromium install, passed externally.
"""
import argparse
import datetime
import json
from pathlib import Path
import sys
import urllib.parse


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--output',required=True)
    ap.add_argument('--packages',required=True);ap.add_argument('--screenshots',required=True)
    args=ap.parse_args();sys.path.insert(0,args.packages)
    from playwright.sync_api import sync_playwright
    root=Path(__file__).resolve().parents[2]/'hometheater/web'
    snapshots=Path(args.screenshots);snapshots.mkdir(parents=True,exist_ok=True)
    reports=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-dev-shm-usage','--disable-gpu','--use-gl=disabled'])
        for width in [1440,390]:
            page=browser.new_page(viewport={'width':width,'height':900},device_scale_factor=1)
            errors=[];calls=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            def route(request):
                url=urllib.parse.urlsplit(request.request.url);path=url.path;calls.append(path)
                def respond(data,status=200):request.fulfill(status=status,content_type='application/json',body=json.dumps(data,ensure_ascii=False))
                if path=='/api/vod/search':
                    if 'empty' in url.query:return respond({'code':0,'data':[]})
                    if 'saved' in url.query:return respond({'code':0,'data':[{'id':2,'media_type':'tv','title':'已保存样例','year':'2021','rating':8.2,
                        'overview':'已有视频，复用播放入口','poster':'','in_library':True,'original_title':'Saved example'}]})
                    return respond({'code':0,'data':[{'id':1,'media_type':'tv','title':'样例 <img src=x onerror=alert(1)>','year':'2021','rating':8.2,
                        'overview':'测试简介 " <script>bad</script>','poster':'','in_library':False,'original_title':'Example'}]})
                if path=='/api/vod/sources':
                    if request.request.post_data_json.get('id')==2:
                        return respond({'code':0,'season':1,'episode':1,'saved':True,'partial':False,'cached':False,
                            'nodes':[],'candidates':[{'token':'saved-fixture','name':'播放已保存视频','reuse_saved':True,
                            'quality':{'label':'已保存'},'sources':['本机']}]})
                    return respond({'code':0,'season':2,'episode':1,'saved':False,'partial':True,'cached':False,
                        'nodes':[{'node':'test','pages':2}], 'candidates':[{'token':'fixture','name':'样例 S02 4K <script>bad</script>',
                        'quality':{'label':'4K 标注·待实测'},'sources':['test']}]})
                if path=='/api/vod/inspect':return respond({'code':0,'file':{'name':'样例.S02E01.2160x1080.mp4','size':1024**3,'quality':{'label':'2160×1080 标注'}}})
                if path=='/api/vod/add':return respond({'code':0,'job':'saved-fixture' if request.request.post_data_json.get('token')=='saved-fixture' else 'fixture','msg':'已排队'})
                if path=='/api/vod/job':
                    if 'saved-fixture' in url.query:return respond({'code':0,'state':'preserved','message':'已有播放入口，已保持原样；未验证当前可读性，也未执行新转存。','refresh_ok':False})
                    return respond({'code':0,'state':'ready','message':'文件已保存，播放入口已激活','refresh_ok':True})
                if path.startswith('/api/'):return respond({})
                if path=='/daily_hot.json':return respond([])
                if url.hostname!='portal.test':return request.abort()
                file=root/('index.html' if path=='/' else path.lstrip('/'))
                if file.is_file():return request.fulfill(path=str(file))
                request.fulfill(status=404,body='missing')
            page.route('**/*',route)
            page.goto('http://portal.test/',wait_until='domcontentloaded')
            page.wait_for_function("typeof App !== 'undefined' && document.documentElement.hasAttribute('data-theme')")
            page.locator('#vodSearchInput').fill('样例');page.locator('#btnVodSearch').click()
            page.locator('#vodFind_0').wait_for()
            page.locator('#vodSeason_0').fill('2');page.locator('#vodEpisode_0').fill('1')
            page.locator('#vodFind_0').click();page.locator('#vodCheck_0_0').wait_for()
            assert page.locator('#vodAdd_0_0').is_disabled()
            assert page.locator('#vodSources_0 script').count()==0
            assert '部分节点' in page.locator('#vodMessage_0').inner_text()
            page.locator('#vodCheck_0_0').click()
            page.wait_for_function("!document.getElementById('vodAdd_0_0').disabled")
            assert '2160×1080' in page.locator('#vodFile_0_0').inner_text()
            page.locator('#vodAdd_0_0').click()
            page.locator('#vodFile_0_0 a').wait_for(timeout=15000)
            assert ':8097' in page.locator('#vodFile_0_0 a').get_attribute('href')
            overflow=page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
            page.locator('#vodSearchSection').screenshot(path=str(snapshots/f'portal-{width}.png'))
            page.locator('#vodSearchInput').fill('saved');page.locator('#btnVodSearch').click()
            page.locator('#vodFind_0').click()
            page.wait_for_function("App.vodSources[0]?.[0]?.reuse_saved === true")
            assert page.locator('#vodCheck_0_0').is_hidden()
            assert page.locator('#vodAdd_0_0').is_enabled()
            assert page.locator('#vodAdd_0_0').inner_text()=='点播已存视频'
            page.locator('#vodAdd_0_0').click()
            page.locator('#vodFile_0_0 a').wait_for(timeout=15000)
            assert '未执行新转存' in page.locator('#vodFile_0_0').inner_text()
            assert ':8097' in page.locator('#vodFile_0_0 a').get_attribute('href')
            overflow=overflow or page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
            page.locator('#vodSearchSection').screenshot(path=str(snapshots/f'portal-saved-{width}.png'))
            page.locator('#vodSearchInput').fill('empty');page.locator('#btnVodSearch').click()
            page.wait_for_function("document.getElementById('vodResultList').textContent.includes('没有匹配作品')")
            reports.append({'width':width,'js_errors':errors,'horizontal_overflow':overflow,
                            'source_to_ready':True,'saved_reuse_check_hidden':True,'saved_reuse_to_preserved':True,
                            'escaped_external_content':True,'empty_state':True,
                            'initialized': '/api/sync/status' in calls,'pass':not errors and not overflow})
            page.close()
        browser.close()
    report={'time':datetime.datetime.now().astimezone().isoformat(),'tests':reports,'all_passed':all(r['pass'] for r in reports)}
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if not report['all_passed']:raise SystemExit(1)


if __name__=='__main__':main()
