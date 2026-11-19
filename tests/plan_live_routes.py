#!/usr/bin/env python3
"""Prepare a local-only exact migration plan using a snapshot of production state."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import urllib.parse


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output',required=True)
    args=ap.parse_args()
    base=Path('/home/dong/projects/hometheater')
    with tempfile.TemporaryDirectory(prefix='cinema-route-plan-') as tmp:
        os.environ.update(VOD_BASE_DIR=str(base),VOD_DATA_DIR=tmp,AI_PARSE_ENABLED='0')
        with closing(sqlite3.connect((base/'data/quark_plays.db').as_uri()+'?mode=ro',uri=True,timeout=2)) as source,closing(sqlite3.connect(Path(tmp)/'quark_plays.db')) as target:
            source.backup(target)
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
        from quark_vod import db,config,strm_manager
        plan=[];skipped=[]
        for row in db._conn().execute('SELECT DISTINCT strm_path FROM strm_map'):
            path=Path(row[0])
            if not path.is_file():continue
            old=path.read_text().strip()
            if not strm_manager.is_live(old) or '/play?' in old:continue
            remote=strm_manager.parse_live_remote_path(old)
            rel=path.relative_to(config.CINEMA2_DIR).parts
            identities=db._conn().execute('SELECT season,episode FROM strm_map WHERE strm_path=?',(str(path),)).fetchall()
            coordinates={(r['season'],r['episode']) for r in identities}
            if not remote or len(coordinates)!=1:
                skipped.append({'path':str(path),'reason':'ambiguous coordinates'});continue
            season,episode=next(iter(coordinates))
            done=db.find_done_transfer(rel[0],rel[1],season,episode)
            if not done or config.write_path(done['remote_path'])!=remote:
                skipped.append({'path':str(path),'reason':'state mismatch'});continue
            new=config.PENDING_BASE_URL+'/play?'+urllib.parse.urlencode({'cat':rel[0],'t':rel[1],'s':season,'e':episode,'r':remote})
            plan.append({'path':str(path),'before_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'after':new,'transfer_id':done['id']})
        data={'changes':plan,'skipped':skipped,'production_written':False}
        Path(args.output).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'changes':len(plan),'skipped':len(skipped),'production_written':False}))


if __name__=='__main__':main()
