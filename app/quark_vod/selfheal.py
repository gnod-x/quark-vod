#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""点播栈自愈（2026-10-16 新增）

解决的问题
==========
strm 一旦回写为 live 直链，Jellyfin 就直接请求 OpenList（:5244），
**完全绕过 8099**。此时若用户手动删除了夸克网盘里的点播文件：

  - strm 永久停在 live 死链上，Jellyfin 点播必然黑屏（FFmpeg exit code 8/183）；
  - 没有任何代码路径能检测并恢复；
  - `find_done_transfer` 只查数据库 status='done'，不校验文件真实存在；
  - cleanup.py 是死代码（无 cron/timer/线程），且 remove 失败即 continue，
    连手动跑都无法还原。

三道防线
========
  ① 播放前校验（server.py）：命中 done 时先校验文件在否，不在则作废并重转
  ② 后台自愈（本模块 scan_and_heal）：定时扫 live strm，失效的自动回退 pending
  ③ cleanup 修复（cleanup.py）：删除失败也要还原 strm

本模块负责第 ② 道。

判定权威性
==========
必须用夸克官方 API（`_quark_list`），**不得**用 OpenList 列表——
后者带目录缓存且长期不刷新（见 CLAUDE.md 红线 #17）。

用法
====
  python3 -m quark_vod.selfheal              # 单次扫描（手动/定时）
  python3 -m quark_vod.selfheal --dry-run    # 预演，不改任何东西
  # 后台线程由 server.py 在启动时拉起（config.SELFHEAL_ENABLED）
"""

import os
import time

from . import config
from . import db
from . import strm_manager
from .openlist_client import OpenListClient


def _iter_live_strms():
    """遍历 cinema2 下所有 live strm，返回 [(path, remote_path)]。"""
    out = []
    base = config.CINEMA2_DIR
    if not os.path.isdir(base):
        return out
    for root, _dirs, files in os.walk(base):
        for fn in files:
            if not fn.endswith(".strm"):
                continue
            p = os.path.join(root, fn)
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read().strip()
            except Exception:
                continue
            if not strm_manager.is_live(content):
                continue
            remote = strm_manager.parse_live_remote_path(content)
            if remote:
                out.append((p, remote))
    return out


def _remote_exists_batch(tr, remote_paths):
    """批量校验多个网盘路径是否存在（大幅降低夸克 API 调用量）。

    【为什么必须批量 —— 夸克封控，2026-10-16】
    自愈校验必须走夸克官方 API（OpenList 缓存严重滞后，实测 /夸克/点播
    只显示 1 项而实际有 13 项，会把有效影片误判为失效），而该 API 用
    **Web cookie** 认证。红线 #23 要求把 cookie 使用面收敛到写入侧，
    故必须严控调用量。

    原实现逐级下钻，每部影片 6 次调用（14 部 × 6 × 12 轮/小时 ≈ 1008 次/小时）。
    本实现改为：**只列一次 /点播 根目录**，即可判定所有影片目录是否存在——
    因为所有 remote_path 都在 QUARK_VOD_ROOT 之下，第一级目录名一次性拿到。

    返回 {remote_path: bool}。
    """
    result = {}
    if not remote_paths:
        return result

    # 一次调用拿到 /点播 下所有条目
    top_names = set()
    try:
        entries = tr._quark_list(config.QUARK_VOD_ROOT)
        top_names = {e["name"] for e in entries}
    except Exception as ex:
        err_msg = str(ex)
        if "不存在该目录" in err_msg or "object not found" in err_msg.lower():
            # 确凿判定：用户在网盘手动清空/删除了点播根目录，自动重新建立干净的根目录
            try:
                tr.ensure_vod_dir()
            except Exception:
                pass
            top_names = set()  # 网盘点播目录已被连锅端，全量判定为不存在并触发自愈还原！
        else:
            # 真正的网络连接或权限异常 → 保守放行，避免误回退
            return {p: True for p in remote_paths}

    root_parts = [x for x in config.QUARK_VOD_ROOT.split("/") if x]

    for p in remote_paths:
        parts = [x for x in p.split("/") if x]
        # 必须位于 QUARK_VOD_ROOT 之下才能用此批量结论
        if parts[:len(root_parts)] != root_parts:
            result[p] = True          # 不在点播根下，保守放行
            continue
        rest = parts[len(root_parts):]
        if not rest:
            result[p] = True
            continue
        # 第一级（影片目录名 / 裸文件名）必须在 /点播 列表里
        result[p] = rest[0] in top_names
    return result


def _remote_exists(tr, remote_path):
    """校验单个网盘路径是否存在（权威，无缓存）。

    保留此单点版本供调用方按需使用；批量场景请用 `_remote_exists_batch`
    以降低夸克 API 调用量（红线 #23：cookie 使用面收敛）。
    """
    return _remote_exists_batch(tr, [remote_path]).get(remote_path, True)


def scan_and_heal(dry_run=False, verbose=True):
    """扫描 live strm，把失效的回退为 pending。

    返回 (检查数, 失效数, 回退数, 跳过数)。

    调用量（红线 #23，夸克封控）：本函数对夸克官方 API 只发
    **1 次**列目录请求（`_remote_exists_batch` 一次覆盖所有影片目录），
    外加至多 `SELFHEAL_FILE_CHECK_BATCH` 次文件级校验。
    """
    from .transfer import QuarkTransfer
    try:
        ol = OpenListClient()
    except Exception as e:
        if verbose:
            print(f"[selfheal] OpenList 不可用，跳过本次: {e}", flush=True)
        return 0, 0, 0, 0
    tr = QuarkTransfer(ol)

    # 统计夸克 API 调用次数（观测用，便于核对封控风险）
    api_calls = {"n": 0}
    _orig = tr._request

    def _counted(url, *a, **k):
        api_calls["n"] += 1
        return _orig(url, *a, **k)

    tr._request = _counted

    lives = _iter_live_strms()
    checked = broken = restored = skipped = 0
    if verbose:
        print(f"[selfheal] 发现 {len(lives)} 个 live strm，开始校验", flush=True)

    # 按 remote_path 去重（多专区同一影片共享同一 remote_path）
    seen = {}
    for path, remote in lives:
        seen.setdefault(remote, []).append(path)

    # 【批量】一次调用覆盖所有影片目录的存在性判定
    exists_map = _remote_exists_batch(tr, list(seen.keys()))

    for remote, paths in seen.items():
        checked += 1
        if exists_map.get(remote, True):
            continue
        broken += 1
        if verbose:
            print(f"[selfheal] 失效: {remote}", flush=True)
        if dry_run:
            continue
        # 回退每个指向该 remote 的 strm 为 pending
        restore_failed = []
        for p in paths:
            try:
                params = strm_manager.parse_pending_params(_pending_from_map(p))
                if not params:
                    skipped += 1
                    restore_failed.append(p)
                    if verbose:
                        print(f"[selfheal]   跳过（strm_map 无记录）: "
                              f"{os.path.relpath(p, config.CINEMA2_DIR)}", flush=True)
                    continue
                ok = strm_manager.restore_pending(
                    p, params["cat"], params["title"],
                    params["season"], params["episode"])
                if ok:
                    restored += 1
                    if verbose:
                        print(f"[selfheal]   回退: {os.path.relpath(p, config.CINEMA2_DIR)}",
                              flush=True)
                else:
                    # 文件本就是 pending（幂等），不算失败
                    if strm_manager.is_pending(open(p, encoding="utf-8", errors="ignore").read()):
                        restored += 1
                    else:
                        restore_failed.append(p)
            except Exception as e:
                skipped += 1
                restore_failed.append(p)
                if verbose:
                    print(f"[selfheal]   回退失败 {p}: {e}", flush=True)

        # 作废数据库记录，让下次点播重新走搜索/转存。
        #
        # 2026-10-20 修复（151 条死链事故）：原实现在 strm 还原失败时【照样】
        # 作废 DB 记录，导致 DB 说 cleaned（文件已删）、磁盘 strm 却仍指向
        # 已删直链，客户端拿到 500 死链且永不恢复（《凡人修仙传》147 集实测）。
        # 现在：只要有 strm 没还原成功，就保留 DB 的 done 状态（下次点播仍
        # 走防线①校验网盘、发现不存在再重新转存），绝不让 DB 领先于磁盘。
        if restore_failed:
            print(f"[selfheal] ⚠️ {len(restore_failed)}/{len(paths)} 个 strm 回退失败，"
                  f"保留 DB 原状态（下次点播将重新校验转存）: {remote}", flush=True)
            for p in restore_failed:
                print(f"[selfheal]     - {os.path.relpath(p, config.CINEMA2_DIR)}", flush=True)
            continue
        try:
            _invalidate_db_by_remote(remote)
        except Exception as e:
            if verbose:
                print(f"[selfheal]   作废记录失败 {remote}: {e}", flush=True)

    if verbose:
        print(f"[selfheal] 完成: 检查 {checked} 失效 {broken} "
              f"回退 {restored} 跳过 {skipped} "
              f"| 夸克API调用 {api_calls['n']} 次", flush=True)
    return checked, broken, restored, skipped


def heal_remote(remote):
    """针对单个 remote_path 立即自愈（供 8099 /clean 上游失效时调用）。

    与 scan_and_heal 的区别：只校验并回退这一条，1 次夸克 API 调用即可，
    不必等每日整轮扫描。用于「播放中撞上 502」的即时恢复。
    """
    from .transfer import QuarkTransfer
    try:
        tr = QuarkTransfer(OpenListClient())
    except Exception as e:
        print(f"[selfheal] 即时自愈跳过（OpenList 不可用）: {e}", flush=True)
        return 0
    if _remote_exists_batch(tr, [remote]).get(remote, True):
        return 0   # 仍存在：属 OpenList 缓存抖动，不动
    paths = [p for p, r in _iter_live_strms() if r == remote]
    restored = 0
    restore_failed = []
    for p in paths:
        try:
            params = strm_manager.parse_pending_params(_pending_from_map(p))
            if not params:
                restore_failed.append(p)
                continue
            if strm_manager.restore_pending(p, params["cat"], params["title"],
                                            params["season"], params["episode"]):
                restored += 1
                print(f"[selfheal]   即时回退: {os.path.relpath(p, config.CINEMA2_DIR)}", flush=True)
        except Exception as e:
            restore_failed.append(p)
            print(f"[selfheal]   即时回退失败 {p}: {e}", flush=True)
    if restore_failed:
        print(f"[selfheal] ⚠️ 即时自愈 {len(restore_failed)} 个 strm 回退失败，保留 DB 状态", flush=True)
        return restored
    if paths:
        try:
            _invalidate_db_by_remote(remote)
        except Exception:
            pass
    print(f"[selfheal] 即时自愈完成: {remote} 回退 {restored}/{len(paths)}", flush=True)
    return restored


def _pending_from_map(strm_path):
    """从 strm_map 反查出该 strm 对应的 pending 占位 URL。

    用于把 live strm 还原为 pending 时补齐 cat/title/season/episode 参数。

    注意：strm_map 可能对【同一个 strm 文件】存有多条记录，title 不同——
      - `scan_and_register()` 去年份后的 title（如 "狼犬丹尼"）
      - `activate()` 传入的原始 t 参数（如 "狼犬丹尼 (2005)"，带年份）
    二者指向同一 strm_path。重建 pending URL 时**必须优先用带年份的那条**，
    因为 cinema2 的 strm 原始约定就是带年份（见「cinema2 目录与 strm 协议」），
    用无年份的会让后续 /pending 匹配与目录定位出现偏差。
    """
    import urllib.parse
    try:
        rows = db._conn().execute(
            "SELECT cat,title,season,episode FROM strm_map WHERE strm_path=?",
            (strm_path,)).fetchall()
    except Exception:
        return ""
    if not rows:
        return ""
    # 优先选 title 带年份后缀的记录（更贴近 strm 原始参数）
    import re
    best = None
    for r in rows:
        t = r["title"] or ""
        if re.search(r"[(（]\d{4}[)）]\s*$", t):
            best = r
            break
    if best is None:
        best = rows[0]
    q = urllib.parse.urlencode({
        "cat": best["cat"], "t": best["title"],
        "s": best["season"], "e": best["episode"],
    })
    return f"{strm_manager.PENDING_PREFIX}?{q}"


def _invalidate_db_by_remote(remote):
    """把 remote_path 对应的 done 记录作废，使 find_done_transfer 落空。

    保留 title 等信息便于排查，只清 remote_path 并改状态。
    """
    con = db._conn()
    con.execute(
        "UPDATE transfers SET status='cleaned', remote_path=NULL, "
        "error='[自愈] 网盘文件已不存在，已作废待重转', updated_at=? "
        "WHERE remote_path=? AND status='done'",
        (db.now(), remote))
    con.commit()


def heal_loop(stop_event=None, verbose=True):
    """后台自愈循环（由 server.py 启动为 daemon 线程）。"""
    if config.SELFHEAL_INITIAL_DELAY:
        time.sleep(config.SELFHEAL_INITIAL_DELAY)
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            scan_and_heal(verbose=verbose)
        except Exception as e:
            if verbose:
                print(f"[selfheal] 扫描异常: {e}", flush=True)
        # 分段 sleep，便于快速响应停止信号
        slept = 0
        while slept < config.SELFHEAL_INTERVAL:
            if stop_event is not None and stop_event.is_set():
                return
            time.sleep(min(5, config.SELFHEAL_INTERVAL - slept))
            slept += 5


def main():
    import sys
    dry = "--dry-run" in sys.argv
    print(f"[selfheal] {'预演' if dry else '执行'} @ "
          f"{time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    scan_and_heal(dry_run=dry)


if __name__ == "__main__":
    main()
