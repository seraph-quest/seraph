"""Authentic reviewed producer and sealed copy: fail closed on later mutation."""
import json

import pytest
from sqlalchemy import select

from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime
from tests.test_specialist_evidence_runtime import copied_evidence_fixture, reserve_evidence_specialist


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["copied_tamper", "copied_delete", "producer_stale", "vault_changed"])
async def test_original_sealed_handoff_mutation_denies_before_contact(task_runtime, monkeypatch, mutation):
    from src.db.models import WorkBoardTask
    from src.work_board.repository import BoardError
    from src.workflows.specialist_delegation import execute_specialist
    from src.work_board import input_artifacts

    fixture = await copied_evidence_fixture(task_runtime, monkeypatch)
    sessions, workspace, owner, dispatcher, service, envelope, original, reference, plan, planner, transport = fixture
    context, principal = await reserve_evidence_specialist(fixture)
    artifact = next(item for item in json.loads(context.callback.artifact_receipts_json)
        if item["artifact_id"] == context.reservation.handoff_ref.artifact_id)
    copied_path = workspace / artifact["file_path"]
    assert copied_path.is_file()
    assert not transport["contacts"]
    original_source_paths = {str(workspace / item["file_path"]) for item in context.envelope.evidence}
    rereads = []
    original_read = input_artifacts._safe_file_bytes
    def observe_source_read(path, **kwargs):
        if str(path) in original_source_paths:
            rereads.append(str(path))
        return original_read(path, **kwargs)
    monkeypatch.setattr(input_artifacts, "_safe_file_bytes", observe_source_read)
    if mutation == "copied_tamper":
        content = copied_path.read_bytes()
        copied_path.write_bytes(content.replace(b"explicit copied bytes", b"tampered copied bytes"))
        assert copied_path.read_bytes() != content
    elif mutation == "copied_delete":
        copied_path.unlink()
    elif mutation == "producer_stale":
        async with sessions() as db:
            producer = await db.scalar(select(WorkBoardTask).where(
                WorkBoardTask.task_id == reference.removeprefix("board-output:")))
            producer.task_revision += 1
            db.add(producer)
    else:
        from src.vault import repository, crypto
        monkeypatch.setattr(repository, "get_session", sessions)
        monkeypatch.setattr(crypto, "_fernet", None)
        await repository.VaultRepository().store("new-selected-vault-row", "new-private-test-value",
            owner_principal_id=owner.principal_id)
    denied = None
    try:
        await execute_specialist(dispatcher.jobs, service=service,
            invocation_id=context.callback.run_identity,
            fencing_token=context.callback.fencing_token, principal=principal)
    except BoardError as exc:
        denied = exc
    assert not transport["contacts"], "Changed sealed evidence reached original inference contact"
    assert denied is not None, "Changed sealed evidence was accepted"
    assert not rereads, "Specialist reread the original source filesystem"
    assert not (workspace / "copied-result.txt").exists()
    async with sessions() as db:
        children = (await db.execute(select(WorkBoardTask).where(
            WorkBoardTask.idempotency_key.like("specialist:%")))).scalars().all()
        assert not children, "Changed handoff published a child/output owner"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["secret", "raw_path", "depth", "child_limit", "fake_handoff"])
async def test_public_delegate_input_cannot_expand_original_scope(task_runtime, monkeypatch, mutation):
    from pydantic import ValidationError
    from src.work_board.repository import BoardError
    from src.auth.service import authenticate_session
    def mutate(literal):
        if mutation == "secret":
            literal["instruction"] = "Copy Bearer never-authorized-secret-token"
        elif mutation == "raw_path":
            literal["evidence_refs"] = ["selected.txt"]
        elif mutation == "depth":
            literal["limits"]["depth"] = 2
        elif mutation == "child_limit":
            literal["limits"]["max_outstanding_children"] = 1
        else:
            literal["specialist_handoff"] = {"schema_version": "SpecialistEvidenceHandoff.v1"}
    fixture = None
    denied = None
    try:
        fixture = await copied_evidence_fixture(task_runtime, monkeypatch, delegate_input_mutator=mutate)
        sessions, workspace, owner, dispatcher, service, envelope, original, reference, plan, planner, transport = fixture
        operator = await authenticate_session(owner.session_id, touch=False)
        result = await service.execute(dispatcher.jobs, job_id=original["job"]["job_id"],
            owner=original["job"]["lease"]["owner"], fence=original["job"]["lease"]["fencing_token"],
            envelope=envelope, principal=operator.principal)
        assert not result["verified"]
        denied = result
    except (BoardError, ValidationError) as exc:
        denied = exc
    assert denied is not None
    assert not (task_runtime[1] / "copied-result.txt").exists()
    if fixture is not None:
        assert not fixture[-1]["contacts"]


@pytest.mark.asyncio
async def test_original_planner_unrelated_pointer_denied_before_tool_write(task_runtime, monkeypatch):
    from src.work_board.repository import BoardError
    from src.workflows.specialist_delegation import execute_specialist
    fixture = await copied_evidence_fixture(task_runtime, monkeypatch)
    sessions, workspace, owner, dispatcher, service, envelope, original, reference, plan, planner, transport = fixture
    context, principal = await reserve_evidence_specialist(fixture)
    payload = plan.model_dump(mode="json")
    payload["steps"][0]["input"]["content"]["from_evidence"] = "board-output:unrelated-producer"
    transport["content"] = json.dumps(payload)
    denied = None
    try:
        await execute_specialist(dispatcher.jobs, service=service,
            invocation_id=context.callback.run_identity,
            fencing_token=context.callback.fencing_token, principal=principal)
    except (BoardError, ValueError) as exc:
        denied = exc
    assert denied is not None
    assert len(transport["contacts"]) == 1, "Original planner contact precedes its returned invalid pointer"
    assert not (workspace / "copied-result.txt").exists()
    async with sessions() as db:
        from src.db.models import WorkBoardTask
        children = (await db.execute(select(WorkBoardTask).where(
            WorkBoardTask.idempotency_key.like("specialist:%")))).scalars().all()
        assert not children
