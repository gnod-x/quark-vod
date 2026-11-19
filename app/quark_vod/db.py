#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
点播状态库（SQLite）

记录三部曲：转存 -> 播放 -> 清理。
所有表允许幂等重复写（INSERT OR REPLACE），便于断点续跑。
"""

import os
import sqlite3
import threading
import time

from . import config

_local = threading.local()


def _conn():
    if not hasattr(_local, "conn"):
        os.makedirs(os.path.dirname(config.STATE_DB), exist_ok=True)
        con = sqlite3.connect(config.STATE_DB, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        _local.conn = con
    return _local.conn


SCHEMA = """
-- 转存任务：一次点播对应一条
CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cat TEXT NOT NULL,                 -- 专区（国产剧集/欧美大片/...）
    title TEXT NOT NULL,               -- 影片标题（与 cinema2 目录名一致）
    season INTEGER NOT NULL DEFAULT 0, -- 季（电影为 0）
    episode INTEGER NOT NULL DEFAULT 0,-- 集（电影为 0）
    share_url TEXT,                    -- 来源分享链接
    share_pwd TEXT,                    -- 分享密码
    pan_type TEXT,                     -- 网盘类型（quark）
    status TEXT NOT NULL DEFAULT 'pending',
        -- pending   待转存
        -- searching 搜索中
        -- found     已找到分享
        -- saving    转存中
        -- done      转存完成
        -- failed    失败
        -- cleaned   已清理
    error TEXT,
    remote_path TEXT,                  -- 网盘内的最终路径
    file_size INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transfers_lookup
    ON transfers(cat, title, season, episode, status);

-- strm 映射：影片 -> 本地 strm 文件路径（回写直链用）
CREATE TABLE IF NOT EXISTS strm_map (
    cat TEXT NOT NULL,
    title TEXT NOT NULL,
    season INTEGER NOT NULL DEFAULT 0,
    episode INTEGER NOT NULL DEFAULT 0,
    strm_path TEXT NOT NULL,
    PRIMARY KEY (cat, title, season, episode)
);

-- 播放记录：Wholphin 点开即记
CREATE TABLE IF NOT EXISTS plays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cat TEXT NOT NULL,
    title TEXT NOT NULL,
    season INTEGER NOT NULL DEFAULT 0,
    episode INTEGER NOT NULL DEFAULT 0,
    transfer_id INTEGER,
    played_at INTEGER NOT NULL,
    finished INTEGER NOT NULL DEFAULT 0  -- 是否看到尾（1=看完）
);
CREATE INDEX IF NOT EXISTS idx_plays_lookup
    ON plays(cat, title, season, episode);

-- Media resolution access (not a watched/finished event).
CREATE TABLE IF NOT EXISTS playback_access (
    transfer_id INTEGER PRIMARY KEY,
    accessed_at INTEGER NOT NULL
);

-- 清理队列：到期待删
CREATE TABLE IF NOT EXISTS cleanup_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id INTEGER NOT NULL,
    remote_path TEXT NOT NULL,
    due_at INTEGER NOT NULL,           -- 到期时间戳
    cleaned INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cleanup_due ON cleanup_queue(cleaned, due_at);

-- 死链/失效分享黑名单表（秒级熔断与自动拉黑）
CREATE TABLE IF NOT EXISTS dead_sources (
    source_url TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,         -- panshare / cms
    reason TEXT,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL        -- 默认拉黑 7 天，到期自动解封
);
CREATE INDEX IF NOT EXISTS idx_dead_sources_expires ON dead_sources(expires_at);

-- 片源可用性与 4K 质量预检审计表 (Pre-Flight VOD Audit)
CREATE TABLE IF NOT EXISTS source_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cat TEXT NOT NULL,
    title TEXT NOT NULL,
    season INTEGER NOT NULL DEFAULT 0,
    episode INTEGER NOT NULL DEFAULT 0,
    has_quark_source INTEGER NOT NULL DEFAULT 0, -- 1=有有效夸克网盘源, 0=无
    is_4k INTEGER NOT NULL DEFAULT 0,             -- 1=确认含4K/2160p/UHD, 0=普通高清
    best_share_url TEXT,                         -- 锁定的最高品质存活分享链接
    best_share_title TEXT,                       -- 该分享包的真实资源标题 (如 "xxx 4K HDR10")
    checked_at INTEGER NOT NULL,                 -- 最近检查时间戳
    check_status TEXT NOT NULL DEFAULT 'unknown' -- valid_4k / valid_hd / dead / not_found
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_source_audits_item
    ON source_audits(cat, title, season, episode);
CREATE INDEX IF NOT EXISTS idx_source_audits_status
    ON source_audits(check_status, is_4k);

-- 用户手动预热/点播/收藏置顶保护表（免受年份淘汰与 LRU 清理）
CREATE TABLE IF NOT EXISTS user_pinned (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cat TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT 'preheated', -- preheated, manual_add, played, favorited
    pinned_at INTEGER NOT NULL,
    UNIQUE(cat, title)
);
CREATE INDEX IF NOT EXISTS idx_user_pinned ON user_pinned(cat, title);

-- AI 文件名语义解析结果缓存（2026-11-13 试点）
-- key = sha1(filename + "|" + season)；episode 为 NULL 表示「确认为非集数」（电影正片等）
CREATE TABLE IF NOT EXISTS name_parse_cache (
    cache_key TEXT PRIMARY KEY,
    file_name TEXT NOT NULL,
    season INTEGER NOT NULL DEFAULT 0,
    episode INTEGER,                   -- NULL = 非集数；整数 = 集数
    confidence REAL DEFAULT 0,
    origin TEXT NOT NULL DEFAULT 'ai', -- ai / shadow（影子模式观测，不参与生效）
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_name_parse_cache_origin ON name_parse_cache(origin);
"""


def init_db():
    con = _conn()
    con.executescript(SCHEMA)
    con.execute('BEGIN IMMEDIATE')
    try:
        columns = {r[1] for r in con.execute('PRAGMA table_info(transfers)')}
        for name, kind in [('save_task_id','TEXT'),('save_target','TEXT'),('save_started_at','INTEGER')]:
            if name not in columns:
                con.execute(f'ALTER TABLE transfers ADD COLUMN {name} {kind}')
        con.commit()
    except BaseException:
        con.rollback()
        raise


def save_source_audit(cat, title, season, episode, has_quark_source, is_4k,
                      best_share_url="", best_share_title="", check_status="unknown"):
    """保存或更新片源预检审计结果。"""
    now_ts = now()
    try:
        _conn().execute("""
            INSERT OR REPLACE INTO source_audits
            (cat, title, season, episode, has_quark_source, is_4k, best_share_url, best_share_title, checked_at, check_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (cat, title, season, episode, int(has_quark_source), int(is_4k),
              best_share_url, best_share_title, now_ts, check_status))
        _conn().commit()
    except Exception:
        pass


def get_source_audit(cat, title, season=0, episode=0):
    """获取指定条目的最新片源预检记录。"""
    try:
        return _conn().execute("""
            SELECT * FROM source_audits
            WHERE cat=? AND title=? AND season=? AND episode=?
            LIMIT 1
        """, (cat, title, season, episode)).fetchone()
    except Exception:
        return None


def get_locked_4k_share(cat, title, season=0, episode=0):
    """提取经预检验证存活且为 4K 的锁定夸克分享源。

    2026-11-08：追加 check_status 过滤。旧实现只看 has_quark_source 与
    best_share_url，审计已判 dead / 空心分享并作废后若仍有残留字段，
    仍会把它当成最高优先级片源调取，导致播放/预热永远死在这条源上。
    """
    row = get_source_audit(cat, title, season, episode)
    if row and row["has_quark_source"] and row["best_share_url"]:
        if str(row["check_status"] or "").lower() in ("dead", "not_found", "invalid", "expired"):
            return None, False, ""
        # 再次确保不在黑名单中
        if not is_dead_source(row["best_share_url"]):
            return row["best_share_url"], bool(row["is_4k"]), row["best_share_title"]
    return None, False, ""


def bump_audit_checked_at(cat, title, season, share_url, ts=None):
    """把同一分享源在该季下所有剧集的 checked_at 刷新到最新。

    2026-11-08（末路兼职锁源失效事故）：同一整季大包会被锁到该季所有剧集行，
    原内联 SQL 只更新单行且易随表结构演进失配。统一收敛到本函数，
    一次性推进整季锁源时间戳，避免「部分集数已过期判定」导致的反复重审。
    """
    if not share_url:
        return
    try:
        con = _conn()
        con.execute(
            "UPDATE source_audits SET checked_at = ? "
            "WHERE cat=? AND title=? AND season=? AND best_share_url=?",
            (ts if ts is not None else now(), cat, title, season, share_url)
        )
        con.commit()
    except Exception:
        pass


def purge_audit_locked_share(cat, title, season, share_url):
    """作废某季下所有锁定了指定分享源的审计行（剧集回退为待补源）。"""
    if not share_url:
        return
    try:
        con = _conn()
        con.execute(
            "UPDATE source_audits SET has_quark_source=0, is_4k=0, best_share_url='', best_share_title='', "
            "check_status='dead', checked_at=? WHERE cat=? AND title=? AND season=? AND best_share_url=?",
            (now(), cat, title, season, share_url)
        )
        con.commit()
    except Exception:
        pass


def mark_dead_source(source_url, source_type="panshare", reason="", ban_days=7):
    """记录死链/失效分享源，进行秒级熔断。"""
    if not source_url:
        return
    now_ts = now()
    exp_ts = now_ts + ban_days * 86400
    try:
        _conn().execute(
            "INSERT OR REPLACE INTO dead_sources (source_url, source_type, reason, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (source_url.strip(), source_type, reason[:200], now_ts, exp_ts)
        )
        _conn().commit()
    except Exception:
        pass


def is_dead_source(source_url):
    """检查链接是否处于熔断黑名单中（未过期算死链）。"""
    if not source_url:
        return False
    now_ts = now()
    try:
        r = _conn().execute(
            "SELECT 1 FROM dead_sources WHERE source_url=? AND expires_at > ? LIMIT 1",
            (source_url.strip(), now_ts)
        ).fetchone()
        return bool(r)
    except Exception:
        return False


def get_dead_sources_set(source_type=None):
    """批量获取当前有效的死链集合，供内存极速比对。"""
    now_ts = now()
    try:
        if source_type:
            rows = _conn().execute(
                "SELECT source_url FROM dead_sources WHERE source_type=? AND expires_at > ?",
                (source_type, now_ts)
            ).fetchall()
        else:
            rows = _conn().execute(
                "SELECT source_url FROM dead_sources WHERE expires_at > ?",
                (now_ts,)
            ).fetchall()
        return {r["source_url"] for r in rows}
    except Exception:
        return set()


def now():
    return int(time.time())


# ── transfers ─────────────────────────────────────────
def upsert_transfer(cat, title, season, episode, **fields):
    """按 (cat,title,season,episode) 定位；不存在则插入。

    2026-10-25：入口统一清洗 cat/title 中的零宽字符与不可见控制符。
    实测 transfers 表同时存在「白日提灯」与「白日提灯​」各 40 条，
    因 TMDB 标题带零宽空格，同一个 (cat,title) 主键被拆成两组记录，
    导致 find_done_transfer 漏命中、重复转存。
    """
    try:
        from .search import strip_invisible
        cat = strip_invisible(cat) or cat
        title = strip_invisible(title) or title
    except Exception:
        pass
    con = _conn()
    allowed = {'status', 'share_url', 'share_pwd', 'pan_type', 'error', 'remote_path', 'file_size', 'save_task_id', 'save_target', 'save_started_at'}
    if not fields.keys() <= allowed:
        raise ValueError('invalid transfer fields')
    # Serialize SELECT+INSERT across threads and host cron processes.
    # Historical duplicates keep their queue/play references.
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute(
            "SELECT id FROM transfers WHERE cat=? AND title=? AND season=? AND episode=? "
            "ORDER BY updated_at DESC,id DESC LIMIT 1",
            (cat, title, season, episode)).fetchone()
        if row:
            sets = [f"{k}=?" for k in fields] + ['updated_at=?']
            con.execute(f"UPDATE transfers SET {', '.join(sets)} WHERE id=?",
                        (*fields.values(), now(), row['id']))
            tid = row['id']
        else:
            cols = ['cat', 'title', 'season', 'episode', 'created_at', 'updated_at', *fields.keys()]
            vals = [cat, title, season, episode, now(), now(), *fields.values()]
            cur = con.execute(f"INSERT INTO transfers ({','.join(cols)}) VALUES ({','.join('?' for _ in vals)})", vals)
            tid = cur.lastrowid
        con.commit()
        return tid
    except BaseException:
        con.rollback()
        raise


def get_transfer(cat, title, season, episode):
    return _conn().execute(
        "SELECT * FROM transfers WHERE cat=? AND title=? AND season=? AND episode=? "
        "ORDER BY updated_at DESC,id DESC LIMIT 1",
        (cat, title, season, episode)).fetchone()


def get_transfer_by_id(tid):
    return _conn().execute("SELECT * FROM transfers WHERE id=?", (tid,)).fetchone()


def list_transfers(status=None, limit=200):
    q = "SELECT * FROM transfers"
    args = []
    if status:
        q += " WHERE status=?"
        args.append(status)
    q += " ORDER BY updated_at DESC LIMIT ?"
    args.append(limit)
    return _conn().execute(q, args).fetchall()


def library_title(cat, title, season, episode):
    """Recover a legacy row's missing year using its unique on-disk mapping."""
    from .identity import split_title
    if split_title(title)[1]:
        return title
    rows = _conn().execute(
        'SELECT strm_path FROM strm_map WHERE cat=? AND title=? AND season=? AND episode=?',
        (cat, title, season, episode)).fetchall()
    names = set()
    for row in rows:
        path = os.path.abspath(row['strm_path'])
        root = os.path.abspath(config.CINEMA2_DIR)
        if not path.startswith(root + os.sep):
            continue
        parts = os.path.relpath(path, root).split(os.sep)
        if len(parts) >= 3 and split_title(parts[1])[0].casefold() == split_title(title)[0].casefold():
            names.add(parts[1])
    return next(iter(names)) if len(names) == 1 else title


def find_done_transfer(cat, title, season, episode):
    """Reuse only an identical work and media kind; never guess a missing year."""
    from .identity import same_title, media_kind
    rows = _conn().execute(
        "SELECT * FROM transfers WHERE season=? AND episode=? AND status='done' "
        "AND remote_path IS NOT NULL AND remote_path<>'' ORDER BY updated_at DESC,id DESC",
        (season, episode)).fetchall()
    candidates = []
    for row in rows:
        if media_kind(row['cat'], season) != media_kind(cat, season):
            continue
        actual_title = library_title(row['cat'], row['title'], season, episode)
        if same_title(title, actual_title):
            candidates.append(row)
    candidates.sort(key=lambda row: row['cat'] != cat)
    return candidates[0] if candidates else None


# ── name_parse_cache（AI 文件名解析缓存）────────────────
def get_name_parse_cache(keys, origin="ai"):
    """批量读取解析缓存。返回 {cache_key: episode_or_None}；未命中不出现在结果中。"""
    if not keys:
        return {}
    con = _conn()
    out = {}
    keys = list(keys)
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        q = ("SELECT cache_key, episode FROM name_parse_cache WHERE origin=? AND cache_key IN (%s)"
             % ",".join("?" * len(chunk)))
        for row in con.execute(q, [origin] + chunk):
            out[row["cache_key"]] = row["episode"]
    return out


def put_name_parse_cache(rows, origin="ai"):
    """批量写入解析缓存。rows = [(cache_key, file_name, season, episode, confidence)]"""
    if not rows:
        return
    con = _conn()
    now = int(time.time())
    con.executemany(
        "INSERT OR REPLACE INTO name_parse_cache "
        "(cache_key,file_name,season,episode,confidence,origin,created_at) VALUES (?,?,?,?,?,?,?)",
        [(k, n, s, ep, c, origin, now) for (k, n, s, ep, c) in rows])
    con.commit()


# ── strm_map ──────────────────────────────────────────
def save_strm_map(cat, title, season, episode, strm_path):
    con = _conn()
    con.execute(
        "INSERT OR REPLACE INTO strm_map (cat,title,season,episode,strm_path) VALUES (?,?,?,?,?)",
        (cat, title, season, episode, strm_path))
    con.commit()


def remove_strm_maps_below(directory, connection=None):
    con = connection or _conn()
    prefix = os.path.abspath(directory).rstrip(os.sep) + os.sep
    con.execute('DELETE FROM strm_map WHERE substr(strm_path,1,?)=?', (len(prefix), prefix))
    if connection is None:
        con.commit()


def get_strm_path(cat, title, season, episode):
    row = _conn().execute(
        "SELECT strm_path FROM strm_map WHERE cat=? AND title=? AND season=? AND episode=?",
        (cat, title, season, episode)).fetchone()
    return row["strm_path"] if row else None


# ── plays ─────────────────────────────────────────────
def record_access(transfer_id):
    with _conn() as con:
        con.execute('INSERT INTO playback_access(transfer_id,accessed_at) VALUES (?,?) '
                    'ON CONFLICT(transfer_id) DO UPDATE SET accessed_at=excluded.accessed_at',
                    (transfer_id,now()))


def recently_accessed(transfer_id, grace_seconds=6*3600):
    return bool(_conn().execute('SELECT 1 FROM playback_access WHERE transfer_id=? AND accessed_at>?',
                               (transfer_id,now()-grace_seconds)).fetchone())


def record_play(cat, title, season, episode, transfer_id=None, finished=False):
    con = _conn()
    con.execute(
        "INSERT INTO plays (cat,title,season,episode,transfer_id,played_at,finished) "
        "VALUES (?,?,?,?,?,?,?)",
        (cat, title, season, episode, transfer_id, now(), 1 if finished else 0))
    con.commit()


def last_play(cat, title, season, episode):
    return _conn().execute(
        "SELECT * FROM plays WHERE cat=? AND title=? AND season=? AND episode=? "
        "ORDER BY played_at DESC LIMIT 1", (cat, title, season, episode)).fetchone()


# ── cleanup_queue ─────────────────────────────────────
def enqueue_cleanup(transfer_id, remote_path, due_at):
    con = _conn()
    con.execute(
        "INSERT INTO cleanup_queue (transfer_id,remote_path,due_at) VALUES (?,?,?)",
        (transfer_id, remote_path, due_at))
    con.commit()


def due_cleanups(limit=50):
    return _conn().execute(
        "SELECT * FROM cleanup_queue WHERE cleaned=0 AND due_at<=? ORDER BY due_at,id LIMIT ?",
        (now(), limit)).fetchall()


def defer_cleanup(queue_id, seconds=86400):
    with _conn() as con:
        con.execute('UPDATE cleanup_queue SET due_at=? WHERE id=? AND cleaned=0',(now()+seconds,queue_id))


def mark_cleaned(queue_id, transfer_id):
    con = _conn()
    con.execute("UPDATE cleanup_queue SET cleaned=1 WHERE id=?", (queue_id,))
    con.execute("UPDATE transfers SET status='cleaned', updated_at=? WHERE id=?", (now(), transfer_id))
    con.commit()


def stats():
    con = _conn()
    out = {}
    for s in ("pending", "searching", "found", "saving", "done", "failed", "cleaned"):
        out[s] = con.execute("SELECT COUNT(*) c FROM transfers WHERE status=?", (s,)).fetchone()["c"]
    out["plays"] = con.execute("SELECT COUNT(*) c FROM plays").fetchone()["c"]
    out["cleanup_pending"] = con.execute(
        "SELECT COUNT(*) c FROM cleanup_queue WHERE cleaned=0").fetchone()["c"]
    return out


# ── user_pinned ───────────────────────────────────────
def pin_item(cat, title, reason="preheated"):
    """将影视条目登记为用户置顶/预热保护（绝不淘汰）。"""
    if not title:
        return
    clean_title = title.rsplit(" (", 1)[0].strip() if " (" in title else title.strip()
    try:
        con = _conn()
        con.execute("""
            INSERT OR REPLACE INTO user_pinned (cat, title, reason, pinned_at)
            VALUES (?, ?, ?, ?)
        """, (cat or "", clean_title, reason, now()))
        con.commit()
    except Exception:
        pass


def is_pinned(title, cat=""):
    """检查条目是否处于用户置顶/预热保护状态。"""
    if not title:
        return False
    clean_title = title.rsplit(" (", 1)[0].strip() if " (" in title else title.strip()
    try:
        if cat:
            row = _conn().execute(
                "SELECT id FROM user_pinned WHERE (cat=? OR cat='') AND (title=? OR title=?) LIMIT 1",
                (cat, title, clean_title)
            ).fetchone()
        else:
            row = _conn().execute(
                "SELECT id FROM user_pinned WHERE title=? OR title=? LIMIT 1",
                (title, clean_title)
            ).fetchone()
        return bool(row)
    except Exception:
        return False


def get_all_pinned_titles():
    """Protection is safety-critical: database errors must propagate."""
    return {r['title'].strip() for r in _conn().execute('SELECT title FROM user_pinned') if r['title']}


init_db()
