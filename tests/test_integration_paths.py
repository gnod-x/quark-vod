#!/usr/bin/env python3
"""Explicit integration paths must override legacy host layout."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

APP = str(Path(__file__).resolve().parents[1] / "app")


class IntegrationPathTests(unittest.TestCase):
    def test_explicit_database_is_the_only_cleanup_protection_source(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, PYTHONPATH=APP, VOD_BASE_DIR=directory,
                       JELLYFIN2_DB=os.path.join(directory, "explicit.sqlite"))
            code = """
import os, sqlite3
from unittest.mock import patch
from quark_vod import config, cleanup
assert cleanup.JF_DB == config.JELLYFIN2_DB
legacy = os.path.join(config.BASE_DIR, 'jellyfin2/config/data/jellyfin.db')
os.makedirs(os.path.dirname(legacy))
with sqlite3.connect(legacy) as con:
    con.executescript('CREATE TABLE BaseItems (Id INTEGER, Type TEXT, Name TEXT, SeriesName TEXT); CREATE TABLE UserData (ItemId INTEGER, IsFavorite INTEGER, Played INTEGER, PlayCount INTEGER, PlaybackPositionTicks INTEGER);')
    con.execute("INSERT INTO BaseItems VALUES (1,'Movie','legacy-only',NULL)")
    con.execute("INSERT INTO UserData VALUES (1,1,0,0,0)")
try:
    cleanup.get_protected_titles()
except RuntimeError:
    pass
else:
    raise AssertionError('missing explicit DB must not fall back to legacy DB')
with sqlite3.connect(config.JELLYFIN2_DB) as con:
    con.executescript('CREATE TABLE BaseItems (Id INTEGER, Type TEXT, Name TEXT, SeriesName TEXT); CREATE TABLE UserData (ItemId INTEGER, IsFavorite INTEGER, Played INTEGER, PlayCount INTEGER, PlaybackPositionTicks INTEGER);')
    con.execute("INSERT INTO BaseItems VALUES (1,'Movie','explicit-only',NULL)")
    con.execute("INSERT INTO UserData VALUES (1,1,0,0,0)")
with patch.object(cleanup.db, 'get_all_pinned_titles', return_value=[]):
    assert cleanup.get_protected_titles() == {'explicit-only'}
    with patch.object(cleanup, 'JF_DB', legacy):
        assert cleanup.get_protected_titles() == {'legacy-only'}
"""
            subprocess.run([sys.executable, "-c", code], env=env, check=True)

    def test_library_mapping_uses_custom_root_and_rejects_sibling(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, PYTHONPATH=APP, VOD_BASE_DIR=directory,
                       CINEMA2_DIR=os.path.join(directory, "library"),
                       JELLYFIN_LIBRARY_ROOT="/media/custom")
            code = """
import os
from quark_vod import config, repair_metadata
path = os.path.join(config.CINEMA2_DIR, 'movies', 'sample.strm')
assert repair_metadata.jellyfin_library_path(path) == '/media/custom/movies/sample.strm'
for invalid in (config.CINEMA2_DIR + '-other/sample.strm', os.path.join(config.CINEMA2_DIR, '..', 'outside.strm')):
    try:
        repair_metadata.jellyfin_library_path(invalid)
    except ValueError:
        pass
    else:
        raise AssertionError('sibling/traversal path must not map into library')
"""
            subprocess.run([sys.executable, "-c", code], env=env, check=True)


if __name__ == "__main__":
    unittest.main()
