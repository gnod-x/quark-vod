"""Notification config layers; real loopback HTTP, intercepted Telegram, no uplink."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

APP = Path(__file__).resolve().parents[1] / 'app'
CHILD = r'''
import json, os, sys, threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from unittest.mock import patch
settings=json.load(sys.stdin); root=Path.cwd(); seen=[]; telegram=[]
class Handler(BaseHTTPRequestHandler):
 def do_POST(self):
  seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
  self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
 def log_message(self,*args): pass
http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
url='http://127.0.0.1:'+str(http.server_port)+'/notify'
def replace(v):return v.replace('@URL@',url).replace('@ROOT@',str(root))
for name,value in settings.get('files',{}).items(): (root/name).write_bytes(replace(value).encode())
if 'file' in settings:
 (root/'credentials.env').write_text(replace(settings['file']))
 os.environ['VOD_ENV_FILE']=str(root/'credentials.env')
for key,value in settings.get('env',{}).items(): os.environ[key]=replace(value)
os.environ['VOD_BASE_DIR']=str(root)
try:
 from quark_vod import config,db,notifier
 original=notifier._post
 def bounded_post(target,payload):
  parsed=urlsplit(target)
  if parsed.hostname=='api.telegram.org':
   assert target=='https://api.telegram.org/botfake-${LITERAL}/sendMessage', 'unexpected fake Telegram target'
   assert payload['chat_id']=='123', 'unexpected fake chat'
   telegram.append(payload);return
  assert parsed.hostname=='127.0.0.1' and parsed.port==http.server_port,'external networking forbidden'
  return original(target,payload)
 with patch.object(notifier,'_post',side_effect=bounded_post):
  first=notifier.notify('fixture','title','message',settings.get('level','P1'))
  second=notifier.notify('fixture','title','message',settings.get('level','P1')) if settings.get('twice') else None
 result={'first':first,'second':second,'http_count':len(seen),'telegram_count':len(telegram),
         'exported':any(key in os.environ for key in settings.get('must_not_export',[])),
         'payloads':seen,'telegram_payloads':telegram}
 db.close()
 print(json.dumps(result))
except ValueError:
 print(json.dumps({'config_error':'ValueError','http_count':len(seen),'telegram_count':len(telegram)}))
finally:http.shutdown();http.server_close();thread.join()
'''

class NotifierConfigTests(unittest.TestCase):
    def run_case(self, **settings):
        with tempfile.TemporaryDirectory(prefix='vod-notify-config-') as directory:
            env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'PYTHONPATH':str(APP),'PYTHONDONTWRITEBYTECODE':'1'}
            p=subprocess.run([sys.executable,'-B','-c',CHILD],cwd=directory,env=env,
                             text=True,input=json.dumps(settings),capture_output=True,timeout=15)
            self.assertEqual(p.returncode,0,p.stderr)
            return json.loads(p.stdout)

    def delivered(self, result, count=1):
        self.assertEqual(result['first'],{'configured':True,'delivered':count,'failed_channels':[]})

    def test_explicit_file_webhook_real_http_and_deduplication(self):
        r=self.run_case(file='NOTIFY_WEBHOOK_URL=@URL@\n',twice=True,must_not_export=['NOTIFY_WEBHOOK_URL'])
        self.delivered(r);self.assertEqual(r['http_count'],1);self.assertFalse(r['exported'])
        self.assertEqual(r['second'],{'configured':True,'delivered':0,'deduplicated':True})
        self.assertEqual(r['payloads'],[{'event':'fixture','level':'P1','title':'title','message':'message'}])

    def test_file_telegram_literal_token_is_not_exported_or_sent(self):
        r=self.run_case(file='NOTIFY_TELEGRAM_BOT_TOKEN=fake-${LITERAL}\nNOTIFY_TELEGRAM_CHAT_ID=123\n',
                        must_not_export=['NOTIFY_TELEGRAM_BOT_TOKEN','NOTIFY_TELEGRAM_CHAT_ID'])
        self.delivered(r);self.assertEqual(r['telegram_count'],1);self.assertEqual(r['http_count'],0)
        self.assertFalse(r['exported']);self.assertEqual(r['telegram_payloads'][0]['text'],'[P1] title\nmessage')

    def test_file_secret_file_telegram(self):
        r=self.run_case(file='NOTIFY_TELEGRAM_BOT_TOKEN_FILE=@ROOT@/token\nNOTIFY_TELEGRAM_CHAT_ID=123\n',files={'token':'fake-${LITERAL}\n'})
        self.delivered(r);self.assertEqual(r['telegram_count'],1)

    def test_process_secret_file_precedes_file_value(self):
        r=self.run_case(file='NOTIFY_TELEGRAM_BOT_TOKEN=wrong-file-token\nNOTIFY_TELEGRAM_CHAT_ID=123\n',
                        files={'token':'fake-${LITERAL}'},env={'NOTIFY_TELEGRAM_BOT_TOKEN_FILE':'@ROOT@/token'})
        self.delivered(r);self.assertEqual(r['telegram_count'],1)

    def test_process_value_precedes_file_and_process_secret_file(self):
        r=self.run_case(file='NOTIFY_TELEGRAM_BOT_TOKEN=wrong-file-token\nNOTIFY_TELEGRAM_CHAT_ID=wrong\n',
                        env={'NOTIFY_TELEGRAM_BOT_TOKEN':'fake-${LITERAL}','NOTIFY_TELEGRAM_CHAT_ID':'123',
                             'NOTIFY_TELEGRAM_BOT_TOKEN_FILE':'@ROOT@/missing'})
        self.delivered(r)

    def test_process_empty_disables_file_transport(self):
        for key in ('NOTIFY_WEBHOOK_URL','NOTIFY_BARK_URL','NOTIFY_WECOM_WEBHOOK','NOTIFY_TELEGRAM_BOT_TOKEN','NOTIFY_TELEGRAM_CHAT_ID'):
            with self.subTest(key=key):
                text=(f'{key}=@URL@\n' if 'TELEGRAM' not in key else 'NOTIFY_TELEGRAM_BOT_TOKEN=fake-${LITERAL}\nNOTIFY_TELEGRAM_CHAT_ID=123\n')
                r=self.run_case(file=text,env={key:''})
                self.assertEqual(r['first'],{'configured':False,'delivered':0});self.assertEqual(r['http_count'],0)

    def test_empty_process_secret_file_disables_file_transport(self):
        r=self.run_case(file='NOTIFY_WEBHOOK_URL=@URL@\n',env={'NOTIFY_WEBHOOK_URL_FILE':''})
        self.assertEqual(r['first'],{'configured':False,'delivered':0})

    def test_all_http_transports_and_secret_file_sources(self):
        for key in ('NOTIFY_BARK_URL','NOTIFY_WECOM_WEBHOOK','NOTIFY_WEBHOOK_URL'):
            with self.subTest(key=key):
                r=self.run_case(file=f'{key}_FILE=@ROOT@/url\n',files={'url':'@URL@\n'})
                self.delivered(r);self.assertEqual(r['http_count'],1)
                p=r['payloads'][0]
                if key=='NOTIFY_BARK_URL':self.assertEqual(p,{'title':'[P1] title','body':'message','group':'家庭影院'})
                if key=='NOTIFY_WECOM_WEBHOOK':self.assertEqual(p,{'msgtype':'text','text':{'content':'[P1] title\nmessage'}})

    def test_p2_requires_explicit_one_and_process_empty_wins(self):
        for value in ('','0','true','yes','1'):
            with self.subTest(value=value):
                r=self.run_case(file=f'NOTIFY_WEBHOOK_URL=@URL@\nNOTIFY_VIEWING_ENABLED={value}\n',level='P2')
                self.assertEqual(r['http_count'],int(value=='1'))
        r=self.run_case(file='NOTIFY_WEBHOOK_URL=@URL@\nNOTIFY_VIEWING_ENABLED=1\n',env={'NOTIFY_VIEWING_ENABLED':''},level='P2')
        self.assertEqual(r['first']['skipped'],'viewing_disabled')

    def test_unconfigured_and_no_implicit_dotenv(self):
        r=self.run_case(files={'.env':'NOTIFY_WEBHOOK_URL=@URL@\n'})
        self.assertEqual(r['first'],{'configured':False,'delivered':0});self.assertEqual(r['http_count'],0)

    def test_missing_or_invalid_environment_file_fails_before_transport(self):
        for text in ('invalid line','NOTIFY_WEBHOOK_URL="unclosed','BAD-KEY=bad','#'+'x'*1048576):
            with self.subTest(kind=text[:25]):
                r=self.run_case(file=text);self.assertEqual(r['config_error'],'ValueError');self.assertEqual(r['http_count'],0)
        r=self.run_case(env={'VOD_ENV_FILE':'@ROOT@/missing'});self.assertEqual(r['config_error'],'ValueError')

    def test_invalid_secret_file_fails_without_network(self):
        for value in ('@ROOT@/missing','@ROOT@/oversize'):
            r=self.run_case(file=f'NOTIFY_WEBHOOK_URL_FILE={value}\n',files={'oversize':'x'*1048577})
            self.assertEqual(r['config_error'],'ValueError');self.assertEqual(r['http_count'],0)

if __name__=='__main__':unittest.main(verbosity=2)
