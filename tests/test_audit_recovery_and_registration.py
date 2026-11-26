"""Crash recovery and full-scan mapping regressions with synthetic cloud data."""
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

TMP = tempfile.TemporaryDirectory(prefix="vod-audit-recovery-")
os.environ.update(VOD_BASE_DIR=TMP.name, VOD_DATA_DIR=TMP.name + "/data",
                  VOD_CINEMA2_DIR=TMP.name + "/cinema2", AI_PARSE_ENABLED="0",
                  VOD_JELLYFIN_ENABLED="0", SELFHEAL_ENABLED="0")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from quark_vod import config, db, strm_manager
from quark_vod.identity import Work
from quark_vod.transfer import QuarkTransfer, TransferNotReady


class AuditRecoveryAndRegistrationTests(unittest.TestCase):
    def setUp(self):
        db._conn().execute("DELETE FROM strm_map")
        db._conn().execute("DELETE FROM transfers")
        db._conn().commit()

    def test_rename_succeeded_but_done_commit_failed_recovers_exact_saved_fid(self):
        work = Work("电影", "Fixture (2026)", 0, 0)
        parent = config.QUARK_VOD_ROOT + "/vod_recovery"
        original = parent + "/Fixture.mp3"
        repaired = parent + "/Fixture.mkv"
        tid = db.upsert_transfer(*work.args, status="saving", save_target=original,
                                 save_started_at=db.now(), file_size=80 * 1024**2)
        cloud_file = {"name": "Fixture.mp3", "is_dir": False,
                      "fid": "exact-saved-id", "size": 80 * 1024**2}
        unrelated = {"name": "Unrelated.mkv", "is_dir": False,
                     "fid": "another-file-id", "size": 80 * 1024**2}
        transfer = QuarkTransfer(Mock())
        transfer._get_quark_fid = Mock(return_value="slot-id")
        transfer._quark_list_fid = Mock(side_effect=lambda _: [dict(cloud_file), unrelated])
        def rename(*_):
            cloud_file["name"] = "Fixture.mkv"
            return {"path": repaired, **cloud_file}
        transfer.sniff_and_fix_file = Mock(side_effect=rename)
        upsert = db.upsert_transfer
        def fail_done(*args, **fields):
            if fields.get("status") == "done":
                raise sqlite3.OperationalError("synthetic commit failure")
            return upsert(*args, **fields)
        with patch.object(db, "upsert_transfer", side_effect=fail_done):
            with self.assertRaises(sqlite3.OperationalError):
                transfer._resume_save(work, dict(db.get_transfer_by_id(tid)))
        previous = dict(db.get_transfer_by_id(tid))
        self.assertEqual(previous["save_file_fid"], "exact-saved-id")
        self.assertEqual(previous["save_target"], original)
        _, target = transfer._resume_save(work, previous)
        self.assertEqual(target, repaired)
        self.assertEqual(transfer.sniff_and_fix_file.call_count, 1)
        row = db.get_transfer_by_id(tid)
        self.assertEqual(row["status"], "done")
        self.assertIsNone(row["save_file_fid"])

    def test_cleanup_queue_write_failure_cannot_commit_done_or_clear_intent(self):
        work = Work("电影", "Atomic (2026)", 0, 0)
        remote = config.QUARK_VOD_ROOT + "/vod_atomic/Fixture.mkv"
        tid = db.upsert_transfer(*work.args, status="saving", save_target=remote,
                                 save_started_at=db.now(), save_file_fid="atomic-file")
        connection = db._conn()
        connection.execute(
            "CREATE TEMP TRIGGER reject_cleanup BEFORE INSERT ON cleanup_queue "
            "BEGIN SELECT RAISE(FAIL, 'synthetic queue failure'); END"
        )
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                db.complete_transfer(*work.args, remote, 1024, db.now()+86400)
            row = db.get_transfer_by_id(tid)
            self.assertEqual(row["status"], "saving")
            self.assertEqual(row["save_file_fid"], "atomic-file")
            self.assertEqual(row["save_target"], remote)
            self.assertFalse(connection.in_transaction)
        finally:
            connection.execute("DROP TRIGGER reject_cleanup")
        self.assertEqual(db.complete_transfer(*work.args, remote, 1024, db.now()+86400), tid)
        row = db.get_transfer_by_id(tid)
        self.assertEqual(row["status"], "done")
        self.assertIsNone(row["save_file_fid"])
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM cleanup_queue WHERE transfer_id=? AND remote_path=?",
            (tid, remote)).fetchone()[0], 1)

    def test_unknown_disguised_format_keeps_recovery_intent(self):
        work = Work("电影", "Unknown (2026)", 0, 0)
        target = config.QUARK_VOD_ROOT + "/vod_unknown/Unknown.mp3"
        tid = db.upsert_transfer(*work.args, status="saving", save_target=target,
                                 save_started_at=db.now(), file_size=80 * 1024**2)
        transfer = QuarkTransfer(Mock())
        transfer._get_quark_fid = Mock(return_value="slot-id")
        transfer._quark_list_fid = Mock(return_value=[{
            "name": "Unknown.mp3", "is_dir": False, "fid": "unknown-id", "size": 80 * 1024**2,
        }])
        transfer.sniff_and_fix_file = Mock(return_value=None)
        with self.assertRaises(TransferNotReady):
            transfer._resume_save(work, dict(db.get_transfer_by_id(tid)))
        row = db.get_transfer_by_id(tid)
        self.assertNotEqual(row["status"], "done")
        self.assertEqual(row["save_file_fid"], "unknown-id")
        self.assertEqual(row["save_target"], target)

    def test_registration_commits_once_and_prunes_only_missing_library_files(self):
        with tempfile.TemporaryDirectory(prefix="vod-map-scan-") as temporary:
            root = Path(temporary) / "cinema"
            for number in range(20):
                path = root / "电影" / str(number) / (str(number) + ".strm")
                path.parent.mkdir(parents=True)
                path.write_text("http://fixture.invalid/pending")
            outside = str(Path(temporary) / "outside.strm")
            stale = str(root / "电影" / "gone" / "gone.strm")
            db.save_strm_map("电影", "gone", 0, 0, stale)
            db.save_strm_map("外部", "保留", 0, 0, outside)
            trace = []
            db._conn().set_trace_callback(trace.append)
            try:
                with patch.object(config, "CINEMA2_DIR", str(root)):
                    self.assertEqual(strm_manager.scan_and_register(), 20)
            finally:
                db._conn().set_trace_callback(None)
            self.assertEqual(sum(sql == "BEGIN IMMEDIATE" for sql in trace), 1)
            self.assertEqual(sum(sql == "COMMIT" for sql in trace), 1)
            self.assertIsNone(db.get_strm_path("电影", "gone", 0, 0))
            self.assertEqual(db.get_strm_path("外部", "保留", 0, 0), outside)

    def test_incomplete_registration_does_not_prune_or_partially_save(self):
        with tempfile.TemporaryDirectory(prefix="vod-map-incomplete-") as temporary:
            root = Path(temporary)
            season = root / "剧集" / "Fixture" / "Season 01"
            season.mkdir(parents=True)
            (season / "Fixture - S01E01.strm").write_text("pending")
            old = str(root / "剧集" / "old" / "old.strm")
            db.save_strm_map("剧集", "old", 0, 0, old)
            listdir = os.listdir
            def incomplete(path):
                if str(path) == str(season):
                    raise PermissionError("synthetic unreadable season")
                return listdir(path)
            with patch.object(config, "CINEMA2_DIR", str(root)), \
                 patch.object(strm_manager.os, "listdir", side_effect=incomplete):
                with self.assertRaises(PermissionError):
                    strm_manager.scan_and_register()
            self.assertEqual(db.get_strm_path("剧集", "old", 0, 0), old)
            self.assertIsNone(db.get_strm_path("剧集", "Fixture", 1, 1))

    def test_release_group_suffixes_inherit_exact_parent_without_allowing_variants(self):
        transfer = QuarkTransfer(Mock())
        parent = "午夜规则 (2026) 泰剧 1080p"
        for group in ("LeagueWEB", "NTb", "FLUX", "HHWEB", "ADWeb", "FRDS", "CMCT", "WiKi"):
            name = f"The.Rules.S01E01.1080p.WEB-DL.AAC.H.264-{group}.mp4"
            item = {"file_name": name, "relative_path": parent + "/" + name, "size": 1024**3}
            with self.subTest(group=group):
                self.assertIsNotNone(transfer.select_share_video([item], "午夜规则 (2026)", 1, 1))
        for variant in ("Movie", "OVA", "SP", "Special", "Extended"):
            name = f"The.Rules.S01E01.1080p.WEB-DL.AAC.H.264-{variant}.mp4"
            item = {"file_name": name, "relative_path": parent + "/" + name, "size": 1024**3}
            with self.subTest(variant=variant):
                self.assertIsNone(transfer.select_share_video([item], "午夜规则 (2026)", 1, 1))

    def test_explicit_cut_labels_are_not_stripped_as_release_groups(self):
        transfer = QuarkTransfer(Mock())
        variants = (
            "-DirectorsCut", "-DirectorCut", "-Director's.Cut", "-Director’s_Cut",
            ".DIRECTOR.S.CUT-NTb", ".[Director's Cut]-FLUX",
            "-Theatrical", ".[Theatrical Cut]-NTb", "-TheatricalEdition",
            "-Extended", ".[Extended Edition]-FLUX", "-Unrated", "-Uncut",
            "-Version2", ".[Version 2]-NTb",
        )
        for title in ("The.Rules", "午夜规则"):
            for variant in variants:
                name = f"{title}.S01E01.1080p.WEB-DL.AAC.H.264{variant}.mp4"
                item = {"file_name": name, "relative_path": "午夜规则 (2026) 泰剧 1080p/" + name,
                        "size": 1024**3}
                with self.subTest(title=title, variant=variant):
                    self.assertIsNone(transfer.select_share_video([item], "午夜规则 (2026)", 1, 1))

    def test_release_group_boundaries_do_not_reject_unrelated_group_names(self):
        transfer = QuarkTransfer(Mock())
        for group in ("NTb", "FLUX", "SPARKS", "DirectorCutWEB", "TheatricalWEB", "VERSION2WEB"):
            name = f"The.Rules.S01E01.1080p.WEB-DL.AAC.H.264-{group}.mp4"
            item = {"file_name": name, "relative_path": "午夜规则 (2026) 泰剧 1080p/" + name,
                    "size": 1024**3}
            with self.subTest(group=group):
                self.assertIsNotNone(transfer.select_share_video([item], "午夜规则 (2026)", 1, 1))

    def test_version_one_state_upgrades_to_persist_file_identity(self):
        with tempfile.TemporaryDirectory(prefix="vod-v1-upgrade-") as temporary:
            state = str(Path(temporary) / "state.db")
            with sqlite3.connect(state) as connection:
                db._migration_1(connection)
                connection.execute("PRAGMA user_version=1")
                connection.execute("INSERT INTO transfers (cat,title,season,episode,status,created_at,updated_at) "
                                   "VALUES ('电影','legacy',0,0,'saving',1,1)")
            db.close()
            try:
                with patch.object(config, "STATE_DB", state):
                    connection = db.init_db()
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
                    row = connection.execute("SELECT title,save_file_fid FROM transfers").fetchone()
                    self.assertEqual(tuple(row), ("legacy", None))
            finally:
                db.close()


def tearDownModule():
    db.close()
    TMP.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
