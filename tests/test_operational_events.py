"""Fault events stay redacted; all notification transports are intercepted."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch
import urllib.error

TMP=tempfile.TemporaryDirectory(prefix='vod-ops-events-')
os.environ.update(VOD_BASE_DIR=TMP.name,VOD_DATA_DIR=TMP.name+'/data',VOD_JELLYFIN_ENABLED='0')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from quark_vod import operational_events,server,db,cleanup
from quark_vod.transfer import QuarkTransfer,TransferFailed
from quark_vod.quark_accounts import bucket

class Response:
    headers={}
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,n):return json.dumps({'code':14018,'message':'secret-cookie https://private.invalid/token'}).encode()

class OperationalEventTests(unittest.TestCase):
    def test_all_events_only_deliver_fixed_text_and_exception_class(self):
        with patch.object(operational_events._executor,'submit',side_effect=lambda fn:fn()),patch.object(
                operational_events.notifier,'notify',return_value={}) as notify:
            for event in operational_events._EVENTS:
                self.assertTrue(operational_events.emit(event,RuntimeError('secret-cookie https://private.invalid/token')))
            self.assertEqual(notify.call_count,5)
            for call in notify.call_args_list:
                payload=str(call)
                self.assertNotIn('secret-cookie',payload)
                self.assertNotIn('private.invalid',payload)
                self.assertIn('RuntimeError',payload)
            self.assertEqual(notify.call_args_list[0].args[3],'P0')
            self.assertEqual(notify.call_args_list[-1].args[3],'P1')

    def test_risk_response_and_main_http_auth_emit_without_upstream_details(self):
        tr=QuarkTransfer()
        tr.get_cookie=Mock(return_value='main=secret')
        with patch.object(operational_events,'emit') as emit,patch.object(
                bucket,'acquire',return_value=True),patch('urllib.request.urlopen',return_value=Response()):
            self.assertEqual(tr._request('https://drive.quark.cn/fixture')['code'],14018)
            emit.assert_called_once_with('quark_risk_control')
        error=urllib.error.HTTPError('https://private.invalid/token',401,'secret-cookie',{},io.BytesIO())
        with patch.object(operational_events,'emit') as emit,patch.object(
                bucket,'acquire',return_value=True),patch('urllib.request.urlopen',side_effect=error):
            with self.assertRaises(TransferFailed):tr._request('https://drive.quark.cn/fixture')
            self.assertEqual(emit.call_args.args[0],'main_cookie_invalid')

    def test_capacity_and_probe_failure_have_separate_events(self):
        tr=QuarkTransfer()
        tr._request=Mock(return_value={'data':{'total_capacity':1024,'use_capacity':1024}})
        with patch.object(operational_events,'emit') as emit:
            self.assertEqual(tr._check_capacity(2048),(False,0))
            emit.assert_called_once_with('quark_quota_exhausted')
            emit.reset_mock()
            self.assertFalse(server._wait_direct_url_ready('https://private.invalid/token',max_wait=0))
            emit.assert_called_once_with('playback_backend_failed')


    def test_quota_protection_block_alerts_but_dry_run_stays_quiet(self):
        gb=1024**3
        with patch.object(cleanup,'get_vod_disk_usage',return_value=(100*gb,[])),patch.object(
                cleanup,'resolve_quotas',return_value=(850*gb,700*gb,1000*gb,900*gb)),patch.object(
                operational_events,'emit') as emit:
            result=cleanup.enforce_vod_quota.__wrapped__(ol=Mock(),verbose=False)
            self.assertEqual(result,(0,0,100*gb))
            emit.assert_called_once_with('quota_cleanup_blocked')
            emit.reset_mock()
            cleanup.enforce_vod_quota.__wrapped__(ol=Mock(),dry_run=True,verbose=False)
            emit.assert_not_called()

    def test_cookie_health_failure_returns_only_exception_class(self):
        tr=QuarkTransfer()
        tr.get_cookie=Mock(return_value='main=secret')
        tr._request=Mock(side_effect=OSError('secret-cookie'))
        with patch.object(operational_events,'emit') as emit:
            alive,message=tr.check_cookie_alive()
            self.assertFalse(alive)
            self.assertNotIn('secret-cookie',message)
            self.assertIn('OSError',message)
            self.assertEqual(emit.call_args.args[0],'main_cookie_invalid')

def tearDownModule():
    db.close()
    TMP.cleanup()

if __name__=='__main__':unittest.main(verbosity=2)
