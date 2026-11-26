import unittest
from unittest.mock import Mock, patch
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from quark_vod import cinema_api

class CinemaApiTests(unittest.TestCase):
    def setUp(self):
        def auth(authorization: str = Header('')):
            if authorization != 'Bearer isolated-fixture':
                raise HTTPException(401)
        app=FastAPI()
        app.include_router(cinema_api.create_router(auth))
        self.client=TestClient(app)
        self.headers={'Authorization':'Bearer isolated-fixture'}
        self.module=Mock()
        self.module.discovery.return_value={'code':0,'saved':[]}
        self.module.search_all.return_value=[{'title':'测试'}]
        self.module.catalogue.return_value={'code':0,'item':{'title':'测试'}}
        self.module.add_to_library.return_value={'code':0,'job':'a'*24}
        self.module.job_status.return_value={'code':0,'state':'ready','play_url':'/services/vod/play?cat=x&t=y','jellyfin_item_id':'abc'}
        self.patch=patch.object(cinema_api,'_integration',return_value=self.module)
        self.patch.start()
        self.close=patch.object(cinema_api.db,'close').start()
        self.addCleanup(patch.stopall)
    def test_every_route_requires_bearer(self):
        for route,method in [('discovery','get'),('search?kw=x','get'),('job?id='+'a'*24,'get'),*[(x,'post') for x in cinema_api._OPERATIONS]]:
            response=getattr(self.client,method)('/api/admin/cinema/'+route,**({'json':{}} if method=='post' else {}))
            self.assertEqual(response.status_code,401)
        self.module.discovery.assert_not_called()
    def test_identity_fields_and_token_only_selection(self):
        response=self.client.post('/api/admin/cinema/catalogue',headers=self.headers,json={'id':42,'media_type':'tv','season':2,'episode':3,'variant':'hdr'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.module.catalogue.call_args.args[0]['season'],2)
        for payload in ({'id':True,'media_type':'movie'},{'id':42,'media_type':'tv','season':-1},{'id':42,'media_type':'movie','path':'/tmp/escape'}):
            self.assertEqual(self.client.post('/api/admin/cinema/catalogue',headers=self.headers,json=payload).status_code,400)
        self.assertEqual(self.client.post('/api/admin/cinema/add',headers=self.headers,json={'token':'a'*24,'share_url':'https://evil.invalid'}).status_code,400)
        self.assertEqual(self.client.post('/api/admin/cinema/add',headers=self.headers,json={'token':'a'*24}).status_code,200)
    def test_input_type_and_size_are_bounded(self):
        self.assertEqual(self.client.post('/api/admin/cinema/add',headers=self.headers,content='{}').status_code,415)
        self.assertEqual(self.client.post('/api/admin/cinema/add',headers=self.headers,json={'token':'a'*17000}).status_code,413)
        self.assertEqual(self.client.get('/api/admin/cinema/search?kw='+('x'*161),headers=self.headers).status_code,400)
        raw='['*1500+'0'+']'*1500
        self.assertEqual(self.client.post('/api/admin/cinema/add',headers={**self.headers,'Content-Type':'application/json'},content=raw).status_code,400)
    def test_errors_never_leak_upstream(self):
        self.module.discovery.side_effect=RuntimeError('Bearer secret https://signed.invalid/file')
        r=self.client.get('/api/admin/cinema/discovery',headers=self.headers)
        self.assertEqual(r.status_code,503)
        self.assertNotIn('secret',r.text)
        self.close.assert_called()
    def test_missing_optional_integration_is_explicit(self):
        with patch.object(cinema_api,'_integration',side_effect=ModuleNotFoundError()):
            r=self.client.get('/api/admin/cinema/discovery',headers=self.headers)
            self.assertEqual(r.json()['error'],'cinema_integration_unavailable')
    def test_play_link_and_jellyfin_public_endpoint(self):
        with patch.dict('os.environ',{'VOD_JELLYFIN_PUBLIC_URL':'https://cinema.invalid/jellyfin'}):
            r=self.client.get('/api/admin/cinema/job?id='+'a'*24,headers=self.headers)
            self.assertEqual(r.json()['play_url'],'/play?cat=x&t=y')
            self.assertTrue(r.json()['jellyfin_url'].startswith('https://cinema.invalid/jellyfin/web/'))
        app=FastAPI(root_path='/services/vod');app.include_router(cinema_api.create_router(lambda:None))
        r=TestClient(app).get('/api/admin/cinema/job?id='+'a'*24)
        self.assertEqual(r.json()['play_url'],'/services/vod/play?cat=x&t=y')
    def test_jellyfin_link_reads_explicit_environment_file(self):
        import os
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            filename=Path(directory)/'runtime.env'
            filename.write_text('VOD_JELLYFIN_PUBLIC_URL=https://env-file.invalid/jellyfin\n')
            with patch.dict(os.environ, {k:v for k,v in os.environ.items() if k!='VOD_JELLYFIN_PUBLIC_URL'}, clear=True):
                with patch.dict(os.environ, {'VOD_ENV_FILE':str(filename)}):
                    values=cinema_api.config._load_env_file()
                with patch.object(cinema_api.config, '_ENV_FILE_VALUES', values):
                    r=self.client.get('/api/admin/cinema/job?id='+'a'*24,headers=self.headers)
                    self.assertEqual(r.status_code,200)
                    self.assertTrue(r.json()['jellyfin_url'].startswith('https://env-file.invalid/jellyfin/web/'))

    def test_replace_reasons_match_business_module(self):
        for reason in ('dv_color_issue','wrong_audio','wrong_episode','hardcoded_ad','corrupted'):
            self.assertTrue(cinema_api._valid_payload('replace',{'token':'a'*24,'reason':reason}))
        self.assertFalse(cinema_api._valid_payload('replace',{'token':'a'*24,'reason':'arbitrary'}))

if __name__=='__main__': unittest.main()
