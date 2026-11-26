"""Exercise real packaged cinema business functions behind the actual HTTP app."""
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

with tempfile.TemporaryDirectory(prefix='quark-cinema-wiring-') as directory:
    os.environ.update(VOD_BASE_DIR=directory,VOD_ADMIN_TOKEN='isolated-wiring-fixture',VOD_JELLYFIN_ENABLED='0',AI_PARSE_ENABLED='0',SELFHEAL_ENABLED='0',VOD_PREFETCH_ENABLED='0')
    from fastapi.testclient import TestClient
    from main import app
    from quark_vod.integrations.hometheater import vod_search_and_add as cinema
    detail={'id':42,'media_type':'tv','name':'Test Series','original_name':'Test Series','first_air_date':'2025-01-01','origin_country':['US'],'overview':'Fixture','genres':[{'id':18,'name':'Drama'}],'seasons':[{'season_number':2,'episode_count':3,'name':'Season 2'}],'episode_run_time':[50]}
    episodes={'episodes':[{'episode_number':2,'name':'Two','air_date':'2025-01-02'},{'episode_number':3,'name':'Three','air_date':'2025-01-03'}]}
    def metadata(path,**query):
        if path=='search/multi':return {'results':[detail]}
        if path=='tv/42':return dict(detail)
        if path=='tv/42/season/2':return episodes
        if path=='trending/all/day':return {'results':[]}
        raise AssertionError('unexpected metadata call '+path)
    headers={'Authorization':'Bearer isolated-wiring-fixture'}
    with TestClient(app) as client,patch.object(cinema,'_tmdb',side_effect=metadata),patch.object(cinema.search,'search_report',return_value={'results':[],'queries':[],'nodes':[],'partial':False,'cached':False,'configured':False}):
        r=client.get('/api/admin/cinema/search?kw=Test',headers=headers);assert r.status_code==200,r.text;assert r.json()['data'][0]['id']==42
        payload={'id':42,'media_type':'tv','season':2,'episode':3,'variant':'hdr'}
        r=client.post('/api/admin/cinema/catalogue',json=payload,headers=headers);assert r.status_code==200,r.text;assert [e['number'] for e in r.json()['episodes']]==[2,3]
        r=client.post('/api/admin/cinema/sources',json=payload,headers=headers);assert r.status_code==200,r.text;assert r.json()['episode']==3 and not r.json()['candidates']
        r=client.post('/api/admin/cinema/subscribe',json=payload,headers=headers);assert r.status_code==200,r.text
        r=client.get('/api/admin/cinema/discovery',headers=headers);assert r.status_code==200,r.text;assert r.json()['watching'][0]['id']==42
        assert not list(Path(directory).rglob('*.strm')),'read-only fixture must not create media'
        assert not list(Path(directory).rglob('*.mp4'))
    report={'real_business_module':cinema.__name__,'checks':['metadata-search','catalogue-S02E03-HDR','source-search-with-no-provider','subscribe-real-state','discovery-real-calendar'],'external_requests':0,'media_writes':0}
    output=Path(__file__).resolve().parents[1]/'evidence/ui/cinema-wiring.json';output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
