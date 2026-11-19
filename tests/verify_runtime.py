#!/usr/bin/env python3
"""Bounded, read-only cinema inventory; never prints credentials or media URLs."""
import argparse
import collections
import datetime
import hashlib
import json
import pathlib
import re
import sqlite3
import subprocess
import urllib.error
import urllib.request
from contextlib import closing

BASE = pathlib.Path('/home/dong/projects/hometheater')
PROJECT = pathlib.Path(__file__).resolve().parents[1]


def command(args, timeout=20):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError('command failed: ' + args[0] + ' exit ' + str(r.returncode))
    return r.stdout


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read(4 * 1024 * 1024)
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        return r.status, data


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', required=True)
    args = ap.parse_args()
    report = {'time': datetime.datetime.now().astimezone().isoformat(), 'checks': {}}

    def check(name, fun):
        try:
            report['checks'][name] = {'status': 'observed', 'result': fun()}
        except Exception as e:
            report['checks'][name] = {'status': 'error', 'error_type': type(e).__name__}

    def containers():
        values = json.loads(command(['docker', 'inspect', 'jellyfin', 'jellyfin2', 'openlist', 'quark-vod']))
        return [{
            'name': x['Name'].lstrip('/'), 'image': x['Image'],
            'running': x['State']['Running'], 'started': x['State']['StartedAt'],
            'oom_killed': x['State']['OOMKilled'], 'restarts': x['RestartCount'],
            'health': x['State'].get('Health', {}).get('Status'),
            'memory_limit': x['HostConfig']['Memory'],
            'gomemlimit': next((e.split('=',1)[1] for e in x['Config'].get('Env',[]) if e.startswith('GOMEMLIMIT=')), None),
            'network': x['HostConfig']['NetworkMode'],
            'mounts': [{'source': m['Source'], 'destination': m['Destination'], 'rw': m['RW']} for m in x['Mounts']],
        } for x in values]

    check('containers', containers)
    check('compose_hometheater', lambda: command(['docker', 'compose', '-f', str(BASE/'docker-compose.yml'), 'config', '--quiet']).strip() or 'valid')
    check('compose_quark_vod', lambda: command(['docker', 'compose', '-p', 'quark-vod', '-f', str(BASE/'quark-vod.compose.yml'), 'config', '--quiet']).strip() or 'valid')
    check('dgpu_power', lambda: pathlib.Path('/sys/bus/pci/devices/0000:01:00.0/power_state').read_text().strip())
    for name in ('jellyfin', 'jellyfin2', 'openlist', 'quark-vod'):
        check('cgroup_' + name, lambda n=name: command(['docker', 'exec', n, 'sh', '-c', 'for f in memory.current memory.peak memory.max memory.events; do echo $f; cat /sys/fs/cgroup/$f; done']))
    for name in ('jellyfin', 'jellyfin2'):
        check('vaapi_' + name, lambda n=name: {'profiles': len(re.findall(r'VAProfile', command(['docker', 'exec', n, '/usr/lib/jellyfin-ffmpeg/vainfo', '--display', 'drm', '--device', '/dev/dri/renderD128'])))})
        check('wrapper_' + name, lambda n=name: command(['docker', 'exec', n, 'sh', '-c', 'test -x /custom-ffmpeg/ffmpeg && test -x /custom-ffmpeg/ffprobe && LD_LIBRARY_PATH=/custom-ffmpeg/bin /custom-ffmpeg/bin/jq --version']).strip())
    for name in ('hometheater-web', 'rclone-openlist', 'cinema-quark'):
        check('unit_' + name, lambda n=name: command(['systemctl', 'show', n + '.service', '-p', 'Id', '-p', 'ActiveState', '-p', 'SubState', '-p', 'UnitFileState']).splitlines())

    def cron():
        text = command(['crontab', '-l'])
        return [{'schedule': ' '.join(l.split()[:5]), 'scripts': re.findall(r'[\w./-]+\.(?:py|sh)', l), 'cleanup_module': '-m quark_vod.cleanup' in l or bool(re.search(r'run_vod_host\.py.*\bcleanup\b', l)), 'host_runtime_wrapper': 'run_vod_host.py' in l}
                for l in text.splitlines() if l.strip() and not l.lstrip().startswith('#') and 'hometheater' in l]
    check('cron', cron)
    for port, endpoint in ((8096, '/health'), (8097, '/health'), (8099, '/health'), (8099, '/admin'), (8099, '/docs'), (8088, '/'), (5244, '/api/public/settings')):
        check('http_' + str(port) + endpoint, lambda p=port, e=endpoint: {'http': get('http://127.0.0.1:' + str(p) + e)[0]})
    # GET /capability is authenticated and read-only. Only explicit POST probe
    # can create a remote write probe; neither is needed for this inventory.

    env = {}
    for l in (BASE/'.env').read_text().splitlines():
        if '=' in l and not l.lstrip().startswith('#'):
            k, v = l.split('=', 1)
            env[k.strip()] = v.strip().strip(chr(34)).strip(chr(39))
    for port, key in ((8096, 'JELLYFIN_KEY'), (8097, 'JELLYFIN2_KEY')):
        headers = {'Authorization': 'MediaBrowser Token="' + env.get(key, '') + '"'}
        def jellyfin(p=port, h=headers):
            root = 'http://127.0.0.1:' + str(p)
            status, info = get(root + '/System/Info', h)
            _, libraries = get(root + '/Library/VirtualFolders', h)
            _, sessions = get(root + '/Sessions', h)
            return {'http': status, 'version': info.get('Version'),
                    'libraries': [{'type': x.get('CollectionType'), 'locations': x.get('Locations')} for x in libraries],
                    'sessions': len(sessions), 'active_playback': sum(bool(x.get('NowPlayingItem')) for x in sessions)}
        check('jellyfin_' + str(port), jellyfin)

    def database():
        with closing(sqlite3.connect((BASE/'data/quark_plays.db').as_uri() + '?mode=ro', uri=True, timeout=2)) as con:
            con.row_factory = sqlite3.Row
            status = dict(con.execute('SELECT status, COUNT(*) FROM transfers GROUP BY status').fetchall())
            rows = con.execute('SELECT cat,title,season,episode,strm_path FROM strm_map').fetchall()
            done = {tuple(r) for r in con.execute("SELECT cat,title,season,episode FROM transfers WHERE status='done' AND remote_path IS NOT NULL")}
        files = {}
        for r in rows:
            p = pathlib.Path(r['strm_path'])
            # Never recurse through the rclone mount or follow a path outside cinema2.
            if not p.is_relative_to(BASE/'cinema2'):
                continue
            files.setdefault(p, []).append(tuple(r)[:4])
        counts = collections.Counter()
        for p, identities in files.items():
            if not p.is_file():
                counts['missing'] += 1
                continue
            content = p.read_text().strip()
            if '/pending?' in content:
                counts['pending'] += 1
            elif '5244/d/' in content or '8099/clean?' in content or '8099/play?' in content:
                counts['live'] += 1
                if not any(i in done for i in identities):
                    counts['live_without_done_identity'] += 1
            else:
                counts['other'] += 1
        return {'transfer_status': status, 'mapped_rows': len(rows), 'unique_paths': len(files), 'files': dict(counts)}
    check('state_database', database)
    def modules():
        src = PROJECT/'app/quark_vod'
        dst = BASE/'scripts/quark_vod'
        names = sorted({p.name for p in src.glob('*.py')} | {p.name for p in dst.glob('*.py')})
        return {'count': len(names), 'drift': [n for n in names if not (src/n).exists() or not (dst/n).exists() or (src/n).read_bytes() != (dst/n).read_bytes()]}
    check('module_sync', modules)
    def oom():
        output = command(['journalctl', '-k', '-b', '--no-pager', '-o', 'short-iso'], timeout=30)
        counts = collections.Counter()
        first = {}
        last = {}
        for l in output.splitlines():
            if 'Killed process' in l and ('out of memory' in l.lower() or 'oom' in l.lower()):
                m = re.search(r'Killed process \d+ \(([^)]+)\)', l)
                if m:
                    counts[m.group(1)] += 1
                    first.setdefault(m.group(1), l.split()[0])
                    last[m.group(1)] = l.split()[0]
        return {'scope':'retained kernel journal, current boot', 'processes': {
            n: {'count':counts[n], 'first':first[n], 'last':last[n]} for n in counts}}
    check('oom_history', oom)
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    for k, v in report['checks'].items():
        print(k, v['status'])


if __name__ == '__main__':
    main()
