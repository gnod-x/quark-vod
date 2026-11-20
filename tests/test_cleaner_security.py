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
from quark_vod import cleaner


class CleanerSecurityTests(unittest.TestCase):
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
