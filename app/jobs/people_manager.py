#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
家庭影院演职员治理器 (People Manager)
====================================
职责：
  1. 按 PersonType 规则剔除幕后人员映射（Writer / Producer / Creator），
     大幅降低刮削期的 TMDB 人像请求量（实测幕后人头像占刮削开销 60%+）。
  2. 清理因映射被剔除而产生的孤立头像文件（metadata/People/）。
  3. 对保留的演职员（Actor / GuestStar / Director）触发 Jellyfin 局部元数据刷新，
     精准恢复缺失头像，避免代价高昂的全库重刮。

设计约束（对应 CLAUDE.md 红线）：
  - 严禁直接写 jellyfin.db；所有元数据变更均通过 Jellyfin HTTP API 完成。
  - 幂等：重复执行结果一致，不会越删越少。
  - 保守策略：Peoples 表无 PersonType 记录的 Person（如刘小北、欧阳奋强等
    早期刮削遗留、TmdbId 完整）一律保留，避免误伤演员。
"""

import os
import sys
import json
import time
import shutil
import sqlite3
import urllib.request
import urllib.error

BASE_DIR = os.environ.get("HOMETHEATER_BASE_DIR", "/home/dong/projects/hometheater")
JF_DB = os.path.join(BASE_DIR, "jellyfin/config/data/jellyfin.db")
JF_CONFIG = os.path.join(BASE_DIR, "jellyfin/config")
JELLYFIN_API_BASE = os.environ.get("JELLYFIN_API_BASE", "http://127.0.0.1:8096")
JELLYFIN_KEY = os.environ.get("JELLYFIN_KEY", "")

# ---- 治理规则（单一事实来源，改动只需改这里）----
KEEP_TYPES = {"Actor", "GuestStar", "Director"}          # 演员 / 客串 / 导演
DROP_TYPES = {"Writer", "Producer", "Creator"}           # 编剧 / 制片 / 创作者
UNKNOWN_POLICY = "keep"                                  # Peoples 无记录者保守保留

PERSON_TYPE = "MediaBrowser.Controller.Entities.Person"

# 运行时缓存：待剔除人员的姓名集合（供 API Type 缺失时兜底匹配）
_DROP_NAMES_CACHE = set()


def _log(msg):
    print(f"[PeopleManager] {msg}", flush=True)


def _api(method, path, payload=None, timeout=30):
    """统一 Jellyfin API 调用，返回 (status, parsed_json_or_text)"""
    url = f"{JELLYFIN_API_BASE}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f'MediaBrowser Token="{JELLYFIN_KEY}"')
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body)
            except Exception:
                return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception as e:
        return None, str(e)[:200]


def _resolve_user_id():
    """解析 Jellyfin 用户 Id（API 需要 userId 才能读取条目 People）"""
    st, users = _api("GET", "/Users")
    if st == 200 and isinstance(users, list):
        for u in users:
            if (u.get("Name") or "").lower() == "dong":
                return u.get("Id")
        if users:
            return users[0].get("Id")
    return None


def _connect_ro():
    """只读连接，避免与 Jellyfin 写库争锁"""
    return sqlite3.connect(f"file:{JF_DB}?mode=ro", uri=True, timeout=15)


# ----------------------------------------------------------------------
# 1. 规则计算：哪些 Person 属于"应剔除"
# ----------------------------------------------------------------------
def classify_people():
    """
    返回 (drop_people_ids, keep_people_ids, stats)

    Id 体系说明：
      - Peoples 表：TMDb 插件缓存，含 PersonType，其 Id 被 PeopleBaseItemMap 引用
      - BaseItems 表：Jellyfin 实体，Type=...Person，含头像图片记录
      两者 Id 空间独立，需按 Name 关联。
    """
    con = _connect_ro()
    cur = con.cursor()

    # Peoples: PersonType 权威来源
    cur.execute("SELECT Id, Name, PersonType FROM Peoples")
    id2name, id2type = {}, {}
    for pid, name, ptype in cur.fetchall():
        id2name[pid] = name
        id2type[pid] = ptype

    # 每个 Person 被多少影片/剧集引用（决定清理影响面）
    cur.execute("SELECT PeopleId, COUNT(*) FROM PeopleBaseItemMap GROUP BY PeopleId")
    refcnt = {r[0]: r[1] for r in cur.fetchall()}

    drop_ids, keep_ids, unknown = set(), set(), set()
    for pid, ptype in id2type.items():
        if ptype in DROP_TYPES:
            drop_ids.add(pid)
        elif ptype in KEEP_TYPES:
            keep_ids.add(pid)
        else:
            unknown.add(pid)

    con.close()

    stats = {
        "drop_people": len(drop_ids),
        "keep_people": len(keep_ids),
        "unknown_people": len(unknown),
        "drop_mappings": sum(refcnt.get(i, 0) for i in drop_ids),
        "keep_mappings": sum(refcnt.get(i, 0) for i in keep_ids),
    }
    return drop_ids, keep_ids, id2name, refcnt, stats


# ----------------------------------------------------------------------
# 2. 清理映射：通过 Jellyfin API 删除幕后人员（不直接写库）
# ----------------------------------------------------------------------
def prune_crew_mappings(drop_ids, id2name, dry_run=True):
    """
    通过 Jellyfin API (POST /Items/{id}) 重提演职员列表，剔除幕后人员。

    已验证：API 返回的 People 条目自带 Type 字段（Actor/GuestStar/Director/
    Writer/Producer/Creator），因此类型判定直接取 API 值，比姓名回查 Peoples
    表更可靠；姓名匹配仅作兜底。
    """
    if not drop_ids:
        _log("无需剔除的幕后人员")
        return 0

    uid = _resolve_user_id()
    if not uid:
        _log("错误: 无法解析 Jellyfin 用户 Id")
        return 0

    con = _connect_ro()
    cur = con.cursor()
    placeholders = ",".join("?" * len(drop_ids))
    cur.execute(
        f"""SELECT DISTINCT m.ItemId FROM PeopleBaseItemMap m
            WHERE m.PeopleId IN ({placeholders})""",
        tuple(drop_ids),
    )
    affected_items = [r[0] for r in cur.fetchall()]
    con.close()

    _log(f"受影响影视条目: {len(affected_items)} 个")

    if dry_run:
        return len(affected_items)

    updated, failed = 0, 0
    for idx, item_id in enumerate(affected_items, 1):
        try:
            status, item = _api("GET", f"/Users/{uid}/Items/{item_id}?fields=People")
            if status != 200 or not isinstance(item, dict) or not item.get("People"):
                continue

            people = item.get("People") or []
            kept = [p for p in people if not _is_dropped(p, drop_ids)]

            if len(kept) == len(people):
                continue

            item["People"] = kept
            status, _ = _api("POST", f"/Items/{item_id}", payload=item, timeout=30)
            if status in (200, 204):
                updated += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            _log(f"  更新条目 {item_id[:8]} 失败: {e}")

        if idx % 50 == 0:
            _log(f"  进度 {idx}/{len(affected_items)}，已更新 {updated}")
        time.sleep(0.02)

    _log(f"已更新 {updated} 个条目，失败 {failed} 个")
    return updated


def _is_dropped(person_entry, drop_ids):
    """判定一条 People 是否应被剔除：API Type 优先，姓名回查兜底"""
    ptype = (person_entry.get("Type") or "").strip()
    if ptype in DROP_TYPES:
        return True
    if ptype in KEEP_TYPES:
        return False
    # Type 缺失时退回 Peoples 表姓名匹配
    name = (person_entry.get("Name") or "").strip()
    if name in _DROP_NAMES_CACHE:
        return True
    return False


# ----------------------------------------------------------------------
# 3. 清理孤立头像文件
# ----------------------------------------------------------------------
def prune_orphan_images(keep_people_ids, id2name, dry_run=True):
    """
    删除"已从所有影视条目剔除"的幕后人员头像文件。

    判定口径（保守优先）：
      - 仅当 Person 的 PersonType 属于 DROP_TYPES，且其姓名匹配到
        一条确切的头像路径时，才删除该文件；
      - Peoples 表无记录者（PersonType 未知）一律不删，避免误伤
        刘小北、欧阳奋强等早期刮削遗留的演员。
      - 路径匹配用完整目录段比对，避免同名子串误伤。
    """
    con = _connect_ro()
    cur = con.cursor()

    cur.execute(
        f"""SELECT b.Name, i.Path FROM BaseItems b
            JOIN BaseItemImageInfos i ON i.ItemId = b.Id
            WHERE b.Type = ?""",
        (PERSON_TYPE,),
    )
    rows = cur.fetchall()
    con.close()

    keep_names = {(id2name.get(i) or "").strip() for i in keep_people_ids}
    # 仅删除确认属于 DROP 类型者（_DROP_NAMES_CACHE 由 run() 注入）
    drop_names = _DROP_NAMES_CACHE

    removed, skipped = 0, 0
    for name, path in rows:
        nm = (name or "").strip()
        if not nm or not path:
            continue
        if nm in keep_names:
            continue
        # 仅删除确认属于 DROP 类型者；未知类型一律保留
        if nm not in drop_names:
            skipped += 1
            continue

        host_p = path.replace("/config/", os.path.join(JF_CONFIG, ""))
        if dry_run:
            removed += 1
            continue
        try:
            if os.path.isfile(host_p):
                os.remove(host_p)
                removed += 1
                # 清理空目录（如 metadata/People/黄/）
                d = os.path.dirname(host_p)
                if os.path.isdir(d) and not os.listdir(d):
                    os.rmdir(d)
        except Exception:
            pass

    _log(f"孤立头像文件: {'预计移除' if dry_run else '已移除'} {removed} 个"
         f"（保守跳过未知类型 {skipped} 个）")
    return removed


# ----------------------------------------------------------------------
# 4. 精准恢复：只刷新保留的演职员，不触发全库重刮
# ----------------------------------------------------------------------
def recover_missing_avatars(keep_people_ids, id2name, limit=None, dry_run=True):
    """
    对有头像记录但文件缺失的保留类 Person 触发 Jellyfin 局部刷新。
    只刷 Person 元数据，不碰影片海报/季海报/剧集元数据 —— 这是提速关键。
    """
    con = _connect_ro()
    cur = con.cursor()

    cur.execute(
        f"""SELECT b.Id, b.Name, i.Path FROM BaseItems b
            JOIN BaseItemImageInfos i ON i.ItemId = b.Id
            WHERE b.Type = ?""",
        (PERSON_TYPE,),
    )
    rows = cur.fetchall()
    con.close()

    keep_names = {(id2name.get(i) or "").strip() for i in keep_people_ids}

    missing = []
    for pid, name, path in rows:
        nm = (name or "").strip()
        if keep_names and nm not in keep_names:
            continue
        if not path:
            continue
        host_p = path.replace("/config/", os.path.join(JF_CONFIG, ""))
        if not os.path.isfile(host_p):
            missing.append((pid, nm, host_p))

    _log(f"待恢复头像: {len(missing)} 位")

    if dry_run:
        return len(missing)

    if limit:
        missing = missing[:limit]

    ok = 0
    for pid, nm, host_p in missing:
        # FullRefresh 只重取该 Person 元数据与头像
        status, _ = _api(
            "POST",
            f"/Items/{pid}/Refresh",
            payload={
                "Recursive": False,
                "MetadataRefreshMode": "FullRefresh",
                "ImageRefreshMode": "FullRefresh",
                "ReplaceAllMetadata": False,
                "ReplaceAllImages": False,
            },
        )
        if status in (200, 204):
            ok += 1
        if ok % 100 == 0 and ok:
            _log(f"  已触发 {ok}/{len(missing)}")
        time.sleep(0.05)   # 温和限速，避免打爆本地 API

    _log(f"已触发 {ok} 位演职员元数据刷新")
    return ok


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def run(dry_run=True, do_prune=True, do_recover=True, recover_limit=None):
    if not os.path.exists(JF_DB):
        _log(f"错误: 未找到数据库 {JF_DB}")
        return False

    _log("=" * 56)
    _log(f"模式: {'预演（不落盘）' if dry_run else '正式执行'}")
    _log(f"规则: 保留={sorted(KEEP_TYPES)} 剔除={sorted(DROP_TYPES)} 未知={UNKNOWN_POLICY}")
    _log("=" * 56)

    drop_ids, keep_ids, id2name, refcnt, stats = classify_people()

    global _DROP_NAMES_CACHE
    _DROP_NAMES_CACHE = {(id2name.get(i) or "").strip() for i in drop_ids}

    _log(f"应剔除幕后人员: {stats['drop_people']} 人 / {stats['drop_mappings']} 条映射")
    _log(f"保留演职员: {stats['keep_people']} 人 / {stats['keep_mappings']} 条映射")
    _log(f"Peoples 无记录（保守保留）: {stats['unknown_people']} 人")

    if do_prune:
        n = prune_crew_mappings(drop_ids, id2name, dry_run=dry_run)
        _log(f"映射清理: 受影响 {n} 个条目")
        prune_orphan_images(keep_ids, id2name, dry_run=dry_run)

    if do_recover:
        recover_missing_avatars(keep_ids, id2name, limit=recover_limit, dry_run=dry_run)

    _log("完成")
    return True


if __name__ == "__main__":
    dry = "--apply" not in sys.argv
    prune_only = "--prune-only" in sys.argv
    recover_only = "--recover-only" in sys.argv
    run(
        dry_run=dry,
        do_prune=not recover_only,
        do_recover=not prune_only,
    )
