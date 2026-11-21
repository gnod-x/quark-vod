"""The package, CLI and HTTP metadata must share one version source."""
import atexit
import os
from pathlib import Path
import io
import sys
import tempfile
import unittest
from html.parser import HTMLParser

ROOT = Path(__file__).resolve().parents[1]
for key in tuple(os.environ):
    if key.startswith(("VOD_", "QUARK_", "OPENLIST_", "JELLYFIN_", "AI_PARSE_", "SELFHEAL_")):
        del os.environ[key]
_TEST_DATA_DIR = tempfile.TemporaryDirectory(prefix="vod-version-")
atexit.register(_TEST_DATA_DIR.cleanup)
os.environ.update(
    VOD_BASE_DIR=_TEST_DATA_DIR.name,
    VOD_STARTUP_SCAN="0",
    VOD_STARTUP_WRITE_PROBE="0",
    SELFHEAL_ENABLED="0",
    AI_PARSE_ENABLED="0",
)
sys.path.insert(0, str(ROOT / "app"))

from contextlib import redirect_stdout
from unittest.mock import patch

from fastapi.testclient import TestClient
from quark_vod import __version__
from quark_vod.cli import main as cli_main
import main as api


class AdminPageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.footer_version = False
        self.footer_text = []
        self.scripts = []
        self._inside_footer_version = False
        self._inside_script = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self._inside_footer_version = tag == "span" and attrs.get("id") == "footer-version"
        self._inside_script = tag == "script"
        self.footer_version |= self._inside_footer_version

    def handle_endtag(self, tag):
        if tag == "span":
            self._inside_footer_version = False
        elif tag == "script":
            self._inside_script = False

    def handle_data(self, data):
        if self._inside_footer_version:
            self.footer_text.append(data)
        if self._inside_script:
            self.scripts.append(data)


class VersionConsistency(unittest.TestCase):
    def test_fastapi_and_health_use_package_version(self):
        self.assertEqual(api.app.version, __version__)
        self.assertEqual(api.app.openapi()["info"]["version"], __version__)
        with TestClient(api.app) as client:
            self.assertEqual(client.get("/health").json()["version"], __version__)

    def test_admin_footer_displays_health_version(self):
        with TestClient(api.app) as client:
            health_version = client.get("/health").json()["version"]
            response = client.get("/admin")
        self.assertEqual(response.status_code, 200)
        page = AdminPageParser()
        page.feed(response.text)
        self.assertTrue(page.footer_version, "admin page should provide a footer version target")
        self.assertEqual("".join(page.footer_text), "Quark VOD")
        self.assertNotRegex(response.text, r"Quark VOD\s+\d+\.\d+\.\d+")
        script = "\n".join(page.scripts)
        self.assertIn("fetch('/health'", script)
        self.assertIn("byId('footer-version').textContent='Quark VOD '+(data.version||'')", script)
        self.assertEqual(health_version, __version__)

    def test_cli_uses_package_version(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["quark-vod", "--version"]), redirect_stdout(output):
            with self.assertRaises(SystemExit) as stopped:
                cli_main()
        self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"Quark VOD {__version__}")

    def test_pyproject_declares_dynamic_version(self):
        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertIn('dynamic = ["version"]', pyproject)
        self.assertIn('version = {attr = "quark_vod.__version__"}', pyproject)
        self.assertNotIn('version = "3.0.0"', pyproject)

    def test_container_and_compose_version_defaults_match_package(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        compose = (ROOT / "docker-compose.yml").read_text()
        self.assertIn(f"ARG VOD_VERSION={__version__}", dockerfile)
        self.assertIn(f"QUARK_VOD_IMAGE_VERSION:-{__version__}", compose)
        self.assertIn('VOD_VERSION: "${QUARK_VOD_IMAGE_VERSION:-' + __version__ + '}"', compose)
        self.assertIn("org.opencontainers.image.version=", dockerfile)


if __name__ == "__main__":
    unittest.main()
