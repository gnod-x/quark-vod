"""Compare bounded search against the old first-10-links algorithm on identical pages.
Read public indexes only; isolated DB/cache; no save/preheat. No URLs in report.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import urllib.parse
from unittest.mock import patch


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output',required=True)
    ap.add_argument('--keyword',action='append')
    ap.add_argument('--private-candidates')
    args=ap.parse_args()
    reports=[];private=[]
    with tempfile.TemporaryDirectory(prefix='cinema-search-coverage-') as tmp:
        os.environ.update(VOD_BASE_DIR=tmp,VOD_DATA_DIR=tmp+'/data',AI_PARSE_ENABLED='0')
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
        from quark_vod import panshare_matrix as matrix, search
        from quark_vod.quality import quality_hint
        original=matrix._fetch_html
        for keyword in args.keyword or ['沙丘','凡人修仙传','庆余年']:
            pages={};lock=threading.Lock()
            def capture(url,timeout):
                text=original(url,timeout)
                with lock:pages[url]=text
                return text
            with patch.object(matrix,'_fetch_html',side_effect=capture):
                report=matrix.PanshareMatrix.search_report(keyword,use_cache=False)
            before=[];baseline_details=0
            for node in matrix.ACTIVE_PAN_NODES:
                base=node['base'].rstrip('/')
                url=base+'/index.php/vod/search.html?wd='+urllib.parse.quote(keyword)
                text=pages.get(url,'')
                links=re.findall(r'href="(/index\.php/vod/detail/id/\d+\.html|/voddetail/\d+\.html)"',text)
                selected=list(dict.fromkeys(links[:10]));baseline_details+=len(selected)
                for path in selected:
                    detail=pages.get(base+path,'')
                    rows=matrix.parse_download_rows(detail)
                    if not rows:
                        title=re.search('<title>([^<]+)-',detail)
                        name=title.group(1).strip() if title else keyword
                        rows=[{'title':name,'url':u} for u in matrix.extract_quark_links_and_pwd(detail)]
                    before.extend({'source':node['key'],'name':r['title'],'share_url':r['url']} for r in rows)
            before=matrix.merge_items(before)
            after=report['items']
            before_urls={r['share_url'].split('#')[0] for r in before}
            def counts(items):
                season=0 if keyword=='沙丘' else 1
                candidates=[search.SearchResult('panshare',r['source'],r['name'],'','',[],share_url=r['share_url'],names=r.get('names')) for r in items]
                qualified=search.rank_candidates(candidates,keyword,season,1 if season else 0)
                return {'unique_shares':len(items),'qualified':len(qualified),'four_k_hints':sum(quality_hint(c.name)['level']=='4k' for c in qualified)}
            summary={'keyword':keyword,'before':counts(before),'after':counts(after),
                'before_details':baseline_details,'after_details':sum(n['details'] for n in report['nodes']),
                'additional_shares':sum(r['share_url'].split('#')[0] not in before_urls for r in after),
                'nodes':report['nodes'],'partial':report['partial']}
            reports.append(summary);private.append({'keyword':keyword,'items':after})
            print(json.dumps(summary,ensure_ascii=False),flush=True)
    output={'time':datetime.datetime.now().astimezone().isoformat(),'comparison':'same captured pages, old first-10-links vs bounded pagination','samples':reports}
    Path(args.output).write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
    if args.private_candidates:
        path=Path(args.private_candidates);path.touch(mode=0o600,exist_ok=True)
        path.write_text(json.dumps(private,ensure_ascii=False));path.chmod(0o600)


if __name__=='__main__':main()
