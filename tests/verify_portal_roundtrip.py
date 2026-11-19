#!/usr/bin/env python3
"""Submit one NEW production portal episode and verify its Jellyfin2 identity.

Requires --execute. Creates a private online SQLite backup and manifest before
submission. The sample is retained for playback verification and must be removed
with an identity-scoped cleanup afterwards; never restore the whole backup.
No secrets, candidate tokens or share/media URLs enter the public report.
"""
import argparse
from contextlib import closing
import datetime
import json
from pathlib import Path
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

BASE = Path('/home/dong/projects/hometheater')
PORTAL = 'http://127.0.0.1:8088'
JF = 'http://127.0.0.1:8097'


def write_json(path, data):
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def request(root, path, payload=None, token=None, timeout=145):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(root + path, data=body, headers={'Content-Type': 'application/json'})
    if token:
        req.add_unredirected_header('Authorization', 'MediaBrowser Token="' + token + '"')
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read(4 * 1024**2 + 1)
        if len(raw) > 4 * 1024**2:
            raise RuntimeError('response_too_large')
        data = json.loads(raw) if raw else {}
    if root == PORTAL and data.get('code') != 0:
        raise RuntimeError('portal_request_failed')
    return data


def state_rows(key):
    with closing(sqlite3.connect((BASE / 'data/quark_plays.db').as_uri() + '?mode=ro', uri=True, timeout=2)) as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute('SELECT * FROM transfers WHERE cat=? AND title=? AND season=? AND episode=?', key)]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--execute', action='store_true')
    ap.add_argument('--title', default='庆余年')
    ap.add_argument('--year', default='2019')
    ap.add_argument('--season', type=int, default=1)
    ap.add_argument('--episode', type=int, default=1)
    ap.add_argument('--max-bytes', type=int, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--state-dir', type=Path, required=True)
    args = ap.parse_args()
    if not args.execute:
        ap.error('--execute is required for one real portal add')
    args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {'time': datetime.datetime.now().astimezone().isoformat(), 'production_mutation': True,
              'scope': 'production portal API -> save -> Jellyfin2 library; playback verified separately', 'checks': {}}
    manifest = {'submitted': False, 'state_dir': str(args.state_dir)}
    def save():
        write_json(args.output, report)
        write_json(args.state_dir / 'manifest.json', manifest)
    def check(name, value):
        report['checks'][name] = value
        save()
        print(json.dumps({'check': name, 'result': value}, ensure_ascii=False), flush=True)
    env = {}
    for line in (BASE / '.env').read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip(chr(34)).strip(chr(39))
    token = env['JELLYFIN2_KEY']
    started = time.monotonic()
    try:
        catalog = request(PORTAL, '/api/vod/search?kw=' + urllib.parse.quote(args.title))
        item = next(x for x in catalog['data'] if x['title'] == args.title and x['year'] == args.year and x['media_type'] == 'tv')
        sources = request(PORTAL, '/api/vod/sources', {'id': item['id'], 'media_type': 'tv', 'season': args.season, 'episode': args.episode})
        check('discovery', {'catalog_matches': len(catalog['data']), 'candidates': len(sources['candidates']), 'partial': sources['partial']})
        selected = None
        for candidate in sources['candidates'][:6]:
            try:
                inspected = request(PORTAL, '/api/vod/inspect', {'token': candidate['token']})['file']
                if inspected['quality']['level'] == '4k' and 0 < inspected['size'] <= args.max_bytes:
                    selected = candidate
                    break
            except Exception:
                continue
        if selected is None:
            raise RuntimeError('no_inspected_4k_candidate_within_budget')
        check('file_inspection', inspected)
        cat = sources['item']['cat']
        folder = item['title'] + ' (' + item['year'] + ')'
        key = (cat, folder, args.season, args.episode)
        root = BASE / 'cinema2' / cat / folder
        path = root / f'Season {args.season:02d}' / f'{item["title"]} - S{args.season:02d}E{args.episode:02d}.strm'
        manifest.update(key=key, root=str(root), strm_path=str(path), selected_share=selected['share_url'], tmdb_id=item['id'])
        if root.exists():
            raise RuntimeError('sample_library_directory_already_exists')
        for port, key_name in ((8096, 'JELLYFIN_KEY'), (8097, 'JELLYFIN2_KEY')):
            sessions = request('http://127.0.0.1:' + str(port), '/Sessions', token=env[key_name], timeout=8)
            if not isinstance(sessions, list) or any(x.get('NowPlayingItem') for x in sessions):
                raise RuntimeError('playback_not_idle')
        with closing(sqlite3.connect((BASE / 'data/quark_plays.db').as_uri() + '?mode=ro', uri=True, timeout=2)) as con:
            if con.execute("SELECT COUNT(*) FROM transfers WHERE status IN ('searching','saving')").fetchone()[0]:
                raise RuntimeError('transfers_not_idle')
            for table in ('transfers', 'strm_map', 'source_audits', 'user_pinned', 'plays'):
                if con.execute('SELECT COUNT(*) FROM ' + table + ' WHERE title IN (?,?)', (folder, item['title'])).fetchone()[0]:
                    raise RuntimeError('sample_database_records_already_exist')
            backup = args.state_dir / 'quark_plays.before.db'
            backup.touch(mode=0o600)
            with closing(sqlite3.connect(backup)) as dest:
                con.backup(dest, pages=128, sleep=0.05)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
        from quark_vod.transfer import QuarkTransfer
        allowed, free = QuarkTransfer(None)._check_capacity(inspected['size'])
        if not allowed or free < inspected['size'] + 500 * 1024**2:
            raise RuntimeError('free_capacity_not_confirmed')
        check('preflight', {'sample_absent': True, 'playback_idle': True, 'transfers_idle': True, 'backup': str(backup), 'free_bytes': free})
        # Persist an intent before HTTP; even a timeout can mean the add succeeded.
        manifest['submission_attempted'] = True
        save()
        submitted = request(PORTAL, '/api/vod/add', {'token': selected['token']})
        manifest.update(submitted=True, job=submitted['job'])
        save()
        duplicate = request(PORTAL, '/api/vod/add', {'token': selected['token']})
        check('submission', {'accepted': True, 'duplicate_same_job': duplicate['job'] == submitted['job']})
        if duplicate['job'] != submitted['job']:
            raise RuntimeError('duplicate_submission_not_coalesced')
        deadline = time.monotonic() + 480
        states = []
        while time.monotonic() < deadline:
            status = request(PORTAL, '/api/vod/job?id=' + urllib.parse.quote(submitted['job']), timeout=20)
            if not states or states[-1]['state'] != status['state']:
                states.append({'state': status['state'], 'seconds': round(time.monotonic() - started, 2)})
                check('job_states', states)
            if status['state'] == 'ready':
                break
            if status['state'] == 'failed':
                raise RuntimeError('portal_job_failed')
            time.sleep(3)
        else:
            raise RuntimeError('portal_job_timed_out')
        rows = state_rows(key)
        if len(rows) != 1 or rows[0]['status'] != 'done' or not rows[0]['remote_path']:
            raise RuntimeError('expected_one_done_transfer')
        row = rows[0]
        manifest.update(transfer_id=row['id'], remote_path=row['remote_path'])
        save()
        same_source = row['share_url'] == selected['share_url']
        live = '/play?' in path.read_text()
        with closing(sqlite3.connect((BASE / 'data/quark_plays.db').as_uri() + '?mode=ro', uri=True, timeout=2)) as con:
            locked = con.execute('SELECT best_share_url FROM source_audits WHERE cat=? AND title=? AND season=? AND episode=?', key).fetchone()
            pin = con.execute('SELECT COUNT(*) FROM user_pinned WHERE cat=? AND title IN (?,?)', (cat, folder, item['title'])).fetchone()[0]
        result = {'transfer_id': row['id'], 'status': row['status'], 'file_size': row['file_size'], 'selected_source_used': same_source,
                  'selected_source_persisted': bool(locked and locked[0] == selected['share_url']), 'pinned': bool(pin), 'live_strm': live, 'refresh_ok': status['refresh_ok']}
        check('production_state', result)
        if not (same_source and result['selected_source_persisted'] and pin and live and row['file_size'] == inspected['size']):
            raise RuntimeError('production_state_mismatch')
        deadline = time.monotonic() + 240
        found = None
        while time.monotonic() < deadline:
            series = request(JF, '/Items?' + urllib.parse.urlencode({'Recursive': 'true', 'IncludeItemTypes': 'Series', 'SearchTerm': item['title'], 'Fields': 'Path,ProviderIds', 'Limit': 30}), token=token, timeout=20)
            for show in series.get('Items', []):
                if show.get('Path') != '/cinema2/' + cat + '/' + folder:
                    continue
                episodes = request(JF, '/Shows/' + show['Id'] + '/Episodes?' + urllib.parse.urlencode({'Season': args.season, 'Fields': 'Path,MediaStreams'}), token=token, timeout=20)
                found = next((x for x in episodes.get('Items', []) if x.get('Path') == '/cinema2/' + str(path.relative_to(BASE / 'cinema2'))), None)
                if found:
                    break
            if found:
                break
            time.sleep(4)
        if found is None:
            raise RuntimeError('jellyfin_item_not_discovered')
        manifest['jellyfin_item_id'] = found['Id']
        check('jellyfin_library', {'item_id': found['Id'], 'season': found.get('ParentIndexNumber'), 'episode': found.get('IndexNumber'),
                                  'streams': [{k: s.get(k) for k in ('Type', 'Codec', 'Width', 'Height')} for s in found.get('MediaStreams', [])]})
        report['pass'] = True
    except Exception as exc:
        report.update(error_type=type(exc).__name__, error=str(exc) if isinstance(exc, RuntimeError) else 'verification_failed')
        report['pass'] = False
    finally:
        report['seconds'] = round(time.monotonic() - started, 2)
        report['sample_retained_for_playback'] = manifest.get('submission_attempted', False)
        save()
    print(json.dumps({'pass': report['pass'], 'seconds': report['seconds'], 'error': report.get('error')}, ensure_ascii=False), flush=True)
    if not report['pass']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
