"""Explicit host-tool configuration; excluded from public distributions."""
import importlib.util
import os
from pathlib import Path
import sys


def load_runtime():
    if 'quark_vod.config' in sys.modules:
        raise RuntimeError('load host runtime before importing VOD configuration')
    source = Path(os.environ.get('HOMETHEATER_SOURCE_DIR', '/home/dong/projects/hometheater'))
    filename = os.environ.get('HOMETHEATER_VOD_RUNTIME_ENV',
                              '/home/dong/projects/hometheater/.vod-runtime.env')
    spec = importlib.util.spec_from_file_location('_vod_host_adapter', source/'scripts/run_vod_host.py')
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    adapter.prepare_environment(filename)
    os.environ.update(AI_PARSE_ENABLED='0', SELFHEAL_ENABLED='0',
                      VOD_STARTUP_SCAN='0', VOD_STARTUP_WRITE_PROBE='0')


def isolate_state(directory, data_dir=None, isolate_library=True):
    root = Path(directory).resolve()
    if not root.is_relative_to(Path('/tmp').resolve()):
        raise ValueError('verification state must be under /tmp')
    data = Path(data_dir or root/'data').resolve()
    if not data.is_relative_to(root):
        raise ValueError('verification data escaped temporary root')
    os.environ.update(VOD_DATA_DIR=str(data), VOD_STATE_DB=str(data/'quark_plays.db'),
                      QUARK_COOKIE_OVERRIDE_FILE=str(data/'quark_cookie.txt'),
                      VOD_LOG_DIR=str(root/'logs'), VOD_LOG_FILE=str(root/'logs/verify.log'),
                      AI_PARSE_ENABLED='0', SELFHEAL_ENABLED='0',
                      VOD_STARTUP_SCAN='0', VOD_STARTUP_WRITE_PROBE='0',
                      VOD_MAINTENANCE_ENABLED='0')
    if isolate_library:
        os.environ['VOD_CINEMA2_DIR'] = str(root/'cinema2')
