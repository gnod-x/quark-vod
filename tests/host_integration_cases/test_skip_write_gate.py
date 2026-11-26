"""Unverified Intro Skipper installations must never receive implicit writes."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from quark_vod.integrations.hometheater import skip_manager as skip
class SkipWriteGateTests(unittest.TestCase):
    def test_missing_opt_in_disables_all_automatic_entrypoints(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(skip, 'episode_items', side_effect=AssertionError('inventory forbidden')), \
                patch.object(skip, 'load_presets', side_effect=AssertionError('preset read forbidden')):
            for action in (skip.auto_tag_all, skip.apply_all_presets,
                           lambda: skip.apply_series_rule('fixture')):
                self.assertEqual(action()['status'], 'disabled')

    def test_direct_write_rejected_before_read_or_network(self):
        with patch.dict(os.environ, INTRO_SKIPPER_WRITE_ENABLED='0'), \
                patch.object(skip, '_segments', side_effect=AssertionError('read forbidden')), \
                patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
            with self.assertRaises(PermissionError):
                skip.write_episode_timestamps(None, None, 'fixture', op=(0, 90))
            with self.assertRaises(PermissionError):
                skip._api('POST', '/MediaSegmentsApi/fixture', {})

    def test_disabled_auto_tag_cli_completes_without_plugin(self):
        with patch.dict(os.environ, INTRO_SKIPPER_WRITE_ENABLED='0'), \
                patch.object(skip, 'episode_items', side_effect=AssertionError('plugin absent')):
            self.assertEqual(skip.main(['--server', 'jellyfin', 'auto-tag']), 0)


if __name__ == '__main__':
    unittest.main()
