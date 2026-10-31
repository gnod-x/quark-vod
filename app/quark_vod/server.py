#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
点播 HTTP 服务（端口 8099）

Jellyfin2 播放 pending strm 时会请求本服务：

  GET /pending?cat=...&t=...&s=0&e=0
      → 已转存: 302 重定向到 OpenList 直链（秒播）
      → 未转存且无分享链接: 503 + JSON 说明（前端提示需先提供片源）
      → 未转存但有分享链接: 现场转存 → 回写 strm → 302

  GET /health          服务自检
  GET /stats           转存/播放/清理统计
  GET /capability      写能力与 cookie 就绪状态

设计要点：
  - 302 重定向让播放器直接连 OpenList，本服务不中转视频流量
  - 转存是同步阻塞的（播放器等结果），用 TRANSFER_TIMEOUT 兜底
  - 任何失败都返回明确 JSON，不吐空响应（避免 Jellyfin 抛 JsonException）
"""

import json
import os
import re
import socketserver
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

from . import config
from . import db
from . import strm_manager
from . import cleaner
from .openlist_client import OpenListClient, OpenListError

# ── 全局单例 ──────────────────────────────────────────
_ol = None
_ol_err = ""
_ol_retry_at = 0.0        # 下次允许重试构造的时间戳（冷却用）
_transfer = None
_OL_RETRY_COOLDOWN = 10   # 秒；OpenList 未就绪时最多每 10s 重试一次

# ── 并发防重互斥 ──────────────────────────────────────
_in_flight_lock = threading.Lock()
_in_flight_transfers = {}  # key -> threading.Event()

# ── 无源负缓存（2026-10-20《爱你》转圈事故）────────────────
# key: f"nomiss:{cat}:{title}:{season}:{episode}" -> 过期时间戳
# 作用：某片确认无源后 10 分钟内秒返 503，避免 Jellyfin ffprobe 反复
#      触发 20+ 秒的搜索链路把客户端卡在转圈状态。
_nomiss_lock = threading.Lock()
_nomiss_cache = {}
NOMISS_TTL = 600          # 秒


# 死链熔断识别码（2026-10-20 统一）。
# 旧实现中「前置验活」与「转存熔断」两条路径各写一份识别码且不一致——
# 前置覆盖 41010/41031/违规/封禁，转存只覆盖 41011/41012，
# 导致 41010「文件涉及违规内容」等死链在转存阶段不被拉黑，
# 下次点播会重复抓取、重复转存尝试，白耗 3~8s。
# 现统一为一份，两处共用。
DEAD_SHARE_MARKERS = [
    "41011", "41012", "41031", "41010",
    "已取消了分享", "已失效", "已过期",
    "链接内没有文件", "没有文件", "违规", "封禁",
]


def _mark_no_source(cat, title, season, episode):
    """标记某片在 TTL 内无可用片源。"""
    with _nomiss_lock:
        _nomiss_cache[f"nomiss:{cat}:{title}:{season}:{episode}"] = time.time() + NOMISS_TTL
        # 简单容量保护，避免长期运行无限增长
        if len(_nomiss_cache) > 2000:
            now_t = time.time()
            for k in [k for k, v in _nomiss_cache.items() if v < now_t]:
                _nomiss_cache.pop(k, None)

def _clean_title_key(title):
    return re.sub(r"\s*[(（]\d{4}[)）]\s*$", "", (title or "").strip()).lower()


def get_openlist():
    """获取 OpenList 客户端（失败可重试）。

    2026-10-16 修复：原实现用 `_ol_err` 做永久锁——一旦构造失败就再也不重试。
    实测 2026-10-16 10:49:32 cinema-quark 启动，OpenList 容器 10:49:35 才就绪，
    仅差 3 秒；启动瞬间 `_login()` 连不上（Errno 111 Connection refused），
    `_ol_err` 被永久记住。此后 OpenList 早已恢复，8099 却持续返回
    `503 OpenList 不可用` → 已转存影片无法 302 到直链 → Jellyfin 拿到 503 →
    ffmpeg `exited with code 8` → 播放黑屏。
    现在改为带冷却的重试：失败后记录错误，但允许在冷却期后重新尝试。
    """
    global _ol, _ol_err, _ol_retry_at
    if _ol is not None:
        return _ol
    now = time.time()
    if now < _ol_retry_at:
        return None                      # 冷却期内不重复尝试，避免打爆日志
    try:
        _ol = OpenListClient()
        _ol_err = ""
        return _ol
    except Exception as e:
        _ol_err = str(e)
        _ol_retry_at = now + _OL_RETRY_COOLDOWN
        return None


def get_transfer():
    """获取转存器单例。

    注意：不在 OpenList 未就绪时缓存实例——QuarkTransfer 会持有传入的
    ol（可能为 None）并导致后续所有转存报"OpenList 客户端未注入"。
    OpenList 恢复后应能拿到一个完好的转存器。
    """
    global _transfer
    if _transfer is not None:
        return _transfer
    ol = get_openlist()
    if ol is None:
        return None                      # 不缓存坏实例，下次再试
    from .transfer import QuarkTransfer
    _transfer = QuarkTransfer(ol)
    return _transfer


def _direct_url(remote_path):
    """由远端路径生成【播放用】OpenList 直链（纯字符串拼接，无网络调用）。

    2026-10-16 读写分离重构：入参是转存写入路径（/夸克网盘/...），
    这里先经 config.play_path() 换成播放挂载（/夸克，QuarkTV 驱动，不限速），
    再拼成直链。实测 QuarkTV 比 Quark(Web) 快 6 倍，是流畅播放 4K/Remux 的前提。

    与 OpenListClient.direct_url() 的区别：不依赖客户端实例——
    已转存影片的播放重定向不应被 OpenList 的瞬时不可用阻塞。
    """
    enc = urllib.parse.quote(config.play_path(remote_path))
    return f"{config.OPENLIST_PUBLIC}/d{enc}"


def _wait_direct_url_ready(direct_url, remote_path=None, max_wait=3.5):
    """自检直链可读性（Read-After-Write 闭环），严禁向播放器与探针透传 500 死链。

    轮询向 direct_url 发送单字节 Range 请求：
      - 返回 200/206: 表明 OpenList 与网盘流通道畅通，立即可播
      - 返回 500/失败: 触发 OpenList 补救刷新，等待短暂重试
    """
    start_t = time.time()
    while time.time() - start_t < max_wait:
        try:
            req = urllib.request.Request(direct_url, method="GET")
            req.add_header("Range", "bytes=0-0")
            req.add_header("User-Agent", "curl/8.5.0")
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.status in (200, 206):
                    return True
        except urllib.error.HTTPError as e:
            if e.code == 500 and remote_path:
                ol = get_openlist()
                if ol:
                    try:
                        ol.refresh_path_tree(config.play_path(remote_path))
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(0.4)
    return False


def _remote_still_exists(remote_path):
    """校验网盘文件是否仍存在（防线①，2026-10-16 新增）。

    必须用【夸克官方 API】——OpenList 列表缓存严重滞后（实测 /夸克/点播
    只显示 1 项而实际有 13 项），用它校验会把已删除的文件误判为存在、
    也会把有效影片误判为失效。

    【调用量控制（红线 #23，夸克封控）】
    该 API 用 Web cookie 认证，而红线 #23 要求把 cookie 使用面收敛到
    写入侧。故此处复用 selfheal 的**批量**校验：只列一次 /点播 根目录
    即可判定（本函数只查一个路径，故为 1 次调用，而非原逐级下钻的 6 次）。

    保守策略：无法校验时（OpenList 不可用/网络异常）返回 True，
    宁可让本次播放拿到旧直链（可能失败），也不要误作废有效记录。
    """
    ol = get_openlist()
    if ol is None:
        return True                     # 无法校验，保守放行
    try:
        from . import selfheal
        from .transfer import QuarkTransfer
        tr = QuarkTransfer(ol)
        return selfheal._remote_exists(tr, remote_path)
    except Exception:
        return True                     # 校验异常，保守放行



def trigger_preheat_task(cat, title, season=0, episode=0):
    """触发单部影片或单集的后台异步转存预热。"""
    if not title:
        return False, "missing title"
    if not cat:
        try:
            row_map = db._conn().execute(
                "SELECT cat, title FROM strm_map WHERE title LIKE ? LIMIT 1",
                (f"%{_clean_title_key(title)}%",)
            ).fetchone()
            if row_map:
                cat = row_map["cat"]
        except Exception:
            pass

    row = db.find_done_transfer(cat, title, season, episode)
    if row and row["remote_path"] and _remote_still_exists(row["remote_path"]):
        return True, "already_ready"

    key = (_clean_title_key(title), season, episode)
    with _in_flight_lock:
        if key in _in_flight_transfers:
            return True, "in_progress"

    def _bg_preheat():
        try:
            from . import search as search_mod
            prefer = config.COOKIE_READY and config.QUARK_FIRST
            res, url = search_mod.resolve_for_play(cat, title, season, episode, prefer_panshare=prefer)
            if res and res.engine == "panshare" and res.share_url:
                tr = get_transfer()
                if tr and tr.is_ready():
                    tid, rpath = tr.transfer(cat, title, season, episode, res.share_url, res.pan_type or "quark")
                    if rpath:
                        d_url = _direct_url(rpath)
                        strm_manager.activate(cat, title, season, episode, d_url)
                        print(f"[{config.SERVICE_NAME}] [预热成功] {title} S{season}E{episode} -> {d_url}", flush=True)
        except Exception as e:
            print(f"[{config.SERVICE_NAME}] [预热异常] {title} S{season}E{episode}: {e}", flush=True)

    threading.Thread(target=_bg_preheat, daemon=True, name=f"preheat-{title}-s{season}e{episode}").start()
    return True, "started"


def auto_preheat_next_episode(cat, title, season, episode):
    """自动异步预热下一集，实现追剧无感秒播。"""
    if season <= 0 or episode <= 0:
        return
    next_ep = episode + 1
    try:
        row = db._conn().execute(
            "SELECT 1 FROM strm_map WHERE cat=? AND title=? AND season=? AND episode=? LIMIT 1",
            (cat, title, season, next_ep)
        ).fetchone()
        if not row:
            return
        done = db.find_done_transfer(cat, title, season, next_ep)
        if done and done["remote_path"]:
            return
        print(f"[{config.SERVICE_NAME}] 发现正在追剧 {title} S{season}E{episode}，自动在后台预热下一集 E{next_ep}", flush=True)
        trigger_preheat_task(cat, title, season, next_ep)
    except Exception as e:
        print(f"[{config.SERVICE_NAME}] 自动预热下一集失败: {e}", flush=True)

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 静音，自行记录

    # ── 响应助手 ──────────────────────────────────────
    def _send(self, code, body=b"", ctype="application/json", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            # HTTP 头只允许 latin-1（RFC 7230）。CMS 源直链可能带裸中文路径段
            # （实测如 "https://p.bvvvvvvvvv1f.com/video/babai/HD国语/index.m3u8"），
            # 直接 send_header 会 UnicodeEncodeError 崩掉整条连接——
            # 播放器收不到任何字节，表现就是点播"一直转圈卡死"（2026-10-15 实测 56 次）。
            # 修复：URL 类头值按 RFC 3986 百分号转义后再发。
            if isinstance(v, str) and any(ord(c) > 127 for c in v):
                v = urllib.parse.quote(v, safe=":/?#[]@!$&'()*+,;=%")
            self.send_header(k, v)
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # 客户端（如 curl -r 0-0 或 ffprobe）在收到响应头后主动断开连接属正常现象，静默忽略防刷屏
                pass

    def _json(self, code, obj, headers=None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   headers=headers)

    # ── 路由 ──────────────────────────────────────────
    def do_HEAD(self):
        """HEAD 请求快速响应（媒体探针防护），严禁触发云端转存任务。"""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/pending":
            q = urllib.parse.parse_qs(parsed.query)
            cat = (q.get("cat") or [""])[0]
            title = (q.get("t") or [""])[0]
            try:
                season = int((q.get("s") or ["0"])[0])
                episode = int((q.get("e") or ["0"])[0])
            except ValueError:
                season, episode = 0, 0

            row = db.find_done_transfer(cat, title, season, episode)
            if row and row["remote_path"]:
                url = _direct_url(row["remote_path"])
                url = cleaner.clean_url_if_obfuscated(url)
                return self._send(302, b"", ctype="text/plain", headers={"Location": url})
            # 未转存资源：仅返回轻量视频占位响应，不触发任何后台转存
            return self._send(200, b"", ctype="video/mp4", headers={"Accept-Ranges": "bytes"})

        if path == "/clean":
            q = urllib.parse.parse_qs(parsed.query)
            return cleaner.handle_clean_request(self, q, is_head=True)

        if path in ("/health", "/stats", "/capability"):
            return self._send(200, b"", ctype="application/json")

        return self._send(404, b"", ctype="text/plain")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)
        ua = self.headers.get('User-Agent', '')
        print(f"[{config.SERVICE_NAME}] [GET] {self.client_address[0]} {path}?{parsed.query} UA: {ua}", flush=True)

        if path == "/health":
            return self._json(200, {"status": "ok", "service": config.SERVICE_NAME})

        if path == "/stats":
            return self._json(200, db.stats())

        if path == "/capability":
            ol = get_openlist()
            write_ok, write_reason = (False, _ol_err or "未初始化")
            if ol:
                try:
                    write_ok, write_reason = ol.check_write_capable()
                except Exception as e:
                    write_reason = str(e)
            from .transfer import QuarkTransfer
            tr_probe = get_transfer() or QuarkTransfer(None)
            cookie_alive, cookie_status = tr_probe.check_cookie_alive()

            # 2026-10-20：CMS 探测已移除，仅报告夸克网盘链路健康度
            return self._json(200, {
                "cookie_ready": config.COOKIE_READY,
                "cookie_alive": cookie_alive,
                "cookie_status": cookie_status,
                "cookie_hint": config.redact_cookie(config.QUARK_COOKIE),
                "openlist_write": write_ok,
                "openlist_reason": write_reason,
                "panshare_ready": True,   # 已打通快映 4K 与 daily_hot
                "ready": cookie_alive and write_ok,
            })

        if path == "/preheat":
            return self._handle_preheat(q)

        if path == "/clean":
            return cleaner.handle_clean_request(self, q, is_head=False)

        if path == "/pending":
            return self._handle_pending(q)

        return self._json(404, {"error": f"unknown path: {path}"})

    # ── 核心：播放拦截 ────────────────────────────────
    def _handle_pending(self, q):
        cat = (q.get("cat") or [""])[0]
        raw_title = (q.get("t") or [""])[0]
        try:
            season = int((q.get("s") or ["0"])[0])
            episode = int((q.get("e") or ["0"])[0])
        except ValueError:
            return self._json(400, {"error": "invalid s/e"})

        if not cat or not raw_title:
            return self._json(400, {"error": "missing cat/t"})

        from .search import sanitize_title
        title = sanitize_title(raw_title)
        if not title:
            title = raw_title.strip()

        # 0. 无源熔断缓存（2026-10-20《爱你》转圈事故根治）
        #    当某片确认「夸克+CMS 全无源」时，写入 10 分钟负缓存。
        #    之后同一请求直接秒返，不再重复跑 20+ 秒的搜索链路
        #    ——Jellyfin 的 ffprobe 会连续探测多次，每次都跑全链路会把
        #    客户端卡在转圈 21 秒以上且服务端日志稀薄难以定位。
        miss_key = f"nomiss:{cat}:{title}:{season}:{episode}"
        now_ts = time.time()
        with _nomiss_lock:
            hit = _nomiss_cache.get(miss_key)
        if hit and now_ts < hit:
            return self._json(503, {
                "error": "播放源不可用",
                "reason": "该片暂无可播片源（10 分钟内已确认，负缓存命中）",
                "hint": "盘搜节点未检索到该片的夸克网盘资源，资源可能已下架",
                "cached": True,
                "cat": cat, "title": title, "s": season, "e": episode,
            })

        # 1. 已转存完成 → 校验文件真实存在 → 回写 strm 为 QuarkTV 直链，再重定向
        row = db.find_done_transfer(cat, title, season, episode)
        if not row and raw_title != title:
            row = db.find_done_transfer(cat, raw_title, season, episode)

        if row and row["remote_path"]:
            # 2026-10-16 新增【防线①】：数据库说 done ≠ 文件还在。
            # 用户可能手动删除了夸克网盘里的点播文件，此时 remote_path 已失效。
            # 原实现无条件 302 到死链 → 黑屏且永不恢复。
            # 现在先校验（夸克官方 API，无缓存）；不存在则作废记录并落到下面的
            # 「未转存」分支重新搜索/转存。
            if not _remote_still_exists(row["remote_path"]):
                try:
                    db._conn().execute(
                        "UPDATE transfers SET status='cleaned', remote_path=NULL, "
                        "error='[播放前校验] 网盘文件已不存在，已作废待重转', "
                        "updated_at=? WHERE id=?", (db.now(), row["id"]))
                    db._conn().commit()
                except Exception:
                    pass
                row = None          # 落空，走重转分支

        if row and row["remote_path"]:
            # direct_url() 只做 URL 拼接（config.OPENLIST_PUBLIC + quote(path)），
            # 不发起任何网络请求，因此**不应**因 OpenList 暂时连不上而失败。
            # 2026-10-16：原实现要求 get_openlist() 返回客户端才肯生成直链，
            # 导致 OpenList 重启窗口内的 503 让已转存影片也无法播放。
            # 直链本身由 OpenList 在被播放器访问时解析，此处无需前置连接。
            url = _direct_url(row["remote_path"])
            _wait_direct_url_ready(url, remote_path=row["remote_path"])
            url = cleaner.clean_url_if_obfuscated(url)

            # 2026-10-16 新增：把 strm 回写为【QuarkTV 直链】。
            # Why：原实现只在「首次转存成功」时回写 strm，已转存影片的 strm
            # 会永久停在 pending 占位。而 pending 指向 8099 拦截服务——
            # Jellyfin 扫描时探测到的是 8099 的 404/503 JSON 或 302，
            # 拿不到真实媒体信息，导致流元数据缺失/错误
            # （实测 cinema2 25 部影片中 24 部无视频流元数据、1 部误记为 720p/aac），
            # 进而无法正确判定 DirectPlay → 强制转码 → 叠加网盘读取后卡顿。
            # 回写为直链后，Jellyfin 探测到的是真实 MKV，元数据得以正确建立。
            try:
                strm_manager.activate(cat, title, season, episode, url)
            except Exception as e:
                # 2026-10-16：原为 `except: pass`，静默吞掉异常导致
                # "strm 明明该回写却没回写"极难排查（排查本次自愈时踩到）。
                # 回写失败仍不影响本次播放（已 302），但必须留下痕迹。
                print(f"[{config.SERVICE_NAME}] strm 回写失败 "
                      f"{cat}/{title} S{season}E{episode}: {e}", flush=True)

            db.record_play(cat, title, season, episode, transfer_id=row["id"])
            auto_preheat_next_episode(cat, title, season, episode)
            return self._send(302, b"", ctype="text/plain",
                              headers={"Location": url})

        # 2. 未转存：解析夸克网盘片源（2026-10-20 起只认夸克，CMS 降级已彻底移除）
        #    仅 PanShare 引擎：转存到自己网盘 → 302 到 OpenList 4K 直链。
        #    无夸克源时直接返回明确 503，不再降级到第三方低码率 m3u8 切片。
        try:
            from . import search as search_mod
            if not (config.COOKIE_READY and config.QUARK_FIRST):
                _mark_no_source(cat, title, season, episode)
                return self._json(503, {
                    "error": "点播服务未就绪",
                    "reason": "夸克 Cookie 未配置，无法检索网盘片源（CMS 降级已禁用）",
                    "hint": "登录 pan.quark.cn 取 Cookie 写入 .env 的 QUARK_COOKIE 后重启",
                    "cat": cat, "title": title, "s": season, "e": episode,
                })
            result, url = search_mod.resolve_for_play(
                cat, title, season, episode, prefer_panshare=True)
        except Exception as e:
            return self._json(503, {
                "error": "搜索片源失败",
                "reason": str(e)[:200],
                "cat": cat, "title": title, "s": season, "e": episode,
            })

        if result is None or result.engine != "panshare" or not result.share_url:
            _mark_no_source(cat, title, season, episode)
            # 诊断日志：明确记录夸克侧候选情况，排障不再靠猜
            try:
                pan_all = search_mod.search_panshare(title)
                pan_ok = [p for p in pan_all
                          if search_mod.score_search_result(p, title, season=season, episode=episode) >= 30.0]
                print(f"[{config.SERVICE_NAME}] [无源诊断] {cat}/{title} S{season}E{episode} "
                      f"夸克原始候选 {len(pan_all)} / 合格 {len(pan_ok)} —— 无可用夸克网盘源", flush=True)
            except Exception as diag_ex:
                print(f"[{config.SERVICE_NAME}] [无源诊断] 异常: {diag_ex}", flush=True)
            return self._json(503, {
                "error": "无可用夸克网盘源",
                "reason": "所有盘搜节点均未检索到该片的夸克网盘分享资源",
                "hint": "本系统已禁用 CMS 低码率降级，仅提供夸克 4K 片源",
                "cat": cat, "title": title, "s": season, "e": episode,
            })

        # 2b. 网盘分享：需要 cookie + 写能力，走转存
        if result.engine == "panshare" and result.share_url:
            tr = get_transfer()
            if tr is None:
                return self._json(503, {
                    "error": "OpenList 暂不可用",
                    "reason": _ol_err or "客户端初始化失败",
                    "hint": "OpenList 恢复后重试即可；已转存影片不受影响",
                    "cat": cat, "title": title, "s": season, "e": episode,
                })
            if not tr.is_ready():
                return self._json(503, {
                    "error": "点播服务未就绪",
                    "reason": "QUARK_COOKIE 未配置",
                    "hint": "登录 pan.quark.cn 取 Cookie 写入 .env 的 QUARK_COOKIE，"
                            "然后重启 cinema-quark",
                    "cat": cat, "title": title, "s": season, "e": episode,
                })
            # 并发防重互斥：同一部影片只允许一个线程执行真实转存，其余并发请求等待结果
            key = (_clean_title_key(title), season, episode)
            with _in_flight_lock:
                event = _in_flight_transfers.get(key)
                if event is None:
                    event = threading.Event()
                    _in_flight_transfers[key] = event
                    is_leader = True
                else:
                    is_leader = False

            if not is_leader:
                # 伴随线程：等待主线程转存完成（最长等待 TRANSFER_TIMEOUT 秒）
                event.wait(timeout=config.TRANSFER_TIMEOUT)
                row = db.find_done_transfer(cat, title, season, episode)
                if row and row["remote_path"]:
                    direct_url = _direct_url(row["remote_path"])
                    db.record_play(cat, title, season, episode, transfer_id=row["id"])
                    return self._send(302, b"", ctype="text/plain",
                                      headers={"Location": direct_url})

            tid, video_remote_path = None, None
            transfer_err = None
            try:
                if is_leader:
                    # 优先调取经预检审计确认存活且为真 4K 的最高品质锁存源
                    share_candidates = []
                    locked_url, is_4k, l_title = db.get_locked_4k_share(cat, title, season, episode)
                    if locked_url:
                        share_candidates.append(search_mod.SearchResult(
                            engine="panshare", source="precheck_locked_4k" if is_4k else "precheck_locked",
                            name=l_title or title, year="", poster="", episodes=[],
                            share_url=locked_url, pan_type="quark"
                        ))
                    if result and result.share_url not in [c.share_url for c in share_candidates]:
                        share_candidates.append(result)

                    try:
                        for pr in search_mod.search(title, prefer_panshare=True):
                            if pr.engine == "panshare" and pr.share_url and pr.share_url not in [c.share_url for c in share_candidates]:
                                sc = search_mod.score_search_result(pr, title, season=season, episode=episode)
                                if sc >= 30.0:
                                    share_candidates.append(pr)
                    except Exception:
                        pass

                    # 智能优先级排序（2026-10-20 画质择优）：
                    #   1) 集数区间不含目标集的分享包后置（[73-124集] 之类）
                    #   2) 明确标注 4K/臻彩/UHD/2160/HDR 的分享包前置
                    #   3) 其余按相关度得分降序
                    # 旧实现只按「集数区间」排序，导致先搜到的 1080P 源被优先转存，
                    # 网盘里明明还有 4K 臻彩源却永远轮不到（唐宫奇案 E01 实测：
                    # 1080P 奇案TANGGONG vs 4K 唐工契安·凤鸣 2495MB）。
                    if season > 0 and episode > 0:
                        def _cand_priority(c):
                            cn = c.name or ""
                            ep_penalty = 0
                            m = re.search(r"[\[\(]?\s*(\d+)\s*[-~至到—_]\s*(\d+)\s*集?[\]\)]?", cn)
                            if m:
                                try:
                                    s_ep, e_ep = int(m.group(1)), int(m.group(2))
                                    if s_ep < e_ep and (episode < s_ep or episode > e_ep):
                                        ep_penalty = 1
                                except ValueError:
                                    pass
                            is_4k = 1 if re.search(r"4K|臻彩|2160|UHD|HDR", cn, re.I) else 0
                            try:
                                sc = search_mod.score_search_result(
                                    c, title, season=season, episode=episode)
                            except Exception:
                                sc = 0
                            return (ep_penalty, -is_4k, -sc)
                        share_candidates.sort(key=_cand_priority)
                    else:
                        share_candidates.sort(
                            key=lambda c: -1 if re.search(r"4K|臻彩|2160|UHD|HDR", c.name or "", re.I) else 0)

                    # ── 转存前置验活（2026-10-20 全库实测新增）──
                    # 实测 203 部国产有源作品中，168 部失败里 85 部（51%）是分享源已死
                    # （41012 好友取消 / 41011 地址失效 / 41031 分享者被封 / 41010 文件违规）。
                    # 旧实现是「先转存再发现失败」，每次都白耗一次 save+task 轮询（3~8s）
                    # 且要等到转存流程走完才知道。resolve_share 只做 1 次 token + 1 次 detail
                    # 请求，Failed 会立即抛出，可提前识别死链并直接拉黑。
                    live_candidates = []
                    for cand in share_candidates:
                        try:
                            tr.resolve_share(cand.share_url)
                            live_candidates.append(cand)
                            continue
                        except Exception as ex:
                            err_str = str(ex)
                            if any(k in err_str for k in DEAD_SHARE_MARKERS):
                                db.mark_dead_source(cand.share_url, "panshare",
                                                    f"[前置验活] {err_str[:120]}")
                                print(f"[{config.SERVICE_NAME}] [前置验活] 死链已跳过并拉黑: "
                                      f"{cand.share_url}", flush=True)
                                continue
                            # 非死链类异常（网络抖动等）保留候选，交回原流程尝试
                            live_candidates.append(cand)
                    share_candidates = live_candidates

                    for cand in share_candidates:
                        try:
                            tid, video_remote_path = tr.transfer(
                                cat, title, season, episode,
                                cand.share_url, cand.pan_type or "quark")
                            if video_remote_path:
                                break
                        except Exception as ex:
                            transfer_err = ex
                            # 触发死链熔断：若提示好友已取消分享或分享失效，当场拉黑 7 天
                            err_str = str(ex)
                            if any(k in err_str for k in DEAD_SHARE_MARKERS):
                                db.mark_dead_source(cand.share_url, "panshare", err_str)
                                print(f"[{config.SERVICE_NAME}] [熔断拉黑] 失效网盘分享源已拉黑: {cand.share_url}", flush=True)
                            print(f"[{config.SERVICE_NAME}] 网盘源 {cand.share_url} 转存/匹配失败: {ex}，尝试下一个备用源...", flush=True)

                    if not video_remote_path and transfer_err:
                        raise transfer_err
            except Exception as e:
                # 2026-10-20：CMS 降级已彻底移除。转存失败即失败，明确告知无夸克源。
                print(f"[{config.SERVICE_NAME}] [无源诊断] {cat}/{title} S{season}E{episode} "
                      f"夸克网盘转存失败（CMS 降级已禁用）：{str(e)[:200]}", flush=True)
                _mark_no_source(cat, title, season, episode)
                return self._json(503, {
                    "error": "网盘转存失败",
                    "reason": f"夸克网盘转存失败：{str(e)[:200]}",
                    "hint": "本系统已禁用 CMS 低码率降级，仅提供夸克 4K 片源",
                    "cat": cat, "title": title, "s": season, "e": episode,
                })
            finally:
                if is_leader:
                    with _in_flight_lock:
                        ev = _in_flight_transfers.pop(key, None)
                        if ev:
                            ev.set()

            # 转存成功后回写 strm 并重定向
            if video_remote_path:
                direct_url = _direct_url(video_remote_path)
                _wait_direct_url_ready(direct_url, remote_path=video_remote_path)
                direct_url = cleaner.clean_url_if_obfuscated(direct_url)
                try:
                    strm_manager.activate(cat, title, season, episode, direct_url)
                except Exception as e:
                    print(f"[{config.SERVICE_NAME}] strm 回写失败（首次转存后）"
                          f" {cat}/{title} S{season}E{episode}: {e}", flush=True)
                db.record_play(cat, title, season, episode, transfer_id=tid)
                auto_preheat_next_episode(cat, title, season, episode)
                return self._send(302, b"", ctype="text/plain",
                                  headers={"Location": direct_url})

        # 有结果但拿不到可用地址
        return self._json(503, {
            "error": "片源不可用",
            "reason": f"找到《{result.name}》但无 S{season:02d}E{episode:02d} 的地址"
                      if season else f"找到《{result.name}》但无播放地址",
            "matched": result.to_dict(),
            "cat": cat, "title": title, "s": season, "e": episode,
        })



    # ── 预热接口：提前触发后台转存 ───────────────────────
    def _handle_preheat(self, q):
        cat = (q.get("cat") or [""])[0]
        title = (q.get("t") or [""])[0]
        try:
            season = int((q.get("s") or ["0"])[0])
            episode = int((q.get("e") or ["0"])[0])
        except ValueError:
            return self._json(400, {"error": "invalid s/e"})
        if not title:
            return self._json(400, {"error": "missing t"})

        ok, status = trigger_preheat_task(cat, title, season, episode)
        if status == "already_ready":
            row = db.find_done_transfer(cat, title, season, episode)
            return self._json(200, {
                "status": "ready", "title": title, "s": season, "e": episode,
                "msg": "片源已就绪，可随时秒播",
                "url": _direct_url(row["remote_path"]) if row else ""
            })
        elif status == "in_progress":
            return self._json(200, {
                "status": "in_progress", "title": title, "s": season, "e": episode,
                "msg": "正在后台转存中，请稍候"
            })
        else:
            return self._json(200, {
                "status": "started", "title": title, "s": season, "e": episode,
                "msg": f"《{title}》已提交后台转存，完成后即可直接秒播"
            })


class ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def _cleanup_stale_transfers(max_age=3600):
    """启动时清理滞留超时的 searching/saving 半途记录（铁律十三补完，2026-10-25）。

    transfer._do_transfer 的 finally 已兜住进程内异常，但进程被 kill -9、
    宿主机断电等场景不会有 finally 执行，必须由启动期兜底。
    """
    try:
        con = db._conn()
        cutoff = int(time.time()) - max_age
        rows = con.execute(
            "SELECT id, cat, title, season, episode, status FROM transfers "
            "WHERE status IN ('searching','saving') AND updated_at < ?", (cutoff,)).fetchall()
        for r in rows:
            con.execute("UPDATE transfers SET status='failed', error=?, updated_at=? WHERE id=?",
                        ("[启动自愈] 转存中断，未完成", int(time.time()), r["id"]))
            print(f"[{config.SERVICE_NAME}] [启动自愈] {r['cat']}/{r['title']} "
                  f"S{r['season']}E{r['episode']}: {r['status']} -> failed", flush=True)
        con.commit()
        if rows:
            print(f"[{config.SERVICE_NAME}] [启动自愈] 清理 {len(rows)} 条半途记录", flush=True)
    except Exception as e:
        print(f"[{config.SERVICE_NAME}] [启动自愈] 异常: {e}", flush=True)


def main():
    config.ensure_dirs()
    _cleanup_stale_transfers()
    def _bg_scan():
        n = strm_manager.scan_and_register()
        print(f"[{config.SERVICE_NAME}] strm 登记: {n} 个", flush=True)
    threading.Thread(target=_bg_scan, daemon=True).start()

    # 2026-10-16 新增【防线②】：后台自愈线程。
    # Why：strm 变 live 后 Jellyfin 直接请求 OpenList，完全绕过本服务，
    # 故"用户手动删除网盘文件"只能靠后台定时扫描发现并回退 pending。
    if config.SELFHEAL_ENABLED:
        from . import selfheal
        threading.Thread(target=selfheal.heal_loop, daemon=True,
                         name="selfheal").start()
        print(f"[{config.SERVICE_NAME}] 自愈线程已启动"
              f"（间隔 {config.SELFHEAL_INTERVAL}s，首次延迟 "
              f"{config.SELFHEAL_INITIAL_DELAY}s）", flush=True)

    ol = get_openlist()
    if ol:
        ok, reason = ol.check_write_capable()
        print(f"[{config.SERVICE_NAME}] OpenList 写能力: {'就绪' if ok else '未就绪'} ({reason})", flush=True)
    else:
        print(f"[{config.SERVICE_NAME}] OpenList 初始化失败: {_ol_err}", flush=True)
    print(f"[{config.SERVICE_NAME}] Cookie: {config.redact_cookie(config.QUARK_COOKIE)}", flush=True)

    srv = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    print(f"[{config.SERVICE_NAME}] 监听 {config.HOST}:{config.PORT}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
