"""Security regressions for the public stream cleaner."""
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


TMP = tempfile.TemporaryDirectory(prefix="vod-cleaner-security-")
os.environ.update(VOD_BASE_DIR=TMP.name, VOD_ENV_FILE="", AI_PARSE_ENABLED="0")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from quark_vod import cleaner, download_http


class Response(io.BytesIO):
    def __init__(self, body, status, headers):
        super().__init__(body)
        self.status = status
        self.headers = headers


class CleanerSecurityTests(unittest.TestCase):
    def test_stream_open_uses_configured_download_transport_by_default(self):
        public = "https://media.example.invalid/list"
        response = Response(b"video", 206, {
            "Content-Range": "bytes 0-4/5", "Content-Length": "5",
        })
        with patch.object(cleaner.config, "OPENLIST_PUBLIC", public), \
             patch.object(download_http, "urlopen", return_value=response) as opened:
            stream = cleaner.open_clean_stream(public + "/d/video.mkv", 0, "bytes=0-4")
            self.assertEqual(b"".join(stream.chunks()), b"video")
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(opened.call_args.args[0].full_url, public + "/d/video.mkv")

    def test_obfuscation_probe_uses_configured_download_transport(self):
        public = "https://media.example.invalid/list"
        body = bytes.fromhex(cleaner.MKV_MAGIC_HEX) + b"synthetic-media-data" * 3
        response = Response(body, 206, {"Content-Range": f"bytes 0-{len(body)-1}/{len(body)}"})
        with patch.object(cleaner.config, "OPENLIST_PUBLIC", public), \
             patch.object(download_http, "urlopen", return_value=response) as opened:
            self.assertEqual(cleaner.detect_stream_obfuscation(public + "/d/video.mkv"),
                             (False, 0, "matroska"))
        opened.assert_called_once()

    def test_obfuscation_probe_does_not_fall_back_to_unrestricted_urlopen(self):
        with patch("urllib.request.urlopen") as raw:
            self.assertEqual(cleaner.detect_stream_obfuscation("http://127.0.0.1:9/d/video"),
                             (False, 0, ""))
        raw.assert_not_called()

    def test_obfuscated_direct_url_is_not_written_to_logs(self):
        direct_url = "https://media.example.invalid/d/video.mkv?sign=synthetic-secret"
        output = io.StringIO()
        with patch.object(cleaner, "detect_stream_obfuscation", return_value=(True, 8, "png")), \
             contextlib.redirect_stdout(output):
            result = cleaner.clean_url_if_obfuscated(direct_url)

        self.assertIn("/clean?", result)
        self.assertNotIn(direct_url, output.getvalue())
        self.assertNotIn("synthetic-secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()
