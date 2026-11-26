#!/usr/bin/env python3
"""Run one bounded chase and emit a result attributable to its run window."""

from quark_vod.integrations.hometheater import paths
from quark_vod import config as host_config
import datetime as dt
import json

from quark_vod.integrations.hometheater.vod_automation import airing_chase
from quark_vod import db


def main():
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        try:
            result = airing_chase()
            failed = bool(result['errors'])
        finally:
            db.close()
    except Exception as exc:
        print(json.dumps({
            'task': 'airing_chase',
            'started_at': started_at,
            'finished_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'status': 'failed',
            'error_type': type(exc).__name__,
        }, ensure_ascii=False), flush=True)
        raise

    print(json.dumps({
        **result,
        'task': 'airing_chase',
        'started_at': started_at,
        'finished_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'status': 'partial' if failed else 'complete',
    }, ensure_ascii=False), flush=True)
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
