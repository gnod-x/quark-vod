#!/usr/bin/env python3
"""Exercise Jellyfin2 PlaybackInfo and authenticated static stream with real decoding.

Only existing live library items are eligible. A loopback credential adapter adds
Jellyfin auth as an unredirected header, so credentials cannot reach the CDN.
No new source is requested, no watched/session-progress event is submitted.
"""
import argparse
from contextlib import closing
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import pathlib
import re
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import sys
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
from media_result import assess


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output',required=True)
    ap.add_argument('--samples',type=int,default=2)
    ap.add_argument('--item-id',help='Restrict to one existing live Jellyfin item')
    ap.add_argument('--routes',default='jellyfin',help='Comma separated: direct,play,jellyfin')
    ap.add_argument('--positions',default='0,120,600')
    ap.add_argument('--duration',type=int,default=12)
    ap.add_argument('--full',action='store_true',help='Decode one explicit existing item through Jellyfin until EOF')
    ap.add_argument('--decode-timeout',type=int,default=90,help='Per-decode wall timeout, 15 to 600 seconds')
    ap.add_argument('--native',action='store_true',help='Use unwrapped FFmpeg for comparison')
    ap.add_argument('--relay',action='store_true',help='Diagnostic only: relay CDN bytes instead of preserving redirects')
    ap.add_argument('--interleaved-read',choices=('0','1'),help='Diagnostic MOV demuxer setting')
    ap.add_argument('--resolve',action='store_true',help='Resolve redirects once, as the production play wrapper does')
    ap.add_argument('--keepalive',action='store_true',help='Diagnostic FFmpeg HTTP persistent connections')
    args=ap.parse_args()
    routes=args.routes.split(',')
    positions=[int(x) for x in args.positions.split(',')]
    if not set(routes)<=set(('direct','play','jellyfin')):ap.error('unsupported route')
    if not 15<=args.decode_timeout<=600:ap.error('decode timeout must be between 15 and 600 seconds')
    if args.full and (not args.item_id or routes!=['jellyfin'] or positions!=[0]):
        ap.error('--full requires --item-id, --routes jellyfin and --positions 0')
    base=pathlib.Path('/home/dong/projects/hometheater')
    env={}
    for line in (base/'.env').read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k,v=line.split('=',1);env[k.strip()]=v.strip().strip(chr(34)).strip(chr(39))
    token=env['JELLYFIN2_KEY'];root='http://127.0.0.1:8097'
    def get(path,method='GET',data=None):
        body=json.dumps(data).encode() if data is not None else None
        req=urllib.request.Request(root+path,method=method,data=body,headers={'Content-Type':'application/json'})
        req.add_unredirected_header('Authorization','MediaBrowser Token="'+token+'"')
        with urllib.request.urlopen(req,timeout=60) as response:return json.load(response)
    users=get('/Users')
    if not users:raise RuntimeError('no local users')
    with closing(sqlite3.connect((base/'jellyfin2/config/data/jellyfin.db').as_uri()+'?mode=ro',uri=True,timeout=2)) as con:
        items=[];seen=set();live_urls={}
        for iid,path in con.execute("SELECT Id,Path FROM BaseItems WHERE Path LIKE '/cinema2/%strm'"):
            if args.item_id and uuid.UUID(iid)!=uuid.UUID(args.item_id):continue
            host=base/path.lstrip('/')
            if not host.is_file():continue
            value=host.read_text().strip()
            if '/pending' in value or not value.startswith('http'):continue
            group=path.split('/')[2:4]
            key=tuple(group)
            if key in seen:continue
            seen.add(key);items.append(iid);live_urls[iid]=value
            if len(items)>=args.samples:break
    if not items:raise RuntimeError('no existing live library items')
    events=[]
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    opener=urllib.request.build_opener() if args.relay else urllib.request.build_opener(NoRedirect)
    def target_url(item,route):
        if route=='jellyfin':return root+'/Videos/'+str(item)+'/stream?Static=true'
        live=live_urls[item]
        if route=='play':return live
        query=urllib.parse.parse_qs(urllib.parse.urlparse(live).query)
        remote=query.get('r',[''])[0]
        if not remote.startswith('/夸克网盘/'):raise RuntimeError('unsupported_direct_mapping')
        return 'http://127.0.0.1:5244/d'+urllib.parse.quote('/夸克/'+remote[len('/夸克网盘/'):])
    class Proxy(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def do_HEAD(self):self.serve(True)
        def do_GET(self):self.serve(False)
        def serve(self,head):
            started=time.monotonic();sent=False
            event={'request':self.path,'range':self.headers.get('Range',''),'method':'HEAD' if head else 'GET','bytes':0}
            try:
                parts=self.path.strip('/').split('/');index=int(parts[0]);route=parts[1]
                item=items[index]
                headers={'Accept-Encoding':'identity'}
                if self.headers.get('Range'):headers['Range']=self.headers['Range']
                req=urllib.request.Request(target_url(item,route),headers=headers,method='HEAD' if head else 'GET')
                if route=='jellyfin':req.add_unredirected_header('Authorization','MediaBrowser Token="'+token+'"')
                try:
                    response=opener.open(req,timeout=30)
                except urllib.error.HTTPError as exc:
                    if exc.code not in (301,302,303,307,308):raise
                    response=exc
                with response:
                    event.update(status=response.status,content_range=response.headers.get('Content-Range'),content_length=response.headers.get('Content-Length'),header_seconds=round(time.monotonic()-started,3))
                    self.send_response(response.status)
                    if response.status in (301,302,303,307,308):
                        location=urllib.parse.urljoin(req.full_url,response.headers['Location'])
                        if urllib.parse.urlparse(location).scheme not in ('http','https'):raise ValueError('invalid_redirect')
                        self.send_header('Location',location)
                        self.send_header('Content-Length','0')
                        self.send_header('Connection','close');self.end_headers();sent=True
                        return
                    for k in ('Content-Type','Content-Length','Content-Range','Accept-Ranges'):
                        if response.headers.get(k):self.send_header(k,response.headers[k])
                    self.send_header('Connection','close');self.end_headers();sent=True
                    if not head:
                        while True:
                            chunk=response.read1(128*1024)
                            if not chunk:break
                            self.wfile.write(chunk)
                            event['bytes']+=len(chunk)
            except (BrokenPipeError,ConnectionResetError):pass
            except Exception as exc:
                event['error_type']=type(exc).__name__
                try:
                    if not sent:self.send_error(502)
                except Exception:pass
            finally:
                self.close_connection=True
                event['seconds']=round(time.monotonic()-started,3);events.append(event)
    http=ThreadingHTTPServer(('127.0.0.1',0),Proxy)
    thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    name='cinema-jellyfin-play-'+uuid.uuid4().hex[:10]
    report={'time':datetime.datetime.now().astimezone().isoformat(),'samples':[],'native':args.native,'relay':args.relay,'full_decode':args.full,'decode_timeout':args.decode_timeout,'requests':events}
    def save_report():pathlib.Path(args.output).write_text(json.dumps(report,indent=2)+'\n')
    def safe_error(value):
        if isinstance(value,bytes):value=value.decode(errors='replace')
        value=re.sub(r'https?://[^\s]+','[media-url]',value or '')
        value=re.sub(r'Authorization[^\r\n]*','[redacted]',value,flags=re.I)
        return value[-2400:]
    def run(command,timeout=180):
        result=subprocess.run(command,capture_output=True,text=True,timeout=timeout)
        if result.returncode:raise RuntimeError('media_command_failed')
        return result.stdout
    try:
        image=run(['docker','inspect','-f','{{.Image}}','jellyfin2']).strip()
        run(['docker','run','-d','--name',name,'--network','host','--read-only','--memory','512m','--cpus','2',
             '--cap-drop','ALL','--security-opt','no-new-privileges','--tmpfs','/tmp:rw,size=32m','--tmpfs','/cache:rw,size=16m',
             '-v',str(base/'custom-ffmpeg')+':/custom-ffmpeg:ro','--entrypoint','/bin/sh',image,'-c','sleep 1200'])
        for index,item in enumerate(items):
            sample={'number':index+1,'item_id':uuid.UUID(item).hex,'checks':{}};report['samples'].append(sample)
            try:
                info=get('/Items/'+str(item)+'/PlaybackInfo?'+urllib.parse.urlencode({'UserId':users[0]['Id']}),'POST',{})
                sources=info.get('MediaSources') or []
                sample['checks']['playback_info']={'pass':bool(sources) and not info.get('ErrorCode'),'sources':len(sources)}
                sample['media_source_routes']=[('play' if '/play?' in s.get('Path','') else 'pending' if '/pending?' in s.get('Path','') else 'other') for s in sources]
                sample['media_streams']=[{k:stream.get(k) for k in ('Type','Codec','Width','Height')}
                                         for source in sources for stream in source.get('MediaStreams',[])]
                expected=max((float(s.get('RunTimeTicks') or 0)/10000000 for s in sources),default=0) if args.full else args.duration
                if expected<=0:raise RuntimeError('unconfirmed_media_duration')
                sample['expected_decode_seconds']=expected
                for route in routes:
                    for position in positions:
                        url=f'http://127.0.0.1:{http.server_port}/{index}/{route}/{position}'
                        started=time.monotonic();check={'pass':False};sample['checks'][route+'_decode_'+str(position)]=check
                        try:
                            if args.resolve:
                                with urllib.request.urlopen(urllib.request.Request(url,headers={'Range':'bytes=0-0'}),timeout=60) as response:
                                    response.read(1);url=response.geturl()
                            executable='/usr/lib/jellyfin-ffmpeg/ffmpeg' if args.native else '/custom-ffmpeg/ffmpeg'
                            input_options=['-interleaved_read',args.interleaved_read] if args.interleaved_read else []
                            if args.keepalive:input_options+=['-multiple_requests','1']
                            duration_options=[] if args.full else ['-t',str(args.duration)]
                            result=subprocess.run(['docker','exec',name,'timeout','-k','5',str(args.decode_timeout),executable,'-v','warning','-threads','2',*input_options,'-ss',str(position),'-i',url,
                                        *duration_options,'-map','0:v:0','-map','0:a:0?','-threads','2','-progress','pipe:1','-f','null','-'],capture_output=True,text=True,timeout=args.decode_timeout+15)
                            frames=[int(l.split('=',1)[1]) for l in result.stdout.splitlines() if l.startswith('frame=')]
                            check.update(assess(result.stdout,result.stderr,result.returncode,expected),stderr=safe_error(result.stderr))
                        except Exception as exc:check.update(error_type=type(exc).__name__)
                        check['seconds']=round(time.monotonic()-started,2);save_report()
                        print(json.dumps({'sample':index+1,'route':route,'position':position,**check}),flush=True)
            except Exception as exc:sample['error_type']=type(exc).__name__
        report['all_passed']=len(items)==(1 if args.item_id else args.samples) and all(len(s['checks'])==1+len(routes)*len(positions) and all(c['pass'] for c in s['checks'].values()) for s in report['samples'])
    finally:
        subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=20)
        http.shutdown();http.server_close()
    save_report();print(json.dumps({k:v for k,v in report.items() if k!='requests'},indent=2))
    if not report['all_passed']:raise SystemExit(1)


if __name__=='__main__':main()
