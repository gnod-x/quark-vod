#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
片源真 4K 质量与存活预检审计引擎 (Pre-Flight VOD Auditor)
========================================================================
核心原则：
  1. 真实可用性确立：
     提前对片库资源进行全网夸克 4K 存活预检，彻底消灭用户选片点击时的“薛定谔盲盒”。
  2. 纯轻量验票，0 存储占用：
     仅调用夸克官方只读查询接口 (QUARK_SHARE_TOKEN + QUARK_SHARE_DETAIL)，
     【绝对不调用 save_share】—— 零网盘存储占用，零配额消耗！
  3. 4K 严格质量把关：
     精准检测分享包内是否包含真实 2160p/4K/UHD/HDR 高码率正片视频文件。
  4. 锁定健康源秒播：
     经预检确认存活的 4K 资源直接在数据库中锁存，用户点击时 0 延迟秒开，100% 成功。
"""

import os
import re
import sys
import time
import urllib.parse

from . import config
from . import db
from . import search as search_mod
from .transfer import QuarkTransfer, TransferFailed, QUARK_SHARE_DETAIL, is_candidate_video
from .openlist_client import OpenListClient

# 4K / 超高清正片识别正则
FOUR_K_REGEX = re.compile(r'(?i)(?:2160[pP]|4[kK]|UHD|HDR(?:10|\+)?|DoVi|DV|REMUX|原盘|蓝光原盘)')

def is_4k_candidate(filename, size_bytes=0, is_series=False):
    """根据文件名与体积综合裁定是否达到夸克 4K 原画标准。"""
    fn = filename or ""
    if FOUR_K_REGEX.search(fn):
        return True
    # 辅助体积判据（针对部分压制未在文件名中打 4K 标记的高码率文件）
    # 电影 > 4.5 GB，单集剧集 > 1.2 GB
    if is_series and size_bytes >= 1.2 * 1024 * 1024 * 1024:
        return True
    if not is_series and size_bytes >= 4.5 * 1024 * 1024 * 1024:
        return True
    return False


class VodAuditor:
    def __init__(self, transfer=None):
        self.ol = OpenListClient()
        self.tr = transfer or QuarkTransfer(self.ol)

    def audit_tv_season(self, cat, title, season=1, existing_episodes=None, force=False):
        """以【整季大包】为单位进行审计与批量锁源（一次探测，整季通亮，彻底消除逐集重复请求）。"""
        eps_to_check = set(existing_episodes or [1])

        # 检查是否整季都已经转存或审计有效
        if not force:
            unresolved = []
            for ep in eps_to_check:
                audit = db.get_source_audit(cat, title, season, ep)
                if not (audit and audit["has_quark_source"] and audit["best_share_url"] and not db.is_dead_source(audit["best_share_url"])):
                    unresolved.append(ep)
            if not unresolved:
                return True, True, "", f"整季已锁源 ({len(eps_to_check)}集)"

        kw_core = re.sub(r"\s*[(（]\d{4}[)）]\s*$", "", title).strip()
        share_candidates = search_mod.search_panshare(kw_core)
        if not share_candidates and kw_core != title:
            share_candidates = search_mod.search_panshare(title)

        if not share_candidates:
            for ep in eps_to_check:
                db.save_source_audit(cat, title, season, ep,
                                     has_quark_source=0, is_4k=0,
                                     best_share_url="", best_share_title="",
                                     check_status="not_found")
            return False, False, "", "not_found"

        for cand in share_candidates:
            share_url = cand.share_url
            if not share_url or db.is_dead_source(share_url):
                continue

            try:
                stoken, pwd_id, items = self.tr.resolve_share(share_url)
            except TransferFailed as ex:
                err_str = str(ex)
                if any(k in err_str for k in ["已取消了分享", "已失效", "已过期", "链接内没有文件", "41011", "41012"]):
                    db.mark_dead_source(share_url, "panshare", err_str)
                continue
            except Exception:
                continue

            share_title = cand.name or ""
            is_4k_pack = is_4k_candidate(share_title)

            # 遍历提取包内所有视频文件及对应集数
            inspect_items = list(items)
            for it in items:
                if it.get("dir") and it.get("fid"):
                    try:
                        det_url = (f"{QUARK_SHARE_DETAIL}&pwd_id={pwd_id}"
                                   f"&stoken={urllib.parse.quote(stoken)}&pdir_fid={it['fid']}&_page=1&_size=100")
                        sub_resp = self.tr._request(det_url, method="GET", timeout=10)
                        sub_list = (sub_resp.get("data") or {}).get("list", [])
                        inspect_items.extend(sub_list)
                    except Exception:
                        pass

            pack_episodes = {}
            for fit in inspect_items:
                fname = fit.get("file_name", "")
                fsize = fit.get("size", 0) or 0
                if not fit.get("dir") and is_candidate_video(fname, fsize):
                    ep_no = self.tr.extract_episode_num(fname, season=season)
                    if ep_no and ep_no > 0:
                        is_single_4k = is_4k_pack or is_4k_candidate(fname, fsize, is_series=True)
                        if ep_no not in pack_episodes or is_single_4k:
                            pack_episodes[ep_no] = is_single_4k

            if pack_episodes:
                # 命中大包！批量将识别到的全部集数一次性锁存到数据库
                status_str = "valid_4k" if any(pack_episodes.values()) else "valid_hd"
                for ep_num, is_ep_4k in pack_episodes.items():
                    db.save_source_audit(cat, title, season, ep_num,
                                         has_quark_source=1, is_4k=int(is_ep_4k),
                                         best_share_url=share_url, best_share_title=share_title,
                                         check_status="valid_4k" if is_ep_4k else "valid_hd")
                return True, any(pack_episodes.values()), share_url, f"大包命中 {len(pack_episodes)} 集 ({status_str})"

        # 全部候选分享均无效
        for ep in eps_to_check:
            db.save_source_audit(cat, title, season, ep,
                                 has_quark_source=0, is_4k=0,
                                 best_share_url="", best_share_title="",
                                 check_status="dead")
        return False, False, "", "dead"

    def audit_single_item(self, cat, title, season=0, episode=0, force=False):
        """对单部电影执行前置预检与 4K 锁源。"""
        if season > 0:
            return self.audit_tv_season(cat, title, season=season, existing_episodes=[episode], force=force)

        # 电影处理
        done_row = db.find_done_transfer(cat, title, 0, 0)
        if done_row and done_row["remote_path"]:
            db.save_source_audit(cat, title, 0, 0,
                                 has_quark_source=1, is_4k=1,
                                 best_share_url="", best_share_title="已在网盘正片",
                                 check_status="valid_4k")
            return True, True, "", "valid_4k"

        if not force:
            audit = db.get_source_audit(cat, title, 0, 0)
            if audit and (db.now() - audit["checked_at"]) < 86400:
                if audit["has_quark_source"] and audit["best_share_url"]:
                    if not db.is_dead_source(audit["best_share_url"]):
                        return True, bool(audit["is_4k"]), audit["best_share_url"], audit["check_status"]

        kw_core = re.sub(r"\s*[(（]\d{4}[)）]\s*$", "", title).strip()
        share_candidates = search_mod.search_panshare(kw_core)
        if not share_candidates and kw_core != title:
            share_candidates = search_mod.search_panshare(title)

        # 电影形态硬隔离：候选源必须过滤短剧与无关源，得分必须 >= 30.0
        filtered_cands = []
        for cand in (share_candidates or []):
            if cand.share_url and not db.is_dead_source(cand.share_url):
                sc = search_mod.score_search_result(cand, title, season=0, episode=0)
                if sc >= 30.0:
                    filtered_cands.append(cand)
        share_candidates = filtered_cands

        if not share_candidates:
            db.save_source_audit(cat, title, 0, 0,
                                 has_quark_source=0, is_4k=0,
                                 best_share_url="", best_share_title="",
                                 check_status="not_found")
            return False, False, "", "not_found"

        for cand in share_candidates:
            share_url = cand.share_url
            if not share_url or db.is_dead_source(share_url):
                continue

            try:
                stoken, pwd_id, items = self.tr.resolve_share(share_url)
            except TransferFailed as ex:
                err_str = str(ex)
                if any(k in err_str for k in ["已取消了分享", "已失效", "已过期", "链接内没有文件", "41011", "41012"]):
                    db.mark_dead_source(share_url, "panshare", err_str)
                continue
            except Exception:
                continue

            is_4k = is_4k_candidate(cand.name or "")
            matched_video = False

            inspect_items = list(items)
            for it in items:
                if it.get("dir") and it.get("fid"):
                    try:
                        det_url = (f"{QUARK_SHARE_DETAIL}&pwd_id={pwd_id}"
                                   f"&stoken={urllib.parse.quote(stoken)}&pdir_fid={it['fid']}&_page=1&_size=20")
                        sub_resp = self.tr._request(det_url, method="GET", timeout=10)
                        sub_list = (sub_resp.get("data") or {}).get("list", [])
                        inspect_items.extend(sub_list)
                    except Exception:
                        pass

            video_fits = []
            for fit in inspect_items:
                fname = fit.get("file_name", "")
                fsize = fit.get("size", 0) or 0
                if not fit.get("dir") and is_candidate_video(fname, fsize):
                    video_fits.append((fname, fsize))
                    if is_4k_candidate(fname, fsize, is_series=False):
                        is_4k = True

            # 电影防短剧/剧集冒充校验：若包含多个提取出集数的单集视频，坚决不作为单部电影锁存
            if len(video_fits) > 1:
                ep_hits = 0
                for fn, _ in video_fits:
                    if self.tr.extract_episode_num(fn, season=1) is not None:
                        ep_hits += 1
                if ep_hits >= 2 or (len(video_fits) >= 3 and ep_hits >= 1):
                    continue  # 跳过该多集短剧包，继续寻找正片候选

            if video_fits:
                matched_video = True

            if matched_video:
                status_str = "valid_4k" if is_4k else "valid_hd"
                db.save_source_audit(cat, title, 0, 0,
                                     has_quark_source=1, is_4k=int(is_4k),
                                     best_share_url=share_url, best_share_title=cand.name or "",
                                     check_status=status_str)
                return True, is_4k, share_url, status_str

        db.save_source_audit(cat, title, 0, 0,
                             has_quark_source=0, is_4k=0,
                             best_share_url="", best_share_title="",
                             check_status="dead")
        return False, False, "", "dead"

    def check_and_heal_locked_shares(self, limit=None, yield_fn=None, timeout_fn=None):
        """存量已锁源健康体检与自动补位：
        对数据库中所有已锁定的分享链接进行只读轻量存活性探测，
        一旦发现原作者取消分享或失效，立即拉黑并自动调用多源矩阵打捞全新活源替换！
        """
        con = db._conn()
        # 查找所有已锁源且非空的独立分享条目（按 cat, title, season 去重，剧集整包只验一次）
        rows = con.execute("""
            SELECT cat, title, season, best_share_url, is_4k, best_share_title, COUNT(*) as ep_cnt
            FROM source_audits
            WHERE has_quark_source = 1 AND best_share_url != ''
            GROUP BY cat, title, season, best_share_url
            ORDER BY checked_at ASC
        """).fetchall()

        if limit:
            rows = rows[:limit]

        print(f"[{config.SERVICE_NAME}] 开始对数据库中 {len(rows)} 个已锁存的夸克分享源执行健康体检与自愈换源...")
        stats = {"total": len(rows), "healthy": 0, "healed_4k": 0, "healed_hd": 0, "failed_dead": 0}

        for r in rows:
            if timeout_fn and timeout_fn():
                print(f"[{config.SERVICE_NAME}] 达到时间窗口上限，自愈巡检优雅收工。")
                break
            if yield_fn:
                yield_fn()

            cat = r["cat"]
            title = r["title"]
            season = r["season"]
            share_url = r["best_share_url"]

            is_alive = False
            # 轻量只读探测存活性
            try:
                stoken, pwd_id, items = self.tr.resolve_share(share_url)
                if items:
                    is_alive = True
            except TransferFailed as ex:
                err_str = str(ex)
                if any(k in err_str for k in ["已取消了分享", "已失效", "已过期", "链接内没有文件", "41011", "41012"]):
                    db.mark_dead_source(share_url, "panshare", err_str)
            except Exception:
                pass

            if is_alive:
                stats["healthy"] += 1
                # 刷新存活条目的检查时间戳
                now_ts = db.now()
                con.execute("""
                    UPDATE source_audits SET checked_at = ?
                    WHERE cat = ? AND title = ? AND season = ? AND best_share_url = ?
                """, (now_ts, cat, title, season, share_url))
                con.commit()
                tag = f"S{season:02d}" if season else "电影"
                print(f" - [健康存活] 【{cat}】《{title}》({tag}) -> {share_url[:40]}...")
            else:
                # 发现死链！立即触发多源矩阵自动打捞全新活源替换！
                db.mark_dead_source(share_url, "panshare", "分享链接已失效或取消")
                tag = f"S{season:02d}" if season else "电影"
                print(f" - [死链检测] 【{cat}】《{title}》({tag}) 链接已失效！立即启动多源矩阵自动换源补新...")
                if season > 0:
                    ok, is_4k, new_url, status_msg = self.audit_tv_season(cat, title, season=season, force=True)
                else:
                    ok, is_4k, new_url, status_msg = self.audit_single_item(cat, title, 0, 0, force=True)

                if ok and new_url:
                    if is_4k:
                        stats["healed_4k"] += 1
                    else:
                        stats["healed_hd"] += 1
                    print(f"   ★ [自动自愈成功] 已成功替换为全新存活源: {new_url[:40]}... ({status_msg})")
                else:
                    stats["failed_dead"] += 1
                    print(f"   ▲ [自愈未找到新源] 全网暂未搜出新的替代夸克源，已标记为待补充")

            time.sleep(1.0) # 轻量防频控

        print(f"\n[{config.SERVICE_NAME}] 存量锁源体检与自愈完成: 检查={stats['total']}, 依然健康={stats['healthy']}, 成功自动换新4K={stats['healed_4k']}, 换新高清={stats['healed_hd']}, 待补源={stats['failed_dead']}")
        return stats

    def audit_entire_library(self, only_pending=True, limit=None, yield_fn=None, timeout_fn=None):
        """遍历全库执行片源可用性与 4K 质量审计（剧集以整季为单位大包审计）。支持观影感知避让与时间窗口截止。"""
        cinema2 = config.CINEMA2_DIR
        if not os.path.isdir(cinema2):
            return {"total_shows": 0, "valid_4k": 0, "valid_hd": 0, "no_source": 0}

        targets = []
        for cat in sorted(os.listdir(cinema2)):
            cat_dir = os.path.join(cinema2, cat)
            if not os.path.isdir(cat_dir) or cat.startswith("."):
                continue

            is_tv = cat in {"国产剧集", "欧美剧集", "热门韩剧", "国产动漫", "日本动漫", "日本剧集", "泰国剧集"}
            for item_dir in sorted(os.listdir(cat_dir)):
                ipath = os.path.join(cat_dir, item_dir)
                if not os.path.isdir(ipath) or item_dir.startswith("."):
                    continue

                if is_tv:
                    for sub in sorted(os.listdir(ipath)):
                        if not sub.startswith("Season "):
                            continue
                        s_dir = os.path.join(ipath, sub)
                        try:
                            s_num = int(sub.split()[1])
                        except Exception:
                            continue
                        ep_list = []
                        for fn in sorted(os.listdir(s_dir)):
                            if fn.endswith(".strm"):
                                import re
                                m = re.search(r"- S\d{2}E(\d+)\.strm$", fn)
                                if m:
                                    ep_list.append(int(m.group(1)))
                        if ep_list:
                            targets.append(("tv", cat, item_dir, s_num, ep_list))
                else:
                    targets.append(("movie", cat, item_dir, 0, [0]))

        if limit:
            targets = targets[:limit]

        stats = {"total_shows": len(targets), "valid_4k": 0, "valid_hd": 0, "no_source": 0}
        print(f"[{config.SERVICE_NAME}] 开始对片库 {len(targets)} 部影视（剧集整季打包）执行片源存活性与真 4K 审计...")

        for kind, cat, title, s_num, eps in targets:
            if timeout_fn and timeout_fn():
                print(f"[{config.SERVICE_NAME}] 达到时间窗口上限，片源审计优雅收工。")
                break
            if yield_fn:
                yield_fn()

            try:
                if kind == "tv":
                    has_src, is_4k, best_url, status_msg = self.audit_tv_season(cat, title, season=s_num, existing_episodes=eps)
                    print(f" - [{cat:<6}] 《{title}》 第{s_num}季 ({len(eps)}集) -> {status_msg}")
                else:
                    has_src, is_4k, best_url, status_str = self.audit_single_item(cat, title, 0, 0)
                    src_desc = f"4K源: {best_url[:40]}..." if is_4k else ("普通高清" if has_src else "无有效源")
                    print(f" - [{cat:<6}] 《{title}》 (电影) -> {status_str} | {src_desc}")

                if has_src and is_4k:
                    stats["valid_4k"] += 1
                elif has_src:
                    stats["valid_hd"] += 1
                else:
                    stats["no_source"] += 1
            except Exception as err:
                print(f" - [ERROR   ] {cat}/{title} -> {err}")

            time.sleep(1.2) # 轻量防频控

        print(f"\n[{config.SERVICE_NAME}] 审计完成: 影视总数={stats['total_shows']}, 4K覆盖={stats['valid_4k']}, 高清={stats['valid_hd']}, 无有效网盘源={stats['no_source']}")
        return stats

    def fix_disguised_vod_files(self):
        """巡检夸克点播目录（/夸克网盘/点播），自动嗅探并扶正所有伪装大文件（如大体积 .mp3/.pdf 等）。"""
        print(f"[{config.SERVICE_NAME}] 开始巡检网盘点播目录伪装格式文件: {config.QUARK_VOD_ROOT}...", flush=True)
        try:
            vids = self.tr.scan_videos(config.QUARK_VOD_ROOT, max_depth=4)
            print(f"[{config.SERVICE_NAME}] 网盘点播目录文件巡检与格式扶正完成，当前就绪视频总数: {len(vids)}", flush=True)
            return len(vids)
        except Exception as e:
            print(f"[{config.SERVICE_NAME}] 伪装文件巡检异常: {e}", flush=True)
            return 0

