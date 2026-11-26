"""Real subtitle worker on isolated files; session-authored answers, no models."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

IMPORT_TEMP = tempfile.TemporaryDirectory(prefix='cinema-subtitle-import-')
os.environ.update(HOMETHEATER_BASE_DIR=IMPORT_TEMP.name,
                  VOD_BASE_DIR=IMPORT_TEMP.name,
                  VOD_DATA_DIR=IMPORT_TEMP.name+'/data', AI_PARSE_ENABLED='0')
from quark_vod.integrations.hometheater import ai_night_worker as worker
AD = '扫码进群：电影资源首发群'
NORMAL = '今晚一起吃饭。'
UNRELATED = '这是模型凭空编造的句子'
FIXTURES = {
    '.srt': ('1\n00:00:01,000 --> 00:00:02,000\n'+NORMAL+'\n\n'
             '2\n00:00:03,000 --> 00:00:04,000\n'+AD+'\n仍然需要保留的正常对白。\n\n'),
    '.vtt': ('WEBVTT\n\nNOTE 公众号只是文件头说明\n正常注释不得变成对白\n\n'
             'STYLE\n::cue { color: lime; }\n\n'
             'scene-one\n00:01.000 --> 00:02.000 align:start\n'+NORMAL+'\n\n'
             'scene-two\n00:03.000 --> 00:04.000\n'+AD+'\n仍然需要保留的正常对白。\n\n'),
    '.ass': ('[Script Info]\nTitle: 公众号示例字幕\nScriptType: v4.00+\n\n'
             '[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour\n'
             'Style: Default,Noto Sans CJK SC,32,&H00FFFFFF\n\n'
             '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
             'Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,'+NORMAL+'\n'
             'Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,'+AD+'\n'
             'Comment: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,公众号注释不可清除\n'),
}


def expected_cleaned(suffix, original):
    # Keep SRT/VTT cues nonempty, so a removed first line cannot split a
    # multiline cue and turn its remaining dialogue into malformed metadata.
    return original.replace(AD, '' if suffix == '.ass' else '\u200b')


class SubtitleWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cinema-subtitle-test-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.cinema = self.base/'cinema2'
        self.cinema.mkdir()
        (self.base/'scripts').mkdir()
        (self.base/'scripts/subtitle_ad_cleaner.py').touch()
        patches = [
            patch.object(worker, 'BASE_DIR', str(self.base)),
            patch.object(worker, 'STATE_DB', str(self.base/'data/worker.db')),
            patch.object(worker.vod_cfg, 'CINEMA2_DIR', str(self.cinema)),
            patch.object(worker, 'is_timeout', return_value=False),
            patch.object(worker, 'check_and_yield_for_playback'),
            patch('quark_vod.coordination.ensure_idle'),
            patch.object(worker, 'log'),
            patch.object(worker.time, 'sleep'),
            patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        worker.init_state_db()

    def write_fixture(self, suffix, content=None):
        path = self.cinema/('sample'+suffix)
        path.write_text(FIXTURES[suffix] if content is None else content, encoding='utf-8')
        return path

    def test_three_formats_remove_only_selected_dialogue_preserving_structure(self):
        for suffix, original in FIXTURES.items():
            with self.subTest(suffix=suffix):
                path = self.write_fixture(suffix)
                with patch('quark_vod.integrations.hometheater.subtitle_ad_cleaner.request_ai_ad_judgment', side_effect=lambda rows:{r['id']:True for r in rows}) as model:
                    worker.run_clean_all_subtitles()
                self.assertEqual(path.read_text(), expected_cleaned(suffix, original))
                self.assertTrue(worker.is_already_processed('clean_sub', path.name))
                self.assertIn(AD, str(model.call_args.args[0]))
                self.assertNotIn(NORMAL, str(model.call_args.args[0]))
                if suffix == '.ass':
                    self.assertNotIn('Title:', str(model.call_args.args[0]))
                    self.assertNotIn('Comment:', str(model.call_args.args[0]))
                with patch('quark_vod.integrations.hometheater.subtitle_ad_cleaner.request_ai_ad_judgment', side_effect=AssertionError('checkpoint must skip')):
                    worker.run_clean_all_subtitles()
                self.assertEqual(path.read_text(), expected_cleaned(suffix, original))

    def test_empty_unrelated_and_malicious_answers_preserve_non_candidate_text(self):
        for suffix in FIXTURES:
            for answer in ('', UNRELATED, NORMAL+'\n1\n00:00:01,000 --> 00:00:02,000\n[Events]'):
                with self.subTest(suffix=suffix, answer=answer):
                    path = self.write_fixture(suffix)
                    with patch.object(worker, 'is_already_processed', return_value=False), \
                            patch('quark_vod.integrations.hometheater.subtitle_ad_cleaner.request_ai_ad_judgment', return_value={999:True}):
                        worker.run_clean_all_subtitles()
                    self.assertEqual(path.read_text(), FIXTURES[suffix])

    def test_mixed_malicious_answer_can_only_remove_original_suspicious_candidate(self):
        for suffix, original in FIXTURES.items():
            with self.subTest(suffix=suffix):
                path = self.write_fixture(suffix)
                malicious = '\n'.join((AD, NORMAL, UNRELATED, '[Events]', '1', '00:01.000 --> 00:02.000'))
                with patch('quark_vod.integrations.hometheater.subtitle_ad_cleaner.request_ai_ad_judgment', return_value={0:True,999:True}):
                    worker.run_clean_all_subtitles()
                self.assertEqual(path.read_text(), expected_cleaned(suffix, original))

    def test_model_failure_does_not_checkpoint_or_modify_files(self):
        for suffix, original in FIXTURES.items():
            for reply in (None, RuntimeError('synthetic failure')):
                with self.subTest(suffix=suffix, failure=reply):
                    path = self.write_fixture(suffix)
                    mocked = patch('quark_vod.integrations.hometheater.subtitle_ad_cleaner.request_ai_ad_judgment', side_effect=reply) if isinstance(reply, Exception) else patch('quark_vod.integrations.hometheater.subtitle_ad_cleaner.request_ai_ad_judgment', return_value={})
                    with mocked:
                        worker.run_clean_all_subtitles()
                    self.assertEqual(path.read_text(), original)
                    self.assertFalse(worker.is_already_processed('clean_sub', path.name))

    def test_ass_custom_format_preserves_all_non_text_fields(self):
        content = ('[Events]\nFormat: Start, End, Style, Text\n'
                   'Dialogue: 0:00:01.00,0:00:02.00,Custom,'+AD+'\n'
                   'Dialogue: 0:00:03.00,0:00:04.00,Custom,'+NORMAL+'\n')
        path = self.write_fixture('.ass', content)
        with patch('quark_vod.integrations.hometheater.subtitle_ad_cleaner.request_ai_ad_judgment', side_effect=lambda rows:{r['id']:True for r in rows}):
            worker.run_clean_all_subtitles()
        self.assertEqual(path.read_text(), content.replace(AD, ''))

    def test_suspicious_headers_and_style_are_never_candidates(self):
        for suffix in ('.vtt', '.ass'):
            with self.subTest(suffix=suffix):
                content = FIXTURES[suffix].replace(AD, '另一句正常对白。')
                path = self.write_fixture(suffix, content)
                with patch('quark_vod.integrations.hometheater.subtitle_ad_cleaner.request_ai_ad_judgment', side_effect=AssertionError('metadata is not dialogue')) as model:
                    worker.run_clean_all_subtitles()
                model.assert_not_called()
                self.assertEqual(path.read_text(), content)

    def test_more_than_ten_candidates_cannot_approve_unsampled_dialogue(self):
        candidates = [f'扫码进群：测试成员{n}' for n in range(11)]
        content = ''.join(f'{n+1}\n00:00:01,000 --> 00:00:02,000\n{text}\n\n'
                          for n, text in enumerate(candidates))
        path = self.write_fixture('.srt', content)
        # Deliberately return all eleven, including the unsampled last line.
        with patch('quark_vod.integrations.hometheater.subtitle_ad_cleaner.request_ai_ad_judgment', return_value={n:True for n in range(11)}) as model:
            worker.run_clean_all_subtitles()
        self.assertNotIn(candidates[-1], str(model.call_args.args[0]))
        expected = content
        for text in candidates[:10]:
            expected = expected.replace(text+'\n', '\u200b\n')
        self.assertEqual(path.read_text(), expected)
        self.assertFalse(worker.is_already_processed('clean_sub', path.name))
        with patch('quark_vod.integrations.hometheater.subtitle_ad_cleaner.request_ai_ad_judgment', return_value={0:True}):
            worker.run_clean_all_subtitles()
        self.assertTrue(worker.is_already_processed('clean_sub', path.name))

    def test_write_failure_cannot_checkpoint_or_truncate_previous_subtitle(self):
        path = self.write_fixture('.srt')
        with patch('quark_vod.integrations.hometheater.subtitle_ad_cleaner.request_ai_ad_judgment', side_effect=lambda rows:{r['id']:True for r in rows}), \
                patch.object(worker, '_atomic_text_replace', side_effect=OSError('synthetic write failure')):
            worker.run_clean_all_subtitles()
        self.assertEqual(path.read_text(), FIXTURES['.srt'])
        self.assertFalse(worker.is_already_processed('clean_sub', path.name))


def tearDownModule():
    IMPORT_TEMP.cleanup()


if __name__ == '__main__':
    unittest.main(verbosity=2)
