#!/usr/bin/env python3
"""Bounded public search/detail reads, no save/preheat/model/production database."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', required=True)
    ap.add_argument('--node', help='verify one configured node key')
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix='cinema-search-') as tmp:
        os.environ.update(VOD_BASE_DIR=tmp, VOD_DATA_DIR=tmp+'/data', AI_PARSE_ENABLED='0')
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'app'))
        from quark_vod import panshare_matrix as matrix, db
        def check(node):
            start = time.monotonic()
            result = {'node':node['key']}
            base = node['base'].rstrip('/')
            try:
                url = base+'/index.php/vod/search.html?wd='+urllib.parse.quote('凡人修仙传')
                with urllib.request.urlopen(urllib.request.Request(url, headers=matrix.UA), timeout=8) as response:
                    html = response.read(2*1024*1024).decode(errors='replace')
                    result['search_http'] = response.status
                details = list(dict.fromkeys(re.findall(r'href="(/index\.php/vod/detail/id/\d+\.html|/voddetail/\d+\.html)"', html)))
                result['detail_links'] = len(details)
                if details:
                    with urllib.request.urlopen(urllib.request.Request(base+details[0], headers=matrix.UA), timeout=8) as response:
                        html = response.read(2*1024*1024).decode(errors='replace')
                        result['detail_http'] = response.status
                    rows = matrix.parse_download_rows(html)
                    links = [r['url'] for r in rows] or matrix.extract_quark_links_and_pwd(html)
                    result['quark_links'] = sum('quark' in url for url in links)
                result['pass'] = bool(details) and result.get('quark_links',0)>0
            except urllib.error.HTTPError as exc:
                result.update(http=exc.code, error_type=type(exc).__name__, **{'pass':False})
            except Exception as exc:
                result.update(error_type=type(exc).__name__, **{'pass':False})
            result['seconds'] = round(time.monotonic()-start,3)
            return result
        nodes = [node for node in matrix.ACTIVE_PAN_NODES if not args.node or node['key']==args.node]
        if not nodes:
            raise SystemExit('no configured node matched')
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(check, nodes))
        if hasattr(db._local,'conn'):
            db._local.conn.close()
            del db._local.conn
    report = {'time':datetime.datetime.now().astimezone().isoformat(), 'samples':results, 'all_passed':all(r['pass'] for r in results)}
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if not report['all_passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
