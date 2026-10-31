#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全库已转存影片元数据与流信息自愈修复工具

功能：
1. 扫描 cinema2 中所有已处于 live 状态的 .strm 文件
2. 确保 OpenList QuarkTV 播放驱动各级目录缓存已逐级刷新
3. 自检对应条目在 Jellyfin2 数据库中的媒体流元数据（MediaStreamInfos）
4. 对流元数据缺失的条目，主动调用探针生成缓存并触发 Jellyfin2 刷新，全面恢复 DirectPlay
"""

import os
import re
import sys
import json
import sqlite3
import subprocess
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quark_vod import config
from quark_vod.openlist_client import OpenListClient

def main():
    print(f"[{config.SERVICE_NAME}] 开始全面扫描 cinema2 媒体库进行流元数据与缓存对齐自愈...")
    ol = None
    try:
        ol = OpenListClient()
    except Exception as e:
        print(f"OpenList 客户端连接警告: {e}")

    live_strms = []
    for root, dirs, files in os.walk(config.CINEMA2_DIR):
        for f in files:
            if f.endswith(".strm"):
                strm_path = os.path.join(root, f)
                try:
                    with open(strm_path, "r", encoding="utf-8", errors="ignore") as fp:
                        content = fp.read().strip()
                        if content.startswith("http") and not content.startswith(f"{config.PENDING_BASE_URL}/pending"):
                            live_strms.append((strm_path, content))
                except Exception:
                    pass

    print(f"发现已激活（Live 直链）的 .strm 文件共: {len(live_strms)} 部")

    from contextlib import closing
    with closing(sqlite3.connect(f"file:{config.JELLYFIN2_DB}?mode=ro&immutable=1", uri=True)) as con:
        cur = con.cursor()

        refreshed_dirs = set()
        repaired_count = 0

        for strm_path, url in live_strms:
            # 1. 刷新 OpenList 目录缓存
            try:
                parsed = urllib.parse.urlparse(url)
                remote = urllib.parse.unquote(parsed.path)
                if remote.startswith("/d/"):
                    play_remote = remote[len("/d"):]
                    parent_dir = play_remote.rsplit("/", 1)[0]
                    if parent_dir not in refreshed_dirs and ol:
                        ol.refresh_path_tree(parent_dir)
                        refreshed_dirs.add(parent_dir)
            except Exception:
                pass

            # 2. 检查 Jellyfin2 媒体流
            container_path = strm_path.replace(config.CINEMA2_DIR, "/cinema2")
            item = cur.execute("SELECT Id, Name FROM BaseItems WHERE Path = ? LIMIT 1", (container_path,)).fetchone()
            if not item:
                continue
            item_id = item[0]
            streams = cur.execute("SELECT COUNT(*) FROM MediaStreamInfos WHERE ItemId = ?", (item_id,)).fetchone()[0]
            if streams == 0:
                print(f"检测到流元数据缺失: {item[1]} ({container_path})，立即触发探针修复...")
                # 在容器内主动触发一次探针，写入探针缓存
                try:
                    cmd = [
                        "docker", "exec", "jellyfin2",
                        "/custom-ffmpeg/ffprobe",
                        "-i", container_path,
                        "-show_streams", "-show_format",
                        "-print_format", "json"
                    ]
                    subprocess.run(cmd, capture_output=True, timeout=15)
                except Exception as e:
                    print(f"探针预探测警告: {e}")

                # 通知 Jellyfin2 刷新 Item（ReplaceAllMetadata=false 保护 AI 精修简介）
                if config.JELLYFIN2_KEY:
                    try:
                        ref_url = (f"{config.JELLYFIN2_API}/Items/{item_id}/Refresh"
                                   f"?MetadataRefreshMode=FullRefresh&ImageRefreshMode=None"
                                   f"&ReplaceAllMetadata=false&ReplaceAllImages=false")
                        req = urllib.request.Request(ref_url, data=b"", method="POST")
                        req.add_header("Authorization", f'MediaBrowser Token="{config.JELLYFIN2_KEY}"')
                        with urllib.request.urlopen(req, timeout=10) as resp:
                            pass
                    except Exception as e:
                        print(f"Jellyfin2 Item 刷新调用警告: {e}")

                repaired_count += 1

    print(f"[{config.SERVICE_NAME}] 全库扫描与自愈修复完成，修复条目数: {repaired_count}")

if __name__ == "__main__":
    main()
