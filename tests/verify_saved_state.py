#!/usr/bin/env python3
"""Read-only authoritative leaf audit of saved records. No production module state writes."""
import argparse
from contextlib import closing
import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile


sys.path.insert(0, str(Path(__file__).resolve().parent))
from homelab_runtime import load_runtime, isolate_state

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output',required=True)
    args=ap.parse_args()
    base=Path('/home/dong/projects/hometheater')
    report={'time':datetime.datetime.now().astimezone().isoformat(),'production_state_written':False}
    with tempfile.TemporaryDirectory(prefix='cinema-saved-audit-') as tmp:
        load_runtime()
        os.environ.update(VOD_BASE_DIR=str(base),VOD_DATA_DIR=tmp,AI_PARSE_ENABLED='0')
        isolate_state(tmp, data_dir=tmp, isolate_library=True)
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
        from quark_vod import selfheal
        from quark_vod.transfer import QuarkTransfer
        with closing(sqlite3.connect((base/'data/quark_plays.db').as_uri()+'?mode=ro',uri=True,timeout=2)) as con:
            con.row_factory=sqlite3.Row
            all_rows=[dict(r) for r in con.execute("SELECT id,remote_path FROM transfers WHERE status='done'")]
            rows=[r for r in all_rows if r['remote_path']]
            report['done_without_path_ids']=[r['id'] for r in all_rows if not r['remote_path']]
        tr=QuarkTransfer(None);calls=0;errors=[]
        original=tr._request
        def request(*args,**kwargs):
            nonlocal calls
            calls+=1
            try:return original(*args,**kwargs)
            except Exception as exc:
                errors.append(type(exc).__name__);raise
        tr._request=request
        result=selfheal._remote_exists_batch(tr,[r['remote_path'] for r in rows])
        report.update(records=len(rows),unique_paths=len(result),api_calls=calls,
                      missing_transfer_ids=[r['id'] for r in rows if result.get(r['remote_path']) is False],
                      present_or_unknown=sum(result.values()),errors=errors,
                      all_checks_conclusive=not errors)
    Path(args.output).write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))


if __name__=='__main__':main()
