"""Real Task source-writer coverage for historical method metadata."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy import select

from src.memory import task_methods as methods
from src.db.models import WorkBoardStatus, WorkBoardTask
from src.work_board.contracts import WorkBoardAction, WorkBoardActionRequest, WorkBoardOwner
from src.work_board.general_task import GeneralTaskService
from src.work_board.repository import BoardError
from src.work_board.historical_method import (
    historical_method_service,
    project_historical_method,
    stage_attempt_method_metadata,
    verify_historical_method,
)
from tests.test_general_task_contract import Registry, request
from tests.test_general_task_methods import read_request as active_request
from tests.test_general_task_methods import setup_method as setup_active_method
from tests.test_general_task_persistence import task_runtime
from tests.test_task_lessons import no_inference
from tests.test_task_methods import review
from tests.test_work_board_m6_provider_free_journey import OWNER, SESSION, _goal, isolated_runtime


@pytest_asyncio.fixture
async def historical_method_runtime():
    await historical_method_service.start()
    try:
        if historical_method_service.signing_key is None:
            pytest.skip("configured server signing key is unavailable in this test runtime")
        yield
    finally:
        await historical_method_service.stop()


@pytest.mark.asyncio
async def test_actual_general_task_acceptance_and_claim_carry_historical_method(
    task_runtime, historical_method_runtime
):
    sessions, _workspace = task_runtime
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    registry = Registry()
    service = GeneralTaskService(registry)
    service.start()
    try:
        async with sessions() as db:
            db.add(_goal("goal-1", "Historical method acceptance"))
        accepted = request(registry).model_copy(update={"accept": True})
        async with sessions() as db:
            mutation = await service.create(db, owner, accepted)
            task_id = mutation.task.task_id
            assert mutation.task.status is WorkBoardStatus.todo
            assert mutation.task.admitted_method_json

            task_witness = verify_historical_method(
                mutation.task.admitted_method_json,
                task=mutation.task,
                owner=owner,
            )
            assert task_witness is not None
            assert task_witness.attempt_id is None
            assert task_witness.strategy_status == "none"

            ready = await service.repository.promote_task_ready(
                db,
                task_id,
                expected_revision=mutation.task.task_revision,
                actor_principal_id="test-dispatcher",
                actor_session_id="test-dispatch-session",
            )
            assert ready is not None and ready.task.status is WorkBoardStatus.ready
            claim = await service.repository.claim_ready_task(
                db,
                task_id,
                expected_revision=ready.task.task_revision,
                lease_owner="test-claim-worker",
                actor_principal_id="test-claim-worker",
                actor_session_id="test-claim-session",
            )
            assert claim is not None
            assert claim.attempt.admitted_method_json
            attempt_witness = verify_historical_method(
                claim.attempt.admitted_method_json,
                task=claim.task,
                attempt=claim.attempt,
                owner=owner,
            )
            assert attempt_witness is not None
            assert attempt_witness.attempt_id == claim.attempt.attempt_id
            assert attempt_witness.admitted_at == task_witness.admitted_at
            assert attempt_witness.admission_task_revision == task_witness.admission_task_revision
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_actual_active_method_acceptance_publishes_current_witness(
    async_db, monkeypatch, tmp_path, no_inference, historical_method_runtime
):
    _operator, current, owner, registry, service, _dispatcher = await setup_active_method(
        async_db, monkeypatch, tmp_path
    )
    try:
        expected = await current.resolve(owner, "goal", "work.general-task.v1")
        accepted = active_request(registry, "historical-method-active-positive")
        async with async_db() as db:
            mutation = await service.create(db, owner, accepted)
            witness = verify_historical_method(
                mutation.task.admitted_method_json,
                task=mutation.task,
                owner=owner,
            )
            assert mutation.task.status is WorkBoardStatus.todo
            assert witness is not None
            assert witness.strategy_status == "active"
            assert (witness.method_id, witness.version, witness.method_digest) == (
                expected.method_id, expected.version, expected.digest
            )
    finally:
        await current.stop()
        service.stop()


@pytest.mark.asyncio
async def test_historical_method_corruption_legacy_and_key_loss_are_unknown(
    task_runtime, historical_method_runtime
):
    sessions, _workspace = task_runtime
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    registry = Registry()
    service = GeneralTaskService(registry)
    service.start()
    try:
        async with sessions() as db:
            db.add(_goal("goal-1", "Historical method negatives"))
        accepted = request(registry).model_copy(update={"accept": True})
        async with sessions() as db:
            task = (await service.create(db, owner, accepted)).task
            assert task.admitted_method_json
            ready = await service.repository.promote_task_ready(
                db,
                task.task_id,
                expected_revision=task.task_revision,
                actor_principal_id="test-dispatcher",
            )
            claim = await service.repository.claim_ready_task(
                db,
                task.task_id,
                expected_revision=ready.task.task_revision,
                lease_owner="test-claim-worker",
            )
            assert claim is not None

            tampered = json.loads(task.admitted_method_json)
            tampered["input_digest"] = "b" * 64
            assert verify_historical_method(tampered, task=task, owner=owner) is None
            unknown = project_historical_method(tampered, task=task, owner=owner)
            assert unknown["status"] == "unknown"
            assert unknown["reason_code"] == "method_projection_invalid"

            legacy_task = task.model_copy(update={"admitted_method_json": None})
            assert verify_historical_method(None, task=legacy_task, owner=owner) is None
            assert await stage_attempt_method_metadata(
                db, task=legacy_task, new_attempt=claim.attempt
            ) is None
            missing_attempt_id = claim.attempt.model_copy(update={"attempt_id": None})
            assert await stage_attempt_method_metadata(
                db, task=task, new_attempt=missing_attempt_id
            ) is None

            original_key = historical_method_service._key
            historical_method_service._key = None
            try:
                assert await stage_attempt_method_metadata(
                    db, task=task, new_attempt=claim.attempt
                ) is None
            finally:
                historical_method_service._key = original_key
    finally:
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_current_method_rollback_between_preflight_and_promote_cas_denies_without_commit(
    async_db, monkeypatch, tmp_path, no_inference, historical_method_runtime
):
    operator, current, owner, registry, service, _dispatcher = await setup_active_method(
        async_db, monkeypatch, tmp_path
    )
    try:
        triage = active_request(registry, "historical-method-promote-race").model_copy(update={"accept": False})
        async with async_db() as db:
            created = await service.create(db, owner, triage)
        async with async_db() as db:
            stage = await service.validate_acceptance(
                db, owner, created.task.task_id, created.task.task_revision
            )
        assert stage is not None and stage.strategy_status == "active"
        pinned = await current.resolve(owner, "goal", "work.general-task.v1")
        inspected = await methods.inspect_method(operator, pinned.method_id)
        await methods.review_method(operator, review(inspected, "rollback", "race-after-preflight"))
        async with async_db() as db:
            with pytest.raises(BoardError) as failure:
                await service.repository.action_task(
                    db,
                    owner,
                    created.task.task_id,
                    WorkBoardActionRequest(
                        action=WorkBoardAction.promote,
                        expected_revision=created.task.task_revision,
                    ),
                    accepted_method_stage=stage,
                )
            assert failure.value.code == "general_task_strategy_changed"
            await db.rollback()
        async with async_db() as db:
            task = await service.repository.get_task(db, owner, created.task.task_id)
            assert task.status is WorkBoardStatus.triage
            assert task.task_revision == created.task.task_revision
            assert task.admitted_method_json is None
    finally:
        await current.stop()
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_current_method_rollback_between_preflight_and_create_cas_denies_without_task(
    async_db, monkeypatch, tmp_path, no_inference, historical_method_runtime
):
    operator, current, owner, registry, service, _dispatcher = await setup_active_method(
        async_db, monkeypatch, tmp_path
    )
    original_strategy = service.strategy
    service_db = None
    stale_binding = None
    rolled_back = False

    async def stale_preflight(owner_arg, goal_ref, *, db=None):
        nonlocal service_db, stale_binding, rolled_back
        binding = stale_binding
        if db is not None:
            return await original_strategy(owner_arg, goal_ref, db=db)
        if binding is None:
            binding = await original_strategy(owner_arg, goal_ref)
            stale_binding = binding
            if not rolled_back:
                rolled_back = True
                if service_db is not None:
                    await service_db.rollback()
                pinned = await current.resolve(owner, "goal", "work.general-task.v1")
                await methods.review_method(
                    operator,
                    review(await methods.inspect_method(operator, pinned.method_id), "rollback", "race-before-create"),
                )
        return binding

    monkeypatch.setattr(service, "strategy", stale_preflight)
    try:
        request_value = active_request(registry, "historical-method-create-race")
        async with async_db() as db:
            service_db = db
            with pytest.raises(BoardError) as failure:
                await service.create(db, owner, request_value)
            assert failure.value.code == "general_task_strategy_changed"
            await db.rollback()
        async with async_db() as db:
            task = await db.scalar(
                select(WorkBoardTask).where(WorkBoardTask.idempotency_key == request_value.idempotency_key)
            )
            assert task is None
    finally:
        await current.stop()
        service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("signer_state", ["lost", "rotated"])
async def test_signer_loss_or_rotation_between_stage_and_cas_denies_without_status_commit(
    task_runtime, historical_method_runtime, signer_state
):
    sessions, _workspace = task_runtime
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    registry = Registry()
    service = GeneralTaskService(registry)
    service.start()
    original_key = historical_method_service._key
    try:
        async with sessions() as db:
            db.add(_goal("goal-1", "Signer race"))
        inert = request(registry).model_copy(update={"accept": False, "idempotency_key": "signer-race" + signer_state})
        async with sessions() as db:
            created = await service.create(db, owner, inert)
        async with sessions() as db:
            stage = await service.validate_acceptance(db, owner, created.task.task_id, created.task.task_revision)
        assert stage is not None
        if signer_state == "lost":
            historical_method_service._key = None
        else:
            historical_method_service._key = bytes(value ^ 0xFF for value in original_key)
        async with sessions() as db:
            with pytest.raises(BoardError) as failure:
                await service.repository.action_task(
                    db,
                    owner,
                    created.task.task_id,
                    WorkBoardActionRequest(
                        action=WorkBoardAction.promote,
                        expected_revision=created.task.task_revision,
                    ),
                    accepted_method_stage=stage,
                )
            assert failure.value.code in {
                "general_task_method_signer_unavailable",
                "general_task_method_signer_rotated",
            }
            await db.rollback()
        async with sessions() as db:
            task = await service.repository.get_task(db, owner, created.task.task_id)
            assert task.status is WorkBoardStatus.triage
            assert task.task_revision == created.task.task_revision
            assert task.admitted_method_json is None
    finally:
        historical_method_service._key = original_key
        service.stop()
