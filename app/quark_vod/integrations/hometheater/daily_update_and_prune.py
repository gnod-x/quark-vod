#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bounded daily discovery, episode updates, library refresh and protected cleanup. Writes a durable stage report; incomplete or deferred stages return nonzero."""

from quark_vod.integrations.hometheater import paths
from quark_vod import config as host_config

import datetime
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
import time
import shutil
import sqlite3
from contextlib import closing
import subprocess
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

BASE = paths.BASE_DIR
from quark_vod.integrations.hometheater import tmdb_populate
from quark_vod import db as vod_db, strm_manager
from quark_vod.coordination import maintenance, Busy

CINEMA2 = paths.CINEMA2_DIR
DATA_DIR = paths.DATA_DIR
LOG_DIR = paths.LOG_DIR
QUARK_DB = str(__import__("quark_vod.config", fromlist=["STATE_DB"]).STATE_DB)
JF_DB = paths.JELLYFIN2_DB

# ── 配置策略 ──────────────────────────────────────────
# 单专区最大保留影片/剧集数（超过时触发淘汰，剧集整部算 1 部）
MAX_PER_CATEGORY = host_config._number("VOD_HOST_MAX_PER_CATEGORY", 200, 1, 100000)
# 永久保护免淘汰专区（经典永存）
PROTECTED_CATEGORIES = set()  # 港台经典不设免死保护，所有专区均受配额与淘汰治理
TV_CATEGORIES = {"国产剧集", "欧美剧集", "热门韩剧", "国产动漫", "日本动漫", "日本剧集", "泰国剧集"}
LIBRARY_REFRESH_MAX_WAIT = 2700

CURRENT_YEAR = datetime.datetime.now().year
_YEAR_RE = re.compile(r"\((\d{4})\)$")


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [DailyPrune] {msg}", flush=True)


def get_item_year(full_path):
    """解析影视资源的上映/首播年份（先查目录名，再查 nfo 标签）。"""
    base_name = os.path.basename(full_path)
    m = _YEAR_RE.search(base_name)
    if m:
        return int(m.group(1))

    for nfo_name in ("tvshow.nfo", "movie.nfo"):
        nfo_p = os.path.join(full_path, nfo_name)
        if os.path.isfile(nfo_p):
            try:
                tree = ET.parse(nfo_p)
                root = tree.getroot()
                y = root.findtext("year")
                if y and y.isdigit():
                    return int(y)
                for date_tag in ("premiered", "aired", "releasedate"):
                    p = root.findtext(date_tag)
                    if p and len(p) >= 4 and p[:4].isdigit():
                        return int(p[:4])
            except Exception:
                pass
    return None


def get_protected_titles():
    """获取所有受保护（不可删除）的影片标题集合。包含用户收藏、在看/已看、一键预热以及置顶项目。"""
    from contextlib import closing
    protected = set()
    for path in (JF_DB, QUARK_DB):
        if not os.path.isfile(path):
            raise RuntimeError('保护状态数据库缺失或不可访问，停止清理')

    # 1. 查询 jellyfin.db 中已收藏、在看或已看项目（免锁只读）
    if os.path.exists(JF_DB):
        try:
            with closing(sqlite3.connect(f"file:{JF_DB}?mode=ro", uri=True, timeout=2)) as con:
                cur = con.cursor()
                cur.execute("""
                    SELECT b.Type, b.Name, b.SeriesName FROM BaseItems b
                    JOIN UserData u ON b.Id = u.ItemId
                    WHERE u.IsFavorite = 1 OR (u.PlaybackPositionTicks > 0 AND u.Played = 0)
                """)
                for row in cur.fetchall():
                    item_type, name, series_name = row
                    if str(item_type or '').endswith('.Episode') and not str(series_name or '').strip():
                        raise RuntimeError('无法确认受保护剧集的所属作品')
                    if name:
                        protected.add(name.strip())
                    if series_name:
                        protected.add(series_name.strip())
        except Exception as e:
            raise RuntimeError("读取 Jellyfin 保护状态失败，停止清理") from e

    # 2. 查询 quark_plays.db 中用户手动置顶/一键预热保护的项目 (user_pinned)
    try:
        if vod_db and hasattr(vod_db, "get_all_pinned_titles"):
            pinned = vod_db.get_all_pinned_titles()
            protected.update(pinned)
    except Exception as e:
        raise RuntimeError("读取置顶保护失败，停止清理") from e

    # 3. 查询 quark_plays.db 中历史点播记录 (plays) 与有效转存 (transfers)
    if os.path.exists(QUARK_DB):
        try:
            with closing(sqlite3.connect(f"file:{QUARK_DB}?mode=ro", uri=True, timeout=2)) as con:
                cur = con.cursor()
                for row in cur.execute("SELECT t.title FROM playback_access a JOIN transfers t ON t.id=a.transfer_id WHERE a.accessed_at >= strftime('%s','now') - 21600").fetchall():
                    if row[0]:
                        t = row[0].rsplit(" (", 1)[0].strip() if " (" in row[0] else row[0].strip()
                        protected.add(t)
                        protected.add(row[0].strip())
                for row in cur.execute("SELECT title FROM transfers WHERE status IN ('done', 'saving', 'searching', 'pending')").fetchall():
                    if row[0]:
                        t = row[0].rsplit(" (", 1)[0].strip() if " (" in row[0] else row[0].strip()
                        protected.add(t)
                        protected.add(row[0].strip())
        except Exception as e:
            raise RuntimeError("读取转存保护失败，停止清理") from e

    # Recent playback in the last six hours is a protection signal independent
    # of Jellyfin UserData, whose PlaybackPositionTicks can be absent in UI probing.
    try:
        from contextlib import closing
        with closing(sqlite3.connect(f"file:{QUARK_DB}?mode=ro", uri=True, timeout=2)) as con:
            rows = con.execute("""SELECT DISTINCT t.title FROM playback_access a
                JOIN transfers t ON t.id=a.transfer_id
                WHERE a.accessed_at >= strftime('%s','now') - 21600""").fetchall()
            protected.update(row[0] for row in rows if row and row[0])
    except sqlite3.Error as exc:
        raise RuntimeError('近期播放保护状态不可用') from exc
    return protected


def is_item_active_or_done(full_path, cat, name):
    """检查目录内是否包含已转存 (done) 的正片或 live 直链，坚决杜绝误删用户已转存/正在观看内容。"""
    try:
        for root, _, files in os.walk(full_path):
            for f in files:
                if f.endswith(".strm"):
                    p = os.path.join(root, f)
                    with open(p, "r", encoding="utf-8", errors="ignore") as fp:
                        c = fp.read().strip()
                        if not strm_manager.is_pending(c) or urllib.parse.urlsplit(c).path != '/pending':
                            return True
    except Exception:
        return True  # unreadable files are not safe deletion candidates
    return False


@maintenance
def prune_category(cat, protected_titles, limit=MAX_PER_CATEGORY, current_year=CURRENT_YEAR):
    """对单专区执行年度过滤与配额检查淘汰：
    第一阶段：非当前年度（太老）且非用户一键预热/点播的占位资源彻底淘汰清理；
    第二阶段：若当前年度存量仍超出单专区配额上限 (limit)，按 LRU 淘汰热度最低的 pending 占位。
    """
    cat_dir = os.path.join(CINEMA2, cat)
    if not os.path.isdir(cat_dir):
        return 0

    if cat in PROTECTED_CATEGORIES:
        log(f"专区【{cat}】属于经典保护区，跳过淘汰")
        return 0

    entries = [d for d in os.listdir(cat_dir) if not d.startswith(".")]
    pruned = 0
    failures = 0

    # ── 第一阶段：非本年度历史老旧占位资源清理 ──────────────────────────
    remaining_entries = []
    for d in entries:
        full_p = os.path.join(cat_dir, d)
        if not os.path.isdir(full_p):
            continue

        clean_name = d.rsplit(" (", 1)[0].strip() if " (" in d else d
        is_user_protected = (
            d in protected_titles or
            clean_name in protected_titles or
            is_item_active_or_done(full_p, cat, clean_name)
        )

        item_year = get_item_year(full_p)
        # 如果年份早于当前年度，且未受用户预热/在看保护，彻底淘汰清理
        if item_year is not None and item_year < current_year - 2 and not is_user_protected:
            try:
                shutil.rmtree(full_p)
                log(f"已清理滚动三年窗口之外的资源: 【{cat}】{d} (年份: {item_year})")
                if os.path.exists(QUARK_DB):
                    try:
                        with sqlite3.connect(QUARK_DB) as c:
                            vod_db.remove_strm_maps_below(full_p, c)
                    except Exception:
                        failures += 1
                pruned += 1
            except Exception as e:
                failures += 1
                log(f"删除老旧资源失败 {full_p}: {type(e).__name__}")
        else:
            remaining_entries.append((d, full_p, clean_name, is_user_protected))

    # ── 第二阶段：配额超限治理 (LRU 智能淘汰) ───────────────────────
    rem_total = len(remaining_entries)
    if rem_total > limit:
        excess = rem_total - limit
        log(f"专区【{cat}】清理后尚存 {rem_total} 部，超出配额上限 {excess} 部，执行配额淘汰")
        candidates = []
        for d, full_p, clean_name, is_user_protected in remaining_entries:
            if is_user_protected:
                continue
            mtime = os.path.getmtime(full_p)
            try:
                nfo = next((os.path.join(full_p, f) for f in ('movie.nfo','tvshow.nfo')
                            if os.path.isfile(os.path.join(full_p,f))), None)
                rating = float(ET.parse(nfo).getroot().findtext('rating') or 0) if nfo else 0.0
                audit = vod_db._conn().execute(
                    "SELECT MAX(is_4k) FROM source_audits WHERE cat=? AND title=? AND has_quark_source=1",
                    (cat, d)).fetchone()
                has_4k = int(bool(audit and audit[0]))
            except Exception:
                # Unknown source or malformed metadata cannot justify eviction.
                continue
            candidates.append(((has_4k, rating, mtime), d, full_p))

        candidates.sort(key=lambda x: x[0])
        to_delete = candidates[:excess]
        for _, name, path in to_delete:
            try:
                shutil.rmtree(path)
                log(f"已按配额淘汰资源: 【{cat}】{name}")
                if os.path.exists(QUARK_DB):
                    try:
                        with sqlite3.connect(QUARK_DB) as c:
                            vod_db.remove_strm_maps_below(path, c)
                    except Exception:
                        failures += 1
                pruned += 1
            except Exception as e:
                failures += 1
                log(f"配额删除失败 {path}: {type(e).__name__}")

    remaining_count = len([d for d in os.listdir(cat_dir) if not d.startswith(".")])
    log(f"专区【{cat}】清理完成，共淘汰/释放 {pruned} 部资源，现保留 {remaining_count} 部 (近三年作品+预热保留)")
    if failures:
        raise RuntimeError(f'专区清理未完成: {failures}')
    return pruned


@maintenance
def prune_ai_shows():
    """全库扫描并自动淘汰清理所有 AI剧/AI短剧/AIGC 劣质生成条目。"""
    log("=== 开始执行 AI剧 / AIGC 劣质资源智能清理过滤 ===")
    total_ai_pruned = 0
    failures = 0
    protected = get_protected_titles()
    if not os.path.isdir(CINEMA2):
        return 0

    # 预加载 Jellyfin 标签缓存以实现高效判定
    jf_tags_cache = {}
    tmdb_key = None
    try:
        tmdb_key = tmdb_populate.read_tmdb_key()
    except Exception:
        pass
    if os.path.exists(JF_DB):
        try:
            with closing(sqlite3.connect(f"file:{JF_DB}?mode=ro", uri=True, timeout=2)) as con:
                for row in con.cursor().execute("SELECT Path, Tags FROM BaseItems WHERE Tags IS NOT NULL"):
                    if row[0] and row[1]:
                        norm_p = row[0].replace("/cinema2", CINEMA2)
                        jf_tags_cache[norm_p] = [t.strip().lower() for t in row[1].split("|")]
        except Exception:
            pass

    for cat in os.listdir(CINEMA2):
        cat_dir = os.path.join(CINEMA2, cat)
        if not os.path.isdir(cat_dir) or cat.startswith("."):
            continue
        for show_dir_name in sorted(os.listdir(cat_dir)):
            if show_dir_name.startswith("."):
                continue
            show_path = os.path.join(cat_dir, show_dir_name)
            if not os.path.isdir(show_path):
                continue
            clean_name = show_dir_name.rsplit(' (', 1)[0]
            if show_dir_name in protected or clean_name in protected or is_item_active_or_done(show_path, cat, show_dir_name):
                continue
            # Only unprotected pending entries may be filtered.

            is_ai, reason = tmdb_populate.is_ai_directory(show_path, jf_tags_cache=jf_tags_cache, cat=cat, tmdb_key=tmdb_key)
            # 对于电影，检查内部 strm 在 jf_tags_cache 中的标签
            if not is_ai:
                for f in os.listdir(show_path):
                    if f.endswith(".strm"):
                        strm_full = os.path.join(show_path, f)
                        strm_tags = jf_tags_cache.get(strm_full, [])
                        if any(t in tmdb_populate.AI_KEYWORD_NAMES for t in strm_tags):
                            is_ai = True
                            reason = f"strm_tag:{strm_tags}"
                            break

            if is_ai:
                try:
                    shutil.rmtree(show_path)
                    log(f"已清理 AI 影视资源: 【{cat}】{show_dir_name} (原因: {reason})")
                    if os.path.exists(QUARK_DB):
                        try:
                            with sqlite3.connect(QUARK_DB) as c:
                                vod_db.remove_strm_maps_below(show_path, c)
                        except Exception:
                            failures += 1
                    total_ai_pruned += 1
                except Exception as e:
                    failures += 1
                    log(f"删除 AI 影视资源失败 {show_path}: {type(e).__name__}")

    log(f"=== AI剧清理完成，全库共移除 {total_ai_pruned} 部 AI剧/AIGC 劣质资源 ===")
    if failures:
        raise RuntimeError(f'内容过滤清理未完成: {failures}')
    return total_ai_pruned


def run_pruning():
    """全库执行存储配额与年份智能治理。"""
    log(f"=== 开始执行当前年度 ({CURRENT_YEAR}) 资源保留、历史老旧清理与上限 ({MAX_PER_CATEGORY}部/区) 智能治理 ===")
    # 0. 优先执行 AI 剧/AIGC 伪劣资源全量清理
    ai_pruned = prune_ai_shows()

    # 1. 清理连载更新中剧集内的未播出未来 pending 占位
    unaired_pruned = prune_unaired_pending_strms()

    protected = get_protected_titles()
    log(f"当前共锁定 {len(protected)} 部用户预热/在看/收藏保护影视（绝不删除）")

    total_pruned = ai_pruned + unaired_pruned
    if os.path.isdir(CINEMA2):
        for cat in os.listdir(CINEMA2):
            if os.path.isdir(os.path.join(CINEMA2, cat)) and not cat.startswith("."):
                total_pruned += prune_category(cat, protected, limit=MAX_PER_CATEGORY)

    log(f"=== 治理清理结束，全库共移除 {total_pruned} 部非当前年度/超额旧资源/AI资源/未更新占位 ===")
    return total_pruned


@maintenance
def prune_unaired_pending_strms(dry_run=False):
    """清理正在连载/更新中（status != 'Ended'）剧集内未来尚未播出的 pending 占位符。

    保护铁律：
    1. status == 'Ended' 的已完结剧集所有集数均为有效正片，绝不误删；
    2. live、未知/不可读入口以及用户保护条目保留；
    3. 仅清理已确认 pending 且 NFO 明确显示未来播出的单集，缺失信息保留；
    4. 同步移除关联的分集 nfo、剧照及 strm_map 数据库映射；
    5. 若清理后季目录或剧集目录完全为空，自动回收空目录（受保护剧集除外）。
    """
    log("=== 开始检查并清理连载剧集中未播出的未来占位 (消灭未更新占位) ===")
    today_str = datetime.date.today().isoformat()
    total_pruned_strms = 0
    failures = 0
    protected = get_protected_titles()

    if not os.path.isdir(CINEMA2):
        return 0

    to_delete_strm_paths = []

    for cat in os.listdir(CINEMA2):
        if cat not in TV_CATEGORIES:
            continue
        cat_dir = os.path.join(CINEMA2, cat)
        if not os.path.isdir(cat_dir):
            continue

        for show_dir_name in os.listdir(cat_dir):
            if show_dir_name.startswith("."):
                continue
            show_path = os.path.join(cat_dir, show_dir_name)
            if not os.path.isdir(show_path):
                continue

            nfo_path = os.path.join(show_path, "tvshow.nfo")
            if not os.path.isfile(nfo_path):
                continue

            try:
                tree = ET.parse(nfo_path)
                status = tree.getroot().findtext("status")
            except Exception:
                continue

            # 已完结剧集所有集数均已播出，绝不清理
            if status == "Ended":
                continue

            show_title = show_dir_name.rsplit(" (", 1)[0].strip() if " (" in show_dir_name else show_dir_name
            if show_dir_name in protected or show_title in protected:
                continue

            # 遍历该连载剧各季
            season_dirs = [d for d in os.listdir(show_path) if d.startswith("Season ") and os.path.isdir(os.path.join(show_path, d))]
            for s_dir_name in season_dirs:
                s_dir = os.path.join(show_path, s_dir_name)
                for f_name in os.listdir(s_dir):
                    if not f_name.endswith(".strm"):
                        continue
                    strm_path = os.path.join(s_dir, f_name)

                    # 1. 检查是否为已转存直链
                    try:
                        with open(strm_path, "r", encoding="utf-8") as f:
                            head = f.read().strip()
                    except (OSError, UnicodeError):
                        continue
                    if not strm_manager.is_pending(head) or urllib.parse.urlsplit(head).path != '/pending':
                        continue

                    # 2. 检查对应 nfo 的 aired 属性
                    base_name = f_name[:-5]
                    ep_nfo_path = os.path.join(s_dir, f"{base_name}.nfo")
                    ep_thumb_path = os.path.join(s_dir, f"{base_name}-thumb.jpg")

                    is_unaired = False
                    if os.path.isfile(ep_nfo_path):
                        try:
                            etree = ET.parse(ep_nfo_path)
                            aired = etree.getroot().findtext("aired")
                            is_unaired = datetime.date.fromisoformat((aired or '').strip()) > datetime.date.fromisoformat(today_str)
                        except Exception:
                            continue

                    if is_unaired:
                        if not dry_run:
                            try:
                                os.remove(strm_path)
                                to_delete_strm_paths.append(strm_path)
                                total_pruned_strms += 1
                                if os.path.isfile(ep_nfo_path):
                                    os.remove(ep_nfo_path)
                                if os.path.isfile(ep_thumb_path):
                                    os.remove(ep_thumb_path)
                            except Exception as err:
                                failures += 1
                                log(f"删除未更新占位异常 {strm_path}: {type(err).__name__}")
                        else:
                            total_pruned_strms += 1

                # 季目录若已无任何视频文件，回收该季目录
                if not dry_run and os.path.isdir(s_dir):
                    remaining_in_season = [x for x in os.listdir(s_dir) if not x.startswith(".")]
                    if not remaining_in_season or (len(remaining_in_season) == 1 and remaining_in_season[0] == "season.nfo"):
                        try:
                            shutil.rmtree(s_dir)
                        except Exception:
                            failures += 1

            # 剧集若已无任何有效季与视频集数，且非用户收藏/在看保护项，回收未开播剧集目录
            if not dry_run and os.path.isdir(show_path):
                remaining_strms = [
                    f for r, d, files in os.walk(show_path) for f in files if f.endswith(".strm")
                ]
                if not remaining_strms and show_title not in protected:
                    try:
                        shutil.rmtree(show_path)
                        log(f"回收无已播集数的未开播剧集目录: 《{show_title}》")
                    except Exception as err:
                        failures += 1
                        log(f"回收空目录异常 {show_path}: {type(err).__name__}")

    # 同步清理 strm_map 记录
    if to_delete_strm_paths and not dry_run:
        try:
            con = vod_db._conn()
            for p in to_delete_strm_paths:
                con.execute("DELETE FROM strm_map WHERE strm_path = ?", (p,))
            con.commit()
            log(f"已同步从 strm_map 移除 {len(to_delete_strm_paths)} 条未播出占位映射")
        except Exception as err:
            failures += 1
            log(f"清理 strm_map 异常: {type(err).__name__}")

    log(f"=== 未更新占位清理完成，共移除 {total_pruned_strms} 个未播出的未来 pending 占位符 ===")
    if failures:
        raise RuntimeError(f'未来占位清理未完成: {failures}')
    return total_pruned_strms


def sync_airing_episodes():
    """检索本地连载中的热播剧，向 TMDB 差量同步最新上线的集数（纯轻量占位，零预转存）。"""
    log("=== 开始检查未完结连载剧集的新集数更新 (追新同步) ===")
    key = tmdb_populate.read_tmdb_key()
    total_added_eps = 0
    failures = 0

    if not os.path.isdir(CINEMA2):
        return 0

    for cat in os.listdir(CINEMA2):
        if cat not in TV_CATEGORIES:
            continue
        cat_dir = os.path.join(CINEMA2, cat)
        if not os.path.isdir(cat_dir):
            continue

        for show_dir_name in os.listdir(cat_dir):
            if show_dir_name.startswith("."):
                continue
            show_path = os.path.join(cat_dir, show_dir_name)
            if not os.path.isdir(show_path):
                continue

            # 若为 AI 剧，直接跳过追新同步
            if tmdb_populate.is_ai_directory(show_path)[0]:
                continue

            nfo_path = os.path.join(show_path, "tvshow.nfo")
            if not os.path.isfile(nfo_path):
                continue

            tmdbid = None
            status = None
            title = show_dir_name.rsplit(" (", 1)[0].strip() if " (" in show_dir_name else show_dir_name
            try:
                tree = ET.parse(nfo_path)
                root = tree.getroot()
                tmdbid = root.findtext("tmdbid")
                status = root.findtext("status")
            except Exception:
                failures += 1
                continue

            if status == "Ended" or not tmdbid:
                continue

            try:
                url = f"https://api.themoviedb.org/3/tv/{tmdbid}?api_key={key}&language=zh-CN"
                detail = tmdb_populate.tmdb_object(url, 'seasons')
                if str(detail.get('id')) != str(tmdbid):
                    raise ValueError('mismatched TMDB identity')
            except Exception as e:
                failures += 1
                log(f"查询 TMDB 详情失败 《{title}》(id:{tmdbid}): {type(e).__name__}")
                continue

            latest_status = detail.get("status")
            show_failures = failures

            seasons = [s for s in detail.get("seasons", []) if s.get("season_number", 0) >= 1]
            total_show_eps = sum(s.get("episode_count") or 0 for s in seasons)
            if total_show_eps > 300:
                continue

            today_str = datetime.date.today().isoformat()

            for s in seasons:
                s_num = s.get("season_number")
                tmdb_ep_total = s.get("episode_count") or 0
                if tmdb_ep_total <= 0:
                    continue

                # 同步获取该季分集详情：随新集上线同步提取剧照 (still_path) 与分集剧情、播出日期
                ep_info_map = {}
                try:
                    s_url = f"https://api.themoviedb.org/3/tv/{tmdbid}/season/{s_num}?api_key={key}&language=zh-CN"
                    s_detail = tmdb_populate.tmdb_object(s_url, 'episodes')
                    for ep_obj in s_detail.get("episodes", []):
                        e_no = ep_obj.get("episode_number")
                        if e_no:
                            ep_info_map[e_no] = ep_obj
                    if not ep_info_map:
                        raise ValueError('empty season details')
                except Exception:
                    failures += 1
                    continue

                # 仅筛选已正式播出的有效集数（严禁为连载剧未来未播出集数创建虚假占位）
                aired_ep_list = [
                    e for e in range(1, tmdb_ep_total + 1)
                    if tmdb_populate.is_episode_aired(ep_info_map.get(e), s_num, e, detail, today_str)
                ]
                if not aired_ep_list:
                    continue

                s_dir = os.path.join(show_path, f"Season {s_num:02d}")
                os.makedirs(s_dir, exist_ok=True)

                # 检查哪些已播集数本地尚未创建
                missing_eps = []
                for ep in aired_ep_list:
                    ep_path = tmdb_populate.episode_strm_path(s_dir, title, s_num, ep)
                    base_ep = os.path.basename(ep_path)[:-5]
                    if not os.path.isfile(ep_path):
                        missing_eps.append((ep, ep_path, base_ep))

                if missing_eps:
                    log(f"发现新集上线！《{title}》第 {s_num} 季: 新增播出 {len(missing_eps)} 集 (最新播出至第 {max(aired_ep_list)} 集)")

                    created = []
                    for ep, ep_path, base_ep in missing_eps:
                        ep_data = ep_info_map.get(ep, {})
                        thumb_file = f"{base_ep}-thumb.jpg"
                        ep_nfo_file = f"{base_ep}.nfo"

                        thumb_path = os.path.join(s_dir, thumb_file)
                        ep_nfo_path = os.path.join(s_dir, ep_nfo_file)

                        # 1. 随追更同步下载分集高清剧照
                        still_p = ep_data.get("still_path")
                        if still_p and not os.path.isfile(thumb_path):
                            tmdb_populate.download_image(f"{tmdb_populate.IMG_BASE}/w500{still_p}", thumb_path)

                        # 2. 随追更同步生成单集 NFO
                        if not os.path.isfile(ep_nfo_path):
                            tmdb_populate.gen_episode_nfo(ep_nfo_path, ep_data.get("name"), s_num, ep, ep_data)

                        # 3. 写入占位 strm
                        if tmdb_populate.write_pending_strm(ep_path, cat, show_dir_name, s_num, ep):
                            created.append((cat, show_dir_name, s_num, ep, ep_path))

                    if created:
                        try:
                            con = vod_db._conn()
                            for c, t, s_idx, e_idx, p in created:
                                con.execute(
                                    "INSERT OR REPLACE INTO strm_map (cat, title, season, episode, strm_path) VALUES (?, ?, ?, ?, ?)",
                                    (c, t, s_idx, e_idx, p)
                                )
                            con.commit()
                        except Exception as err:
                            failures += 1
                            log(f"strm_map 登记异常: {type(err).__name__}")
                        total_added_eps += len(created)

            # Mark ended only after successful season processing, so failures
            # are retried on the next run instead of permanently skipped.
            if latest_status == 'Ended' and failures == show_failures:
                status_node = root.find('status')
                if status_node is None:
                    status_node = ET.SubElement(root, 'status')
                status_node.text = 'Ended'
                tmp = nfo_path + '.tmp'
                tree.write(tmp, encoding='utf-8', xml_declaration=True)
                os.replace(tmp, nfo_path)
            time.sleep(0.05)

    log(f"=== 连载追新检查结束，追加 {total_added_eps} 个占位，失败 {failures} 项 ===")
    if failures:
        raise RuntimeError(f'追新未完成：新增 {total_added_eps}，失败 {failures}')
    return total_added_eps


def run_update():
    """每日自动拉取 TMDB 最新热映/热播资源（纯轻量刮削，零预转存）。"""
    log("=== 开始拉取各大平台最新热播影视资源 (TMDB 增量发现) ===")
    cmd = [sys.executable, "-m", "quark_vod.integrations.hometheater.tmdb_populate", "--limit", "15"]
    subprocess.run(cmd, check=True, timeout=3600)
    log("最新热播资源增量抓取完成")


def precheck_newly_added(limit=35):
    """每日片源存活健康巡检与真 4K 锁源闭环：
    1. 自动巡检存量已锁定的分享链接，若原作者取消或失效，当场拉黑并自动多源打捞全新活源替换！
    2. 对当天新上架的新片及未锁定资源进行多源矩阵打捞并锁存 4K 源。
    """
    log("=== 开始执行每日片源存活性健康巡检、死链自动换新与增量 4K 锁源 ===")
    try:
        from quark_vod.auditor import VodAuditor
        auditor = VodAuditor()
        # 1. 存量已锁源每日存活体检与失效自愈换新
        heal_stats = auditor.check_and_heal_locked_shares(limit=25)
        log(f"存量锁源检查结束: 实查={heal_stats.get('checked')}/{heal_stats.get('total')}, 分享可读={heal_stats.get('healthy')}, 未确认={heal_stats.get('unknown')}, 换源4K标注={heal_stats.get('healed_4k')}, 其他标注={heal_stats.get('healed_hd')}")
        # 2. 增量新片 4K 验票锁源
        stats = auditor.audit_entire_library(limit=limit)
        log(f"片源审计结束: 实查={stats.get('checked')}/{stats.get('total_shows')}, 全目标4K标注={stats.get('valid_4k')}, 其他完整覆盖={stats.get('valid_hd')}, 未确认完整覆盖={stats.get('no_source')}")
        # 3. 仅统计已保存记录，不改动云文件名。
        total_fixed = auditor.fix_disguised_vod_files()
        log(f"当前已保存视频记录: {total_fixed} 个")
        return {'locked': heal_stats, 'audit': stats, 'saved': total_fixed}
    except Exception as e:
        log(f"片源前置预检与自愈异常: {type(e).__name__}")
        raise


def refresh_jellyfin():
    """Refresh is confirmed by a new successful task result, not a POST status."""
    log("通知配置的 Jellyfin 执行媒体库全量刷新...")
    from quark_vod import config as vod_cfg
    previous = _refresh_task(vod_cfg).get('LastExecutionResult')
    req = urllib.request.Request(f"{vod_cfg.JELLYFIN2_API}/Library/Refresh", data=b"", method="POST")
    if vod_cfg.JELLYFIN2_KEY:
        req.add_header("Authorization", f'MediaBrowser Token="{vod_cfg.JELLYFIN2_KEY}"')
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status not in (200, 204):
            raise RuntimeError('Jellyfin refresh not accepted')
        log(f"Jellyfin 刷新命令已送达: HTTP {resp.status}")
    if not _wait_library_refresh(vod_cfg, previous=previous):
        raise RuntimeError('未确认本次 Jellyfin 扫描成功结束')
    return {'confirmed': True}


def _refresh_task(vod_cfg):
    req = urllib.request.Request(f'{vod_cfg.JELLYFIN2_API}/ScheduledTasks')
    if vod_cfg.JELLYFIN2_KEY:
        req.add_header('Authorization', f'MediaBrowser Token="{vod_cfg.JELLYFIN2_KEY}"')
    with urllib.request.urlopen(req, timeout=10) as resp:
        tasks = json.loads(resp.read(2 * 1024 ** 2))
    if not isinstance(tasks, list):
        raise ValueError('invalid ScheduledTasks response')
    task = next((t for t in tasks if isinstance(t, dict) and t.get('Key') == 'RefreshLibrary'), None)
    if task is None:
        raise ValueError('RefreshLibrary missing')
    return task


def _wait_library_refresh(vod_cfg, max_wait=LIBRARY_REFRESH_MAX_WAIT, interval=15, previous=None):
    """Require observed completion or a changed result for a fast scan."""
    t0 = time.monotonic()
    seen_running = False
    while time.monotonic() - t0 < max_wait:
        try:
            task = _refresh_task(vod_cfg)
            state = task.get('State')
            result = task.get('LastExecutionResult') or {}
            if state == "Running":
                seen_running = True
            elif state == 'Idle' and result and result != previous:
                if result.get('Status') == 'Completed':
                    log(f'Jellyfin2 扫描已确认完成（{int(time.monotonic() - t0)}s，观察到运行={seen_running}）')
                    return True
                log('Jellyfin2 扫描结束但未成功')
                return False
        except Exception:
            pass
        time.sleep(interval)
    log(f"等待 Jellyfin2 扫描确认超时（{max_wait}s），后续元数据清理暂缓")
    return False


def clean_orphan_people():
    """清理孤立的剧照、海报、演职人员头像图片与僵尸元数据，释放 SSD 空间。"""
    purge_script = "quark_vod.integrations.hometheater.purge_orphan_metadata"
    if not __import__("importlib.util", fromlist=["find_spec"]).find_spec(purge_script):
        raise FileNotFoundError("metadata cleanup module missing")
    env = os.environ.copy()
    env['JELLYFIN_API_BASE'] = host_config.JELLYFIN2_API
    try:
        subprocess.run([sys.executable, "-m", purge_script], env=env, check=True, timeout=600)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 75:
            raise Busy('孤儿元数据清理暂缓') from exc
        raise


def run_vod_canary_test(sample_count=10):
    """Read-only saved-media samples; source discovery is reported separately."""
    import random
    from quark_vod import config as cfg
    from quark_vod.transfer import QuarkTransfer
    from quark_vod.selfheal import _remote_exists_batch
    rows=vod_db._conn().execute("SELECT remote_path FROM transfers WHERE status='done' AND remote_path IS NOT NULL").fetchall()
    remotes=list({r['remote_path'] for r in rows})
    selected=random.sample(remotes,min(sample_count,len(remotes)))
    exists=_remote_exists_batch(QuarkTransfer(None),selected)
    readable=0
    for remote in selected:
        if not exists.get(remote,False):
            continue
        url=cfg.OPENLIST_PUBLIC+'/d'+urllib.parse.quote(cfg.play_path(remote))
        try:
            request=urllib.request.Request(url,headers={'Range':'bytes=0-31'})
            with urllib.request.urlopen(request,timeout=10) as response:
                body=response.read(32)
                if response.status in (200,206) and body:
                    readable+=1
        except Exception:
            pass
    log(f'[Canary] 已转存视频短读取通过 {readable}/{len(selected)}；本检查不触发新转存，不代表完整播放')
    return {'sampled':len(selected),'readable':readable}


def _save_run_report(report):
    path = os.path.join(DATA_DIR, 'daily_update_status.json')
    temp = path + '.tmp'
    with open(temp, 'w', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def run_inline_cleanup():
    # The daily pipeline already owns vod_maintenance.lock. Calling main()
    # here would reacquire that flock in a separate descriptor and deadlock.
    from quark_vod.cleanup import clean_mismatched_short_dramas, enforce_vod_quota, run_cleanup
    from quark_vod.openlist_client import OpenListClient
    mismatched = clean_mismatched_short_dramas()
    quota = enforce_vod_quota(OpenListClient())
    expiry = run_cleanup()
    if isinstance(quota, dict) and quota.get('blocked'):
        raise RuntimeError('quota cleanup blocked')
    return {'complete': True, 'mismatched': mismatched, 'quota': quota, 'expiry': expiry}


def main():
    import fcntl
    lock_path = os.path.join(DATA_DIR, "vod_maintenance.lock")
    os.makedirs(DATA_DIR, exist_ok=True)
    lock_fp = open(lock_path, "a+")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fp.close()
        log('其他影院维护任务尚未结束，跳过本次日更，等待下次调度')
        return 75

    report = {'started_at': datetime.datetime.now().astimezone().isoformat(), 'stages': []}

    def stage(name, action):
        entry = {'name': name}
        try:
            result = action()
            entry.update(status='complete', result=result)
            if isinstance(result, dict) and result.get('errors'):
                entry['status'] = 'unconfirmed'
            if name == 'canary' and (not result['sampled'] or result['readable'] != result['sampled']):
                entry['status'] = 'unconfirmed'
            if name == 'sources' and result and (result['locked'].get('unknown') or
                    result['locked'].get('failed_dead') or result['locked'].get('errors') or
                    result['audit'].get('errors')):
                entry['status'] = 'unconfirmed'
        except Exception as exc:
            entry.update(status='deferred' if isinstance(exc, Busy) else 'failed', error=type(exc).__name__)
            log(f'{name} {entry["status"]}: {type(exc).__name__}')
        report['stages'].append(entry)
        return entry['status'] == 'complete'

    try:
        # A failed protection/prune stage stops the pipeline; destructive work
        # must never interpret unavailable protection data as an empty set.
        if stage('pruning', run_pruning):
            if '--prune-only' not in sys.argv:
                from quark_vod.integrations.hometheater.vod_automation import run_staging, retire_ghosts, airing_chase
                stage('staging', run_staging)
                stage('tmdb', run_update)
                stage('episodes', airing_chase)
                stage('ghosts', retire_ghosts)
                stage('sources', lambda: precheck_newly_added(limit=30))
            if stage('jellyfin_refresh', refresh_jellyfin):
                stage('orphan_metadata', clean_orphan_people)
            else:
                report['stages'].append({'name': 'orphan_metadata', 'status': 'skipped',
                                         'reason': 'scan_not_confirmed'})
            if '--prune-only' not in sys.argv:
                stage('canary', lambda: run_vod_canary_test(sample_count=10))
            stage('cleanup', run_inline_cleanup)
        report['status'] = ('complete' if all(s['status'] == 'complete' for s in report['stages']) else 'partial')
        report['finished_at'] = datetime.datetime.now().astimezone().isoformat()
        _save_run_report(report)
        from quark_vod.notifier import notify
        if report['status'] != 'complete':
            notify('daily_partial', '影院日更未完成', '请检查宿主日更报告中的失败阶段。', level='P1')
        log('日更阶段执行结束: ' + report['status'] + '（统计不代表整片/设备播放验收）')
        return 0 if report['status'] == 'complete' else 1
    finally:
        try:
            fcntl.flock(lock_fp, fcntl.LOCK_UN)
            lock_fp.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
