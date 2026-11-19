"""Offline regressions: synthetic bytes, temporary SQLite, no production startup."""
import asyncio
import io
import os
from pathlib import Path
import sys
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
from contextlib import ExitStack
from unittest.mock import Mock, patch

TMP = tempfile.TemporaryDirectory(prefix='vod-regression-')
os.environ.update(VOD_BASE_DIR=TMP.name, VOD_DATA_DIR=TMP.name + '/data',
                  VOD_CINEMA2_DIR=TMP.name + '/cinema2', AI_PARSE_ENABLED='0')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from quark_vod import cleaner, cleanup, config, coordination, db, strm_manager
from quark_vod.openlist_client import OpenListClient, OpenListError
from quark_vod.transfer import QuarkTransfer, TransferFailed
import main
from starlette.requests import Request


class Upstream(io.BytesIO):
    def __init__(self, body, status, headers):
        super().__init__(body)
        self.status = status
        self.headers = headers


def request(range_value='', method='GET'):
    headers = [(b'range', range_value.encode())] if range_value else []
    return Request({'type': 'http', 'method': method, 'path': '/pending',
                    'raw_path': b'/pending', 'query_string': b'',
                    'scheme': 'http', 'server': ('fixture.invalid', 80),
                    'client': ('127.0.0.1', 1), 'headers': headers})


def response_bytes(response):
    if hasattr(response, 'sync_body'):
        return b''.join(response.sync_body)
    async def read():
        if not hasattr(response, 'body_iterator'):
            return response.body
        return b''.join([chunk async for chunk in response.body_iterator])
    return asyncio.run(read())


class StreamTests(unittest.TestCase):
    def test_media_disconnect_releases_slot_even_when_send_raises(self):
        slot=Mock()
        chunks=iter([b'first'])
        # Real generators support close; the response must call it on failure.
        def data():yield from chunks
        response=main.MediaResponse(data(),headers={'Content-Length':'5'})
        async def fail(*args):raise OSError('disconnected')
        with patch.object(main.media_cache,'stream_slots',slot),patch.object(main.StreamingResponse,'__call__',side_effect=fail):
            with self.assertRaises(OSError):asyncio.run(response({},None,None))
        slot.release.assert_called_once()

    def invoke(self, body, code, headers, rng='', method='GET'):
        upstream = Upstream(body, code, headers)
        def capture_stream(body, **kwargs):
            response = main.Response(**kwargs)
            response.sync_body = body
            return response
        with patch.object(main, 'StreamingResponse', side_effect=capture_stream), patch.object(cleaner.urllib.request, 'urlopen', return_value=upstream) as opened:
            result = main.clean_stream(request(rng, method), 'http://fixture.invalid/video', 8)
            actual = response_bytes(result)
        return result, actual, opened.call_args.args[0], upstream

    def test_closed_range(self):
        r, body, req, upstream = self.invoke(b'234', 206, {'Content-Length': '3', 'Content-Range': 'bytes 10-12/18'}, 'bytes=2-4')
        self.assertEqual((r.status_code, body, r.headers.get('content-range')), (206, b'234', 'bytes 2-4/10'))
        self.assertEqual(req.get_header('Range'), 'bytes=10-12')
        self.assertTrue(upstream.closed)

    def test_suffix_range(self):
        r, body, req, _ = self.invoke(b'789', 206, {'Content-Length': '3', 'Content-Range': 'bytes 15-17/18'}, 'bytes=-3')
        self.assertEqual(req.get_header('Range'), 'bytes=-3')
        self.assertEqual((r.status_code, body, r.headers['content-range']), (206, b'789', 'bytes 7-9/10'))

    def test_long_suffix_never_leaks_prefix(self):
        r, body, _, _ = self.invoke(b'PNGFAKE!0123456789', 206, {'Content-Length': '18', 'Content-Range': 'bytes 0-17/18'}, 'bytes=-99')
        self.assertEqual(body, b'0123456789')
        self.assertEqual(r.headers['content-range'], 'bytes 0-9/10')
        self.assertEqual(r.headers['content-length'], '10')

    def test_full_response_is_200(self):
        r, body, _, _ = self.invoke(b'0123456789', 206, {'Content-Length': '10', 'Content-Range': 'bytes 8-17/18'})
        self.assertEqual((r.status_code, body), (200, b'0123456789'))
        self.assertNotIn('content-range', r.headers)

    def test_range_ignored_by_upstream(self):
        r, body, _, _ = self.invoke(b'PNGFAKE!0123456789', 200, {'Content-Length': '18'}, 'bytes=2-4')
        self.assertEqual((r.status_code, body, r.headers['content-length']), (206, b'234', '3'))
        self.assertEqual(r.headers['content-range'], 'bytes 2-4/10')

    def test_full_ignored_range(self):
        r, body, _, _ = self.invoke(b'PNGFAKE!0123456789', 200, {'Content-Length': '18'})
        self.assertEqual((r.status_code, body), (200, b'0123456789'))
        self.assertEqual(r.headers['content-length'], '10')

    def test_head_closes_stream(self):
        r, body, _, upstream = self.invoke(b'0123456789', 206, {'Content-Length': '10', 'Content-Range': 'bytes 8-17/18'}, method='HEAD')
        self.assertEqual((r.status_code, body), (200, b''))
        self.assertTrue(upstream.closed)

    def test_multipart_range_is_ignored_safely(self):
        r, body, req, _ = self.invoke(b'0123456789', 206, {'Content-Length': '10', 'Content-Range': 'bytes 8-17/18'}, 'bytes=0-1,4-5')
        self.assertEqual((r.status_code, body), (200, b'0123456789'))
        self.assertEqual(req.get_header('Range'), 'bytes=8-')

    def test_unsatisfiable_range_no_heal(self):
        error = urllib.error.HTTPError('http://fixture.invalid', 416, 'range', {'Content-Range': 'bytes */18'}, io.BytesIO())
        with patch.object(cleaner.urllib.request, 'urlopen', side_effect=error), patch.object(cleaner, '_trigger_selfheal_for') as heal:
            r = main.clean_stream(request('bytes=100-'), 'http://fixture.invalid/video', 8)
        self.assertEqual(r.status_code, 416)
        self.assertEqual(r.headers['content-range'], 'bytes */10')
        heal.assert_not_called()

    def test_negative_skip_rejected(self):
        with patch.object(cleaner.urllib.request, 'urlopen') as opened:
            r = main.clean_stream(request(), 'http://fixture.invalid/video', -1)
        self.assertEqual(r.status_code, 400)
        opened.assert_not_called()


class StateTests(unittest.TestCase):
    def setUp(self):
        # Business fixtures have no running Jellyfin; the actual session guard
        # is exercised separately rather than bypassed by a missing DB.
        idle = patch.object(coordination, 'ensure_idle')
        idle.start()
        self.addCleanup(idle.stop)
        for table in ('transfers', 'strm_map', 'plays', 'cleanup_queue', 'user_pinned', 'playback_access'):
            db._conn().execute('DELETE FROM ' + table)
        db._conn().commit()
        self.path = Path(config.CINEMA2_DIR)/'电影'/'样例 (2026)'/'样例.strm'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text('http://127.0.0.1:5244/d/夸克/点播/样例/video.mkv\n')
        self.tid = db.upsert_transfer('电影', '样例 (2026)', 0, 0, status='done', remote_path='/夸克网盘/点播/样例/video.mkv')
        db.save_strm_map('电影', '样例 (2026)', 0, 0, str(self.path))
        db.enqueue_cleanup(self.tid, '/夸克网盘/点播/样例/video.mkv', 1)

    def test_admin_reset_restore_failure_preserves_done(self):
        with patch.object(strm_manager, 'restore_pending', side_effect=OSError('fixture read-only')):
            r = main.admin_reset_transfer(self.tid)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(db.get_transfer_by_id(self.tid)['status'], 'done')

    def test_expiry_restore_failure_preserves_done(self):
        with patch.object(cleanup, 'OpenListClient', return_value=Mock()) as factory, patch.object(cleanup, 'get_protected_titles', return_value=set()), patch.object(strm_manager, 'restore_pending', side_effect=OSError('fixture read-only')):
            result = cleanup.run_cleanup(verbose=False)
            factory.return_value.remove.assert_not_called()
        self.assertEqual(db.get_transfer_by_id(self.tid)['status'], 'done')
        self.assertEqual(result, (0, 1, 0))
        self.assertEqual(len(db.due_cleanups()), 1)

    def test_expiry_remove_failure_not_success(self):
        ol = Mock()
        ol.remove.side_effect = OpenListError('fixture failed')
        with patch.object(cleanup, 'OpenListClient', return_value=ol), patch.object(cleanup, 'get_protected_titles', return_value=set()):
            result = cleanup.run_cleanup(verbose=False)
        self.assertEqual(result, (0, 1, 0))
        self.assertEqual(len(db.due_cleanups()), 1)
        self.assertTrue(strm_manager.is_pending(self.path.read_text().strip()))

    def test_mkdir_checks_application_error(self):
        ol = OpenListClient.__new__(OpenListClient)
        ol._request = Mock(return_value={'code': 500, 'message': 'fixture failure'})
        ol.list = Mock(return_value=[])
        with self.assertRaises(OpenListError):
            ol.mkdir('/fixture')

    def test_openlist_json_401_reauth_once(self):
        ol = OpenListClient.__new__(OpenListClient)
        ol.base = 'http://fixture.invalid'
        ol.token = 'synthetic-old'
        ol._login = Mock(side_effect=lambda: setattr(ol, 'token', 'synthetic-new'))
        first = Upstream(b'{"code":401}', 200, {})
        second = Upstream(b'{"code":200,"data":{}}', 200, {})
        with patch('urllib.request.urlopen', side_effect=[first, second]) as opened:
            self.assertEqual(ol._request('/api/fs/list', {})['code'], 200)
        ol._login.assert_called_once()
        self.assertEqual(opened.call_count, 2)

    def test_write_probe_cleanup_on_failed_readback(self):
        ol = OpenListClient.__new__(OpenListClient)
        ol._write_probe_lock = threading.Lock()
        ol._write_probe_cache = None
        ol._request = Mock(side_effect=[{'code':200},{'code':200,'data':{'content':[]}}])
        ol.remove = Mock()
        self.assertFalse(ol.check_write_capable()[0])
        ol.remove.assert_called_once()
        created = ol._request.call_args_list[0].args[1]['path']
        self.assertEqual(ol.remove.call_args.args[0],created)
        self.assertIn('/vod_write_probe_',created)

    def test_4k_debug_does_not_use_hdr_alone(self):
        from quark_vod.search import SearchResult
        item=SearchResult(engine='panshare',source='fixture',name='样例 HDR 1080p',year='',poster='',episodes=[],share_url='https://fixture.invalid/s/id',pan_type='quark')
        with patch.object(main.search_mod,'search',return_value=[item]), patch.object(main.search_mod,'score_search_result',return_value=90), patch.object(db,'get_locked_4k_share',return_value=(None,False,'')):
            result=main.admin_search_debug('样例','电影',0,0)
        self.assertFalse(result['candidates'][0]['is_4k_hint'])

    def test_pending_done_redirect_records_access_without_watch_event(self):
        server=main.legacy_srv
        with patch.object(server,'_remote_still_exists',return_value=True), patch.object(server,'_wait_direct_url_ready',return_value=True), patch.object(cleaner,'clean_url_if_obfuscated',side_effect=lambda u:u), patch.object(strm_manager,'trigger_jellyfin_refresh_item'), patch.object(server,'get_transfer') as transfer:
            result=server.Handler._handle_pending(main.DummyHandler({}),{'cat':['电影'],'t':['样例 (2026)'],'s':['0'],'e':['0']})
        self.assertEqual(result.status_code,302)
        self.assertIn('/d/',result.headers['location'])
        transfer.assert_not_called()
        self.assertEqual(db._conn().execute('SELECT COUNT(*) FROM plays').fetchone()[0],0)
        self.assertTrue(db.recently_accessed(self.tid))

    def test_fastapi_redirect_encodes_unicode_location(self):
        result=main.legacy_srv.PlaybackResult('http://fixture.invalid/d/影片/样例.mp4',self.tid,'/夸克网盘/点播/影片/样例.mp4')
        with patch.object(main.legacy_srv,'resolve_playback',return_value=result):
            response=main.pending(request(),'电影','样例 (2026)',0,0)
        location=response.headers['location']
        self.assertEqual(response.status_code,302)
        self.assertNotRegex(location,r'[^\x00-\x7f]')
        self.assertIn('%E5',location)

    def test_write_path_is_bounded_and_idempotent(self):
        for original, expected in [('/夸克/点播/a', '/夸克网盘/点播/a'), ('/夸克网盘/点播/a', '/夸克网盘/点播/a'), ('/夸克other/a', '/夸克other/a')]:
            self.assertEqual(config.write_path(original), expected)
            self.assertEqual(config.write_path(expected), expected)

    def test_cookie_reads_latest_committed_wal(self):
        path = Path(config.BASE_DIR)/'openlist/data/data.db'
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute('CREATE TABLE IF NOT EXISTS x_storages(mount_path TEXT,addition TEXT)')
            writer.commit()
            writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            writer.execute('DELETE FROM x_storages')
            writer.execute('INSERT INTO x_storages VALUES (?,?)', (config.QUARK_MOUNT, '{"cookie":"synthetic-current"}'))
            writer.commit()
            with patch.object(config,'_cached_quark_cookie',''), patch.dict(os.environ, {'QUARK_COOKIE':''}):
                self.assertEqual(config.get_quark_cookie(), 'synthetic-current')
        writer.close()

    def test_admin_selfheal_uses_existing_function(self):
        with patch.object(main.selfheal, 'scan_and_heal', return_value=(1, 0, 0, 0)) as scan:
            self.assertTrue(main.admin_trigger_selfheal()['ok'])
        scan.assert_called_once()

    def test_path_lookup_pages_and_propagates_errors(self):
        tr = QuarkTransfer(None)
        with patch.object(tr, '_request', side_effect=[
            {'code':0,'data':{'_total':2,'list':[{'file_name':'other','fid':'1'}]}},
            {'code':0,'data':{'_total':2,'list':[{'file_name':'点播','fid':'2'}]}},
        ]) as called:
            self.assertEqual(tr._get_quark_fid('/夸克网盘/点播'), '2')
            self.assertEqual(called.call_count, 2)
        with patch.object(tr, '_request', side_effect=TransferFailed('network unavailable')):
            with self.assertRaises(TransferFailed):
                tr._get_quark_fid('/夸克网盘/点播')

    def test_partial_listing_not_treated_as_complete(self):
        tr = QuarkTransfer(None)
        with patch.object(tr, '_request', return_value={'code':0,'data':{'_total':10,'list':[]}}):
            with self.assertRaises(TransferFailed):
                tr._quark_list_fid('0')

    def test_recursive_usage_enforces_entry_limit(self):
        ol = Mock()
        ol.list.return_value = [{'name': 'd'+str(i), 'is_dir': True} for i in range(5)]
        with self.assertRaises(OpenListError):
            cleanup.get_vod_disk_usage(ol, max_entries=7)
        self.assertLessEqual(ol.list.call_count, 8)

    def test_expiry_already_pending_completes(self):
        strm_manager.restore_pending(str(self.path), '电影', '样例 (2026)', 0, 0)
        with patch.object(cleanup, 'OpenListClient', return_value=Mock()), patch.object(cleanup, 'get_protected_titles', return_value=set()):
            self.assertEqual(cleanup.run_cleanup(verbose=False), (1,0,0))
        self.assertEqual(db.get_transfer_by_id(self.tid)['status'],'cleaned')

    def test_old_expiry_cannot_invalidate_new_transfer(self):
        new_path='/夸克网盘/点播/new/video.mkv'
        db.upsert_transfer('电影','样例 (2026)',0,0,status='done',remote_path=new_path)
        with patch.object(cleanup,'OpenListClient',return_value=Mock()) as factory, patch.object(cleanup,'get_protected_titles',return_value=set()):
            self.assertEqual(cleanup.run_cleanup(verbose=False),(0,0,1))
            factory.return_value.remove.assert_not_called()
        self.assertEqual(db.get_transfer_by_id(self.tid)['status'],'done')

    def directory_fixture(self):
        db._conn().execute('UPDATE cleanup_queue SET remote_path=?', ('/夸克网盘/点播/样例',))
        db._conn().commit()
        sibling = self.path.parent/'sibling.strm'
        sibling.write_text('http://127.0.0.1:5244/d/夸克/点播/样例/sibling.mkv')
        tid = db.upsert_transfer('剧集', '另一个作品', 1, 2, status='done', remote_path='/夸克网盘/点播/样例/sibling.mkv')
        db.save_strm_map('剧集', '另一个作品', 1, 2, str(sibling))
        return tid, sibling

    def test_expiry_directory_restores_all_siblings(self):
        tid, sibling = self.directory_fixture()
        with patch.object(cleanup, 'OpenListClient', return_value=Mock()) as factory, patch.object(cleanup, 'get_protected_titles', return_value=set()):
            self.assertEqual(cleanup.run_cleanup(verbose=False), (1, 0, 0))
            factory.return_value.remove.assert_called_once_with('/夸克网盘/点播/样例')
        for transfer_id in (tid, self.tid):
            self.assertEqual(db.get_transfer_by_id(transfer_id)['status'], 'cleaned')
        for path in (self.path, sibling):
            self.assertTrue(strm_manager.is_pending(path.read_text().strip()))

    def test_expiry_directory_protected_sibling_blocks_delete(self):
        self.directory_fixture()
        with patch.object(cleanup, 'OpenListClient', return_value=Mock()) as factory, patch.object(cleanup, 'get_protected_titles', return_value={'另一个作品'}):
            self.assertEqual(cleanup.run_cleanup(verbose=False), (0, 0, 1))
            factory.return_value.remove.assert_not_called()

    def test_expiry_directory_newer_retention_blocks_delete(self):
        tid, _ = self.directory_fixture()
        db.enqueue_cleanup(tid, '/夸克网盘/点播/样例', db.now() + 86400)
        with patch.object(cleanup, 'OpenListClient', return_value=Mock()) as factory, patch.object(cleanup, 'get_protected_titles', return_value=set()):
            self.assertEqual(cleanup.run_cleanup(verbose=False), (0, 0, 1))
            factory.return_value.remove.assert_not_called()

    def test_pending_unready_returns_503_without_play_or_activation(self):
        server = main.legacy_srv
        with patch.object(server, '_remote_still_exists', return_value=True), patch.object(server, '_wait_direct_url_ready', return_value=False), patch.object(strm_manager, 'activate') as activate:
            result = server.Handler._handle_pending(main.DummyHandler({}), {'cat':['电影'], 't':['样例 (2026)'], 's':['0'], 'e':['0']})
        self.assertEqual(result.status_code, 503)
        activate.assert_not_called()
        self.assertEqual(db._conn().execute('SELECT COUNT(*) FROM plays').fetchone()[0], 0)

    def test_pending_missing_remote_restore_failure_preserves_done(self):
        server = main.legacy_srv
        with patch.object(server, '_remote_still_exists', return_value=False), patch.object(strm_manager, 'restore_pending', side_effect=OSError('fixture read-only')), patch.object(server, 'get_transfer') as transfer:
            result = server.Handler._handle_pending(main.DummyHandler({}), {'cat':['电影'], 't':['样例 (2026)'], 's':['0'], 'e':['0']})
        self.assertEqual(result.status_code, 503)
        transfer.assert_not_called()
        self.assertEqual(db.get_transfer_by_id(self.tid)['status'], 'done')
        self.assertFalse(strm_manager.is_pending(self.path.read_text().strip()))

    def transfer_fixture(self):
        tr = QuarkTransfer(Mock())
        tr.ensure_ready = Mock()
        tr.ensure_vod_dir = Mock(return_value='fixture-root')
        tr._check_capacity = Mock(return_value=(True, 10**12))
        tr._get_quark_fid = Mock(return_value='target-dir')
        item = {'fid':'selected', 'share_fid_token':'synthetic', 'file_name':'Fixture.mkv',
                'size':1024**3, 'dir':False, 'parent_fid':'nested', 'relative_path':'Fixture.mkv'}
        tr.resolve_share = Mock(return_value=('synthetic', 'fixture', [item]))
        tr.share_files = Mock(return_value=[item])
        tr._request = Mock(return_value={'code':0,'data':{'task_id':'task'}})
        tr._wait_saved_task = Mock()
        tr._quark_list_fid = Mock(side_effect=[[],[{'name':'Fixture.mkv','size':1024**3,'is_dir':False,'fid':'saved'}]])
        tr.prefetch_episodes = Mock(return_value={})
        return tr,item

    def test_transfer_saves_only_selected_file_and_exact_queue(self):
        tr, item = self.transfer_fixture()
        tid, remote = tr._do_transfer('电影','Fixture (2026)',0,0,'https://pan.quark.cn/s/fixture')
        self.assertTrue(remote.startswith(config.QUARK_VOD_ROOT+'/vod_'))
        self.assertTrue(remote.endswith('/Fixture.mkv'))
        payload=tr._request.call_args.kwargs['payload']
        self.assertEqual(payload['fid_list'],['selected'])
        self.assertEqual(payload['pdir_fid'],'nested')
        self.assertEqual(db.get_transfer_by_id(tid)['status'],'done')
        self.assertIsNone(db.get_transfer_by_id(tid)['save_task_id'])
        self.assertEqual(db._conn().execute('SELECT remote_path FROM cleanup_queue WHERE transfer_id=?',(tid,)).fetchone()[0],remote)

    def test_transfer_timeout_reuses_original_task_without_second_save(self):
        tr, item = self.transfer_fixture()
        tr._wait_saved_task.side_effect = TransferFailed('task temporarily unavailable')
        with self.assertRaises(TransferFailed):tr._do_transfer('电影','Fixture (2026)',0,0,'https://pan.quark.cn/s/fixture')
        failed=db.get_transfer('电影','Fixture (2026)',0,0)
        self.assertEqual(failed['status'],'failed')
        self.assertEqual(failed['save_task_id'],'task')
        tr._wait_saved_task.side_effect=None
        tr._quark_list_fid.side_effect=[[],[{'name':'Fixture.mkv','size':1024**3,'is_dir':False,'fid':'saved'}]]
        tid,remote=tr._do_transfer('电影','Fixture (2026)',0,0,'https://pan.quark.cn/s/fixture')
        self.assertEqual(tr._request.call_count,1)
        self.assertEqual(db.get_transfer_by_id(tid)['status'],'done')

    def test_save_connection_loss_does_not_blindly_resubmit(self):
        from quark_vod.transfer import TransferNotReady
        tr,item=self.transfer_fixture()
        tr._request.side_effect=TransferFailed('socket lost after submission')
        with self.assertRaises(TransferFailed):tr._do_transfer('电影','Fixture (2026)',0,0,'https://pan.quark.cn/s/fixture')
        tr._quark_list_fid.side_effect=[[]]
        with self.assertRaises(TransferNotReady):tr._do_transfer('电影','Fixture (2026)',0,0,'https://pan.quark.cn/s/fixture')
        self.assertEqual(tr._request.call_count,1)

    def test_existing_selected_file_is_not_saved_again(self):
        tr,item=self.transfer_fixture()
        tr._quark_list_fid.side_effect=[[{'name':'Fixture.mkv','size':1024**3,'is_dir':False,'fid':'saved'}]]
        tid,remote=tr._do_transfer('电影','Fixture (2026)',0,0,'https://pan.quark.cn/s/fixture')
        tr._request.assert_not_called()
        self.assertEqual(db.get_transfer_by_id(tid)['status'],'done')

    def test_recovery_works_after_source_share_disappears(self):
        tr,item=self.transfer_fixture()
        tr._wait_saved_task.side_effect=TransferFailed('temporary')
        with self.assertRaises(TransferFailed):tr._do_transfer('电影','Fixture (2026)',0,0,'https://pan.quark.cn/s/fixture')
        tr.resolve_share.side_effect=TransferFailed('share removed')
        tr._quark_list_fid.side_effect=[[{'name':'Fixture.mkv','size':1024**3,'is_dir':False,'fid':'saved'}]]
        tid,remote=tr._do_transfer('电影','Fixture (2026)',0,0,'https://pan.quark.cn/s/fixture')
        self.assertEqual(db.get_transfer_by_id(tid)['status'],'done')
        self.assertEqual(tr.resolve_share.call_count,1)

    def test_parent_season_prevents_wrong_episode(self):
        tr=QuarkTransfer(Mock())
        files=[{'file_name':'01.mkv','relative_path':'Season 02/01.mkv','size':1024**3}]
        self.assertIsNone(tr.select_share_video(files,'fixture',1,1))

    def test_multi_movie_package_does_not_pick_largest_unrelated_feature(self):
        tr=QuarkTransfer(Mock())
        files=[{'file_name':name,'relative_path':name,'size':1024**3} for name in ('无关片甲.mkv','无关片乙.mkv')]
        with self.assertRaises(TransferFailed):tr.select_share_video(files,'目标影片 (2026)',0,0)

    def test_recent_resolution_protects_cloud_cleanup(self):
        db.record_access(self.tid)
        with patch.object(cleanup,'OpenListClient',return_value=Mock()) as factory,patch.object(cleanup,'get_protected_titles',return_value=set()):
            self.assertEqual(cleanup.run_cleanup(verbose=False),(0,0,1))
            factory.return_value.remove.assert_not_called()

    def test_share_pagination_and_parent_fid(self):
        tr=QuarkTransfer(Mock())
        pages=[{'code':0,'data':{'_total':101,'list':[{'file_name':f'S01E{i:02}.mkv','fid':str(i),'dir':False,'size':1024**3} for i in range(1,101)]}},
               {'code':0,'data':{'_total':101,'list':[{'file_name':'S01E101.mkv','fid':'101','dir':False,'size':1024**3}]}}]
        tr._request=Mock(side_effect=pages)
        files=tr.share_files('token','share',[{'file_name':'season','fid':'nested','dir':True}])
        self.assertEqual(len(files),101)
        self.assertTrue(all(f['parent_fid']=='nested' for f in files))

    def test_leaf_deleted_is_detected_without_deleting_healthy_sibling(self):
        tr=Mock()
        tr._quark_list.side_effect=[ [{'name':'show','is_dir':True}], [{'name':'E02.mkv','is_dir':False}] ]
        root=config.QUARK_VOD_ROOT+'/show/'
        self.assertEqual(main.selfheal._remote_exists_batch(tr,[root+'E01.mkv',root+'E02.mkv']),{root+'E01.mkv':False,root+'E02.mkv':True})
        self.assertEqual(tr._quark_list.call_count,2)

    def test_leaf_check_network_failure_preserves_state(self):
        tr=Mock()
        tr._quark_list.side_effect=TransferFailed('authentication temporarily failed')
        root=config.QUARK_VOD_ROOT+'/show/E01.mkv'
        self.assertEqual(main.selfheal._remote_exists_batch(tr,[root]),{root:True})

    def test_activation_does_not_revive_unrelated_or_empty_remote(self):
        other = db.upsert_transfer('其他电影', '样例 (2026)', 0, 0, status='cleaned', remote_path=None)
        conflicting = db.upsert_transfer('另一区', '样例 (2026)', 0, 0, status='cleaned', remote_path='/夸克网盘/点播/different/video.mkv')
        strm_manager._sync_transfer_done('电影', ['样例 (2026)'], 0, 0, '/夸克/点播/样例/video.mkv')
        for tid in (other, conflicting):
            self.assertEqual(db.get_transfer_by_id(tid)['status'], 'cleaned')

    def test_quota_failed_delete_does_not_report_freed_bytes(self):
        size=300*1024**3
        item={'name':'样例','remote_path':'/夸克网盘/点播/样例','size':size}
        ol=Mock()
        ol.remove.side_effect=OpenListError('synthetic delete failure')
        with patch.object(cleanup,'get_vod_disk_usage',return_value=(size,[item])), patch.object(cleanup,'resolve_quotas',return_value=(1,1,0,0)), patch.object(cleanup,'get_protected_titles',return_value=set()):
            self.assertEqual(cleanup.enforce_vod_quota(ol=ol,verbose=False),(0,0,size))
        self.assertEqual(db.get_transfer_by_id(self.tid)['status'],'done')

    def test_quota_path_boundary_does_not_clean_similar_title(self):
        size=300*1024**3
        other=db.upsert_transfer('电影','另一个作品',0,0,status='done',remote_path='/夸克网盘/点播/样例续集/video.mkv')
        item={'name':'样例','remote_path':'/夸克网盘/点播/样例','size':size}
        with patch.object(cleanup,'get_vod_disk_usage',return_value=(size,[item])), patch.object(cleanup,'resolve_quotas',return_value=(1,1,0,0)), patch.object(cleanup,'get_protected_titles',return_value=set()):
            self.assertEqual(cleanup.enforce_vod_quota(ol=Mock(),verbose=False)[0],1)
        self.assertEqual(db.get_transfer_by_id(other)['status'],'done')
        self.assertEqual(db.get_transfer_by_id(self.tid)['status'],'cleaned')

    def test_quota_restore_failure_preserves_remote_and_database(self):
        size = 300*1024**3
        item = {'name':'样例', 'remote_path':'/夸克网盘/点播/样例', 'size':size}
        ol = Mock()
        with patch.object(cleanup, 'get_vod_disk_usage', return_value=(size,[item])), patch.object(cleanup, 'resolve_quotas', return_value=(1,1,0,0)), patch.object(cleanup, 'get_protected_titles', return_value=set()), patch.object(strm_manager, 'restore_pending', side_effect=OSError('synthetic read-only')):
            self.assertEqual(cleanup.enforce_vod_quota(ol=ol, verbose=False), (0,0,size))
        ol.remove.assert_not_called()
        self.assertEqual(db.get_transfer_by_id(self.tid)['status'], 'done')

    def test_protection_database_failure_aborts_cleanup(self):
        with patch.object(cleanup.os.path, 'exists', return_value=True), patch.object(cleanup.sqlite3, 'connect', side_effect=sqlite3.OperationalError('fixture unavailable')):
            with self.assertRaises(RuntimeError):
                cleanup.get_protected_titles()

    def test_missing_protection_database_aborts_cleanup_and_dry_run(self):
        missing = Path(TMP.name)/'missing-protection'/ 'jellyfin.db'
        original = self.path.read_text()
        for dry in (False, True):
            with self.subTest(dry_run=dry), patch.object(cleanup, 'JF_DB', str(missing)), \
                 patch.object(cleanup, 'OpenListClient') as factory:
                with self.assertRaises(RuntimeError):
                    cleanup.run_cleanup(dry_run=dry, verbose=False)
                factory.return_value.remove.assert_not_called()
                self.assertEqual(db.get_transfer_by_id(self.tid)['status'], 'done')
                self.assertEqual(self.path.read_text(), original)
                self.assertFalse(missing.exists())

    def test_protection_query_maps_episode_progress_to_series(self):
        protected_db=Path(TMP.name)/'protected-jellyfin.db'
        with sqlite3.connect(protected_db) as con:
            con.executescript('''
                CREATE TABLE BaseItems (Id TEXT, Name TEXT, SeriesName TEXT, Type TEXT);
                CREATE TABLE UserData (ItemId TEXT, IsFavorite INTEGER, Played INTEGER,
                    PlayCount INTEGER, PlaybackPositionTicks INTEGER);
            ''')
            con.execute('INSERT INTO BaseItems VALUES (?,?,?,?)', ('episode','第一集','样例剧','MediaBrowser.Controller.Entities.TV.Episode'))
            con.execute('INSERT INTO UserData VALUES (?,?,?,?,?)', ('episode',1,0,0,0))
            con.execute('INSERT INTO BaseItems VALUES (?,?,?,?)', ('movie','样例电影','','MediaBrowser.Controller.Entities.Movies.Movie'))
            con.execute('INSERT INTO UserData VALUES (?,?,?,?,?)', ('movie',0,0,2,0))
        with patch.object(cleanup,'JF_DB',str(protected_db)), \
             patch.object(cleanup.db,'get_all_pinned_titles',return_value=set()):
            protected=cleanup.get_protected_titles()
        self.assertNotIn('第一集',protected)
        self.assertIn('样例剧',protected)
        self.assertIn('样例电影',protected)

    def test_protection_query_fails_closed_for_episode_without_series(self):
        protected_db=Path(TMP.name)/'protected-episode-without-series.db'
        with sqlite3.connect(protected_db) as con:
            con.executescript('''
                CREATE TABLE BaseItems (Id TEXT, Name TEXT, SeriesName TEXT, Type TEXT);
                CREATE TABLE UserData (ItemId TEXT, IsFavorite INTEGER, Played INTEGER,
                    PlayCount INTEGER, PlaybackPositionTicks INTEGER);
            ''')
            con.execute('INSERT INTO BaseItems VALUES (?,?,?,?)', ('episode','第一集','','MediaBrowser.Controller.Entities.TV.Episode'))
            con.execute('INSERT INTO UserData VALUES (?,?,?,?,?)', ('episode',1,0,0,0))
        with patch.object(cleanup,'JF_DB',str(protected_db)), \
             patch.object(cleanup.db,'get_all_pinned_titles',return_value=set()):
            with self.assertRaisesRegex(RuntimeError,'无法读取 Jellyfin'):
                cleanup.get_protected_titles()

    def test_mismatch_cleanup_preserves_directory_used_by_valid_series(self):
        remote = '/夸克网盘/点播/短剧包'
        db.upsert_transfer('电影', '样例 (2026)', 0, 0, status='done', remote_path=remote+'/S01E01.mkv')
        db.upsert_transfer('剧集', '合法剧集', 1, 2, status='done', remote_path=remote+'/S01E02.mkv')
        with patch.object(cleanup, 'get_protected_titles', return_value=set()), patch.object(cleanup, 'OpenListClient', return_value=Mock()) as factory:
            self.assertEqual(cleanup.clean_mismatched_short_dramas(verbose=False), 0)
            factory.return_value.remove.assert_not_called()
        self.assertEqual(db.get_transfer_by_id(self.tid)['status'], 'done')

    def test_mismatch_cleanup_respects_protection(self):
        db.upsert_transfer('电影', '样例 (2026)', 0, 0, status='done', remote_path='/夸克网盘/点播/短剧包/video.mkv')
        with patch.object(cleanup, 'get_protected_titles', return_value={'样例 (2026)'}), patch.object(cleanup, 'OpenListClient', return_value=Mock()) as factory:
            self.assertEqual(cleanup.clean_mismatched_short_dramas(verbose=False), 0)
            factory.return_value.remove.assert_not_called()

    def test_selfheal_restore_noop_cannot_invalidate_live_strm(self):
        heal = main.selfheal
        remote = '/夸克网盘/点播/样例/video.mkv'
        with patch.object(heal, 'OpenListClient', return_value=Mock()), patch.object(heal, '_remote_exists_batch', return_value={remote:False}), patch.object(heal, '_iter_live_strms', return_value=[(str(self.path),remote)]), patch.object(strm_manager, 'restore_pending', return_value=False):
            self.assertEqual(heal.heal_remote(remote), 0)
        self.assertEqual(db.get_transfer_by_id(self.tid)['status'], 'done')

    def test_selfheal_missing_root_never_creates_it(self):
        tr = Mock()
        tr._quark_list.side_effect = TransferFailed('不存在该目录')
        remote = '/夸克网盘/点播/样例/video.mkv'
        self.assertEqual(main.selfheal._remote_exists_batch(tr,[remote]), {remote:False})
        tr.ensure_vod_dir.assert_not_called()

    def test_selfheal_reconciles_missing_done_without_live_map(self):
        heal=main.selfheal
        remote=db.get_transfer_by_id(self.tid)['remote_path']
        with patch.object(heal,'_iter_live_strms',return_value=[]),patch.object(heal,'_remote_exists_batch',return_value={remote:False}):
            result=heal.scan_and_heal(verbose=False)
        self.assertEqual(result,(1,1,0,0))
        self.assertEqual(db.get_transfer_by_id(self.tid)['status'],'cleaned')
        self.assertIsNone(db.get_transfer_by_id(self.tid)['remote_path'])

    def test_selfheal_corrects_done_without_path_without_changing_library(self):
        db.upsert_transfer('电影','样例 (2026)',0,0,status='done',remote_path=None)
        original=self.path.read_text()
        with patch.object(main.selfheal,'_iter_live_strms',return_value=[]):
            main.selfheal.scan_and_heal(verbose=False)
        self.assertEqual(db.get_transfer_by_id(self.tid)['status'],'failed')
        self.assertEqual(self.path.read_text(),original)

    def test_selfheal_preserves_file_reappearing_before_mutation(self):
        heal=main.selfheal
        remote=db.get_transfer_by_id(self.tid)['remote_path']
        with patch.object(heal,'_iter_live_strms',return_value=[]),patch.object(heal,'_remote_exists_batch',side_effect=[{remote:False},{remote:True}]):
            heal.scan_and_heal(verbose=False)
        self.assertEqual(db.get_transfer_by_id(self.tid)['status'],'done')

    def test_share_task_timeout_is_not_reported_as_success(self):
        tr = QuarkTransfer(Mock())
        tr.resolve_share = Mock(return_value=('synthetic','fixture',[{'fid':'synthetic-file'}]))
        tr._request = Mock(return_value={'code':0,'data':{'task_id':'synthetic-task'}})
        with patch.object(config,'TRANSFER_TIMEOUT',0):
            with self.assertRaises(TransferFailed):
                tr.save_share('https://fixture.invalid/share')


class CleanupScheduleTests(unittest.TestCase):
    def test_missing_jellyfin_database_blocks_actual_and_dry_run_work(self):
        missing = str(Path(TMP.name)/'missing-session-guard.db')
        for dry in (False, True):
            with self.subTest(dry_run=dry), patch.object(config, 'JELLYFIN2_DB', missing), \
                 patch.object(cleanup, 'OpenListClient') as factory, \
                 patch.object(cleanup, 'resolve_quotas', return_value=(100, 70, 100, 1)), \
                 patch.object(cleanup, 'get_vod_disk_usage', return_value=(1, [])):
                with self.assertRaises(coordination.Busy):
                    cleanup.enforce_vod_quota(dry_run=dry, verbose=False)
                factory.assert_not_called()

    def test_default_lock_wait_allows_daily_scan_to_finish_after_six(self):
        import fcntl
        elapsed=[0]
        with tempfile.TemporaryDirectory(dir=TMP.name) as directory:
            path=Path(directory)/'maintenance.lock'
            with path.open('a+') as owner, path.open('a+') as waiter:
                fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)

                def sleep(seconds):
                    elapsed[0]+=seconds
                    # Daily scan completed around 06:26; cleanup starts at 06:00.
                    if elapsed[0]>=26*60:
                        fcntl.flock(owner,fcntl.LOCK_UN)

                with patch.object(cleanup.time,'monotonic',side_effect=lambda:elapsed[0]), \
                     patch.object(cleanup.time,'sleep',side_effect=sleep):
                    self.assertTrue(cleanup._wait_maintenance_lock(waiter))
                self.assertEqual(elapsed[0],26*60)
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)

    def test_lock_wait_timeout_is_bounded_and_does_not_acquire_busy_lock(self):
        import fcntl
        elapsed=[0]
        with tempfile.TemporaryDirectory(dir=TMP.name) as directory:
            path=Path(directory)/'maintenance.lock'
            with path.open('a+') as owner, path.open('a+') as waiter:
                fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with patch.object(cleanup.time,'monotonic',side_effect=lambda:elapsed[0]), \
                     patch.object(cleanup.time,'sleep',side_effect=lambda seconds:elapsed.__setitem__(0,elapsed[0]+seconds)):
                    self.assertFalse(cleanup._wait_maintenance_lock(waiter,timeout=10))
                self.assertEqual(elapsed[0],10)

    def test_cleanup_main_timeout_returns_deferred_without_running_work(self):
        names=('OpenListClient','clean_mismatched_short_dramas','enforce_vod_quota',
               'prune_jellyfin2_orphan_metadata','run_cleanup')
        with ExitStack() as stack:
            stack.enter_context(patch.object(sys,'argv',['cleanup']))
            wait=stack.enter_context(patch.object(cleanup,'_wait_maintenance_lock',return_value=False))
            work=[stack.enter_context(patch.object(cleanup,name)) for name in names]
            self.assertEqual(cleanup.main(),75)
        wait.assert_called_once()
        self.assertTrue(wait.call_args.args[0].closed)
        for function in work:function.assert_not_called()

    def test_cleanup_main_runs_after_lock_and_closes_its_descriptor(self):
        names=('OpenListClient','clean_mismatched_short_dramas','enforce_vod_quota','run_cleanup')
        with ExitStack() as stack:
            stack.enter_context(patch.object(sys,'argv',['cleanup']))
            wait=stack.enter_context(patch.object(cleanup,'_wait_maintenance_lock',return_value=True))
            work=[stack.enter_context(patch.object(cleanup,name)) for name in names]
            pictures=stack.enter_context(patch.object(cleanup,'prune_jellyfin2_orphan_metadata'))
            work[-1].return_value=(0,0,0)
            self.assertEqual(cleanup.main(),0)
        for function in work:function.assert_called_once()
        pictures.assert_not_called()
        self.assertTrue(wait.call_args.args[0].closed)

    def test_cleanup_cron_quota_only_does_not_run_expiry_or_images(self):
        with patch.object(sys, 'argv', ['cleanup', '--enforce-quota']), \
             patch.object(cleanup, 'OpenListClient'), \
             patch.object(cleanup, 'clean_mismatched_short_dramas') as mismatch, \
             patch.object(cleanup, 'enforce_vod_quota') as quota, \
             patch.object(cleanup, 'run_cleanup') as expiry, \
             patch.object(cleanup, 'prune_jellyfin2_orphan_metadata') as pictures:
            self.assertEqual(cleanup.main(), 0)
        mismatch.assert_called_once()
        quota.assert_called_once()
        expiry.assert_not_called()
        pictures.assert_not_called()


def tearDownModule():
    if hasattr(db._local, 'conn'):
        db._local.conn.close()
        del db._local.conn
    TMP.cleanup()


if __name__ == '__main__':
    unittest.main(verbosity=2)
