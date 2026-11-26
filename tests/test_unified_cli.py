"""CLI dispatch must select paths before importing task or API configuration."""
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from quark_vod.cli import main


class UnifiedCLI(unittest.TestCase):
    def test_jobs_passes_worker_arguments_without_loading_web(self):
        module = types.ModuleType("quark_vod.automation_runner")
        captured = []
        def run(argv):
            captured.append((argv, os.environ.get("VOD_BASE_DIR"), os.environ.get("VOD_ENV_FILE")))
            return 75
        module.cli_main = run
        with patch.dict(sys.modules, {"quark_vod.automation_runner": module}), patch.dict(os.environ, {}, clear=True):
            result = main(["jobs", "--data-dir", "fixture-state", "--env", "fixture.env",
                           "run", "airing_chase", "--execute"])
        self.assertEqual(result, 75)
        self.assertEqual(captured, [(["run", "airing_chase", "--execute"],
                                   os.path.abspath("fixture-state"), os.path.abspath("fixture.env"))])

    def test_global_arguments_before_jobs_are_supported(self):
        module = types.ModuleType("quark_vod.automation_runner")
        module.cli_main = Mock(return_value=0)
        with patch.dict(sys.modules, {"quark_vod.automation_runner": module}), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(main(["--data-dir", "fixture-state", "jobs", "status"]), 0)
            self.assertEqual(os.environ["VOD_BASE_DIR"], os.path.abspath("fixture-state"))
        module.cli_main.assert_called_once_with(["status"])

    def test_serve_rejects_unknown_options(self):
        with self.assertRaises(SystemExit) as stopped:
            main(["serve", "--not-a-real-option"])
        self.assertEqual(stopped.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
