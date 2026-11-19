#!/usr/bin/env python3
"""Read one existing live item per Jellyfin via authenticated static stream API."""
import argparse
from contextlib import closing
import datetime
import json
from pathlib import Path
import signal
import sqlite3
import urllib.error
import urllib.request


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output',required=True)
    args=ap.parse_args()
    base=Path('/home/dong/projects/hometheater')
    env={}
    for line in (base/'.env').read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k,v=line.split('=',1)
            env[k.strip()]=v.strip().strip(chr(34)).strip(chr(39))
    def expired(*args):
        raise TimeoutError('stream deadline')
    signal.signal(signal.SIGALRM,expired)
    results=[]
    for name,port,folder,key in [('jellyfin',8096,'cinema','JELLYFIN_KEY'),('jellyfin2',8097,'cinema2','JELLYFIN2_KEY')]:
        result={'service':name}
        item=None
        with closing(sqlite3.connect((base/name/'config/data/jellyfin.db').as_uri()+'?mode=ro',uri=True,timeout=2)) as con:
            for item_id,raw in con.execute("SELECT Id,Path FROM BaseItems WHERE Path LIKE ?",('/'+folder+'/%strm',)):
                path=base/raw.lstrip('/')
                if path.is_file():
                    value=path.read_text().strip()
                    if '/pending' not in value and ('5244/d/' in value or '8099/clean?' in value or '8099/play?' in value):
                        item=item_id
                        break
        try:
            assert item,'no existing live item'
            signal.setitimer(signal.ITIMER_REAL,25)
            req=urllib.request.Request('http://127.0.0.1:'+str(port)+'/Videos/'+str(item)+'/stream?Static=true',headers={'Range':'bytes=0-65535','Accept-Encoding':'identity'})
            # Credentials must not follow a redirect to OpenList or the CDN.
            req.add_unredirected_header('Authorization','MediaBrowser Token="'+env[key]+'"')
            with urllib.request.urlopen(req,timeout=20) as response:
                data=response.read(65536)
                signature='mkv' if data.startswith(b'\x1aE\xdf\xa3') else 'mp4' if b'ftyp' in data[:16] else 'other'
                result.update(http=response.status,bytes=len(data),media_signature=signature)
                result['pass']=response.status==206 and len(data)==65536 and signature in ('mkv','mp4')
        except urllib.error.HTTPError as exc:
            result.update(http=exc.code,**{'pass':False})
        except Exception as exc:
            result.update(error_type=type(exc).__name__,**{'pass':False})
        finally:
            signal.setitimer(signal.ITIMER_REAL,0)
        results.append(result)
    report={'time':datetime.datetime.now().astimezone().isoformat(),'samples':results,'all_passed':all(r['pass'] for r in results)}
    Path(args.output).write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
    if not report['all_passed']:raise SystemExit(1)


if __name__=='__main__':main()
