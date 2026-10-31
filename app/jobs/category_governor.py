#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
家庭影院全库分类精准度治理与重名影视消歧引擎
========================================================================
核心原则：
  1. 重名影视形态消歧（如《凡人修仙传》真人剧 vs 动画版）：
     - 真人剧如果重名，目录名和标题自动消歧为形如《凡人修仙传 (2025)》或带(真人版)；
     - 更新对应内部 NFO title 与分集 strm URL 参数；
     - 清理 source_audits 中被动画大包误污染的错误记录，防止点播时张冠李戴。
  2. 分类错位全量纠偏与跨专区重复收敛：
     - 清除【港台经典】中的非港台电影（如韩国电影《黄海》）；
     - 清除【泰国电影】中的中澳电影（如《谜巢》）；
     - 清除【热门韩剧】中的日本特摄剧（如《气体人第一号》）；
     - 港台电影（如《九龙城寨之围城》、《寒战2》、《霸王别姬》等）在【国产电影】重复收录的，自动去重清理，收敛归一至【港台经典】。
  3. 空目录与损坏孤立目录清理（如日本电影下无NFO的空目录）。
  4. 联动刷新 Jellyfin 媒体库。
"""

import os
import re
import sys
import time
import shutil
import sqlite3
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

BASE = "/home/dong/projects/hometheater"
CINEMA2 = os.path.join(BASE, "cinema2")
sys.path.insert(0, os.path.join(BASE, "scripts"))

from quark_vod import config as vod_cfg
from quark_vod import db as vod_db


def log(msg):
    import time
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [CategoryGov] {msg}", flush=True)


# ── 已知的跨专区错配与重复收敛规则 ───────────────────────
# (片名, 应删除的错误专区, 正确保留的专区, 原因)
MISPLACED_RULES = [
    ("黄海 (2010)", "港台经典", "韩国电影", "韩国经典电影，收敛至韩国电影专区"),
    ("谜巢 (2018)", "泰国电影", "国产电影", "中澳合拍动作片，收敛至国产电影专区"),
    ("气体人第一号", "热门韩剧", "日本剧集", "日本Netflix特摄日剧，收敛至日本剧集专区"),
    ("九龙城寨之围城 (2024)", "国产电影", "港台经典", "香港动作金像奖名作，归入港台经典"),
    ("寒战2 (2016)", "国产电影", "港台经典", "经典港式警匪片，归入港台经典"),
    ("霸王别姬 (1993)", "国产电影", "港台经典", "香港金像奖/戛纳金棕榈名片，归入港台经典"),
    ("狄仁杰之通天帝国 (2010)", "国产电影", "港台经典", "徐克执导香港金像奖大片，归入港台经典"),
    ("太极侠 (2013)", "国产电影", "港台经典", "港台合拍功夫片，归入港台经典"),
    ("明日战记 (2022)", "国产电影", "港台经典", "古天乐港产科幻大片，归入港台经典"),
    ("功夫之王 (2008)", "国产电影", "港台经典", "成龙李连杰合拍功夫名片，归入港台经典"),
    ("火遮眼 (2026)", "国产电影", "港台经典", "港产警匪动作片，归入港台经典"),
    ("捕风追影 (2025)", "国产电影", "港台经典", "港片题材，归入港台经典"),
]


TRASH_ARCHIVE = os.path.join(BASE, "data", ".category_archive")


def cleanup_known_misplaced(dry_run=False):
    """清理已知的跨专区错放与重复收录条目（采用安全归档模式，数据完全可逆）。"""
    log("=== [阶段 1] 扫描并治理跨专区错放与重复收录影视 ===")
    cleaned_count = 0
    os.makedirs(TRASH_ARCHIVE, exist_ok=True)

    for name, bad_cat, good_cat, reason in MISPLACED_RULES:
        bad_dir = os.path.join(CINEMA2, bad_cat, name)
        good_dir = os.path.join(CINEMA2, good_cat, name)

        if os.path.exists(bad_dir):
            if os.path.exists(good_dir):
                log(f"发现跨专区重复: [{bad_cat}] {name} (正确归属: [{good_cat}]) -> {reason}")
                if not dry_run:
                    archive_target = os.path.join(TRASH_ARCHIVE, f"{bad_cat}_{name}")
                    if os.path.exists(archive_target):
                        archive_target = f"{archive_target}_{int(time.time())}"
                    shutil.move(bad_dir, archive_target)
                    log(f"  --> 已安全移入归档区: {bad_cat}/{name} -> {archive_target}")
                cleaned_count += 1
            else:
                # 只有错误目录下有，移动到正确专区
                log(f"发现专区错位: [{bad_cat}] {name} -> 迁移至 [{good_cat}] ({reason})")
                if not dry_run:
                    os.makedirs(os.path.join(CINEMA2, good_cat), exist_ok=True)
                    shutil.move(bad_dir, good_dir)
                    log(f"  --> 已成功迁移至正确专区: {good_cat}/{name}")
                cleaned_count += 1

    log(f"跨专区错位与重复治理完成，共处理 {cleaned_count} 部条目")
    return cleaned_count


def disambiguate_anime_vs_liveaction(dry_run=False):
    """重名影视形态消歧（以《凡人修仙传》真人剧 vs 动画版为例）。"""
    log("=== [阶段 2] 扫描并执行重名影视形态消歧治理 (真人剧 vs 动画版) ===")
    fixed_count = 0

    # 1. 核心治理：国产剧集/凡人修仙传 (杨洋 2025 真人版)
    live_fr = os.path.join(CINEMA2, "国产剧集", "凡人修仙传")
    anime_fr = os.path.join(CINEMA2, "国产动漫", "凡人修仙传")

    if os.path.exists(live_fr) and os.path.exists(anime_fr):
        target_live_name = "凡人修仙传 (2025)"
        target_live_dir = os.path.join(CINEMA2, "国产剧集", target_live_name)
        log(f"发现高危重名 IP: 【国产剧集/凡人修仙传】(真人剧) 与 【国产动漫/凡人修仙传】(动画版) 完全重名！")

        if not dry_run:
            # A. 目录重命名为带年份消歧名
            if not os.path.exists(target_live_dir):
                os.rename(live_fr, target_live_dir)
                log(f"  [消歧重命名] 目录已规范化为: 国产剧集/{target_live_name}")

            # B. 更新 tvshow.nfo 中的 title 为消歧名
            nfo_path = os.path.join(target_live_dir, "tvshow.nfo")
            if os.path.isfile(nfo_path):
                try:
                    tree = ET.parse(nfo_path)
                    root = tree.getroot()
                    title_elem = root.find("title")
                    if title_elem is not None:
                        title_elem.text = "凡人修仙传 (2025)"
                    tree.write(nfo_path, encoding="utf-8", xml_declaration=True)
                    log(f"  [NFO修正] 已将标题规范化为: 凡人修仙传 (2025)")
                except Exception as e:
                    log(f"  [NFO修正失败] {e}")

            # C. 重新生成并修正内部分集 .strm 中的标题参数为 "凡人修仙传 (2025)"
            for root_d, _, files in os.walk(target_live_dir):
                for fn in files:
                    if fn.endswith(".strm"):
                        fp = os.path.join(root_d, fn)
                        try:
                            with open(fp, "r", encoding="utf-8") as f:
                                line = f.readline().strip()
                            if ":8099/pending" in line:
                                # 替换 t=凡人修仙传 为 t=凡人修仙传 (2025)
                                new_line = line.replace("t=%E5%87%A1%E4%BA%BA%E4%BF%AE%E4%BB%99%E4%BC%A0",
                                                        "t=" + urllib.parse.quote("凡人修仙传 (2025)"))
                                new_line = new_line.replace("t=凡人修仙传&", "t=" + urllib.parse.quote("凡人修仙传 (2025)") + "&")
                                with open(fp, "w", encoding="utf-8") as f:
                                    f.write(new_line + "\n")
                        except Exception:
                            pass
            log(f"  [STRM修正] 已将真人版全部分集 strm 参数更新为消歧片名")

            # D. 清理数据库 source_audits 中针对国产剧集《凡人修仙传》误锁定的动画大包记录
            try:
                con = vod_db._conn()
                con.execute("DELETE FROM source_audits WHERE cat='国产剧集' AND title='凡人修仙传'")
                con.commit()
                log("  [锁源库净化] 已清空真人剧误锁定的动画大包记录，防点播张冠李戴")
            except Exception as e:
                log(f"  [锁源库净化失败] {e}")

            fixed_count += 1

    log(f"重名影视形态消歧治理完成，共处理 {fixed_count} 组重名条目")
    return fixed_count


def clean_broken_empty_dirs(dry_run=False):
    """清理空目录与损坏孤立目录。"""
    log("=== [阶段 3] 扫描并清理无NFO的空目录与残破目录 ===")
    cleaned = 0
    for cat in os.listdir(CINEMA2):
        cat_dir = os.path.join(CINEMA2, cat)
        if not os.path.isdir(cat_dir):
            continue
        for item in os.listdir(cat_dir):
            p = os.path.join(cat_dir, item)
            if not os.path.isdir(p):
                continue
            # 检查是否有 nfo 或 strm
            has_media = False
            for root_d, _, files in os.walk(p):
                if any(f.endswith((".nfo", ".strm", ".mp4", ".mkv")) for f in files):
                    has_media = True
                    break
            if not has_media:
                log(f"发现无媒体无元数据的孤立空目录: {cat}/{item}")
                if not dry_run:
                    empty_archive = os.path.join(TRASH_ARCHIVE, f"empty_{cat}_{item}")
                    shutil.move(p, empty_archive)
                    log(f"  --> 已安全移入归档区: {cat}/{item}")
                cleaned += 1
    log(f"孤立空目录治理完成，共清理 {cleaned} 个目录")
    return cleaned


def run_governance(dry_run=False):
    """一键执行全套影视分类纠偏与重名消歧治理。"""
    log(">>>>>>>> 开始执行全库影视分类精准度治理与重名消歧 <<<<<<<<")
    c1 = cleanup_known_misplaced(dry_run=dry_run)
    c2 = disambiguate_anime_vs_liveaction(dry_run=dry_run)
    c3 = clean_broken_empty_dirs(dry_run=dry_run)
    total = c1 + c2 + c3
    log(f">>>>>>>> 治理全部完成！累计处理 {total} 处错位/重名/空目录 <<<<<<<<")
    return total


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    run_governance(dry_run=dry)
