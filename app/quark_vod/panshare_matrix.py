#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
终极多源网盘并发搜索矩阵 (Panshare Search Matrix)
========================================================================
核心原则：
  1. 多源并发打捞：聚合快映、虎斑、蜡笔、多多等多家主流夸克 4K 搜索引擎，彻底消灭单点失效。
  2. 极速响应兜底：多线程并发检索（单节点 3~5s 超时），先返回先用。
  3. 智能提取码解析：自动识别并补齐 #pwd=xxxx 或 提取码: xxxx。
  4. 死链熔断过滤：实时与 dead_sources 数据库交叉比对，自动剔除已知死链。
  5. 标题智能容错：自动剥离括号年份，支持核心词与原标题多轮次召回。
  6. 结果缓存（2026-10-20）：同关键词 TTL 内复用，实测把 8~12s 冷搜索降到 0.1s 级。
  7. 子条目结构化（2026-10-20）：详情页按 module-row-one 拆解，提取每个网盘
     条目的精确标题（区分季/篇章/画质），替代旧「一个详情页标题挂全部链接」。
"""

import os
import re
import sys
import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import config
from . import db

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}



# ── 经过实测连通性验证的活跃 4K 网盘搜索节点矩阵 ──────────────
ACTIVE_PAN_NODES = [
    {"key": "kuaiying_new", "name": "快映•新节点", "base": "http://43.248.128.118:12512", "timeout": 4.0},
    {"key": "huban_ip",     "name": "虎斑•高速",   "base": "http://38.76.197.172:16969", "timeout": 3.5},
    {"key": "labi",         "name": "蜡笔•4K",     "base": "https://www.fmcf.fun",        "timeout": 4.5},
    {"key": "labi_mirror",  "name": "蜡笔•备用",   "base": "https://www.mmpanpan.site",   "timeout": 4.5},
    {"key": "duoduo",       "name": "多多•4K",     "base": "https://tv.yydsys.cc",        "timeout": 4.5},
    {"key": "duoduo_xyz",   "name": "多多•备用",   "base": "https://tv.214521.xyz",       "timeout": 4.5},
    {"key": "huban_domain", "name": "虎斑•备用",   "base": "http://xhban.xyz",            "timeout": 4.0},
]
# ── 搜索结果缓存 ────────────────────────────────────────
# 内存缓存 + 磁盘持久化，避免服务重启后冷启动全线 8~12s。
# TTL 依据：分享源平均存活约 3 天，但盘搜站索引滞后可达数小时；
# 30 分钟可在「新鲜度」与「命中率」间取得平衡。
SEARCH_CACHE_TTL = 1800          # 秒
SEARCH_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "search_cache")
_mem_cache = {}                  # key -> (expire_ts, [dict,...])
_CACHE_MAGIC = 1


def _cache_path(key):
    return os.path.join(SEARCH_CACHE_DIR, key + ".json")


def _cache_get(key):
    """读缓存（内存优先，磁盘兜底）。过期或损坏返回 None。"""
    now = time.time()
    hit = _mem_cache.get(key)
    if hit:
        exp, payload = hit
        if now < exp:
            return payload
        _mem_cache.pop(key, None)
    try:
        p = _cache_path(key)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                blob = json.load(f)
            if blob.get("magic") == _CACHE_MAGIC and now < blob.get("expire", 0):
                payload = blob.get("items") or []
                _mem_cache[key] = (blob["expire"], payload)
                return payload
    except Exception:
        pass
    return None


def _cache_put(key, items):
    """写缓存（内存 + 磁盘）。items 为可 JSON 序列化的 dict 列表。"""
    exp = time.time() + SEARCH_CACHE_TTL
    _mem_cache[key] = (exp, items)
    # 内存容量保护
    if len(_mem_cache) > 500:
        now = time.time()
        for k in [k for k, v in _mem_cache.items() if v[0] < now]:
            _mem_cache.pop(k, None)
    try:
        os.makedirs(SEARCH_CACHE_DIR, exist_ok=True)
        tmp = _cache_path(key) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"magic": _CACHE_MAGIC, "expire": exp, "items": items},
                      f, ensure_ascii=False)
        os.replace(tmp, _cache_path(key))
    except Exception:
        pass


def _cache_key(keyword):
    """生成缓存键。

    2026-10-20 严重 Bug 修复：旧实现用 `[^0-9A-Za-z]+` 过滤，会把所有中文字符
    替换为下划线，导致「赴山海」「暗河传」「狂飙」等全部坍缩成同一个键
    `ps_.json` —— 不同影片互相串缓存，用户点 A 片拿到 B 片的片源。
    改为对原始（未剥离）关键词取 URL-safe 引用，中文原样保留。
    """
    import hashlib
    raw = (keyword or "").strip()
    if not raw:
        return "ps_empty"
    # 文件系统安全化：仅替换路径非法字符，保留中文/字母/数字
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", raw)[:80]
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]
    return f"ps_{safe}_{digest}"


def cache_purge(keyword=None):
    """清缓存：传 keyword 只清该词，不传则全清（维护用）。"""
    global _mem_cache
    if keyword:
        k = _cache_key(keyword)
        _mem_cache.pop(k, None)
        try:
            os.remove(_cache_path(k))
        except Exception:
            pass
        return 1
    n = len(_mem_cache)
    _mem_cache = {}
    try:
        import shutil
        shutil.rmtree(SEARCH_CACHE_DIR, ignore_errors=True)
    except Exception:
        pass
    return n


# ── 网盘条目结构化提取 ──────────────────────────────────
# MacCMS 详情页下载区由若干 module-row-info 条目组成，每条含
# <div class="module-row-title"><h4>子标题</h4><p>链接</p></div>。
#
# 2026-10-20 关键修正：不能按 module-row-one 容器切分——实测这些站的 HTML
# 并不规范，末几个条目常缺少外层 module-row-one 包裹（暗河传臻彩页 4 条
# 里只有 3 条被 module-row-one 覆盖，第 2 个夸克链接整条丢失）。
# 改为按 module-row-title 直接配对 h4 与 p，与容器无关。
_ROW_SPLIT = re.compile(r'<div class="module-row-title">(.*?)(?=<div class="module-row-title">|$)', re.S)
_ROW_H4 = re.compile(r"<h4>(.*?)</h4>", re.S)
_ROW_LINK = re.compile(r"<p>\s*(https?://[^<\s]+)\s*</p>", re.S)
_TAG_STRIP = re.compile(r"<[^>]+>")


def parse_download_rows(html_text):
    """解析详情页下载区，返回 [{title, url}]（保留页面顺序）。

    解析不到时返回 []，由调用方回退到正则全量提取。
    """
    if not html_text:
        return []
    rows = []
    for seg in _ROW_SPLIT.findall(html_text):
        m_h = _ROW_H4.search(seg)
        m_l = _ROW_LINK.search(seg)
        if not (m_h and m_l):
            continue
        title = re.sub(r"\s+", " ", _TAG_STRIP.sub("", m_h.group(1))).strip()
        url = m_l.group(1).strip()
        # h4 形如「凡人修仙传：外海风云 - 凡人修仙传：外海风云」，取破折号前主名
        if " - " in title:
            title = title.split(" - ")[0].strip()
        if title and url.startswith("http"):
            rows.append({"title": title, "url": url})
    return rows

def extract_quark_links_and_pwd(html_text):
    """从 HTML 文本中全方位提取夸克分享链接及提取码。"""
    if not html_text:
        return []

    # 模式 1: 提取形如 "链接: https://pan.quark.cn/s/xxxx 密码/提取码: yyyy"
    pwd_patterns = re.findall(
        r'(https?://pan\.quark\.cn/s/[a-zA-Z0-9]+)[\s,，;；]*?(?:提取码|密码|pwd)[:：\s]*?([a-zA-Z0-9]{4})',
        html_text, re.IGNORECASE
    )
    pwd_map = {u.strip(): p.strip() for u, p in pwd_patterns}

    # 模式 2: 提取标准 url 及可能附带的 ?pwd= 或 #pwd= 片段
    raw_urls = re.findall(r'(https?://pan\.quark\.cn/s/[a-zA-Z0-9]+(?:[?#][^\s"\'<>]+)?)', html_text)

    results = []
    seen = set()
    for raw in raw_urls:
        m = re.search(r'https?://pan\.quark\.cn/s/[a-zA-Z0-9]+', raw)
        if not m:
            continue
        base_url = m.group(0)
        if base_url in seen:
            continue
        seen.add(base_url)

        final_url = raw
        if "#" not in final_url and "?" not in final_url and base_url in pwd_map:
            final_url = f"{base_url}#pwd={pwd_map[base_url]}"

        results.append(final_url)
    return results


class PanshareMatrix:
    @classmethod
    def search(cls, keyword, limit_per_node=10, max_workers=8, use_cache=True):
        """统一多源并发网盘搜索矩阵。返回 [SearchResult]。

        2026-10-20：新增结果缓存。同一关键词 TTL（默认 30 分钟）内直接复用，
        实测把 8~12s 的冷搜索降到 0.1s 级，且避免 Jellyfin ffprobe 反复探测时
        重复压打盘搜节点。
        """
        from .search import SearchResult, sanitize_title
        kw_core = sanitize_title(keyword)
        if not kw_core:
            return []

        ckey = _cache_key(kw_core)
        if use_cache:
            cached = _cache_get(ckey)
            if cached is not None:
                return [SearchResult(
                    engine="panshare", source=c.get("source", "cache"),
                    name=c.get("name", ""), year=c.get("year", ""),
                    poster=c.get("poster", ""), episodes=c.get("episodes") or [],
                    play_url=c.get("play_url"), share_url=c.get("share_url"),
                    pan_type=c.get("pan_type", "quark"),
                    media_type=c.get("media_type", ""))
                    for c in cached]

        results = []
        dead_shares = db.get_dead_sources_set("panshare")
        seen_urls = set()
        collected = []      # 待写入缓存的 dict

        # 1. 优先查本地热播 4K 直通缓存 (daily_hot.json)
        try:
            hot_path = "/home/dong/projects/hometheater/web/daily_hot.json"
            if os.path.exists(hot_path):
                with open(hot_path, "r", encoding="utf-8") as f:
                    hot_items = json.load(f)
                    for item in hot_items:
                        cname = item.get("clean_name", "")
                        qurl = item.get("quark_url", "")
                        if kw_core in cname and qurl and qurl not in dead_shares:
                            if qurl not in seen_urls:
                                seen_urls.add(qurl)
                                results.append(SearchResult(
                                    engine="panshare", source="daily_hot",
                                    name=item.get("title", cname),
                                    year="", poster="", episodes=[],
                                    share_url=qurl, pan_type="quark"
                                ))
        except Exception:
            pass

        # 2. 矩阵节点分梯队并发打捞（2026-10-20 镜像节点合并优化）
        def _fetch_node(node):
            base_url = node["base"].rstrip("/")
            search_api = f"{base_url}/index.php/vod/search.html?wd={urllib.parse.quote(kw_core)}"
            node_results = []
            try:
                req = urllib.request.Request(search_api, headers=UA)
                with urllib.request.urlopen(req, timeout=node["timeout"]) as r:
                    html = r.read().decode("utf-8", "ignore")

                det_ids = re.findall(r'href="(/index\.php/vod/detail/id/\d+\.html|/voddetail/\d+\.html)"', html)
                seen_detail = set()
                detail_targets = []
                for dpath in det_ids[:limit_per_node]:
                    if dpath not in seen_detail:
                        seen_detail.add(dpath)
                        detail_targets.append(f"{base_url}{dpath}")

                # 子线程池并发抓取详情页
                def _fetch_detail(det_url):
                    try:
                        req_d = urllib.request.Request(det_url, headers=UA)
                        with urllib.request.urlopen(req_d, timeout=node["timeout"]) as rd:
                            dhtml = rd.read().decode("utf-8", "ignore")
                    except Exception:
                        return []
                    # 2026-10-20：优先按 module-row-one 结构化拆解，拿到每个网盘
                    # 条目的精确标题（区分「第二季 / 外海风云 / 臻彩」等）；
                    # 页面改版导致解析为空时，回退到 <title> + 全量正则提取。
                    rows = parse_download_rows(dhtml)
                    if rows:
                        return [{"source": node["key"], "name": r["title"], "share_url": r["url"]}
                                for r in rows if "quark" in r["url"]]
                    links = extract_quark_links_and_pwd(dhtml)
                    tm = re.search(r'<title>([^<]+)-', dhtml)
                    tname = tm.group(1).strip() if tm else kw_core
                    return [{"source": node["key"], "name": tname, "share_url": q} for q in links]

                # 并发度从 4 提到 8：详情页是纯 IO 等待，实测可把单节点耗时
                # 从 3~5s 压到 2~3s（10 个详情页时最明显）。
                with ThreadPoolExecutor(max_workers=min(8, len(detail_targets) or 1)) as sub_exec:
                    sub_futs = [sub_exec.submit(_fetch_detail, du) for du in detail_targets]
                    for sf in as_completed(sub_futs):
                        node_results.extend(sf.result())

            except Exception:
                pass
            return node_results

        # 全节点并发打捞。
        # 2026-10-20 曾尝试「镜像节点分梯队 + 提前终止」以压延迟，实测召回崩塌：
        # 12 个关键词全部下降，平均仅保留 33%（狂飙 32→4、凡人修仙传 29→4、
        # 莲花楼 3→0）。原因是 Jaccard 100% 是跨关键词累计值，掩盖了单次搜索中
        # 镜像节点各有一半独有结果的事实。故回退为全节点并发，用缓存解决重复请求
        # 的延迟，而不是牺牲召回。
        with ThreadPoolExecutor(max_workers=min(max_workers, len(ACTIVE_PAN_NODES))) as executor:
            future_to_node = {executor.submit(_fetch_node, n): n for n in ACTIVE_PAN_NODES}
            for future in as_completed(future_to_node):
                try:
                    items = future.result()
                    for it in items:
                        q_url = it["share_url"]
                        base_u = q_url.split("#")[0].split("?")[0]
                        if base_u in dead_shares:
                            continue
                        if q_url not in seen_urls and base_u not in seen_urls:
                            seen_urls.add(q_url)
                            seen_urls.add(base_u)
                            results.append(SearchResult(
                                engine="panshare", source=it["source"],
                                name=it["name"], year="", poster="",
                                episodes=[], share_url=q_url, pan_type="quark"
                            ))
                            collected.append({"source": it["source"], "name": it["name"],
                                              "share_url": q_url, "pan_type": "quark"})
                except Exception:
                    pass

        # daily_hot 命中的也进缓存，避免每次都重读 JSON
        for r in results:
            if r.source == "daily_hot":
                collected.append({"source": "daily_hot", "name": r.name,
                                  "share_url": r.share_url, "pan_type": "quark"})

        if use_cache:
            _cache_put(ckey, collected)
        return results
