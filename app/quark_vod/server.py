"""Playback coordination. HTTP routes live in main.py.

Pending requests and explicit preheating join the same bounded job. A successful
result means the saved file is readable. With Jellyfin enabled, its exact library
STRM must also be activated; standalone playback does not write a media library.
A timeout never starts a second save while the original job is still running.
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from collections import OrderedDict
from dataclasses import dataclass
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import cleaner, cleanup, config, coordination, db, strm_manager
from .auditor import is_4k_candidate
from .identity import Work, same_title, split_title
from .openlist_client import OpenListClient

_ol = None
_ol_err = ''
_ol_retry_at = 0.0
_transfer = None
_client_lock = threading.RLock()
_in_flight_lock = threading.Lock()
_in_flight_transfers = {}
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='vod-resolve')
MAX_PENDING_JOBS = 16
_ready_results = OrderedDict()
_ready_lock = threading.Lock()
READY_TTL = 30
DEAD_SHARE_MARKERS = ('41011', '41012', '41031', '41010', '已取消了分享', '已失效', '已过期', '违规', '封禁')


class PlaybackUnavailable(RuntimeError):
    def __init__(self, message, code='temporarily_unavailable'):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PlaybackResult:
    url: str
    transfer_id: int
    remote_path: str


def get_openlist():
    global _ol, _ol_err, _ol_retry_at
    with _client_lock:
        if _ol is not None:
            return _ol
        if time.monotonic() < _ol_retry_at:
            return None
        try:
            _ol = OpenListClient()
            _ol_err = ''
        except Exception as exc:
            _ol_err = type(exc).__name__
            _ol_retry_at = time.monotonic() + 10
        return _ol


def get_transfer():
    global _transfer
    with _client_lock:
        if _transfer is None:
            ol = get_openlist()
            if ol is None:
                return None
            from .transfer import QuarkTransfer
            _transfer = QuarkTransfer(ol)
        return _transfer


def _direct_url(remote_path):
    return config.OPENLIST_PUBLIC + '/d' + urllib.parse.quote(config.play_path(remote_path))


def _wait_direct_url_ready(direct_url, remote_path=None, max_wait=12):
    deadline = time.monotonic() + max_wait
    refreshed = False
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(direct_url, headers={'Range': 'bytes=0-31', 'User-Agent': 'curl/8.5.0'})
            with urllib.request.urlopen(req, timeout=min(4, max(.1, deadline-time.monotonic()))) as response:
                head = response.read(32)
                content_type = response.headers.get('Content-Type', '').lower()
                if response.status in (200, 206) and head and not any(t in content_type for t in ('text/html', 'application/json')):
                    return True
        except Exception:
            pass
        if not refreshed and remote_path:
            refreshed = True
            ol = get_openlist()
            if ol:
                try:
                    ol.refresh_path_tree(config.play_path(remote_path))
                except Exception:
                    pass
        time.sleep(.3)
    return False


def _remote_still_exists(remote_path):
    # Read errors are not evidence of deletion. Readiness still gates playback.
    try:
        from .selfheal import _remote_exists
        from .transfer import QuarkTransfer
        return _remote_exists(QuarkTransfer(None), remote_path)
    except Exception:
        return True


def _resolve_strm_row(cat, title, season=0, episode=0):
    """Only fill omitted season/episode from an exact, unambiguous library work."""
    rows = db._conn().execute(
        'SELECT cat,title,season,episode FROM strm_map WHERE season>0 ORDER BY season,episode').fetchall()
    matches = [r for r in rows if (not cat or r['cat'] == cat)
               and split_title(r['title'])[0].casefold() == split_title(title)[0].casefold()
               and same_title(title, db.library_title(r['cat'], r['title'], r['season'], r['episode']))
               and (not season or r['season'] == season) and (not episode or r['episode'] == episode)]
    if not matches or len({(r['cat'], split_title(r['title'])[0]) for r in matches}) != 1:
        return cat, season, episode, False
    first = matches[0]
    return cat or first['cat'], season or first['season'], episode or first['episode'], True


def _finish(work, tid, remote):
    url = _direct_url(remote)
    if not _wait_direct_url_ready(url, remote_path=remote):
        raise PlaybackUnavailable('文件已转存，播放通道暂未就绪，请稍后重试', 'saved_not_ready')
    url = cleaner.clean_url_if_obfuscated(url)
    # Standalone installs have no pre-generated Jellyfin STRM library. Returning
    # the verified media URL needs no filesystem mutation. Keep the strict
    # activation gate for integrated deployments (including mapping failures).
    if config.VOD_JELLYFIN_ENABLED:
        try:
            strm_manager.activate(*work.args, url)
        except Exception as exc:
            raise PlaybackUnavailable('文件可读，但片库映射更新失败', 'activation_failed') from exc
    # This is a successful resolution/access, not proof of actual watch progress.
    # No automatic permanent pin; real watch/favourite protection comes from Jellyfin.
    db.record_access(tid)
    if remote.lower().endswith(('.mp4','.m4v','.mov','.mp41')):
        from . import media_cache
        skip=int((urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get('skip') or ['0'])[0]) if '/clean?' in url else 0
        url=media_cache.playback_url(db.get_transfer_by_id(tid),skip)
    return PlaybackResult(url, tid, remote)


def _resolve_once(work):
    from . import search
    # The season-level lock also prevents two episodes saving the same package.
    lock_name = 'work:' + repr((work.cat, work.title.casefold(), work.season))
    with coordination.playback_lease(), coordination.file_lock(lock_name, timeout=config.TRANSFER_TIMEOUT):
        row = db.find_done_transfer(*work.args)
        if row and not _remote_still_exists(row['remote_path']):
            if not cleanup._restore_all(row, verbose=False):
                raise PlaybackUnavailable('远端文件已失效，但片库占位恢复失败', 'restore_failed')
            con = db._conn()
            with con:
                con.execute("UPDATE transfers SET status='cleaned',remote_path=NULL,error='remote missing',updated_at=? "
                            "WHERE id=? AND remote_path=?", (db.now(), row['id'], row['remote_path']))
            row = None
        if row:
            return _finish(work, row['id'], row['remote_path'])

        tr = get_transfer()
        if tr is None or not tr.is_ready():
            raise PlaybackUnavailable('网盘服务或 Cookie 尚未就绪', 'not_configured')
        tried = set()
        last_error = None

        def attempt(candidate):
            nonlocal last_error
            if candidate.share_url in tried or (candidate.source != 'resume' and db.is_dead_source(candidate.share_url)):
                return None
            tried.add(candidate.share_url)
            try:
                # transfer validates the target file before saving it.
                tid, remote = tr.transfer(*work.args, candidate.share_url)
            except Exception as exc:
                from .transfer import TransferNotReady
                if isinstance(exc, TransferNotReady):
                    raise PlaybackUnavailable(str(exc), 'transfer_unconfirmed') from exc
                state = db.get_transfer(*work.args)
                if state and state['save_started_at']:
                    raise PlaybackUnavailable('上次转存结果尚未确认，请稍后重试', 'transfer_unconfirmed') from exc
                last_error = exc
                if any(marker in str(exc) for marker in DEAD_SHARE_MARKERS):
                    db.mark_dead_source(candidate.share_url, 'panshare', 'upstream share unavailable')
                print(f'[{config.SERVICE_NAME}] 候选转存失败 {work.title}: {type(exc).__name__}', flush=True)
                return None
            if not remote:
                last_error = PlaybackUnavailable('转存未返回文件')
                return None
            # Once saved, failure of the reading side must NOT save another copy.
            return _finish(work, tid, remote)

        # Resume a durable save before consulting public search or switching source.
        unfinished = db.get_transfer(*work.args)
        if unfinished and unfinished['save_started_at'] and unfinished['share_url']:
            pending = search.SearchResult(engine='panshare',source='resume',name=work.title,year='',
                                          poster='',episodes=[],share_url=unfinished['share_url'],pan_type='quark')
            result = attempt(pending)
            if result:
                return result

        # Locked shares remain useful even when public search nodes are down.
        locked, _, locked_title = db.get_locked_4k_share(*work.args)
        if locked:
            candidate = search.SearchResult(engine='panshare', source='locked', name=locked_title or work.title,
                                           year='', poster='', episodes=[], share_url=locked, pan_type='quark')
            # This audit row is already keyed by the exact category/title/season/episode
            # and the auditor checked the target file. Re-scoring its display title can
            # discard valid sources with release labels such as "(德国版)".
            result = attempt(candidate)
            if result:
                return result
        try:
            title, aliases = strm_manager.library_search_identity(work.cat, work.title, work.season)
            report = search.search_report(title, aliases=aliases, season=work.season, episode=work.episode)
            candidates = search.rank_candidates(report['results'], title, work.season, work.episode,
                                                work.cat, aliases=aliases)
        except Exception as exc:
            raise PlaybackUnavailable('片源搜索暂不可用，请稍后重试', 'search_unavailable') from exc
        search_deadline = time.monotonic() + config.TRANSFER_TIMEOUT
        for candidate in candidates[:8]:
            if time.monotonic() > search_deadline:
                break
            result = attempt(candidate)
            if result:
                return result
        if last_error is not None:
            message = '找到片源候选，但未能确认或转存目标视频，可在门户检查或换源'
            if report['partial']:
                message += '；本轮搜索未完成'
            raise PlaybackUnavailable(message, 'transfer_failed')
        if report.get('configured') is False:
            raise PlaybackUnavailable('尚未配置搜索源，请在管理台配置并加载插件', 'search_not_configured')
        if report['partial']:
            raise PlaybackUnavailable('片源搜索未完成，请稍后重试', 'search_incomplete')
        raise PlaybackUnavailable('本轮未找到可尝试的匹配片源，可在门户重新查找', 'source_unavailable')


def _run(work):
    try:
        return _resolve_once(work)
    except PlaybackUnavailable:
        raise
    except coordination.Busy as exc:
        raise PlaybackUnavailable(str(exc), 'busy') from exc
    except Exception as exc:
        print(f'[{config.SERVICE_NAME}] 点播失败 {work.title}: {type(exc).__name__}', flush=True)
        raise PlaybackUnavailable('点播暂时失败，请稍后重试') from exc


def _submit(work):
    with _in_flight_lock:
        future = _in_flight_transfers.get(work.key)
        if future is not None:
            return future, False
        if len(_in_flight_transfers) >= MAX_PENDING_JOBS:
            raise PlaybackUnavailable('点播队列已满，请稍后重试', 'busy')
        future = _executor.submit(_run, work)
        _in_flight_transfers[work.key] = future
    def finished(done):
        with _in_flight_lock:
            if _in_flight_transfers.get(work.key) is done:
                _in_flight_transfers.pop(work.key, None)
    future.add_done_callback(finished)
    return future, True


def resolve_playback(work):
    # Recover the unique dated library identity for legacy undated URLs before
    # reserving a task; otherwise aliases could save the same film concurrently.
    work = Work(work.cat, db.library_title(*work.args), work.season, work.episode)
    # A player can reopen hundreds of byte ranges for one MP4 seek. Repeating
    # cloud existence/readiness checks for each one adds a network round trip
    # per packet. Reuse only successful resolutions for a fixed short interval,
    # and check local source/state on every hit so cleanup or a new source wins.
    with _ready_lock:
        cached=_ready_results.get(work.key)
    if cached and cached[0]>time.monotonic():
        with coordination.playback_lease():
            row=db.find_done_transfer(*work.args)
            if row and row['id']==cached[1].transfer_id and row['remote_path']==cached[1].remote_path:
                return cached[1]
    future, _ = _submit(work)
    try:
        result=future.result(timeout=config.TRANSFER_TIMEOUT)
        with _ready_lock:
            _ready_results[work.key]=(time.monotonic()+READY_TTL,result)
            _ready_results.move_to_end(work.key)
            while len(_ready_results)>64:_ready_results.popitem(last=False)
        return result
    except FutureTimeout as exc:
        raise PlaybackUnavailable('转存仍在后台执行，请稍后重试', 'in_progress') from exc


def trigger_preheat_task(cat, title, season=0, episode=0):
    cat, season, episode, _ = _resolve_strm_row(cat, title, season, episode)
    work = Work(cat, db.library_title(cat,title,season,episode), season, episode)
    _, started = _submit(work)  # reservation precedes background execution
    if started:
        db.pin_item(cat, title, reason='preheated')
    return True, 'started' if started else 'in_progress'


class Handler:
    """Compatibility shim for older Python callers; production uses FastAPI."""
    def _handle_pending(self, q):
        try:
            work = Work(q.get('cat', [''])[0], q.get('t', [''])[0],
                        int(q.get('s', ['0'])[0]), int(q.get('e', ['0'])[0]))
            if not work.cat:
                raise ValueError('missing cat')
            result = resolve_playback(work)
            return self._send(302, b'', ctype='text/plain', headers={'Location': result.url, 'Cache-Control':'no-store'})
        except ValueError as exc:
            return self._json(400, {'error':str(exc)})
        except PlaybackUnavailable as exc:
            return self._json(503, {'error':str(exc), 'code':exc.code}, headers={'Retry-After':'5','Cache-Control':'no-store'})

    def _handle_preheat(self, q):
        try:
            ok, status = trigger_preheat_task(q.get('cat',[''])[0], q.get('t',[''])[0],
                                             int(q.get('s',['0'])[0]), int(q.get('e',['0'])[0]))
            return self._json(202, {'ok':ok,'status':status})
        except ValueError as exc:
            return self._json(400, {'error':str(exc)})
        except PlaybackUnavailable as exc:
            return self._json(503, {'error':str(exc), 'code':exc.code}, headers={'Retry-After':'5'})


def _cleanup_stale_transfers(max_age=3600):
    con = db._conn()
    with con:
        con.execute("UPDATE transfers SET status='failed',error='transfer interrupted',updated_at=? "
                    "WHERE status IN ('searching','saving') AND updated_at<?", (db.now(), db.now()-max_age))


def main():
    import uvicorn
    uvicorn.run('main:app', host=config.HOST, port=config.PORT)


if __name__ == '__main__':
    main()
