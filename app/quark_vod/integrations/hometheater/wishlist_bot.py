#!/usr/bin/env python3
"""Authorized Telegram polling and authenticated WeCom encrypted callbacks."""

from quark_vod.integrations.hometheater import paths
from quark_vod import config as host_config
import base64
import hashlib
import hmac
import json
from pathlib import Path
import struct
import time
import urllib.request
import xml.etree.ElementTree as ET

from quark_vod import config, db
from quark_vod.coordination import file_lock


def ingest_message(text, source='bot'):
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        raise ValueError('wishlist message must contain 1..2000 characters')
    from quark_vod.integrations.hometheater.vod_search_and_add import submit_wishlist
    result = submit_wishlist(text.strip(), source=source)
    if result.get('status') not in {'ambiguous', 'not_found', 'failed'}:
        from quark_vod.integrations.hometheater.vod_automation import run_staging
        result['promotion'] = run_staging(limit=5)
    return result


def _telegram(method, data, token):
    # Request URL contains a secret: never log request objects or exception messages.
    request = urllib.request.Request('https://api.telegram.org/bot' + token + '/' + method,
        data=json.dumps(data).encode(), headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            body = response.read(2 * 1024**2 + 1)
        if len(body) > 2 * 1024**2:
            raise ValueError()
        result = json.loads(body)
        if result.get('ok') is not True:
            raise ValueError()
        return result.get('result')
    except Exception:
        raise RuntimeError('Telegram bot API unavailable') from None


def poll_telegram():
    token = config._secret('TELEGRAM_BOT_TOKEN')
    allowed = {x.strip() for x in config._value('TELEGRAM_ALLOWED_CHAT_IDS').split(',') if x.strip()}
    if not token or not allowed:
        return {'configured': False, 'processed': 0, 'errors': 0}
    path = Path(config.DATA_DIR) / 'telegram_wishlist_offset.json'
    with file_lock('telegram-wishlist'):
        state = json.loads(path.read_text()) if path.exists() else {}
        offset = int(state.get('offset', 0))
        failures = state.get('failures') or {}
        updates = _telegram('getUpdates', {'offset': offset, 'limit': 50, 'timeout': 0,
                                         'allowed_updates': ['message']}, token)
        if not isinstance(updates, list):
            raise ValueError('invalid Telegram update list')
        processed = errors = 0
        for update in updates:
            message = update.get('message') or {}
            chat = str((message.get('chat') or {}).get('id', ''))
            if chat in allowed and isinstance(message.get('text'), str):
                text = message['text']
                if text.startswith('/watch '):
                    text = text[7:]
                try:
                    result = ingest_message(text, source='telegram')
                    # No raw share URL, token or remote media URL is echoed.
                    status = result.get('status', 'pending')
                    reply = '心愿已接收：' + status
                    if status == 'ambiguous':
                        reply = '找到多个同名作品，请补充片名、年份和电影/剧集类型。'
                    link = result.get('jellyfin_url') or ''
                    if link.startswith('https://') and 'details?id=' in link:
                        reply += '\n' + link
                    _telegram('sendMessage', {'chat_id': chat, 'text': reply[:2000],
                                             'disable_web_page_preview': True}, token)
                    processed += 1
                except Exception:
                    errors += 1
                    key = str(update['update_id'])
                    failures[key] = int(failures.get(key, 0)) + 1
                    if failures[key] < 3:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        temporary = path.with_suffix('.tmp')
                        temporary.write_text(json.dumps({'offset':offset,'failures':failures}))
                        temporary.replace(path)
                        break
                    # A persistently malformed message cannot starve later wishes.
                    # Keep its update id and count for diagnosis, without storing message text.
            offset = max(offset, int(update['update_id']) + 1)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'offset': offset, 'failures':dict(list(failures.items())[-100:])}))
            temporary.replace(path)
    return {'configured': True, 'processed': processed, 'errors': errors}


class WecomCallback:
    """Official sorted SHA1 signature + AES-CBC envelope, with recipient and replay checks."""
    def __init__(self, token=None, aes_key=None, corp_id=None, clock=time.time):
        self.token = token if token is not None else config._secret('WECHAT_CALLBACK_TOKEN')
        self.aes_key = aes_key if aes_key is not None else config._secret('WECHAT_ENCODING_AES_KEY')
        self.corp_id = corp_id if corp_id is not None else config._value('WECHAT_CORP_ID')
        self.clock = clock

    @property
    def configured(self):
        return bool(self.token and self.aes_key and self.corp_id)

    def decrypt(self, signature, timestamp, nonce, encrypted):
        if not self.configured:
            raise PermissionError('WeCom callback is not configured')
        if abs(self.clock() - int(timestamp)) > 300 or len(nonce) > 128:
            raise PermissionError('expired WeCom callback')
        digest = hashlib.sha1(''.join(sorted([self.token, str(timestamp), str(nonce), encrypted])).encode()).hexdigest()
        if not hmac.compare_digest(digest, signature):
            raise PermissionError('invalid WeCom callback signature')
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError:
            raise RuntimeError('install quark-vod[wishlist] for encrypted WeCom callbacks') from None
        key = base64.b64decode(self.aes_key + '=')
        ciphertext = base64.b64decode(encrypted, validate=True)
        if len(key) != 32 or not ciphertext or len(ciphertext) % 16:
            raise PermissionError('invalid WeCom encryption envelope')
        decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
        clear = decryptor.update(ciphertext) + decryptor.finalize()
        padding = clear[-1]
        if not 1 <= padding <= 32 or clear[-padding:] != bytes([padding]) * padding:
            raise PermissionError('invalid WeCom padding')
        clear = clear[:-padding]
        if len(clear) < 20:
            raise PermissionError('invalid WeCom payload')
        length = struct.unpack('!I', clear[16:20])[0]
        if length > len(clear) - 20:
            raise PermissionError('invalid WeCom payload length')
        content = clear[20:20 + length]
        recipient = clear[20 + length:].decode('utf-8')
        if not hmac.compare_digest(recipient, self.corp_id):
            raise PermissionError('wrong WeCom recipient')
        return content

    def verify_echo(self, msg_signature, timestamp, nonce, echostr):
        return self.decrypt(msg_signature, timestamp, nonce, echostr).decode('utf-8')

    def receive(self, msg_signature, timestamp, nonce, body):
        if len(body) > 65536:
            raise ValueError('WeCom callback too large')
        envelope = ET.fromstring(body)
        encrypted = envelope.findtext('Encrypt') or ''
        root = ET.fromstring(self.decrypt(msg_signature, timestamp, nonce, encrypted))
        sender = root.findtext('FromUserName') or ''
        allowed = {s.strip() for s in config._value('WECHAT_ALLOWED_USERS').split(',') if s.strip()}
        if not allowed or sender not in allowed:
            raise PermissionError('WeCom sender is not allowed')
        if root.findtext('MsgType') != 'text':
            return {'status': 'ignored'}
        message_id = root.findtext('MsgId')
        if not message_id:
            raise PermissionError('WeCom message has no id')
        key = hashlib.sha256(message_id.encode()).hexdigest()
        path = Path(config.DATA_DIR) / 'wecom_wishlist_seen.json'
        with file_lock('wecom-wishlist'):
            seen = json.loads(path.read_text()) if path.exists() else {}
            now = self.clock()
            seen = {k: v for k, v in seen.items() if now - float(v) < 86400}
            if key in seen:
                return {'status': 'duplicate'}
            result = ingest_message(root.findtext('Content') or '', source='wecom')
            seen[key] = now
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(seen))
            temporary.replace(path)
        return result


if __name__ == '__main__':
    try:
        result = poll_telegram()
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(int(bool(result['errors'])))
    finally:
        db.close()
