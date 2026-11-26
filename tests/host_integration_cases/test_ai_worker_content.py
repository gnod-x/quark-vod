"""Offline content-stage acceptance: real files/checkpoints, synthetic answers only."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

TEMP = tempfile.TemporaryDirectory(prefix='cinema-worker-content-import-')
os.environ.update(HOMETHEATER_BASE_DIR=TEMP.name, VOD_BASE_DIR=TEMP.name,
                  VOD_DATA_DIR=TEMP.name+'/data', AI_PARSE_ENABLED='0')
from quark_vod.integrations.hometheater import ai_night_worker as worker
from quark_vod.integrations.hometheater import tmdb_populate
class ContentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cinema-worker-content-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.cinema = self.base/'cinema2'
        self.cinema.mkdir()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for obj, name, value in [(worker,'BASE_DIR',str(self.base)),
                (worker,'STATE_DB',str(self.base/'state.db')),
                (worker.paths,'JELLYFIN2_LOG_DIR',str(self.base/'jellyfin2/config/log')),
                (worker.vod_cfg,'CINEMA2_DIR',str(self.cinema)),
                (worker.vod_cfg,'JELLYFIN2_DB',str(self.base/'jellyfin.db')),
                (worker.vod_cfg,'JELLYFIN2_KEY','fixture-key')]:
            self.stack.enter_context(patch.object(obj,name,value))
        for name in ['log','check_and_yield_for_playback']:
            self.stack.enter_context(patch.object(worker,name))
        self.stack.enter_context(patch('quark_vod.coordination.ensure_idle'))
        self.stack.enter_context(patch.object(worker,'_get_jellyfin_user_id',return_value='fixture-user'))
        self.stack.enter_context(patch.object(worker.vod_cfg,'get_jellyfin2_key',return_value='fixture-key'))
        self.stack.enter_context(patch.object(worker,'is_timeout',return_value=False))
        self.stack.enter_context(patch.object(worker.time,'sleep'))
        self.stack.enter_context(patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden')))
        self.llm = self.stack.enter_context(patch.object(worker,'call_llm',side_effect=AssertionError('model forbidden')))
        worker.init_state_db()

    def movie(self, plot=False):
        folder=self.cinema/'电影'/'fixture';folder.mkdir(parents=True)
        worker.vod_db.save_source_audit('电影','fixture',0,0,1,0,'https://pan.quark.cn/s/fixture','','valid_hd')
        path=folder/'movie.nfo'
        path.write_text('<movie><title>合成作品</title><year>2026</year>'+
                        ('<plot>旧简介</plot>' if plot else '')+'<tag>原标签</tag></movie>')
        return path

    def test_error_logs_refresh_only_failed_quark_and_mark_failed_cms(self):
        logs=self.base/'jellyfin2/config/log';logs.mkdir(parents=True)
        (logs/'FFmpeg.Transcode.failed.log').write_text('-i "http://fixture/d/夸克/点播/a.mp4"\nexited with code 8\n403 Forbidden https://fixture.invalid/dead.m3u8')
        (logs/'FFmpeg.Transcode.good.log').write_text('-i "http://fixture/d/夸克/点播/good.mp4"\nhttps://fixture.invalid/good.m3u8')
        (logs/'log_today.log').write_text('HTTP error 404 https://fixture.invalid/missing.m3u8\n正常 https://fixture.invalid/ok.m3u8')
        old=logs/'FFmpeg.Transcode.old.log';old.write_text('-i "http://fixture/d/夸克/点播/old.mp4"\nexited with code 8')
        os.utime(old,(0,0))
        client=Mock()
        with patch.object(worker,'OpenListClient',return_value=client),patch.object(worker.vod_db,'mark_dead_source') as dead:
            worker.run_yesterday_playback_selfheal()
        client.refresh_path_tree.assert_called_once_with('/夸克/点播/a.mp4')
        self.assertEqual({c.args[0] for c in dead.call_args_list},{'https://fixture.invalid/dead.m3u8','https://fixture.invalid/missing.m3u8'})
        self.llm.assert_not_called()

    def test_image_repair_writes_success_and_preserves_failed_download(self):
        movie=self.movie();movie.write_text('<movie><thumb>https://fixture/poster</thumb></movie>')
        poster=movie.parent/'poster.jpg';poster.write_bytes(b'broken')
        season=self.cinema/'series/Season 01';season.mkdir(parents=True)
        (season/'season.nfo').write_text('<season/>')
        for i in [1,2]:
            (season/f'ep{i}.strm').write_text('http://fixture/play')
            (season/f'ep{i}.nfo').write_text(f'<episode><thumb>https://fixture/ep{i}</thumb></episode>')
        failed=season/'ep2-thumb.jpg';failed.write_bytes(b'old')
        def download(url,path):
            if url.endswith('ep2'):return False
            Path(path).write_bytes(b'image'*300);return True
        with patch.object(tmdb_populate,'download_image',side_effect=download):
            worker.run_library_integrity_check()
        self.assertGreater(poster.stat().st_size,1000)
        self.assertGreater((season/'ep1-thumb.jpg').stat().st_size,1000)
        self.assertEqual(failed.read_bytes(),b'old')
        self.llm.assert_not_called()

    def test_refine_creates_plot_and_failed_api_remains_retryable(self):
        nfo=self.movie()
        reply=json.dumps({'refined_plot':'当前会话人工编写的合成简介','highlights':['人工看点'], 'mood_tags':['合成标签']})
        self.llm.side_effect=None;self.llm.return_value=('session-fixture',reply)
        with patch.object(worker,'_sync_nfo_to_jellyfin2',return_value=False):
            worker.run_refine_movie_plots_and_tags()
        root=ET.parse(nfo).getroot()
        self.assertIn('人工编写',root.findtext('plot') or '')
        self.assertIn('原标签',[x.text for x in root.findall('tag')])
        self.assertFalse(worker.is_already_processed('refine_nfo','电影/fixture/movie.nfo'))
        with patch.object(worker,'_sync_nfo_to_jellyfin2',return_value=True) as sync:
            worker.run_refine_movie_plots_and_tags()
        sync.assert_called_once()
        self.assertTrue(worker.is_already_processed('refine_nfo','电影/fixture/movie.nfo'))
        self.assertEqual(self.llm.call_count,1)
        with patch.object(worker,'_sync_nfo_to_jellyfin2') as sync:
            worker.run_refine_movie_plots_and_tags()
        sync.assert_not_called()

    def test_people_api_outcomes_have_separate_durable_checkpoints(self):
        with sqlite3.connect(worker.vod_cfg.JELLYFIN2_DB) as con:
            con.execute('CREATE TABLE BaseItems(Id TEXT,Name TEXT,Overview TEXT,Type TEXT)')
            con.execute('CREATE TABLE Peoples(Id TEXT,Name TEXT)')
            con.executemany('INSERT INTO BaseItems VALUES(?,?,?,?)',[(p,p,'','MediaBrowser.Controller.Entities.Person') for p in ['a','b']])
        reply=json.dumps([{'id':p,'chinese_name':'合成人物'+p,'biography':'当前会话人工编写的合成人物资料','famous_works':['合成作品']} for p in ['a','b']])
        self.llm.side_effect=None;self.llm.return_value=('session-fixture',reply)
        writes=[]
        def api(req,**kwargs):
            if req.get_method() == 'GET':
                pid = req.full_url.rsplit('/',1)[-1]
                return io.BytesIO(json.dumps({'Id':pid,'Name':pid,'ProviderIds':{'Tmdb':'123'}}).encode())
            data=json.loads(req.data);writes.append(data)
            if data['Id']=='b':raise OSError('synthetic API failure')
            response=io.BytesIO();response.status=204;return response
        with patch('urllib.request.urlopen',side_effect=api):worker.run_enrich_peoples_knowledge()
        self.assertTrue(worker.is_already_processed('enrich_people','people_a_a'))
        self.assertFalse(worker.is_already_processed('enrich_people','people_b_b'))
        self.assertEqual({d['Id'] for d in writes},{'a','b'})
        self.assertTrue(all(d['Name'] == d['Id'] and d['ProviderIds'] == {'Tmdb':'123'} for d in writes))
        writes.clear()
        self.llm.return_value=('session-fixture',json.dumps([{'id':'b','biography':'重试的合成人物资料','famous_works':[]}]))
        def ok(req,**kwargs):
            if req.get_method() == 'GET':
                pid = req.full_url.rsplit('/',1)[-1]
                return io.BytesIO(json.dumps({'Id':pid,'Name':pid,'ProviderIds':{'Tmdb':'123'}}).encode())
            writes.append(json.loads(req.data));response=io.BytesIO();response.status=204;return response
        with patch('urllib.request.urlopen',side_effect=ok):worker.run_enrich_peoples_knowledge()
        self.assertEqual([d['Id'] for d in writes],['b'])
        self.assertTrue(worker.is_already_processed('enrich_people','people_b_b'))


def tearDownModule():
    TEMP.cleanup()


if __name__=='__main__':unittest.main(verbosity=2)
