#!/usr/bin/env python3
"""Decode an existing saved MP4 through isolated ASGI Range caching. No cloud writes."""
import argparse
from contextlib import closing
import datetime
import json
import os
from pathlib import Path
import re
import resource
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parent))
from media_result import assess


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--transfer-id',type=int,required=True)
    ap.add_argument('--output',required=True)
    args=ap.parse_args()
    base=Path('/home/dong/projects/hometheater')
    with closing(sqlite3.connect((base/'data/quark_plays.db').as_uri()+'?mode=ro',uri=True,timeout=2)) as con:
        con.row_factory=sqlite3.Row
        row=dict(con.execute("SELECT * FROM transfers WHERE id=? AND status='done'",(args.transfer_id,)).fetchone())
    report={'time':datetime.datetime.now().astimezone().isoformat(),'source_transfer_id':args.transfer_id,'checks':[],'production_state_written':False}
    container='cinema-cache-'+uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix='cinema-cache-') as tmp:
        os.environ.update(VOD_BASE_DIR=str(base),VOD_DATA_DIR=tmp,AI_PARSE_ENABLED='0')
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
        from quark_vod import config,db,media_cache
        import main as api
        import uvicorn
        tid=db.upsert_transfer(row['cat'],row['title'],row['season'],row['episode'],status='done',remote_path=row['remote_path'],file_size=row['file_size'])
        sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128)
        config.PENDING_BASE_URL=f'http://127.0.0.1:{sock.getsockname()[1]}'
        server=uvicorn.Server(uvicorn.Config(api.app,lifespan='off',access_log=False,log_level='critical'))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
        def command(cmd,timeout=120):return subprocess.run(cmd,capture_output=True,text=True,timeout=timeout)
        try:
            for _ in range(100):
                if server.started:break
                time.sleep(.05)
            image=command(['docker','inspect','-f','{{.Image}}','jellyfin2']).stdout.strip()
            created=command(['docker','run','-d','--name',container,'--network','host','--read-only','--memory','512m','--cpus','2','--cap-drop','ALL','--security-opt','no-new-privileges',
                '--tmpfs','/tmp:rw,size=32m','--tmpfs','/cache:rw,size=16m','-v',str(base/'custom-ffmpeg')+':/custom-ffmpeg:ro','--entrypoint','/bin/sh',image,'-c','sleep 600'])
            if created.returncode:raise RuntimeError('container_failed')
            url=media_cache.playback_url(db.get_transfer_by_id(tid))
            for position in (0,120,600):
                begin=time.monotonic()
                r=command(['docker','exec',container,'timeout','-k','5','90','/custom-ffmpeg/ffmpeg','-v','warning','-threads','2','-ss',str(position),'-i',url,
                    '-t','12','-map','0:v:0','-map','0:a:0?','-threads','2','-progress','pipe:1','-f','null','-'],105)
                frames=[int(l.split('=',1)[1]) for l in r.stdout.splitlines() if l.startswith('frame=')]
                check={'seek':position,'seconds':round(time.monotonic()-begin,2),'frames':max(frames) if frames else 0,'exit_code':r.returncode,
                       'stderr':re.sub(r'https?://[^\s]+','[media-url]',r.stderr)[-1600:],**assess(r.stdout,r.stderr,r.returncode)}
                report['checks'].append(check);print(json.dumps(check),flush=True)
            report['cache_bytes']=media_cache.cache._bytes
            report['asgi_peak_rss_kib']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            report['decoder_memory_peak']=command(['docker','exec',container,'cat','/sys/fs/cgroup/memory.peak']).stdout.strip()
            report['all_passed']=all(c['pass'] for c in report['checks'])
        finally:
            command(['docker','rm','-f',container],30)
            server.should_exit=True;thread.join(10);sock.close()
    Path(args.output).write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
    if not report['all_passed']:raise SystemExit(1)


if __name__=='__main__':main()
