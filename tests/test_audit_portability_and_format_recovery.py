"""Regression tests for portable mount lookup and post-save media repair."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

TMP = tempfile.TemporaryDirectory(prefix="vod-audit-portability-")
os.environ.update(VOD_BASE_DIR=TMP.name, VOD_DATA_DIR=str(Path(TMP.name) / "data"),
                  VOD_CINEMA2_DIR=str(Path(TMP.name) / "cinema2"), AI_PARSE_ENABLED="0",
                  VOD_JELLYFIN_ENABLED="0", VOD_MAINTENANCE_ENABLED="1",
                  OPENLIST_PUBLIC="https://openlist.example/base")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from quark_vod import config, db, strm_manager
from quark_vod.identity import Work
from quark_vod.transfer import QuarkTransfer


class AuditPortabilityAndRepairTests(unittest.TestCase):
    def test_quark_fid_uses_configured_mount_root(self):
        transfer = QuarkTransfer(None)
        transfer._quark_list_fid = Mock(side_effect=[
            [{"name": "vod", "fid": "vod-id"}],
            [{"name": "work", "fid": "work-id"}],
        ])
        with patch.object(config, "QUARK_MOUNT", "/cloud-drive"), \
             patch.object(config, "QUARK_VOD_ROOT", "/cloud-drive/vod"):
            self.assertEqual(transfer._get_quark_fid("/cloud-drive/vod/work"), "work-id")
            self.assertIsNone(transfer._get_quark_fid("/another-drive/vod/work"))
        self.assertEqual(transfer._quark_list_fid.call_count, 2)
        self.assertEqual(transfer._quark_list_fid.call_args_list[0].args, ("0",))

    def test_sniff_refreshes_parent_before_openlist_direct_url(self):
        storage = Mock()
        sequence = Mock()
        sequence.attach_mock(storage.refresh_path_tree, "refresh")
        sequence.attach_mock(storage.direct_url, "direct")
        storage.direct_url.return_value = "https://openlist.example/base/d/file"
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"\x1aE\xdf\xa3" + b"0" * 12
        transfer = QuarkTransfer(storage)
        transfer.rename_quark_file = Mock(return_value=True)
        with patch("quark_vod.download_http.urlopen", return_value=response):
            fixed = transfer.sniff_and_fix_file(
                {"fid": "file-id", "name": "Fixture.mp3", "size": 60 * 1024 * 1024},
                config.QUARK_VOD_ROOT + "/vod_fixture",
            )
        self.assertTrue(fixed["name"].endswith(".mkv"))
        calls = sequence.mock_calls
        self.assertLess(next(i for i, c in enumerate(calls) if c[0] == "refresh"),
                        next(i for i, c in enumerate(calls) if c[0] == "direct"))

    def test_resume_save_repairs_disguised_video_before_marking_done(self):
        transfer = QuarkTransfer(Mock())
        transfer._get_quark_fid = Mock(return_value="slot-id")
        size = 80 * 1024 * 1024
        transfer._quark_list_fid = Mock(return_value=[
            {"name": "Fixture.mp3", "size": size, "is_dir": False, "fid": "saved-id"}
        ])
        original = config.QUARK_VOD_ROOT + "/vod_fixture/Fixture.mp3"
        repaired = config.QUARK_VOD_ROOT + "/vod_fixture/Fixture.mkv"
        transfer.sniff_and_fix_file = Mock(return_value={
            "path": repaired, "name": "Fixture.mkv", "size": size, "fid": "saved-id"
        })
        work = Work("电影", "Fixture (2026)", 0, 0)
        tid, remote = transfer._resume_save(work, {
            "save_target": original, "save_task_id": None, "file_size": size
        })
        transfer.sniff_and_fix_file.assert_called_once()
        self.assertEqual(remote, repaired)
        row = db.get_transfer_by_id(tid)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["remote_path"], repaired)
        self.assertIsNone(row["save_started_at"])

    def test_openlist_strm_detection_uses_configured_origin_and_path(self):
        with patch.object(config, "OPENLIST_PUBLIC", "https://files.example:8443/openlist"), \
             patch.object(config, "OPENLIST_API", "http://api.example:5244"):
            self.assertTrue(strm_manager._is_configured_openlist_download(
                "https://files.example:8443/openlist/d/movie.mkv"))
            self.assertTrue(strm_manager._is_configured_openlist_download(
                "http://api.example:5244/d/movie.mkv"))
            self.assertFalse(strm_manager._is_configured_openlist_download(
                "https://files.example:8443/openlist-other/d/movie.mkv"))
            self.assertFalse(strm_manager._is_configured_openlist_download(
                "https://files.example:8444/openlist/d/movie.mkv"))

    def test_reasoning_effort_is_opt_in_for_compatible_endpoints(self):
        from quark_vod import name_parser
        class Reply:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self):
                return json.dumps({"choices": [{"message": {"content": json.dumps([
                    {"i": 0, "file_name": "S01E01.mkv", "episode": 1, "same": True, "confidence": 1}
                ])}}]}).encode()
        captured = []
        def capture(req, timeout):
            captured.append(json.loads(req.data))
            return Reply()
        for call in (lambda: name_parser._ask_model(["S01E01.mkv"], 1),):
            with patch.object(config, "AI_PARSE_BASE_URL", "https://model.example/v1"), \
                 patch.object(config, "AI_PARSE_MODEL", "test-model"), \
                 patch.object(config, "AI_PARSE_REASONING_EFFORT", ""), \
                 patch.object(config, "load_cliproxy_token", return_value=""), \
                 patch("urllib.request.urlopen", side_effect=capture):
                call()
        self.assertTrue(all("reasoning_effort" not in payload for payload in captured))


def tearDownModule():
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        del db._local.conn
    TMP.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
