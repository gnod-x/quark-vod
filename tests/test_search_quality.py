"""Regression cases for actual source omissions and false resolution claims."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

TMP=tempfile.TemporaryDirectory(prefix='vod-search-test-')
os.environ.update(VOD_BASE_DIR=TMP.name,VOD_DATA_DIR=TMP.name+'/data',AI_PARSE_ENABLED='0')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from quark_vod import panshare_matrix as matrix, search, db
from quark_vod.quality import quality_hint
from quark_vod.transfer import QuarkTransfer, TransferFailed
from quark_vod.auditor import VodAuditor


def candidate(name,url='https://pan.quark.cn/s/abc123',**kw):
    return search.SearchResult('panshare','test',name,'','',[],share_url=url,**kw)


class SearchTests(unittest.TestCase):
    def setUp(self):
        with db._conn() as con:
            con.execute('DELETE FROM source_audits')
            con.execute('DELETE FROM dead_sources')

    def test_duplicate_links_are_deduped_before_limit(self):
        html=''.join(f'<a href="/voddetail/{n}.html">x</a>'*3 for n in range(10))
        self.assertEqual(len(matrix.detail_paths(html)[:10]),10)

    def test_pagination_collects_later_4k_candidate(self):
        base='https://fixture.invalid'
        pages={base+'/index.php/vod/search.html?wd=test':'<a href="/voddetail/1.html">1</a><a href="/vod/search/page/2.html">下一页</a>',
               base+'/vod/search/page/2.html':'<a href="/voddetail/2.html">2</a>',
               base+'/voddetail/1.html':'<title>test 1080p</title>https://pan.quark.cn/s/aaa',
               base+'/voddetail/2.html':'<title>test 2160p</title>https://pan.quark.cn/s/bbb'}
        with patch.object(matrix,'_fetch_html',side_effect=lambda url,timeout:pages[url]):
            items,report=matrix._fetch_node({'key':'test','base':base},'test',24,3)
        self.assertEqual(report['pages'],2)
        self.assertEqual({i['share_url'] for i in items},{'https://pan.quark.cn/s/aaa','https://pan.quark.cn/s/bbb'})

    def test_next_page_cannot_switch_origin(self):
        self.assertIsNone(matrix.next_page('<a href="http://127.0.0.1/vod/search">下一页</a>','https://a.test/vod/search','https://a.test'))

    def test_password_and_later_richer_title_survive_dedup(self):
        items=matrix.merge_items([{'source':'a','name':'样例','share_url':'https://pan.quark.cn/s/abc'},
            {'source':'b','name':'样例 4K','share_url':'https://pan.quark.cn/s/abc?pwd=1234'}])
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['share_url'],'https://pan.quark.cn/s/abc#pwd=1234')
        self.assertEqual(items[0]['sources'],['a','b'])
        self.assertEqual(items[0]['name'],'样例 4K')

    def test_html_entities_and_password_next_to_link(self):
        rows=matrix.parse_download_rows("<div class='module-row-title'><h4>A &amp; B - 4K</h4><p>https://pan.quark.cn/s/abc</p>提取码：a123</div>")
        self.assertEqual(rows,[{'title':'A & B - 4K','url':'https://pan.quark.cn/s/abc#pwd=a123'}])

    def test_dead_source_filtered_on_cache_hit(self):
        key=matrix._cache_key('样例')
        matrix._cache_put(key,{'items':[{'share_url':'https://pan.quark.cn/s/dead'}],'nodes':[],'partial':False})
        with patch.object(db,'get_dead_sources_set',return_value={'https://pan.quark.cn/s/dead'}):
            self.assertEqual(matrix.PanshareMatrix.search_report('样例')['items'],[])

    def test_partial_empty_results_have_short_cache_ttl(self):
        with patch.object(matrix.time,'time',return_value=100):
            matrix._cache_put('partial',{'items':[],'nodes':[],'partial':True})
        with patch.object(matrix.time,'time',return_value=146):
            self.assertIsNone(matrix._cache_get('partial'))

    def test_regular_long_series_not_confused_with_short_drama(self):
        self.assertGreaterEqual(search.score_search_result(candidate('凡人修仙传 4K 全170集'),'凡人修仙传',season=1,episode=100),85)
        self.assertEqual(search.score_search_result(candidate('凡人修仙传 短剧 80集'),'凡人修仙传',season=1,episode=1),-1)

    def test_explicit_wrong_season_and_year_still_rejected(self):
        self.assertEqual(search.score_search_result(candidate('庆余年 第三季 4K'),'庆余年',season=2,episode=1),-1)
        self.assertEqual(search.score_search_result(candidate('沙丘 1984 4K'),'沙丘 (2021)'),-1)
        self.assertGreaterEqual(search.score_search_result(candidate('庆余年 S02 4K'),'庆余年',season=2,episode=1),85)

    def test_numbered_sequels_and_live_action_are_not_same_work(self):
        self.assertEqual(search.score_search_result(candidate('沙丘2 - 沙丘2'),'沙丘 (2021)'),-1)
        self.assertFalse(search.matches_kind(candidate('凡人修仙传真人版'),'国产动漫'))
        self.assertTrue(search.matches_kind(candidate('凡人修仙传 4K'),'国产动漫'))

    def test_later_season_year_is_not_series_premiere_year(self):
        c=candidate('庆余年 S02 2024 2160p')
        self.assertGreaterEqual(search.score_search_result(c,'庆余年 (2019)',season=2,episode=1),85)
        files=[{'file_name':'庆余年.2024.S02E01.2160p.mkv','relative_path':'Season 02/庆余年.2024.S02E01.2160p.mkv','size':2*1024**3}]
        self.assertIsNotNone(QuarkTransfer(None).select_share_video(files,'庆余年 (2019)',2,1))

    def test_queries_retain_year_and_original_title(self):
        self.assertEqual(search.query_variants('沙丘 (2021)',['Dune']),['沙丘','沙丘2021','Dune'])

    def test_verified_nfo_original_title_matches_full_release_and_episode(self):
        original='수영부 또라이에게 찍혀버렸다!'
        exact=candidate(original+' S01E01 1080p')
        adjacent=candidate(original+' S01E01 2017 4K')
        other_series=candidate('수영부 선배에게 찍혀버렸다! S01E01 4K')
        other_season=candidate(original+' S02E01 4K')
        kwargs={'aliases':(original,),'season':1,'episode':1}
        self.assertGreaterEqual(search.score_search_result(exact,'被游泳队的疯批盯上了！ (2026)',**kwargs),85)
        self.assertEqual(search.score_search_result(adjacent,'被游泳队的疯批盯上了！ (2026)',**kwargs),-1)
        self.assertEqual(search.score_search_result(other_series,'被游泳队的疯批盯上了！ (2026)',**kwargs),-1)
        self.assertEqual(search.score_search_result(other_season,'被游泳队的疯批盯上了！ (2026)',**kwargs),-1)

    def test_release_markers_are_allowed_after_alias_but_unrelated_suffixes_are_not(self):
        alias='그날 밤은 실수였나요'
        good=candidate(alias+' S01E01 4K HDR')
        poison=candidate(alias+' 시즌1 무료 보기')
        self.assertGreaterEqual(search.score_search_result(good,'韩国剧 (2026)',season=1,episode=1,aliases=(alias,)),85)
        self.assertEqual(search.score_search_result(poison,'韩国剧 (2026)',season=1,episode=1,aliases=(alias,)),-1)

    def test_playback_identity_reads_only_matching_bounded_nfo(self):
        from pathlib import Path
        from quark_vod import config, strm_manager
        folder=Path(config.CINEMA2_DIR)/'热门韩剧'/'显示标题'
        folder.mkdir(parents=True,exist_ok=True)
        (folder/'tvshow.nfo').write_text(
            '<tvshow><title>显示标题</title><originaltitle>원제 제목</originaltitle>'
            '<year>2026</year><tmdbid>42</tmdbid></tvshow>',encoding='utf-8')
        self.assertEqual(strm_manager.library_search_identity('热门韩剧','显示标题',1),
                         ('显示标题 (2026)',('원제 제목',)))
        (folder/'tvshow.nfo').write_text(
            '<tvshow><title>另一部作品</title><originaltitle>evil alias</originaltitle>'
            '<year>1999</year></tvshow>',encoding='utf-8')
        self.assertEqual(strm_manager.library_search_identity('热门韩剧','显示标题',1),('显示标题',()))
        self.assertEqual(strm_manager.library_search_identity('热门韩剧','../别的',1),('../别的',()))

    def test_quality_uses_dimensions_before_loose_2160(self):
        self.assertEqual(quality_hint('2160x1080.mp4')['level'],'hd')
        self.assertEqual(quality_hint('3840x2160.mkv')['level'],'4k')
        self.assertEqual(quality_hint('3840x1600.mkv')['level'],'4k')
        self.assertEqual(quality_hint('1080p.mkv','4K分享')['level'],'hd')
        self.assertEqual(quality_hint('HDR.REMUX.mkv')['level'],'unknown')

    def test_same_episode_prefers_explicit_resolution_over_size(self):
        files=[{'name':'样例.S01E01.1080p.mkv','path':'样例.S01E01.1080p.mkv','size':10*1024**3},
               {'name':'样例.S01E01.2160p.mkv','path':'样例.S01E01.2160p.mkv','size':2*1024**3}]
        self.assertIn('2160p',QuarkTransfer.match_episode_video(files,1,1)['name'])

    def test_series_share_file_must_match_show_identity_season_and_episode(self):
        tr=QuarkTransfer(None)
        files=[
            {'file_name':'수영부 또라이에게 찍혀버렸다! S01E01 1080p.mkv','relative_path':'S01/수영부 또라이에게 찍혀버렸다! S01E01 1080p.mkv','size':200*1024**2},
            {'file_name':'错误剧名 S01E01 4K.mkv','relative_path':'S01/错误剧名 S01E01 4K.mkv','size':2*1024**3},
            {'file_name':'수영부 또라이에게 찍혀버렸다! S02E01 4K.mkv','relative_path':'S02/수영부 또라이에게 찍혀버렸다! S02E01 4K.mkv','size':2*1024**3},
            {'file_name':'수영부 또라이에게 찍혀버렸다! S01E02 E01 Extra.mkv','relative_path':'S01/수영부 또라이에게 찍혀버렸다! S01E02 E01 Extra.mkv','size':2*1024**3},
        ]
        chosen=tr.select_share_video(files,'被游泳队的疯批盯上了！ (2026)',1,1,
                                     aliases=('수영부 또라이에게 찍혀버렸다!',))
        self.assertIsNotNone(chosen)
        self.assertIn('S01E01',chosen['name'])
        self.assertIn('1080p',chosen['name'])

    def test_audit_uses_recursive_exact_target_not_first_package(self):
        from unittest.mock import Mock
        tr=Mock()
        tr.resolve_share.return_value=('token','id',[{}])
        tr.share_files.return_value=[{'file_name':'x'}]
        tr.select_share_video.return_value={'name':'样例.S02E03.1080p.mkv','size':5*1024**3}
        with patch.object(search,'search',return_value=[candidate('样例 第二季 4K')]),patch.object(db,'save_source_audit') as save:
            result=VodAuditor(tr).audit_tv_season('剧集','样例',2,[3],force=True)
        self.assertEqual(result[:2],(True,False))
        self.assertEqual(save.call_args.args[3],3)
        self.assertEqual(save.call_args.kwargs['is_4k'],0)
        tr.share_files.assert_called_once()

    def test_share_timeout_preserves_all_existing_episode_locks(self):
        url='https://pan.quark.cn/s/fixture'
        for ep in (2,4):db.save_source_audit('剧集','样例',1,ep,1,1,url,'样例 4K','valid_4k')
        for failure in (TransferFailed('TimeoutError'), TransferFailed('upstream HTTP 403'),
                        TransferFailed('解析分享失败: 登录状态已过期'), TransferFailed('stoken 已失效'), OSError('offline')):
            tr=Mock();tr.resolve_share.side_effect=failure
            with patch('quark_vod.auditor.time.sleep'),patch.object(search,'search') as search_call:
                stats=VodAuditor(tr).check_and_heal_locked_shares()
            self.assertFalse(db.is_dead_source(url))
            self.assertEqual([db.get_source_audit('剧集','样例',1,e)['best_share_url'] for e in (2,4)],[url,url])
            search_call.assert_not_called()
            self.assertEqual(stats['unknown'],1)

    def test_confirmed_dead_share_heals_original_episodes_not_episode_one(self):
        url='https://pan.quark.cn/s/cancelled'
        for ep in (3,7):db.save_source_audit('剧集','样例',2,ep,1,1,url,'样例 第二季 4K','valid_4k')
        tr=Mock();tr.resolve_share.side_effect=TransferFailed('分享已取消了分享')
        auditor=VodAuditor(tr)
        with patch('quark_vod.auditor.time.sleep'),patch.object(auditor,'audit_tv_season',return_value=(False,False,'','unconfirmed')) as heal:
            auditor.check_and_heal_locked_shares()
        self.assertEqual(heal.call_args.kwargs['existing_episodes'],[3,7])
        self.assertTrue(db.is_dead_source(url))

    def test_partial_episode_coverage_never_claims_whole_season_4k(self):
        tr=Mock();tr.resolve_share.return_value=('token','id',[{}]);tr.share_files.return_value=[]
        tr.select_share_video.side_effect=lambda files,title,season,ep,**kwargs:{'name':'样例.S01E01.2160p.mkv','size':1024**3} if ep==1 else None
        with patch.object(search,'search',return_value=[candidate('样例 4K')]):
            result=VodAuditor(tr).audit_tv_season('剧集','样例',1,[1,2],force=True)
        self.assertEqual(result[:2],(False,False))
        self.assertIsNotNone(db.get_source_audit('剧集','样例',1,1))
        self.assertIsNone(db.get_source_audit('剧集','样例',1,2))
        self.assertIn('1/2',result[3])

    def test_dead_status_cannot_be_reused_as_fresh_audit(self):
        db.save_source_audit('剧集','样例',1,1,1,1,'https://pan.quark.cn/s/fixture','','dead')
        with patch.object(search,'search',return_value=[]) as search_call:
            result=VodAuditor(Mock()).audit_tv_season('剧集','样例',1,[1])
        search_call.assert_called_once()
        self.assertFalse(result[0])

    def test_timeout_after_playback_yield_stops_before_share_request(self):
        db.save_source_audit('剧集','样例',1,1,1,1,'https://pan.quark.cn/s/fixture','','valid_4k')
        tr=Mock()
        stats=VodAuditor(tr).check_and_heal_locked_shares(yield_fn=Mock(),timeout_fn=Mock(side_effect=[False,True]))
        tr.resolve_share.assert_not_called()
        self.assertEqual(stats['checked'],0)

    def test_readable_share_root_does_not_refresh_file_quality_evidence(self):
        url='https://pan.quark.cn/s/fixture'
        db.save_source_audit('剧集','样例',1,1,1,1,url,'','valid_4k')
        with db._conn() as con:con.execute('UPDATE source_audits SET checked_at=1')
        tr=Mock();tr.resolve_share.return_value=('token','id',[{'file_name':'readme.txt'}])
        with patch('quark_vod.auditor.time.sleep'):
            stats=VodAuditor(tr).check_and_heal_locked_shares()
        self.assertEqual(stats['healthy'],1)
        self.assertEqual(db.get_source_audit('剧集','样例',1,1)['checked_at'],1)

    def test_missing_locked_episode_is_invalidated_without_marking_share_dead(self):
        url='https://pan.quark.cn/s/fixture'
        for ep in (1,2):db.save_source_audit('剧集','样例',1,ep,1,0,url,'样例','valid_hd')
        tr=Mock();tr.resolve_share.return_value=('token','id',[{}]);tr.share_files.return_value=[]
        tr.select_share_video.return_value=None
        with patch.object(search,'search',return_value=[]):
            result=VodAuditor(tr).audit_tv_season('剧集','样例',1,[1],force=True)
        self.assertFalse(result[0])
        self.assertFalse(db.get_source_audit('剧集','样例',1,1)['has_quark_source'])
        self.assertEqual(db.get_source_audit('剧集','样例',1,1)['check_status'],'not_found')
        self.assertTrue(db.get_source_audit('剧集','样例',1,2)['has_quark_source'])
        self.assertFalse(db.is_dead_source(url))

    def test_inconclusive_file_recheck_preserves_old_episode_evidence(self):
        url='https://pan.quark.cn/s/fixture'
        db.save_source_audit('剧集','样例',1,1,1,0,url,'样例','valid_hd')
        before=dict(db.get_source_audit('剧集','样例',1,1))
        tr=Mock();tr.resolve_share.side_effect=TransferFailed('upstream HTTP 404')
        with patch.object(search,'search',return_value=[]):
            VodAuditor(tr).audit_tv_season('剧集','样例',1,[1],force=True)
        self.assertEqual(dict(db.get_source_audit('剧集','样例',1,1)),before)

    def test_existing_locked_files_are_rechecked_even_without_search_candidates(self):
        url='https://pan.quark.cn/s/fixture'
        db.save_source_audit('剧集','样例',1,1,1,0,url,'样例','valid_hd')
        tr=Mock();tr.resolve_share.return_value=('token','id',[{}]);tr.share_files.return_value=[]
        tr.select_share_video.return_value={'name':'样例.S01E01.1080p.mp4','size':1024**3}
        with patch.object(search,'search',return_value=[]):
            result=VodAuditor(tr).audit_tv_season('剧集','样例',1,[1],force=True)
        self.assertTrue(result[0])
        self.assertEqual(db.get_source_audit('剧集','样例',1,1)['best_share_url'],url)

    def test_limited_audit_rotates_after_unsuccessful_targets(self):
        from quark_vod import config
        with tempfile.TemporaryDirectory(dir=TMP.name) as root:
            for title in ('A','B','C'):(Path(root)/'cinema/movie'/title).mkdir(parents=True)
            auditor=VodAuditor(Mock())
            with patch.object(config,'CINEMA2_DIR',str(Path(root)/'cinema')),patch.object(config,'DATA_DIR',str(Path(root)/'data')), \
                 patch.object(auditor,'audit_single_item',return_value=(False,False,'','unconfirmed')) as audit,patch('quark_vod.auditor.time.sleep'):
                auditor.audit_entire_library(limit=2)
                self.assertEqual([c.args[1] for c in audit.call_args_list],['A','B'])
                audit.reset_mock();auditor.audit_entire_library(limit=2)
                self.assertEqual([c.args[1] for c in audit.call_args_list],['C','A'])

    def test_unstarted_audit_does_not_advance_cursor(self):
        from quark_vod import config
        with tempfile.TemporaryDirectory(dir=TMP.name) as root:
            for title in ('A','B'):(Path(root)/'cinema/movie'/title).mkdir(parents=True)
            auditor=VodAuditor(Mock())
            with patch.object(config,'CINEMA2_DIR',str(Path(root)/'cinema')),patch.object(config,'DATA_DIR',str(Path(root)/'data')), \
                 patch.object(auditor,'audit_single_item',return_value=(False,False,'','unconfirmed')) as audit,patch('quark_vod.auditor.time.sleep'):
                stats=auditor.audit_entire_library(limit=1,timeout_fn=lambda:True)
                self.assertEqual(stats['checked'],0);audit.assert_not_called()
                auditor.audit_entire_library(limit=1)
                self.assertEqual(audit.call_args.args[1],'A')

    def test_audit_exception_is_reported_as_unconfirmed(self):
        from quark_vod import config
        with tempfile.TemporaryDirectory(dir=TMP.name) as root:
            (Path(root)/'cinema/movie/A').mkdir(parents=True)
            auditor=VodAuditor(Mock())
            with patch.object(config,'CINEMA2_DIR',str(Path(root)/'cinema')),patch.object(config,'DATA_DIR',str(Path(root)/'data')), \
                 patch.object(auditor,'audit_single_item',side_effect=OSError('fixture')),patch('quark_vod.auditor.time.sleep'):
                stats=auditor.audit_entire_library(limit=1)
            self.assertEqual(stats['no_source'],1)
            self.assertEqual(stats['errors'],1)

    def test_limited_locked_share_checks_rotate_even_after_unknown_results(self):
        from quark_vod import config
        import hashlib
        import json
        urls = {name: 'https://pan.quark.cn/s/fixture'+name for name in ('A','B','C')}
        for name,url in urls.items():
            db.save_source_audit('movie',name,0,0,1,0,url,name,'valid_hd')
        tr=Mock()
        def resolve(url):
            if url==urls['A']:raise TransferFailed('upstream HTTP 404')
            return 'token','id',[{}]
        tr.resolve_share.side_effect=resolve
        auditor=VodAuditor(tr)
        with tempfile.TemporaryDirectory(dir=TMP.name) as root,patch.object(config,'DATA_DIR',root),patch('quark_vod.auditor.time.sleep'):
            first=auditor.check_and_heal_locked_shares(limit=2)
            second=auditor.check_and_heal_locked_shares(limit=2)
            self.assertEqual([c.args[0] for c in tr.resolve_share.call_args_list],[urls['A'],urls['B'],urls['C'],urls['A']])
            self.assertEqual((first['healthy'],first['unknown']),(1,1))
            self.assertEqual(second['checked'],2)
            cursor=Path(root)/'source_lock_cursor.json'
            value=json.loads(cursor.read_text())
            self.assertEqual(value['after'],['movie','A',0,hashlib.sha256(urls['A'].encode()).hexdigest()])
            self.assertNotIn('https',cursor.read_text())
            self.assertEqual(cursor.stat().st_mode & 0o777,0o644)
            self.assertFalse(list(Path(root).glob('.source-lock-*')))
        self.assertFalse(db.is_dead_source(urls['A']))

    def test_locked_share_not_started_after_yield_keeps_cursor(self):
        from quark_vod import config
        for name in ('A','B'):
            db.save_source_audit('movie',name,0,0,1,0,'https://pan.quark.cn/s/fixture'+name,name,'valid_hd')
        tr=Mock();tr.resolve_share.return_value=('token','id',[{}])
        with tempfile.TemporaryDirectory(dir=TMP.name) as root,patch.object(config,'DATA_DIR',root),patch('quark_vod.auditor.time.sleep'):
            auditor=VodAuditor(tr)
            stats=auditor.check_and_heal_locked_shares(limit=1,yield_fn=Mock(),timeout_fn=Mock(side_effect=[False,True]))
            self.assertEqual(stats['checked'],0)
            self.assertFalse((Path(root)/'source_lock_cursor.json').exists())
            auditor.check_and_heal_locked_shares(limit=1)
            tr.resolve_share.assert_called_once_with('https://pan.quark.cn/s/fixtureA')

    def test_locked_share_healing_error_does_not_abort_later_checks(self):
        from quark_vod import config
        for name in ('A','B'):
            db.save_source_audit('剧集',name,1,1,1,0,'https://pan.quark.cn/s/fixture'+name,name,'valid_hd')
        tr=Mock();tr.resolve_share.side_effect=[TransferFailed('分享已取消'),('token','id',[{}])]
        auditor=VodAuditor(tr)
        with tempfile.TemporaryDirectory(dir=TMP.name) as root,patch.object(config,'DATA_DIR',root),patch('quark_vod.auditor.time.sleep'), \
                patch.object(auditor,'audit_tv_season',side_effect=OSError('fixture')):
            stats=auditor.check_and_heal_locked_shares(limit=2)
            self.assertEqual((stats['checked'],stats['healthy'],stats['unknown'],stats['errors']),(2,1,1,1))

    def test_zero_limit_never_starts_a_root_or_file_audit(self):
        from quark_vod import config
        db.save_source_audit('movie','A',0,0,1,0,'https://pan.quark.cn/s/fixture','A','valid_hd')
        with tempfile.TemporaryDirectory(dir=TMP.name) as root:
            (Path(root)/'cinema/movie/A').mkdir(parents=True)
            tr=Mock();auditor=VodAuditor(tr)
            with patch.object(config,'DATA_DIR',str(Path(root)/'data')),patch.object(config,'CINEMA2_DIR',str(Path(root)/'cinema')), \
                    patch.object(auditor,'audit_single_item') as audit,patch('quark_vod.auditor.time.sleep'):
                self.assertEqual(auditor.check_and_heal_locked_shares(limit=0)['checked'],0)
                self.assertEqual(auditor.audit_entire_library(limit=0)['checked'],0)
                tr.resolve_share.assert_not_called();audit.assert_not_called()


if __name__=='__main__':unittest.main(verbosity=2)
