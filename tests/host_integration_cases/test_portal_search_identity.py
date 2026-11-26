"""Search-only TMDB fields cannot misclassify a movie or its library presence."""
import ast
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock

from quark_vod.integrations.hometheater import vod_search_and_add as _packaged_portal
SOURCE = Path(_packaged_portal.__file__)
TREE = ast.parse(SOURCE.read_text())


class PortalSearchIdentityTests(unittest.TestCase):
    def setUp(self):
        self.item = {'id': 42, 'title': 'Sample', 'media_type': 'movie'}
        self.detail = {**self.item, 'production_countries': [{'iso_3166_1': 'CN'}]}
        self.cache = {}
        self.tmdb = Mock(return_value=self.detail)
        self.is_ai_content = Mock(return_value=False)
        self.summary = Mock(side_effect=lambda item: {'cat': self.ns['guess_category'](item),
            'in_library': self.ns['guess_category'](item) == '国产电影'})
        self.ns = {'_details': self.cache, '_recall': lambda cache,key: cache.get(key),
            '_remember': lambda cache,key,value: cache.__setitem__(key,value),
            '_tmdb': self.tmdb, '_summary': self.summary,
            'tmdb_populate': SimpleNamespace(is_ai_content=self.is_ai_content,
                fmt_title=lambda item: item.get('title')),
            '_metadata_slots': threading.BoundedSemaphore(2),
            '_metadata_pool': SimpleNamespace(map=lambda fn,items: map(fn,items))}
        nodes = [x for x in TREE.body if isinstance(x, ast.FunctionDef)
                 and x.name in ('guess_category', '_search_summary', 'search_all')]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), self.ns)

    def test_movie_search_enriches_country_before_category_and_library_lookup(self):
        result = self.ns['_search_summary'](self.item)
        self.assertEqual(result, {'cat': '国产电影', 'in_library': True})
        self.assertEqual(self.summary.call_args.args[0]['production_countries'], self.detail['production_countries'])
        self.ns['_search_summary'](self.item)
        self.tmdb.assert_called_once_with('movie/42', append_to_response='keywords,alternative_titles,translations')

    def test_existing_country_and_tv_results_do_not_fetch_extra_details(self):
        for item in (self.detail, {**self.item, 'media_type': 'tv', 'origin_country': ['CN']}):
            self.ns['_search_summary'](item)
        self.tmdb.assert_not_called()

    def test_mismatched_detail_identity_is_rejected(self):
        self.tmdb.return_value = {**self.detail, 'id': 99}
        with self.assertRaises(ValueError):
            self.ns['_search_summary'](self.item)
        self.summary.assert_not_called()
        self.assertEqual(self.cache, {})

    def test_search_limits_enrichment_to_twenty_results_and_preserves_order(self):
        items = [{**self.item, 'id':n, 'production_countries':[{'iso_3166_1':'CN'}]} for n in range(25)]
        self.tmdb.return_value = {'results':items}
        self.summary.side_effect = lambda item: item['id']
        self.assertEqual(self.ns['search_all']('Sample'),list(range(20)))
        self.tmdb.assert_called_once_with('search/multi',query='Sample',include_adult='false')

    def test_metadata_overload_is_rejected_before_network_requests(self):
        slots = SimpleNamespace(acquire=Mock(return_value=False), release=Mock())
        self.ns['_metadata_slots'] = slots
        with self.assertRaisesRegex(ValueError, '查询繁忙'):
            self.ns['search_all']('Sample')
        slots.acquire.assert_called_once_with(timeout=1)
        slots.release.assert_not_called()
        self.tmdb.assert_not_called()

    def test_metadata_batch_slot_is_released_after_failure(self):
        slots = threading.BoundedSemaphore(1)
        self.ns['_metadata_slots'] = slots
        self.tmdb.side_effect = ValueError('metadata unavailable')
        with self.assertRaises(ValueError):
            self.ns['search_all']('Sample')
        self.assertTrue(slots.acquire(blocking=False))
        slots.release()


if __name__ == '__main__':
    unittest.main(verbosity=2)
