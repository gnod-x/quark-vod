"""Safety and behavior regressions for the evolution transfer/variant/account pipeline."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

TMP=tempfile.TemporaryDirectory(prefix='vod-transfer-evolution-')
os.environ.update(VOD_BASE_DIR=TMP.name,VOD_DATA_DIR=TMP.name+'/data',
                  VOD_CINEMA2_DIR=TMP.name+'/cinema',AI_PARSE_ENABLED='0',VOD_JELLYFIN_ENABLED='0',
                  SELFHEAL_ENABLED='0')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from quark_vod import config,db,strm_manager,server,cleaner,selfheal,cleanup
from quark_vod.identity import Work
from quark_vod.quality import parse_media_tags,video_rank
from quark_vod.transfer import QuarkTransfer,TransferFailed,TransferNotReady,ConfirmedNonVideo,is_candidate_video
from quark_vod.subtitles import matching_subtitles
from quark_vod.quark_accounts import AccountPool

class Response:
    status=206
    headers={'Content-Type':'video/x-matroska'}
    def __init__(self,data):self.data=data
    def read(self,n=-1):return self.data[:n] if n>=0 else self.data
    def __enter__(self):return self
    def __exit__(self,*_):pass

class EvolutionTests(unittest.TestCase):
    def setUp(self):
        db.close()
        config.ensure_dirs()
        db.init_db()
        with db._conn() as con:
            for table in ('transfers','strm_map','source_audits','cleanup_queue','dead_sources','playback_access'):
                con.execute('DELETE FROM '+table)

    def item(self,name,parent='样例 (2026)',fid='v1'):
        return {'file_name':name,'relative_path':parent+'/'+name,'size':2*1024**3,'fid':fid,
                'parent_fid':'folder','share_fid_token':'synthetic'}

    def test_known_archives_docs_audio_installers_never_become_video_by_size(self):
        for extension in ('zip','rar','7z','pdf','apk','exe','mp3','flac','iso','txt'):
            self.assertFalse(is_candidate_video('S01E01.'+extension,80*1024**3),extension)
        for extension in ('mkv1','mp41','mov1','bin','dat',''):
            self.assertTrue(is_candidate_video('S01E01'+('.'+extension if extension else ''),1024**3))
        self.assertFalse(is_candidate_video('small.dat',10))

    def test_exact_identity_allows_episode_titles_audio_and_subtitle_groups(self):
        tr=QuarkTransfer()
        for suffix in ('风起澹州','粤语.双语.简繁.特效字幕','-桜都字幕组','-弯弯字幕组'):
            item=self.item('样例.S01E01.'+suffix+'.mkv')
            self.assertIsNotNone(tr.select_share_video([item],'样例 (2026)',1,1),suffix)
        self.assertIsNone(tr.select_share_video([self.item('其他作品.S01E01.风起澹州.mkv')],'样例 (2026)',1,1))
        self.assertIsNone(tr.select_share_video([self.item('样例.S01E01.幕后花絮.mkv')],'样例 (2026)',1,1))

    def test_parts_and_tv_remasters_are_supported_but_explicit_parts_conflict(self):
        tr=QuarkTransfer()
        for label in ('Part1','Part2','Remastered','Remaster'):
            self.assertIsNotNone(tr.select_share_video([self.item('样例.S01E01.'+label+'.mkv')],'样例 (2026)',1,1))
        item=self.item('样例.Part2.S01E01.2160p.mkv','样例 Part1 (2026)')
        self.assertIsNone(tr.select_share_video([item],'样例 Part1 (2026)',1,1))

    def test_s00_specials_have_tv_identity_and_exact_episode(self):
        work=Work('日本动漫','样例 (2026)',0,1)
        self.assertEqual(work.args,('日本动漫','样例 (2026)',0,1))
        with self.assertRaises(ValueError):Work('电影','样例',0,1)
        item=self.item('样例.S00E01.SP.2160p.mkv','样例 (2026)/Season 00')
        self.assertIsNotNone(QuarkTransfer().select_share_video([item],work.title,0,1))
        self.assertIsNone(QuarkTransfer().select_share_video([item],work.title,0,2))

    def test_air_date_does_not_fragment_into_fake_episode(self):
        tr=QuarkTransfer()
        self.assertIsNone(tr.extract_episode_num('综艺.20261120.2160p.mkv'))
        item=self.item('样例.20261120.2160p.mkv')
        self.assertIsNone(tr.select_share_video([item],'样例 (2026)',1,12))
        self.assertIsNotNone(tr.select_share_video([item],'样例 (2026)',1,12,aired='20261120'))

    def test_absolute_numbering_requires_offset_and_explicit_matching_package(self):
        tr=QuarkTransfer()
        item=self.item('E13.mkv','样例 (2026) 第二季')
        self.assertIsNone(tr.select_share_video([item],'样例 (2026)',2,1))
        self.assertIsNotNone(tr.select_share_video([item],'样例 (2026)',2,1,episode_offset=12))
        self.assertIsNone(tr.select_share_video([self.item('E13.mkv','其他作品 第二季')],'样例 (2026)',2,1,episode_offset=12))
        self.assertIsNone(tr.select_share_video([self.item('E13.mkv','样例 (2026) 第三季')],'样例 (2026)',2,1,episode_offset=12))

    def test_quality_v2_resolution_dv_compatibility_and_oversized_penalty(self):
        tags=lambda name,size=2*1024**3:parse_media_tags(name,size,'tv')
        self.assertGreater(tags('1080p')['score'],tags('720p')['score'])
        self.assertGreater(tags('720p')['score'],tags('480p')['score'])
        self.assertGreater(tags('2160p HDR10')['score'],tags('2160p DV')['score'])
        self.assertTrue(tags('2160p DV')['dv_requires_support'])
        self.assertFalse(tags('2160p DV HDR10')['dv_requires_support'])
        self.assertLess(tags('2160p REMUX',80*1024**3)['score'],tags('2160p',2*1024**3)['score'])
        self.assertLess(tags('384×2160')['resolution'],2160)
        self.assertIn('特效字幕',tags('2160p 国粤双语 特效字幕')['badges'])
        self.assertFalse(tags('2160p')['verified'])

    def test_variant_selection_cannot_return_wrong_audio_or_hdr(self):
        tr=QuarkTransfer()
        files=[self.item('样例.S01E01.2160p.HDR10.mkv',fid='hdr'),
               self.item('样例.S01E01.1080p.国语.SDR.mkv',fid='dub')]
        self.assertEqual(tr.select_share_video(files,'样例 (2026)',1,1,variant='dubbed')['share']['fid'],'dub')
        self.assertEqual(tr.select_share_video(files,'样例 (2026)',1,1,variant='hdr')['share']['fid'],'hdr')
        self.assertIsNone(tr.select_share_video(files,'样例 (2026)',1,1,selected_fid='outside'))
        self.assertIsNone(tr.select_share_video(files,'样例 (2026)',1,1,variant='hdr',selected_fid='dub'))

    def test_share_tree_prunes_other_seasons_and_lru_reuses_complete_snapshot(self):
        tr=QuarkTransfer()
        tr.get_cookie=Mock(return_value='')
        tr._share_page_all=Mock(return_value=[self.item('样例.S01E01.mkv'),dict(self.item('样例.S01E01.chs.ass'),size=1024)])
        roots=[{'dir':True,'fid':'s1','file_name':'Season 01'},
               {'dir':True,'fid':'s2','file_name':'Season 02'},
               {'dir':True,'fid':'ex','file_name':'Extras'}]
        one=tr.share_files('synthetic','id',roots,season=1)
        self.assertEqual(len(one),2)
        tr._share_page_all.assert_called_once_with('synthetic','id','s1')
        two=tr.share_files('synthetic2','id',roots,season=1)
        self.assertEqual(one,two)
        self.assertEqual(tr._share_page_all.call_count,1)

    def test_bad_magic_is_terminal_but_network_failure_retains_intent(self):
        tr=QuarkTransfer(storage=Mock())
        tr.storage.direct_url.return_value='http://fixture.invalid/d/file'
        file={'fid':'f','name':'masked.dat','size':1024**3}
        with patch('quark_vod.download_http.urlopen',return_value=Response(b'%PDF-1.7'+b'0'*64)):
            with self.assertRaises(ConfirmedNonVideo):tr.sniff_and_fix_file(file,config.QUARK_VOD_ROOT+'/vod_test')
        with patch('quark_vod.download_http.urlopen',side_effect=OSError()):
            self.assertIsNone(tr.sniff_and_fix_file(file,config.QUARK_VOD_ROOT+'/vod_test'))
        with patch('quark_vod.download_http.urlopen',return_value=Response(b'\x89PNG\r\n\x1a\n\x1aE\xdf\xa3'+b'0'*64)),patch.object(tr,'rename_quark_file',return_value=True):
            self.assertTrue(tr.sniff_and_fix_file(file,config.QUARK_VOD_ROOT+'/vod_test')['name'].endswith('.mkv'))

    def test_confirmed_nonvideo_only_clears_after_exact_slot_deletion(self):
        work=Work('电影','样例 (2026)')
        slot=config.QUARK_VOD_ROOT+'/vod_test'
        tid=db.upsert_transfer(*work.args,status='saving',share_url='https://pan.quark.cn/s/fixture',
                               save_started_at=db.now(),save_target=slot+'/masked.dat',save_file_fid='f')
        item={'fid':'f','name':'masked.dat','size':1024**3,'is_dir':False}
        tr=QuarkTransfer(storage=Mock())
        tr._get_quark_fid=Mock(return_value='folder')
        tr._quark_list_fid=Mock(return_value=[item])
        tr.delete_remote=Mock(side_effect=TransferFailed('unconfirmed deletion'))
        with patch.object(cleanup,'_restore_all',return_value=True):
            with self.assertRaises(TransferFailed):tr._discard_confirmed_nonvideo(work,dict(db.get_transfer_by_id(tid)),item,slot)
            self.assertIsNotNone(db.get_transfer_by_id(tid)['save_started_at'])
            tr.delete_remote=Mock(return_value=True)
            tr._discard_confirmed_nonvideo(work,dict(db.get_transfer_by_id(tid)),item,slot)
        row=db.get_transfer_by_id(tid)
        self.assertIsNone(row['save_started_at'])
        self.assertTrue(db.is_dead_source('https://pan.quark.cn/s/fixture'))
        tr.delete_remote.assert_called_once_with(slot)

    def test_selfheal_reuses_fid_from_parent_listing(self):
        root=config.QUARK_VOD_ROOT
        tr=Mock()
        tr._quark_list.return_value=[{'name':'vod_test','fid':'folder','is_dir':True}]
        tr._quark_list_fid.return_value=[{'name':'episode.mkv','fid':'f','is_dir':False}]
        result=selfheal._remote_exists_batch(tr,[root+'/vod_test/episode.mkv',root+'/vod_test/missing.mkv'])
        self.assertTrue(result[root+'/vod_test/episode.mkv'])
        self.assertFalse(result[root+'/vod_test/missing.mkv'])
        tr._quark_list.assert_called_once_with(root)
        tr._quark_list_fid.assert_called_once_with('folder')

    def test_readiness_head_is_reused_for_obfuscation(self):
        url='http://fixture.invalid/d/probe'
        cleaner._probe_cache.clear()
        with patch.object(server,'get_openlist',return_value=None),patch('quark_vod.download_http.urlopen',return_value=Response(b'\x89PNG\r\n\x1a\n\x1aE\xdf\xa3'+b'0'*64)) as fetch:
            self.assertTrue(server._wait_direct_url_ready(url,max_wait=1))
            self.assertEqual(cleaner.detect_stream_obfuscation(url),(True,8,'matroska'))
        self.assertEqual(fetch.call_count,1)

    def test_scout_rotation_and_rolling_main_cookie_stays_in_memory_by_default(self):
        with patch.object(config,'QUARK_SCOUT_COOKIES',('scout=a','scout=b')),patch.object(config,'QUARK_COOKIE_STATE_FILE',''):
            pool=AccountPool()
            self.assertEqual(pool.scout('main=x'),'scout=a')
            self.assertEqual(pool.scout('main=x'),'scout=b')
            self.assertEqual(pool.scout('main=x'),'scout=a')
            pool.remember_token('main-token','main=x')
            updated=pool.rolling('main=x',['__puus=rotated; Path=/; HttpOnly'],'main=x')
            self.assertIn('__puus=rotated',updated)
            self.assertEqual(pool.main('main=x'),updated)
            self.assertEqual(pool.token_cookie('main-token','fallback'),updated)
            self.assertEqual(pool.scout('main=x'),'scout=b')

    def test_main_revalidates_account_bound_save_token_after_scout_selection(self):
        from quark_vod.transfer import QUARK_SHARE_SAVE
        tr=QuarkTransfer(storage=Mock())
        tr.ensure_ready=Mock()
        tr.ensure_vod_dir=Mock()
        tr._get_quark_fid=Mock(return_value='slot')
        scout=self.item('样例.S01E01.mkv')
        main=dict(scout,share_fid_token='main-file-token')
        tr.resolve_share=Mock(side_effect=[('scout-token','share',[scout]),('main-token','share',[main])])
        tr.share_files=Mock(return_value=[scout])
        tr._share_page_all=Mock(return_value=[main])
        tr._quark_list_fid=Mock(side_effect=[[],[{'name':scout['file_name'],'fid':'saved','size':scout['size'],'is_dir':False}]])
        tr._check_capacity=Mock(return_value=(True,10**12))
        captured=[]
        def request(url,**kwargs):
            if url==QUARK_SHARE_SAVE:captured.append(kwargs['payload'])
            return {'code':0,'data':{}}
        tr._request=Mock(side_effect=request)
        with patch.object(config,'QUARK_SCOUT_COOKIES',('scout=a',)):
            tr._do_transfer('剧集','样例 (2026)',1,1,'https://pan.quark.cn/s/fixture')
        self.assertEqual(captured[0]['stoken'],'main-token')
        self.assertEqual(captured[0]['fid_token_list'],['main-file-token'])
        self.assertEqual(tr.resolve_share.call_args.kwargs,{'account':'main'})

    def test_subtitles_are_same_episode_and_same_enumerated_directory_only(self):
        tr=QuarkTransfer()
        selected=self.item('样例.S01E01.mkv')
        good=dict(self.item('样例.S01E01.chs.ass'),size=2048)
        bad=dict(self.item('样例.S01E02.chs.ass'),size=2048)
        outside=dict(good,relative_path='其他作品/'+good['file_name'])
        self.assertEqual(matching_subtitles(tr,[good,bad,outside],selected,1,1),[good])



    def test_top_three_previews_overlap_and_choose_actual_file_quality(self):
        from quark_vod import search
        work=Work('电影','并发样例 (2026)')
        candidates=[search.SearchResult(engine='fixture',source='fixture',name=work.title,year='2026',
                       poster='',episodes=[],share_url='https://pan.quark.cn/s/'+str(i),pan_type='quark') for i in range(3)]
        barrier=threading.Barrier(3)
        tr=Mock()
        def preview(url,*args,**kwargs):
            barrier.wait(timeout=2)
            index=int(url.rsplit('/',1)[1])
            return {'name':('2160p' if index==2 else '720p')+'.mkv','size':2*1024**3,'media_type':'movie'}
        tr.inspect_share_video.side_effect=preview
        tr.transfer.return_value=(123,config.QUARK_VOD_ROOT+'/vod_preview/file.mkv')
        with patch.object(server,'get_transfer',return_value=tr),patch.object(server,'_finish',return_value='ready'),patch.object(
                strm_manager,'library_search_identity',return_value=(work.title,[])),patch.object(
                search,'search_report',return_value={'results':candidates,'partial':False}),patch.object(
                search,'rank_candidates',return_value=candidates):
            self.assertEqual(server._resolve_once(work),'ready')
        self.assertEqual(tr.inspect_share_video.call_count,3)
        self.assertEqual(tr.transfer.call_args.args[-1],candidates[2].share_url)

    def test_durable_subtitle_recovery_never_reposts_an_unknown_submission(self):
        from quark_vod import subtitles
        work=Work('剧集','字幕片 (2026)',1,1)
        slot=config.QUARK_VOD_ROOT+'/vod_subtitle'
        tid=db.complete_transfer(*work.args,slot+'/video.mkv',1024,db.now()+86400)
        tr=QuarkTransfer(storage=Mock())
        source=dict(self.item('字幕片.S01E01.chs.ass',parent='字幕片 (2026)',fid='sub-recovery'),size=1024)
        selected=self.item('字幕片.S01E01.mkv',parent='字幕片 (2026)')
        with patch.object(subtitles,'schedule_pending'):
            subtitles.queue_companions(tr,work,tid,[source],selected,'https://pan.quark.cn/s/fixture',slot)
        journal=Path(config.DATA_DIR)/'subtitle_intents'/f'{tid}-sub-recovery.json'
        self.assertNotIn('share_fid_token',journal.read_text())
        tr._get_quark_fid=Mock(return_value='folder')
        tr._quark_list_fid=Mock(return_value=[])
        tr.resolve_share=Mock(return_value=('token','share',[]))
        tr._share_page_all=Mock(return_value=[source])
        tr._request=Mock(side_effect=TimeoutError())
        self.assertEqual(subtitles.recover_pending(tr),1)
        self.assertEqual(tr._request.call_count,1)
        state=json.loads(journal.read_text())
        self.assertEqual(state['phase'],'submitting')
        state['retry_after']=0
        journal.write_text(json.dumps(state))
        self.assertEqual(subtitles.recover_pending(tr),1)
        self.assertEqual(tr._request.call_count,1)
        state=json.loads(journal.read_text());state['retry_after']=0
        journal.write_text(json.dumps(state))
        tr._quark_list_fid.return_value=[{'fid':'saved','name':source['file_name'],'size':1024,'is_dir':False}]
        self.assertEqual(subtitles.recover_pending(tr),1)
        self.assertFalse(journal.exists())
        self.assertEqual(tr._request.call_count,1)

    def test_same_tv_episode_versions_keep_transfer_and_strm_state_independent(self):
        work=Work('欧美剧集','独立版本剧 (2026)',1,1)
        default=Path(strm_manager.strm_path_for(*work.args))
        default.parent.mkdir(parents=True,exist_ok=True)
        default.write_text(strm_manager.PENDING_PREFIX+'?cat=欧美剧集&t=独立版本剧+(2026)&s=1&e=1')
        db.save_strm_map(*work.args,str(default))
        paths={'':default,**{variant:Path(strm_manager.ensure_variant(*work.args,variant))
                             for variant in ('hdr','dubbed')}}
        remotes={variant:config.QUARK_VOD_ROOT+'/vod_episode_'+(variant or 'default')+'/episode.mkv'
                 for variant in paths}
        ids={'hdr':db.complete_transfer(*work.args,remotes['hdr'],1024,db.now()+86400,variant='hdr')}
        # The default or dubbed request must never adopt another version's done row.
        self.assertEqual(db.find_done_transfer(*work.args,variant='hdr')['id'],ids['hdr'])
        self.assertIsNone(db.find_done_transfer(*work.args))
        self.assertIsNone(db.find_done_transfer(*work.args,variant='dubbed'))
        self.assertIsNone(db.get_transfer(*work.args))
        for variant in ('','dubbed'):
            ids[variant]=db.complete_transfer(*work.args,remotes[variant],2048,db.now()+86400,
                                              variant=variant)
        self.assertEqual(len(set(ids.values())),3)
        db.close()
        for variant in paths:
            with self.subTest(variant=variant):
                self.assertEqual(db.get_transfer(*work.args,variant=variant)['id'],ids[variant])
                self.assertEqual(db.find_done_transfer(*work.args,variant=variant)['remote_path'],remotes[variant])
                self.assertEqual(db.get_strm_path(*work.args,variant=variant),str(paths[variant]))
                self.assertIsNone(db.find_done_transfer(work.cat,work.title,1,2,variant=variant))
        with patch.object(strm_manager,'trigger_jellyfin_refresh_item'):
            for variant in ('','dubbed'):
                strm_manager.activate(*work.args,config.OPENLIST_PUBLIC+'/d'+config.play_path(remotes[variant]),
                                      variant=variant)
        untouched_text={variant:paths[variant].read_text() for variant in ('','dubbed')}
        untouched_rows={variant:dict(db.get_transfer_by_id(ids[variant])) for variant in ('','dubbed')}
        # Activation repairs only this exact episode/version, even with siblings present.
        db.upsert_transfer(*work.args,variant='hdr',status='failed',error='activation retry')
        with patch.object(strm_manager,'trigger_jellyfin_refresh_item'):
            strm_manager.activate(*work.args,config.OPENLIST_PUBLIC+'/d'+config.play_path(remotes['hdr']),
                                  variant='hdr')
        self.assertEqual(db.get_transfer_by_id(ids['hdr'])['status'],'done')
        self.assertIn('variant=hdr',paths['hdr'].read_text())
        self.assertTrue(cleanup._restore_all(dict(db.get_transfer_by_id(ids['hdr'])),verbose=False))
        self.assertTrue(strm_manager.is_pending(paths['hdr'].read_text().strip()))
        for variant in ('','dubbed'):
            with self.subTest(untouched_variant=variant):
                self.assertEqual(paths[variant].read_text(),untouched_text[variant])
                self.assertEqual(dict(db.get_transfer_by_id(ids[variant])),untouched_rows[variant])
                self.assertEqual(db.get_strm_path(*work.args,variant=variant),str(paths[variant]))

    def test_multi_versions_activate_and_cleanup_independently(self):
        work=Work('电影','版本片 (2026)')
        base=Path(config.CINEMA2_DIR)/work.cat/work.title
        base.mkdir(parents=True,exist_ok=True)
        default=base/(work.title+'.strm')
        default.write_text(strm_manager.PENDING_PREFIX+'?cat=电影&t=版本片+(2026)')
        db.save_strm_map(*work.args,str(default))
        hdr=Path(strm_manager.ensure_variant(*work.args,'hdr'))
        dubbed=Path(strm_manager.ensure_variant(*work.args,'dubbed'))
        sdr=Path(strm_manager.ensure_variant(*work.args,'sdr'))
        self.assertIn(' - 4K HDR 原声.strm',str(hdr))
        default_before=default.read_text()
        dubbed_before=dubbed.read_text()
        remote=config.QUARK_VOD_ROOT+'/vod_hdr/file.mkv'
        tid=db.upsert_transfer(*work.args,variant='hdr',status='done',remote_path=remote)
        default_tid=db.upsert_transfer(*work.args,status='failed',remote_path=remote)
        dubbed_tid=db.upsert_transfer(*work.args,variant='dubbed',status='failed',remote_path=remote)
        with patch.object(strm_manager,'trigger_jellyfin_refresh_item'):
            strm_manager.activate(*work.args,config.OPENLIST_PUBLIC+'/d'+config.play_path(remote),variant='hdr')
        self.assertIn('variant=hdr',hdr.read_text())
        self.assertEqual(db.get_transfer_by_id(default_tid)['status'],'failed')
        self.assertEqual(db.get_transfer_by_id(dubbed_tid)['status'],'failed')
        self.assertEqual(default.read_text(),default_before)
        self.assertEqual(dubbed.read_text(),dubbed_before)
        self.assertTrue(cleanup._restore_all(dict(db.get_transfer_by_id(tid)),verbose=False))
        self.assertIn('variant=hdr',hdr.read_text())
        self.assertTrue(strm_manager.is_pending(hdr.read_text().strip()))
        self.assertEqual(default.read_text(),default_before)
        self.assertEqual(dubbed.read_text(),dubbed_before)
        self.assertEqual(strm_manager.scan_and_register(),4)
        self.assertEqual(db.get_strm_path(*work.args,variant='sdr'),str(sdr))
        self.assertEqual(db.get_strm_path(*work.args,variant='hdr'),str(hdr))

def tearDownModule():
    db.close()
    TMP.cleanup()

if __name__=='__main__':unittest.main(verbosity=2)
