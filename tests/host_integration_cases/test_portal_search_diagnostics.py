"""Search availability is explicit; internal failures never leak provider secrets."""
import ast
from pathlib import Path
from types import SimpleNamespace
import logging
import unittest
from unittest.mock import Mock

from quark_vod.integrations.hometheater import vod_search_and_add as _packaged_portal
SOURCE = Path(_packaged_portal.__file__)
NODE = next(x for x in ast.parse(SOURCE.read_text()).body
            if isinstance(x, ast.FunctionDef) and x.name == 'source_search')


class PortalSearchDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.search = SimpleNamespace(search_report=Mock())
        work = {'item': {'cat': '国产剧集', 'original_title': 'Sample'},
                'season': 1, 'episode': 1, 'folder': '样例 (2026)', 'seasons': [1]}
        self.ns = {'__name__': 'portal_search_diagnostics', 'logging': logging,
                   '_work': lambda _: work, '_aliases': lambda _: (), '_options': lambda _: {},
                   'search': self.search, 'db': SimpleNamespace(
                       get_locked_4k_share=Mock(return_value=(None, False, '')),
                       find_done_transfer=Mock(return_value=None))}
        exec(compile(ast.Module(body=[NODE], type_ignores=[]), str(SOURCE), 'exec'), self.ns)

    def report(self, **extra):
        self.search.search_report.return_value = {'results': [], 'queries': ['样例'],
            'nodes': [], 'partial': True, 'cached': False, **extra}
        return self.ns['source_search']({})

    def test_unconfigured_is_distinct_from_a_completed_empty_search(self):
        result = self.report(configured=False, configuration_error='invalid_configuration')
        self.assertFalse(result['configured'])
        self.assertEqual(result['configuration_error'], 'invalid_configuration')
        complete = self.report(configured=True, partial=False, configuration_error=None)
        self.assertTrue(complete['configured'])
        self.assertFalse(complete['partial'])
        self.assertIsNone(complete['configuration_error'])

    def test_unexpected_failure_is_unknown_and_logs_only_exception_type(self):
        self.search.search_report.side_effect = RuntimeError('private-cookie-and-signed-url')
        with self.assertLogs('portal_search_diagnostics', level='WARNING') as logs:
            result = self.ns['source_search']({})
        self.assertIsNone(result['configured'])
        self.assertTrue(result['partial'])
        self.assertEqual(result['configuration_error'], 'source_search_failed')
        self.assertEqual(result['candidates'], [])
        self.assertIn('RuntimeError', logs.output[0])
        self.assertNotIn('private-cookie', str(result) + str(logs.output))


if __name__ == '__main__':
    unittest.main(verbosity=2)
