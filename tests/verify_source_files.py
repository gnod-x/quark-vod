"""Read a few real share directories; no save, preheat or production DB writes."""
import argparse,datetime,json,os,sys,tempfile,time
from pathlib import Path

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--candidates',required=True);ap.add_argument('--output',required=True);args=ap.parse_args()
    raw=json.loads(Path(args.candidates).read_text());checks=[]
    with tempfile.TemporaryDirectory(prefix='cinema-source-files-') as tmp:
        os.environ.update(VOD_BASE_DIR='/home/dong/projects/hometheater',VOD_DATA_DIR=tmp+'/data',AI_PARSE_ENABLED='0')
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
        from quark_vod import search
        from quark_vod.transfer import QuarkTransfer
        from quark_vod.quality import quality_hint
        tr=QuarkTransfer(None)
        for group in raw:
            keyword=group['keyword'];season=0 if keyword=='沙丘' else 1;episode=1 if season else 0
            cat='国产动漫' if '凡人' in keyword else ('国产剧集' if season else '欧美大片')
            title=keyword+' (2021)' if not season else keyword
            results=[search.SearchResult('panshare',r['source'],r['name'],'','',[],share_url=r['share_url'],names=r.get('names')) for r in group['items']]
            for c in search.rank_candidates(results,title,season,episode,cat)[:3]:
                start=time.monotonic();check={'query':keyword,'share_title':c.name,'season':season,'episode':episode}
                try:
                    video=tr.inspect_share_video(c.share_url,title,season,episode,timeout=45)
                    if video:check.update(found=True,name=video['name'],size=video['size'],quality=quality_hint(video['name'],c.name))
                    else:check.update(found=False)
                except Exception as e:check.update(found=False,error_type=type(e).__name__)
                check['seconds']=round(time.monotonic()-start,3);checks.append(check);print(json.dumps(check,ensure_ascii=False),flush=True)
    report={'time':datetime.datetime.now().astimezone().isoformat(),'checks':checks,'cloud_writes':False}
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
if __name__=='__main__':main()
