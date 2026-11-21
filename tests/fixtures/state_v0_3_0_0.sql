-- Frozen schema from public 3.0.0 (cb6eefc), including its three ALTER statements.
CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cat TEXT NOT NULL,
    title TEXT NOT NULL,
    season INTEGER NOT NULL DEFAULT 0,
    episode INTEGER NOT NULL DEFAULT 0,
    share_url TEXT,
    share_pwd TEXT,
    pan_type TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    remote_path TEXT,
    file_size INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transfers_lookup
    ON transfers(cat, title, season, episode, status);
CREATE TABLE IF NOT EXISTS strm_map (
    cat TEXT NOT NULL,
    title TEXT NOT NULL,
    season INTEGER NOT NULL DEFAULT 0,
    episode INTEGER NOT NULL DEFAULT 0,
    strm_path TEXT NOT NULL,
    PRIMARY KEY (cat, title, season, episode)
);
CREATE TABLE IF NOT EXISTS plays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cat TEXT NOT NULL,
    title TEXT NOT NULL,
    season INTEGER NOT NULL DEFAULT 0,
    episode INTEGER NOT NULL DEFAULT 0,
    transfer_id INTEGER,
    played_at INTEGER NOT NULL,
    finished INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_plays_lookup
    ON plays(cat, title, season, episode);
CREATE TABLE IF NOT EXISTS playback_access (
    transfer_id INTEGER PRIMARY KEY,
    accessed_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cleanup_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id INTEGER NOT NULL,
    remote_path TEXT NOT NULL,
    due_at INTEGER NOT NULL,
    cleaned INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cleanup_due ON cleanup_queue(cleaned, due_at);
CREATE TABLE IF NOT EXISTS dead_sources (
    source_url TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    reason TEXT,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dead_sources_expires ON dead_sources(expires_at);
CREATE TABLE IF NOT EXISTS source_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cat TEXT NOT NULL,
    title TEXT NOT NULL,
    season INTEGER NOT NULL DEFAULT 0,
    episode INTEGER NOT NULL DEFAULT 0,
    has_quark_source INTEGER NOT NULL DEFAULT 0,
    is_4k INTEGER NOT NULL DEFAULT 0,
    best_share_url TEXT,
    best_share_title TEXT,
    checked_at INTEGER NOT NULL,
    check_status TEXT NOT NULL DEFAULT 'unknown'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_source_audits_item
    ON source_audits(cat, title, season, episode);
CREATE INDEX IF NOT EXISTS idx_source_audits_status
    ON source_audits(check_status, is_4k);
CREATE TABLE IF NOT EXISTS user_pinned (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cat TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT 'preheated',
    pinned_at INTEGER NOT NULL,
    UNIQUE(cat, title)
);
CREATE INDEX IF NOT EXISTS idx_user_pinned ON user_pinned(cat, title);
CREATE TABLE IF NOT EXISTS name_parse_cache (
    cache_key TEXT PRIMARY KEY,
    file_name TEXT NOT NULL,
    season INTEGER NOT NULL DEFAULT 0,
    episode INTEGER,
    confidence REAL DEFAULT 0,
    origin TEXT NOT NULL DEFAULT 'ai',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_name_parse_cache_origin ON name_parse_cache(origin);
ALTER TABLE transfers ADD COLUMN save_task_id TEXT;
ALTER TABLE transfers ADD COLUMN save_target TEXT;
ALTER TABLE transfers ADD COLUMN save_started_at INTEGER;
