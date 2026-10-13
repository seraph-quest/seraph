'Test diagnostics read checked result only during ORIGINAL held stage.'
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path


def install(context):
    from src.execution import repo_original_producer as original
    actual_stage = original.stage_original_producer_completion
    actual_result = original.original_producer_completion_result
    directory = Path(context['journey_directory'])

    @contextmanager
    def stage(*args, **kwargs):
        with actual_stage(*args, **kwargs) as physical:
            checked = actual_result(physical)
            manifest = checked['manifest']
            assert manifest == checked['original_producer_completion']['manifest']
            packet = {
                'diagnostic_origin': 'active_original_staged_signed_manifest',
                'current_owner_stage_checked': True,
                'backend_restart_echild_observed': False,
                'process_cleanup': manifest['process_cleanup'],
                'supervisor_transport': manifest['supervisor_transport'],
                'commands': [{name: entry[name] for name in ('stdout_eof', 'stderr_eof', 'waited', 'cleanup')}
                             for entry in manifest['commands']],
                'outcome': checked['original_producer_completion']['outcome'],
                'completion_sha256': hashlib.sha256(original.canonical(checked['original_producer_completion'])).hexdigest(),
            }
            target = directory / 'original-physical-closure.json'
            temporary = directory / ('physical-' + str(os.getpid()) + '.pending')
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'w') as stream:
                json.dump(packet, stream, sort_keys=True); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, target)
            yield physical
    original.stage_original_producer_completion = stage

    def restore():
        if original.stage_original_producer_completion is not stage:
            raise RuntimeError('closure_diagnostic_restore_order_changed')
        original.stage_original_producer_completion = actual_stage
    return restore
