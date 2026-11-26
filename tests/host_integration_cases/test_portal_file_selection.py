"""Exact raw Quark file identities survive portal enumeration and inspection."""
import ast
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock

from quark_vod.integrations.hometheater import vod_search_and_add as _packaged_portal
SOURCE = Path(_packaged_portal.__file__)
TREE = ast.parse(SOURCE.read_text())


class PortalFileSelectionTests(unittest.TestCase):
    def setUp(self):
        self.raw = {'file_name': 'Sample.2024.1080p.mkv', 'size': 600_000_000,
                    'fid': 'exact-file', 'relative_path': 'Sample (2024)/Sample.2024.1080p.mkv'}
        self.video = {'name': self.raw['file_name'], 'size': self.raw['size'],
                      'share': self.raw.copy()}
        self.transfer = SimpleNamespace(resolve_share=Mock(return_value=('secret', 'share', [])),
            share_files=Mock(return_value=[self.raw]), select_share_video=Mock(return_value=self.video),
            inspect_share_video=Mock(return_value=self.video))
        self.selected = {'work': {'folder': 'Sample (2024)', 'season': 0, 'episode': 0,
                                  'item': {'original_title': 'Sample'}},
                         'candidate': SimpleNamespace(name='Sample', share_url='https://example.invalid/share')}
        self.remember = Mock()
        self.slots = SimpleNamespace(acquire=Mock(return_value=True), release=Mock())
        self.ns = {'_selected': lambda _: self.selected, 'QuarkTransfer': lambda _: self.transfer,
            '_aliases': lambda _: (), '_options': lambda _: {},
            '_remember': self.remember, '_candidates': {}, '_inspect_slots': self.slots,
            'quality_hint': lambda *args: {'level': 'unknown'},
            'secrets': SimpleNamespace(token_urlsafe=lambda _: 'ephemeral-child'), 'time': time}
        nodes = [x for x in TREE.body if isinstance(x, ast.FunctionDef)
                 and x.name in ('list_source_files', '_inspect')]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), self.ns)

    def test_enumeration_uses_selected_normalized_name_and_original_fid(self):
        result = self.ns['list_source_files']({'token': 'parent'})
        self.assertEqual(result['files'][0]['name'], self.raw['file_name'])
        self.assertEqual(result['files'][0]['size'], self.raw['size'])
        child = self.remember.call_args.args[2]
        self.assertEqual(child['selected_fid'], 'exact-file')
        self.assertEqual(child['inspection']['fid'], 'exact-file')
        self.assertNotIn('fid', result['files'][0])
        self.assertNotIn('share_url', result['files'][0])

    def test_inspection_binds_default_selection_to_the_actual_share_file(self):
        result = self.ns['_inspect'](self.selected)
        self.assertEqual(result['fid'], 'exact-file')
        self.assertEqual(result['name'], self.raw['file_name'])
        self.slots.release.assert_called_once()

    def test_enumeration_does_not_publish_an_unaddressable_file(self):
        self.raw.pop('fid')
        self.video['share'].pop('fid')
        self.assertEqual(self.ns['list_source_files']({'token': 'parent'})['files'], [])
        self.remember.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
