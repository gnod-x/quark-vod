#!/usr/bin/env python3
"""Bounded host automation. Unknown source/protection state never removes media."""

from quark_vod.integrations.hometheater import paths
from quark_vod import config as host_config
import datetime as dt
from contextlib import ExitStack
import json
import os
from pathlib import Path
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from quark_vod import config, db, search, strm_manager
from quark_vod.coordination import maintenance_window, playback_lease, file_lock, Busy
from quark_vod.transfer import QuarkTransfer
from quark_vod.openlist_client import OpenListClient
from quark_vod.identity import Work

BASE = Path(paths.BASE_DIR)


def notify(event, title, message, level='P1'):
    from quark_vod.notifier import notify as send
    return send(event, title, message, level=level)


def aliases_from_detail(detail):
    names = [detail.get('original_title'), detail.get('original_name')]
    alt = detail.get('alternative_titles') or {}
    names.extend(x.get('title') for x in alt.get('titles', alt.get('results', [])) if isinstance(x, dict))
    for row in (detail.get('translations') or {}).get('translations', []):
        if row.get('iso_639_1') in {'en', 'zh', 'ja', 'ko'}:
            data = row.get('data') or {}
            names.extend([data.get('title'), data.get('name')])
    names.extend(s.get('name') for s in detail.get('seasons', [])
                 if s.get('name') and not __import__('re').fullmatch(r'(?i)season\s*\d+|第.+季', s['name']))
    return list(dict.fromkeys(str(n).strip() for n in names if str(n or '').strip()))[:24]


def source_evidence(cat, title, season, episodes, aliases=(), *, require_matrix=False, transfer=None):
    """Verify exact files. No-source is a scoped result, never a global assertion."""
    report = search.search_report(title, aliases=aliases, season=season,
                                  episode=episodes[0] if episodes else 0, use_cache=False)
    matrix = False
    if require_matrix:
        from quark_vod.panshare_matrix import source_snapshot
        snapshot, error = source_snapshot()
        providers = {getattr(n, 'provider', '') for n in snapshot.enabled_nodes}
        matrix = not error and {'html', 'tg_web', 'pansou'}.issubset(providers)
    complete = bool(report.get('configured')) and not report.get('partial') and not report.get('configuration_error')
    if require_matrix and not matrix:
        complete = False
    candidates = search.rank_candidates(report.get('results', []), title, season,
                                        episodes[0] if episodes else 0, cat, aliases=aliases)
    transfer = transfer or QuarkTransfer(OpenListClient())
    found = {}
    errors = 0
    for candidate in candidates[:12]:
        if db.is_dead_source(candidate.share_url):
            continue
        try:
            token, pwd, roots = transfer.resolve_share(candidate.share_url)
            files = transfer.share_files(token, pwd, roots)
            for ep in episodes:
                selected = transfer.select_share_video(files, title, season, ep, aliases=aliases)
                if not selected:
                    continue
                from quark_vod.quality import quality_hint
                quality = quality_hint(selected.get('name', ''), candidate.name)
                is4k = quality.get('level') == '4k'
                old = found.get(ep)
                if old and (old['is_4k'] or not is4k):
                    continue
                found[ep] = {'is_4k': is4k, 'share_url': candidate.share_url,
                             'share_title': candidate.name, 'file': selected}
            if all(ep in found and found[ep]['is_4k'] for ep in episodes):
                break
        except Exception:
            errors += 1
    complete = complete and not errors and len(candidates) <= 12
    for ep, row in found.items():
        db.save_source_audit(cat, title, season, ep, has_quark_source=1, is_4k=int(row['is_4k']),
            best_share_url=row['share_url'], best_share_title=row['share_title'],
            check_status='valid_4k' if row['is_4k'] else 'valid_hd')
    if complete:
        for ep in set(episodes) - found.keys():
            db.save_source_audit(cat, title, season, ep, has_quark_source=0, is_4k=0,
                                 check_status='no_source')
    return {'found': found, 'complete': complete, 'no_source': complete and not found,
            'errors': errors, 'matrix': matrix}


def retry_delay(release_date, retries=0, today=None):
    today = today or dt.date.today()
    try:
        age = (today - dt.date.fromisoformat(str(release_date)[:10])).days
    except ValueError:
        age = 91
    return (1 if age <= 30 else 3 if age <= 90 else 7) * 86400


def stage(item, detail, cat, title, kind, *, status='pending'):
    media_type = 'movie' if kind == 'movie' else 'tv'
    release = item.get('release_date') or item.get('first_air_date') or ''
    year = release[:4]
    return db.upsert_staging_media(item['id'], cat, title, year, media_type,
        popularity=float(item.get('popularity') or 0), release_date=release, status=status,
        metadata={'item': item, 'detail': detail, 'aliases': aliases_from_detail(detail)})


def seed_calendar(item, detail, cat, title, *, series_id=''):
    if detail.get('status') in {'Ended', 'Canceled'}:
        return
    latest = max((int(s.get('season_number') or 0) for s in detail.get('seasons', [])), default=0)
    if not latest:
        return
    upcoming = detail.get('next_episode_to_air') or detail.get('last_episode_to_air') or {}
    date = upcoming.get('air_date') or dt.date.today().isoformat()
    watching = False
    try:
        from quark_vod.integrations.hometheater.daily_update_and_prune import get_protected_titles
        protected = get_protected_titles()
        watching = title in protected or title.rsplit(' (', 1)[0] in protected
    except Exception:
        pass  # Unknown protection never authorizes background transfer.
    db.upsert_airing_calendar(item['id'], cat, title, latest, date, is_user_watching=watching,
        metadata={'item': item, 'detail': detail, 'series_id': series_id})


def gate_media(item, detail, cat, title, kind, targets):
    """Return only proven episode identities, after persisting the incubation row."""
    stage(item, detail, cat, title, kind)
    aliases = aliases_from_detail(detail)
    found = {}
    complete = True
    for season, episodes in targets.items():
        evidence = source_evidence(cat, title, season, episodes, aliases)
        found[season] = set(evidence['found'])
        complete = complete and evidence['complete']
    any_found = any(found.values())
    release = item.get('release_date') or item.get('first_air_date') or ''
    db.update_staging_media(item['id'], 'movie' if kind == 'movie' else 'tv',
        status='pending',
        next_check_at=int(time.time()) + retry_delay(release))
    if kind != 'movie':
        seed_calendar(item, detail, cat, title)
    return found, complete


def _api(path, payload=None, method='GET', timeout=8):
    key = config.get_jellyfin2_key()
    if not key:
        raise Busy('Jellyfin2 credential unavailable')
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(config.JELLYFIN2_API + path, data=data, method=method,
        headers={'Authorization': 'MediaBrowser Token="' + key + '"', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(4 * 1024**2 + 1)
    if len(body) > 4 * 1024**2:
        raise ValueError('oversized Jellyfin response')
    return json.loads(body) if body else {}


def refresh_series(tmdb_id, title):
    query = urllib.parse.urlencode({'Recursive': 'true', 'IncludeItemTypes': 'Series',
        'AnyProviderIdEquals': 'tmdb.' + str(tmdb_id), 'Fields': 'ProviderIds', 'Limit': 2})
    data = _api('/Items?' + query)
    items = data.get('Items') if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) != 1:
        raise Busy('Jellyfin series identity not yet indexed')
    item = items[0]
    if str((item.get('ProviderIds') or {}).get('Tmdb', '')) != str(tmdb_id):
        raise ValueError('mismatched Jellyfin series identity')
    _api('/Items/' + urllib.parse.quote(item['Id'], safe='') + '/Refresh?Recursive=true&MetadataRefreshMode=Default&ImageRefreshMode=Default&ReplaceAllMetadata=false&ReplaceAllImages=false',
         {}, 'POST')
    return item['Id']


def publish_staged(row, *, max_episodes=500, only_season=None):
    from quark_vod.integrations.hometheater import tmdb_populate as tm
    metadata = row.get('metadata') or json.loads(row.get('metadata_json') or '{}')
    if metadata.get('needs_tmdb_refresh'):
        media = row['media_type']
        key = tm.read_tmdb_key()
        detail = tm.tmdb_object(f"https://api.themoviedb.org/3/{media}/{row['tmdb_id']}?api_key={key}&language=zh-CN&append_to_response=alternative_titles,translations",
                                'seasons' if media == 'tv' else None)
        item = {k: detail[k] for k in ('id', 'title', 'name', 'release_date', 'first_air_date', 'poster_path', 'backdrop_path', 'popularity') if k in detail}
        metadata.update(item=item, detail=detail, needs_tmdb_refresh=False)
    else:
        item, detail = metadata['item'], metadata['detail']
    kind = 'movie' if row['media_type'] == 'movie' else 'series'
    title, cat = row['title'], row['cat']
    directory = Path(config.CINEMA2_DIR) / cat / title
    if Path(cat).name != cat or Path(title).name != title or cat in {'.', '..'} or title in {'.', '..'}:
        raise ValueError('unsafe media identity')
    key = tm.read_tmdb_key() if kind != 'movie' else None
    maps = {}
    targets = {0: [0]} if kind == 'movie' else {}
    for season in detail.get('seasons', []) if kind != 'movie' else []:
        number = int(season.get('season_number') or 0)
        if number < 1 or (only_season is not None and number != only_season):
            continue
        body = tm.tmdb_object(f"https://api.themoviedb.org/3/tv/{item['id']}/season/{number}?api_key={key}&language=zh-CN", 'episodes')
        maps[number] = {int(ep['episode_number']): ep for ep in body['episodes']
                        if ep.get('episode_number') and tm.is_episode_aired(ep, number, int(ep['episode_number']), detail)}
        targets[number] = sorted(maps[number])
    if sum(len(x) for x in targets.values()) > max_episodes:
        raise ValueError('episode budget exceeded')
    proven, complete = gate_media(item, detail, cat, title, kind, targets)
    if not any(proven.values()):
        return {'published': 0, 'partial': not complete}
    added = 0
    with playback_lease(), ExitStack() as locks:
        for season in sorted(proven):
            if proven[season]:
                locks.enter_context(file_lock('work:' + repr((cat, title.casefold(), season)), timeout=2))
        directory.mkdir(parents=True, exist_ok=True)
        if kind == 'movie':
            tm.gen_movie_nfo(str(directory), item, detail)
            path = directory / (title + '.strm')
            added += int(tm.add_pending_strm(str(path), cat, title, 0, 0))
            db.save_strm_map(cat, title, 0, 0, str(path))
        else:
            tm.gen_tv_nfo(str(directory), item, detail)
            for season, episodes in proven.items():
                if not episodes:
                    continue
                season_dir = directory / f'Season {season:02d}'
                season_dir.mkdir(exist_ok=True)
                season_detail = next(s for s in detail['seasons'] if s.get('season_number') == season)
                tm.gen_season_nfo(str(season_dir), item, detail, season_detail)
                for ep in sorted(episodes):
                    path = tm.episode_strm_path(str(season_dir), tm.sanitize(tm.fmt_title(item)), season, ep)
                    added += int(tm.add_pending_strm(path, cat, title, season, ep))
                    db.save_strm_map(cat, title, season, ep, path)
                    info = maps[season][ep]
                    tm.gen_episode_nfo(str(Path(path).with_suffix('.nfo')), info.get('name'), season, ep, info)
                    if info.get('still_path'):
                        tm.download_image(tm.IMG_BASE + '/w500' + info['still_path'], str(Path(path).with_suffix('')) + '-thumb.jpg')
        tm.download_poster(str(directory), item, kind)
        tm.download_fanart(str(directory), item)
    db.update_staging_media(row['tmdb_id'], row['media_type'], status='live')
    if added:
        notify('media_published', '新片已入库', title, 'P2')
    return {'published': added, 'partial': not complete}


def run_staging(limit=20):
    stats = {'checked': 0, 'published': 0, 'errors': 0}
    for row in db.list_staging_media(due_before=int(time.time()), limit=limit):
        stats['checked'] += 1
        try:
            result = publish_staged(row)
            stats['published'] += result['published']
            stats['errors'] += int(result['partial'])
            if not result['published']:
                db.update_staging_media(row['tmdb_id'], row['media_type'],
                    retry_count=int(row.get('retry_count') or 0) + 1,
                    next_check_at=int(time.time()) + retry_delay(row.get('release_date', '')))
        except Exception:
            stats['errors'] += 1
            db.update_staging_media(row['tmdb_id'], row['media_type'],
                retry_count=int(row.get('retry_count') or 0) + 1, next_check_at=int(time.time()) + 86400)
    return stats


def retire_ghosts(limit=20):
    """Retire only complete three-layer no-source evidence, keeping exact rollback files."""
    from quark_vod.integrations.hometheater import daily_update_and_prune as daily
    stats = {'checked': 0, 'retired': 0, 'protected': 0, 'unknown': 0}
    root = Path(config.CINEMA2_DIR).resolve(strict=True)
    cursor_file = Path(config.DATA_DIR) / 'ghost_cursor.json'
    after = ''
    if cursor_file.exists():
        after = json.loads(cursor_file.read_text()).get('after', '')
    folders = sorted(p for cat in root.iterdir() if cat.is_dir() and not cat.is_symlink()
                     for p in cat.iterdir() if p.is_dir() and not p.is_symlink())
    ordered = [p for p in folders if str(p) > after] + [p for p in folders if str(p) <= after]
    for folder in ordered[:limit]:
        stats['checked'] += 1
        cursor_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = cursor_file.with_suffix('.tmp')
        temporary.write_text(json.dumps({'after': str(folder)}))
        temporary.replace(cursor_file)
        paths = sorted(folder.rglob('*.strm'))
        if not paths or len(paths) > 500:
            stats['unknown'] += 1
            continue
        targets = {}
        safe = True
        for path in paths:
            if path.is_symlink():
                safe = False
                break
            text = path.read_text().strip()
            parsed = urllib.parse.urlsplit(text)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path != '/pending' or not strm_manager.is_pending(text):
                safe = False
                break
            if query.get('cat', [''])[0] != folder.parent.name or query.get('title', [''])[0] != folder.name:
                safe = False
                break
            targets.setdefault(int(query.get('season', ['0'])[0]), []).append(int(query.get('episode', ['0'])[0]))
        if not safe:
            stats['protected'] += 1
            continue
        nfo = folder / ('movie.nfo' if 0 in targets else 'tvshow.nfo')
        if not nfo.is_file() or nfo.is_symlink():
            stats['unknown'] += 1
            continue
        node = ET.parse(nfo).getroot()
        tmdb_id = node.findtext('tmdbid')
        if not str(tmdb_id or '').isdigit():
            stats['unknown'] += 1
            continue
        proofs = [source_evidence(folder.parent.name, folder.name, s, sorted(set(eps)), require_matrix=True)
                  for s, eps in targets.items()]
        if not proofs or not all(p['no_source'] for p in proofs):
            stats['unknown'] += 1
            continue
        with maintenance_window():
            protected = daily.get_protected_titles()
            if folder.name in protected or folder.name.rsplit(' (', 1)[0] in protected:
                stats['protected'] += 1
                continue
            con = db._conn()
            if con.execute("SELECT 1 FROM transfers WHERE cat=? AND title=? AND (status='done' OR save_started_at IS NOT NULL OR save_file_fid IS NOT NULL) LIMIT 1",
                           (folder.parent.name, folder.name)).fetchone():
                stats['protected'] += 1
                continue
            # Recheck STRM contents while no playback/add writer can change the directory.
            if any(p.is_symlink() or urllib.parse.urlsplit(p.read_text().strip()).path != '/pending' for p in paths):
                raise Busy('media changed during source audit')
            kind = 'movie' if 0 in targets else 'tv'
            db.upsert_staging_media(tmdb_id, folder.parent.name, folder.name, node.findtext('year') or '',
                kind, status='pending', metadata={'archive': str(folder), 'needs_tmdb_refresh': True},
                next_check_at=int(time.time()) + 86400)
            archive = Path(config.DATA_DIR) / 'staging_archive'
            archive.mkdir(exist_ok=True)
            destination = archive / (kind + '-' + str(tmdb_id) + '-' + str(time.time_ns()))
            folder.rename(destination)
            try:
                db.remove_strm_maps_below(str(folder))
            except Exception:
                destination.rename(folder)
                raise
            db.update_staging_media(tmdb_id, kind, metadata={'archive': str(destination), 'needs_tmdb_refresh': True})
            stats['retired'] += 1
        cursor_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = cursor_file.with_suffix('.tmp')
        temporary.write_text(json.dumps({'after': str(folder)}))
        temporary.replace(cursor_file)
    return stats


def airing_chase(limit=20):
    from quark_vod.integrations.hometheater import tmdb_populate as tm
    stats = {'checked': 0, 'added': 0, 'errors': 0}
    today = dt.date.today().isoformat()
    for row in db.list_airing_calendar(due_before=today, limit=limit):
        stats['checked'] += 1
        try:
            metadata = row.get('metadata') or {}
            key = tm.read_tmdb_key()
            detail = tm.tmdb_object(f"https://api.themoviedb.org/3/tv/{row['tmdb_id']}?api_key={key}&language=zh-CN&append_to_response=alternative_titles,translations", 'seasons')
            if detail.get('status') in {'Ended', 'Canceled'}:
                db.update_airing_calendar(row['tmdb_id'], row['season'], next_air_date='9999-12-31')
                continue
            latest = max(int(s.get('season_number') or 0) for s in detail['seasons'])
            if latest != row['season']:
                seed_calendar(metadata['item'], detail, row['cat'], row['title'])
                db.update_airing_calendar(row['tmdb_id'], row['season'], next_air_date='9999-12-31')
                continue
            item = metadata['item']
            stage(item, detail, row['cat'], row['title'], 'series')
            staged = {'tmdb_id': row['tmdb_id'], 'cat': row['cat'], 'title': row['title'],
                      'media_type': 'tv', 'metadata': {'item': item, 'detail': detail}}
            with file_lock('work:' + repr((row['cat'], row['title'].casefold(), 0)), timeout=2):
                result = publish_staged(staged, only_season=latest)
            stats['added'] += result['published']
            if result['published']:
                refresh_series(row['tmdb_id'], row['title'])
                if row.get('is_user_watching'):
                    from quark_vod.server import resolve_playback
                    audits = db._conn().execute("SELECT episode FROM source_audits WHERE cat=? AND title=? AND season=? AND has_quark_source=1 ORDER BY episode DESC LIMIT 1",
                        (row['cat'], row['title'], row['season'])).fetchone()
                    if audits:
                        resolve_playback(Work(row['cat'], row['title'], row['season'], audits['episode']))
            next_ep = detail.get('next_episode_to_air') or {}
            db.update_airing_calendar(row['tmdb_id'], row['season'],
                next_air_date=next_ep.get('air_date') or (dt.date.today() + dt.timedelta(days=1)).isoformat(),
                metadata={'item': item, 'detail': detail})
        except Exception:
            stats['errors'] += 1
    if stats['errors']:
        notify('airing_partial', '追更有未确认条目', '本轮追更存在失败，请查看调度日志。')
    return stats


def _upgrade_file_quality(name, size, media_type):
    """Score exact file labels and size; never promote a share-title hint."""
    import re
    from quark_vod.quality import parse_media_tags, quality_hint
    if not name or not size or int(size) <= 0:
        return None
    tags = parse_media_tags(name, size, media_type)
    tags['level'] = quality_hint(name)['level']
    # Core V2 already rewards dual audio/effects; distinguish explicit HDR10+.
    tags['score'] += 8 if re.search(r'(?i)HDR[ ._-]*10\+', name) else 0
    return tags


def _old_upgrade_quality(row):
    """Only the completed exact file, tied to this audit source, proves old HD."""
    from quark_vod.panshare_matrix import canonical_share
    if row['check_status'] not in ('valid_hd', 'selected_file'):
        return None
    variant = dict(row).get('variant', '')
    current = db.find_done_transfer(row['cat'], row['title'], row['season'], row['episode'], variant=variant)
    if not current or any(current[key] for key in ('save_started_at', 'save_task_id', 'save_target')):
        return None
    source = canonical_share(current['share_url'] or '')
    if not source or source != canonical_share(row['best_share_url'] or ''):
        return None
    name = Path(current['remote_path'] or '').name
    tags = _upgrade_file_quality(name, current['file_size'], 'tv' if row['episode'] else 'movie')
    return tags if tags and tags['level'] == 'hd' and 0 < tags['resolution'] < 2160 else None


def quality_upgrade(limit=10):
    """Replace protected, proven HD only with a higher-scored compatible 4K file."""
    from quark_vod.integrations.hometheater import daily_update_and_prune as daily
    from quark_vod.integrations.hometheater.vod_search_and_add import replace_source
    from quark_vod.quality import matches_variant
    stats = {'checked': 0, 'upgraded': 0, 'deferred': 0}
    with maintenance_window():
        protected = daily.get_protected_titles()
    rows = db._conn().execute("SELECT * FROM source_audits WHERE has_quark_source=1 AND is_4k=0 ORDER BY checked_at LIMIT ?", (limit * 5,)).fetchall()
    for row in rows:
        if stats['checked'] >= limit:
            break
        if row['title'] not in protected and row['title'].rsplit(' (', 1)[0] not in protected:
            continue
        stats['checked'] += 1
        try:
            old_quality = _old_upgrade_quality(row)
            if not old_quality:
                stats['deferred'] += 1
                continue
            report = search.search_report(row['title'], season=row['season'], episode=row['episode'], use_cache=False)
            if report['partial']:
                stats['deferred'] += 1
                continue
            candidates = search.rank_candidates(report['results'], row['title'], row['season'], row['episode'], row['cat'])
            transfer = QuarkTransfer(OpenListClient())
            variant = dict(row).get('variant', '')
            best = None
            for proposed in candidates[:8]:
                if proposed.share_url == row['best_share_url']:
                    continue
                try:
                    token, pwd, roots = transfer.resolve_share(proposed.share_url)
                    files = transfer.share_files(token, pwd, roots, season=row['season'] if row['episode'] else None)
                    selected = transfer.select_share_video(files, row['title'], row['season'], row['episode'], variant=variant)
                    if not selected:
                        continue
                    exact = selected.get('share') or {}
                    name = exact.get('file_name') or selected.get('name', '')
                    size = exact.get('size') or selected.get('size') or 0
                    fid = str(exact.get('fid') or '')
                    quality = _upgrade_file_quality(name, size, 'tv' if row['episode'] else 'movie')
                    if (not fid or not quality or quality['level'] != '4k'
                            or not matches_variant(quality, variant)
                            or quality['dv_requires_support'] and variant in ('', 'sdr')
                            or quality['score'] <= old_quality['score']):
                        continue
                    if best is None or quality['score'] > best[0]:
                        best = (quality['score'], proposed, fid)
                except Exception:
                    continue
            if best:
                _, candidate, selected_fid = best
                result = replace_source(row['cat'], row['title'], row['season'], row['episode'],
                                        candidate, reason='quality_upgrade', variant=variant,
                                        selected_fid=selected_fid)
                if result.get('ok') and result.get('state') == 'ready':
                    stats['upgraded'] += 1
                    notify('quality_upgraded', '画质已升级', row['title'] + ' 已换用更高综合分的 4K 片源。', 'P2')
        except Exception:
            stats['deferred'] += 1
    return stats


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('task', choices=['staging', 'ghosts', 'airing', 'upgrade', 'digest'])
    args = parser.parse_args()
    try:
        with file_lock('automation:' + args.task):
            if args.task == 'digest':
                path = Path(config.DATA_DIR) / 'daily_update_status.json'
                text = json.dumps(json.loads(path.read_text()) if path.exists() else {'status': 'no_report'}, ensure_ascii=False)
                result = notify('daily_digest', '影院调度日报', text[:1500])
            else:
                result = {'staging': run_staging, 'ghosts': retire_ghosts,
                          'airing': airing_chase, 'upgrade': quality_upgrade}[args.task]()
            if result.get('errors'):
                notify('automation_partial', '影院调度未完成', args.task + ' 有条目未确认，将在下一轮重试。')
            print(json.dumps(result, ensure_ascii=False))
            return int(bool(result.get('errors')))
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
