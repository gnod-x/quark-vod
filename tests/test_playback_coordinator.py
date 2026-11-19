"""Concurrent playback and maintenance contracts, isolated from real services."""
import concurrent.futures
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

TMP = tempfile.TemporaryDirectory(prefix='vod-playback-')
os.environ.update(VOD_BASE_DIR=TMP.name, VOD_DATA_DIR=TMP.name+'/data',
                  VOD_CINEMA2_DIR=TMP.name+'/cinema2', AI_PARSE_ENABLED='0')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'app'))
from quark_vod import config, db, server, strm_manager, cleaner, search, coordination
from quark_vod.identity import Work
from quark_vod.transfer import TransferFailed

config.ensure_dirs()
db.init_db()


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        for table in ('transfers','strm_map','plays','cleanup_queue','source_audits','dead_sources','user_pinned','playback_access'):
            db._conn().execute('DELETE FROM '+table)
        db._conn().commit()
        server._in_flight_transfers.clear()
        server._ready_results.clear()
        self.work = Work('电影', '样例 (2026)')
        self.remote = '/夸克网盘/点播/样例/video.mkv'
        self.path = Path(config.CINEMA2_DIR)/'电影'/'样例 (2026)'/'样例.strm'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.path.write_text('http://localhost:8099/pending?cat=电影&t=样例+(2026)&s=0&e=0')
        db.save_strm_map(*self.work.args,str(self.path))
        self.stack=[]
        for obj, name, kwargs in [
            (server,'_wait_direct_url_ready',{'return_value':True}),
            (server,'_remote_still_exists',{'return_value':True}),
            (cleaner,'clean_url_if_obfuscated',{'side_effect':lambda u:u}),
            (strm_manager,'trigger_jellyfin_refresh_item',{}),
        ]:
            p=patch.object(obj,name,**kwargs);p.start();self.stack.append(p)

    def tearDown(self):
        for p in reversed(self.stack):p.stop()

    def candidate(self):
        return search.SearchResult(engine='panshare',source='fixture',name='样例 (2026) 4K',year='2026',
                                   poster='',episodes=[],share_url='https://pan.quark.cn/s/fixture',pan_type='quark')

    @staticmethod
    def source_report(*results, partial=False):
        return {'results': list(results), 'partial': partial, 'nodes': [], 'queries': [], 'cached': False}

    def save(self,*args):
        tid=db.upsert_transfer(*args[:4],status='done',remote_path=self.remote)
        return tid,self.remote

    def test_empty_partial_search_is_distinguished_from_no_qualified_candidates(self):
        tr = Mock()
        cases = ((True, 'search_incomplete'), (False, 'source_unavailable'))
        for partial, expected in cases:
            with self.subTest(partial=partial), patch.object(server, 'get_transfer', return_value=tr), \
                 patch.object(search, 'search_report', return_value=self.source_report(partial=partial)):
                with self.assertRaises(server.PlaybackUnavailable) as failed:
                    server._resolve_once(self.work)
                self.assertEqual(failed.exception.code, expected)
        tr.transfer.assert_not_called()

    def test_preheat_and_two_gets_share_one_reserved_task(self):
        entered,release=threading.Event(),threading.Event()
        def save(*args):
            entered.set();self.assertTrue(release.wait(3));return self.save(*args)
        tr=Mock();tr.transfer.side_effect=save
        with patch.object(server,'get_transfer',return_value=tr), patch.object(search,'search_report',return_value=self.source_report(self.candidate())) as lookup:
            self.assertEqual(server.trigger_preheat_task(*self.work.args),(True,'started'))
            self.assertTrue(entered.wait(3))
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                futures=[pool.submit(server.resolve_playback,self.work) for _ in range(2)]
                self.assertEqual(server.trigger_preheat_task(*self.work.args),(True,'in_progress'))
                release.set();results=[f.result(5) for f in futures]
        self.assertTrue(all(r.url for r in results))
        self.assertEqual(tr.transfer.call_count,1)
        self.assertEqual(lookup.call_count,1)
        self.assertEqual(tr.transfer.call_args.args[:4],self.work.args)
        self.assertEqual(len(tr.transfer.call_args.args),5) # pan_type is NOT a password

    def test_locked_source_works_without_search(self):
        cand=self.candidate()
        db.save_source_audit(*self.work.args,True,True,cand.share_url,cand.name,'valid_4k')
        tr=Mock();tr.transfer.side_effect=self.save
        with patch.object(server,'get_transfer',return_value=tr),patch.object(search,'search_report',side_effect=OSError('offline')) as lookup:
            result=server.resolve_playback(self.work)
        self.assertTrue(result.url);lookup.assert_not_called()

    def test_exact_episode_locked_source_bypasses_display_title_score_and_falls_back(self):
        work=Work('欧美剧集','各取所需 (2026)',1,1)
        locked=search.SearchResult(engine='panshare',source='locked',name='各取所需(德国版)剧情介绍-',
            year='',poster='',episodes=[],share_url='https://pan.quark.cn/s/locked',pan_type='quark')
        fallback=search.SearchResult(engine='panshare',source='fixture',name='各取所需',
            year='',poster='',episodes=[],share_url='https://pan.quark.cn/s/fallback',pan_type='quark')
        self.assertLess(search.score_search_result(locked,work.title,season=1,episode=1),30)
        db.save_source_audit(*work.args,True,False,locked.share_url,locked.name,'valid_hd')
        def transfer(*args):
            if args[-1] == locked.share_url:
                raise TransferFailed('upstream HTTP 404')
            return self.save(*args)
        tr=Mock();tr.transfer.side_effect=transfer
        with patch.object(server,'get_transfer',return_value=tr), \
             patch.object(search,'search_report',return_value=self.source_report(fallback, partial=True)), \
             patch.object(strm_manager,'activate'):
            result=server.resolve_playback(work)
        self.assertTrue(result.url)
        self.assertEqual([call.args[-1] for call in tr.transfer.call_args_list],
                         [locked.share_url,fallback.share_url])

    def test_legacy_undated_episode_falls_back_after_locked_http404(self):
        # Match the failed production request: an undated legacy folder and
        # a failed row recorded before the coordinator tries another share.
        work=Work('欧美剧集','各取所需',1,1)
        path=Path(config.CINEMA2_DIR)/work.cat/work.title/'Season 01'/'各取所需 - S01E01.strm'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('http://localhost:8099/pending?cat=欧美剧集&t=各取所需&s=1&e=1')
        db.save_strm_map(*work.args,str(path))
        locked='https://pan.quark.cn/s/legacy-locked'
        db.save_source_audit(*work.args,True,False,locked,'各取所需(德国版)剧情介绍-','valid_hd')
        fallback=search.SearchResult(engine='panshare',source='fixture',name='各取所需',
            year='',poster='',episodes=[],share_url='https://pan.quark.cn/s/legacy-fallback',pan_type='quark')

        def transfer(*args):
            if args[-1]==locked:
                db.upsert_transfer(*work.args,status='failed',share_url=locked,
                    error='TransferFailed: upstream HTTP 404')
                raise TransferFailed('upstream HTTP 404')
            return self.save(*args)

        tr=Mock();tr.transfer.side_effect=transfer
        with patch.object(server,'get_transfer',return_value=tr), \
             patch.object(search,'search_report',return_value=self.source_report(fallback)) as lookup:
            result=server.resolve_playback(work)
        self.assertTrue(result.url)
        self.assertEqual([call.args[-1] for call in tr.transfer.call_args_list],[locked,fallback.share_url])
        self.assertEqual(lookup.call_args.args[0],work.title)
        self.assertEqual(db.get_transfer(*work.args)['status'],'done')
        self.assertIn('/play?',path.read_text())
        self.assertFalse(db.is_dead_source(locked),'HTTP 404 alone does not prove a cancelled share')
        self.assertEqual(db._conn().execute('SELECT count(*) FROM transfers WHERE cat=? AND title=?',
            (work.cat,work.title)).fetchone()[0],1)

    def test_playback_search_uses_verified_nfo_alias_and_year_without_changing_storage_identity(self):
        from pathlib import Path
        work=Work('热门韩剧','显示标题',1,1)
        folder=Path(config.CINEMA2_DIR)/work.cat/'显示标题'
        season_dir=folder/'Season 01'
        season_dir.mkdir(parents=True,exist_ok=True)
        nfo=folder/'tvshow.nfo'
        nfo.write_text('<tvshow><title>显示标题</title><originaltitle>원제 작품</originaltitle>'
                       '<year>2026</year></tvshow>',encoding='utf-8')
        path=season_dir/'显示标题 - S01E01.strm'
        path.write_text('http://localhost:8099/pending')
        db.save_strm_map(*work.args,str(path))
        candidate=self.candidate();candidate.name='원제 작품 S01E01 1080p'
        candidate.names=[candidate.name]
        tr=Mock();tr.transfer.side_effect=self.save
        with patch.object(server,'get_transfer',return_value=tr), \
             patch.object(search,'search_report',return_value=self.source_report(candidate)) as lookup, \
             patch.object(strm_manager,'activate'):
            server.resolve_playback(work)
        self.assertEqual(lookup.call_args.args[0],'显示标题 (2026)')
        self.assertEqual(lookup.call_args.kwargs['aliases'],('원제 작품',))
        self.assertEqual(tr.transfer.call_args.args[:4],work.args)

    def test_readiness_failure_keeps_saved_file_and_allows_retry(self):
        tr=Mock();tr.transfer.side_effect=self.save
        with patch.object(server,'get_transfer',return_value=tr),patch.object(search,'search_report',return_value=self.source_report(self.candidate())),patch.object(server,'_wait_direct_url_ready',return_value=False),patch.object(strm_manager,'activate') as activate:
            with self.assertRaises(server.PlaybackUnavailable):server.resolve_playback(self.work)
            activate.assert_not_called()
        self.assertEqual(db.find_done_transfer(*self.work.args)['remote_path'],self.remote)
        with patch.object(server,'get_transfer') as transfer:
            self.assertTrue(server.resolve_playback(self.work).url)
            transfer.assert_not_called()

    def test_range_reopens_share_readiness_but_source_reset_invalidates(self):
        tid=db.upsert_transfer(*self.work.args,status='done',remote_path=self.remote)
        with patch.object(server,'_wait_direct_url_ready',return_value=True) as ready:
            first=server.resolve_playback(self.work)
            self.assertEqual(server.resolve_playback(self.work),first)
            self.assertEqual(ready.call_count,1)
            db.upsert_transfer(*self.work.args,status='cleaned',remote_path=None)
            with patch.object(server,'get_transfer',return_value=None):
                with self.assertRaises(server.PlaybackUnavailable):server.resolve_playback(self.work)

    def test_retry_after_transient_failure_is_not_negative_cached(self):
        tr=Mock()
        attempt={'n':0}
        def transfer(*args):
            attempt['n']+=1
            if attempt['n']==1:raise TransferFailed('temporary network error')
            return self.save(*args)
        tr.transfer.side_effect=transfer
        with patch.object(server,'get_transfer',return_value=tr),patch.object(search,'search_report',return_value=self.source_report(self.candidate(), partial=True)):
            with self.assertRaises(server.PlaybackUnavailable) as failed:
                server.resolve_playback(self.work)
            self.assertEqual(failed.exception.code, 'transfer_failed')
            self.assertIn('本轮搜索未完成', str(failed.exception))
            self.assertTrue(server.resolve_playback(self.work).url)
        self.assertEqual(tr.transfer.call_count,2)

    def test_year_and_type_are_never_reused(self):
        db.upsert_transfer('电影','样例 (1995)',0,0,status='done',remote_path=self.remote)
        self.assertIsNone(db.find_done_transfer(*self.work.args))
        db.upsert_transfer('国产动漫','同名 (2026)',1,1,status='done',remote_path=self.remote)
        self.assertIsNone(db.find_done_transfer('国产剧集','同名 (2026)',1,1))

    def test_legacy_missing_year_uses_library_mapping(self):
        db.upsert_transfer('电影','样例',0,0,status='done',remote_path=self.remote)
        self.assertIsNone(db.find_done_transfer(*self.work.args))
        db.save_strm_map('电影','样例',0,0,str(self.path))
        self.assertIsNotNone(db.find_done_transfer(*self.work.args))
        self.assertIsNone(db.find_done_transfer('电影','样例 (1995)',0,0))

    def test_no_substring_preheat_correction(self):
        db.save_strm_map('国产剧集','样例续集 (2026)',1,1,str(self.path))
        self.assertEqual(server._resolve_strm_row('电影','样例 (2026)',0,0),('电影',0,0,False))

    def test_activation_cannot_write_another_year_even_with_dirty_alias(self):
        wrong=self.path.parent.parent/'样例 (1995)'/'old.strm'
        wrong.parent.mkdir(exist_ok=True);wrong.write_text('http://fixture/pending?t=old')
        db.save_strm_map('电影','样例',0,0,str(wrong))
        with self.assertRaises(FileNotFoundError):
            strm_manager.activate('电影','样例 (2000)',0,0,'http://localhost:5244/d/夸克/点播/video.mkv')
        self.assertIn('pending',wrong.read_text())

    def test_adjacent_year_remakes_are_rejected_in_search(self):
        candidate=self.candidate()
        candidate.name='样例 (2025) 4K'
        candidate.year='2025'
        self.assertLess(search.score_search_result(candidate,'样例 (2026)'),0)

    def test_missing_leaf_does_not_stay_live_after_retransfer_failure(self):
        db.upsert_transfer(*self.work.args,status='done',remote_path=self.remote)
        strm_manager._atomic_write(str(self.path),server._direct_url(self.remote))
        with patch.object(server,'_remote_still_exists',return_value=False),patch.object(server,'get_transfer',return_value=None):
            with self.assertRaises(server.PlaybackUnavailable):server.resolve_playback(self.work)
        self.assertTrue(strm_manager.is_pending(self.path.read_text().strip()))
        self.assertIsNone(db.find_done_transfer(*self.work.args))

    def test_same_season_different_episodes_are_serialized(self):
        count={'active':0,'peak':0}
        guard=threading.Lock()
        entered=threading.Event();release=threading.Event()
        first=Work('国产剧集','串行剧 (2026)',1,1)
        second=Work('国产剧集','串行剧 (2026)',1,2)
        def save(*args):
            with guard:
                count['active']+=1;count['peak']=max(count['peak'],count['active'])
            entered.set();release.wait(3)
            with guard:count['active']-=1
            return 999,self.remote
        candidate=self.candidate();candidate.name='串行剧 (2026) S01 4K'
        tr=Mock();tr.transfer.side_effect=save
        with patch.object(server,'get_transfer',return_value=tr),patch.object(search,'search_report',return_value=self.source_report(candidate)),patch.object(search,'score_search_result',return_value=90),patch.object(strm_manager,'activate'):
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                a=pool.submit(server.resolve_playback,first)
                self.assertTrue(entered.wait(2))
                b=pool.submit(server.resolve_playback,second)
                release.set();a.result(4);b.result(4)
        self.assertEqual(count['peak'],1)

    def test_maintenance_stops_on_jellyfin_session_query_failure(self):
        import urllib.request
        with patch.object(coordination.os.path,'isfile',return_value=True),patch.object(config,'get_jellyfin2_key',return_value='synthetic'),patch.object(urllib.request,'urlopen',side_effect=OSError('offline')):
            with self.assertRaises(coordination.Busy):coordination.ensure_idle()

    def test_maintenance_stops_when_jellyfin_database_is_missing(self):
        import urllib.request
        with patch.object(config, 'JELLYFIN2_DB', str(Path(TMP.name)/'missing.db')), \
             patch.object(urllib.request, 'urlopen') as network:
            with self.assertRaises(coordination.Busy):coordination.ensure_idle()
        network.assert_not_called()

    def test_session_guard_accepts_only_confirmed_idle_list(self):
        import io
        import json
        import urllib.request
        for sessions, allowed in (([], True), ([{'NowPlayingItem': {'Name': 'fixture'}}], False), ({}, False)):
            with self.subTest(allowed=allowed), patch.object(coordination.os.path, 'isfile', return_value=True), \
                 patch.object(config, 'get_jellyfin2_key', return_value='synthetic'), \
                 patch.object(urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(sessions).encode())):
                if allowed:
                    coordination.ensure_idle()
                else:
                    with self.assertRaises(coordination.Busy):coordination.ensure_idle()

    def test_atomic_write_keeps_jellyfin_read_permissions(self):
        self.path.chmod(0o644)
        strm_manager._atomic_write(str(self.path),'http://fixture/media')
        self.assertEqual(self.path.stat().st_mode & 0o777,0o644)

    def test_stable_play_route_retains_remote_and_identity(self):
        db.upsert_transfer(*self.work.args,status='done',remote_path=self.remote)
        result=server.resolve_playback(self.work)
        content=self.path.read_text().strip()
        self.assertIn('/play?',content)
        self.assertEqual(strm_manager.parse_live_remote_path(content),self.remote)
        self.assertIn('/d/',result.url)

    def test_fastapi_get_and_head_use_same_saved_work(self):
        import main
        from starlette.requests import Request
        db.upsert_transfer(*self.work.args,status='done',remote_path=self.remote)
        def req(method):
            return Request({'type':'http','method':method,'scheme':'http','path':'/play',
                            'query_string':b'','headers':[],'client':('127.0.0.1',1234),'server':('test',80)})
        response=main.pending(req('GET'),*self.work.args)
        self.assertEqual(response.status_code,302)
        self.assertIn('/d/',response.headers['location'])
        with patch.object(server,'resolve_playback',side_effect=AssertionError('HEAD must not transfer')):
            head=main.pending(req('HEAD'),*self.work.args)
            self.assertEqual(head.status_code,302)
        bad=main.pending(req('GET'),self.work.cat,self.work.title,1,0)
        self.assertEqual(bad.status_code,400)

    def test_concurrent_state_inserts_keep_one_row(self):
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            ids=list(pool.map(lambda _:db.upsert_transfer('电影','并发样例 (2026)',0,0,status='saving'),range(24)))
        self.assertEqual(len(set(ids)),1)

    def test_exclusive_maintenance_cannot_overlap_playback(self):
        @coordination.maintenance
        def mutate():return 'changed'
        with coordination.playback_lease():
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                with self.assertRaises(coordination.Busy):pool.submit(mutate).result()
        with patch.object(coordination, 'ensure_idle'):
            self.assertEqual(mutate(),'changed')


if __name__=='__main__':unittest.main(verbosity=2)
