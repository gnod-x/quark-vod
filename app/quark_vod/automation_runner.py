"""Durable bounded task queue and independent, fixed-command task execution."""
from contextlib import contextmanager
from datetime import datetime
import fcntl
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from zoneinfo import ZoneInfo

from . import automation_settings as settings

STATE_FILE = 'automation-runs.json'
MAX_RUNS = 32
MAX_QUEUE = 16
MAX_ACTIVE = 2
FINAL = {'complete','partial','failed','busy','timeout','interrupted','dry_run'}
_TERMINATE_GRACE = 3


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _state(directory):
    value = settings.read_json(directory, STATE_FILE)
    if value is None:
        return {'version':1, 'runs':[], 'slots':{}, 'worker':{}}
    if (type(value) is not dict or set(value) != {'version','runs','slots','worker'}
            or value['version'] != 1 or type(value['runs']) is not list
            or len(value['runs']) > MAX_RUNS or type(value['slots']) is not dict
            or set(value['slots']) - set(settings.TASKS) or type(value['worker']) is not dict):
        raise settings.AutomationError('automation_state_invalid')
    for row in value['runs']:
        if (type(row) is not dict or row.get('task') not in settings.TASKS
                or row.get('status') not in FINAL | {'queued','running'}
                or type(row.get('id')) is not str or len(row['id']) != 32
                or any(c not in '0123456789abcdef' for c in row['id'])):
            raise settings.AutomationError('automation_state_invalid')
    return value


def _save(directory, state):
    # Only discard completed history; pending/running jobs are never evicted.
    while len(state['runs']) > MAX_RUNS:
        index = next((i for i,r in enumerate(state['runs']) if r['status'] in FINAL), None)
        if index is None:
            raise settings.AutomationError('automation_queue_full', 409)
        state['runs'].pop(index)
    settings.write_json(directory, STATE_FILE, state)


def runs():
    with settings.locked() as directory:
        value = _state(directory)
        return {'runs':list(reversed(value['runs'])), 'worker':value['worker'], 'max_active':MAX_ACTIVE}


def _enqueue(state, task, config, source, dry_run, slot=''):
    if task not in settings.TASKS:
        raise settings.AutomationError('automation_unknown_task', 404)
    if not config['tasks'][task]['enabled']:
        raise settings.AutomationError('automation_task_disabled', 409)
    if any(r['task'] == task and r['status'] in {'queued','running'} for r in state['runs']):
        raise settings.AutomationError('automation_task_busy', 409)
    if sum(r['status'] == 'queued' for r in state['runs']) >= MAX_QUEUE:
        raise settings.AutomationError('automation_queue_full', 409)
    row = {'id':uuid.uuid4().hex, 'task':task, 'status':'queued', 'source':source,
           'dry_run':dry_run, 'queued_at':now(), 'started_at':None, 'finished_at':None,
           'exit_code':None, 'error':None,
           'settings_revision':config['revision'], 'timeout_seconds':config['tasks'][task]['timeout_seconds']}
    state['runs'].append(row)
    if slot:
        state['slots'][task] = slot
    return row


def enqueue(task, revision, dry_run=True):
    if not settings.revision_valid(revision) or type(dry_run) is not bool:
        settings.invalid()
    with settings.locked() as directory:
        config = settings.load_locked(directory)
        if revision != config['revision']:
            raise settings.AutomationError('automation_revision_conflict', 409)
        state = _state(directory)
        row = _enqueue(state, task, config, 'manual', dry_run)
        _save(directory, state)
        return {'run_id':row['id'], 'status':'queued', 'dry_run':dry_run}


def due(expression, stamp):
    minute, hour, dom, month, dow = settings.cron_fields(expression)
    cron_dow = (stamp.weekday() + 1) % 7
    fields = expression.split()
    dom_match, dow_match = stamp.day in dom, cron_dow in dow or (cron_dow == 0 and 7 in dow)
    # Standard cron OR semantics when both date fields are restricted.
    date_match = ((dom_match or dow_match) if not fields[2].startswith('*') and not fields[4].startswith('*')
                  else dom_match and dow_match)
    return stamp.minute in minute and stamp.hour in hour and stamp.month in month and date_match


def tick(stamp=None, dry_run=True):
    if type(dry_run) is not bool:
        settings.invalid()
    with settings.locked() as directory:
        config = settings.load_locked(directory)
        if not config['enabled']:
            return {'queued':[], 'scheduler_enabled':False}
        stamp = stamp or datetime.now(ZoneInfo(config['timezone']))
        if stamp.tzinfo is None:
            raise settings.AutomationError('automation_timezone_required', 400)
        stamp = stamp.astimezone(ZoneInfo(config['timezone']))
        slot = stamp.strftime('%Y-%m-%dT%H:%M')
        state = _state(directory)
        queued = []
        for task, spec in config['tasks'].items():
            if (not spec['enabled'] or state['slots'].get(task) == slot
                    or not any(due(expression, stamp) for expression in spec['schedules'])):
                continue
            try:
                row = _enqueue(state, task, config, 'schedule', dry_run, slot)
                queued.append(row['id'])
            except settings.AutomationError as exc:
                if exc.code not in {'automation_task_busy','automation_queue_full'}:
                    raise
        _save(directory, state)
        return {'queued':queued, 'scheduler_enabled':True}


def _finish(run_id, status, exit_code=None, error=None, output_bytes=0):
    with settings.locked() as directory:
        state = _state(directory)
        for row in state['runs']:
            if row['id'] == run_id:
                row.update(status=status, finished_at=now(), exit_code=exit_code, error=error,
                           output_bytes=output_bytes)
                break
        _save(directory, state)


def execution_context():
    with open('/proc/sys/kernel/random/boot_id') as stream:
        boot = stream.read().strip()
    return {'execution_boot_id':boot,'execution_namespace':os.readlink('/proc/self/ns/pid')}


def _group_writers(pgid):
    writers = []
    with os.scandir('/proc') as entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                with open(entry.path+'/stat') as stream:
                    text = stream.read()
                fields = text[text.rfind(')')+2:].split()
                if fields[0] != 'Z' and int(fields[2]) == pgid:
                    writers.append(int(entry.name))
            except (FileNotFoundError,ProcessLookupError):
                continue
            except PermissionError:
                raise settings.AutomationError('automation_recovery_uncertain') from None
    return writers


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def _stop_group(pgid):
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + _TERMINATE_GRACE
    while _group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(.05)
    if _group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 2
    while _group_writers(pgid):
        if time.monotonic() >= deadline:
            raise settings.AutomationError('automation_processes_survived')
        time.sleep(.02)


def _stop(process):
    # The original parent may already have exited while grandchildren still write.
    _stop_group(process.pid)
    process.wait(timeout=5)


def _identity(pid):
    try:
        with open(f'/proc/{pid}/stat') as stream:
            text = stream.read()
        fields = text[text.rfind(')')+2:].split()
        return None if fields[0] == 'Z' else fields[19]
    except (OSError, ValueError, IndexError):
        return None


def _owned_processes(row):
    pgid = row.get('child_pid')
    if not pgid:
        # Launch gate prevents all business work until this field has been committed.
        return []
    current = _identity(pgid)
    if current is not None and current != row.get('child_identity'):
        raise settings.AutomationError('automation_recovery_uncertain')
    result = []
    with os.scandir('/proc') as entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                with open(entry.path+'/stat') as stream:
                    text = stream.read()
                fields = text[text.rfind(')')+2:].split()
                if fields[0] != 'Z' and int(fields[2]) == pgid:
                    if int(fields[3]) != pgid:
                        raise settings.AutomationError('automation_recovery_uncertain')
                    result.append(int(entry.name))
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                raise settings.AutomationError('automation_recovery_uncertain') from None
    return result


def recover(terminate=False):
    if type(terminate) is not bool:
        settings.invalid()
    recovered, pending = [], []
    # Serialize with claims and completions so no new owner can replace this record.
    with settings.locked() as directory:
        state = _state(directory)
        for row in state['runs']:
            if row['status'] != 'running':
                continue
            context = execution_context()
            if not row.get('execution_boot_id') or not row.get('execution_namespace'):
                raise settings.AutomationError('automation_recovery_uncertain')
            if row['execution_boot_id'] != context['execution_boot_id']:
                row.update(status='interrupted',finished_at=now(),error='previous_boot')
                recovered.append(row['id'])
                continue
            if row['execution_namespace'] != context['execution_namespace']:
                raise settings.AutomationError('automation_recovery_namespace_mismatch',409)
            owner = row.get('owner_pid')
            if owner and _identity(owner) == row.get('owner_identity'):
                continue
            processes = _owned_processes(row)
            if processes and terminate:
                groups = {os.getpgid(pid) for pid in processes if _identity(pid) is not None}
                for group in groups:
                    # start_new_session creates a dedicated group whose ID is saved at launch.
                    if group != row.get('child_pid'):
                        raise settings.AutomationError('automation_recovery_uncertain')
                    _stop_group(group)
                processes = _owned_processes(row)
            if processes:
                row['error'] = 'recovery_required'
                pending.append(row['id'])
            else:
                row.update(status='interrupted',finished_at=now(),error='execution_owner_lost')
                recovered.append(row['id'])
        _save(directory,state)
    return {'recovered':recovered, 'pending':pending}


def run_next(stop_event=None, run_id=None):
    with settings.locked() as directory:
        state = _state(directory)
        if sum(r['status'] == 'running' for r in state['runs']) >= MAX_ACTIVE:
            return None
        row = next((r for r in state['runs'] if r['status'] == 'queued' and (run_id is None or r['id'] == run_id)), None)
        if row is None:
            return None
        config = settings.load_locked(directory)
        # Changes to task enablement apply before execution; never run disabled queued work.
        if not config['tasks'][row['task']]['enabled']:
            row.update(status='interrupted', finished_at=now(), error='task_disabled')
            _save(directory, state)
            return dict(row)
        row.update(**execution_context())
        row.update(status='running', started_at=now(), owner_pid=os.getpid(),
                   owner_identity=_identity(os.getpid()), settings_revision=config['revision'],
                   timeout_seconds=config['tasks'][row['task']]['timeout_seconds'])
        _save(directory, state)
        row = dict(row)
    if row['dry_run']:
        _finish(row['id'], 'dry_run')
        return {**row, 'status':'dry_run'}
    process = None
    try:
        # Fixed module and allowlisted task only: no caller-provided commands, paths or arguments.
        process = subprocess.Popen(
            [sys.executable, '-m', 'quark_vod.automation_child', row['task']],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
            env={**os.environ, 'VOD_AUTOMATION_RUN_ID':row['id'],
                 'VOD_AUTOMATION_AI_SNAPSHOT':__import__('json').dumps(config['ai']),
                 'VOD_AUTOMATION_SETTINGS_REVISION':str(config['revision']),
                 'TZ':config['timezone'],
                 'VOD_AUTOMATION_TIMEOUT_SECONDS':str(row['timeout_seconds'])})
        with settings.locked() as directory:
            state = _state(directory)
            for current in state['runs']:
                if current['id'] == row['id']:
                    current.update(child_pid=process.pid,child_identity=_identity(process.pid))
                    break
            _save(directory,state)
        size = [0]
        def drain():
            # Job output can contain upstream secrets. Count and discard it; only fixed status is persisted.
            while True:
                chunk = process.stdout.read(8192)
                if not chunk:
                    break
                size[0] = min(size[0] + len(chunk), 2**53 - 1)
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        deadline = time.monotonic() + row['timeout_seconds']
        status = error = None
        while process.poll() is None:
            if stop_event is not None and stop_event.is_set():
                status, error = 'interrupted', 'worker_stopped'
                _stop(process)
                break
            if time.monotonic() >= deadline:
                status, error = 'timeout', 'task_timeout'
                _stop(process)
                break
            time.sleep(.1)
        if status is None and _group_alive(process.pid):
            status, error = 'failed', 'task_descendants_left_running'
        _stop_group(process.pid)
        reader.join(timeout=2)
        process.stdout.close()
        code = process.returncode
        status = status or ('complete' if code == 0 else 'busy' if code == 75 else 'partial' if code == 1 else 'failed')
        _finish(row['id'], status, code, error, size[0])
        return {**row, 'status':status, 'exit_code':code}
    except Exception:
        if process is not None:
            try:
                _stop(process)
            except Exception:
                # A surviving writer is never released for a duplicate run.
                with settings.locked() as directory:
                    state = _state(directory)
                    for current in state['runs']:
                        if current['id'] == row['id']:
                            current['error'] = 'recovery_required'
                    _save(directory,state)
                return {**row,'status':'running','error':'recovery_required'}
        _finish(row['id'], 'failed', error='task_execution_failed')
        return {**row, 'status':'failed'}


@contextmanager
def worker_lock():
    directory = descriptor = None
    try:
        directory = settings.private._directory()
        descriptor = os.open('automation-worker.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
        settings.private._private_file(descriptor)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise settings.AutomationError('automation_worker_busy', 409) from None
        yield
    except (OSError, settings.private.SettingsError):
        raise settings.AutomationError() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def _heartbeat(status):
    with settings.locked() as directory:
        state = _state(directory)
        state['worker'] = {'status':status, 'pid':os.getpid(), 'updated_at':now()}
        _save(directory, state)


def worker(*, stop_event=None, dry_run=True, poll_seconds=1):
    """Single scheduler owner; executor runs separately so long jobs do not block scheduling."""
    if type(dry_run) is not bool or not .1 <= poll_seconds <= 60:
        settings.invalid()
    stop_event = stop_event or threading.Event()
    with worker_lock():
        # A prior worker owning this queue has exited. In-flight writes must be recovered
        # by each job's existing intent/checkpoint logic, never automatically duplicated.
        recover()
        def execute_queue():
            while not stop_event.is_set():
                try:
                    if run_next(stop_event) is None:
                        stop_event.wait(poll_seconds)
                except settings.AutomationError:
                    stop_event.wait(poll_seconds)
        executors = [threading.Thread(target=execute_queue, name=f'automation-executor-{index}', daemon=True)
                     for index in range(MAX_ACTIVE)]
        for executor in executors:
            executor.start()
        try:
            while not stop_event.is_set():
                tick(dry_run=dry_run)
                _heartbeat('running')
                stop_event.wait(poll_seconds)
        finally:
            stop_event.set()
            for executor in executors:
                executor.join(timeout=_TERMINATE_GRACE + 8)
            _heartbeat('stopped')


def cli_main(argv=None):
    """Task CLI used by quark-vod jobs; execution is dry-run unless --execute."""
    import argparse
    import json
    parser = argparse.ArgumentParser(prog='quark-vod jobs')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    sub.add_parser('status')
    recovery = sub.add_parser('recover')
    recovery.add_argument('--terminate',action='store_true')
    run = sub.add_parser('run')
    run.add_argument('task', choices=list(settings.TASKS))
    run.add_argument('--execute', action='store_true')
    tick_parser = sub.add_parser('tick')
    tick_parser.add_argument('--execute', action='store_true')
    worker_parser = sub.add_parser('worker')
    worker_parser.add_argument('--execute', action='store_true')
    worker_parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'list':
            result = {'jobs':settings.catalog(), 'settings':settings.get_settings()}
        elif args.command == 'status':
            result = runs()
        elif args.command == 'recover':
            result = recover(args.terminate)
        elif args.command == 'run':
            result = enqueue(args.task,settings.get_settings()['revision'],not args.execute)
            result = run_next(run_id=result['run_id']) or result
        elif args.command == 'tick':
            result = tick(dry_run=not args.execute)
        elif args.once:
            with worker_lock():
                tick(dry_run=not args.execute)
                result = run_next()
            result = result or {'status':'idle'}
        else:
            event = threading.Event()
            previous = {}
            for signum in (signal.SIGTERM,signal.SIGINT):
                previous[signum] = signal.signal(signum,lambda *_:event.set())
            try:
                worker(stop_event=event,dry_run=not args.execute)
            finally:
                for signum,handler in previous.items():
                    signal.signal(signum,handler)
            result = {'status':'stopped'}
        print(json.dumps(result,ensure_ascii=False))
        if isinstance(result,dict) and result.get('status') in {'failed','partial','timeout','interrupted'}:
            return 1
        if isinstance(result,dict) and result.get('status') == 'busy':
            return 75
        return 0
    except settings.AutomationError as exc:
        print(json.dumps({'error':exc.code},ensure_ascii=False))
        return 75 if exc.code.endswith('_busy') else 1
