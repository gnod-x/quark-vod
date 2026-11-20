"""Server entrypoints must not log request targets containing signed URLs."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


TMP = tempfile.TemporaryDirectory(prefix="vod-server-logging-")
os.environ.update(VOD_BASE_DIR=TMP.name, VOD_ENV_FILE="", AI_PARSE_ENABLED="0")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from quark_vod import cli, server


class ServerLoggingTests(unittest.TestCase):
    def test_all_supported_entrypoints_disable_uvicorn_access_logs(self):
        for start in (lambda: cli.main([]), server.main):
            with self.subTest(entrypoint=start), patch("uvicorn.run") as run:
                start()
                self.assertFalse(run.call_args.kwargs["access_log"])


if __name__ == "__main__":
    unittest.main()
