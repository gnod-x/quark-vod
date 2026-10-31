#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Quark-VOD Engine (FastAPI + Whitebox Admin Console)
===================================================
提供 100% 兼容原有 cinema-quark (8099) 的流媒体点播拦截接口，
同时新增 /admin 白盒可视化管理台与 /docs (Swagger UI) 调试接口。
"""

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

from quark_vod import cleaner, cleanup, config, db, search as search_mod, selfheal, server as legacy_srv, strm_manager
from quark_vod.transfer import QuarkTransfer


class DummyHandler:
    """适配器：将旧版 Handler._handle_pending / _handle_preheat 的返回无损桥接为 FastAPI Response。"""

    def __init__(self, headers: dict):
        self.headers = headers
        self.status_code = 200
        self.body = b""
        self.ctype = "application/json"
        self.resp_headers = {}

    def _send(self, code, body=b"", ctype="application/json", headers=None):
        self.status_code = code
        self.body = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.ctype = ctype
        self.resp_headers = {}
        for k, v in (headers or {}).items():
            if isinstance(v, str) and any(ord(c) > 127 for c in v):
                v = urllib.parse.quote(v, safe=":/?#[]@!$&'()*+,;=%")
            self.resp_headers[k] = v
        return self.to_fastapi_response()

    def _json(self, code, obj, headers=None):
        return self._send(
            code,
            json.dumps(obj, ensure_ascii=False).encode("utf-8"),
            ctype="application/json",
            headers=headers,
        )

    def to_fastapi_response(self) -> Response:
        return Response(
            content=self.body,
            status_code=self.status_code,
            media_type=self.ctype,
            headers=self.resp_headers,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务启动期自愈与后台守护线程初始化（对齐原 server.main()）。"""
    config.ensure_dirs()
    legacy_srv._cleanup_stale_transfers()

    def _bg_scan():
        try:
            n = strm_manager.scan_and_register()
            print(f"[{config.SERVICE_NAME}] strm 登记完成: {n} 个", flush=True)
        except Exception as e:
            print(f"[{config.SERVICE_NAME}] strm 登记异常: {e}", flush=True)

    threading.Thread(target=_bg_scan, daemon=True, name="strm-scan").start()

    if config.SELFHEAL_ENABLED:
        threading.Thread(target=selfheal.heal_loop, daemon=True, name="selfheal").start()
        print(
            f"[{config.SERVICE_NAME}] 自愈线程已启动（间隔 {config.SELFHEAL_INTERVAL}s，"
            f"首次延迟 {config.SELFHEAL_INITIAL_DELAY}s）",
            flush=True,
        )

    ol = legacy_srv.get_openlist()
    if ol:
        try:
            ok, reason = ol.check_write_capable()
            print(f"[{config.SERVICE_NAME}] OpenList 写能力: {'就绪' if ok else '未就绪'} ({reason})", flush=True)
        except Exception as e:
            print(f"[{config.SERVICE_NAME}] OpenList 写能力检查异常: {e}", flush=True)
    else:
        print(f"[{config.SERVICE_NAME}] OpenList 暂未就绪: {legacy_srv._ol_err}", flush=True)

    print(f"[{config.SERVICE_NAME}] Cookie: {config.redact_cookie(config.get_quark_cookie())}", flush=True)
    yield


app = FastAPI(
    title="Quark-VOD Engine (Jellyfin2 伴生点播引擎)",
    description="家庭影院双轨流媒体点播拦截、五维身份隔离搜索、夸克读写分离转存与三道防线自愈引擎",
    version="2.0.0",
    lifespan=lifespan,
)


# ==============================================================================
# 1. 核心流媒体兼容接口（100% 兼容原有客户端、custom-ffmpeg 探针与验证脚本）
# ==============================================================================


@app.get("/health", tags=["Core VOD"])
@app.head("/health", tags=["Core VOD"])
def health(request: Request):
    if request.method == "HEAD":
        return Response(status_code=200, media_type="application/json")
    return {"status": "ok", "service": config.SERVICE_NAME, "engine": "fastapi-v2"}


@app.get("/stats", tags=["Core VOD"])
@app.head("/stats", tags=["Core VOD"])
def stats(request: Request):
    if request.method == "HEAD":
        return Response(status_code=200, media_type="application/json")
    return db.stats()


@app.get("/capability", tags=["Core VOD"])
@app.head("/capability", tags=["Core VOD"])
def capability(request: Request):
    if request.method == "HEAD":
        return Response(status_code=200, media_type="application/json")
    ol = legacy_srv.get_openlist()
    write_ok, write_reason = (False, legacy_srv._ol_err or "未初始化")
    if ol:
        try:
            write_ok, write_reason = ol.check_write_capable()
        except Exception as e:
            write_reason = str(e)
    tr_probe = legacy_srv.get_transfer() or QuarkTransfer(None)
    cookie_alive, cookie_status = tr_probe.check_cookie_alive()
    cur_cookie = config.get_quark_cookie()
    return {
        "cookie_ready": bool(cur_cookie),
        "cookie_alive": cookie_alive,
        "cookie_status": cookie_status,
        "cookie_hint": config.redact_cookie(cur_cookie),
        "openlist_write": write_ok,
        "openlist_reason": write_reason,
        "panshare_ready": True,
        "ready": bool(cookie_alive and write_ok),
    }


@app.api_route("/pending", methods=["GET", "HEAD"], tags=["Core VOD"])
def pending(
    request: Request,
    cat: str = Query("", description="媒体库分类，如 国产电影 / 国产剧集"),
    t: str = Query("", description="影片标题"),
    s: int = Query(0, description="季数 (电影为 0)"),
    e: int = Query(0, description="集数 (电影为 0)"),
):
    ua = request.headers.get("user-agent", "")
    client_ip = request.client.host if request.client else "unknown"
    print(f"[{config.SERVICE_NAME}] [{request.method}] {client_ip} /pending?{request.url.query} UA: {ua}", flush=True)

    if request.method == "HEAD":
        row = db.find_done_transfer(cat, t, s, e)
        if row and row["remote_path"]:
            url = legacy_srv._direct_url(row["remote_path"])
            url = cleaner.clean_url_if_obfuscated(url)
            h = DummyHandler(dict(request.headers))
            return h._send(302, b"", ctype="text/plain", headers={"Location": url})
        return Response(content=b"", status_code=200, media_type="video/mp4", headers={"Accept-Ranges": "bytes"})

    q = {"cat": [cat], "t": [t], "s": [str(s)], "e": [str(e)]}
    dummy = DummyHandler(dict(request.headers))
    return legacy_srv.Handler._handle_pending(dummy, q)


@app.get("/preheat", tags=["Core VOD"])
def preheat(
    cat: str = Query("", description="媒体库分类"),
    t: str = Query("", description="影片标题"),
    s: int = Query(0, description="季数"),
    e: int = Query(0, description="集数"),
):
    q = {"cat": [cat], "t": [t], "s": [str(s)], "e": [str(e)]}
    dummy = DummyHandler({})
    return legacy_srv.Handler._handle_preheat(dummy, q)


@app.api_route("/clean", methods=["GET", "HEAD"], tags=["Core VOD"])
def clean_stream(
    request: Request,
    url: str = Query(..., description="原始 OpenList 直链 URL"),
    skip: int = Query(8, description="需剥离的前导混淆字节数（默认 8 字节 PNG 头）"),
):
    """零拷贝 Range 字节平移流式透传（解决 8 字节 PNG 图种伪装头）。"""
    if not url:
        return JSONResponse(status_code=400, content={"error": "missing url"})

    range_hdr = request.headers.get("range", "")
    req = urllib.request.Request(url)
    for h in ("User-Agent", "Accept", "Accept-Encoding"):
        val = request.headers.get(h.lower())
        if val:
            req.add_header(h, val)

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
        resp = urllib.request.urlopen(req, timeout=20)
        code = resp.status
        if code >= 400:
            resp.close()
            cleaner._trigger_selfheal_for(url)
            return JSONResponse(status_code=502, content={"error": f"upstream HTTP {code}"})

        cl = resp.headers.get("Content-Length")
        cr = resp.headers.get("Content-Range")
        cd = resp.headers.get("Content-Disposition")
        ct = resp.headers.get("Content-Type", "video/x-matroska")

        out_headers = {"Accept-Ranges": "bytes"}
        if cd:
            out_headers["Content-Disposition"] = cd
        if cl:
            out_headers["Content-Length"] = cl
        if cr:
            m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+)", cr)
            if m:
                s_o, e_o, t_o = int(m.group(1)), int(m.group(2)), int(m.group(3))
                out_headers["Content-Range"] = f"bytes {s_o - skip}-{e_o - skip}/{t_o - skip}"

        if request.method == "HEAD":
            resp.close()
            return Response(status_code=code, media_type=ct, headers=out_headers)

        def _iter_chunks():
            try:
                while True:
                    buf = resp.read(128 * 1024)
                    if not buf:
                        break
                    yield buf
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try:
                    resp.close()
                except Exception:
                    pass

        return StreamingResponse(_iter_chunks(), status_code=code, media_type=ct, headers=out_headers)
    except Exception as e:
        cleaner._trigger_selfheal_for(url)
        return JSONResponse(status_code=502, content={"error": "clean proxy stream error", "detail": str(e)[:150]})


# ==============================================================================
# 2. 白盒可视化管理台 API (/admin & /api/admin/*)
# ==============================================================================


class CookieUpdateReq(BaseModel):
    cookie: str


@app.get("/", include_in_schema=False)
def root_redirect():
    return RedirectResponse(url="/admin")


@app.get("/admin", response_class=HTMLResponse, tags=["Whitebox Admin"])
def admin_page():
    html_path = os.path.join(os.path.dirname(__file__), "static", "admin.html")
    if os.path.exists(html_path):
        return FileResponse(html_path, media_type="text/html; charset=utf-8")
    return HTMLResponse("<h1>Admin UI not found</h1>", status_code=404)


@app.get("/api/admin/overview", tags=["Whitebox Admin"])
def admin_overview():
    """获取管理台全景概览：能力状态 + 统计数据 + 最近转存 + 最近播放 + 锁源统计。"""
    cap = capability(Request({"type": "http", "method": "GET", "headers": []}))
    st = db.stats()
    con = db._conn()

    recent_transfers = [
        dict(r)
        for r in con.execute(
            "SELECT t.id, t.cat, t.title, t.season, t.episode, t.status, t.share_url, t.remote_path, "
            "ROUND(COALESCE(t.file_size, 0)/1073741824.0, 2) AS size_gb, t.error, t.created_at, t.updated_at, "
            "(SELECT COUNT(*) FROM plays p WHERE p.transfer_id = t.id) AS play_count "
            "FROM transfers t ORDER BY t.updated_at DESC LIMIT 30"
        ).fetchall()
    ]

    recent_plays = [
        dict(r)
        for r in con.execute(
            "SELECT id, cat, title, season, episode, transfer_id, played_at "
            "FROM plays ORDER BY id DESC LIMIT 20"
        ).fetchall()
    ]

    audit_summary = {}
    try:
        for st_name, cnt in con.execute(
            "SELECT check_status, COUNT(*) FROM source_audits GROUP BY check_status"
        ).fetchall():
            audit_summary[st_name or "unknown"] = cnt
    except Exception:
        pass

    live_strm_count = 0
    total_strm_map = 0
    try:
        total_strm_map = con.execute("SELECT COUNT(*) FROM strm_map").fetchone()[0]
        live_strm_count = con.execute("SELECT COUNT(*) FROM transfers WHERE status='done'").fetchone()[0]
    except Exception:
        pass

    return {
        "capability": cap,
        "stats": st,
        "audit_summary": audit_summary,
        "strm_summary": {"registered": total_strm_map, "active_done": live_strm_count},
        "recent_transfers": recent_transfers,
        "recent_plays": recent_plays,
        "server_time": int(time.time()),
    }


@app.get("/api/admin/search-debug", tags=["Whitebox Admin"])
def admin_search_debug(
    title: str = Query(..., description="要测试搜索的片名"),
    cat: str = Query("", description="专区分类（可选，用于判断电影/剧集/动漫）"),
    season: int = Query(0, description="季数 (0=电影)"),
    episode: int = Query(0, description="集数 (0=电影)"),
):
    """白盒搜索与五维打分调试器：展示所有盘搜候选、得分高低与淘汰原因。"""
    t0 = time.time()
    clean_t = search_mod.sanitize_title(title) or title
    locked_url, is_4k, l_title = db.get_locked_4k_share(cat, clean_t, season, episode)

    candidates = []
    raw_results = search_mod.search(clean_t, prefer_panshare=True)
    for r in raw_results:
        sc = search_mod.score_search_result(r, title, season=season, episode=episode)
        candidates.append(
            {
                "name": r.name,
                "source": r.source,
                "engine": r.engine,
                "year": r.year,
                "share_url": r.share_url,
                "score": round(sc, 2),
                "qualified": sc >= 30.0,
                "is_4k_hint": bool(re.search(r"4K|臻彩|2160|UHD|HDR", r.name or "", re.I)),
            }
        )
    candidates.sort(key=lambda x: (-x["score"], -int(x["is_4k_hint"])))
    elapsed = round(time.time() - t0, 3)
    return {
        "query": {"raw_title": title, "clean_title": clean_t, "cat": cat, "season": season, "episode": episode},
        "elapsed_sec": elapsed,
        "locked_share": {"url": locked_url, "is_4k": bool(is_4k), "title": l_title} if locked_url else None,
        "total_candidates": len(candidates),
        "qualified_count": sum(1 for c in candidates if c["qualified"]),
        "candidates": candidates,
    }


@app.post("/api/admin/cookie", tags=["Whitebox Admin"])
def admin_update_cookie(req: CookieUpdateReq):
    """在线热更新 QUARK_COOKIE 并立即校验有效性。"""
    config.set_quark_cookie(req.cookie)
    # 重置转存器缓存以应用新 Cookie
    legacy_srv._transfer = None
    tr = QuarkTransfer(None)
    alive, status_msg = tr.check_cookie_alive()
    return {
        "ok": alive,
        "cookie_hint": config.redact_cookie(config.get_quark_cookie()),
        "cookie_alive": alive,
        "cookie_status": status_msg,
    }


@app.post("/api/admin/selfheal", tags=["Whitebox Admin"])
def admin_trigger_selfheal():
    """手动触发一次全库 live strm 自愈巡检。"""
    t0 = time.time()
    res = selfheal.run_once(verbose=True)
    return {"ok": True, "elapsed_sec": round(time.time() - t0, 2), "result": res}


@app.get("/api/admin/quota", tags=["Whitebox Admin"])
def admin_check_quota():
    """实时递归统计网盘 `/点播` 目录占用容量。"""
    ol = legacy_srv.get_openlist()
    if not ol:
        return JSONResponse(status_code=503, content={"error": "OpenList 未就绪"})
    try:
        total_bytes, items = cleanup.get_vod_disk_usage(ol)
        return {
            "total_gb": round(total_bytes / (1024**3), 2),
            "quota_limit_gb": 2048.0,
            "folder_count": len(items),
            "top_folders": [
                {"name": it["name"], "size_gb": round(it["size"] / (1024**3), 2), "modified": it.get("modified", "")}
                for it in sorted(items, key=lambda x: x["size"], reverse=True)[:15]
            ],
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.delete("/api/admin/transfer/{transfer_id}", tags=["Whitebox Admin"])
def admin_reset_transfer(transfer_id: int):
    """重置单条转存记录（将 strm 还原为 pending 并将 DB 状态标为 cleaned，方便重新选源转存）。"""
    con = db._conn()
    row = con.execute(
        "SELECT id, cat, title, season, episode, remote_path FROM transfers WHERE id=?", (transfer_id,)
    ).fetchone()
    if not row:
        return JSONResponse(status_code=404, content={"error": "记录不存在"})
    cat, title, s, e = row["cat"], row["title"], row["season"], row["episode"]
    sp = db.get_strm_path(cat, title, s, e)
    restored = False
    if sp and os.path.isfile(sp):
        restored = strm_manager.restore_pending(sp, cat, title, s, e)
    con.execute(
        "UPDATE transfers SET status='cleaned', remote_path=NULL, error='[管理台手动重置]', updated_at=? WHERE id=?",
        (int(time.time()), transfer_id),
    )
    con.commit()
    # 清除负缓存
    with legacy_srv._nomiss_lock:
        legacy_srv._nomiss_cache.pop(f"nomiss:{cat}:{title}:{s}:{e}", None)
    return {"ok": True, "id": transfer_id, "strm_restored": restored}
