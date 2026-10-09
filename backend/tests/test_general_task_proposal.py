"""Original allowance/deadline mechanics, without inference or credentials."""
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.work_board.contracts import TaskProposalGroupV1
from src.work_board.general_task_proposal import group_identity, new_group
from src.work_board.repository import BoardError
from tests.test_general_task_contract import Registry, request
from src.work_board.contracts import WorkBoardOwner
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime


def test_stable_group_identity_and_original_deadline_are_not_mutable_intent_authority():
    owner = WorkBoardOwner(principal_id="operator:fixture", session_id="root-original")
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    original = request()
    group = new_group(owner, original.input, Registry().entries, goal_revision=1,
        request_key="original", expires_at=now + timedelta(seconds=90), now=now)
    assert group.original_deadline_at == now + timedelta(seconds=90)
    changed = new_group(owner, original.input.model_copy(update={"intent": "different"}),
        Registry().entries, goal_revision=1, request_key="original",
        expires_at=now + timedelta(seconds=90), now=now)
    assert changed.group_id == group.group_id
    assert changed.initial_input_digest != group.initial_input_digest
    assert group_identity(owner, original.input.goal_ref, 2, "original") != group.group_id
    assert TaskProposalGroupV1.model_validate_json(group.model_dump_json()) == group


@pytest.mark.parametrize("changes", [{"max_inference_calls": True}, {"max_steps": 17},
    {"issued_at": "2026-10-08T00:00:00"}, {"original_deadline_at": "2026-10-08T00:00:00+01:00"},
    {"group_id": "A" * 64}, {"owner_principal_id": "x" * 129}])
def test_closed_group_rejects_coercion_and_unbounded_or_non_utc_identity(changes):
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    group = new_group(WorkBoardOwner(principal_id="operator:fixture", session_id="root-original"),
        request().input, Registry().entries, goal_revision=1, request_key="original",
        expires_at=now + timedelta(seconds=90), now=now)
    with pytest.raises(ValidationError):
        TaskProposalGroupV1.model_validate({**group.model_dump(), **changes})


def test_expired_root_cannot_mint_task_clock():
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    with pytest.raises(BoardError, match="Original task authority expired"):
        new_group(WorkBoardOwner(principal_id="operator:fixture", session_id="root-original"),
            request().input, Registry().entries, goal_revision=1, request_key="original",
            expires_at=now, now=now)


@pytest.mark.asyncio
async def test_charged_native_child_publication_uses_bounded_reads_and_preserves_denials(task_runtime, monkeypatch):
    import inspect
    from dataclasses import replace
    from sqlalchemy import select
    from src.db.models import InferenceCostReservation as Row, WorkBoardTask, OperatorSession
    from src.work_board.contracts import GeneralTaskEnvelope
    from src.work_board.dispatcher import _parse_typed_input
    from src.work_board.general_task_proposal import seal_proposal_publication, recheck_proposal_publication
    from src.workflows.inference_accounting import InferenceAccountingError
    from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
    from tests.test_inference_accounting import request as inference_request
    from tests.test_specialist_parent_synthesis import genuine_mcp_registry, charged_parent

    async with genuine_mcp_registry(task_runtime[1], monkeypatch) as (registry, descriptor, received, protocol):
        sessions, workspace, owner, service, dispatcher, planner, transport, creation, task_id = await charged_parent(
            task_runtime, monkeypatch, registry, descriptor)
        # Genuine unrelated ledger history uses the canonical broker and final
        # scripted transport, never raw inserts or rewritten financial witnesses.
        history_contacts = []
        async def zero_cost_transport():
            history_contacts.append(True)
            return {"id": "gen-history", "usage": {"cost": "0"}}
        for ordinal in range(8):
            history_request = inference_request("publication-history-" + str(ordinal),
                owner=owner.principal_id if ordinal < 4 else "service:unrelated-history")
            if ordinal < 4:
                history_request = replace(history_request, session_id=owner.session_id)
            await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(history_request, zero_cost_transport)
        assert len(history_contacts) == 8
        async with sessions() as db:
            adapter = type(db)
        execute = adapter.execute
        observed = []
        async def bounded_publication(db, statement, *args, **kwargs):
            if any(frame.function == "recheck_proposal_publication" for frame in inspect.stack()):
                sql = str(statement)
                if sql.startswith("SELECT") and "inference_cost_reservations" in sql:
                    assert statement._limit_clause is not None, sql
                    predicates = str(statement._where_criteria)
                    assert "group_lookup_key" in " ".join(map(str, statement._where_criteria)), sql
                    assert "owner_id" in sql, predicates
                    observed.append((sql, statement._limit_clause.value))
            return await execute(db, statement, *args, **kwargs)
        monkeypatch.setattr(adapter, "execute", bounded_publication)
        await dispatcher.run_pass()
        async with sessions() as db:
            children = list((await db.execute(select(WorkBoardTask).where(
                WorkBoardTask.idempotency_key.like("specialist:%")))).scalars())
            assert len(children) == 1
            child = GeneralTaskEnvelope.model_validate(_parse_typed_input(children[0]))
            parent = await service.repository.get_task(db, owner, task_id)
            envelope = GeneralTaskEnvelope.model_validate(_parse_typed_input(parent))
            assert child.proposal_group == envelope.proposal_group
            witness = await seal_proposal_publication(db, owner, envelope, goal_revision=1)
            costs = list((await db.execute(select(Row).order_by(Row.operation_id))).scalars())
            assert len(costs) >= 10
            history = next(row for row in costs if row.operation_id == "publication-history-0")
            initial = next(row for row in costs if row.operation_id == envelope.proposal_provenance.initial_operation_id)
            financial = [(row.operation_id, row.model_dump(mode="json"), row.group_lookup_key) for row in costs]
            original_contacts = len(transport["contacts"])
            # Mutations are negative source-corruption fixtures in savepoints;
            # every denial rolls them back and preserves actual financial bytes.
            class RollbackFixture(Exception):
                pass
            for key in (None, "invalid"):
                with pytest.raises(RollbackFixture):
                    async with db.begin_nested():
                        history.group_lookup_key = key
                        await db.flush()
                        with pytest.raises(BoardError) as rejected:
                            await recheck_proposal_publication(db, owner, witness)
                        assert rejected.value.status_code == 409 and rejected.value.code == "general_task_provenance_changed"
                        raise RollbackFixture()
            manual = envelope.model_copy(update={"proposal_provenance": None})
            with pytest.raises(BoardError) as rejected:
                await seal_proposal_publication(db, owner, manual, goal_revision=1)
            assert rejected.value.code == "general_task_provenance_missing"
            with pytest.raises(RollbackFixture):
                async with db.begin_nested():
                    initial.state = "unknown"
                    await db.flush()
                    with pytest.raises(BoardError) as rejected:
                        await recheck_proposal_publication(db, owner, witness)
                    assert rejected.value.code == "general_task_group_unknown"
                    raise RollbackFixture()
            with pytest.raises(RollbackFixture):
                async with db.begin_nested():
                    root = await db.get(OperatorSession, owner.session_id)
                    root.revoked_at = datetime.now(timezone.utc)
                    await db.flush()
                    with pytest.raises(InferenceAccountingError, match="authority_invalid"):
                        await recheck_proposal_publication(db, owner, witness)
                    raise RollbackFixture()
            with pytest.raises(RollbackFixture):
                async with db.begin_nested():
                    from tests.test_inference_group_lookup import reservation
                    for ordinal in range(3, 14):
                        extra = reservation(envelope.proposal_group, ordinal)
                        extra.group_lookup_key = "g:" + envelope.proposal_group.group_id
                        db.add(extra)
                    await db.flush()
                    with pytest.raises(BoardError) as rejected:
                        await recheck_proposal_publication(db, owner, witness)
                    assert rejected.value.code == "general_task_provenance_changed"
                    raise RollbackFixture()
            after = list((await db.execute(select(Row).order_by(Row.operation_id))).scalars())
            assert [(row.operation_id, row.model_dump(mode="json"), row.group_lookup_key) for row in after] == financial
            assert len(transport["contacts"]) == original_contacts
            assert await recheck_proposal_publication(db, owner, witness) == envelope
        assert any(limit == 13 for _, limit in observed)
        assert all(limit in (1, 13) for _, limit in observed)
