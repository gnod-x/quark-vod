#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线验证内容过滤、已播占位和片库保护；仅写 TemporaryDirectory。"""

import os
import json
import sys
import datetime
import shutil
import sqlite3
import subprocess
import unittest
import urllib.request

import tempfile
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory(prefix="cinema-jobs-")
BASE_DIR = TEMP.name
os.environ.update(HOMETHEATER_BASE_DIR=BASE_DIR, VOD_BASE_DIR=BASE_DIR,
                  VOD_DATA_DIR=BASE_DIR + "/data",
                  VOD_STATE_DB=BASE_DIR + "/data/quark_plays.db", AI_PARSE_ENABLED="0")
NETWORK = patch("urllib.request.urlopen", side_effect=AssertionError("offline test attempted network"))
NETWORK.start()

from quark_vod import config as vod_cfg
from quark_vod import db as vod_db
from quark_vod import coordination
from quark_vod.integrations.hometheater import tmdb_populate
from quark_vod.integrations.hometheater import daily_update_and_prune
from quark_vod.integrations.hometheater import purge_orphan_metadata
class TestDailyPruneAndPopulate(unittest.TestCase):

    def setUp(self):
        idle = patch.object(coordination, 'ensure_idle')
        idle.start()
        self.addCleanup(idle.stop)
        root=tempfile.TemporaryDirectory(dir=BASE_DIR)
        self.addCleanup(root.cleanup)
        path=Path(root.name)/'jellyfin.db'
        database=patch.object(daily_update_and_prune,'JF_DB',str(path))
        database.start();self.addCleanup(database.stop)
        state_database = patch.object(daily_update_and_prune, 'QUARK_DB', vod_cfg.STATE_DB)
        state_database.start();self.addCleanup(state_database.stop)
        # Protection is fail-closed: create the real temporary state schema
        # explicitly instead of depending on another test's lazy DB access.
        vod_db.init_db()
        with sqlite3.connect(path) as con:
            con.executescript('CREATE TABLE IF NOT EXISTS BaseItems(Id TEXT, Type TEXT, Name TEXT, SeriesName TEXT, Path TEXT, Tags TEXT);'
                'CREATE TABLE IF NOT EXISTS UserData(ItemId TEXT, IsFavorite INTEGER, Played INTEGER, PlayCount INTEGER, PlaybackPositionTicks INTEGER);')

    def test_cron_skips_when_other_maintenance_is_running(self):
        import fcntl
        Path(daily_update_and_prune.DATA_DIR).mkdir(parents=True,exist_ok=True)
        with open(Path(daily_update_and_prune.DATA_DIR)/'vod_maintenance.lock','a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with patch.object(daily_update_and_prune,'run_pruning') as work:
                daily_update_and_prune.main()
            work.assert_not_called()



    def test_populate_preserves_play_and_clean_sources(self):
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as temp:
            path=Path(temp)/'target.strm'
            for value in ('http://localhost:8099/play?cat=x&t=y', 'http://localhost:8099/clean?url=x', 'http://localhost:5244/d/file'):
                path.write_text(value)
                self.assertFalse(tmdb_populate.write_pending_strm(str(path),'movie','title',0,0))
                self.assertEqual(path.read_text(),value)

    def test_protection_failure_stops_pruning(self):
        with patch.object(daily_update_and_prune.os.path,'exists',return_value=True),patch.object(daily_update_and_prune.sqlite3,'connect',side_effect=sqlite3.OperationalError('unavailable')):
            with self.assertRaises(RuntimeError):daily_update_and_prune.get_protected_titles()

    def test_episode_user_protection_requires_a_series_identity(self):
        for series_name in (None, '', '   ', '用户在看的剧'):
            with self.subTest(series_name=series_name),tempfile.TemporaryDirectory(dir=BASE_DIR) as root:
                path=Path(root)/'jellyfin.db'
                con=sqlite3.connect(path)
                try:
                    con.executescript('CREATE TABLE BaseItems(Id TEXT, Type TEXT, Name TEXT, SeriesName TEXT);'
                        'CREATE TABLE UserData(ItemId TEXT, IsFavorite INTEGER, Played INTEGER, PlayCount INTEGER, PlaybackPositionTicks INTEGER);')
                    con.execute('INSERT INTO BaseItems VALUES (?,?,?,?)',('episode','MediaBrowser.Controller.Entities.TV.Episode','第一集',series_name))
                    con.execute('INSERT INTO UserData VALUES (?,?,?,?,?)',('episode',0,0,1,10000))
                    con.commit()
                finally:con.close()
                with patch.object(daily_update_and_prune,'JF_DB',str(path)), \
                     patch.object(daily_update_and_prune,'QUARK_DB',vod_cfg.STATE_DB), \
                     patch.object(vod_db,'get_all_pinned_titles',return_value=set()):
                    if not series_name or not series_name.strip():
                        with self.assertRaises(RuntimeError):daily_update_and_prune.get_protected_titles()
                    else:
                        self.assertIn(series_name,daily_update_and_prune.get_protected_titles())

    def test_missing_protection_database_is_not_an_empty_protection_set(self):
        with tempfile.TemporaryDirectory(dir=BASE_DIR) as root:
            for setting in ('JF_DB','QUARK_DB'):
                missing=Path(root)/(setting+'.db')
                with self.subTest(database=setting),patch.object(daily_update_and_prune,setting,str(missing)):
                    with self.assertRaises(RuntimeError):daily_update_and_prune.get_protected_titles()
                self.assertFalse(missing.exists())

    def test_map_removal_uses_directory_boundary_and_literal_percent(self):
        prefix=os.path.join(vod_cfg.CINEMA2_DIR,'movie','name%_')
        vod_db.save_strm_map('movie','literal',0,0,prefix+'/file.strm')
        vod_db.save_strm_map('movie','similar',0,0,prefix+'sequel/file.strm')
        vod_db.remove_strm_maps_below(prefix)
        self.assertIsNone(vod_db.get_strm_path('movie','literal',0,0))
        self.assertIsNotNone(vod_db.get_strm_path('movie','similar',0,0))

    def test_ai_filter_does_not_delete_already_saved_video(self):
        path=Path(vod_cfg.CINEMA2_DIR)/'_test_'/'AI saved'
        path.mkdir(parents=True,exist_ok=True)
        (path/'video.strm').write_text('http://localhost:8099/play?cat=_test_&t=AI+saved')
        try:
            with patch.object(tmdb_populate,'is_ai_directory',return_value=(True,'fixture')),patch.object(tmdb_populate,'read_tmdb_key',return_value='synthetic'):
                daily_update_and_prune.prune_ai_shows()
            self.assertTrue((path/'video.strm').exists())
        finally:shutil.rmtree(path)

    def test_03_lru_prune_safety(self):
        """测试 3: 验证 LRU 淘汰安全保护机制（已转存的正片与收藏影视绝对不能被删除）"""
        test_cat = "_测试专区_"
        test_cat_dir = os.path.join(vod_cfg.CINEMA2_DIR, test_cat)
        os.makedirs(test_cat_dir, exist_ok=True)
        try:
            # 1. 模拟已转存直链影片 (Live)
            done_dir = os.path.join(test_cat_dir, "已转存影片 (2026)")
            os.makedirs(done_dir, exist_ok=True)
            with open(os.path.join(done_dir, "已转存影片 (2026).strm"), "w") as f:
                f.write("http://127.0.0.1:5244/d/夸克/点播/test.mp4\n")

            # 2. 模拟收藏保护影片
            fav_dir = os.path.join(test_cat_dir, "用户收藏影片 (2026)")
            os.makedirs(fav_dir, exist_ok=True)
            with open(os.path.join(fav_dir, "用户收藏影片 (2026).strm"), "w") as f:
                f.write(f"{vod_cfg.PENDING_BASE_URL}/pending?cat=test&t=fav\n")

            # 3. 模拟纯未转存旧占位影片 (待淘汰)
            old_dir = os.path.join(test_cat_dir, "冷门旧片 (2020)")
            os.makedirs(old_dir, exist_ok=True)
            with open(os.path.join(old_dir, "冷门旧片 (2020).strm"), "w") as f:
                f.write(f"{vod_cfg.PENDING_BASE_URL}/pending?cat=test&t=old\n")
            # 人为设置旧时间
            os.utime(old_dir, (1000000000, 1000000000))

            protected = {"用户收藏影片"}
            # 设置配额上限为 2，超出 1 部应触发淘汰
            pruned = daily_update_and_prune.prune_category(test_cat, protected, limit=2)

            self.assertEqual(pruned, 1, "应淘汰 1 部冷门旧片")
            self.assertTrue(os.path.exists(done_dir), "已转存 live 正片绝对不能被删除")
            self.assertTrue(os.path.exists(fav_dir), "收藏保护影片绝对不能被删除")
            self.assertFalse(os.path.exists(old_dir), "冷门旧片应已被安全移除")
        finally:
            if os.path.exists(test_cat_dir):
                shutil.rmtree(test_cat_dir)


    def test_05_ai_drama_filtering_and_blocking(self):
        """测试 5: 验证 AI剧/AIGC 劣质生成物与微短剧全链路阻断与精准识别"""
        # 1. 语义、关键词、制作机构与超限形态阻断测试
        ai_item_1 = {"name": "时刻热血短剧", "overview": "唐季礼入局AI短剧赛道"}
        ai_item_2 = {"name": "普通仙侠剧", "overview": "正常剧情", "keywords": {"results": [{"id": 374887, "name": "ai generated"}]}}
        ai_item_3 = {"name": "斑马思维.Zebra.Logic.S4", "overview": "在线AI课程"}
        ai_micro_drama = {"name": "他深情侵入", "origin_country": ["CN"], "keywords": {"results": [{"id": 383994, "name": "micro drama"}]}}
        ai_hongguo = {"name": "修仙界搞回收", "origin_country": ["CN"]}
        ai_hongguo_detail = {"name": "修仙界搞回收", "networks": [{"name": "Hong Guo Short Drama"}], "number_of_episodes": 136}
        ai_high_ep = {"name": "红妆刃", "origin_country": ["CN"]}
        ai_high_ep_detail = {"name": "红妆刃", "networks": [{"name": "Tencent Video"}], "number_of_episodes": 68}

        # 正常影视（坚决防误伤）
        normal_item = {"name": "王道浪漫剧", "overview": "讲述纯爱少男少女的故事", "poster_path": "/test.jpg"}
        normal_scifi = {"name": "克拉拉与太阳 (2026)", "overview": "探讨人型机器人与爱的科幻大片", "keywords": {"results": [{"id": 310, "name": "artificial intelligence (a.i.)"}]}}
        normal_cctv = {"name": "伟大的长征", "origin_country": ["CN"]}
        normal_cctv_detail = {"name": "伟大的长征", "networks": [{"name": "CCTV-1"}], "number_of_episodes": 60}
        normal_web = {"name": "兰香如故", "origin_country": ["CN"]}
        normal_web_detail = {"name": "兰香如故", "networks": [{"name": "Tencent Video"}], "number_of_episodes": 47}

        self.assertTrue(tmdb_populate.is_ai_content(ai_item_1), "应识别 AI短剧 文本特征")
        self.assertTrue(tmdb_populate.is_ai_content(ai_item_2), "应识别 TMDB 374887 ai generated 关键词")
        self.assertTrue(tmdb_populate.is_ai_content(ai_item_3), "应识别 斑马/在线AI课程 等劣质噪音")
        self.assertTrue(tmdb_populate.is_ai_content(ai_micro_drama, cat="国产剧集"), "应识别 国产微短剧 关键词")
        self.assertTrue(tmdb_populate.is_ai_content(ai_hongguo, detail=ai_hongguo_detail, cat="国产动漫"), "应识别 红果短剧 平台")
        self.assertTrue(tmdb_populate.is_ai_content(ai_high_ep, detail=ai_high_ep_detail, cat="国产剧集"), "应拦截 非央视/非卫视 >55集 国产微短剧")

        self.assertFalse(tmdb_populate.is_ai_content(normal_item), "不应误杀普通浪漫剧")
        self.assertFalse(tmdb_populate.is_ai_content(normal_scifi, cat="欧美大片"), "不应误杀欧美正规AI题材科幻大片")
        self.assertFalse(tmdb_populate.is_ai_content(normal_cctv, detail=normal_cctv_detail, cat="国产剧集"), "不应误杀央视上星重大革命历史剧")
        self.assertFalse(tmdb_populate.is_ai_content(normal_web, detail=normal_web_detail, cat="国产剧集"), "不应误杀47集正规古装剧")

        # 2. is_blocked 阻断测试
        cfg = tmdb_populate.CATEGORIES["国产剧集"]
        self.assertTrue(tmdb_populate.is_blocked(cfg, ai_item_1, cat="国产剧集"), "is_blocked 必须拦截 AI短剧")
        self.assertFalse(tmdb_populate.is_blocked(cfg, normal_item, cat="国产剧集"), "is_blocked 不应拦截正常影片")

        # 3. 本地模拟目录检测与淘汰测试
        test_cat = "_测试专区_"
        test_cat_dir = os.path.join(vod_cfg.CINEMA2_DIR, test_cat)
        ai_dir = os.path.join(test_cat_dir, "全AI生成测试短剧")
        os.makedirs(ai_dir, exist_ok=True)
        try:
            with open(os.path.join(ai_dir, "tvshow.nfo"), "w", encoding="utf-8") as f:
                f.write("<tvshow><title>全AI生成测试短剧</title><plot>本剧采用全AI生成制作</plot></tvshow>")
            is_ai, reason = tmdb_populate.is_ai_directory(ai_dir)
            self.assertTrue(is_ai, "应检测出本地模拟 AI 剧目录")
        finally:
            if os.path.exists(test_cat_dir):
                shutil.rmtree(test_cat_dir)

    def test_06_unaired_episodes_pruning_and_prevention(self):
        """测试 6: 验证连载剧集未来未播出集数判定与 pending 占位智能清理机制"""
        import datetime
        today_str = datetime.date.today().isoformat()
        yesterday_str = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        future_str = (datetime.date.today() + datetime.timedelta(days=7)).isoformat()

        # 1. 验证 is_episode_aired 函数逻辑
        detail_mock = {
            "status": "Returning Series",
            "last_episode_to_air": {"season_number": 1, "episode_number": 5, "air_date": yesterday_str}
        }
        self.assertTrue(tmdb_populate.is_episode_aired({"air_date": yesterday_str}, 1, 5, detail_mock, today_str))
        self.assertFalse(tmdb_populate.is_episode_aired({"air_date": future_str}, 1, 6, detail_mock, today_str))
        self.assertTrue(tmdb_populate.is_episode_aired({}, 1, 4, detail_mock, today_str))
        self.assertFalse(tmdb_populate.is_episode_aired({}, 1, 7, detail_mock, today_str))

        # 2. 模拟本地目录清理测试
        test_cat = "国产剧集"
        test_show = "_连载未播测试剧_"
        show_dir = os.path.join(vod_cfg.CINEMA2_DIR, test_cat, test_show)
        s1_dir = os.path.join(show_dir, "Season 01")
        os.makedirs(s1_dir, exist_ok=True)

        try:
            # 创建 tvshow.nfo (连载中)
            with open(os.path.join(show_dir, "tvshow.nfo"), "w", encoding="utf-8") as f:
                f.write(f"<tvshow><title>{test_show}</title><status>Returning Series</status></tvshow>")

            # E01: 过去已播出，pending 占位 -> 必须保留
            with open(os.path.join(s1_dir, f"{test_show} - S01E01.strm"), "w") as f:
                f.write(f"{vod_cfg.PENDING_BASE_URL}/pending?cat=test&t=show&s=1&e=1\n")
            with open(os.path.join(s1_dir, f"{test_show} - S01E01.nfo"), "w") as f:
                f.write(f"<episodedetails><title>第1集</title><aired>{yesterday_str}</aired></episodedetails>")

            # E02: 未来未播出，pending 占位 -> 必须清理
            with open(os.path.join(s1_dir, f"{test_show} - S01E02.strm"), "w") as f:
                f.write(f"{vod_cfg.PENDING_BASE_URL}/pending?cat=test&t=show&s=1&e=2\n")
            with open(os.path.join(s1_dir, f"{test_show} - S01E02.nfo"), "w") as f:
                f.write(f"<episodedetails><title>第2集</title><aired>{future_str}</aired></episodedetails>")

            # E03: 未来未播出，但用户已转存为真实 live 直链 -> 必须受到铁律保护，绝不能删
            with open(os.path.join(s1_dir, f"{test_show} - S01E03.strm"), "w") as f:
                f.write("http://127.0.0.1:5244/d/夸克/点播/test_live.mp4\n")
            with open(os.path.join(s1_dir, f"{test_show} - S01E03.nfo"), "w") as f:
                f.write(f"<episodedetails><title>第3集</title><aired>{future_str}</aired></episodedetails>")

            pruned = daily_update_and_prune.prune_unaired_pending_strms(dry_run=False)

            self.assertTrue(os.path.exists(os.path.join(s1_dir, f"{test_show} - S01E01.strm")), "已播出集数必须保留")
            self.assertFalse(os.path.exists(os.path.join(s1_dir, f"{test_show} - S01E02.strm")), "未来未播出 pending 占位必须被清理")
            self.assertFalse(os.path.exists(os.path.join(s1_dir, f"{test_show} - S01E02.nfo")), "未来未播出 nfo 必须被同步清理")
            self.assertTrue(os.path.exists(os.path.join(s1_dir, f"{test_show} - S01E03.strm")), "真实转存 live 正片必须受保护")
        finally:
            if os.path.exists(show_dir):
                shutil.rmtree(show_dir)

class TestJobEntrypoints(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(coordination, 'ensure_idle'))
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=BASE_DIR)))
        self.cinema = self.root / 'cinema2'
        self.cinema.mkdir()
        self.state = self.root / 'tmdb-state.json'
        for module in (tmdb_populate, daily_update_and_prune):
            self.stack.enter_context(patch.object(module, 'CINEMA2', str(self.cinema)))
            self.stack.enter_context(patch.object(module, 'log'))
        self.stack.enter_context(patch('quark_vod.integrations.hometheater.vod_automation.gate_media', side_effect=lambda item,detail,cat,title,kind,targets: ({s:set(eps) for s,eps in targets.items()}, True)))
        self.stack.enter_context(patch.object(tmdb_populate, 'STATE_FILE', str(self.state)))
        self.stack.enter_context(patch.object(tmdb_populate, 'LOG_DIR', str(self.root / 'logs')))
        self.stack.enter_context(patch.object(daily_update_and_prune, 'DATA_DIR', str(self.root / 'data')))
        self.stack.enter_context(patch.object(tmdb_populate, 'read_tmdb_key', return_value='synthetic'))
        self.stack.enter_context(patch.object(tmdb_populate, 'download_poster', return_value=False))
        self.stack.enter_context(patch.object(tmdb_populate, 'download_fanart', return_value=False))
        self.stack.enter_context(patch.object(tmdb_populate.time, 'sleep'))
        self.stack.enter_context(patch.object(tmdb_populate, 'CATEGORIES', {
            'movies': {'media': 'movie', 'kind': 'movie', 'params': ''},
            'series': {'media': 'tv', 'kind': 'series', 'params': ''}}))

    def item(self, ident, series=False):
        return {'id': ident, 'name' if series else 'title': f'测试作品{ident}',
                'poster_path': '/synthetic.jpg',
                'first_air_date' if series else 'release_date': f'{tmdb_populate.CUR_YEAR}-01-01'}

    def test_main_pages_past_existing_titles_and_registers_new_movie(self):
        old = self.cinema / 'movies' / '原译名'
        old.mkdir(parents=True)
        (old / 'movie.nfo').write_text('<movie><tmdbid>1</tmdbid></movie>')
        pages = []
        def api(url):
            if '/discover/' in url:
                page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)['page'][0])
                pages.append(page)
                return {'results': [self.item(page)], 'total_pages': 8}
            return {'id': 2, 'runtime': 90}
        with patch.object(tmdb_populate, 'http_json', side_effect=api):
            self.assertEqual(tmdb_populate.main(['--cats', 'movies', '--limit', '1']), 0)
        self.assertEqual(pages, [1, 2])
        self.assertEqual(len(list(self.cinema.rglob('*.strm'))), 1)
        state = json.loads(self.state.read_text())
        self.assertEqual(state['last_new'], 1)
        self.assertEqual(state['categories']['movies']['pages'], 2)

    def test_main_has_a_hard_page_budget_for_filtered_results(self):
        pages = []
        def api(url):
            pages.append(url)
            return {'results': [dict(self.item(1), adult=True)], 'total_pages': 10000}
        with patch.object(tmdb_populate, 'http_json', side_effect=api), \
             patch.object(tmdb_populate, 'is_blocked', return_value=True):
            self.assertEqual(tmdb_populate.main(['--cats', 'movies', '--max-pages', '2']), 0)
        self.assertEqual(len(pages), 2)
        self.assertEqual(json.loads(self.state.read_text())['categories']['movies']['pages'], 2)

    def test_main_counts_multiple_series_without_overwriting_accumulator(self):
        def api(url):
            path = urllib.parse.urlsplit(url).path
            if '/discover/' in path:
                return {'results': [self.item(2, True), self.item(3, True)], 'total_pages': 1}
            ident = int(path.split('/tv/')[1].split('/')[0])
            if '/season/' in path:
                return {'episodes': [{'episode_number': e, 'air_date': '2020-01-01'}
                                     for e in range(1, ident + 1)]}
            return {'id': ident, 'status': 'Ended', 'seasons': [{'season_number': 1, 'episode_count': ident}]}
        with patch.object(tmdb_populate, 'http_json', side_effect=api):
            self.assertEqual(tmdb_populate.main(['--cats', 'series']), 0)
        self.assertEqual(len(list(self.cinema.rglob('*.strm'))), 5)
        self.assertEqual(json.loads(self.state.read_text())['last_eps'], 5)

    def test_main_failure_is_nonzero_and_season_failure_is_retryable(self):
        responses = [
            {'results': [self.item(2, True)], 'total_pages': 1},
            {'id': 2, 'status': 'Ended', 'seasons': [{'season_number': 1, 'episode_count': 1}]},
            TimeoutError('synthetic secret must not enter report')]
        with patch.object(tmdb_populate, 'http_json', side_effect=responses):
            self.assertEqual(tmdb_populate.main(['--cats', 'series']), 1)
        self.assertFalse(list(self.cinema.rglob('*.nfo')))
        state = json.loads(self.state.read_text())
        self.assertEqual(state['status'], 'partial')
        self.assertEqual(state['errors'][0]['stage'], 'season')
        self.assertNotIn('synthetic secret', self.state.read_text())
        responses[-1] = {'episodes': [{'episode_number': 1, 'air_date': '2020-01-01'}]}
        with patch.object(tmdb_populate, 'http_json', side_effect=responses):
            self.assertEqual(tmdb_populate.main(['--cats', 'series']), 0)
        self.assertEqual(len(list(self.cinema.rglob('*.strm'))), 1)

    def test_main_discover_and_detail_failure_are_not_success(self):
        for responses in ([TimeoutError()], [{'results': [self.item(1)], 'total_pages': 1},
                                             {'success': False, 'status_code': 7}]):
            with self.subTest(responses=responses), patch.object(tmdb_populate, 'http_json', side_effect=responses):
                self.assertEqual(tmdb_populate.main(['--cats', 'movies']), 1)
            self.assertFalse(list(self.cinema.rglob('*.strm')))

    def test_full_keeps_existing_identity_and_live_source_after_title_change(self):
        old = self.cinema / 'movies' / '旧译名 (2026)'
        old.mkdir(parents=True)
        (old / 'movie.nfo').write_text('<movie><tmdbid>1</tmdbid></movie>')
        live = old / '旧译名 (2026).strm'
        live.write_text('http://localhost:8099/play?cat=movies&t=old')
        with patch.object(tmdb_populate, 'http_json', side_effect=[
                {'results': [self.item(1)], 'total_pages': 1}, {'id': 1, 'runtime': 90}]):
            self.assertEqual(tmdb_populate.main(['--cats', 'movies', '--full']), 0)
        self.assertEqual(list((self.cinema / 'movies').iterdir()), [old])
        self.assertIn('/play?', live.read_text())
        self.assertEqual(json.loads(self.state.read_text())['last_eps'], 0)

    def make_show(self, title='测试剧 (2026)'):
        show = self.cinema / '国产剧集' / title
        season = show / 'Season 01'
        season.mkdir(parents=True)
        (show / 'tvshow.nfo').write_text('<tvshow><status>Returning Series</status><tmdbid>9</tmdbid></tvshow>')
        return show, season

    def test_future_prune_retains_live_unknown_unreadable_and_unknown_dates(self):
        show, season = self.make_show()
        pending = vod_cfg.PENDING_BASE_URL + '/pending?cat=test&t=show&s=1&e=1'
        future = (datetime.date.today() + datetime.timedelta(days=7)).isoformat()
        sources = [pending, 'http://localhost:8099/play?x=1', 'http://localhost:8099/clean?x=1',
                   'http://localhost:5244/d/test', 'unknown', pending, pending, pending,
                   vod_cfg.PENDING_BASE_URL + '/pending-other?x=1']
        for i, source in enumerate(sources):
            (season / f'{i}.strm').write_text(source)
            (season / f'{i}.nfo').write_text(f'<episodedetails><aired>{future if i != 6 else ""}</aired></episodedetails>')
        (season / '7.nfo').write_text('invalid XML')
        real_open = open
        def guarded_open(path, *args, **kwargs):
            if Path(path) == season / '5.strm':
                raise PermissionError('fixture')
            return real_open(path, *args, **kwargs)
        with patch('builtins.open', side_effect=guarded_open), \
             patch.object(daily_update_and_prune, 'get_protected_titles', return_value=set()):
            self.assertEqual(daily_update_and_prune.prune_unaired_pending_strms(), 1)
        self.assertFalse((season / '0.strm').exists())
        self.assertTrue(all((season / f'{i}.strm').exists() for i in range(1, len(sources))))

    def test_future_prune_retains_protected_title_with_year(self):
        show, season = self.make_show()
        (season / '1.strm').write_text(vod_cfg.PENDING_BASE_URL + '/pending?x=1')
        (season / '1.nfo').write_text('<episodedetails><aired>2099-01-01</aired></episodedetails>')
        with patch.object(daily_update_and_prune, 'get_protected_titles', return_value={show.name}):
            self.assertEqual(daily_update_and_prune.prune_unaired_pending_strms(), 0)
        self.assertTrue((season / '1.strm').exists())

    def test_episode_sync_keeps_year_identity_and_existing_playback(self):
        show, season = self.make_show()
        live = season / '测试剧 (2026) - S01E01.strm'
        live.write_text('http://localhost:8099/play?x=1')
        with patch.object(tmdb_populate, 'is_ai_directory', return_value=(False, '')), \
             patch.object(tmdb_populate, 'http_json', side_effect=[
                 {'id': 9, 'status': 'Ended', 'seasons': [{'season_number': 1, 'episode_count': 2}]},
                 {'episodes': [{'episode_number': 1, 'air_date': '2020-01-01'},
                               {'episode_number': 2, 'air_date': '2020-01-02'}]}]):
            self.assertEqual(daily_update_and_prune.sync_airing_episodes(), 1)
        new = season / '测试剧 - S01E02.strm'
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(new.read_text()).query)
        self.assertEqual(query['t'], [show.name])
        self.assertEqual(vod_db.get_strm_path('国产剧集', show.name, 1, 2), str(new))
        self.assertEqual(live.read_text(), 'http://localhost:8099/play?x=1')
        self.assertEqual(len(list(season.glob('*.strm'))), 2)

    def test_full_series_reuses_existing_episode_filename_and_live_source(self):
        show = self.cinema / 'series' / '旧剧名 (2026)'
        season = show / 'Season 01'
        season.mkdir(parents=True)
        (show / 'tvshow.nfo').write_text('<tvshow><tmdbid>2</tmdbid></tvshow>')
        live = season / '旧剧名 - S01E01.strm'
        live.write_text('http://localhost:8099/play?x=1')
        with patch.object(tmdb_populate, 'http_json', side_effect=[
                {'results': [self.item(2, True)], 'total_pages': 1},
                {'id': 2, 'status': 'Ended', 'seasons': [{'season_number': 1, 'episode_count': 1}]},
                {'episodes': [{'episode_number': 1, 'air_date': '2020-01-01'}]}]):
            self.assertEqual(tmdb_populate.main(['--cats', 'series', '--full']), 0)
        self.assertEqual(list(season.glob('*.strm')), [live])
        self.assertEqual(live.read_text(), 'http://localhost:8099/play?x=1')

    def test_episode_sync_failure_does_not_mark_series_ended(self):
        show, _ = self.make_show()
        with patch.object(tmdb_populate, 'is_ai_directory', return_value=(False, '')), \
             patch.object(tmdb_populate, 'http_json', side_effect=[
                 {'id': 9, 'status': 'Ended', 'seasons': [{'season_number': 1, 'episode_count': 2}]}, TimeoutError()]):
            with self.assertRaises(RuntimeError):
                daily_update_and_prune.sync_airing_episodes()
        self.assertIn('Returning Series', (show / 'tvshow.nfo').read_text())
        self.assertFalse(list(self.cinema.rglob('*.strm')))

    def daily_stages(self):
        self.stack.enter_context(patch.object(sys, 'argv', ['daily_update_and_prune.py']))
        for name in ('run_staging', 'retire_ghosts', 'airing_chase'):
            self.stack.enter_context(patch('quark_vod.integrations.hometheater.vod_automation.'+name, return_value={'errors':0}))
        self.stack.enter_context(patch.object(daily_update_and_prune, 'run_inline_cleanup', return_value={'complete':True}))
        names = ['run_pruning', 'run_update', 'sync_airing_episodes', 'precheck_newly_added',
                 'refresh_jellyfin', 'clean_orphan_people', 'run_vod_canary_test']
        stages = {name: self.stack.enter_context(patch.object(daily_update_and_prune, name, return_value=None))
                  for name in names}
        stages['run_vod_canary_test'].return_value = {'sampled': 10, 'readable': 10}
        return stages

    def daily_report(self):
        return json.loads((self.root / 'data' / 'daily_update_status.json').read_text())

    def test_daily_reports_tmdb_failure_and_continues_independent_checks(self):
        stages = self.daily_stages()
        stages['run_update'].side_effect = subprocess.CalledProcessError(1, ['fixture'])
        self.assertEqual(daily_update_and_prune.main(), 1)
        self.assertEqual(self.daily_report()['status'], 'partial')
        stages['run_vod_canary_test'].assert_called_once()
        self.assertEqual({s['name']:s for s in self.daily_report()['stages']}['tmdb']['status'], 'failed')

    def test_daily_stops_after_protection_failure(self):
        stages = self.daily_stages()
        stages['run_pruning'].side_effect = RuntimeError('protection unavailable')
        self.assertEqual(daily_update_and_prune.main(), 1)
        for name, stage in stages.items():
            if name != 'run_pruning':
                stage.assert_not_called()

    def test_daily_skips_orphan_cleanup_after_unconfirmed_scan(self):
        stages = self.daily_stages()
        stages['refresh_jellyfin'].side_effect = RuntimeError('scan unconfirmed')
        self.assertEqual(daily_update_and_prune.main(), 1)
        stages['clean_orphan_people'].assert_not_called()
        stages['run_vod_canary_test'].assert_called_once()

    def test_daily_records_canary_failure_and_zero_samples(self):
        stages = self.daily_stages()
        for result in ({'sampled': 10, 'readable': 9}, {'sampled': 0, 'readable': 0}):
            stages['run_vod_canary_test'].return_value = result
            self.assertEqual(daily_update_and_prune.main(), 1)
            self.assertEqual({s['name']:s for s in self.daily_report()['stages']}['canary']['status'], 'unconfirmed')
        stages['run_vod_canary_test'].return_value = {'sampled': 1, 'readable': 1}
        self.assertEqual(daily_update_and_prune.main(), 0)
        self.assertEqual(self.daily_report()['status'], 'complete')

    def test_tmdb_child_failure_is_propagated(self):
        with patch.object(subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['fixture'])):
            with self.assertRaises(subprocess.CalledProcessError):
                daily_update_and_prune.run_update()

    def test_source_unknown_is_reported_without_suppressing_short_read(self):
        stages = self.daily_stages()
        stages['precheck_newly_added'].return_value = {'locked': {'unknown': 1}, 'audit': {'no_source': 0}}
        self.assertEqual(daily_update_and_prune.main(), 1)
        self.assertEqual({s['name']:s for s in self.daily_report()['stages']}['sources']['status'], 'unconfirmed')
        stages['run_vod_canary_test'].assert_called_once()

    def test_dead_source_without_replacement_is_reported_as_unconfirmed(self):
        stages = self.daily_stages()
        stages['precheck_newly_added'].return_value = {'locked': {'failed_dead': 1}, 'audit': {'no_source': 0}}
        self.assertEqual(daily_update_and_prune.main(), 1)
        self.assertEqual({s['name']:s for s in self.daily_report()['stages']}['sources']['status'], 'unconfirmed')
        stages['run_vod_canary_test'].assert_called_once()

    def test_metadata_protection_failure_stops_all_deletions(self):
        with patch.object(purge_orphan_metadata, 'get_active_people_names', side_effect=RuntimeError('unavailable')), \
             patch.object(purge_orphan_metadata, 'purge_orphan_library_metadata') as library, \
             patch.object(purge_orphan_metadata, 'purge_orphan_people_dirs') as people:
            self.assertEqual(purge_orphan_metadata.main(), 1)
        library.assert_not_called()
        people.assert_not_called()

    def test_metadata_busy_does_not_continue_to_person_deletion(self):
        with patch.object(purge_orphan_metadata, 'get_active_people_names', return_value={str(i) for i in range(100)}), \
             patch.object(purge_orphan_metadata, 'purge_orphan_library_metadata', side_effect=daily_update_and_prune.Busy()), \
             patch.object(purge_orphan_metadata, 'purge_orphan_person_entities_via_api') as people:
            self.assertEqual(purge_orphan_metadata.main(), 75)
        people.assert_not_called()

    def test_metadata_people_permission_failure_keeps_independent_phases_and_partial(self):
        people_root=self.root/'People'
        orphan=people_root/'A'/'orphan'
        orphan.mkdir(parents=True)
        (orphan/'poster.jpg').write_bytes(b'fixture')
        active={str(i) for i in range(100)}
        with patch.object(purge_orphan_metadata,'get_active_people_names',return_value=active), \
             patch.object(purge_orphan_metadata,'PEOPLE_DIR',str(people_root)), \
             patch.object(purge_orphan_metadata,'purge_orphan_library_metadata'), \
             patch.object(purge_orphan_metadata.shutil,'rmtree',side_effect=PermissionError(13,'denied')), \
             patch.object(purge_orphan_metadata,'purge_orphan_person_entities_via_api',return_value=1) as api, \
             patch.object(purge_orphan_metadata,'trigger_db_optimization',return_value={'status':'submitted'}) as optimize, \
             patch.object(purge_orphan_metadata,'log') as logs:
            self.assertEqual(purge_orphan_metadata.main(),1)
        api.assert_called_once_with(active,max_workers=25)
        optimize.assert_called_once()
        self.assertTrue(orphan.exists())
        self.assertTrue(any('partial; failed=people_disk' in str(call) for call in logs.call_args_list))

    def test_metadata_unknown_library_reference_stops_later_independent_phases(self):
        error=purge_orphan_metadata.cleanup.MetadataCleanupError('references_unavailable')
        with patch.object(purge_orphan_metadata,'get_active_people_names',return_value={str(i) for i in range(100)}), \
             patch.object(purge_orphan_metadata,'purge_orphan_library_metadata',side_effect=error), \
             patch.object(purge_orphan_metadata,'purge_orphan_people_dirs') as disk, \
             patch.object(purge_orphan_metadata,'purge_orphan_person_entities_via_api') as api, \
             patch.object(purge_orphan_metadata,'trigger_db_optimization') as optimize:
            self.assertEqual(purge_orphan_metadata.main(),1)
        disk.assert_not_called()
        api.assert_not_called()
        optimize.assert_not_called()

    def test_metadata_reference_reread_error_cannot_be_downgraded_to_partial(self):
        error=purge_orphan_metadata.cleanup.MetadataCleanupError('file_removal_failed',removed=0,errno=0)
        error.__cause__=sqlite3.OperationalError('fixture reference unavailable')
        with patch.object(purge_orphan_metadata,'get_active_people_names',return_value={str(i) for i in range(100)}), \
             patch.object(purge_orphan_metadata,'purge_orphan_library_metadata',side_effect=error), \
             patch.object(purge_orphan_metadata,'purge_orphan_people_dirs') as disk, \
             patch.object(purge_orphan_metadata,'purge_orphan_person_entities_via_api') as api, \
             patch.object(purge_orphan_metadata,'trigger_db_optimization') as optimize:
            self.assertEqual(purge_orphan_metadata.main(),1)
        disk.assert_not_called()
        api.assert_not_called()
        optimize.assert_not_called()

    def test_metadata_person_database_disappears_stops_optimization(self):
        with patch.object(purge_orphan_metadata,'get_active_people_names',return_value={str(i) for i in range(100)}), \
             patch.object(purge_orphan_metadata,'JF_DB',str(self.root/'missing-person.db')), \
             patch.object(purge_orphan_metadata,'purge_orphan_library_metadata'), \
             patch.object(purge_orphan_metadata,'purge_orphan_people_dirs'), \
             patch.object(purge_orphan_metadata,'trigger_db_optimization') as optimize:
            self.assertEqual(purge_orphan_metadata.main(),1)
        optimize.assert_not_called()

    def test_metadata_database_read_error_is_not_empty_protection(self):
        with patch.object(purge_orphan_metadata, 'JF_DB', str(self.root / 'missing.db')):
            with self.assertRaises(RuntimeError):
                purge_orphan_metadata.get_active_people_names()

    def metadata_fixture(self):
        from quark_vod import cleanup
        library = self.root / 'jellyfin2/config/metadata/library'
        library.mkdir(parents=True)
        database = self.root / 'jf.db'
        with sqlite3.connect(database) as conn:
            conn.execute('CREATE TABLE BaseItemImageInfos (Path TEXT)')
            conn.executemany('INSERT INTO BaseItemImageInfos VALUES (?)',
                             [(f'/config/metadata/library/{i}.jpg',) for i in range(100)])
        for i in range(100):
            (library / f'{i}.jpg').write_bytes(b'referenced')
        self.stack.enter_context(patch.object(cleanup, 'JF_DB', str(database)))
        self.stack.enter_context(patch.object(cleanup.config, 'BASE_DIR', str(self.root)))
        self.stack.enter_context(patch.object(cleanup.config, 'JELLYFIN_METADATA_LIBRARY_DIR', str(library)))
        self.stack.enter_context(patch.object(cleanup.config, 'JELLYFIN_METADATA_LIBRARY_PREFIX', '/config/metadata/library'))
        return cleanup, library, database

    def test_strict_metadata_cleanup_keeps_referenced_images(self):
        cleanup, library, database = self.metadata_fixture()
        keep = library / '0.jpg'
        orphan = library / 'orphan.jpg'
        orphan.write_bytes(b'orphan')
        self.assertEqual(cleanup.prune_jellyfin2_orphan_metadata(strict=True, verbose=False), (1, 6))
        self.assertEqual(keep.read_bytes(), b'referenced')
        self.assertFalse(orphan.exists())
        orphan.write_bytes(b'another')
        with sqlite3.connect(database) as conn:
            conn.execute('DELETE FROM BaseItemImageInfos')
        with self.assertRaisesRegex(cleanup.MetadataCleanupError, 'reference_sample_too_small'):
            cleanup.prune_jellyfin2_orphan_metadata(strict=True, verbose=False)
        self.assertTrue(keep.exists())
        self.assertTrue(orphan.exists())

    def test_unwritable_orphan_parent_stops_before_any_unlink(self):
        cleanup, library, _ = self.metadata_fixture()
        restricted = library / 'restricted'
        restricted.mkdir()
        (restricted / 'orphan.jpg').write_bytes(b'orphan')
        (library / 'writable.jpg').write_bytes(b'orphan')
        access = os.access

        def check(path, mode):
            return path != str(restricted) and access(path, mode)

        with patch.object(cleanup.os, 'access', side_effect=check):
            with self.assertRaisesRegex(cleanup.MetadataCleanupError, 'parent_permission_denied=1') as exc:
                cleanup.prune_jellyfin2_orphan_metadata(strict=True, verbose=False)
        self.assertNotIn(str(restricted), str(exc.exception))
        self.assertTrue((restricted / 'orphan.jpg').exists())
        self.assertTrue((library / 'writable.jpg').exists())

    def test_incomplete_image_tree_retains_orphans(self):
        cleanup, library, _ = self.metadata_fixture()
        (library / '0.jpg').unlink()
        orphan = library / 'orphan.jpg'
        orphan.write_bytes(b'orphan')
        with self.assertRaisesRegex(cleanup.MetadataCleanupError, 'referenced_files_missing missing=1'):
            cleanup.prune_jellyfin2_orphan_metadata(strict=True, verbose=False)
        self.assertTrue(orphan.exists())

    def test_linked_metadata_subdirectory_stops_before_removal(self):
        cleanup, library, _ = self.metadata_fixture()
        outside = self.root / 'outside'
        outside.mkdir()
        target = outside / 'keep.jpg'
        target.write_bytes(b'keep')
        (library / 'link').symlink_to(outside, target_is_directory=True)
        orphan = library / 'orphan.jpg'
        orphan.write_bytes(b'orphan')
        with self.assertRaisesRegex(cleanup.MetadataCleanupError, 'unsafe_symlink=1'):
            cleanup.prune_jellyfin2_orphan_metadata(strict=True, verbose=False)
        self.assertEqual(target.read_bytes(), b'keep')
        self.assertTrue(orphan.exists())

    def test_metadata_subprocess_failure_reaches_daily_report(self):
        with patch.object(daily_update_and_prune.os.path, 'isfile', return_value=True), \
             patch.object(subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['fixture'])):
            with self.assertRaises(subprocess.CalledProcessError):
                daily_update_and_prune.clean_orphan_people()

    def test_metadata_subprocess_busy_defers_without_suppressing_canary(self):
        action = daily_update_and_prune.clean_orphan_people
        stages = self.daily_stages()
        stages['clean_orphan_people'].side_effect = action
        with patch.object(daily_update_and_prune.os.path, 'isfile', return_value=True), \
             patch.object(subprocess, 'run', side_effect=subprocess.CalledProcessError(75, ['fixture'])):
            self.assertEqual(daily_update_and_prune.main(), 1)
        stage = next(s for s in self.daily_report()['stages'] if s['name'] == 'orphan_metadata')
        self.assertEqual(stage['status'], 'deferred')
        stages['run_vod_canary_test'].assert_called_once()

    def test_scan_requires_new_success_or_observed_completion(self):
        old = {'Status': 'Completed', 'EndTimeUtc': '2020-01-01T00:00:00Z'}
        new = {'Status': 'Completed', 'EndTimeUtc': '2020-01-02T00:00:00Z'}
        for result, expected in ((old, False), (new, True), ({'Status': 'Failed'}, False), ({}, False)):
            with self.subTest(result=result), \
                 patch.object(daily_update_and_prune, '_refresh_task', return_value={'State': 'Idle', 'LastExecutionResult': result}), \
                 patch.object(daily_update_and_prune.time, 'monotonic', side_effect=range(20)):
                self.assertEqual(daily_update_and_prune._wait_library_refresh(vod_cfg, max_wait=3, previous=old), expected)
        with patch.object(daily_update_and_prune, '_refresh_task', side_effect=ValueError('missing task')), \
             patch.object(daily_update_and_prune.time, 'monotonic', side_effect=range(20)):
            self.assertFalse(daily_update_and_prune._wait_library_refresh(vod_cfg, max_wait=3, previous=old))

    def test_library_refresh_default_budget_covers_slow_scan(self):
        old = {'Status': 'Completed', 'EndTimeUtc': '2020-01-01T00:00:00Z'}
        new = {'Status': 'Completed', 'EndTimeUtc': '2020-01-02T00:00:00Z'}
        # The production scan took 2375.895 seconds. Advance a virtual clock
        # through every poll so this fails if the default returns to 1800.
        for budget, expected in ((None, True), (1800, False)):
            elapsed = [0]

            def sleep(seconds):
                elapsed[0] += seconds

            def task(_config):
                complete = elapsed[0] >= 2376
                return {'State': 'Idle' if complete else 'Running',
                        'LastExecutionResult': new if complete else old}

            with self.subTest(budget=budget), \
                 patch.object(daily_update_and_prune, '_refresh_task', side_effect=task), \
                 patch.object(daily_update_and_prune.time, 'monotonic', side_effect=lambda: elapsed[0]), \
                 patch.object(daily_update_and_prune.time, 'sleep', side_effect=sleep):
                options = {} if budget is None else {'max_wait': budget}
                actual = daily_update_and_prune._wait_library_refresh(vod_cfg, previous=old, **options)
                self.assertEqual(actual, expected)
                self.assertEqual(elapsed[0], 2385 if expected else 1800)

    def test_running_scan_cannot_reuse_previous_success(self):
        old = {'Status': 'Completed', 'EndTimeUtc': '2020-01-01T00:00:00Z'}
        with patch.object(daily_update_and_prune, '_refresh_task', side_effect=[
                {'State': 'Running', 'LastExecutionResult': old},
                {'State': 'Idle', 'LastExecutionResult': old}]), \
             patch.object(daily_update_and_prune.time, 'monotonic', side_effect=range(20)):
            self.assertFalse(daily_update_and_prune._wait_library_refresh(vod_cfg, max_wait=3, previous=old))

def tearDownModule():
    NETWORK.stop()
    if hasattr(vod_db._local, "conn"):
        vod_db._local.conn.close()
        del vod_db._local.conn
    TEMP.cleanup()


if __name__ == "__main__":
    unittest.main()
