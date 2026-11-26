"""Save-intent reset regressions use only a temporary state DB and STRM."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

TMP = tempfile.TemporaryDirectory(prefix="vod-reset-intent-")
SEARCH = Path(TMP.name) / "search.json"
SEARCH.write_text(json.dumps({"version": 1, "nodes": []}))
os.environ.update(VOD_BASE_DIR=TMP.name, VOD_DATA_DIR=str(Path(TMP.name) / "data"),
                  VOD_CINEMA2_DIR=str(Path(TMP.name) / "cinema2"),
                  VOD_SEARCH_CONFIG=str(SEARCH), VOD_STARTUP_SCAN="0",
                  VOD_STARTUP_WRITE_PROBE="0", VOD_MAINTENANCE_ENABLED="1",
                  VOD_JELLYFIN_ENABLED="1", AI_PARSE_ENABLED="0", SELFHEAL_ENABLED="0")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import main
from quark_vod import config, coordination, db, strm_manager


class AdminResetIntentTests(unittest.TestCase):
    def setUp(self):
        for table in ("transfers", "strm_map", "cleanup_queue", "plays", "playback_access", "user_pinned", "source_audits"):
            db._conn().execute("DELETE FROM " + table)
        db._conn().commit()
        self.strm = Path(config.CINEMA2_DIR) / "电影" / "样例 (2026)" / "样例.strm"
        self.strm.parent.mkdir(parents=True, exist_ok=True)
        self.live = "http://fixture.invalid/play?transfer=1\n"
        self.strm.write_text(self.live)
        main.legacy_srv._ready_results.clear()
        self.tid = db.upsert_transfer("电影", "样例 (2026)", 0, 0, status="saving",
                                      remote_path=None, save_target="/夸克网盘/点播/vod_fixture/video.mkv",
                                      save_started_at=db.now(), save_task_id=None,
                                      share_url="https://fixture.invalid/share")
        db.save_strm_map("电影", "样例 (2026)", 0, 0, str(self.strm))

    def reset(self):
        with patch.object(coordination, "ensure_idle"):
            return main.admin_reset_transfer(self.tid)

    def seed_force_state(self):
        target = db.get_transfer_by_id(self.tid)["save_target"]
        with db._conn() as con:
            con.execute("INSERT INTO cleanup_queue(transfer_id,remote_path,due_at) VALUES(?,?,?)",
                        (self.tid, target, db.now() + 86400))
        db.save_source_audit("电影", "样例 (2026)", 0, 0, True, False,
                             "https://fixture.invalid/share", "样例", "valid_hd")
        cached = main.legacy_srv.PlaybackResult("http://fixture.invalid/media", self.tid, "/fixture")
        main.legacy_srv._ready_results["target"] = (time.monotonic() + 30, cached)

    def force_state(self):
        tables = ("transfers", "strm_map", "cleanup_queue", "plays", "playback_access", "user_pinned", "source_audits")
        return ({name: [tuple(row) for row in db._conn().execute("SELECT * FROM " + name + " ORDER BY rowid")]
                 for name in tables}, self.strm.read_bytes(), dict(main.legacy_srv._ready_results))

    def assert_force_unconfirmed(self):
        self.seed_force_state()
        before = self.force_state()
        backend = Mock(spec=["_wait_saved_task", "delete_remote"])
        with patch.object(coordination, "ensure_idle"), \
             patch.object(main.legacy_srv, "get_transfer", return_value=backend) as acquire, \
             patch.object(strm_manager, "restore_pending", wraps=strm_manager.restore_pending) as restore:
            response = main.admin_reset_transfer(self.tid, force=1)
        self.assertIsInstance(response, main.JSONResponse)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.body)["code"], "save_intent_unconfirmed")
        acquire.assert_not_called()
        restore.assert_not_called()
        backend._wait_saved_task.assert_not_called()
        backend.delete_remote.assert_not_called()
        self.assertEqual(self.force_state(), before)

    def test_force_reset_missing_task_id_preserves_all_state_and_live_entry(self):
        self.assert_force_unconfirmed()

    def test_force_reset_does_not_expire_unknown_save_intent(self):
        db.upsert_transfer("电影", "样例 (2026)", 0, 0, save_started_at=1)
        self.assert_force_unconfirmed()

    def test_force_reset_failed_label_does_not_prove_unknown_submission_terminal(self):
        db.upsert_transfer("电影", "样例 (2026)", 0, 0, status="failed")
        self.assert_force_unconfirmed()

    def test_force_reset_remote_path_does_not_prove_unknown_submission_terminal(self):
        db.upsert_transfer("电影", "样例 (2026)", 0, 0,
                           remote_path="/夸克网盘/点播/vod_fixture/video.mkv")
        self.assert_force_unconfirmed()

    def test_force_reset_saving_status_without_timestamp_is_still_unconfirmed(self):
        db.upsert_transfer("电影", "样例 (2026)", 0, 0, save_started_at=None)
        self.assert_force_unconfirmed()

    def test_force_reset_unknown_intent_is_rejected_before_backend_availability(self):
        before = self.force_state()
        with patch.object(coordination, "ensure_idle"), \
             patch.object(main.legacy_srv, "get_transfer", return_value=None) as acquire:
            response = main.admin_reset_transfer(self.tid, force=1)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.body)["code"], "save_intent_unconfirmed")
        acquire.assert_not_called()
        self.assertEqual(self.force_state(), before)

    def test_force_reset_waits_for_known_task_before_exact_cleanup(self):
        self.seed_force_state()
        db.upsert_transfer("电影", "样例 (2026)", 0, 0, save_task_id="fixture-task")
        other_id = db.complete_transfer("电影", "其他 (2026)", 0, 0,
                                       "/夸克网盘/点播/vod_other/video.mkv", 1024, db.now() + 86400)
        other_before = dict(db.get_transfer_by_id(other_id))
        other_queue = [tuple(row) for row in db._conn().execute("SELECT * FROM cleanup_queue WHERE transfer_id=?", (other_id,))]
        events = []
        backend = Mock(spec=["_wait_saved_task", "delete_remote"])
        backend._wait_saved_task.side_effect = lambda task_id: events.append(("terminal", task_id))

        def delete_exact(path):
            self.assertTrue(strm_manager.is_pending(self.strm.read_text()))
            self.assertEqual(db.get_transfer_by_id(self.tid)["status"], "saving")
            events.append(("delete", path))

        backend.delete_remote.side_effect = delete_exact
        with patch.object(coordination, "ensure_idle"), patch.object(main.legacy_srv, "get_transfer", return_value=backend):
            result = main.admin_reset_transfer(self.tid, force=1)
        self.assertEqual(result, {"ok": True, "id": self.tid, "strm_restored": True})
        self.assertEqual(events, [("terminal", "fixture-task"), ("delete", "/夸克网盘/点播/vod_fixture/video.mkv")])
        row = db.get_transfer_by_id(self.tid)
        self.assertEqual(row["status"], "cleaned")
        for field in ("remote_path", "save_target", "save_task_id", "save_started_at", "save_file_fid"):
            self.assertIsNone(row[field])
        self.assertEqual(db._conn().execute("SELECT cleaned FROM cleanup_queue WHERE transfer_id=?", (self.tid,)).fetchone()[0], 1)
        self.assertIsNone(db.get_source_audit("电影", "样例 (2026)", 0, 0))
        self.assertNotIn("target", main.legacy_srv._ready_results)
        self.assertEqual(dict(db.get_transfer_by_id(other_id)), other_before)
        self.assertEqual([tuple(item) for item in db._conn().execute("SELECT * FROM cleanup_queue WHERE transfer_id=?", (other_id,))], other_queue)

    def test_force_reset_known_task_not_terminal_preserves_all_state(self):
        self.seed_force_state()
        db.upsert_transfer("电影", "样例 (2026)", 0, 0, save_task_id="fixture-task")
        before = self.force_state()
        backend = Mock(spec=["_wait_saved_task", "delete_remote"])
        backend._wait_saved_task.side_effect = TimeoutError("fixture task still unknown")
        with patch.object(coordination, "ensure_idle"), patch.object(main.legacy_srv, "get_transfer", return_value=backend):
            response = main.admin_reset_transfer(self.tid, force=1)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.body)["code"], "save_intent_unconfirmed")
        backend._wait_saved_task.assert_called_once_with("fixture-task")
        backend.delete_remote.assert_not_called()
        self.assertEqual(self.force_state(), before)

    def test_force_reset_completed_record_without_pending_intent_keeps_exact_cleanup(self):
        db.upsert_transfer("电影", "样例 (2026)", 0, 0, status="done",
                           remote_path="/夸克网盘/点播/vod_fixture/video.mkv",
                           save_target=None, save_started_at=None, save_task_id=None)
        backend = Mock(spec=["_wait_saved_task", "delete_remote"])
        with patch.object(coordination, "ensure_idle"), patch.object(main.legacy_srv, "get_transfer", return_value=backend):
            result = main.admin_reset_transfer(self.tid, force=1)
        self.assertEqual(result, {"ok": True, "id": self.tid, "strm_restored": True})
        backend._wait_saved_task.assert_not_called()
        backend.delete_remote.assert_called_once_with("/夸克网盘/点播/vod_fixture/video.mkv")
        self.assertEqual(db.get_transfer_by_id(self.tid)["status"], "cleaned")

    def test_unconfirmed_save_intent_is_not_cleared_or_retried(self):
        before = dict(db.get_transfer_by_id(self.tid))
        response = self.reset()
        after = dict(db.get_transfer_by_id(self.tid))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.body), {"error": "原转存意图结果未知，已保留原任务和播放入口；请先通过原转存流程恢复并确认终态，待保存意图清除后再重置", "code": "save_intent_unconfirmed"})
        self.assertEqual(after["status"], "saving")
        for field in ("save_target", "save_task_id", "save_started_at", "share_url"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(self.strm.read_text(), self.live)

    def test_terminal_failed_intent_can_be_reset_and_fields_are_null(self):
        db.upsert_transfer("电影", "样例 (2026)", 0, 0, status="failed",
                           remote_path="/夸克网盘/点播/vod_fixture/video.mkv",
                           save_target=None, save_task_id=None, save_started_at=None)
        self.strm.write_text(self.live)
        result = main.legacy_srv.PlaybackResult("http://fixture.invalid/media", self.tid, "/fixture")
        other = main.legacy_srv.PlaybackResult("http://fixture.invalid/other", self.tid + 100, "/other")
        main.legacy_srv._ready_results["target"] = (time.monotonic() + 30, result)
        main.legacy_srv._ready_results["other"] = (time.monotonic() + 30, other)
        response = self.reset()
        self.assertNotIn("target", main.legacy_srv._ready_results)
        self.assertIn("other", main.legacy_srv._ready_results)
        row = db.get_transfer_by_id(self.tid)
        self.assertEqual(response, {"ok": True, "id": self.tid, "strm_restored": True})
        self.assertEqual(row["status"], "cleaned")
        self.assertIsNone(row["remote_path"])
        for field in ("save_target", "save_task_id", "save_started_at"):
            self.assertIsNone(row[field])
        self.assertTrue(strm_manager.is_pending(self.strm.read_text()))


def tearDownModule():
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        del db._local.conn
    TMP.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
