#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Optional traditional-library synchronizer. Uses configured source/library paths, OpenList and Jellyfin APIs. Host networking and credentials must be explicitly supplied; this module never edits hosts."""

from quark_vod.integrations.hometheater import paths
from quark_vod import config as host_config

import os
import re
import urllib.parse
import urllib.request
import json
import time
import sys
import subprocess
import shutil
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from quark_vod.integrations.hometheater.ai_extractor import (
    resolve_entries_smart, generate_nfo_file, fallback_regex_parse,
    extract_episode_smart, natural_sort_key, is_ad_or_junk_path,
    get_cached_item, clean_orphaned_cached_items, get_existing_media_raw_names
)


BASE_DIR = paths.BASE_DIR
SRC_BASE = paths.SOURCE_DIR
VIDEO_ROOT = paths.VIDEO_ROOT
CINEMA_BASE = paths.CINEMA_DIR
JELLYFIN_ROOT = os.path.join(paths.JELLYFIN_CONFIG, "root/default")
OPENLIST_BASE_URL = host_config._value("OPENLIST_BASE_URL", __import__("quark_vod.config", fromlist=["OPENLIST_PUBLIC"]).OPENLIST_PUBLIC + "/d/")
OPENLIST_LOCAL_API = host_config._value("OPENLIST_LOCAL_API", host_config.OPENLIST_API)
OPENLIST_REMOTE_API = host_config._value("OPENLIST_REMOTE_API", __import__("quark_vod.config", fromlist=["OPENLIST_API"]).OPENLIST_API)
OPENLIST_API_CANDIDATES = [OPENLIST_LOCAL_API, OPENLIST_REMOTE_API]
CURRENT_OPENLIST_API = OPENLIST_LOCAL_API
OPENLIST_ADMIN_USER = host_config._value("OPENLIST_ADMIN_USER", "admin")
OPENLIST_ADMIN_PASS = host_config.OPENLIST_ADMIN_PASS
JELLYFIN_KEY = host_config._secret("JELLYFIN_KEY")
JELLYFIN_API_BASE = host_config._value("JELLYFIN_API_BASE", paths.TRADITIONAL_API)

SERIES_CATEGORIES = {"国产剧集", "热门韩剧", "欧美剧集", "国产动漫"}
MOVIE_CATEGORIES = {"国产电影", "港台经典", "欧美大片", "韩国电影"}
HYBRID_CATEGORIES = {"日本动漫"}
ALL_CATEGORIES = [
    "国产电影", "港台经典", "欧美大片", "韩国电影",
    "国产剧集", "热门韩剧", "欧美剧集", "国产动漫", "日本动漫"
]
SERIES_TO_MOVIE_CAT = {
    "欧美剧集": "欧美大片",
    "国产剧集": "国产电影",
    "热门韩剧": "韩国电影",
    "国产动漫": "国产动漫",
    "日本动漫": "日本动漫",
}
MOVIE_TO_SERIES_SOURCE = {
    "欧美大片": "欧美剧集",
    "国产电影": "国产剧集",
    "韩国电影": "热门韩剧",
}

YEAR_REGEX = r'(?<!\d)(19\d\d|20\d\d)(?!\d)'

def get_openlist_token():
    """获取 OpenList (Alist) 管理员身份 Token"""
    global CURRENT_OPENLIST_API
    for api_base in OPENLIST_API_CANDIDATES:
        try:
            url = f"{api_base}/api/auth/login"
            payload = json.dumps({
                "username": OPENLIST_ADMIN_USER,
                "password": OPENLIST_ADMIN_PASS
            }).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("code") == 200:
                    CURRENT_OPENLIST_API = api_base
                    return data.get("data", {}).get("token", "")
        except Exception:
            continue
    return ""

def refresh_openlist_cache(token=""):
    """主动穿透刷新 OpenList 夸克存储缓存，确保获取云端网盘最新真实文件列表"""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = token

    paths_to_refresh = ["/夸克", "/夸克/视频"]
    for cat in ALL_CATEGORIES:
        paths_to_refresh.append(f"/夸克/视频/{cat}")

    def _refresh_one(p):
        try:
            url = f"{CURRENT_OPENLIST_API}/api/fs/list"
            payload = json.dumps({
                "path": p,
                "password": "",
                "page": 1,
                "per_page": 1,
                "refresh": True
            }).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                d = json.loads(resp.read().decode("utf-8"))
                if d.get("code") == 200:
                    return 1
        except Exception:
            pass
        return 0

    refreshed_count = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        for res in pool.map(_refresh_one, paths_to_refresh):
            refreshed_count += res
    if refreshed_count > 0:
        print(f"[云端刷新] 成功向 OpenList 发送穿透刷新请求，已实时更新 {refreshed_count} 个网盘专区目录缓存")

def get_openlist_cloud_files(token=""):
    """递归获取 OpenList 云端真实存在的视频文件相对路径集合，用于击穿本地 rclone 缓存"""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = token

    cloud_files = set()
    video_exts = ('.mkv', '.mp4', '.ts', '.iso', '.mov')
    ignore_keywords = ["扫码", "关注", "防走失", "最新发布", "样片"]
    scan_errors = []

    def fetch_dir(path):
        items = None
        for attempt in range(2):
            try:
                url = f"{CURRENT_OPENLIST_API}/api/fs/list"
                payload = json.dumps({
                    "path": path,
                    "password": "",
                    "page": 1,
                    "per_page": 0,
                    "refresh": False
                }).encode("utf-8")
                req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    d = json.loads(resp.read().decode("utf-8"))
                    if d.get("code") == 200:
                        items = d.get("data", {}).get("content", []) or []
                        break
            except Exception as ex:
                if attempt == 1:
                    scan_errors.append((path, str(ex)))
                time.sleep(0.3)
        return path, items

    try:
        pending_dirs = ["/夸克/视频"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            while pending_dirs:
                next_dirs = []
                futures = [pool.submit(fetch_dir, p) for p in pending_dirs]
                for fut in as_completed(futures):
                    path, items = fut.result()
                    if items is None:
                        continue
                    for it in items:
                        sub_name = it.get("name", "")
                        sub_path = f"{path.rstrip('/')}/{sub_name}"
                        if it.get("is_dir"):
                            next_dirs.append(sub_path)
                        else:
                            if sub_name.lower().endswith(video_exts):
                                if not any(k in sub_name for k in ignore_keywords):
                                    rel = sub_path
                                    if rel.startswith("/夸克"):
                                        rel = rel[len("/夸克"):]
                                    cloud_files.add(rel)
                pending_dirs = next_dirs
        if scan_errors:
            print(f"[云端直连] 警告: 扫描过程有 {len(scan_errors)} 个目录获取失败或超时，已开启安全保护机制")
        if cloud_files:
            print(f"[云端直连] 已通过 OpenList API 实时获取云端 {len(cloud_files)} 个真实视频文件索引")
            return cloud_files, (len(scan_errors) == 0)
    except Exception:
        pass
    return None, False

def clean_movie_title(raw_name, parent_hint=""):
    """清洗电影文件名或目录名中的技术标签、发布组、广告等，输出 '片名 (年份)'"""
    base_name = os.path.basename(raw_name)
    parent_base = os.path.basename(parent_hint) if parent_hint else ""

    # 优先查库：支持 Web 端人工覆盖及 AI 缓存动态接管，无缓存时走下方兜底清洗规则
    # 注意：若 parent_base 是系列合集目录（含合集/系列/Trilogy等），单片视频文件绝不能错误继承合集总称
    try:
        is_parent_collection = bool(parent_base and re.search(r'合集|系列|全集|Collection|Trilogy|\[\d+-\d+部\]', parent_base))
        cached = get_cached_item(base_name) or (get_cached_item(parent_base) if (parent_base and not is_parent_collection) else None)
        if cached and cached.get("clean_title"):
            c_title = re.sub(r'[\\/*?:"<>|]', '', str(cached["clean_title"])).strip()
            c_year = cached.get("year")
            if c_year and str(c_year) not in c_title:
                return f"{c_title} ({c_year})"
            return c_title
    except Exception:
        pass

    s = os.path.splitext(base_name)[0].replace(chr(160), ' ').strip()

    # 针对已知的特殊简写与系列合集进行确定性校准
    if re.search(r'(?i)^(N-H!?2|NH2[._\-\s]*4K)$', s) or re.search(r'(?i)^(N-H!?2|NH2[._\-\s]*4K)$', parent_base):
        return "年会不能停2 (2026)"
    if '鸣鸟与蛇之歌' in s or '鸣鸟与蛇之歌' in parent_base:
        return "饥饿游戏：鸣鸟与蛇之歌 (2023)"
    if '嘲笑鸟' in s or '嘲笑鸟' in parent_base:
        if ('下' in s or '下' in parent_base or re.search(r'(?i)part[._\s]*2', s + ' ' + parent_base)):
            return "饥饿游戏3：嘲笑鸟(下) (2015)"
        elif ('上' in s or '上' in parent_base or re.search(r'(?i)part[._\s]*1', s + ' ' + parent_base)):
            return "饥饿游戏3：嘲笑鸟(上) (2014)"
    if ('饥饿游戏2' in s or '饥饿游戏2' in parent_base or re.search(r'(?i)catching[._\s]*fire', s + ' ' + parent_base)):
        return "饥饿游戏2：星火燎原 (2013)"
    if ('饥饿游戏' in s or '饥饿游戏' in parent_base or re.search(r'(?i)hunger[._\s]*games', s + ' ' + parent_base)):
        if not re.search(r'饥饿游戏[2345]|嘲笑鸟|星火燎原|鸣鸟与蛇', s + ' ' + parent_base):
            return "饥饿游戏 (2012)"

    # 毒液系列校准
    if re.search(r'(?i)(venom.*last.*dance|最后一舞|毒液\s*3|venom\s*3)', s):
        return "毒液：最后一舞 (2024)"
    if re.search(r'(?i)(venom.*carnage|屠杀开始|毒液\s*2|venom\s*2)', s):
        return "毒液2：屠杀开始 (2021)"
    if re.search(r'(?i)(\bvenom\b|毒液)', s):
        if not re.search(r'(?i)(carnage|last.*dance|屠杀|最后一舞|合集|系列|\b[23]\b|[23]部|第[23]部)', s):
            return "毒液：致命守护者 (2018)"

    # 复仇者联盟系列校准
    combined_av = f"{s} {parent_base}"
    if re.search(r'(?i)(endgame|终局之战|复仇者联盟\s*4)', combined_av):
        return "复仇者联盟4：终局之战 (2019)"
    if re.search(r'(?i)(infinity[._\s]*war|无限战争|复仇者联盟\s*3)', combined_av):
        return "复仇者联盟3：无限战争 (2018)"
    if re.search(r'(?i)(age[._\s]*of[._\s]*ultron|奥创纪元|复仇者联盟\s*2)', combined_av):
        return "复仇者联盟2：奥创纪元 (2015)"
    if re.search(r'(?i)(the[._\s]*avengers|复仇者联盟\s*1)', combined_av):
        return "复仇者联盟 (2012)"

    is_parent_collection = bool(re.search(r'(?i)(合集|系列|部曲|collection|trilogy|boxset|quadrilogy|\b[1-9]-[1-9]部\b)', parent_base))
    has_cn_parent = bool(re.search(r'[\u4e00-\u9fa5]', parent_base))
    has_cn_s = bool(re.search(r'[\u4e00-\u9fa5]', s))
    src_str = parent_base if (has_cn_parent and not has_cn_s and not is_parent_collection) else s

    # 提取年份（优先从清洗源字符串提取，若无则从文件名与父级目录名提取）
    m_year = re.search(YEAR_REGEX, src_str)
    if not m_year and s:
        m_year = re.search(YEAR_REGEX, s)
    if not m_year and parent_base:
        m_year = re.search(YEAR_REGEX, parent_base)
    year = m_year.group(1) if m_year else ""

    # 去除方括号、尖括号、特殊符号内的文字
    s_clean = re.sub(r'\[.*?\]|\【.*?\】|\(.*?\)|\（.*?\）|\{.*?\}', ' ', src_str)
    # 剔除群号、微信号、公众号与营销导流词
    s_clean = re.sub(r'(?i)(?:加[微v]|关注|防失联|首发|交流|汁源|资源|QQ|微信).*?群', ' ', s_clean)
    s_clean = re.sub(r'(?i)(?:QQ群|微信群|交流群|资源群|防失联群|进群|加群|扫码|汁源[裙群]|关注公[众眾]号|公众号|群号)[:：\s]*\w+', ' ', s_clean)
    s_clean = re.sub(r'(?i)[QqＱｑ][QqＱｑ](?:群|号)?[:：\s]*\d+', ' ', s_clean)

    # 过滤技术规格标签
    s_clean = re.sub(r'(?i)(?<![a-zA-Z0-9])(4k|2160p|1080p|720p|hdr10plus|hdr10\+|hdr10|hdr|dovi|dv|remux|bluray|blu-ray|web-dl|webrip|x264|x265|hevc|h264|h265|avc|aac|dts-hd|dts-x|dts|truehd|atmos|ddp5\.1|ddp|pcm|60fps|120fps|colorTV|wuke|fgt|yuntv|hmxz|my|10bit|8bit|2audio|3audio|4audio|3audios|muhd|frds|wiki|chd|hds|beast|cmct|ade|hqc|cinefile|rarbg|yify|uhd|proper|repack|swtyblz|nogrp|iamable|flux|telly|hamiltnshare)(?![a-zA-Z0-9])', ' ', s_clean)
    s_clean = re.sub(r'(?i)(蓝光原盘|remux原盘|原盘|杜比视界|杜比全景声|全景声|国英双音轨|国英音轨|国粤双语|国粤双音轨|国语音轨|粤语音轨|双音轨|多音轨|特效字幕|内封特效字幕|内封简繁英双语特效字幕|外挂字幕|外挂简中|外挂双语|简繁英字幕|中英双字|国配中字|官译中字|简繁英特效字幕|内封简繁特效字幕|中文字幕|双语字幕|官方中字|简繁字幕|嵌入中字|内封.*?字幕|目前最高画质版本|AI4K修复|典藏版|加长典藏版|加长版|国语粤语日语|韩语中字|英语中字|高码率|高码)', ' ', s_clean)

    # 截取年份前的片名
    if year:
        parts = re.split(YEAR_REGEX, s_clean, maxsplit=1)
        main_title = parts[0].strip(' ._-&+~')
    else:
        main_title = s_clean.strip(' ._-&+~')

    # 去除首部无意义标识（如 X.、l~、沈腾2018 等）
    main_title = re.sub(r'^[Xxl~\.\-\s]+', '', main_title)
    main_title = re.sub(r'^(沈腾|周星驰|漫威)\s*', '', main_title)
    main_title = re.sub(r'(?:^|\s)[&+~～]+(?:\s|$)', ' ', main_title)
    main_title = re.sub(r'[._\-\s]+', ' ', main_title).strip(' &_+-')

    # 针对部分特殊片名优化
    main_title = main_title.replace('Du Sheng圣', '赌圣').replace('Du.Sheng圣', '赌圣')

    # 若清洗后为空，尝试从 parent_base 提取片名
    if not main_title and parent_base:
        p_clean = re.sub(r'\[.*?\]|\【.*?\】|\(.*?\)|\（.*?\）|\{.*?\}', ' ', parent_base)
        p_clean = re.sub(r'(?i)\b(4k|2160p|1080p|720p|hdr10|hdr|dv|remux|bluray|blu-ray|web-dl|webrip|x264|x265|hevc|h264|h265|aac|dts-hd|dts|truehd|atmos|60fps|colorTV|wuke|fgt|yuntv|hmxz|my|10bit|8bit|2audio|4audio|muhd|frds|wiki|chd|hds|beast|cmct|ade|hqc|cinefile|rarbg|yify|uhd)\b', ' ', p_clean)
        p_clean = re.sub(YEAR_REGEX, ' ', p_clean)
        main_title = re.sub(r'[._\-\s]+', ' ', p_clean).strip()

    if not main_title:
        main_title = re.sub(r'[\[\]\【\】\(\)\（\）]', ' ', s).strip()

    # 清除路径非法字符
    main_title = re.sub(r'[\\/*?:"<>|]', '', main_title).strip()

    if year and year not in main_title:
        return f"{main_title} ({year})"
    return main_title

def clean_show_title(entry_name):
    """清洗剧集目录名，输出 '剧集名 (年份)'"""
    base_name = os.path.basename(entry_name)
    s = base_name.replace(chr(160), ' ').strip()
    s = re.sub(r'^[Xxl~\.\-\s]+', '', s)
    m_year = re.search(YEAR_REGEX, s)
    year = m_year.group(1) if m_year else ""
    clean = re.sub(r'[(（\[【].*', '', s).strip()
    clean = re.sub(r'(?i)(?:加[微v]|关注|防失联|首发|交流|汁源|资源|QQ|微信).*?群', ' ', clean)
    clean = re.sub(r'(?i)(?:QQ群|微信群|交流群|资源群|防失联群|进群|加群|扫码|汁源[裙群]|关注公[众眾]号|公众号|群号)[:：\s]*\w+', ' ', clean)
    clean = re.sub(r'(?i)[QqＱｑ][QqＱｑ](?:群|号)?[:：\s]*\d+', ' ', clean)
    clean = re.sub(r'(?i)\b(4k|2160p|1080p|720p|hdr10|hdr|dv|remux|bluray|blu-ray|web-dl|webrip|x264|x265|265|hevc|h264|h265|aac|dts-hd|dts|truehd|atmos|60fps|colorTV|wuke|fgt|yuntv|hmxz|my|nf)\b', ' ', clean)
    clean = re.sub(YEAR_REGEX, ' ', clean)
    clean = re.sub(r'[._\-\s]+', ' ', clean).strip()
    clean = re.sub(r'[\\/*?:"<>|]', '', clean).strip()
    if not clean:
        clean = s
    if year and year not in clean:
        return f"{clean} ({year})"
    return clean

def find_video_files(path, cloud_files=None):
    """查找目录下所有视频文件，过滤明显样片和广告"""
    res = []
    if not os.path.exists(path):
        return res
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not is_ad_or_junk_path(d)]
        for f in sorted(files):
            if f.lower().endswith(('.mkv', '.mp4', '.ts', '.iso', '.mov')):
                if not is_ad_or_junk_path(f) and not is_ad_or_junk_path(os.path.join(root, f)):
                    full_p = os.path.join(root, f)
                    if cloud_files is not None:
                        rel_p = full_p.replace(SRC_BASE, "")
                        if rel_p not in cloud_files:
                            continue
                    res.append(full_p)
    return res

def make_strm_file(target_file, rel_path, incremental=False):
    """创建 .strm 直链指向 OpenList 本地代理"""
    quoted_rel = urllib.parse.quote(rel_path)
    stream_url = f"{OPENLIST_BASE_URL}{quoted_rel}\n"
    if os.path.exists(target_file):
        try:
            with open(target_file, "r", encoding="utf-8") as f:
                if f.read() == stream_url:
                    return False
        except Exception:
            pass
    os.makedirs(os.path.dirname(target_file), exist_ok=True)
    with open(target_file, "w", encoding="utf-8") as f:
        f.write(stream_url)
    return True

def sync_movies(cat_name, cat_dir, cloud_files=None, is_complete=True, incremental=False):
    """同步电影专区，递归支持独立单片、合集嵌套子目录及平铺独立电影"""
    dest_dir = os.path.join(CINEMA_BASE, cat_name)
    os.makedirs(dest_dir, exist_ok=True)
    count = 0
    new_count = 0
    skip_count = 0
    deleted_count = 0
    valid_dirs = set()
    valid_strm_files = set()

    def record_strm(target_file, rel_path):
        nonlocal new_count, skip_count
        created = make_strm_file(target_file, rel_path, incremental=incremental)
        if created:
            new_count += 1
        else:
            skip_count += 1
        return created

    cat_cloud_prefix = f"/视频/{cat_name}/"
    cloud_entries = set()
    cloud_files_by_entry = {}
    if cloud_files:
        for f_rel in cloud_files:
            if f_rel.startswith(cat_cloud_prefix):
                sub = f_rel[len(cat_cloud_prefix):]
                top_name = sub.split('/')[0]
                cloud_entries.add(top_name)
                if top_name not in cloud_files_by_entry:
                    cloud_files_by_entry[top_name] = []
                cloud_files_by_entry[top_name].append(SRC_BASE + f_rel)

    use_cloud_only = bool(is_complete and cloud_files is not None)
    local_entries = set() if use_cloud_only else (set(os.listdir(cat_dir)) if os.path.exists(cat_dir) else set())
    all_entries = sorted(local_entries | cloud_entries)
    ai_results = resolve_entries_smart(list(all_entries), category_hint=cat_name)

    items_to_process = []
    for entry in all_entries:
        items_to_process.append((entry, cat_name, os.path.join(cat_dir, entry), cat_cloud_prefix, cloud_files_by_entry.get(entry, []), ai_results.get(entry)))

    # 跨分类智能捕获：检查对应剧集专区中是否误入了电影或合集条目（例如 欧美剧集 下的 饥饿游戏五部曲）
    series_source_cat = MOVIE_TO_SERIES_SOURCE.get(cat_name)
    if series_source_cat:
        s_cat_dir = os.path.join(VIDEO_ROOT, series_source_cat)
        s_cloud_prefix = f"/视频/{series_source_cat}/"
        s_cloud_entries = set()
        s_cloud_files_by_entry = {}
        if cloud_files:
            for f_rel in cloud_files:
                if f_rel.startswith(s_cloud_prefix):
                    sub = f_rel[len(s_cloud_prefix):]
                    top_name = sub.split('/')[0]
                    s_cloud_entries.add(top_name)
                    if top_name not in s_cloud_files_by_entry:
                        s_cloud_files_by_entry[top_name] = []
                    s_cloud_files_by_entry[top_name].append(SRC_BASE + f_rel)
        s_local_entries = set() if use_cloud_only else (set(os.listdir(s_cat_dir)) if os.path.exists(s_cat_dir) else set())
        s_all_entries = sorted(s_local_entries | s_cloud_entries)
        if s_all_entries:
            s_ai_results = resolve_entries_smart(list(s_all_entries), category_hint=series_source_cat)
            for s_e in s_all_entries:
                s_info = s_ai_results.get(s_e) or fallback_regex_parse(s_e)
                if s_info.get("media_type") in ("movie", "collection") or s_info.get("is_collection"):
                    # 双重校验：若该条目包含明确的剧集特征（如 S01E01 或分集），坚决不应作为电影接入电影专区
                    sample_files = s_cloud_files_by_entry.get(s_e, [])
                    if not sample_files and os.path.isdir(os.path.join(s_cat_dir, s_e)):
                        sample_files = find_video_files(os.path.join(s_cat_dir, s_e), None)
                    if any(extract_episode_smart(os.path.basename(vf))[1] is not None for vf in sample_files):
                        continue
                    print(f"[{cat_name}] 智能接入跨专区电影/合集条目: {series_source_cat}/{s_e}")
                    items_to_process.append((s_e, series_source_cat, os.path.join(s_cat_dir, s_e), s_cloud_prefix, s_cloud_files_by_entry.get(s_e, []), s_info))

    for entry, src_cat, entry_path, src_cloud_prefix, entry_cloud_files, ai_info_item in items_to_process:
        if is_ad_or_junk_path(entry) or (ai_info_item and ai_info_item.get("is_ad")):
            print(f"[{cat_name}] [AI拦截广告] 已跳过广告导流条目: {entry}")
            continue

        ai_info = ai_info_item or fallback_regex_parse(entry)
        c_title = ai_info.get("title") or entry
        c_year = ai_info.get("year")
        movie_name = f"{c_title} ({c_year})" if c_year else c_title

        # 若是跨专区调度的条目，先行清理其在原剧集目录中错误生成的剧集目录
        if src_cat != cat_name:
            for bogus in [entry, c_title, "饥饿游戏"]:
                b_dir = os.path.join(CINEMA_BASE, src_cat, bogus)
                if os.path.isdir(b_dir):
                    try:
                        shutil.rmtree(b_dir)
                        print(f"[{cat_name}] 清除跨专区错误归类目录: {src_cat}/{bogus}")
                    except Exception:
                        pass

        is_direct_video_entry = entry.lower().endswith(('.mkv', '.mp4', '.ts', '.iso', '.mov')) and (
            (use_cloud_only and f"{src_cloud_prefix}{entry}" in cloud_files)
            or (not use_cloud_only and (os.path.isfile(entry_path) or not os.path.isdir(entry_path)))
        )
        if is_direct_video_entry:
            if is_complete and cloud_files is not None:
                rel_check = f"{src_cloud_prefix}{entry}"
                if rel_check not in cloud_files:
                    continue
            movie_folder = os.path.join(dest_dir, movie_name)
            strm_path = os.path.join(movie_folder, f"{movie_name}.strm")
            rel = f"{src_cloud_prefix}{entry}"
            record_strm(strm_path, rel)
            generate_nfo_file(movie_folder, title=c_title, year=c_year, media_type="movie", original_title=entry, overwrite=True)
            valid_dirs.add(movie_folder)
            valid_strm_files.add(strm_path)
            count += 1
            continue

        # 目录形式的条目（可能是单片嵌套在子文件夹中、单片平铺、或多片合集）
        subdirs = [] if use_cloud_only else ([d for d in os.listdir(entry_path) if os.path.isdir(os.path.join(entry_path, d))] if os.path.isdir(entry_path) else [])
        for cf in entry_cloud_files:
            rel = cf.replace(SRC_BASE + src_cloud_prefix + entry + "/", "")
            if "/" in rel:
                subdirs.append(rel.split("/")[0])
        subdirs = sorted(set(subdirs))

        subdirs_with_videos = []
        for d in subdirs:
            sd_path = os.path.join(entry_path, d)
            disk_vf = [] if use_cloud_only else (find_video_files(sd_path, cloud_files if is_complete else None) if os.path.isdir(sd_path) else [])
            cloud_sd_prefix = SRC_BASE + src_cloud_prefix + entry + "/" + d + "/"
            cloud_vf = [f for f in entry_cloud_files if f.startswith(cloud_sd_prefix)]
            merged_vf = sorted(set(disk_vf) | set(cloud_vf))
            if is_complete and cloud_files is not None:
                merged_vf = [f for f in merged_vf if f.replace(SRC_BASE, "") in cloud_files]
            if len(merged_vf) > 0:
                subdirs_with_videos.append((d, merged_vf))

        root_disk_vf = [] if use_cloud_only else ([os.path.join(entry_path, f) for f in os.listdir(entry_path)
                        if os.path.isfile(os.path.join(entry_path, f)) and f.lower().endswith(('.mkv', '.mp4', '.ts', '.iso', '.mov'))
                        and not is_ad_or_junk_path(f)] if os.path.isdir(entry_path) else [])
        root_cloud_vf = [f for f in entry_cloud_files if "/" not in f.replace(SRC_BASE + src_cloud_prefix + entry + "/", "")]
        root_v_files = sorted(set(root_disk_vf) | set(root_cloud_vf))
        if is_complete and cloud_files is not None:
            root_v_files = [f for f in root_v_files if f.replace(SRC_BASE, "") in cloud_files]

        total_sub_videos = sum(len(vfs) for _, vfs in subdirs_with_videos)
        total_all_videos = len(root_v_files) + total_sub_videos

        if total_all_videos == 0:
            continue

        is_collection = bool(ai_info.get("is_collection") or ai_info.get("media_type") == "collection")
        is_single_movie = (total_all_videos == 1 and not is_collection)

        if is_single_movie:
            vf = root_v_files[0] if root_v_files else subdirs_with_videos[0][1][0]
            movie_folder = os.path.join(dest_dir, movie_name)
            strm_path = os.path.join(movie_folder, f"{movie_name}.strm")
            rel = vf.replace(SRC_BASE, "")
            record_strm(strm_path, rel)
            generate_nfo_file(movie_folder, title=c_title, year=c_year, media_type="movie", original_title=entry, overwrite=True)
            valid_dirs.add(movie_folder)
            valid_strm_files.add(strm_path)
            count += 1
        else:
            all_videos = root_v_files + [vf for _, vfs in subdirs_with_videos for vf in vfs]
            is_cd_parts = (not is_collection and len(all_videos) > 1 and all(re.search(r'(?i)\b(cd|part|disc)\s*[0-9]', os.path.basename(vf)) for vf in all_videos))

            if is_cd_parts:
                movie_name = clean_movie_title(entry)
                movie_folder = os.path.join(dest_dir, movie_name)
                for idx, vf in enumerate(all_videos, 1):
                    strm_path = os.path.join(movie_folder, f"{movie_name} - Part{idx}.strm")
                    rel = vf.replace(SRC_BASE, "")
                    record_strm(strm_path, rel)
                    valid_strm_files.add(strm_path)
                clean_m_t = movie_name.split(' (')[0]
                m_y = re.search(r'\((\d{4})\)$', movie_name)
                sub_y = int(m_y.group(1)) if m_y else None
                generate_nfo_file(movie_folder, title=clean_m_t, year=sub_y, media_type="movie", original_title=entry, overwrite=True)
                valid_dirs.add(movie_folder)
                count += 1
            else:
                # 遍历子目录内的各部电影
                for sd, v_files in subdirs_with_videos:
                    cached_sd = get_cached_item(sd)
                    if len(v_files) == 1:
                        vf = v_files[0]
                        if cached_sd and cached_sd.get("clean_title"):
                            sub_t = cached_sd["clean_title"]
                            sub_y = cached_sd.get("year")
                            sub_movie_name = f"{sub_t} ({sub_y})" if sub_y else sub_t
                        else:
                            cached_vf = get_cached_item(os.path.basename(vf))
                            if cached_vf and cached_vf.get("clean_title"):
                                sub_t = cached_vf["clean_title"]
                                sub_y = cached_vf.get("year")
                                sub_movie_name = f"{sub_t} ({sub_y})" if sub_y else sub_t
                            else:
                                sub_movie_name = clean_movie_title(vf, parent_hint=sd)
                                sub_t = sub_movie_name.split(' (')[0]
                                m_y = re.search(r'\((\d{4})\)$', sub_movie_name)
                                sub_y = int(m_y.group(1)) if m_y else None
                        movie_folder = os.path.join(dest_dir, sub_movie_name)
                        strm_path = os.path.join(movie_folder, f"{sub_movie_name}.strm")
                        rel = vf.replace(SRC_BASE, "")
                        record_strm(strm_path, rel)
                        generate_nfo_file(movie_folder, title=sub_t, year=sub_y, media_type="movie", original_title=os.path.basename(vf), overwrite=True)
                        valid_dirs.add(movie_folder)
                        valid_strm_files.add(strm_path)
                        count += 1
                    elif len(v_files) > 1:
                        sub_is_parts = all(re.search(r'(?i)\b(cd|part|disc)\s*[0-9]', os.path.basename(vf)) for vf in v_files)
                        if sub_is_parts:
                            if cached_sd and cached_sd.get("clean_title"):
                                sub_t = cached_sd["clean_title"]
                                sub_y = cached_sd.get("year")
                                sub_movie_name = f"{sub_t} ({sub_y})" if sub_y else sub_t
                            else:
                                sub_movie_name = clean_movie_title(sd)
                                sub_t = sub_movie_name.split(' (')[0]
                                m_y = re.search(r'\((\d{4})\)$', sub_movie_name)
                                sub_y = int(m_y.group(1)) if m_y else None
                            movie_folder = os.path.join(dest_dir, sub_movie_name)
                            for idx, vf in enumerate(v_files, 1):
                                strm_path = os.path.join(movie_folder, f"{sub_movie_name} - Part{idx}.strm")
                                rel = vf.replace(SRC_BASE, "")
                                record_strm(strm_path, rel)
                                valid_strm_files.add(strm_path)
                            generate_nfo_file(movie_folder, title=sub_t, year=sub_y, media_type="movie", original_title=sd, overwrite=True)
                            valid_dirs.add(movie_folder)
                            count += 1
                        else:
                            for vf in v_files:
                                cached_vf = get_cached_item(os.path.basename(vf))
                                if cached_vf and cached_vf.get("clean_title"):
                                    sub_t = cached_vf["clean_title"]
                                    sub_y = cached_vf.get("year")
                                    sub_movie_name = f"{sub_t} ({sub_y})" if sub_y else sub_t
                                else:
                                    sub_movie_name = clean_movie_title(vf, parent_hint=sd)
                                    sub_t = sub_movie_name.split(' (')[0]
                                    m_y = re.search(r'\((\d{4})\)$', sub_movie_name)
                                    sub_y = int(m_y.group(1)) if m_y else None
                                movie_folder = os.path.join(dest_dir, sub_movie_name)
                                strm_path = os.path.join(movie_folder, f"{sub_movie_name}.strm")
                                rel = vf.replace(SRC_BASE, "")
                                record_strm(strm_path, rel)
                                generate_nfo_file(movie_folder, title=sub_t, year=sub_y, media_type="movie", original_title=os.path.basename(vf), overwrite=True)
                                valid_dirs.add(movie_folder)
                                valid_strm_files.add(strm_path)
                                count += 1

                # 遍历直接位于合集根目录下的视频文件（例如 饥饿游戏4.鸣鸟与蛇之歌）
                for vf in root_v_files:
                    cached_vf = get_cached_item(os.path.basename(vf))
                    if cached_vf and cached_vf.get("clean_title"):
                        sub_t = cached_vf["clean_title"]
                        sub_y = cached_vf.get("year")
                        sub_movie_name = f"{sub_t} ({sub_y})" if sub_y else sub_t
                    else:
                        sub_movie_name = clean_movie_title(vf, parent_hint=entry)
                        sub_t = sub_movie_name.split(' (')[0]
                        m_y = re.search(r'\((\d{4})\)$', sub_movie_name)
                        sub_y = int(m_y.group(1)) if m_y else None
                    movie_folder = os.path.join(dest_dir, sub_movie_name)
                    strm_path = os.path.join(movie_folder, f"{sub_movie_name}.strm")
                    rel = vf.replace(SRC_BASE, "")
                    record_strm(strm_path, rel)
                    generate_nfo_file(movie_folder, title=sub_t, year=sub_y, media_type="movie", original_title=os.path.basename(vf), overwrite=True)
                    valid_dirs.add(movie_folder)
                    valid_strm_files.add(strm_path)
                    count += 1

    allow_deletion = (is_complete and cloud_files is not None) or (cloud_files is None and len(local_entries) > 0)
    if allow_deletion:
        # 1. 深度清理有效电影目录内已被删除或更名的旧版失效 .strm 文件
        for m_dir in valid_dirs:
            if os.path.isdir(m_dir):
                for old_f in os.listdir(m_dir):
                    if old_f.endswith(".strm"):
                        full_strm = os.path.join(m_dir, old_f)
                        if full_strm not in valid_strm_files:
                            try:
                                os.remove(full_strm)
                                deleted_count += 1
                                print(f"[{cat_name}] 清除电影失效版本文件: {os.path.basename(m_dir)}/{old_f}")
                            except Exception:
                                pass

        # 2. 清理已被用户从网盘删除的整部电影目录
        for item in os.listdir(dest_dir):
            full_item = os.path.join(dest_dir, item)
            if os.path.isdir(full_item) and full_item not in valid_dirs:
                try:
                    shutil.rmtree(full_item)
                    deleted_count += 1
                    print(f"[{cat_name}] 清除已删除电影条目目录: {item}")
                except Exception:
                    pass
    else:
        if cloud_files is None or not is_complete:
            print(f"[{cat_name}] 云端索引未完全校验，保护现有本地电影目录不被误删")

    print(f"[{cat_name}] 处理完成，生成/更新 {count} 部独立电影，清理 {deleted_count} 个失效条目")
    return count, new_count, skip_count, deleted_count

def sync_series(cat_name, cat_dir, cloud_files=None, is_complete=True, incremental=False):
    """同步电视剧集专区，智能提取集数、画质择优、处理特别篇并入库"""
    dest_dir = os.path.join(CINEMA_BASE, cat_name)
    os.makedirs(dest_dir, exist_ok=True)
    show_count = 0
    ep_count = 0
    new_count = 0
    skip_count = 0
    deleted_count = 0
    valid_show_dirs = set()
    allow_deletion = (is_complete and cloud_files is not None)

    def record_strm(target_file, rel_path):
        nonlocal new_count, skip_count
        created = make_strm_file(target_file, rel_path, incremental=incremental)
        if created:
            new_count += 1
        else:
            skip_count += 1
        return created

    cat_cloud_prefix = f"/视频/{cat_name}/"
    cloud_entries = set()
    cloud_files_by_entry = {}
    if cloud_files:
        for f_rel in cloud_files:
            if f_rel.startswith(cat_cloud_prefix):
                sub = f_rel[len(cat_cloud_prefix):]
                top_name = sub.split('/')[0]
                cloud_entries.add(top_name)
                if top_name not in cloud_files_by_entry:
                    cloud_files_by_entry[top_name] = []
                cloud_files_by_entry[top_name].append(SRC_BASE + f_rel)

    use_cloud_only = bool(is_complete and cloud_files is not None)
    local_entries = set() if use_cloud_only else (set(os.listdir(cat_dir)) if os.path.exists(cat_dir) else set())
    all_entries = sorted(local_entries | cloud_entries)
    ai_results = resolve_entries_smart(list(all_entries), category_hint=cat_name)

    for entry in all_entries:
        if is_ad_or_junk_path(entry) or (ai_results.get(entry) and ai_results[entry].get("is_ad")):
            print(f"[{cat_name}] [AI拦截广告] 已跳过广告导流条目: {entry}")
            continue
        full_path = os.path.join(cat_dir, entry)
        disk_vf = [] if use_cloud_only else (find_video_files(full_path, cloud_files if is_complete else None) if os.path.isdir(full_path) else [])
        cloud_vf = cloud_files_by_entry.get(entry, [])
        v_files = sorted(set(disk_vf) | set(cloud_vf))
        if is_complete and cloud_files is not None:
            v_files = [f for f in v_files if f.replace(SRC_BASE, "") in cloud_files]
        if not v_files:
            continue

        ai_info = ai_results.get(entry) or fallback_regex_parse(entry)
        if ai_info.get("media_type") in ("movie", "collection") or ai_info.get("is_collection"):
            # 双重保护：如果该条目下包含明显的剧集单集（如 S01E01 或分集编号），修正分类为剧集，禁止移交电影库
            has_episodes = any(extract_episode_smart(os.path.basename(vf))[1] is not None for vf in v_files)
            if has_episodes:
                ai_info["media_type"] = "series"
                ai_info["is_collection"] = False
            else:
                print(f"[{cat_name}] 检测到条目属于电影/合集，已自动移交电影专区处理，跳过剧集处理: {entry}")
                for bogus in [entry, ai_info.get("title"), clean_show_title(entry), "饥饿游戏"]:
                    if bogus:
                        b_dir = os.path.join(dest_dir, bogus)
                        if os.path.isdir(b_dir):
                            try:
                                shutil.rmtree(b_dir)
                                print(f"[{cat_name}] 清除错误归类为剧集的历史目录: {bogus}")
                            except Exception:
                                pass
                continue
        show_title = ai_info.get("title") or entry
        show_year = ai_info.get("year")
        show_name = f"{show_title} ({show_year})" if show_year else show_title
        show_dir = os.path.join(dest_dir, show_name)
        os.makedirs(show_dir, exist_ok=True)
        generate_nfo_file(show_dir, title=show_title, year=show_year, media_type="series", original_title=entry, overwrite=True)

        seasons_dict = defaultdict(dict)
        specials = []
        unmatched = []

        for vf in v_files:
            base_fn = os.path.basename(vf)
            rel_from_entry = vf.replace(full_path, "").lstrip("/\\")

            if any(k in base_fn for k in ["导演剪辑版", "特别篇", "彩蛋", "SP", "sp", "番外", "预告", "公益片尾"]) or \
               any(k in rel_from_entry for k in ["第 0 季", "第0季", "Season 0", "Season 00", "Specials"]):
                specials.append(vf)
                continue

            season_num, ep_num = extract_episode_smart(base_fn, rel_from_entry)
            if ep_num is None:
                unmatched.append((season_num, vf))
                continue

            if season_num == 0:
                specials.append(vf)
                continue

            if ep_num not in seasons_dict[season_num]:
                seasons_dict[season_num][ep_num] = vf
            else:
                cur = seasons_dict[season_num][ep_num].lower()
                new = vf.lower()
                if ('4k' in new or '2160' in new) and not ('4k' in cur or '2160' in cur):
                    seasons_dict[season_num][ep_num] = vf

        if unmatched:
            unmatched_by_s = defaultdict(list)
            for s_n, vf in unmatched:
                unmatched_by_s[s_n].append(vf)
            for s_n, vfs in unmatched_by_s.items():
                sorted_vfs = sorted(vfs, key=lambda x: natural_sort_key(os.path.basename(x)))
                existing_eps = set(seasons_dict[s_n].keys())
                next_ep = 1
                for vf in sorted_vfs:
                    while next_ep in existing_eps:
                        next_ep += 1
                    seasons_dict[s_n][next_ep] = vf
                    existing_eps.add(next_ep)
            unmatched = []

        valid_seasons = set()
        for s_num in sorted(seasons_dict.keys()):
            season_dir = os.path.join(show_dir, f"Season {s_num:02d}")
            os.makedirs(season_dir, exist_ok=True)
            valid_seasons.add(season_dir)
            valid_strms = set()

            for ep_num in sorted(seasons_dict[s_num].keys()):
                vf = seasons_dict[s_num][ep_num]
                rel = vf.replace(SRC_BASE, "")
                strm_name = f"{show_title} - S{s_num:02d}E{ep_num:02d}.strm"
                strm_path = os.path.join(season_dir, strm_name)
                valid_strms.add(strm_name)
                record_strm(strm_path, rel)
                ep_count += 1

            if allow_deletion:
                for old_f in os.listdir(season_dir):
                    if old_f.endswith(".strm") and old_f not in valid_strms:
                        try:
                            os.remove(os.path.join(season_dir, old_f))
                            deleted_count += 1
                            print(f"[{cat_name}] 清除失效剧集文件: {show_title}/Season {s_num:02d}/{old_f}")
                        except Exception:
                            pass

            if len(os.listdir(season_dir)) == 0:
                try:
                    shutil.rmtree(season_dir)
                except Exception:
                    pass

        # 特别篇 Season 00
        sp_dir = os.path.join(show_dir, "Season 00")
        if specials:
            os.makedirs(sp_dir, exist_ok=True)
            valid_seasons.add(sp_dir)
            valid_sp_strms = set()
            for sp_idx, sp_vf in enumerate(sorted(specials), 1):
                rel = sp_vf.replace(SRC_BASE, "")
                sp_strm_name = f"{show_title} - S00E{sp_idx:02d}.strm"
                sp_strm_path = os.path.join(sp_dir, sp_strm_name)
                valid_sp_strms.add(sp_strm_name)
                record_strm(sp_strm_path, rel)
                ep_count += 1
            if allow_deletion:
                for old_sp in os.listdir(sp_dir):
                    if old_sp.endswith(".strm") and old_sp not in valid_sp_strms:
                        try:
                            os.remove(os.path.join(sp_dir, old_sp))
                            deleted_count += 1
                            print(f"[{cat_name}] 清除失效特别篇文件: {show_title}/Season 00/{old_sp}")
                        except Exception:
                            pass
        else:
            if allow_deletion and os.path.exists(sp_dir):
                try:
                    shutil.rmtree(sp_dir)
                    deleted_count += 1
                    print(f"[{cat_name}] 清除已删除特别篇目录: {show_title}/Season 00")
                except Exception:
                    pass

        if allow_deletion:
            for item in os.listdir(show_dir):
                p = os.path.join(show_dir, item)
                if os.path.isdir(p) and p not in valid_seasons:
                    try:
                        shutil.rmtree(p)
                        deleted_count += 1
                        print(f"[{cat_name}] 清除已失效季度目录: {show_title}/{item}")
                    except Exception:
                        pass

        if len(valid_seasons) > 0:
            valid_show_dirs.add(show_dir)
            show_count += 1

    allow_deletion = (is_complete and cloud_files is not None) or (cloud_files is None and len(local_entries) > 0)
    if allow_deletion:
        # 清理已被用户从网盘删除的整部剧集目录
        for item in os.listdir(dest_dir):
            full_item = os.path.join(dest_dir, item)
            if os.path.isdir(full_item) and full_item not in valid_show_dirs:
                try:
                    shutil.rmtree(full_item)
                    deleted_count += 1
                    print(f"[{cat_name}] 清除已删除剧集目录: {item}")
                except Exception:
                    pass
    else:
        if cloud_files is None or not is_complete:
            print(f"[{cat_name}] 云端索引未完全校验，保护现有本地剧集目录不被误删")

    print(f"[{cat_name}] 处理完成，共同步 {show_count} 部剧集，{ep_count} 集，清理 {deleted_count} 个失效条目")
    return ep_count, new_count, skip_count, deleted_count

def sync_hybrid(cat_name, cat_dir, cloud_files=None, is_complete=True, incremental=False):
    """处理混合类型专区（如 日本动漫），支持单部电影、独立剧场版及连载剧集"""
    return sync_series(cat_name, cat_dir, cloud_files, is_complete=is_complete, incremental=incremental)

def setup_jellyfin_libraries():
    """在 Jellyfin 中自动注册并配置所有 9 大专区媒体库"""
    print("正在检查并健全 Jellyfin 专区媒体库配置...")
    libraries = {
        "国产电影": {"type": "movies", "coll": "movies.collection"},
        "港台经典": {"type": "movies", "coll": "movies.collection"},
        "欧美大片": {"type": "movies", "coll": "movies.collection"},
        "韩国电影": {"type": "movies", "coll": "movies.collection"},
        "国产剧集": {"type": "tvshows", "coll": "tvshows.collection"},
        "热门韩剧": {"type": "tvshows", "coll": "tvshows.collection"},
        "欧美剧集": {"type": "tvshows", "coll": "tvshows.collection"},
        "国产动漫": {"type": "tvshows", "coll": "tvshows.collection"},
        "日本动漫": {"type": "tvshows", "coll": "tvshows.collection"},
    }

    # 1. 确保物理目录与配置在 Jellyfin config 中完整就绪
    os.makedirs(JELLYFIN_ROOT, exist_ok=True)
    series_opt_xml = ""
    gc_opt = os.path.join(JELLYFIN_ROOT, "国产剧集", "options.xml")
    if os.path.exists(gc_opt):
        with open(gc_opt, "r", encoding="utf-8") as f:
            series_opt_xml = f.read()

    for lib_name, meta in libraries.items():
        lib_dir = os.path.join(JELLYFIN_ROOT, lib_name)
        os.makedirs(lib_dir, exist_ok=True)
        # mblink 文件
        mblink_path = os.path.join(lib_dir, f"{lib_name}.mblink")
        target_cinema_path = f"/cinema/{lib_name}"
        os.makedirs(os.path.join(CINEMA_BASE, lib_name), exist_ok=True)
        with open(mblink_path, "w", encoding="utf-8") as f:
            f.write(target_cinema_path)
        # 集合类型标记文件
        coll_file = os.path.join(lib_dir, meta["coll"])
        if not os.path.exists(coll_file):
            with open(coll_file, "w", encoding="utf-8") as f:
                f.write("")
        # options.xml
        opt_file = os.path.join(lib_dir, "options.xml")
        if not os.path.exists(opt_file):
            if meta["type"] == "movies":
                opt_content = f'''<?xml version="1.0" encoding="utf-8"?>
<LibraryOptions xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <Enabled>true</Enabled>
  <EnablePhotos>true</EnablePhotos>
  <EnableRealtimeMonitor>false</EnableRealtimeMonitor>
  <PathInfos>
   <MediaPathInfo>
     <Path>{target_cinema_path}</Path>
   </MediaPathInfo>
 </PathInfos>
 <SaveLocalMetadata>false</SaveLocalMetadata>
 <AutomaticallyAddToCollection>true</AutomaticallyAddToCollection>
 <PreferredMetadataLanguage>zh-cn</PreferredMetadataLanguage>
 <MetadataCountryCode>CN</MetadataCountryCode>
 <DisabledLocalMetadataReaders />
 <LocalMetadataReaderOrder><string>Nfo</string></LocalMetadataReaderOrder>
 <TypeOptions>
    <TypeOptions>
      <Type>Movie</Type>
      <MetadataFetchers><string>TheMovieDb</string></MetadataFetchers>
      <MetadataFetcherOrder><string>TheMovieDb</string></MetadataFetcherOrder>
      <ImageFetchers><string>TheMovieDb</string></ImageFetchers>
      <ImageFetcherOrder><string>TheMovieDb</string></ImageFetcherOrder>
    </TypeOptions>
  </TypeOptions>
</LibraryOptions>'''
            else:
                if series_opt_xml:
                    opt_content = series_opt_xml.replace("/cinema/国产剧集", target_cinema_path)
                else:
                    opt_content = f'''<?xml version="1.0" encoding="utf-8"?>
<LibraryOptions xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <Enabled>true</Enabled>
  <PathInfos><MediaPathInfo><Path>{target_cinema_path}</Path></MediaPathInfo></PathInfos>
  <PreferredMetadataLanguage>zh-cn</PreferredMetadataLanguage>
  <MetadataCountryCode>CN</MetadataCountryCode>
  <SeasonZeroDisplayName>特别篇</SeasonZeroDisplayName>
</LibraryOptions>'''
            with open(opt_file, "w", encoding="utf-8") as f:
                f.write(opt_content)

    # 2. 尝试向 Jellyfin API 动态同步 VirtualFolders
    try:
        req = urllib.request.Request(
            f"{JELLYFIN_API_BASE}/Library/VirtualFolders",
            headers={"Authorization": f'MediaBrowser Token="{JELLYFIN_KEY}"'}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            existing_libs = json.loads(resp.read().decode("utf-8"))
            existing_names = {item.get("Name") for item in existing_libs}

            for lib_name, meta in libraries.items():
                if lib_name not in existing_names:
                    add_url = f"{JELLYFIN_API_BASE}/Library/VirtualFolders?name={urllib.parse.quote(lib_name)}&collectionType={meta['type']}&paths[]={urllib.parse.quote(f'/cinema/{lib_name}')}&refreshLibrary=true"
                    add_req = urllib.request.Request(
                        add_url,
                        headers={"Authorization": f'MediaBrowser Token="{JELLYFIN_KEY}"', "Content-Type": "application/json"},
                        method="POST"
                    )
                    try:
                        with urllib.request.urlopen(add_req, timeout=5) as add_resp:
                            print(f"成功向 Jellyfin 动态注册媒体库: [{lib_name}] -> {add_resp.status}")
                    except Exception as e:
                        print(f"注册 Jellyfin 媒体库 [{lib_name}] 提示:", e)
    except Exception as e:
        print("检查 Jellyfin 运行时媒体库 API 结果:", e)

def trigger_jellyfin_refresh():
    """向本地 Jellyfin (8096) 触发全局扫描与海报元数据刮削更新"""
    print("正在通知 Jellyfin 全局刷新并刮削海报...")
    try:
        req = urllib.request.Request(
            f"{JELLYFIN_API_BASE}/Library/Refresh",
            headers={"Authorization": f'MediaBrowser Token="{JELLYFIN_KEY}"'},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"Jellyfin 媒体库扫描指令发送成功: HTTP {resp.status}")
            return True
    except Exception as e:
        print("Jellyfin 刷新请求结果:", e)
        return False

def wait_for_jellyfin_scan_complete(max_wait_sec=300):
    """
    等待 Jellyfin 媒体库扫描（RefreshLibrary）任务完全退出 Running 状态，
    避免外部打标工具与 Jellyfin 服务端在扫描入库时发生 SQLite 写库死锁争抢。
    """
    start_time = time.time()
    time.sleep(2)
    while time.time() - start_time < max_wait_sec:
        try:
            req = urllib.request.Request(
                f"{JELLYFIN_API_BASE}/ScheduledTasks",
                headers={"Authorization": f'MediaBrowser Token="{JELLYFIN_KEY}"'}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    tasks = json.loads(resp.read().decode('utf-8'))
                    scan_task = next((t for t in tasks if t.get("Key") == "RefreshLibrary"), None)
                    if not scan_task or scan_task.get("State") != "Running":
                        return True
        except Exception:
            pass
        time.sleep(3)
    return False

def ensure_fallback_posters():
    """
    兜底补齐缺失海报：
    1. 若某剧集季度（Season）在 TMDB 无独立季海报，自动继承其父级剧集（Series）海报至 Season 目录；
    2. 若某部电影（Movie）在 TMDB 未能匹配到海报且本地无 poster.jpg，自动从 .strm 视频流截取高清画面作为海报。
    """
    import sqlite3
    jf_db = paths.JELLYFIN_DB
    if not os.path.exists(jf_db):
        return
    refreshed_ids = []
    try:
        conn = sqlite3.connect(f"file:{jf_db}?mode=ro", uri=True, timeout=2)
        c = conn.cursor()
        season_rows = c.execute("""
            SELECT s.Id, s.Path,
                   (SELECT img.Path FROM BaseItemImageInfos img WHERE img.ItemId = s.SeriesId AND img.ImageType = 0 LIMIT 1)
            FROM BaseItems s
            WHERE s.Type = 'MediaBrowser.Controller.Entities.TV.Season'
              AND NOT EXISTS (
                  SELECT 1 FROM BaseItemImageInfos img WHERE img.ItemId = s.Id AND img.ImageType = 0
              )
        """).fetchall()
        for sid, spath, series_poster in season_rows:
            if spath and series_poster:
                host_spath = spath.replace("/cinema/", os.path.join(CINEMA_BASE, ""))
                host_poster = series_poster.replace("/config/", paths.JELLYFIN_CONFIG + "/")
                dest_poster = os.path.join(host_spath, "poster.jpg")
                if os.path.isdir(host_spath) and os.path.isfile(host_poster) and not os.path.exists(dest_poster):
                    shutil.copy2(host_poster, dest_poster)
                    refreshed_ids.append(sid)

        movie_rows = c.execute("""
            SELECT m.Id, m.Path
            FROM BaseItems m
            WHERE m.Type = 'MediaBrowser.Controller.Entities.Movies.Movie'
              AND NOT EXISTS (
                  SELECT 1 FROM BaseItemImageInfos img WHERE img.ItemId = m.Id AND img.ImageType = 0
              )
        """).fetchall()
        conn.close()
        for mid, mpath in movie_rows:
            if not mpath:
                continue
            host_mpath = mpath.replace("/cinema/", os.path.join(CINEMA_BASE, ""))
            movie_dir = os.path.dirname(host_mpath) if host_mpath.endswith(".strm") else host_mpath
            dest_poster = os.path.join(movie_dir, "poster.jpg")
            if os.path.isdir(movie_dir) and not os.path.exists(dest_poster) and os.path.isfile(host_mpath):
                try:
                    with open(host_mpath, "r", encoding="utf-8") as f:
                        strm_url = f.read().strip()
                    if strm_url.startswith("http"):
                        subprocess.run(
                            [host_config._value("VOD_HOST_FFMPEG_BINARY", "ffmpeg"), "-y", "-ss", "00:02:00", "-i", strm_url, "-frames:v", "1", "-q:v", "2", dest_poster],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25
                        )
                        if os.path.exists(dest_poster):
                            refreshed_ids.append(mid)
                except Exception:
                    pass
    except Exception:
        pass

    for iid in refreshed_ids:
        try:
            clean_id = iid.replace("-", "").lower()
            req = urllib.request.Request(
                f"{JELLYFIN_API_BASE}/Items/{clean_id}/Refresh?MetadataRefreshMode=FullRefresh&ImageRefreshMode=FullRefresh&ReplaceAllMetadata=false&ReplaceAllImages=false",
                headers={"Authorization": f'MediaBrowser Token="{JELLYFIN_KEY}"'},
                method="POST"
            )
            urllib.request.urlopen(req, timeout=5).close()
        except Exception:
            pass

def run_post_scan_worker():
    """在受管理进程内等待传统 Jellyfin 扫描，再补齐海报与片头片尾。"""
    if not host_config._boolean("VOD_HOST_TRADITIONAL_ENABLED"):
        raise RuntimeError("traditional library integration is disabled")
    import datetime as _dt
    script_path = "quark_vod.integrations.hometheater.skip_manager"
    log_path = os.path.join(paths.LOG_DIR, "skip.log")
    try:
        wait_for_jellyfin_scan_complete(max_wait_sec=300)
        time.sleep(2)
        ensure_fallback_posters()
        if host_config._boolean("INTRO_SKIPPER_WRITE_ENABLED"):
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as lf:
                now_str = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                lf.write("\n=== skip auto-tag (错峰完成): " + now_str + " ===\n")
                lf.flush()
                result = subprocess.run(
                    [sys.executable, "-m", script_path, "--server", "jellyfin", "auto-tag"],
                    stdout=lf, stderr=lf,
                    text=True
                )
                if result.returncode != 0:
                    lf.write("片头片尾同步异常退出 (returncode=" + str(result.returncode) + ")\n")
    except Exception as ex:
        try:
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write("片头片尾后台进程异常: " + str(ex) + "\n")
        except Exception:
            pass

def trigger_skip_sync():
    """在受管理进程内执行片头片尾同步，超时与退出均由 runner 管理。"""
    try:
        print("等待传统 Jellyfin 扫描完成后执行片头片尾同步...")
        if host_config._boolean("INTRO_SKIPPER_WRITE_ENABLED"):
            run_post_scan_worker()
    except Exception as e:
        print("片头片尾同步提示:", e)

def run_sync(incremental=False):
    if not __import__("quark_vod.config", fromlist=["_boolean"])._boolean("VOD_HOST_TRADITIONAL_ENABLED"):
        raise RuntimeError("traditional library integration is disabled")
    mode_name = "增量同步" if incremental else "全量同步"
    print("========================================")
    print(f"=== 开始{mode_name}网盘视频与更新海报 ===")
    print("========================================")


    total_items = 0
    total_new = 0
    total_skipped = 0
    total_deleted = 0
    if not os.path.exists(VIDEO_ROOT):
        print(f"错误: 未找到网盘视频挂载根目录 {VIDEO_ROOT}", file=sys.stderr)
        return False

    # 1. 穿透刷新 OpenList 网盘实时缓存，并探测云端权威文件列表
    token = get_openlist_token()
    refresh_openlist_cache(token)
    cloud_files, is_complete = get_openlist_cloud_files(token)

    # 先构建 Jellyfin 媒体库结构
    setup_jellyfin_libraries()

    # 遍历所有受管辖的专区分类，确保网盘整类增删均能完整同步
    for cat in ALL_CATEGORIES:
        cat_dir = os.path.join(VIDEO_ROOT, cat)
        cinema_cat_dir = os.path.join(CINEMA_BASE, cat)
        has_cloud_cat = bool(cloud_files and any(f.startswith(f"/视频/{cat}/") for f in cloud_files))
        if os.path.isdir(cat_dir) or has_cloud_cat:
            if cat in MOVIE_CATEGORIES:
                items, new_c, skip_c, deleted = sync_movies(cat, cat_dir, cloud_files, is_complete=is_complete, incremental=incremental)
            elif cat in HYBRID_CATEGORIES:
                items, new_c, skip_c, deleted = sync_hybrid(cat, cat_dir, cloud_files, is_complete=is_complete, incremental=incremental)
            else:
                items, new_c, skip_c, deleted = sync_series(cat, cat_dir, cloud_files, is_complete=is_complete, incremental=incremental)
            total_items += items
            total_new += new_c
            total_skipped += skip_c
            total_deleted += deleted
        else:
            # 若该专区分类在网盘端已被彻底删除或不存在，则彻底清空 cinema 下对应的旧条目
            if is_complete and os.path.exists(cinema_cat_dir):
                for old_entry in os.listdir(cinema_cat_dir):
                    p = os.path.join(cinema_cat_dir, old_entry)
                    if os.path.isdir(p):
                        try:
                            shutil.rmtree(p)
                            total_deleted += 1
                            print(f"[{cat}] 云端专区已不存在，清除残留条目目录: {old_entry}")
                        except Exception:
                            pass

    if incremental:
        try:
            _all_names = get_existing_media_raw_names(VIDEO_ROOT, cloud_files=cloud_files if is_complete else None)
            _cl_count = clean_orphaned_cached_items(_all_names)
            if _cl_count > 0:
                print(f"[AI记忆库] 自动清理 {_cl_count} 个已从网盘删除的失效条目记忆")
        except Exception as _e:
            pass
        print(f"增量媒体索引扫描完成，全库共 {total_items} 个影视条目（检测到 {total_new} 个新增影视条目，跳过 {total_skipped} 个已有条目，清理 {total_deleted} 个失效条目）。")
        if total_new > 0 or total_deleted > 0:
            trigger_jellyfin_refresh()
            if total_new > 0:
                trigger_skip_sync()
        else:
            print("增量检测：媒体库无新增/失效条目，跳过重复的全库刮削扫描。")
        print("=== 增量同步完成！ ===")
    else:
        try:
            _all_names = get_existing_media_raw_names(VIDEO_ROOT, cloud_files=cloud_files if is_complete else None)
            _cl_count = clean_orphaned_cached_items(_all_names)
            if _cl_count > 0:
                print(f"[AI记忆库] 自动清理 {_cl_count} 个已从网盘删除的失效条目记忆")
        except Exception as _e:
            pass
        print(f"全量媒体索引扫描完成，累计处理/更新 {total_items} 个影视条目，同步清理 {total_deleted} 个失效条目。")
        trigger_jellyfin_refresh()
        trigger_skip_sync()
        print("=== 全量同步完成！ ===")
    return True

if __name__ == "__main__":
    if "--post-scan-worker" in sys.argv:
        run_post_scan_worker()
        sys.exit(0)
    is_inc = "--incremental" in sys.argv or "--mode=incremental" in sys.argv or "-i" in sys.argv
    ok = run_sync(incremental=is_inc)
    if not ok:
        sys.exit(1)
