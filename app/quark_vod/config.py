#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
点播服务配置（cinema-quark, 端口 8099）

单一事实来源：所有路径、端口、超时、策略集中在此。
Cookie 通过 .env 的 QUARK_COOKIE 注入，绝不硬编码（红线 #8）。
"""

import os

# ── 服务自身 ──────────────────────────────────────────
HOST = os.environ.get("VOD_HOST", "0.0.0.0")
PORT = int(os.environ.get("VOD_PORT", "8099"))
SERVICE_NAME = os.environ.get("VOD_SERVICE_NAME", "cinema-quark")

# ── 路径（支持环境变量解耦，默认保持 /home/dong/projects/hometheater 100% 兼容 strm_map）──
BASE_DIR = os.environ.get("VOD_BASE_DIR", "/home/dong/projects/hometheater")
CINEMA2_DIR = os.environ.get("VOD_CINEMA2_DIR", os.path.join(BASE_DIR, "cinema2"))
DATA_DIR = os.environ.get("VOD_DATA_DIR", os.path.join(BASE_DIR, "data"))
LOG_DIR = os.environ.get("VOD_LOG_DIR", os.path.join(BASE_DIR, "logs"))

STATE_DB = os.path.join(DATA_DIR, "quark_plays.db")      # 播放/转存记录
LOG_FILE = os.path.join(LOG_DIR, "quark_service.log")
COOKIE_OVERRIDE_FILE = os.path.join(DATA_DIR, "quark_cookie.txt")

# ── OpenList ──────────────────────────────────────────
def _get_env_or_dot_env(key: str, default: str = "") -> str:
    val = os.environ.get(key, "").strip()
    if val:
        return val
    for p in (os.path.join(BASE_DIR, ".env"), os.path.join(DATA_DIR, ".secrets.env"), "/app/.env"):
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith(f"{key}="):
                            return line.split("=", 1)[1].strip().strip('"').strip("'")
            except Exception:
                pass
    return default

OPENLIST_API = os.environ.get("OPENLIST_API", "http://127.0.0.1:5244")
OPENLIST_ADMIN_USER = _get_env_or_dot_env("OPENLIST_ADMIN_USER", "admin")
OPENLIST_ADMIN_PASS = _get_env_or_dot_env("OPENLIST_ADMIN_PASS", "")

# ── OpenList 挂载点（读写分离，2026-10-16 重构）──────────
# 背景：同一个夸克账号在 OpenList 里挂了两个驱动，速度差 6 倍——
#   /夸克网盘  驱动 Quark    (Web cookie)      实测 0.6~0.8 MB/s  ← 被限速
#   /夸克      驱动 QuarkTV  (refresh_token)   实测 3.8~4.3 MB/s  ← 不限速
# 实测交叉验证（同一文件、同一时段、同方法）：
#   霸王别姬 1080p Remux: QuarkTV 4.0 MB/s  vs  Quark 0.7 MB/s
#   毒液     4K Remux:    QuarkTV 4.1 MB/s  vs  Quark 0.6 MB/s
# 结论：速度只取决于驱动，与文件/码率无关。老栈 8096 之所以能流畅播 4K
#      原盘（45.9 Mbps），正是因为它走 QuarkTV。
#
# 架构决策（用户 2026-10-16 拍板）：
#   写入（转存）→ 必须用 Quark(Web)：它持有 1780 字符 cookie，
#                  drive.quark.cn 的 sharepage/save 转存 API 依赖该 cookie。
#                 QuarkTV 用 refresh_token，不适用于转存 API。
#   读取（播放）→ 必须用 QuarkTV：不限速，是流畅播放的唯一前提。
# 两个挂载点是同一账号，/点播 目录内容实时一致（实测 17 项完全相同），
# 故无需任何数据迁移，仅需在生成播放直链时替换挂载前缀。
QUARK_MOUNT = "/夸克网盘"                        # 【写入】转存目标挂载（需 cookie）
QUARK_VOD_ROOT = f"{QUARK_MOUNT}/点播"           # 【写入】点播资源根目录
QUARK_PLAY_MOUNT = "/夸克"                       # 【读取】播放挂载（QuarkTV，不限速）
QUARK_PLAY_ROOT = f"{QUARK_PLAY_MOUNT}/点播"     # 【读取】播放根目录
# 直链前缀（播放器最终拿到的地址）
OPENLIST_PUBLIC = os.environ.get("OPENLIST_PUBLIC", "http://192.168.101.240:5244")
PENDING_BASE_URL = os.environ.get("PENDING_BASE_URL", "http://192.168.101.240:8099")


def play_path(remote_path):
    """把「转存写入路径」转换为「播放读取路径」。

    写入侧用 Quark(cookie)，播放侧用 QuarkTV(不限速)，二者是同一账号下的
    两个挂载点。此函数只做挂载前缀替换，不做任何网络请求。

    例：/夸克网盘/点播/霸王别姬/x.mkv → /夸克/点播/霸王别姬/x.mkv

    已是播放路径或不属于写入挂载的，原样返回（幂等，可反复调用）。
    """
    if not remote_path:
        return remote_path
    if remote_path == QUARK_MOUNT:
        return QUARK_PLAY_MOUNT
    if remote_path.startswith(QUARK_MOUNT + "/"):
        return QUARK_PLAY_MOUNT + remote_path[len(QUARK_MOUNT):]
    return remote_path

# ── Jellyfin2 ─────────────────────────────────────────
def get_jellyfin2_key():
    k = os.environ.get("JELLYFIN2_KEY", "")
    if k:
        return k
    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("JELLYFIN2_KEY="):
                        return line.split("=", 1)[1].strip()
        except Exception:
            pass
    return ""

JELLYFIN2_API = os.environ.get("JELLYFIN2_API_BASE", "http://127.0.0.1:8097")
JELLYFIN2_KEY = get_jellyfin2_key()
JELLYFIN2_DB = os.path.join(BASE_DIR, "jellyfin2/config/data/jellyfin.db")

# ── 凭据（支持 .env 注入或从 OpenList 数据库自动继承）────
_cached_quark_cookie = ""
_cached_cookie_ts = 0.0

def get_quark_cookie():
    global _cached_quark_cookie, _cached_cookie_ts
    # 1. 优先支持管理台热更新持久化的 quark_cookie.txt
    if os.path.exists(COOKIE_OVERRIDE_FILE):
        try:
            with open(COOKIE_OVERRIDE_FILE, "r", encoding="utf-8") as f:
                c_override = f.read().strip()
                if c_override:
                    _cached_quark_cookie = c_override
                    return c_override
        except Exception:
            pass
    # 2. 其次读取环境变量 QUARK_COOKIE
    cookie = os.environ.get("QUARK_COOKIE", "").strip()
    if cookie:
        return cookie
    now = time.time() if "time" in globals() else __import__("time").time()
    if _cached_quark_cookie and (now - _cached_cookie_ts) < 60.0:
        return _cached_quark_cookie
    # 3. 兜底从 OpenList data.db 免锁只读继承
    try:
        import sqlite3, json
        from contextlib import closing
        db_path = os.path.join(BASE_DIR, "openlist/data/data.db")
        if os.path.exists(db_path):
            with closing(sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)) as con:
                cur = con.cursor()
                cur.execute("SELECT addition FROM x_storages WHERE mount_path=?", (QUARK_MOUNT,))
                row = cur.fetchone()
                if row and row[0]:
                    add = json.loads(row[0])
                    c = add.get("cookie", "")
                    if c:
                        _cached_quark_cookie = c
                        _cached_cookie_ts = now
                        return c
    except Exception:
        pass
    return _cached_quark_cookie


def set_quark_cookie(new_cookie: str):
    """供 /admin 控制台在线热更新 Cookie 并落盘持久化。"""
    global _cached_quark_cookie, _cached_cookie_ts, QUARK_COOKIE, COOKIE_READY
    c = (new_cookie or "").strip()
    os.makedirs(DATA_DIR, exist_ok=True)
    if c:
        with open(COOKIE_OVERRIDE_FILE, "w", encoding="utf-8") as f:
            f.write(c)
        _cached_quark_cookie = c
        _cached_cookie_ts = __import__("time").time()
    else:
        if os.path.exists(COOKIE_OVERRIDE_FILE):
            os.remove(COOKIE_OVERRIDE_FILE)
        _cached_quark_cookie = ""
        _cached_cookie_ts = 0.0
    QUARK_COOKIE = get_quark_cookie()
    COOKIE_READY = bool(QUARK_COOKIE)
    return QUARK_COOKIE

QUARK_COOKIE = get_quark_cookie()
COOKIE_READY = bool(QUARK_COOKIE)

# ── 转存策略 ──────────────────────────────────────────
# 单次转存等待上限（秒）。超过则返回 503 让播放器重试
TRANSFER_TIMEOUT = 180
# 同一影片的并发转存数（防网盘风控）
TRANSFER_CONCURRENCY = 2
# 转存完成后保留天数（到期自动删除，0=永不自动删）
KEEP_DAYS = 7
# 单个文件大小上限（字节），超过则拒绝转存（防撑爆网盘）
MAX_FILE_SIZE = 30 * 1024 * 1024 * 1024      # 30GB

# ── 转存结果回读校验（2026-10-16）─────────────────────
# OpenList 的 fs/list 目录缓存不随转存实时刷新（实测远超 30s，且 refresh=true
# 会返回 "Refresh without permission"），故转存成功后必须轮询等待目录可见。
# 原 30s 过短导致大量"文件已转存但误判失败"，引发重复转存与根目录残留。
PROBE_VERIFY_TIMEOUT = 180

# ── 播放引擎策略（2026-10-16 用户决策）────────────────
# 播放引擎优先级：夸克网盘优先，CMS 作为兜底。
# True  = 优先 PanShare(网盘分享)→转存→OpenList 直链；仅当搜不到分享时才用 CMS
# False = 旧行为（CMS 优先）
#
# Why：CMS 是第三方采集站的 m3u8 直链，文件在别人服务器上，随时可能 404、
#      限速、下线，不受自己控制（实测《八佰》源站直链即 404）。网盘转存
#      虽占用空间、首次需等待，但片源稳定、速度可控。
#      保留 CMS 兜底是为避免 PanShare 未收录的片子彻底无法播放。
QUARK_FIRST = True

# ── CMS 第三方切片兜底策略 ────────────────────────────
# 2026-10-20 用户决策：CMS 降级已彻底移除，恒为 False。
# 原因：第三方 CMS 采集站为低码率 m3u8 切片（实测 1056kb/s / 1920x800），
#       与夸克 4K 原画（HEVC 3840x2160 / 6Mbps）体验差距过大，且夹带广告。
# 保留此常量仅为兼容旧代码引用，任何位置都不应再据此分支。
ALLOW_CMS_FALLBACK = False

# ── 清理策略 ──────────────────────────────────────────
# 每天清理时间（本地时间）
CLEANUP_HOUR = 4
# 清理时是否同步还原 strm 为 pending 占位（True=看完可再转存）
RESTORE_STRM_AFTER_CLEAN = True

# ── 自愈策略（2026-10-16 新增，防"手动删除网盘文件后永久黑屏"）──
# 背景：strm 一旦回写为 live 直链，Jellyfin 就直接请求 OpenList，
#       **完全绕过 8099**。此时若网盘文件被手动删除，strm 会永久停在死链上，
#       无任何代码路径能检测并恢复 → 点播必然黑屏且永不重新转存。
# 三道防线：
#   ① 播放前校验：8099 命中 done 时先查夸克官方 API 确认文件在，不在则作废并重转
#   ② 后台自愈：定时扫 live strm，失效的自动回退 pending
#   ③ cleanup 修复：删除失败也要还原 strm（原实现 continue 跳过）
# 后台自愈扫描间隔（秒）。默认 1800（30 分钟）。
#
# 【为什么不是 5 分钟 —— 夸克封控风险，2026-10-16 用户提出】
# 自愈校验必须走夸克官方 API（OpenList 缓存严重滞后，会把有效影片误判为失效，
# 实测 /夸克/点播 只显示 1 项而实际有 13 项），而该 API 用 **Web cookie** 认证。
# 红线 #23 要求把 Web cookie 的使用面收敛到写入侧，故必须严格控制调用量。
#
# 实测原始实现：逐级下钻校验，每部影片 6 次调用 →
#   14 部 × 6 × 12 轮/小时 = 1008 次/小时 ≈ 24000 次/天（封控风险高，已废弃）。
# 优化后（批量 + 轮转）：
#   每次扫描只列 1 次 /点播（覆盖所有影片的目录存在性）
#   + 轮转校验 1 个影片目录内的文件 = 约 2 次/轮。
#   调整为每天 1 次（86400s），每天仅需 2 次 API 调用，彻底消除高频轮询风险与过度防御。
SELFHEAL_INTERVAL = int(os.environ.get("SELFHEAL_INTERVAL", 86400))
# 每轮扫描额外校验几个影片目录内的具体文件（轮转，避免一次全查）。
# 设为 0 则只校验目录存在性（最省调用）；设为较大值则覆盖更快但调用更多。
SELFHEAL_FILE_CHECK_BATCH = 1
# 启动后延迟多久开始首次自愈扫描（秒），避免与启动期其他任务抢资源
SELFHEAL_INITIAL_DELAY = 120
# 自愈总开关
SELFHEAL_ENABLED = True

# ── AI 文件名语义解析兜底（2026-11-13 试点）─────────────
# Why：extract_episode_num 的规则 D 宽松匹配是红线 7/8 两次事故的共同根因——
#      发布组命名无限而正则只能覆盖踩过的坑（HDR10→10、d3 1→E1、H.264→264）。
# 设计：规则 A/C（SxxEyy、第N集）为强证据永不进 AI；规则 B（季号冲突）永不进 AI；
#      只有「规则 D 弱匹配」与「全部未命中」才按分享包批量送模型复核。
#      模型只能把可疑数字改成 None，或为漏判补集数，严禁推翻强证据。
AI_PARSE_ENABLED = os.environ.get("AI_PARSE_ENABLED", "1") == "1"
AI_PARSE_SHADOW = os.environ.get("AI_PARSE_SHADOW", "0") == "1"
# 实测 cn/deepseek-v4.1-flash 批量 12 个文件名 2.56s；单条 1.85~2.5s
AI_PARSE_BASE_URL = os.environ.get("AI_PARSE_BASE_URL", "http://127.0.0.1:8317/v1")
AI_PARSE_MODEL = os.environ.get("AI_PARSE_MODEL", "cn/deepseek-v4.1-flash")
AI_PARSE_TIMEOUT = float(os.environ.get("AI_PARSE_TIMEOUT", "8.0"))
# 单次批量请求的文件名上限（超出分片，防止上下文过长导致 JSON 破损）
AI_PARSE_BATCH_SIZE = int(os.environ.get("AI_PARSE_BATCH_SIZE", "24"))
# 置信度门槛：低于此值一律判 None（宁可回落规则结果，也不采信模糊判断）
AI_PARSE_MIN_CONFIDENCE = float(os.environ.get("AI_PARSE_MIN_CONFIDENCE", "0.7"))
# 进程内熔断：连续失败达到阈值后进入冷却，期间直接回落规则结果
AI_PARSE_MAX_FAILS = int(os.environ.get("AI_PARSE_MAX_FAILS", "3"))
AI_PARSE_COOLDOWN = int(os.environ.get("AI_PARSE_COOLDOWN", "300"))

# ── 搜索源（网盘搜索站）───────────────────────────────
#
# ⚠️ DEPRECATED（2026-11-13）：本列表**无任何代码引用**，仅为历史遗留。
#   实际生效的搜索节点是 `panshare_matrix.ACTIVE_PAN_NODES`，那才是唯一事实来源。
#   两者曾经漂移（此处的「木偶」4 个地址实测全部失效，且从未被使用；
#   「快映」旧的 xsayang.fun 搜索接口超时）。**新增/调整节点请只改
#   panshare_matrix.py**，不要再往这里加。
PAN_SEARCH_SITES = [
    {"key": "快映", "name": "快映 • 4K", "api": "csp_PanWebShare",
     "site": ["http://xsayang.fun", "http://38.76.197.172:12521"]},
    {"key": "木偶", "name": "木偶 • 4K", "api": "csp_PanWebShare",
     "site": ["https://123.666291.xyz", "https://666.666291.xyz",
              "https://www.muou.site", "https://www.muou.asia"]},
    {"key": "多多", "name": "多多 • 4K", "api": "csp_PanWebShare",
     "site": ["https://tv.yydsys.top", "https://tv.yydsys.cc", "https://tv.214521.xyz"]},
    {"key": "蜡笔", "name": "蜡笔 • 4K", "api": "csp_PanWebShare",
     "site": ["https://www.fmcf.fun", "https://www.mmpanpan.site",
              "http://tvpanpan.site", "http://www.tvpanpan.site"]},
    {"key": "八零", "name": "八零 • 4K", "api": "csp_PanWebShare",
     "site": ["https://woog.nxog.eu.org", "https://ogkk.nxog.eu.org", "https://woog.430520.xyz"]},
    {"key": "虎斑", "name": "虎斑 • 4K", "api": "csp_PanWebShare",
     "site": ["http://xhban.xyz", "http://38.76.197.172:16969"]},
]
# 只接受夸克网盘分享（其他网盘转存不了）
ACCEPT_PAN_TYPES = ("quark", "夸克")

# ── 路径派生 ──────────────────────────────────────────
def ensure_dirs():
    """只建本地目录（日志/数据）。

    注意：QUARK_VOD_ROOT 是 OpenList 的【虚拟挂载路径】，不是本地路径，
    绝不能用 os.makedirs 创建——它由 OpenList 驱动在网盘侧管理。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)


def redact_cookie(cookie: str) -> str:
    """日志用：只显示 cookie 长度与前 8 字符。"""
    if not cookie:
        return "(未配置)"
    return f"{cookie[:8]}...({len(cookie)}字符)"
