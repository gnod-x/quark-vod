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

import re
import threading
import time
import urllib.parse
import urllib.request
from . import config

# 伪装特征常量
PNG_MAGIC_HEX = "89504e470d0a1a0a"
MKV_MAGIC_HEX = "1a45dfa3"
MP4_FTYP_HEX = "66747970"


def detect_stream_obfuscation(direct_url, timeout=2.5):
    """检测直链是否被注入了文件头伪装（图种混淆）。

    发送轻量 Range: bytes=0-63 请求（仅 64 字节）：
      - 若前 8 字节为 PNG 魔数，且后续包含 MKV 或 MP4 签名：
        返回 (True, 8, "matroska" 或 "mp4")
      - 纯净流：
        返回 (False, 0, "")
    """
    if not direct_url or not direct_url.startswith("http"):
        return False, 0, ""

    try:
        req = urllib.request.Request(direct_url)
        req.add_header("Range", "bytes=0-63")
        req.add_header("User-Agent", "curl/8.14.1")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(64)
            if not data or len(data) < 16:
                return False, 0, ""
            hex_data = data.hex()

            # 1. 8 字节 PNG 伪装
            if hex_data.startswith(PNG_MAGIC_HEX):
                if hex_data[16:24] == MKV_MAGIC_HEX:
                    return True, 8, "matroska"
                if MP4_FTYP_HEX in hex_data[16:]:
                    return True, 8, "mp4"
                return True, 8, "matroska"

            # 2. 纯净 MKV
            if hex_data.startswith(MKV_MAGIC_HEX):
                return False, 0, "matroska"

            # 3. 纯净 MP4 (offset 4 为 ftyp)
            if len(hex_data) >= 16 and hex_data[8:16] == MP4_FTYP_HEX:
                return False, 0, "mp4"

    except Exception:
        pass

    return False, 0, ""


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
        print(f"[{config.SERVICE_NAME}] [自动流清洗] 检测到 {skip} 字节混淆头({fmt})，激活纯净路由: {direct_url} -> {clean_url}", flush=True)
        return clean_url
    return direct_url


def handle_clean_request(handler, q, is_head=False):
    """8099 /clean 请求处理（零拷贝 Range 字节平移透传）。"""
    raw_url = (q.get("url") or [""])[0]
    if not raw_url:
        return handler._json(400, {"error": "missing url"})

    try:
        skip = int((q.get("skip") or ["8"])[0])
    except ValueError:
        skip = 8

    range_hdr = handler.headers.get("Range", "")
    req = urllib.request.Request(raw_url)

    # 透传客户端主要标头
    for h in ("User-Agent", "Accept", "Accept-Encoding"):
        val = handler.headers.get(h)
        if val:
            req.add_header(h, val)

    # 核心算法：对客户端 Range 请求加上 skip 偏移
    if range_hdr.startswith("bytes="):
        r_val = range_hdr[6:].strip()
        parts = r_val.split("-")
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if len(parts) > 1 and parts[1] else ""
        t_start = start + skip
        t_end = (end + skip) if end != "" else ""
        req.add_header("Range", f"bytes={t_start}-{t_end}")
    else:
        req.add_header("Range", f"bytes={skip}-")

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            code = resp.status
            if code >= 400:
                # 2026-10-25：上游返回错误码（OpenList 文件已删等）时，
                # 旧实现会原样把 500 透传给播放器，strm 永远卡在 clean 死链。
                # 此处主动触发一次异步自愈回退，让下次播放走 pending 重新转存。
                _trigger_selfheal_for(raw_url)
                raise RuntimeError(f"upstream HTTP {code}")
            cl = resp.headers.get("Content-Length")
            cr = resp.headers.get("Content-Range")
            cd = resp.headers.get("Content-Disposition")
            ct = resp.headers.get("Content-Type", "video/x-matroska")

            handler.send_response(code)
            handler.send_header("Content-Type", ct)
            handler.send_header("Accept-Ranges", "bytes")
            if cd:
                handler.send_header("Content-Disposition", cd)
            if cl:
                handler.send_header("Content-Length", cl)
            if cr:
                # bytes 8-1219326423/1219326424 -> bytes 0-1219326415/1219326416
                m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+)", cr)
                if m:
                    s_o, e_o, t_o = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    handler.send_header("Content-Range", f"bytes {s_o - skip}-{e_o - skip}/{t_o - skip}")
            handler.end_headers()

            if not is_head:
                while True:
                    buf = resp.read(128 * 1024)
                    if not buf:
                        break
                    handler.wfile.write(buf)
    except (BrokenPipeError, ConnectionResetError):
        # 客户端暂停或退出播放，正常断开
        pass
    except Exception as e:
        if not is_head:
            try:
                handler._json(502, {"error": "clean proxy stream error", "detail": str(e)[:150]})
            except Exception:
                pass


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
                print(f"[{config.SERVICE_NAME}] [clean自愈] 回退失败 {remote}: {ex}", flush=True)

        threading.Thread(target=_bg, daemon=True, name="clean-heal").start()
    except Exception:
        pass
