"""Run the host-owned shell wrappers against synthetic curl/ffprobe executables."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
# Jellyfin mounts the hometheater wrappers. The old local copy is not deployed
# and must not silently replace the authoritative host integration under test.
SOURCE = ROOT.parent / 'hometheater/custom-ffmpeg'


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='probe-fixture-', dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root/'calls.jsonl'
        probe = self.root/'probe'
        probe.write_text('''#!/usr/bin/python3
import os,sys,json
args=sys.argv[1:]
with open(os.environ['FIXTURE_LOG'],'a') as f:f.write(json.dumps(args)+'\\n')
mode=os.environ.get('FIXTURE_MODE','ok')
if mode=='empty':print('{"streams":[],"format":{}}')
elif mode=='malformed':print('{"streams":[')
else:
 target=args[args.index('-i')+1] if '-i' in args else ''
 print(json.dumps({'streams':[{'codec_type':'video','width':1920 if 'asset=one' in target else 3840},{'codec_type':'attachment'},{'codec_type':'subtitle','codec_name':'ass'}],'format':{'format_name':'matroska'}}))
''')
        probe.chmod(0o700)
        curl = self.root/'curl'
        curl.write_text('''#!/usr/bin/python3
import sys
if '-w' in sys.argv:print('http://fixture.invalid/video?asset=one',end='')
else:sys.stdout.buffer.write(b'plain media bytes')
''')
        curl.chmod(0o700)
        # Change only dependencies; all argument/cache logic remains the real file.
        for name in ('ffprobe', 'ffmpeg'):
            text = (SOURCE/name).read_text()
            text = text.replace('/usr/bin/curl', str(curl))
            text = text.replace('/usr/lib/jellyfin-ffmpeg/ffprobe', str(probe))
            text = text.replace('/usr/lib/jellyfin-ffmpeg/ffmpeg', str(probe))
            text = text.replace('/custom-ffmpeg/bin/jq', '/usr/bin/jq')
            text = text.replace('/cache/ffprobe_cache', str(self.root/'cache'))
            (self.root/name).write_text(text)
        self.env = dict(os.environ, FIXTURE_LOG=str(self.log),
                        FFPROBE_BINARY=str(probe), FFPROBE_JQ='/usr/bin/jq',
                        FFPROBE_CACHE_DIR=str(self.root/'cache'))

    def run_probe(self, target, mode='ok', fmt='-print_format'):
        env = dict(self.env, FIXTURE_MODE=mode)
        r = subprocess.run(['bash', str(self.root/'ffprobe'), '-show_streams', '-show_format', fmt, 'json', '-i', target], capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def calls(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_query_identifies_media(self):
        one = json.loads(self.run_probe('http://fixture.invalid/video?asset=one'))
        two = json.loads(self.run_probe('http://fixture.invalid/video?asset=two'))
        self.assertNotEqual(one['streams'][0]['width'], two['streams'][0]['width'])

    def test_pending_never_cached_after_redirect(self):
        target = 'http://fixture.invalid:8099/pending?t=one'
        self.run_probe(target)
        self.run_probe(target)
        self.assertEqual(len(self.calls()), 2)

    def test_stable_play_never_caches_obsolete_source(self):
        target = 'http://fixture.invalid:8099/play?t=one&r=file'
        self.run_probe(target)
        self.run_probe(target)
        self.assertEqual(len(self.calls()), 2)

    def test_empty_streams_not_cached(self):
        target = 'http://fixture.invalid/video'
        self.run_probe(target, 'empty')
        self.run_probe(target, 'empty')
        self.assertEqual(len(self.calls()), 2)

    def test_malformed_json_never_cached(self):
        self.run_probe('http://fixture.invalid/video', 'malformed')
        self.assertFalse(list((self.root/'cache').glob('*.json')))

    def test_valid_cache_and_subtitle_preservation(self):
        target = 'http://fixture.invalid/video'
        first = json.loads(self.run_probe(target))
        second = json.loads(self.run_probe(target))
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual([x['codec_type'] for x in first['streams']], ['video','subtitle'])

    def test_of_alias_uses_json_cache(self):
        target = 'http://fixture.invalid/video'
        self.run_probe(target, fmt='-of')
        self.run_probe(target, fmt='-of')
        self.assertEqual(len(self.calls()), 1)

    def test_ffmpeg_output_format_and_segment_timing_preserved(self):
        args = ['-f','hls','-i','http://fixture.invalid/video','-f','hls','-hls_time','2','-max_delay','5000','/fixture/out.m3u8']
        r = subprocess.run(['bash', str(self.root/'ffmpeg'), *args], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode,0,r.stderr)
        called = self.calls()[0]
        self.assertNotIn('-f', called[:called.index('-i')])
        self.assertEqual(called[called.index('-hls_time')+1], '2')
        self.assertEqual(called[called.index('-max_delay')+1], '5000')
        self.assertNotIn('-reconnect_at_eof', called)

    def test_ffmpeg_resolves_stable_play_before_seek(self):
        r = subprocess.run(['bash',str(self.root/'ffmpeg'),'-ss','120','-i',
                            'http://fixture.invalid:8099/play?t=one&r=file','-f','null','-'],
                           env=self.env,capture_output=True,text=True,timeout=10)
        self.assertEqual(r.returncode,0,r.stderr)
        called=self.calls()[0]
        self.assertEqual(called[called.index('-i')+1],'http://fixture.invalid/video?asset=one')
        self.assertLess(called.index('-ss'),called.index('-i'))
        self.assertEqual(called[called.index('-ss')+1],'120')


if __name__ == '__main__':
    unittest.main(verbosity=2)
