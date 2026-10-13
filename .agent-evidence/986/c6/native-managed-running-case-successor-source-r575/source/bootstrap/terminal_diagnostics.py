'Original finite constructor capture inside writer; emit only AFTER commit.'
import json
import os
from pathlib import Path


def install(context):
    from sqlalchemy import inspect
    from src.workflows import repo_repair_source as source
    actual_capture = source.capture_recovered_repository_final_writer
    actual_observe = source.observe_recovered_repository_final_writer
    pending = {}
    directory = Path(context['journey_directory'])

    async def capture(*args, **kwargs):
        actual = await actual_capture(*args, **kwargs)
        witness = kwargs['completion_witness']
        state = source._recovered_finalizer_slot(witness)['state']
        stage = state['pending']
        if stage['kind'] != kwargs['kind'] or stage['after'] is None:
            raise AssertionError('original_constructor_capture_absent')
        # Memory only in original SQL writer, after its exact constructor
        # comparison and before real commit. Never emit/read marker here.
        expected = []
        for (model, key), raw in stage['after'].items():
            expected.append({'identity': {'table': model.__tablename__,
                'columns': [column.key for column in inspect(model).primary_key],
                'key': list(key) if isinstance(key, tuple) else [key]}, 'raw': raw})
        pending[witness] = {'kind': kwargs['kind'], 'expected': expected}
        return actual

    async def observe(jobs, *, completion_witness, kind):
        packet = pending.pop(completion_witness)
        if packet['kind'] != kind:
            raise AssertionError('original_constructor_observation_order_changed')
        actual = await actual_observe(jobs, completion_witness=completion_witness, kind=kind)
        # Original independently checked every committed row before returning.
        # Only now, outside SQL, can a private diagnostic be written.
        if kind == 'terminal':
            packet.update(origin='original_finite_constructor_before_commit', original_postcommit_readback=True)
            target = directory / 'terminal-constructors.json'
            temporary = directory / 'terminal-constructors.pending'
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(packet, stream, sort_keys=True); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, target)
        return actual

    source.capture_recovered_repository_final_writer = capture
    source.observe_recovered_repository_final_writer = observe

    def restore():
        if source.capture_recovered_repository_final_writer is not capture or source.observe_recovered_repository_final_writer is not observe:
            raise RuntimeError('terminal_diagnostic_restore_order_changed')
        source.capture_recovered_repository_final_writer = actual_capture
        source.observe_recovered_repository_final_writer = actual_observe
    return restore
