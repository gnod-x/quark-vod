#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
strm 生命周期管理

三种状态：
  pending  占位（内容指向 /pending，触发转存）
  live     直链（内容为 OpenList 地址，可播放）
  （清理后回到 pending，可再次转存）

铁律：
  - 只有确认远端文件存在，才把 strm 写成 live
  - 写 strm 用临时文件 + os.replace，避免半成品
  - 绝不覆盖已经指向 OpenList 直链的 strm（幂等保护）
"""

import os
import tempfile
import stat
import urllib.parse
import queue
import threading
import time

from . import config
from . import db
from . import cleaner, download_http

VARIANT_LABELS={'hdr':'4K HDR 原声','sdr':'SDR 兼容','dubbed':'1080P 国配','remux':'4K REMUX'}

def variant_from_path(path):
    stem=os.path.splitext(os.path.basename(path))[0]
    return next((key for key,label in VARIANT_LABELS.items() if stem.endswith(' - '+label)),'')

PENDING_PREFIX = f"{config.PENDING_BASE_URL}/pending"

# 后台限流刷新队列（解决全季 40 集连带激活时并发发起 40 个 FullRefresh 挤占首播带宽）
_refresh_queue: "queue.Queue[str]" = queue.Queue()
_refresh_worker_started = False
_refresh_worker_lock = threading.Lock()


def _do_jellyfin_refresh_sync(strm_path: str):
    """同步执行单个条目的 Jellyfin2 流信息刷新（免锁只读查询 + ReplaceAllMetadata=false）。"""
    if not config.VOD_JELLYFIN_ENABLED:
        return
    try:
        from contextlib import closing
        import urllib.request
        import sqlite3
        if not strm_path or not os.path.isfile(strm_path):
            return
        relative = os.path.relpath(strm_path, config.CINEMA2_DIR)
        if relative == os.pardir or relative.startswith(os.pardir + os.sep):
            return
        container_path = os.path.join(config.JELLYFIN_LIBRARY_ROOT, relative)
        with closing(sqlite3.connect(f"file:{config.JELLYFIN2_DB}?mode=ro", uri=True, timeout=2)) as con:
            cur = con.cursor()
            row = cur.execute("SELECT Id FROM BaseItems WHERE Path = ? LIMIT 1", (container_path,)).fetchone()
        if not row or not row[0]:
            return
        item_id = row[0]
        url = (f"{config.JELLYFIN2_API}/Items/{item_id}/Refresh"
               f"?MetadataRefreshMode=FullRefresh&ImageRefreshMode=None"
               f"&ReplaceAllMetadata=false&ReplaceAllImages=false")
        req = urllib.request.Request(url, data=b"", method="POST")
        if config.JELLYFIN2_KEY:
            req.add_header("Authorization", f'MediaBrowser Token="{config.JELLYFIN2_KEY}"')
        with urllib.request.urlopen(req, timeout=10) as resp:
            pass
    except Exception:
        pass


def _ensure_refresh_worker():
    global _refresh_worker_started
    with _refresh_worker_lock:
        if _refresh_worker_started:
            return
        _refresh_worker_started = True

        def _worker():
            while True:
                p = _refresh_queue.get()
                try:
                    _do_jellyfin_refresh_sync(p)
                except Exception:
                    pass
                finally:
                    _refresh_queue.task_done()
                    db.close()
                time.sleep(1.5)  # 限流：每 1.5s 串行刷新一集，保护首播带宽与 Jellyfin2 CPU

        threading.Thread(target=_worker, daemon=True, name="jf2-refresh-limiter").start()


def strm_path_for(cat, title, season, episode, *, variant=""):
    """推导 cinema2 中的 strm 路径。

    电影:   cinema2/<cat>/<title> (<year>)/<title> (<year>).strm
    剧集:   cinema2/<cat>/<title>/Season NN/<title> - SNNENN.strm
    """
    if variant not in ("",*VARIANT_LABELS):raise ValueError("未知版本")
    if episode:
        d = os.path.join(config.CINEMA2_DIR, cat, title, f"Season {season:02d}")
        f = f"{title} - S{season:02d}E{episode:02d}.strm"
    else:
        d = os.path.join(config.CINEMA2_DIR, cat, title)
        f = f"{title}.strm"
    if variant:f=f[:-5]+' - '+VARIANT_LABELS[variant]+'.strm'
    return os.path.join(d, f)


def ensure_variant(cat, title, season, episode, variant):
    """Create an opt-in version beside an existing, validated work directory."""
    from pathlib import Path
    from .identity import Work
    work=Work(cat,title,season,episode,variant)
    if not variant:raise ValueError('必须指定版本')
    root=Path(config.CINEMA2_DIR).resolve(strict=True)
    path=Path(strm_path_for(*work.args,variant=variant))
    if (any(value in ('.','..') or '/' in value or chr(92) in value for value in (cat,title))
            or root not in path.parent.resolve(strict=True).parents):
        raise ValueError('版本路径越界')
    if path.is_symlink():raise ValueError('版本入口不能是符号链接')
    if not path.exists():
        query=urllib.parse.urlencode({'cat':cat,'t':title,'s':season,'e':episode,'variant':variant})
        _atomic_write(str(path),PENDING_PREFIX+'?'+query)
    db.save_strm_map(*work.args,str(path),variant=variant)
    return str(path)


def library_search_identity(cat, title, season):
    """Use the exact work's NFO for search, leaving its storage key unchanged."""
    import re
    from pathlib import Path
    import xml.etree.ElementTree as ET
    from .identity import normalize, split_title

    if any(not part or part in ('.', '..') or '/' in part or chr(92) in part for part in (cat, title)):
        return title, ()
    root = Path(config.CINEMA2_DIR).absolute()
    kind = 'tvshow' if season or os.path.isfile(os.path.join(config.CINEMA2_DIR,cat,title,'tvshow.nfo')) else 'movie'
    path = root / cat / title / (kind + '.nfo')
    try:
        if path.resolve() != path or not path.is_file():
            return title, ()
        with path.open('rb') as stream:
            raw = stream.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            return title, ()
        metadata = ET.fromstring(raw)
    except (OSError, ValueError, RuntimeError, ET.ParseError):
        return title, ()

    name, folder_year = split_title(title)
    nfo_name, nfo_title_year = split_title(metadata.findtext('title'))
    nfo_name = normalize(re.sub(r'[\\/:*?"<>|\r\n\t]', ' ', nfo_name)).rstrip('.')
    if metadata.tag != kind or nfo_name.casefold() != name.casefold():
        return title, ()
    year = normalize(metadata.findtext('year'))
    years = {y for y in (folder_year, nfo_title_year, year) if re.fullmatch(r'(?:19|20)\d{2}', y)}
    if len(years) > 1:
        return title, ()
    original = normalize(metadata.findtext('originaltitle'))
    aliases=[]
    for value in [original]+[element.text or '' for element in metadata.findall('aka')]:
        value=normalize(value)
        if 0<len(value)<=120 and value.casefold()!=name.casefold() and value not in aliases:aliases.append(value)
    if season:
        season_nfo=root/cat/title/f'Season {season:02d}'/'season.nfo'
        try:
            if season_nfo.resolve()==season_nfo and season_nfo.stat().st_size<=128*1024:
                season_metadata=ET.fromstring(season_nfo.read_bytes())
                value=normalize(season_metadata.findtext('title'))
                if value and not re.fullmatch(r'(?i)Season\s*\d+|第\s*\d+\s*季',value):aliases.append(value)
        except (OSError,ValueError,RuntimeError,ET.ParseError):pass
    aliases=tuple(aliases)
    return f'{name} ({next(iter(years))})' if years else title, aliases


def scan_and_register():
    """Commit a complete library mapping scan in one transaction.

    Missing files are pruned only after successful enumeration of the same root.
    A missing/unreadable library or partial scan cannot erase existing mappings.
    """
    import re
    from . import coordination

    root = os.path.abspath(config.CINEMA2_DIR)
    if not os.path.isdir(root):
        return 0
    with coordination.file_lock("library", exclusive=True, timeout=5):
        root_before = os.stat(root)
        rows = []
        for cat in sorted(os.listdir(root)):
            catdir = os.path.join(root, cat)
            if not os.path.isdir(catdir):
                continue
            for title_dir in sorted(os.listdir(catdir)):
                tdir = os.path.join(catdir, title_dir)
                if not os.path.isdir(tdir):
                    continue
                title = title_dir.strip()
                for fn in sorted(os.listdir(tdir)):
                    path = os.path.join(tdir, fn)
                    if fn.endswith(".strm") and not re.search(r" - S\d+E\d+", fn) and os.path.isfile(path):
                        rows.append((cat, title, 0, 0, path, variant_from_path(path)))
                for sub in sorted(os.listdir(tdir)):
                    if not sub.startswith("Season "):
                        continue
                    sdir = os.path.join(tdir, sub)
                    if not os.path.isdir(sdir):
                        continue
                    try:
                        season = int(sub.split()[1])
                    except (IndexError, ValueError):
                        continue
                    for fn in sorted(os.listdir(sdir)):
                        match = re.search(r"- S(\d{1,3})E(\d{1,4})(?: - [^.]+)?\.strm$", fn)
                        if not match or int(match.group(1)) != season:
                            continue
                        path = os.path.join(sdir, fn)
                        if os.path.isfile(path):
                            rows.append((cat, title, season, int(match.group(2)), path, variant_from_path(path)))
        root_after = os.stat(root)
        if (root_before.st_dev, root_before.st_ino) != (root_after.st_dev, root_after.st_ino):
            raise RuntimeError("library root changed during registration scan")

        con = db._conn()
        con.execute("BEGIN IMMEDIATE")
        try:
            con.executemany(
                "INSERT OR REPLACE INTO strm_map (cat,title,season,episode,strm_path,variant) VALUES (?,?,?,?,?,?)",
                rows,
            )
            stale = []
            prefix = root.rstrip(os.sep) + os.sep
            for row in con.execute("SELECT cat,title,season,episode,strm_path,variant FROM strm_map"):
                path = os.path.abspath(row["strm_path"])
                if not path.startswith(prefix):
                    continue
                try:
                    os.stat(path)
                except FileNotFoundError:
                    stale.append((row["cat"], row["title"], row["season"], row["episode"], row["strm_path"],row["variant"]))
            con.executemany(
                "DELETE FROM strm_map WHERE cat=? AND title=? AND season=? AND episode=? AND strm_path=? AND variant=?",
                stale,
            )
            con.commit()
        except BaseException:
            con.rollback()
            raise
        return len(rows)


def read_strm(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except Exception:
        return ""


def is_pending(content):
    return content.startswith(PENDING_PREFIX)


def is_live(content):
    return content.startswith("http") and not is_pending(content)


def parse_live_remote_path(content):
    """从 live strm 内容反推网盘【写入侧】路径。

    live 内容形如：
      https://openlist.example.invalid/d/%E5%A4%B8%E5%85%8B/%E7%82%B9%E6%92%AD/...
    其中 %E5%A4%B8%E5%85%8B = /夸克（播放侧挂载，QuarkTV）。
    需要转回写入侧（/夸克网盘）才能用夸克官方 API 校验存在性。

    返回 /夸克网盘/点播/... 或 None（非 live / 无法解析）。

    2026-10-25 修复（976 条 8099/clean 死链自愈盲区）：
    clean 路由的内容形如
      https://vod.example.invalid/clean?url=<内层直链>&skip=8
    旧实现直接 urlparse 外层 URL，得到 path='/clean' 而非 '/d/...'，
    于是无条件返回 None，导致 selfheal 完全看不见所有 clean 流
    （实测 1012 条 live strm 中只识别到 36 条，976 条网盘已删的
     clean 死链永远卡在 502 且永不回退）。必须先解包内层 url 再解析。
    """
    import urllib.parse
    if not is_live(content):
        return None
    try:
        parsed = urllib.parse.urlparse(content)
        if parsed.path == '/play':
            remote = (urllib.parse.parse_qs(parsed.query).get('r') or [''])[0]
            remote = config.write_path(remote)
            if remote.startswith(config.QUARK_VOD_ROOT + '/') and '..' not in remote.split('/'):
                return remote
            return None
        # clean 代理：解包内层真实直链（内层 url 可能已被 quote 一层）
        if parsed.path.rstrip("/") == "/clean" or parsed.path.endswith("/clean"):
            inner = (urllib.parse.parse_qs(parsed.query).get("url") or [""])[0]
            if not inner or not inner.startswith("http"):
                return None
            content = urllib.parse.unquote(inner)
            parsed = urllib.parse.urlparse(content)
        u = parsed
        # path 形如 /d/夸克/点播/xxx
        p = urllib.parse.unquote(u.path)
    except Exception:
        return None
    if not p.startswith("/d/"):
        return None
    remote = p[len("/d"):]          # /夸克/点播/xxx
    # 播放侧挂载前缀 → 写入侧挂载前缀
    if remote == config.QUARK_PLAY_MOUNT or remote.startswith(config.QUARK_PLAY_MOUNT + "/"):
        remote = config.QUARK_MOUNT + remote[len(config.QUARK_PLAY_MOUNT):]
    return remote


def parse_pending_params(content):
    """从 pending strm 内容解析出 cat/title/season/episode。

    返回 dict 或 None。用于自愈时回退/重建。
    """
    import urllib.parse
    if not is_pending(content):
        return None
    try:
        u = urllib.parse.urlparse(content)
        q = urllib.parse.parse_qs(u.query)
        return {
            "cat": (q.get("cat") or [""])[0],
            "title": (q.get("t") or [""])[0],
            "season": int((q.get("s") or ["0"])[0] or 0),
            "episode": int((q.get("e") or ["0"])[0] or 0),
            "variant": (q.get("variant") or [""])[0],
        }
    except Exception:
        return None


def _atomic_write(path, content):
    previous = os.stat(path) if os.path.exists(path) else None
    fd, temporary = tempfile.mkstemp(prefix='.strm-', dir=os.path.dirname(path))
    try:
        os.fchmod(fd, stat.S_IMODE(previous.st_mode) if previous else 0o644)
        if previous and os.geteuid() == 0:
            os.fchown(fd, previous.st_uid, previous.st_gid)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content + '\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _is_configured_openlist_download(url):
    """Share the configured-origin/path rules with the media transport."""
    return download_http.allowed_openlist_url(url)


def write_live(path, direct_url, force=False):
    """把 strm 写成直链（原子写入）。若内容相同则跳过。

    2026-10-20 修复「画质降级锁定」：旧实现只允许「普通直链 → clean 直链」
    单向升级，一旦某集被写成 1080P 直链，后续即使找到 4K 臻彩源也无法替换，
    导致全库画质被首个命中源永久锁死。
    现在改为按「源等级」比较，允许低等级被高等级覆盖：
      clean（含伪装清洗，通常是 4K 原盘/臻彩） > 网盘直链 > CMS m3u8
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    cur = read_strm(path)
    if cur == direct_url:
        return False
    if is_live(cur) and not force:
        def _rank(u):
            # 2026-10-20：clean 与 direct 都是「夸克网盘源」，必须视为同一等级，
            # 否则 clean→direct 的画质升级会被误判为降级而拒绝
            # （唐宫奇案：1080P clean 源挡住了 4K 臻彩 direct 源）。
            # 只有 CMS 第三方切片才应被网盘源覆盖，反之不允许。
            if "/clean?" in u or "/play?" in u or _is_configured_openlist_download(u):
                return 2
            if ".m3u8" in u:
                return 1
            return 0
        if _rank(direct_url) < _rank(cur):
            return False
    _atomic_write(path, direct_url)
    return True


def restore_pending(path, cat, title, season, episode, *, variant=None):
    """把 strm 还原为 pending 占位（清理后调用）。

    ⚠️ 严禁剥离年份后缀：pending 的 t 参数必须与 cinema2 目录名保持一致
    （如「功夫 (2004)」）。2026-10-20 全库校对发现，旧实现在这里调用
    sanitize_title 把年份抹掉，导致还原后的点播请求丢失年份信号，
    同名不同片（《功夫 2004》vs《功夫旋风儿 1984》）互相串台。
    这里只清洗不可见控制字符，保留年份。
    """
    if not os.path.isfile(path):
        return False
    from .search import strip_invisible
    safe_title = strip_invisible(title) or title
    import urllib.parse
    if variant is None:
        variant=variant_from_path(path)
    if variant not in ("",*VARIANT_LABELS):raise ValueError('未知版本')
    params={"cat":cat,"t":safe_title,"s":season,"e":episode}
    if variant:params['variant']=variant
    q = urllib.parse.urlencode(params)
    url = f"{PENDING_PREFIX}?{q}"
    cur = read_strm(path)
    if is_pending(cur):
        return False
    _atomic_write(path, url)
    return True


def activation_paths(cat, title, season, episode, *, variant=""):
    """Return exact identity-matched paths used by activation and replacement rollback."""
    from .identity import same_title, media_kind
    opts={"variant":variant} if variant else {}
    actual_title = db.library_title(cat, title, season, episode, **opts)
    rows = db._conn().execute(
        'SELECT cat,title,strm_path FROM strm_map WHERE season=? AND episode=? AND variant=?',
        (season, episode,variant)).fetchall()
    paths = set()
    for row in rows:
        path = row['strm_path']
        if (not os.path.isfile(path) or os.path.islink(path)
                or variant_from_path(path)!=variant
                or os.path.commonpath((os.path.realpath(path),os.path.realpath(config.CINEMA2_DIR)))!=os.path.realpath(config.CINEMA2_DIR)):
            continue
        import re
        episode_hint = re.search(r'S(\d+)E(\d+)(?: - [^.]+)?\.strm$', os.path.basename(path))
        if episode_hint and tuple(map(int, episode_hint.groups())) != (season, episode):
            continue
        kind, name, year = _work_identity(path)
        path_title = f'{name} ({year})' if year else name
        if kind == media_kind(cat, season) and same_title(actual_title, path_title):
            paths.add(path)
    # Support newly generated files not yet registered by a background scan.
    fallback = _fuzzy_find(cat, actual_title, season, episode, **opts)
    if (fallback and not os.path.islink(fallback)
            and variant_from_path(fallback)==variant
            and os.path.commonpath((os.path.realpath(fallback),os.path.realpath(config.CINEMA2_DIR)))==os.path.realpath(config.CINEMA2_DIR)):
        paths.add(fallback)
    if not paths:
        raise FileNotFoundError(f'找不到同一作品的 STRM: {cat}/{title} S{season}E{episode}')
    return sorted(paths)


def activate(cat, title, season, episode, direct_url, background_batch=False, *, variant=""):
    """Activate only paths whose folder identity agrees with the requested work."""
    opts={"variant":variant} if variant else {}
    paths=activation_paths(cat,title,season,episode,variant=variant)
    if not paths:raise FileNotFoundError('没有可激活的同作品 STRM')
    remote = parse_live_remote_path(direct_url)
    changed = False
    for path in sorted(paths):
        url = direct_url
        if remote:
            # Every new playback returns through the resolver. Deleting a leaf
            # or expiring a save can no longer leave a permanent direct dead link.
            rel = os.path.relpath(path, config.CINEMA2_DIR).split(os.sep)
            query = urllib.parse.urlencode({'cat':rel[0], 't':rel[1], 's':season, 'e':episode, 'r':remote, **opts})
            url = config.PENDING_BASE_URL + '/play?' + query
        wrote = write_live(path, url)
        changed = changed or wrote
        if wrote:
            trigger_jellyfin_refresh_item(path, throttled=background_batch)
    primary = next((p for p in sorted(paths) if os.path.relpath(p, config.CINEMA2_DIR).split(os.sep)[0] == cat), sorted(paths)[0])
    db.save_strm_map(cat, title, season, episode, primary, **opts)
    _sync_transfer_done(cat, [title], season, episode, parse_live_remote_path(direct_url), variant=variant)
    return changed


def _sync_transfer_done(cat, title_candidates, season, episode, remote_path, *, variant=""):
    """仅修复同分类、标题、季集、版本且精确远端一致的既有记录。"""
    import time as _time
    if not remote_path:
        return
    remote_candidates = (config.write_path(remote_path), config.play_path(remote_path))
    try:
        con = db._conn()
        now_ts = int(_time.time())
        touched = 0
        for t_cand in title_candidates:
            cur = con.execute(
                "SELECT id FROM transfers WHERE cat=? AND title=? AND season=? AND episode=? "
                "AND variant=? AND remote_path IN (?,?)",
                (cat, t_cand, season, episode, variant, *remote_candidates))
            ids = [r["id"] for r in cur.fetchall()]
            for tid in ids:
                con.execute(
                    "UPDATE transfers SET status='done', error=NULL, updated_at=? WHERE id=? AND status<>'done'",
                    (now_ts, tid))
                touched += 1
        if touched:
            con.commit()
            print(f"[{config.SERVICE_NAME}] [DB同步] {cat}/{title_candidates[0]} "
                  f"S{season}E{episode}: {touched} 条记录已覆盖为 done", flush=True)
        else:
            print(f"[{config.SERVICE_NAME}] [DB同步] {cat}/{title_candidates[0]} "
                  f"S{season}E{episode}: 无既有记录可覆盖（strm 已为活链）", flush=True)
    except Exception as e:
        print(f"[{config.SERVICE_NAME}] [DB同步失败] {cat}/{title_candidates[0]} "
              f"S{season}E{episode}: {e}", flush=True)


def trigger_jellyfin_refresh_item(strm_path, throttled=False):
    """异步通知 Jellyfin2 刷新单个条目的媒体流信息（保障 DirectPlay，杜绝未知流强制转码）。

    throttled=True 时走后台串行限流队列（间隔 1.5s），防止整季 40 集连带激活时
    40 个并发 FullRefresh 挤占当前首播集的网络带宽与 Jellyfin2 CPU。
    """
    if not strm_path or not os.path.isfile(strm_path):
        return
    if throttled:
        _ensure_refresh_worker()
        _refresh_queue.put(strm_path)
        return
    threading.Thread(target=_do_jellyfin_refresh_sync, args=(strm_path,), daemon=True).start()


def _strm_path_by_title(title, season, episode, cat=None):
    """仅按 title 在 strm_map 中定位 strm（忽略可能脏的 cat）。

    同一部片可能存在于多个专区（如《霸王别姬》同时在 国产电影/ 与 港台经典/），
    此时返回全部匹配项中的第一个存在的文件。

    2026-10-20 修复跨专区污染：传入 cat 时优先返回该专区内的路径。
    """
    try:
        rows = db._conn().execute(
            "SELECT cat, strm_path FROM strm_map WHERE title=? AND season=? AND episode=?",
            (title, season, episode)).fetchall()
    except Exception:
        return None
    if cat:
        for r in rows:
            p = r["strm_path"]
            if r["cat"] == cat and p and os.path.isfile(p):
                return p
    for r in rows:
        p = r["strm_path"]
        if p and os.path.isfile(p):
            return p
    return None


def _work_identity(strm_path):
    """从 strm 路径反推作品身份：(媒体类别, 去年份片名, 年份或 '')。

    用于跨专区回写前的同作品校验，杜绝《凡人修仙传》(动漫) 与
    《凡人修仙传 (2025)》(真人剧) 这类同名不同作品互相污染。

    媒体类别由专区名推断，是最可靠的隔离信号：
      动漫类（国产动漫/日本动漫）/ 剧集类（*剧集/热门韩剧）/ 电影类（*电影/欧美大片/港台经典）
    """
    import re
    try:
        rel = os.path.relpath(strm_path, config.CINEMA2_DIR)
        parts = rel.split(os.sep)
        cat = parts[0] if parts else ""
        title_dir = parts[1] if len(parts) > 1 else ""
    except Exception:
        cat, title_dir = "", ""

    if "动漫" in cat:
        media_class = "anime"
    elif "剧" in cat:
        media_class = "tv"
    else:
        media_class = "movie"

    m = re.search(r"[(（](\d{4})[)）]\s*$", title_dir)
    year = m.group(1) if m else ""
    name = re.sub(r"\s*[(（]\d{4}[)）]\s*$", "", title_dir).strip()
    return media_class, name, year


def _same_work(path_a, path_b):
    """判断两个 strm 是否属于同一部作品（跨专区回写前的强制校验）。

    规则：
      1) 媒体类别必须一致（动漫 / 剧集 / 电影）——动漫与真人剧同名极常见；
      2) 去年份片名必须一致；
      3) 若双方都带年份，年份也必须一致。
    仅片名相同但类别或年份不同 → 判定为不同作品。
    """
    ca, na, ya = _work_identity(path_a)
    cb, nb, yb = _work_identity(path_b)
    if ca != cb:
        return False
    if not na or not nb or na != nb:
        return False
    if ya and yb and ya != yb:
        return False
    return True


def _all_strm_paths_by_title(title, season, episode, *, variant=""):
    """返回该片所有存在的 strm 路径（多专区时需全部回写）。"""
    out = []
    try:
        rows = db._conn().execute(
            "SELECT strm_path FROM strm_map WHERE title=? AND season=? AND episode=? AND variant=?",
            (title, season, episode,variant)).fetchall()
    except Exception:
        return out
    for r in rows:
        p = r["strm_path"]
        if p and os.path.isfile(p):
            out.append(p)
    return out


def _fuzzy_find(cat, title, season, episode, *, variant=""):
    """目录名带年份等情况时模糊匹配。

    2026-10-16 修复：目录名与文件名存在年份差异时自适应匹配。
    """
    import re
    from .search import sanitize_title
    catdir = os.path.join(config.CINEMA2_DIR, cat)
    if not os.path.isdir(catdir):
        return None
    core = sanitize_title(title)

    def _match(d):
        dn = sanitize_title(d)
        from .identity import same_title
        return same_title(title, d)

    cands = [d for d in os.listdir(catdir)
             if os.path.isdir(os.path.join(catdir, d)) and _match(d)]
    if not cands:
        return None
    cands.sort(key=lambda d: abs(len(d) - len(title or "")))

    for cand in cands:
        base = os.path.join(catdir, cand)
        if episode:
            d = os.path.join(base, f"Season {season:02d}")
            if os.path.isdir(d):
                target_token = f"S{season:02d}E{episode:02d}"
                for fn in sorted(os.listdir(d)):
                    if fn.endswith(".strm") and variant_from_path(fn)==variant and re.search(rf'S0*{season}E0*{episode}(?: - [^.]+)?\.strm$', fn):
                        fp = os.path.join(d, fn)
                        if os.path.isfile(fp):
                            return fp
        else:
            for fn in sorted(os.listdir(base)):
                if fn.endswith(".strm") and (not re.search(r" - S\d+E\d+", fn)) and variant_from_path(fn)==variant:
                    fp = os.path.join(base, fn)
                    if os.path.isfile(fp):
                        return fp
    return None

def episode_context(cat, title, season, episode):
    """Read bounded local episode metadata; incomplete previous seasons give no offset."""
    from pathlib import Path
    import xml.etree.ElementTree as ET
    if any(not value or value in ('.','..') or '/' in value or chr(92) in value for value in (cat,title)):
        return 0,''
    root=Path(config.CINEMA2_DIR).absolute()
    work=root/cat/title
    if work.resolve()!=work or not work.is_dir():return 0,''
    offset=0
    for number in range(1,season):
        directory=work/f'Season {number:02d}'
        try:
            episodes=set()
            for path in list(directory.glob('*.nfo'))[:1001]:
                if path.is_symlink() or path.stat().st_size>128*1024:continue
                doc=ET.fromstring(path.read_bytes())
                if doc.tag!='episodedetails':continue
                if int(doc.findtext('season','-1'))==number:
                    episodes.add(int(doc.findtext('episode','-1')))
            if not episodes or episodes!=set(range(1,max(episodes)+1)):return 0,''
            offset+=len(episodes)
        except (OSError,ValueError,ET.ParseError):return 0,''
    aired=''
    try:
        for path in list((work/f'Season {season:02d}').glob('*.nfo'))[:1001]:
            if path.is_symlink() or path.stat().st_size>128*1024:continue
            doc=ET.fromstring(path.read_bytes())
            if (doc.tag=='episodedetails' and int(doc.findtext('season','-1'))==season
                    and int(doc.findtext('episode','-1'))==episode):
                aired=doc.findtext('aired','').replace('-','')
                if len(aired)!=8 or not aired.isdigit():aired=''
                break
    except (OSError,ValueError,ET.ParseError):pass
    return offset,aired
