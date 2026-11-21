"""Save-intent reset regressions use only a temporary state DB and STRM."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

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
        for table in ("transfers", "strm_map", "cleanup_queue", "plays", "playback_access", "user_pinned"):
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
