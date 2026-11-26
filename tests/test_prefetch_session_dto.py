"""Actual Jellyfin Sessions omit MediaSources for a selected STRM item."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

TMP=tempfile.TemporaryDirectory(prefix='vod-real-session-dto-')
os.environ.update(VOD_ENV_FILE='',VOD_BASE_DIR=TMP.name,VOD_DATA_DIR=TMP.name+'/data',
                  VOD_CINEMA2_DIR=TMP.name+'/cinema2',VOD_JELLYFIN_ENABLED='1',AI_PARSE_ENABLED='0')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from quark_vod import config,db,prefetch,strm_manager
from quark_vod.identity import Work


class Response:
    def __init__(self,value):self.value=json.dumps(value).encode()
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,limit):return self.value[:limit]


class RealSessionsDtoTests(unittest.TestCase):
    def setUp(self):
        config.ensure_dirs();db.init_db()
        self.work=Work('国产剧集','精确剧 (2026)',1,1)
        self.path=Path(strm_manager.strm_path_for(*self.work.args))
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.path.write_text('http://fixture.invalid/play')
        db.save_strm_map(*self.work.args,str(self.path))
        self.item_id='12345678123456781234567812345678'
        self.other_id='87654321876543218765432187654321'
        self.sessions=[{'NowPlayingItem':{'Id':self.item_id,'ParentIndexNumber':1,'IndexNumber':1,
                         'Path':'/cinema2/'+str(self.path.relative_to(config.CINEMA2_DIR)),
                         'RunTimeTicks':1000},
                       'PlayState':{'MediaSourceId':self.item_id,'PositionTicks':450,'IsPaused':False}}]
        self.patches=[patch.object(config,'get_jellyfin2_key',return_value='fixture'),
                      patch('urllib.request.urlopen',side_effect=lambda *a,**k:Response(self.sessions))]
        for item in self.patches:item.start();self.addCleanup(item.stop)

    def watching(self):return prefetch.SmartPrefetcher()._watching(self.work)

    def test_actual_session_without_media_sources_accepts_45_percent(self):
        self.assertTrue(self.watching())

    def test_below_threshold_still_rejected(self):
        self.sessions[0]['PlayState']['PositionTicks']=449
        self.assertFalse(self.watching())

    def test_paused_session_still_rejected(self):
        self.sessions[0]['PlayState']['IsPaused']=True
        self.assertFalse(self.watching())

    def test_other_selected_version_without_sources_is_unknown(self):
        self.sessions[0]['PlayState']['MediaSourceId']=self.other_id
        self.assertFalse(self.watching())

    def test_remote_media_source_can_use_same_item_local_strm_path(self):
        self.sessions[0]['NowPlayingItem']['MediaSources']=[{'Id':self.item_id,'Path':'http://fixture.invalid/episode.mkv'}]
        self.assertTrue(self.watching())

    def test_other_version_remote_source_cannot_use_default_item_path(self):
        self.sessions[0]['PlayState']['MediaSourceId']=self.other_id
        self.sessions[0]['NowPlayingItem']['MediaSources']=[{'Id':self.other_id,'Path':'http://fixture.invalid/episode.mkv'}]
        self.assertFalse(self.watching())

    def test_conflicting_local_source_path_is_not_overridden(self):
        self.sessions[0]['NowPlayingItem']['MediaSources']=[{'Id':self.item_id,'Path':'/cinema2/other.strm'}]
        self.assertFalse(self.watching())

    def test_duplicate_selected_sources_are_ambiguous(self):
        item=self.sessions[0]['NowPlayingItem']
        item['MediaSources']=[{'Id':self.item_id,'Path':item['Path']},{'Id':self.item_id,'Path':item['Path']}]
        self.assertFalse(self.watching())

    def test_unknown_selected_source_among_other_sources_is_rejected(self):
        self.sessions[0]['NowPlayingItem']['MediaSources']=[{'Id':self.other_id,'Path':'/cinema2/other.strm'}]
        self.assertFalse(self.watching())

    def test_missing_item_id_does_not_authorize_path_fallback(self):
        self.sessions[0]['NowPlayingItem'].pop('Id')
        self.assertFalse(self.watching())

    def test_non_uuid_even_equal_ids_do_not_authorize_fallback(self):
        self.sessions[0]['NowPlayingItem']['Id']='unconfirmed'
        self.sessions[0]['PlayState']['MediaSourceId']='unconfirmed'
        self.assertFalse(self.watching())

    def test_uuid_formatting_differences_are_normalized(self):
        self.sessions[0]['PlayState']['MediaSourceId']='12345678-1234-5678-1234-567812345678'
        self.assertTrue(self.watching())

    def test_same_item_wrong_work_path_still_rejected(self):
        self.sessions[0]['NowPlayingItem']['Path']='/cinema2/国产剧集/其他 (2026)/Season 01/其他 - S01E01.strm'
        self.assertFalse(self.watching())

    def test_same_item_wrong_episode_still_rejected(self):
        self.sessions[0]['NowPlayingItem']['IndexNumber']=2
        self.assertFalse(self.watching())


def tearDownModule():
    db.close();TMP.cleanup()


if __name__=='__main__':unittest.main(verbosity=2)
