"""Replacement saga regressions: real SQLite/STRM, mocked cloud and probes only."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock,patch

BOOT=tempfile.TemporaryDirectory(prefix='vod-replacement-boot-')
os.environ.update(VOD_BASE_DIR=BOOT.name,VOD_DATA_DIR=BOOT.name+'/data',
                  VOD_CINEMA2_DIR=BOOT.name+'/cinema2',VOD_JELLYFIN_ENABLED='1',
                  VOD_MAINTENANCE_ENABLED='1',AI_PARSE_ENABLED='0',SELFHEAL_ENABLED='0')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from quark_vod import config,coordination,db,replacement,server,strm_manager
from quark_vod.identity import Work
from quark_vod.transfer import QuarkTransfer,TransferFailed,TransferNotReady,QUARK_SHARE_SAVE

class SourceReplacementTests(unittest.TestCase):
    def setUp(self):
        db.close()
        temporary=tempfile.TemporaryDirectory(prefix='vod-source-replacement-')
        self.addCleanup(temporary.cleanup)
        root=Path(temporary.name)
        settings=patch.multiple(config,STATE_DB=str(root/'data/state.db'),
                                DATA_DIR=str(root/'data'),CINEMA2_DIR=str(root/'cinema2'),
                                VOD_JELLYFIN_ENABLED=True)
        settings.start()
        self.addCleanup(settings.stop)
        self.addCleanup(db.close)
        config.ensure_dirs()
        self.work=Work('欧美剧集','换源剧 (2026)',1,1)
        self.old_share='https://pan.quark.cn/s/old123'
        self.new_share='https://pan.quark.cn/s/new123'
        self.old_remote=config.QUARK_VOD_ROOT+'/vod_old/video.mkv'
        self.old_id=db.complete_transfer(*self.work.args,self.old_remote,1024,db.now()+86400)
        db.upsert_transfer(*self.work.args,share_url=self.old_share)
        self.path=Path(strm_manager.strm_path_for(*self.work.args))
        self.path.parent.mkdir(parents=True)
        self.path.write_text(strm_manager.PENDING_PREFIX+'?cat=欧美剧集&t=换源剧+(2026)&s=1&e=1')
        db.save_strm_map(*self.work.args,str(self.path))
        self.refresh=patch.object(strm_manager,'trigger_jellyfin_refresh_item')
        self.refresh.start();self.addCleanup(self.refresh.stop)
        strm_manager.activate(*self.work.args,self.url(self.old_remote))
        db.save_source_audit(*self.work.args,True,False,self.old_share,'旧片源','valid_hd')
        self.old_row=dict(db.get_transfer_by_id(self.old_id))
        self.old_text=self.path.read_text()
        self.old_audit=dict(db.get_source_audit(*self.work.args))
        self.files=[{'file_name':'换源剧.S01E01.2160p.HDR10.国语.mkv',
                     'relative_path':'换源剧 (2026)/换源剧.S01E01.2160p.HDR10.国语.mkv',
                     'fid':'selected','parent_fid':'0','size':2*1024**3,'share_fid_token':'fixture-token'}]
        self.saved=[]
        self.tr=QuarkTransfer(storage=Mock())
        self.tr.ensure_ready=Mock()
        self.tr.ensure_vod_dir=Mock()
        self.tr.resolve_share=Mock(side_effect=lambda url,**kwargs:('fixture-token',QuarkTransfer.parse_share(url)[0],self.files))
        self.tr.share_files=Mock(side_effect=lambda *args,**kwargs:self.files)
        self.tr._get_quark_fid=Mock(return_value='candidate-folder')
        self.tr._quark_list_fid=Mock(side_effect=lambda *args,**kwargs:list(self.saved))
        self.tr._check_capacity=Mock(return_value=(True,10**12))
        self.tr._wait_saved_task=Mock()
        self.submit_count=0
        self.mode='success'
        self.tr._request=Mock(side_effect=self.request)
        self.tr.delete_remote=Mock(return_value=True)
        self.invalidate=Mock()
        self.req=SimpleNamespace(cat=self.work.cat,title=self.work.title,season=1,episode=1,
                                 variant='',share_url=self.new_share,selected_fid='selected',reason='wrong_audio')
        for target in ('quark_vod.subtitles.queue_companions','quark_vod.subtitles.schedule_pending'):
            patched=patch(target);patched.start();self.addCleanup(patched.stop)
        ready=patch.object(server,'_wait_direct_url_ready',return_value=True)
        self.ready=ready.start();self.addCleanup(ready.stop)
        clean=patch.object(server.cleaner,'clean_url_if_obfuscated',side_effect=lambda url:url)
        clean.start();self.addCleanup(clean.stop)

    @staticmethod
    def url(remote):
        return config.OPENLIST_PUBLIC+'/d'+config.play_path(remote)

    def request(self,url,**kwargs):
        self.assertEqual(url,QUARK_SHARE_SAVE)
        self.submit_count+=1
        if self.mode=='reject':return {'code':400,'message':'fixture rejection'}
        self.saved=[{'fid':'saved-file','name':self.files[0]['file_name'],
                     'is_dir':False,'size':self.files[0]['size']}]
        if self.mode=='unknown':raise TimeoutError('fixture response lost')
        return {'code':0,'data':{'task_id':'fixture-task'}}

    def run_replace(self,finish=None):
        with coordination.playback_lease():
            return replacement.replace(self.req,self.tr,finish or server._finish,self.invalidate)

    def candidate(self):
        rows=db._conn().execute('SELECT * FROM transfers WHERE id<>? ORDER BY id',(self.old_id,)).fetchall()
        self.assertEqual(len(rows),1)
        return dict(rows[0])

    def assert_old_preserved(self):
        self.assertEqual(dict(db.get_transfer_by_id(self.old_id)),self.old_row)
        self.assertEqual(self.path.read_text(),self.old_text)
        self.assertEqual(dict(db.get_source_audit(*self.work.args)),self.old_audit)
        self.assertFalse(db.is_dead_source(self.old_share))
        self.tr.delete_remote.assert_not_called()

    def test_empty_activation_paths_rejects_before_cloud_save(self):
        with patch.object(strm_manager,'activation_paths',return_value=[]):
            with self.assertRaises(replacement.ReplacementUnavailable):self.run_replace()
        self.assert_old_preserved()
        self.assertEqual(self.submit_count,0)
        self.assertEqual(db._conn().execute('SELECT COUNT(*) FROM transfers').fetchone()[0],1)

    def test_selected_fid_rejection_changes_no_old_state_or_candidate(self):
        self.req.selected_fid='not-in-enumeration'
        with self.assertRaises(replacement.ReplacementUnavailable) as caught:self.run_replace()
        self.assertEqual(caught.exception.status,422)
        self.assert_old_preserved()
        self.assertEqual(db._conn().execute('SELECT COUNT(*) FROM transfers').fetchone()[0],1)
        self.assertEqual(self.submit_count,0)

    def test_save_rejection_keeps_old_and_candidate_is_a_separate_row(self):
        self.mode='reject'
        with self.assertRaises(TransferFailed):self.run_replace()
        self.assert_old_preserved()
        candidate=self.candidate()
        self.assertEqual(candidate['status'],'failed')
        self.assertEqual(candidate['share_url'],self.new_share)
        self.assertNotEqual(candidate['id'],self.old_id)
        self.assertIsNone(candidate['save_started_at'])

    def test_unknown_save_resumes_original_candidate_without_second_submit(self):
        self.mode='unknown'
        with self.assertRaises(TimeoutError):self.run_replace()
        self.assert_old_preserved()
        candidate=self.candidate()
        self.assertIsNotNone(candidate['save_started_at'])
        self.assertIn('/vod_',candidate['save_target'])
        self.assertEqual(self.submit_count,1)
        self.mode='success'
        result=self.run_replace()
        self.assertEqual(result['transfer_id'],candidate['id'])
        self.assertEqual(self.submit_count,1)
        self.assertEqual(self.candidate()['status'],'done')

    def test_magic_unknown_preserves_old_and_exact_candidate_intent(self):
        self.files[0]['file_name']='换源剧.S01E01.2160p.HDR10.国语.dat'
        self.files[0]['relative_path']='换源剧 (2026)/'+self.files[0]['file_name']
        with patch.object(self.tr,'sniff_and_fix_file',return_value=None):
            with self.assertRaises(TransferNotReady):self.run_replace()
        self.assert_old_preserved()
        candidate=self.candidate()
        self.assertEqual(candidate['status'],'failed')
        self.assertIsNotNone(candidate['save_started_at'])
        self.assertEqual(candidate['save_file_fid'],'saved-file')

    def test_readiness_failure_retains_old_and_retry_reuses_saved_candidate(self):
        self.ready.return_value=False
        with self.assertRaises(server.PlaybackUnavailable):self.run_replace()
        self.assert_old_preserved()
        candidate=self.candidate()
        self.assertTrue(candidate['remote_path'])
        self.assertEqual(candidate['status'],'failed')
        self.assertEqual(self.submit_count,1)
        self.ready.return_value=True
        result=self.run_replace()
        self.assertEqual(result['transfer_id'],candidate['id'])
        self.assertEqual(self.submit_count,1)

    def test_activation_failure_after_partial_write_restores_old_entry(self):
        activate=strm_manager.activate
        def partial(*args,**kwargs):
            activate(*args,**kwargs)
            raise OSError('fixture post-write activation failure')
        with patch.object(strm_manager,'activate',side_effect=partial):
            with self.assertRaises(server.PlaybackUnavailable):self.run_replace()
        self.assert_old_preserved()
        self.assertEqual(self.candidate()['status'],'failed')
        self.assertEqual(self.submit_count,1)

    def test_success_commits_new_audit_ban_and_strm_before_exact_old_leaf_delete(self):
        observed=[]
        def delete(remote):
            self.assertEqual(remote,self.old_remote)
            self.assertTrue(db.is_dead_source(self.old_share))
            self.assertEqual(db.get_source_audit(*self.work.args)['best_share_url'],self.new_share)
            candidate=self.candidate()
            self.assertEqual(candidate['status'],'done')
            self.assertEqual(strm_manager.parse_live_remote_path(self.path.read_text().strip()),
                             candidate['remote_path'])
            receipt=db._conn().execute(
                'SELECT old_transfer_id,committed_at FROM replacement_commits WHERE candidate_id=?',
                (candidate['id'],)).fetchone()
            self.assertEqual(receipt['old_transfer_id'],self.old_id)
            self.assertGreater(receipt['committed_at'],0)
            observed.append(remote)
            return True
        self.tr.delete_remote.side_effect=delete
        result=self.run_replace()
        self.assertTrue(result['ok'])
        self.assertFalse(result['cleanup_pending'])
        self.assertEqual(observed,[self.old_remote])
        self.assertEqual(db.get_transfer_by_id(self.old_id)['status'],'cleaned')
        self.assertIsNone(db.get_transfer_by_id(self.old_id)['remote_path'])
        self.assertEqual(db._conn().execute('SELECT reason FROM dead_sources').fetchone()[0],'wrong_audio')
        self.assertFalse(replacement._journal_path(self.work).exists())

    def test_delete_failure_keeps_new_live_and_retry_only_retires_old(self):
        self.tr.delete_remote.side_effect=OSError('fixture delete unavailable')
        result=self.run_replace()
        self.assertTrue(result['ok'])
        self.assertTrue(result['cleanup_pending'])
        candidate=self.candidate()
        self.assertEqual(candidate['status'],'done')
        self.assertEqual(strm_manager.parse_live_remote_path(self.path.read_text().strip()),candidate['remote_path'])
        self.assertEqual(db.get_transfer_by_id(self.old_id)['remote_path'],self.old_remote)
        self.assertTrue(replacement._journal_path(self.work).exists())
        self.tr.delete_remote.side_effect=None
        finish=Mock(side_effect=AssertionError('committed replacement must not reactivate'))
        result=self.run_replace(finish=finish)
        self.assertFalse(result['cleanup_pending'])
        finish.assert_not_called()
        self.assertEqual(self.submit_count,1)
        self.assertEqual(self.tr.delete_remote.call_args_list[0].args,(self.old_remote,))
        self.assertEqual(self.tr.delete_remote.call_args_list[1].args,(self.old_remote,))

    def test_cleanup_retry_retains_old_leaf_if_entry_no_longer_confirms_candidate(self):
        self.tr.delete_remote.side_effect=OSError('fixture delete unavailable')
        first=self.run_replace()
        self.assertTrue(first['cleanup_pending'])
        # A later repair/manual edit removed the new live route. Do not destroy
        # the remaining old recovery object based only on an earlier commit.
        pending=strm_manager.PENDING_PREFIX+'?cat=欧美剧集&t=换源剧+(2026)&s=1&e=1'
        self.path.write_text(pending)
        self.tr.delete_remote.reset_mock(side_effect=True)
        result=self.run_replace()
        self.assertTrue(result['cleanup_pending'])
        self.assertEqual(self.path.read_text(),pending)
        self.assertEqual(db.get_transfer_by_id(self.old_id)['remote_path'],self.old_remote)
        self.tr.delete_remote.assert_not_called()
        self.assertEqual(self.submit_count,1)

    def test_staged_candidate_is_not_a_done_source_before_activation(self):
        def verify_then_finish(work,tid,remote):
            self.assertNotEqual(tid,self.old_id)
            self.assertEqual(db.get_transfer_by_id(tid)['status'],'found')
            self.assertEqual(db.find_done_transfer(*work.args,**work.options)['id'],self.old_id)
            self.assertEqual(self.path.read_text(),self.old_text)
            return server._finish(work,tid,remote)
        self.assertTrue(self.run_replace(finish=verify_then_finish)['ok'])

    def test_unconfirmed_false_delete_keeps_cleanup_pending(self):
        self.tr.delete_remote.return_value=False
        result=self.run_replace()
        self.assertTrue(result['cleanup_pending'])
        self.assertEqual(db.get_transfer_by_id(self.old_id)['remote_path'],self.old_remote)
        self.assertEqual(self.candidate()['status'],'done')
        self.assertTrue(replacement._journal_path(self.work).exists())

    def test_same_share_different_file_switches_without_banning_entire_share(self):
        self.req.share_url=self.old_share
        result=self.run_replace()
        self.assertTrue(result['ok'])
        self.assertFalse(result['cleanup_pending'])
        self.assertFalse(db.is_dead_source(self.old_share))
        self.assertEqual(self.candidate()['share_url'],self.old_share)
        self.tr.delete_remote.assert_called_once_with(self.old_remote)
        self.assertEqual(db.get_source_audit(*self.work.args)['best_share_url'],self.old_share)
        with self.assertRaises(replacement.ReplacementUnavailable):
            self.run_replace()
        self.assertEqual(self.submit_count,1)

    def test_terminal_empty_failure_can_choose_another_share(self):
        self.mode='reject'
        with self.assertRaises(TransferFailed):self.run_replace()
        failed=self.candidate()
        self.assert_old_preserved()
        self.req.share_url='https://pan.quark.cn/s/third123'
        self.mode='success'
        result=self.run_replace()
        self.assertTrue(result['ok'])
        self.assertNotIn(result['transfer_id'],(self.old_id,failed['id']))
        self.assertEqual(db.get_transfer_by_id(failed['id'])['status'],'failed')
        self.assertEqual(db.get_transfer_by_id(result['transfer_id'])['share_url'],self.req.share_url)
        self.assertEqual(self.submit_count,2)

    def test_unknown_save_cannot_switch_candidates_or_submit_again(self):
        self.mode='unknown'
        with self.assertRaises(TimeoutError):self.run_replace()
        candidate=self.candidate()
        self.req.share_url='https://pan.quark.cn/s/third123'
        with self.assertRaises(replacement.ReplacementUnavailable) as caught:self.run_replace()
        self.assertEqual(caught.exception.code,'replacement_in_progress')
        self.assertEqual(self.candidate()['id'],candidate['id'])
        self.assertEqual(self.submit_count,1)
        self.assert_old_preserved()

    def test_process_exit_after_activation_retries_existing_candidate_without_resave(self):
        def interrupted(work,tid,remote):
            server._finish(work,tid,remote)
            raise SystemExit('fixture process exit before source commit')
        with self.assertRaises(SystemExit):self.run_replace(finish=interrupted)
        candidate=self.candidate()
        self.assertEqual(dict(db.get_source_audit(*self.work.args)),self.old_audit)
        self.assertFalse(db.is_dead_source(self.old_share))
        self.assertEqual(db.get_transfer_by_id(self.old_id)['remote_path'],self.old_remote)
        self.tr.delete_remote.assert_not_called()
        state=json.loads(replacement._journal_path(self.work).read_text())
        self.assertEqual(state['phase'],'activating')
        self.assertIsNone(db._conn().execute('SELECT 1 FROM replacement_commits WHERE candidate_id=?',
                                            (candidate['id'],)).fetchone())
        result=self.run_replace()
        self.assertEqual(result['transfer_id'],candidate['id'])
        self.assertFalse(result['cleanup_pending'])
        self.assertEqual(self.submit_count,1)

    def test_same_share_exit_before_commit_does_not_treat_old_audit_as_commit_proof(self):
        self.req.share_url=self.old_share
        def interrupted(work,tid,remote):
            server._finish(work,tid,remote)
            raise SystemExit('fixture same-share exit before source commit')
        with self.assertRaises(SystemExit):self.run_replace(finish=interrupted)
        candidate=self.candidate()
        self.assertEqual(db.get_source_audit(*self.work.args)['check_status'],'valid_hd')
        result=self.run_replace()
        self.assertEqual(result['transfer_id'],candidate['id'])
        self.assertEqual(db.get_source_audit(*self.work.args)['check_status'],'selected_file')
        self.assertFalse(db.is_dead_source(self.old_share))
        self.assertEqual(self.submit_count,1)

    def test_same_share_previous_selected_file_audit_is_not_this_candidate_commit(self):
        self.req.share_url=self.old_share
        db.save_source_audit(*self.work.args,True,False,self.old_share,'旧手选文件','selected_file')
        def interrupted(work,tid,remote):
            server._finish(work,tid,remote)
            raise SystemExit('fixture second same-share selection exits before commit')
        with self.assertRaises(SystemExit):self.run_replace(finish=interrupted)
        finish=Mock(wraps=server._finish)
        result=self.run_replace(finish=finish)
        self.assertTrue(result['ok'])
        finish.assert_called_once()
        self.assertEqual(db.get_source_audit(*self.work.args)['best_share_title'],'')
        self.assertEqual(self.submit_count,1)

    def test_sqlite_publish_failure_rolls_back_receipt_audit_ban_and_restores_strm(self):
        with db._conn() as con:
            # Fail after the receipt/audit writes in the same real SQLite transaction.
            con.execute("CREATE TRIGGER reject_replacement_ban BEFORE INSERT ON dead_sources "
                        "BEGIN SELECT RAISE(ABORT,'fixture publish failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.run_replace()
        self.assert_old_preserved()
        candidate=self.candidate()
        self.assertEqual(candidate['status'],'failed')
        self.assertIsNone(db._conn().execute('SELECT 1 FROM replacement_commits WHERE candidate_id=?',
                                            (candidate['id'],)).fetchone())
        with db._conn() as con:con.execute('DROP TRIGGER reject_replacement_ban')
        result=self.run_replace()
        self.assertEqual(result['transfer_id'],candidate['id'])
        self.assertEqual(self.submit_count,1)
        receipt=db._conn().execute('SELECT old_transfer_id FROM replacement_commits WHERE candidate_id=?',
                                  (candidate['id'],)).fetchone()
        self.assertEqual(receipt['old_transfer_id'],self.old_id)
        self.assertTrue(db.is_dead_source(self.old_share))

    def test_process_exit_after_sqlite_commit_recognizes_committed_candidate(self):
        write=replacement._write
        def interrupted(path,state):
            if state['phase']=='committed':
                raise OSError('fixture journal fsync failed after sqlite commit')
            return write(path,state)
        with patch.object(replacement,'_write',side_effect=interrupted):
            with self.assertRaises(OSError):self.run_replace()
        candidate=self.candidate()
        self.assertEqual(candidate['status'],'done')
        self.assertIsNotNone(db._conn().execute('SELECT 1 FROM replacement_commits WHERE candidate_id=?',
                                               (candidate['id'],)).fetchone())
        self.assertEqual(db.get_source_audit(*self.work.args)['best_share_url'],self.new_share)
        self.assertTrue(db.is_dead_source(self.old_share))
        self.assertEqual(db.get_transfer_by_id(self.old_id)['remote_path'],self.old_remote)
        self.tr.delete_remote.assert_not_called()
        finish=Mock(side_effect=AssertionError('committed operation cannot reactivate or resave'))
        result=self.run_replace(finish=finish)
        self.assertEqual(result['transfer_id'],candidate['id'])
        self.assertFalse(result['cleanup_pending'])
        finish.assert_not_called()
        self.assertEqual(self.submit_count,1)

    def test_hdr_replacement_does_not_change_default_or_dubbed(self):
        for variant in ('hdr','dubbed'):
            path=strm_manager.ensure_variant(*self.work.args,variant)
            remote=config.QUARK_VOD_ROOT+'/vod_old_'+variant+'/video.mkv'
            tid=db.complete_transfer(*self.work.args,remote,1024,db.now()+86400,variant=variant)
            db.upsert_transfer(*self.work.args,variant=variant,share_url=self.old_share)
            strm_manager.activate(*self.work.args,self.url(remote),variant=variant)
            db.save_source_audit(*self.work.args,True,False,self.old_share,'旧片源','valid_hd',variant=variant)
        dubbed=dict(db.get_transfer(*self.work.args,variant='dubbed'))
        dubbed_path=Path(db.get_strm_path(*self.work.args,variant='dubbed'))
        dubbed_text=dubbed_path.read_text()
        hdr_old=dict(db.get_transfer(*self.work.args,variant='hdr'))
        self.req.variant='hdr'
        result=self.run_replace()
        self.assertTrue(result['ok'])
        self.assertEqual(dict(db.get_transfer_by_id(self.old_id)),self.old_row)
        self.assertEqual(self.path.read_text(),self.old_text)
        self.assertEqual(dict(db.get_source_audit(*self.work.args)),self.old_audit)
        self.assertEqual(dict(db.get_transfer_by_id(dubbed['id'])),dubbed)
        self.assertEqual(dubbed_path.read_text(),dubbed_text)
        self.tr.delete_remote.assert_called_once_with(hdr_old['remote_path'])
        self.assertEqual(db.get_source_audit(*self.work.args,variant='hdr')['best_share_url'],self.new_share)

    def test_fixed_row_updates_reject_another_work_or_variant_and_preserve_old(self):
        candidate=db.create_transfer(*self.work.args,share_url=self.new_share)
        other=Work(self.work.cat,'另一部剧 (2026)',1,1)
        before=dict(db.get_transfer_by_id(candidate))
        with self.assertRaises(ValueError):
            db.upsert_transfer(*other.args,_transfer_id=candidate,status='saving')
        with self.assertRaises(ValueError):
            db.complete_transfer(*self.work.args,'/wrong',1024,99,variant='hdr',
                                 _transfer_id=candidate,_staged=True)
        with self.assertRaises(ValueError):
            self.tr.transfer(*other.args,self.new_share,transfer_id=candidate)
        self.assertEqual(dict(db.get_transfer_by_id(candidate)),before)
        self.assert_old_preserved()
        self.assertEqual(self.submit_count,0)

def tearDownModule():
    db.close()
    BOOT.cleanup()

if __name__=='__main__':unittest.main(verbosity=2)
