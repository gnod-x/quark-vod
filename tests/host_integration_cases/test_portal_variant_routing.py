#!/usr/bin/env python3
"""Catalogue variant regressions; execute only the target function, no services."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from quark_vod.integrations.hometheater import vod_search_and_add as _packaged_portal
SOURCE = Path(_packaged_portal.__file__)
TREE = ast.parse(SOURCE.read_text())


class CatalogueVariantTests(unittest.TestCase):
    def setUp(self):
        detail = {"id": 1, "seasons": [{"season_number": 1, "episode_count": 2}]}
        episodes = {"episodes": [{"episode_number": 1}, {"episode_number": 2}]}
        self.db = SimpleNamespace(validate_variant=Mock(side_effect=self.validate),
                                  get_transfer=Mock(side_effect=self.transfer))
        self.ns = {"db": self.db, "_tmdb": lambda path, **kw: episodes if "/season/" in path else detail.copy(),
                   "_remember": Mock(), "_details": {}, "_summary": lambda obj: {"cat": "TV", "title": "Sample", "year": "2024"},
                   "_library_folder": lambda *args: None,
                   "tmdb_populate": SimpleNamespace(is_episode_aired=lambda *args: True)}
        node = next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == "catalogue")
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), self.ns)

    @staticmethod
    def validate(value):
        if value not in ("", "hdr", "sdr", "dubbed", "remux"):
            raise ValueError("invalid variant")
        return value

    @staticmethod
    def transfer(cat, folder, season, episode, *, variant=""):
        return {"status": "done"} if variant == "hdr" and episode == 2 else None

    def test_saved_status_and_response_use_requested_variant(self):
        for variant, expected in (("hdr", [False, True]), ("sdr", [False, False]), ("", [False, False])):
            with self.subTest(variant=variant):
                self.db.get_transfer.reset_mock()
                result = self.ns["catalogue"]({"id": 1, "media_type": "tv", "season": 1, "variant": variant})
                self.assertEqual(result["variant"], variant)
                self.assertEqual([ep["saved"] for ep in result["episodes"]], expected)
                self.assertTrue(all(call.kwargs == {"variant": variant} for call in self.db.get_transfer.call_args_list))

    def test_invalid_variant_rejected_before_catalogue_calls(self):
        with self.assertRaises(ValueError):
            self.ns["catalogue"]({"id": 1, "media_type": "tv", "variant": "unknown"})
        self.db.get_transfer.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
