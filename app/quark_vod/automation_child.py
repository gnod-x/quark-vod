"""Private launch gate: no job may write before its durable process identity is registered."""
import os
import sys
import time
from . import automation_runner as runner, automation_settings as settings


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in settings.TASKS:
        return 64
    run_id = os.environ.get('VOD_AUTOMATION_RUN_ID','')
    if len(run_id) != 32:
        return 64
    deadline = time.monotonic() + 5
    identity = runner._identity(os.getpid())
    while time.monotonic() < deadline:
        with settings.locked() as directory:
            state = runner._state(directory)
            row = next((r for r in state['runs'] if r['id'] == run_id),None)
        if not row or row['status'] != 'running':
            return 74
        context = runner.execution_context()
        if any(row.get(key) != value for key,value in context.items()):
            return 74
        if row.get('child_pid') == os.getpid() and row.get('child_identity') == identity:
            owner = row.get('owner_pid')
            if not owner or runner._identity(owner) != row.get('owner_identity'):
                return 74
            os.execv(sys.executable,[sys.executable,'-m',
                                    'quark_vod.integrations.hometheater.execute',sys.argv[1]])
        time.sleep(.02)
    return 74


if __name__ == '__main__':
    raise SystemExit(main())
