"""Database lifecycle and access accounting regressions use isolated state only."""
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
TMP = tempfile.TemporaryDirectory(prefix='vod-db-lifecycle-')
os.environ.update(VOD_BASE_DIR=TMP.name,VOD_DATA_DIR=str(Path(TMP.name)/'data'),VOD_CINEMA2_DIR=str(Path(TMP.name)/'cinema'),AI_PARSE_ENABLED='0',SELFHEAL_ENABLED='0')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from quark_vod import config, db

class DatabaseLifecycleTests(unittest.TestCase):
    def setUp(self):
        con=db._conn()
        for table in ('plays','playback_access','transfers','strm_map','user_pinned'):
            con.execute('DELETE FROM '+table)
        con.commit()

    def test_range_requests_refresh_protection_without_inflating_sessions(self):
        tid=db.upsert_transfer('电影','Fixture (2026)',0,0,status='done',remote_path='/fixture')
        with patch.object(db,'now',return_value=10000):
            db.record_access(tid)
            db.record_access(tid)
        with patch.object(db,'now',return_value=10001):
            db.record_access(tid)
        self.assertEqual(db.stats()['playback_access_sessions'],1)
        self.assertEqual(db._conn().execute('SELECT accessed_at FROM playback_access').fetchone()[0],10001)
        with patch.object(db,'now',return_value=10001+db.ACCESS_SESSION_SECONDS):
            db.record_access(tid)
        self.assertEqual(db.stats()['plays'],2)
        self.assertEqual(db._conn().execute('SELECT MAX(finished) FROM plays').fetchone()[0],0)

    def test_clock_rollback_cannot_reduce_access_protection_timestamp(self):
        tid=db.upsert_transfer('电影','Clock (2026)',0,0,status='done',remote_path='/fixture')
        with patch.object(db,'now',return_value=20000):db.record_access(tid)
        with patch.object(db,'now',return_value=10000):db.record_access(tid)
        self.assertEqual(db._conn().execute('SELECT accessed_at FROM playback_access').fetchone()[0],20000)
        self.assertEqual(db.stats()['playback_access_sessions'],1)

    def test_automatic_marker_never_overwrites_explicit_pin(self):
        for explicit in ('manual','favorited','unknown-safe-reason'):
            db.pin_item('电影','Fixture (2026)',reason=explicit)
            for automatic in ('preheated','manual_add','played'):
                db.pin_item('电影','Fixture (2026)',reason=automatic)
                self.assertEqual(db._conn().execute('SELECT reason FROM user_pinned').fetchone()[0],explicit)
            self.assertIn('Fixture',db.get_all_pinned_titles())

    def test_legacy_title_mapping_is_one_query_and_preserves_year_identity(self):
        for i in range(20):
            db.upsert_transfer('电影','Other '+str(i),0,0,status='done',remote_path='/other')
        tid=db.upsert_transfer('电影','Fixture',0,0,status='done',remote_path='/fixture')
        db.save_strm_map('电影','Fixture',0,0,str(Path(config.CINEMA2_DIR)/'电影'/'Fixture (2026)'/'main.strm'))
        statements=[]
        db._conn().set_trace_callback(statements.append)
        try:
            self.assertEqual(db.find_done_transfer('电影','Fixture (2026)',0,0)['id'],tid)
        finally:
            db._conn().set_trace_callback(None)
        self.assertEqual(len([q for q in statements if q.startswith('SELECT')]),1)
        self.assertIsNone(db.find_done_transfer('电影','Fixture (2025)',0,0))

    def test_critical_writes_propagate_without_logging_sensitive_values(self):
        bad=Mock()
        bad.execute.side_effect=sqlite3.OperationalError('secret signed-url cookie')
        for operation in (
            lambda: db.save_source_audit('电影','secret title',0,0,True,True,'secret-url'),
            lambda: db.mark_dead_source('secret-url'),
        ):
            with patch.object(db,'_conn',return_value=bad),self.assertLogs(db._log,level='ERROR') as logs:
                with self.assertRaises(sqlite3.OperationalError):operation()
            self.assertNotIn('secret', '\n'.join(logs.output))
        with patch.object(db,'_conn',side_effect=sqlite3.OperationalError('private')):
            with self.assertRaises(sqlite3.OperationalError):db.is_pinned('Fixture')

    def test_audit_write_commit_failure_rolls_back_pending_state(self):
        con=db._conn()
        proxy=Mock(wraps=con)
        proxy.commit.side_effect=sqlite3.OperationalError("private")
        with patch.object(db,"_conn",return_value=proxy),self.assertLogs(db._log,level="ERROR"):
            with self.assertRaises(sqlite3.OperationalError):
                db.save_source_audit("电影","Fixture",0,0,True,True)
        self.assertFalse(con.in_transaction)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM source_audits").fetchone()[0],0)
        db.upsert_transfer("电影","After rollback",0,0,status="done",remote_path="/fixture")

def tearDownModule():
    if hasattr(db._local,'conn'):
        db._local.conn.close()
        del db._local.conn
    TMP.cleanup()
if __name__=='__main__':unittest.main(verbosity=2)
