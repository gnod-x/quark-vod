"""Offline wishlist configuration boundaries; all message/network work is synthetic."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory(prefix='wishlist-explicit-')
os.environ.update(VOD_BASE_DIR=TEMP.name, VOD_DATA_DIR=TEMP.name, VOD_STATE_DB=TEMP.name+'/state.db', AI_PARSE_ENABLED='0')
from quark_vod.integrations.hometheater import wishlist_bot as bot
from quark_vod import config


class WishlistExplicitConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=TEMP.name)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch.object(config, '_ENV_FILE_VALUES', {}))
        self.stack.enter_context(patch.object(config, 'DATA_DIR', str(self.root)))
        self.stack.enter_context(patch.object(bot, 'file_lock', return_value=__import__('contextlib').nullcontext()))
        self.network = self.stack.enter_context(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))
        self.api = self.stack.enter_context(patch.object(bot, '_telegram', return_value=[]))
        self.ingest = self.stack.enter_context(patch.object(bot, 'ingest_message', return_value={'status':'pending'}))

    def env_file(self, text):
        path = self.root/'explicit.env'; path.write_text(text)
        os.environ['VOD_ENV_FILE'] = str(path)
        config._ENV_FILE_VALUES = config._load_env_file()

    def test_telegram_explicit_file_credentials_and_whitelist(self):
        self.env_file('TELEGRAM_BOT_TOKEN=fixture-token\nTELEGRAM_ALLOWED_CHAT_IDS=7\n')
        updates=[{'update_id':1,'message':{'chat':{'id':8},'text':'forbidden'}}, {'update_id':2,'message':{'chat':{'id':7},'text':'/watch accepted'}}]
        self.api.side_effect = [updates, {}]
        result = bot.poll_telegram()
        self.assertEqual(result, {'configured':True,'processed':1,'errors':0})
        self.ingest.assert_called_once_with('accepted', source='telegram')
        self.assertEqual(self.api.call_args_list[0].args[2], 'fixture-token')
        self.assertEqual(json.loads((self.root/'telegram_wishlist_offset.json').read_text())['offset'],3)

    def test_telegram_secret_file(self):
        secret=self.root/'token'; secret.write_text('file-token\n')
        self.env_file('TELEGRAM_BOT_TOKEN_FILE='+str(secret)+'\nTELEGRAM_ALLOWED_CHAT_IDS=7\n')
        self.assertTrue(bot.poll_telegram()['configured'])
        self.assertEqual(self.api.call_args.args[2], 'file-token')
        self.assertNotIn('TELEGRAM_BOT_TOKEN', os.environ)

    def test_process_empty_token_disables_file_token(self):
        self.env_file('TELEGRAM_BOT_TOKEN=fixture\nTELEGRAM_ALLOWED_CHAT_IDS=7\n')
        os.environ['TELEGRAM_BOT_TOKEN']=''
        self.assertFalse(bot.poll_telegram()['configured']); self.api.assert_not_called()

    def test_process_secret_file_overrides_explicit_file_direct_token(self):
        secret=self.root/'override-token'; secret.write_text('override-file-token')
        self.env_file('TELEGRAM_BOT_TOKEN=file-direct-token\nTELEGRAM_ALLOWED_CHAT_IDS=7\n')
        os.environ['TELEGRAM_BOT_TOKEN_FILE']=str(secret)
        self.assertTrue(bot.poll_telegram()['configured'])
        self.assertEqual(self.api.call_args.args[2], 'override-file-token')

    def test_process_empty_whitelist_disables_file_whitelist(self):
        self.env_file('TELEGRAM_BOT_TOKEN=fixture\nTELEGRAM_ALLOWED_CHAT_IDS=7\n')
        os.environ['TELEGRAM_ALLOWED_CHAT_IDS']=''
        self.assertFalse(bot.poll_telegram()['configured']); self.api.assert_not_called()

    def test_invalid_secret_file_fails_before_network(self):
        self.env_file('TELEGRAM_BOT_TOKEN_FILE='+str(self.root/'missing')+'\nTELEGRAM_ALLOWED_CHAT_IDS=7\n')
        with self.assertRaisesRegex(ValueError,'TELEGRAM_BOT_TOKEN_FILE'): bot.poll_telegram()
        self.api.assert_not_called(); self.network.assert_not_called()

    def test_no_implicit_dotenv_or_notify_fallback(self):
        (self.root/'.env').write_text('TELEGRAM_BOT_TOKEN=fixture\nTELEGRAM_ALLOWED_CHAT_IDS=7\n')
        os.environ.update(NOTIFY_TELEGRAM_BOT_TOKEN='notify',NOTIFY_TELEGRAM_CHAT_ID='7')
        with patch('os.getcwd', return_value=str(self.root)):
            self.assertFalse(bot.poll_telegram()['configured'])
        self.api.assert_not_called()

    def test_wecom_secret_files_and_explicit_values(self):
        token=self.root/'callback-token'; token.write_text('callback-file')
        key=self.root/'aes-key'; key.write_text('aes-file')
        self.env_file('WECHAT_CALLBACK_TOKEN_FILE='+str(token)+'\nWECHAT_ENCODING_AES_KEY_FILE='+str(key)+'\nWECHAT_CORP_ID=corp-file\nWECHAT_ALLOWED_USERS=alice\n')
        callback=bot.WecomCallback()
        self.assertEqual((callback.token,callback.aes_key,callback.corp_id),('callback-file','aes-file','corp-file'))
        payload=b'<xml><Encrypt>fixture</Encrypt></xml>'
        with patch.object(callback,'decrypt',return_value=b'<xml><FromUserName>bob</FromUserName><MsgType>text</MsgType></xml>'):
            with self.assertRaisesRegex(PermissionError,'sender'): callback.receive('s','1','n',payload)
        with patch.object(callback,'decrypt',return_value=b'<xml><FromUserName>alice</FromUserName><MsgType>image</MsgType></xml>'):
            self.assertEqual(callback.receive('s','1','n',payload),{'status':'ignored'})
        self.ingest.assert_not_called()

    def test_wecom_process_empty_values_win(self):
        self.env_file('WECHAT_CALLBACK_TOKEN=token\nWECHAT_ENCODING_AES_KEY=key\nWECHAT_CORP_ID=corp\nWECHAT_ALLOWED_USERS=alice\n')
        os.environ.update(WECHAT_CALLBACK_TOKEN='',WECHAT_ENCODING_AES_KEY='',WECHAT_CORP_ID='',WECHAT_ALLOWED_USERS='')
        self.assertFalse(bot.WecomCallback().configured)
        callback=bot.WecomCallback('explicit','explicit','explicit')
        with patch.object(callback,'decrypt',return_value=b'<xml><FromUserName>alice</FromUserName><MsgType>image</MsgType></xml>'):
            with self.assertRaisesRegex(PermissionError,'sender'): callback.receive('s','1','n',b'<xml/>')

    def test_wecom_constructor_overrides_even_empty(self):
        self.env_file('WECHAT_CALLBACK_TOKEN_FILE='+str(self.root/'missing')+'\nWECHAT_ENCODING_AES_KEY_FILE='+str(self.root/'missing-key')+'\nWECHAT_CORP_ID=file-corp\n')
        callback=bot.WecomCallback('', '', '')
        self.assertEqual((callback.token,callback.aes_key,callback.corp_id),('','',''))
        self.assertFalse(callback.configured)
        callback=bot.WecomCallback('constructor-token','constructor-key','constructor-corp')
        self.assertTrue(callback.configured)

    def test_wecom_invalid_secret_file_fails_locally(self):
        self.env_file('WECHAT_CALLBACK_TOKEN_FILE='+str(self.root/'missing')+'\n')
        with self.assertRaisesRegex(ValueError,'WECHAT_CALLBACK_TOKEN_FILE'): bot.WecomCallback()
        self.api.assert_not_called(); self.network.assert_not_called()


if __name__ == '__main__':
    unittest.main()
