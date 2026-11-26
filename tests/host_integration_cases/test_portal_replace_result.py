#!/usr/bin/env python3
"""Replacement completion flags; isolated function execution, no state/services."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from quark_vod.integrations.hometheater import vod_search_and_add as _packaged_portal
SOURCE = Path(_packaged_portal.__file__)
TREE = ast.parse(SOURCE.read_text())


class ReplaceResultTests(unittest.TestCase):
    def run_replacement(self, cleanup_pending):
        work = {"item": {"cat": "TV"}, "detail": {}, "folder": "Sample", "season": 1, "episode": 2, "variant": "hdr"}
        selected = {"work": work, "candidate": object()}
        job = {"replace": True, "reason": "wrong_audio", "key": ("TV", "Sample", 1, 2), "work": work, "variant": "hdr"}
        db = SimpleNamespace(close=Mock(), find_done_transfer=Mock(return_value={"status": "done"}), get_transfer=Mock())
        ns = {"db": db, "coordination": SimpleNamespace(Busy=type("Busy", (Exception,), {})),
              "_slots": SimpleNamespace(release=Mock()), "_core_resolve": Mock(return_value={"ok": True, "cleanup_pending": cleanup_pending}),
              "_refresh_work": Mock(return_value=True), "_recall": lambda *args: job, "_jobs": {},
              "playback_links": Mock(return_value={"play_url": "/play?variant=hdr", "jellyfin_item_id": None})}
        nodes = [node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name in ("_do_add", "job_status")]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), ns)
        ns["_do_add"](selected, job)
        result = ns["job_status"]("fixture")
        ns["_core_resolve"].assert_called_once_with(selected, replace=True, reason="wrong_audio")
        ns["_slots"].release.assert_called_once()
        self.assertEqual(job["stage"], "ready")
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["variant"], "hdr")
        self.assertEqual(result["play_url"], "/play?variant=hdr")
        self.assertEqual(result["cleanup_pending"], cleanup_pending)
        return result

    def test_replacement_ready_after_old_files_cleaned(self):
        result = self.run_replacement(False)
        self.assertEqual(result["message"], "已替换片源并激活播放入口")

    def test_replacement_ready_reports_pending_cleanup_without_hiding_playback(self):
        result = self.run_replacement(True)
        self.assertIn("旧文件清理尚待重试", result["message"])
        self.assertIn("新片源可正常播放", result["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
