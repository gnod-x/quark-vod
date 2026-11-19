"""Portal regressions: isolated library and database, no service or cloud writes."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

TMP=tempfile.TemporaryDirectory(prefix='vod-portal-test-')
os.environ.update(VOD_BASE_DIR=TMP.name,VOD_DATA_DIR=TMP.name+'/data',VOD_CINEMA2_DIR=TMP.name+'/cinema2',AI_PARSE_ENABLED='0')
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))
# Import the authoritative host job, with quark_vod already loaded from app.
from quark_vod import db, config
sys.path.insert(0,str(ROOT.parent/'hometheater/scripts'))
import vod_search_and_add as portal
portal.CINEMA2=TMP.name+'/cinema2'

MOVIE={'id':123,'title':'样例','release_date':'2021-01-01','media_type':'movie','genres':[],
       'production_countries':[{'iso_3166_1':'CN'}], 'poster_path':None}
TV={'id':124,'name':'样例剧','first_air_date':'2020-01-01','media_type':'tv','origin_country':['CN'],
    'genres':[],'seasons':[{'season_number':2,'episode_count':2}],
    'last_episode_to_air':{'season_number':2,'episode_number':1}}


class PortalTests(unittest.TestCase):
    def setUp(self):
        portal.CINEMA2=str(Path(TMP.name)/'cinema2'/self.id().rsplit('.',1)[-1])
        portal._details.clear();portal._candidates.clear();portal._jobs.clear()
        con=db._conn()
        for t in ('strm_map','transfers','source_audits'):con.execute('DELETE FROM '+t)
        con.commit()

    def test_metadata_is_not_a_4k_or_source_claim_and_no_poster_allowed(self):
        with patch.object(portal,'_tmdb',return_value={'results':[MOVIE]}):items=portal.search_all('样例')
        self.assertEqual(len(items),1)
        self.assertIsNone(items[0]['has_4k_quark'])
        self.assertEqual(items[0]['source_status'],'unchecked')

    def test_library_identity_does_not_match_sequel_or_other_year(self):
        root=Path(portal.CINEMA2)/'国产电影';(root/'样例2 (2021)').mkdir(parents=True,exist_ok=True)
        (root/'样例2 (2021)'/'x.strm').write_text('x')
        self.assertFalse(portal.check_in_library('国产电影','样例','2021'))

    def test_library_identity_finds_legacy_folder_by_tmdb_nfo_id(self):
        show=Path(portal.CINEMA2)/'欧美剧集'/'各取所需'
        season=show/'Season 01';season.mkdir(parents=True)
        (show/'tvshow.nfo').write_text('<tvshow><tmdbid>298550</tmdbid></tvshow>')
        (season/'各取所需 - S01E01.strm').write_text('pending')
        self.assertTrue(portal.check_in_library('欧美剧集','各取所需','2026',298550))
        (show/'tvshow.nfo').write_text('<tvshow><tmdbid>999999</tmdbid></tvshow>')
        self.assertFalse(portal.check_in_library('欧美剧集','各取所需','2026',298550))

    def test_duplicate_tmdb_folders_are_treated_as_existing(self):
        root=Path(portal.CINEMA2)/'国产电影'
        for name in ('样例 (2021)','样例'):
            folder=root/name;folder.mkdir(parents=True)
            (folder/'movie.nfo').write_text('<movie><tmdbid>123</tmdbid></movie>')
            (folder/'movie.strm').write_text('fixture')
        self.assertTrue(portal.check_in_library('国产电影','样例','2021',123))

    def test_work_reuses_legacy_tmdb_folder_and_existing_episode_filename(self):
        show=Path(portal.CINEMA2)/'国产剧集'/'样例剧'
        season=show/'Season 02';season.mkdir(parents=True)
        (show/'tvshow.nfo').write_text('<tvshow><uniqueid type="tmdb">124</uniqueid></tvshow>')
        existing=season/'旧标题 - S02E01.strm';existing.write_text('http://fixture/pending')
        season_detail={'episodes':[{'episode_number':1,'air_date':'2020-01-01','name':'第一集'}]}
        with patch.object(portal,'_tmdb',side_effect=lambda path,**kw:season_detail if '/season/' in path else TV):
            work=portal._work({'id':124,'media_type':'tv','season':2,'episode':1})
        self.assertEqual(work['folder'],'样例剧')
        path=Path(portal.tmdb_populate.episode_strm_path(str(season),work['item']['title'],2,1))
        self.assertEqual(path,existing)

    def test_ambiguous_tmdb_library_folders_stop_new_add(self):
        root=Path(portal.CINEMA2)/'国产电影'
        for name in ('样例 (2021)','样例'):
            folder=root/name;folder.mkdir(parents=True)
            (folder/'movie.nfo').write_text('<movie><tmdbid>123</tmdbid></movie>')
            (folder/'movie.strm').write_text('fixture')
        with self.assertRaises(ValueError):portal._library_folder('国产电影','样例','2021',123)

    def test_source_search_includes_exact_audited_locked_share_when_public_nodes_fail(self):
        from quark_vod import search
        locked_url='https://pan.quark.cn/s/locked-fixture'
        locked_name='各取所需(德国版)剧情介绍-'
        db.save_source_audit('国产剧集','样例剧 (2020)',2,1,True,True,locked_url,locked_name,'valid_4k')
        report={'results':[],'queries':['样例'],'nodes':[],'partial':False,'cached':False}
        with patch.object(portal,'_tmdb',side_effect=lambda path,**kw:TV if path=='tv/124' else {'episodes':[{'episode_number':1,'air_date':'2020-01-01','name':'第一集'}]}), \
             patch.object(search,'search_report',side_effect=OSError('search indexes offline')):
            result=portal.source_search({'id':124,'media_type':'tv','season':2,'episode':1})
        self.assertEqual(len(result['candidates']),1)
        self.assertTrue(result['candidates'][0]['locked'])
        self.assertEqual(result['candidates'][0]['share_url'],locked_url)
        self.assertEqual(result['candidates'][0]['name'],locked_name)
        self.assertEqual(result['candidates'][0]['quality']['level'],'4k')
        self.assertFalse(result['candidates'][0]['quality']['verified'])
        self.assertTrue(result['partial'])

    def test_saved_file_has_explicit_reuse_action_ahead_of_share_candidates(self):
        from quark_vod import search
        db.upsert_transfer('国产剧集','样例剧 (2020)',2,1,status='done',remote_path='/夸克网盘/点播/saved.mp4')
        share=search.SearchResult(engine='panshare',source='fixture',name='样例剧 S02E01 4K',year='',poster='',episodes=[],
            share_url='https://pan.quark.cn/s/fixture',pan_type='quark')
        with patch.object(portal,'_tmdb',side_effect=lambda path,**kw:TV if path=='tv/124' else {'episodes':[{'episode_number':1,'air_date':'2020-01-01','name':'第一集'}]}), \
             patch.object(search,'search_report',return_value={'results':[share],'queries':[],'nodes':[],'partial':False,'cached':False}):
            result=portal.source_search({'id':124,'media_type':'tv','season':2,'episode':1})
        self.assertTrue(result['saved'])
        self.assertEqual(result['candidates'][0]['status'],'saved')
        self.assertTrue(result['candidates'][0]['reuse_saved'])
        self.assertEqual(result['candidates'][1]['share_url'],share.share_url)
        selected=portal._selected({'token':result['candidates'][0]['token']})
        self.assertIsNone(selected['candidate'])
        with self.assertRaises(ValueError):portal.inspect_source({'token':result['candidates'][0]['token']})

    def test_locked_source_replaces_duplicate_public_search_row(self):
        from quark_vod import search
        url='https://pan.quark.cn/s/locked-fixture';name='样例剧 (2020) S02E01 德国版'
        db.save_source_audit('国产剧集','样例剧 (2020)',2,1,True,False,url,name,'valid_hd')
        candidate=search.SearchResult(engine='panshare',source='fixture',name=name,year='',poster='',episodes=[],share_url=url,pan_type='quark')
        with patch.object(portal,'_tmdb',side_effect=lambda path,**kw:TV if path=='tv/124' else {'episodes':[{'episode_number':1,'air_date':'2020-01-01','name':'第一集'}]}), \
             patch.object(search,'search_report',return_value={'results':[candidate],'queries':[],'nodes':[],'partial':False,'cached':False}):
            result=portal.source_search({'id':124,'media_type':'tv','season':2,'episode':1})
        self.assertEqual(len(result['candidates']),1)
        self.assertTrue(result['candidates'][0]['locked'])

    def test_add_reuses_saved_live_media_without_inspecting_selected_candidate(self):
        key=('国产电影','样例 (2021)',0,0)
        db.upsert_transfer(*key,status='done',remote_path='/夸克网盘/点播/sample.mp4')
        media=Path(portal.CINEMA2)/'国产电影'/'样例 (2021)'/'样例 (2021).strm'
        media.parent.mkdir(parents=True);media.write_text('http://localhost:8099/play?cat=x')
        db.save_strm_map(*key,str(media))
        work={'item':{'cat':key[0],'title':'样例','year':'2021'},'detail':{},'folder':key[1],
              'season':0,'episode':0}
        job={'stage':'queued'}
        portal._slots.acquire()
        response=Mock();response.__enter__=Mock(return_value=Mock(status=202));response.__exit__=Mock(return_value=False)
        with patch.object(portal.strm_manager,'is_live',return_value=True), \
             patch.object(portal,'_inspect',side_effect=AssertionError('saved playback must not inspect a new source')), \
             patch.object(portal,'_queue_preheat') as preheat, \
             patch.object(portal.urllib.request,'urlopen',return_value=response):
            portal._do_add({'work':work,'candidate':None},job)
        self.assertEqual(job['stage'],'preserved')
        self.assertIn('未验证当前可读性',job['message'])
        preheat.assert_not_called()
        self.assertEqual(job['strm_path'],str(media))
        job.update(key=key,refresh_ok=False)
        portal._remember(portal._jobs,'saved-job',job)
        with patch.object(portal,'_refresh') as refresh:
            status=portal.job_status('saved-job')
        self.assertEqual(status['state'],'preserved')
        refresh.assert_not_called()

    def test_add_retries_existing_saved_media_when_mapped_strm_is_pending(self):
        key=('国产电影','样例 (2021)',0,0)
        db.upsert_transfer(*key,status='done',remote_path='/夸克网盘/点播/sample.mp4')
        media=Path(portal.CINEMA2)/'国产电影'/'样例 (2021)'/'样例 (2021).strm'
        media.parent.mkdir(parents=True);media.write_text('http://localhost:8099/pending?cat=x')
        db.save_strm_map(*key,str(media))
        work={'item':{'cat':key[0],'title':'样例','year':'2021'},'detail':{},'folder':key[1],
              'season':0,'episode':0}
        job={'stage':'queued'}
        portal._slots.acquire()
        response=Mock();response.__enter__=Mock(return_value=Mock(status=202));response.__exit__=Mock(return_value=False)
        with patch.object(portal.strm_manager,'is_live',return_value=False), \
             patch.object(portal.strm_manager,'is_pending',return_value=True), \
             patch.object(portal,'_inspect',side_effect=AssertionError('saved playback must not inspect a new source')), \
             patch.object(portal,'_refresh',return_value=True), \
             patch.object(portal.urllib.request,'urlopen',return_value=response):
            portal._do_add({'work':work,'candidate':None},job)
        self.assertEqual(job['stage'],'submitted')
        self.assertTrue(job['refresh_ok'])

    def test_add_restores_missing_strm_for_existing_saved_video(self):
        key=('国产电影','样例 (2021)',0,0)
        db.upsert_transfer(*key,status='done',remote_path='/夸克网盘/点播/sample.mp4')
        media=Path(portal.CINEMA2)/'国产电影'/'样例 (2021)'/'样例 (2021).strm'
        work={'item':{'cat':key[0],'title':'样例','year':'2021'},'detail':{},'folder':key[1],
              'season':0,'episode':0}
        job={'stage':'queued'}
        portal._slots.acquire()
        response=Mock();response.__enter__=Mock(return_value=Mock(status=202));response.__exit__=Mock(return_value=False)
        with patch.object(portal.strm_manager,'is_live',return_value=False), \
             patch.object(portal.strm_manager,'is_pending',return_value=False), \
             patch.object(portal,'_inspect',side_effect=AssertionError('saved playback must not inspect a new source')), \
             patch.object(portal,'_refresh',return_value=True), \
             patch.object(portal.urllib.request,'urlopen',return_value=response):
            portal._do_add({'work':work,'candidate':None},job)
        self.assertEqual(job['stage'],'submitted')
        self.assertTrue(media.is_file())
        self.assertIn('/pending?',media.read_text())
        self.assertEqual(db.get_strm_path(*key),str(media))

    def test_missing_saved_episode_entry_restores_nfo_identity_and_mapping(self):
        key=('国产剧集','样例剧',2,1)
        db.upsert_transfer(*key,status='done',remote_path='/夸克网盘/点播/sample/episode.mp4')
        work={'item':{'cat':key[0],'title':'样例剧','year':'2020'},
              'detail':{'id':124,'seasons':[{'season_number':2,'name':'第 2 季'}]},
              'folder':'样例剧','season':2,'episode':1,
              'episode_data':{'name':'第一集','air_date':'2020-01-01'}}
        root=Path(portal.CINEMA2)/key[0]/key[1]
        media=root/'Season 02'/'样例剧 - S02E01.strm'
        job={'stage':'queued'};portal._slots.acquire()
        with patch.object(portal,'_refresh',return_value=True), \
             patch.object(portal,'_queue_preheat'):
            portal._do_add({'work':work,'candidate':None},job)
        self.assertEqual(job['stage'],'submitted')
        self.assertIn('<tmdbid>124</tmdbid>',(root/'tvshow.nfo').read_text())
        self.assertTrue((root/'Season 02'/'season.nfo').is_file())
        self.assertTrue(media.is_file())
        self.assertEqual(db.get_strm_path(*key),str(media))

    def test_existing_episode_metadata_is_preserved_during_add(self):
        from quark_vod import search
        root=Path(portal.CINEMA2)/'国产剧集'/'样例剧'
        season=root/'Season 02';season.mkdir(parents=True)
        show_nfo=root/'tvshow.nfo';show_nfo.write_text('<tvshow><custom>preserve</custom></tvshow>')
        season_nfo=season/'season.nfo';season_nfo.write_text('<season><custom>preserve</custom></season>')
        episode_nfo=season/'旧标题 - S02E01.nfo';episode_nfo.write_text('<episodedetails><custom>preserve</custom></episodedetails>')
        episode=season/'旧标题 - S02E01.strm';episode.write_text(config.PENDING_BASE_URL+'/pending?cat=x&t=y&s=2&e=1')
        candidate=search.SearchResult(engine='panshare',source='fixture',name='样例剧 S02E01',year='',poster='',episodes=[],
            share_url='https://pan.quark.cn/s/fixture',pan_type='quark')
        detail={'id':124,'seasons':[{'season_number':2}]}
        work={'item':{'cat':'国产剧集','title':'样例剧','year':'2020'},'detail':detail,'folder':'样例剧',
              'season':2,'episode':1,'episode_data':{'name':'新标题'}}
        job={'stage':'queued'}
        portal._slots.acquire()
        response=Mock();response.__enter__=Mock(return_value=Mock(status=202));response.__exit__=Mock(return_value=False)
        with patch.object(portal,'_inspect',return_value={'quality':{'level':'hd'}}), \
             patch.object(portal,'_refresh',return_value=True), \
             patch.object(portal.urllib.request,'urlopen',return_value=response), \
             patch.object(portal.tmdb_populate,'gen_tv_nfo') as tv, \
             patch.object(portal.tmdb_populate,'gen_season_nfo') as sn, \
             patch.object(portal.tmdb_populate,'gen_episode_nfo') as en:
            portal._do_add({'work':work,'candidate':candidate},job)
        self.assertEqual(job['stage'],'submitted')
        self.assertEqual((show_nfo.read_text(),season_nfo.read_text(),episode_nfo.read_text()),
                         ('<tvshow><custom>preserve</custom></tvshow>',
                          '<season><custom>preserve</custom></season>',
                          '<episodedetails><custom>preserve</custom></episodedetails>'))
        self.assertTrue(episode.exists())
        tv.assert_not_called();sn.assert_not_called();en.assert_not_called()

    def test_add_does_not_replace_existing_live_strm(self):
        from quark_vod import search
        key=('国产电影','样例 (2021)',0,0)
        path=Path(portal.CINEMA2)/'国产电影'/'样例 (2021)'/'样例 (2021).strm'
        path.parent.mkdir(parents=True);path.write_text('http://127.0.0.1:8099/play?cat=x&t=y')
        db.save_strm_map(*key,str(path))
        work={'item':{'cat':key[0],'title':'样例','year':'2021'},'detail':{},'folder':key[1],
              'season':0,'episode':0}
        candidate=search.SearchResult(engine='panshare',source='fixture',name='样例 (2021) 4K',year='2021',
            poster='',episodes=[],share_url='https://pan.quark.cn/s/new',pan_type='quark')
        job={'stage':'queued'};portal._slots.acquire()
        with patch.object(portal,'_inspect',return_value={'quality':{'level':'4k'}}), \
             patch.object(portal,'_refresh',return_value=True), \
             patch.object(portal,'_queue_preheat') as preheat:
            portal._do_add({'work':work,'candidate':candidate},job)
        self.assertEqual(job['stage'],'preserved')
        self.assertIn('未验证当前可读性',job['message'])
        self.assertEqual(path.read_text(),'http://127.0.0.1:8099/play?cat=x&t=y')
        preheat.assert_not_called()

    def test_saved_live_row_is_not_overwritten_by_candidate(self):
        from quark_vod import search
        key=('国产电影','样例 (2021)',0,0)
        db.upsert_transfer(*key,status='done',remote_path='/夸克网盘/点播/sample.mp4')
        path=Path(portal.CINEMA2)/'国产电影'/'样例 (2021)'/'样例 (2021).strm'
        path.parent.mkdir(parents=True);path.write_text('http://127.0.0.1:8099/play?cat=x&t=y')
        db.save_strm_map(*key,str(path))
        work={'item':{'cat':key[0],'title':'样例','year':'2021'},'detail':{},'folder':key[1],
              'season':0,'episode':0}
        candidate=search.SearchResult(engine='panshare',source='fixture',name='样例 4K',year='',poster='',episodes=[],
            share_url='https://pan.quark.cn/s/new',pan_type='quark')
        job={'stage':'queued'};portal._slots.acquire()
        with patch.object(portal,'_inspect',side_effect=AssertionError('live item must not inspect')), \
             patch.object(portal,'_queue_preheat',side_effect=AssertionError('live item must not restart playback')):
            portal._do_add({'work':work,'candidate':candidate},job)
        self.assertEqual(job['stage'],'preserved')
        self.assertIn('未验证当前可读性',job['message'])
        self.assertEqual(path.read_text(),'http://127.0.0.1:8099/play?cat=x&t=y')

    def test_yearless_library_folder_without_matching_nfo_is_not_guessed(self):
        show=Path(portal.CINEMA2)/'欧美剧集'/'样例无年份'
        show.mkdir(parents=True)
        (show/'Season 01').mkdir()
        (show/'Season 01'/'episode.strm').write_text('pending')
        self.assertFalse(portal.check_in_library('欧美剧集','样例无年份','2026',298550))

    def test_missing_season_and_future_episode_do_not_create_files(self):
        season={'episodes':[{'episode_number':1,'air_date':'2020-01-01'},{'episode_number':2,'air_date':'2099-01-01'}]}
        with patch.object(portal,'_tmdb',side_effect=lambda path,**kw:season if '/season/' in path else TV):
            with self.assertRaises(ValueError):portal._work({'id':124,'media_type':'tv','season':1})
            with self.assertRaises(ValueError):portal._work({'id':124,'media_type':'tv','season':2,'episode':2})
            work=portal._work({'id':124,'media_type':'tv','season':2,'episode':1})
        self.assertEqual(work['folder'],'样例剧 (2020)')
        self.assertEqual(work['item']['episodes'],[1])

    def test_external_payload_cannot_choose_directory_or_share(self):
        with self.assertRaises(ValueError):portal.add_to_library({'cat':'../../tmp','title':'x','share_url':'http://evil'})
        with patch.object(portal,'_tmdb',return_value=MOVIE):
            work=portal._work({'id':123,'media_type':'movie','cat':'../../tmp','title':'evil'})
        self.assertEqual(work['item']['cat'],'国产电影')
        self.assertEqual(work['folder'],'样例 (2021)')

    def test_source_inspection_is_read_only_and_file_dimensions_override_pack(self):
        tr=Mock();tr.resolve_share.return_value=('token','id',[]);tr.share_files.return_value=[]
        tr.inspect_share_video.return_value={'name':'样例.2160x1080.mp4','size':1024**3}
        work={'item':{'title':'样例','original_title':''},'folder':'样例 (2021)','season':0,'episode':0}
        candidate=Mock(name='unused');candidate.name='样例 4K';candidate.share_url='https://pan.quark.cn/s/abc'
        with patch.object(portal,'QuarkTransfer',return_value=tr):result=portal._inspect({'work':work,'candidate':candidate})
        self.assertEqual(result['quality']['level'],'hd');tr.transfer.assert_not_called()

    def test_done_without_activated_strm_is_not_ready(self):
        key=('国产电影','样例 (2021)',0,0)
        db.upsert_transfer(*key,status='done',remote_path='/夸克网盘/点播/vod/a.mkv')
        portal._remember(portal._jobs,'job',{'key':key,'stage':'submitted','strm_path':'/missing','refresh_ok':False})
        self.assertEqual(portal.job_status('job')['state'],'preparing')

    def test_jellyfin_refresh_has_auth(self):
        response=Mock();response.__enter__=Mock(return_value=Mock(status=204));response.__exit__=Mock(return_value=False)
        with patch.object(config,'get_jellyfin2_key',return_value='fixture'),patch.object(portal.urllib.request,'urlopen',return_value=response) as request:
            self.assertTrue(portal._refresh())
        self.assertEqual(request.call_args.args[0].get_header('Authorization'),'MediaBrowser Token="fixture"')

    def test_enqueue_deduplicates_work_and_does_not_claim_ready(self):
        work={'item':{'cat':'国产电影'},'folder':'样例 (2021)','season':0,'episode':0}
        portal._remember(portal._candidates,'token',{'work':work,'candidate':Mock()})
        with patch.object(portal._pool,'submit') as submit:
            first=portal.add_to_library({'token':'token'});second=portal.add_to_library({'token':'token'})
        self.assertEqual(first['job'],second['job']);submit.assert_called_once();portal._slots.release()
        self.assertEqual(portal.job_status(first['job'])['state'],'queued')


if __name__=='__main__':unittest.main(verbosity=2)
