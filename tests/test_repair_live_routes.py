"""Metadata repair refresh paths follow shared stable/legacy STRM parsers."""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from urllib.parse import quote

_TEMP = tempfile.TemporaryDirectory(prefix="repair-live-routes-")
os.environ.update(VOD_BASE_DIR=_TEMP.name, AI_PARSE_ENABLED="0", SELFHEAL_ENABLED="0")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from quark_vod import config, strm_manager
from quark_vod.repair_metadata import live_refresh_parent

class RepairLiveRoutesTests(unittest.TestCase):
    def test_play_legacy_and_clean_refresh_same_playback_parent(self):
        write = config.QUARK_VOD_ROOT + "/sample/movie.mkv"
        direct = "http://openlist.test/d" + quote(config.play_path(write), safe="/")
        routes = (
            "http://vod.test/play?r=" + quote(write, safe=""),
            direct,
            "http://vod.test/clean?url=" + quote(direct, safe="") + "&skip=8",
        )
        for route in routes:
            with self.subTest(route=route):
                self.assertEqual(live_refresh_parent(route),
                                 os.path.dirname(config.play_path(write)))

    def test_pending_does_not_refresh_remote_directories(self):
        self.assertIsNone(live_refresh_parent(
            config.PENDING_BASE_URL + "/pending?cat=movies&t=sample&s=0&e=0"))

if __name__ == "__main__":
    unittest.main(verbosity=2)
