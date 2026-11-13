#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
片源搜索模块

两个引擎：

  1. CMS 引擎（默认，已实测稳定）
     标准苹果 CMS /api.php/provide/vod 接口，返回 m3u8 直链。
     优点：接口规范、可用性高、返回海报+集数+直链。
     本质：播放的是【源站 CDN】，不落到自己网盘。

  2. PanShare 引擎（网盘分享搜索，预留）
     对接快映/木偶/多多等 PanWebShare 站，返回夸克分享链接。
     启用后配合 transfer.py 可转存到自己网盘。
     当前状态：这些站多为纯 SPA，接口需逆向，框架期先占位。

重要说明（架构知情权）：
  两条路线的"播放来源"不同，务必知悉：
    CMS      -> 播放源站 CDN 的 m3u8（不占网盘空间，稳定性看源站）
    PanShare -> 转存到你的夸克网盘再播（占空间，但画质/稳定性自己可控）
  默认走 CMS，配了 QUARK_COOKIE 且 PanShare 就绪后优先走 PanShare。
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config
from . import db

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# ── CMS 源（2026-10-18 最新高可用源池）──────────────────
CMS_SITES = [
    {"key": "ikun", "name": "⚡ikun┃官方M3U8", "api": "https://ikunzyapi.com/api.php/provide/vod"},
    {"key": "guangsu", "name": "⚡光速┃官方M3U8", "api": "https://api.guangsuapi.com/api.php/provide/vod"},
    {"key": "bfzy", "name": "⚡暴风┃官方M3U8", "api": "https://bfzyapi.com/api.php/provide/vod"},
    {"key": "lzi",  "name": "⚡量子┃官方M3U8", "api": "https://cj.lziapi.com/api.php/provide/vod"},
    {"key": "xinlang", "name": "⚡新浪┃官方M3U8", "api": "http://api.xinlangapi.com/xinlangapi.php/provide/vod"},
    {"key": "feifan", "name": "⚡非凡┃官方M3U8", "api": "http://cj.ffzyapi.com/api.php/provide/vod"},
]

# 单源超时（秒）。CMS 源响应 3~4s，留余量
SEARCH_TIMEOUT = 20


class SearchResult:
    """统一的搜索结果。"""

    def __init__(self, engine, source, name, year, poster, episodes, play_url=None,
                 share_url=None, pan_type=None, media_type=""):
        self.engine = engine          # cms / panshare
        self.source = source          # 源站 key
        self.name = name
        self.year = year or ""
        self.poster = poster or ""
        self.episodes = episodes or []  # [(集号, 直链或分享链接)]
        self.play_url = play_url        # 电影直链
        self.share_url = share_url      # 网盘分享链接（panshare）
        self.pan_type = pan_type
        self.media_type = media_type or ""   # CMS type_name（国产剧/动作片/日韩动漫…）

    def url_for(self, episode=0):
        """取指定集的地址。电影传 0。

        【严禁回落铁律】请求剧集（episode > 0）时，若候选没有该集，
        绝不允许回落到 play_url——那会把同名的「电影/正片」当成剧集某一集
        （2026-10-20《爱你》事故：剧情片「正片」被当成国产剧第 1 集）。
        """
        if episode and episode > 0:
            for ep, url in self.episodes:
                if ep == episode:
                    return url
            return None
        return self.play_url

    def to_dict(self):
        return {
            "engine": self.engine, "source": self.source,
            "name": self.name, "year": self.year,
            "poster": self.poster, "episode_count": len(self.episodes),
            "pan_type": self.pan_type,
        }


def _get_json(url, timeout=SEARCH_TIMEOUT):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def search_cms(keyword, sites=None, limit_per_site=8):
    """CMS 引擎搜索。返回 [SearchResult]。

    并发请求多个源，先返回先用；单源失败不影响其他源。

    2026-10-20 修复截断缺陷：旧实现盲目取每个源返回的前 N 条
    （`list[:limit_per_site]`）。苹果 CMS 的相关度排序会把「XX爱你」
    这类同词根条目排在正主前面——实测《爱你》的国产剧正片排在第 9 位，
    被 limit_per_site=8 直接截掉，观众只能看到电影版。现在先按片名
    相关度预筛再截断，保证正主不被同词根条目挤出窗口。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    kc = sanitize_title(keyword)
    kc_clean = _clean_title_text(_strip_template_noise(kc)) if kc else ""

    targets = sites or CMS_SITES
    results = []

    def _one(site):
        url = f"{site['api']}?ac=detail&wd={urllib.parse.quote(keyword)}"
        try:
            d = _get_json(url)
        except Exception:
            return []
        raw = d.get("list") or []
        if not raw:
            return []
        if kc_clean:
            def _rel(v):
                nm = _clean_title_text(_strip_template_noise((v.get("vod_name") or "")))
                if not nm:
                    return (2, 0)
                if nm == kc_clean:
                    return (0, 0)
                if nm.startswith(kc_clean):
                    return (0, len(nm) - len(kc_clean))
                if kc_clean in nm:
                    return (1, 0)
                return (2, 0)
            picked = sorted(raw, key=_rel)[:limit_per_site]
        else:
            picked = raw[:limit_per_site]
        out = []
        for v in picked:
            name = (v.get("vod_name") or "").strip()
            if not name:
                continue
            poster = v.get("vod_pic") or ""
            pu = v.get("vod_play_url") or ""
            eps = []
            movie_url = ""
            # 支持 $$$ 多播放源组拆分（如 m3u8 组 $$$ 网页播放组），优先选择包含 .m3u8 的真实切片流
            sources = pu.split("$$$") if "$$$" in pu else [pu]
            sources.sort(key=lambda s: ".m3u8" in s.lower(), reverse=True)
            for s_group in sources:
                group_eps = []
                group_movie = ""
                for line in s_group.split("#"):
                    line = line.strip()
                    if not line:
                        continue
                    # 提取链接地址
                    parts = line.split("$")
                    addr = parts[-1].strip() if parts else ""
                    label = parts[0].strip() if len(parts) > 1 else ""
                    if not addr.startswith("http"):
                        continue
                    # 优先挑带 .m3u8 的切片
                    if not addr.lower().endswith(".m3u8") and ".m3u8" in line:
                        for p in parts:
                            if p.startswith("http") and ".m3u8" in p.lower():
                                addr = p.strip()
                                break
                    # 提取集号（"第01集" / "第 1 集" / "EP01"）
                    import re
                    m = re.search(r"(\d{1,3})", label)
                    ep_no = int(m.group(1)) if m else 0
                    if ep_no:
                        group_eps.append((ep_no, addr))
                    elif not group_movie:
                        group_movie = addr
                if (group_eps or group_movie) and (not eps and not movie_url):
                    eps = group_eps
                    movie_url = group_movie
                    if any(".m3u8" in str(x).lower() for x in (eps or [movie_url])):
                        break
            if not eps and not movie_url:
                continue
            out.append(SearchResult(
                engine="cms", source=site["key"], name=name,
                year=v.get("vod_year"), poster=poster,
                episodes=sorted(eps), play_url=movie_url,
                media_type=(v.get("type_name") or "").strip()))
        return out

    with ThreadPoolExecutor(max_workers=min(4, len(targets))) as ex:
        futs = {ex.submit(_one, s): s for s in targets}
        for f in as_completed(futs):
            try:
                results.extend(f.result())
            except Exception:
                pass
    return results


def search_panshare(keyword):
    """PanShare 引擎：委托给终极多源网盘并发搜索矩阵 (PanshareMatrix)，聚合快映、虎斑、蜡笔、多多等全部主流节点。"""
    try:
        from .panshare_matrix import PanshareMatrix
        return PanshareMatrix.search(keyword)
    except Exception as e:
        print(f"PanshareMatrix 调用异常: {e}", file=sys.stderr)
        return []


ROMAN_NUM_MAP = {
    'Ⅰ': '1', 'Ⅱ': '2', 'Ⅲ': '3', 'Ⅳ': '4', 'Ⅴ': '5',
    'Ⅵ': '6', 'Ⅶ': '7', 'Ⅷ': '8', 'Ⅸ': '9', 'Ⅹ': '10',
}

# 匹配所有 Unicode 格式控制符、零宽空格、方向标记、非标准空格（如 ​ 等）
INVISIBLE_CHARS_PATTERN = re.compile(r"[​-‏‪-‮﻿ 　\t\r\n]+")


def strip_invisible(text):
    """仅清除 Unicode 格式控制符/零宽空格/方向符/非标准空白，保留年份与片名原貌。

    与 sanitize_title 的区别：sanitize_title 用于「检索关键词」（需要去年份，
    否则网盘站按纯片名建索引会搜不到）；本函数用于「需要保留年份的场合」，
    如 strm 的 pending 占位参数、作品身份判定。
    """
    if not text:
        return ""
    s = INVISIBLE_CHARS_PATTERN.sub(" ", str(text))
    return re.sub(r"\s+", " ", s).strip()


def sanitize_title(text):
    """工业级文本正规化管道：
    1. 彻底清除 Unicode 格式控制符/零宽空格/方向符/非标准空格（彻底消灭 \\u200b 等）；
    2. 剥离末尾可能存在的年份标记，如 ' (2025)', '（2026）'；
    3. 规范化连续空白为单个空格并两端 strip。
    """
    if not text:
        return ""
    s = strip_invisible(text)
    s = re.sub(r"\s*[(（]\d{4}[)）]\s*$", "", s)
    return s.strip()


def normalize_keyword(kw):
    """规范化片名关键词（罗马数字替换、全角字符转换）。"""
    res = kw
    for r_char, a_num in ROMAN_NUM_MAP.items():
        res = res.replace(r_char, a_num)
    return res


def search(keyword, prefer_panshare=False):
    """统一搜索入口（2026-10-20 起只返回夸克网盘结果）。

    prefer_panshare=True 且 cookie 就绪时检索网盘分享；否则返回空。
    CMS 降级已彻底移除，多轮递进召回（年份变体/主标题/原始词）仅走网盘。
    """
    target_kw = sanitize_title(keyword)
    if not target_kw:
        target_kw = keyword.strip()

    norm_kw = normalize_keyword(target_kw)
    main_kw = re.split(r"[\s~～:：\-—_]+", target_kw)[0].strip()

    # 年份变体召回：网盘站常把年份拼在片名后（《爱你2025》），而纯片名
    # （《爱你》）会混入大量同名微短剧。2026-10-20 实测：《爱你》纯名搜索
    # 0 个合格源，而《爱你2025》一次命中 3 个正确 4K 源。
    year_variants = []
    m_year_kw = re.search(r"[(（](\d{4})[)）]\s*$", keyword or "")
    if m_year_kw:
        year_variants.append(f"{target_kw}{m_year_kw.group(1)}")

    results = []
    seen_shares = set()

    def _append_unique(res_list):
        for r in res_list:
            # 2026-10-20 修复：去重键必须纳入 media_type。
            # 苹果 CMS 同一关键词会返回多条同名但不同类型的条目
            # （《爱你》同时是「剧情片」和「国产剧」），旧实现用
            # (source, name) 做 key，导致后被解析的国产剧被同名剧情片挤掉，
            # 观众点剧集时只能看到电影版（或反之）。
            key = r.share_url or r.play_url or (r.source, r.name, r.media_type)
            if key not in seen_shares:
                seen_shares.add(key)
                results.append(r)

    if prefer_panshare and config.COOKIE_READY:
        _append_unique(search_panshare(target_kw))
        if norm_kw != target_kw:
            _append_unique(search_panshare(norm_kw))
        if main_kw and main_kw != target_kw and len(main_kw) >= 2:
            if not any(score_search_result(r, keyword) >= 30.0 for r in results):
                _append_unique(search_panshare(main_kw))
        for yv in year_variants:
            if not any(score_search_result(r, keyword) >= 30.0 for r in results):
                _append_unique(search_panshare(yv))

    # 2026-10-20：CMS 降级已彻底移除。搜索仅返回夸克网盘（panshare）结果，
    # 不再请求苹果 CMS 采集站。多轮递进召回（年份变体/主标题/原始词）全部只走网盘。
    if not any(score_search_result(r, keyword) >= 30.0 for r in results):
        if prefer_panshare and config.COOKIE_READY and target_kw != keyword:
            _append_unique(search_panshare(keyword))

    return results


STOP_WORDS = {
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "with", "for", "by", "at", "from",
    "season", "part", "ep", "episode", "movie", "animation", "tv", "hd", "4k", "1080p", "720p", "2160p",
    "web-dl", "edr", "hdr", "sdr", "全集", "精选", "国语", "粤语", "中字", "双字", "无删减", "剪辑版", "未删减", "完整版",
}

SERIES_MARKERS = [
    r"[（\(]?\s*\d+\s*集\s*[）\)]?",
    r"全\s*\d+\s*集",
    r"第\s*[0-9一二三四五六七八九十]+\s*[季部期集话]",
    r"[._\-\s]S0*\d+",
    r"[._\-\s]EP?0*\d+",
    r"短剧",
    r"连载",
    r"更新至",
]

TEMPLATE_NOISE_REGEX = re.compile(
    r"(?:剧情介绍|在线观看|免费观看|高清全集|4K\[臻彩\]|4K|臻彩|超清|高清|1080[pP]|2160[pP]|WEB-DL|HDR|EDR|夸克网盘|迅雷下载|网盘下载|全集|完结|更新至\d+集?|第\d+集|--.*|-.*$)",
    re.IGNORECASE
)


def _strip_template_noise(text):
    return TEMPLATE_NOISE_REGEX.sub("", text or "").strip()


def _clean_title_text(text):
    return re.sub(r"[\s\.\-_:：~～!！?？\(\)\[\]（）【】\'\"`]+", "", text or "").lower()


CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


# ── 合法片名限定后缀白名单（2026-10-20 全库校对新增）──
# 国产正片常在主片名后追加「限定语」以区分版本/篇章，这类后缀属于同一部作品；
# 而「恶人传记」「功夫旋风儿」这类把主片名当作词根另起新名的，才是真异名。
LEGAL_SUFFIX_TOKENS = [
    # 发行形态
    "剧版", "影版", "电影版", "电视剧版", "网络版", "电视版", "完整版", "未删减版",
    "导演剪辑版", "加长版", "精华版", "纯享版",
    # 篇章 / 续作标识
    "篇", "部", "季", "章", "卷", "篇终章",
    # 画质规格（常见于盘搜标题）
    "臻彩", "4k", "4K", "2160", "1080", "高清", "超清", "hd", "HD",
    # 年份（《凡人修仙传2025》《繁花1990》）
]


def _is_legal_title_suffix(suffix):
    """判断片名主标题之后的后缀是否为「合法限定语」而非「另起新名」。

    合法示例（应判同一作品，85 分）：
      苍兰诀(剧版)      → suffix='剧版'     → 合法
      唐朝诡事录之长安   → suffix='之长安'   → 篇章地名，合法
      凡人修仙传2025     → suffix='2025'     → 年份，合法
      繁花1990          → suffix='1990'      → 年份，合法
      长相思第二季       → suffix='第二季'    → 季数，合法
    非法示例（应判不同作品，20 分）：
      恶人传记          → suffix='传记'      → 另起新名
      功夫旋风儿        → suffix='旋风儿'    → 另起新名
    """
    if not suffix:
        return True
    sfx = suffix.strip()

    # 1) 纯年份
    if re.fullmatch(r"(?:19|20)\d{2}", sfx):
        return True
    # 2) 纯季数（第二季/第2季/2/S02）
    if re.fullmatch(r"第?\s*[0-9一二三四五六七八九十]+\s*[季部]?", sfx):
        return True
    # 3) 篇章地名：「之长安」「之月升沧海」「外海风云」等
    if sfx.startswith("之"):
        return True
    # 4) 白名单词缀命中（画质规格/发行形态）
    low = sfx.lower()
    if any(tok.lower() in low for tok in LEGAL_SUFFIX_TOKENS):
        return True
    # 5) 括号包裹的限定语整体视为合法：(剧版)/(臻彩)/(2025)
    if re.fullmatch(r"[(（].{1,12}[)）]", sfx):
        return True
    return False


def _extract_season_from_name(name):
    """从候选名称中提取季数；无季数标记返回 None。

    覆盖：第二季 / 第2季 / Season 2 / S02 / 2nd Season / 第四季 Part 2。
    注意：返回 None 表示「未标注季数」，通常视为第一季（由调用方决定）。
    """
    t = name or ""
    m = re.search(r"第\s*([0-9]{1,2}|[一二两三四五六七八九十])\s*季", t)
    if m:
        v = m.group(1)
        return int(v) if v.isdigit() else CN_NUM.get(v)
    m = re.search(r"[Ss]eason\s*([0-9]{1,2})", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(?:^|[\s._\-\[\(])S([0-9]{1,2})(?:[E\s._\-\]\)]|$)", t)
    if m:
        return int(m.group(1))
    m = re.search(r"([0-9]{1,2})(?:st|nd|rd|th)\s*Season", t, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def score_search_result(r, title, year=None, season=0, episode=0):
    """计算搜索结果与目标标题的相关度得分。完全不相关或弱匹配返回 -1。"""
    core = sanitize_title(title)
    if not core:
        return -1
    rn = sanitize_title(r.name or "")
    if not rn:
        return -1

    # ── 年份硬校验（2026-10-20 全库校对）──
    # 网盘站标题常把年份直接拼在片名后（《十月围城2014》vs《十月围城 2009》），
    # 旧逻辑会因「额外数字后缀」误判，或直接忽略年份差异导致串片。
    want_year = None
    if year:
        want_year = str(year).strip()
    else:
        m_wy = re.search(r"[(（](\d{4})[)）]\s*$", title or "")
        if m_wy:
            want_year = m_wy.group(1)
    if want_year and want_year.isdigit():
        # 候选名中显式出现的 4 位年份
        cand_years = re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", rn)
        if cand_years:
            try:
                if all(abs(int(y) - int(want_year)) > 1 for y in cand_years):
                    return -1
            except ValueError:
                pass
        # CMS 侧的结构化年份字段
        ry = str(getattr(r, "year", "") or "").strip()
        if ry.isdigit() and abs(int(ry) - int(want_year)) > 1:
            return -1

    # ── 影视类型硬隔离（2026-10-20《爱你》事故根治）──
    # CMS 的 type_name 是判断「剧集 / 电影」最权威的信号，而片名完全相同
    # （《爱你》既是 2025 国产剧，也是 2025 剧情片）。若不做类型校验，
    # 剧集点播会命中同名的「剧情片·正片」并被当作第 1 集播放。
    mt = getattr(r, "media_type", "") or ""
    if mt:
        # 注意：「剧情片」「动作片」以“片”结尾但含“剧”字，必须先判电影再判剧集，
        # 否则类型隔离对《爱你》（剧情片 vs 国产剧）完全失效。
        if mt.endswith("片") or "电影" in mt:
            is_movie_meta, is_tv_meta = True, False
        elif re.search(r"剧集|电视剧|剧$|动漫|动画|综艺|番", mt):
            is_movie_meta, is_tv_meta = False, True
        else:
            is_movie_meta = is_tv_meta = False
        want_tv = (season > 0)
        if want_tv and is_movie_meta:
            return -1
        if (not want_tv) and is_tv_meta:
            return -1

    # ── 分季硬隔离（2026-10-20《钻石王牌》事故根治）──
    # 请求 S02 时，绝不能命中「第三季 / 第四季 / OAD」等其它季的包。
    # 旧实现只看片名，导致 S02E20 被写入第三季第 20 集。
    if season > 0:
        req_season = int(season)
        cand_season = _extract_season_from_name(rn)
        # 未标注季数的资源视为第一季（如「钻石王牌」2013 = 第 1 季）
        if cand_season is None:
            cand_season = 1
        if cand_season != req_season:
            return -1
        # 季数已匹配 → 剥离季数标记后再做片名比对，
        # 避免「钻石王牌 第二季」被当成「钻石王牌」+ 额外汉字后缀而降分。
        if cand_season > 1:
            rn = re.sub(r"第\s*[0-9一二两三四五六七八九十]+\s*季", "", rn)
            rn = re.sub(r"[Ss]eason\s*[0-9]{1,2}", "", rn)
            rn = re.sub(r"[0-9]{1,2}(?:st|nd|rd|th)\s*Season", "", rn, flags=re.IGNORECASE)
            rn = re.sub(r"\s+", " ", rn).strip()

    # 全时段短剧拦截（杜绝竖屏短剧冒充正规电视剧）：
    # 若候选名包含“（80集）”、“83集”、“短剧”、“微短剧”，而目标片名本身不含该词：
    if re.search(r"[（\(]?\s*\d{2,3}\s*集\s*[）\)]?", rn) or "短剧" in rn or "微短剧" in rn:
        if "短剧" not in core and not re.search(r"\d{2,3}\s*集", core):
            return -1

    # 特别篇 / OAD / 番外 / 剧场版 隔离：这些是独立作品，不属于正片某季某集。
    # 2026-10-20 全库校对发现《钻石王牌 OAD》被打 100 分并可能抢占正片。
    # 注意：中文与英文之间不存在 \b 边界（都是 \w），故不能用 \bOAD\b。
    _sp_re = r"OAD|OVA|特别篇|番外篇|剧场版|总集篇|特典"
    if re.search(_sp_re, rn, re.IGNORECASE):
        if not re.search(_sp_re, core, re.IGNORECASE):
            return -1

    # 电影形态硬隔离：若点播为电影 (season=0, episode=0)
    is_movie = (season == 0 and episode == 0)
    if is_movie:
        # 如果候选名称包含明显剧集/短剧特征（如（70集）、短剧、全集等），而目标标题本身不含此类词
        for sm in SERIES_MARKERS:
            if re.search(sm, rn, re.IGNORECASE) and not re.search(sm, core, re.IGNORECASE):
                return -1  # 坚决杜绝短剧/多集剧集冒充单部电影

    # 剥离网页抓取模板噪音（如 "剧情介绍-"、"4K[臻彩]" 等）
    clean_core = _strip_template_noise(core)
    clean_rn = _strip_template_noise(rn)

    c_core = _clean_title_text(clean_core)
    c_rn = _clean_title_text(clean_rn)
    if not c_core or not c_rn:
        return -1

    zh_in_core = "".join(re.findall(r"[一-龥]+", clean_core))
    zh_in_rn = "".join(re.findall(r"[一-龥]+", clean_rn))

    # 前缀篡改双向硬拦截：
    #   正向（候选比目标多前缀）：目标「赴山海」 vs 候选「以歌赴山海」→ 山寨蹭名，剔除
    #   反向（目标比候选多前缀）：目标「以歌赴山海」 vs 候选「赴山海」→ 同样不是同一部，剔除
    # 2026-10-20 修复：原规则只判正向，导致点播山寨片名时反把正剧资源配给它（实测 62 分）。
    if zh_in_core and zh_in_rn:
        if zh_in_core != zh_in_rn and zh_in_core in zh_in_rn and not zh_in_rn.startswith(zh_in_core):
            return -1
        if zh_in_core != zh_in_rn and zh_in_rn in zh_in_core and not zh_in_core.startswith(zh_in_rn):
            return -1

    # 1. 严格相等或纯中文核心完全一致
    if c_core == c_rn or (zh_in_core and zh_in_core == zh_in_rn):
        s = 100.0
    else:
        # 2. 提取主标题（剔除副标题 ～、: 等）
        main_title = re.split(r"[\s~～:：\-—_]+", clean_core)[0].strip()
        c_main = _clean_title_text(main_title)

        # 若 r_name 以 c_main 前缀匹配（例如 "功夫.Kung.Fu.Hustle.2004..."），符合高质量电影资源命名
        if c_main and (c_main == c_rn or c_rn.startswith(c_main)):
            if zh_in_core and zh_in_rn and zh_in_rn != zh_in_core and len(zh_in_rn) > len(zh_in_core):
                # 2026-10-20 分级制：不再一刀切判 20 分。
                # 国产正片名常带【合法限定后缀】，与真正的异名作品混在一起无法用长度区分，
                # 故拆成白名单/黑名单两级：
                #   白名单（合法限定）→ 85 分：如 苍兰诀(剧版)、唐朝诡事录之长安、凡人修仙传2025
                #   黑名单（真异名）  → 20 分：如 恶人传记、功夫旋风儿、功夫女足
                suffix = zh_in_rn[len(zh_in_core):] if zh_in_rn.startswith(zh_in_core) else ""
                if _is_legal_title_suffix(suffix):
                    s = 85.0
                else:
                    s = 20.0
            else:
                s = 85.0
        elif zh_in_core and zh_in_rn and zh_in_core == zh_in_rn:
            s = 80.0
        elif c_main and c_main in c_rn:
            # 只有在非前缀匹配时，严格限制中文长度比率，杜绝短词被泛词短剧/综艺弱命中
            ratio_zh = len(zh_in_core) / len(zh_in_rn) if zh_in_rn else 0
            if ratio_zh >= 0.4:
                s = 50.0 + ratio_zh * 30.0
            else:
                return -1
        # 3. 互相为子串
        elif c_core in c_rn:
            ratio = len(c_core) / len(c_rn)
            # 2026-10-20：子串命中且长度占比过低（如「功夫」命中「功夫女足之外星人篇」）
            # 属于不同作品，必须低于 30 分门槛，杜绝同名不同片串播。
            s = (60.0 + ratio * 20.0) if ratio >= 0.5 else 20.0
        elif c_rn in c_core:
            ratio = len(c_rn) / len(c_core)
            if ratio >= 0.4:
                s = 50.0 + ratio * 20.0
            else:
                return -1
        else:
            # 4. 中文核心词命中校验（严禁仅靠英文停用词如 THE 弱匹配）
            zh_words = [w for w in re.findall(r"[一-龥]{2,}", core) if w not in STOP_WORDS]
            en_words = [w.lower() for w in re.findall(r"[A-Za-z0-9]{3,}", core) if w.lower() not in STOP_WORDS]
            if zh_words:
                matched_zh = [w for w in zh_words if _clean_title_text(w) in c_rn]
                if not matched_zh:
                    return -1
                total_zh_len = sum(len(w) for w in zh_words)
                matched_zh_len = sum(len(w) for w in matched_zh)
                if matched_zh_len / total_zh_len < 0.4:
                    return -1
                s = 30.0 + (matched_zh_len / total_zh_len) * 20.0
            elif en_words:
                matched_en = [w for w in en_words if w in c_rn]
                if not matched_en:
                    return -1
                total_en_len = sum(len(w) for w in en_words)
                matched_en_len = sum(len(w) for w in matched_en)
                if matched_en_len / total_en_len < 0.5:
                    return -1
                s = 30.0 + (matched_en_len / total_en_len) * 20.0
            else:
                return -1

    if year and str(year) == str(r.year):
        s += 30.0
    s += min(len(r.episodes), 50) / 100.0
    return s


def match_best(results, title, year=None, season=0, episode=0):
    """从搜索结果中挑最匹配的一条。

    2026-10-20 修复引擎优先级倒挂：CMS 结果因集数多会多拿 0.5 分奖励
    （`min(len(episodes),50)/100`），而网盘源 episodes 恒为空，导致
    「同为 100 分的正确网盘 4K 源」被「100.3 分的 CMS 死链」挤掉，
    最终降级到不可播的第三方切片。现在：
      1) 网盘源（panshare）优先于 CMS —— 网盘画质/稳定性自己可控；
      2) 同引擎内再按得分排序。
    """
    if not results:
        return None
    scored = [(score_search_result(r, title, year=year, season=season, episode=episode), i, r)
              for i, r in enumerate(results)]

    # 2026-11-13：边界分数的一次性 AI 语义复核（见 title_judge.py）。
    # 只对 15~90 分的候选做整批一次请求；100 分（清洗后标题完全相等）不进模型。
    # 注意 85 分档（白名单命中）也在此区间——那正是规则最需要语义判断的地方。
    # 任何异常均返回空覆盖表，行为与改造前完全一致。
    try:
        from . import title_judge
        overrides = title_judge.judge(results, title, season=season, episode=episode)
    except Exception:
        overrides = {}
    if overrides:
        scored = [(overrides.get(id(r), sc), i, r) for sc, i, r in scored]

    passed = [t for t in scored if t[0] >= 30.0]
    if not passed:
        return None
    passed.sort(key=lambda t: (0 if t[2].engine == "panshare" else 1, -t[0], t[1]))
    return passed[0][2]


def is_m3u8_alive(url, timeout=6.0):
    """探测 m3u8 是否真实可用（200 且内容合法即活）。

    2026-10-20 调优：超时从 2.5s 提到 6.0s。实测部分 CDN（bfikuncdn 等）
    首包延迟达 3.7s，2.5s 会把活链误判为死链并写入 3 天黑名单，
    造成「明明有源却反复降级失败」。
    """
    if not url or not url.startswith("http"):
        return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                head = resp.read(128).decode("utf-8", "ignore")
                return "#EXTM3U" in head or len(head) > 20
    except Exception:
        pass
    return False


def find_best_playable_cms(title, season=0, episode=0):
    """搜索所有相关 CMS 源并逐一探测，挑选首个真实 200 可播且相关度最高的直链 (多源轮换，杜绝 404 死链)。

    2026-10-20 强化（全库校对发现的两类系统性错配）：
      ① 集数存在性前置校验：剧集点播时，候选必须真实包含该集，否则跳过。
         旧实现依赖 url_for 回落 play_url，导致《爱你》的「剧情片·正片」
         被当成国产剧第 1 集写入 strm。
      ② 年度校验：片名相同但年份差超过 1 年（《功夫 2004》vs《功夫旋风儿 1984》）
         直接判为不同作品，杜绝同名不同片。
    """
    import re
    kw_core = re.sub(r"\s*[(（]\d{4}[)）]\s*$", "", title or "").strip()
    target_kw = kw_core if kw_core else title
    want_year = None
    m_year = re.search(r"[(（](\d{4})[)）]\s*$", title or "")
    if m_year:
        want_year = m_year.group(1)

    norm_kw = normalize_keyword(target_kw)

    cms_results = search_cms(target_kw)
    if norm_kw != target_kw:
        cms_results.extend(search_cms(norm_kw))
    if not cms_results and target_kw != title:
        cms_results = search_cms(title)
    if not cms_results:
        return None, None

    dead_cms = db.get_dead_sources_set("cms")
    scored = []
    seen_urls = set()
    for r in cms_results:
        # ① 剧集必须真实包含该集（禁止回落 play_url）
        if season > 0 and episode > 0:
            has_ep = any(ep == episode for ep, _u in (r.episodes or []))
            if not has_ep:
                continue
        # ② 年度硬校验
        if want_year and r.year and str(r.year).strip().isdigit():
            try:
                if abs(int(r.year) - int(want_year)) > 1:
                    continue
            except ValueError:
                pass

        ep_url = r.url_for(episode if season else 0)
        if ep_url and ep_url not in dead_cms and ep_url not in seen_urls:
            seen_urls.add(ep_url)
            sc = score_search_result(r, title, season=season, episode=episode)
            # 严格门槛：得分必须 >= 30 分才允许进入降级候选列表，严禁张冠李戴
            if sc >= 30.0:
                scored.append((sc, r, ep_url))

    scored.sort(key=lambda t: t[0], reverse=True)

    for sc, r, u in scored:
        if is_m3u8_alive(u):
            return r, u
        else:
            # 探测不通（如 404 / 限制区域），拉黑 3 天，后续跳过不再盲目探测
            db.mark_dead_source(u, "cms", "m3u8 unplayable / 404", ban_days=3)
    return None, None


def resolve_for_play(cat, title, season, episode, prefer_panshare=False):
    """为一次播放解析片源。

    返回 (SearchResult, url)。找不到返回 (None, None)。
    """
    results = search(title, prefer_panshare=prefer_panshare)
    best = match_best(results, title, season=season, episode=episode)
    if not best:
        return None, None
    url = best.url_for(episode if season else 0)
    if not url:
        return best, None
    return best, url
