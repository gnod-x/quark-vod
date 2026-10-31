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
import queue
import threading
import time

from . import config
from . import db
from . import cleaner

PENDING_PREFIX = f"{config.PENDING_BASE_URL}/pending"

# 后台限流刷新队列（解决全季 40 集连带激活时并发发起 40 个 FullRefresh 挤占首播带宽）
_refresh_queue: "queue.Queue[str]" = queue.Queue()
_refresh_worker_started = False
_refresh_worker_lock = threading.Lock()


def _do_jellyfin_refresh_sync(strm_path: str):
    """同步执行单个条目的 Jellyfin2 流信息刷新（免锁只读查询 + ReplaceAllMetadata=false）。"""
    try:
        from contextlib import closing
        import urllib.request
        import sqlite3
        if not strm_path or not os.path.isfile(strm_path):
            return
        container_path = strm_path.replace(config.CINEMA2_DIR, "/cinema2")
        with closing(sqlite3.connect(f"file:{config.JELLYFIN2_DB}?mode=ro&immutable=1", uri=True)) as con:
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
                time.sleep(1.5)  # 限流：每 1.5s 串行刷新一集，保护首播带宽与 Jellyfin2 CPU

        threading.Thread(target=_worker, daemon=True, name="jf2-refresh-limiter").start()


def strm_path_for(cat, title, season, episode):
    """推导 cinema2 中的 strm 路径。

    电影:   cinema2/<cat>/<title> (<year>)/<title> (<year>).strm
    剧集:   cinema2/<cat>/<title>/Season NN/<title> - SNNENN.strm
    """
    if season:
        d = os.path.join(config.CINEMA2_DIR, cat, title, f"Season {season:02d}")
        f = f"{title} - S{season:02d}E{episode:02d}.strm"
    else:
        d = os.path.join(config.CINEMA2_DIR, cat, title)
        f = f"{title}.strm"
    return os.path.join(d, f)


def scan_and_register():
    """扫描 cinema2，把 strm 登记到 strm_map（供回写定位）。

    路径规则（与 tmdb_populate.py 一致）：
      电影: <cat>/<title> [(year)]/<title> [(year)].strm
      剧集: <cat>/<title>/Season NN/<title> - SNNENN.strm
    剧集的 title 取影片目录名（去掉年份后缀）。
    """
    import re
    registered = 0
    if not os.path.isdir(config.CINEMA2_DIR):
        return 0
    for cat in sorted(os.listdir(config.CINEMA2_DIR)):
        catdir = os.path.join(config.CINEMA2_DIR, cat)
        if not os.path.isdir(catdir):
            continue
        for title_dir in sorted(os.listdir(catdir)):
            tdir = os.path.join(catdir, title_dir)
            if not os.path.isdir(tdir):
                continue
            # 去掉 " (2026)" 年份后缀，得到纯 title
            title = re.sub(r"\s*\(\d{4}\)\s*$", "", title_dir).strip()

            # 电影：目录下直接有 .strm
            for fn in sorted(os.listdir(tdir)):
                if fn.endswith(".strm") and " - S" not in fn:
                    db.save_strm_map(cat, title, 0, 0, os.path.join(tdir, fn))
                    registered += 1

            # 剧集：Season NN 子目录
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
                    m = re.search(r"- S(\d{2})E(\d{2})\.strm$", fn)
                    if not m:
                        continue
                    episode = int(m.group(2))
                    db.save_strm_map(cat, title, season, episode,
                                     os.path.join(sdir, fn))
                    registered += 1
    return registered


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
      http://192.168.101.240:5244/d/%E5%A4%B8%E5%85%8B/%E7%82%B9%E6%92%AD/...
    其中 %E5%A4%B8%E5%85%8B = /夸克（播放侧挂载，QuarkTV）。
    需要转回写入侧（/夸克网盘）才能用夸克官方 API 校验存在性。

    返回 /夸克网盘/点播/... 或 None（非 live / 无法解析）。

    2026-10-25 修复（976 条 8099/clean 死链自愈盲区）：
    clean 路由的内容形如
      http://192.168.101.240:8099/clean?url=<内层直链>&skip=8
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
        }
    except Exception:
        return None


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
            if "/clean?" in u or "5244/d/" in u:
                return 2
            if ".m3u8" in u:
                return 1
            return 0
        if _rank(direct_url) < _rank(cur):
            return False
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(direct_url + "\n")
    os.replace(tmp, path)
    return True


def restore_pending(path, cat, title, season, episode):
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
    q = urllib.parse.urlencode({"cat": cat, "t": safe_title, "s": season, "e": episode})
    url = f"{PENDING_PREFIX}?{q}"
    cur = read_strm(path)
    if is_pending(cur):
        return False
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(url + "\n")
    os.replace(tmp, path)
    return True


def activate(cat, title, season, episode, direct_url, background_batch=False):
    """转存成功后：登记 strm_map + 写 live + 同步 DB 记录状态。

    具备工业级标题脱水与四级自适应定位容错能力：
      1) 统一剥离 Unicode 控制符与 (2025) 年份后缀
      2) 候选标题双向索引 (clean_title + raw_title)
      3) 自适应匹配跨专区多重 strm

    2026-10-21 修复（152 条 DB 滞后导致重复转存）：原实现只写 strm 文件与
    strm_map，DB 状态完全依赖调用方 upsert。而 4 个调用方写入的 title 用的是
    【原始带年份名】（如「霸王别姬 (1993)」），selfheal 作废时又按 remote_path
    维度匹配，两者对不上 → 出现「strm 是活链、transfers 却是 cleaned/failed」，
    `find_done_transfer` 查不到 → 下次点播重复跑 8~12s 搜索转存。
    现在由 activate() 自己把该 (cat,title,season,episode) 的记录覆盖为 done，
    覆盖时同时兼容带年份与无年份两种 title 形态，确保 DB 永不落后于磁盘。
    """
    from .search import sanitize_title
    clean_title = sanitize_title(title)
    title_candidates = [t for t in [clean_title, title] if t]
    path = None

    for t_cand in title_candidates:
        # 1) strm_map 精确（cat+title+season+episode）
        try:
            p = db.get_strm_path(cat, t_cand, season, episode)
            if p and os.path.isfile(p):
                path = p
                break
        except Exception:
            pass
        # 2) strm_map 仅按 title 查（忽略脏 cat），但优先同专区
        p = _strm_path_by_title(t_cand, season, episode, cat=cat)
        if p and os.path.isfile(p):
            path = p
            break
        # 3) 路径推导
        p = strm_path_for(cat, t_cand, season, episode)
        if p and os.path.isfile(p):
            path = p
            break
        # 4) 模糊查找
        p = _fuzzy_find(cat, t_cand, season, episode)
        if p and os.path.isfile(p):
            path = p
            break

    if not path:
        raise FileNotFoundError(f"找不到 strm: {cat}/{title} S{season}E{episode}")

    try:
        db.save_strm_map(cat, clean_title, season, episode, path)
        if clean_title != title:
            db.save_strm_map(cat, title, season, episode, path)
    except Exception:
        pass

    # 自动文件头混淆嗅探与透明清洗
    try:
        direct_url = cleaner.clean_url_if_obfuscated(direct_url)
    except Exception:
        pass

    changed = write_live(path, direct_url)
    trigger_jellyfin_refresh_item(path, throttled=background_batch)

    # 同一部片可能存在于多个专区（实测《霸王别姬》同时在 国产电影/ 与 港台经典/），
    # 必须把【所有】专区下的 strm 一并回写。
    # 2026-10-20 修复跨专区污染：必须通过 _same_work 校验同作品（片名+年份），
    # 严禁把《凡人修仙传》(2020 动漫) 的直链写进《凡人修仙传 (2025)》(真人剧)。
    seen_paths = {os.path.abspath(path)}
    for t_cand in title_candidates:
        for p in _all_strm_paths_by_title(t_cand, season, episode):
            ap = os.path.abspath(p)
            if ap in seen_paths:
                continue
            if not _same_work(path, p):
                continue
            seen_paths.add(ap)
            try:
                if write_live(p, direct_url):
                    trigger_jellyfin_refresh_item(p, throttled=True)
            except Exception:
                pass

    # ── DB 覆盖：把该影片该集的记录同步为 done（2026-10-21）──
    # strm 已确为活链，DB 必须跟上，否则 find_done_transfer 落空导致重复转存。
    # 覆盖顺序：先精确四字段，再退化到 (title,season,episode) 忽略 cat 的脏记录，
    # 两种 title 形态（带/不带年份）都试，确保历史脏数据被一并纠正。
    _sync_transfer_done(cat, title_candidates, season, episode)
    return changed


def _sync_transfer_done(cat, title_candidates, season, episode):
    """把 (cat, title, season, episode) 对应的 transfers 记录覆盖为 done。

    2026-10-21 新增：activate() 的 DB 自愈步骤。处理三类历史脏数据：
      1) status 为 cleaned/failed 但 strm 已是活链（自愈作废与转存成功竞态）；
      2) cat 不一致（同片被放进多个专区，upsert 时用了另一个 cat）；
      3) title 带/不带年份两种形态各有一条记录，只更新了一条。
    找不到任何记录时不插入（避免为从未走转存流程的条目造假），
    仅打印提示交由调用方判断。
    """
    import time as _time
    try:
        con = db._conn()
        now_ts = int(_time.time())
        touched = 0
        for t_cand in title_candidates:
            cur = con.execute(
                "SELECT id FROM transfers WHERE cat=? AND title=? AND season=? AND episode=?",
                (cat, t_cand, season, episode))
            ids = [r["id"] for r in cur.fetchall()]
            if not ids:
                # 退化：忽略 cat 与 title 年份形态，只按 season/episode + 名称包含
                cur = con.execute(
                    "SELECT id, title FROM transfers WHERE season=? AND episode=? "
                    "AND (title=? OR title=?)",
                    (season, episode, t_cand, t_cand))
                ids = [r["id"] for r in cur.fetchall()]
            for tid in ids:
                con.execute(
                    "UPDATE transfers SET status='done', error=NULL, updated_at=? WHERE id=?",
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


def _all_strm_paths_by_title(title, season, episode):
    """返回该片所有存在的 strm 路径（多专区时需全部回写）。"""
    out = []
    try:
        rows = db._conn().execute(
            "SELECT strm_path FROM strm_map WHERE title=? AND season=? AND episode=?",
            (title, season, episode)).fetchall()
    except Exception:
        return out
    for r in rows:
        p = r["strm_path"]
        if p and os.path.isfile(p):
            out.append(p)
    return out


def _fuzzy_find(cat, title, season, episode):
    """目录名带年份等情况时模糊匹配。

    2026-10-16 修复：目录名与文件名存在年份差异时自适应匹配。
    """
    from .search import sanitize_title
    catdir = os.path.join(config.CINEMA2_DIR, cat)
    if not os.path.isdir(catdir):
        return None
    core = sanitize_title(title)

    def _match(d):
        dn = sanitize_title(d)
        return bool(core) and (dn == core or core in dn or dn in core)

    cands = [d for d in os.listdir(catdir)
             if os.path.isdir(os.path.join(catdir, d)) and _match(d)]
    if not cands:
        return None
    cands.sort(key=lambda d: abs(len(d) - len(title or "")))

    for cand in cands:
        base = os.path.join(catdir, cand)
        if season:
            d = os.path.join(base, f"Season {season:02d}")
            if os.path.isdir(d):
                target_token = f"S{season:02d}E{episode:02d}"
                for fn in sorted(os.listdir(d)):
                    if fn.endswith(".strm") and target_token in fn:
                        fp = os.path.join(d, fn)
                        if os.path.isfile(fp):
                            return fp
        else:
            for fn in sorted(os.listdir(base)):
                if fn.endswith(".strm") and (" - S" not in fn):
                    fp = os.path.join(base, fn)
                    if os.path.isfile(fp):
                        return fp
    return None
