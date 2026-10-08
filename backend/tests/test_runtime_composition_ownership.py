"""Real SQLite + fsynced external witness; composition supplies no grant."""
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import sqlite3
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, select

from config.settings import settings
from src.db.engine import get_session as canonical_session, override_session_factory
from src.db.models import RuntimeCompositionState, WorkflowRunState
from src.runtime_plugins.ownership import (
    DOMAINS, CompositionBindingError, RuntimeCompositionBinding,
    bind_invocation, validate_invocation, method_dependencies,
    begin_native_writer, initialize_fresh_deployment,
)
from src.workspace.production import (
    ProductionWorkspace, ProductionWorkspaceReconciliationError,
    maintenance_fence, prepare_lifecycle_directory, read_lifecycle_receipt,
    read_accounting_checkpoint,
)
from src.workflows.job_runtime import DurableJobRepository, DurableJobLeaseError, DurableJobTransitionError, NativeServiceClaim, _digest
from tests.test_durable_job_runtime import _spec


@pytest_asyncio.fixture
async def composition_db(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(settings, "workspace_dir", str(root))
    monkeypatch.setenv("SERAPH_WORKSPACE_LIFECYCLE_PATH", str(tmp_path / "deployment"))
    workspace = ProductionWorkspace(host_root=root)
    prepare_lifecycle_directory(workspace)
    engine = create_async_engine(f"sqlite+aiosqlite:///{root / 'seraph.db'}")
    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    @asynccontextmanager
    async def native_sessions():
        async with canonical_session() as db:
            db.info["composition_writer_owner"] = "durable_jobs"
            yield db
    monkeypatch.setattr("src.workflows.job_runtime.get_session", native_sessions)
    with override_session_factory(factory):
        with maintenance_fence(workspace):
            async with canonical_session() as db:
                await begin_native_writer(db, owner="composition_maintenance", fresh=True)
                await initialize_fresh_deployment(db, composition_digests={domain: "a" * 64 for domain in DOMAINS})
        yield root, engine, factory, workspace
    await engine.dispose()


async def bound_spec(*, method="tasks.admit", branch="workflow", job_id="bound-1"):
    async with canonical_session() as db:
        binding = await bind_invocation(db, method=method, native_branch=branch)
    original = _spec(job_id=job_id, dedupe_key=job_id)
    return replace(original, identity=replace(original.identity, job_kind="workflow"),
                   composition_binding=binding)


async def native_claim_fixture(monkeypatch):
    from src.auth.service import create_session
    from src.runtime_plugins.bridge import CordisHost
    from src.runtime_plugins.composition import ReviewedComposition
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-claim")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    _, operator = await create_session()
    # Native type/SQL contract fixture, not a claim of stock host execution.
    reviewed = ReviewedComposition(Path("/owned-fixture"), Path("/owned-fixture/node"),
        "v24.13.1", {}, "b" * 64, "c" * 64)
    host = CordisHost()
    host.reviewed, host.boot_nonce, host.state = reviewed, "d" * 64, "ready"
    host.process, host._cleanup_state = SimpleNamespace(returncode=None), "pending"
    async with canonical_session() as db:
        binding = await bind_invocation(db, method="tasks.admit", native_branch="workflow", reviewed_composition=reviewed)
    original = _spec(job_id="native-claim", dedupe_key="native-claim")
    spec = replace(original, identity=replace(original.identity, job_kind="workflow", owner_kind="user",
        owner_principal_id=operator.principal.principal_id), service_id=None, session_id=operator.session_id,
        operator_session_id=operator.session_id, composition_binding=binding,
        declared_authority={"principal": operator.principal.principal_id, "owner_kind": "user", "session_id": operator.session_id},
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    repo = DurableJobRepository()
    await repo.admit_job(spec)
    await repo.transition_job(spec.identity.job_id, "queued")
    return repo, spec, host, operator


@pytest.mark.asyncio
async def test_native_claim_freezes_exact_attempt_host_boot_and_hidden_receipt(composition_db, monkeypatch):
    repo, spec, host, _ = await native_claim_fixture(monkeypatch)
    claim = await repo.claim_service_job(spec.identity.job_id, host=host, owner="native-worker")
    assert isinstance(claim, NativeServiceClaim)
    payload = claim.checkpoint["payload"]
    assert payload["host_boot_nonce"] == claim.host_boot_nonce == "d" * 64
    assert payload["package_digest"] == spec.composition_binding.host_package_digest
    assert payload["attempt_count"] == 1 and payload["fencing_token"] == 1
    assert payload["origin_method"] == "tasks.admit"
    assert claim.checkpoint["checkpoint_id"] == "runtime-service-invocation:" + payload["claim_ref"]
    assert not claim.job["checkpoints"]
    with pytest.raises(TypeError):
        payload["host_boot_nonce"] = "e" * 64
    async with canonical_session() as db:
        run = await db.scalar(select(WorkflowRunState))
        receipt = json.loads(run.checkpoint_receipts_json)[0]
        assert receipt["safe"] is True and receipt["state_digest"] == _digest(receipt["payload"])
        assert receipt["payload"]["composition_binding_digest"] == spec.composition_binding.binding_digest
    with pytest.raises(DurableJobTransitionError, match="only queued"):
        await repo.claim_service_job(spec.identity.job_id, host=host, owner="native-worker")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["boot", "reviewed", "revoked", "journal"])
async def test_native_claim_rejects_original_authority_host_or_journal_change(composition_db, monkeypatch, change):
    _, _, _, workspace = composition_db
    repo, spec, host, operator = await native_claim_fixture(monkeypatch)
    if change == "revoked":
        from src.db.models import OperatorSession
        async with canonical_session() as db:
            (await db.get(OperatorSession, operator.session_id)).revoked_at = datetime.now(timezone.utc)
    elif change == "reviewed":
        host.reviewed = replace(host.reviewed, package_digest="e" * 64)
    before = read_lifecycle_receipt(workspace)
    if change == "journal":
        with pytest.raises(ProductionWorkspaceReconciliationError, match="journal_mint_denied"):
            async with canonical_session() as db:
                await begin_native_writer(db, owner="durable_jobs")
                await db.execute(update(WorkflowRunState).values(checkpoint_receipts_json=json.dumps([
                    {"checkpoint_id": "runtime-service-invocation:forged", "safe": True, "payload": {}}
                ])))
    else:
        async def change_boot(db, run):
            host.boot_nonce = "e" * 64
        with pytest.raises(DurableJobLeaseError, match="original"):
            await repo.claim_service_job(spec.identity.job_id, host=host, owner="native-worker",
                claim_authority_check=change_boot if change == "boot" else None)
    assert read_lifecycle_receipt(workspace) == before
    async with canonical_session() as db:
        run = await db.scalar(select(WorkflowRunState))
        assert run.status == "queued" and run.attempt_count == 0 and run.fencing_token == 0
        assert run.checkpoint_receipts_json == "[]"


def test_method_closure_is_finite_and_optional_services_do_not_gate_reads():
    assert method_dependencies("goals.read") == ("seraph.authority.v1", "seraph.goals.v1", "seraph.tasks.v1")
    assert "seraph.memory.v1" not in method_dependencies("agent-loop.startTurn", native_branch="direct_turn")
    assert "seraph.memory.v1" in method_dependencies("agent-loop.startTurn", native_branch="generic_turn")
    assert "seraph.inference.v1" in method_dependencies("goals.read", programme_bound=True)
    from src.runtime_plugins.ownership import method_closure
    assert method_closure("research.buildPlan", "public_research") == ("research.buildPlan",)
    assert "inference.request" in method_closure("research.executeAccepted", "public_research")
    with pytest.raises(CompositionBindingError, match="unsupported"):
        method_dependencies("capabilities.invoke", native_branch="arbitrary_adapter")


@pytest.mark.parametrize("value", [0.0, -0.0, 0.6, math.nextafter(0.6, 1.0), 1.7976931348623157e308])
def test_turn_float_codec_preserves_exact_binary64_only_in_extension(value):
    from src.workspace.accounting_witness import encode_turn_float, decode_turn_float
    encoded = encode_turn_float(value)
    assert encoded == {"type": "float64", "hex": value.hex()}
    assert decode_turn_float(encoded).hex() == value.hex()


@pytest.mark.parametrize("value", [None, True, 1, "0.6", float("nan"), float("inf"), float("-inf")])
def test_turn_float_codec_rejects_coercion_or_nonfinite(value):
    from src.workspace.accounting_witness import encode_turn_float
    with pytest.raises(ProductionWorkspaceReconciliationError, match="float_invalid"):
        encode_turn_float(value)


def test_turn_float_decoder_rejects_noncanonical_hex_and_unknown_fields():
    from src.workspace.accounting_witness import decode_turn_float, composition_row_digest
    for value in ({"type": "float64", "hex": "0.6"}, {"type": "float64", "hex": "0x0.0p+0", "extra": True}):
        with pytest.raises(ProductionWorkspaceReconciliationError, match="float"):
            decode_turn_float(value)
    with pytest.raises(ProductionWorkspaceReconciliationError, match="scalar_schema_invalid"):
        composition_row_digest("runtime_composition_states", "seraph.tasks.v1", {
            "runtime_domain": "seraph.tasks.v1", "owner_kind": "legacy", "epoch": 1.0,
            "composition_digest": "a" * 64, "state": "ready", "recovery_receipt_ref": None})


@pytest.mark.asyncio
async def test_real_message_episode_best_effort_catch_cannot_clear_retention_failure(composition_db, monkeypatch):
    from src.agent.session import SessionManager
    from src.db.models import Message, MemoryEpisode
    from src.memory.episodes import build_message_episode
    _, _, _, workspace = composition_db
    manager = SessionManager()
    conversation = await manager.get_for_ingress(None, owner_principal_id="operator:episode-proof")
    def unsupported_episode(**kwargs):
        return replace(build_message_episode(**kwargs), salience=float("inf"))
    monkeypatch.setattr("src.agent.session.build_message_episode", unsupported_episode)
    before = read_lifecycle_receipt(workspace)
    with pytest.raises(ProductionWorkspaceReconciliationError, match="extension_float_invalid"):
        async with canonical_session() as db:
            await begin_native_writer(db, owner="native_ingress")
            # Real owner catches the savepoint's exception and returns Message;
            # the outer publisher must still reject that poisoned transaction.
            message = await manager._add_message_in_db(db, conversation.id, "user", "Original plain message")
            assert message.content == "Original plain message"
            assert db.info.get("composition_sticky_failure") is not None
    assert read_lifecycle_receipt(workspace) == before
    async with canonical_session() as db:
        assert (await db.execute(select(Message))).scalars().all() == []
        assert (await db.execute(select(MemoryEpisode))).scalars().all() == []


@pytest.mark.asyncio
async def test_fresh_inventory_once_and_exact_closed_binding(composition_db):
    root, _, _, workspace = composition_db
    witness = read_lifecycle_receipt(workspace)["runtime_composition"]
    assert len(witness["inventory"]) == 14
    assert all(row["epoch"] == 1 and row["owner_kind"] == "legacy" for row in witness["inventory"])
    spec = await bound_spec()
    binding = spec.composition_binding
    assert RuntimeCompositionBinding.from_json(binding.to_json()) == binding
    assert binding.called_epoch == 1
    payload = json.loads(binding.to_json())
    payload["dependency_vector"][0]["epoch"] = 2
    with pytest.raises(CompositionBindingError, match="invalid"):
        RuntimeCompositionBinding.from_json(json.dumps(payload))
    with pytest.raises(CompositionBindingError, match="invalid"):
        RuntimeCompositionBinding.from_json('{"schema_version":1,"schema_version":1}')
    with maintenance_fence(workspace):
        async with canonical_session() as db:
            await begin_native_writer(db, owner="composition_maintenance")
            with pytest.raises(CompositionBindingError, match="fresh_deployment"):
                await initialize_fresh_deployment(db, composition_digests={domain: "b" * 64 for domain in DOMAINS})
    assert read_lifecycle_receipt(workspace)["runtime_composition"] == witness


@pytest.mark.asyncio
async def test_admission_replay_and_mutable_heartbeat_publish_same_owner(composition_db):
    _, _, _, workspace = composition_db
    repo = DurableJobRepository()
    spec = await bound_spec()
    first = await repo.admit_job(spec)
    replay = await repo.admit_job(spec)
    assert first["job_id"] == replay["job_id"]
    async with canonical_session() as db:
        rows = list((await db.execute(select(WorkflowRunState))).scalars())
        assert len(rows) == 1 and rows[0].composition_binding_json == spec.composition_binding.to_json()
        assert rows[0].run_fingerprint != spec.run_fingerprint
    queued = await repo.transition_job(first["job_id"], "queued")
    claimed = await repo.claim_job(first["job_id"], owner="worker", lease_seconds=60)
    before = read_lifecycle_receipt(workspace)["runtime_composition"]
    await repo.heartbeat_job(first["job_id"], owner="worker", fencing_token=claimed["lease"]["fencing_token"])
    after = read_lifecycle_receipt(workspace)["runtime_composition"]
    assert before["closure_digest"] != after["closure_digest"]
    assert before["inventory_digest"] == after["inventory_digest"]
    checkpoint = read_accounting_checkpoint(workspace)
    assert checkpoint["schema_version"] == 2
    assert checkpoint["composition_target"] == after
    assert checkpoint["composition_delta"][0]["table_id"] == "workflow_run_states"
    assert "secret_token" not in json.dumps(checkpoint)


@pytest.mark.asyncio
@pytest.mark.parametrize("style", ["orm", "bulk", "text"])
async def test_unhooked_retained_write_rolls_back_without_publication(composition_db, style):
    _, _, _, workspace = composition_db
    repo = DurableJobRepository()
    spec = await bound_spec()
    await repo.admit_job(spec)
    before = read_lifecycle_receipt(workspace)
    with pytest.raises(ProductionWorkspaceReconciliationError, match="native_writer_required"):
        async with canonical_session() as db:
            if style == "orm":
                run = await db.scalar(select(WorkflowRunState))
                run.result_summary = "unhooked"
                db.add(run)
            elif style == "bulk":
                await db.execute(update(WorkflowRunState).values(result_summary="unhooked"))
            else:
                await db.execute(text("UPDATE workflow_run_states SET result_summary='unhooked'"))
    assert read_lifecycle_receipt(workspace) == before
    async with canonical_session() as db:
        assert (await db.scalar(select(WorkflowRunState))).result_summary is None


@pytest.mark.asyncio
@pytest.mark.parametrize("style", ["core", "driver_sql", "connection_text"])
async def test_direct_connection_cannot_hide_untracked_write_in_native_delta(composition_db, style):
    _, _, _, workspace = composition_db
    repo = DurableJobRepository()
    await repo.admit_job(await bound_spec(job_id="tracked"))
    await repo.admit_job(await bound_spec(job_id="untracked"))
    before = read_lifecycle_receipt(workspace)
    with pytest.raises(ProductionWorkspaceReconciliationError, match="unhooked_connection_sql"):
        async with canonical_session() as db:
            await begin_native_writer(db, owner="durable_jobs")
            await db.execute(update(WorkflowRunState).where(
                WorkflowRunState.run_identity == "tracked").values(result_summary="tracked change"))
            connection = await db.connection()
            if style == "core":
                await connection.execute(update(WorkflowRunState).where(
                    WorkflowRunState.run_identity == "untracked").values(result_summary="hidden change"))
            elif style == "driver_sql":
                await connection.exec_driver_sql(
                    "UPDATE workflow_run_states SET result_summary='hidden change' WHERE run_identity='untracked'")
            else:
                await connection.execute(text(
                    "UPDATE workflow_run_states SET result_summary='hidden change' WHERE run_identity='untracked'"))
    assert read_lifecycle_receipt(workspace) == before
    async with canonical_session() as db:
        assert all(run.result_summary is None for run in (await db.execute(select(WorkflowRunState))).scalars())


@pytest.mark.asyncio
@pytest.mark.parametrize("style", ["orm_source", "bulk_source", "bulk_binding"])
async def test_native_writer_cannot_rebind_original_source_or_composition(composition_db, style):
    _, _, _, workspace = composition_db
    spec = await bound_spec()
    await DurableJobRepository().admit_job(spec)
    before = read_lifecycle_receipt(workspace)
    with pytest.raises(ProductionWorkspaceReconciliationError, match="binding_retrofit_denied"):
        async with canonical_session() as db:
            await begin_native_writer(db, owner="durable_jobs")
            if style == "orm_source":
                run = await db.scalar(select(WorkflowRunState))
                run.source_task_id = "replacement-task"
            elif style == "bulk_source":
                await db.execute(update(WorkflowRunState).values(source_task_id="replacement-task"))
            else:
                await db.execute(update(WorkflowRunState).values(composition_binding_json=spec.composition_binding.to_json()))
    assert read_lifecycle_receipt(workspace) == before
    async with canonical_session() as db:
        run = await db.scalar(select(WorkflowRunState))
        assert run.source_task_id is None
        assert run.composition_binding_json == spec.composition_binding.to_json()


@pytest.mark.asyncio
async def test_internal_commit_cannot_bypass_canonical_precommit_publisher(composition_db):
    _, _, _, workspace = composition_db
    await DurableJobRepository().admit_job(await bound_spec())
    before = read_lifecycle_receipt(workspace)
    with pytest.raises(ProductionWorkspaceReconciliationError, match="unpublished_internal_commit"):
        async with canonical_session() as db:
            await begin_native_writer(db, owner="durable_jobs")
            run = await db.scalar(select(WorkflowRunState))
            run.result_summary = "unpublished"
            await db.commit()
    assert read_lifecycle_receipt(workspace) == before
    async with canonical_session() as db:
        assert (await db.scalar(select(WorkflowRunState))).result_summary is None


@pytest.mark.asyncio
async def test_stale_epoch_final_native_cas_denies_without_legacy_reset(composition_db):
    _, engine, _, workspace = composition_db
    repo = DurableJobRepository()
    spec = await bound_spec()
    await repo.admit_job(spec)
    await repo.transition_job(spec.identity.job_id, "queued")
    # Deliberate stopped SQL-only damage is NOT a cutover: original external
    # witness remains unchanged and prevents all composition-bearing writers.
    async with engine.begin() as connection:
        await connection.execute(update(RuntimeCompositionState).where(
            RuntimeCompositionState.runtime_domain == "seraph.tasks.v1").values(epoch=2))
    with pytest.raises(ProductionWorkspaceReconciliationError, match="continuity"):
        await repo.claim_job(spec.identity.job_id, owner="worker")
    assert next(row for row in read_lifecycle_receipt(workspace)["runtime_composition"]["inventory"]
                if row["runtime_domain"] == "seraph.tasks.v1")["epoch"] == 1


@pytest.mark.asyncio
async def test_partial_inventory_and_missing_private_bytes_fail_closed(composition_db):
    _, engine, _, workspace = composition_db
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM runtime_composition_states WHERE runtime_domain='seraph.memory.v1'"))
    async with canonical_session() as db:
        assert len((await db.execute(select(RuntimeCompositionState))).scalars().all()) == 13
        assert await db.scalar(text("PRAGMA foreign_keys")) == 1
    with pytest.raises(ProductionWorkspaceReconciliationError, match="incomplete"):
        async with canonical_session() as db:
            await begin_native_writer(db, owner="durable_jobs")
    assert len(read_lifecycle_receipt(workspace)["runtime_composition"]["inventory"]) == 14


@pytest.mark.asyncio
async def test_no_inference_restore_retains_every_bound_historical_job_and_exact_retry(composition_db, tmp_path):
    root, _, _, workspace = composition_db
    from src.workspace.accounting_continuity import retain_inference_accounting
    repo = DurableJobRepository()
    for identifier in ("bound-active", "bound-terminal"):
        await repo.admit_job(await bound_spec(job_id=identifier))
    await repo.cancel_job("bound-terminal")
    target = tmp_path / "retained"
    target.mkdir()
    with sqlite3.connect(root / "seraph.db") as source, sqlite3.connect(target / "seraph.db") as destination:
        source.backup(destination)
    with maintenance_fence(workspace):
        result = retain_inference_accounting(active=root, target=target, database_path="seraph.db")
        replay = retain_inference_accounting(active=root, target=target, database_path="seraph.db")
    assert result == replay == {"status": "not_initialized"}
    with sqlite3.connect(target / "seraph.db") as db:
        rows = db.execute("SELECT run_identity,status,fencing_token,lease_owner FROM workflow_run_states ORDER BY run_identity").fetchall()
        assert [row[0] for row in rows] == ["bound-active", "bound-terminal"]
        assert all(row[1] == "blocked" and row[2] >= 1 and row[3] is None for row in rows)
        assert db.execute("SELECT COUNT(*) FROM runtime_composition_states WHERE state='blocked'").fetchone()[0] == 14
    checkpoint = read_accounting_checkpoint(workspace)
    assert checkpoint["composition_target"]["table_counts"]["workflow_run_states"] == 2
    assert read_lifecycle_receipt(workspace)["runtime_composition"] == checkpoint["composition_base"]


@pytest.mark.asyncio
async def test_private_artifact_real_bytes_and_missing_readback_block(composition_db):
    root, _, _, workspace = composition_db
    from src.work_board.input_artifacts import _write_payload, _safe_file_bytes
    import hashlib
    repo = DurableJobRepository()
    spec = await bound_spec()
    await repo.admit_job(spec)
    await repo.transition_job(spec.identity.job_id, "queued")
    claimed = await repo.claim_job(spec.identity.job_id, owner="worker", lease_seconds=60)
    fence = claimed["lease"]["fencing_token"]
    reference = "artifacts/work-board/native-proof.json"
    payload = b'{"bounded":"native readback"}'
    digest = hashlib.sha256(payload).hexdigest()
    _write_payload(root / reference, payload)
    actual = _safe_file_bytes(root / reference, expected_digest=digest, expected_size=len(payload))
    await repo.record_artifact(spec.identity.job_id, file_path=reference, content=actual,
                               owner="worker", fencing_token=fence)
    await repo.record_readback(spec.identity.job_id, target_path=reference,
        target_digest=digest, content_sha256=digest, status="succeeded", readback_id="native-readback",
        details={"verified": True, "no_learning": True}, owner="worker", fencing_token=fence)
    witness = read_lifecycle_receipt(workspace)["runtime_composition"]
    assert witness["artifact_count"] == 1
    assert "bounded" not in json.dumps(witness)
    (root / reference).unlink()
    from src.work_board.repository import BoardError
    async with canonical_session() as db:
        assert (await db.scalar(select(WorkflowRunState))).run_identity == spec.identity.job_id
        assert db.info.get("composition_guard") is None
    with pytest.raises(BoardError, match="unavailable"):
        await repo.heartbeat_job(spec.identity.job_id, owner="worker", fencing_token=fence)
    assert read_lifecycle_receipt(workspace)["runtime_composition"] == witness


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["partial", "stale"])
async def test_degraded_inventory_allows_auth_and_read_without_private_proof_or_publication(
    composition_db, monkeypatch, damage,
):
    from src.auth.service import create_session
    _, engine, _, workspace = composition_db
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-degraded-read")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    before = read_lifecycle_receipt(workspace)
    checkpoint = read_accounting_checkpoint(workspace)
    async with engine.begin() as connection:
        if damage == "partial":
            await connection.execute(text("DELETE FROM runtime_composition_states WHERE runtime_domain='seraph.memory.v1'"))
        else:
            await connection.execute(update(RuntimeCompositionState).values(epoch=2))
    def forbidden_full_read(*args, **kwargs):
        pytest.fail("ordinary auth/status read must not calculate full closure or private bytes")
    with monkeypatch.context() as reads:
        reads.setattr("src.workspace.accounting_witness.composition_closure", forbidden_full_read)
        _, operator = await create_session()
        assert operator.session_id
        async with canonical_session() as db:
            rows = (await db.execute(select(RuntimeCompositionState))).scalars().all()
            assert len(rows) == (13 if damage == "partial" else 14)
            assert db.info.get("composition_read_guard") is not None
            assert db.info.get("composition_guard") is None
    with pytest.raises(ProductionWorkspaceReconciliationError, match="incomplete|continuity"):
        async with canonical_session() as db:
            await begin_native_writer(db, owner="finite_service")
    assert read_lifecycle_receipt(workspace) == before
    assert read_accounting_checkpoint(workspace) == checkpoint


@pytest.mark.asyncio
async def test_actual_audit_owner_upgrades_before_fk_write_and_keeps_existing_parent_noop(composition_db):
    from src.audit.repository import audit_repository
    from src.db.models import AuditEvent, Session
    _, _, _, workspace = composition_db
    event = await audit_repository.log_event(event_type="native_turn_started", summary="Started", session_id="audit-root")
    async with canonical_session() as db:
        parent = await db.get(Session, "audit-root")
        assert parent is not None and parent.owner_principal_id is None
        assert await db.scalar(text("PRAGMA foreign_keys")) == 1
        assert (await db.get(AuditEvent, event.id)).session_id == parent.id
    before = read_lifecycle_receipt(workspace)
    await audit_repository.log_event(event_type="native_turn_started", summary="Again", session_id="audit-root")
    assert read_lifecycle_receipt(workspace) == before
    async def denied(db):
        assert db.info.get("native_writer_started") is True
        raise DurableJobLeaseError("stale original audit invocation")
    with pytest.raises(DurableJobLeaseError, match="stale original"):
        await audit_repository.log_event(event_type="native_turn_started", summary="Denied",
            session_id="denied-root", composition_authority_check=denied)
    async with canonical_session() as db:
        assert await db.get(Session, "denied-root") is None
        assert len((await db.execute(select(AuditEvent))).scalars().all()) == 2


@pytest.mark.asyncio
async def test_original_turn_claim_without_completed_output_remains_unknown_and_cannot_replay(composition_db, monkeypatch):
    from tests.test_native_turn_bootstrap import prepare_turn, reserve
    from src.runtime_plugins.bridge import CordisHost
    from src.agent.turn_execution import validate_native_turn_claim
    from src.workflows.job_runtime import _restart_recovery_state
    manager, ingress, admission, content = await prepare_turn(monkeypatch)
    _, _, admitted = await reserve(manager, ingress, admission, content)
    host = CordisHost()
    host.reviewed, host.boot_nonce, host.state = admission.reviewed_composition, "d" * 64, "ready"
    host.process, host._cleanup_state = SimpleNamespace(returncode=None), "pending"
    repo = DurableJobRepository()
    queued = await repo.queue_job(admission.job_id, expected_revision=admitted["revision"])
    async def authority_check(db, run):
        await validate_native_turn_claim(db, run, admission)
    claim = await repo.claim_service_job(admission.job_id, host=host, owner="original-turn",
        expected_revision=queued["revision"], claim_authority_check=authority_check)
    async with canonical_session() as db:
        run = await db.scalar(select(WorkflowRunState))
        assert json.loads(run.effect_receipts_json) == []
        assert _restart_recovery_state(run) == ("unknown_external_effect", "native_turn_physical_completion_unproven")
        original = (run.attempt_count, run.fencing_token, run.deadline_at, run.checkpoint_receipts_json)
    assert claim.job["native_turn_execution"] == {"phase": "possibly_started", "physical_completion": "unproven", "replay": "denied"}
    with pytest.raises(DurableJobTransitionError, match="physical completion is unproven"):
        await repo.retry_job(admission.job_id, owner_kind="user",
            owner_principal_id=ingress.principal_id, reconciliation_receipt={
                "effect_id": f"job-failure:{admission.job_id}", "effect_type": "job_failure",
                "target_path": f"job:{admission.job_id}", "status": "read_back", "outcome": "no_external_effect"})
    with pytest.raises(DurableJobTransitionError, match="physical completion is unproven"):
        await repo.transition_job(admission.job_id, "queued")
    async with canonical_session() as db:
        run = await db.scalar(select(WorkflowRunState))
        assert (run.attempt_count, run.fencing_token, run.deadline_at, run.checkpoint_receipts_json) == original


async def original_sdk_controlled_exception(execution, *, approval):
    """Actual canonical producer and stock SDK callback, with zero model calls."""
    import asyncio
    from smolagents import ToolCallingAgent
    from smolagents.memory import ActionStep, Timing
    from smolagents.utils import AgentToolExecutionError
    from src.approval.runtime import set_runtime_context, reset_runtime_context
    from src.tools.approval import ApprovalTool
    from src.tools.clarify_tool import clarify
    from tests.test_approval_tools import DummyExecuteCodeTool
    from tests.test_native_turn_transport import ScriptedModel
    model = ScriptedModel()
    leaf = DummyExecuteCodeTool() if approval else clarify
    tool = ApprovalTool(leaf, force_approval=True) if approval else leaf
    agent = ToolCallingAgent(tools=[tool], model=model, max_steps=1, verbosity_level=0)
    execution.prepare_agent(agent)
    def callback():
        tokens = set_runtime_context(execution.admission.ingress.session_id, "high_risk",
            trust_principal=execution.admission.principal)
        try:
            try:
                agent.execute_tool_call(tool.name,
                    {"code": "never execute before approval"} if approval else {"question": "Private clarification question"})
            except AgentToolExecutionError as error:
                agent.step_callbacks.callback(ActionStep(step_number=1,
                    timing=Timing(start_time=0, end_time=1), error=error), agent=agent)
        finally:
            reset_runtime_context(tokens)
    from src.agent.exceptions import ClarificationRequired
    from src.approval.exceptions import ApprovalRequired
    expected = ApprovalRequired if approval else ClarificationRequired
    with pytest.raises(expected) as observed:
        await execution.execute(asyncio.to_thread(execution.run_callback, callback))
    assert execution.worker.done() and model.calls == 0
    if approval:
        assert leaf.calls == []
    return observed.value


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["clarification", "approval", "approval_resolved", "approval_wrong_tool",
    "clarification_rollback", "approval_cost", "approval_expiry", "approval_forged", "approval_expired",
    "approval_root_revoked", "clarification_deadline_expired",
    *[f"approval_stale:{field}" for field in ("approval_id", "tool_name", "fingerprint", "owner_principal_id",
        "operator_session_id", "conversation_id", "expires_at", "created_at")]])
async def test_controlled_settlement_retains_selected_rows_decision_and_stopped_restore(composition_db, monkeypatch, tmp_path, outcome):
    import asyncio
    from uuid import uuid5, NAMESPACE_URL
    from tests.test_native_turn_bootstrap import prepare_turn, reserve
    from src.runtime_plugins.bridge import CordisHost
    from src.agent.turn_execution import NativeTurnExecution, validate_native_turn_claim
    from src.agent.exceptions import ClarificationRequired
    from src.approval.exceptions import ApprovalRequired
    from src.approval.repository import approval_repository
    from src.db.models import Message, ApprovalRequest
    from src.workspace.accounting_continuity import retain_inference_accounting
    from src.workflows.job_runtime import _native_turn_pending
    root, _, _, workspace = composition_db
    manager, ingress, admission, content = await prepare_turn(monkeypatch)
    _, _, admitted = await reserve(manager, ingress, admission, content)
    host = CordisHost()
    host.reviewed, host.boot_nonce, host.state = admission.reviewed_composition, "d" * 64, "ready"
    host.process, host._cleanup_state = SimpleNamespace(returncode=None), "pending"
    repo = DurableJobRepository()
    queued = await repo.queue_job(admission.job_id, expected_revision=admitted["revision"])
    async def check(db, run):
        await validate_native_turn_claim(db, run, admission)
    claim = await repo.claim_service_job(admission.job_id, host=host, owner="original-controlled",
        expected_revision=queued["revision"], claim_authority_check=check)
    execution = NativeTurnExecution(admission, host, claim, None)
    is_approval = outcome.startswith("approval")
    if outcome == "approval_forged":
        exception = ApprovalRequired(approval_id="f" * 32, session_id=ingress.session_id,
            tool_name="execute_code", risk_level="high", summary="Fabricated public exception")
        async def forged_callback():
            raise exception
        with pytest.raises(ApprovalRequired) as observed:
            await execution.execute(forged_callback())
        assert observed.value is exception and execution.worker.done()
    else:
        exception = await original_sdk_controlled_exception(execution, approval=is_approval)
    if is_approval:
        request = await approval_repository.get(exception.approval_id)
        assert request is not None or outcome == "approval_forged"
        if outcome == "approval_wrong_tool":
            exception.tool_name = "wrong_tool"
        if outcome == "approval_resolved":
            assert (await approval_repository.resolve(request.id, "denied")).status == "denied"
        if outcome.startswith("approval_stale:"):
            from src.approval.repository import _approval_writer_session
            field = outcome.split(":", 1)[1]
            replacements = {"approval_id": "e" * 32, "tool_name": "changed_tool", "fingerprint": "e" * 64,
                "owner_principal_id": "operator:other", "operator_session_id": "changed-root",
                "conversation_id": "changed-conversation", "expires_at": request.expires_at + timedelta(seconds=60),
                "created_at": request.created_at + timedelta(seconds=1)}
            async with _approval_writer_session() as db:
                changed = await db.get(ApprovalRequest, request.id)
                setattr(changed, "id" if field == "approval_id" else field, replacements[field])
        if outcome == "approval_expired":
            from src.approval.repository import _approval_writer_session
            async with _approval_writer_session() as db:
                changed = await db.get(ApprovalRequest, request.id)
                changed.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        if outcome == "approval_root_revoked":
            from src.auth.service import revoke_session
            await revoke_session(ingress.operator_session_id)
    identifier = uuid5(NAMESPACE_URL,
        f"seraph-chat:{ingress.principal_id}:{ingress.conversation_id}:{ingress.message_id}:clarification").hex
    from src.api.chat import chat_ingress_metadata
    original_effects = []
    if outcome == "approval_cost":
        await repo.record_effect(admission.job_id, effect_type="governed_inference", effect_id="owned-unknown-cost",
            status="unknown", details={"unknown_cost_outstanding": True}, owner="original-controlled",
            fencing_token=claim.checkpoint["payload"]["fencing_token"])
        async with canonical_session() as db:
            original_effects = json.loads((await db.scalar(select(WorkflowRunState))).effect_receipts_json)
    before_failure = read_lifecycle_receipt(workspace)
    if outcome == "clarification_rollback":
        def fail_publication(*args, **kwargs):
            raise OSError("controlled fsync rollback")
        monkeypatch.setattr("src.workspace.production.write_lifecycle_receipt", fail_publication)
    async def settle():
        return await manager.record_native_turn_controlled(ingress.session_id, exception, execution=execution,
            content=exception.render_message() if not is_approval else None,
            message_id=identifier if not is_approval else None,
            metadata_json=chat_ingress_metadata(ingress) if not is_approval else None)
    if outcome == "clarification_deadline_expired":
        class ExpiredClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(minutes=2)
        monkeypatch.setattr("src.agent.turn_execution.datetime", ExpiredClock)
    if outcome in {"approval_resolved", "approval_wrong_tool", "approval_forged", "clarification_rollback",
            "approval_expired", "approval_root_revoked", "clarification_deadline_expired"} or outcome.startswith("approval_stale:"):
        expected = OSError if outcome == "clarification_rollback" else DurableJobLeaseError
        match = "canonical controlled producer required" if outcome in {"approval_wrong_tool", "approval_forged"} else "rollback|reservation changed"
        if outcome == "approval_root_revoked":
            from src.agent.turn_execution import NativeTurnBlocked
            expected, match = NativeTurnBlocked, "original_root_inactive"
        if outcome == "clarification_deadline_expired":
            expected, match = asyncio.TimeoutError, "deadline expired"
        with pytest.raises(expected, match=match):
            await settle()
        assert read_lifecycle_receipt(workspace) == before_failure
        async with canonical_session() as db:
            run = await db.scalar(select(WorkflowRunState))
            assert run.status == "running" and run.lease_owner == "original-controlled"
            assert _native_turn_pending(run)
            assert not any(item["checkpoint_id"] == "conversation:controlled-outcome" for item in json.loads(run.checkpoint_receipts_json))
            assert len((await db.execute(select(Message))).scalars().all()) == 1
        if outcome == "clarification_rollback":
            assert read_accounting_checkpoint(workspace)["composition_target"] != before_failure["runtime_composition"]
        return
    await settle()
    expected_status = "cost_liability" if outcome == "approval_cost" else "awaiting_approval" if is_approval else "paused"
    async with canonical_session() as db:
        run = await db.scalar(select(WorkflowRunState))
        assert run.status == expected_status
        assert json.loads(run.effect_receipts_json) == original_effects
        assert run.attempt_count == 1 and run.fencing_token == claim.checkpoint["payload"]["fencing_token"]
        assert run.lease_owner is None and run.goal_id is None and not _native_turn_pending(run)
        receipts = json.loads(run.checkpoint_receipts_json)
        receipt = next(item for item in receipts if item["checkpoint_id"] == "conversation:controlled-outcome")
        assert receipt["payload"]["input_message_ref"] == ingress.message_id
        assert receipt["payload"]["no_learning"] is True
        assert "Private" not in json.dumps(receipt)
        messages = (await db.execute(select(Message))).scalars().all()
        assert len(messages) == (1 if is_approval else 2)
    before = read_lifecycle_receipt(workspace)["runtime_composition"]
    if is_approval:
        decision = "denied"
        if outcome == "approval_expiry":
            class ExpiredClock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return datetime.now(tz) + timedelta(minutes=6)
            with monkeypatch.context() as clock:
                clock.setattr("src.approval.repository.datetime", ExpiredClock)
                assert await approval_repository.list_pending(approval_id=request.id) == []
            decision = "expired"
        else:
            decided = await approval_repository.resolve(request.id, "denied")
            assert decided.status == "denied"
        after = read_lifecycle_receipt(workspace)["runtime_composition"]
        assert after != before and after["table_counts"]["approval_requests"] == 1
        async with canonical_session() as db:
            assert (await db.scalar(select(WorkflowRunState))).status == expected_status
            assert (await db.get(ApprovalRequest, request.id)).status == decision
    else:
        assert before["table_counts"]["messages"] == 2
    target = tmp_path / "controlled-retained"
    target.mkdir()
    with sqlite3.connect(root / "seraph.db") as source, sqlite3.connect(target / "seraph.db") as destination:
        source.backup(destination)
    with maintenance_fence(workspace):
        result = retain_inference_accounting(active=root, target=target, database_path="seraph.db")
        assert retain_inference_accounting(active=root, target=target, database_path="seraph.db") == result
    with sqlite3.connect(target / "seraph.db") as db:
        assert db.execute("SELECT status FROM workflow_run_states").fetchone()[0] == "blocked"
        assert json.loads(db.execute("SELECT effect_receipts_json FROM workflow_run_states").fetchone()[0]) == original_effects
        assert db.execute("SELECT COUNT(*) FROM runtime_composition_states WHERE state='ready'").fetchone()[0] == 0
        if is_approval:
            assert db.execute("SELECT status FROM approval_requests WHERE id=?", (request.id,)).fetchone()[0] == decision
        else:
            assert db.execute("SELECT id FROM messages WHERE role='assistant'").fetchone()[0] == identifier


@pytest.mark.asyncio
async def test_large_complete_historical_restore_blocks_without_trimming_or_partial_sql(composition_db, tmp_path):
    from src.workspace.accounting_continuity import retain_inference_accounting
    root, _, _, workspace = composition_db
    repo = DurableJobRepository()
    original = await bound_spec(job_id="historical-template")
    for start in (0, 64, 128):
        async with canonical_session() as db:
            await begin_native_writer(db, owner="durable_jobs")
            for index in range(start, min(start + 64, 129)):
                identity = f"history-{index:03d}"
                spec = replace(original, identity=replace(original.identity, job_id=identity,
                    idempotency_key=identity))
                await repo._admit_in_session(db, spec)
    before_receipt = read_lifecycle_receipt(workspace)
    before_checkpoint = read_accounting_checkpoint(workspace)
    assert before_receipt["runtime_composition"]["table_counts"]["workflow_run_states"] == 129
    target = tmp_path / "large-retained"
    target.mkdir()
    with sqlite3.connect(root / "seraph.db") as source, sqlite3.connect(target / "seraph.db") as destination:
        source.backup(destination)
    with sqlite3.connect(target / "seraph.db") as db:
        before_rows = db.execute("SELECT run_identity,status,revision,fencing_token,checkpoint_receipts_json FROM workflow_run_states ORDER BY run_identity").fetchall()
    with maintenance_fence(workspace), pytest.raises(ProductionWorkspaceReconciliationError, match="delta|128|limit|bound"):
        retain_inference_accounting(active=root, target=target, database_path="seraph.db")
    with sqlite3.connect(target / "seraph.db") as db:
        assert db.execute("SELECT run_identity,status,revision,fencing_token,checkpoint_receipts_json FROM workflow_run_states ORDER BY run_identity").fetchall() == before_rows
        assert db.execute("SELECT COUNT(*) FROM workflow_run_states").fetchone()[0] == 129
        assert db.execute("SELECT COUNT(*) FROM runtime_composition_states WHERE state='ready'").fetchone()[0] == 14
    assert read_lifecycle_receipt(workspace) == before_receipt
    assert read_accounting_checkpoint(workspace) == before_checkpoint


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", [None, "missing_audit", "broken_predecessor", "missing_fk"])
async def test_stopped_restore_keeps_exact_native_audit_chain_and_fk_or_blocks(composition_db, tmp_path, damage):
    from src.db.models import AuditEvent
    from src.runtime_plugins.ownership import CompositionDependency, transition_owner, restore_audit_reference, restored_recovery_reference
    from src.workspace.accounting_continuity import retain_inference_accounting
    root, _, _, workspace = composition_db
    spec = await bound_spec(job_id="audit-parent-job")
    await DurableJobRepository().admit_job(spec)
    before = CompositionDependency("seraph.tasks.v1", "legacy", 1, "a" * 64)
    after = CompositionDependency("seraph.tasks.v1", "cordis", 2, "b" * 64)
    # Pure canonical SQL recovery-proof mechanics; no host boot/quiescence claim.
    with maintenance_fence(workspace):
        async with canonical_session() as db:
            await begin_native_writer(db, owner="composition_maintenance")
            event = AuditEvent(session_id=spec.session_id, event_type="runtime_composition_recovery",
                details_json=json.dumps({"schema_version": 1, "runtime_domain": before.runtime_domain,
                    "prior": before.payload(), "target": after.payload(), "state": "blocked",
                    "phase": "awaiting_boot", "prior_recovery_receipt_ref": None}))
            db.add(event)
            await db.flush()
            first_id = event.id
            await transition_owner(db, runtime_domain=before.runtime_domain, expected=before,
                owner_kind=after.owner_kind, epoch=after.epoch, composition_digest=after.composition_digest,
                state="blocked", recovery_receipt_ref=first_id)
        async with canonical_session() as db:
            await begin_native_writer(db, owner="composition_maintenance")
            event = AuditEvent(session_id=spec.session_id, event_type="runtime_composition_recovery",
                details_json=json.dumps({"schema_version": 1, "runtime_domain": after.runtime_domain,
                    "prior": after.payload(), "target": after.payload(), "state": "ready",
                    "phase": "boot_verified", "prior_recovery_receipt_ref": first_id}))
            db.add(event)
            await db.flush()
            second_id = event.id
            await transition_owner(db, runtime_domain=after.runtime_domain, expected=after,
                owner_kind=after.owner_kind, epoch=after.epoch, composition_digest=after.composition_digest,
                state="ready", recovery_receipt_ref=second_id)
    receipt_before = read_lifecycle_receipt(workspace)
    checkpoint_before = read_accounting_checkpoint(workspace)
    assert receipt_before["runtime_composition"]["table_counts"]["audit_events"] == 2
    target = tmp_path / "audit-chain-retained"
    target.mkdir()
    with sqlite3.connect(root / "seraph.db") as source, sqlite3.connect(target / "seraph.db") as destination:
        source.backup(destination)
    if damage is not None:
        # Deliberate stopped canonical corruption, never a production FK bypass.
        with sqlite3.connect(root / "seraph.db") as db:
            if damage == "missing_audit":
                db.execute("DELETE FROM audit_events WHERE id=?", (first_id,))
            elif damage == "missing_fk":
                db.execute("DELETE FROM sessions WHERE id=?", (spec.session_id,))
            else:
                raw = json.loads(db.execute("SELECT details_json FROM audit_events WHERE id=?", (second_id,)).fetchone()[0])
                raw["prior_recovery_receipt_ref"] = "e" * 32
                db.execute("UPDATE audit_events SET details_json=? WHERE id=?", (json.dumps(raw), second_id))
        with maintenance_fence(workspace), pytest.raises((ProductionWorkspaceReconciliationError, CompositionBindingError)):
            retain_inference_accounting(active=root, target=target, database_path="seraph.db")
        with sqlite3.connect(target / "seraph.db") as db:
            assert db.execute("SELECT recovery_receipt_ref FROM runtime_composition_states WHERE runtime_domain=?", (after.runtime_domain,)).fetchone()[0] == second_id
            assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 2
        assert read_lifecycle_receipt(workspace) == receipt_before
        assert read_accounting_checkpoint(workspace) == checkpoint_before
        return
    with maintenance_fence(workspace):
        result = retain_inference_accounting(active=root, target=target, database_path="seraph.db")
        assert retain_inference_accounting(active=root, target=target, database_path="seraph.db") == result
    with sqlite3.connect(target / "seraph.db") as db:
        marker = db.execute("SELECT recovery_receipt_ref FROM runtime_composition_states WHERE runtime_domain=?", (after.runtime_domain,)).fetchone()[0]
        assert len(marker) == 105 and restore_audit_reference(marker) == second_id
        assert restore_audit_reference(restored_recovery_reference("c" * 64, marker)) == second_id
        assert db.execute("SELECT COUNT(*) FROM audit_events WHERE id IN (?,?)", (first_id, second_id)).fetchone()[0] == 2
        assert db.execute("SELECT session_id FROM audit_events WHERE id=?", (first_id,)).fetchone()[0] == spec.session_id
        assert db.execute("SELECT id FROM sessions WHERE id=?", (spec.session_id,)).fetchone()[0] == spec.session_id
        assert db.execute("SELECT state FROM runtime_composition_states WHERE runtime_domain=?", (after.runtime_domain,)).fetchone()[0] == "blocked"


@pytest.mark.asyncio
async def test_readonly_availability_absent_fresh_inventory_returns_only_legacy_signal(tmp_path, monkeypatch):
    from src.runtime_plugins.composition import ReviewedComposition
    from src.runtime_plugins.ownership import inspect_invocation_availability
    root = tmp_path / "fresh-cpu"
    root.mkdir()
    monkeypatch.setattr(settings, "workspace_dir", str(root))
    monkeypatch.setenv("SERAPH_WORKSPACE_LIFECYCLE_PATH", str(tmp_path / "fresh-deployment"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{root / 'seraph.db'}")
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    reviewed = ReviewedComposition(Path("/owned-fixture"), Path("/owned-fixture/node"), "v24.13.1", {}, "b" * 64, "c" * 64)
    try:
        with override_session_factory(factory):
            async with canonical_session() as db:
                result = await inspect_invocation_availability(db, method="conversation.accept",
                    native_branch="direct_turn", reviewed_composition=reviewed)
                assert result.available is False and result.reason_code == "composition_inventory_absent"
                assert set(result.__dict__) == {"available", "reason_code"}
                assert (await db.execute(select(RuntimeCompositionState))).scalars().all() == []
                assert (await db.execute(select(WorkflowRunState))).scalars().all() == []
                assert not db.new and not db.dirty and db.info.get("composition_guard") is None
            assert not ProductionWorkspace(host_root=root).lifecycle_directory.exists()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", [None, "partial", "stale", "empty_retained"])
async def test_readonly_availability_exact_inventory_or_denied_never_retained_fallback(composition_db, damage):
    from src.runtime_plugins.composition import ReviewedComposition
    from src.runtime_plugins.ownership import inspect_invocation_availability
    _, engine, _, workspace = composition_db
    reviewed = ReviewedComposition(Path("/owned-fixture"), Path("/owned-fixture/node"), "v24.13.1", {}, "b" * 64, "c" * 64)
    before = read_lifecycle_receipt(workspace)
    checkpoint = read_accounting_checkpoint(workspace)
    if damage is not None:
        async with engine.begin() as connection:
            if damage == "partial":
                await connection.execute(text("DELETE FROM runtime_composition_states WHERE runtime_domain='seraph.memory.v1'"))
            elif damage == "stale":
                await connection.execute(update(RuntimeCompositionState).values(epoch=2))
            else:
                await connection.execute(text("DELETE FROM runtime_composition_states"))
    async with canonical_session() as db:
        if damage is None:
            result = await inspect_invocation_availability(db, method="conversation.accept",
                native_branch="direct_turn", reviewed_composition=reviewed)
            assert result.available is True and result.reason_code is None
            assert set(result.__dict__) == {"available", "reason_code"}
        else:
            expected = {"partial": "inventory_incomplete", "stale": "inventory_stale", "empty_retained": "retained_inventory_missing"}[damage]
            with pytest.raises(CompositionBindingError, match=expected):
                await inspect_invocation_availability(db, method="conversation.accept",
                    native_branch="direct_turn", reviewed_composition=reviewed)
        assert not db.new and not db.dirty and db.info.get("composition_guard") is None
    assert read_lifecycle_receipt(workspace) == before
    assert read_accounting_checkpoint(workspace) == checkpoint


@pytest.mark.asyncio
async def test_postpublication_sql_rollback_keeps_pending_delta_not_overwritten(composition_db, monkeypatch):
    _, _, _, workspace = composition_db
    repo = DurableJobRepository()
    spec = await bound_spec()
    await repo.admit_job(spec)
    from src.workspace import production
    original = production.write_lifecycle_receipt
    def fail_target(workspace, receipt, **kwargs):
        raise OSError("injected receipt fsync failure")
    monkeypatch.setattr(production, "write_lifecycle_receipt", fail_target)
    with pytest.raises(OSError, match="injected"):
        await repo.transition_job(spec.identity.job_id, "queued")
    checkpoint = read_accounting_checkpoint(workspace)
    monkeypatch.setattr(production, "write_lifecycle_receipt", original)
    with pytest.raises(ProductionWorkspaceReconciliationError, match="pending"):
        await repo.transition_job(spec.identity.job_id, "queued")
    assert read_accounting_checkpoint(workspace) == checkpoint
    assert read_lifecycle_receipt(workspace)["runtime_composition"] == checkpoint["composition_base"]


@pytest.mark.asyncio
async def test_ordinary_checkpoint_cannot_mint_original_claim(composition_db):
    from src.workflows.job_runtime import DurableJobTransitionError
    repo = DurableJobRepository()
    spec = await bound_spec()
    await repo.admit_job(spec)
    await repo.transition_job(spec.identity.job_id, "queued")
    claimed = await repo.claim_job(spec.identity.job_id, owner="worker", lease_seconds=60)
    for identifier in ("runtime-service-invocation", "runtime-service-invocation:forged", "conversation:assistant-message"):
        with pytest.raises(DurableJobTransitionError, match="protected"):
            await repo.record_checkpoint(spec.identity.job_id, checkpoint_id=identifier,
                state={"forged": True}, checkpoint_payload={"forged": True},
                owner="worker", fencing_token=claimed["lease"]["fencing_token"])
    assert (await repo.get_job(spec.identity.job_id))["checkpoints"] == []


@pytest.mark.asyncio
async def test_atomic_shared_delta_bound_rolls_back_all_native_admissions(composition_db):
    _, _, _, workspace = composition_db
    repo = DurableJobRepository()
    spec = await bound_spec()
    before = read_lifecycle_receipt(workspace)
    with pytest.raises(ProductionWorkspaceReconciliationError, match="delta_exceeded"):
        async with canonical_session() as db:
            await begin_native_writer(db, owner="durable_jobs")
            for index in range(129):
                identity = replace(spec.identity, job_id=f"atomic-{index}", idempotency_key=f"atomic-{index}")
                await repo._admit_in_session(db, replace(spec, identity=identity))
    async with canonical_session() as db:
        assert (await db.execute(select(WorkflowRunState))).scalars().all() == []
    assert read_lifecycle_receipt(workspace) == before


@pytest.mark.asyncio
async def test_owner_cas_requires_stopped_new_epoch_and_original_boot_receipt(composition_db):
    _, _, _, workspace = composition_db
    from src.runtime_plugins.ownership import CompositionDependency, transition_owner
    from src.db.models import AuditEvent
    before = CompositionDependency("seraph.tasks.v1", "legacy", 1, "a" * 64)
    after = CompositionDependency("seraph.tasks.v1", "cordis", 2, "b" * 64)
    with maintenance_fence(workspace):
        async with canonical_session() as db:
            await begin_native_writer(db, owner="composition_maintenance")
            receipt = AuditEvent(event_type="runtime_composition_recovery", actor="managed_maintenance",
                details_json=json.dumps({"schema_version": 1, "runtime_domain": before.runtime_domain,
                    "prior": before.payload(), "target": after.payload(), "state": "blocked", "phase": "awaiting_boot",
                    "prior_recovery_receipt_ref": None}))
            db.add(receipt)
            await db.flush()
            await transition_owner(db, runtime_domain=before.runtime_domain, expected=before,
                owner_kind=after.owner_kind, epoch=after.epoch, composition_digest=after.composition_digest,
                state="blocked", recovery_receipt_ref=receipt.id)
    async with canonical_session() as db:
        with pytest.raises(CompositionBindingError, match="dependency_unavailable"):
            await bind_invocation(db, method="tasks.inspect")
    with maintenance_fence(workspace):
        async with canonical_session() as db:
            await begin_native_writer(db, owner="composition_maintenance")
            original_boot_ref = (await db.get(RuntimeCompositionState, after.runtime_domain)).recovery_receipt_ref
            receipt = AuditEvent(event_type="runtime_composition_recovery", actor="managed_maintenance",
                details_json=json.dumps({"schema_version": 1, "runtime_domain": after.runtime_domain,
                    "prior": after.payload(), "target": after.payload(), "state": "ready", "phase": "boot_verified",
                    "prior_recovery_receipt_ref": original_boot_ref}))
            db.add(receipt)
            await db.flush()
            await transition_owner(db, runtime_domain=after.runtime_domain, expected=after,
                owner_kind=after.owner_kind, epoch=after.epoch, composition_digest=after.composition_digest,
                state="ready", recovery_receipt_ref=receipt.id)
    async with canonical_session() as db:
        current = await bind_invocation(db, method="tasks.inspect")
        assert current.called_epoch == 2
    with maintenance_fence(workspace):
        with pytest.raises(CompositionBindingError, match="epoch_conflict"):
            async with canonical_session() as db:
                await begin_native_writer(db, owner="composition_maintenance")
                await transition_owner(db, runtime_domain=after.runtime_domain, expected=after,
                    owner_kind="legacy", epoch=1, composition_digest="a" * 64,
                    state="blocked", recovery_receipt_ref="missing")
    assert next(row for row in read_lifecycle_receipt(workspace)["runtime_composition"]["inventory"]
                if row["runtime_domain"] == after.runtime_domain)["epoch"] == 2
