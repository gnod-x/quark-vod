#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Quark-VOD API, playback proxy and authenticated administration.
"""

import hmac
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
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from quark_vod import __version__, cleaner, cleanup, config, db, search as search_mod, selfheal, server as legacy_srv, strm_manager
from quark_vod.transfer import QuarkTransfer
from quark_vod import coordination
from quark_vod import media_cache
from quark_vod import search_plugins
from quark_vod import download_http


class MediaResponse(StreamingResponse):
    """Release capacity even when send(), the client or the iterator fails."""
    def __init__(self,chunks,close_stream=None,**kwargs):
        super().__init__(chunks,**kwargs)
        self.chunks=chunks
        self.close_stream=close_stream

    async def __call__(self,scope,receive,send):
        try:
            await super().__call__(scope,receive,send)
        finally:
            try:
                try:
                    self.chunks.close()
                finally:
                    if self.close_stream:
                        self.close_stream()
            finally:
                media_cache.stream_slots.release()


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
    # Initialize and, when needed, upgrade the state schema before any startup
    # cleanup or background task can observe partially migrated data.
    db.init_db()
    legacy_srv._cleanup_stale_transfers()

    def _bg_scan():
        try:
            n = strm_manager.scan_and_register()
            print(f"[{config.SERVICE_NAME}] strm 登记完成: {n} 个", flush=True)
        except Exception as e:
            print(f"[{config.SERVICE_NAME}] strm 登记异常: {e}", flush=True)

    if config.VOD_STARTUP_SCAN:
        threading.Thread(target=_bg_scan, daemon=True, name="strm-scan").start()

    stop_event = threading.Event()
    if config.SELFHEAL_ENABLED:
        threading.Thread(target=selfheal.heal_loop, kwargs={"stop_event": stop_event},
                         daemon=True, name="selfheal").start()
        print(
            f"[{config.SERVICE_NAME}] 自愈线程已启动（间隔 {config.SELFHEAL_INTERVAL}s，"
            f"首次延迟 {config.SELFHEAL_INITIAL_DELAY}s）",
            flush=True,
        )

    # Starting an unconfigured instance never logs into a drive or writes a probe.
    if config.VOD_STARTUP_WRITE_PROBE:
        _probe_capability()

    print(f"[{config.SERVICE_NAME}] Cookie: {config.redact_cookie(config.get_quark_cookie())}", flush=True)
    try:
        yield
    finally:
        stop_event.set()
        db.close()


app = FastAPI(
    title="Quark VOD",
    description="可独立部署的夸克点播服务，支持搜索源热加载和可选媒体库集成。",
    version=__version__,
    lifespan=lifespan,
)


@app.middleware("http")
async def admin_auth(request: Request, call_next):
    path = request.url.path
    protected = path.startswith("/api/admin/") or path in {"/stats", "/capability", "/preheat"}
    if protected:
        token = config.get_admin_token()
        if not token:
            return JSONResponse(status_code=503, content={"error": "admin_not_configured"},
                                headers={"Cache-Control": "no-store"})
        authorization = request.headers.get("authorization", "")
        scheme, _, supplied = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.encode(), token.encode()):
            return JSONResponse(status_code=401, content={"error": "unauthorized"},
                                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"})
    response = await call_next(request)
    if path == "/admin" or path.startswith("/api/admin/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(coordination.Busy)
async def maintenance_busy(request, exc):
    return JSONResponse(status_code=409,content={'error':str(exc),'code':'busy'})


# ==============================================================================
# 1. 核心流媒体兼容接口（100% 兼容原有客户端、custom-ffmpeg 探针与验证脚本）
# ==============================================================================


@app.get("/health", tags=["Core VOD"])
@app.head("/health", tags=["Core VOD"])
async def health(request: Request):
    if request.method == "HEAD":
        return Response(status_code=200, media_type="application/json")
    return {"status": "ok", "service": config.SERVICE_NAME, "engine": "fastapi-v3", "version": __version__}


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
    registry = search_plugins.get_registry().status()
    checked = getattr(app.state, "capability_probe", {})
    return {
        "cookie_ready": bool(config.get_quark_cookie()),
        "cookie_hint": config.redact_cookie(config.get_quark_cookie()),
        "cookie_alive": checked.get("cookie_alive"),
        "cookie_status": checked.get("cookie_status", "尚未执行连接验证"),
        "openlist_write": checked.get("openlist_write"),
        "openlist_reason": checked.get("openlist_reason", "尚未执行连接验证"),
        "panshare_ready": bool(registry.get("enabled_count")),
        "ready": bool(checked.get("ready") and registry.get("enabled_count")),
        "probe_time": checked.get("probe_time"),
        "plugins": registry,
    }


def _probe_capability():
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
    registry = search_plugins.get_registry().status()
    result = {
        "cookie_ready": bool(cur_cookie),
        "cookie_alive": cookie_alive,
        "cookie_status": cookie_status,
        "cookie_hint": config.redact_cookie(cur_cookie),
        "openlist_write": write_ok,
        "openlist_reason": write_reason,
        "panshare_ready": bool(registry.get("enabled_count")),
        "ready": bool(cookie_alive and write_ok and registry.get("enabled_count")),
        "probe_time": int(time.time()),
    }
    app.state.capability_probe = result
    return result


@app.post("/api/admin/capability/probe", tags=["Administration"])
def admin_probe_capability():
    """Explicit authenticated probe; may create and remove a unique drive probe."""
    return _probe_capability()


@app.get("/api/admin/plugins", tags=["Administration"])
def admin_plugins():
    registry = search_plugins.get_registry()
    registry.snapshot()
    return registry.status()


@app.post("/api/admin/plugins/reload", tags=["Administration"])
def admin_reload_plugins():
    try:
        return search_plugins.get_registry().reload()
    except search_plugins.PluginConfigError:
        return JSONResponse(status_code=422, content={"error": "invalid_plugin_configuration",
                                                     "active": search_plugins.get_registry().status()})


@app.api_route("/play", methods=["GET", "HEAD"], tags=["Core VOD"])
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
    print(f"[{config.SERVICE_NAME}] {request.method} {request.url.path} S{s}E{e}", flush=True)

    if request.method == "HEAD":
        row = db.find_done_transfer(cat, t, s, e)
        if row and row["remote_path"]:
            url = legacy_srv._direct_url(row["remote_path"])
            url = cleaner.clean_url_if_obfuscated(url)
            if row['remote_path'].lower().endswith(('.mp4','.m4v','.mov','.mp41')):
                skip=int((urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get('skip') or ['0'])[0]) if '/clean?' in url else 0
                url=media_cache.playback_url(row,skip)
            h = DummyHandler(dict(request.headers))
            return h._send(302, b"", ctype="text/plain", headers={"Location": url})
        return Response(content=b"", status_code=200, media_type="video/mp4", headers={"Accept-Ranges": "bytes"})

    try:
        from quark_vod.identity import Work
        work = Work(cat,t,s,e)
        if not work.cat:
            raise ValueError('missing cat')
        result = legacy_srv.resolve_playback(work)
        return RedirectResponse(result.url,status_code=302,headers={'Cache-Control':'no-store'})
    except ValueError as exc:
        return JSONResponse(status_code=400,content={'error':str(exc)})
    except legacy_srv.PlaybackUnavailable as exc:
        return JSONResponse(status_code=503,content={'error':str(exc),'code':exc.code},
                            headers={'Retry-After':'5','Cache-Control':'no-store'})


@app.api_route('/media/{transfer_id}',methods=['GET','HEAD'],tags=['Core VOD'])
def cached_media(request: Request,transfer_id: int,v: str='',skip: int=0):
    row=db.get_transfer_by_id(transfer_id)
    if not row or row['status']!='done' or not row['remote_path']:
        return Response(status_code=404)
    if v!=media_cache.version(row) or skip not in (0,8):
        return Response(status_code=409,headers={'Cache-Control':'no-store'})
    if not media_cache.stream_slots.acquire(timeout=10):
        return Response(status_code=503,headers={'Retry-After':'2'})
    try:
        with coordination.playback_lease():
            status,headers,chunks=media_cache.cache.open(v,legacy_srv._direct_url(row['remote_path']),skip,request.headers.get('range',''))
            if request.method!='HEAD':db.record_access(transfer_id)
        if request.method=='HEAD':
            chunks.close();media_cache.stream_slots.release()
            return Response(status_code=status,headers=headers)
        return MediaResponse(chunks,status_code=status,headers=headers)
    except cleaner.CleanStreamError as exc:
        media_cache.stream_slots.release()
        return JSONResponse(status_code=exc.status,content={'error':str(exc)},headers=exc.headers)
    except Exception:
        media_cache.stream_slots.release()
        raise


@app.get("/preheat", tags=["Core VOD"])
def preheat(
    cat: str = Query("", description="媒体库分类"),
    t: str = Query("", description="影片标题"),
    s: int = Query(0, description="季数"),
    e: int = Query(0, description="集数"),
):
    try:
        ok,status = legacy_srv.trigger_preheat_task(cat,t,s,e)
        return JSONResponse(status_code=202,content={'ok':ok,'status':status})
    except ValueError as exc:
        return JSONResponse(status_code=400,content={'error':str(exc)})
    except legacy_srv.PlaybackUnavailable as exc:
        return JSONResponse(status_code=503,content={'error':str(exc),'code':exc.code})


def media_url_allowed(url: str) -> bool:
    """Only configured OpenList download origins may enter the public proxy."""
    return download_http.allowed_openlist_url(url)


@app.api_route("/clean", methods=["GET", "HEAD"], tags=["Core VOD"])
def clean_stream(
    request: Request,
    url: str = Query(..., description="原始 OpenList 直链 URL"),
    skip: int = Query(8, ge=0, le=4096, description="需剥离的前导混淆字节数（默认 8 字节 PNG 头）"),
):
    """流式剥离前导字节，统一处理完整请求、单区间和后缀 Range。"""
    if not url:
        return JSONResponse(status_code=400, content={"error": "missing url"})
    if not media_url_allowed(url):
        return JSONResponse(status_code=403, content={"error": "media_origin_not_allowed"})
    if not media_cache.stream_slots.acquire(timeout=10):
        return Response(status_code=503, headers={"Retry-After": "2"})
    stream = None
    handed_off = False
    try:
        stream = cleaner.open_clean_stream(url, skip, request.headers.get("range", ""), request.headers,
                                          opener=download_http.urlopen)
        if request.method == "HEAD":
            return Response(status_code=stream.status, headers=stream.headers)
        response = MediaResponse(stream.chunks(), close_stream=stream.close,
                                 status_code=stream.status, headers=stream.headers)
        handed_off = True
        return response
    except cleaner.CleanStreamError as exc:
        return JSONResponse(status_code=exc.status, content={"error": str(exc)}, headers=exc.headers)
    except Exception as exc:
        return JSONResponse(status_code=502, content={"error": "clean proxy stream error", "type": type(exc).__name__})
    finally:
        if not handed_off:
            try:
                if stream is not None:
                    stream.close()
            finally:
                media_cache.stream_slots.release()


# ==============================================================================
# 2. 白盒可视化管理台 API (/admin & /api/admin/*)
# ==============================================================================


class CookieUpdateReq(BaseModel):
    cookie: str = Field(max_length=32768)


@app.get("/", include_in_schema=False)
def root_redirect():
    return RedirectResponse(url="/admin")


@app.get("/admin", response_class=HTMLResponse, tags=["Whitebox Admin"])
def admin_page():
    html_path = os.path.join(os.path.dirname(search_plugins.__file__), "static", "admin.html")
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
        "playback_metric": "access_sessions",
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
    locked_url, is_4k, l_title = db.get_locked_4k_share(cat, title, season, episode)

    candidates = []
    report = search_mod.search_report(title, season=season, episode=episode)
    raw_results = report['results']
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
                "is_4k_hint": legacy_srv.is_4k_candidate(r.name or ""),
            }
        )
    candidates.sort(key=lambda x: (-x["score"], -int(x["is_4k_hint"])))
    elapsed = round(time.time() - t0, 3)
    return {
        "query": {"raw_title": title, "clean_title": clean_t, "cat": cat, "season": season, "episode": episode},
        "elapsed_sec": elapsed,
        "configured": report.get("configured", True),
        "partial": report.get("partial", False),
        "locked_share": {"url": locked_url, "is_4k": bool(is_4k), "title": l_title} if locked_url else None,
        "total_candidates": len(candidates),
        "qualified_count": sum(1 for c in candidates if c["qualified"]),
        "candidates": candidates,
    }


@app.post("/api/admin/cookie", tags=["Whitebox Admin"])
def admin_update_cookie(req: CookieUpdateReq):
    """在线热更新 QUARK_COOKIE 并立即校验有效性。"""
    try:
        config.set_quark_cookie(req.cookie)
    except ValueError:
        return JSONResponse(status_code=422, content={"error": "invalid_cookie_header"})
    except OSError:
        return JSONResponse(status_code=503, content={"error": "cookie_persistence_failed"})
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
    if not config.VOD_MAINTENANCE_ENABLED:
        return JSONResponse(status_code=409, content={"error": "maintenance_disabled"})
    t0 = time.time()
    res = selfheal.scan_and_heal(verbose=True)
    return {"ok": True, "elapsed_sec": round(time.time() - t0, 2), "result": res}


@app.get("/api/admin/quota", tags=["Whitebox Admin"])
def admin_check_quota():
    """实时递归统计网盘 `/点播` 目录占用容量。"""
    ol = legacy_srv.get_openlist()
    if not ol:
        return JSONResponse(status_code=503, content={"error": "OpenList 未就绪"})
    try:
        total_bytes, items = cleanup.get_vod_disk_usage(ol)
        trigger, target, account_total, account_used = cleanup.resolve_quotas()
        return {
            "total_gb": round(total_bytes / (1024**3), 2),
            "quota_limit_gb": round(trigger / (1024**3), 2),
            "quota_target_gb": round(target / (1024**3), 2),
            "quota_scope": "account" if account_total else "vod_fallback",
            "account_total_gb": round(account_total / (1024**3), 2),
            "account_used_gb": round(account_used / (1024**3), 2),
            "folder_count": len(items),
            "top_folders": [
                {"name": it["name"], "size_gb": round(it["size"] / (1024**3), 2), "modified": it.get("modified", "")}
                for it in sorted(items, key=lambda x: x["size"], reverse=True)[:15]
            ],
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.delete("/api/admin/transfer/{transfer_id}", tags=["Whitebox Admin"])
@coordination.maintenance
def admin_reset_transfer(transfer_id: int):
    """重置可确认的记录；未决保存意图必须先核对，避免重复提交。"""
    con = db._conn()
    row = con.execute(
        "SELECT id, cat, title, season, episode, remote_path, status, "
        "save_target, save_task_id, save_started_at, save_file_fid FROM transfers WHERE id=?", (transfer_id,)
    ).fetchone()
    if not row:
        return JSONResponse(status_code=404, content={"error": "记录不存在"})
    if (row["status"] == "saving" or row["save_target"] or row["save_task_id"]
            or row["save_started_at"] or row["save_file_fid"]):
        return JSONResponse(status_code=409, content={
            "error": "原转存意图结果未知，已保留原任务和播放入口；请先通过原转存流程恢复并确认终态，待保存意图清除后再重置",
            "code": "save_intent_unconfirmed",
        })
    cat, title, s, e = row["cat"], row["title"], row["season"], row["episode"]
    sp = db.get_strm_path(cat, title, s, e)
    restored = False
    if sp and os.path.isfile(sp):
        try:
            restored = strm_manager.restore_pending(sp, cat, title, s, e)
            if not strm_manager.is_pending(strm_manager.read_strm(sp)):
                raise OSError("STRM is still live")
        except Exception as exc:
            return JSONResponse(status_code=409, content={"error": "STRM 还原失败，数据库未修改", "type": type(exc).__name__})
    con.execute(
        "UPDATE transfers SET status='cleaned', remote_path=NULL, save_target=NULL, "
        "save_task_id=NULL, save_started_at=NULL, save_file_fid=NULL, error='[管理台手动重置]', updated_at=? WHERE id=?",
        (int(time.time()), transfer_id),
    )
    con.commit()
    legacy_srv.invalidate_transfer_cache(transfer_id)
    return {"ok": True, "id": transfer_id, "strm_restored": restored}
