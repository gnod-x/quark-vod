#!/usr/bin/env python3
"""Portal add coordination regressions; uses temporary state and no service calls."""
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

_TEMP_BASE = tempfile.TemporaryDirectory(prefix='portal-add-lock-test-')
BASE = _TEMP_BASE.name
os.environ.update(
    HOMETHEATER_BASE_DIR=BASE,
    VOD_BASE_DIR=BASE,
    VOD_DATA_DIR=str(Path(BASE) / 'data'),
    VOD_STATE_DB=str(Path(BASE) / 'data' / 'state.db'),
    VOD_CINEMA2_DIR=str(Path(BASE) / 'cinema2'),
    AI_PARSE_ENABLED='0',
    VOD_MAINTENANCE_ENABLED='1',
    VOD_JELLYFIN_ENABLED='1',
)
SCRIPTS = Path(__file__).resolve().parents[1]

from quark_vod import config, coordination, db
from quark_vod.integrations.hometheater import vod_search_and_add as portal
class PortalAddCoordinationTests(unittest.TestCase):
    def setUp(self):
        if hasattr(db._local, 'conn'):
            db._local.conn.close()
            del db._local.conn
        self.temp = tempfile.TemporaryDirectory(prefix='portal-add-case-')
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / 'data'
        self.cinema = Path(self.temp.name) / 'cinema2'
        self.db_patch = patch.object(config, 'DATA_DIR', str(self.data))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.state_patch = patch.object(config, 'STATE_DB', str(self.data / 'state.db'))
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.timeout_patch = patch.object(config, 'TRANSFER_TIMEOUT', 0.25)
        self.timeout_patch.start()
        self.addCleanup(self.timeout_patch.stop)
        self.cinema_patch = patch.object(portal, 'CINEMA2', str(self.cinema))
        self.cinema_patch.start()
        self.addCleanup(self.cinema_patch.stop)
        db.init_db()
        con = db._conn()
        for table in ('source_audits', 'strm_map', 'transfers', 'user_pinned'):
            con.execute('DELETE FROM ' + table)
        con.commit()
        portal._details.clear()
        portal._candidates.clear()
        portal._jobs.clear()

    def tearDown(self):
        if hasattr(db._local, 'conn'):
            db._local.conn.close()
            del db._local.conn

    @staticmethod
    def selected():
        work = {
            'item': {'cat': '国产电影', 'title': '样例', 'year': '2021'},
            'detail': {}, 'folder': '样例 (2021)', 'season': 0, 'episode': 0,
        }
        return {'work': work, 'candidate': SimpleNamespace(share_url='https://pan.quark.cn/s/fixture', name='样例')}

    def run_add(self):
        job = {'stage': 'queued'}
        portal._slots.acquire()
        with patch.object(portal, '_inspect', return_value={'quality': {'level': 'hd'}}), \
             patch.object(portal, '_ensure_metadata'), \
             patch.object(portal, '_refresh', return_value=True), \
             patch.object(portal, '_queue_preheat'), \
             patch.object(portal, '_core_resolve', return_value={'ok':True,'state':'ready'}), \
             patch.object(config, 'get_admin_token', return_value='fixture-token'), \
             patch.object(config, 'get_jellyfin2_key', return_value='fixture-key'):
            portal._do_add(self.selected(), job)
        return job

    def test_add_is_allowed_during_shared_playback_lease(self):
        with patch.object(coordination, 'ensure_idle', side_effect=AssertionError('add must not wait for idle')), \
             patch.object(coordination, 'maintenance_window', side_effect=AssertionError('destructive lease used')) as maintenance:
            with coordination.playback_lease():
                job = self.run_add()
        path = Path(job['strm_path'])
        self.assertEqual(job['stage'], 'ready', job)
        self.assertTrue(path.is_file())
        self.assertIn('/pending?', path.read_text())
        maintenance.assert_not_called()

    def test_batch_writer_still_requires_maintenance(self):
        path = self.cinema / "batch.strm"
        path.parent.mkdir(parents=True)
        with patch.object(coordination, 'maintenance_window',
                          side_effect=coordination.Busy('synthetic active playback')):
            with self.assertRaisesRegex(coordination.Busy, 'active playback'):
                portal.tmdb_populate.write_pending_strm(str(path), 'movies', 'sample', 0, 0)
        self.assertFalse(path.exists())

    def test_add_pending_writer_preserves_every_activated_source(self):
        path = self.cinema / "active.strm"
        path.parent.mkdir(parents=True)
        for content in (
            'http://vod.test/play?cat=x&t=sample&r=/example/movie.mkv',
            'http://vod.test/clean?url=synthetic',
            'http://openlist.test/d/example/movie.mkv',
        ):
            with self.subTest(content=content):
                path.write_text(content)
                with patch.object(coordination, 'ensure_idle',
                                  side_effect=AssertionError('non-destructive add')):
                    self.assertFalse(portal.tmdb_populate.add_pending_strm(
                        str(path), 'movies', 'sample', 0, 0))
                self.assertEqual(path.read_text(), content)

    def test_busy_lease_reason_is_returned_without_writing_files(self):
        job = {'stage': 'queued'}
        portal._slots.acquire()
        with patch.object(coordination, 'playback_lease', side_effect=coordination.Busy('synthetic maintenance contention')), \
             patch.object(portal, '_inspect', return_value={'quality': {'level': 'hd'}}):
            portal._do_add(self.selected(), job)
        self.assertEqual(job['stage'], 'failed')
        self.assertEqual(job['message'], 'synthetic maintenance contention')
        self.assertFalse(self.cinema.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
