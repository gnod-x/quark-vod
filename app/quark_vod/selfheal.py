"""Authoritative leaf validation and conservative restoration of stale STRMs.

Discovery/network scans do not hold the library mutation lock. A missing file
is checked again under an exclusive lease immediately before restoration. New
playback and maintenance therefore cannot overwrite each other's source state.
"""
import os
import time
import urllib.parse
from . import config, db, strm_manager
from .coordination import maintenance_window, Busy
from .openlist_client import OpenListClient


def _iter_live_strms():
    """遍历 cinema2 下所有 live strm，返回 [(path, remote_path)]。"""
    out = []
    base = config.CINEMA2_DIR
    if not os.path.isdir(base):
        return out
    for root, _dirs, files in os.walk(base):
        for fn in files:
            if not fn.endswith(".strm"):
                continue
            p = os.path.join(root, fn)
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read().strip()
            except Exception:
                continue
            if not strm_manager.is_live(content):
                continue
            remote = strm_manager.parse_live_remote_path(content)
            if remote:
                out.append((p, remote))
    return out


def _remote_exists_batch(tr, remote_paths):
    """Check every path component, sharing directory results within this batch.

    Missing leaf files are detected even if the movie folder still exists.
    An incomplete/auth/network response is unknown, never deletion evidence.
    """
    cache = {}
    fids = {}
    root = config.QUARK_VOD_ROOT.rstrip('/')
    def listing(path):
        if path not in cache:
            try:
                entries = (tr._quark_list_fid(fids[path]) if path in fids and
                           callable(getattr(tr,'_quark_list_fid',None)) else tr._quark_list(path))
                cache[path] = {entry['name']:entry for entry in entries}
                for name,item in cache[path].items():
                    if item.get('is_dir') and item.get('fid'):
                        fids[path+'/'+name]=item['fid']
            except Exception as exc:
                cache[path] = {} if '不存在该目录' in str(exc) else None
        return cache[path]
    result = {}
    for original in remote_paths:
        path = config.write_path(original)
        result[original] = True
        if not path.startswith(root + '/') or '..' in path.split('/'):
            continue
        parent = root
        parts = path[len(root)+1:].split('/')
        for index, part in enumerate(parts):
            entries = listing(parent)
            if entries is None:
                break
            item = entries.get(part)
            if item is None or (index < len(parts)-1 and not item.get('is_dir')):
                result[original] = False
                break
            parent += '/' + part
    return result


def _remote_exists(tr, remote_path):
    return _remote_exists_batch(tr, [remote_path]).get(remote_path, True)


def _pending_from_map(strm_path):
    rows = db._conn().execute(
        'SELECT cat,title,season,episode,variant FROM strm_map WHERE strm_path=?', (strm_path,)).fetchall()
    if not rows:
        return ''
    parts = os.path.relpath(strm_path, config.CINEMA2_DIR).split(os.sep)
    row = rows[0]
    cat, title = (parts[0],parts[1]) if len(parts)>=3 and '..' not in parts else (row['cat'],row['title'])
    query=urllib.parse.urlencode({'cat':cat,'t':title,'s':row['season'],'e':row['episode'],**({'variant':row['variant']} if row['variant'] else {})})
    return strm_manager.PENDING_PREFIX+'?'+query


def _invalidate_db_by_remote(remote):
    with db._conn() as con:
        con.execute("UPDATE transfers SET status='cleaned',remote_path=NULL,error='remote file missing',updated_at=? "
                    "WHERE remote_path IN (?,?) AND status='done'",
                    (db.now(),config.write_path(remote),config.play_path(remote)))


def _restore_missing(tr, remote, paths):
    return _restore_missing_batch(tr, {remote: paths})


def _restore_missing_batch(tr, groups):
    """Recheck a bounded group under the mutation lease, then reconcile locally."""
    restored=failed=0
    with maintenance_window():
        exists = _remote_exists_batch(tr, list(groups))
        for remote, paths in groups.items():
            if exists.get(remote, True):
                continue
            local_failed = 0
            for path in paths:
                try:
                    current=strm_manager.read_strm(path)
                    current_remote=strm_manager.parse_live_remote_path(current)
                    if current_remote and config.write_path(current_remote)!=config.write_path(remote):
                        continue  # source was replaced since the scan
                    params=strm_manager.parse_pending_params(_pending_from_map(path))
                    if not params or not os.path.isfile(path):
                        local_failed+=1
                        continue
                    strm_manager.restore_pending(path,params['cat'],params['title'],params['season'],params['episode'],**({'variant':params['variant']} if params.get('variant') else {}))
                    if strm_manager.is_pending(strm_manager.read_strm(path)):
                        restored+=1
                    else:
                        local_failed+=1
                except Exception:
                    local_failed+=1
            if not local_failed:
                _invalidate_db_by_remote(remote)
            failed += local_failed
    return restored,failed


def scan_and_heal(dry_run=False, verbose=True):
    from .transfer import QuarkTransfer
    tr=QuarkTransfer(None)
    if not dry_run:
        # A historical done row without a path cannot represent a saved file.
        # Preserve the history and any current library source; only correct the
        # inconsistent row, under the same maintenance gate as other changes.
        if db._conn().execute("SELECT 1 FROM transfers WHERE status='done' AND (remote_path IS NULL OR remote_path='') LIMIT 1").fetchone():
            with maintenance_window(), db._conn() as con:
                con.execute("UPDATE transfers SET status='failed',error='done record has no saved path',updated_at=? "
                            "WHERE status='done' AND (remote_path IS NULL OR remote_path='')",(db.now(),))
    groups={}
    for path,remote in _iter_live_strms():
        groups.setdefault(config.write_path(remote),[]).append(path)
    # Historical done rows can outlive both their files and their library map.
    # Keeping them as done makes later playback reuse a nonexistent saved file.
    # Reconcile their state too; do not delete history or any cloud content.
    for row in db._conn().execute("SELECT remote_path FROM transfers WHERE status='done' AND remote_path IS NOT NULL"):
        remote=config.write_path(row['remote_path'])
        if remote.startswith(config.QUARK_VOD_ROOT.rstrip('/')+'/'):
            groups.setdefault(remote,[])
    exists=_remote_exists_batch(tr,list(groups))
    broken=restored=skipped=0
    missing={}
    for remote,paths in groups.items():
        if exists.get(remote,True):
            continue
        broken+=1
        if dry_run:
            continue
        missing[remote]=paths
    # Bound network work under each exclusive lease. Listing results are shared
    # within a chunk so old whole-season imports do not repeat hundreds of calls.
    targets=list(missing)
    for start in range(0,len(targets),32):
        batch={remote:missing[remote] for remote in targets[start:start+32]}
        try:
            success,fail=_restore_missing_batch(tr,batch)
            restored+=success;skipped+=fail
        except Busy:
            skipped+=sum(max(1,len(paths)) for paths in batch.values())
    if verbose:
        print(f'[selfheal] 检查 {len(groups)} 个视频；缺失 {broken}；恢复 {restored}；暂缓/失败 {skipped}',flush=True)
    return len(groups),broken,restored,skipped


def heal_remote(remote):
    from .transfer import QuarkTransfer
    paths=[path for path,target in _iter_live_strms() if config.write_path(target)==config.write_path(remote)]
    if not paths:
        return 0
    try:
        return _restore_missing(QuarkTransfer(None),remote,paths)[0]
    except Busy:
        return 0


def heal_loop(stop_event=None, verbose=True):
    def wait(seconds):
        if stop_event is not None:
            return stop_event.wait(seconds)
        time.sleep(seconds)
        return False
    if wait(config.SELFHEAL_INITIAL_DELAY):
        return
    while stop_event is None or not stop_event.is_set():
        interval=config.SELFHEAL_INTERVAL
        try:
            if config.MAINTENANCE_ENABLED:
                from .subtitles import schedule_pending
                schedule_pending()
                _,_,_,skipped=scan_and_heal(verbose=verbose)
                if skipped:
                    interval=max(900,min(1800,config.SELFHEAL_INTERVAL))
        except Exception as exc:
            interval=max(300,min(1800,config.SELFHEAL_INTERVAL))
            print(f'[selfheal] 检查失败: {type(exc).__name__}',flush=True)
        finally:
            db.close()
        if wait(interval):
            return


def main():
    import sys
    scan_and_heal(dry_run='--dry-run' in sys.argv)


if __name__=='__main__':
    main()
