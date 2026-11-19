#!/usr/bin/env python3
"""Isolated real Quark save -> FastAPI -> wrapped FFprobe/FFmpeg decode and seek.

Creates one unique cloud test directory and removes it from the normal tree.
Uses a temporary SQLite/media tree and loopback port; never writes production
library records. Deletion can retain a recycle-bin copy, reported explicitly.
"""
import argparse
from contextlib import closing, redirect_stdout, redirect_stderr
import datetime
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
from media_result import assess


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    sources=ap.add_mutually_exclusive_group(required=True)
    sources.add_argument('--transfer-id',type=int)
    sources.add_argument('--source-json',help='private inspected source with cat/title/season/episode/share_url')
    ap.add_argument('--max-bytes',type=int,default=6*1024**3)
    ap.add_argument('--output',required=True)
    ap.add_argument('--execute',action='store_true')
    args=ap.parse_args()
    if not args.execute:raise SystemExit('cloud write requires --execute')
    base=Path('/home/dong/projects/hometheater')
    if args.source_json:
        row=json.loads(Path(args.source_json).read_text())
        if not all(k in row for k in ('cat','title','season','episode','share_url')):
            raise SystemExit('invalid_source_fixture')
        if not row['share_url'].startswith('https://pan.quark.cn/s/'):
            raise SystemExit('not_quark_share')
    else:
        with closing(sqlite3.connect((base/'data/quark_plays.db').as_uri()+'?mode=ro',uri=True,timeout=2)) as con:
            con.row_factory=sqlite3.Row
            row=dict(con.execute("SELECT * FROM transfers WHERE id=? AND status='done'",(args.transfer_id,)).fetchone())
    report={'time':datetime.datetime.now().astimezone().isoformat(),'source_transfer_id':args.transfer_id,'checks':{},
            'production_library_written':False,'recycle_cleanup_requested':False}
    root='/夸克网盘/点播/vod_verify_'+uuid.uuid4().hex
    report['temporary_root']=root
    created=False;container=None;httpserver=None;thread=None
    with tempfile.TemporaryDirectory(prefix='cinema-roundtrip-') as tmp:
        os.chmod(tmp,0o755)
        os.environ.update(VOD_BASE_DIR=str(base),VOD_DATA_DIR=tmp+'/data',VOD_CINEMA2_DIR=tmp+'/cinema2',AI_PARSE_ENABLED='0')
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
        from quark_vod import config,db,strm_manager,server
        from quark_vod.transfer import QuarkTransfer
        from quark_vod.openlist_client import OpenListClient
        from quark_vod.identity import Work
        import main as api
        import uvicorn
        config.QUARK_VOD_ROOT=root;config.QUARK_PLAY_ROOT=config.play_path(root)
        config.SELFHEAL_ENABLED=False
        ol=OpenListClient();tr=QuarkTransfer(ol)
        def command(command,timeout=180):
            r=subprocess.run(command,capture_output=True,text=True,timeout=timeout)
            if r.returncode:
                raise RuntimeError('command_failed_'+str(r.returncode))
            return r.stdout
        def media(url,seek=0):
            executable='/custom-ffmpeg/ffprobe' if seek is None else '/custom-ffmpeg/ffmpeg'
            cmd=['docker','exec',container,executable,'-v','error']
            if seek is None:
                data=json.loads(command(cmd+['-show_streams','-show_format','-print_format','json','-i',url],300))
                streams=data.get('streams') or []
                codecs=[{'type':s.get('codec_type'),'codec':s.get('codec_name'),'width':s.get('width'),'height':s.get('height')} for s in streams if s.get('codec_type') in ('video','audio')]
                if not any(s['type']=='video' for s in codecs):raise RuntimeError('no_video_stream')
                return {'streams':codecs,'duration':data.get('format',{}).get('duration'),'pass':True}
            result=subprocess.run(cmd+['-threads','2','-ss',str(seek),'-i',url,'-t','12','-map','0:v:0','-map','0:a:0?',
                                '-threads','2','-progress','pipe:1','-f','null','-'],capture_output=True,text=True,timeout=180)
            verdict=assess(result.stdout,result.stderr,result.returncode)
            if not verdict['pass']:raise RuntimeError('incomplete_decode')
            return {**verdict,'seconds_requested':12,'seek_seconds':seek}
        try:
            token,pid,items=tr.resolve_share(row['share_url'])
            files=tr.share_files(token,pid,items)
            best=tr.select_share_video(files,row['title'],row['season'],row['episode'])
            if not best or best['size']>args.max_bytes:raise RuntimeError('selected_file_over_limit')
            capacity=tr._request('https://drive.quark.cn/1/clouddrive/member?pr=ucpro&fr=pc').get('data') or {}
            if int(capacity.get('total_capacity') or 0)-int(capacity.get('use_capacity') or 0)<best['size']+1024**3:
                raise RuntimeError('insufficient_space')
            report['selected_bytes']=best['size']
            if tr._get_quark_fid(root):raise RuntimeError('unique_root_exists')
            created=True;ol.mkdir(root)
            sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128)
            port=sock.getsockname()[1]
            config.PENDING_BASE_URL=f'http://127.0.0.1:{port}'
            strm_manager.PENDING_PREFIX=config.PENDING_BASE_URL+'/pending'
            work=Work(row['cat'],row['title'],row['season'],row['episode'])
            path=Path(config.CINEMA2_DIR)/work.cat/work.title/'fixture.strm'
            path.parent.mkdir(parents=True)
            pending=config.PENDING_BASE_URL+'/pending?'+urllib.parse.urlencode({'cat':work.cat,'t':work.title,'s':work.season,'e':work.episode})
            path.write_text(pending+'\n');db.save_strm_map(*work.args,str(path))
            db.save_source_audit(*work.args,True,False,row['share_url'],work.title,'valid_hd')
            server._ol=ol;server._transfer=tr
            httpserver=uvicorn.Server(uvicorn.Config(api.app,host='127.0.0.1',port=port,lifespan='off',access_log=False,log_level='critical'))
            thread=threading.Thread(target=httpserver.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
            for _ in range(100):
                if httpserver.started:break
                time.sleep(.05)
            image=command(['docker','inspect','-f','{{.Image}}','jellyfin2']).strip()
            container='cinema-roundtrip-'+uuid.uuid4().hex[:10]
            command(['docker','run','-d','--name',container,'--network','host','--read-only','--memory','512m','--cpus','2',
                     '--cap-drop','ALL','--security-opt','no-new-privileges','--tmpfs','/tmp:rw,size=32m','--tmpfs','/cache:rw,size=16m',
                     '-v',str(base/'custom-ffmpeg')+':/custom-ffmpeg:ro','--entrypoint','/bin/sh',image,'-c','sleep 1800'])
            # Suppress private paths and URLs; report only summaries and error types.
            with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()),patch.object(strm_manager,'trigger_jellyfin_refresh_item'):
                begin=time.monotonic()
                result=server.resolve_playback(work)
                report['checks']['initial_save']={'pass':True,'seconds':round(time.monotonic()-begin,2)}
                live=path.read_text().strip()
                report['checks']['stable_route']={'pass':'/play?' in live and strm_manager.parse_live_remote_path(live).startswith(root+'/')}
                report['checks']['probe_pending']=media(pending,None)
                report['checks']['decode_start']=media(live,0)
                report['checks']['decode_seek_120']=media(live,120)
                report['checks']['decode_seek_600']=media(live,600)
                # Verify repeat playback preserves the exact saved file and DB row.
                again=server.resolve_playback(work)
                report['checks']['repeat_same_file']={'pass':again.remote_path==result.remote_path and again.transfer_id==result.transfer_id}
        except Exception as exc:
            report['error_type']=type(exc).__name__
            # Known local assertions are safe; never record upstream bodies/URLs.
            if isinstance(exc,RuntimeError) and str(exc).replace('_','').isalnum():report['stage_error']=str(exc)
        finally:
            if httpserver:httpserver.should_exit=True
            if thread:thread.join(10)
            if container:subprocess.run(['docker','rm','-f',container],capture_output=True,timeout=20)
            if created:
                try:
                    ol.remove(root);report['normal_directory_removed']=tr._get_quark_fid(root) is None
                except Exception as exc:report['cleanup_error_type']=type(exc).__name__
            report['all_passed']=len(report['checks'])==7 and all(c['pass'] for c in report['checks'].values()) and report.get('normal_directory_removed',False)
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if not report['all_passed']:raise SystemExit(1)


if __name__=='__main__':main()
