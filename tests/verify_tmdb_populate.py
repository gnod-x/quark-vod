#!/usr/bin/env python3
"""Run the real TMDB metadata entrypoint against a disposable library/database.

Default mode reads the existing TMDB key and sends GETs only to TMDB.
No production library, Quark cloud, Jellyfin API or image download is touched.
--offline-fixture exercises the same worker with synthetic responses instead.
"""
import argparse
from contextlib import redirect_stdout, redirect_stderr
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from unittest.mock import patch
import xml.etree.ElementTree as ET

BASE = Path(os.environ.get('HOMETHEATER_SOURCE_DIR', '/home/dong/projects/hometheater'))
if not (BASE/'scripts/tmdb_populate.py').is_file():
    BASE = Path(__file__).resolve().parents[2]/'hometheater'
CATEGORIES = ('国产电影', '国产剧集')


def worker(root, fixture):
    root = Path(root).resolve()
    if root.parent != Path(tempfile.gettempdir()).resolve() or not root.name.startswith('cinema-tmdb-verify-'):
        raise ValueError('worker directory must be the isolated temporary root')
    # Establish paths before importing any module that initializes SQLite.
    os.environ.update(HOMETHEATER_BASE_DIR=str(root), VOD_BASE_DIR=str(root),
                      VOD_CINEMA2_DIR=str(root/'cinema2'), VOD_DATA_DIR=str(root/'data'),
                      VOD_LOG_DIR=str(root/'logs'), AI_PARSE_ENABLED='0',
                      PENDING_BASE_URL='http://tmdb-verification.invalid:8099')
    for name in ('QUARK_COOKIE', 'JELLYFIN2_KEY', 'OPENLIST_ADMIN_PASS'):
        os.environ.pop(name, None)
    sys.path.insert(0, str(BASE/'scripts'))
    import tmdb_populate as job
    from quark_vod import config, coordination, db, strm_manager
    for path in (job.CINEMA2, job.STATE_FILE, job.LOG_DIR, config.STATE_DB, config.CINEMA2_DIR):
        if not Path(path).resolve().is_relative_to(root):
            raise RuntimeError('unexpected persistence path')
    report = {'time': datetime.datetime.now().astimezone().isoformat(),
              'mode': 'offline_fixture' if fixture else 'live_tmdb',
              'source_sha256': hashlib.sha256((BASE/'scripts/tmdb_populate.py').read_bytes()).hexdigest(),
              'production_mutation': False, 'images_tested': False, 'request_count': 0}
    started = time.monotonic()
    deadline = started + 200
    original_http = job.http_json
    original_urlopen = urllib.request.urlopen

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(NoRedirect)

    def guarded_open(request, *args, **kwargs):
        if fixture:
            raise AssertionError('offline fixture attempted network')
        url = request.full_url if isinstance(request, urllib.request.Request) else request
        parsed = urllib.parse.urlsplit(url)
        method = request.get_method() if isinstance(request, urllib.request.Request) else 'GET'
        if parsed.scheme != 'https' or parsed.netloc != 'api.themoviedb.org' or method != 'GET':
            raise RuntimeError('request outside TMDB read-only scope')
        if report['request_count'] >= 20 or time.monotonic() >= deadline:
            raise TimeoutError('verification request budget exhausted')
        report['request_count'] += 1
        kwargs['timeout'] = min(float(kwargs.get('timeout', 25)), 15)
        return opener.open(request, *args, **kwargs)

    def fixture_http(url, retries=3):
        report['fixture_response_count'] = report.get('fixture_response_count', 0) + 1
        path = urllib.parse.urlsplit(url).path
        year = job.CUR_YEAR
        if '/discover/' in path:
            movie = path.endswith('/movie')
            return {'total_pages': 1, 'results': [{
                'id': 101 if movie else 202,
                'title' if movie else 'name': '验收电影' if movie else '验收剧集',
                'release_date' if movie else 'first_air_date': f'{year}-01-01',
                'poster_path': '/synthetic.jpg', 'origin_country': ['CN']}]}
        if '/movie/' in path:
            return {'id': 101, 'runtime': 90}
        if '/season/' in path:
            return {'episodes': [{'episode_number': 1, 'air_date': f'{year}-01-01'},
                                 {'episode_number': 2, 'air_date': '2099-01-01'}]}
        return {'id': 202, 'status': 'Returning Series',
                'last_episode_to_air': {'season_number': 1, 'episode_number': 1},
                'seasons': [{'season_number': 1, 'episode_count': 2}]}

    try:
        urllib.request.urlopen = guarded_open
        if fixture:
            job.read_tmdb_key = lambda: 'synthetic-verification-key'
            job.http_json = fixture_http
        else:
            job.TMDB_XML = str(BASE/'jellyfin/config/plugins/configurations/Jellyfin.Plugin.Tmdb.xml')
            job.http_json = lambda url, retries=3: original_http(url, retries=1)
        # Scope is metadata/identity, not optional artwork availability.
        job.download_poster = lambda *a, **kw: False
        job.download_fanart = lambda *a, **kw: False
        job.download_image = lambda *a, **kw: False
        # All persistence paths above are verified under the disposable root.
        # Exercise the real maintenance lock, but isolate the external Jellyfin
        # idle boundary: this metadata verifier never reads production user data
        # or calls Sessions. Production must still fail closed if its DB is lost.
        report['external_idle_boundary'] = 'isolated_temporary_library'
        with patch.object(coordination, 'ensure_idle'), open(os.devnull, 'w') as sink, redirect_stdout(sink), redirect_stderr(sink):
            code = job.main(['--cats', ','.join(CATEGORIES), '--limit', '1', '--max-pages', '1'])
        state = json.loads(Path(job.STATE_FILE).read_text())
        files = list(Path(job.CINEMA2).rglob('*.strm'))
        rows = db._conn().execute('SELECT cat,title,season,episode,strm_path FROM strm_map').fetchall()
        mapped = {Path(row['strm_path']) for row in rows}
        identities_ok = mapped == set(files) and len(rows) == len(files)
        for row in rows:
            path = Path(row['strm_path'])
            assert path.resolve().is_relative_to(root/'cinema2')
            content = path.read_text().strip()
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(content).query)
            identities_ok = identities_ok and strm_manager.is_pending(content) and (
                query.get('cat') == [row['cat']] and query.get('t') == [row['title']]
                and query.get('s') == [str(row['season'])] and query.get('e') == [str(row['episode'])])
        nfos = list(Path(job.CINEMA2).rglob('*.nfo'))
        for path in nfos:
            ET.parse(path)
        transfer_count = db._conn().execute('SELECT count(*) FROM transfers').fetchone()[0]
        report.update(exit_code=code, state=state, strm_count=len(files), nfo_count=len(nfos),
                      identities_match=bool(identities_ok), transfer_count=transfer_count)
        report['pass'] = (code == 0 and state.get('status') == 'complete' and identities_ok
                          and transfer_count == 0 and len(files) >= 2
                          and all(state['categories'][cat]['new'] == 1 for cat in CATEGORIES))
        if fixture:
            report['pass'] = report['pass'] and len(files) == 2 and report['request_count'] == 0
    except Exception as exc:
        # URLs/API keys may occur in exception messages; only record the type.
        report.update(error_type=type(exc).__name__, **{'pass': False})
    finally:
        urllib.request.urlopen = original_urlopen
        if hasattr(db._local, 'conn'):
            db._local.conn.close()
            del db._local.conn
    report['seconds'] = round(time.monotonic() - started, 3)
    (root/'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['pass'] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output')
    parser.add_argument('--offline-fixture', action='store_true')
    parser.add_argument('--worker', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        return worker(args.worker, args.offline_fixture)
    if not args.output and not args.offline_fixture:
        parser.error('live verification requires --output')
    report = {'pass': False, 'production_mutation': False}
    with tempfile.TemporaryDirectory(prefix='cinema-tmdb-verify-') as directory:
        root = Path(directory)
        command = [sys.executable, '-I', str(Path(__file__).resolve()), '--worker', directory]
        if args.offline_fixture:
            command.append('--offline-fixture')
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=240)
            if (root/'result.json').is_file():
                report = json.loads((root/'result.json').read_text())
            else:
                report['error_type'] = 'WorkerDidNotProduceReport'
            report['worker_exit_code'] = result.returncode
            report['pass'] = bool(report['pass'] and result.returncode == 0)
        except subprocess.TimeoutExpired:
            report['error_type'] = 'WorkerTimeout'
    report['temporary_directory_removed'] = not root.exists()
    report['pass'] = bool(report['pass'] and report['temporary_directory_removed'])
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['pass'] else 1


if __name__ == '__main__':
    sys.exit(main())
