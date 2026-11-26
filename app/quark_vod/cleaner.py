#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
流清洗与伪装文件头自适应剥离模块 (Zero-Copy Stream Cleaner)

【背景与痛点】
部分国内网盘分享源为绕过平台版权或违规 Hash 审查，故意在标准 MKV/MP4 容器前
插入伪装字节（最典型的是 8 字节 PNG 魔数 \x89PNG\r\n\x1a\n 或 4 字节 BMP 魔数）。
这会导致：
  1. FFmpeg/FFprobe 解复用器误判为 png_pipe，陷入死循环读取，消耗 600MB+ 内存并 120s 超时；
  2. 电视端 ExoPlayer / 原生播放器因无法识别文件签名抛出 UnrecognizedInputFormatException；
  3. 客户端等待 PlaybackInfo 超时，界面陷入永无止境的转圈卡死。

【解决方案】
本模块提供双轨清洗能力：
  1. 快速嗅探：在转存自检或回写阶段单次读取前 64 字节，识别是否存在伪装前导并计算偏移量。
  2. 透明平移代理：在 8099 提供 /clean 端点，对请求的 HTTP Range 头部做透明平移映射
     （如 client 请求 0-1000 -> 向 OpenList 请求 8-1008），以零拷贝流式透传给客户端。
     客户端读取到的第 0 字节即为纯净的标准容器头，实现 100% DirectPlay 直播秒开。
  3. 纯净流零开销：对未混淆的绝大多数正常资源直接放行原生直链，0 性能损失。
"""

from collections import OrderedDict
import re
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass
from . import config, download_http

# 伪装特征常量
PNG_MAGIC_HEX = "89504e470d0a1a0a"
MKV_MAGIC_HEX = "1a45dfa3"
MP4_FTYP_HEX = "66747970"


class CleanStreamError(Exception):
    def __init__(self, status, message, headers=None):
        super().__init__(message)
        self.status = status
        self.headers = headers or {}


def parse_range_header(value):
    """Only single byte ranges are supported. Ignore malformed/multipart ranges."""
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", (value or "").strip(), re.I)
    if not match:
        return None
    left, right = match.groups()
    if not left:
        return (None, int(right)) if right and int(right) > 0 else None
    start, end = int(left), int(right) if right else None
    return (start, end) if end is None or end >= start else None


@dataclass
class CleanStream:
    response: object
    status: int
    headers: dict
    discard: int
    length: object

    def close(self):
        self.response.close()

    def chunks(self):
        discard, remaining = self.discard, self.length
        try:
            while discard:
                part = self.response.read(min(discard, 128 * 1024))
                if not part:
                    raise OSError("upstream ended before clean stream start")
                discard -= len(part)
            while remaining is None or remaining > 0:
                part = self.response.read(128 * 1024 if remaining is None else min(remaining, 128 * 1024))
                if not part:
                    if remaining:
                        raise OSError("upstream ended before clean stream end")
                    break
                yield part
                if remaining is not None:
                    remaining -= len(part)
        finally:
            self.close()


def open_clean_stream(url, skip, range_header="", request_headers=None, *, opener=None):
    """Open and validate a prefixed stream; body reads are bounded and lazy."""
    if not isinstance(skip, int) or skip < 0:
        raise CleanStreamError(400, "invalid skip")
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise CleanStreamError(400, "HTTP URL required")
    selected = parse_range_header(range_header)
    headers = {"Accept-Encoding": "identity"}
    for key in ("User-Agent", "Accept"):
        value = (request_headers or {}).get(key.lower()) or (request_headers or {}).get(key)
        if value:
            headers[key] = value
    if selected is None:
        translated = f"bytes={skip}-"
    elif selected[0] is None:
        translated = f"bytes=-{selected[1]}"
    else:
        start, end = selected
        translated = f"bytes={start + skip}-{end + skip if end is not None else ''}"
    headers["Range"] = translated
    try:
        response = (opener or download_http.urlopen)(urllib.request.Request(url, headers=headers), timeout=20)
    except urllib.error.HTTPError as exc:
        try:
            if exc.code == 416:
                match = re.fullmatch(r"bytes \*/(\d+)", exc.headers.get("Content-Range", ""))
                out = {"Content-Range": f"bytes */{max(0, int(match[1]) - skip)}"} if match else {}
                raise CleanStreamError(416, "range not satisfiable", out) from None
            if exc.code in (404, 410) or exc.code >= 500:
                _trigger_selfheal_for(url)
            raise CleanStreamError(502, f"upstream HTTP {exc.code}") from None
        finally:
            exc.close()
    try:
        if response.status not in (200, 206):
            raise CleanStreamError(502, "unexpected upstream status")
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise CleanStreamError(502, "encoded upstream cannot be byte translated")
        actual_start = 0
        actual_end = total = None
        if response.status == 206:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
            if not match:
                raise CleanStreamError(502, "invalid upstream Content-Range")
            actual_start, actual_end, total = map(int, match.groups())
            if not (0 <= actual_start <= actual_end < total):
                raise CleanStreamError(502, "invalid upstream byte bounds")
        elif response.headers.get("Content-Length") is not None:
            total = int(response.headers["Content-Length"])
            actual_end = total - 1
        clean_total = max(0, total - skip) if total is not None else None
        # If a server ignores Range without declaring a size, a full 200 is valid.
        if total is None:
            selected = None
        if selected is None:
            start, end = 0, clean_total - 1 if clean_total is not None else None
        elif selected[0] is None:
            start, end = max(0, clean_total - selected[1]), clean_total - 1
        else:
            start = selected[0]
            end = min(selected[1], clean_total - 1) if selected[1] is not None else clean_total - 1
        if selected is not None and (clean_total == 0 or start >= clean_total):
            raise CleanStreamError(416, "range not satisfiable", {"Content-Range": f"bytes */{clean_total}"})
        raw_start = skip + start
        if actual_start > raw_start or (end is not None and actual_end < skip + end):
            raise CleanStreamError(502, "upstream returned a different byte range")
        length = max(0, end - start + 1) if end is not None else None
        out = {"Accept-Ranges": "bytes", "Content-Type": response.headers.get("Content-Type", "video/x-matroska")}
        if response.headers.get("Content-Disposition"):
            out["Content-Disposition"] = response.headers["Content-Disposition"]
        if length is not None:
            out["Content-Length"] = str(length)
        if selected is not None:
            out["Content-Range"] = f"bytes {start}-{end}/{clean_total}"
        return CleanStream(response, 206 if selected is not None else 200, out, raw_start - actual_start, length)
    except Exception:
        response.close()
        raise


_probe_cache = OrderedDict()
_probe_lock = threading.Lock()

def remember_probe(direct_url, data):
    with _probe_lock:
        _probe_cache[direct_url]=(time.monotonic()+300, bytes(data))
        _probe_cache.move_to_end(direct_url)
        while len(_probe_cache)>64:_probe_cache.popitem(last=False)

def detect_stream_obfuscation(direct_url, timeout=2.5):
    """Reuse the readiness probe; only remove a proven video prefix."""
    if not direct_url or not direct_url.startswith("http"):
        return False, 0, ""
    with _probe_lock:
        cached=_probe_cache.get(direct_url)
    data=cached[1] if cached and cached[0]>time.monotonic() else None
    if data is None:
        try:
            req=urllib.request.Request(direct_url,headers={"Range":"bytes=0-63","User-Agent":"curl/8.14.1"})
            with download_http.urlopen(req,timeout=timeout) as response:
                data=response.read(64)
            remember_probe(direct_url,data)
        except Exception:return False,0,""
    if not data or len(data)<16:return False,0,""
    if data.startswith(bytes.fromhex(PNG_MAGIC_HEX)):
        if data[8:12]==bytes.fromhex(MKV_MAGIC_HEX):return True,8,"matroska"
        if data[12:16]==b"ftyp":return True,8,"mp4"
        return False,0,""
    if data[:4]==bytes.fromhex(MKV_MAGIC_HEX):return False,0,"matroska"
    if data[4:8]==b"ftyp":return False,0,"mp4"
    return False,0,""


def clean_url_if_obfuscated(direct_url):
    """若直链存在混淆，转换为 8099 透明流清洗 URL；否则原样返回直链。"""
    if not direct_url or not direct_url.startswith("http"):
        return direct_url
    if "/clean?" in direct_url or "/pending?" in direct_url:
        return direct_url

    is_obf, skip, fmt = detect_stream_obfuscation(direct_url)
    if is_obf and skip > 0:
        enc_url = urllib.parse.quote(direct_url)
        clean_url = f"{config.PENDING_BASE_URL}/clean?url={enc_url}&skip={skip}"
        # Direct URLs may carry short-lived signatures. Keep them out of logs;
        # the returned URL still contains the upstream address for the player.
        print(f"[{config.SERVICE_NAME}] [自动流清洗] 检测到 {skip} 字节混淆头({fmt})，已激活纯净路由（URL 已省略）", flush=True)
        return clean_url
    return direct_url


def handle_clean_request(handler, q, is_head=False):
    """兼容旧 HTTPServer，复用 FastAPI 相同的区间转换。"""
    raw_url = (q.get("url") or [""])[0]
    stream = None
    sent = False
    try:
        try:
            skip = int((q.get("skip") or ["8"])[0])
        except ValueError:
            return handler._json(400, {"error": "invalid skip"})
        stream = open_clean_stream(raw_url, skip, handler.headers.get("Range", ""), handler.headers)
        handler.send_response(stream.status)
        for key, value in stream.headers.items():
            handler.send_header(key, value)
        handler.end_headers()
        sent = True
        if not is_head:
            for chunk in stream.chunks():
                handler.wfile.write(chunk)
    except CleanStreamError as exc:
        if not sent:
            handler._json(exc.status, {"error": str(exc)}, headers=exc.headers)
    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception as exc:
        if not sent:
            handler._json(502, {"error": "clean proxy stream error", "type": type(exc).__name__})
    finally:
        if stream:
            stream.close()


# ── 上游失效主动自愈（2026-10-25）────────────────────────────
# 节流：同一 remote_path 10 分钟内只触发一次，避免播放器反复重连风暴
_heal_lock = threading.Lock()
_heal_recent = {}


def _trigger_selfheal_for(raw_url, ttl=600):
    """上游直链失效时，异步把对应 strm 回退为 pending 并作废 DB 记录。

    为什么要做：strm 指向 :8099/clean 时完全绕过 /pending 的播放前校验，
    一旦网盘文件被清理，客户端只拿到 502，没有任何路径能恢复。
    """
    try:
        from . import strm_manager
        remote = strm_manager.parse_live_remote_path(raw_url)
        if not remote:
            return
        now = time.time()
        with _heal_lock:
            if _heal_recent.get(remote, 0) > now - ttl:
                return
            _heal_recent[remote] = now
            if len(_heal_recent) > 500:
                for k in [k for k, v in _heal_recent.items() if v < now - ttl]:
                    _heal_recent.pop(k, None)

        def _bg():
            try:
                from . import selfheal
                selfheal.heal_remote(remote)
            except Exception as ex:
                print(f"[{config.SERVICE_NAME}] [clean自愈] 回退失败: {type(ex).__name__}", flush=True)
            finally:
                from . import db
                db.close()

        threading.Thread(target=_bg, daemon=True, name="clean-heal").start()
    except Exception:
        pass
