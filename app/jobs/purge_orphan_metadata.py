#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jellyfin2 孤儿剧照、演职员头像与僵尸元数据全量深度清理器
=========================================================
功能：
  1. 清理 metadata/library 下未被引用的历史孤儿剧照与海报。
  2. 清理 metadata/People 下随影视淘汰而残留的无主演职员头像目录。
  3. 通过官方 Jellyfin API 批量注销已淘汰影视遗留的僵尸 Person 实体记录。
  4. 清理 cache/images 下已淘汰资源的过期缩略图缓存。
  5. 触发 Jellyfin 官方数据库优化任务 (OptimizeDatabaseTask)。
"""

import os
import sys
import time
import shutil
import sqlite3
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

BASE = "/home/dong/projects/hometheater"
sys.path.insert(0, os.path.join(BASE, "scripts"))
sys.path.insert(0, "/home/dong/projects/quark-vod/app")

from quark_vod import config as vod_cfg
from quark_vod import cleanup

JF_DB = os.path.join(BASE, "jellyfin2/config/data/jellyfin.db")
METADATA_DIR = os.path.join(BASE, "jellyfin2/config/metadata")
PEOPLE_DIR = os.path.join(METADATA_DIR, "People")
LIB_DIR = os.path.join(METADATA_DIR, "library")
CACHE_IMG_DIR = os.path.join(BASE, "jellyfin2/cache/images")


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [PurgeMetadata] {msg}", flush=True)


def get_active_people_names():
    """获取当前影视库中真实引用的演职员姓名集合（免锁只读）。"""
    if not os.path.exists(JF_DB):
        return set()
    try:
        with closing(sqlite3.connect(f"file:{JF_DB}?mode=ro&immutable=1", uri=True)) as con:
            cur = con.cursor()
            cur.execute("""
                SELECT DISTINCT lower(p.Name)
                FROM Peoples p
                JOIN PeopleBaseItemMap m ON p.Id = m.PeopleId
            """)
            return {r[0] for r in cur.fetchall() if r[0]}
    except Exception as e:
        log(f"读取活跃演职员失败: {e}")
        return set()


def purge_orphan_library_metadata():
    """清理 metadata/library 下未引用的剧照/海报。"""
    log("=== 阶段 1: 清理 metadata/library 孤儿剧照与海报 ===")
    try:
        files, b = cleanup.prune_jellyfin2_orphan_metadata(dry_run=False, verbose=False)
        mb = b / (1024 * 1024)
        log(f"已清理孤儿剧照/海报: {files} 个文件, 释放 {mb:.2f} MB")
        return files, b
    except Exception as e:
        log(f"清理 library 元数据失败: {e}")
        return 0, 0


def purge_orphan_people_dirs(active_names):
    """清理 metadata/People 磁盘目录。"""
    log("=== 阶段 2: 清理 metadata/People 孤儿演职员头像磁盘目录 ===")
    if not os.path.isdir(PEOPLE_DIR) or len(active_names) < 100:
        log("活跃演职员样本过少或目录不存在，跳过磁盘目录清理以策安全")
        return 0, 0

    deleted_dirs = 0
    deleted_bytes = 0
    empty_letters = 0

    for letter in os.listdir(PEOPLE_DIR):
        letter_dir = os.path.join(PEOPLE_DIR, letter)
        if not os.path.isdir(letter_dir):
            continue
        for name in os.listdir(letter_dir):
            p_dir = os.path.join(letter_dir, name)
            if not os.path.isdir(p_dir):
                continue
            if name.lower() not in active_names:
                try:
                    sz = sum(os.path.getsize(os.path.join(p_dir, f))
                             for f in os.listdir(p_dir) if os.path.isfile(os.path.join(p_dir, f)))
                    shutil.rmtree(p_dir)
                    deleted_dirs += 1
                    deleted_bytes += sz
                except Exception:
                    pass

        if os.path.isdir(letter_dir) and not os.listdir(letter_dir):
            try:
                os.rmdir(letter_dir)
                empty_letters += 1
            except Exception:
                pass

    mb = deleted_bytes / (1024 * 1024)
    log(f"已清理孤儿头像目录: {deleted_dirs} 个, 空字母层: {empty_letters} 个, 释放 {mb:.2f} MB")
    return deleted_dirs, deleted_bytes


def purge_orphan_person_entities_via_api(active_names, max_workers=20):
    """通过 Jellyfin 官方 API 注销 BaseItems 中已无影视引用的 Person 僵尸实体。"""
    log("=== 阶段 3: 通过 Jellyfin API 注销 BaseItems 僵尸 Person 实体 ===")
    if not os.path.exists(JF_DB) or len(active_names) < 100:
        return 0

    orphan_ids = []
    try:
        with closing(sqlite3.connect(f"file:{JF_DB}?mode=ro&immutable=1", uri=True)) as con:
            cur = con.cursor()
            cur.execute("SELECT Id, Name FROM BaseItems WHERE type='MediaBrowser.Controller.Entities.Person'")
            for pid, name in cur.fetchall():
                if name and name.lower() not in active_names:
                    orphan_ids.append(pid)
    except Exception as e:
        log(f"读取僵尸 Person 列表失败: {e}")
        return 0

    total = len(orphan_ids)
    log(f"发现待注销僵尸 Person 实体: {total} 个")
    if total == 0:
        return 0

    def _del_one(pid):
        req = urllib.request.Request(
            f"{vod_cfg.JELLYFIN2_API}/Items/{pid}",
            headers={"Authorization": f'MediaBrowser Token="{vod_cfg.JELLYFIN2_KEY}"'},
            method="DELETE",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status in (200, 204)
        except urllib.error.HTTPError as ex:
            return ex.code == 404
        except Exception:
            return False

    deleted_count = 0
    batch_size = 1000
    for i in range(0, total, batch_size):
        chunk = orphan_ids[i:i + batch_size]
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            results = list(ex.map(_del_one, chunk))
        batch_ok = sum(1 for r in results if r)
        deleted_count += batch_ok
        log(f"  注销进度: {min(i + batch_size, total)}/{total} ({deleted_count} 成功)")

    log(f"僵尸 Person 实体注销完成: 共注销 {deleted_count} 个")
    return deleted_count


def purge_resized_image_cache():
    """清理 cache/images 下的旧客户端缩略图缓存。"""
    log("=== 阶段 4: 清理 cache/images 过期缩略图缓存 ===")
    if not os.path.isdir(CACHE_IMG_DIR):
        return 0, 0

    freed = 0
    count = 0
    for root, dirs, files in os.walk(CACHE_IMG_DIR, topdown=False):
        for f in files:
            fp = os.path.join(root, f)
            try:
                sz = os.path.getsize(fp)
                os.remove(fp)
                freed += sz
                count += 1
            except Exception:
                pass
        for d in dirs:
            dp = os.path.join(root, d)
            try:
                os.rmdir(dp)
            except Exception:
                pass

    mb = freed / (1024 * 1024)
    log(f"已清理客户端缩略图缓存: {count} 个文件, 释放 {mb:.2f} MB")
    return count, freed


def trigger_db_optimization():
    """触发 Jellyfin 官方数据库优化任务。"""
    log("=== 阶段 5: 通知 Jellyfin 触发数据库压缩优化 (OptimizeDatabaseTask) ===")
    try:
        import json
        req_list = urllib.request.Request(
            f"{vod_cfg.JELLYFIN2_API}/ScheduledTasks",
            headers={"Authorization": f'MediaBrowser Token="{vod_cfg.JELLYFIN2_KEY}"'},
        )
        with urllib.request.urlopen(req_list, timeout=10) as r:
            tasks = json.loads(r.read().decode("utf-8"))
        task_id = next((t["Id"] for t in tasks if t.get("Key") == "OptimizeDatabaseTask"), None)
        if not task_id:
            log("未找到 OptimizeDatabaseTask 任务 ID")
            return

        req = urllib.request.Request(
            f"{vod_cfg.JELLYFIN2_API}/ScheduledTasks/Running/{task_id}",
            data=b"",
            headers={"Authorization": f'MediaBrowser Token="{vod_cfg.JELLYFIN2_KEY}"'},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            log(f"数据库优化任务已触发: HTTP {resp.status}")
    except Exception as e:
        log(f"触发数据库优化任务失败: {e}")


def main():
    log("开始执行全量孤儿剧照、演职员头像与元数据清理...")
    active_names = get_active_people_names()
    log(f"当前影院受保护的活跃演职员姓名总数: {len(active_names)}")

    purge_orphan_library_metadata()
    purge_orphan_people_dirs(active_names)
    purge_orphan_person_entities_via_api(active_names, max_workers=25)
    purge_resized_image_cache()
    trigger_db_optimization()
    log("全量孤儿元数据与头像瘦身治理完成！")


if __name__ == "__main__":
    main()
