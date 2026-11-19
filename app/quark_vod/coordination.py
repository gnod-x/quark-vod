"""Cross-process coordination for host jobs and the container.

Playback holds a shared library lease until readiness and activation complete.
Destructive jobs take an exclusive lease; overlapping maintenance skips safely.
Locks live in the shared data volume, never in a container's private /tmp.
"""
from contextlib import contextmanager
from functools import wraps
import fcntl
import hashlib
import json
import urllib.request
import os
import threading
import time
from . import config

_local = threading.local()


class Busy(RuntimeError):
    pass


@contextmanager
def file_lock(name, *, exclusive=True, timeout=0):
    held = getattr(_local, 'held', {})
    if name in held:
        if exclusive and not held[name]:
            raise RuntimeError('cannot upgrade an active shared lease')
        yield
        return
    directory = os.path.join(config.DATA_DIR, 'locks')
    try:
        os.makedirs(directory, mode=0o1777)
        os.chmod(directory, 0o1777)
    except FileExistsError:
        pass
    path = os.path.join(directory, hashlib.sha256(name.encode()).hexdigest() + '.lock')
    # flock works on read-only descriptors on Linux. Host jobs can lock files
    # created by the root container without changing ownership or permissions.
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            descriptor = os.open(path, os.O_RDONLY)
    with os.fdopen(descriptor, 'r') as stream:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(stream, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Busy('影院维护或转存正在进行，请稍后重试')
                time.sleep(0.05)
        held[name] = exclusive
        _local.held = held
        try:
            yield
        finally:
            held.pop(name)
            fcntl.flock(stream, fcntl.LOCK_UN)


def playback_lease():
    return file_lock('library', exclusive=False, timeout=2)


@contextmanager
def maintenance_window():
    nested = 'library' in getattr(_local, 'held', {})
    with file_lock('library', exclusive=True):
        if not nested:
            ensure_idle()
        yield


def maintenance(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        with maintenance_window():
            return fn(*args, **kwargs)
    return guarded


def ensure_idle():
    """Do not remove/relocate media while Jellyfin reports an active session.

    Missing database/credentials and unknown session state stop maintenance.
    Offline business fixtures must explicitly substitute this external guard;
    absence of a production database is never evidence that the library is idle.
    """
    if not os.path.isfile(config.JELLYFIN2_DB):
        raise Busy('Jellyfin 保护数据库缺失或不可访问，暂缓维护')
    key = config.get_jellyfin2_key()
    if not key:
        raise Busy('无法读取 Jellyfin 凭证，暂缓维护')
    request = urllib.request.Request(config.JELLYFIN2_API + '/Sessions',
        headers={'Authorization': 'MediaBrowser Token="' + key + '"'})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            sessions = json.loads(response.read(2 * 1024**2))
        if not isinstance(sessions, list):
            raise ValueError('invalid session response')
    except Exception as exc:
        raise Busy('无法核对 Jellyfin 播放状态，暂缓维护') from exc
    if any(session.get('NowPlayingItem') for session in sessions):
        raise Busy('Jellyfin 正在播放，暂缓维护')
