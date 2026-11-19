#!/usr/bin/env python3
"""Convert existing readable live STRMs to /play without saving new media.

Requires a healthy deployed /play route. Backs up every changed STRM separately.
No missing remote or unmatched identity is automatically activated.
"""
import argparse
from contextlib import redirect_stdout
import datetime
import io
import json
from pathlib import Path
import sys
import urllib.request


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--execute',action='store_true')
    ap.add_argument('--output',required=True)
    args=ap.parse_args()
    if not args.execute:raise SystemExit('--execute required')
    with urllib.request.urlopen('http://127.0.0.1:8099/openapi.json',timeout=5) as response:
        if '/play' not in json.load(response).get('paths',{}):raise RuntimeError('new endpoint not deployed')
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
    from quark_vod import config,db,selfheal,server,strm_manager
    from quark_vod.coordination import maintenance_window
    from quark_vod.identity import Work
    backup=Path('/home/dong/agent-doc-backups')/('cinema-route-migration-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(mode=0o700)
    report={'time':datetime.datetime.now().astimezone().isoformat(),'backup':str(backup),'changed':0,'skipped':[]}
    with maintenance_window(),redirect_stdout(io.StringIO()):
        lives=selfheal._iter_live_strms()
        for path,remote in lives:
            p=Path(path);content=p.read_text().strip()
            if '/play?' in content:continue
            params=strm_manager.parse_pending_params(selfheal._pending_from_map(path))
            if not params:
                report['skipped'].append('no_identity');continue
            work=Work(params['cat'],params['title'],params['season'],params['episode'])
            row=db.find_done_transfer(*work.args)
            if not row or config.write_path(row['remote_path'])!=config.write_path(remote):
                report['skipped'].append('state_mismatch');continue
            if not server._remote_still_exists(remote) or not server._wait_direct_url_ready(server._direct_url(remote),remote_path=remote):
                report['skipped'].append('unreadable');continue
            target=backup/p.relative_to(config.BASE_DIR);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(p.read_bytes())
            strm_manager.activate(*work.args,server._direct_url(remote))
            report['changed']+=1
    report['all_migrated']=not report['skipped']
    Path(args.output).write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
    if not report['all_migrated']:raise SystemExit(1)


if __name__=='__main__':main()
