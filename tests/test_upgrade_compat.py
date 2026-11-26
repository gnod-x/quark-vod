"""Upgrade compatibility regressions for the public SQLite state contract."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from quark_vod import db


LEGACY_TRANSFERS = """
CREATE TABLE transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cat TEXT NOT NULL,
    title TEXT NOT NULL,
    season INTEGER NOT NULL DEFAULT 0,
    episode INTEGER NOT NULL DEFAULT 0,
    share_url TEXT,
    share_pwd TEXT,
    pan_type TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    remote_path TEXT,
    file_size INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    operator_note TEXT
)
"""

RELEASE_SCHEMA = ROOT / "tests" / "fixtures" / "state_v0_3_0_0.sql"
PRE3_SCHEMA = ROOT / "tests" / "fixtures" / "state_v0_pre3.sql"
INCOMPLETE_TRANSFERS = LEGACY_TRANSFERS.replace("    file_size INTEGER DEFAULT 0,\n", "")
RELEASE_ROWS = {
    "transfers": (1, "tv", "Example", 1, 2, "https://example.invalid/share", None,
                  "quark", "saving", None, None, 0, 10, 20, "task-42", "/target", 17),
    "strm_map": ("tv", "Example", 1, 2, "/library/example.strm"),
    "plays": (9, "tv", "Example", 1, 2, 1, 21, 0),
    "playback_access": (1, 22),
    "cleanup_queue": (8, 1, "/target/example.mkv", 99, 0),
    "dead_sources": ("https://example.invalid/dead", "panshare", "expired", 11, 35),
    "source_audits": (3, "tv", "Example", 1, 2, 1, 1, "https://example.invalid/share",
                      "Example 4K", 42, "valid_4k"),
    "user_pinned": (4, "tv", "Example", "played", 53),
    "name_parse_cache": ("example-key", "Example.S01E02.mkv", 1, 2, 0.99, "ai", 61),
}


class UpgradeCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="vod-upgrade-")
        self.state_db = Path(self.directory.name) / "data" / "quark_plays.db"
        self.state_db.parent.mkdir()
        self._old_state_db = db.config.STATE_DB
        self._close_thread_connection()
        db.config.STATE_DB = str(self.state_db)
        self.addCleanup(self._restore_database)

    @staticmethod
    def _close_thread_connection():
        db.close()

    def _restore_database(self):
        self._close_thread_connection()
        db.config.STATE_DB = self._old_state_db
        self.directory.cleanup()

    def _create_legacy_database(self):
        with closing(sqlite3.connect(self.state_db)) as connection, connection:
            connection.executescript(LEGACY_TRANSFERS)
            connection.execute(
                "INSERT INTO transfers "
                "(cat,title,season,episode,status,remote_path,created_at,updated_at,operator_note) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("剧集", "旧记录", 2, 3, "done", "/夸克网盘/点播/旧记录.mkv", 10, 20, "保留"),
            )

    def _create_release_database(self):
        connection = sqlite3.connect(self.state_db)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(RELEASE_SCHEMA.read_text(encoding="utf-8"))
        with connection:
            for table, row in RELEASE_ROWS.items():
                bind = ",".join("?" for _ in row)
                connection.execute(f"INSERT INTO {table} VALUES ({bind})", row)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
        return connection

    def _assert_release_rows(self, connection):
        for table, row in RELEASE_ROWS.items():
            with self.subTest(table=table):
                columns = [column[1] for column in connection.execute(f"PRAGMA table_info({table})")]
                # Migration may append fields or rebuild a primary key. Verify every
                # public release column by name, independently of the physical order.
                with closing(sqlite3.connect(":memory:")) as release:
                    release.executescript(RELEASE_SCHEMA.read_text(encoding="utf-8"))
                    old_columns = [column[1] for column in release.execute(f"PRAGMA table_info({table})")]
                projection = ",".join(old_columns)
                self.assertEqual([tuple(r) for r in connection.execute(f"SELECT {projection} FROM {table}")], [row])
                if "variant" in columns:
                    self.assertEqual(connection.execute(f"SELECT variant FROM {table}").fetchone()[0], "")
                if table == "transfers" and "save_file_fid" in columns:
                    self.assertIsNone(connection.execute("SELECT save_file_fid FROM transfers").fetchone()[0])

    def test_pre3_main_upgrades_without_losing_existing_state(self):
        old_rows = {table: row for table, row in RELEASE_ROWS.items() if table != "playback_access"}
        old_rows["transfers"] = old_rows["transfers"][:-3]
        with closing(sqlite3.connect(self.state_db)) as previous, previous:
            previous.executescript(PRE3_SCHEMA.read_text(encoding="utf-8"))
            columns = {}
            for table, row in old_rows.items():
                columns[table] = [column[1] for column in previous.execute(f"PRAGMA table_info({table})")]
                bind = ",".join("?" for _ in row)
                previous.execute(f"INSERT INTO {table} VALUES ({bind})", row)
            self.assertEqual(previous.execute("PRAGMA user_version").fetchone()[0], 0)

        upgraded = db.init_db()
        self.assertEqual(upgraded.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        for table, row in old_rows.items():
            with self.subTest(table=table):
                projection = ",".join(columns[table])
                self.assertEqual([tuple(record) for record in upgraded.execute(f"SELECT {projection} FROM {table}")], [row])
        self.assertEqual(tuple(upgraded.execute("SELECT save_task_id,save_target,save_started_at FROM transfers").fetchone()),
                         (None, None, None))
        self.assertEqual(upgraded.execute("SELECT COUNT(*) FROM playback_access").fetchone()[0], 0)

    def test_v2_to_v3_preserves_multiple_episode_mappings_and_allows_hdr_sibling(self):
        cat,title='欧美剧集','迁移版本剧 (2026)'
        expected={}
        with closing(sqlite3.connect(self.state_db)) as previous,previous:
            # Freeze the released schema, adding only the shipped v2 file identity column.
            previous.executescript(RELEASE_SCHEMA.read_text(encoding='utf-8'))
            previous.execute('ALTER TABLE transfers ADD COLUMN save_file_fid TEXT')
            previous.execute('PRAGMA user_version=2')
            for episode in (1,2,3):
                path=f'/library/{title}/Season 01/{title} - S01E{episode:02d}.strm'
                remote=f'/夸克网盘/点播/vod_legacy_{episode}/episode.mkv'
                previous.execute(
                    'INSERT INTO transfers(id,cat,title,season,episode,status,remote_path,created_at,updated_at,save_file_fid) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (episode,cat,title,1,episode,'done',remote,10,20,'legacy-fid-'+str(episode)))
                previous.execute('INSERT INTO strm_map(cat,title,season,episode,strm_path) VALUES (?,?,?,?,?)',
                                 (cat,title,1,episode,path))
                previous.execute(
                    'INSERT INTO source_audits(id,cat,title,season,episode,has_quark_source,is_4k,best_share_url,checked_at,check_status) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (episode,cat,title,1,episode,1,0,'https://example.invalid/legacy-'+str(episode),30,'valid_hd'))
                previous.execute('INSERT INTO cleanup_queue(transfer_id,remote_path,due_at) VALUES (?,?,?)',
                                 (episode,remote,100+episode))
                expected[episode]=(path,remote,'legacy-fid-'+str(episode))
        upgraded=db.init_db()
        self.assertEqual(upgraded.execute('PRAGMA user_version').fetchone()[0],3)
        for episode,(path,remote,fid) in expected.items():
            with self.subTest(legacy_episode=episode):
                transfer=db.get_transfer(cat,title,1,episode)
                self.assertEqual((transfer['id'],transfer['variant'],transfer['status'],
                                  transfer['remote_path'],transfer['save_file_fid']),
                                 (episode,'','done',remote,fid))
                self.assertEqual(db.get_strm_path(cat,title,1,episode),path)
                audit=db.get_source_audit(cat,title,1,episode)
                self.assertEqual((audit['variant'],audit['best_share_url']),
                                 ('','https://example.invalid/legacy-'+str(episode)))
                queue=upgraded.execute('SELECT remote_path,due_at FROM cleanup_queue WHERE transfer_id=?',
                                       (episode,)).fetchone()
                self.assertEqual(tuple(queue),(remote,100+episode))
        hdr_path='/library/'+title+'/Season 01/'+title+' - S01E01 - 4K HDR 原声.strm'
        db.save_strm_map(cat,title,1,1,hdr_path,variant='hdr')
        hdr_id=db.upsert_transfer(cat,title,1,1,variant='hdr',status='saving',save_file_fid='hdr-fid')
        db.save_source_audit(cat,title,1,1,True,True,'https://example.invalid/hdr',variant='hdr')
        self.assertNotIn(hdr_id,expected)
        self.assertEqual(db.get_strm_path(cat,title,1,1,variant='hdr'),hdr_path)
        self.assertEqual(db.get_transfer(cat,title,1,1,variant='hdr')['status'],'saving')
        self.assertEqual(db.get_source_audit(cat,title,1,1,variant='hdr')['best_share_url'],
                         'https://example.invalid/hdr')
        for episode,(path,remote,fid) in expected.items():
            self.assertEqual(db.get_strm_path(cat,title,1,episode),path)
            self.assertEqual(db.get_transfer(cat,title,1,episode)['remote_path'],remote)
            self.assertEqual(db.get_source_audit(cat,title,1,episode)['best_share_url'],
                             'https://example.invalid/legacy-'+str(episode))
        self.assertEqual(upgraded.execute('SELECT COUNT(*) FROM strm_map').fetchone()[0],4)
        self.assertEqual(upgraded.execute('SELECT COUNT(*) FROM transfers').fetchone()[0],4)
        self.assertEqual(upgraded.execute('SELECT COUNT(*) FROM source_audits').fetchone()[0],4)

    def test_public_release_upgrades_and_wal_backup_restores_old_version(self):
        previous = self._create_release_database()
        backup_path = self.state_db.with_name("pre-upgrade.db")
        with closing(sqlite3.connect(backup_path)) as backup:
            previous.backup(backup)
        self.assertEqual(db.init_db().execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        self._assert_release_rows(db._conn())
        db.close()
        previous.close()

        restored_path = self.state_db.with_name("restored.db")
        with closing(sqlite3.connect(backup_path)) as backup, closing(sqlite3.connect(restored_path)) as restored:
            backup.backup(restored)
            self.assertEqual(restored.execute("PRAGMA user_version").fetchone()[0], 0)
            self._assert_release_rows(restored)
        db.config.STATE_DB = str(restored_path)
        db.init_db()
        self._assert_release_rows(db._conn())

    def test_two_cold_connections_migrate_only_once(self):
        self._create_release_database()
        barrier = threading.Barrier(2, timeout=10)

        def initialize():
            try:
                barrier.wait()
                return db.init_db().execute("PRAGMA user_version").fetchone()[0]
            finally:
                db.close()

        migration = Mock(wraps=db._migration_1)
        with patch.dict(db._MIGRATIONS, {1: migration}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(initialize) for _ in range(2)]
                self.assertEqual([future.result(timeout=15) for future in futures],
                                 [db.SCHEMA_VERSION] * 2)
            migration.assert_called_once()
        self._assert_release_rows(db._conn())

    def test_invalid_legacy_schema_is_rejected_without_partial_upgrade(self):
        for case, modified in {
            "missing field": INCOMPLETE_TRANSFERS,
            "non-unique pin identity": RELEASE_SCHEMA.read_text(encoding="utf-8").replace(
                "pinned_at INTEGER NOT NULL,\n    UNIQUE(cat, title)", "pinned_at INTEGER NOT NULL"
            ),
        }.items():
            with self.subTest(case=case):
                db.close()
                db.config.STATE_DB = str(self.state_db.with_name(case + ".db"))
                with closing(sqlite3.connect(db.config.STATE_DB)) as connection:
                    connection.executescript(modified)
                    before = connection.execute("SELECT name, sql FROM sqlite_schema ORDER BY name").fetchall()
                with self.assertRaises(db.DatabaseUpgradeError):
                    db.init_db()
                db.close()
                with closing(sqlite3.connect(db.config.STATE_DB)) as connection:
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
                    self.assertEqual(connection.execute("SELECT name, sql FROM sqlite_schema ORDER BY name").fetchall(),
                                     before)

    def test_integrity_check_failure_aborts_before_migration_changes(self):
        self._create_legacy_database()
        check = Mock(side_effect=db.DatabaseUpgradeError("synthetic integrity failure"))
        with patch.object(db, "_validate_database_integrity", check):
            with self.assertRaisesRegex(db.DatabaseUpgradeError, "synthetic integrity failure"):
                db.init_db()

        check.assert_called_once()
        db.close()
        with closing(sqlite3.connect(self.state_db)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(transfers)")}
            self.assertNotIn("save_task_id", columns)
            self.assertEqual(
                connection.execute("SELECT title,status FROM transfers").fetchall(),
                [("旧记录", "done")],
            )

    def test_current_schema_startup_skips_upgrade_integrity_scan(self):
        self._create_legacy_database()
        db.init_db()
        db.close()

        check = Mock(side_effect=AssertionError("current schema should not be scanned"))
        with patch.object(db, "_validate_database_integrity", check):
            db.init_db()
        check.assert_not_called()
        db.close()

    def test_failed_cold_start_never_runs_jobs(self):
        import main

        async def start():
            async with main.lifespan(main.app):
                self.fail("invalid database must prevent lifespan startup")

        for case, setup in {
            "future": f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}",
            "invalid": INCOMPLETE_TRANSFERS,
        }.items():
            with self.subTest(case=case):
                db.close()
                db.config.STATE_DB = str(self.state_db.with_name(case + ".db"))
                with closing(sqlite3.connect(db.config.STATE_DB)) as connection:
                    connection.executescript(setup)
                with (
                    patch.object(main.config, "ensure_dirs"),
                    patch.object(main.config, "VOD_STARTUP_SCAN", True),
                    patch.object(main.config, "SELFHEAL_ENABLED", True),
                    patch.object(main.config, "VOD_STARTUP_WRITE_PROBE", True),
                    patch.object(main.legacy_srv, "_cleanup_stale_transfers") as cleanup,
                    patch.object(main.threading, "Thread") as thread,
                    patch.object(main, "_probe_capability") as probe,
                ):
                    with self.assertRaises(db.DatabaseUpgradeError):
                        asyncio.run(start())
                    cleanup.assert_not_called()
                    thread.assert_not_called()
                    probe.assert_not_called()

    def test_legacy_state_and_unknown_columns_survive_initialization(self):
        self._create_legacy_database()

        db.init_db()
        columns = {row[1] for row in db._conn().execute("PRAGMA table_info(transfers)")}
        row = db._conn().execute(
            "SELECT cat,title,season,episode,status,remote_path,operator_note "
            "FROM transfers WHERE title=?",
            ("旧记录",),
        ).fetchone()

        self.assertTrue({"save_task_id", "save_target", "save_started_at"} <= columns)
        self.assertEqual(
            db._conn().execute("PRAGMA user_version").fetchone()[0],
            db.SCHEMA_VERSION,
        )
        self.assertEqual(
            tuple(row),
            ("剧集", "旧记录", 2, 3, "done", "/夸克网盘/点播/旧记录.mkv", "保留"),
        )
        for table in ("strm_map", "plays", "playback_access", "cleanup_queue", "dead_sources"):
            self.assertIsNotNone(db._conn().execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone(), table)

    def test_initialization_is_idempotent_and_preserves_transfer_count(self):
        self._create_legacy_database()

        db.init_db()
        first = db._conn().execute("SELECT COUNT(*) FROM transfers").fetchone()[0]
        db.init_db()
        second = db._conn().execute("SELECT COUNT(*) FROM transfers").fetchone()[0]

        self.assertEqual((first, second), (1, 1))
        self.assertEqual(
            db._conn().execute("SELECT status FROM transfers WHERE title=?", ("旧记录",)).fetchone()[0],
            "done",
        )

    def test_new_columns_default_to_null_for_legacy_rows(self):
        self._create_legacy_database()

        db.init_db()
        row = db._conn().execute(
            "SELECT save_task_id,save_target,save_started_at FROM transfers WHERE title=?",
            ("旧记录",),
        ).fetchone()

        self.assertEqual(tuple(row), (None, None, None))

    def test_newer_database_version_is_rejected_without_changes(self):
        db.init_db()
        db._conn().execute(
            "INSERT INTO transfers (cat,title,created_at,updated_at) VALUES (?,?,?,?)",
            ("电影", "不应丢失", 30, 40),
        )
        db._conn().commit()
        db.close()

        future_version = db.SCHEMA_VERSION + 1
        with closing(sqlite3.connect(self.state_db)) as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute(f"PRAGMA user_version = {future_version}")
            before_journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            before_schema = connection.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            ).fetchall()
            before_rows = connection.execute(
                "SELECT id,cat,title,created_at,updated_at FROM transfers"
            ).fetchall()

        with self.assertRaises(db.DatabaseUpgradeError):
            db.init_db()
        db.close()

        with closing(sqlite3.connect(self.state_db)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], future_version)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], before_journal_mode)
            self.assertEqual(
                connection.execute(
                    "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
                ).fetchall(),
                before_schema,
            )
            self.assertEqual(
                connection.execute("SELECT id,cat,title,created_at,updated_at FROM transfers").fetchall(),
                before_rows,
            )

    def test_failed_migration_rolls_back_and_can_be_retried(self):
        self._create_legacy_database()
        migration = db._MIGRATIONS[1]

        def fail_after_migration(connection):
            migration(connection)
            raise RuntimeError("injected migration failure")

        with patch.dict(db._MIGRATIONS, {1: fail_after_migration}):
            with self.assertRaisesRegex(RuntimeError, "injected migration failure"):
                db.init_db()

        db.close()
        with closing(sqlite3.connect(self.state_db)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall(),
                [("transfers",)],
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(transfers)")}
            self.assertNotIn("save_task_id", columns)

        db.init_db()
        self.assertEqual(db._conn().execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        self.assertEqual(
            tuple(db._conn().execute("SELECT title,status FROM transfers").fetchall()[0]),
            ("旧记录", "done"),
        )

    def test_concurrent_initialization_preserves_legacy_state(self):
        self._create_legacy_database()
        ready = threading.Barrier(2)

        def initialize():
            try:
                ready.wait(timeout=5)
                db.init_db()
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(initialize) for _ in range(2)]
            for future in futures:
                future.result(timeout=10)

        with closing(sqlite3.connect(self.state_db)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            self.assertEqual(
                connection.execute("SELECT title,status FROM transfers").fetchall(),
                [("旧记录", "done")],
            )

    def test_cross_process_initialization_serializes_migration(self):
        self._create_legacy_database()
        environment = os.environ.copy()
        environment.pop("VOD_STATE_DB", None)
        environment["VOD_BASE_DIR"] = str(self.state_db.parent.parent)
        child = (
            "import sys,time\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from quark_vod import db\n"
            "original = db._migration_1\n"
            "def delayed(connection):\n"
            "    time.sleep(0.25)\n"
            "    original(connection)\n"
            "db._MIGRATIONS[1] = delayed\n"
            "connection = db.init_db()\n"
            "print(connection.execute('PRAGMA user_version').fetchone()[0])\n"
            "db.close()\n"
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-I", "-c", child, str(ROOT / "app")],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        results = [process.communicate(timeout=10) for process in processes]

        self.assertEqual([process.returncode for process in processes], [0, 0], results)
        self.assertEqual([stdout.strip() for stdout, _ in results], [str(db.SCHEMA_VERSION)] * 2)
        self.assertTrue(all(not stderr for _, stderr in results), results)
        with closing(sqlite3.connect(self.state_db)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            self.assertEqual(
                connection.execute("SELECT title,status FROM transfers").fetchall(),
                [("旧记录", "done")],
            )


if __name__ == "__main__":
    unittest.main()
