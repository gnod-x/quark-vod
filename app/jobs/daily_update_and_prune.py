#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日热门资源自动更新、连载追新与存储配额智能淘汰治理器
========================================================================
功能：
  1. 配额控制：单专区上限严格限制为 100 部（连续剧整部算 1 个资源），
     总片库容量严格收敛在 4GB 元数据以内。
  2. LRU 智能淘汰：
     - 白名单保护：已看（plays/UserData）、收藏（Favorites）及港台经典专区绝不删除。
     - 淘汰对象：零播放、非经典、上架久、热度跌出榜单的旧片。
  3. 每日拉取：自动从 TMDB Trending/Popular 抓取各大平台最新热播新片入库（纯轻量刮削）。
  4. 连载追新：自动检索库中连载中（未完结）的热播剧，向 TMDB 同步最新上线的集数，
     自动生成 pending 占位符与分集元数据，实现无感追剧。
  5. 联动刷新：自动触发 Jellyfin 媒体库扫描并清理孤立演职员缓存。
"""

import datetime
import os
import re
import sys
import time
import shutil
import sqlite3
import subprocess
import urllib.request
import xml.etree.ElementTree as ET

BASE = "/home/dong/projects/hometheater"
sys.path.insert(0, os.path.join(BASE, "scripts"))
import tmdb_populate
from quark_vod import db as vod_db

CINEMA2 = os.path.join(BASE, "cinema2")
DATA_DIR = os.path.join(BASE, "data")
LOG_DIR = os.path.join(BASE, "logs")
QUARK_DB = os.path.join(DATA_DIR, "quark_plays.db")
JF_DB = os.path.join(BASE, "jellyfin2/config/data/jellyfin.db")

# ── 配置策略 ──────────────────────────────────────────
# 单专区最大保留影片/剧集数（超过时触发淘汰，剧集整部算 1 部）
MAX_PER_CATEGORY = 200
# 永久保护免淘汰专区（经典永存）
PROTECTED_CATEGORIES = set()  # 港台经典不设免死保护，所有专区均受配额与淘汰治理
TV_CATEGORIES = {"国产剧集", "欧美剧集", "热门韩剧", "国产动漫", "日本动漫", "日本剧集", "泰国剧集"}

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

    # 1. 查询 jellyfin.db 中已收藏、在看或已看项目（免锁只读）
    if os.path.exists(JF_DB):
        try:
            with closing(sqlite3.connect(f"file:{JF_DB}?mode=ro&immutable=1", uri=True)) as con:
                cur = con.cursor()
                cur.execute("""
                    SELECT b.Name, b.SeriesName FROM BaseItems b
                    JOIN UserData u ON b.Id = u.ItemId
                    WHERE u.IsFavorite = 1 OR u.Played = 1 OR u.PlayCount > 0 OR u.PlaybackPositionTicks > 0
                """)
                for row in cur.fetchall():
                    if row[0]:
                        protected.add(row[0].strip())
                    if row[1]:
                        protected.add(row[1].strip())
        except Exception as e:
            log(f"读取 Jellyfin UserData 失败: {e}")

    # 2. 查询 quark_plays.db 中用户手动置顶/一键预热保护的项目 (user_pinned)
    try:
        if vod_db and hasattr(vod_db, "get_all_pinned_titles"):
            pinned = vod_db.get_all_pinned_titles()
            protected.update(pinned)
    except Exception as e:
        log(f"读取 user_pinned 失败: {e}")

    # 3. 查询 quark_plays.db 中历史点播记录 (plays) 与有效转存 (transfers)
    if os.path.exists(QUARK_DB):
        try:
            with closing(sqlite3.connect(f"file:{QUARK_DB}?mode=ro&immutable=1", uri=True)) as con:
                cur = con.cursor()
                for row in cur.execute("SELECT title FROM plays").fetchall():
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
            log(f"读取 quark_plays 历史记录失败: {e}")

    return protected


def is_item_active_or_done(full_path, cat, name):
    """检查目录内是否包含已转存 (done) 的正片或 live 直链，坚决杜绝误删用户已转存/正在观看内容。"""
    try:
        for root, _, files in os.walk(full_path):
            for f in files:
                if f.endswith(".strm"):
                    p = os.path.join(root, f)
                    with open(p, "r", encoding="utf-8", errors="ignore") as fp:
                        c = fp.read(64)
                        if "5244" in c or (c.startswith("http") and "/pending" not in c):
                            return True
    except Exception:
        pass
    return False


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
        if item_year is not None and item_year < current_year and not is_user_protected:
            try:
                shutil.rmtree(full_p)
                log(f"已清理非本年度历史老旧资源: 【{cat}】{d} (年份: {item_year})")
                if os.path.exists(QUARK_DB):
                    try:
                        with sqlite3.connect(QUARK_DB) as c:
                            c.execute("DELETE FROM strm_map WHERE strm_path LIKE ?", (f"{full_p}%",))
                    except Exception:
                        pass
                pruned += 1
            except Exception as e:
                log(f"删除老旧资源失败 {full_p}: {e}")
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
            candidates.append((mtime, d, full_p))

        candidates.sort(key=lambda x: x[0])
        to_delete = candidates[:excess]
        for _, name, path in to_delete:
            try:
                shutil.rmtree(path)
                log(f"已按配额淘汰资源: 【{cat}】{name}")
                if os.path.exists(QUARK_DB):
                    try:
                        with sqlite3.connect(QUARK_DB) as c:
                            c.execute("DELETE FROM strm_map WHERE strm_path LIKE ?", (f"{path}%",))
                    except Exception:
                        pass
                pruned += 1
            except Exception as e:
                log(f"配额删除失败 {path}: {e}")

    remaining_count = len([d for d in os.listdir(cat_dir) if not d.startswith(".")])
    log(f"专区【{cat}】清理完成，共淘汰/释放 {pruned} 部资源，现保留 {remaining_count} 部 (本年度新片+预热保留)")
    return pruned


def run_pruning():
    """全库执行存储配额与年份智能治理。"""
    log(f"=== 开始执行当前年度 ({CURRENT_YEAR}) 资源保留、历史老旧清理与上限 ({MAX_PER_CATEGORY}部/区) 智能治理 ===")
    # 1. 清理连载更新中剧集内的未播出未来 pending 占位
    unaired_pruned = prune_unaired_pending_strms()

    protected = get_protected_titles()
    log(f"当前共锁定 {len(protected)} 部用户预热/在看/收藏保护影视（绝不删除）")

    total_pruned = unaired_pruned
    if os.path.isdir(CINEMA2):
        for cat in os.listdir(CINEMA2):
            if os.path.isdir(os.path.join(CINEMA2, cat)) and not cat.startswith("."):
                total_pruned += prune_category(cat, protected, limit=MAX_PER_CATEGORY)

    log(f"=== 治理清理结束，全库共移除 {total_pruned} 部非当前年度/超额旧资源/未更新占位 ===")
    return total_pruned


def prune_unaired_pending_strms(dry_run=False):
    """清理正在连载/更新中（status != 'Ended'）剧集内未来尚未播出的 pending 占位符。

    保护铁律：
    1. status == 'Ended' 的已完结剧集所有集数均为有效正片，绝不误删；
    2. 已转存为 live 直链（含 5244 或 /d/）的真实正片视频流无论播出日期均受铁律保护，严禁删除；
    3. 仅清理 status != 'Ended' 且 aired > 今天（或无播出日期）的纯 pending 占位；
    4. 同步移除关联的分集 nfo、剧照及 strm_map 数据库映射；
    5. 若清理后季目录或剧集目录完全为空，自动回收空目录（受保护剧集除外）。
    """
    log("=== 开始检查并清理连载剧集中未播出的未来占位 (消灭未更新占位) ===")
    today_str = datetime.date.today().isoformat()
    total_pruned_strms = 0
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

            # 遍历该连载剧各季
            season_dirs = [d for d in os.listdir(show_path) if d.startswith("Season ") and os.path.isdir(os.path.join(show_path, d))]
            for s_dir_name in season_dirs:
                s_dir = os.path.join(show_path, s_dir_name)
                for f_name in os.listdir(s_dir):
                    if not f_name.endswith(".strm"):
                        continue
                    strm_path = os.path.join(s_dir, f_name)

                    # 1. 检查是否为已转存直链
                    head = ""
                    try:
                        with open(strm_path, "r", encoding="utf-8", errors="ignore") as f:
                            head = f.read(128)
                    except Exception:
                        pass
                    if "5244" in head or "/d/" in head:
                        continue  # 真实已转存直链，严禁删除

                    # 2. 检查对应 nfo 的 aired 属性
                    base_name = f_name[:-5]
                    ep_nfo_path = os.path.join(s_dir, f"{base_name}.nfo")
                    ep_thumb_path = os.path.join(s_dir, f"{base_name}-thumb.jpg")

                    is_unaired = False
                    if os.path.isfile(ep_nfo_path):
                        try:
                            etree = ET.parse(ep_nfo_path)
                            aired = etree.getroot().findtext("aired")
                            if not aired or not aired.strip() or aired.strip() > today_str:
                                is_unaired = True
                        except Exception:
                            is_unaired = True
                    else:
                        is_unaired = True

                    if is_unaired:
                        if not dry_run:
                            try:
                                os.remove(strm_path)
                                if os.path.isfile(ep_nfo_path):
                                    os.remove(ep_nfo_path)
                                if os.path.isfile(ep_thumb_path):
                                    os.remove(ep_thumb_path)
                                to_delete_strm_paths.append(strm_path)
                            except Exception as err:
                                log(f"删除未更新占位异常 {strm_path}: {err}")
                        total_pruned_strms += 1

                # 季目录若已无任何视频文件，回收该季目录
                if not dry_run and os.path.isdir(s_dir):
                    remaining_in_season = [x for x in os.listdir(s_dir) if not x.startswith(".")]
                    if not remaining_in_season or (len(remaining_in_season) == 1 and remaining_in_season[0] == "season.nfo"):
                        try:
                            shutil.rmtree(s_dir)
                        except Exception:
                            pass

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
                        log(f"回收空目录异常 {show_path}: {err}")

    # 同步清理 strm_map 记录
    if to_delete_strm_paths and not dry_run:
        try:
            con = vod_db._conn()
            for p in to_delete_strm_paths:
                con.execute("DELETE FROM strm_map WHERE strm_path = ?", (p,))
            con.commit()
            log(f"已同步从 strm_map 移除 {len(to_delete_strm_paths)} 条未播出占位映射")
        except Exception as err:
            log(f"清理 strm_map 异常: {err}")

    log(f"=== 未更新占位清理完成，共移除 {total_pruned_strms} 个未播出的未来 pending 占位符 ===")
    return total_pruned_strms


def sync_airing_episodes():
    """检索本地连载中的热播剧，向 TMDB 差量同步最新上线的集数（纯轻量占位，零预转存）。"""
    log("=== 开始检查未完结连载剧集的新集数更新 (追新同步) ===")
    key = tmdb_populate.read_tmdb_key()
    total_added_eps = 0

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
                continue

            if status == "Ended" or not tmdbid:
                continue

            try:
                url = f"https://api.themoviedb.org/3/tv/{tmdbid}?api_key={key}&language=zh-CN"
                detail = tmdb_populate.http_json(url)
            except Exception as e:
                log(f"查询 TMDB 详情失败 《{title}》(id:{tmdbid}): {e}")
                continue

            latest_status = detail.get("status")
            if latest_status == "Ended" and status != "Ended":
                try:
                    status_node = root.find("status")
                    if status_node is not None:
                        status_node.text = "Ended"
                        tree.write(nfo_path, encoding="utf-8", xml_declaration=True)
                except Exception:
                    pass

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
                    s_detail = tmdb_populate.http_json(s_url)
                    for ep_obj in s_detail.get("episodes", []):
                        e_no = ep_obj.get("episode_number")
                        if e_no:
                            ep_info_map[e_no] = ep_obj
                except Exception:
                    pass

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
                    base_ep = f"{title} - S{s_num:02d}E{ep:02d}"
                    ep_file = f"{base_ep}.strm"
                    ep_path = os.path.join(s_dir, ep_file)
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
                        if tmdb_populate.write_pending_strm(ep_path, cat, title, s_num, ep):
                            created.append((cat, title, s_num, ep, ep_path))

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
                            log(f"strm_map 登记异常: {err}")
                        total_added_eps += len(created)

            time.sleep(0.05)

    log(f"=== 连载追新检查完成，全库共追加 {total_added_eps} 个最新集数待播占位 ===")
    return total_added_eps


def run_update():
    """每日自动拉取 TMDB 最新热映/热播资源（纯轻量刮削，零预转存）。"""
    log("=== 开始拉取各大平台最新热播影视资源 (TMDB 增量发现) ===")
    cmd = [sys.executable, os.path.join(BASE, "scripts", "tmdb_populate.py"), "--limit", "15"]
    try:
        subprocess.run(cmd, check=True)
        log("最新热播资源增量抓取完成")
    except Exception as e:
        log(f"TMDB 抓取执行失败: {e}")


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
        log(f"存量锁源自愈巡检完成: 健康={heal_stats.get('healthy')}, 自动换新4K={heal_stats.get('healed_4k')}, 换新高清={heal_stats.get('healed_hd')}")
        # 2. 增量新片 4K 验票锁源
        stats = auditor.audit_entire_library(limit=limit)
        log(f"增量新片 4K 锁源完成: 4K锁源={stats.get('valid_4k')}, 高清={stats.get('valid_hd')}, 无有效源={stats.get('no_source')}")
        # 3. 网盘点播目录伪装视频（如被分享者改成.mp3/.pdf避审查）智能巡检与格式扶正
        total_fixed = auditor.fix_disguised_vod_files()
        log(f"网盘伪装视频巡检与格式扶正完成，当前就绪直链视频: {total_fixed} 个")
    except Exception as e:
        log(f"片源前置预检与自愈异常: {e}")


def refresh_jellyfin():
    """通知 Jellyfin 2 扫描新媒体库，并等待扫描真正完成（而非仅 HTTP 204 送达）。

    2026-10-26 补强：/Library/Refresh 只返回 204 表示"已入队"，实际扫描
    会持续 30~60 分钟（实测 10-26 05:27 入队，06:25 才扫完）。若不等待，
    后续 update_vod_quota/其它步骤会与扫描并发，且金丝雀抽检在扫描未完成
    时得到的元数据不可信。
    轮询 ScheduledTasks 中 RefreshLibrary 的 State，最多等待 REFRESH_WAIT_SECS。
    """
    log("通知 Jellyfin2 (8097) 执行媒体库全量刷新...")
    try:
        from quark_vod import config as vod_cfg
        req = urllib.request.Request(f"{vod_cfg.JELLYFIN2_API}/Library/Refresh", data=b"", method="POST")
        if vod_cfg.JELLYFIN2_KEY:
            req.add_header("Authorization", f'MediaBrowser Token="{vod_cfg.JELLYFIN2_KEY}"')
        with urllib.request.urlopen(req, timeout=10) as resp:
            log(f"Jellyfin 刷新命令已送达: HTTP {resp.status}")
        _wait_library_refresh(vod_cfg)
    except Exception as e:
        log(f"Jellyfin 刷新请求失败: {e}")


def _wait_library_refresh(vod_cfg, max_wait=1800, interval=15):
    """轮询 RefreshLibrary 任务状态直到 Idle（或超时）。"""
    import json
    t0 = time.time()
    seen_running = False
    while time.time() - t0 < max_wait:
        try:
            req = urllib.request.Request(f"{vod_cfg.JELLYFIN2_API}/ScheduledTasks")
            if vod_cfg.JELLYFIN2_KEY:
                req.add_header("Authorization", f'MediaBrowser Token="{vod_cfg.JELLYFIN2_KEY}"')
            with urllib.request.urlopen(req, timeout=10) as resp:
                tasks = json.loads(resp.read().decode("utf-8"))
            state = next((t.get("State") for t in tasks if t.get("Key") == "RefreshLibrary"), None)
            if state == "Running":
                seen_running = True
            elif seen_running and state == "Idle":
                log(f"Jellyfin2 媒体库扫描已完成（耗时 {int(time.time() - t0)}s）")
                return True
            elif not seen_running and time.time() - t0 > 60:
                # 60s 内仍未进入 Running，说明任务已结束或被合并
                log("Jellyfin2 媒体库扫描未观察到 Running 状态（可能已瞬时完成），继续")
                return True
        except Exception:
            pass
        time.sleep(interval)
    log(f"[警告] 等待 Jellyfin2 扫描完成超时（{max_wait}s），继续后续流程")
    return False


def clean_orphan_people():
    """清理孤立的剧照、海报、演职人员头像图片与僵尸元数据，释放 SSD 空间。"""
    purge_script = os.path.join(BASE, "scripts", "purge_orphan_metadata.py")
    if os.path.exists(purge_script):
        log("执行孤儿剧照、演职员头像与僵尸元数据全量深度清理...")
        try:
            env = os.environ.copy()
            env["JELLYFIN_API_BASE"] = "http://127.0.0.1:8097"
            env["HOMETHEATER_BASE_DIR"] = BASE
            subprocess.run([sys.executable, purge_script], env=env, timeout=600)
        except Exception as e:
            log(f"深度元数据清理异常: {e}")


def run_vod_canary_test(sample_count=10):
    """每日点播金丝雀健康抽检哨兵。

    从库中随机抽取 10 部影视执行轻量解析与连通性自检，监控当日网盘与源站大盘健康度。
    """
    import glob
    import random
    import urllib.parse
    log(f"=== [Canary] 开始执行点播链路金丝雀自动健康抽检 (随机抽检 {sample_count} 部) ===")
    # 2026-10-26 修复：此前从 5.2 万个 strm 中完全随机抽样，其中 99.9% 是 pending，
    # 于是抽到活链的概率极低，日可用率长期被 pending 的"能否 resolve 到源"污染，
    # 无法反映 DirectPlay 活链的真实健康度。现拆成两组各自抽样：
    #   A 组（活链）：实测 Range 可达性，这是播放时真正走的路径；
    #   B 组（pending）：实测能否 resolve 到可用源，反映搜索侧健康度。
    all_strms = glob.glob(f"{CINEMA2}/**/*.strm", recursive=True)
    if not all_strms:
        log("[Canary] 库中无 strm 文件，跳过抽检")
        return

    live_strms, pending_strms = [], []
    for sp in all_strms:
        try:
            with open(sp, "r", encoding="utf-8", errors="ignore") as fp:
                line = fp.readline().strip()
        except Exception:
            continue
        if ":5244/d/" in line or "/夸克" in line or ":8099/clean" in line:
            live_strms.append(sp)
        elif ":8099/pending" in line:
            pending_strms.append(sp)

    half = max(1, sample_count // 2)
    sample = random.sample(live_strms, min(half, len(live_strms))) + \
             random.sample(pending_strms, min(sample_count - half, len(pending_strms)))
    success = 0
    from quark_vod import search as search_mod

    for sp in sample:
        try:
            with open(sp, "r", encoding="utf-8") as fp:
                line = fp.readline().strip()

            if ":5244/d/" in line or "/夸克" in line or ":8099/clean" in line:
                # 针对已激活直链：单字节 Range 探测
                req = urllib.request.Request(line, headers={"User-Agent": "curl/8.14.1", "Range": "bytes=0-0"})
                with urllib.request.urlopen(req, timeout=3) as resp:
                    if resp.status in (200, 206):
                        success += 1
            elif ".m3u8" in line:
                if search_mod.is_m3u8_alive(line):
                    success += 1
            elif ":8099/pending" in line:
                # 针对未激活资源：轻量解析是否有可用源
                parsed = urllib.parse.urlparse(line)
                q = urllib.parse.parse_qs(parsed.query)
                cat = (q.get("cat") or [""])[0]
                t = (q.get("t") or [""])[0]
                s = int((q.get("s") or ["0"])[0])
                e = int((q.get("e") or ["0"])[0])
                res, u = search_mod.resolve_for_play(cat, t, s, e, prefer_panshare=True)
                if res is not None:
                    success += 1
        except Exception:
            pass

    rate = (success / len(sample)) * 100 if sample else 0
    log(f"[Canary] 金丝雀抽检完成: 成功 {success}/{len(sample)} 部 "
        f"(活链 {len(live_strms)} / pending {len(pending_strms)} 各半抽样) 可用率: {rate:.1f}%)")
    if rate < 70.0:
        log(f"[Canary 警告] 点播链路可用率低于 70%，请关注源站搜索可用性或网盘 Cookies！")


def main():
    import fcntl
    lock_path = os.path.join(DATA_DIR, "vod_maintenance.lock")
    os.makedirs(DATA_DIR, exist_ok=True)
    lock_fp = open(lock_path, "a+")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX)
    except Exception:
        pass

    try:
        if "--prune-only" in sys.argv:
            log("收到 --prune-only 参数，仅执行当前年度资源治理与老旧资源清理...")
            run_pruning()
            refresh_jellyfin()
            clean_orphan_people()
            log("年度治理与老旧清理全部完成！")
            return

        # 0. 全库影视分类精准度治理与重名消歧（如《凡人修仙传》真人剧 vs 动画版）
        try:
            import category_governor
            category_governor.run_governance(dry_run=False)
        except Exception as e:
            log(f"分类治理与重名消歧执行异常: {e}")

        # 1. 检查配额并执行 LRU 淘汰（单区上限 200 部）
        run_pruning()
        # 2. 增量拉取各大平台最新上映/热播资源
        run_update()
        # 3. 连载剧集增量追新同步（自动追加新上线集数，同步下载剧照与NFO）
        sync_airing_episodes()
        # 4. 前置 4K 验票与锁源（确保新入库影视落地即可播）
        precheck_newly_added(limit=30)
        # 5. 刷新影院并清理孤立头像
        refresh_jellyfin()
        clean_orphan_people()
        # 6. 金丝雀点播链路健康自动抽检
        run_vod_canary_test(sample_count=10)
        log("每日影视更新、连载追新、片源预检与存储治理全部顺利完成！")
    finally:
        try:
            fcntl.flock(lock_fp, fcntl.LOCK_UN)
            lock_fp.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()

