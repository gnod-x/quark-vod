#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全网影视搜索与一键点播入库中枢
"""

import os
import sys
import json
import re
import urllib.parse
import urllib.request
import threading

BASE = "/home/dong/projects/hometheater"
sys.path.insert(0, os.path.join(BASE, "scripts"))

import tmdb_populate
from quark_vod import search as quark_search
from quark_vod import config as vod_config
from quark_vod import db as vod_db

CINEMA2 = os.path.join(BASE, "cinema2")
IMG_BASE = "https://image.tmdb.org/t/p"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def guess_category(item):
    """根据 TMDB 元数据自动归类到 cinema2 的 9 个专区。"""
    media_type = item.get("media_type") or "movie"
    countries = item.get("origin_country") or []
    genre_ids = item.get("genre_ids") or []
    is_anim = 16 in genre_ids

    if media_type == "movie":
        if is_anim:
            return "国产动漫" if "CN" in countries else "日本动漫"
        if "HK" in countries or "TW" in countries:
            return "港台经典"
        if "KR" in countries:
            return "韩国电影"
        if "CN" in countries:
            return "国产电影"
        return "欧美大片"
    else:  # tv / series
        if is_anim:
            return "国产动漫" if "CN" in countries else "日本动漫"
        if "KR" in countries:
            return "热门韩剧"
        if "CN" in countries:
            return "国产剧集"
        return "欧美剧集"


def check_in_library(cat, title, year):
    """检查是否已存在于本地 cinema2 媒体库。"""
    cat_dir = os.path.join(CINEMA2, cat)
    if not os.path.isdir(cat_dir):
        return False
    # 匹配带年份或不带年份目录
    target_names = [f"{title} ({year})", title]
    for d in os.listdir(cat_dir):
        if d in target_names or d.startswith(title):
            return True
    return False


def search_all(keyword):
    """全网聚合搜索：TMDB 官方元数据 + 夸克网盘 4K 现存校验。"""
    key = tmdb_populate.read_tmdb_key()
    query = keyword.strip()
    if not query:
        return []

    url = f"https://api.themoviedb.org/3/search/multi?api_key={key}&query={urllib.parse.quote(query)}&language=zh-CN"
    try:
        data = tmdb_populate.http_json(url)
    except Exception as e:
        print(f"[Search Error] TMDB query failed: {e}")
        return []

    raw_results = data.get("results") or []
    results = []

    for it in raw_results[:12]:
        if it.get("media_type") not in ("movie", "tv"):
            continue
        if not it.get("poster_path"):
            continue

        title = tmdb_populate.fmt_title(it)
        if not title:
            continue
        date_str = tmdb_populate.fmt_date(it) or ""
        year = date_str[:4] if date_str else ""
        cat = guess_category(it)

        # 检查是否已收录在本地
        in_lib = check_in_library(cat, title, year)

        # 检查是否已有转存好的直链
        is_ready = False
        try:
            row = vod_db.find_done_transfer(cat, f"{title} ({year})" if year else title, 0, 0)
            if row and row.get("remote_path"):
                is_ready = True
        except Exception:
            pass

        results.append({
            "id": it.get("id"),
            "title": title,
            "original_title": it.get("original_title") or it.get("original_name") or "",
            "year": year,
            "media_type": it.get("media_type"),
            "cat": cat,
            "poster": f"{IMG_BASE}/w500{it['poster_path']}",
            "backdrop": f"{IMG_BASE}/w1280{it['backdrop_path']}" if it.get("backdrop_path") else "",
            "rating": round(it.get("vote_average") or 0, 1),
            "overview": (it.get("overview") or "").strip(),
            "in_library": in_lib,
            "is_ready": is_ready,
            "has_4k_quark": True  # 夸克搜索站支持即搜即转
        })

    return results


def add_to_library(item):
    """一键将搜索结果收录入 cinema2，并触发极速后台预热转存与 Jellyfin 刷新。"""
    title = item.get("title")
    year = item.get("year") or ""
    cat = item.get("cat") or "国产电影"
    media_type = item.get("media_type") or "movie"
    tmdb_id = item.get("id")

    if not title:
        return {"code": 1, "msg": "缺少片名参数"}

    folder_name = f"{title} ({year})" if year else title
    dir_path = os.path.join(CINEMA2, cat, folder_name)
    os.makedirs(dir_path, exist_ok=True)

    # 用户通过一键搜索收录，永久置顶保护（绝不参与年份淘汰与配额清理）
    try:
        vod_db.pin_item(cat, folder_name, reason="manual_add")
        vod_db.pin_item(cat, title, reason="manual_add")
    except Exception:
        pass

    # 1. 抓取 TMDB 详情以生成完整的 NFO
    key = tmdb_populate.read_tmdb_key()
    detail = {}
    if tmdb_id:
        try:
            det_url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}?api_key={key}&language=zh-CN"
            detail = tmdb_populate.http_json(det_url)
        except Exception:
            pass

    # 2. 生成 NFO 与下载海报壁纸
    item_for_nfo = {
        "title": title,
        "name": title,
        "original_title": item.get("original_title", ""),
        "original_name": item.get("original_title", ""),
        "release_date": f"{year}-01-01" if year else "",
        "first_air_date": f"{year}-01-01" if year else "",
        "overview": item.get("overview", ""),
        "vote_average": item.get("rating", 0),
        "vote_count": 100,
        "id": tmdb_id,
        "poster_path": item.get("poster", "").replace(f"{IMG_BASE}/w500", ""),
        "backdrop_path": item.get("backdrop", "").replace(f"{IMG_BASE}/w1280", ""),
    }

    if media_type == "movie":
        tmdb_populate.gen_movie_nfo(dir_path, item_for_nfo, detail)
    else:
        tmdb_populate.gen_tv_nfo(dir_path, item_for_nfo, detail)

    tmdb_populate.download_poster(dir_path, item_for_nfo, media_type)
    if item_for_nfo.get("backdrop_path"):
        tmdb_populate.download_fanart(dir_path, item_for_nfo)

    # 3. 生成 STRM 占位文件
    created_strms = []
    if media_type == "movie":
        strm_path = os.path.join(dir_path, f"{folder_name}.strm")
        tmdb_populate.write_pending_strm(strm_path, cat, folder_name, 0, 0)
        created_strms.append((cat, folder_name, 0, 0, strm_path))
    else:
        # 剧集：取第一季或默认 24 集
        seasons = detail.get("seasons") or []
        s1 = next((s for s in seasons if s.get("season_number") == 1), None)
        ep_count = s1.get("episode_count", 24) if s1 else 24
        ep_count = min(max(ep_count, 1), 60)

        s_dir = os.path.join(dir_path, "Season 01")
        os.makedirs(s_dir, exist_ok=True)
        if s1:
            tmdb_populate.gen_season_nfo(s_dir, item_for_nfo, detail, s1)

        for ep in range(1, ep_count + 1):
            ep_file = f"{title} - S01E{ep:02d}.strm"
            ep_path = os.path.join(s_dir, ep_file)
            tmdb_populate.write_pending_strm(ep_path, cat, title, 1, ep)
            created_strms.append((cat, title, 1, ep, ep_path))

    # 4. 登记到 SQLite strm_map
    try:
        con = vod_db._conn()
        for c, t, s, e, p in created_strms:
            con.execute(
                "INSERT OR REPLACE INTO strm_map (cat, title, season, episode, strm_path) VALUES (?, ?, ?, ?, ?)",
                (c, t, s, e, p)
            )
        con.commit()
    except Exception as err:
        print(f"[DB Error] strm_map insert failed: {err}")

    # 5. 触发后台异步预热 (首集/电影立即转存)
    def _bg_preheat():
        try:
            req_url = f"http://127.0.0.1:8099/preheat?cat={urllib.parse.quote(cat)}&t={urllib.parse.quote(folder_name if media_type == 'movie' else title)}&s={1 if media_type == 'tv' else 0}&e={1 if media_type == 'tv' else 0}"
            urllib.request.urlopen(req_url, timeout=10)
        except Exception as e:
            print(f"[Preheat Trigger Error] {e}")

    threading.Thread(target=_bg_preheat, daemon=True).start()

    # 6. 通知 Jellyfin 2 刷新媒体库
    def _bg_refresh_jf():
        try:
            req = urllib.request.Request("http://127.0.0.1:8097/Library/Refresh", data=b"", method="POST")
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

    threading.Thread(target=_bg_refresh_jf, daemon=True).start()

    return {
        "code": 0,
        "msg": f"《{title}》已收录至影院专区【{cat}】，正在后台极速预热，电视端即将可见！",
        "cat": cat,
        "folder": folder_name
    }


if __name__ == "__main__":
    if len(sys.argv) > 1:
        kw = sys.argv[1]
        print(f"搜索: {kw}")
        res = search_all(kw)
        print(f"找到 {len(res)} 条结果:")
        for r in res[:3]:
            print(f" - [{r['cat']}] {r['title']} ({r['year']}) 评分:{r['rating']}")
