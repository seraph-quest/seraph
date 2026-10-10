"""Actual issuers/live child; resource selection only, no admission effects."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import os

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from config.settings import settings
from src.auth import service as auth
from src.auth.ownership import enroll, revoke_identity
from src.db.engine import get_session, override_session_factory, _configure_sqlite_connection
from src.goals.repository import GoalRepository
from src.goals.contracts import GoalProgrammeRequest, GoalProgrammeAccept
from src.guardian.goal_programmes import goal_programme_service, stage_programme_policy
from src.guardian.goal_discovery import current_goal_discovery, goal_discovery_service
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError, COMPOSITION_DESCRIPTORS, _trace_memory_numeric_charges
from src.memory.composition_headers import _certify_current_memory_snapshot_on_connection, read_programme_identity
from src.model_fabric.configuration import OpenRouterSetup, write_model_fabric_configuration
from src.api.model_fabric_settings import _setup_configuration
from src.runtime_plugins.bridge import cordis_host
from src.runtime_plugins.ownership import DOMAINS, begin_native_writer, initialize_fresh_deployment
from src.security.trust_contract import EgressClass
from src.workflows.job_runtime import DurableJobRepository
from src.workspace.accounting_continuity import transition_programme_envelope
from src.workspace.production import ProductionWorkspace, ProductionWorkspaceReconciliationError, maintenance_fence, prepare_lifecycle_directory
from tests.test_auth_session_composition_privacy import sql_state


class SelectionObserved(RuntimeError):
    """Original boundary observation exits before it returns any authority."""


def private_outputs(case):
    files = {}
    for directory in (case.workspace.host_root, case.workspace.lifecycle_directory):
        for path in directory.rglob("*"):
            if path.is_file() and not path.name.startswith("seraph.db"):
                files[str(path)] = path.read_bytes()
    return files


@pytest_asyncio.fixture
async def genuine_live_selection(tmp_path, monkeypatch, request):
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(settings, "workspace_dir", str(root))
    monkeypatch.setenv("SERAPH_WORKSPACE_LIFECYCLE_PATH", str(tmp_path / "deployment"))
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-real-selection-issuer")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_idle_seconds", 300)
    monkeypatch.setattr(settings, "operator_auth_absolute_seconds", 3600)
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    contacts = []
    async def deny_async(*args, **kwargs):
        contacts.append("async-provider")
        raise AssertionError("provider contact forbidden")
    def deny_sync(*args, **kwargs):
        contacts.append("sync-provider")
        raise AssertionError("provider contact forbidden")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_sync)
    engine = create_async_engine("sqlite+aiosqlite:///" + str(root / "seraph.db"))
    event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    workspace = ProductionWorkspace(host_root=root)
    prepare_lifecycle_directory(workspace)
    assert not goal_programme_service._running
    try:
        with override_session_factory(factory):
            # Original canonical writer persists explicit bounded operator policy.
            # No credential, canary or capability proof is supplied or fabricated.
            setup = OpenRouterSetup(model_ids=("openai/gpt-4o-mini",), capabilities=("text",),
                allowed_upstreams=("openai",), egress_class=EgressClass.CLOUD_ALLOWED_FULL,
                cloud_egress_acknowledged=True, spend_ceiling_microusd=1000,
                request_cost_bound_microusd=100, credential_ref="vault:openrouter_api_key")
            write_model_fabric_configuration(_setup_configuration(setup, profiles=(), policies=()))
            token, operator = await auth.create_session()
            original_session_id = operator.session_id
            identity_id, _, _ = await enroll(operator)
            # Read back through the original bearer owner after enrollment;
            # create_session alone intentionally does not issue an Identity.
            operator = await auth.authenticate_token(token, touch=False)
            assert operator.session_id == original_session_id
            assert operator.operator_identity_id == identity_id
            assert operator.ownership_continuity == "stable"
            goal = await GoalRepository().create("Original private selection goal",
                owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id)
            await goal_programme_service.start()
            requested = GoalProgrammeRequest(expected_goal_revision=goal.revision,
                public_brief="Track dated public releases", budget={"max_inference_microusd": 1000})
            preview = await goal_programme_service.preview(operator=operator, goal_id=goal.id, request=requested)
            accepted = await goal_programme_service.accept(operator=operator, goal_id=goal.id,
                request=GoalProgrammeAccept(**requested.model_dump(), review_digest=preview["review_digest"],
                    public_web_acknowledged=True, local_artifacts_acknowledged=True,
                    inference_ceiling_acknowledged=True))
            assert accepted["state"] == "active"
            scenario = request.node.callspec.params["scenario"]
            if scenario == "goal_revision":
                changed = await GoalRepository().update(goal.id, title="Changed by original Goal owner",
                    expected_revision=goal.revision, expected_owner_principal_id=operator.principal.principal_id,
                    expected_owner_session_id=operator.session_id)
                assert changed.revision == goal.revision + 1
            elif scenario == "identity_revoked":
                await revoke_identity(operator)
            # All Auth/Goal/programme issuers precede genuine composition activation.
            with maintenance_fence(workspace):
                async with get_session() as db:
                    await begin_native_writer(db, owner="composition_maintenance", fresh=True)
                    await initialize_fresh_deployment(db, composition_digests={domain: "a" * 64 for domain in DOMAINS})
            await DurableJobRepository().configure_inference_accounting(1000)
            with maintenance_fence(workspace):
                transition_programme_envelope(workspace=workspace, budget=HeaderReadBudget())
            case = SimpleNamespace(database=root / "seraph.db", engine=engine, workspace=workspace,
                operator=operator, goal_id=goal.id, programme=accepted, contacts=contacts,
                policy=stage_programme_policy())
            assert cordis_host.process is None or cordis_host.process.returncode is not None
            monkeypatch.setattr(cordis_host, "node_path", Path(os.environ["SERAPH_CORDIS_TEST_NODE"]))
            async with current_goal_discovery():
                assert await cordis_host.start(), cordis_host.snapshot()
                assert cordis_host.admitting and cordis_host.reviewed is not None
                yield case
    finally:
        await cordis_host.stop()
        if cordis_host._cleanup_task is not None:
            await cordis_host._cleanup_task
        assert cordis_host.snapshot()["cleanup"]["process_reaped"] is True
        await goal_programme_service.stop()
        await engine.dispose()
        assert contacts == []


@contextmanager
def observe_identity_body(engine, budget, identity_id, *, forbidden=False):
    bodies, provenance = [], []
    def before(connection, cursor, statement, parameters, context, executemany):
        # The original Identity3 body reader's literal closed SELECT, not its
        # permitted numeric locator/schema/header queries; no values captured.
        normalized = " ".join(statement.lower().split())
        if normalized.startswith('select "id","created_at","revoked_at" from operator_identities '):
            assert not forbidden
            assert charges and charges[-1][0] == ("programme-identity-body", identity_id)
            assert charges[-1][1] > 0
            bodies.append("original-identity3-body")
    provenance_columns = [(table, set(COMPOSITION_DESCRIPTORS[table].columns))
        for table in ("goals", "operator_sessions")]
    def after(connection, cursor, statement, parameters, context, executemany):
        names = {item[0] for item in cursor.description or ()}
        for table, columns in provenance_columns:
            if columns.issubset(names):
                assert charges and charges[-1][0][:2] == ("body", table)
                assert charges[-1][1] > 0
                provenance.append(table)
    event.listen(engine.sync_engine, "before_cursor_execute", before)
    event.listen(engine.sync_engine, "after_cursor_execute", after)
    try:
        with _trace_memory_numeric_charges(budget) as charges:
            yield bodies, provenance
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)
        event.remove(engine.sync_engine, "after_cursor_execute", after)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["positive", "copy", "capacity", "goal_revision", "identity_revoked", "fresh_begin"])
async def test_genuine_original_live_capacity_selection(genuine_live_selection, monkeypatch, scenario):
    case = genuine_live_selection
    before, outputs = sql_state(case), private_outputs(case)
    observations = []
    async def boundary(**requested):
        assert requested["goal_id"] == case.goal_id
        budget = HeaderReadBudget()
        async with get_session(header_budget=budget) as db:
            guard = await begin_native_writer(db, owner="finite_service", fresh=False, header_budget=budget)
            connection = await db.connection()
            certificate = await connection.run_sync(
                lambda current: _certify_current_memory_snapshot_on_connection(current, budget))
            if scenario == "capacity":
                budget.debit(budget.remaining)  # Original budget API, negative only.
            def select(current):
                return guard._select_programme_admission(current, certificate,
                    service=goal_discovery_service, host=cordis_host, goal_id=case.goal_id,
                    programme_id=case.programme["id"], grant_revision=case.programme["grant_revision"])
            with observe_identity_body(case.engine, budget, case.operator.operator_identity_id,
                    forbidden=scenario in {"capacity", "goal_revision"}) as (bodies, provenance):
                if scenario in {"capacity", "goal_revision"}:
                    error, code = ((HeaderBoundsError, "canonical_bound_not_certified") if scenario == "capacity"
                        else (ProductionWorkspaceReconciliationError, "programme_original_generation_changed"))
                    with pytest.raises(error, match="^" + code + "$"):
                        await connection.run_sync(select)
                    assert bodies == [] and guard._prospective_admission is None
                else:
                    programme, binding, common33, identity = await connection.run_sync(select)
                    assert common33 is certificate and common33.budget is identity.budget is budget
                    assert binding.owner_identity_id == case.operator.operator_identity_id
                    assert binding.issuer_root_id == case.operator.session_id
                    selection = identity.original_selection
                    assert selection.runs == {} and selection.identity_ids == (binding.owner_identity_id,)
                    assert guard._selection is not selection
                    if scenario == "copy":
                        with pytest.raises(HeaderBoundsError, match="^header_certificate_unavailable$"):
                            await connection.run_sync(lambda current: read_programme_identity(replace(identity), binding.owner_identity_id))
                        with pytest.raises(ProductionWorkspaceReconciliationError, match="^programme_original_selection_unavailable$"):
                            guard._validate_programme_selection(replace(selection))
                        assert bodies == []
                    if scenario == "fresh_begin":
                        spent = budget.remaining
                        await db.rollback()
                        await begin_native_writer(db, owner="finite_service", fresh=False, header_budget=budget)
                        connection = await db.connection()
                        with pytest.raises(ProductionWorkspaceReconciliationError, match="^programme_original_selection_unavailable$"):
                            guard._validate_programme_selection(selection)
                        certificate = await connection.run_sync(
                            lambda current: _certify_current_memory_snapshot_on_connection(current, budget))
                        programme, new_binding, common33, identity = await connection.run_sync(select)
                        assert new_binding == binding and common33.budget is budget
                        assert budget.remaining < spent
                    body = await connection.run_sync(lambda current: read_programme_identity(identity, binding.owner_identity_id))
                    assert bodies == ["original-identity3-body"]
                    canonical = identity.original_selection
                    reason = goal_programme_service._reason(programme,
                        SimpleNamespace(**canonical.goals[binding.goal_id]),
                        SimpleNamespace(**canonical.issuers[binding.issuer_root_id]),
                        SimpleNamespace(id=body[0], created_at=body[1], revoked_at=body[2]),
                        case.policy.epoch, case.policy.digest, case.policy.blocked_reason)
                    assert reason == ("programme_identity_revoked" if scenario == "identity_revoked" else None)
                    guard._finish_programme_admission_selection()
                    with pytest.raises(ProductionWorkspaceReconciliationError, match="^programme_original_selection_unavailable$"):
                        guard._validate_programme_selection(identity.original_selection)
                expected_reads = ([] if scenario == "capacity" else ["goals"] if scenario == "goal_revision"
                    else ["goals", "operator_sessions"] * (2 if scenario == "fresh_begin" else 1))
                assert provenance == expected_reads
            observations.append("actual-original-admission-boundary")
            # Unwind the original owner through its rollback/close path; a
            # normal session exit would request successful publication.
            raise SelectionObserved("capacity inspected; no authority returned")
    # Observe the existing actual owner boundary without returning an authority,
    # skipping a guard or executing the unresolved strategy/file/job paths.
    monkeypatch.setattr(goal_programme_service, "assert_authority", boundary)
    with pytest.raises(SelectionObserved, match="^capacity inspected; no authority returned$"):
        await goal_discovery_service.admit(goal_id=case.goal_id, programme_id=case.programme["id"],
            grant_revision=case.programme["grant_revision"])
    assert observations == ["actual-original-admission-boundary"]
    assert sql_state(case) == before and private_outputs(case) == outputs
    assert goal_discovery_service._tasks == set() and case.contacts == []
