"""Original stopped v2 publication/recovery mechanics; no execution Source."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json

import pytest
import pytest_asyncio

from tests.test_runtime_composition_ownership import composition_db
from src.db.engine import get_session as canonical_session
from src.db.models import AuditEvent, WorkflowRunState
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
from src.runtime_plugins.ownership import CompositionDependency, begin_native_writer, transition_owner
from src.workflows.job_runtime import DurableJobRepository
from src.workspace import production
from src.workspace.accounting_continuity import transition_programme_envelope, reconcile_programme_envelope_transition
from src.workspace.accounting_witness import _programme_envelope, _programme_transition, prepare_composition_session
from src.workspace.production import maintenance_fence, read_lifecycle_receipt, read_accounting_checkpoint
from src.work_board.research_parent import DISCOVERY_KIND


@pytest_asyncio.fixture
async def stopped_v1(composition_db):
    # Existing owner creates the accounting row and external witness. No job,
    # accepted programme, claim, HTTP contact or execution authority is minted.
    await DurableJobRepository().configure_inference_accounting(1000)
    root, engine, factory, workspace = composition_db
    assert read_lifecycle_receipt(workspace)["runtime_composition"]["schema_version"] == 1
    return root, engine, factory, workspace


def files(workspace):
    return {name: (workspace.lifecycle_directory / name).read_bytes()
        for name in ("receipt.json", "accounting-checkpoint.json")}


def transition(workspace):
    budget = HeaderReadBudget()
    with maintenance_fence(workspace):
        result = transition_programme_envelope(workspace=workspace, budget=budget)
    assert budget.remaining < HeaderReadBudget().remaining
    return result


@pytest.mark.asyncio
async def test_stopped_empty_transition_has_real_prior_and_checkpoint_first(stopped_v1, monkeypatch):
    root, _engine, _factory, workspace = stopped_v1
    prior = read_lifecycle_receipt(workspace)
    database = (root / "seraph.db").read_bytes()
    original = production._write_lifecycle_receipt_locked
    observations = []
    def observe_receipt(actual_workspace, payload, **kwargs):
        checkpoint = read_accounting_checkpoint(actual_workspace)
        retained = read_lifecycle_receipt(actual_workspace)
        observations.append((checkpoint, retained))
        assert checkpoint["composition_base"] == prior["runtime_composition"]
        assert checkpoint["composition_target"] == payload["runtime_composition"]
        assert retained == prior
        return original(actual_workspace, payload, **kwargs)
    monkeypatch.setattr(production, "_write_lifecycle_receipt_locked", observe_receipt)
    result = transition(workspace)
    assert len(observations) == 1
    target = result["runtime_composition"]
    assert target["schema_version"] == 2 and target["programme"] is None
    assert target["native"] == prior["runtime_composition"]
    assert target["transition_ref"]
    checkpoint = read_accounting_checkpoint(workspace)
    receipt = read_lifecycle_receipt(workspace)
    assert checkpoint["composition_target"] == receipt["runtime_composition"] == target
    assert checkpoint["composition_delta"] == []
    assert checkpoint["composition_programme_transition"] == receipt["composition_programme_transition"]
    assert receipt["inference_accounting"] == prior["inference_accounting"]
    assert (root / "seraph.db").read_bytes() == database


@pytest.mark.asyncio
async def test_ordinary_lifecycle_publication_cannot_upgrade_v1(stopped_v1):
    _root, _engine, _factory, workspace = stopped_v1
    prior = read_lifecycle_receipt(workspace)
    candidate = _programme_envelope(prior["runtime_composition"], None, None)
    _payload, reference = _programme_transition(prior["runtime_composition"], candidate)
    candidate["transition_ref"] = reference
    before = files(workspace)
    with pytest.raises(production.ProductionWorkspaceReconciliationError, match="composition_explicit_transition_required"):
        production.write_lifecycle_receipt(workspace, {**prior, "runtime_composition": candidate})
    assert files(workspace) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["running", "leased", "selected"])
async def test_actual_untrusted_run_presence_stops_transition_before_publication(stopped_v1, state):
    root, _engine, factory, workspace = stopped_v1
    # Adversarial durable row, not admission: even an untrusted composed
    # discovery address must stop this empty transition before authority derivation.
    async with factory() as db:
        db.add(WorkflowRunState(id="untrusted-row", run_identity="untrusted-run",
            root_run_identity="untrusted-run", workflow_name="mechanics", tool_name="mechanics",
            job_kind=DISCOVERY_KIND if state == "selected" else "workflow", owner_kind="service",
            status="running" if state == "running" else "blocked",
            lease_owner="untrusted-lease" if state == "leased" else None,
            composition_binding_json="{}" if state == "selected" else None))
        await db.commit()
    before = files(workspace)
    database = (root / "seraph.db").read_bytes()
    reason = "composition_transition_requires_empty_programmes" if state == "selected" else "composition_transition_requires_stopped_owner"
    with maintenance_fence(workspace), pytest.raises(production.ProductionWorkspaceReconciliationError, match=reason):
        transition_programme_envelope(workspace=workspace, budget=HeaderReadBudget())
    assert files(workspace) == before
    assert (root / "seraph.db").read_bytes() == database


def interrupt_transition(workspace, monkeypatch):
    prior = read_lifecycle_receipt(workspace)
    def interrupt(*args, **kwargs):
        raise OSError("receipt publication interrupted")
    with monkeypatch.context() as scoped:
        scoped.setattr(production, "_write_lifecycle_receipt_locked", interrupt)
        with maintenance_fence(workspace), pytest.raises(OSError, match="publication interrupted"):
            transition_programme_envelope(workspace=workspace, budget=HeaderReadBudget())
    checkpoint = read_accounting_checkpoint(workspace)
    assert read_lifecycle_receipt(workspace) == prior
    assert checkpoint["composition_base"] == prior["runtime_composition"]
    assert checkpoint["composition_target"]["schema_version"] == 2
    return checkpoint


@pytest.mark.asyncio
async def test_exact_pending_transition_recovery_rereads_and_preserves_checkpoint(stopped_v1, monkeypatch):
    _root, _engine, _factory, workspace = stopped_v1
    pending = interrupt_transition(workspace, monkeypatch)
    pending_bytes = files(workspace)["accounting-checkpoint.json"]
    before = files(workspace)
    async with canonical_session() as db:
        with pytest.raises(production.ProductionWorkspaceReconciliationError,
                match="composition_pending_checkpoint_requires_reconciliation"):
            await prepare_composition_session(db, header_budget=HeaderReadBudget())
    assert files(workspace) == before
    with maintenance_fence(workspace), pytest.raises(production.ProductionWorkspaceReconciliationError,
            match="composition_pending_checkpoint_requires_reconciliation"):
        transition_programme_envelope(workspace=workspace, budget=HeaderReadBudget())
    with maintenance_fence(workspace):
        result = reconcile_programme_envelope_transition(workspace=workspace, budget=HeaderReadBudget())
    assert result["runtime_composition"] == pending["composition_target"]
    assert read_lifecycle_receipt(workspace)["runtime_composition"] == pending["composition_target"]
    assert files(workspace)["accounting-checkpoint.json"] == pending_bytes
    # Recovery is explicit and bound to an interrupted v1 receipt, never an
    # idempotent permission to fabricate a new transition after v2 publication.
    with maintenance_fence(workspace), pytest.raises(production.ProductionWorkspaceReconciliationError,
            match="composition_explicit_transition_requires_v1"):
        reconcile_programme_envelope_transition(workspace=workspace, budget=HeaderReadBudget())


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["base", "target", "transition", "delta"])
async def test_pending_recovery_rejects_exact_binding_change(stopped_v1, monkeypatch, damage):
    _root, _engine, _factory, workspace = stopped_v1
    pending = deepcopy(interrupt_transition(workspace, monkeypatch))
    if damage == "base":
        pending["composition_base"]["closure_digest"] = "f" * 64
    elif damage == "target":
        pending["composition_target"]["transition_ref"] = "f" * 64
    elif damage == "transition":
        pending["composition_programme_transition"]["previous_witness_digest"] = "f" * 64
    else:
        pending["composition_delta"] = [{"table_id": "goals", "key": "foreign"}]
    # Simulate interrupted-file tampering with the original safe writer, not
    # an owner grant; recovery must independently re-read and reject it.
    production._write_private_checkpoint(workspace.lifecycle_directory / "accounting-checkpoint.json", pending)
    before = files(workspace)
    with maintenance_fence(workspace), pytest.raises(production.ProductionWorkspaceReconciliationError,
            match="composition_transition_recovery_binding_changed"):
        reconcile_programme_envelope_transition(workspace=workspace, budget=HeaderReadBudget())
    assert files(workspace) == before


@pytest.mark.asyncio
async def test_v2_original_local_first_writer_rechecks_and_publishes_real_owner(stopped_v1):
    _root, _engine, _factory, workspace = stopped_v1
    base = transition(workspace)["runtime_composition"]
    budget = HeaderReadBudget()
    with maintenance_fence(workspace):
        async with canonical_session() as db:
            guard = await begin_native_writer(db, owner="composition_maintenance", header_budget=budget)
            connection = await db.connection()
            await connection.run_sync(guard._ensure_programme_writer)
            captured = guard._programme_writer
            assert captured[0] is connection.sync_connection
            assert captured[1] is connection.sync_connection.get_transaction()
            assert captured[2] is connection.sync_connection.connection.driver_connection
            assert guard.header_budget is budget and guard._selection.owner is guard
            prior = next(item for item in base["native"]["inventory"] if item["runtime_domain"] == "seraph.tasks.v1")
            expected = CompositionDependency(*(prior[key] for key in ("runtime_domain", "owner_kind", "epoch", "composition_digest")))
            target = CompositionDependency(expected.runtime_domain, expected.owner_kind, expected.epoch + 1, expected.composition_digest)
            event = AuditEvent(event_type="runtime_composition_recovery", actor="managed_maintenance",
                details_json=json.dumps({"schema_version": 1, "runtime_domain": expected.runtime_domain,
                    "prior": expected.payload(), "target": target.payload(), "state": "blocked",
                    "prior_recovery_receipt_ref": prior["recovery_receipt_ref"], "phase": "awaiting_boot"}))
            db.add(event)
            await db.flush()
            await transition_owner(db, runtime_domain=expected.runtime_domain, expected=expected,
                owner_kind=target.owner_kind, epoch=target.epoch, composition_digest=target.composition_digest,
                state="blocked", recovery_receipt_ref=event.id)
            assert guard._programme_writer == captured
    receipt, checkpoint = read_lifecycle_receipt(workspace), read_accounting_checkpoint(workspace)
    final = receipt["runtime_composition"]
    assert final["schema_version"] == 2 and final["programme"] is None
    assert final["transition_ref"] == base["transition_ref"]
    assert checkpoint["composition_base"] == base and checkpoint["composition_target"] == final
    assert any(item["table_id"] == "runtime_composition_states" for item in checkpoint["composition_delta"])
    assert budget.remaining < HeaderReadBudget().remaining


@pytest.mark.asyncio
async def test_v2_guard_requires_original_frame_task_and_local_connection(stopped_v1):
    _root, engine, _factory, workspace = stopped_v1
    transition(workspace)
    async with canonical_session() as db:
        with pytest.raises(production.ProductionWorkspaceReconciliationError, match="programme_original_budget_required"):
            await prepare_composition_session(db)
    budget = HeaderReadBudget()
    async with canonical_session() as db:
        guard = await begin_native_writer(db, owner="composition_maintenance", header_budget=budget)
        connection = await db.connection()
        await connection.run_sync(guard._ensure_programme_writer)
        selection = guard._selection
        await connection.run_sync(lambda actual: guard._validate_programme_selection(selection))
        with pytest.raises(production.ProductionWorkspaceReconciliationError,
                match="programme_original_selection_unavailable"):
            await connection.run_sync(lambda actual: guard._validate_programme_selection(replace(selection)))
        with pytest.raises(HeaderBoundsError, match="memory_retention_writer_unavailable"):
            await connection.run_sync(lambda actual: guard._validate_native_writer_snapshot(HeaderReadBudget(), actual))
        async def foreign_task():
            with pytest.raises(HeaderBoundsError, match="memory_retention_writer_changed"):
                await connection.run_sync(guard._ensure_programme_writer)
            with pytest.raises(production.ProductionWorkspaceReconciliationError,
                    match="programme_original_selection_unavailable"):
                await connection.run_sync(lambda actual: guard._validate_programme_selection(selection))
        await asyncio.create_task(foreign_task())
        async with engine.connect() as foreign_connection:
            with pytest.raises(HeaderBoundsError, match="memory_retention_writer_unavailable"):
                await foreign_connection.run_sync(guard._ensure_programme_writer)
        # Successful unchanged publication still uses the genuine local owner.
        await guard.publish()
        assert guard.prepared_commit
