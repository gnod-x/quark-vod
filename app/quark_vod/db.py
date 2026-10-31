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
"""


def init_db():
    con = _conn()
    con.executescript(SCHEMA)
    con.commit()


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
    """提取经预检验证存活且为 4K 的锁定夸克分享源。"""
    row = get_source_audit(cat, title, season, episode)
    if row and row["has_quark_source"] and row["best_share_url"]:
        # 再次确保不在黑名单中
        if not is_dead_source(row["best_share_url"]):
            return row["best_share_url"], bool(row["is_4k"]), row["best_share_title"]
    return None, False, ""


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
    row = con.execute(
        "SELECT id FROM transfers WHERE cat=? AND title=? AND season=? AND episode=?",
        (cat, title, season, episode)).fetchone()
    if row:
        sets = ", ".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE transfers SET {sets}, updated_at=? WHERE id=?",
                    (*fields.values(), now(), row["id"]))
        tid = row["id"]
    else:
        cols = ["cat", "title", "season", "episode", "created_at", "updated_at", *fields.keys()]
        vals = [cat, title, season, episode, now(), now(), *fields.values()]
        ph = ",".join("?" * len(cols))
        cur = con.execute(f"INSERT INTO transfers ({','.join(cols)}) VALUES ({ph})", vals)
        tid = cur.lastrowid
    con.commit()
    return tid


def get_transfer(cat, title, season, episode):
    return _conn().execute(
        "SELECT * FROM transfers WHERE cat=? AND title=? AND season=? AND episode=?",
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


def find_done_transfer(cat, title, season, episode):
    """已转存完成且未清理的记录。

    2026-10-16 修复：原来用 cat+title+season+episode 四字段精确匹配，实际必挂：
      1) strm 里的 t 参数带年份后缀（"霸王别姬 (1993)"），而转存时入库的 title
         是搜索匹配到的名称（"霸王别姬"，无年份）；
      2) 同一部片可能被 VOD 生成器放进多个专区（实测《霸王别姬 (1993)》同时存在
         于 港台经典/ 与 国产电影/），cat 也会不一致。

    2026-10-20 修复跨作品污染：原「任一方包含另一方」的兜底会把同名不同作品
    串起来（《凡人修仙传》动漫 vs 《凡人修仙传 (2025)》真人剧）。现在：
      - 名称必须去年份后完全相等，不再允许模糊包含；
      - 请求带年份时，记录也带年份则必须一致；
      - 同分时优先返回 cat 匹配的记录。
    """
    import re
    raw = (title or "").strip()
    m = re.search(r"[(（](\d{4})[)）]\s*$", raw)
    want_year = m.group(1) if m else ""
    core = re.sub(r"\s*[(（]\d{4}[)）]\s*$", "", raw).strip()
    if not core:
        return None

    rows = _conn().execute(
        "SELECT * FROM transfers WHERE season=? AND episode=? "
        "AND status='done' AND remote_path IS NOT NULL",
        (season, episode)).fetchall()
    if not rows:
        return None

    matched = []
    for r in rows:
        rt = (r["title"] or "").strip()
        rm = re.search(r"[(（](\d{4})[)）]\s*$", rt)
        r_year = rm.group(1) if rm else ""
        rn = re.sub(r"\s*[(（]\d{4}[)）]\s*$", "", rt).strip()
        if rn != core:
            continue
        # 双方都带年份时必须一致，杜绝动漫/真人剧同名互串
        if want_year and r_year and want_year != r_year:
            continue
        matched.append((0 if (cat and r["cat"] == cat) else 1, r))

    if not matched:
        return None
    matched.sort(key=lambda x: x[0])
    return matched[0][1]


# ── strm_map ──────────────────────────────────────────
def save_strm_map(cat, title, season, episode, strm_path):
    con = _conn()
    con.execute(
        "INSERT OR REPLACE INTO strm_map (cat,title,season,episode,strm_path) VALUES (?,?,?,?,?)",
        (cat, title, season, episode, strm_path))
    con.commit()


def get_strm_path(cat, title, season, episode):
    row = _conn().execute(
        "SELECT strm_path FROM strm_map WHERE cat=? AND title=? AND season=? AND episode=?",
        (cat, title, season, episode)).fetchone()
    return row["strm_path"] if row else None


# ── plays ─────────────────────────────────────────────
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
        "SELECT * FROM cleanup_queue WHERE cleaned=0 AND due_at<=? LIMIT ?",
        (now(), limit)).fetchall()


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


init_db()
