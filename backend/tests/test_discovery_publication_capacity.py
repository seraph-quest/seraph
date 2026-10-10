"""Original jobs frame plumbing; no discovery/Source authority is fabricated."""
from contextlib import asynccontextmanager
import json

import pytest
from sqlalchemy import text

from src.db.engine import get_session as original_engine_session, override_session_factory
from src.workflows.job_runtime import get_session as original_job_session, DurableJobRepository
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError, MAX_BYTES
from tests.test_inference_accounting import accounting_db


@pytest.mark.parametrize("through_jobs", [False, True])
@pytest.mark.parametrize("exhausted", [False, True])
@pytest.mark.asyncio
async def test_original_discovery_frame_is_bound_before_session_yield(
        accounting_db, monkeypatch, through_jobs, exhausted):
    """Use the actual shared-session engine and real file DB, not a guard DTO."""
    _root, _engine, factory = accounting_db
    from src.workflows import durable_state, job_runtime
    from src.workspace import accounting_witness
    monkeypatch.setattr(durable_state, "get_session", original_engine_session)
    monkeypatch.setattr(job_runtime, "get_session", original_job_session)
    original_probe = accounting_witness.prepare_composition_read_session
    seen = []
    yielded = False
    budget = HeaderReadBudget()
    budget.debit(MAX_BYTES if exhausted else 17)
    before = budget.remaining

    async def observed_probe(db, *, header_budget=None):
        assert yielded is False
        assert header_budget is budget
        assert header_budget.remaining == before
        seen.append(header_budget)
        return await original_probe(db, header_budget=header_budget)

    monkeypatch.setattr(accounting_witness, "prepare_composition_read_session", observed_probe)
    async with factory() as db:
        original_rows = (await db.execute(text("SELECT COUNT(*) FROM workflow_run_states"))).scalar_one()
    with override_session_factory(factory):
        scope = (DurableJobRepository()._session(header_budget=budget) if through_jobs
            else original_job_session(header_budget=budget))
        if exhausted:
            with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
                async with scope:
                    yielded = True
            assert yielded is False
            assert budget.remaining == 0
        else:
            async with scope as db:
                yielded = True
                assert (await db.execute(text("SELECT 1"))).scalar_one() == 1
                assert db.info["composition_writer_owner"] == "durable_jobs"
                assert budget.remaining == before - len(json.dumps([1, None], separators=(",", ":")).encode())
    assert seen == [budget]
    assert budget.references == set() and budget.future_references == set()
    async with factory() as db:
        assert (await db.execute(text("SELECT COUNT(*) FROM workflow_run_states"))).scalar_one() == original_rows


@pytest.mark.asyncio
async def test_original_jobs_default_session_keeps_zero_argument_factory(accounting_db, monkeypatch):
    """The new optional frame does not change ordinary fixture/factory calls."""
    _root, _engine, factory = accounting_db
    from src.workflows import durable_state, job_runtime
    monkeypatch.setattr(job_runtime, "get_session", original_job_session)
    calls = []

    @asynccontextmanager
    async def zero_argument_sessions():
        calls.append("original")
        async with factory.accounting_sessions() as db:
            yield db

    monkeypatch.setattr(durable_state, "get_session", zero_argument_sessions)
    async with DurableJobRepository()._session() as db:
        assert (await db.execute(text("SELECT 1"))).scalar_one() == 1
    assert calls == ["original"]


@pytest.mark.parametrize("scenario", ["allow", "identity_revoked", "copy", "foreign_guard", "foreign_frame"])
@pytest.mark.asyncio
async def test_original_selected_current_authority_uses_only_identity3(
        genuine_live_selection, monkeypatch, scenario):
    """Real issuer/Goal/programme/host owner; selected reader never admits."""
    from dataclasses import replace
    from src.guardian.goal_programmes import goal_programme_service, GoalProgrammeError
    from src.guardian.goal_discovery import goal_discovery_service
    from src.runtime_plugins.bridge import cordis_host
    from src.runtime_plugins.ownership import begin_native_writer
    from src.memory.composition_headers import _certify_current_memory_snapshot_on_connection
    from tests.test_programme_prospective_live_selection import (
        SelectionObserved, observe_identity_body, private_outputs)
    from tests.test_auth_session_composition_privacy import sql_state
    case = genuine_live_selection
    before, outputs = sql_state(case), private_outputs(case)
    observed = []

    async def actual_boundary(**requested):
        assert requested["goal_id"] == case.goal_id
        budget = HeaderReadBudget()
        async with original_engine_session(header_budget=budget) as db:
            guard = await begin_native_writer(db, owner="finite_service", header_budget=budget)
            connection = await db.connection()
            common = await connection.run_sync(
                lambda conn: _certify_current_memory_snapshot_on_connection(conn, budget))
            programme, binding, common, identity = await connection.run_sync(lambda conn:
                guard._select_programme_admission(conn, common, service=goal_discovery_service, host=cordis_host,
                    goal_id=case.goal_id, programme_id=case.programme["id"],
                    grant_revision=case.programme["grant_revision"]))
            selected_guard, selected_identity = guard, identity
            if scenario == "copy":
                selected_identity = replace(identity)
            elif scenario == "foreign_guard":
                selected_guard = object()  # Negative data, never an issuer.
            elif scenario == "foreign_frame":
                selected_identity = replace(identity, budget=HeaderReadBudget())
            with observe_identity_body(case.engine, budget, binding.owner_identity_id,
                    forbidden=scenario in {"copy", "foreign_guard", "foreign_frame"}) as (bodies, _provenance):
                kwargs = dict(db=db, binding=binding, policy=case.policy,
                    original_admission_guard=selected_guard, identity_certificate=selected_identity)
                if scenario == "allow":
                    result = await goal_programme_service.validate_current_binding(**kwargs)
                    assert result == programme and result.goal_id == case.goal_id
                    assert bodies == ["original-identity3-body"]
                    from datetime import timezone
                    from uuid import uuid5, NAMESPACE_URL
                    from src.memory.header_bounds import _trace_memory_numeric_charges
                    from src.workspace.production import ProductionWorkspaceReconciliationError
                    day = goal_programme_service._clock().astimezone(timezone.utc).date().isoformat()
                    identifier = uuid5(NAMESPACE_URL,
                        f"seraph:public-discovery:{binding.owner_identity_id}:{programme.id}:{day}")
                    original_job_id = "goal-discovery:" + identifier.hex
                    remaining = budget.remaining
                    with _trace_memory_numeric_charges(budget) as incoming:
                        await connection.run_sync(lambda conn: guard._check_original_discovery_incoming(
                            conn, common, service=goal_discovery_service, host=cordis_host, job_id=original_job_id))
                    assert len(incoming) == 10 and all(amount == 4 for _, amount in incoming)
                    assert all(appearance[0] == "discovery-incoming" and appearance[3] == original_job_id
                        for appearance, _ in incoming)
                    assert budget.remaining == remaining - 40
                    wrong_job_id = "goal-discovery:" + ("0" if identifier.hex[0] != "0" else "1") + identifier.hex[1:]
                    with _trace_memory_numeric_charges(budget) as denied:
                        with pytest.raises(ProductionWorkspaceReconciliationError,
                                match="^programme_original_selection_unavailable$"):
                            await connection.run_sync(lambda conn: guard._check_original_discovery_incoming(
                                conn, common, service=goal_discovery_service, host=cordis_host, job_id=wrong_job_id))
                    assert denied == [] and budget.remaining == remaining - 40
                else:
                    error = HeaderBoundsError if scenario == "copy" else GoalProgrammeError
                    reason = ("header_certificate_unavailable" if scenario == "copy" else
                        "programme_identity_revoked" if scenario == "identity_revoked" else
                        "programme_original_selection_unavailable")
                    with pytest.raises(error, match="^" + reason + "$" ):
                        await goal_programme_service.validate_current_binding(**kwargs)
                    assert bodies == (["original-identity3-body"] if scenario == "identity_revoked" else [])
            assert identity.budget is common.budget is guard.header_budget is budget
            observed.append("actual-current-owner-reader")
            raise SelectionObserved("actual selected authority read; no admission")

    # Existing genuine service entry owns the task. The observer returns no
    # authority and cannot reach staging/strategy/job construction.
    monkeypatch.setattr(goal_programme_service, "assert_authority", actual_boundary)
    with pytest.raises(SelectionObserved, match="^actual selected authority read; no admission$"):
        await goal_discovery_service.admit(goal_id=case.goal_id, programme_id=case.programme["id"],
            grant_revision=case.programme["grant_revision"])
    assert observed == ["actual-current-owner-reader"]
    assert sql_state(case) == before and private_outputs(case) == outputs
    assert case.contacts == [] and goal_discovery_service._tasks == set()


# Genuine fixture is imported unchanged from the independently reviewed original
# lifecycle suite. Its full direct closure must be present in the integrated epoch.
from tests.test_programme_prospective_live_selection import genuine_live_selection
