"""Offline automation config, scheduler, queue, API and process lifecycle regressions."""
from contextlib import ExitStack
from datetime import datetime
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from quark_vod import config, automation_settings as s, automation_runner as r
from quark_vod.automation_api import create_router


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='vod-auto-')
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(config,'DATA_DIR',str(self.root)))
        self.stack.enter_context(patch.object(config,'get_admin_token',return_value='offline-fixture'))
        app = FastAPI()
        app.include_router(create_router())
        self.client = TestClient(app)
        self.stack.callback(self.client.close)
        self.auth = {'Authorization':'Bearer offline-fixture'}

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def enable(self, task='digest', scheduler=False):
        value = s.defaults()
        value['revision'] = s.get_settings()['revision']
        value['enabled'] = scheduler
        value['tasks'][task]['enabled'] = True
        return s.update_settings(value)

    def api_value(self):
        value = s.get_settings()
        return {key:value[key] for key in ('revision','enabled','timezone','ai','tasks')}

    def test_defaults_no_ai_no_execution_and_disabled_manual(self):
        value = s.get_settings()
        self.assertFalse(value['enabled'])
        self.assertFalse(any(t['enabled'] for t in value['tasks'].values()))
        self.assertEqual(s.ai_models(),[])
        self.assertFalse((self.root/s.FILENAME).exists())
        self.assertEqual(r.tick(),{'queued':[],'scheduler_enabled':False})
        with self.assertRaises(s.AutomationError) as cm:
            r.enqueue('digest',0)
        self.assertEqual(cm.exception.code,'automation_task_disabled')

    def test_auth_blocks_all_side_effects(self):
        for method,url,kw in [('get','',{}),('put','',{'json':{}}),
                              ('get','/jobs',{}),('get','/runs',{}),
                              ('post','/tasks/digest/run',{'json':{'revision':0}})]:
            response=getattr(self.client,method)('/api/admin/automation'+url,**kw)
            self.assertEqual(response.status_code,401)
        self.assertEqual(list(self.root.iterdir()),[])
        with patch.object(config,'get_admin_token',return_value=''):
            self.assertEqual(self.client.get('/api/admin/automation',headers=self.auth).status_code,503)

    def test_atomic_settings_conflict_and_private_permissions(self):
        before=self.enable()
        target=self.root/s.FILENAME
        inode=target.stat().st_ino
        value=self.api_value()
        value['ai'].update(enabled=True,base_url='https://example.invalid/v1/',primary_model='test/model',
                           fallback_models=['test/fallback'],reasoning=dict(s.REASONING))
        saved=s.update_settings(value)
        self.assertEqual(saved['revision'],2)
        self.assertNotEqual(inode,target.stat().st_ino)
        self.assertEqual(target.stat().st_mode&0o777,0o600)
        self.assertEqual(s.ai_models(),['test/model','test/fallback'])
        self.assertEqual(s.get_ai_config()['base_url'],'https://example.invalid/v1')
        content=target.read_bytes()
        with self.assertRaises(s.AutomationError) as cm:s.update_settings(value)
        self.assertEqual(cm.exception.code,'automation_revision_conflict')
        self.assertEqual(target.read_bytes(),content)

    def test_strict_values_credentials_unknown_fields_and_cron(self):
        for mutate in [
            lambda v:v.update(enabled=1),
            lambda v:v.update(timezone='Not/AZone'),
            lambda v:v['ai'].update(base_url='https://user:secret@example.invalid/v1'),
            lambda v:v['ai'].update(base_url='https://example.invalid/v1?key=secret'),
            lambda v:v['ai'].update(api_key='secret'),
            lambda v:v['ai'].update(primary_model='x; touch /tmp/injected'),
            lambda v:v['tasks']['digest'].update(timeout_seconds=True),
            lambda v:v['tasks']['digest'].update(schedules=['60 8 * * *']),
            lambda v:v['tasks']['digest'].update(schedules=['* * * *']),
            lambda v:v['tasks'].update(arbitrary={'enabled':True}),
        ]:
            value=s.defaults();mutate(value)
            with self.assertRaises(s.AutomationError):s.update_settings(value)
        for raw in (b'{"revision":0,"revision":1}',b'{"a":NaN}',b'[]',b'x'*32769):
            with self.assertRaises(s.AutomationError):s.parse_request(raw)

    def test_corrupt_and_symlink_settings_fail_closed(self):
        self.enable()
        target=self.root/s.FILENAME
        target.write_text('{"version":1,"revision":1}')
        with self.assertRaises(s.AutomationError):s.get_settings()
        target.unlink()
        outside=self.root/'outside';outside.write_text('{}');outside.chmod(0o600)
        target.symlink_to(outside)
        with self.assertRaises(s.AutomationError):s.get_settings()
        self.assertEqual(outside.read_text(),'{}')

    def test_cron_timezone_or_semantics_and_minute_dedup(self):
        self.enable(scheduler=True)
        stamp=datetime(2026,11,26,8,0,tzinfo=ZoneInfo('Asia/Shanghai'))
        self.assertTrue(r.due('0 8 * * *',stamp))
        self.assertTrue(r.due('*/5 8 * * *',stamp))
        self.assertTrue(r.due('0 8 26 * 0',stamp))
        self.assertFalse(r.due('0 8 25 * 0',stamp))
        self.assertFalse(r.due('0 8 */1 * 0',stamp))
        self.assertEqual(len(r.tick(stamp)['queued']),1)
        self.assertEqual(r.tick(stamp)['queued'],[])
        self.assertEqual(r.run_next()['status'],'dry_run')
        self.assertEqual(r.tick(stamp)['queued'],[])

    def test_manual_cas_dry_run_dedup_and_disable_before_execution(self):
        self.enable()
        with self.assertRaises(s.AutomationError):r.enqueue('digest',0)
        job=r.enqueue('digest',1)
        with self.assertRaises(s.AutomationError):r.enqueue('digest',1)
        with patch.object(r.subprocess,'Popen',side_effect=AssertionError('must not spawn')):
            self.assertEqual(r.run_next()['status'],'dry_run')
        self.assertEqual(r.runs()['runs'][0]['status'],'dry_run')
        r.enqueue('digest',1)
        value=self.api_value();value['tasks']['digest']['enabled']=False;s.update_settings(value)
        self.assertEqual(r.run_next()['status'],'interrupted')

    def test_http_contract_body_and_queue(self):
        result=self.client.get('/api/admin/automation',headers=self.auth)
        self.assertEqual(result.status_code,200)
        value=self.api_value();value['tasks']['digest']['enabled']=True
        saved=self.client.put('/api/admin/automation',headers=self.auth,json=value)
        self.assertEqual(saved.status_code,200,saved.text)
        response=self.client.post('/api/admin/automation/tasks/digest/run',headers=self.auth,json={'revision':1})
        self.assertEqual(response.status_code,202,response.text)
        self.assertTrue(response.json()['dry_run'])
        self.assertEqual(len(self.client.get('/api/admin/automation/runs',headers=self.auth).json()['runs']),1)
        self.assertEqual(self.client.post('/api/admin/automation/tasks/digest/run',headers=self.auth,
                                         json={'revision':1,'command':'bad'}).status_code,400)
        self.assertEqual(self.client.put('/api/admin/automation',headers=self.auth,content='{}').status_code,400)

    def test_real_independent_process_fixed_argv_and_safe_status(self):
        self.enable()
        r.enqueue('digest',1,False)
        real_popen=subprocess.Popen
        def child(argv,**kwargs):
            self.assertEqual(argv[1:3],['-m','quark_vod.automation_child'])
            self.assertEqual(argv[-1],'digest')
            self.assertNotIn('shell',kwargs)
            return real_popen([sys.executable,'-c',
                              'print("https://secret.invalid/?token=private"); raise SystemExit(75)'],**kwargs)
        with patch.object(r.subprocess,'Popen',side_effect=child):
            result=r.run_next()
        self.assertEqual(result['status'],'busy')
        state=(self.root/r.STATE_FILE).read_text()
        self.assertNotIn('secret.invalid',state)
        self.assertNotIn('private',state)
        self.assertGreater(r.runs()['runs'][0]['output_bytes'],0)

    def test_timeout_kills_process_group_and_records_failure(self):
        self.enable()
        value=self.api_value();value['tasks']['digest']['timeout_seconds']=10;s.update_settings(value)
        r.enqueue('digest',2,False)
        real_popen=subprocess.Popen
        children=[]
        def child(argv,**kwargs):
            p=real_popen([sys.executable,'-c','import time; time.sleep(30)'],**kwargs);children.append(p);return p
        with patch.object(r.subprocess,'Popen',side_effect=child):
            self.assertEqual(r.run_next()['status'],'timeout')
        self.assertIsNotNone(children[0].poll())
        self.assertEqual(r.runs()['runs'][0]['error'],'task_timeout')

    def test_worker_singleton_and_graceful_stop(self):
        with r.worker_lock():
            with self.assertRaises(s.AutomationError): 
                with r.worker_lock():pass
        event=threading.Event()
        thread=threading.Thread(target=r.worker,kwargs={'stop_event':event,'poll_seconds':.1})
        thread.start()
        time.sleep(.2);event.set();thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(r.runs()['worker']['status'],'stopped')

    def test_history_is_bounded_and_settings_changes_affect_next_run(self):
        self.enable()
        for _ in range(40):
            r.enqueue('digest',1);r.run_next()
        self.assertEqual(len(r.runs()['runs']),r.MAX_RUNS)
        self.assertLess((self.root/r.STATE_FILE).stat().st_size,16384)


    def test_actual_start_revision_and_ai_snapshot_are_frozen(self):
        self.enable()
        r.enqueue('digest',1,False)
        value=self.api_value()
        value['ai'].update(enabled=True,primary_model='new/model',base_url='https://example.invalid/v1')
        s.update_settings(value)
        real_popen=subprocess.Popen
        def child(argv,**kwargs):
            self.assertEqual(kwargs['env']['VOD_AUTOMATION_SETTINGS_REVISION'],'2')
            ai=json.loads(kwargs['env']['VOD_AUTOMATION_AI_SNAPSHOT'])
            self.assertEqual(ai['primary_model'],'new/model')
            return real_popen([sys.executable,'-c','pass'],**kwargs)
        with patch.object(r.subprocess,'Popen',side_effect=child):
            r.run_next()
        self.assertEqual(r.runs()['runs'][0]['settings_revision'],2)
        frozen=copy.deepcopy(value['ai'])
        with patch.dict(os.environ,{'VOD_AUTOMATION_AI_SNAPSHOT':json.dumps(frozen)}):
            value=self.api_value();value['ai']['primary_model']='later/model';s.update_settings(value)
            self.assertEqual(s.get_ai_config()['primary_model'],'new/model')

    def test_two_execution_slots_apply_to_all_claimers(self):
        value=s.defaults()
        for task in ('digest','rss_subscriber','airing_chase'):
            value['tasks'][task]['enabled']=True
        s.update_settings(value)
        for task in ('digest','rss_subscriber','airing_chase'):
            r.enqueue(task,1,False)
        real_popen=subprocess.Popen
        def child(argv,**kwargs):
            return real_popen([sys.executable,'-c','import time; time.sleep(.6)'],**kwargs)
        threads=[threading.Thread(target=r.run_next) for _ in range(2)]
        with patch.object(r.subprocess,'Popen',side_effect=child):
            for t in threads:t.start()
            deadline=time.monotonic()+2
            while sum(x['status']=='running' for x in r.runs()['runs'])<2 and time.monotonic()<deadline:
                time.sleep(.02)
            self.assertIsNone(r.run_next())
            self.assertEqual(sum(x['status']=='running' for x in r.runs()['runs']),2)
            for t in threads:t.join(3)
        self.assertEqual(sum(x['status']=='queued' for x in r.runs()['runs']),1)

    def test_exited_parent_cannot_leave_writing_grandchild(self):
        self.enable();r.enqueue('digest',1,False)
        pidfile=self.root/'grandchild.pid'
        script = ('import os,signal,time; p=os.fork(); '
                  'time.sleep(.2) if p else None; '
                  'os._exit(0) if p else None; '
                  'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                  'open('+repr(str(pidfile))+',"w").write(str(os.getpid())); time.sleep(30)')
        real_popen=subprocess.Popen
        def child(argv,**kwargs):
            return real_popen([sys.executable,'-c',script],**kwargs)
        with patch.object(r.subprocess,'Popen',side_effect=child):
            result=r.run_next()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(r.runs()['runs'][0]['error'],'task_descendants_left_running')
        pid=int(pidfile.read_text())
        self.assertIsNone(r._identity(pid))

    def test_recover_dead_owner_never_reexecutes_live_writer(self):
        self.enable();job=r.enqueue('digest',1,False)
        process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],
                                 env={**os.environ,'VOD_AUTOMATION_RUN_ID':job['run_id']},
                                 start_new_session=True)
        try:
            with s.locked() as directory:
                state=r._state(directory)
                state['runs'][0].update(**r.execution_context())
                state['runs'][0].update(status='running',owner_pid=99999999,owner_identity='missing',
                                       child_pid=process.pid,child_identity=r._identity(process.pid))
                r._save(directory,state)
            recovered=r.recover()
            self.assertEqual(recovered['pending'],[job['run_id']])
            self.assertEqual(r.runs()['runs'][0]['status'],'running')
            recovered=r.recover(terminate=True)
            process.wait(timeout=3)
            self.assertEqual(recovered['recovered'],[job['run_id']])
            self.assertEqual(r.runs()['runs'][0]['status'],'interrupted')
            self.assertIsNotNone(process.poll())
        finally:
            if process.poll() is None:r._stop(process)

    def test_recover_dead_owner_without_writer_releases_task(self):
        self.enable();job=r.enqueue('digest',1,False)
        with s.locked() as directory:
            state=r._state(directory)
            state['runs'][0].update(**r.execution_context())
            state['runs'][0].update(status='running',owner_pid=99999999,owner_identity='missing')
            r._save(directory,state)
        self.assertEqual(r.recover()['recovered'],[job['run_id']])
        self.assertEqual(r.enqueue('digest',1)['status'],'queued')


    def test_real_launch_gate_and_packaged_digest_without_credentials(self):
        self.enable()
        env={key:value for key,value in os.environ.items()
             if not key.startswith(('VOD_','NOTIFY_','QUARK_','JELLYFIN_','OPENLIST_','AI_PARSE_','SELFHEAL_'))}
        env.update(VOD_BASE_DIR=str(self.root),VOD_DATA_DIR=str(self.root),AI_PARSE_ENABLED='0',
                   SELFHEAL_ENABLED='0')
        with patch.dict(os.environ,env,clear=True):
            job=r.enqueue('digest',1,False)
            result=r.run_next()
        self.assertEqual(result['status'],'complete',r.runs())
        self.assertEqual(r.runs()['runs'][0]['exit_code'],0)
        self.assertEqual(r.runs()['runs'][0]['settings_revision'],1)
        orphan_env={**env,'VOD_AUTOMATION_RUN_ID':'0'*32}
        result=subprocess.run([sys.executable,'-m','quark_vod.automation_child','digest'],
                              env=orphan_env,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,74)
        self.assertEqual(result.stdout,b'')

    def test_read_only_settings_import_does_not_create_directories(self):
        absent=self.root/'missing'/'data'
        with patch.object(config,'DATA_DIR',str(absent)):
            self.assertFalse(s.get_ai_config()['enabled'])
            self.assertFalse(absent.parent.exists())


    def test_recovery_refuses_mixed_pid_namespaces_on_same_boot(self):
        self.enable();job=r.enqueue('digest',1,False)
        with s.locked() as directory:
            state=r._state(directory)
            state['runs'][0].update(**r.execution_context())
            state['runs'][0].update(status='running',owner_pid=99999999,owner_identity='missing',
                                   execution_namespace='pid:[different]')
            r._save(directory,state)
        before=(self.root/r.STATE_FILE).read_bytes()
        for terminate in (False,True):
            with self.assertRaises(s.AutomationError) as cm:
                r.recover(terminate)
            self.assertEqual(cm.exception.code,'automation_recovery_namespace_mismatch')
        self.assertEqual((self.root/r.STATE_FILE).read_bytes(),before)

    def test_recovery_previous_boot_is_interrupted_without_pid_signals(self):
        self.enable();job=r.enqueue('digest',1,False)
        with s.locked() as directory:
            state=r._state(directory)
            state['runs'][0].update(**r.execution_context())
            state['runs'][0].update(status='running',owner_pid=os.getpid(),owner_identity=r._identity(os.getpid()),
                                   execution_boot_id='previous-boot')
            r._save(directory,state)
        with patch.object(r.os,'killpg',side_effect=AssertionError('must not signal current boot')):
            self.assertEqual(r.recover(True)['recovered'],[job['run_id']])
        self.assertEqual(r.runs()['runs'][0]['error'],'previous_boot')

    def test_unconfirmed_group_cleanup_never_releases_running_task(self):
        self.enable();r.enqueue('digest',1,False)
        real_popen=subprocess.Popen
        children=[]
        def child(argv,**kwargs):
            p=real_popen([sys.executable,'-c','pass'],**kwargs);children.append(p);return p
        with patch.object(r.subprocess,'Popen',side_effect=child), \
             patch.object(r,'_stop_group',side_effect=s.AutomationError('automation_processes_survived')):
            self.assertEqual(r.run_next()['status'],'running')
        for p in children:p.wait(timeout=2)
        self.assertEqual(r.runs()['runs'][0]['error'],'recovery_required')
        with self.assertRaises(s.AutomationError):r.enqueue('digest',1)


    def test_large_legal_settings_above_notification_limit_round_trip(self):
        value=s.defaults()
        schedule='0'+'\u2003'*111+'8 * * *'
        self.assertLessEqual(len(schedule),120)
        for task in value['tasks'].values():
            task['schedules']=[schedule]*8
        raw=json.dumps(value,ensure_ascii=False).encode()
        self.assertGreater(len(raw),16384)
        self.assertLess(len(raw),s.MAX_BYTES)
        parsed=s.parse_request(raw)
        saved=s.update_settings(parsed)
        self.assertEqual(s.get_settings()['tasks'],saved['tasks'])
        target=self.root/s.FILENAME
        self.assertGreater(target.stat().st_size,16384)
        self.assertLessEqual(target.stat().st_size,s.MAX_BYTES)
        self.assertEqual(target.stat().st_mode&0o777,0o600)

    def test_32_real_full_runs_survive_16k_and_history_eviction(self):
        self.enable()
        real_popen=subprocess.Popen
        def child(argv,**kwargs):
            return real_popen([sys.executable,'-c','pass'],**kwargs)
        identifiers=[]
        with patch.object(r.subprocess,'Popen',side_effect=child):
            for _ in range(34):
                identifiers.append(r.enqueue('digest',1,False)['run_id'])
                self.assertEqual(r.run_next()['status'],'complete')
        target=self.root/r.STATE_FILE
        self.assertGreater(target.stat().st_size,16384)
        self.assertLessEqual(target.stat().st_size,s.STATE_MAX_BYTES)
        records=r.runs()['runs']
        self.assertEqual(len(records),32)
        self.assertEqual({row['id'] for row in records},set(identifiers[2:]))
        for row in records:
            for field in ('child_pid','child_identity','execution_boot_id','execution_namespace',
                          'owner_pid','owner_identity','started_at','finished_at','settings_revision'):
                self.assertIn(field,row)
            self.assertEqual(row['status'],'complete')
        with s.locked() as directory:
            state=r._state(directory)
            r._save(directory,state)
        self.assertEqual(r.runs()['runs'],records)

    def test_storage_limits_refuse_oversize_without_changing_previous_value(self):
        self.enable()
        for filename,limit in ((s.FILENAME,s.MAX_BYTES),(r.STATE_FILE,s.STATE_MAX_BYTES)):
            with s.locked() as directory:
                if filename == r.STATE_FILE:
                    r._save(directory,r._state(directory))
                target=self.root/filename
                before=target.read_bytes();inode=target.stat().st_ino
                with self.assertRaises(s.AutomationError) as cm:
                    s.write_json(directory,filename,{'padding':'x'*limit})
                self.assertEqual(cm.exception.code,'automation_file_too_large')
                self.assertEqual(target.read_bytes(),before)
                self.assertEqual(target.stat().st_ino,inode)
                self.assertFalse(any(p.name.startswith('.'+filename) for p in self.root.iterdir()))
                self.assertIsNotNone(s.read_json(directory,filename))
        # Invalid external oversized files fail closed rather than returning truncated JSON.
        target=self.root/r.STATE_FILE
        target.write_bytes(b' '*s.STATE_MAX_BYTES+b'{}')
        with s.locked() as directory:
            with self.assertRaises(s.AutomationError):s.read_json(directory,r.STATE_FILE)

if __name__ == '__main__':
    unittest.main()
