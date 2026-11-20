"""Keep the local package version aligned with its built distribution."""
from pathlib import Path
import sys
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from quark_vod import __version__


class PackageVersionConsistency(unittest.TestCase):
    def test_package_version_matches_pyproject(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(project["project"]["version"], __version__)


if __name__ == "__main__":
    unittest.main()
