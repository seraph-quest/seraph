"""Genuine native admission and unlinked Attempt recovery metadata."""
from datetime import datetime,timedelta,timezone
import json

import pytest
from sqlalchemy import select

from src.auth.service import revoke_session
from src.db.models import WorkBoardTask,WorkBoardAttempt
from src.goals.repository import GoalRepository
from src.memory.task_methods import CurrentMethod,TaskMethodStrategyResolver
from src.native_tools.registry import ToolRegistry
from src.work_board.contracts import WorkBoardOwner
from src.work_board.general_task import GeneralTaskService
from src.work_board.historical_method import historical_method_service,verify_historical_method
from src.operator.home_projection import home_projection
from tests.test_home_continuation import accounting_db,home_setup
from tests.test_general_task_methods import read_request
from tests.test_general_task_planner import forbid_external_inference


@pytest.mark.parametrize('original',['valid_revoked_root','legacy_null','corrupt_mac','active_lease'])
async def test_genuine_unlinked_attempt_reclaim_keeps_historical_contract(accounting_db,monkeypatch,forbid_external_inference,original):
    client,operator = await home_setup(accounting_db,monkeypatch)
    goal = await GoalRepository().create('Private attempt recovery source',
        owner_principal_id=operator.principal.principal_id,owner_session_id=operator.session_id)
    await historical_method_service.start()
    current = CurrentMethod()
    await current.start()
    registry = ToolRegistry()
    registry.start()
    service = GeneralTaskService(registry,strategy_resolver=TaskMethodStrategyResolver(current))
    service.start()
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id,session_id=operator.session_id)
    sessions = accounting_db[2].accounting_sessions
    request = read_request(registry,'original-unlinked-'+original)
    request = request.model_copy(update={'input':request.input.model_copy(update={'goal_ref':goal.id})})
    try:
        async with sessions() as db:
            created = await service.create(db,owner,request)
            original_raw = created.task.admitted_method_json
            original_witness = verify_historical_method(original_raw,task=created.task)
            assert original_witness is not None and original_witness.strategy_status=='none'
            ready = await service.repository.promote_task_ready(db,created.task.task_id,
                expected_revision=created.task.task_revision,actor_principal_id='actual-test-dispatcher')
            claim = await service.repository.claim_ready_task(db,created.task.task_id,
                expected_revision=ready.task.task_revision,lease_owner='original-worker',lease_seconds=300 if original=='active_lease' else 1,
                now=datetime.now(timezone.utc)-timedelta(seconds=10) if original!='active_lease' else None)
            assert claim is not None and claim.attempt.workflow_run_id is None
            assert verify_historical_method(claim.attempt.admitted_method_json,task=claim.task,attempt=claim.attempt)
            task_id,attempt_id,revision = claim.task.task_id,claim.attempt.attempt_id,claim.task.task_revision
        if original=='active_lease':
            from src.work_board.repository import BoardError
            async with sessions() as db:
                with pytest.raises(BoardError) as failure:
                    await service.repository.reclaim_expired_attempt(db,task_id,attempt_id,
                        expected_revision=revision,lease_owner='replacement-worker')
                assert failure.value.code=='lease_active'
                old = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id==attempt_id))
                assert old.ended_at is None and old.lease_owner=='original-worker'
                assert old.fencing_token==claim.attempt.fencing_token
            return
        if original=='valid_revoked_root':
            await revoke_session(operator.session_id)
        else:
            async with sessions() as db:
                task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==task_id))
                if original=='legacy_null':
                    task.admitted_method_json = None
                else:
                    tampered = json.loads(task.admitted_method_json)
                    tampered['input_digest']='f'*64
                    task.admitted_method_json=json.dumps(tampered)
        async with sessions() as db:
            reclaim_now = (
                datetime.now(timezone.utc).replace(tzinfo=None)
                if original == 'valid_revoked_root' else None
            )
            recovered = await service.repository.reclaim_expired_attempt(db,task_id,attempt_id,
                expected_revision=revision,lease_owner='replacement-worker',now=reclaim_now)
            assert recovered is not None
            assert recovered.attempt.attempt_id!=attempt_id and recovered.attempt.workflow_run_id is None
            assert recovered.attempt.lease_expires_at.tzinfo is not None
            witness = verify_historical_method(recovered.attempt.admitted_method_json,
                task=recovered.task,attempt=recovered.attempt)
            if original=='valid_revoked_root':
                assert witness is not None
                assert witness.admitted_at==original_witness.admitted_at
                assert witness.admission_task_revision==original_witness.admission_task_revision
                assert witness.input_digest==original_witness.input_digest
                assert recovered.task.admitted_method_json==original_raw
            else:
                assert recovered.attempt.admitted_method_json is None and witness is None
            old = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id==attempt_id))
            assert old.outcome=='lease_expired' and old.ended_at is not None
    finally:
        service.stop()
        registry.stop()
        await current.stop()
        await historical_method_service.stop()
        home_projection.stop()
        await client.aclose()
