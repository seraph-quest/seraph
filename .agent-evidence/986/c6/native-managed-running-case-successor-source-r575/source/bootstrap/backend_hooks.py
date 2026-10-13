'Private test hooks, installed ONLY after kernel receipt/parent ACK.\n\nOriginal Source functions, owners, signers, admission and result remain real.\nNo API, authority rows or artifact validity is supplied by these observations.\n'
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import time
import sqlite3


def sql_epoch(context):
    database = Path('/tmp/seraph-1009-n12-runtime/workspace/seraph.db')
    connection = sqlite3.connect('file:' + str(database) + '?mode=ro', uri=True)
    try:
        connection.execute('BEGIN')
        result = {}
        names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for name in names:
            if not name.replace('_', '').isalnum():
                raise RuntimeError('literal_sql_table_name_changed')
            rows = connection.execute('SELECT * FROM "' + name + '" ORDER BY rowid').fetchall()
            normalized = [[{'bytes': value.hex()} if isinstance(value, bytes) else value for value in row] for row in rows]
            raw = json.dumps(normalized, sort_keys=True, separators=(',', ':')).encode()
            result[name] = {'count': len(rows), 'sha256': hashlib.sha256(raw).hexdigest()}
        return result
    finally:
        connection.close()


def install(context, *, extra_restorers):
    import httpx
    from sqlalchemy import inspect as mapper
    from src.workflows import repo_repair_source as source
    from src.workflows import repo_repair_source_recovery as recovery
    from src.workflows.job_runtime import _canonical
    from fastapi import Request
    from src import app as app_module
    from src.api import workflows as workflows_api
    from src.work_board.contracts import WorkBoardOwner

    directory = Path(context['journey_directory'])
    # Restart preserves the controller's already reviewed original diagnostic
    # epoch. This boolean gates only test transport/observations, never Source
    # authority. The canonical cold helper stages fresh current rows itself.
    config = json.loads((directory / 'case.json').read_text())
    selected = config['cutpoint']
    expected_requests = {}
    actual_precontact, actual_preview = source._repository_precontact, source.repository_source_preview
    actual_publish, actual_append = recovery.publish_original_repository_completion, source._append_repository_record
    actual_stage = recovery.stage_original_repository_completion_publication
    original_client = httpx.Client
    failed_once = False
    original_context = None
    contact_epoch_reviewed = False
    if (directory / 'source-contact-epoch.json').exists():
        release = directory / 'contact-reviewed.json'
        if not release.exists() or json.loads(release.read_text()) != {'nonce': context['nonce']}:
            raise RuntimeError('original_contact_epoch_restart_receipt_changed')
        contact_epoch_reviewed = True

    def retain(name, value):
        # Fixed private observation names; caller never provides authority.
        target = directory / name
        raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
        temp = target.with_suffix('.pending')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, target)

    async def snapshot(jobs, staged):
        nonlocal original_context
        identifiers, rows = [], []
        from src.db.models import (WorkBoardTask, WorkBoardAttempt, WorkBoardInputArtifact,
                                   WorkflowRunState, Goal, OperatorSession)
        # Literal actual Source constructor order at _repository_precontact.
        expected_models = [WorkflowRunState, WorkflowRunState, WorkflowRunState,
            WorkBoardTask, WorkBoardAttempt, WorkBoardInputArtifact, WorkBoardTask,
            WorkBoardAttempt, Goal, OperatorSession, WorkBoardInputArtifact]
        binding, authority = staged['binding'], staged['authority']
        expected_ids = [staged['run'].id, staged['parent'].id, staged['child'].id,
            None, binding.attempt_id, None, authority.task.creation_sequence,
            authority.attempt.attempt_id, staged['group'].goal_id,
            staged['group'].owner_session_id, authority.task.input_artifact_id]
        if len(staged['rows']) != 11 or [model for model, _key, _raw in staged['rows']] != expected_models:
            raise RuntimeError('original_source_constructor_vector_changed')
        # WorkBoardTask PK is creation_sequence; its separate task_id must
        # still match the exact canonical native binding.
        parent_task_raw = json.loads(staged['rows'][3][2])
        if parent_task_raw['task_id'] != binding.task_id:
            raise RuntimeError('original_parent_task_binding_changed')
        expected_ids[3] = parent_task_raw['creation_sequence']
        expected_ids[5] = parent_task_raw['input_artifact_id']
        if (json.loads(staged['rows'][6][2])['task_id'] != authority.task.task_id
                or json.loads(staged['rows'][7][2])['attempt_id'] != authority.attempt.attempt_id
                or json.loads(staged['rows'][9][2])['id'] != staged['group'].owner_session_id):
            raise RuntimeError('original_source_linked_identity_changed')
        seen = set()
        async with jobs._session() as db:
            for index, (model, key, original_raw) in enumerate(staged['rows']):
                if key != expected_ids[index] or (model.__tablename__, str(key)) in seen:
                    raise RuntimeError('original_source_identity_vector_changed')
                seen.add((model.__tablename__, str(key)))
                item = await db.get(model, key)
                if item is None:
                    raise RuntimeError('original_current_row_missing')
                columns = [column.key for column in mapper(model).primary_key]
                identifiers.append({'table': model.__tablename__, 'columns': columns,
                    'key': list(key) if isinstance(key, tuple) else [key]})
                actual_raw = _canonical(item.model_dump(mode='json'))
                if actual_raw != original_raw:
                    raise RuntimeError('original_staged_source_row_drift')
                rows.append({'table': model.__tablename__, 'raw': original_raw})
        if len(rows) != 11:
            raise RuntimeError('original_context_not_eleven_rows')
        packet = {'row_ids': identifiers, 'rows': rows, 'root_id': staged['run'].run_identity}
        retain('current-eleven.json', packet)
        original_context = staged
        return packet

    async def precontact(service, jobs, **kwargs):
        nonlocal contact_epoch_reviewed
        staged = await actual_precontact(service, jobs, **kwargs)
        packet = await snapshot(jobs, staged)
        if (directory / 'original-root.json').exists() and not contact_epoch_reviewed:
            # Pause the actual staged owner epoch BEFORE its caller's writer
            # or model contact; no diagnostic file operation occurs in SQL.
            retain('source-contact-epoch.json', packet)
            retain('precontact-ready.json', {'root_id': staged['run'].run_identity})
            await wait_release('contact-reviewed.json')
            contact_epoch_reviewed = True
        return staged

    async def preview(service, jobs, **kwargs):
        result = await actual_preview(service, jobs, **kwargs)
        body = result['egress']['request_body']
        expected_requests[kwargs['job_id']] = body
        return result

    def final_transport(request):
        if (str(request.url) != 'https://openrouter.ai/api/v1/chat/completions'
                or request.method != 'POST' or len(expected_requests) != 1):
            raise RuntimeError('unowned_scripted_provider_request')
        body = json.loads(request.content)
        if body != next(iter(expected_requests.values())):
            raise RuntimeError('original_prepared_provider_body_changed')
        if original_context is None or not contact_epoch_reviewed:
            raise RuntimeError('actual_source_contact_epoch_absent')
        facts = json.loads(body['messages'][1]['content'])
        output = dict(config['model_output'])
        output['base_snapshot_sha256'] = facts['source_packet']['base_snapshot_sha256']
        retain('scripted-contact.json', {'real_provider_socket_contacts': 0,
            'scripted_request_sha256': hashlib.sha256(request.content).hexdigest(), 'actual_cost_microusd': 0})
        return httpx.Response(200, json={'id': 'managed-scripted-final-transport',
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'cost': '0.000000'},
            'choices': [{'message': {'role': 'assistant', 'content': json.dumps(output)}}]})

    def client(*args, **kwargs):
        if 'transport' in kwargs:
            raise RuntimeError('unexpected_original_transport_override')
        return original_client(*args, **kwargs, transport=httpx.MockTransport(final_transport))

    def append(run, identity, payload, **kwargs):
        nonlocal failed_once
        if (selected == 'cleanup_readback_sql_rollback' and not failed_once
                and not (directory / 'sql-rollback.json').exists()
                and identity.startswith('repository:readback:')):
            failed_once = True
            raise RuntimeError('managed_actual_readback_rollback')
        return actual_append(run, identity, payload, **kwargs)

    async def wait_release(name):
        # Observer only: real publication still enforces original limits/cutoffs.
        cutoff = time.monotonic() + 5
        release = directory / name
        while time.monotonic() < cutoff:
            if release.exists():
                if json.loads(release.read_text()) != {'nonce': context['nonce']}:
                    raise RuntimeError('observer_release_changed')
                return
            await asyncio.sleep(0.01)
        raise RuntimeError('managed_observer_window_expired')

    async def publish(service, jobs, **kwargs):
        sql_before = sql_epoch(context)
        before = None
        async with jobs._session() as db:
            before = _canonical((await jobs._fetch(db, kwargs['job_id'])).model_dump(mode='json'))
        try:
            result = await actual_publish(service, jobs, **kwargs)
        except RuntimeError as error:
            if str(error) != 'managed_actual_readback_rollback':
                raise
            async with jobs._session() as db:
                after = _canonical((await jobs._fetch(db, kwargs['job_id'])).model_dump(mode='json'))
            if before != after:
                raise RuntimeError('whole_cleanup_rollback_failed')
            if sql_epoch(context) != sql_before:
                raise RuntimeError('whole_sql_epoch_rollback_failed')
            retain('sql-rollback.json', {'root_id': kwargs['job_id'], 'before': before, 'after': after})
            await wait_release('retry-original.json')
            # SAME authentic Source/jobs/producer/result, fresh original writer.
            result = await actual_publish(service, jobs, **kwargs)
            retain('sql-retry.json', {'root_id': kwargs['job_id'], 'same_original_result': True})
            retain('append-committed.json', {'root_id': kwargs['job_id'], 'before_terminal': True})
            await wait_release('continue-original.json')
        if selected == 'append_commit_before_terminal_cas':
            retain('append-committed.json', {'root_id': kwargs['job_id'], 'before_terminal': True})
            await wait_release('continue-original.json')
        return result

    @asynccontextmanager
    async def stage(service, jobs, **kwargs):
        sql_before = sql_epoch(context)
        yielded = False
        async with jobs._session() as db:
            before = _canonical((await jobs._fetch(db, kwargs['job_id'])).model_dump(mode='json'))
        try:
            async with actual_stage(service, jobs, **kwargs) as witness:
                if selected == 'append_commit_before_terminal_cas':
                    retain('append-committed.json', {'root_id': kwargs['job_id'], 'before_terminal': True})
                    await wait_release('continue-original.json')
                yielded = True
                yield witness
        except RuntimeError as error:
            if yielded or str(error) != 'managed_actual_readback_rollback':
                raise
            async with jobs._session() as db:
                after = _canonical((await jobs._fetch(db, kwargs['job_id'])).model_dump(mode='json'))
            if before != after:
                raise RuntimeError('whole_cleanup_rollback_failed')
            if sql_epoch(context) != sql_before:
                raise RuntimeError('whole_sql_epoch_rollback_failed')
            retain('sql-rollback.json', {'root_id': kwargs['job_id'], 'before': before, 'after': after})
            await wait_release('retry-original.json')
            async with actual_stage(service, jobs, **kwargs) as witness:
                retain('sql-retry.json', {'root_id': kwargs['job_id'], 'same_original_result': True,
                       'original_scoped_cold_owner': True})
                retain('append-committed.json', {'root_id': kwargs['job_id'], 'before_terminal': True})
                await wait_release('continue-original.json')
                yield witness

    source._repository_precontact = precontact
    source.repository_source_preview = preview
    source._append_repository_record = append
    recovery.publish_original_repository_completion = publish
    recovery.stage_original_repository_completion_publication = stage
    httpx.Client = client
    original_factory = app_module.create_app

    async def original_reconcile(request: Request):
        operator = workflows_api._require_authenticated_capability_operator(request)
        body = await request.json()
        if set(body) != {'job_id', 'expected_job_revision'} or type(body['expected_job_revision']) is not int:
            raise RuntimeError('managed_control_grammar_changed')
        original = json.loads((directory / 'original-root.json').read_text())
        if body['job_id'] != original['job_id']:
            raise RuntimeError('managed_control_original_root_changed')
        tasks, service, jobs = workflows_api._repo_repair_source_owner()
        # Actual authenticated bearer/current owner and canonical registered
        # physical stage are checked by the existing ownerless helper.
        try:
            return await recovery._reconcile_original_repository_cleanup(service, jobs,
                job_id=body['job_id'], expected_job_revision=body['expected_job_revision'],
                owner=WorkBoardOwner(principal_id=operator.principal.principal_id,
                                     session_id=operator.session_id))
        except recovery.RepositorySourceRecoveryError as error:
            from fastapi import HTTPException
            raise HTTPException(status_code=error.status_code,
                                detail={'code': error.code, 'no_learning': True}) from error
        except ValueError as error:
            from fastapi import HTTPException
            raise HTTPException(status_code=409, detail={'code': 'repository_source_recovery_blocked',
                                'original_reason': str(error), 'no_learning': True}) from error

    def factory(*args, **kwargs):
        app = original_factory(*args, **kwargs)
        app.add_api_route('/api/_managed-test/original-reconcile', original_reconcile, methods=['POST'])
        app.add_event_handler('shutdown', restore)
        return app

    def restore():
        # Observer is installed last and restored first. This preserves exact
        # wrapper identities before the outer hooks restore their originals.
        for callback in reversed(extra_restorers):
            callback()
        extra_restorers.clear()
        bindings = [(source, '_repository_precontact', precontact, actual_precontact),
            (source, 'repository_source_preview', preview, actual_preview),
            (source, '_append_repository_record', append, actual_append),
            (recovery, 'publish_original_repository_completion', publish, actual_publish),
            (recovery, 'stage_original_repository_completion_publication', stage, actual_stage),
            (httpx, 'Client', client, original_client), (app_module, 'create_app', factory, original_factory)]
        for module, name, wrapper, actual in bindings:
            if getattr(module, name) is not wrapper:
                raise RuntimeError('managed_hook_restore_order_changed')
            setattr(module, name, actual)
    app_module.create_app = factory

    allowed_points = {'intent_before_popen', 'registration_commit_before_ack',
                      'cleanup_artifact_fsync_before_append', 'unknown_first_stage',
                      'public_stop_before_uncertainty'}
    def emit(point, fields):
        if point not in allowed_points or fields['nonce'] != context['nonce'] or fields['point'] != point:
            raise RuntimeError('original_observer_marker_grammar_changed')
        root = json.loads((directory / 'original-root.json').read_text())
        if fields['job_id'] != root['job_id'] or fields['backend_pid'] != os.getpid():
            raise RuntimeError('original_observer_owner_changed')
        retain(point + '.json', {**fields, 'root_id': fields['job_id']})
    def released(point):
        if point not in allowed_points:
            raise RuntimeError('unowned_original_observer_release')
        path = directory / ('release-' + point + '.json')
        if not path.exists():
            return False
        from managed_fixture_dotenv import private_file
        if json.loads(private_file(path).read_text()) != {'nonce': context['nonce']}:
            raise RuntimeError('original_observer_release_nonce_changed')
        return True
    policy_capabilities = set()
    def policy_observed(fields):
        if (set(fields) != {'capability', 'request_sha256', 'real_provider_socket_contacts'}
                or fields['capability'] not in {'text', 'structured_output', 'health', 'latency_ms'}
                or fields['capability'] in policy_capabilities
                or type(fields['real_provider_socket_contacts']) is not int
                or fields['real_provider_socket_contacts'] != 0
                or not isinstance(fields['request_sha256'], str)
                or len(fields['request_sha256']) != 64
                or any(value not in '0123456789abcdef' for value in fields['request_sha256'])):
            raise RuntimeError('unowned_policy_canary_observation')
        policy_capabilities.add(fields['capability'])
        retain('policy-canary-' + str(os.getpid()) + '-' + fields['capability'] + '.json',
               {'backend_pid': os.getpid(), **fields})
    return emit, released, policy_observed
