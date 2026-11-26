"""Real app/browser smoke in an ephemeral, unconfigured installation."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.request
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'evidence'/'ui';OUT.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(prefix='quark-unified-browser-') as directory:
    token=secrets.token_urlsafe(24)
    env={k:v for k,v in os.environ.items() if not k.startswith(('VOD_','QUARK_','OPENLIST_','JELLYFIN','TMDB','AI_','SELFHEAL','HOMETHEATER'))}
    env.update(VOD_BASE_DIR=directory,VOD_ADMIN_TOKEN=token,PYTHONPATH=str(ROOT/'app'),PYTHONDONTWRITEBYTECODE='1',VOD_JELLYFIN_ENABLED='0',SELFHEAL_ENABLED='0',AI_PARSE_ENABLED='0',VOD_PREFETCH_ENABLED='0')
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    process=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-m','uvicorn','main:app','--host','127.0.0.1','--port',str(port),'--no-access-log'],env=env,cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
    origin=f'http://127.0.0.1:{port}'
    try:
        for _ in range(80):
            if process.poll() is not None:raise RuntimeError(process.stderr.read())
            try:
                with urllib.request.urlopen(origin+'/health',timeout=.5):break
            except OSError:time.sleep(.1)
        else:raise RuntimeError('real app did not start')
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
            pages=[]
            for width in (1440,390):
                page=browser.new_page(viewport={'width':width,'height':1000 if width>500 else 844});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(origin+'/admin');page.locator('#token').fill(token);page.locator('#connect button').click();page.locator('#workspace').wait_for(state='visible')
                page.locator('#tab-automation').click();page.wait_for_function("document.querySelector('#automationFields').disabled===false")
                assert page.locator('#automationJobs [data-job]').count()==9
                assert not page.locator('#schedulerEnabled').is_checked()
                assert not page.locator('#automationAiEnabled').is_checked()
                assert page.locator('#automationJobs [data-field=enabled]:checked').count()==(1 if width==390 else 0)
                if width==1440:
                    job=page.locator('[data-job=ai_night_worker]');job.locator('[data-field=enabled]').check();page.locator('#automationPrimary').fill('local-config-fixture');page.locator('#automationSave').click();page.wait_for_function("document.querySelector('#automationMessage').textContent.includes('已保存')")
                    job.get_by_role('button',name='预演检查').click();page.wait_for_function("document.querySelector('#automationMessage').textContent.includes('已排队')")
                    child=subprocess.run([str(ROOT/'.venv/bin/python'),'-c','from quark_vod.automation_runner import cli_main; raise SystemExit(cli_main(["worker","--once"]))'],env=env,cwd=ROOT,text=True,capture_output=True,timeout=20)
                    assert child.returncode==0,child.stderr
                    page.locator('#automationRefreshRuns').click();page.wait_for_function("document.querySelector('#automationRuns').textContent.includes('预演完成')")
                page.screenshot(path=str(OUT/f'actual-automation-{width}.png'),full_page=True)
                page.locator('#tab-cinema').click();page.wait_for_function("document.querySelector('#cinemaMessage').textContent.includes('选择作品')")
                assert '暂无已存作品' in page.locator('#cinemaGroups').inner_text()
                assert 'TMDB' in page.locator('#cinemaGroups').inner_text()
                page.screenshot(path=str(OUT/f'actual-cinema-empty-{width}.png'),full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert not errors,errors
                pages.append({'width':width,'catalog':9,'js_errors':errors,'real_app':True,'credentials':'ephemeral process-only fixture'})
                page.close()
            browser.close()
        (OUT/'actual-browser.json').write_text(json.dumps(pages,ensure_ascii=False,indent=2))
        print(json.dumps(pages,ensure_ascii=False))
    finally:
        process.terminate()
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:process.kill();process.wait()
