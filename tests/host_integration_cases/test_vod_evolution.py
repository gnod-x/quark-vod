"""Offline acceptance for incubation, public feeds, authenticated callbacks and skip API."""
import base64
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

TEMP = tempfile.TemporaryDirectory(prefix='vod-evolution-')
os.environ.update(HOMETHEATER_BASE_DIR=TEMP.name, VOD_BASE_DIR=TEMP.name,
    VOD_DATA_DIR=TEMP.name+'/data', VOD_STATE_DB=TEMP.name+'/data/state.db', AI_PARSE_ENABLED='0')
from quark_vod.integrations.hometheater import vod_automation as automation
from quark_vod.integrations.hometheater import wishlist_bot as bot
from quark_vod.integrations.hometheater import douban_watchlist_sync as douban
from quark_vod.integrations.hometheater import skip_manager as skip
from quark_vod import config, db, search
from quark_vod.search import SearchResult


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(dir=TEMP.name)
        self.addCleanup(self.root.cleanup)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(config,'DATA_DIR',self.root.name))
        self.stack.enter_context(patch.dict(os.environ, INTRO_SKIPPER_WRITE_ENABLED='1'))
        self.stack.enter_context(patch('quark_vod.coordination.ensure_idle'))
        self.stack.enter_context(patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden')))

    def candidate(self):
        return SearchResult('panshare','fixture','样例 4K','','',[],share_url='https://pan.quark.cn/s/fixture')

    def test_partial_search_never_overwrites_negative_evidence(self):
        report={'configured':True,'partial':True,'results':[]}
        with patch.object(search,'search_report',return_value=report), patch.object(db,'save_source_audit') as save:
            result=automation.source_evidence('剧集','样例',1,[1],transfer=Mock())
        self.assertFalse(result['complete']);self.assertFalse(result['no_source'])
        save.assert_not_called()

    def test_complete_empty_search_persists_exact_negative(self):
        report={'configured':True,'partial':False,'results':[]}
        with patch.object(search,'search_report',return_value=report), patch.object(db,'save_source_audit') as save:
            result=automation.source_evidence('剧集','样例',2,[3,4],transfer=Mock())
        self.assertTrue(result['no_source'])
        self.assertEqual([c.args[3] for c in save.call_args_list],[3,4])
        self.assertTrue(all(c.kwargs['check_status']=='no_source' for c in save.call_args_list))

    def test_share_failure_keeps_missing_episode_unknown(self):
        report={'configured':True,'partial':False,'results':[self.candidate()]}
        transfer=Mock();transfer.resolve_share.side_effect=TimeoutError()
        with patch.object(search,'search_report',return_value=report),patch.object(search,'rank_candidates',return_value=report['results']),patch.object(db,'save_source_audit') as save:
            result=automation.source_evidence('剧集','样例',1,[1],transfer=transfer)
        self.assertFalse(result['complete']);save.assert_not_called()

    def test_retry_calendar_and_aliases(self):
        today=dt.date(2027,1,1)
        self.assertEqual(automation.retry_delay('2026-12-31',today=today),86400)
        self.assertEqual(automation.retry_delay('2026-11-20',today=today),3*86400)
        self.assertEqual(automation.retry_delay('bad',today=today),7*86400)
        aliases=automation.aliases_from_detail({'original_name':'Original','alternative_titles':{'results':[{'title':'别名'}]},'translations':{'translations':[{'iso_639_1':'zh','data':{'name':'译名'}}]},'seasons':[{'name':'Season 2'},{'name':'新章'}]})
        self.assertEqual(aliases,['Original','别名','译名','新章'])

    def test_douban_only_wish_and_official_feed(self):
        blob=b'<rss><channel><item><title>Wish A</title><guid>1</guid></item><item><title>watched B</title></item></channel></rss>'
        self.assertEqual([x['title'] for x in douban.parse_wishlist(blob)],['A'])
        douban.validate_feed('https://www.douban.com/feed/people/fixture/interests')
        for url in ['http://www.douban.com/feed/people/a/interests','https://evil.test/feed/people/a/interests','https://www.douban.com:444/feed/people/a/interests','https://www.douban.com/feed/people/../interests']:
            with self.assertRaises(ValueError):douban.validate_feed(url)

    def test_skip_api_contract_idempotence_and_conflict(self):
        stored=[]
        def api(method,path,payload=None):
            if method=='GET':return {'Items':list(stored)}
            self.assertTrue(path.startswith('/MediaSegmentsApi/'))
            stored.append(payload);return {}
        with patch.object(skip,'_api',side_effect=api) as request:
            self.assertEqual(skip.write_episode_timestamps(None,None,'episode',op=(0,90)),1)
            self.assertEqual(skip.write_episode_timestamps(None,None,'episode',op=(0,90)),0)
            with self.assertRaises(RuntimeError):skip.write_episode_timestamps(None,None,'episode',op=(0,80))
        self.assertEqual(sum(x.args[0]=='POST' for x in request.call_args_list),1)

    def test_skip_unknown_runtime_has_no_guessed_outro(self):
        with patch.object(skip,'episode_items',return_value=[{'Id':'ep'}]),patch.object(skip,'write_episode_timestamps') as write:
            skip.apply_series_rule('fixture',probe_duration=False)
        self.assertIsNone(write.call_args.args[4])

    def test_telegram_whitelist_and_retry_offset(self):
        updates=[{'update_id':1,'message':{'chat':{'id':8},'text':'bad'}},{'update_id':2,'message':{'chat':{'id':7},'text':'/watch fixture'}}]
        def api(method,data,token):
            return updates if method=='getUpdates' else {}
        with patch.dict(os.environ,TELEGRAM_BOT_TOKEN='fixture',TELEGRAM_ALLOWED_CHAT_IDS='7'),patch.object(bot,'_telegram',side_effect=api),patch.object(bot,'ingest_message',return_value={'status':'pending'}) as ingest:
            result=bot.poll_telegram()
        ingest.assert_called_once_with('fixture',source='telegram')
        self.assertEqual(result['processed'],1)
        self.assertEqual(json.loads((Path(self.root.name)/'telegram_wishlist_offset.json').read_text())['offset'],3)

    def encrypted(self, callback, payload, recipient=None):
        from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
        key=base64.b64decode(callback.aes_key+'=')
        clear=b'0'*16+struct.pack('!I',len(payload))+payload+(recipient or callback.corp_id).encode()
        pad=32-len(clear)%32;clear+=bytes([pad])*pad
        enc=Cipher(algorithms.AES(key),modes.CBC(key[:16])).encryptor()
        encrypted=base64.b64encode(enc.update(clear)+enc.finalize()).decode()
        ts='1000';nonce='fixture'
        signature=hashlib.sha1(''.join(sorted([callback.token,ts,nonce,encrypted])).encode()).hexdigest()
        return signature,ts,nonce,encrypted

    def test_wecom_crypto_recipient_sender_and_replay(self):
        callback=bot.WecomCallback('fixture',base64.b64encode(b'k'*32).decode().rstrip('='),'corp',clock=lambda:1000)
        payload=b'<xml><FromUserName>alice</FromUserName><MsgType>text</MsgType><MsgId>123</MsgId><Content>fixture</Content></xml>'
        signature,ts,nonce,encrypted=self.encrypted(callback,payload)
        envelope=('<xml><Encrypt>'+encrypted+'</Encrypt></xml>').encode()
        with patch.dict(os.environ,WECHAT_ALLOWED_USERS='alice'),patch.object(bot,'ingest_message',return_value={'status':'pending'}) as ingest:
            self.assertEqual(callback.receive(signature,ts,nonce,envelope)['status'],'pending')
            self.assertEqual(callback.receive(signature,ts,nonce,envelope)['status'],'duplicate')
        ingest.assert_called_once()
        with self.assertRaises(PermissionError):callback.decrypt('bad',ts,nonce,encrypted)
        wrong=self.encrypted(callback,payload,'wrong')
        with self.assertRaises(PermissionError):callback.decrypt(*wrong)
        stale=bot.WecomCallback(callback.token,callback.aes_key,callback.corp_id,clock=lambda:2000)
        with self.assertRaises(PermissionError):stale.decrypt(signature,ts,nonce,encrypted)

    def test_staging_publishes_only_proven_episodes(self):
        from quark_vod.integrations.hometheater import tmdb_populate as tm
        cinema=Path(self.root.name)/'cinema';cinema.mkdir()
        item={'id':123,'name':'fixture','first_air_date':'2026-01-01'}
        detail={'seasons':[{'season_number':1},{'season_number':2}]}
        row={'tmdb_id':123,'cat':'series','title':'fixture','media_type':'tv',
             'metadata':{'item':item,'detail':detail}}
        with patch.object(config,'CINEMA2_DIR',str(cinema)),patch.object(tm,'read_tmdb_key',return_value='fixture'),patch.object(tm,'tmdb_object',return_value={'episodes':[{'episode_number':1},{'episode_number':2}]} ) as fetch,patch.object(tm,'is_episode_aired',return_value=True),patch.object(automation,'gate_media',return_value=({2:{1}},True)) as gate,patch.object(tm,'gen_tv_nfo'),patch.object(tm,'gen_season_nfo'),patch.object(tm,'gen_episode_nfo'),patch.object(tm,'download_poster'),patch.object(tm,'download_fanart'),patch.object(db,'save_strm_map'),patch.object(db,'update_staging_media'),patch.object(automation,'notify'),patch.object(tm,'add_pending_strm',side_effect=lambda path,*args: Path(path).write_text('pending') or True):
            automation.publish_staged(row,only_season=2)
        fetch.assert_called_once()
        self.assertEqual(gate.call_args.args[5],{2:[1,2]})
        paths=list(cinema.rglob('*.strm'))
        self.assertEqual(len(paths),1);self.assertIn('S02E01',paths[0].name)

    def test_ghost_unknown_advances_cursor_and_keeps_files(self):
        cinema=Path(self.root.name)/'cinema';(cinema/'cat'/'a').mkdir(parents=True)
        (cinema/'cat'/'b').mkdir()
        with patch.object(config,'CINEMA2_DIR',str(cinema)):
            self.assertEqual(automation.retire_ghosts(limit=1)['unknown'],1)
            first=json.loads((Path(self.root.name)/'ghost_cursor.json').read_text())['after']
            self.assertEqual(automation.retire_ghosts(limit=1)['unknown'],1)
            second=json.loads((Path(self.root.name)/'ghost_cursor.json').read_text())['after']
        self.assertNotEqual(first,second)
        self.assertTrue((cinema/'cat'/'a').is_dir())

    def test_daily_inline_cleanup_does_not_reenter_main(self):
        from quark_vod.integrations.hometheater import daily_update_and_prune as daily
        with patch('quark_vod.cleanup.main') as main,patch('quark_vod.cleanup.clean_mismatched_short_dramas',return_value=0),patch('quark_vod.cleanup.enforce_vod_quota',return_value=(0,0,0)),patch('quark_vod.cleanup.run_cleanup',return_value=None),patch('quark_vod.openlist_client.OpenListClient'):
            self.assertTrue(daily.run_inline_cleanup()['complete'])
        main.assert_not_called()


if __name__=='__main__':unittest.main()
