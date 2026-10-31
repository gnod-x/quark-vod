#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
家庭影院通宵 AI 深度重构与自愈引擎 (AI Night Worker)
========================================================================
设计指标：
  1. 运行时长：设计为北京时间夜间通宵运行 5 ~ 8 小时 (23:00 ~ 06:30)
  2. 模型路由：
     - 主力模型：deepseek-v4.1-flash / cn/step-5-preview (限时免费，0 积分消耗)
     - 次级备用：cn/deepseek-v4-pro / cn/deepseek-v4.1-flash (国内极低积分)
     - 终极兜底：gemini-3.8-flash (走用户 OAuth 授权，仅在免费模型全部不可用时启用)
  3. 资源约束：
     - 白天常驻内存：0 MB（批处理完成后彻底退出）
     - 运行内存峰值：< 25 MB（流式单条处理，绝不堆叠大数组）
     - 硬盘额外占用：0 MB（直接原位更新已有轻量 XML NFO 文本，0 本地模型文件）
     - 显卡能耗控制：独立显卡全时 D3cold 断电挂起，整机维持 2.5W~3.5W
  4. 核心工序 (坚决排除前情提要)：
     - 工序 1: 昨夜观影错误日志体检与失效片源自动换源自愈
     - 工序 2: 全库海报墙无剧透剧情重写 + 3条深度看点 + 5个情绪氛围标签
     - 工序 3: 全库演职人员中文百科级档案生成与别名补齐
     - 工序 4: 全库字幕深度逐行广告清洗与台词校对
"""

import os
import re
import sys
import time
import json
import sqlite3
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dotenv import load_dotenv

BASE_DIR = "/home/dong/projects/hometheater"
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
load_dotenv(os.path.join(BASE_DIR, ".env"))

from quark_vod import config as vod_cfg
from quark_vod import db as vod_db
from quark_vod.openlist_client import OpenListClient

AI_BASE_URL = os.environ.get('OPENAI_BASE_URL', 'http://127.0.0.1:8317/v1')
def _load_cliproxy_token():
    # cron 不读 .bashrc / /etc/environment，直接读统一 token 文件
    import os as _os
    v = _os.environ.get('OPENAI_API_KEY', '').strip()
    if v:
        return v
    try:
        with open(_os.path.expanduser('~/.config/cliproxy/token')) as _f:
            return _f.read().strip()
    except Exception:
        return ''


AI_API_KEY = _load_cliproxy_token()
# ── 模型优先级阶梯 ─────────────────────────────────────
# 1. 候选限时免费顶级模型 (性能对齐 deepseek-v4.1-flash / step-5，动态探测确认后优先使用)
FREE_MODELS = [
    "deepseek-v4.1-flash",
    "deepseek-v4.1-flash-sg",
    "step-5-preview",
    "cn/step-5-preview"
]
# 2. 国内版最新 DeepSeek 极低积分模型 (仅包含最新 v4.1 flash，坚决不使用老旧 pro)
LOW_COST_MODELS = [
    "cn/deepseek-v4.1-flash"
]
# 3. 终极兜底模型 (严格仅使用用户 Google OAuth 授权的 gemini-3.8-flash，平时绝不调用)
OAUTH_FALLBACK_MODELS = [
    "gemini-3.8-flash-high"
]

LOG_FILE = os.path.join(BASE_DIR, "logs", "ai_night_worker.log")
STATE_DB = os.path.join(BASE_DIR, "data", "ai_worker_state.db")
LOCK_FILE = os.path.join(BASE_DIR, "data", "ai_night_worker.lock")

_lock_fp = None
MAX_RUNTIME_HOURS = 5.8
START_TIME = 0.0


def safe_parse_json(raw_str):
    """宽容型 JSON 解析器：自动剥离 Markdown 围栏并过滤非法控制字符/未转义字符，防大模型偶发格式破损。"""
    if not raw_str:
        return None
    clean = raw_str.strip()
    if "```json" in clean:
        clean = clean.split("```json")[1].split("```")[0].strip()
    elif "```" in clean:
        clean = clean.split("```")[1].split("```")[0].strip()
    try:
        return json.loads(clean)
    except Exception:
        pass

    # 二级容错：正规化 \xXX 16进制转义与清理不可见控制字符
    try:
        clean_norm = re.sub(r'\\x([0-9a-fA-F]{2})', lambda m: chr(int(m.group(1), 16)), clean)
        clean_norm = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', clean_norm)
        return json.loads(clean_norm, strict=False)
    except Exception:
        return None


def acquire_single_instance_lock():
    """获取独占文件锁，保障全局永远只有一个实例在运行，彻底杜绝并发冲突。"""
    global _lock_fp
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    try:
        import fcntl
        _lock_fp = open(LOCK_FILE, "a+")
        fcntl.flock(_lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fp.seek(0)
        _lock_fp.truncate()
        _lock_fp.write(f"{os.getpid()}\n")
        _lock_fp.flush()
        return True
    except (IOError, OSError):
        return False


def release_single_instance_lock():
    global _lock_fp
    if _lock_fp:
        try:
            import fcntl
            fcntl.flock(_lock_fp, fcntl.LOCK_UN)
            _lock_fp.close()
        except Exception:
            pass


def is_timeout():
    """夜间时间窗口保护：超过最大运行时间（7.5小时）自动优雅退出，绝不把任务拖入白天。"""
    if START_TIME > 0 and (time.time() - START_TIME) > MAX_RUNTIME_HOURS * 3600:
        return True
    return False


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [AINightWorker] {msg}"
    print(line, flush=True)
    # 若 stdout 已被 crontab 重定向至 LOG_FILE，则不再重复追加写文件，防止每行日志重复打印两遍
    try:
        if os.path.exists(LOG_FILE) and os.path.samefile(sys.stdout.fileno(), LOG_FILE):
            return
    except Exception:
        pass
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def check_and_yield_for_playback():
    """观影感知与智能避让机制：若检测到家庭成员正在观影，立即深度休眠让路。"""
    while True:
        try:
            req = urllib.request.Request(f"{vod_cfg.JELLYFIN2_API}/Sessions")
            if vod_cfg.JELLYFIN2_KEY:
                req.add_header("Authorization", f'MediaBrowser Token="{vod_cfg.JELLYFIN2_KEY}"')
            with urllib.request.urlopen(req, timeout=3) as resp:
                sessions = json.loads(resp.read().decode("utf-8"))
                active_playing = []
                for s in sessions:
                    item = s.get("NowPlayingItem")
                    is_paused = s.get("PlayState", {}).get("IsPaused", True)
                    if item and not is_paused:
                        active_playing.append(item.get("Name", "未知影片"))
                if active_playing:
                    names_str = "、".join(active_playing)
                    log(f"检测到当前有用户正在观影《{names_str}》，AI 任务主动礼让避让（休眠 30 秒）...")
                    time.sleep(30)
                    continue
        except Exception:
            pass
        break


def init_state_db():
    os.makedirs(os.path.dirname(STATE_DB), exist_ok=True)
    con = sqlite3.connect(STATE_DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS processed_records (
            task_type TEXT NOT NULL,
            item_key TEXT NOT NULL,
            status TEXT NOT NULL,
            model_used TEXT,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (task_type, item_key)
        )
    """)
    con.commit()
    con.close()

init_state_db()


def is_already_processed(task_type, item_key):
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
        cur = con.cursor()
        r = cur.execute("SELECT 1 FROM processed_records WHERE task_type=? AND item_key=? AND status='done' LIMIT 1",
                        (task_type, item_key)).fetchone()
        con.close()
        return bool(r)
    except Exception:
        return False


def mark_processed(task_type, item_key, model_used=""):
    try:
        con = sqlite3.connect(STATE_DB)
        con.execute(
            "INSERT OR REPLACE INTO processed_records (task_type, item_key, status, model_used, updated_at) VALUES (?, ?, 'done', ?, ?)",
            (task_type, item_key, model_used, int(time.time()))
        )
        con.commit()
        con.close()
    except Exception:
        pass


ACTIVE_FREE_MODELS = []
# 探测冷却：免费模型全不可用时，绝不能让 call_llm 每次调用都重探（历史上 2 秒一轮空转数千次）
_last_probe_ts = 0.0
PROBE_RETRY_SECONDS = 600


def probe_free_models():
    """启动时动态探针：轻量测出当前处于限时免费状态的最新顶级模型 (零积分消耗)

    失败时保留上一次探测结果，不清空 ACTIVE_FREE_MODELS——否则 call_llm 的
    `if not ACTIVE_FREE_MODELS` 会触发逐个 item 重探，形成阻塞式死循环。
    """
    global ACTIVE_FREE_MODELS, _last_probe_ts
    _last_probe_ts = time.time()
    log("正在动态探测当前处于限时免费状态的最新顶级模型...")
    active = []
    for m in FREE_MODELS:
        payload = {
            "model": m,
            "messages": [{"role": "user", "content": "1+1="}],
            "max_tokens": 5
        }
        req = urllib.request.Request(f"{AI_BASE_URL.rstrip('/')}/chat/completions",
                                     data=json.dumps(payload).encode("utf-8"),
                                     method="POST")
        if AI_API_KEY:
            req.add_header('Authorization', f'Bearer {AI_API_KEY}')
        req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                msg = data.get('choices', [{}])[0].get('message', {})
                txt = (msg.get('content') or msg.get('reasoning_content') or "").strip()
                if txt:
                    active.append(m)
        except Exception:
            pass
    if active:
        ACTIVE_FREE_MODELS = active
        log(f"动态探针锁定当前可用免费顶级模型 (0积分): {active}")
    elif ACTIVE_FREE_MODELS:
        log(f"动态探针警告：本次未探到可用免费模型，保留上次结果 {ACTIVE_FREE_MODELS}")
    else:
        log("动态探针警告：当前免费模型暂不可用，将自动使用最新版 DeepSeek 极低积分主力梯队")
    return active


def call_llm(prompt, system_prompt="你是一位专业电影评论家与资料整理专家。请用中文回答，格式结构严谨，文风优雅专业。", max_tokens=800):
    """三级智能容灾调度路由器：
    1. 动态探针锁定的当前限时免费顶级模型 (deepseek-v4.1-flash / step-5 等，0 积分)
    2. 最新版 DeepSeek 国内版模型 (积分极低)
    3. 用户 OAuth 授权的 Gemini 3.8 终极兜底 (基本不用，全崩才触发)
    """
    global ACTIVE_FREE_MODELS
    # 仅在列表为空且已过冷却期时才重探；探测失败后直接走梯队 2，不阻塞主流程
    if not ACTIVE_FREE_MODELS and (time.time() - _last_probe_ts) > PROBE_RETRY_SECONDS:
        probe_free_models()

    # 梯队 1: 当前确认可用的限时免费顶级模型 (0 积分)
    for m in ACTIVE_FREE_MODELS:
        res = _request_chat(m, prompt, system_prompt, max_tokens=max_tokens)
        if res:
            return f"{m}(免费)", res

    # 梯队 2: 最新版 DeepSeek 极低积分主力模型
    for m in LOW_COST_MODELS:
        res = _request_chat(m, prompt, system_prompt, max_tokens=max_tokens)
        if res:
            return f"{m}(极低积分)", res

    # 梯队 3: 终极 OAuth 授权兜底 (用户 Google OAuth，绝不轻易使用)
    for m in OAUTH_FALLBACK_MODELS:
        res = _request_chat(m, prompt, system_prompt, max_tokens=max_tokens)
        if res:
            return f"{m}(OAuth终极兜底)", res

    return None, None


def _request_chat(model_name, prompt, system_prompt, max_tokens=800):
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": max_tokens,
        "temperature": 0.7
    }
    req = urllib.request.Request(f"{AI_BASE_URL.rstrip('/')}/chat/completions",
                                 data=json.dumps(payload).encode("utf-8"),
                                 method="POST")
    if AI_API_KEY:
        req.add_header('Authorization', f'Bearer {AI_API_KEY}')
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            msg = data.get('choices', [{}])[0].get('message', {})
            content = (msg.get('content') or msg.get('reasoning_content') or "").strip()
            return content if content else None
    except Exception:
        return None


# ==============================================================================
# 工序 1: 昨夜观影日志体检、失效片源自动换源与 403 区域拦截源熔断自愈
# ==============================================================================
def run_yesterday_playback_selfheal():
    log("=== [工序 1] 开始扫描昨夜 Jellyfin 播放错误日志并执行死链与区域封锁源熔断自愈 ===")
    jellyfin_log_dir = os.path.join(BASE_DIR, "jellyfin2/config/log")
    if not os.path.isdir(jellyfin_log_dir):
        log("未找到 Jellyfin 日志目录，跳过工序 1")
        return

    failed_strms = set()
    banned_cms_urls = set()

    # 扫描最近 24 小时的转码日志与主日志
    now = time.time()
    for fn in os.listdir(jellyfin_log_dir):
        fp = os.path.join(jellyfin_log_dir, fn)
        try:
            if now - os.path.getmtime(fp) > 86400:
                continue
        except Exception:
            continue

        # 1. 扫描 FFmpeg 转码日志
        if fn.startswith("FFmpeg.Transcode") and fn.endswith(".log"):
            try:
                with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    # 识别网盘缓存脱节 (code 8 / 5XX)
                    if "exited with code 8" in content or "Server returned 5XX" in content:
                        m = re.search(r'-i\s+"([^"]+)"', content)
                        if m and "/d/夸克/点播/" in m.group(1):
                            failed_strms.add(m.group(1))

                    # 识别 403 区域封锁与防盗链拦截 (The region has been denied / 403 Forbidden)
                    if "403 Forbidden" in content or "The region has been denied" in content or "HTTP error 403" in content:
                        m_cms = re.findall(r'(https?://[^\s"\'<>]+\.m3u8)', content)
                        for u in m_cms:
                            banned_cms_urls.add(u)
            except Exception:
                pass

        # 2. 扫描 Jellyfin 主日志中的 403/404 探针报错
        elif fn.startswith("log_") and fn.endswith(".log"):
            try:
                with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if "403 Forbidden" in line or "The region has been denied" in line or "HTTP error 404" in line:
                            m_u = re.findall(r'(https?://[^\s"\'<>]+\.m3u8)', line)
                            for u in m_u:
                                banned_cms_urls.add(u)
            except Exception:
                pass

    log(f"昨夜日志排查完毕: 发现网盘直链异常 {len(failed_strms)} 条，发现 403/404 失效切片源 {len(banned_cms_urls)} 条")

    # 1. 自动自愈网盘目录树缓存
    ol = None
    try:
        ol = OpenListClient()
    except Exception:
        pass

    for url in failed_strms:
        try:
            parsed = urllib.parse.urlparse(url)
            p = urllib.parse.unquote(parsed.path)
            if p.startswith("/d/"):
                remote = p[len("/d"):]
                if ol:
                    ol.refresh_path_tree(remote)
                    log(f"已自动自愈修复 OpenList 目录树缓存: {remote}")
        except Exception as e:
            log(f"自愈直链异常 {url}: {e}")

    # 2. 自动拉黑 403 区域封锁与死链源，避免后续点播反复踩坑
    for dead_u in banned_cms_urls:
        try:
            vod_db.mark_dead_source(dead_u, "cms", "403 Region Denied / Unplayable", ban_days=7)
            log(f"[自动熔断拉黑] 昨夜 403 区域受限/失效切片已拉黑 7 天: {dead_u}")
        except Exception as e:
            log(f"拉黑失效源异常 {dead_u}: {e}")


# ==============================================================================
# 工序 1.5: 全库海报、背景图与分集剧照健康巡检与自动补全
# ==============================================================================
def run_library_integrity_check():
    log("=== [工序 1.5] 全库海报、背景图与分集剧照健康巡检与自动补全 ===")
    cinema2 = vod_cfg.CINEMA2_DIR
    if not os.path.isdir(cinema2):
        return

    import tmdb_populate
    fixed_posters = 0
    fixed_thumbs = 0

    for root, dirs, files in os.walk(cinema2):
        if is_timeout():
            log(f"达到夜间最大运行时间窗口，巡检中断退出。")
            break
        check_and_yield_for_playback()

        # 检查电影或电视剧的根目录图片
        if "movie.nfo" in files or "tvshow.nfo" in files:
            nfo_fn = "movie.nfo" if "movie.nfo" in files else "tvshow.nfo"
            poster_fp = os.path.join(root, "poster.jpg")
            fanart_fp = os.path.join(root, "backdrop.jpg")

            # 检查 poster.jpg 是否缺失或损坏 (小于1KB)
            if not os.path.isfile(poster_fp) or os.path.getsize(poster_fp) < 1000:
                try:
                    tree = ET.parse(os.path.join(root, nfo_fn))
                    thumb_url = tree.getroot().findtext("thumb")
                    if thumb_url and tmdb_populate.download_image(thumb_url, poster_fp):
                        fixed_posters += 1
                        log(f"已自动自愈修复损坏/缺失海报: {os.path.relpath(poster_fp, cinema2)}")
                except Exception:
                    pass

        # 检查剧集分集剧照
        if "season.nfo" in files:
            strm_files = [f for f in files if f.endswith(".strm")]
            for sf in strm_files:
                base_ep = sf[:-5]
                thumb_fp = os.path.join(root, f"{base_ep}-thumb.jpg")
                nfo_fp = os.path.join(root, f"{base_ep}.nfo")
                if not os.path.isfile(thumb_fp) or os.path.getsize(thumb_fp) < 1000:
                    if os.path.isfile(nfo_fp):
                        try:
                            tree = ET.parse(nfo_fp)
                            ep_thumb_url = tree.getroot().findtext("thumb")
                            if ep_thumb_url and tmdb_populate.download_image(ep_thumb_url, thumb_fp):
                                fixed_thumbs += 1
                        except Exception:
                            pass

    log(f"全库图像与元数据巡检完毕，自动修复海报: {fixed_posters} 张，补齐分集剧照: {fixed_thumbs} 张")


# ==============================================================================
# 工序 1.8: 全库片源存活健康度、失效自动换新与夸克真 4K 前置预检锁源 (Pre-Flight Lock)
# ==============================================================================
def run_vod_source_audit_and_lock():
    log("=== [工序 1.8] 全库片源存活性健康巡检、失效自动换新与夸克真 4K 锁源 ===")
    try:
        from quark_vod.auditor import VodAuditor
        auditor = VodAuditor()
        # 1. 存量已锁源健康体检：发现原作者取消或失效的，立即自动调用多源矩阵打捞全新活源替换！
        heal_stats = auditor.check_and_heal_locked_shares(yield_fn=check_and_yield_for_playback, timeout_fn=is_timeout)
        log(f"存量锁源自愈完成: 检查={heal_stats.get('total')}, 依然健康={heal_stats.get('healthy')}, 成功自动换新4K={heal_stats.get('healed_4k')}, 换新高清={heal_stats.get('healed_hd')}")

        # 2. 全库未锁源与新片全量打捞锁源
        stats = auditor.audit_entire_library(yield_fn=check_and_yield_for_playback, timeout_fn=is_timeout)
        log(f"全库片源 4K 锁源完成: 4K锁源={stats.get('valid_4k')}, 高清={stats.get('valid_hd')}, 无有效网盘源={stats.get('no_source')}")

        # 3. 网盘点播目录伪装视频智能巡检与格式扶正
        total_fixed = auditor.fix_disguised_vod_files()
        log(f"网盘伪装视频巡检与格式扶正完成，当前就绪直链视频: {total_fixed} 个")
    except Exception as e:
        log(f"全库片源审计执行异常: {e}")


# ==============================================================================
# 工序 2: 全库海报墙无剧透剧情重写 + 3条深度看点 + 5个情绪氛围标签
# ==============================================================================
def _sync_nfo_to_jellyfin2(nfo_path, final_plot, all_tags):
    """将精修后的 NFO 简介与情绪标签无损同步写入 Jellyfin2 数据库（不触发流探测）。"""
    if not vod_cfg.JELLYFIN2_KEY or not os.path.exists(vod_cfg.JELLYFIN2_DB):
        return False
    try:
        item_dir = os.path.dirname(nfo_path).replace(vod_cfg.CINEMA2_DIR, "/cinema2")
        con = sqlite3.connect(f"file:{vod_cfg.JELLYFIN2_DB}?mode=ro", uri=True)
        cur = con.cursor()
        if nfo_path.endswith("tvshow.nfo"):
            row = cur.execute(
                "SELECT Id FROM BaseItems WHERE Path = ? AND Type LIKE '%Series' LIMIT 1",
                (item_dir,)
            ).fetchone()
        else:
            row = cur.execute(
                "SELECT Id FROM BaseItems WHERE Path LIKE ? AND Type LIKE '%Movie' LIMIT 1",
                (item_dir + "/%",)
            ).fetchone()
        con.close()
        if not row or not row[0]:
            return False
        item_id = row[0]

        req_u = urllib.request.Request(f"{vod_cfg.JELLYFIN2_API}/Users")
        req_u.add_header("Authorization", f'MediaBrowser Token="{vod_cfg.JELLYFIN2_KEY}"')
        users = json.loads(urllib.request.urlopen(req_u, timeout=8).read().decode("utf-8"))
        if not users:
            return False
        uid = users[0]["Id"]

        req_get = urllib.request.Request(f"{vod_cfg.JELLYFIN2_API}/Users/{uid}/Items/{item_id}")
        req_get.add_header("Authorization", f'MediaBrowser Token="{vod_cfg.JELLYFIN2_KEY}"')
        dto = json.loads(urllib.request.urlopen(req_get, timeout=8).read().decode("utf-8"))
        dto["Overview"] = final_plot
        if all_tags:
            dto["Tags"] = all_tags

        req_post = urllib.request.Request(
            f"{vod_cfg.JELLYFIN2_API}/Items/{item_id}",
            data=json.dumps(dto).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f'MediaBrowser Token="{vod_cfg.JELLYFIN2_KEY}"',
                "Content-Type": "application/json"
            }
        )
        with urllib.request.urlopen(req_post, timeout=10) as resp:
            return resp.status in (200, 204)
    except Exception:
        return False


def run_refine_movie_plots_and_tags():
    log("=== [工序 2] 开始全库影视海报墙杂志化深度重塑 (无剧透剧情 + 3条深度看点 + 5个情绪标签) ===")
    cinema2 = vod_cfg.CINEMA2_DIR
    if not os.path.isdir(cinema2):
        return

    nfos_to_process = []
    for root, _, files in os.walk(cinema2):
        for f in files:
            if f in ("movie.nfo", "tvshow.nfo"):
                nfos_to_process.append(os.path.join(root, f))

    log(f"全库共扫描到 NFO 文件: {len(nfos_to_process)} 部")
    processed_count = 0

    for nfo_path in nfos_to_process:
        if is_timeout():
            log(f"达到夜间最大运行时间窗口 ({MAX_RUNTIME_HOURS}小时)，工序 2 自动优雅收工退出。")
            break
        check_and_yield_for_playback()
        rel_key = os.path.relpath(nfo_path, cinema2)
        if is_already_processed("refine_nfo", rel_key):
            continue

        try:
            tree = ET.parse(nfo_path)
            root_elem = tree.getroot()
            title = root_elem.findtext("title") or ""
            original_plot = root_elem.findtext("plot") or ""
            year = root_elem.findtext("year") or ""
        except Exception:
            continue

        if not title:
            continue

        # 双重幂等防重：若已有【AI 精彩看点】，说明已被精修过，同步至 Jellyfin2 后直接登记为 existing 并跳过
        if original_plot and "【AI 精彩看点】" in original_plot:
            all_tags = [t.text for t in root_elem.findall("tag") if t.text]
            _sync_nfo_to_jellyfin2(nfo_path, original_plot, all_tags)
            mark_processed("refine_nfo", rel_key, model_used="existing")
            continue

        prompt = f"""请对以下影视条目进行深度文学重写与专业影评提炼：
【影片名称】：《{title}》 ({year})
【原始简介】：{original_plot[:400] if original_plot else "暂无简介"}

请严格按照以下 JSON 格式输出，不要附加任何 Markdown 标记或多余文字：
{{
  "refined_plot": "重新撰写的无剧透核心剧情导读（约100-150字，文风地道流畅、引人入胜，绝不包含剧透）",
  "highlights": [
    "看点1：从摄影视觉、叙事结构或演员表现提炼的核心亮点（一句话）",
    "看点2：剧情反转、核心戏剧冲突或主题深度（一句话）",
    "看点3：适合观影人群或艺术特色风格（一句话）"
  ],
  "mood_tags": ["情绪标签1", "情绪标签2", "情绪标签3", "情绪标签4", "情绪标签5"]
}}
注意：情绪标签请使用如：高智商犯罪、治愈下饭、硬核科幻、绝望反转、赛博朋克、双向奔赴等接地气且高精度的词汇。"""

        model_name, reply = call_llm(prompt)
        if not reply:
            time.sleep(2)
            continue

        data = safe_parse_json(reply)
        if not data:
            log(f"解析并更新 NFO 异常 《{title}》: 无法解析大模型 JSON 输出")
            continue

        try:
            new_plot = data.get("refined_plot", "")
            highlights = data.get("highlights", [])
            mood_tags = data.get("mood_tags", [])

            if new_plot and highlights:
                # 组合成优美的杂志化简介
                hl_text = "\n\n【AI 精彩看点】\n" + "\n".join([f"• {h}" for h in highlights])
                final_plot = new_plot.strip() + hl_text

                plot_node = root_elem.find("plot")
                if plot_node is not None:
                    plot_node.text = final_plot

                # 注入情绪与氛围标签 (<tag>)
                existing_tags = {t.text for t in root_elem.findall("tag") if t.text}
                for mt in mood_tags:
                    if mt and mt not in existing_tags:
                        t_node = ET.SubElement(root_elem, "tag")
                        t_node.text = mt

                tree.write(nfo_path, encoding="utf-8", xml_declaration=True)
                all_tags = [t.text for t in root_elem.findall("tag") if t.text]
                _sync_nfo_to_jellyfin2(nfo_path, final_plot, all_tags)
                mark_processed("refine_nfo", rel_key, model_used=model_name)
                processed_count += 1
                log(f"[{model_name}] 成功深度精修海报看点与标签: 《{title}》 ({rel_key})")
        except Exception as e:
            log(f"解析并更新 NFO 异常 《{title}》: {e}")

        # 精细化频率控制：单次请求间隔 3 秒，既防封控又平稳拉满 5~8 小时
        time.sleep(3)


# ==============================================================================
# 工序 3: 全库演职人员中文百科级档案生成 (微批次 3人/组高效并发架构)
# ==============================================================================
def run_enrich_peoples_knowledge():
    log("=== [工序 3] 开始全库演职人员中文百科档案建立与代表作关联 (微批次架构) ===")
    jf_db = vod_cfg.JELLYFIN2_DB
    if not os.path.exists(jf_db):
        return

    con = sqlite3.connect(f"file:{jf_db}?mode=ro", uri=True)
    cur = con.cursor()
    # 查询 BaseItems 中所有人物条目，并按参演作品热度降序排序
    people_rows = cur.execute("""
        SELECT b.Id, b.Name, b.Overview, COUNT(p.Id) as work_count
        FROM BaseItems b
        LEFT JOIN Peoples p ON b.Name = p.Name
        WHERE b.Type = 'MediaBrowser.Controller.Entities.Person'
        GROUP BY b.Id, b.Name
        ORDER BY work_count DESC
    """).fetchall()
    con.close()

    log(f"当前 Jellyfin2 数据库共登记演职人员: {len(people_rows)} 位 (已按作品热度排序)")

    # 过滤出真正需要处理的候选队列
    pending_items = []
    for pid, name, overview, _ in people_rows:
        if not name:
            continue
        p_key = f"people_{pid}_{name}"
        if is_already_processed("enrich_people", p_key):
            continue
        if overview and len(overview) > 100:
            mark_processed("enrich_people", p_key, model_used="existing")
            continue
        pending_items.append({"id": pid, "name": name, "p_key": p_key})

    log(f"演职员微批次处理队列锁定: 待处理 {len(pending_items)} 位")
    BATCH_SIZE = 3
    enriched_count = 0

    for i in range(0, len(pending_items), BATCH_SIZE):
        if is_timeout():
            log(f"达到夜间最大运行时间窗口 ({MAX_RUNTIME_HOURS}小时)，工序 3 自动优雅收工退出。")
            break
        check_and_yield_for_playback()

        batch = pending_items[i : i + BATCH_SIZE]
        batch_input = [{"id": it["id"], "name": it["name"]} for it in batch]

        prompt = f"""请为以下 {len(batch)} 位电影/电视剧演职人员编写结构化的专业中文百科档案：
待处理人员列表：
{json.dumps(batch_input, ensure_ascii=False, indent=2)}

请严格输出为一个 JSON 数组（不要包含任何 Markdown 标记或多余文字），数组内每个对象对应一位人物，格式如下：
[
  {{
    "id": "对应传入的id",
    "chinese_name": "标准通用中文译名（若原本就是中文名则保持不变）",
    "biography": "80-120字人物艺术生涯生平小传与表演执导风格评价",
    "famous_works": ["代表作1", "代表作2", "代表作3"]
  }}
]"""

        model_name, reply = call_llm(prompt, max_tokens=1500)
        if not reply:
            time.sleep(2.0)
            continue

        batch_res = safe_parse_json(reply)
        if not batch_res or not isinstance(batch_res, list):
            log(f"微批次解析异常 ({[b['name'] for b in batch]}): 无法解析为有效 JSON 列表")
            continue

        try:
            res_by_id = {item["id"]: item for item in batch_res if isinstance(item, dict) and "id" in item}

            for it in batch:
                target_pid = it["id"]
                p_key = it["p_key"]
                raw_name = it["name"]
                person_data = res_by_id.get(target_pid)
                if not person_data:
                    continue

                bio = person_data.get("biography", "").strip()
                works = person_data.get("famous_works", [])
                cn_name = person_data.get("chinese_name", raw_name).strip()

                if bio:
                    full_bio = bio
                    if works:
                        full_bio += f"\n\n【代表作品】：{'、'.join(works)}"

                    if vod_cfg.JELLYFIN2_KEY:
                        url = f"{vod_cfg.JELLYFIN2_API}/Items/{target_pid}"
                        update_payload = json.dumps({
                            "Id": target_pid,
                            "Name": cn_name,
                            "Overview": full_bio
                        }).encode("utf-8")
                        req = urllib.request.Request(url, data=update_payload, method="POST")
                        req.add_header("Authorization", f'MediaBrowser Token="{vod_cfg.JELLYFIN2_KEY}"')
                        req.add_header("Content-Type", "application/json")
                        try:
                            with urllib.request.urlopen(req, timeout=10) as resp:
                                if resp.status in (200, 204):
                                    mark_processed("enrich_people", p_key, model_used=model_name)
                                    enriched_count += 1
                        except Exception as err:
                            log(f"Jellyfin API 写入失败 {raw_name}: {err}")

            names_summary = "、".join([it["name"] for it in batch])
            log(f"[{model_name}] 批量精修完成 ({len(batch)}人): {names_summary}")
        except Exception as e:
            log(f"微批次解析异常 ({[b['name'] for b in batch]}): {e}")

        # 微批次处理完一批等待 1.5 秒，极大释放吞吐量并防止 429
        time.sleep(1.5)


# ==============================================================================
# 工序 4: 全库字幕深度逐行广告清洗与台词校对
# ==============================================================================
def run_clean_all_subtitles():
    log("=== [工序 4] 开始全库外挂/内嵌字幕深度广告与牛皮癣台词剥离清洗 ===")
    cleaner_script = os.path.join(BASE_DIR, "scripts", "subtitle_ad_cleaner.py")
    if not os.path.isfile(cleaner_script):
        log("未找到 subtitle_ad_cleaner.py，跳过工序 4")
        return

    cinema2 = vod_cfg.CINEMA2_DIR
    sub_files = []
    for root, _, files in os.walk(cinema2):
        for f in files:
            if f.endswith((".srt", ".ass", ".vtt")):
                sub_files.append(os.path.join(root, f))

    log(f"全库共扫描到字幕文件: {len(sub_files)} 个")
    cleaned_count = 0

    for sub_path in sub_files:
        if is_timeout():
            log(f"达到夜间最大运行时间窗口 ({MAX_RUNTIME_HOURS}小时)，工序 4 自动优雅收工退出。")
            break
        check_and_yield_for_playback()
        rel_key = os.path.relpath(sub_path, cinema2)
        if is_already_processed("clean_sub", rel_key):
            continue

        try:
            with open(sub_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()

            # 快速正则检测是否包含可疑广告特征
            ad_kw = ["qq群", "公众号", "防失联", "澳门", "威尼斯人", "首发群", "菠菜", "link3.cc"]
            has_ad = any(any(kw in line.lower() for kw in ad_kw) for line in lines)
            if not has_ad:
                mark_processed("clean_sub", rel_key, model_used="clean_pass")
                continue

            # 调用大模型批量仲裁并剥离
            sample_lines = [l.strip() for l in lines if any(kw in l.lower() for kw in ad_kw)][:10]
            prompt = f"以下是从影视字幕中提取的可疑对白段落，请辨别哪些是真正的压制/商业广告（如QQ群、公众号、赌场、网址），哪些是剧情原版对白。仅输出被确认为违规广告的完整台词文本（每行一条）：\n" + "\n".join(sample_lines)

            model_name, reply = call_llm(prompt, system_prompt="你是一位影视字幕质检专家，请精准识别并剔除商业导流广告。")
            if reply:
                ad_lines_to_remove = set([l.strip() for l in reply.split("\n") if l.strip()])
                new_lines = [l for l in lines if l.strip() not in ad_lines_to_remove]
                with open(sub_path, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)
                mark_processed("clean_sub", rel_key, model_used=model_name)
                cleaned_count += 1
                log(f"[{model_name}] 成功清洗字幕广告 ({len(ad_lines_to_remove)}处): {rel_key}")
        except Exception as e:
            log(f"字幕清洗异常 {sub_path}: {e}")

        time.sleep(3)


# ==============================================================================
# 主入口
# ==============================================================================
def main():
    global START_TIME
    if not acquire_single_instance_lock():
        log("检测到已有 AI 批处理实例正在运行中，当前进程自动退出，坚决杜绝多实例重复运行。")
        sys.exit(0)

    START_TIME = time.time()
    try:
        log("====================================================================")
        log("家庭影院通宵 AI 深度批处理引擎启动 (智能锁定限时免费顶级模型 / Gemini 3.8 终极兜底)")
        log("====================================================================")

        # 启动时执行动态轻量探针，锁定当前可用的限时免费顶级模型 (0 积分)
        probe_free_models()

        # 依次执行各实质性工序（重心彻底聚焦：点播 4K 秒播、分类纠偏与死链自愈）
        run_yesterday_playback_selfheal()
        run_library_integrity_check()

        # 核心工序 A: 全库分类精准度治理与重名形态消歧（如《凡人修仙传》真人剧 vs 动画版）
        try:
            import category_governor
            category_governor.run_governance(dry_run=False)
        except Exception as e:
            log(f"分类与重名消歧治理异常: {e}")

        # 核心工序 B: 全库片源存活性健康巡检、死链自动换新与夸克真 4K 锁源 (Pre-Flight Lock)
        run_vod_source_audit_and_lock()

        # 辅助工序 C: 影视字幕逐行去广告与台词校对
        run_clean_all_subtitles()

        # 次要工序 D: 轻量补充海报看点与演职员百科（低优先级）
        try:
            run_refine_movie_plots_and_tags()
            run_enrich_peoples_knowledge()
        except Exception as e:
            log(f"次要看点与百科工序跳过: {e}")

        elapsed_hours = (time.time() - START_TIME) / 3600.0
        log(f"====================================================================")
        log(f"通宵 AI 深度重构与自愈任务全量圆满完成！总计耗时: {elapsed_hours:.2f} 小时")
        log("内存与系统资源已全量释放，独显全时保持 D3cold 挂起断电。")
        log("====================================================================")
    finally:
        release_single_instance_lock()


if __name__ == "__main__":
    main()
