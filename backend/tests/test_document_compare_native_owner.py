"""Current-authority denials at the actual ready supervisor delivery boundary."""
from datetime import timedelta
import json

import pytest

from tests.test_inference_accounting import accounting_db
from tests.test_document_pairs import test_authenticated_private_pair_reserve_stream_seal_and_exact_bind as journey


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["lease", "fence", "capacity", "child", "reap", "task",
    "attempt", "session", "goal", "cancel", "deadline", "source"])
async def test_ready_comparison_denies_drift_before_private_delivery(accounting_db,monkeypatch,drift):
    from sqlalchemy import inspect, select
    from src.db.models import WorkflowRunState, WorkBoardTask, WorkBoardAttempt, OperatorSession, Goal, WorkBoardInputArtifact
    from src.work_board import document_compare_native as native
    from src.work_board.repository import BoardError
    original=native.record_child
    denials=[]
    async def checked(jobs,task,attempt,inputs,spec,capacity,process,packet,runner,fence):
        sessions=accounting_db[2].accounting_sessions
        async with sessions() as db:
            if drift in {"task","goal"}:
                model=WorkBoardTask if drift=="task" else Goal
                predicate=WorkBoardTask.task_id==task.task_id if drift=="task" else Goal.id==task.goal_id
                row=await db.scalar(select(model).where(predicate))
                field="task_revision" if drift=="task" else "revision"
                replacement=getattr(row,field)+1
            elif drift in {"attempt","cancel"}:
                row=await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id==attempt.attempt_id))
                field="fencing_token" if drift=="attempt" else "cancel_requested_at"
                replacement=row.fencing_token+1 if drift=="attempt" else native.now()
            elif drift=="session":
                row=await db.get(OperatorSession,task.owner_session_id)
                field="revoked_at";replacement=native.now()
            elif drift=="source":
                row=await db.get(WorkBoardInputArtifact,task.input_artifact_id)
                field="metadata_digest";replacement="0"*64
            else:
                row=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==spec.identity.job_id))
                field={"lease":"lease_expires_at","fence":"fencing_token","deadline":"deadline_at"}.get(drift,"checkpoint_receipts_json")
                if drift in {"lease","deadline"}:replacement=native.now()-timedelta(seconds=1)
                elif drift=="fence":replacement=row.fencing_token+1
                else:
                    history=json.loads(row.checkpoint_receipts_json)
                    if drift=="capacity":history[-1]["payload"]["nonce"]="0"*32
                    else:history.append({"checkpoint_id":"document-reaped" if drift=="reap" else "document-child","payload":{},"safe":True})
                    replacement=json.dumps(history)
            identity=inspect(row).identity
            previous=getattr(row,field);setattr(row,field,replacement)
            await db.commit()
        before=await jobs.get_job(spec.identity.job_id)
        delivered=[]
        original_write=process.stdin.write
        def observed_write(data):
            delivered.append(len(data))
            return original_write(data)
        process.stdin.write=observed_write
        try:
            with pytest.raises(BoardError) as refused:
                await original(jobs,task,attempt,inputs,spec,capacity,process,packet,runner,fence)
        finally:
            process.stdin.write=original_write
        assert delivered==[]
        denials.append(refused.value.code)
        after=await jobs.get_job(spec.identity.job_id)
        assert after==before
        # Real ready processes remain alive waiting on their empty input pipe;
        # a denied publication did not deliver private bytes or mint a reap.
        assert process.returncode is None
        assert not (native.directory_path(task.input_artifact_id)/(capacity["nonce"]+".witness.json")).exists()
        async with sessions() as db:
            fresh=await db.get(type(row),identity)
            setattr(fresh,field,previous)
            await db.commit()
        return await original(jobs,task,attempt,inputs,spec,capacity,process,packet,runner,fence)
    monkeypatch.setattr(native,"record_child",checked)
    # Complete the authenticated API, actual parser and physical report/CSV
    # readback after restoring the exact original authority; no replacement job.
    await journey(accounting_db,monkeypatch,"native")
    assert len(denials)==1
