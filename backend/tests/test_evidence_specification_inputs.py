"""Actual input producer/file/SQLite handoff; reviewed proposal is a fixture.

No authenticated model-generation or whole native operator claim is made.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from config.settings import settings
from src.db.models import WorkBoardInputArtifact
from src.memory.evidence_specification_inputs import stage_specification_input, recheck_specification_input, bind_specification_input
from src.work_board.contracts import WorkBoardInputArtifactCreate
from src.work_board.input_artifacts import prepare_input_artifact, resolve_input_artifact_for_task, bind_input_artifact
from src.work_board.repository import BoardError, WorkBoardRepository, _begin_sqlite_immediate
from tests.test_evidence_dependency_tokens import canonical_fact
from tests.test_browser_task_runtime import _input


@pytest.mark.asyncio
@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('change',['none','foreign-task','consumed','physical-drift','row-race','rollback','pipeline'])
async def test_prepared_exact_input_handoff_preserves_original_and_rolls_back_atomically(async_db,tmp_path,monkeypatch,change):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(settings,'workspace_dir',str(tmp_path))
    monkeypatch.setattr(WorkBoardRepository,'_safe_text',AsyncMock(side_effect=lambda value,**kw:value))
    async with async_db() as db:
        owner,task,_memory,_source=await canonical_fact(db)
        task_id=task.task_id
        async def prepare(label,inputs):
            return await prepare_input_artifact(db,owner,WorkBoardInputArtifactCreate(schema_version=1,
                capability_id='browser.public-task.v1',goal_id=task.goal_id,goal_revision=task.goal_revision,
                input=inputs,idempotency_key=label))
        original=await prepare('original-public-input',_input())
        resolved=await resolve_input_artifact_for_task(db,owner,artifact_id=original.artifact_id,
            goal_id=task.goal_id,goal_revision=task.goal_revision,capability_id=task.capability_id)
        await bind_input_artifact(db,owner,artifact=resolved,task_id=task_id,task_revision=1)
        task.input_artifact_id=original.artifact_id;task.typed_input_ref=original.typed_input_ref
        task.typed_input_digest=original.typed_input_digest
        await db.commit()
        inputs=_input();inputs['actions'][1]['max_chars']=256
        target=await prepare('replacement-public-input',inputs)
        item={'capability_id':task.capability_id,'typed_input_ref':target.typed_input_ref,
            'typed_input_digest':target.typed_input_digest}
        target_row=await db.get(WorkBoardInputArtifact,target.artifact_id)
        if change=='foreign-task':target_row.bound_task_id='another-task'
        elif change=='consumed':target_row.state='consumed'
        elif change=='pipeline':task.pipeline_operation_id='reviewed-existing-operation'
        elif change=='physical-drift':
            (tmp_path/target.typed_input_ref.removeprefix('workspace-json:')).write_bytes(b'changed outside the guarded writer')
        await db.commit()
        if change in {'foreign-task','consumed','physical-drift','pipeline'}:
            with pytest.raises(BoardError):await stage_specification_input(db,owner,task,'specify',[item])
            return
        staged=await stage_specification_input(db,owner,task,'specify',[item])
        if change=='row-race':
            target_row.state='revoked';await db.commit()
        await _begin_sqlite_immediate(db)
        task=await WorkBoardRepository().get_task(db,owner,task_id)
        def forbidden(*a,**kw):raise AssertionError('File inspection inside input handoff writer')
        monkeypatch.setattr('src.work_board.input_artifacts._safe_file_bytes',forbidden)
        if change=='row-race':
            with pytest.raises(BoardError):await recheck_specification_input(db,owner,task,staged)
            await db.rollback()
        else:
            current=await recheck_specification_input(db,owner,task,staged)
            await WorkBoardRepository()._cas_task_update(db,owner,task,expected_revision=1,
                values={'task_revision':2,'input_artifact_id':target.artifact_id,
                    'typed_input_ref':target.typed_input_ref,'typed_input_digest':target.typed_input_digest})
            await bind_specification_input(db,owner,task,staged,current)
            if change=='rollback':await db.rollback()
            else:await db.commit()
    async with async_db() as reopened:
        task=await WorkBoardRepository().get_task(reopened,owner,task_id)
        old=await reopened.get(WorkBoardInputArtifact,original.artifact_id)
        new=await reopened.get(WorkBoardInputArtifact,target.artifact_id)
        assert old.bound_task_id==task_id and old.bound_task_revision==1
        assert task.input_artifact_id==(target.artifact_id if change=='none' else original.artifact_id)
        assert new.state==('bound' if change=='none' else 'revoked' if change=='row-race' else 'pending')
        if change=='none':assert new.bound_task_id==task_id and new.bound_task_revision==2
        with (tmp_path/'actual-prepared-input-handoff.json').open('x') as f:
            json.dump({'boundary':__doc__,'change':change,'task_revision':task.task_revision,
                'old_artifact_id':old.artifact_id,'old_state':old.state,'new_artifact_id':new.artifact_id,
                'new_state':new.state,'no_learning':True},f,indent=2)
