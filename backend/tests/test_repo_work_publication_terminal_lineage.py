"""Retained terminal C1 evidence cannot be replaced by a success label."""
import json

import pytest
from sqlalchemy import select

from src.db.models import WorkBoardTask, WorkBoardAttempt
from src.model_fabric.effective_policy import configuration_mutation_lock
from src.workflows.job_runtime import DurableJobLeaseError
from src.work_board.repository import BoardError
from src.workflows.repo_repair_source import (
    read_repository_original, stage_repository_publication_witness,
    assert_repository_publication_witness,
)
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.test_repo_work_task_publication import _actual_source_callback_journey
from tests.test_repo_work_source_publication import selected_publisher


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["missing_task_result", "missing_board_readback",
    "reopened_attempt", "corrupt_final_artifact", "missing_plan_receipt", "corrupt_plan_receipt"])
async def test_actual_source_publication_requires_complete_terminal_c1_evidence(
        accounting_db, monkeypatch, drift, repository_admission_signer):
    flow = await _actual_source_callback_journey(accounting_db, monkeypatch,
        False, "test_python", publication_profile=True)
    _publisher, _adapter, _connection, transport = await selected_publisher(flow, monkeypatch)
    source, jobs, owner = flow["service"].repository_source_service, flow["jobs"], flow["owner"]
    async with configuration_mutation_lock:
        witness = await stage_repository_publication_witness(source, jobs,
            repair_job_id=flow["root_id"], owner=owner)
    assert_repository_publication_witness(witness)
    async with flow["factory"]() as db:
        root = await jobs._fetch(db, flow["root_id"])
        binding = read_repository_original(root)[4]
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
        attempt = await db.get(WorkBoardAttempt, binding.attempt_id)
        parent = await jobs._fetch(db, binding.parent_job_id)
        if drift == "missing_task_result":
            task.result_refs_json = "[]"
        elif drift == "missing_board_readback":
            effects = json.loads(parent.effect_receipts_json)
            assert any(item["effect_type"] == "board_child_readback" for item in effects)
            parent.effect_receipts_json = json.dumps([
                item for item in effects if item["effect_type"] != "board_child_readback"])
        elif drift == "reopened_attempt":
            assert attempt.ended_at is not None
            attempt.ended_at = None
        elif drift == "corrupt_final_artifact":
            references = json.loads(task.result_refs_json)
            assert len(references) == 1
            path = flow["workspace"] / references[0]["file_path"]
            path.write_bytes(path.read_bytes() + b"\nchanged actual final artifact\n")
        else:
            from src.workflows.general_task_guard import read_manifest
            from src.work_board.general_task import digest
            manifest = read_manifest(parent)
            receipt_digest = manifest.step_receipt_digests[-1]
            key = digest([parent.run_identity, manifest.creation_digest, "StepReceipt.v1", receipt_digest])
            path = flow["workspace"] / f"artifacts/work-board/general-tasks/{key}-{receipt_digest}.json"
            assert path.is_file()
            if drift == "missing_plan_receipt":
                path.unlink()
            else:
                path.write_bytes(path.read_bytes() + b"\nchanged actual plan receipt\n")
        await db.commit()
    def forbidden_selected_source_read(*args, **kwargs):
        raise AssertionError("Incomplete terminal lineage must deny before selected Source artifact reads")
    with monkeypatch.context() as boundary:
        boundary.setattr(source, "_read_private_artifact", forbidden_selected_source_read)
        async with configuration_mutation_lock:
            with pytest.raises((DurableJobLeaseError, BoardError)):
                await stage_repository_publication_witness(source, jobs,
                    repair_job_id=flow["root_id"], owner=owner)
    assert not any(method != "GET" for method, _path, _body in transport.calls)
    assert (await jobs.get_job(flow["root_id"]))["status"] == "succeeded"
