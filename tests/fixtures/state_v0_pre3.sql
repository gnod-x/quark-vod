-- Frozen schema from pre-3.0 main, commit e206d53.
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
