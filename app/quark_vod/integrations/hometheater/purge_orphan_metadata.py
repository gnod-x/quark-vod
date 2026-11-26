#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jellyfin2 孤儿剧照、演职员头像与僵尸元数据全量深度清理器
=========================================================
功能：
  1. 清理 metadata/library 下未被引用的历史孤儿剧照与海报。
  2. 清理 metadata/People 下随影视淘汰而残留的无主演职员头像目录。
  3. 通过官方 Jellyfin API 批量注销已淘汰影视遗留的僵尸 Person 实体记录。
  4. 缩略图缓存由 Jellyfin 管理，本任务不清空正在使用的缓存。
  5. 触发 Jellyfin 官方数据库优化任务 (OptimizeDatabaseTask)。
"""

from quark_vod.integrations.hometheater import paths
from quark_vod import config as host_config

import os
import sys
import time
import shutil
import sqlite3
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

BASE = paths.BASE_DIR

from quark_vod import config as vod_cfg
from quark_vod import cleanup
from quark_vod.coordination import maintenance, Busy

JF_DB = paths.JELLYFIN2_DB
METADATA_DIR = paths.JELLYFIN2_METADATA_DIR
PEOPLE_DIR = os.path.join(METADATA_DIR, "People")
LIB_DIR = os.path.join(METADATA_DIR, "library")


class IndependentStageError(RuntimeError):
    """A local/remote operation failed after its protection state was known."""


class PartialCleanupError(RuntimeError):
    """Independent phases ran, but at least one phase did not complete."""
    def __init__(self, failures):
        self.failures = tuple(failures)
        super().__init__("partial: " + ",".join(self.failures))


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [PurgeMetadata] {msg}", flush=True)


def get_active_people_names():
    """获取当前影视库中真实引用的演职员姓名集合（免锁只读）。"""
    if not os.path.exists(JF_DB):
        raise RuntimeError('Jellyfin database missing')
    try:
        with closing(sqlite3.connect(f"file:{JF_DB}?mode=ro", uri=True, timeout=2)) as con:
            cur = con.cursor()
            cur.execute("""
                SELECT DISTINCT lower(p.Name)
                FROM Peoples p
                JOIN PeopleBaseItemMap m ON p.Id = m.PeopleId
            """)
            return {r[0] for r in cur.fetchall() if r[0]}
    except Exception as e:
        raise RuntimeError('读取活跃演职员失败，暂缓清理') from e


def purge_orphan_library_metadata():
    """清理 metadata/library 下未引用的剧照/海报。"""
    log("=== 阶段 1: 清理 metadata/library 孤儿剧照与海报 ===")
    files, size = cleanup.prune_jellyfin2_orphan_metadata(dry_run=False, verbose=False, strict=True)
    log(f"已清理孤儿剧照/海报: {files} 个文件, 释放 {size / (1024 * 1024):.2f} MB")
    return files, size


def purge_orphan_people_dirs(active_names):
    """清理 metadata/People 磁盘目录。"""
    log("=== 阶段 2: 清理 metadata/People 孤儿演职员头像磁盘目录 ===")
    if not os.path.isdir(PEOPLE_DIR) or len(active_names) < 100:
        log("活跃演职员样本过少或目录不存在，跳过磁盘目录清理以策安全")
        return 0, 0

    deleted_dirs = 0
    deleted_bytes = 0
    empty_letters = 0
    failures = 0

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
                except OSError:
                    failures += 1

        if os.path.isdir(letter_dir) and not os.listdir(letter_dir):
            try:
                os.rmdir(letter_dir)
                empty_letters += 1
            except Exception:
                pass

    mb = deleted_bytes / (1024 * 1024)
    log(f"已清理孤儿头像目录: {deleted_dirs} 个, 空字母层: {empty_letters} 个, 释放 {mb:.2f} MB")
    if failures:
        raise IndependentStageError(f'头像目录清理失败 {failures} 项')
    return deleted_dirs, deleted_bytes


def purge_orphan_person_entities_via_api(active_names, max_workers=20):
    """通过 Jellyfin 官方 API 注销 BaseItems 中已无影视引用的 Person 僵尸实体。"""
    log("=== 阶段 3: 通过 Jellyfin API 注销 BaseItems 僵尸 Person 实体 ===")
    if not os.path.exists(JF_DB) or len(active_names) < 100:
        raise RuntimeError("Person 引用保护不可确认，停止后续清理")

    orphan_ids = []
    try:
        with closing(sqlite3.connect(f"file:{JF_DB}?mode=ro", uri=True, timeout=2)) as con:
            cur = con.cursor()
            cur.execute("SELECT Id, Name FROM BaseItems WHERE type='MediaBrowser.Controller.Entities.Person'")
            for pid, name in cur.fetchall():
                if name and name.lower() not in active_names:
                    orphan_ids.append(pid)
    except Exception as e:
        raise RuntimeError('读取 Person 列表失败') from e

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
    if deleted_count != total:
        raise IndependentStageError(f'Person 注销未完成: {deleted_count}/{total}')
    return deleted_count


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
            return {'status': 'not_available'}

        req = urllib.request.Request(
            f"{vod_cfg.JELLYFIN2_API}/ScheduledTasks/Running/{task_id}",
            data=b"",
            headers={"Authorization": f'MediaBrowser Token="{vod_cfg.JELLYFIN2_KEY}"'},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status not in (200, 204):
                raise RuntimeError('optimization not accepted')
            log(f"数据库优化任务已入队: HTTP {resp.status}，未等待其执行完成")
            return {'status': 'submitted'}
    except Exception as e:
        raise IndependentStageError('触发数据库优化任务失败') from e


@maintenance
def run_cleanup():
    log("开始执行全量孤儿剧照、演职员头像与元数据清理...")
    active_names = get_active_people_names()
    if len(active_names) < 100:
        raise RuntimeError('活跃演职员保护样本不足，暂缓所有清理')
    log(f"当前影院受保护的活跃演职员姓名总数: {len(active_names)}")

    # These phases share a verified active-name protection set, but a local
    # People filesystem failure does not invalidate the independent DB/API view.
    # Reference/identity failures and Busy are never downgraded to partial work.
    failures = []
    stages = (
        ("library", purge_orphan_library_metadata),
        ("people_disk", lambda: purge_orphan_people_dirs(active_names)),
        ("person_api", lambda: purge_orphan_person_entities_via_api(active_names, max_workers=25)),
        ("optimize", trigger_db_optimization),
    )
    for name, action in stages:
        try:
            action()
        except Busy:
            raise
        except cleanup.MetadataCleanupError:
            # Library errors may wrap a failed reference re-read. Never treat
            # their diagnostic code as proof that protection remained known.
            raise
        except IndependentStageError as exc:
            failures.append(name)
            log(f"阶段 {name} 未完成: {type(exc).__name__}")
        except OSError as exc:
            # Only the People disk phase reads no new protection state.
            if name != "people_disk":
                raise
            failures.append(name)
            log(f"阶段 {name} 未完成: {type(exc).__name__}(errno={exc.errno})")
    if failures:
        log("孤儿元数据清理结果: partial; failed=" + ",".join(failures))
        raise PartialCleanupError(failures)
    log("孤儿元数据与头像清理阶段完成；客户端缩略图缓存由 Jellyfin 管理")
    return {"status": "complete"}



def main():
    try:
        run_cleanup()
        return 0
    except Exception as exc:
        if isinstance(exc, cleanup.MetadataCleanupError):
            detail = str(exc)
        else:
            causes = []
            cause = exc
            while cause is not None and len(causes) < 6:
                label = type(cause).__name__
                if isinstance(cause, OSError) and cause.errno is not None:
                    label += f'(errno={cause.errno})'
                causes.append(label)
                cause = cause.__cause__
            detail = ' <- '.join(causes)
        log(f'元数据清理未完成: {detail}')
        return 75 if isinstance(exc, Busy) else 1


if __name__ == "__main__":
    sys.exit(main())
