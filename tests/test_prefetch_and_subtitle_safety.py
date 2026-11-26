"""Offline identity and sidecar regressions for prefetch and subtitles."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

TMP=tempfile.TemporaryDirectory(prefix='vod-sidecar-prefetch-')
os.environ.update(VOD_BASE_DIR=TMP.name,VOD_DATA_DIR=TMP.name+'/data',
                  VOD_CINEMA2_DIR=TMP.name+'/cinema2',VOD_JELLYFIN_ENABLED='1',AI_PARSE_ENABLED='0')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from quark_vod import config,db,strm_manager,subtitles,prefetch
from quark_vod.identity import Work
from quark_vod.transfer import QuarkTransfer

class Response:
    def __init__(self,value):self.data=json.dumps(value).encode()
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,n):return self.data[:n]

class PrefetchSubtitleSafetyTests(unittest.TestCase):
    def setUp(self):
        db.close();config.ensure_dirs();db.init_db()
        with db._conn() as con:
            con.execute('DELETE FROM strm_map')
        self.work=Work('欧美剧集','精确剧 (2026)',1,1)
        self.path=Path(strm_manager.strm_path_for(*self.work.args))
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.path.write_text('pending fixture')
        db.save_strm_map(*self.work.args,str(self.path))
        self.sessions=[{'NowPlayingItem':{'SeriesName':'精确剧','ParentIndexNumber':1,'IndexNumber':1,
                         'Path':'/cinema2/'+str(self.path.relative_to(config.CINEMA2_DIR)),
                         'RunTimeTicks':1000},'PlayState':{'PositionTicks':500,'IsPaused':False}}]
        self.patches=[patch.object(config,'get_jellyfin2_key',return_value='fixture'),
                      patch('urllib.request.urlopen',side_effect=lambda *a,**k:Response(self.sessions))]
        for item in self.patches:item.start();self.addCleanup(item.stop)

    def test_prefetch_matches_dated_work_by_exact_strm_path_without_series_year(self):
        self.assertTrue(prefetch.SmartPrefetcher()._watching(self.work))
        self.sessions[0]['PlayState']['IsPaused']=True
        self.assertFalse(prefetch.SmartPrefetcher()._watching(self.work))

    def test_prefetch_rejects_wrong_work_episode_and_version(self):
        watcher=prefetch.SmartPrefetcher()
        self.sessions[0]['NowPlayingItem']['Path']='/cinema2/欧美剧集/其他 (2026)/Season 01/S01E01.strm'
        self.assertFalse(watcher._watching(self.work))
        self.sessions[0]['NowPlayingItem']['Path']='/cinema2/'+str(self.path.relative_to(config.CINEMA2_DIR))
        hdr=Work(*self.work.args,'hdr')
        hdrpath=strm_manager.ensure_variant(*self.work.args,'hdr')
        self.assertFalse(watcher._watching(hdr))
        self.sessions[0]['NowPlayingItem']['Path']='/cinema2/'+str(Path(hdrpath).relative_to(config.CINEMA2_DIR))
        self.assertTrue(watcher._watching(hdr))
        self.sessions[0]['NowPlayingItem']['IndexNumber']=2
        self.assertFalse(watcher._watching(hdr))

    def test_prefetch_uses_selected_media_source_and_rejects_unknown_selection(self):
        item=self.sessions[0]['NowPlayingItem']
        item['MediaSources']=[{'Id':'default','Path':item['Path']},
                              {'Id':'other','Path':'/cinema2/other.strm'}]
        watcher=prefetch.SmartPrefetcher()
        self.assertFalse(watcher._watching(self.work))
        self.sessions[0]['PlayState']['MediaSourceId']='other'
        self.assertFalse(watcher._watching(self.work))
        self.sessions[0]['PlayState']['MediaSourceId']='default'
        self.assertTrue(watcher._watching(self.work))

    def test_subtitle_choice_deduplicates_sidecar_destination_and_ignores_english_only(self):
        selected={'file_name':'精确剧.S01E01.mkv','relative_path':'精确剧/精确剧.S01E01.mkv'}
        def item(name):return {'file_name':name,'relative_path':'精确剧/'+name,'size':1024}
        chs=item('精确剧.S01E01.CHS.ass')
        lower=item('精确剧.S01E01.双语.ass')
        english=item('精确剧.S01E01.ENG.srt')
        cht=item('精确剧.S01E01.CHT.ass')
        chosen=subtitles.matching_subtitles(QuarkTransfer(),[lower,chs,english,cht],selected,1,1)
        self.assertEqual(chosen,[chs,cht])

    def test_prefetch_rejects_dirty_mapping_even_when_session_matches_it(self):
        hdrpath=Path(strm_manager.ensure_variant(*self.work.args,'hdr'))
        db.save_strm_map(*self.work.args,str(hdrpath))
        self.sessions[0]['NowPlayingItem']['Path']='/cinema2/'+str(hdrpath.relative_to(config.CINEMA2_DIR))
        self.assertFalse(prefetch.SmartPrefetcher()._watching(self.work))

    def test_activation_empty_paths_is_not_success(self):
        original=self.path.read_text()
        with patch.object(strm_manager,'activation_paths',return_value=[]):
            with self.assertRaises(FileNotFoundError):
                strm_manager.activate(*self.work.args,'http://fixture.invalid/d/file.mkv')
        self.assertEqual(self.path.read_text(),original)

    def test_historical_default_mapping_remains_valid(self):
        self.assertEqual(strm_manager.activation_paths(*self.work.args),[str(self.path)])

    def test_symlink_library_mapping_cannot_activate_outside_file(self):
        outside=Path(TMP.name)/'external-file.strm';outside.write_text('keep')
        self.path.unlink();self.path.symlink_to(outside)
        with self.assertRaises(FileNotFoundError):
            strm_manager.activate(*self.work.args,'http://fixture.invalid/d/file.mkv')
        self.assertEqual(outside.read_text(),'keep')
        self.path.unlink();self.path.write_text('pending fixture')

    def test_dirty_default_map_cannot_activate_hdr_file(self):
        hdrpath=Path(strm_manager.ensure_variant(*self.work.args,'hdr'))
        db.save_strm_map(*self.work.args,str(hdrpath))
        # Remove the valid default fallback to expose only the dirty mapping.
        self.path.unlink()
        original=hdrpath.read_text()
        with self.assertRaises(FileNotFoundError):
            strm_manager.activate(*self.work.args,'http://fixture.invalid/d/file.mkv')
        self.assertEqual(hdrpath.read_text(),original)

    def test_subtitle_outside_library_mapping_retains_intent_without_download(self):
        outside=Path(TMP.name)/'outside.strm';outside.write_text('fixture')
        db.save_strm_map(*self.work.args,str(outside))
        selected={'file_name':'精确剧.S01E01.mkv','relative_path':'精确剧/精确剧.S01E01.mkv'}
        source={'file_name':'精确剧.S01E01.CHS.ass','relative_path':'精确剧/精确剧.S01E01.CHS.ass',
                'size':1024,'fid':'sub-outside'}
        transfer=Mock()
        transfer.extract_episode_num.return_value=1
        transfer._get_quark_fid.return_value='slot'
        transfer._quark_list_fid.return_value=[{'name':source['file_name'],'size':1024,'fid':'saved','is_dir':False}]
        with patch('quark_vod.download_http.urlopen') as download:
            with self.assertRaises(RuntimeError):
                subtitles.save_companions(transfer,self.work,99,[source],selected,
                                         'https://pan.quark.cn/s/fixture',config.QUARK_VOD_ROOT+'/vod_fixture')
            download.assert_not_called()
        self.assertTrue((Path(config.DATA_DIR)/'subtitle_intents/99-sub-outside.json').exists())

def tearDownModule():
    db.close();TMP.cleanup()

if __name__=='__main__':unittest.main(verbosity=2)
