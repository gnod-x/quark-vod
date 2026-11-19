#!/usr/bin/env python3
"""Read small ranges from existing live STRM only; never call pending/preheat."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import signal
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing

BASE=Path('/home/dong/projects/hometheater')


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output',required=True)
    args=ap.parse_args()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def deadline(*args):raise TimeoutError('bounded media request expired')
    signal.signal(signal.SIGALRM,deadline)
    paths=[]
    for p in (BASE/'cinema').glob('*/*/*.strm'):
        paths.append(('traditional',p))
        if len(paths)==2:break
    with closing(sqlite3.connect((BASE/'data/quark_plays.db').as_uri()+'?mode=ro',uri=True,timeout=2)) as con:
        candidates=con.execute('SELECT DISTINCT strm_path FROM strm_map').fetchall()
    vod=[]
    for (raw,) in candidates:
        p=Path(raw)
        if p.is_relative_to(BASE/'cinema2') and p.is_file():
            url=p.read_text().strip()
            if '/pending' not in url and ('5244/d/' in url or '8099/clean?' in url):vod.append(('vod',p))
    paths+=vod[:3]
    results=[]
    for kind,p in paths:
        url=p.read_text().strip()
        if not url.startswith(('http://','https://')) or '/pending' in url:continue
        result={'kind':kind,'path_id':hashlib.sha256(str(p).encode()).hexdigest()[:16]}
        began=datetime.datetime.now().timestamp()
        try:
            signal.setitimer(signal.ITIMER_REAL,18)
            req=urllib.request.Request(url,headers={'Range':'bytes=0-65535','Accept-Encoding':'identity','User-Agent':'cinema-verification'})
            with opener.open(req,timeout=12) as r:
                data=r.read(65536)
                result.update(http=r.status,bytes=len(data),content_type=r.headers.get('Content-Type'),range_supported=r.status==206,
                              media_signature=('png' if data.startswith(b'\x89PNG') else 'mkv' if data.startswith(b'\x1aE\xdf\xa3') else 'mp4' if b'ftyp' in data[:16] else 'other'))
                result['pass']=r.status==206 and len(data)==65536 and result['media_signature'] in ('mkv','mp4')
        except urllib.error.HTTPError as exc:result.update({'http':exc.code,'pass':False})
        except Exception as exc:result.update({'error_type':type(exc).__name__,'pass':False})
        finally:signal.setitimer(signal.ITIMER_REAL,0)
        result['seconds']=round(datetime.datetime.now().timestamp()-began,3)
        results.append(result)
        print(json.dumps(result))
    passed=len(results)==5 and all(r['pass'] for r in results)
    Path(args.output).write_text(json.dumps({'time':datetime.datetime.now().astimezone().isoformat(),'vod_live_files':len(vod),'samples':results,'all_passed':passed},indent=2)+'\n')
    if not passed:
        raise SystemExit(1)


if __name__=='__main__':main()
