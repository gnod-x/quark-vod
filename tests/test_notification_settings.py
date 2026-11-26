"""Authenticated settings, private persistence and bounded Telegram test transport."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BOOT = tempfile.TemporaryDirectory(prefix='vod-notify-api-')
for key in tuple(os.environ):
    if key.startswith(('VOD_', 'NOTIFY_', 'QUARK_', 'JELLYFIN_', 'OPENLIST_', 'AI_PARSE_', 'SELFHEAL_')):
        os.environ.pop(key)
os.environ.update(VOD_BASE_DIR=BOOT.name, VOD_STARTUP_SCAN='0', VOD_STARTUP_WRITE_PROBE='0',
                  AI_PARSE_ENABLED='0', SELFHEAL_ENABLED='0')
APP = Path(__file__).resolve().parents[1] / 'app'
sys.path.insert(0, str(APP))
from fastapi.testclient import TestClient
from quark_vod import config, db, notification_settings as ns, notifier
import main

ORIGINAL_POST = notifier._post
TOKEN = '123456789:' + 'a' * 35
TOKEN2 = '987654321:' + 'b' * 35
AUTH = {'Authorization': 'Bearer fixture-admin'}
URL = '/api/admin/notifications'

class NotificationSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='vod-notify-setting-')
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(config, 'DATA_DIR', str(self.root)))
        self.stack.enter_context(patch.object(config, 'STATE_DB', str(self.root/'state.db')))
        self.stack.enter_context(patch.object(config, '_ENV_FILE_VALUES', {}))
        self.stack.enter_context(patch.dict(os.environ, {k:v for k,v in os.environ.items() if not k.startswith('NOTIFY_')}, clear=True))
        self.stack.enter_context(patch.object(config, 'get_admin_token', return_value='fixture-admin'))
        self.client = TestClient(main.app)
        self.stack.enter_context(patch.object(notifier, '_post', side_effect=AssertionError('network forbidden unless explicitly intercepted')))

    def tearDown(self):
        self.client.close(); db.close(); self.stack.close(); self.temp.cleanup()

    def value(self, revision=0, enabled=True, token=TOKEN, chat='-1001234567', viewing=False):
        tg = {'enabled':enabled,'chat_id':chat}
        if token is not None:tg['bot_token']=token
        return {'revision':revision,'viewing_enabled':viewing,'telegram':tg}

    def save(self, **kwargs):
        value=self.value(**kwargs)
        r=self.client.put(URL,json=value,headers=AUTH)
        self.assertEqual(r.status_code,200,r.text)
        self.assertNotIn(TOKEN,r.text);self.assertNotIn('bot_token',r.text)
        return r.json()

    def error(self,response,status,code):
        self.assertEqual(response.status_code,status,response.text)
        self.assertEqual(response.json(),{'error':code})
        self.assertNotIn(TOKEN,response.text)
        self.assertEqual(response.headers['cache-control'],'no-store')

    def test_auth_all_methods_and_no_request_side_effects(self):
        for method,path,kwargs in [('get',URL,{}),('put',URL,{'json':self.value()}),('post',URL+'/test',{'json':{'revision':0}})]:
            r=getattr(self.client,method)(path,**kwargs)
            self.assertEqual(r.status_code,401)
        self.assertEqual(list(self.root.iterdir()),[])
        with patch.object(config,'get_admin_token',return_value=''):
            self.assertEqual(self.client.get(URL,headers=AUTH).status_code,503)

    def test_environment_fallback_is_read_only_and_test_requires_saved(self):
        with patch.dict(os.environ,{'NOTIFY_TELEGRAM_BOT_TOKEN':TOKEN,'NOTIFY_TELEGRAM_CHAT_ID':'123','NOTIFY_VIEWING_ENABLED':'1'}):
            result=self.client.get(URL,headers=AUTH)
            self.assertEqual(result.status_code,200)
            self.assertEqual(result.json(),{'revision':0,'source':'environment','viewing_enabled':True,
                'telegram':{'enabled':True,'configured':True,'token_configured':True,'chat_id':'123'}})
            self.assertFalse((self.root/'notification-settings.json').exists())
            self.error(self.client.post(URL+'/test',json={'revision':0},headers=AUTH),409,'notification_not_saved')
            self.assertFalse((self.root/'notification-test-rate.json').exists())

    def test_save_private_atomically_and_blank_token_preserves(self):
        first=self.save();self.assertEqual(first['revision'],1);self.assertEqual(first['source'],'server')
        p=self.root/'notification-settings.json'
        self.assertEqual(p.stat().st_mode&0o777,0o600)
        self.assertEqual((self.root/'notification-settings.lock').stat().st_mode&0o777,0o600)
        inode=p.stat().st_ino
        second=self.save(revision=1,token='',chat='456',viewing=True)
        self.assertNotEqual(inode,p.stat().st_ino)
        self.assertEqual(second['revision'],2);self.assertTrue(second['telegram']['token_configured'])
        self.assertEqual(ns.runtime_settings()['telegram']['bot_token'],TOKEN)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith('.')],[])
        self.assertNotIn('NOTIFY_TELEGRAM_BOT_TOKEN',os.environ)

    def test_token_omission_replace_clear_and_no_fallback_after_save(self):
        self.save()
        self.save(revision=1,token=None)
        self.save(revision=2,token=TOKEN2)
        value=self.value(revision=3,enabled=False,token=None,chat='')
        value['telegram']['clear_token']=True
        r=self.client.put(URL,json=value,headers=AUTH);self.assertEqual(r.status_code,200)
        with patch.dict(os.environ,{'NOTIFY_TELEGRAM_BOT_TOKEN':TOKEN,'NOTIFY_TELEGRAM_CHAT_ID':'123','NOTIFY_VIEWING_ENABLED':'1'}):
            result=ns.runtime_settings();self.assertFalse(result['telegram']['enabled'])
            self.assertEqual(result['telegram']['bot_token'],'');self.assertFalse(result['viewing_enabled'])
            self.assertFalse(self.client.get(URL,headers=AUTH).json()['telegram']['token_configured'])
        self.error(self.client.post(URL+'/test',json={'revision':4},headers=AUTH),409,'notification_disabled')
        self.assertFalse((self.root/'notification-test-rate.json').exists())

    def test_conflict_does_not_overwrite_or_send_or_claim_rate(self):
        self.save();before=(self.root/'notification-settings.json').read_bytes()
        self.error(self.client.put(URL,json=self.value(),headers=AUTH),409,'notification_revision_conflict')
        self.error(self.client.post(URL+'/test',json={'revision':0},headers=AUTH),409,'notification_revision_conflict')
        self.assertEqual(before,(self.root/'notification-settings.json').read_bytes())
        self.assertFalse((self.root/'notification-test-rate.json').exists())

    def test_strict_body_shapes_types_duplicate_keys_and_limits(self):
        cases=[b'null',b'[]',b'{',b'{"revision":0,"revision":1}',b'{"revision":NaN}',b'\xff',b'{"x":"'+TOKEN.encode()+b'"}',b'x'*16385]
        for raw in cases:
            with self.subTest(raw=raw[:20]):
                self.error(self.client.put(URL,content=raw,headers={**AUTH,'Content-Type':'application/json'}),400,'notification_invalid_request')
        values=[self.value(),self.value(),self.value(),self.value(),self.value(),self.value(),self.value()]
        values[0]['revision']=True;values[1]['viewing_enabled']=1;values[2]['telegram']['enabled']='true'
        values[3]['telegram']['chat_id']=123;values[4]['telegram']['extra']='bad';values[5]['extra']='bad';values[6]['telegram']['bot_token']=None
        for value in values:self.error(self.client.put(URL,json=value,headers=AUTH),400,'notification_invalid_request')
        self.error(self.client.put(URL,json=self.value(),headers={**AUTH,'Content-Type':'text/plain'}),400,'notification_invalid_request')
        self.error(self.client.post(URL+'/test',json={'revision':0,'message':TOKEN},headers=AUTH),400,'notification_invalid_request')
        # A streamed body without Content-Length must still obey the bound.
        self.error(self.client.put(URL,content=iter([b'x'*8192]*3),headers={**AUTH,'Content-Type':'application/json'}),400,'notification_invalid_request')

    def test_token_chat_validation_and_enabled_requires_credentials(self):
        for token,chat in [('bad','123'),(TOKEN,'@username'),(TOKEN,'0'),(TOKEN,'123\n'),('', '123')]:
            self.error(self.client.put(URL,json=self.value(token=token,chat=chat),headers=AUTH),400,'notification_invalid_request')
        value=self.value();value['telegram']['clear_token']=True
        self.error(self.client.put(URL,json=value,headers=AUTH),400,'notification_invalid_request')
        self.assertFalse((self.root/'notification-settings.json').exists())

    def test_test_message_fixed_and_success_means_only_api_acceptance(self):
        self.save()
        with patch.object(notifier,'_post') as post:
            r=self.client.post(URL+'/test',json={'revision':1},headers=AUTH)
            self.assertEqual(r.json(),{'accepted':True,'channel':'telegram'})
            post.assert_called_once_with('https://api.telegram.org/bot'+TOKEN+'/sendMessage',
                {'chat_id':'-1001234567','text':'家庭影院通知测试：Telegram API 接收验证。'})
        self.assertEqual((self.root/'notification-test-rate.json').stat().st_mode&0o777,0o600)
        with patch.object(notifier,'_post') as post:
            r=self.client.post(URL+'/test',json={'revision':1},headers=AUTH)
            self.error(r,429,'notification_rate_limited');self.assertGreaterEqual(int(r.headers['Retry-After']),1)
            post.assert_not_called()

    def test_failed_test_also_persists_cooldown_without_secret_leak(self):
        self.save()
        with patch.object(notifier,'_post',side_effect=OSError('https://api.telegram.org/bot'+TOKEN)):
            self.error(self.client.post(URL+'/test',json={'revision':1},headers=AUTH),502,'notification_delivery_failed')
        self.error(self.client.post(URL+'/test',json={'revision':1},headers=AUTH),429,'notification_rate_limited')

    def test_corrupt_settings_fail_closed_and_never_overwritten(self):
        p=self.root/'notification-settings.json'
        for raw in ('null','{}','{"schema":1,"schema":1}','not-json','x'*16385):
            p.write_text(raw);p.chmod(0o600)
            with patch.dict(os.environ,{'NOTIFY_TELEGRAM_BOT_TOKEN':TOKEN,'NOTIFY_TELEGRAM_CHAT_ID':'123'}):
                self.error(self.client.get(URL,headers=AUTH),503,'notification_settings_unavailable')
                self.error(self.client.put(URL,json=self.value(),headers=AUTH),503,'notification_settings_unavailable')
                self.assertEqual(notifier.notify('fixture','title','message')['error'],'notification_settings_unavailable')
            self.assertEqual(p.read_text(),raw)

    def test_unsafe_settings_lock_and_parent_paths_rejected(self):
        target=self.root/'outside';target.write_text('untouched')
        for name in ('notification-settings.json','notification-settings.lock','notification-test-rate.json'):
            with self.subTest(name=name):
                if name.endswith('rate.json'):self.save()
                p=self.root/name
                if p.exists():p.unlink()
                p.symlink_to(target)
                if name.endswith('rate.json'):r=self.client.post(URL+'/test',json={'revision':1},headers=AUTH)
                else:r=self.client.get(URL,headers=AUTH)
                self.error(r,503,'notification_settings_unavailable');p.unlink()
        self.assertEqual(target.read_text(),'untouched')
        for name in ('notification-settings.json','notification-settings.lock'):
            p=self.root/name
            if p.exists():p.unlink()
            p.mkdir();self.error(self.client.get(URL,headers=AUTH),503,'notification_settings_unavailable');p.rmdir()
        directory=self.root/'real';directory.mkdir();link=self.root/'linked';link.symlink_to(directory,target_is_directory=True)
        with patch.object(config,'DATA_DIR',str(link)):
            self.error(self.client.get(URL,headers=AUTH),503,'notification_settings_unavailable')

    def test_unsafe_permissions_and_hardlinks_rejected(self):
        self.save();p=self.root/'notification-settings.json';p.chmod(0o644)
        self.error(self.client.get(URL,headers=AUTH),503,'notification_settings_unavailable')
        p.chmod(0o600);os.link(p,self.root/'hardlink')
        self.error(self.client.get(URL,headers=AUTH),503,'notification_settings_unavailable')

    def test_corrupt_rate_state_fails_closed(self):
        self.save();p=self.root/'notification-test-rate.json'
        for raw in ('null','{}','{"last_test_at":NaN}','{"last_test_at":true}'):
            p.write_text(raw);p.chmod(0o600)
            self.error(self.client.post(URL+'/test',json={'revision':1},headers=AUTH),503,'notification_settings_unavailable')

    def test_hot_reload_affects_runtime_and_preserves_other_transports(self):
        self.save(viewing=True)
        with patch.dict(os.environ,{'NOTIFY_WEBHOOK_URL':'http://127.0.0.1:1/fixture'}),patch.object(notifier,'_post') as post:
            r=notifier.notify('fixture','title','message',level='P2')
            self.assertEqual(r['delivered'],2);self.assertEqual(post.call_count,2)
        self.save(revision=1,enabled=False,viewing=False,token=None)
        with patch.object(notifier,'_post') as post:
            r=notifier.notify('fixture2','title','message',level='P2')
            self.assertEqual(r['skipped'],'viewing_disabled');post.assert_not_called()
        with patch.dict(os.environ,{'NOTIFY_WEBHOOK_URL':'http://127.0.0.1:1/fixture'}),patch.object(notifier,'_post') as post:
            r=notifier.notify('fixture3','title','message')
            self.assertEqual(r['delivered'],1);self.assertEqual(post.call_args.args[0],'http://127.0.0.1:1/fixture')

    def test_cross_process_revision_and_rate_claim(self):
        self.save()
        script='''import json,sys\nfrom quark_vod import notification_settings as ns\ntry:\n value=ns.reserve_test(1);print(json.dumps({'claimed':True}))\nexcept ns.SettingsError as e:print(json.dumps({'error':e.code}))\n'''
        env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'PYTHONPATH':str(APP),'VOD_BASE_DIR':str(self.root),'VOD_DATA_DIR':str(self.root),'PYTHONDONTWRITEBYTECODE':'1'}
        workers=[subprocess.Popen([sys.executable,'-B','-c',script],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
        results=[]
        for worker in workers:
            out,err=worker.communicate(timeout=10);self.assertEqual(worker.returncode,0,err);results.append(json.loads(out))
        self.assertEqual(sum(r.get('claimed',False) for r in results),1)
        self.assertIn({'error':'notification_rate_limited'},results)
        # Revisions conflict across independent processes as well.
        script='''import json\nfrom quark_vod import notification_settings as ns\nv={'revision':1,'viewing_enabled':False,'telegram':{'enabled':False,'chat_id':''}}\ntry:print(json.dumps({'revision':ns.update_settings(v)['revision']}))\nexcept ns.SettingsError as e:print(json.dumps({'error':e.code}))\n'''
        workers=[subprocess.Popen([sys.executable,'-B','-c',script],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
        results=[]
        for worker in workers:
            out,err=worker.communicate(timeout=10);self.assertEqual(worker.returncode,0,err);results.append(json.loads(out))
        self.assertIn({'revision':2},results);self.assertIn({'error':'notification_revision_conflict'},results)

    def test_revision_exhaustion_does_not_save_unreadable_settings(self):
        self.save()
        p=self.root/'notification-settings.json';value=json.loads(p.read_text())
        value['revision']=2**53-2;p.write_text(json.dumps(value));before=p.read_bytes()
        self.assertEqual(self.client.get(URL,headers=AUTH).json()['revision'],2**53-2)
        self.error(self.client.put(URL,json=self.value(revision=2**53-2),headers=AUTH),503,'notification_settings_unavailable')
        self.assertEqual(p.read_bytes(),before)

    def test_cross_process_first_directory_and_lock_creation(self):
        directory=self.root/'new'/'data'
        env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'PYTHONPATH':str(APP),
             'VOD_BASE_DIR':str(self.root),'VOD_DATA_DIR':str(directory),'PYTHONDONTWRITEBYTECODE':'1'}
        script='from quark_vod import notification_settings as ns; assert ns.get_settings()["revision"]==0'
        workers=[subprocess.Popen([sys.executable,'-B','-c',script],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(6)]
        for worker in workers:
            out,err=worker.communicate(timeout=10);self.assertEqual(worker.returncode,0,err)
        self.assertEqual((directory/'notification-settings.lock').stat().st_mode&0o777,0o600)
        self.assertFalse((directory/'notification-settings.json').exists())

    def test_telegram_transport_requires_positive_ack_and_rejects_redirect(self):
        class Response:
            def __init__(self,raw):self.raw=raw
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return self.raw[:n]
        for body in (b'{}',b'{"ok":false}',b'x'*65537):
            with patch('urllib.request.OpenerDirector.open',return_value=Response(body)):
                with self.assertRaises(ValueError):ORIGINAL_POST('https://api.telegram.org/bot'+TOKEN+'/sendMessage',{})
        with patch('urllib.request.OpenerDirector.open',return_value=Response(b'{"ok":true}')):
            ORIGINAL_POST('https://api.telegram.org/bot'+TOKEN+'/sendMessage',{})
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import threading
        seen=[]
        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append('post');self.send_response(302);self.send_header('Location','/forbidden');self.end_headers()
            def do_GET(self):
                seen.append('get');self.send_response(200);self.end_headers();self.wfile.write(b'{"ok":true}')
            def log_message(self,*args):pass
        http=ThreadingHTTPServer(('127.0.0.1',0),Redirect)
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        try:
            with self.assertRaises(Exception):ORIGINAL_POST('http://127.0.0.1:'+str(http.server_port)+'/redirect',{})
            self.assertEqual(seen,['post'])
        finally:http.shutdown();http.server_close();thread.join()



def tearDownModule():
    db.close();BOOT.cleanup()

if __name__=='__main__':unittest.main(verbosity=2)
