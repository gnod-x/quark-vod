#!/usr/bin/env python3
"""Scheduled traditional-library sync with bounded logging and single-instance lock."""

from quark_vod.integrations.hometheater import paths
from quark_vod import config as host_config
import datetime
from contextlib import redirect_stdout
import os
import sys
from quark_vod.coordination import file_lock, Busy
from quark_vod.integrations.hometheater.sync_all import run_sync


class Tee:
    def __init__(self, *streams):
        self.streams = streams
    def write(self, value):
        for stream in self.streams:
            stream.write(value)
        return len(value)
    def flush(self):
        for stream in self.streams:
            stream.flush()


def wait_for_scan(timeout=900, interval=5):
    import urllib.request, json, time
    endpoint = paths.TRADITIONAL_API
    key = host_config._secret('JELLYFIN_KEY')
    if not key:
        raise RuntimeError('traditional Jellyfin credential missing')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        request = urllib.request.Request(endpoint + '/ScheduledTasks',
            headers={'Authorization': 'MediaBrowser Token="' + key + '"'})
        with urllib.request.urlopen(request, timeout=8) as response:
            tasks = json.loads(response.read(2 * 1024**2))
        scans = [t for t in tasks if t.get('Key') == 'RefreshLibrary']
        if len(scans) != 1:
            raise RuntimeError('library scan status unknown')
        scan = scans[0]
        if scan.get('State') == 'Idle' and (scan.get('LastExecutionResult') or {}).get('Status') == 'Completed':
            return True
        time.sleep(interval)
    raise RuntimeError('library scan did not complete')


def prune_people():
    # Run separate processes so imported 8097 defaults cannot leak into 8096 work.
    import subprocess
    env = os.environ.copy()
    env['JELLYFIN_API_BASE'] = paths.TRADITIONAL_API
    package = "quark_vod.integrations.hometheater."
    subprocess.run([sys.executable, "-m", package + "people_manager", '--prune-only', '--apply'], env=env, check=True)
    subprocess.run([sys.executable, "-m", package + "skip_manager", '--server', 'jellyfin', 'auto-tag'], env=env, check=True)


def main():
    if not __import__("quark_vod.config", fromlist=["_boolean"])._boolean("VOD_HOST_TRADITIONAL_ENABLED"):
        raise RuntimeError("traditional library integration is disabled")
    os.makedirs(paths.LOG_DIR, exist_ok=True)
    path = os.path.join(paths.LOG_DIR, "sync.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    full = '--full' in sys.argv or '-f' in sys.argv or (datetime.datetime.now().hour == 4 and '--incremental' not in sys.argv)
    try:
        with file_lock('traditional-sync'):
            with open(path, 'a', encoding='utf-8', buffering=1) as logfile:
                try:
                    same = os.fstat(sys.stdout.fileno()).st_ino == os.fstat(logfile.fileno()).st_ino
                except (OSError, AttributeError):
                    same = False
                with redirect_stdout(sys.stdout if same else Tee(sys.stdout, logfile)):
                    print(f'{datetime.datetime.now().isoformat()} sync mode={"full" if full else "incremental"}', flush=True)
                    if run_sync(incremental=not full) is False:
                        raise RuntimeError('traditional library sync failed')
                    if full:
                        wait_for_scan()
                        prune_people()
    except Busy:
        print('已有同步任务，跳过本次执行', flush=True)
        return 75


if __name__ == '__main__':
    raise SystemExit(main())
