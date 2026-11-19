"""Read-only live portal work search -> source search -> file inspection."""
import argparse,datetime,json,urllib.request,urllib.parse,time
from pathlib import Path

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',required=True);ap.add_argument('--source-output',required=True);args=ap.parse_args()
    root='http://127.0.0.1:8088';report={'time':datetime.datetime.now().astimezone().isoformat(),'production_mutation':False}
    def call(path,payload=None):
        request=urllib.request.Request(root+path,data=json.dumps(payload).encode() if payload else None,
                                      headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=145) as response:
            data=json.loads(response.read(2*1024*1024))
            if data.get('code')!=0:raise RuntimeError(data.get('msg','request_failed'))
            return data
    started=time.monotonic()
    try:
        data=call('/api/vod/search?kw='+urllib.parse.quote('庆余年'))
        item=next(x for x in data['data'] if x['title']=='庆余年' and x['media_type']=='tv')
        report['catalogue']={'matches':len(data['data']),'title':item['title'],'year':item['year'],'source_status':item['source_status']}
        sources=call('/api/vod/sources',{'id':item['id'],'media_type':item['media_type'],'season':1,'episode':1})
        report['search']={'count':len(sources['candidates']),'partial':sources['partial'],'queries':sources['queries'],'nodes':sources['nodes']}
        checks=[]
        for candidate in sources['candidates'][:5]:
            try:
                result=call('/api/vod/inspect',{'token':candidate['token']})
                check={'candidate':candidate['name'],**result['file']};checks.append(check)
                if result['file']['quality']['level']=='4k':
                    fixture={'cat':sources['item']['cat'],'title':item['title']+' ('+item['year']+')','season':1,'episode':1,'share_url':candidate['share_url']}
                    path=Path(args.source_output);path.touch(mode=0o600,exist_ok=True);path.write_text(json.dumps(fixture,ensure_ascii=False));path.chmod(0o600)
                    report['four_k_file_found']=True;break
            except Exception as e:checks.append({'candidate':candidate['name'],'error_type':type(e).__name__})
        report['inspection']=checks
        report['pass']=bool(report.get('four_k_file_found'))
    except Exception as exc:
        report.update(error_type=type(exc).__name__,**{'pass':False})
    report['seconds']=round(time.monotonic()-started,3)
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if not report['pass']:raise SystemExit(1)
if __name__=='__main__':main()
