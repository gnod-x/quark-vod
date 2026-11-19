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
  python3 tmdb_populate.py --full     # 在分页/数量上限内更新已有元数据，保留 live
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

BASE = os.environ.get("HOMETHEATER_BASE_DIR", "/home/dong/projects/hometheater")
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
# TMDB AIGC 纯生成类通用关键词（全球专区通用过滤：AI生成、AI动画、AIGC、AI slop）
GLOBAL_AI_EXCLUDE_KEYWORDS = "374887,379282,385701,386289,372737,383566,387745"

# 国内专区特有的微短剧/竖屏短剧/AI短剧关键词（只针对国产剧集、国产动漫过滤，绝不误伤欧美/日本的AI题材科幻大片如《克拉拉与太阳》）
CN_SHORT_EXCLUDE_KEYWORDS = "310,370469,382841,383994,381124,374944,308960"

AI_EXCLUDE_KEYWORD_IDS = f"{GLOBAL_AI_EXCLUDE_KEYWORDS},{CN_SHORT_EXCLUDE_KEYWORDS}"

GLOBAL_AI_KEYWORD_IDS = {374887, 379282, 385701, 386289, 372737, 383566, 387745}
CN_SHORT_KEYWORD_IDS = {310, 370469, 382841, 383994, 381124, 374944, 308960}
AI_KEYWORD_ID_SET = GLOBAL_AI_KEYWORD_IDS | CN_SHORT_KEYWORD_IDS

AI_KEYWORD_NAMES = {
    "ai generated", "ai slop", "aigc", "generative ai", "ai animation",
    "ai-generated-media", "ai-generated content", "ai generated content"
}

AI_NETWORKS = {
    "hong guo short drama", "hongguo short drama", "kuaishou", "douyin",
    "短剧", "微短剧", "漫剧", "时刻热血剧场", "时刻互动", "百川中文新剧", "剧点短剧", "大芒短剧"
}

# 广电总局电视剧集数规范与国产长篇正规动漫白名单
DONGHUA_LEGIT_WHITELIST = {
    "斗罗大陆", "斗罗大陆ⅱ绝世唐门", "凡人修仙传", "师兄啊师兄", "万古神帝", "万古至尊：李云霄传",
    "喜羊羊与灰太狼之古古怪界有古怪", "喜羊羊与灰太狼之破界山海诀", "喜羊羊与灰太狼之古古怪界有古怪番外篇",
    "安全警长啦咘啦哆之巡逻行动", "百变校巴之奇妙大自然", "超级飞侠之奇芯宝藏", "格林童话故事动画",
    "盘龙", "择天记", "择日飞升", "将夜", "深空彼岸", "搜神记", "史上最强炼体老祖", "掌门低调点",
    "大夏守墓人", "成也萧河", "驭灵师", "灵境行者", "太古神尊", "一斩苍穹", "东大高武学院",
    "缔灵爱水心缠", "李熊猫", "花仙子之魔法香对论", "汤直志异", "百日成王", "我家隔壁盘丝洞",
    "我把末日上交给了国家", "苏东坡与杭州的故事"
}

AI_TEXT_PATTERN = re.compile(
    r'(?<![a-zA-Z0-9])AI([短微]?剧|漫剧|生成|制作|技术制作|动画|全流程|课程)|'
    r'AIGC([短微]?剧|漫剧|动画)?|'
    r'首部AI|全AI|时刻互动|时刻热血剧场|动态漫|(?<!浪)漫剧|'
    r'在线AI课程|斑马(思维|百科|英语|语文)|巨乳|3P性爱|'
    r'\b(ai[-_ ]?(generated|slop)|generative[-_ ]?ai)\b',
    re.I
)

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
        raise RuntimeError(f"未找到 TMDB 配置: {TMDB_XML}")
    tree = ET.parse(TMDB_XML)
    key = tree.getroot().findtext("TmdbApiKey")
    if not key:
        raise RuntimeError('TmdbApiKey 为空')
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


def tmdb_object(url, list_field=None):
    """Reject API error envelopes and incomplete list responses."""
    data = http_json(url)
    if not isinstance(data, dict) or not data or data.get('success') is False:
        raise ValueError('invalid TMDB response')
    if list_field and not isinstance(data.get(list_field), list):
        raise ValueError('missing TMDB list')
    return data


def sanitize(name):
    """清理文件名非法字符。"""
    return re.sub(r'[\\/:*?"<>|\r\n\t]', " ", name).strip().rstrip(".")


def fmt_title(item):
    """剧集用 name，电影用 title。"""
    return (item.get("title") or item.get("name") or "").strip()


def fmt_date(item):
    return (item.get("release_date") or item.get("first_air_date") or "")[:10]


def is_ai_content(item, detail=None, cat=None):
    """检测影视资源是否属于 AI 剧 / AI 短剧 / AIGC 劣质生成物 / 微短剧。"""
    title = fmt_title(item)
    orig = item.get("original_title") or item.get("original_name") or ""
    ov = item.get("overview") or ""

    # 1. 垃圾/低俗/AI课程关键词预检
    if any(w in title for w in ['斑马', '巨乳', '3P性爱', '3p性爱', '在线AI课程']):
        return True

    # 2. 标题与剧情文本特征匹配
    combined_text = f"{title} {orig} {ov}"
    if detail:
        d_title = fmt_title(detail)
        d_orig = detail.get("original_title") or detail.get("original_name") or ""
        d_ov = detail.get("overview") or ""
        combined_text += f" {d_title} {d_orig} {d_ov}"

    if AI_TEXT_PATTERN.search(combined_text):
        return True

    # 3. 关键词 ID 与名称匹配
    kw_objs = []
    for src in ((item, detail) if detail else (item,)):
        if "keywords" in src:
            kw_data = src["keywords"]
            if isinstance(kw_data, dict):
                kw_objs.extend(kw_data.get("results") or kw_data.get("keywords") or [])
            elif isinstance(kw_data, list):
                kw_objs.extend(kw_data)

    kw_ids = set()
    kw_names = set()
    for k in kw_objs:
        if isinstance(k, dict):
            if k.get("id"):
                kw_ids.add(k["id"])
            if k.get("name"):
                kw_names.add(k["name"].strip().lower())

    # 全球专区通用：纯 AIGC 关键词拦截
    if kw_ids.intersection(GLOBAL_AI_KEYWORD_IDS):
        return True
    if any(kn in AI_KEYWORD_NAMES for kn in kw_names):
        return True

    # 国产专区特有：微短剧关键词拦截（避免误伤欧美/日本合法科幻大片）
    is_cn = (cat and cat.startswith("国产")) or (item.get("origin_country") == ["CN"])
    if is_cn and kw_ids.intersection(CN_SHORT_KEYWORD_IDS):
        return True

    # 4. 制作方/播出平台过滤（红果短剧、快手、抖音、微短剧机构）
    if detail:
        nets = [n.get("name", "").lower() for n in detail.get("networks", []) if isinstance(n, dict)]
        prods = [p.get("name", "").lower() for p in detail.get("production_companies", []) if isinstance(p, dict)]
        all_companies = nets + prods
        if any(any(an in name for an in AI_NETWORKS) for name in all_companies):
            return True

        # 5. 集数形态过滤（国产剧集 > 55 集且非央视/卫视上星播出；劣质动态漫/微短剧 > 120 集）
        seasons = [s for s in detail.get("seasons", []) if isinstance(s, dict) and s.get("season_number", 0) >= 1]
        eps = sum(s.get("episode_count") or 0 for s in seasons) or detail.get("number_of_episodes") or 0
        if cat == "国产剧集" and eps > 55:
            is_cctv_or_tv = any(x in ' '.join(nets) for x in ['cctv-1', 'cctv-8', 'cctv', 'hunan', 'zhejiang', 'dragon', 'jiangsu', 'beijing', 'dongfang'])
            if not is_cctv_or_tv:
                return True
        elif cat == "国产动漫" and eps > 120:
            if title.lower() not in DONGHUA_LEGIT_WHITELIST:
                return True

    return False


def is_ai_directory(dpath, jf_tags_cache=None, cat=None, tmdb_key=None):
    """检测本地目录是否为 AI 剧 / 微短剧 / AIGC 劣质影视。
    返回 (bool, reason)。
    """
    show_name = os.path.basename(dpath)

    # 1. 垃圾/低俗/AI课程关键词预检（0ms 秒级拦截）
    if any(w in show_name for w in ['斑马', '巨乳', '3P性爱', '3p性爱']):
        return True, "junk_title"

    # 2. 本地 NFO 快速解析
    for nfo_name in ("tvshow.nfo", "movie.nfo"):
        nfo_p = os.path.join(dpath, nfo_name)
        if os.path.isfile(nfo_p):
            try:
                tree = ET.parse(nfo_p)
                r = tree.getroot()
                title = r.findtext("title") or show_name
                orig = r.findtext("originaltitle") or ""
                plot = (r.findtext("plot") or "").split("【AI 精彩看点】")[0]
                outline = (r.findtext("outline") or "").split("【AI 精彩看点】")[0]
                text = f"{title} {orig} {plot} {outline}"
                if AI_TEXT_PATTERN.search(text):
                    return True, "text_pattern"
                # Check tags in NFO
                tags = [t.text.strip().lower() for t in r.findall("tag") if t.text]
                if any(t in AI_KEYWORD_NAMES for t in tags):
                    return True, "nfo_tag"

                # 3. 针对国产专区（剧集与动漫共约 200 条）调用 TMDB API 精准复核制作方、微短剧标签与集数形态
                tid = r.findtext("tmdbid")
                if tid and tmdb_key and cat in ("国产剧集", "国产动漫"):
                    try:
                        url = f"https://api.themoviedb.org/3/tv/{tid}?api_key={tmdb_key}&append_to_response=keywords"
                        d = http_json(url)
                        if is_ai_content({"title": title}, detail=d, cat=cat):
                            return True, f"tmdb_detail_filter(tid:{tid})"
                    except Exception:
                        pass
            except Exception:
                pass

    # Check Jellyfin DB tags if cache provided
    if jf_tags_cache and dpath in jf_tags_cache:
        tags = jf_tags_cache[dpath]
        if any(t in AI_KEYWORD_NAMES for t in tags):
            return True, "jf_tag"

    return False, ""


def is_blocked(cfg, item, cat=None):
    """内容过滤：成人/三级/噪音/AI剧。"""
    title = fmt_title(item)
    ov = item.get("overview") or ""
    kw = cfg.get("block_keywords")
    if kw and (re.search(kw, title, re.I) or re.search(kw, ov, re.I)):
        return True
    # 过滤 AI 剧 / AI 短剧 / AIGC 劣质生成条目
    if is_ai_content(item, cat=cat):
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


def is_episode_aired(ep_data, s_num, e, detail=None, today_str=None):
    """判断剧集单集是否已正式播出/更新（防止连载中剧集提前生成未来未播出的虚假占位）。"""
    if today_str is None:
        today_str = datetime.date.today().isoformat()
    air_date = ep_data.get("air_date") if ep_data else None
    if air_date:
        return air_date <= today_str
    if detail:
        last_ep = detail.get("last_episode_to_air")
        if last_ep:
            last_s = last_ep.get("season_number", 0)
            last_e = last_ep.get("episode_number", 0)
            if s_num < last_s:
                return True
            if s_num == last_s and e <= last_e:
                return True
            return False
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
    """Only create/update pending placeholders; retain every activated source."""
    from quark_vod import config, strm_manager
    from quark_vod.coordination import maintenance
    @maintenance
    def write():
        if os.path.isfile(strm_path):
            try:
                with open(strm_path, encoding='utf-8') as stream:
                    current = stream.read().strip()
            except (OSError, UnicodeError):
                return False
            if not strm_manager.is_pending(current) or urllib.parse.urlsplit(current).path != '/pending':
                return False
        payload = config.PENDING_BASE_URL + '/pending?' + urllib.parse.urlencode(
            {'cat':cat,'t':show_title,'s':s,'e':e})
        strm_manager._atomic_write(strm_path, payload)
        return True
    return write()


def episode_strm_path(season_dir, title, season, episode):
    """Reuse the episode's existing filename even if its title has changed."""
    matches = []
    if os.path.isdir(season_dir):
        suffix = re.compile(rf' - S0*{season}E0*{episode}\.strm$', re.I)
        for name in os.listdir(season_dir):
            if suffix.search(name) and os.path.isfile(os.path.join(season_dir, name)):
                matches.append(name)
    if len(matches) > 1:
        raise RuntimeError('ambiguous existing episode filenames')
    name = matches[0] if matches else f'{title} - S{season:02d}E{episode:02d}.strm'
    return os.path.join(season_dir, name)


def load_state():
    if os.path.isfile(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as stream:
                state = json.load(stream)
            return state if isinstance(state, dict) else {}
        except Exception:
            pass
    return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as stream:
        json.dump(st, stream, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="在数量/分页上限内更新已有元数据，保留 live")
    ap.add_argument("--limit", type=int, default=60, help="每个专区拉取条数")
    ap.add_argument("--max-pages", type=int, default=10, help="每区最多扫描页数（1—500）")
    ap.add_argument("--cats", type=str, default="", help="只跑指定专区，逗号分隔")
    args = ap.parse_args(argv)
    cats = [c.strip() for c in args.cats.split(',') if c.strip()] if args.cats else list(CATEGORIES)
    if args.limit < 1 or not 1 <= args.max_pages <= 500:
        ap.error('--limit 必须大于 0，--max-pages 必须在 1—500 之间')
    if not cats or any(cat not in CATEGORIES for cat in cats):
        ap.error('专区名称无效')

    os.makedirs(LOG_DIR, exist_ok=True)
    key = read_tmdb_key()
    log(f"TMDB Key 已从插件配置读取（长度 {len(key)}）")

    state = load_state()
    errors = []
    categories = {}

    def failure(cat, stage, exc, **identity):
        # Exception strings may contain a TMDB URL with its API key.
        errors.append(dict(cat=cat, stage=stage, error=type(exc).__name__, **identity))
        log(f'[{cat}] {stage} 失败 ({type(exc).__name__})，本轮将标记未完成')

    total_new = total_skip = total_eps = 0
    total_poster = total_fanart = 0

    for cat in cats:
        cfg = CATEGORIES[cat]
        media = cfg["media"]
        kind = cfg["kind"]
        cat_dir = os.path.join(CINEMA2, cat)
        os.makedirs(cat_dir, exist_ok=True)

        # 建立专区已有影视的 tmdbid 索引，从源头杜绝因 TMDB 官方译名变更产生多目录重复入库 (如《午夜规则》)
        existing_tmdb_ids = {}
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
                            existing_tmdb_ids[str(tid).strip()] = existing_folder
                    except Exception:
                        pass
                    break

        target_new = args.limit
        page, max_page, pages_scanned = 1, args.max_pages, 0
        new_cnt = skip_cnt = ep_cnt = 0
        poster_cnt = fanart_cnt = 0

        # 分页向后扫描：解决不翻页导致每日增量任务永远重复抓取前 20 条的严重 Bug
        while page <= max_page and new_cnt < target_new:
            path = f"/discover/{media}?{cfg['params'].lstrip('&')}&without_keywords={AI_EXCLUDE_KEYWORD_IDS}&page={page}"
            if cfg.get("exclude_genres"):
                path += f"&without_genres={cfg['exclude_genres']}"
            path += "&include_adult=true"
            url = f"https://api.themoviedb.org/3{path}&api_key={key}&language=zh-CN"
            try:
                data = tmdb_object(url, 'results')
                total_pages = int(data.get('total_pages', 1))
                if total_pages < 0:
                    raise ValueError('invalid total_pages')
            except Exception as e:
                failure(cat, 'discover', e, page=page)
                break
            pages_scanned = page

            raw_results = data.get("results", [])
            if not raw_results:
                break

            results = [r for r in raw_results if not is_blocked(cfg, r, cat=cat)]
            for item in results:
                if new_cnt >= target_new:
                    break

                item_id_str = str(item.get("id") or "").strip()
                if item_id_str and item_id_str in existing_tmdb_ids and not args.full:
                    skip_cnt += 1
                    continue

                title = sanitize(fmt_title(item))
                if not title or not item_id_str:
                    failure(cat, 'identity', ValueError('missing title or id'))
                    continue
                year = (fmt_date(item) or "0")[:4]
                if year.isdigit() and int(year) < CUR_YEAR and not args.full:
                    skip_cnt += 1
                    continue
                if kind == "movie":
                    folder = f"{title} ({year})" if year != "0" else title
                else:
                    folder = title
                folder = existing_tmdb_ids.get(item_id_str, folder)
                dir_path = os.path.join(cat_dir, folder)

                nfo = "movie.nfo" if kind == "movie" else "tvshow.nfo"
                nfo_path = os.path.join(dir_path, nfo)
                if os.path.isfile(nfo_path) and not args.full:
                    skip_cnt += 1
                    continue

                # 详情（拿 runtime/genres/status/seasons/keywords/networks）
                try:
                    detail = tmdb_object(
                        f"https://api.themoviedb.org/3/{media}/{item['id']}?api_key={key}&language=zh-CN&append_to_response=keywords",
                        'seasons' if kind != 'movie' else None
                    )
                    if str(detail.get('id')) != item_id_str:
                        raise ValueError('mismatched TMDB identity')
                except Exception as exc:
                    failure(cat, 'detail', exc, tmdb_id=item_id_str)
                    continue

                # 详情页二次阻断：拦截 TMDB 详情页中附带的 AI 关键词、短剧平台或微短剧超限集数
                if is_ai_content(item, detail, cat=cat):
                    log(f"    [跳过-AI/短剧过滤] 详情或关键词命中 AI剧/微短剧/劣质动漫: 《{title}》")
                    skip_cnt += 1
                    continue

                # 剧集校验：在创建目录与下载海报之前进行集数校验，坚决杜绝空目录与裂图泄露至海报墙
                is_updating = False
                today_str = datetime.date.today().isoformat()
                if kind != "movie":
                    seasons = [s for s in detail.get("seasons", []) if s.get("season_number", 0) >= 1]
                    if not seasons:
                        skip_cnt += 1
                        continue
                    show_eps = sum(s.get("episode_count") or 0 for s in seasons)
                    if show_eps > MAX_EPISODES_PER_SHOW:
                        log(f"    [跳过-集数超限] 《{title}》 共 {show_eps} 集，超过单剧上限 {MAX_EPISODES_PER_SHOW}")
                        skip_cnt += 1
                        continue

                    status = detail.get("status")
                    is_updating = (status != "Ended" and status != "Canceled")

                    # 连载更新中的剧集：若连第一集都未开播（无 last_episode_to_air），跳过创建，严禁创建空目录
                    if is_updating:
                        last_ep = detail.get("last_episode_to_air")
                        if not last_ep:
                            log(f"    [跳过-尚未开播] 连载剧集尚未开播/暂无已播出集数: 《{title}》")
                            skip_cnt += 1
                            continue
                    # Fetch before writing the title NFO: a transient season error
                    # must not leave a half-created title that later runs skip.
                    episode_maps = {}
                    try:
                        for season in seasons:
                            s_num = season['season_number']
                            s_data = tmdb_object(
                                f"https://api.themoviedb.org/3/tv/{item['id']}/season/{s_num}?api_key={key}&language=zh-CN",
                                'episodes')
                            episode_maps[s_num] = {ep['episode_number']: ep for ep in s_data['episodes']
                                                   if ep.get('episode_number')}
                            if season.get('episode_count') and not episode_maps[s_num]:
                                raise ValueError('empty season details')
                    except Exception as exc:
                        failure(cat, 'season', exc, tmdb_id=item_id_str)
                        continue

                os.makedirs(dir_path, exist_ok=True)

                if kind == "movie":
                    gen_movie_nfo(dir_path, item, detail)
                else:
                    gen_tv_nfo(dir_path, item, detail)
                new_cnt += 1
                if item_id_str:
                    existing_tmdb_ids[item_id_str] = folder

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
                            except Exception as exc:
                                failure(cat, 'strm_map', exc, tmdb_id=item_id_str)
                else:
                    # 剧集：按 TMDB 实际季集生成（跳过特别篇 season 0）
                    seasons = [s for s in detail.get("seasons", []) if s.get("season_number", 0) >= 1]
                    for season in seasons:
                        s_num = season.get("season_number")

                        # 拉取分季详情：获取单集名称、官方分集剧照 (still_path) 与单集剧情、播出日期
                        ep_info_map = episode_maps[s_num]

                        ep_total = season.get("episode_count") or 0
                        # 连载更新中的剧集只生成已播出的有效集数；完结剧集全部生成
                        valid_eps = []
                        for e in range(1, ep_total + 1):
                            ep_data = ep_info_map.get(e, {})
                            if is_updating and not is_episode_aired(ep_data, s_num, e, detail, today_str):
                                continue
                            valid_eps.append((e, ep_data))

                        if not valid_eps:
                            continue

                        s_dir = os.path.join(dir_path, f"Season {s_num:02d}")
                        os.makedirs(s_dir, exist_ok=True)
                        s_nfo = os.path.join(s_dir, "season.nfo")
                        if not os.path.isfile(s_nfo) or args.full:
                            gen_season_nfo(s_dir, item, detail, season)

                        for e, ep_data in valid_eps:
                            strm_path = episode_strm_path(s_dir, title, s_num, e)
                            base_ep = os.path.basename(strm_path)[:-5]
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
                                    except Exception as exc:
                                        failure(cat, 'strm_map', exc, tmdb_id=item_id_str)
                # 轻微限速，防 TMDB 429
                time.sleep(0.08)

            if page >= total_pages:
                break
            page += 1

        categories[cat] = {'pages': pages_scanned, 'new': new_cnt, 'skipped': skip_cnt, 'strms': ep_cnt}
        log(f"[{cat}] 新增/更新 {new_cnt} 条 (实查 {pages_scanned} 页)，已存在跳过 {skip_cnt} 条，海报 {poster_cnt} 张，背景 {fanart_cnt} 张，占位 strm {ep_cnt} 个")
        total_new += new_cnt
        total_skip += skip_cnt
        total_eps += ep_cnt
        total_poster += poster_cnt
        total_fanart += fanart_cnt

    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    state["last_new"] = total_new
    state["last_eps"] = total_eps
    state['status'] = 'partial' if errors else 'complete'
    state['error_count'] = len(errors)
    state['errors'] = errors[:50]
    state['categories'] = categories
    save_state(state)

    log(f"{state['status']}：新增/更新 {total_new} 条，跳过 {total_skip} 条，海报 {total_poster} 张，背景 {total_fanart} 张，占位 strm {total_eps} 个，失败 {len(errors)} 项")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
