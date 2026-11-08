#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TMDB 热门元数据生成器 for jellyfin2 (cinema2)

功能：
  从 TMDB discover/trending 拉取近年热门影视，生成 Jellyfin nfo + strm(占位)，
  海报/背景图全部用 TMDB 远程 URL（Jellyfin 会自动下载并缓存）。

设计原则：
  - 幂等：重复执行只更新，不重复创建；已有 strm（真实直链）的绝不覆盖
  - 分类照搬 jellyfin(8096) 的 9 个专区，路径/命名完全一致
  - 内容过滤：排除成人/三级/纪录片噪音，保证海报墙干净
  - 无 cookie 依赖，可独立运行

用法：
  python3 tmdb_populate.py            # 增量更新（默认每区 60 条）
  python3 tmdb_populate.py --full     # 全量重建（清空 cinema2 后重新生成）
  python3 tmdb_populate.py --limit 100
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.dom import minidom

CUR_YEAR = datetime.datetime.now().year

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根
sys.path.insert(0, os.path.join(BASE, "scripts"))
try:
    from quark_vod import db as vod_db
except Exception:
    vod_db = None

CFG_DIR = os.path.join(BASE, "jellyfin", "config", "plugins", "configurations")
TMDB_XML = os.path.join(CFG_DIR, "Jellyfin.Plugin.Tmdb.xml")
CINEMA2 = os.path.join(BASE, "cinema2")
STATE_FILE = os.path.join(BASE, "data", "tmdb_populate_state.json")
LOG_DIR = os.path.join(BASE, "logs")

IMG_BASE = "https://image.tmdb.org/t/p"

# ── 13 个专区 → TMDB discover 参数 ──────────────────────────────
# 与 jellyfin(8096/8097) 的媒体库名称、CollectionType 保持一致
# 严格限制：只收录当前年度 (CUR_YEAR) 影视资源，太老资源不入库
CATEGORIES = {
    "国产剧集": dict(
        media="tv", kind="series",
        params=f"&with_origin_country=CN&sort_by=popularity.desc&first_air_date.gte={CUR_YEAR}-01-01",
        exclude_genres="16,99,10764",
    ),
    "国产动漫": dict(
        media="tv", kind="series",
        params=f"&with_origin_country=CN&with_genres=16&sort_by=popularity.desc&first_air_date.gte={CUR_YEAR}-01-01",
        exclude_genres="99,10764",
    ),
    "国产电影": dict(
        media="movie", kind="movie",
        params=f"&with_origin_country=CN&sort_by=popularity.desc&primary_release_date.gte={CUR_YEAR}-01-01",
        exclude_genres="99,10764",
    ),
    "日本动漫": dict(
        media="tv", kind="series",
        params=f"&with_origin_country=JP&with_genres=16&sort_by=popularity.desc&first_air_date.gte={CUR_YEAR}-01-01",
        exclude_genres="99,10764",
    ),
    "日本剧集": dict(
        media="tv", kind="series",
        params=f"&with_origin_country=JP&sort_by=popularity.desc&first_air_date.gte={CUR_YEAR}-01-01",
        exclude_genres="16,99,10764",  # 排除动画（归日本动漫专区），纯真人日剧
    ),
    "日本电影": dict(
        media="movie", kind="movie",
        params=f"&with_origin_country=JP&sort_by=popularity.desc&primary_release_date.gte={CUR_YEAR}-01-01&vote_count.gte=5",
        exclude_genres="16,99,10764",  # 排除动画电影（归日本动漫专区），纯真人日影
    ),
    "欧美剧集": dict(
        media="tv", kind="series",
        params=f"&with_origin_country=US|GB|FR|DE|ES|IT&sort_by=popularity.desc&first_air_date.gte={CUR_YEAR}-01-01",
        exclude_genres="16,99,10764",
    ),
    "欧美大片": dict(
        media="movie", kind="movie",
        params=f"&with_origin_country=US|GB&sort_by=popularity.desc&primary_release_date.gte={CUR_YEAR}-01-01",
        exclude_genres="16,99,10764",
    ),
    "港台经典": dict(
        media="movie", kind="movie",
        params=f"&with_origin_country=HK|TW&sort_by=popularity.desc&primary_release_date.gte={CUR_YEAR}-01-01",
        exclude_genres="99,10764",
    ),
    "热门韩剧": dict(
        media="tv", kind="series",
        params=f"&with_origin_country=KR&sort_by=popularity.desc&first_air_date.gte={CUR_YEAR}-01-01",
        exclude_genres="16,99,10764",
    ),
    "韩国电影": dict(
        media="movie", kind="movie",
        params=f"&with_origin_country=KR&sort_by=popularity.desc&primary_release_date.gte={CUR_YEAR}-01-01&vote_count.gte=5",
        exclude_genres="99,10764",
    ),
    "泰国电影": dict(
        media="movie", kind="movie",
        params=f"&with_origin_country=TH&sort_by=popularity.desc&primary_release_date.gte={CUR_YEAR}-01-01&vote_count.gte=3",
        exclude_genres="99,10764",
    ),
    "泰国剧集": dict(
        media="tv", kind="series",
        params=f"&with_origin_country=TH&sort_by=popularity.desc&first_air_date.gte={CUR_YEAR}-01-01",
        exclude_genres="16,99,10764",
    ),
}

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# 单剧集数上限：超过此值的超长篇动漫跳过（防止 strm 爆炸拖垮 Jellyfin 扫描）
MAX_EPISODES_PER_SHOW = 300


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def read_tmdb_key():
    """从 Jellyfin 的 Tmdb 插件配置读取 Key（红线 #8：不进命令行/日志）。"""
    if not os.path.isfile(TMDB_XML):
        sys.exit(f"[FATAL] 未找到 {TMDB_XML}")
    tree = ET.parse(TMDB_XML)
    key = tree.getroot().findtext("TmdbApiKey")
    if not key:
        sys.exit("[FATAL] TmdbApiKey 为空")
    return key.strip()


def http_json(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if i == retries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def sanitize(name):
    """清理文件名非法字符。"""
    return re.sub(r'[\\/:*?"<>|\r\n\t]', " ", name).strip().rstrip(".")


def fmt_title(item):
    """剧集用 name，电影用 title。"""
    return (item.get("title") or item.get("name") or "").strip()


def fmt_date(item):
    return (item.get("release_date") or item.get("first_air_date") or "")[:10]


def is_blocked(cfg, item):
    """内容过滤：成人/三级/噪音。"""
    title = fmt_title(item)
    ov = item.get("overview") or ""
    kw = cfg.get("block_keywords")
    if kw and (re.search(kw, title, re.I) or re.search(kw, ov, re.I)):
        return True
    # 无海报的直接跳过（海报墙不能有裂图）
    if not item.get("poster_path"):
        return True
    # 无标题的跳过
    if not title:
        return True
    # 排除"人物"类型结果
    if item.get("media_type") == "person":
        return True
    return False


def download_poster(dir_path, item, kind):
    """下载海报到本地 poster.jpg。

    2026-10-15 实测：仅靠 nfo 里的 <thumb> 远程 URL，Jellyfin 不会主动抓取
    （有 nfo 且 lockdata=false 时判定元数据已完整，导致 168 部中仅 23 部有海报）。
    本地 poster.jpg 是 Jellyfin 的标准约定，入库时自动登记为 Primary，无需 API 刷新。
    同时保留 nfo 里的 <thumb> 作为兜底（Jellyfin 部分版本仍会读取）。
    """
    if not item.get("poster_path"):
        return False
    dst = os.path.join(dir_path, "poster.jpg")
    # 已存在且非空则跳过（幂等）
    if os.path.isfile(dst) and os.path.getsize(dst) > 1000:
        return False
    url = f"{IMG_BASE}/w500{item['poster_path']}"
    try:
        req = urllib.request.Request(url, headers=UA)
        data = urllib.request.urlopen(req, timeout=30).read()
        if len(data) < 1000:
            return False
        tmp = dst + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dst)
        return True
    except Exception:
        return False


def download_fanart(dir_path, item):
    """下载背景图到本地 backdrop.jpg（可选，失败不影响主流程）。"""
    if not item.get("backdrop_path"):
        return False
    dst = os.path.join(dir_path, "backdrop.jpg")
    if os.path.isfile(dst) and os.path.getsize(dst) > 1000:
        return False
    url = f"{IMG_BASE}/w1280{item['backdrop_path']}"
    try:
        req = urllib.request.Request(url, headers=UA)
        data = urllib.request.urlopen(req, timeout=30).read()
        if len(data) < 1000:
            return False
        tmp = dst + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dst)
        return True
    except Exception:
        return False


def write_xml(path, root_tag, fields):
    """生成 Jellyfin 标准 nfo（UTF-8 + standalone）。"""
    root = ET.Element(root_tag)
    for tag, val in fields:
        el = ET.SubElement(root, tag)
        el.text = str(val)
    xml_bytes = ET.tostring(root, encoding="utf-8")
    dom = minidom.parseString(xml_bytes)
    pretty = dom.toprettyxml(indent="  ", encoding="utf-8")
    # minidom 会加 xml 声明，Jellyfin 认
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(pretty)
    os.replace(tmp, path)


def gen_movie_nfo(dir_path, item, detail):
    fields = [
        ("title", fmt_title(item)),
        ("originaltitle", item.get("original_title") or item.get("original_name") or ""),
        ("year", (fmt_date(item) or "0")[:4]),
        ("plot", (item.get("overview") or "").strip()),
        ("outline", (item.get("overview") or "").strip()[:200]),
        ("runtime", detail.get("runtime") or 0),
        ("rating", item.get("vote_average") or 0),
        ("votes", item.get("vote_count") or 0),
        ("tmdbid", item.get("id")),
        ("premiered", fmt_date(item)),
        ("lockdata", "false"),
    ]
    # 远程海报 + 背景（Jellyfin 会自动抓取缓存）
    if item.get("poster_path"):
        fields.append(("thumb", f"{IMG_BASE}/w500{item['poster_path']}"))
    if item.get("backdrop_path"):
        fields.append(("fanart", f"{IMG_BASE}/w1280{item['backdrop_path']}"))
    write_xml(os.path.join(dir_path, "movie.nfo"), "movie", fields)


def gen_tv_nfo(dir_path, item, detail):
    fields = [
        ("title", fmt_title(item)),
        ("originaltitle", item.get("original_name") or item.get("original_title") or ""),
        ("year", (fmt_date(item) or "0")[:4]),
        ("plot", (item.get("overview") or "").strip()),
        ("outline", (item.get("overview") or "").strip()[:200]),
        ("rating", item.get("vote_average") or 0),
        ("votes", item.get("vote_count") or 0),
        ("tmdbid", item.get("id")),
        ("premiered", fmt_date(item)),
        ("status", detail.get("status") or ""),
        ("lockdata", "false"),
    ]
    for g in detail.get("genres", []) or []:
        fields.append(("genre", g.get("name") or ""))
    if item.get("poster_path"):
        fields.append(("thumb", f"{IMG_BASE}/w500{item['poster_path']}"))
    if item.get("backdrop_path"):
        fields.append(("fanart", f"{IMG_BASE}/w1280{item['backdrop_path']}"))
    write_xml(os.path.join(dir_path, "tvshow.nfo"), "tvshow", fields)


def gen_season_nfo(season_dir, item, detail, season):
    fields = [
        ("title", f"第 {season.get('season_number')} 季"),
        ("plot", (season.get("overview") or "").strip()),
        ("seasonnumber", season.get("season_number")),
        ("lockdata", "false"),
    ]
    if season.get("poster_path"):
        fields.append(("thumb", f"{IMG_BASE}/w500{season['poster_path']}"))
    write_xml(os.path.join(season_dir, "season.nfo"), "season", fields)


def download_image(url, dst_path):
    """通用图片下载助手，带尺寸检查与原子替换。"""
    if not url:
        return False
    if os.path.isfile(dst_path) and os.path.getsize(dst_path) > 1000:
        return False
    try:
        req = urllib.request.Request(url, headers=UA)
        data = urllib.request.urlopen(req, timeout=20).read()
        if len(data) < 1000:
            return False
        tmp = dst_path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dst_path)
        return True
    except Exception:
        return False


def gen_episode_nfo(nfo_path, ep_title, s, e, ep_data):
    """生成单集标准 episodedetails nfo。"""
    fields = [
        ("title", ep_title or ep_data.get("name") or f"第 {e} 集"),
        ("season", s),
        ("episode", e),
        ("plot", (ep_data.get("overview") or "").strip()),
        ("aired", ep_data.get("air_date") or ""),
        ("rating", ep_data.get("vote_average") or 0),
        ("lockdata", "false"),
    ]
    if ep_data.get("still_path"):
        fields.append(("thumb", f"{IMG_BASE}/w500{ep_data['still_path']}"))
    write_xml(nfo_path, "episodedetails", fields)


def write_pending_strm(strm_path, cat, show_title, s, e):
    """占位 strm：内容指向转存服务（未来 C 阶段实现）。
    已存在真实直链的 strm 绝不覆盖。"""
    if os.path.isfile(strm_path):
        head = ""
        try:
            with open(strm_path, "r", encoding="utf-8", errors="ignore") as f:
                head = f.read(64)
        except Exception:
            pass
        if "5244" in head:  # OpenList 真实直链
            return False
    payload = (
        "http://192.168.101.240:8099/pending?"
        + urllib.parse.urlencode({"cat": cat, "t": show_title, "s": s, "e": e})
    )
    tmp = strm_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload + "\n")
    os.replace(tmp, strm_path)
    return True


def load_state():
    if os.path.isfile(STATE_FILE):
        try:
            return json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    json.dump(st, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="全量重建（仍保留真实 strm）")
    ap.add_argument("--limit", type=int, default=60, help="每个专区拉取条数")
    ap.add_argument("--cats", type=str, default="", help="只跑指定专区，逗号分隔")
    args = ap.parse_args()

    os.makedirs(LOG_DIR, exist_ok=True)
    key = read_tmdb_key()
    log(f"TMDB Key 已从插件配置读取（长度 {len(key)}）")

    cats = args.cats.split(",") if args.cats else list(CATEGORIES.keys())
    state = load_state()

    total_new = total_skip = total_eps = 0
    total_poster = total_fanart = 0

    for cat in cats:
        cfg = CATEGORIES[cat]
        media = cfg["media"]
        kind = cfg["kind"]
        cat_dir = os.path.join(CINEMA2, cat)
        os.makedirs(cat_dir, exist_ok=True)

        # 建立专区已有影视的 tmdbid 索引，从源头杜绝因 TMDB 官方译名变更产生多目录重复入库 (如《午夜规则》)
        existing_tmdb_ids = set()
        for existing_folder in os.listdir(cat_dir):
            ef_path = os.path.join(cat_dir, existing_folder)
            if not os.path.isdir(ef_path):
                continue
            for nfo_name in ("movie.nfo", "tvshow.nfo"):
                ef_nfo = os.path.join(ef_path, nfo_name)
                if os.path.isfile(ef_nfo):
                    try:
                        tree = ET.parse(ef_nfo)
                        tid = tree.findtext("tmdbid")
                        if tid:
                            existing_tmdb_ids.add(str(tid).strip())
                    except Exception:
                        pass
                    break

        target_new = args.limit if args.limit else 60
        new_cnt = skip_cnt = ep_cnt = 0
        poster_cnt = fanart_cnt = 0

        # 分页向后扫描：解决不翻页导致每日增量任务永远重复抓取前 20 条的严重 Bug
        page = 1
        max_page = 15
        while page <= max_page and new_cnt < target_new:
            path = f"/discover/{media}?{cfg['params'].lstrip('&')}&page={page}"
            if cfg.get("exclude_genres"):
                path += f"&without_genres={cfg['exclude_genres']}"
            path += "&include_adult=true"
            url = f"https://api.themoviedb.org/3{path}&api_key={key}&language=zh-CN"
            try:
                data = http_json(url)
            except Exception as e:
                log(f"[{cat}] TMDB 请求第 {page} 页失败: {e}")
                break

            raw_results = data.get("results", [])
            if not raw_results:
                break

            results = [r for r in raw_results if not is_blocked(cfg, r)]
            for item in results:
                if new_cnt >= target_new:
                    break

                item_id_str = str(item.get("id") or "").strip()
                if item_id_str and item_id_str in existing_tmdb_ids and not args.full:
                    skip_cnt += 1
                    continue

                title = sanitize(fmt_title(item))
                year = (fmt_date(item) or "0")[:4]
                if year.isdigit() and int(year) < CUR_YEAR and not args.full:
                    skip_cnt += 1
                    continue
                if kind == "movie":
                    folder = f"{title} ({year})" if year != "0" else title
                else:
                    folder = title
                dir_path = os.path.join(cat_dir, folder)

                nfo = "movie.nfo" if kind == "movie" else "tvshow.nfo"
                nfo_path = os.path.join(dir_path, nfo)
                if os.path.isfile(nfo_path) and not args.full:
                    skip_cnt += 1
                    continue

                os.makedirs(dir_path, exist_ok=True)

                # 详情（拿 runtime/genres/status/seasons）
                try:
                    detail = http_json(
                        f"https://api.themoviedb.org/3/{media}/{item['id']}?api_key={key}&language=zh-CN"
                    )
                except Exception:
                    detail = {}

                if kind == "movie":
                    gen_movie_nfo(dir_path, item, detail)
                else:
                    gen_tv_nfo(dir_path, item, detail)
                new_cnt += 1
                if item_id_str:
                    existing_tmdb_ids.add(item_id_str)

                # 下载本地海报 + 背景图
                if download_poster(dir_path, item, kind):
                    poster_cnt += 1
                if download_fanart(dir_path, item):
                    fanart_cnt += 1

                if kind == "movie":
                    strm_path = os.path.join(dir_path, f"{folder}.strm")
                    if write_pending_strm(strm_path, cat, folder, 0, 0):
                        ep_cnt += 1
                        if vod_db:
                            try:
                                vod_db.save_strm_map(cat, folder, 0, 0, strm_path)
                            except Exception:
                                pass
                else:
                    # 剧集：按 TMDB 实际季集生成（跳过特别篇 season 0）
                    seasons = [s for s in detail.get("seasons", []) if s.get("season_number", 0) >= 1]
                    total_eps = sum(s.get("episode_count") or 0 for s in seasons)
                    if total_eps > MAX_EPISODES_PER_SHOW:
                        log(f"    [跳过] {title} 共 {total_eps} 集，超过上限 {MAX_EPISODES_PER_SHOW}")
                        continue
                    for season in seasons:
                        s_num = season.get("season_number")
                        s_dir = os.path.join(dir_path, f"Season {s_num:02d}")
                        os.makedirs(s_dir, exist_ok=True)
                        s_nfo = os.path.join(s_dir, "season.nfo")
                        if not os.path.isfile(s_nfo) or args.full:
                            gen_season_nfo(s_dir, item, detail, season)

                        # 拉取分季详情：获取单集名称、官方分集剧照 (still_path) 与单集剧情
                        ep_info_map = {}
                        try:
                            s_url = f"https://api.themoviedb.org/3/tv/{item['id']}/season/{s_num}?api_key={key}&language=zh-CN"
                            s_detail = http_json(s_url)
                            for ep_obj in s_detail.get("episodes", []):
                                ep_num = ep_obj.get("episode_number")
                                if ep_num:
                                    ep_info_map[ep_num] = ep_obj
                        except Exception:
                            pass

                        ep_total = season.get("episode_count") or 0
                        for e in range(1, ep_total + 1):
                            ep_data = ep_info_map.get(e, {})
                            base_ep = f"{folder} - S{s_num:02d}E{e:02d}"
                            strm_path = os.path.join(s_dir, f"{base_ep}.strm")
                            thumb_path = os.path.join(s_dir, f"{base_ep}-thumb.jpg")
                            ep_nfo_path = os.path.join(s_dir, f"{base_ep}.nfo")

                            # 1. 随更新同步下载分集高清剧照（彻底消灭黑块与裂图）
                            still_p = ep_data.get("still_path")
                            if still_p and not os.path.isfile(thumb_path):
                                download_image(f"{IMG_BASE}/w500{still_p}", thumb_path)

                            # 2. 随更新同步写入单集 NFO
                            if not os.path.isfile(ep_nfo_path) or args.full:
                                gen_episode_nfo(ep_nfo_path, ep_data.get("name"), s_num, e, ep_data)

                            # 3. 写入占位 strm
                            if write_pending_strm(strm_path, cat, folder, s_num, e):
                                ep_cnt += 1
                                if vod_db:
                                    try:
                                        vod_db.save_strm_map(cat, folder, s_num, e, strm_path)
                                    except Exception:
                                        pass
                # 轻微限速，防 TMDB 429
                time.sleep(0.08)

            total_pages = data.get("total_pages") or 1
            if page >= total_pages:
                break
            page += 1

        log(f"[{cat}] 新增/更新 {new_cnt} 条 (扫描至第 {page-1} 页)，已存在跳过 {skip_cnt} 条，海报 {poster_cnt} 张，背景 {fanart_cnt} 张，占位 strm {ep_cnt} 个")
        total_new += new_cnt
        total_skip += skip_cnt
        total_eps += ep_cnt
        total_poster += poster_cnt
        total_fanart += fanart_cnt

    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state["last_new"] = total_new
    state["last_eps"] = total_eps
    save_state(state)

    log(f"完成：新增/更新 {total_new} 条，跳过 {total_skip} 条，海报 {total_poster} 张，背景 {total_fanart} 张，占位 strm {total_eps} 个")


if __name__ == "__main__":
    main()
