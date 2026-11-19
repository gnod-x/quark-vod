#!/usr/bin/env python3
"""Check both directory membership and bytes. Never sync or delete anything."""
import argparse
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]


def differences(source, destination):
    names = {p.name for p in source.glob('*.py')} | {p.name for p in destination.glob('*.py')}
    return sorted(n for n in names if not (source/n).is_file() or not (destination/n).is_file()
                  or (source/n).read_bytes() != (destination/n).read_bytes())


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source', type=Path, default=PROJECT/'app/quark_vod')
    ap.add_argument('--destination', type=Path, default=PROJECT.parent/'hometheater/scripts/quark_vod')
    args = ap.parse_args()
    if not args.source.is_dir() or not args.destination.is_dir():
        print('FAIL: missing source or destination')
        return 1
    drift = differences(args.source, args.destination)
    for n in drift:
        print('FAIL: module drift:', n)
    if not drift:
        print('PASS: all module names and bytes match')
    return bool(drift)


if __name__ == '__main__':
    sys.exit(main())
