"""Atomic, revisioned automation and AI configuration; jobs are opt-in."""
from contextlib import contextmanager
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import notification_settings as private

MAX_BYTES = 32768
STATE_MAX_BYTES = 65536
FILENAME = 'automation-settings.json'
TASKS = {
    'ai_night_worker': ('夜间维护', ['0 23 * * *'], 16200),
    'rss_subscriber': ('影视推荐', ['0 3,15 * * *'], 1800),
    'auto_sync': ('传统影院同步', ['0 4 * * *'], 3600),
    'daily_update_and_prune': ('点播库日更', ['0 5 * * *'], 10800),
    'airing_chase': ('连载追更', ['30 0,12,18 * * *', '0 20,22 * * *'], 1800),
    'digest': ('调度日报', ['0 8 * * *'], 300),
    'quality_upgrade': ('画质升级', ['30 2 * * 0'], 3600),
    'douban_watchlist_sync': ('豆瓣片单', ['30 7,19 * * *'], 1800),
    'wishlist_bot': ('心愿单', ['* * * * *'], 55),
}
EFFORTS = {'none', 'low', 'medium', 'high'}
REASONING = {'probe': 'none', 'subtitle': 'low', 'people': 'low', 'plot': 'high', 'filename': 'low'}
MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}\Z')


class AutomationError(Exception):
    def __init__(self, code='automation_unavailable', status=503):
        super().__init__(code)
        self.code, self.status = code, status


def invalid():
    raise AutomationError('automation_invalid_request', 400)


def parse_request(raw):
    try:
        if len(raw) > MAX_BYTES:
            invalid()
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=private._pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if type(value) is not dict:
            invalid()
        return value
    except (ValueError, UnicodeError, RecursionError):
        invalid()


def revision_valid(value):
    return type(value) is int and 0 <= value < 2**53 - 1


def cron_fields(expression):
    if type(expression) is not str or len(expression) > 120:
        invalid()
    parts = expression.split()
    if len(parts) != 5:
        invalid()
    result = []
    for field, (lo, hi) in zip(parts, ((0,59), (0,23), (1,31), (1,12), (0,7))):
        values = set()
        for fragment in field.split(','):
            if not re.fullmatch(r'(?:\*|\d{1,2}(?:-\d{1,2})?)(?:/\d{1,2})?', fragment):
                invalid()
            base, _, stride = fragment.partition('/')
            step = int(stride) if stride else 1
            if not 1 <= step <= hi - lo + 1:
                invalid()
            if base == '*':
                start, stop = lo, hi
            elif '-' in base:
                start, stop = map(int, base.split('-'))
            else:
                start = stop = int(base)
                if stride:
                    stop = hi
            if not lo <= start <= stop <= hi:
                invalid()
            values.update(range(start, stop + 1, step))
        result.append(values)
    return result


def valid_base_url(value):
    if type(value) is not str or len(value) > 2048 or value != value.strip():
        invalid()
    if not value:
        return value
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
            invalid()
        parsed.port
    except ValueError:
        invalid()
    return value.rstrip('/')


def validate(value):
    if (type(value) is not dict
            or set(value) != {'revision', 'enabled', 'timezone', 'ai', 'tasks'}
            or not revision_valid(value['revision']) or type(value['enabled']) is not bool
            or type(value['timezone']) is not str or len(value['timezone']) > 80):
        invalid()
    try:
        ZoneInfo(value['timezone'])
    except (ZoneInfoNotFoundError, ValueError):
        invalid()
    ai = value['ai']
    if (type(ai) is not dict or set(ai) != {'enabled','base_url','primary_model','fallback_models','reasoning'}
            or type(ai['enabled']) is not bool or type(ai['primary_model']) is not str
            or (ai['primary_model'] and not MODEL.fullmatch(ai['primary_model']))
            or type(ai['fallback_models']) is not list or len(ai['fallback_models']) > 5
            or any(type(m) is not str or not MODEL.fullmatch(m) for m in ai['fallback_models'])
            or len(set(ai['fallback_models'])) != len(ai['fallback_models'])
            or type(ai['reasoning']) is not dict or set(ai['reasoning']) != set(REASONING)
            or any(type(e) is not str or e not in EFFORTS for e in ai['reasoning'].values())):
        invalid()
    valid_base_url(ai['base_url'])
    if ai['enabled'] and (not ai['primary_model'] or not ai['base_url']):
        invalid()
    tasks = value['tasks']
    if type(tasks) is not dict or set(tasks) != set(TASKS):
        invalid()
    for task in tasks.values():
        if (type(task) is not dict or set(task) != {'enabled','schedules','timeout_seconds'}
                or type(task['enabled']) is not bool or type(task['schedules']) is not list
                or not 1 <= len(task['schedules']) <= 8
                or type(task['timeout_seconds']) is not int or not 10 <= task['timeout_seconds'] <= 21600):
            invalid()
        for expression in task['schedules']:
            cron_fields(expression)
    return value


def defaults():
    base_url = private.config._value('OPENAI_BASE_URL', '')
    try:
        base_url = valid_base_url(base_url)
    except AutomationError:
        base_url = ''
    return {'revision': 0, 'enabled': False, 'timezone': 'Asia/Shanghai',
            'ai': {'enabled': False, 'base_url': base_url, 'primary_model': '', 'fallback_models': [],
                   'reasoning': dict(REASONING)},
            'tasks': {key: {'enabled': False, 'schedules': list(schedule), 'timeout_seconds': timeout}
                      for key, (_, schedule, timeout) in TASKS.items()}}


@contextmanager
def locked():
    directory = descriptor = None
    try:
        directory = private._directory()
        descriptor = os.open('automation.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
        private._private_file(descriptor)
        deadline = time.monotonic() + 2
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AutomationError()
                time.sleep(.02)
        yield directory
    except (private.SettingsError, OSError, ValueError, UnicodeError, RecursionError):
        raise AutomationError() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def storage_limit(name):
    return MAX_BYTES if name == FILENAME else STATE_MAX_BYTES


def read_json(directory, name):
    limit = storage_limit(name)
    try:
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                 dir_fd=directory)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor,'rb') as stream:
            private._private_file(stream.fileno())
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise AutomationError('automation_file_too_large')
        value = json.loads(raw.decode(),object_pairs_hook=private._pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if value is None:
            raise AutomationError()
        return value
    except (private.SettingsError, OSError, ValueError, UnicodeError, RecursionError):
        raise AutomationError() from None


def write_json(directory, name, value):
    raw = (json.dumps(value,ensure_ascii=False,separators=(',',':'))+'\n').encode()
    if len(raw) > storage_limit(name):
        raise AutomationError('automation_file_too_large')
    temporary = '.'+name+'.'+secrets.token_hex(12)
    descriptor = None
    try:
        read_json(directory,name)
        descriptor = os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,
                             0o600,dir_fd=directory)
        with os.fdopen(descriptor,'wb') as stream:
            descriptor = None
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,name,src_dir_fd=directory,dst_dir_fd=directory)
        os.fsync(directory)
    except (private.SettingsError,OSError,ValueError):
        raise AutomationError() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary,dir_fd=directory)
        except FileNotFoundError:
            pass


def load_locked(directory):
    value = read_json(directory, FILENAME)
    if value is None:
        return {**defaults(), 'version': 1, 'source': 'defaults'}
    try:
        if (type(value) is not dict or set(value) != {'version','revision','enabled','timezone','ai','tasks'}
                or type(value['version']) is not int or value['version'] != 1 or value['revision'] < 1):
            raise ValueError()
        validate({key: value[key] for key in ('revision','enabled','timezone','ai','tasks')})
    except (AutomationError, ValueError, TypeError, KeyError):
        raise AutomationError('automation_settings_invalid') from None
    return {**value, 'source': 'server'}


def get_settings():
    # Atomic replacement makes read locks unnecessary; module imports must not
    # create directories or lock files merely to discover disabled defaults.
    descriptor = None
    try:
        descriptor = os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC)
        for part in Path(os.path.abspath(private.config.DATA_DIR)).parts[1:]:
            try:
                following = os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,
                                    dir_fd=descriptor)
            except FileNotFoundError:
                return {**defaults(),'version':1,'source':'defaults'}
            os.close(descriptor)
            descriptor = following
        if os.fstat(descriptor).st_mode & 0o002:
            raise AutomationError()
        return load_locked(descriptor)
    except OSError:
        raise AutomationError() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def update_settings(value):
    value = copy.deepcopy(validate(value))
    with locked() as directory:
        old = load_locked(directory)
        if value['revision'] != old['revision']:
            raise AutomationError('automation_revision_conflict', 409)
        value['revision'] += 1
        value['ai']['base_url'] = valid_base_url(value['ai']['base_url'])
        write_json(directory, FILENAME, {**value, 'version': 1})
    return {**value, 'version': 1, 'source': 'server'}


def get_ai_config():
    snapshot = os.environ.get('VOD_AUTOMATION_AI_SNAPSHOT')
    if snapshot is not None:
        try:
            ai = parse_request(snapshot.encode())
            value = defaults()
            value['ai'] = ai
            validate(value)
            return copy.deepcopy(ai)
        except AutomationError:
            raise AutomationError('automation_ai_snapshot_invalid') from None
    return copy.deepcopy(get_settings()['ai'])


def ai_models(snapshot=None):
    ai = get_ai_config() if snapshot is None else snapshot
    if not ai['enabled'] or not ai['primary_model']:
        return []
    return list(dict.fromkeys([ai['primary_model'], *ai['fallback_models']]))


def catalog():
    return [{'id': key, 'label': label, 'description': label}
            for key, (label, _, _) in TASKS.items()]
