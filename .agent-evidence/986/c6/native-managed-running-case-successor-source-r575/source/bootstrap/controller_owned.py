'Finite owned backend stop for negative mutation before original restart.'
import os
from pathlib import Path
import select
import signal


def stop_backend_for_mutation(manager, context, *, pid, start, listener_readback):
    if getattr(manager, 'stopped_backend', None):
        raise RuntimeError('duplicate_owned_backend_stop')
    roles = manager.verify_original_managed_roles()
    service = roles['backend_service']
    if service['pid'] != pid or service['start'] != start:
        raise RuntimeError('owned_service_changed')
    db_identity = manager.read_runtime_database(pid, start)
    fd = os.pidfd_open(pid)
    try:
        proc = Path('/proc') / str(pid)
        actual_start = int((proc / 'stat').read_text().rsplit(')', 1)[1].split()[19])
        argv = (proc / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
        if (actual_start != start or proc.stat().st_uid != os.getuid()
                or argv != [value.encode() for value in context['reviewed_backend_process_argv']]):
            raise RuntimeError('exact_backend_stop_identity_changed')
        listener_readback(pid, start)
        signal.pidfd_send_signal(fd, signal.SIGKILL)
        poll = select.poll(); poll.register(fd, select.POLLIN)
        if not poll.poll(5000):
            raise RuntimeError('owned_backend_stop_not_reaped')
        manager.stopped_backend = {'pid': pid, 'start': start, 'pidfd': fd,
                                  'database_identity': list(db_identity)}
        manager.last_killed_pid = pid
        return manager.stopped_backend
    except BaseException:
        os.close(fd)
        raise


def restart_after_mutation(manager):
    from held_outcomes import assert_backend_stopped
    assert_backend_stopped(manager)
    # Original manager remains tailing; original local run reuses its frontend.
    manager.run()


def finish(manager, context, *, original_checked_closure=None, held_receipt=None):
    if held_receipt is not None:
        if held_receipt.get('fixture_preserved') is not True or held_receipt.get('completion_proven') is not False:
            raise RuntimeError('held_receipt_not_conservative')
        # Keep owned controllers/native pidfds/physical DB/material for Root.
        # No original local-down command or physical removal is authorized here.
        return {'fixture_preserved': True, 'managed_cleanup_executed': False}
    if original_checked_closure is None:
        raise RuntimeError('original_signed_closure_or_actual_held_receipt_required')
    # Root journey verifies original accessor within its scoped guard first.
    from assertions import assert_closure
    assert_closure(original_checked_closure)
    manager.down_archive_remove(context)
    if getattr(manager, 'stopped_backend', None):
        os.close(manager.stopped_backend['pidfd'])
        manager.stopped_backend = None
    return {'fixture_preserved': False, 'managed_cleanup_executed': True}
