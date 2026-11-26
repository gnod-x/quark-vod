"""New work notification is scoped to one existing media directory."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import urllib.parse
import urllib.request
from unittest.mock import Mock, patch

from quark_vod.integrations.hometheater import vod_search_and_add as _packaged_portal
SOURCE = Path(_packaged_portal.__file__)
TREE=ast.parse(SOURCE.read_text())


class PortalLibraryNotifyTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='portal-notify-')
        self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name)
        self.root=self.base/'Movies'/'Sample (2024)'
        self.root.mkdir(parents=True)
        (self.root/'Sample.strm').write_text('http://engine.invalid/play')
        self.request=Mock(return_value={})
        self.ident=Mock(return_value=None)
        self.work={'item':{'cat':'Movies','id':42},'folder':'Sample (2024)'}
        self.ns={'Path':Path,'CINEMA2':str(self.base),'urllib':urllib,
            'config':SimpleNamespace(JELLYFIN_LIBRARY_ROOT='/container-library'),
            '_jf_request':self.request,'_jellyfin_id':self.ident}
        nodes=[x for x in TREE.body if isinstance(x,ast.FunctionDef)
               and x.name in ('_refresh_work','_has_library_media')]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),str(SOURCE),'exec'),self.ns)

    def test_new_work_sends_only_its_exact_container_directory(self):
        self.assertTrue(self.ns['_refresh_work'](self.work))
        self.request.assert_called_once_with('/Library/Media/Updated',method='POST',
            payload={'Updates':[{'Path':'/container-library/Movies/Sample (2024)','UpdateType':'Created'}]})

    def test_existing_work_keeps_the_item_specific_refresh(self):
        self.ident.return_value='existing-id'
        self.assertTrue(self.ns['_refresh_work'](self.work))
        self.request.assert_called_once_with('/Items/existing-id/Refresh?Recursive=true',method='POST')

    def test_missing_media_and_missing_folder_never_refresh_a_category(self):
        (self.root/'Sample.strm').unlink()
        self.assertFalse(self.ns['_refresh_work'](self.work))
        self.assertFalse(self.ns['_refresh_work']({'item':{'cat':'Movies','id':42}}))
        self.request.assert_not_called()

    def test_escaped_or_symlink_directory_is_rejected(self):
        self.assertFalse(self.ns['_refresh_work']({**self.work,'folder':'../other'}))
        link=self.base/'Movies'/'Link'
        link.symlink_to(self.root,target_is_directory=True)
        self.assertFalse(self.ns['_refresh_work']({**self.work,'folder':'Link'}))
        self.request.assert_not_called()

    def test_unconfigured_jellyfin_does_not_report_notification_success(self):
        self.request.return_value=None
        self.assertFalse(self.ns['_refresh_work'](self.work))


class JellyfinJsonRequestTests(unittest.TestCase):
    def test_post_payload_uses_json_body_and_content_type(self):
        response=Mock()
        response.read.return_value=b''
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=False)
        node=next(x for x in TREE.body if isinstance(x,ast.FunctionDef) and x.name=='_jf_request')
        ns={'config':SimpleNamespace(JELLYFIN2_API='http://jellyfin.invalid',get_jellyfin2_key=lambda:'test-only'),
            'json':json,'urllib':urllib}
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(SOURCE),'exec'),ns)
        payload={'Updates':[{'Path':'/library/one','UpdateType':'Created'}]}
        with patch.object(urllib.request,'urlopen',return_value=response) as opened:
            self.assertEqual(ns['_jf_request']('/Library/Media/Updated','POST',payload),{})
        request=opened.call_args.args[0]
        self.assertEqual(json.loads(request.data),payload)
        self.assertEqual(request.get_header('Content-type'),'application/json')
        self.assertEqual(request.method,'POST')


class PreservedLibraryNotifyTests(unittest.TestCase):
    def test_saved_reuse_retries_library_notification_only_once_per_job(self):
        job={'key':('Movies','Sample (2024)',0,0),'stage':'preserved',
            'refresh_ok':False,'refresh_attempted':False,'work':{'fixture':True},
            'links':{'play_url':'/play','jellyfin_item_id':None}}
        refresh=Mock(return_value=True)
        ns={'_jobs':{},'_recall':lambda *args:job,
            'db':SimpleNamespace(find_done_transfer=Mock(return_value=None),get_transfer=Mock(return_value=None)),
            '_refresh_work':refresh}
        node=next(x for x in TREE.body if isinstance(x,ast.FunctionDef) and x.name=='job_status')
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(SOURCE),'exec'),ns)
        self.assertTrue(ns['job_status']('same-job')['refresh_ok'])
        self.assertTrue(ns['job_status']('same-job')['refresh_ok'])
        refresh.assert_called_once_with(job['work'])


if __name__=='__main__':
    unittest.main(verbosity=2)
