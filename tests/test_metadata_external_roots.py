"""Explicit external image mappings must prove references cannot reach library."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from quark_vod import cleanup, config


class ExternalMetadataRootsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="metadata-mappings-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.library = self.base / "library"
        self.external = self.base / "external"
        self.library.mkdir()
        self.external.mkdir()
        (self.library / "keep.jpg").write_bytes(b"library")
        (self.external / "actor.jpg").write_bytes(b"external")
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)
        self.con.execute("CREATE TABLE BaseItemImageInfos(Path TEXT)")
        patcher = patch.object(config, "JELLYFIN_METADATA_LIBRARY_PREFIX", "/db/library")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.entries = [{"prefix": "/db/people", "root": str(self.external)}]

    def refs(self, *paths, entries=None):
        self.con.execute("DELETE FROM BaseItemImageInfos")
        self.con.executemany("INSERT INTO BaseItemImageInfos VALUES(?)", [(p,) for p in paths])
        with patch.object(config, "JELLYFIN_METADATA_EXTERNAL_ROOTS",
                          json.dumps(self.entries if entries is None else entries)):
            return cleanup._metadata_references(self.con, str(self.library))

    def test_native_env_file_loads_metadata_settings_and_process_env_wins(self):
        keys = ("JELLYFIN_METADATA_LIBRARY_DIR", "JELLYFIN_METADATA_LIBRARY_PREFIX",
                "JELLYFIN_METADATA_EXTERNAL_ROOTS")
        values = (str(self.library), "/db/library", json.dumps(self.entries))
        envfile = self.base / "native.env"
        envfile.write_text("".join(key + "=" + value + "\n" for key, value in zip(keys, values)))
        code = ("import json,sys;sys.path.insert(0," +
                repr(str(Path(__file__).resolve().parents[1] / "app")) +
                ");from quark_vod import config;print(json.dumps([" +
                ",".join("config." + key for key in keys) + "]))")
        env = {k: v for k, v in os.environ.items()
               if k not in keys and k != "VOD_ENV_FILE"}
        env["VOD_ENV_FILE"] = str(envfile)
        def load():
            return json.loads(subprocess.check_output(
                [sys.executable, "-BI", "-c", code], env=env, text=True))
        self.assertEqual(load(), list(values))
        overrides = ("/process/library", "/process/prefix", "[]")
        env.update(zip(keys, overrides))
        self.assertEqual(load(), list(overrides))

    def test_explicit_external_file_is_excluded_and_library_kept(self):
        self.assertEqual(self.refs("/db/library/keep.jpg", "/db/people/actor.jpg"),
                         {str(self.library / "keep.jpg")})

    def test_default_empty_mapping_rejects_other_local_prefix(self):
        with self.assertRaises(cleanup.MetadataCleanupError):
            self.refs("/db/people/actor.jpg", entries=[])

    def test_unknown_prefix_is_still_rejected(self):
        with self.assertRaises(cleanup.MetadataCleanupError):
            self.refs("/unknown/actor.jpg")

    def test_parent_traversal_is_rejected(self):
        with self.assertRaises(cleanup.MetadataCleanupError):
            self.refs("/db/people/../library/keep.jpg")

    def test_symlink_into_library_is_rejected(self):
        (self.external / "escape.jpg").symlink_to(self.library / "keep.jpg")
        with self.assertRaises(cleanup.MetadataCleanupError):
            self.refs("/db/people/escape.jpg")

    def test_missing_path_with_proven_external_boundary_is_excluded(self):
        self.assertEqual(self.refs("/db/people/missing.jpg"), set())
        self.assertEqual(self.refs("/db/people/missing-parent/missing.jpg"), set())

    def test_dangling_link_escape_is_rejected(self):
        for name, target in (
            ("library.jpg", self.library / "missing.jpg"),
            ("outside.jpg", self.base / "outside-missing.jpg"),
        ):
            (self.external / name).symlink_to(target)
            with self.subTest(name=name), self.assertRaises(cleanup.MetadataCleanupError):
                self.refs("/db/people/" + name)

    def test_permission_unknown_is_rejected(self):
        original_stat = os.stat
        def denied(path, *args, **kwargs):
            if str(path) == str(self.external / "actor.jpg"):
                raise PermissionError("synthetic inaccessible reference")
            return original_stat(path, *args, **kwargs)
        with patch("os.stat", side_effect=denied):
            with self.assertRaises(cleanup.MetadataCleanupError):
                self.refs("/db/people/actor.jpg")

    def test_overlapping_prefixes_and_roots_rejected(self):
        nested = self.external / "nested"
        nested.mkdir()
        cases = [
            self.entries + self.entries,
            self.entries + [{"prefix": "/db/people/sub", "root": str(self.base)}],
            [{"prefix": "/db/library/sub", "root": str(self.external)}],
            [{"prefix": "/db/people", "root": str(self.library)}],
            [{"prefix": "/db/people", "root": str(self.base)}],
            self.entries + [{"prefix": "/db/studio", "root": str(nested)}],
        ]
        for entries in cases:
            with self.subTest(entries=entries), self.assertRaises(cleanup.MetadataCleanupError):
                self.refs("/db/library/keep.jpg", entries=entries)

    def test_alias_root_into_library_is_rejected(self):
        alias = self.base / "alias"
        alias.symlink_to(self.library, target_is_directory=True)
        with self.assertRaises(cleanup.MetadataCleanupError):
            self.refs("/db/people/keep.jpg",
                      entries=[{"prefix": "/db/people", "root": str(alias)}])

    def test_invalid_json_or_schema_rejected(self):
        for raw in ('[{"prefix":"/a","prefix":"/b","root":"/tmp"}]', "{", "{}", '[{"prefix":"/x","root":"/missing"}]',
                    '[{"prefix":"/x","root":"/tmp","extra":1}]'):
            with self.subTest(raw=raw), patch.object(config, "JELLYFIN_METADATA_EXTERNAL_ROOTS", raw):
                with self.assertRaises(cleanup.MetadataCleanupError):
                    cleanup._metadata_references(self.con, str(self.library))


if __name__ == "__main__":
    unittest.main(verbosity=2)
