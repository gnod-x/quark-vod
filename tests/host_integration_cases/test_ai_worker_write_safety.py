"""Local writes and subtitle structure, using disposable data and no network."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory(prefix='cinema-write-safety-')
os.environ.update(HOMETHEATER_BASE_DIR=TEMP.name, VOD_BASE_DIR=TEMP.name,
                  VOD_DATA_DIR=TEMP.name+'/data', AI_PARSE_ENABLED='0')
from quark_vod.integrations.hometheater import ai_night_worker as worker
class WriteSafetyTests(unittest.TestCase):
    def setUp(self):
        guard = patch("quark_vod.coordination.ensure_idle")
        guard.start()
        self.addCleanup(guard.stop)

    def test_failed_atomic_commit_preserves_original_and_removes_temporary(self):
        root = Path(TEMP.name)/'atomic'
        root.mkdir(exist_ok=True)
        path = root/'movie.nfo'
        path.write_text('<movie/>')
        path.chmod(0o640)
        with patch.object(worker.os, 'replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                worker._atomic_text_replace(str(path), '<movie><plot>new</plot></movie>')
        self.assertEqual(path.read_text(), '<movie/>')
        self.assertEqual(list(root.iterdir()), [path])
        worker._atomic_text_replace(str(path), 'complete')
        self.assertEqual(path.read_text(), 'complete')
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_srt_multiline_cue_keeps_index_timestamp_and_ordinary_dialogue(self):
        lines = ['1\n', '00:00:01,000 --> 00:00:04,000\n', '扫码进群\n', '普通对白\n', '\n']
        dialogue = worker._subtitle_dialogue_lines(lines, '.srt')
        self.assertEqual(dialogue, {2:'扫码进群', 3:'普通对白'})
        result, count = worker._blank_subtitle_dialogue(lines, dialogue, {'扫码进群'}, '.srt')
        self.assertEqual(count, 1)
        self.assertEqual(result[:2], lines[:2])
        self.assertEqual(result[3:], lines[3:])
        self.assertTrue(result[2].strip())
        self.assertEqual(worker._subtitle_dialogue_lines(result, '.srt')[3], '普通对白')

    def test_ass_preserves_style_comments_and_comma_in_text(self):
        lines = ['[V4+ Styles]\n', 'Format: Name, Fontname\n', 'Style: 广告,Arial\n',
                 '[Events]\n', 'Format: Layer, Start, End, Text\n',
                 'Comment: 0,0:00:01.00,0:00:03.00,扫码进群\n',
                 'Dialogue: 0,0:00:01.00,0:00:03.00,公众号,广告\n']
        dialogue = worker._subtitle_dialogue_lines(lines, '.ass')
        self.assertEqual(dialogue, {6:'公众号,广告'})
        result, count = worker._blank_subtitle_dialogue(lines, dialogue, {'公众号,广告'}, '.ass')
        self.assertEqual(count, 1)
        self.assertEqual(result[:6], lines[:6])
        self.assertEqual(result[6], 'Dialogue: 0,0:00:01.00,0:00:03.00,\n')

    def test_webvtt_header_and_style_are_not_dialogue(self):
        lines = ['WEBVTT\n', '\n', 'STYLE\n', '::cue { color: red; }\n', '\n',
                 'cue-1\n', '00:01.000 --> 00:03.000 align:start\n', '扫码进群\n', '\n']
        self.assertEqual(worker._subtitle_dialogue_lines(lines, '.vtt'), {7:'扫码进群'})

    def test_acl_and_xattrs_are_preserved(self):
        import subprocess
        with tempfile.TemporaryDirectory(dir=TEMP.name) as directory:
            path = Path(directory)/'nfo'
            path.write_text('old')
            os.setxattr(path, 'user.verification', b'keep')
            subprocess.run(['setfacl', '-m', 'u:12345:r--', str(path)], check=True, capture_output=True)
            before = {n:os.getxattr(path,n) for n in os.listxattr(path)}
            old_stat = path.stat()
            worker._atomic_text_replace(str(path), 'new')
            self.assertEqual(before, {n:os.getxattr(path,n) for n in os.listxattr(path)})
            self.assertEqual((path.stat().st_uid,path.stat().st_gid,path.stat().st_mode),
                             (old_stat.st_uid,old_stat.st_gid,old_stat.st_mode))

    def test_metadata_copy_failure_preserves_original(self):
        with tempfile.TemporaryDirectory(dir=TEMP.name) as directory:
            path = Path(directory)/'nfo'
            path.write_text('old')
            os.setxattr(path, 'user.verification', b'keep')
            with patch.object(worker.os, 'setxattr', side_effect=PermissionError('cannot preserve')):
                with self.assertRaises(PermissionError):
                    worker._atomic_text_replace(str(path), 'new')
            self.assertEqual(path.read_text(), 'old')
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_owner_preservation_failure_aborts_commit(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory(dir=TEMP.name) as directory:
            path = Path(directory)/'nfo'
            path.write_text('old')
            fake = SimpleNamespace(st_uid=path.stat().st_uid+1, st_gid=path.stat().st_gid)
            with patch.object(worker.os,'fstat',return_value=fake), \
                 patch.object(worker.os,'fchown',side_effect=PermissionError('owner denied')):
                with self.assertRaises(PermissionError):
                    worker._atomic_text_replace(str(path),'new')
            self.assertEqual(path.read_text(),'old')
            self.assertEqual(list(Path(directory).iterdir()),[path])

    def test_changed_source_aborts_commit(self):
        with tempfile.TemporaryDirectory(dir=TEMP.name) as directory:
            path = Path(directory)/'nfo'
            path.write_text('old')
            original = worker.os.fsync
            def concurrent_write(fd):
                path.write_text('concurrent update')
                original(fd)
            with patch.object(worker.os, 'fsync', side_effect=concurrent_write):
                with self.assertRaises(RuntimeError):
                    worker._atomic_text_replace(str(path), 'new')
            self.assertEqual(path.read_text(), 'concurrent update')

    def test_invalid_encoding_preserved_and_crlf_survives_valid_clean(self):
        with tempfile.TemporaryDirectory(dir=TEMP.name) as directory:
            base = Path(directory)
            (base/'scripts').mkdir()
            (base/'scripts/subtitle_ad_cleaner.py').touch()
            cinema = base/'cinema2'
            cinema.mkdir()
            path = cinema/'bad.srt'
            invalid = b'1\r\n00:00:01,000 --> 00:00:03,000\r\n'+ '公众号'.encode()+b'\xff\r\n'
            path.write_bytes(invalid)
            with patch.object(worker,'BASE_DIR',str(base)), patch.object(worker.vod_cfg,'CINEMA2_DIR',str(cinema)), \
                 patch.object(worker,'is_timeout',return_value=False), patch.object(worker,'check_and_yield_for_playback'), \
                 patch.object(worker,'is_already_processed',return_value=False), patch.object(worker,'mark_processed') as marked, \
                 patch('quark_vod.integrations.hometheater.subtitle_ad_cleaner.request_ai_ad_judgment',side_effect=lambda rows: {x['id']:True for x in rows}) as model, \
                 patch.object(worker,'log'), patch.object(worker.time,'sleep'):
                worker.run_clean_all_subtitles()
                self.assertEqual(path.read_bytes(),invalid)
                model.assert_not_called()
                marked.assert_not_called()
                valid='1\r\n00:00:01,000 --> 00:00:03,000\r\n扫码进群\r\n普通对白\r\n\r\n'
                path.write_bytes(valid.encode())
                worker.run_clean_all_subtitles()
                self.assertEqual(path.read_bytes(),valid.replace('扫码进群','\u200b').encode())


if __name__ == '__main__':
    unittest.main()
