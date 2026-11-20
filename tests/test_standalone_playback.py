"""Standalone resolver integration with real SQLite and loopback media HTTP."""
import concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

TMP = tempfile.TemporaryDirectory(prefix='vod-standalone-')
os.environ.update(VOD_BASE_DIR=TMP.name, VOD_DATA_DIR=TMP.name+'/data',
                  VOD_CINEMA2_DIR=TMP.name+'/library', VOD_JELLYFIN_ENABLED='0',
                  VOD_MAINTENANCE_ENABLED='0', AI_PARSE_ENABLED='0')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'app'))
from quark_vod import config, db, server, strm_manager, cleaner, coordination
from quark_vod.identity import Work
config.ensure_dirs()
db.init_db()
MEDIA = b'\x1a\x45\xdf\xa3' + b'synthetic-media' * 20

class Media(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        body = MEDIA[:32] if self.headers.get('Range') else MEDIA
        self.send_response(206 if self.headers.get('Range') else 200)
        self.send_header('Content-Type', 'video/x-matroska')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

class StandaloneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(('127.0.0.1', 0), Media)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.public = 'http://127.0.0.1:' + str(cls.http.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown(); cls.http.server_close(); cls.thread.join()

    def setUp(self):
        self.work = Work('电影', '独立样例 (2026)')
        self.remote = config.QUARK_VOD_ROOT + '/sample/video.mkv'
        self.tid = db.upsert_transfer(*self.work.args, status='done', remote_path=self.remote)
        self.patches = [patch.object(config, 'OPENLIST_PUBLIC', self.public),
                        patch.object(config, 'VOD_JELLYFIN_ENABLED', False),
                        patch.object(server, '_remote_still_exists', return_value=True),
                        patch.object(cleaner, 'clean_url_if_obfuscated', side_effect=lambda u:u)]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()

    def test_first_install_without_strm_returns_actually_readable_url(self):
        self.assertEqual(db._conn().execute('SELECT COUNT(*) FROM strm_map').fetchone()[0], 0)
        with patch.object(strm_manager, 'activate', side_effect=AssertionError('unexpected library mutation')):
            result = server._resolve_once(self.work)
        self.assertEqual(result.transfer_id, self.tid)
        with urllib.request.urlopen(result.url) as response:
            self.assertEqual(response.read(), MEDIA)
        self.assertFalse(list(Path(config.CINEMA2_DIR).rglob('*.strm')))
        self.assertEqual(db._conn().execute('SELECT COUNT(*) FROM playback_access WHERE transfer_id=?', (self.tid,)).fetchone()[0], 1)

    def test_existing_symlink_and_outside_mapping_are_never_written(self):
        outside = Path(TMP.name)/'outside.strm'
        outside.write_text('preserve me')
        Path(config.CINEMA2_DIR).mkdir(parents=True, exist_ok=True)
        link = Path(config.CINEMA2_DIR)/'linked.strm'
        if not link.is_symlink(): link.symlink_to(outside)
        db.save_strm_map(*self.work.args, str(link))
        try:
            server._resolve_once(self.work)
            self.assertTrue(link.is_symlink())
            self.assertEqual(outside.read_text(), 'preserve me')
            db.save_strm_map(*self.work.args, str(outside))
            server._resolve_once(self.work)
            self.assertEqual(outside.read_text(), 'preserve me')
        finally:
            db._conn().execute('DELETE FROM strm_map'); db._conn().commit()
            link.unlink(); outside.unlink()

    def test_enabled_jellyfin_retains_missing_mapping_failure(self):
        with patch.object(config, 'VOD_JELLYFIN_ENABLED', True):
            with self.assertRaises(server.PlaybackUnavailable) as failed:
                server._resolve_once(self.work)
        self.assertEqual(failed.exception.code, 'activation_failed')

    def test_unreadable_saved_file_still_fails_before_access_or_activation(self):
        with patch.object(server, '_wait_direct_url_ready', return_value=False), \
             patch.object(db, 'record_access') as access:
            with self.assertRaises(server.PlaybackUnavailable) as failed:
                server._resolve_once(self.work)
        self.assertEqual(failed.exception.code, 'saved_not_ready')
        access.assert_not_called()

    def test_playback_keeps_shared_lease_and_maintenance_stays_fail_closed(self):
        def probe(*args, **kwargs):
            def exclusive():
                with coordination.file_lock('library', exclusive=True): pass
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                with self.assertRaises(coordination.Busy): pool.submit(exclusive).result()
            return True
        with patch.object(server, '_wait_direct_url_ready', side_effect=probe):
            server._resolve_once(self.work)
        with patch.object(config, 'VOD_MAINTENANCE_ENABLED', True):
            with self.assertRaises(coordination.Busy): coordination.ensure_idle()

if __name__ == '__main__': unittest.main()
