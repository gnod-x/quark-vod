#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
点播状态库（SQLite）

记录三部曲：转存 -> 播放 -> 清理。
所有表允许幂等重复写（INSERT OR REPLACE），便于断点续跑。
"""

import os
import json
import logging
import sqlite3
import threading
import time

from . import config

SCHEMA_VERSION = 3
_BUSY_TIMEOUT = 30
_local = threading.local()
_log = logging.getLogger(__name__)
ACCESS_SESSION_SECONDS = 30 * 60


class DatabaseUpgradeError(RuntimeError):
    """Raised when the state database cannot be safely opened or upgraded."""


def _raw_conn():
    """Return this thread's connection without triggering initialization."""
    if not hasattr(_local, "conn"):
        os.makedirs(os.path.dirname(config.STATE_DB), exist_ok=True)
        con = sqlite3.connect(config.STATE_DB, timeout=_BUSY_TIMEOUT)
        con.row_factory = sqlite3.Row
        _local.conn = con
        _local.initialized = False
    return _local.conn


def _conn():
    return init_db()


# Freeze the SQL for each released migration. Add changes as a new migration.
_SCHEMA_V1 = """
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


def _schema_statements(script):
    # executescript() commits any pending transaction before running the SQL.
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            yield statement
            statement = ""
    if statement.strip():
        raise DatabaseUpgradeError("state schema has an unterminated SQL statement")


_REQUIRED_COLUMNS = {
    "transfers": {
        "id", "cat", "title", "season", "episode", "share_url", "share_pwd", "pan_type",
        "status", "error", "remote_path", "file_size", "created_at", "updated_at", "save_task_id",
        "save_target", "save_started_at", "save_file_fid", "variant",
    },
    "strm_map": {"cat", "title", "season", "episode", "strm_path", "variant"},
    "plays": {"id", "cat", "title", "season", "episode", "transfer_id", "played_at", "finished"},
    "playback_access": {"transfer_id", "accessed_at"},
    "cleanup_queue": {"id", "transfer_id", "remote_path", "due_at", "cleaned"},
    "dead_sources": {"source_url", "source_type", "reason", "created_at", "expires_at"},
    "source_audits": {
        "id", "cat", "title", "season", "episode", "has_quark_source", "is_4k", "best_share_url",
        "best_share_title", "checked_at", "check_status", "variant",
    },
    "user_pinned": {"id", "cat", "title", "reason", "pinned_at"},
    "name_parse_cache": {"cache_key", "file_name", "season", "episode", "confidence", "origin", "created_at"},
    "staging_media": {"tmdb_id","cat","title","year","media_type","retry_count","next_check_at","status","metadata_json","created_at","updated_at"},
    "airing_calendar": {"tmdb_id","cat","title","season","next_air_date","typical_air_hour","is_user_watching","metadata_json","updated_at"},
    "notification_events": {"fingerprint","event","level","created_at","delivered_at"},
    "wishlist_ingest": {"source","entry_key","status","metadata_json","updated_at"},
    "replacement_commits": {"candidate_id", "old_transfer_id", "committed_at"},
}
_PRIMARY_KEYS = {
    "transfers": ("id",),
    "strm_map": ("cat", "title", "season", "episode", "variant"),
    "plays": ("id",),
    "playback_access": ("transfer_id",),
    "cleanup_queue": ("id",),
    "dead_sources": ("source_url",),
    "source_audits": ("id",),
    "user_pinned": ("id",),
    "name_parse_cache": ("cache_key",),
    "staging_media": ("tmdb_id","media_type"),
    "airing_calendar": ("tmdb_id","season"),
    "notification_events": ("fingerprint",),
    "wishlist_ingest": ("source","entry_key"),
    "replacement_commits": ("candidate_id",),
}
_UNIQUE_KEYS = {
    "source_audits": ("cat", "title", "season", "episode", "variant"),
    "user_pinned": ("cat", "title"),
}


def _validate_schema(con):
    problems = []
    for table, required in _REQUIRED_COLUMNS.items():
        columns = {row["name"]: row for row in con.execute("SELECT * FROM pragma_table_info(?)", (table,))}
        if not columns:
            problems.append(f"{table} (missing table)")
            continue
        absent = sorted(required - columns.keys())
        if absent:
            problems.append(f"{table} (missing columns: {', '.join(absent)})")
        primary = tuple(
            column["name"] for column in sorted(columns.values(), key=lambda column: column["pk"]) if column["pk"]
        )
        expected_key = _PRIMARY_KEYS[table]
        if primary != expected_key:
            problems.append(f"{table} (expected primary key: {', '.join(expected_key)})")
        key = _UNIQUE_KEYS.get(table)
        if primary == ("id",) or key:
            indexes = list(con.execute("SELECT * FROM pragma_index_list(?)", (table,)))
            # Inserts into these tables omit id and need SQLite to allocate a rowid.
            if primary == ("id",) and (
                columns["id"]["type"].upper() != "INTEGER" or any(index["origin"] == "pk" for index in indexes)
            ):
                problems.append(f"{table} (id must be an auto-assigned integer primary key)")
            if key and not any(
                set(key) == {row["name"] for row in con.execute("SELECT * FROM pragma_index_info(?)", (index["name"],))}
                for index in indexes if index["unique"] and not index["partial"]
            ):
                problems.append(f"{table} (missing unique key: {', '.join(key)})")
    if problems:
        raise DatabaseUpgradeError("incompatible state database schema: " + "; ".join(problems))


def _migration_1(con):
    """Create the 3.0 state schema and add fields from pre-versioned databases."""
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_schema WHERE type IN ('table', 'view')")}
    if "transfers" not in tables and any(not name.startswith("sqlite_") for name in tables):
        raise DatabaseUpgradeError("unrecognized unversioned state database: missing transfers table")
    for statement in _schema_statements(_SCHEMA_V1):
        con.execute(statement)
    columns = {row[1] for row in con.execute("PRAGMA table_info(transfers)")}
    for name, kind in (("save_task_id", "TEXT"), ("save_target", "TEXT"), ("save_started_at", "INTEGER")):
        if name not in columns:
            con.execute(f"ALTER TABLE transfers ADD COLUMN {name} {kind}")


def _migration_2(con):
    """Persist the exact saved file identity before a remote format rename."""
    columns = {row[1] for row in con.execute("PRAGMA table_info(transfers)")}
    if "save_file_fid" not in columns:
        con.execute("ALTER TABLE transfers ADD COLUMN save_file_fid TEXT")


def _migration_3(con):
    for table in ("transfers", "source_audits"):
        con.execute(f"ALTER TABLE {table} ADD COLUMN variant TEXT NOT NULL DEFAULT ''")
    con.execute("ALTER TABLE strm_map RENAME TO strm_map_v2")
    con.execute("CREATE TABLE strm_map(cat TEXT NOT NULL,title TEXT NOT NULL,season INTEGER NOT NULL DEFAULT 0,episode INTEGER NOT NULL DEFAULT 0,variant TEXT NOT NULL DEFAULT '',strm_path TEXT NOT NULL,PRIMARY KEY(cat,title,season,episode,variant))")
    con.execute("INSERT INTO strm_map(cat,title,season,episode,strm_path) SELECT cat,title,season,episode,strm_path FROM strm_map_v2")
    con.execute("DROP TABLE strm_map_v2")
    con.execute("DROP INDEX idx_source_audits_item")
    con.execute("CREATE UNIQUE INDEX idx_source_audits_item ON source_audits(cat,title,season,episode,variant)")
    con.execute("CREATE INDEX idx_strm_map_se_title ON strm_map(season,episode,title)")
    con.execute("CREATE INDEX idx_transfers_status_lookup ON transfers(cat,season,episode,status)")
    con.execute("CREATE TABLE staging_media(tmdb_id INTEGER NOT NULL,cat TEXT NOT NULL,title TEXT NOT NULL,year TEXT NOT NULL DEFAULT '',media_type TEXT NOT NULL,popularity REAL NOT NULL DEFAULT 0,release_date TEXT NOT NULL DEFAULT '',retry_count INTEGER NOT NULL DEFAULT 0,next_check_at INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'pending',metadata_json TEXT NOT NULL DEFAULT '{}',created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,PRIMARY KEY(tmdb_id,media_type))")
    con.execute("CREATE INDEX idx_staging_due ON staging_media(status,next_check_at)")
    con.execute("CREATE TABLE airing_calendar(tmdb_id INTEGER NOT NULL,cat TEXT NOT NULL,title TEXT NOT NULL,season INTEGER NOT NULL,next_air_date TEXT NOT NULL DEFAULT '',typical_air_hour INTEGER NOT NULL DEFAULT 18,is_user_watching INTEGER NOT NULL DEFAULT 0,metadata_json TEXT NOT NULL DEFAULT '{}',updated_at INTEGER NOT NULL,PRIMARY KEY(tmdb_id,season))")
    con.execute("CREATE INDEX idx_airing_due ON airing_calendar(next_air_date)")
    con.execute("CREATE TABLE notification_events(fingerprint TEXT PRIMARY KEY,event TEXT NOT NULL,level TEXT NOT NULL,created_at INTEGER NOT NULL,delivered_at INTEGER)")
    con.execute("CREATE TABLE wishlist_ingest(source TEXT NOT NULL,entry_key TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',metadata_json TEXT NOT NULL DEFAULT '{}',updated_at INTEGER NOT NULL,PRIMARY KEY(source,entry_key))")
    con.execute("CREATE TABLE replacement_commits(candidate_id INTEGER PRIMARY KEY,old_transfer_id INTEGER,committed_at INTEGER NOT NULL)")


_MIGRATIONS = {1: _migration_1, 2: _migration_2, 3: _migration_3}


def _read_version(con):
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if not 0 <= version <= SCHEMA_VERSION:
        raise DatabaseUpgradeError(
            f"unsupported state database schema version {version}; this binary supports 0 through {SCHEMA_VERSION}"
        )
    return version


def _validate_database_integrity(con):
    results = [row[0] for row in con.execute("PRAGMA quick_check")]
    if results != ["ok"]:
        raise DatabaseUpgradeError(
            "state database failed SQLite quick_check; restore a verified backup before retrying"
        )


def _ensure_wal(con):
    # A peer can briefly hold a read lock between our commit and the mode change.
    deadline = time.monotonic() + _BUSY_TIMEOUT
    while True:
        try:
            mode = con.execute("PRAGMA journal_mode").fetchone()[0]
            if mode == "wal" or con.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal":
                return
            break
        except sqlite3.OperationalError as exc:
            remaining = deadline - time.monotonic()
            if exc.sqlite_errorcode & 0xff != sqlite3.SQLITE_BUSY or remaining <= 0:
                raise DatabaseUpgradeError("cannot enable WAL on compatible state database; retry startup") from exc
            time.sleep(min(0.05, remaining))
    raise DatabaseUpgradeError("SQLite refused WAL for the state database; check its filesystem")


def init_db():
    """Open or upgrade the state database in one transactional, retryable step.

    Databases created before schema versioning report ``user_version=0`` and
    receive the idempotent version-1 migration.  A database newer than this
    binary is rejected without writes; downgrade requires restoring a backup.
    """
    try:
        con = _raw_conn()
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseUpgradeError("cannot open state database") from exc
    if _local.initialized:
        return con
    if con.in_transaction:
        raise DatabaseUpgradeError("cannot initialize state database during an active transaction")
    try:
        # Serialize startup too, so an older process cannot observe a peer's migration midway.
        con.execute("BEGIN IMMEDIATE")
        current = _read_version(con)
        if current < SCHEMA_VERSION:
            _validate_database_integrity(con)
        for target_version in range(current + 1, SCHEMA_VERSION + 1):
            migration = _MIGRATIONS.get(target_version)
            if migration is None:
                raise DatabaseUpgradeError(f"no migration registered for schema version {target_version}")
            migration(con)
        _validate_schema(con)
        if current < SCHEMA_VERSION:
            con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        con.commit()
        _ensure_wal(con)
        _local.initialized = True
    except BaseException as exc:
        # Closing also releases a failed transaction, including on a failed commit.
        close()
        if isinstance(exc, sqlite3.Error):
            raise DatabaseUpgradeError(f"cannot validate or migrate state database: {exc}") from exc
        raise
    return con


def close():
    """Close this thread's state connection, primarily for tests and maintenance."""
    con = getattr(_local, "conn", None)
    if con is not None:
        con.close()
        del _local.conn
    _local.initialized = False


def save_source_audit(cat, title, season, episode, has_quark_source, is_4k,
                      best_share_url="", best_share_title="", check_status="unknown", *, variant=""):
    """保存或更新片源预检审计结果。"""
    now_ts = now()
    con = _conn()
    try:
        con.execute("""
            INSERT OR REPLACE INTO source_audits
            (cat, title, season, episode, has_quark_source, is_4k, best_share_url, best_share_title, checked_at, check_status, variant)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (cat, title, season, episode, int(has_quark_source), int(is_4k),
              best_share_url, best_share_title, now_ts, check_status, validate_variant(variant)))
        con.commit()
    except Exception as exc:
        con.rollback()
        _log.error("state write failed: save_source_audit (%s)", type(exc).__name__)
        raise


def get_source_audit(cat, title, season=0, episode=0, *, variant=""):
    """获取指定条目的最新片源预检记录。"""
    try:
        return _conn().execute("""
            SELECT * FROM source_audits
            WHERE cat=? AND title=? AND season=? AND episode=? AND variant=?
            LIMIT 1
        """, (cat, title, season, episode, validate_variant(variant))).fetchone()
    except Exception:
        return None


def get_locked_4k_share(cat, title, season=0, episode=0, *, variant=""):
    """提取经预检验证存活且为 4K 的锁定夸克分享源。

    2026-11-08：追加 check_status 过滤。旧实现只看 has_quark_source 与
    best_share_url，审计已判 dead / 空心分享并作废后若仍有残留字段，
    仍会把它当成最高优先级片源调取，导致播放/预热永远死在这条源上。
    """
    row = get_source_audit(cat, title, season, episode, variant=variant)
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
    con = _conn()
    try:
        con = _conn()
        con.execute(
            "UPDATE source_audits SET checked_at = ? "
            "WHERE cat=? AND title=? AND season=? AND best_share_url=?",
            (ts if ts is not None else now(), cat, title, season, share_url)
        )
        con.commit()
    except Exception as exc:
        con.rollback()
        _log.error("state write failed: bump_audit_checked_at (%s)", type(exc).__name__)
        raise


def purge_audit_locked_share(cat, title, season, share_url):
    """作废某季下所有锁定了指定分享源的审计行（剧集回退为待补源）。"""
    if not share_url:
        return
    con = _conn()
    try:
        con = _conn()
        con.execute(
            "UPDATE source_audits SET has_quark_source=0, is_4k=0, best_share_url='', best_share_title='', "
            "check_status='dead', checked_at=? WHERE cat=? AND title=? AND season=? AND best_share_url=?",
            (now(), cat, title, season, share_url)
        )
        con.commit()
    except Exception as exc:
        con.rollback()
        _log.error("state write failed: purge_audit_locked_share (%s)", type(exc).__name__)
        raise


def mark_dead_source(source_url, source_type="panshare", reason="", ban_days=7):
    """记录死链/失效分享源，进行秒级熔断。"""
    if not source_url:
        return
    source_url = _canonical_source(source_url)
    now_ts = now()
    exp_ts = now_ts + ban_days * 86400
    con = _conn()
    try:
        con.execute(
            "INSERT OR REPLACE INTO dead_sources (source_url, source_type, reason, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (source_url.strip(), source_type, reason[:200], now_ts, exp_ts)
        )
        con.commit()
    except Exception as exc:
        con.rollback()
        _log.error("state write failed: mark_dead_source (%s)", type(exc).__name__)
        raise


def is_dead_source(source_url):
    """检查链接是否处于熔断黑名单中（未过期算死链）。"""
    if not source_url:
        return False
    source_url = _canonical_source(source_url)
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
def upsert_transfer(cat, title, season, episode, *, _cleanup_due_at=None, _transfer_id=None, variant="", **fields):
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
    variant = validate_variant(variant)
    con = _conn()
    allowed = {'status', 'share_url', 'share_pwd', 'pan_type', 'error', 'remote_path', 'file_size', 'save_task_id', 'save_target', 'save_started_at', 'save_file_fid'}
    if not fields.keys() <= allowed:
        raise ValueError('invalid transfer fields')
    if _cleanup_due_at is not None and ((fields.get("status") != "done" and not (_transfer_id is not None and fields.get("status") == "found")) or not fields.get("remote_path")):
        raise ValueError("cleanup scheduling requires a completed transfer and exact path")
    # Serialize SELECT+INSERT across threads and host cron processes.
    # Historical duplicates keep their queue/play references.
    con.execute("BEGIN IMMEDIATE")
    try:
        if _transfer_id is not None:
            row = con.execute(
                "SELECT id FROM transfers WHERE id=? AND cat=? AND title=? AND season=? AND episode=? AND variant=?",
                (_transfer_id, cat, title, season, episode, variant)).fetchone()
            if row is None:
                raise ValueError("transfer id does not match the requested work")
        else:
            row = con.execute(
                "SELECT id FROM transfers WHERE cat=? AND title=? AND season=? AND episode=? AND variant=? "
                "ORDER BY updated_at DESC,id DESC LIMIT 1",
                (cat, title, season, episode, variant)).fetchone()
        if row:
            sets = [f"{k}=?" for k in fields] + ['updated_at=?']
            con.execute(f"UPDATE transfers SET {', '.join(sets)} WHERE id=?",
                        (*fields.values(), now(), row['id']))
            tid = row['id']
        else:
            cols = ['cat', 'title', 'season', 'episode', 'variant', 'created_at', 'updated_at', *fields.keys()]
            vals = [cat, title, season, episode, variant, now(), now(), *fields.values()]
            cur = con.execute(f"INSERT INTO transfers ({','.join(cols)}) VALUES ({','.join('?' for _ in vals)})", vals)
            tid = cur.lastrowid
        if _cleanup_due_at is not None:
            con.execute("INSERT INTO cleanup_queue (transfer_id,remote_path,due_at) VALUES (?,?,?)",
                        (tid, fields["remote_path"], _cleanup_due_at))
        con.commit()
        return tid
    except BaseException:
        con.rollback()
        raise


def create_transfer(cat, title, season, episode, *, variant="", share_url="", share_pwd="", status="pending"):
    """Create a distinct transfer row for a staged operation such as source replacement."""
    from .search import strip_invisible
    cat = strip_invisible(cat) or cat
    title = strip_invisible(title) or title
    variant = validate_variant(variant)
    if status not in ("pending", "searching", "saving"):
        raise ValueError("invalid initial transfer status")
    timestamp = now()
    con = _conn()
    with con:
        cur = con.execute(
            "INSERT INTO transfers (cat,title,season,episode,variant,share_url,share_pwd,pan_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (cat,title,season,episode,variant,share_url,share_pwd,"quark",status,timestamp,timestamp))
    return cur.lastrowid


def complete_transfer(cat, title, season, episode, remote_path, file_size, due_at, *, variant="", _transfer_id=None, _staged=False):
    """Atomically finish a confirmed save and record its expiry work."""
    return upsert_transfer(
        cat, title, season, episode, status="found" if _staged else "done", remote_path=remote_path,
        file_size=file_size, error=None, save_target=None, save_task_id=None,
        save_started_at=None, save_file_fid=None, _cleanup_due_at=due_at,
        _transfer_id=_transfer_id, variant=variant,
    )


def get_transfer(cat, title, season, episode, *, variant=""):
    return _conn().execute(
        "SELECT * FROM transfers WHERE cat=? AND title=? AND season=? AND episode=? AND variant=? "
        "ORDER BY updated_at DESC,id DESC LIMIT 1",
        (cat, title, season, episode, validate_variant(variant))).fetchone()


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


def library_title(cat, title, season, episode, *, variant=""):
    """Recover a legacy row's missing year using its unique on-disk mapping."""
    from .identity import split_title
    if split_title(title)[1]:
        return title
    rows = _conn().execute(
        'SELECT strm_path FROM strm_map WHERE cat=? AND title=? AND season=? AND episode=? AND variant=?',
        (cat, title, season, episode, validate_variant(variant))).fetchall()
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


def find_done_transfer(cat, title, season, episode, *, variant=""):
    """Resolve exact work identity with one query, including legacy mapping years."""
    from .identity import same_title, media_kind, split_title
    rows = _conn().execute(
        "SELECT t.*, m.strm_path AS identity_strm_path FROM transfers t "
        "LEFT JOIN strm_map m ON m.cat=t.cat AND m.title=t.title "
        "AND m.season=t.season AND m.episode=t.episode AND m.variant=t.variant "
        "WHERE t.season=? AND t.episode=? AND t.variant=? AND t.status='done' "
        "AND t.remote_path IS NOT NULL AND t.remote_path<>'' "
        "ORDER BY t.updated_at DESC,t.id DESC", (season, episode, validate_variant(variant))).fetchall()
    candidates = []
    root = os.path.abspath(config.CINEMA2_DIR)
    for row in rows:
        if media_kind(row['cat'], season) != media_kind(cat, season):
            continue
        actual_title = row['title']
        if split_title(actual_title)[0].casefold() != split_title(title)[0].casefold():
            continue
        mapped = row['identity_strm_path']
        if not split_title(actual_title)[1] and mapped:
            path = os.path.abspath(mapped)
            if path.startswith(root + os.sep):
                parts = os.path.relpath(path, root).split(os.sep)
                if len(parts) >= 3 and split_title(parts[1])[0].casefold() == split_title(actual_title)[0].casefold():
                    actual_title = parts[1]
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
def save_strm_map(cat, title, season, episode, strm_path, *, variant=""):
    con = _conn()
    con.execute(
        "INSERT OR REPLACE INTO strm_map (cat,title,season,episode,strm_path,variant) VALUES (?,?,?,?,?,?)",
        (cat, title, season, episode, strm_path, validate_variant(variant)))
    con.commit()


def remove_strm_maps_below(directory, connection=None):
    con = connection or _conn()
    prefix = os.path.abspath(directory).rstrip(os.sep) + os.sep
    con.execute('DELETE FROM strm_map WHERE substr(strm_path,1,?)=?', (len(prefix), prefix))
    if connection is None:
        con.commit()


def get_strm_path(cat, title, season, episode, *, variant=""):
    row = _conn().execute(
        "SELECT strm_path FROM strm_map WHERE cat=? AND title=? AND season=? AND episode=? AND variant=?",
        (cat, title, season, episode, validate_variant(variant))).fetchone()
    return row["strm_path"] if row else None


# ── plays ─────────────────────────────────────────────
def record_access(transfer_id):
    """Refresh retention on every request; count access sessions, not Range blocks."""
    con = _conn()
    con.execute('BEGIN IMMEDIATE')
    try:
        timestamp = now()
        previous = con.execute('SELECT accessed_at FROM playback_access WHERE transfer_id=?',
                               (transfer_id,)).fetchone()
        if previous is None or timestamp - previous['accessed_at'] >= ACCESS_SESSION_SECONDS:
            con.execute(
                'INSERT INTO plays(cat,title,season,episode,transfer_id,played_at,finished) '
                'SELECT cat,title,season,episode,id,?,0 FROM transfers WHERE id=?',
                (timestamp, transfer_id))
        con.execute('INSERT INTO playback_access(transfer_id,accessed_at) VALUES (?,?) '
                    'ON CONFLICT(transfer_id) DO UPDATE SET accessed_at=MAX(playback_access.accessed_at,excluded.accessed_at)',
                    (transfer_id,timestamp))
        con.commit()
    except BaseException:
        con.rollback()
        raise


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
    out["playback_access_sessions"] = out["plays"]
    out["cleanup_pending"] = con.execute(
        "SELECT COUNT(*) c FROM cleanup_queue WHERE cleaned=0").fetchone()["c"]
    return out


# ── user_pinned ───────────────────────────────────────
def pin_item(cat, title, reason="preheated"):
    """Record an automatic marker without weakening an existing explicit pin."""
    if not title:
        return
    clean_title = title.rsplit(" (", 1)[0].strip() if " (" in title else title.strip()
    try:
        with _conn() as con:
            con.execute("""
                INSERT INTO user_pinned (cat,title,reason,pinned_at) VALUES (?,?,?,?)
                ON CONFLICT(cat,title) DO UPDATE SET
                  reason=excluded.reason, pinned_at=excluded.pinned_at
                WHERE user_pinned.reason IN ('preheated','manual_add','played')
                   OR excluded.reason NOT IN ('preheated','manual_add','played')
            """, (cat or "",clean_title,reason,now()))
    except Exception as exc:
        _log.error("state write failed: pin_item (%s)", type(exc).__name__)
        raise


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
    except Exception as exc:
        _log.error("protection query failed: is_pinned (%s)", type(exc).__name__)
        raise


def get_all_pinned_titles():
    """Return explicit pins only; automatic/legacy reasons do not block retention."""
    rows = _conn().execute(
        "SELECT title FROM user_pinned WHERE COALESCE(reason,'') NOT IN ('preheated','manual_add','played')"
    )
    return {r['title'].strip() for r in rows if r['title']}


def validate_variant(variant):
    if variant not in ("", "hdr", "sdr", "dubbed", "remux"):
        raise ValueError("unsupported media variant")
    return variant


def _canonical_source(url):
    from .panshare_matrix import canonical_share
    return canonical_share(str(url)) or str(url).strip()


def clear_source_audit(cat, title, season, episode, *, variant=""):
    with _conn() as con:
        con.execute("DELETE FROM source_audits WHERE cat=? AND title=? AND season=? AND episode=? AND variant=?",
                    (cat,title,season,episode,validate_variant(variant)))


def _metadata_row(row):
    data = dict(row)
    try:
        data["metadata"] = json.loads(data.get("metadata_json") or "{}")
    except (ValueError,TypeError):
        data["metadata"] = {}
    return data


def upsert_staging_media(tmdb_id, cat, title, year, media_type, popularity=0, release_date="", status="pending", metadata=None, next_check_at=None):
    if media_type not in ("tv","movie") or int(tmdb_id)<=0:
        raise ValueError("invalid staged identity")
    with _conn() as con:
        con.execute("INSERT INTO staging_media(tmdb_id,cat,title,year,media_type,popularity,release_date,status,metadata_json,next_check_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(tmdb_id,media_type) DO UPDATE SET cat=excluded.cat,title=excluded.title,year=excluded.year,popularity=excluded.popularity,release_date=excluded.release_date,status=excluded.status,metadata_json=excluded.metadata_json,next_check_at=excluded.next_check_at,updated_at=excluded.updated_at",
                    (int(tmdb_id),cat,title,str(year),media_type,popularity,release_date,status,json.dumps(metadata or {},ensure_ascii=False),now() if next_check_at is None else int(next_check_at),now(),now()))
    return _metadata_row(_conn().execute("SELECT * FROM staging_media WHERE tmdb_id=? AND media_type=?",(tmdb_id,media_type)).fetchone())


def list_staging_media(due_before=None, limit=100, status="pending"):
    conditions=[]; args=[]
    if status is not None:
        conditions.append("status=?"); args.append(status)
    if due_before is not None:
        conditions.append("next_check_at<=?"); args.append(int(due_before))
    args.append(min(1000,max(1,int(limit))))
    query="SELECT * FROM staging_media"+(" WHERE "+" AND ".join(conditions) if conditions else "")+" ORDER BY next_check_at,popularity DESC LIMIT ?"
    return [_metadata_row(row) for row in _conn().execute(query,args)]


def update_staging_media(tmdb_id, media_type, **fields):
    return _update_metadata("staging_media", {"tmdb_id":tmdb_id,"media_type":media_type},fields,
                            {"cat","title","year","popularity","release_date","retry_count","next_check_at","status","metadata"})


def upsert_airing_calendar(tmdb_id, cat, title, season, next_air_date, typical_air_hour=18, is_user_watching=False, metadata=None):
    with _conn() as con:
        con.execute("INSERT INTO airing_calendar(tmdb_id,cat,title,season,next_air_date,typical_air_hour,is_user_watching,metadata_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(tmdb_id,season) DO UPDATE SET cat=excluded.cat,title=excluded.title,next_air_date=excluded.next_air_date,typical_air_hour=excluded.typical_air_hour,is_user_watching=excluded.is_user_watching,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",
                    (int(tmdb_id),cat,title,int(season),next_air_date,int(typical_air_hour),int(bool(is_user_watching)),json.dumps(metadata or {},ensure_ascii=False),now()))
    return _metadata_row(_conn().execute("SELECT * FROM airing_calendar WHERE tmdb_id=? AND season=?",(tmdb_id,season)).fetchone())


def list_airing_calendar(due_before=None, limit=100):
    query="SELECT * FROM airing_calendar"
    args=[]
    if due_before is not None:
        query+=" WHERE next_air_date<>'' AND next_air_date<=?"
        args.append(str(due_before))
    query+=" ORDER BY is_user_watching DESC,next_air_date LIMIT ?"
    args.append(min(1000,max(1,int(limit))))
    return [_metadata_row(row) for row in _conn().execute(query,args)]


def update_airing_calendar(tmdb_id, season, **fields):
    return _update_metadata("airing_calendar",{"tmdb_id":tmdb_id,"season":season},fields,
                            {"cat","title","next_air_date","typical_air_hour","is_user_watching","metadata"})


def _update_metadata(table, keys, fields, allowed):
    if not fields or not fields.keys()<=allowed:
        raise ValueError("invalid metadata fields")
    values={("metadata_json" if k=="metadata" else k):(json.dumps(v,ensure_ascii=False) if k=="metadata" else v) for k,v in fields.items()}
    values["updated_at"]=now()
    with _conn() as con:
        con.execute("UPDATE "+table+" SET "+",".join(k+"=?" for k in values)+" WHERE "+" AND ".join(k+"=?" for k in keys),[*values.values(),*keys.values()])
    row=_conn().execute("SELECT * FROM "+table+" WHERE "+" AND ".join(k+"=?" for k in keys),list(keys.values())).fetchone()
    return _metadata_row(row) if row else None


def notification_claim(fingerprint,event,level,dedupe_seconds=21600):
    con=_conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        row=con.execute("SELECT created_at FROM notification_events WHERE fingerprint=?",(fingerprint,)).fetchone()
        if row and row["created_at"]>now()-dedupe_seconds:
            con.rollback(); return False
        con.execute("INSERT OR REPLACE INTO notification_events(fingerprint,event,level,created_at) VALUES(?,?,?,?)",(fingerprint,event,level,now()))
        con.execute("DELETE FROM notification_events WHERE created_at<?",(now()-30*86400,))
        con.commit(); return True
    except BaseException:
        con.rollback(); raise
