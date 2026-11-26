#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only share/file checks with persistent per-episode source hints.

Never saves cloud media. File names provide resolution hints, not decoded
resolution or a guarantee of future playback. Inconclusive probes retain locks.
"""

import hashlib
import json
import os
import re
import tempfile
import time

from . import config
from . import db
from . import search as search_mod
from . import strm_manager
from .transfer import QuarkTransfer, TransferFailed
from .openlist_client import OpenListClient

# Compatibility function: a name hint, never inferred from size/HDR/REMUX.
from .quality import quality_hint
FOUR_K_REGEX = re.compile(r"(?i)2160p|4k|uhd")


def is_4k_candidate(filename, size_bytes=0, is_series=False):
    return quality_hint(filename)['level'] == '4k'


def _confirmed_dead(message):
    # Expired login/stoken/cookie is not an expired public share.
    text = str(message).lower()
    if any(word in text for word in ('cookie', 'token', '登录', '身份', '鉴权', '权限')):
        return False
    return ('分享链接内没有文件' in text or
            any(code in text for code in ('41011', '41012')) or
            (any(word in text for word in ('分享', '链接')) and
             any(word in text for word in ('已取消', '已失效', '已过期'))))


def _load_cursor(filename, with_share=False):
    try:
        with open(os.path.join(config.DATA_DIR, filename), encoding='utf-8') as stream:
            value = json.loads(stream.read(65536)).get('after')
        if (isinstance(value, list) and len(value) == (4 if with_share else 3) and
                all(isinstance(x, str) for x in value[:2]) and type(value[2]) is int and
                (not with_share or isinstance(value[3], str) and re.fullmatch(r'[0-9a-f]{64}', value[3]))):
            return tuple(value)
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _save_cursor(filename, position):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=config.DATA_DIR,
                                         prefix='.'+filename[:-5].replace('_', '-')+'-', delete=False) as stream:
            temporary = stream.name
            os.fchmod(stream.fileno(), 0o644)
            json.dump({'version': 1, 'after': list(position)}, stream, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, os.path.join(config.DATA_DIR, filename))
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _audit_cursor():
    return _load_cursor('source_audit_cursor.json')


def _advance_audit_cursor(cat, title, season):
    _save_cursor('source_audit_cursor.json', (cat, title, season))


def _locked_share_key(row):
    # Persist stable ordering without exposing a public-share URL in a 0644 file.
    digest = hashlib.sha256(row['best_share_url'].encode('utf-8')).hexdigest()
    return row['cat'], row['title'], row['season'], digest


class VodAuditor:
    def __init__(self, transfer=None):
        self.ol = transfer.ol if transfer is not None else OpenListClient()
        self.tr = transfer or QuarkTransfer(self.ol)

    def _audit_targets(self, cat, title, season, episodes, force):
        """Use the same recursive file/identity selection as actual transfer."""
        targets = sorted(set(episodes))[:500]
        cached = {ep: db.get_source_audit(cat, title, season, ep) for ep in targets}
        invalid_status = {'dead', 'not_found', 'invalid', 'expired'}
        if not force and targets and all(row and row['check_status'] == 'no_source'
                and db.now() - row['checked_at'] < 86400 for row in cached.values()):
            return False, False, '', 'cached scoped no_source'
        if not force and all(row and row['has_quark_source'] and row['best_share_url']
                and str(row['check_status'] or '').lower() not in invalid_status
                and db.now()-row['checked_at'] < 86400 and not db.is_dead_source(row['best_share_url'])
                for row in cached.values()):
            return True, all(bool(row['is_4k']) for row in cached.values()), '', 'cached hints'
        search_title, aliases = strm_manager.library_search_identity(cat, title, season)
        best = {}
        missing = set()
        attempted = set()
        search_complete = False
        inspection_errors = []

        def inspect_source(url, label):
            if url in attempted or db.is_dead_source(url):
                return
            attempted.add(url)
            try:
                token, pwd_id, roots = self.tr.resolve_share(url)
                files = self.tr.share_files(token, pwd_id, roots)
                for ep in targets:
                    video = self.tr.select_share_video(files, search_title, season, ep, aliases=aliases)
                    if not video:
                        row = cached[ep]
                        if row and row['best_share_url'] == url:
                            missing.add(ep)
                        continue
                    hint = quality_hint(video['name'], label)
                    is4k = hint['level'] == '4k'
                    if ep not in best or (is4k and not best[ep][0]):
                        best[ep] = (is4k, url, label)
            except Exception:
                # A timeout or incomplete traversal does not prove a dead source.
                inspection_errors.append(True)
                return

        # Existing file evidence is independent of search coverage. Recheck up
        # to two locked shares, even when the current search cannot find them.
        locked = {}
        for row in cached.values():
            if (row and row['has_quark_source'] and row['best_share_url'] and
                    str(row['check_status'] or '').lower() not in invalid_status):
                locked.setdefault(row['best_share_url'], row['best_share_title'] or title)
        for url, label in list(locked.items())[:2]:
            inspect_source(url, label)
        if not all(ep in best and best[ep][0] for ep in targets):
            try:
                report = search_mod.search_report(search_title, season=season,
                    episode=targets[0] if targets else 0, aliases=aliases)
                search_complete = bool(report.get('configured')) and not report.get('partial') and not report.get('configuration_error')
                candidates = search_mod.rank_candidates(
                    report['results'], search_title, season, targets[0] if targets else 0, cat, aliases=aliases)
                if len(candidates) > 6:
                    search_complete = False
            except Exception:
                candidates = []
            for candidate in candidates[:6]:
                inspect_source(candidate.share_url, candidate.name)
                if all(ep in best and best[ep][0] for ep in targets):
                    break
        if search_complete and not inspection_errors:
            for ep in set(targets) - best.keys():
                # Complete configured search plus successful enumerations is scoped absence evidence.
                db.save_source_audit(cat, title, season, ep, has_quark_source=0, is_4k=0,
                                     check_status='no_source')
        # Complete enumeration can disprove this episode without disproving the
        # public share or changing evidence for episodes outside this request.
        for ep in missing - best.keys():
            db.save_source_audit(cat, title, season, ep, has_quark_source=0, is_4k=0,
                                 check_status='not_found')
        for ep, (is4k, url, label) in best.items():
            db.save_source_audit(cat, title, season, ep, has_quark_source=1, is_4k=int(is4k),
                best_share_url=url, best_share_title=label,
                check_status='valid_4k' if is4k else 'valid_hd')
        # A season is usable only when every requested episode has an exact file.
        # Partial enumeration must not be reported as a complete HD/4K source.
        complete = bool(targets) and len(best) == len(targets)
        status = f'checked files {len(best)}/{len(targets)}' if best else 'unconfirmed'
        return complete, complete and all(x[0] for x in best.values()), \
            next(iter(best.values()))[1] if best else '', status

    def audit_tv_season(self, cat, title, season=1, existing_episodes=None, force=False):
        return self._audit_targets(cat, title, season, existing_episodes or [1], force)

    def audit_single_item(self, cat, title, season=0, episode=0, force=False):
        if season:
            return self.audit_tv_season(cat, title, season, [episode], force)
        done = db.find_done_transfer(cat, title, 0, 0)
        if done and done['remote_path']:
            is4k = quality_hint(os.path.basename(done['remote_path']))['level'] == '4k'
            return True, is4k, '', 'saved; resolution hint only'
        return self._audit_targets(cat, title, 0, [0], force)

    def check_and_heal_locked_shares(self, limit=None, yield_fn=None, timeout_fn=None):
        """Rotate root checks separately from per-episode file evidence.

        Callers bound each phase by count/time. Unknown results retain locks;
        only confirmed dead shares trigger replacement of their original targets.
        """
        con = db._conn()
        rows = con.execute("""
            SELECT cat, title, season, best_share_url,
                   GROUP_CONCAT(episode) AS episodes
            FROM source_audits
            WHERE has_quark_source = 1 AND best_share_url != ''
            GROUP BY cat, title, season, best_share_url
        """).fetchall()
        available = len(rows)
        rows.sort(key=_locked_share_key)
        cursor = _load_cursor('source_lock_cursor.json', with_share=True)
        if cursor:
            split = next((i for i, row in enumerate(rows) if _locked_share_key(row) > cursor), len(rows))
            rows = rows[split:] + rows[:split]
        if limit is not None:
            rows = rows[:max(0, limit)]

        print(f"[{config.SERVICE_NAME}] 本轮检查 {len(rows)}/{available} 组已锁源的分享根目录；不刷新分集文件证据。")
        stats = {"available": available, "total": len(rows), "checked": 0, "healthy": 0, "unknown": 0,
                 "healed_4k": 0, "healed_hd": 0, "failed_dead": 0, "errors": 0}

        for r in rows:
            if timeout_fn and timeout_fn():
                print(f"[{config.SERVICE_NAME}] 达到时间窗口上限，自愈巡检优雅收工。")
                break
            if yield_fn:
                yield_fn()
            if timeout_fn and timeout_fn():
                break

            stats["checked"] += 1
            try:
                outcome = self._check_locked_share(r)
                stats[outcome] += 1
            except Exception as exc:
                stats['unknown'] += 1
                stats['errors'] += 1
                print(f" - [检查异常] {r['cat']}/{r['title']} -> {type(exc).__name__}")
            finally:
                # Even inconclusive/error results yield their turn. A budget
                # stop before a request must not skip that unstarted group.
                _save_cursor('source_lock_cursor.json', _locked_share_key(r))
            time.sleep(1.0)

        print(f"\n[{config.SERVICE_NAME}] 存量锁源检查结束: 实查={stats['checked']}/{stats['total']}, 分享可读={stats['healthy']}, 未确认={stats['unknown']}, 换源4K标注={stats['healed_4k']}, 其他标注={stats['healed_hd']}, 待补源={stats['failed_dead']}")
        return stats

    def _check_locked_share(self, row):
        cat, title, season = row['cat'], row['title'], row['season']
        share_url = row['best_share_url']
        tag = f'S{season:02d}' if season else '电影'
        confirmed_dead = False
        unknown_reason = ''
        try:
            _, _, listed = self.tr.resolve_share(share_url)
            if listed:
                print(f" - [分享可读] 【{cat}】《{title}》({tag})，分集文件证据由片源审计更新")
                return 'healthy'
            confirmed_dead = True
        except TransferFailed as exc:
            confirmed_dead = _confirmed_dead(exc)
            unknown_reason = type(exc).__name__
        except Exception as exc:
            unknown_reason = type(exc).__name__
        if not confirmed_dead:
            print(f" - [待确认] 【{cat}】《{title}》({tag}) 探测未能判定源状态: {unknown_reason}，保留原锁源")
            return 'unknown'

        db.purge_audit_locked_share(cat, title, season, share_url)
        if not db.is_dead_source(share_url):
            db.mark_dead_source(share_url, 'panshare', '分享链接已失效或取消')
        print(f" - [死链检测] 【{cat}】《{title}》({tag}) 已确认失效，检查原目标的替代源。")
        if season > 0:
            episodes = sorted({int(x) for x in str(row['episodes'] or '').split(',') if x.isdigit()})
            ok, is_4k, new_url, status = self.audit_tv_season(
                cat, title, season=season, existing_episodes=episodes or [1], force=True)
        else:
            ok, is_4k, new_url, status = self.audit_single_item(cat, title, 0, 0, force=True)
        if ok and new_url:
            print(f"   [自动换源] 已确认原目标的替代文件 ({status})")
            return 'healed_4k' if is_4k else 'healed_hd'
        print('   [待补源] 本次搜索范围未确认覆盖原目标的替代源。')
        return 'failed_dead'

    def audit_entire_library(self, only_pending=True, limit=None, yield_fn=None, timeout_fn=None):
        """遍历全库执行片源可用性与 4K 质量审计（剧集以整季为单位大包审计）。支持观影感知避让与时间窗口截止。"""
        cinema2 = config.CINEMA2_DIR
        if not os.path.isdir(cinema2):
            return {"total_shows": 0, "checked": 0, "valid_4k": 0, "valid_hd": 0, "no_source": 0}

        targets = []
        for cat in sorted(os.listdir(cinema2)):
            cat_dir = os.path.join(cinema2, cat)
            if not os.path.isdir(cat_dir) or cat.startswith("."):
                continue

            for item_dir in sorted(os.listdir(cat_dir)):
                ipath = os.path.join(cat_dir, item_dir)
                if not os.path.isdir(ipath) or item_dir.startswith("."):
                    continue

                # The library layout, not a fixed category list, distinguishes
                # episodic works. This also covers new and user-defined categories.
                season_dirs = [sub for sub in sorted(os.listdir(ipath))
                               if sub.startswith("Season ")
                               and os.path.isdir(os.path.join(ipath, sub))]
                if season_dirs:
                    for sub in season_dirs:
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

        targets.sort(key=lambda row: (row[1], row[2], row[3]))
        cursor = _audit_cursor()
        if cursor:
            split = next((i for i, row in enumerate(targets) if (row[1], row[2], row[3]) > cursor), len(targets))
            targets = targets[split:] + targets[:split]
        if limit is not None:
            targets = targets[:max(0, limit)]

        stats = {"total_shows": len(targets), "checked": 0, "valid_4k": 0, "valid_hd": 0, "no_source": 0, "errors": 0}
        print(f"[{config.SERVICE_NAME}] 本轮核验 {len(targets)} 组作品/季的目标文件与 4K 名称提示；不转存媒体。")

        for kind, cat, title, s_num, eps in targets:
            if timeout_fn and timeout_fn():
                print(f"[{config.SERVICE_NAME}] 达到时间窗口上限，片源审计优雅收工。")
                break
            if yield_fn:
                yield_fn()
            if timeout_fn and timeout_fn():
                break

            stats['checked'] += 1
            try:
                if kind == "tv":
                    has_src, is_4k, best_url, status_msg = self.audit_tv_season(cat, title, season=s_num, existing_episodes=eps)
                    print(f" - [{cat:<6}] 《{title}》 第{s_num}季 ({len(eps)}集) -> {status_msg}")
                else:
                    has_src, is_4k, best_url, status_str = self.audit_single_item(cat, title, 0, 0)
                    src_desc = "4K名称标注" if is_4k else ("其他分辨率标注" if has_src else "未确认有效源")
                    print(f" - [{cat:<6}] 《{title}》 (电影) -> {status_str} | {src_desc}")

                if has_src and is_4k:
                    stats["valid_4k"] += 1
                elif has_src:
                    stats["valid_hd"] += 1
                else:
                    stats["no_source"] += 1
            except Exception as err:
                stats['no_source'] += 1
                stats['errors'] += 1
                print(f" - [ERROR   ] {cat}/{title} -> {type(err).__name__}")
            finally:
                # Failed/empty targets also yield their turn; budget expiry
                # before a request leaves that target eligible for next run.
                _advance_audit_cursor(cat, title, s_num)

            time.sleep(1.2) # 轻量防频控

        print(f"\n[{config.SERVICE_NAME}] 审计结束: 实查={stats['checked']}/{stats['total_shows']}, 全目标4K标注={stats['valid_4k']}, 其他完整覆盖={stats['valid_hd']}, 未确认完整覆盖={stats['no_source']}")
        return stats

    def fix_disguised_vod_files(self):
        """Inspection-only inventory. Renaming live cloud files invalidates STRMs.

        Disguised input handling belongs to the selected-file transfer pipeline;
        scheduled broad scans must not rename existing media behind Jellyfin.
        """
        rows = db._conn().execute("SELECT COUNT(*) FROM transfers WHERE status='done'").fetchone()
        return rows[0]
