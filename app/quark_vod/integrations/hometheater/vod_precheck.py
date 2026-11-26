#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bounded source-audit helpers for the configured Quark-VOD library."""

from quark_vod.integrations.hometheater import paths
from quark_vod import config as host_config

import os
import sys
import argparse
import sqlite3

BASE_DIR = paths.BASE_DIR

from quark_vod import config as vod_cfg
from quark_vod import db as vod_db
from quark_vod.auditor import VodAuditor

def print_report():
    con = vod_db._conn()
    cur = con.cursor()

    # 统计总条目
    total_audits = cur.execute("SELECT COUNT(*) FROM source_audits").fetchone()[0]
    valid_4k = cur.execute("SELECT COUNT(*) FROM source_audits WHERE is_4k = 1 AND has_quark_source = 1").fetchone()[0]
    valid_hd = cur.execute("SELECT COUNT(*) FROM source_audits WHERE is_4k = 0 AND has_quark_source = 1").fetchone()[0]
    dead_cnt = cur.execute("SELECT COUNT(*) FROM source_audits WHERE check_status IN ('dead', 'not_found')").fetchone()[0]
    dead_banned = cur.execute("SELECT COUNT(*) FROM dead_sources WHERE expires_at > ?", (vod_db.now(),)).fetchone()[0]

    print("\n====================================================================")
    print("      家庭影院网盘点播【全库夸克 4K 片源保真与健康大盘报表】")
    print("====================================================================")
    print(f" • 已完成前置预检条目数: {total_audits} 部")
    print(f" • 确认锁定【真 4K 原画资源】: {valid_4k} 部 (点播秒开，100% 成功)")
    print(f" • 确认锁定【普通高清资源】  : {valid_hd} 部")
    print(f" • 全网未搜到有效夸克源      : {dead_cnt} 部")
    print(f" • 当前黑名单中的失效死链    : {dead_banned} 个 (已熔断拦截，不踩坑)")
    if total_audits > 0:
        p4k = (valid_4k / total_audits) * 100
        print(f" • 夸克真 4K 原画覆盖保真率 : {p4k:.1f}%")
    print("====================================================================\n")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="输出健康大盘报表")
    ap.add_argument("--heal", action="store_true", help="对存量已锁定源执行健康巡检，失效自动重新搜索打捞新源替换")
    ap.add_argument("--sync", action="store_true", help="全量全库扫描：为所有未锁定的影视打捞并预存 4K 夸克链接")
    ap.add_argument("--fix-formats", action="store_true", help="网盘伪装文件巡检与格式扶正：自动检测魔数并重命名")
    ap.add_argument("--sample", type=int, default=0, help="抽检 N 部影视")
    ap.add_argument("--all", action="store_true", help="全套执行：先执行存量失效自愈，再全量补充未锁源影视，并扶正伪装格式")
    args = ap.parse_args()

    if args.report and not (args.heal or args.sync or args.all or args.sample or args.fix_formats):
        print_report()
        return

    auditor = VodAuditor()

    # 1. 存量失效自愈换新
    if args.heal or args.all:
        auditor.check_and_heal_locked_shares(limit=args.sample if args.sample and not args.all else None)

    # 2. 全量/增量未锁源打捞
    if args.sync or args.all or (args.sample and not args.heal and not args.fix_formats):
        auditor.audit_entire_library(limit=args.sample if args.sample and not args.all else None)

    # 3. 伪装格式视频自动巡检与扶正
    if args.fix_formats or args.all:
        auditor.fix_disguised_vod_files()

    print_report()

if __name__ == "__main__":
    main()
