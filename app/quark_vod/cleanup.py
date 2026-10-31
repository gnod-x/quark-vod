
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
夸克网盘点播资源定时清理与 2TB 智能配额治理引擎
========================================================================
核心机制：
  1. 2TB 存储配额监控：每日扫描 /夸克/点播 真实占用，若超 2TB 自动 LRU 淘汰至 1TB。
  2. LRU 智能淘汰排序：
     - 白名单保护：正在追的剧集（未看完）、已收藏（Favorites）及港台经典专区绝不删除。
     - 淘汰优先级：从未播放（0次播放）> 最早播放（played_at 最旧）。
  3. 回收站安全删除：通过 OpenList 将云端视频移入网盘回收站，防误删。
  4. 虚拟解耦与 strm 还原：删除云端视频后，原子还原对应 .strm 为 pending 占位符，
     保留本地高清海报与 NFO 元数据，用户随时可二次点击秒级转存。
  5. 到期常规清理：原有 7 天未重播到期清理任务（KEEP_DAYS 机制）。
"""

import os
import sys
import time
import sqlite3

from . import config
from . import db
from . import strm_manager
from .openlist_client import OpenListClient, OpenListError

JF_DB = os.path.join(config.BASE_DIR, "jellyfin2/config/data/jellyfin.db")

# 2TB 触发上限与 1TB 目标水位
MAX_VOD_BYTES = 2 * 1024 * 1024 * 1024 * 1024       # 2TB (2,199,023,255,552 字节)
TARGET_VOD_BYTES = 1 * 1024 * 1024 * 1024 * 1024    # 1TB (1,099,511,627,776 字节)
PROTECTED_CATEGORIES = set()  # 港台经典不设免死保护，所有专区均受配额与淘汰治理


def get_protected_titles():
    """获取所有受保护（不可被网盘配额清理删除）的影片标题集合。仅收藏（Favorites）项目享有永久豁免。"""
    from contextlib import closing
    protected = set()

    # 查询 jellyfin.db 中已收藏的条目（包含电影与剧集，免锁只读）
    if os.path.exists(JF_DB):
        try:
            with closing(sqlite3.connect(f"file:{JF_DB}?mode=ro&immutable=1", uri=True)) as con:
                cur = con.cursor()
                cur.execute("""
                    SELECT b.Name, b.SeriesName FROM BaseItems b
                    JOIN UserData u ON b.Id = u.ItemId
                    WHERE u.IsFavorite = 1
                """)
                for row in cur.fetchall():
                    if row[0]:
                        protected.add(row[0].strip())
                    if row[1]:
                        protected.add(row[1].strip())
        except Exception as e:
            print(f"[cleanup] 读取 Jellyfin UserData 失败: {e}", flush=True)

    return protected


def get_vod_disk_usage(ol, max_depth=4, max_entries=400):
    """统计 /夸克/点播 目录下所有影视目录及文件的实际字节占用。

    2026-10-20 修复（网盘爆满 500GB 无感知）：原实现只展开【一层】子目录。
    第三方 4K 大包普遍是三层结构（`分享根 → 片名目录 → 集数区间目录 → 视频文件`），
    视频文件全在第二层，导致占用被严重低估——实测现行算法 1929 GB，
    递归后 2496 GB（低估 30%），配额检测因此判定"容量在限制内"从不淘汰，
    最终网盘仅剩 1.83 GB，任何 4K 转存必失败（save_as_top_fids: []）。

    与铁律十六（转存验票必须递归展开子目录）同一模式：那次修的是验票层，
    这次修的是配额层。限深 4 层、限 400 项，兼顾准确度与夸克 API 风控。
    """
    vod_root = config.QUARK_VOD_ROOT  # /夸克/点播
    items = []
    total_bytes = 0

    def _walk(path, depth):
        """递归统计目录字节数，返回 (总字节, 文件数)。超限深/超条目数即截断。"""
        if depth > max_depth:
            return 0, 0
        total = 0
        nfiles = 0
        try:
            entries = ol.list(path)
        except Exception:
            return 0, 0
        for it in entries:
            if len(items) >= max_entries:
                break
            if it.get("is_dir"):
                sub_size, sub_n = _walk(f"{path}/{it['name']}", depth + 1)
                total += sub_size
                nfiles += sub_n
            else:
                total += (it.get("size") or 0)
                nfiles += 1
        return total, nfiles

    for it in ol.list(vod_root):
        name = it["name"]
        p = f"{vod_root}/{name}"
        if it.get("is_dir"):
            sub_size, nfiles = _walk(p, 1)
            items.append({
                "name": name,
                "remote_path": p,
                "size": sub_size,
                "is_dir": True,
                "files": [],
                "file_count": nfiles,
            })
        else:
            sz = it.get("size", 0)
            items.append({
                "name": name,
                "remote_path": p,
                "size": sz,
                "is_dir": False,
                "files": [name],
                "file_count": 1,
            })
        total_bytes += items[-1]["size"]

    return total_bytes, items


def enforce_vod_quota(ol=None, max_bytes=MAX_VOD_BYTES, target_bytes=TARGET_VOD_BYTES,
                      dry_run=False, verbose=True):
    """
    检查夸克网盘点播目录容量是否超 2TB；若超标，按 LRU 智能淘汰至 1TB 目标水位。
    返回 (淘汰数量, 释放字节数, 剩余总字节数)。
    """
    if ol is None:
        ol = OpenListClient()

    total_bytes, items = get_vod_disk_usage(ol)
    gb = 1024 ** 3
    tb = 1024 ** 4

    if verbose:
        print(f"[quota] 当前点播网盘实际占用: {total_bytes / gb:.2f} GB / {max_bytes / tb:.2f} TB",
              flush=True)

    if total_bytes <= max_bytes:
        if verbose:
            print("[quota] 容量在配额限制内，无需执行配额淘汰。", flush=True)
        return 0, 0, total_bytes

    excess_gb = (total_bytes - target_bytes) / gb
    print(f"[quota] ⚠️ 点播目录已超过 2TB 上限！准备淘汰释放至少 {excess_gb:.2f} GB 空间至 1TB 目标水位...",
          flush=True)

    protected_titles = get_protected_titles()

    # 读取 transfers 表中所有 done 的记录
    con = db._conn()
    cur = con.cursor()
    cur.execute("SELECT * FROM transfers WHERE status='done'")
    done_transfers = cur.fetchall()

    # 2026-10-20 修复（淘汰后残留 151 条死链）：原实现按目录名匹配到
    # 【第一条】done 记录就 break，淘汰时只还原这一条的 strm、只把这一条
    # 标 cleaned。而一部剧通常有 33~47 条 done 记录（每集一条），其余记录
    # 仍是 done、strm 仍指向已删直链 → 客户端拿到 500 死链且永不恢复
    # （《墨雨云间》39 集、太平年 47 集实测）。
    # 现按 (cat, title) 聚合该影片的【全部】done 记录，淘汰时统一还原 + 作废。
    by_title = {}
    for tr in done_transfers:
        by_title.setdefault((tr["cat"], tr["title"]), []).append(tr)

    # 建立目录/路径到 transfer 记录的映射
    candidates = []
    for item in items:
        matched_group = []
        for (cat, title), trs in by_title.items():
            rp = trs[0]["remote_path"] or ""
            if item["name"] in rp or rp.startswith(item["remote_path"]):
                matched_group = trs
                break

        matched_tr = matched_group[0] if matched_group else None
        cat = matched_tr["cat"] if matched_tr else ""
        title = matched_tr["title"] if matched_tr else item["name"]

        # 检查是否受白名单保护（除用户收藏外，其他均不设永久保护）
        is_protected = False
        if cat in PROTECTED_CATEGORIES:
            is_protected = True
        elif title in protected_titles:
            is_protected = True

        if is_protected:
            if verbose:
                print(f"  [白名单保护] {title}（用户已收藏，豁免淘汰）", flush=True)
            continue

        # 查询该片在 plays 表中的最后播放时间和总播放次数
        cur.execute(
            "SELECT COUNT(*) as cnt, MAX(played_at) as last_play FROM plays WHERE title=?",
            (title,))
        p_row = cur.fetchone()
        play_cnt = p_row["cnt"] if p_row else 0
        last_played = p_row["last_play"] if p_row and p_row["last_play"] else 0

        candidates.append({
            "item": item,
            "matched_tr": matched_tr,
            "matched_group": matched_group,
            "title": title,
            "cat": cat,
            "play_cnt": play_cnt,
            "last_played": last_played,
            "size": item["size"]
        })

    # 排序规则：从未播放 (play_cnt == 0) 排在最前，其次按最后播放时间由旧到新 (last_played ASC)
    candidates.sort(key=lambda x: (x["play_cnt"] > 0, x["last_played"], -x["size"]))

    evicted_cnt = 0
    freed_bytes = 0

    for cand in candidates:
        if total_bytes <= target_bytes:
            break

        item = cand["item"]
        remote = item["remote_path"]
        sz = item["size"]
        title = cand["title"]

        if dry_run:
            print(f"  [配额预演] 将淘汰移入回收站: {remote} ({title}, 释放 {sz / gb:.2f} GB)", flush=True)
            total_bytes -= sz
            freed_bytes += sz
            evicted_cnt += 1
            continue

        removed = True
        try:
            ol.remove(remote)
            print(f"  [已移入回收站] {remote} ({title}, 释放 {sz / gb:.2f} GB)", flush=True)
        except OpenListError as e:
            removed = False
            print(f"  [删除异常] {remote}: {e}（仍将还原 strm 状态）", flush=True)

        # 还原该影片【全部集数】的 strm 为 pending，并作废全部 done 记录。
        # 只处理 matched_tr 单条会留下大量 done 记录 + 死链 strm（见上面注释）。
        matched_group = cand.get("matched_group") or ([cand["matched_tr"]] if cand["matched_tr"] else [])
        if matched_group and config.RESTORE_STRM_AFTER_CLEAN:
            for tr in matched_group:
                _restore_all(tr, verbose=verbose)
            try:
                ids = [tr["id"] for tr in matched_group]
                con.execute(
                    f"UPDATE transfers SET status='cleaned', remote_path=NULL, updated_at=? "
                    f"WHERE id IN ({','.join('?' * len(ids))})",
                    [db.now(), *ids])
                con.commit()
                if verbose:
                    print(f"  [状态同步] {title}: {len(ids)} 条 done 记录已作废", flush=True)
            except Exception as e:
                print(f"  [警告] 作废记录失败 {title}: {e}", flush=True)

        total_bytes -= sz
        freed_bytes += sz
        evicted_cnt += 1

    print(f"[quota] 配额淘汰完成！共清理 {evicted_cnt} 部冷门影片，释放 {freed_bytes / gb:.2f} GB，当前点播网盘占用: {total_bytes / gb:.2f} GB",
          flush=True)
    return evicted_cnt, freed_bytes, total_bytes


def run_cleanup(dry_run=False, verbose=True):
    """执行到期的定期清理（7天到期清理）。返回 (成功数, 失败数, 跳过数)。"""
    ol = OpenListClient()
    protected_titles = get_protected_titles()
    rows = db.due_cleanups(limit=100)
    ok = fail = skip = 0

    for row in rows:
        qid = row["id"]
        tid = row["transfer_id"]
        remote = row["remote_path"]
        tr = db.get_transfer_by_id(tid)
        if not tr:
            skip += 1
            continue
        # 用户已收藏的影片永久保护，跳过到期清理
        if tr["title"] in protected_titles:
            if verbose:
                print(f"  [收藏保护] {tr['title']} 用户已收藏，跳过到期清理", flush=True)
            skip += 1
            continue
        # 有播放记录且未看完的，延后清理（保护正在追的剧）
        last = db.last_play(tr["cat"], tr["title"], tr["season"], tr["episode"])
        if last and not last["finished"]:
            if verbose:
                print(f"  [延后] {tr['title']} S{tr['season']}E{tr['episode']} 仍在观看", flush=True)
            skip += 1
            continue

        if dry_run:
            print(f"  [预演] 将删除 {remote} ({tr['title']})", flush=True)
            ok += 1
            continue

        removed = True
        try:
            ol.remove(remote)
        except OpenListError as e:
            removed = False
            if verbose:
                print(f"  [删除失败] {remote}: {e}（仍将还原 strm）", flush=True)

        if config.RESTORE_STRM_AFTER_CLEAN:
            restored = _restore_all(tr, verbose=verbose)
            if not restored and verbose:
                print(f"  [警告] 未找到可还原的 strm: {tr['title']}", flush=True)

        db.mark_cleaned(qid, tid)
        if removed:
            ok += 1
            if verbose:
                print(f"  [已清理] {tr['title']} S{tr['season']}E{tr['episode']} -> {remote}",
                      flush=True)
        else:
            ok += 1

    return ok, fail, skip


def _restore_all(tr, verbose=True):
    """还原该影片【所有】专区下的 strm 为 pending。"""
    cat, title = tr["cat"], tr["title"]
    season, episode = tr["season"], tr["episode"]
    paths = set()

    # 1) strm_map 精确
    p = db.get_strm_path(cat, title, season, episode)
    if p and os.path.isfile(p):
        paths.add(p)
    # 2) strm_map 仅按 title
    for q in strm_manager._all_strm_paths_by_title(title, season, episode):
        paths.add(q)
    # 3) 路径推导 + 模糊查找
    for q in (strm_manager.strm_path_for(cat, title, season, episode),
              strm_manager._fuzzy_find(cat, title, season, episode)):
        if q and os.path.isfile(q):
            paths.add(q)

    n = 0
    for q in paths:
        try:
            if strm_manager.restore_pending(q, cat, title, season, episode):
                n += 1
                if verbose:
                    print(f"  [已还原占位] {q}", flush=True)
        except Exception as e:
            if verbose:
                print(f"  [警告] 还原失败 {q}: {e}", flush=True)
    return n


def clean_mismatched_short_dramas(dry_run=False, verbose=True):
    """自动巡检并清除误匹配的短剧/多集剧集大包（防 2TB 网盘配额被垃圾短剧占满）。

    针对电影（season=0, episode=0）记录，若转存路径包含明显短剧命名（如（xx集）、短剧），
    或其实际目录下散落多个带集数的小视频文件，触发自动清理并作废记录，还原 strm 为 pending。
    """
    import re
    from .transfer import QuarkTransfer
    ol = OpenListClient()
    tr_engine = QuarkTransfer(ol)
    con = db._conn()
    cur = con.cursor()

    cur.execute("SELECT id, cat, title, season, episode, remote_path FROM transfers WHERE season=0 AND episode=0 AND status='done' AND remote_path IS NOT NULL")
    done_movies = cur.fetchall()

    cleaned_count = 0
    series_pats = [r"[（\(]?\s*\d+\s*集\s*[）\)]?", r"短剧", r"全\s*\d+\s*集"]

    for row in done_movies:
        rpath = row["remote_path"] or ""
        # 1. 检查路径是否包含明显短剧特征
        is_bad = any(re.search(p, rpath) for p in series_pats)

        # 2. 如果路径存在，检查是否为目录且内部含有多集小视频
        target_dir = rpath.rsplit("/", 1)[0] if rpath.endswith((".mp4", ".mkv", ".ts")) else rpath
        if not is_bad and target_dir and target_dir != config.QUARK_VOD_ROOT:
            try:
                sub_entries = tr_engine._quark_list(target_dir)
                v_count = sum(1 for it in sub_entries if not it["is_dir"] and any(it["name"].lower().endswith(ext) for ext in (".mp4", ".mkv", ".ts")))
                if v_count >= 5:  # 电影目录下居然有 5 个以上独立视频文件，高度疑似短剧大包
                    is_bad = True
            except Exception:
                pass

        if is_bad:
            print(f"  [发现错配短剧脏数据] {row['title']} -> {rpath}", flush=True)
            if dry_run:
                cleaned_count += 1
                continue
            # 执行删除与清理
            try:
                write_dir = config.write_path(target_dir)
                tr_engine.delete_remote(write_dir)
                print(f"  [已删除短剧大包] {write_dir}", flush=True)
            except Exception as e:
                print(f"  [删除短剧失败] {e}", flush=True)

            cur.execute("UPDATE transfers SET status='cleaned', remote_path=NULL, error='[自动巡检] 清除错配短剧大包', updated_at=? WHERE id=?",
                        (db.now(), row["id"]))
            con.commit()
            _restore_all(dict(row), verbose=verbose)
            cleaned_count += 1

    if cleaned_count > 0:
        print(f"[cleanup] 错配短剧与异常大包治理完成，共清理 {cleaned_count} 个错配短剧目录", flush=True)
    return cleaned_count


def prune_jellyfin2_orphan_metadata(dry_run=False, verbose=True):
    """清理 jellyfin2/config/metadata/library 下未被 BaseItemImageInfos 引用的历史孤儿图片。

    背景（2026-10-31 专项治理）：
    backfill_episode_thumbs 将单集剧照迁入 /cinema2/*-thumb.jpg 后，Jellyfin2 更新了
    BaseItemImageInfos 指向本地剧照，但未删除 /config/metadata/library/<xx>/<guid>/poster.jpg
    下的旧副本，导致堆积 18,975 个僵尸图片文件（5.33 GB）。
    """
    from contextlib import closing
    lib_dir = os.path.join(config.BASE_DIR, "jellyfin2/config/metadata/library")
    if not os.path.exists(JF_DB) or not os.path.isdir(lib_dir):
        return 0, 0

    try:
        with closing(sqlite3.connect(f"file:{JF_DB}?mode=ro&immutable=1", uri=True)) as con:
            cur = con.cursor()
            referenced = set()
            for (p,) in cur.execute(
                "SELECT Path FROM BaseItemImageInfos WHERE Path LIKE '%/metadata/library/%'"
            ).fetchall():
                if p:
                    referenced.add(
                        p.replace("/config/", os.path.join(config.BASE_DIR, "jellyfin2/config/"))
                    )
    except Exception as e:
        print(f"[cleanup] 读取 BaseItemImageInfos 失败，跳过孤儿元数据清理: {e}", flush=True)
        return 0, 0

    # 安全阈值保护：若数据库读取异常导致 referenced 过少，拒绝清理
    if len(referenced) < 100:
        return 0, 0

    removed_files = 0
    removed_bytes = 0
    removed_dirs = 0

    for root, dirs, files in os.walk(lib_dir, topdown=False):
        for fn in files:
            fp = os.path.join(root, fn)
            if fp not in referenced:
                try:
                    sz = os.path.getsize(fp)
                    if not dry_run:
                        os.remove(fp)
                    removed_files += 1
                    removed_bytes += sz
                except OSError:
                    pass
        # 若子目录已空则顺带移除空目录
        if root != lib_dir and not dry_run:
            try:
                if not os.listdir(root):
                    os.rmdir(root)
                    removed_dirs += 1
            except OSError:
                pass

    if verbose and (removed_files > 0 or dry_run):
        mb = round(removed_bytes / (1024 * 1024), 2)
        print(
            f"[cleanup] Jellyfin2 孤儿元数据清理: {'(预演) ' if dry_run else ''}"
            f"移除文件 {removed_files} 个 ({mb} MB), 清理空目录 {removed_dirs} 个",
            flush=True,
        )
    return removed_files, removed_bytes


def main():
    import sys
    import fcntl
    dry = "--dry-run" in sys.argv
    enforce_quota_only = "--enforce-quota" in sys.argv

    lock_path = os.path.join(config.DATA_DIR, "vod_maintenance.lock")
    os.makedirs(config.DATA_DIR, exist_ok=True)
    lock_fp = open(lock_path, "a+")
    try:
        if not dry:
            fcntl.flock(lock_fp, fcntl.LOCK_EX)
    except Exception:
        pass

    try:
        print(f"[cleanup] 开始 {'预演' if dry else '执行'} @ {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
        ol = OpenListClient()

        # 1. 自动巡检并清理误匹配的短剧大包（防止占用网盘配额）
        clean_mismatched_short_dramas(dry_run=dry, verbose=True)

        # 2. 执行 2TB 容量配额检测与 LRU 智能淘汰
        enforce_vod_quota(ol=ol, dry_run=dry, verbose=True)

        # 3. 清理 Jellyfin2 metadata/library 下的孤儿图片缓存
        prune_jellyfin2_orphan_metadata(dry_run=dry, verbose=True)

        # 4. 如果非仅配额检查，顺带执行过期队列表的清理
        if not enforce_quota_only:
            ok, fail, skip = run_cleanup(dry_run=dry)
            print(f"[cleanup] 到期清理完成: 成功 {ok} 失败 {fail} 跳过 {skip}", flush=True)
    finally:
        try:
            fcntl.flock(lock_fp, fcntl.LOCK_UN)
            lock_fp.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()

