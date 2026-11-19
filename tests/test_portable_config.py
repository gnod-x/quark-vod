"""Portable settings, explicit credentials and crash-safe cookie regressions."""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "app/quark_vod/config.py"


class PortableSettingsTests(unittest.TestCase):
    @contextmanager
    def settings(self, env=None, directory=None):
        with tempfile.TemporaryDirectory(prefix="vod-config-test-") as temporary:
            root = Path(directory or temporary)
            previous = os.getcwd()
            try:
                os.chdir(root)
                with patch.dict(os.environ, env or {}, clear=True):
                    spec = importlib.util.spec_from_file_location("isolated_vod_config", SOURCE)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    yield module
            finally:
                os.chdir(previous)

    def test_clean_import_is_portable_and_side_effect_free(self):
        with patch.object(sqlite3, "connect", side_effect=AssertionError("unexpected database read")):
            with self.settings() as cfg:
                self.assertEqual(cfg.BASE_DIR, str(Path.cwd() / "var"))
                self.assertFalse(Path(cfg.BASE_DIR).exists())
                self.assertEqual(cfg.HOST, "127.0.0.1")
                self.assertEqual(cfg.PORT, 8099)
                self.assertEqual(cfg.OPENLIST_DB_PATH, "")
                self.assertFalse(cfg.COOKIE_READY)
                for name in ("VOD_JELLYFIN_ENABLED", "VOD_MAINTENANCE_ENABLED", "VOD_STARTUP_SCAN",
                             "VOD_STARTUP_WRITE_PROBE", "SELFHEAL_ENABLED", "AI_PARSE_ENABLED"):
                    self.assertFalse(getattr(cfg, name), name)
                self.assertEqual(cfg.AI_PARSE_BASE_URL, "")
                self.assertEqual(cfg.AI_PARSE_MODEL, "")
                self.assertEqual(cfg.load_cliproxy_token(), "")

    def test_ambient_dotenv_and_home_token_are_not_discovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("QUARK_COOKIE=ambient-secret\nOPENLIST_ADMIN_PASS=ambient-pass\n")
            token = root / ".config/cliproxy/token"
            token.parent.mkdir(parents=True)
            token.write_text("ambient-token")
            with self.settings({"HOME": directory}, directory) as cfg:
                self.assertEqual(cfg.get_quark_cookie(), "")
                self.assertEqual(cfg.OPENLIST_ADMIN_PASS, "")
                self.assertEqual(cfg.load_cliproxy_token(), "")

    def test_explicit_environment_file_and_process_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "settings.env"
            p.write_text("# example\nexport OPENLIST_ADMIN_USER='operator'\nOPENLIST_ADMIN_PASS=file-pass\nQUARK_COOKIE=file-cookie\nVOD_PORT=8199\n")
            with self.settings({"VOD_ENV_FILE": str(p), "OPENLIST_ADMIN_PASS": "process-pass",
                                "QUARK_COOKIE": ""}) as cfg:
                self.assertEqual(cfg.OPENLIST_ADMIN_USER, "operator")
                self.assertEqual(cfg.OPENLIST_ADMIN_PASS, "process-pass")
                self.assertEqual(cfg.PORT, 8199)
                self.assertEqual(cfg.get_quark_cookie(), "")

    def test_process_secret_file_overrides_file_literal_and_rotates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file = root / "settings.env"
            env_file.write_text("JELLYFIN2_KEY=file-literal\n")
            secret = root / "key"
            secret.write_text("first-key\n")
            with self.settings({"VOD_ENV_FILE": str(env_file), "JELLYFIN2_KEY_FILE": str(secret)}) as cfg:
                self.assertEqual(cfg.get_jellyfin2_key(), "first-key")
                secret.write_text("rotated-key\n")
                self.assertEqual(cfg.get_jellyfin2_key(), "rotated-key")

    def test_missing_secret_file_fails_without_echoing_its_contents(self):
        with self.assertRaisesRegex(ValueError, "VOD_ADMIN_TOKEN_FILE"):
            with self.settings({"VOD_ADMIN_TOKEN_FILE": "/nonexistent/example-secret-file"}):
                pass

    def test_invalid_environment_file_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "bad.env"
            p.write_text("NOT AN ASSIGNMENT\n")
            with self.assertRaisesRegex(ValueError, "line 1"):
                with self.settings({"VOD_ENV_FILE": str(p)}):
                    pass

    def test_paths_and_nonmatching_mount_prefixes(self):
        with self.settings({"VOD_BASE_DIR": "runtime", "VOD_DATA_DIR": "state",
                            "QUARK_MOUNT": "/write", "QUARK_PLAY_MOUNT": "/read",
                            "QUARK_VOD_ROOT": "/write/library", "QUARK_PLAY_ROOT": "/read/media"}) as cfg:
            self.assertEqual(cfg.DATA_DIR, str(Path.cwd() / "runtime/state"))
            self.assertEqual(cfg.play_path("/write/library/a.mp4"), "/read/media/a.mp4")
            self.assertEqual(cfg.write_path("/read/media/a.mp4"), "/write/library/a.mp4")
            self.assertEqual(cfg.play_path("/write-other/a.mp4"), "/write-other/a.mp4")
            self.assertEqual(cfg.write_path(cfg.write_path("/read/media/a.mp4")), "/write/library/a.mp4")

    def test_invalid_flags_numbers_urls_and_mounts_fail(self):
        cases = [("VOD_PORT", "0"), ("TRANSFER_CONCURRENCY", "-1"),
                 ("AI_PARSE_MIN_CONFIDENCE", "nan"), ("SELFHEAL_ENABLED", "maybe"),
                 ("OPENLIST_API", "file:///etc/passwd"), ("OPENLIST_API", "https://user:secret@example.test"),
                 ("PENDING_BASE_URL", "http://example.test?secret=value"),
                 ("QUARK_MOUNT", "/a/../b"), ("VOD_HOST", "")]
        for name, value in cases:
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                with self.settings({name: value}):
                    pass

    def test_cookie_override_is_atomic_private_and_clearable(self):
        with self.settings() as cfg:
            previous = os.umask(0)
            try:
                self.assertEqual(cfg.set_quark_cookie("synthetic-cookie"), "synthetic-cookie")
            finally:
                os.umask(previous)
            target = Path(cfg.COOKIE_OVERRIDE_FILE)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(cfg.get_quark_cookie(), "synthetic-cookie")
            self.assertEqual(list(target.parent.glob(".quark-cookie-*")), [])
            self.assertEqual(cfg.set_quark_cookie(""), "")
            self.assertFalse(target.exists())

    def test_cookie_commit_failure_keeps_old_override(self):
        with self.settings() as cfg:
            cfg.set_quark_cookie("old-synthetic-cookie")
            with patch.object(cfg.os, "replace", side_effect=OSError("synthetic disk failure")):
                with self.assertRaises(OSError):
                    cfg.set_quark_cookie("new-synthetic-cookie")
            self.assertEqual(cfg.get_quark_cookie(), "old-synthetic-cookie")
            self.assertEqual(list(Path(cfg.DATA_DIR).glob(".quark-cookie-*")), [])

    def test_cookie_rejects_header_injection(self):
        with self.settings() as cfg:
            with self.assertRaises(ValueError):
                cfg.set_quark_cookie("synthetic\r\nInjected: yes")
            self.assertFalse(Path(cfg.COOKIE_OVERRIDE_FILE).exists())

    def test_openlist_cookie_requires_opt_in_and_reads_committed_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "openlist.db"
            writer = sqlite3.connect(p)
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("CREATE TABLE x_storages(mount_path TEXT, addition TEXT)")
                writer.commit()
                writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                writer.execute("INSERT INTO x_storages VALUES (?,?)",
                               ("/夸克网盘", json.dumps({"cookie": "synthetic-current-wal"})))
                writer.commit()
                with self.settings() as cfg:
                    self.assertEqual(cfg.get_quark_cookie(), "")
                for explicit in ({"OPENLIST_DB_PATH": str(p)}, {"OPENLIST_DB_PATH": str(p), "QUARK_COOKIE": ""}):
                    with self.settings(explicit) as cfg:
                        self.assertEqual(cfg.get_quark_cookie(), "synthetic-current-wal")
            finally:
                writer.close()

    def test_openlist_invalid_cookie_values_are_not_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "openlist.db"
            with sqlite3.connect(p) as writer:
                writer.execute("CREATE TABLE x_storages(mount_path TEXT, addition TEXT)")
                writer.execute("INSERT INTO x_storages VALUES (?,?)",
                               ("/夸克网盘", json.dumps({"cookie": None})))
            with self.settings({"OPENLIST_DB_PATH": str(p)}) as cfg:
                self.assertEqual(cfg.get_quark_cookie(), "")
                self.assertFalse(cfg.COOKIE_READY)

    def test_no_cookie_retained_after_source_removed(self):
        with self.settings({"QUARK_COOKIE": "synthetic-environment"}) as cfg:
            self.assertEqual(cfg.get_quark_cookie(), "synthetic-environment")
            os.environ.pop("QUARK_COOKIE")
            self.assertEqual(cfg.get_quark_cookie(), "")

    def test_ai_token_file_is_explicit_and_refreshes(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("synthetic-ai-one")
            with self.settings({"AI_PARSE_TOKEN_FILE": str(token)}) as cfg:
                self.assertEqual(cfg.load_cliproxy_token(), "synthetic-ai-one")
                token.write_text("synthetic-ai-two")
                self.assertEqual(cfg.load_cliproxy_token(), "synthetic-ai-two")

    def test_admin_secret_file_rotates_live(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "admin-token"
            token.write_text("synthetic-admin-one")
            with self.settings({"VOD_ADMIN_TOKEN_FILE": str(token)}) as cfg:
                self.assertEqual(cfg.get_admin_token(), "synthetic-admin-one")
                token.write_text("synthetic-admin-two")
                self.assertEqual(cfg.get_admin_token(), "synthetic-admin-two")

    def test_enabled_ai_requires_explicit_provider_and_model(self):
        with self.assertRaisesRegex(ValueError, "AI_PARSE_ENABLED requires"):
            with self.settings({"AI_PARSE_ENABLED": "1"}):
                pass

    def test_cookie_redaction_never_exposes_prefix(self):
        with self.settings() as cfg:
            text = cfg.redact_cookie("private-cookie-never-log")
            self.assertNotIn("private", text)
            self.assertNotIn("23", text)
            self.assertEqual(cfg.redact_cookie(""), "(未配置)")


if __name__ == "__main__":
    unittest.main()
