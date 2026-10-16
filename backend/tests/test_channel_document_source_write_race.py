"""Actual authenticated retirement interleaved between preflight and final CAS."""
import json

import pytest
from sqlalchemy import select

from tests.test_telegram_document_ingest import prepare, setup_workspace, authenticated_setup_operator  # noqa: F401
from tests.general_task_method_lifecycle import AdmissionSignerLifetime
from src.db.models import WorkBoardTask, WorkBoardInputArtifact, TelegramInboundUpdate
from src.work_board.channel_capture import staged_captured_source_identity
from src.work_board.contracts import GeneralTaskPlanUpdate, PlanSpec
from src.work_board.repository import BoardError

pytestmark = pytest.mark.parametrize('async_db', ['file'], indirect=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['plan', 'promotion'])
async def test_source_retirement_after_successful_preflight_blocks_final_positive_cas(
    client, async_db, setup_workspace, monkeypatch, operation,
):
    from src.api import documents as document_api, work_board as board_api
    from src.work_board import repository as repository_module, input_artifacts
    from src.work_board.historical_method import historical_method_service
    monkeypatch.setattr(document_api, 'get_session', async_db)
    adapter, boundary, document_service, event, owner, raw, contacts = await prepare(
        client, monkeypatch, async_db, 'csv')
    service = board_api.dispatcher.general_tasks
    signer_lifetime = AdmissionSignerLifetime(historical_method_service)
    try:
        captured = await client.post('/api/telegram/updates', json=event)
        assert captured.status_code == 200, captured.text
        task_id = captured.json()['channel_task_capture']['task_id']
        if operation == 'promotion':
            await signer_lifetime.start()
            assert historical_method_service.signing_key is not None
        descriptor = next(item for item in service.snapshot()[0] if item.tool_id == 'read_file')
        plan = PlanSpec(revision=1, steps=[{'step_id':'read', 'tool_id':'read_file',
            'input':{'file_path':'proof.txt'}, 'output_contract':descriptor.output_schema}])
        if operation == 'promotion':
            with staged_captured_source_identity():
                async with async_db() as db:
                    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
                    await service.update_plan(db, owner, task_id, GeneralTaskPlanUpdate(
                        expected_revision=task.task_revision, expected_plan_revision=0,
                        idempotency_key='review-before-promotion-race', plan=plan))
        async with async_db() as db:
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
            original_task = task.model_dump(mode='json')
            original_artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id)
            original_metadata = original_artifact.model_dump(mode='json')
            original_bytes = input_artifacts._payload_path(original_artifact).read_bytes()
            row = await db.scalar(select(TelegramInboundUpdate).where(
                TelegramInboundUpdate.idempotency_key == captured.json()['idempotency_key']))
            source_id = json.loads(row.receipt_json)['channel_task_capture']['document_source']['artifact_id']
            source_revision = (await db.get(WorkBoardInputArtifact, source_id)).revision
        interleaved = []
        async def retire_after_preflight(db):
            await db.commit()  # same completed staging transaction as the real writer helper
            retired = await client.delete(f'/api/documents/sources/{source_id}',
                params={'expected_revision':source_revision})
            assert retired.status_code == 200, retired.text
            interleaved.append(source_id)
        if operation == 'plan':
            actual_begin = repository_module._begin_sqlite_immediate
            async def begin_after_retirement(db):
                if not interleaved:
                    await retire_after_preflight(db)
                await actual_begin(db)
            monkeypatch.setattr(repository_module, '_begin_sqlite_immediate', begin_after_retirement)
            with staged_captured_source_identity():
                async with async_db() as db:
                    with pytest.raises(BoardError) as rejected:
                        await service.update_plan(db, owner, task_id, GeneralTaskPlanUpdate(
                            expected_revision=original_task['task_revision'], expected_plan_revision=0,
                            idempotency_key='race-plan-replacement', plan=plan))
                    assert rejected.value.code == 'channel_document_source_unsealed'
                    await db.rollback()
        else:
            actual_action = board_api.repository.action_task
            async def action_after_retirement(db, actor, identity, request, **kwargs):
                assert identity == task_id
                await retire_after_preflight(db)
                return await actual_action(db, actor, identity, request, **kwargs)
            monkeypatch.setattr(board_api.repository, 'action_task', action_after_retirement)
            result = await client.post(f'/api/work-board/tasks/{task_id}/actions',
                json={'action':'promote','expected_revision':original_task['task_revision']})
            assert result.status_code == 409, result.text
            assert result.json()['detail']['code'] == 'channel_document_source_unsealed', result.text
        assert interleaved == [source_id]
        async with async_db() as db:
            current = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
            assert current.model_dump(mode='json') == original_task
            artifact = await db.get(WorkBoardInputArtifact, current.input_artifact_id)
            assert artifact.model_dump(mode='json') == original_metadata
            assert input_artifacts._payload_path(artifact).read_bytes() == original_bytes
            replacements = (await db.scalars(select(WorkBoardInputArtifact).where(
                WorkBoardInputArtifact.idempotency_key == 'general-edit:race-plan-replacement'))).all()
            if operation == 'plan':
                assert len(replacements) == 1
                assert replacements[0].bound_task_id is None
                replacement = replacements[0]
                replacement_path = input_artifacts._payload_path(replacement)
                await input_artifacts.delete_input_artifact(
                    db, owner, artifact_id=replacement.artifact_id,
                    expected_revision=replacement.revision)
                assert not replacement_path.exists()
        if operation == 'plan':
            async with async_db() as db:
                replacement = await db.get(WorkBoardInputArtifact, replacement.artifact_id)
                assert replacement.state == 'deleted'
                assert replacement.bound_task_id is None
        assert contacts == ['POST','GET'] and raw
    finally:
        await signer_lifetime.close()
        await document_service.stop()
        await boundary.http.aclose()
