"""Private, shared notification settings. Public views never contain bot tokens."""
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time

from . import config

MAX_BYTES = 16384
_SETTINGS = 'notification-settings.json'
_RATE = 'notification-test-rate.json'
_LOCK = 'notification-settings.lock'
_TEST_INTERVAL = 30
_MISSING = object()
_TOKEN = re.compile(r'[0-9]{5,20}:[A-Za-z0-9_-]{20,200}\Z')
_CHAT = re.compile(r'-?[1-9][0-9]{0,19}\Z')


class SettingsError(Exception):
    def __init__(self, code='notification_settings_unavailable', status=503, retry_after=None):
        super().__init__(code)
        self.code, self.status, self.retry_after = code, status, retry_after


def invalid():
    raise SettingsError('notification_invalid_request', 400)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate field')
        result[key] = value
    return result


def parse_request(raw):
    try:
        if len(raw) > MAX_BYTES:
            invalid()
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
        if type(value) is not dict:
            invalid()
        return value
    except (ValueError, UnicodeError, RecursionError):
        invalid()


def _revision(value):
    return type(value) is int and 0 <= value < 2**53 - 1


def validate_update(value):
    if (type(value) is not dict or set(value) != {'revision', 'viewing_enabled', 'telegram'}
            or not _revision(value['revision']) or type(value['viewing_enabled']) is not bool):
        invalid()
    tg = value['telegram']
    if (type(tg) is not dict or not {'enabled', 'chat_id'} <= set(tg)
            or set(tg) - {'enabled', 'chat_id', 'bot_token', 'clear_token'}
            or type(tg['enabled']) is not bool or type(tg['chat_id']) is not str
            or ('bot_token' in tg and type(tg['bot_token']) is not str)
            or ('clear_token' in tg and type(tg['clear_token']) is not bool)):
        invalid()
    chat = tg['chat_id']
    token = tg.get('bot_token', '')
    if (len(chat) > 21 or (chat and not _CHAT.fullmatch(chat))
            or len(token) > 222 or (token and not _TOKEN.fullmatch(token))
            or (token and tg.get('clear_token', False))):
        invalid()
    if tg['enabled'] and not chat:
        invalid()
    return value


def validate_test(value):
    if type(value) is not dict or set(value) != {'revision'} or not _revision(value['revision']):
        invalid()
    return value['revision']


def _private_file(fd):
    st = os.fstat(fd)
    if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid()
            or stat.S_IMODE(st.st_mode) != 0o600):
        raise SettingsError()


def _directory():
    # Traverse each component with NOFOLLOW; no symlink ancestor or path race.
    path = os.path.abspath(config.DATA_DIR)
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in Path(path).parts[1:]:
            try:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = following
        st = os.fstat(fd)
        if st.st_mode & stat.S_IWOTH:
            raise SettingsError()
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _locked():
    directory = lock = None
    try:
        directory = _directory()
        lock = os.open(_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
        _private_file(lock)
        deadline = time.monotonic() + 2
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SettingsError()
                time.sleep(0.02)
        yield directory
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise SettingsError() from None
    finally:
        if lock is not None:
            os.close(lock)
        if directory is not None:
            os.close(directory)


def _read(directory, name):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    except FileNotFoundError:
        return _MISSING
    with os.fdopen(fd, 'rb') as stream:
        _private_file(stream.fileno())
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise SettingsError()
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise SettingsError() from None


def _write(directory, name, value):
    temporary = '.' + name + '.' + secrets.token_hex(12)
    fd = None
    try:
        # Refuse unsafe existing targets even though replace itself would not follow them.
        _read(directory, name)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, 'wb') as stream:
            fd = None
            stream.write((json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def _load(directory):
    saved = _read(directory, _SETTINGS)
    if saved is _MISSING:
        token = config._secret('NOTIFY_TELEGRAM_BOT_TOKEN')
        chat = config._value('NOTIFY_TELEGRAM_CHAT_ID')
        return {'revision': 0, 'source': 'environment',
                'viewing_enabled': config._value('NOTIFY_VIEWING_ENABLED', '0') == '1',
                'telegram': {'enabled': bool(token and chat), 'chat_id': chat, 'bot_token': token}}
    try:
        if (type(saved) is not dict or set(saved) != {'schema', 'revision', 'viewing_enabled', 'telegram'}
                or type(saved['schema']) is not int or saved['schema'] != 1
                or not _revision(saved['revision']) or saved['revision'] < 1):
            raise SettingsError()
        value = {key: saved[key] for key in ('revision', 'viewing_enabled', 'telegram')}
        validate_update(value)
        if set(saved['telegram']) != {'enabled', 'chat_id', 'bot_token'}:
            raise SettingsError()
        if saved['telegram']['enabled'] and not saved['telegram']['bot_token']:
            raise SettingsError()
    except SettingsError:
        raise SettingsError() from None
    return {**value, 'source': 'server'}


def _view(settings):
    tg = settings['telegram']
    return {'revision': settings['revision'], 'source': settings['source'],
            'viewing_enabled': settings['viewing_enabled'],
            'telegram': {'enabled': tg['enabled'], 'configured': bool(tg['bot_token'] and tg['chat_id']),
                         'token_configured': bool(tg['bot_token']), 'chat_id': tg['chat_id']}}


def get_settings():
    with _locked() as directory:
        return _view(_load(directory))


def runtime_settings():
    """Internal delivery snapshot; intentionally includes the private token."""
    with _locked() as directory:
        return _load(directory)


def update_settings(value):
    validate_update(value)
    with _locked() as directory:
        current = _load(directory)
        if value['revision'] != current['revision']:
            raise SettingsError('notification_revision_conflict', 409)
        tg = value['telegram']
        token = '' if tg.get('clear_token') else tg.get('bot_token') or current['telegram']['bot_token']
        if (token and not _TOKEN.fullmatch(token)) or (tg['enabled'] and not token):
            invalid()
        if not _revision(current['revision'] + 1):
            raise SettingsError()
        saved = {'schema': 1, 'revision': current['revision'] + 1,
                 'viewing_enabled': value['viewing_enabled'],
                 'telegram': {'enabled': tg['enabled'], 'chat_id': tg['chat_id'], 'bot_token': token}}
        _write(directory, _SETTINGS, saved)
        return _view({**saved, 'source': 'server'})


def reserve_test(revision):
    validate_test({'revision': revision})
    with _locked() as directory:
        current = _load(directory)
        if revision != current['revision']:
            raise SettingsError('notification_revision_conflict', 409)
        if current['source'] != 'server':
            raise SettingsError('notification_not_saved', 409)
        tg = current['telegram']
        if not tg['enabled']:
            raise SettingsError('notification_disabled', 409)
        if not tg['bot_token'] or not tg['chat_id']:
            raise SettingsError('notification_not_configured', 409)
        now = time.time()
        rate = _read(directory, _RATE)
        if rate is not _MISSING:
            if (type(rate) is not dict or set(rate) != {'last_test_at'}
                    or type(rate['last_test_at']) not in (int, float)
                    or not math.isfinite(rate['last_test_at']) or rate['last_test_at'] < 0):
                raise SettingsError()
            remaining = rate['last_test_at'] + _TEST_INTERVAL - now
            if remaining > 0:
                raise SettingsError('notification_rate_limited', 429, max(1, math.ceil(remaining)))
        # Reserve before networking; failures also consume the persistent cooldown.
        _write(directory, _RATE, {'last_test_at': now})
        return dict(tg)


def send_test(revision):
    tg = reserve_test(revision)
    from . import notifier
    try:
        notifier._post('https://api.telegram.org/bot' + tg['bot_token'] + '/sendMessage',
                       {'chat_id': tg['chat_id'], 'text': '家庭影院通知测试：Telegram API 接收验证。'})
    except Exception:
        raise SettingsError('notification_delivery_failed', 502) from None
    return {'accepted': True, 'channel': 'telegram'}
