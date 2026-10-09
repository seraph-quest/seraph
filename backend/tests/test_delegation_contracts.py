"""Original bounded delegation and actual private Work evidence; no provider."""
import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from src.work_board.contracts import TaskLimits, WorkBoardOwner, GeneralTaskEnvelope
from src.work_board.repository import BoardError
from src.workflows.delegation_contracts import (
    DelegateRequest, DelegateLimits, ChildResult, verify_delegate_request,
    verify_child_result, resolve_delegation_evidence, validate_delegation_instruction,
    recheck_delegation_evidence,
)
from tests.test_general_task_persistence import task_runtime
from tests.general_task_method_lifecycle import native_admission_lifecycle
from tests.test_work_board_m6_provider_free_journey import isolated_runtime, OWNER, SESSION, _goal


def request(**changes):
    return DelegateRequest.model_validate({"parent_task_id": "parent-original", "step_id": "child-step",
        "role": "files", "instruction": "Read explicitly selected evidence",
        "evidence_refs": ["board-output:producer-original"], "allowed_tool_ids": ["read_file"],
        "limits": {"max_steps": 2, "max_inference_calls": 0, "wall_seconds": 10,
            "max_cost_microusd": 0}, **changes})


def verify(value=None, **changes):
    return verify_delegate_request(value or request(), **{
        "parent_task_id": "parent-original", "parent_limits": TaskLimits(),
        "parent_allowed_tool_ids": ["read_file"],
        "parent_evidence_refs": ["board-output:producer-original"], **changes})


def child(index, status="succeeded"):
    return {"status": status, "request": request(step_id=f"child-{index}").model_dump(mode="json")}


def test_closed_exact_wire_contract_and_nonrecursive_limits():
    original = request()
    assert DelegateRequest.model_validate_json(original.model_dump_json()) == original
    assert verify(original) == original
    for changes in ({"role": "vault"}, {"success": True}, {"instruction": "😀" * 3000},
                    {"allowed_tool_ids": ["delegate_task"]}, {"allowed_tool_ids": ["read_file", "read_file"]},
                    {"instruction": "password=credential-canary"}):
        with pytest.raises(ValidationError):
            request(**changes)
    for limits in ({"depth": 0}, {"depth": True}, {"max_outstanding_children": 1},
                   {"max_outstanding_children": False}):
        with pytest.raises(ValidationError):
            DelegateLimits.model_validate(limits)


@pytest.mark.parametrize("change,code", [
    ({"parent_task_id": "foreign-parent"}, "delegation_depth_denied"),
    ({"parent_is_child": True}, "delegation_depth_denied"),
    ({"parent_allowed_tool_ids": []}, "delegation_tool_scope_denied"),
    ({"parent_evidence_refs": []}, "delegation_evidence_scope_denied"),
    ({"existing_children": [child(0, "running"), child(1, "unknown_external_effect")]}, "delegation_simultaneous_limit"),
    ({"existing_children": [child(i) for i in range(4)]}, "delegation_total_limit"),
    ({"parent_limits": TaskLimits(max_steps=3), "existing_children": [child(0)]}, "delegation_budget_denied"),
    ({"parent_limits": TaskLimits(wall_seconds=1)}, "delegation_deadline_denied"),
    ({"parent_limits": TaskLimits(max_outstanding_children=0)}, "delegation_simultaneous_limit"),
])
def test_original_scope_limits_and_unknown_children_hold_slots(change, code):
    with pytest.raises(BoardError) as error:
        verify(**change)
    assert error.value.code == code


def test_finished_siblings_do_not_refund_consumed_allocation_or_allow_duplicate():
    assert verify(existing_children=[child(0), child(1)]) == request()
    with pytest.raises(BoardError) as error:
        verify(existing_children=[{"status": "succeeded", "request": request().model_dump(mode="json")}])
    assert error.value.code == "delegation_child_already_retained"
    original = request()
    original.allowed_tool_ids.append("delegate_task")
    with pytest.raises(ValidationError):
        verify(original)


def test_child_prose_cannot_adopt_parent_success_or_unread_artifact():
    result = ChildResult(child_id="child-original", artifact_refs=["artifact-original"],
        unresolved=["Sibling remains blocked"], summary_ref="artifact-original")
    assert verify_child_result(result, child_id="child-original",
        verified_artifact_refs=["artifact-original"]) == result
    with pytest.raises(BoardError):
        verify_child_result(result, child_id="foreign-child", verified_artifact_refs=result.artifact_refs)
    with pytest.raises(BoardError):
        verify_child_result(result, child_id="child-original", verified_artifact_refs=[])
    with pytest.raises(ValidationError):
        ChildResult.model_validate({**result.model_dump(), "parent_success": True})
    with pytest.raises(ValidationError):
        ChildResult(child_id="child-original", summary_ref="unverified-prose")


async def evidence_parent(task_runtime, *, text="hello"):
    """Real #998 callback, durable artifact/readback and operator review."""
    from src.work_board.general_task import GeneralTaskService
    from src.work_board.dispatcher import WorkBoardDispatcher, _parse_typed_input
    from src.work_board.review import complete_review
    from src.db.models import WorkBoardAttempt
    from tests.test_general_task_contract import Registry, request as task_request
    sessions, workspace = task_runtime
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    registry = Registry()
    service = GeneralTaskService(registry)
    service.start()
    async with sessions() as db:
        db.add(_goal("goal-1", "Original evidence scope"))
        db.add(_goal("producer-goal", "Original producer scope"))
    async with sessions() as db:
        proposed = task_request(registry)
        producer_request = proposed.model_copy(update={"accept": True,
            "input": proposed.input.model_copy(update={"goal_ref": "producer-goal"}),
            "plan": proposed.plan.model_copy(update={"steps": [proposed.plan.steps[0].model_copy(
                update={"input": {"text": text}})]})})
        producer = (await service.create(db, owner, producer_request)).task
        producer_id = producer.task_id
    dispatcher = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
    actual = await dispatcher.run_pass()
    assert actual["completed"] == 1, actual
    async with sessions() as db:
        producer = await service.repository.get_task(db, owner, producer_id)
        attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == producer_id))
        await complete_review(db, owner, producer_id, expected_revision=producer.task_revision,
            attempt_id=attempt.attempt_id, repository=service.repository)
    parent_request = task_request(registry).model_copy(update={"idempotency_key": "delegating-parent",
        "input": task_request(registry).input.model_copy(update={"evidence_refs": ["board-output:" + producer_id]})})
    async with sessions() as db:
        parent = (await service.create(db, owner, parent_request)).task
        envelope = GeneralTaskEnvelope.model_validate(_parse_typed_input(parent))
    assert len(registry.calls) == 1
    return sessions, workspace, owner, service, envelope, registry


@pytest.mark.asyncio
async def test_selected_evidence_is_real_readback_and_empty_selection_copies_nothing(task_runtime, native_admission_lifecycle):
    sessions, _workspace, owner, service, envelope, registry = await evidence_parent(task_runtime)
    refs = envelope.task_input.evidence_refs
    async with sessions() as db:
        copied = await resolve_delegation_evidence(service, db, owner, envelope, refs)
        assert len(copied) == 1
        assert json.loads(copied[0].content)["output"] == {"text": "hello"}
        assert copied[0].content_sha256 == envelope.evidence[0]["content_sha256"]
        assert len(await resolve_delegation_evidence(service, db, owner, envelope, [])) == 0
    assert len(registry.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["foreign_owner", "root_revoked", "goal_revoked", "producer_goal_revoked", "producer_revoked", "unknown_effect", "tamper", "symlink"])
async def test_canonical_revocation_or_physical_tamper_denies_without_contact(task_runtime, failure, native_admission_lifecycle):
    from src.db.models import OperatorSession, Goal, WorkBoardTask, WorkBoardStatus, WorkBoardAttempt, WorkflowRunState
    sessions, workspace, owner, service, envelope, registry = await evidence_parent(task_runtime)
    refs = envelope.task_input.evidence_refs
    path = workspace / envelope.evidence[0]["file_path"]
    if failure == "foreign_owner":
        owner = WorkBoardOwner(principal_id="foreign-owner", session_id=SESSION)
    elif failure == "tamper":
        path.write_text("tampered bytes")
    elif failure == "symlink":
        retained = path.with_name("foreign-original.json")
        path.rename(retained)
        path.symlink_to(retained)
    else:
        async with sessions() as db:
            if failure == "root_revoked":
                (await db.get(OperatorSession, SESSION)).revoked_at = datetime.now(timezone.utc)
            elif failure == "goal_revoked":
                (await db.get(Goal, "goal-1")).revision += 1
            elif failure == "producer_goal_revoked":
                (await db.get(Goal, "producer-goal")).revision += 1
            elif failure == "unknown_effect":
                attempt = await db.scalar(select(WorkBoardAttempt).where(
                    WorkBoardAttempt.task_id == refs[0].removeprefix("board-output:")))
                run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id))
                effects = json.loads(run.effect_receipts_json)
                effects.append({"effect_id": "unresolved-original-contact", "receipt_kind": "effect",
                    "effect_type": "external_write", "status": "intent", "fencing_token": run.fencing_token})
                run.effect_receipts_json = json.dumps(effects)
            else:
                producer = await db.scalar(select(WorkBoardTask).where(
                    WorkBoardTask.task_id == refs[0].removeprefix("board-output:")))
                producer.status = WorkBoardStatus.blocked
    async with sessions() as db:
        with pytest.raises(BoardError):
            await resolve_delegation_evidence(service, db, owner, envelope, refs)
    assert len(registry.calls) == 1


@pytest.mark.asyncio
async def test_known_vault_secret_instruction_denies_in_original_sqlite(task_runtime, native_admission_lifecycle):
    from src.vault.repository import vault_repository
    sessions, _workspace = task_runtime
    await vault_repository.store("handoff-secret", "credential-canary-original")
    async with sessions() as db:
        with pytest.raises(BoardError) as error:
            await validate_delegation_instruction(db, request(instruction="Copy credential-canary-original"))
        assert error.value.code == "delegation_credentials_denied"


@pytest.mark.asyncio
async def test_one_byte_known_credential_instruction_and_physical_handoff_are_denied(task_runtime, native_admission_lifecycle):
    from src.vault.repository import vault_repository
    from src.vault.redaction import redact_secrets_in_text_readonly
    await vault_repository.store("single-byte-secret", "Z")
    sessions, _workspace, owner, service, envelope, registry = await evidence_parent(task_runtime, text="Z")
    async with sessions() as db:
        assert await redact_secrets_in_text_readonly(db, "Z") == "Z"  # Existing default retained.
        with pytest.raises(BoardError) as instruction:
            await validate_delegation_instruction(db, request(instruction="Read Z"))
        assert instruction.value.code == "delegation_credentials_denied"
        with pytest.raises(BoardError) as evidence:
            await resolve_delegation_evidence(service, db, owner, envelope, envelope.task_input.evidence_refs)
        assert evidence.value.code == "delegation_credentials_denied"
        for invalid in (True, 0, 7, "1"):
            with pytest.raises(ValueError):
                await redact_secrets_in_text_readonly(db, "Z", minimum_secret_length=invalid)
    assert len(registry.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("text,code", [
    ("credential-canary-original", "delegation_credentials_denied"),
    ('{"conversation_id":"unrelated","messages":[]}', "delegation_private_context_denied"),
    ("secret://" + "a" * 32, "delegation_credentials_denied"),
    ("x" * 32700, "delegation_handoff_limit"),
])
async def test_actual_output_cannot_copy_credentials_conversation_or_oversized_envelope(task_runtime, text, code, native_admission_lifecycle):
    from src.vault.repository import vault_repository
    if code == "delegation_credentials_denied":
        await vault_repository.store("handoff-secret", "credential-canary-original")
    sessions, _workspace, owner, service, envelope, registry = await evidence_parent(task_runtime, text=text)
    async with sessions() as db:
        with pytest.raises(BoardError) as error:
            await resolve_delegation_evidence(service, db, owner, envelope, envelope.task_input.evidence_refs)
        assert error.value.code == code
    assert len(registry.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "copied", "mutated", "root", "goal", "run", "vault"])
async def test_staged_source_packet_metadata_recheck_has_no_io_and_fences_races(task_runtime, monkeypatch, change, native_admission_lifecycle):
    from dataclasses import replace
    from src.db.models import OperatorSession, Goal, WorkBoardAttempt, WorkflowRunState
    from src.vault.repository import vault_repository
    from src.work_board.repository import _begin_sqlite_immediate
    sessions, _workspace, owner, service, envelope, registry = await evidence_parent(task_runtime)
    async with sessions() as db:
        staged = await resolve_delegation_evidence(service, db, owner, envelope, envelope.task_input.evidence_refs)
    if change == "copied":
        staged = replace(staged)
    elif change == "mutated":
        object.__setattr__(staged[0], "content", "Foreign copied output")
    elif change == "vault":
        await vault_repository.store("new-credential-after-staging", "new-value-after-stage")
    elif change != "none":
        async with sessions() as db:
            if change == "root":
                (await db.get(OperatorSession, SESSION)).revoked_at = datetime.now(timezone.utc)
            elif change == "goal":
                (await db.get(Goal, "producer-goal")).revision += 1
            else:
                attempt = await db.scalar(select(WorkBoardAttempt).where(
                    WorkBoardAttempt.task_id == envelope.task_input.evidence_refs[0].removeprefix("board-output:")))
                run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id))
                run.revision += 1
    def no_io(*args, **kwargs):
        raise AssertionError("canonical metadata recheck attempted physical or vault decryption IO")
    monkeypatch.setattr("src.work_board.input_artifacts._safe_file_bytes", no_io)
    monkeypatch.setattr("src.vault.redaction.redact_secrets_in_text_readonly", no_io)
    async with sessions() as db:
        await _begin_sqlite_immediate(db)
        if change == "none":
            assert await recheck_delegation_evidence(db, owner, envelope, staged) is staged
        else:
            with pytest.raises(BoardError):
                await recheck_delegation_evidence(db, owner, envelope, staged)
    assert len(registry.calls) == 1
