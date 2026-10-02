from __future__ import annotations

import asyncio
import json
import hashlib
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.models import GuardianRoutine, GuardianRoutineVersion, SQLModel, WorkflowRunState
from sqlmodel import select
from src.workflows.routine_steps import RoutineStepContext, guardian_watch_run
from src.workflows.routine_templates import (
    ROUTINE_STEP_IDS,
    render_runbook,
    render_workflow,
    validate_generated_files,
)
from src.workflows.routines import (
    RoutineError,
    RoutineInstallRequest,
    RoutineService,
    ROUTINE_PACK_RUNBOOK_REFERENCE,
    _child_job_id,
    _expected_publication_job_id,
    _publication_binding_checkpoint,
    _verified_readback,
)


ROUTINE_ID = "0123456789abcdef0123456789abcdef"


def _async_value(value):
    async def _value():
        return value

    return _value()


@asynccontextmanager
async def _local_table_database(model):
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(model.__table__.create)

    @asynccontextmanager
    async def _get_session():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    try:
        yield _get_session
    finally:
        await engine.dispose()


def _locked_install_job(routine_id: str, version: GuardianRoutineVersion) -> dict:
    job_id = f"routine-install:{routine_id}:v{version.version}"
    return {
        "job_id": job_id,
        "status": "running",
        "revision": 7,
        "lease": {
            "owner": f"routine:{job_id}",
            "fencing_token": 1,
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        },
        "declared_authority": {
            "routine_id": routine_id,
            "routine_version": int(version.version),
            "workflow_sha256": version.workflow_sha256,
            "runbook_sha256": version.runbook_sha256,
            "source_provenance_sha256": hashlib.sha256(
                version.source_provenance_json.encode("utf-8")
            ).hexdigest(),
        },
    }


def test_routine_template_is_fixed_order_and_not_user_invocable():
    workflow = render_workflow(routine_id=ROUTINE_ID, version=1, name="Research follow-through")
    runbook = render_runbook(routine_id=ROUTINE_ID, version=1, name="Research follow-through")
    result = validate_generated_files(
        workflow=workflow,
        runbook=runbook,
        routine_id=ROUTINE_ID,
        version=1,
    )
    assert result["valid"] is True
    assert result["step_order"] == list(ROUTINE_STEP_IDS)
    assert "user_invocable: false" in workflow
    assert "command:" not in runbook
    assert "routine_invocation_job_id" in workflow


def test_verified_readback_requires_successful_durable_job():
    assert not _verified_readback({"status": "succeeded", "effects": []})
    assert not _verified_readback(
        {
            "status": "succeeded",
            "effects": [{"receipt_kind": "readback", "status": "unknown", "reconciled": False}],
        }
    )
    assert _verified_readback(
        {
            "status": "succeeded",
            "effects": [{"receipt_kind": "readback", "status": "succeeded", "reconciled": True}],
        }
    )


@pytest.mark.asyncio
async def test_generic_routine_step_without_trusted_context_fails_closed():
    with pytest.raises(PermissionError):
        await guardian_watch_run(
            "routine-invocation:job",
            child_job_id="routine-child:watch",
            context=None,
        )


@pytest.mark.asyncio
async def test_guardian_watch_step_binds_parent_context_and_dispatches_exact_child():
    service = SimpleNamespace(execute_watch_step=AsyncMock(return_value={"status": "succeeded"}))
    context = RoutineStepContext(
        principal_id="owner",
        session_id="session",
        lease_owner="routine-child:watch",
        fencing_token=3,
        runtime_job_id="routine-parent:invocation",
    )

    result = await guardian_watch_run(
        "routine-parent:invocation",
        child_job_id="routine-child:watch",
        context=context,
        service=service,
    )

    assert result == {"status": "succeeded"}
    service.execute_watch_step.assert_awaited_once_with("routine-child:watch", context=context)
    with pytest.raises(PermissionError, match="parent mismatch"):
        await guardian_watch_run(
            "other-parent",
            child_job_id="routine-child:watch",
            context=context,
            service=service,
        )


def test_routine_service_is_single_existing_job_surface():
    assert isinstance(RoutineService(), RoutineService)


def test_routine_children_are_uuid5_bound_to_invocation_and_step():
    invocation = "01234567-89ab-cdef-0123-456789abcdef"
    watch = _child_job_id(invocation, "watch")
    publication = _child_job_id(invocation, "publication")
    assert watch == _child_job_id(invocation, "watch")
    assert watch.startswith("routine-child:")
    assert publication.startswith("routine-child:")
    assert watch != publication


def test_publication_adoption_checkpoint_prefers_newest_binding():
    job = {
        "checkpoints": [
            {
                "checkpoint_id": "routine-child:adoption_pending",
                "payload": {"m3_job_id": "expected-m3", "status": "prepare_pending"},
            },
            {
                "checkpoint_id": "routine-child:prepared",
                "payload": {"m3_job_id": "expected-m3", "status": "awaiting_approval"},
            },
        ]
    }
    assert _publication_binding_checkpoint(job) == {
        "m3_job_id": "expected-m3",
        "status": "awaiting_approval",
    }


def test_publication_m3_job_identity_is_deterministic():
    operation_uuid = "01234567-89ab-cdef-0123-456789abcdef"
    expected = _expected_publication_job_id("principal-1", operation_uuid)
    assert expected.startswith("ghfollow_")
    assert expected == _expected_publication_job_id("principal-1", operation_uuid)


@pytest.mark.asyncio
async def test_install_replay_with_stale_revision_reaches_committed_reconciliation(monkeypatch):
    service = RoutineService()
    routine = SimpleNamespace(
        id=ROUTINE_ID,
        owner_session_id="session-1",
        revision=3,
        state="installed",
    )
    version = SimpleNamespace(
        version=1,
        source_provenance_json="{}",
        installed_package_digest="package-digest",
    )
    job = {
        "job_id": f"routine-install:{ROUTINE_ID}:v1",
        "job_kind": "routine_install",
        "status": "running",
        "declared_authority": {"routine_id": ROUTINE_ID, "routine_version": 1},
    }
    reconciled = {"status": "installed", "recovery": "reconciled"}

    async def fake_routine(*_args, **_kwargs):
        return routine

    async def fake_version(*_args, **_kwargs):
        return version

    async def fake_reconcile(*_args, **_kwargs):
        return reconciled

    class FakeJobs:
        async def get_job(self, _job_id):
            return job

    import src.workflows.routines as routines_module

    monkeypatch.setattr(service, "_routine", fake_routine)
    monkeypatch.setattr(service, "_version", fake_version)
    monkeypatch.setattr(service, "_reconcile_committed_install", fake_reconcile)
    monkeypatch.setattr(routines_module, "durable_job_repository", FakeJobs())

    result = await service.install(
        ROUTINE_ID,
        RoutineInstallRequest(version=1, expected_routine_revision=2, approval_id="approval-1"),
        owner_principal_id="principal-1",
        owner_session_id="session-1",
    )
    assert result == reconciled


def test_routine_template_does_not_expose_arbitrary_step_arguments():
    workflow = render_workflow(routine_id=ROUTINE_ID, version=2, name="Guarded")
    assert "command" not in workflow
    assert "routine_invocation_job_id" in workflow
    assert "url" not in workflow
    assert workflow.index("guardian_watch_run") < workflow.index("github_followthrough")


def test_generated_routine_steps_are_available_through_native_loader():
    from src.native_tools.loader import reload_tools

    names = {item.name for item in reload_tools()}
    assert {"guardian_watch_run", "github_followthrough"}.issubset(names)


@pytest.mark.asyncio
async def test_generated_step_rejects_context_bound_to_another_parent():
    with pytest.raises(PermissionError, match="runtime parent"):
        await RoutineService().execute_generated_step(
            "routine-invocation-target",
            "guardian_watch_run",
            context=RoutineStepContext(
                "principal-1",
                "session-1",
                "runner-1",
                7,
                runtime_job_id="routine-invocation-other",
            ),
        )


@pytest.mark.asyncio
async def test_routine_resume_requires_persisted_approval_owner_session(monkeypatch):
    import src.workflows.routines as routines_module

    monkeypatch.setattr(
        routines_module.approval_repository,
        "get",
        lambda _approval_id: _async_value(
            SimpleNamespace(
                status="approved",
                owner_principal_id="principal-1",
                operator_session_id="session-owner",
                details_json=json.dumps({"durable_job_id": "routine-install-1", "expires_at": 4_000_000_000}),
            )
        ),
    )
    with pytest.raises(RoutineError, match="approval_owner_session_mismatch"):
        await RoutineService()._resume_approval(
            {"job_id": "routine-install-1"},
            "approval-1",
            owner_principal_id="principal-1",
            owner_session_id="session-other",
        )


@pytest.mark.asyncio
async def test_routine_install_revoke_wins_before_approval_consumption_or_package_write(monkeypatch):
    service = RoutineService()
    routine_id = "routine-install-revoke-race"
    owner = "principal-install-race"
    session = "session-install-race"
    routine_before = SimpleNamespace(
        id=routine_id,
        owner_session_id=session,
        owner_principal_id=owner,
        revision=1,
        state="prepared",
        current_version=None,
    )
    routine_after = SimpleNamespace(
        id=routine_id,
        owner_session_id=session,
        owner_principal_id=owner,
        revision=2,
        state="revoked",
        current_version=None,
    )
    version = SimpleNamespace(
        version=1,
        workflow_bytes="workflow bytes",
        runbook_bytes="runbook bytes",
        workflow_sha256=hashlib.sha256(b"workflow bytes").hexdigest(),
        runbook_sha256=hashlib.sha256(b"runbook bytes").hexdigest(),
        source_provenance_json="{}",
        installed_package_digest=None,
    )
    job = {
        "job_id": f"routine-install:{routine_id}:v1",
        "status": "awaiting_approval",
        "revision": 4,
        "owner": {"kind": "user", "principal_id": owner},
        "operator_session_id": session,
        "goal_id": "goal-install-race",
        "goal_revision": 1,
        "plan_revision": 1,
        "capability_version": "guardian-routine.v1",
        "authority_digest": "authority-install-race",
        "budget_digest": "budget-install-race",
        "declared_authority": {
            "routine_id": routine_id,
            "routine_version": 1,
            "workflow_sha256": version.workflow_sha256,
            "runbook_sha256": version.runbook_sha256,
            "source_provenance_sha256": hashlib.sha256(b"{}").hexdigest(),
        },
        "lease": {"owner": None, "fencing_token": 0},
    }
    approval = SimpleNamespace(
        status="approved",
        owner_principal_id=owner,
        operator_session_id=session,
        details_json=json.dumps(
            {
                "durable_job_id": job["job_id"],
                "approval_expires_at": 4_000_000_000,
            }
        ),
    )
    resume = AsyncMock(side_effect=AssertionError("revoked routine must not consume approval"))

    monkeypatch.setattr(service, "_routine", AsyncMock(side_effect=[routine_before, routine_after]))
    monkeypatch.setattr(service, "_version", AsyncMock(return_value=version))
    monkeypatch.setattr(
        "src.workflows.routines.approval_repository.get",
        lambda _approval_id: _async_value(approval),
    )
    monkeypatch.setattr("src.workflows.routines.durable_job_repository.get_job", lambda _job_id: _async_value(job))
    monkeypatch.setattr("src.workflows.routines.durable_job_repository.resume_approved_job", resume)
    save = AsyncMock(side_effect=AssertionError("revoked routine must not write package files"))
    monkeypatch.setattr("src.workflows.routines.save_workspace_contribution", save)

    with pytest.raises(RoutineError, match="routine_revoked_terminal"):
        await service.install(
            routine_id,
            RoutineInstallRequest(version=1, expected_routine_revision=1, approval_id="approval-install-race"),
            owner_principal_id=owner,
            owner_session_id=session,
        )

    resume.assert_not_awaited()
    save.assert_not_awaited()


def _patch_locked_install_files(monkeypatch, tmp_path):
    import src.workflows.routines as routines_module

    workspace_root = tmp_path / "workspace-capabilities"
    staging_root = tmp_path / "routine-install-staging"
    package_root = staging_root / "package"
    writes: list[object] = []
    published_writes: list[object] = []

    monkeypatch.setattr(
        routines_module,
        "workspace_capability_package_root",
        lambda: workspace_root,
    )
    monkeypatch.setattr(
        routines_module,
        "_routine_pack_root",
        lambda _routine_id, _version: tmp_path / "published-routine-pack",
    )
    monkeypatch.setattr(
        routines_module,
        "_routine_install_staging_root",
        lambda _routine_id, _version: staging_root,
    )

    def save(contribution_type, *, file_name, content, **_kwargs):
        path = workspace_root / contribution_type / file_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        published_writes.append(path)
        return path

    def create_once(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            assert path.read_text(encoding="utf-8") == content
        else:
            path.write_text(content, encoding="utf-8")
        writes.append(path)

    def materialize(_service, _routine_id, _version, *, allow_create, root_override=None):
        manifest_content = "manifest"
        runbook_content = "package-runbook"
        root = Path(root_override or package_root)
        manifest_path = root / "manifest.yaml"
        runbook_path = root / ROUTINE_PACK_RUNBOOK_REFERENCE
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        runbook_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(manifest_content, encoding="utf-8")
        runbook_path.write_text(runbook_content, encoding="utf-8")
        return {
            "root": root,
            "manifest_content": manifest_content,
            "runbook_content": runbook_content,
            "digest": "package-digest",
        }

    monkeypatch.setattr(routines_module, "save_workspace_contribution", save)
    monkeypatch.setattr(routines_module, "_create_once_workspace_text", create_once)
    monkeypatch.setattr(
        routines_module,
        "_read_workspace_text_bounded",
        lambda path, **_kwargs: (path.read_text(encoding="utf-8"), False),
    )
    monkeypatch.setattr(routines_module, "validate_generated_files", lambda **_kwargs: {"valid": True})
    monkeypatch.setattr(RoutineService, "_materialize_routine_package", materialize)
    return workspace_root, package_root, writes, published_writes


def _locked_install_rows(routine_id: str = "routine-lock-race"):
    version = GuardianRoutineVersion(
        id="routine-lock-version",
        routine_id=routine_id,
        version=1,
        source_provenance_json="{}",
        workflow_bytes="workflow",
        workflow_sha256=hashlib.sha256(b"workflow").hexdigest(),
        runbook_bytes="runbook",
        runbook_sha256=hashlib.sha256(b"runbook").hexdigest(),
    )
    routine = GuardianRoutine(
        id=routine_id,
        owner_principal_id="principal-lock",
        owner_session_id="session-lock",
        name="Lock race",
        state="prepared",
        revision=1,
        current_version=None,
    )
    return routine, version, _locked_install_job(routine_id, version)


def _durable_install_lease(job: dict) -> SimpleNamespace:
    lease = job.get("lease") if isinstance(job.get("lease"), dict) else {}
    expires_at = lease.get("expires_at") or (datetime.now(timezone.utc) + timedelta(minutes=5))
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    return SimpleNamespace(
        status=job.get("status"),
        lease_owner=lease.get("owner"),
        fencing_token=lease.get("fencing_token"),
        lease_expires_at=expires_at,
    )


@pytest.mark.asyncio
async def test_install_lock_observes_revoke_winner_before_any_package_write(monkeypatch, tmp_path):
    import src.workflows.routines as routines_module

    workspace_root, package_root, writes, published_writes = _patch_locked_install_files(monkeypatch, tmp_path)
    routine, version, job = _locked_install_rows()
    routine.state = "revoked"
    routine.revision = 2
    events: list[str] = []

    class FakeResult:
        def __init__(self, row=None, rowcount=0):
            self.row = row
            self.rowcount = rowcount

        def scalars(self):
            return self

        def first(self):
            return self.row

    class FakeDb:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def execute(self, statement):
            sql = str(statement).lower()
            if "begin immediate" in sql:
                events.append("lock")
                return FakeResult()
            if sql.startswith("select") and "guardian_routines" in sql:
                return FakeResult(routine)
            if sql.startswith("select") and "guardian_routine_versions" in sql:
                return FakeResult(version)
            if sql.startswith("select") and "workflow_run_states" in sql:
                return FakeResult(
                    SimpleNamespace(
                        status=job["status"],
                        lease_owner=job["lease"]["owner"],
                        fencing_token=job["lease"]["fencing_token"],
                        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                    )
                )
            raise AssertionError(f"unexpected statement: {statement}")

    db = FakeDb()

    @asynccontextmanager
    async def get_session():
        try:
            yield db
        except Exception:
            raise
        else:
            await db.commit()

    monkeypatch.setattr(routines_module.db_engine, "get_session", get_session)
    with pytest.raises(RoutineError, match="routine_revoked_terminal"):
        await RoutineService()._install_package_under_routine_lock(
            job=job,
            routine_id=routine.id,
            version_number=1,
            expected_routine_revision=1,
            owner_principal_id=routine.owner_principal_id,
            owner_session_id=routine.owner_session_id,
            workflow_name="lock-race.md",
            runbook_name="lock-race.yaml",
        )

    assert writes == []
    assert published_writes == []
    assert not any(path.is_file() for path in workspace_root.rglob("*"))
    assert not any(path.is_file() for path in package_root.rglob("*"))


@pytest.mark.asyncio
async def test_install_lock_commits_package_before_revoke_can_commit(monkeypatch, tmp_path):
    import src.workflows.routines as routines_module

    workspace_root, package_root, writes, published_writes = _patch_locked_install_files(monkeypatch, tmp_path)
    routine, version, job = _locked_install_rows()
    events: list[str] = []
    revoke_outcome: list[str] = []

    class FakeResult:
        def __init__(self, row=None, rowcount=0):
            self.row = row
            self.rowcount = rowcount

        def scalars(self):
            return self

        def first(self):
            return self.row

    class FakeDb:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def execute(self, statement):
            sql = str(statement).lower()
            if "begin immediate" in sql:
                events.append("lock")
                return FakeResult()
            if sql.startswith("select") and "guardian_routines" in sql:
                return FakeResult(routine)
            if sql.startswith("select") and "guardian_routine_versions" in sql:
                return FakeResult(version)
            if sql.startswith("select") and "workflow_run_states" in sql:
                return FakeResult(
                    SimpleNamespace(
                        status=job["status"],
                        lease_owner=job["lease"]["owner"],
                        fencing_token=job["lease"]["fencing_token"],
                        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                    )
                )
            if sql.startswith("update guardian_routine_versions"):
                version.installed_package_digest = "package-digest"
                return FakeResult(rowcount=1)
            if sql.startswith("update guardian_routines"):
                routine.state = "installed"
                routine.revision = 2
                return FakeResult(rowcount=1)
            raise AssertionError(f"unexpected statement: {statement}")

        async def commit(self):
            events.append("install_commit")
            if "revoke_waiting" in events:
                # A revoke that was waiting on this writer lock now loses its
                # expected-revision CAS and cannot overwrite the installation.
                revoke_outcome.append("stale")

    db = FakeDb()

    @asynccontextmanager
    async def get_session():
        try:
            yield db
        except Exception:
            raise
        else:
            await db.commit()

    monkeypatch.setattr(routines_module.db_engine, "get_session", get_session)
    original_create = routines_module._create_once_workspace_text

    def create_and_request_revoke(*args, **kwargs):
        if "revoke_waiting" not in events:
            events.append("revoke_waiting")
        events.append("file_write")
        return original_create(*args, **kwargs)

    monkeypatch.setattr(routines_module, "_create_once_workspace_text", create_and_request_revoke)
    result = await RoutineService()._install_package_under_routine_lock(
        job=job,
        routine_id=routine.id,
        version_number=1,
        expected_routine_revision=1,
        owner_principal_id=routine.owner_principal_id,
        owner_session_id=routine.owner_session_id,
        workflow_name="lock-race.md",
        runbook_name="lock-race.yaml",
    )

    assert [path.name for path in writes] == ["lock-race.md", "lock-race.yaml"]
    assert result["package_digest"] == "package-digest"
    assert revoke_outcome == ["stale"]
    assert events.index("lock") < events.index("revoke_waiting")
    assert events.index("revoke_waiting") < events.index("install_commit")
    assert routine.state == "installed"
    assert routine.revision == 2
    assert version.installed_package_digest == "package-digest"
    assert all(path.is_file() for path in writes)
    assert published_writes == []
    assert (workspace_root / "manifest.yaml").exists() is False
    assert (package_root / "manifest.yaml").read_text(encoding="utf-8") == "manifest"


@pytest.mark.asyncio
async def test_install_lock_real_sqlite_writer_fence_serializes_revoke(monkeypatch, tmp_path):
    """A real SQLite writer cannot revoke between the locked read and commit."""

    import src.workflows.routines as routines_module

    workspace_root, package_root, writes, published_writes = _patch_locked_install_files(monkeypatch, tmp_path)
    routine, version, job = _locked_install_rows("0123456789abcdef0123456789abcdee")
    database_path = tmp_path / "routine-lock-race.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 5},
        pool_size=5,
        max_overflow=5,
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: SQLModel.metadata.create_all(
                sync,
                tables=[GuardianRoutine.__table__, GuardianRoutineVersion.__table__, WorkflowRunState.__table__],
            )
        )
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        db.add(routine)
        db.add(version)
        db.add(
            WorkflowRunState(
                run_identity=job["job_id"],
                root_run_identity=job["job_id"],
                workflow_name="routine_install",
                tool_name="guardian:routine-install",
                status="running",
                job_kind="routine_install",
                owner_kind="user",
                owner_principal_id=routine.owner_principal_id,
                operator_session_id=routine.owner_session_id,
                declared_authority_json=json.dumps(job["declared_authority"], sort_keys=True),
                lease_owner=job["lease"]["owner"],
                lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                fencing_token=job["lease"]["fencing_token"],
                revision=job["revision"],
            )
        )
        await db.commit()

    revoke_task: asyncio.Task[int] | None = None

    async def revoke_after_install_lock() -> int:
        async with factory() as db:
            result = await db.execute(
                update(GuardianRoutine)
                .where(
                    GuardianRoutine.id == routine.id,
                    GuardianRoutine.revision == 1,
                )
                .values(state="revoked", revision=2)
            )
            await db.commit()
            return int(result.rowcount or 0)

    original_create = routines_module._create_once_workspace_text

    def create_and_queue_revoke(path, content):
        nonlocal revoke_task
        if revoke_task is None:
            revoke_task = asyncio.create_task(revoke_after_install_lock())
        original_create(path, content)

    monkeypatch.setattr(routines_module, "_create_once_workspace_text", create_and_queue_revoke)
    @asynccontextmanager
    async def get_session():
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    monkeypatch.setattr(routines_module.db_engine, "get_session", get_session)
    try:
        result = await RoutineService()._install_package_under_routine_lock(
            job=job,
            routine_id=routine.id,
            version_number=1,
            expected_routine_revision=1,
            owner_principal_id=routine.owner_principal_id,
            owner_session_id=routine.owner_session_id,
            workflow_name="lock-race.md",
            runbook_name="lock-race.yaml",
        )
        assert result["package_digest"] == "package-digest"
        assert revoke_task is not None
        assert await revoke_task == 0
        async with factory() as db:
            current = (await db.execute(select(GuardianRoutine).where(GuardianRoutine.id == routine.id))).scalars().one()
            assert current.state == "installed"
            assert current.revision == 2
        assert [path.name for path in writes] == ["lock-race.md", "lock-race.yaml"]
        assert published_writes == []
        assert (workspace_root / "manifest.yaml").exists() is False
        assert (package_root / "manifest.yaml").read_text(encoding="utf-8") == "manifest"
    finally:
        if revoke_task is not None and not revoke_task.done():
            revoke_task.cancel()
        await engine.dispose()


@pytest.mark.asyncio
async def test_install_lock_rejects_reclaimed_durable_job_fence_before_selector_write(monkeypatch, tmp_path):
    import src.workflows.routines as routines_module

    workspace_root, package_root, writes, published_writes = _patch_locked_install_files(monkeypatch, tmp_path)
    routine, version, job = _locked_install_rows("0123456789abcdef0123456789abcddc")

    class FakeResult:
        def __init__(self, row=None, rowcount=0):
            self.row = row
            self.rowcount = rowcount

        def scalars(self):
            return self

        def first(self):
            return self.row

    class FakeDb:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def execute(self, statement):
            sql = str(statement).lower()
            if "begin immediate" in sql:
                return FakeResult()
            if sql.startswith("select") and "guardian_routines" in sql:
                return FakeResult(routine)
            if sql.startswith("select") and "guardian_routine_versions" in sql:
                return FakeResult(version)
            if sql.startswith("select") and "workflow_run_states" in sql:
                return FakeResult(
                    SimpleNamespace(
                        status="running",
                        lease_owner="reclaimed-worker",
                        fencing_token=2,
                        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                    )
                )
            raise AssertionError(f"unexpected statement: {statement}")

    @asynccontextmanager
    async def get_session():
        yield FakeDb()

    monkeypatch.setattr(routines_module.db_engine, "get_session", get_session)
    with pytest.raises(RoutineError, match="routine_install_job_fence_stale"):
        await RoutineService()._install_package_under_routine_lock(
            job=job,
            routine_id=routine.id,
            version_number=1,
            expected_routine_revision=1,
            owner_principal_id=routine.owner_principal_id,
            owner_session_id=routine.owner_session_id,
            workflow_name="lock-race.md",
            runbook_name="lock-race.yaml",
        )
    assert writes == []
    assert published_writes == []
    assert not any(path.is_file() for path in workspace_root.rglob("*"))
    assert not any(path.is_file() for path in package_root.rglob("*"))


class _RecoveryResult:
    def __init__(self, rows):
        self.rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)


@pytest.mark.asyncio
async def test_recover_pending_install_removes_uncommitted_staging_without_live_files(monkeypatch, tmp_path):
    import src.workflows.routines as routines_module

    token = "abcdefabcdefabcdefabcdefabcdefab"
    staging_root = tmp_path / "routine-install-staging"
    tree = staging_root / token / "v1"
    (tree / "workspace" / "workflows").mkdir(parents=True)
    (tree / "workspace" / "workflows" / "partial.md").write_text("partial", encoding="utf-8")
    (tree / "package").mkdir()
    routine = GuardianRoutine(
        id=token,
        owner_principal_id="principal-recovery",
        owner_session_id="session-recovery",
        state="prepared",
        current_version=None,
        revision=1,
    )
    version = GuardianRoutineVersion(
        routine_id=token,
        version=1,
        workflow_bytes="workflow",
        workflow_sha256=hashlib.sha256(b"workflow").hexdigest(),
        runbook_bytes="runbook",
        runbook_sha256=hashlib.sha256(b"runbook").hexdigest(),
        source_provenance_json="{}",
        installed_package_digest=None,
    )

    class FakeDb:
        async def execute(self, statement):
            sql = str(statement).lower()
            if "guardian_routines" in sql:
                return _RecoveryResult([routine])
            if "guardian_routine_versions" in sql:
                return _RecoveryResult([version])
            raise AssertionError(statement)

        def expunge(self, _row):
            return None

    @asynccontextmanager
    async def get_session():
        yield FakeDb()

    monkeypatch.setattr(routines_module, "_safe_resolve", lambda _path: staging_root)
    monkeypatch.setattr(routines_module.db_engine, "get_session", get_session)
    publish = AsyncMock(side_effect=AssertionError("uncommitted staging must not publish"))
    monkeypatch.setattr(RoutineService, "_publish_staged_install", publish)

    receipts = await RoutineService().recover_pending_installs()

    assert receipts == [
        {
            "status": "cleaned",
            "reason_code": "routine_install_uncommitted_staging",
            "routine_id": token,
            "version": 1,
        }
    ]
    assert not tree.exists()
    assert not (tmp_path / "workflows").exists()
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_recover_pending_install_republishes_canonical_commit(monkeypatch, tmp_path):
    import src.workflows.routines as routines_module

    token = "fedcbafedcbafedcbafedcbafedcbafe"
    staging_root = tmp_path / "routine-install-staging"
    tree = staging_root / token / "v1"
    (tree / "workspace" / "workflows").mkdir(parents=True)
    (tree / "workspace" / "runbooks").mkdir(parents=True)
    (tree / "workspace" / "workflows" / "routine.md").write_text("workflow", encoding="utf-8")
    (tree / "workspace" / "runbooks" / "routine.yaml").write_text("runbook", encoding="utf-8")
    (tree / "package").mkdir()
    routine = GuardianRoutine(
        id=token,
        owner_principal_id="principal-recovery",
        owner_session_id="session-recovery",
        state="installed",
        current_version=1,
        revision=2,
    )
    version = GuardianRoutineVersion(
        routine_id=token,
        version=1,
        workflow_bytes="workflow",
        workflow_sha256=hashlib.sha256(b"workflow").hexdigest(),
        runbook_bytes="runbook",
        runbook_sha256=hashlib.sha256(b"runbook").hexdigest(),
        source_provenance_json="{}",
        installed_package_digest="package-digest",
    )

    class FakeDb:
        async def execute(self, statement):
            sql = str(statement).lower()
            if "guardian_routines" in sql:
                return _RecoveryResult([routine])
            if "guardian_routine_versions" in sql:
                return _RecoveryResult([version])
            raise AssertionError(statement)

        def expunge(self, _row):
            return None

    @asynccontextmanager
    async def get_session():
        db = FakeDb()
        yield db

    monkeypatch.setattr(routines_module, "_safe_resolve", lambda _path: staging_root)
    monkeypatch.setattr(routines_module.db_engine, "get_session", get_session)
    job_id = f"routine-install:{token}:v1"

    class FakeJobs:
        async def get_job(self, requested_job_id):
            assert requested_job_id == job_id
            return {"job_id": job_id, "status": "running"}

    service = RoutineService()
    monkeypatch.setattr(routines_module, "durable_job_repository", FakeJobs())

    async def reconcile(*_args, **_kwargs):
        routines_module._cleanup_install_staging_root(tree)
        return {"status": "installed"}

    monkeypatch.setattr(service, "_reconcile_committed_install", reconcile)

    receipts = await service.recover_pending_installs()

    assert receipts == [
        {
            "status": "recovered",
            "reason_code": "routine_install_publication_reconciled",
            "routine_id": token,
            "version": 1,
        }
    ]
    assert not tree.exists()


@pytest.mark.asyncio
async def test_recover_pending_install_preserves_malformed_staging(monkeypatch, tmp_path):
    import src.workflows.routines as routines_module

    staging_root = tmp_path / "routine-install-staging"
    malformed = staging_root / "not-a-routine" / "v1"
    malformed.mkdir(parents=True)
    monkeypatch.setattr(routines_module, "_safe_resolve", lambda _path: staging_root)

    receipts = await RoutineService().recover_pending_installs()

    assert receipts == []
    assert malformed.exists()


@pytest.mark.asyncio
async def test_publication_lock_rechecks_committed_revoke_before_any_public_file(monkeypatch, tmp_path):
    import src.workflows.routines as routines_module

    workspace_root, package_root, _writes, published_writes = _patch_locked_install_files(monkeypatch, tmp_path)
    routine, version, job = _locked_install_rows()
    routine.state = "revoked"
    routine.current_version = 1
    routine.revision = 3
    version.installed_package_digest = "package-digest"
    staging_root = tmp_path / "routine-install-staging"
    (staging_root / "workspace" / "workflows").mkdir(parents=True)
    (staging_root / "workspace" / "runbooks").mkdir(parents=True)
    (staging_root / "workspace" / "workflows" / "lock-race.md").write_text("workflow", encoding="utf-8")
    (staging_root / "workspace" / "runbooks" / "lock-race.yaml").write_text("runbook", encoding="utf-8")
    (staging_root / "package").mkdir()

    class FakeResult:
        def __init__(self, row=None):
            self.row = row

        def scalars(self):
            return self

        def first(self):
            return self.row

    class FakeDb:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def execute(self, statement):
            sql = str(statement).lower()
            if "begin immediate" in sql:
                return FakeResult()
            if sql.startswith("select") and "workflow_run_states" in sql:
                return FakeResult(_durable_install_lease(job))
            if sql.startswith("select") and "guardian_routines" in sql:
                return FakeResult(routine)
            if sql.startswith("select") and "guardian_routine_versions" in sql:
                return FakeResult(version)
            raise AssertionError(statement)

    @asynccontextmanager
    async def get_session():
        yield FakeDb()

    monkeypatch.setattr(routines_module.db_engine, "get_session", get_session)

    with pytest.raises(RoutineError, match="routine_install_publication_stale"):
        await RoutineService()._publish_staged_install_under_routine_lock(
            job=job,
            routine_id=routine.id,
            version_number=1,
            owner_principal_id=routine.owner_principal_id,
            owner_session_id=routine.owner_session_id,
            staging_root=staging_root,
            package_digest="package-digest",
            workflow_name="lock-race.md",
            runbook_name="lock-race.yaml",
        )

    assert published_writes == []
    assert not any(path.is_file() for path in workspace_root.rglob("*"))
    assert not any(path.is_file() for path in package_root.rglob("*"))
    assert staging_root.exists()


@pytest.mark.asyncio
async def test_publication_rejects_reclaimed_durable_job_fence_in_real_sqlite_before_public_write(
    monkeypatch,
    tmp_path,
):
    import src.workflows.routines as routines_module

    workspace_root, package_root, _writes, published_writes = _patch_locked_install_files(monkeypatch, tmp_path)
    routine, version, job = _locked_install_rows("routine-publish-stale-fence")
    routine.state = "installed"
    routine.current_version = 1
    routine.revision = 2
    version.installed_package_digest = "package-digest"
    staging_root = tmp_path / "routine-install-staging"
    (staging_root / "workspace" / "workflows").mkdir(parents=True)
    (staging_root / "workspace" / "runbooks").mkdir(parents=True)
    (staging_root / "workspace" / "workflows" / "lock-race.md").write_text("workflow", encoding="utf-8")
    (staging_root / "workspace" / "runbooks" / "lock-race.yaml").write_text("runbook", encoding="utf-8")
    (staging_root / "package").mkdir()
    database_path = tmp_path / "publication-stale-fence.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: SQLModel.metadata.create_all(
                sync,
                tables=[GuardianRoutine.__table__, GuardianRoutineVersion.__table__, WorkflowRunState.__table__],
            )
        )
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        db.add(routine)
        db.add(version)
        db.add(
            WorkflowRunState(
                run_identity=job["job_id"],
                root_run_identity=job["job_id"],
                workflow_name="routine_install",
                tool_name="guardian:routine-install",
                status="running",
                job_kind="routine_install",
                owner_kind="user",
                owner_principal_id=routine.owner_principal_id,
                operator_session_id=routine.owner_session_id,
                declared_authority_json=json.dumps(job["declared_authority"], sort_keys=True),
                lease_owner="reclaimed-worker",
                lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                fencing_token=job["lease"]["fencing_token"] + 1,
                revision=job["revision"] + 1,
            )
        )
        await db.commit()

    @asynccontextmanager
    async def get_session():
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    monkeypatch.setattr(routines_module.db_engine, "get_session", get_session)
    try:
        with pytest.raises(RoutineError, match="routine_install_job_fence_stale"):
            await RoutineService()._publish_staged_install_under_routine_lock(
                job=job,
                routine_id=routine.id,
                version_number=1,
                owner_principal_id=routine.owner_principal_id,
                owner_session_id=routine.owner_session_id,
                staging_root=staging_root,
                package_digest="package-digest",
                workflow_name="lock-race.md",
                runbook_name="lock-race.yaml",
            )
        assert published_writes == []
        assert not any(path.is_file() for path in workspace_root.rglob("*"))
        assert not any(path.is_file() for path in package_root.rglob("*"))
        assert staging_root.exists()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_publication_lock_wins_before_revoke_and_commits_public_bytes(monkeypatch, tmp_path):
    import src.workflows.routines as routines_module

    workspace_root, _package_root, _writes, published_writes = _patch_locked_install_files(monkeypatch, tmp_path)
    routine, version, job = _locked_install_rows()
    routine.state = "installed"
    routine.current_version = 1
    routine.revision = 2
    version.installed_package_digest = "package-digest"
    staging_root = tmp_path / "routine-install-staging"
    (staging_root / "workspace" / "workflows").mkdir(parents=True)
    (staging_root / "workspace" / "runbooks").mkdir(parents=True)
    (staging_root / "workspace" / "workflows" / "lock-race.md").write_text("workflow", encoding="utf-8")
    (staging_root / "workspace" / "runbooks" / "lock-race.yaml").write_text("runbook", encoding="utf-8")
    (staging_root / "package").mkdir()
    events: list[str] = []
    revoke_outcome: list[str] = []

    class FakeResult:
        def __init__(self, row=None):
            self.row = row

        def scalars(self):
            return self

        def first(self):
            return self.row

    class FakeDb:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def execute(self, statement):
            sql = str(statement).lower()
            if "begin immediate" in sql:
                events.append("publication_lock")
                return FakeResult()
            if sql.startswith("select") and "workflow_run_states" in sql:
                return FakeResult(_durable_install_lease(job))
            if sql.startswith("select") and "guardian_routines" in sql:
                return FakeResult(routine)
            if sql.startswith("select") and "guardian_routine_versions" in sql:
                return FakeResult(version)
            raise AssertionError(statement)

        async def commit(self):
            events.append("publication_commit")
            if "revoke_waiting" in events:
                revoke_outcome.append("stale")

    @asynccontextmanager
    async def get_session():
        db = FakeDb()
        yield db
        await db.commit()

    monkeypatch.setattr(routines_module.db_engine, "get_session", get_session)
    original_save = routines_module.save_workspace_contribution

    def save_and_queue_revoke(*args, **kwargs):
        if "revoke_waiting" not in events:
            events.append("revoke_waiting")
        return original_save(*args, **kwargs)

    monkeypatch.setattr(routines_module, "save_workspace_contribution", save_and_queue_revoke)
    result = await RoutineService()._publish_staged_install_under_routine_lock(
        job=job,
        routine_id=routine.id,
        version_number=1,
        owner_principal_id=routine.owner_principal_id,
        owner_session_id=routine.owner_session_id,
        staging_root=staging_root,
        package_digest="package-digest",
        workflow_name="lock-race.md",
        runbook_name="lock-race.yaml",
    )

    assert result["package_digest"] == "package-digest"
    assert revoke_outcome == ["stale"]
    assert events.index("publication_lock") < events.index("revoke_waiting") < events.index("publication_commit")
    assert all(path.is_file() for path in published_writes)


@pytest.mark.asyncio
async def test_publication_failure_restores_manifest_and_keeps_private_staging(monkeypatch, tmp_path):
    import src.workflows.routines as routines_module

    workspace_root, _package_root, _writes, published_writes = _patch_locked_install_files(monkeypatch, tmp_path)
    routine, version, job = _locked_install_rows()
    routine.state = "installed"
    routine.current_version = 1
    routine.revision = 2
    version.installed_package_digest = "package-digest"
    staging_root = tmp_path / "routine-install-staging"
    (staging_root / "workspace" / "workflows").mkdir(parents=True)
    (staging_root / "workspace" / "runbooks").mkdir(parents=True)
    (staging_root / "workspace" / "workflows" / "lock-race.md").write_text("workflow", encoding="utf-8")
    (staging_root / "workspace" / "runbooks" / "lock-race.yaml").write_text("runbook", encoding="utf-8")
    (staging_root / "package").mkdir()
    manifest_path = workspace_root / "manifest.yaml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text("original-manifest", encoding="utf-8")

    class FakeResult:
        def __init__(self, row=None):
            self.row = row

        def scalars(self):
            return self

        def first(self):
            return self.row

    class FakeDb:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def execute(self, statement):
            sql = str(statement).lower()
            if "begin immediate" in sql:
                return FakeResult()
            if sql.startswith("select") and "workflow_run_states" in sql:
                return FakeResult(_durable_install_lease(job))
            if sql.startswith("select") and "guardian_routines" in sql:
                return FakeResult(routine)
            if sql.startswith("select") and "guardian_routine_versions" in sql:
                return FakeResult(version)
            raise AssertionError(statement)

        async def commit(self):
            raise AssertionError("publication must fail before commit")

    @asynccontextmanager
    async def get_session():
        db = FakeDb()
        try:
            yield db
        except Exception:
            raise
        else:
            await db.commit()

    monkeypatch.setattr(routines_module.db_engine, "get_session", get_session)
    original_save = routines_module.save_workspace_contribution
    save_calls = 0

    def fail_on_second_public_write(*args, **kwargs):
        nonlocal save_calls
        save_calls += 1
        if save_calls == 2:
            raise RuntimeError("simulated workspace contribution crash")
        return original_save(*args, **kwargs)

    monkeypatch.setattr(routines_module, "save_workspace_contribution", fail_on_second_public_write)
    with pytest.raises(RuntimeError, match="workspace contribution crash"):
        await RoutineService()._publish_staged_install_under_routine_lock(
            job=job,
            routine_id=routine.id,
            version_number=1,
            owner_principal_id=routine.owner_principal_id,
            owner_session_id=routine.owner_session_id,
            staging_root=staging_root,
            package_digest="package-digest",
            workflow_name="lock-race.md",
            runbook_name="lock-race.yaml",
        )

    assert save_calls == 2
    assert manifest_path.read_text(encoding="utf-8") == "original-manifest"
    assert all(not path.is_file() for path in published_writes)
    assert not any(path.is_file() for path in (workspace_root / "workflows").rglob("*"))
    assert staging_root.exists()


@pytest.mark.asyncio
async def test_publication_post_move_failure_restores_private_package_for_retry(monkeypatch, tmp_path):
    import src.workflows.routines as routines_module

    workspace_root, _staging_package_root, _writes, published_writes = _patch_locked_install_files(monkeypatch, tmp_path)
    routine, version, job = _locked_install_rows()
    routine.state = "installed"
    routine.current_version = 1
    routine.revision = 2
    version.installed_package_digest = "package-digest"
    staging_root = tmp_path / "routine-install-staging"
    (staging_root / "workspace" / "workflows").mkdir(parents=True)
    (staging_root / "workspace" / "runbooks").mkdir(parents=True)
    (staging_root / "workspace" / "workflows" / "lock-race.md").write_text("workflow", encoding="utf-8")
    (staging_root / "workspace" / "runbooks" / "lock-race.yaml").write_text("runbook", encoding="utf-8")
    (staging_root / "package").mkdir()

    class FakeResult:
        def __init__(self, row=None):
            self.row = row

        def scalars(self):
            return self

        def first(self):
            return self.row

    class FakeDb:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def execute(self, statement):
            sql = str(statement).lower()
            if "begin immediate" in sql:
                return FakeResult()
            if sql.startswith("select") and "workflow_run_states" in sql:
                return FakeResult(_durable_install_lease(job))
            if sql.startswith("select") and "guardian_routines" in sql:
                return FakeResult(routine)
            if sql.startswith("select") and "guardian_routine_versions" in sql:
                return FakeResult(version)
            raise AssertionError(statement)

    @asynccontextmanager
    async def get_session():
        yield FakeDb()

    monkeypatch.setattr(routines_module.db_engine, "get_session", get_session)
    service = RoutineService()
    original_verified = service._verified_public_install
    fail_once = True

    def fail_after_move(*args, **kwargs):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise RuntimeError("simulated post-move verification crash")
        return original_verified(*args, **kwargs)

    monkeypatch.setattr(service, "_verified_public_install", fail_after_move)

    with pytest.raises(RuntimeError, match="post-move verification crash"):
        await service._publish_staged_install_under_routine_lock(
            job=job,
            routine_id=routine.id,
            version_number=1,
            owner_principal_id=routine.owner_principal_id,
            owner_session_id=routine.owner_session_id,
            staging_root=staging_root,
            package_digest="package-digest",
            workflow_name="lock-race.md",
            runbook_name="lock-race.yaml",
        )

    assert (staging_root / "package").is_dir()
    assert (staging_root / "package" / "manifest.yaml").read_text(encoding="utf-8") == "manifest"
    assert (
        (staging_root / "package" / ROUTINE_PACK_RUNBOOK_REFERENCE).read_text(
            encoding="utf-8"
        )
        == "package-runbook"
    )
    published_package_root = tmp_path / "published-routine-pack"
    assert not published_package_root.exists()
    assert all(not path.is_file() for path in published_writes)

    published = await service._publish_staged_install_under_routine_lock(
        job=job,
        routine_id=routine.id,
        version_number=1,
        owner_principal_id=routine.owner_principal_id,
        owner_session_id=routine.owner_session_id,
        staging_root=staging_root,
        package_digest="package-digest",
        workflow_name="lock-race.md",
        runbook_name="lock-race.yaml",
    )

    assert published["package_digest"] == "package-digest"
    assert published_package_root.is_dir()
    assert (published_package_root / "manifest.yaml").is_file()
    assert (staging_root / "package").is_dir()
    assert all(path.is_file() for path in published_writes)


@pytest.mark.asyncio
async def test_publication_recovers_after_process_death_after_package_move(monkeypatch, tmp_path):
    import src.workflows.routines as routines_module

    workspace_root, _staging_package_root, _writes, published_writes = _patch_locked_install_files(monkeypatch, tmp_path)
    monkeypatch.setattr(routines_module.settings, "workspace_dir", str(tmp_path))
    routine, version, base_job = _locked_install_rows(ROUTINE_ID)
    routine.state = "installed"
    routine.current_version = 1
    routine.revision = 2
    version.installed_package_digest = "package-digest"
    staging_root = tmp_path / "routine-install-staging"
    tree = staging_root / ROUTINE_ID / "v1"
    (tree / "workspace" / "workflows").mkdir(parents=True)
    (tree / "workspace" / "runbooks").mkdir(parents=True)
    (tree / "workspace" / "workflows" / "lock-race.md").write_text("workflow", encoding="utf-8")
    (tree / "workspace" / "runbooks" / "lock-race.yaml").write_text("runbook", encoding="utf-8")
    (tree / "package").mkdir()
    monkeypatch.setattr(routines_module, "_safe_resolve", lambda _path: staging_root)
    monkeypatch.setattr(routines_module, "_routine_install_staging_root", lambda _id, _version: tree)
    monkeypatch.setattr(routines_module, "routine_slug", lambda _id, _version: "lock-race")
    durable_state = {"status": base_job["status"], "lease": dict(base_job["lease"])}

    class FakeResult:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = list(rows if rows is not None else ([row] if row is not None else []))

        def scalars(self):
            return self

        def first(self):
            return self.row

        def all(self):
            return list(self.rows)

    class FakeDb:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def execute(self, statement):
            sql = str(statement).lower()
            if "begin immediate" in sql:
                return FakeResult()
            if sql.startswith("select") and "workflow_run_states" in sql:
                return FakeResult(_durable_install_lease(durable_state))
            if sql.startswith("select") and "guardian_routines" in sql:
                return FakeResult(routine, [routine])
            if sql.startswith("select") and "guardian_routine_versions" in sql:
                return FakeResult(version, [version])
            raise AssertionError(statement)

        def expunge(self, _row):
            return None

    @asynccontextmanager
    async def get_session():
        yield FakeDb()

    monkeypatch.setattr(routines_module.db_engine, "get_session", get_session)
    service = RoutineService()
    original_verified = service._verified_public_install
    process_died = True

    def die_after_move(*args, **kwargs):
        nonlocal process_died
        if process_died:
            process_died = False
            raise KeyboardInterrupt("simulated process death after package move")
        return original_verified(*args, **kwargs)

    monkeypatch.setattr(service, "_verified_public_install", die_after_move)

    with pytest.raises(KeyboardInterrupt, match="process death"):
        await service._publish_staged_install_under_routine_lock(
            job=base_job,
            routine_id=routine.id,
            version_number=1,
            owner_principal_id=routine.owner_principal_id,
            owner_session_id=routine.owner_session_id,
            staging_root=tree,
            package_digest="package-digest",
            workflow_name="lock-race.md",
            runbook_name="lock-race.yaml",
        )

    published_package_root = tmp_path / "published-routine-pack"
    assert tree.exists()
    assert not (tree / "package").exists()
    assert published_package_root.is_dir()

    job_id = f"routine-install:{routine.id}:v1"
    job = {
        **base_job,
        "job_id": job_id,
        "job_kind": "routine_install",
        "status": "blocked",
        "revision": 3,
        "lease": {"owner": None, "fencing_token": 0},
        "artifacts": [],
        "effects": [],
    }

    class FakeJobs:
        async def get_job(self, requested_job_id):
            assert requested_job_id == job_id
            return {
                **job,
                "lease": dict(job["lease"]),
                "artifacts": list(job["artifacts"]),
                "effects": list(job["effects"]),
            }

        async def resume_job(self, requested_job_id, **_kwargs):
            assert requested_job_id == job_id and job["status"] == "blocked"
            job["status"] = "queued"
            job["revision"] += 1
            return await self.get_job(job_id)

        async def claim_job(self, requested_job_id, *, owner, **_kwargs):
            assert requested_job_id == job_id and job["status"] == "queued"
            job["status"] = "running"
            job["revision"] += 1
            job["lease"] = {
                "owner": owner,
                "fencing_token": 1,
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            }
            durable_state.update({"status": job["status"], "lease": dict(job["lease"])})
            return await self.get_job(job_id)

        async def record_artifact(self, requested_job_id, *, file_path, artifact_type, content, **_kwargs):
            assert requested_job_id == job_id
            job["revision"] += 1
            job["artifacts"].append(
                {
                    "file_path": file_path,
                    "artifact_type": artifact_type,
                    "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                }
            )
            return await self.get_job(job_id)

        async def record_effect(self, requested_job_id, **receipt):
            assert requested_job_id == job_id
            job["revision"] += 1
            job["effects"].append({**receipt, "receipt_kind": "effect", "status": "succeeded"})
            return await self.get_job(job_id)

        async def record_readback(self, requested_job_id, **receipt):
            assert requested_job_id == job_id
            job["revision"] += 1
            job["effects"].append({**receipt, "receipt_kind": "readback", "status": "succeeded"})
            return await self.get_job(job_id)

        async def transition_job(self, requested_job_id, status, **_kwargs):
            assert requested_job_id == job_id and job["status"] == "running"
            job["status"] = status
            job["revision"] += 1
            return await self.get_job(job_id)

    monkeypatch.setattr(routines_module, "durable_job_repository", FakeJobs())
    monkeypatch.setattr(service, "_package_readback", lambda *_args, **_kwargs: {"digest": "package-digest"})
    monkeypatch.setattr(service, "read", AsyncMock(return_value={"status": "installed"}))

    receipts = await service.recover_pending_installs()

    assert receipts == [
        {
            "status": "recovered",
            "reason_code": "routine_install_publication_reconciled",
            "routine_id": routine.id,
            "version": 1,
        }
    ]
    assert job["status"] == "succeeded"
    assert len(job["artifacts"]) == 4
    assert len([item for item in job["effects"] if item.get("receipt_kind") == "effect"]) == 4
    assert len([item for item in job["effects"] if item.get("receipt_kind") == "readback"]) == 4
    assert not tree.exists()
    assert published_package_root.is_dir()
    assert all(path.is_file() for path in published_writes)


@pytest.mark.asyncio
async def test_reconcile_committed_install_fills_missing_receipts_after_mid_receipt_failure(monkeypatch, tmp_path):
    import src.workflows.routines as routines_module

    monkeypatch.setattr(routines_module.settings, "workspace_dir", str(tmp_path))
    routine = GuardianRoutine(
        id=ROUTINE_ID,
        owner_principal_id="principal-receipt",
        owner_session_id="session-receipt",
        state="installed",
        current_version=1,
        revision=2,
    )
    version = GuardianRoutineVersion(
        routine_id=ROUTINE_ID,
        version=1,
        workflow_bytes="workflow",
        workflow_sha256=hashlib.sha256(b"workflow").hexdigest(),
        runbook_bytes="runbook",
        runbook_sha256=hashlib.sha256(b"runbook").hexdigest(),
        source_provenance_json="{}",
        installed_package_digest="package-digest",
    )
    public_root = tmp_path / "public"
    files = (
        (public_root / "workflow.md", "workflow", "routine_workflow"),
        (public_root / "runbook.yaml", "runbook", "routine_runbook"),
    )
    for path, content, _kind in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    staging_root = tmp_path / "artifacts" / "routine-install-staging" / ROUTINE_ID / "v1"
    staging_root.mkdir(parents=True)
    (staging_root / "marker").write_text("keep-until-terminal", encoding="utf-8")
    job_id = f"routine-install:{ROUTINE_ID}:v1"
    job_state: dict[str, Any] = {
        "job_id": job_id,
        "job_kind": "routine_install",
        "status": "running",
        "revision": 10,
        "lease": {
            "owner": "crashed-runner",
            "fencing_token": 4,
            "expires_at": "2000-01-01T00:00:00+00:00",
        },
        "declared_authority": {"routine_id": ROUTINE_ID, "routine_version": 1},
        "artifacts": [],
        "effects": [],
    }
    calls: list[str] = []
    fail_once = True

    class FakeJobs:
        async def get_job(self, requested_job_id):
            assert requested_job_id == job_id
            return dict(job_state, artifacts=list(job_state["artifacts"]), effects=list(job_state["effects"]), lease=dict(job_state["lease"]))

        async def recover_stale_job(self, _job_id):
            calls.append("recover")
            job_state["status"] = "blocked"
            job_state["revision"] += 1
            job_state["lease"] = {}
            return await self.get_job(job_id)

        async def resume_job(self, _job_id, **_kwargs):
            calls.append("resume")
            job_state["status"] = "accepted"
            job_state["revision"] += 1
            return await self.get_job(job_id)

        async def queue_job(self, _job_id, **_kwargs):
            calls.append("queue")
            job_state["status"] = "queued"
            job_state["revision"] += 1
            return await self.get_job(job_id)

        async def claim_job(self, _job_id, **_kwargs):
            calls.append("claim")
            job_state["status"] = "running"
            job_state["revision"] += 1
            job_state["lease"] = {"owner": "recovery-runner", "fencing_token": 5}
            return await self.get_job(job_id)

        async def record_artifact(self, _job_id, **kwargs):
            calls.append("artifact")
            job_state["revision"] += 1
            job_state["artifacts"].append({
                "file_path": kwargs["file_path"],
                "artifact_type": kwargs["artifact_type"],
                "content_sha256": routines_module._sha(kwargs["content"]),
            })
            return await self.get_job(job_id)

        async def record_effect(self, _job_id, **kwargs):
            nonlocal fail_once
            calls.append("effect")
            if fail_once:
                fail_once = False
                raise RuntimeError("simulated crash between receipt writes")
            job_state["revision"] += 1
            job_state["effects"].append({
                "effect_id": kwargs["effect_id"],
                "effect_type": kwargs["effect_type"],
                "target_path": kwargs["target_path"],
                "target_digest": kwargs["target_digest"],
                "status": kwargs["status"],
                "receipt_kind": "effect",
                "details": kwargs["details"],
            })
            return await self.get_job(job_id)

        async def record_readback(self, _job_id, **kwargs):
            calls.append("readback")
            job_state["revision"] += 1
            for item in job_state["effects"]:
                if item["effect_id"] == kwargs["effect_id"]:
                    item.update({"receipt_kind": "readback", "status": "succeeded", "details": kwargs["details"]})
            return await self.get_job(job_id)

        async def transition_job(self, _job_id, _status, **_kwargs):
            calls.append("transition")
            job_state["status"] = "succeeded"
            job_state["revision"] += 1
            return await self.get_job(job_id)

    monkeypatch.setattr(routines_module, "durable_job_repository", FakeJobs())
    service = RoutineService()
    monkeypatch.setattr(service, "_routine", AsyncMock(return_value=routine))
    monkeypatch.setattr(service, "_version", AsyncMock(return_value=version))
    monkeypatch.setattr(service, "_package_readback", lambda *_args: {"digest": "package-digest"})
    monkeypatch.setattr(service, "_publish_staged_install_under_routine_lock", AsyncMock(return_value={"package_digest": "package-digest", "package_files": files}))
    monkeypatch.setattr(service, "read", AsyncMock(return_value={"status": "installed"}))

    first = await service._reconcile_committed_install(
        routine,
        version,
        job_id=job_id,
        job=job_state,
        owner_principal_id=routine.owner_principal_id,
        owner_session_id=routine.owner_session_id,
    )
    assert first is None
    assert job_state["status"] == "running"
    assert len(job_state["artifacts"]) == 1
    assert (staging_root / "marker").exists()
    assert calls[:4] == ["recover", "resume", "queue", "claim"]

    second = await service._reconcile_committed_install(
        routine,
        version,
        job_id=job_id,
        job=job_state,
        owner_principal_id=routine.owner_principal_id,
        owner_session_id=routine.owner_session_id,
    )
    assert second == {"status": "installed"}
    assert job_state["status"] == "succeeded"
    assert len(job_state["artifacts"]) == 2
    assert calls.count("transition") == 1
    assert not staging_root.exists()


@pytest.mark.asyncio
async def test_install_receipt_failure_enters_local_finalize_recovery_and_completes(monkeypatch, tmp_path):
    """A committed install is blocked, then recovered into complete receipts."""

    import src.workflows.routines as routines_module

    monkeypatch.setattr(routines_module.settings, "workspace_dir", str(tmp_path))
    routine = GuardianRoutine(
        id=ROUTINE_ID,
        owner_principal_id="principal-recovery-install",
        owner_session_id="session-recovery-install",
        state="prepared",
        current_version=None,
        revision=1,
    )
    version = GuardianRoutineVersion(
        routine_id=ROUTINE_ID,
        version=1,
        workflow_bytes="workflow",
        workflow_sha256=hashlib.sha256(b"workflow").hexdigest(),
        runbook_bytes="runbook",
        runbook_sha256=hashlib.sha256(b"runbook").hexdigest(),
        source_provenance_json="{}",
    )
    committed_routine = GuardianRoutine(
        id=ROUTINE_ID,
        owner_principal_id=routine.owner_principal_id,
        owner_session_id=routine.owner_session_id,
        state="installed",
        current_version=1,
        revision=2,
    )
    committed_version = GuardianRoutineVersion(
        routine_id=ROUTINE_ID,
        version=1,
        workflow_bytes=version.workflow_bytes,
        workflow_sha256=version.workflow_sha256,
        runbook_bytes=version.runbook_bytes,
        runbook_sha256=version.runbook_sha256,
        source_provenance_json=version.source_provenance_json,
        installed_package_digest="package-digest",
    )
    public_root = tmp_path / "public"
    package_files = (
        (public_root / "workflow.md", "workflow", "routine_workflow"),
        (public_root / "runbook.yaml", "runbook", "routine_runbook"),
    )
    for path, content, _kind in package_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    staging_root = tmp_path / "artifacts" / "routine-install-staging" / ROUTINE_ID / "v1"
    staging_root.mkdir(parents=True)
    (staging_root / "marker").write_text("keep-until-terminal", encoding="utf-8")
    monkeypatch.setattr(routines_module, "_safe_resolve", lambda _path: tmp_path / "artifacts" / "routine-install-staging")
    monkeypatch.setattr(routines_module, "_routine_install_staging_root", lambda _id, _version: staging_root)

    job_id = f"routine-install:{ROUTINE_ID}:v1"
    authority = {
        "routine_id": ROUTINE_ID,
        "routine_version": 1,
        "workflow_sha256": version.workflow_sha256,
        "runbook_sha256": version.runbook_sha256,
        "source_provenance_sha256": routines_module._sha(version.source_provenance_json),
    }
    job_state: dict[str, Any] = {
        "job_id": job_id,
        "job_kind": "routine_install",
        "status": "awaiting_approval",
        "revision": 1,
        "declared_authority": authority,
        "lease": {},
        "artifacts": [],
        "effects": [],
    }
    installer_owner = f"routine:{job_id}"
    calls: list[str] = []
    fail_receipts = True

    def copy_job() -> dict[str, Any]:
        return {
            **job_state,
            "lease": dict(job_state.get("lease") or {}),
            "artifacts": list(job_state.get("artifacts") or []),
            "effects": list(job_state.get("effects") or []),
        }

    class FakeJobs:
        async def get_job(self, requested_job_id):
            assert requested_job_id == job_id
            return copy_job()

        async def resume_approved_job(self, *_args, **_kwargs):
            job_state["status"] = "queued"
            job_state["revision"] += 1
            return copy_job()

        async def resume_job(self, requested_job_id, **_kwargs):
            assert requested_job_id == job_id
            job_state["status"] = "accepted"
            job_state["revision"] += 1
            return copy_job()

        async def queue_job(self, requested_job_id, **_kwargs):
            assert requested_job_id == job_id
            job_state["status"] = "queued"
            job_state["revision"] += 1
            return copy_job()

        async def claim_job(self, requested_job_id, *, owner, **_kwargs):
            assert requested_job_id == job_id
            calls.append("claim")
            job_state["status"] = "running"
            job_state["revision"] += 1
            job_state["lease"] = {
                "owner": owner,
                "fencing_token": 1 if not job_state.get("recovery_claimed") else 2,
            }
            job_state["recovery_claimed"] = True
            return copy_job()

        async def recover_stale_job(self, requested_job_id):
            assert requested_job_id == job_id
            calls.append("recover_stale")
            raise RuntimeError("live install lease remains")

        async def transition_job(self, requested_job_id, status, **_kwargs):
            assert requested_job_id == job_id
            calls.append(status)
            job_state["status"] = status
            job_state["revision"] += 1
            if status == "blocked":
                job_state["failure_reason"] = "routine_install_local_finalize_pending"
                job_state["lease"] = {}
            if status == "succeeded":
                job_state["lease"] = {}
            return copy_job()

        async def record_artifact(self, requested_job_id, *, file_path, artifact_type, content, **_kwargs):
            assert requested_job_id == job_id
            job_state["revision"] += 1
            job_state["artifacts"].append(
                {
                    "file_path": file_path,
                    "artifact_type": artifact_type,
                    "content_sha256": routines_module._sha(content),
                }
            )
            return copy_job()

        async def record_effect(self, requested_job_id, **receipt):
            nonlocal fail_receipts
            assert requested_job_id == job_id
            calls.append("effect")
            if fail_receipts:
                fail_receipts = False
                raise RuntimeError("receipt writer crashed")
            job_state["revision"] += 1
            job_state["effects"].append({**receipt, "receipt_kind": "effect", "status": "succeeded"})
            return copy_job()

        async def record_readback(self, requested_job_id, **receipt):
            assert requested_job_id == job_id
            job_state["revision"] += 1
            job_state["effects"].append({**receipt, "receipt_kind": "readback", "status": "succeeded"})
            return copy_job()

    monkeypatch.setattr(routines_module, "durable_job_repository", FakeJobs())
    service = RoutineService()
    routine_reads = [routine, committed_routine]
    version_reads = [version, committed_version]

    async def read_routine(*_args, **_kwargs):
        return routine_reads.pop(0) if routine_reads else committed_routine

    async def read_version(*_args, **_kwargs):
        return version_reads.pop(0) if version_reads else committed_version

    monkeypatch.setattr(service, "_routine", read_routine)
    monkeypatch.setattr(service, "_version", read_version)
    monkeypatch.setattr(service, "_resume_approval", AsyncMock(return_value={"status": "queued", "revision": 2}))
    monkeypatch.setattr(
        service,
        "_install_package_under_routine_lock",
        AsyncMock(
            return_value={
                "routine": committed_routine,
                "version": committed_version,
                "package_digest": "package-digest",
                "package_files": package_files,
                "staging_root": staging_root,
            }
        ),
    )
    monkeypatch.setattr(
        service,
        "_publish_staged_install_under_routine_lock",
        AsyncMock(return_value={"package_digest": "package-digest", "package_files": package_files}),
    )
    monkeypatch.setattr(service, "_package_readback", lambda *_args, **_kwargs: {"digest": "package-digest"})
    monkeypatch.setattr(service, "read", AsyncMock(return_value={"status": "installed"}))

    with pytest.raises(RoutineError) as install_error:
        await service.install(
            ROUTINE_ID,
            RoutineInstallRequest(version=1, expected_routine_revision=1, approval_id="approval-install"),
            owner_principal_id=routine.owner_principal_id,
            owner_session_id=routine.owner_session_id,
        )
    assert install_error.value.code == "routine_install_local_finalize_pending"
    assert job_state["status"] == "blocked"
    assert job_state["failure_reason"] == "routine_install_local_finalize_pending"
    assert staging_root.exists()

    # Let startup recovery use the real receipt reconciler against the same
    # durable job state.  Only the package publication and readback are
    # intercepted; artifact/effect/readback records are exercised above.
    monkeypatch.setattr(service, "_record_install_receipts", RoutineService._record_install_receipts.__get__(service))

    class FakeDb:
        async def execute(self, statement):
            sql = str(statement).lower()
            if "guardian_routines" in sql:
                return _RecoveryResult([committed_routine])
            if "guardian_routine_versions" in sql:
                return _RecoveryResult([committed_version])
            raise AssertionError(statement)

        def expunge(self, _row):
            return None

    @asynccontextmanager
    async def get_session():
        yield FakeDb()

    monkeypatch.setattr(routines_module.db_engine, "get_session", get_session)
    receipts = await service.recover_pending_installs()

    assert receipts == [
        {
            "status": "recovered",
            "reason_code": "routine_install_publication_reconciled",
            "routine_id": ROUTINE_ID,
            "version": 1,
        }
    ]
    assert job_state["status"] == "succeeded"
    assert len(job_state["artifacts"]) == 2
    assert len([item for item in job_state["effects"] if item.get("receipt_kind") == "effect"]) == 2
    assert len([item for item in job_state["effects"] if item.get("receipt_kind") == "readback"]) == 2
    assert not staging_root.exists()


@pytest.mark.asyncio
async def test_reconcile_committed_install_does_not_write_receipts_with_live_lease(monkeypatch, tmp_path):
    import src.workflows.routines as routines_module

    routine = GuardianRoutine(
        id=ROUTINE_ID,
        owner_principal_id="principal-live-lease",
        owner_session_id="session-live-lease",
        state="installed",
        current_version=1,
        revision=2,
    )
    version = GuardianRoutineVersion(
        routine_id=ROUTINE_ID,
        version=1,
        workflow_bytes="workflow",
        workflow_sha256=hashlib.sha256(b"workflow").hexdigest(),
        runbook_bytes="runbook",
        runbook_sha256=hashlib.sha256(b"runbook").hexdigest(),
        source_provenance_json="{}",
        installed_package_digest="package-digest",
    )
    public_root = tmp_path / "public"
    files = (
        (public_root / "workflow.md", "workflow", "routine_workflow"),
        (public_root / "runbook.yaml", "runbook", "routine_runbook"),
    )
    for path, content, _kind in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    staging_root = tmp_path / "artifacts" / "routine-install-staging" / ROUTINE_ID / "v1"
    staging_root.mkdir(parents=True)
    (staging_root / "marker").write_text("keep-live-lease", encoding="utf-8")
    job_id = f"routine-install:{ROUTINE_ID}:v1"
    job_state: dict[str, Any] = {
        "job_id": job_id,
        "job_kind": "routine_install",
        "status": "running",
        "revision": 10,
        "lease": {
            "owner": "other-live-runner",
            "fencing_token": 9,
            "expires_at": "2999-01-01T00:00:00+00:00",
        },
        "declared_authority": {"routine_id": ROUTINE_ID, "routine_version": 1},
        "artifacts": [],
        "effects": [],
    }
    calls: list[str] = []

    class FakeJobs:
        async def get_job(self, requested_job_id):
            assert requested_job_id == job_id
            calls.append("get")
            return dict(
                job_state,
                artifacts=list(job_state["artifacts"]),
                effects=list(job_state["effects"]),
                lease=dict(job_state["lease"]),
            )

        async def recover_stale_job(self, _job_id):
            calls.append("recover")
            raise RuntimeError("active lease remains")

        async def record_artifact(self, *_args, **_kwargs):
            raise AssertionError("live lease must not record artifacts")

        async def record_effect(self, *_args, **_kwargs):
            raise AssertionError("live lease must not record effects")

        async def record_readback(self, *_args, **_kwargs):
            raise AssertionError("live lease must not record readback")

        async def transition_job(self, *_args, **_kwargs):
            raise AssertionError("live lease must not terminalize the job")

    monkeypatch.setattr(routines_module, "durable_job_repository", FakeJobs())
    service = RoutineService()
    monkeypatch.setattr(service, "_routine", AsyncMock(return_value=routine))
    monkeypatch.setattr(service, "_version", AsyncMock(return_value=version))
    monkeypatch.setattr(service, "_package_readback", lambda *_args: {"digest": "package-digest"})
    publish = AsyncMock(return_value={"package_digest": "package-digest", "package_files": files})
    monkeypatch.setattr(service, "_publish_staged_install_under_routine_lock", publish)

    result = await service._reconcile_committed_install(
        routine,
        version,
        job_id=job_id,
        job=job_state,
        owner_principal_id=routine.owner_principal_id,
        owner_session_id=routine.owner_session_id,
    )

    assert result is None
    assert calls == ["get", "recover", "get"]
    publish.assert_not_awaited()
    assert files[0][0].read_text(encoding="utf-8") == "workflow"
    assert files[1][0].read_text(encoding="utf-8") == "runbook"
    assert job_state["artifacts"] == []
    assert job_state["effects"] == []
    assert (staging_root / "marker").exists()


@pytest.mark.asyncio
async def test_pause_scan_reads_all_owner_bound_routine_jobs_beyond_page_limit(monkeypatch):
    rows = [
        WorkflowRunState(
            run_identity=f"routine-child:{index}",
            root_run_identity=f"routine-child:{index}",
            workflow_name="guardian-routine",
            job_kind="routine_guardian_watch_run_child",
            owner_kind="user",
            owner_principal_id="principal-1",
            operator_session_id="session-1",
            status="queued",
            declared_authority_json=json.dumps({"routine_id": "routine-1", "step_id": "guardian_watch_run"}),
        )
        for index in range(101)
    ]
    import src.workflows.routines as routines_module

    async with _local_table_database(WorkflowRunState) as get_session:
        monkeypatch.setattr(routines_module.db_engine, "get_session", get_session)
        async with get_session() as db:
            db.add_all(rows)

        jobs = [
            job
            async for job in RoutineService()._list_routine_jobs(
                "routine-1",
                owner_principal_id="principal-1",
                owner_session_id="session-1",
            )
        ]
        assert len(jobs) == 101
        assert {item["job_id"] for item in jobs} == {f"routine-child:{index}" for index in range(101)}


@pytest.mark.asyncio
async def test_watch_wrapper_dispatches_persisted_child_and_records_no_learning(monkeypatch):
    child_id = "routine-child:watch-test"
    child = {
        "job_id": child_id,
        "status": "running",
        "session_id": "session-1",
        "owner": {"principal_id": "principal-1"},
        "lease": {"owner": "runner-1", "fencing_token": 7},
        "parent_fencing_token": 7,
        "revision": 1,
        "declared_authority": {
            "step_id": "guardian_watch_run",
            "parent_job_id": "routine-invocation-1",
            "routine_invocation_job_id": "routine-invocation-1",
            "source_watch_id": "watch-1",
            "source_watch_revision": 2,
        },
        "checkpoints": [],
    }

    class FakeJobs:
        async def get_job(self, _job_id):
            if _job_id == "routine-invocation-1":
                return {
                    "job_id": "routine-invocation-1",
                    "status": "running",
                    "lease": {"owner": "routine:parent", "fencing_token": 7},
                }
            return child

        async def record_checkpoint(self, _job_id, **kwargs):
            child["revision"] += 1
            child["checkpoints"].append({"checkpoint_id": kwargs["checkpoint_id"], "payload": kwargs.get("checkpoint_payload")})
            return child

        async def record_effect(self, _job_id, **_kwargs):
            child["revision"] += 1
            return {"revision": child["revision"], "receipt": {"effect_id": "effect-1"}}

        async def record_readback(self, _job_id, **_kwargs):
            child["revision"] += 1
            return {"revision": child["revision"]}

        async def transition_job(self, _job_id, status, **_kwargs):
            child["status"] = status
            child["revision"] += 1
            return child

    class FakeWatch:
        async def run_watch(self, watch_id, **kwargs):
            assert watch_id == "watch-1"
            assert kwargs["occurrence_id"] == child_id
            assert kwargs["expected_plan_revision"] == 2
            return {"status": "no_change", "job_id": "m1-child-1"}

    import src.workflows.routines as routines_module

    monkeypatch.setattr(routines_module, "durable_job_repository", FakeJobs())
    monkeypatch.setattr(routines_module, "source_watch_service", FakeWatch())
    result = await RoutineService().execute_watch_step(
        child_id,
        context=RoutineStepContext("principal-1", "session-1", "runner-1", 7, runtime_job_id="routine-invocation-1"),
    )
    assert result["status"] == "no_change"
    assert result["child_status"] == "succeeded"
    assert child["status"] == "succeeded"


@pytest.mark.asyncio
async def test_followthrough_wrapper_uses_only_persisted_m3_child(monkeypatch):
    child_id = "routine-child:publication-test"
    child = {
        "job_id": child_id,
        "status": "running",
        "session_id": "session-1",
        "owner": {"principal_id": "principal-1"},
        "lease": {"owner": "runner-1", "fencing_token": 9},
        "parent_fencing_token": 9,
        "revision": 1,
        "declared_authority": {
            "step_id": "github_followthrough",
            "parent_job_id": "routine-invocation-1",
            "routine_invocation_job_id": "routine-invocation-1",
        },
        "checkpoints": [{"checkpoint_id": "routine-child:prepared", "payload": {"m3_job_id": "ghfollow_1"}}],
    }

    class FakeJobs:
        async def get_job(self, _job_id):
            if _job_id == "routine-invocation-1":
                return {
                    "job_id": "routine-invocation-1",
                    "status": "running",
                    "lease": {"owner": "routine:parent", "fencing_token": 9},
                }
            return child

        async def record_checkpoint(self, _job_id, **kwargs):
            child["revision"] += 1
            child["checkpoints"].append({"checkpoint_id": kwargs["checkpoint_id"], "payload": kwargs.get("checkpoint_payload")})
            return child

        async def record_effect(self, _job_id, **_kwargs):
            child["revision"] += 1
            return {"revision": child["revision"], "receipt": {"effect_id": "effect-2"}}

        async def record_readback(self, _job_id, **_kwargs):
            child["revision"] += 1
            return {"revision": child["revision"]}

        async def transition_job(self, _job_id, status, **_kwargs):
            child["status"] = status
            child["revision"] += 1
            return child

    class FakeFollowthrough:
        async def execute(self, *, owner_principal_id, job_id, owner_session_id, external_mutation_granted):
            assert owner_principal_id == "principal-1"
            assert job_id == "ghfollow_1"
            assert owner_session_id == "session-1"
            assert external_mutation_granted is True
            return {"status": "succeeded", "job_id": job_id}

    import src.workflows.routines as routines_module

    monkeypatch.setattr(routines_module, "durable_job_repository", FakeJobs())
    monkeypatch.setattr(routines_module, "GitHubFollowthroughService", FakeFollowthrough)
    result = await RoutineService().execute_followthrough_step(
        child_id,
        context=RoutineStepContext("principal-1", "session-1", "runner-1", 9, external_mutation_granted=True, runtime_job_id="routine-invocation-1"),
    )
    assert result["status"] == "succeeded"
    assert result["m3_job_id"] == "ghfollow_1"
    assert result["child_status"] == "succeeded"
    assert child["status"] == "succeeded"


@pytest.mark.asyncio
async def test_generated_followthrough_stale_parent_fence_preserves_m3_child(monkeypatch):
    """A stale wrapper fence must leave M3 for fenced recovery/reconciliation."""

    parent_id = "routine-invocation-1"
    child_id = _child_job_id("01234567-89ab-cdef-0123-456789abcdef", "publication")
    parent = {
        "job_id": parent_id,
        "job_kind": "routine_invocation",
        "status": "running",
        "owner": {"principal_id": "principal-1"},
        "session_id": "session-1",
        "operator_session_id": "session-1",
        "lease": {"owner": "routine:parent", "fencing_token": 7},
        "declared_authority": {
            "owner_kind": "user",
            "goal_owner_principal_id": "principal-1",
            "goal_owner_session_id": "session-1",
            "session_id": "session-1",
            "routine_id": ROUTINE_ID,
            "routine_revision": 3,
            "invocation_uuid": "01234567-89ab-cdef-0123-456789abcdef",
        },
    }
    child = {
        "job_id": child_id,
        "status": "blocked",
        "owner": {"principal_id": "principal-1"},
        "session_id": "session-1",
        "parent_fencing_token": 6,
        "lease": {"owner": None, "expires_at": None, "fencing_token": 2},
        "declared_authority": {
            "step_id": "github_followthrough",
            "parent_job_id": parent_id,
            "routine_invocation_job_id": parent_id,
        },
        "checkpoints": [
            {"checkpoint_id": "routine-child:prepared", "payload": {"m3_job_id": "ghfollow_1"}}
        ],
    }

    class FakeJobs:
        async def get_job(self, job_id):
            return parent if job_id == parent_id else child

        async def transition_job(self, job_id, status, **_kwargs):
            assert job_id == parent_id
            parent["status"] = status
            return parent

    service = RoutineService()
    import src.workflows.routines as routines_module

    monkeypatch.setattr(routines_module, "durable_job_repository", FakeJobs())
    monkeypatch.setattr(service, "_require_active_routine", AsyncMock())
    cancel = AsyncMock(side_effect=AssertionError("stale M3 child must remain recoverable"))
    monkeypatch.setattr(service, "_cancel_stale_child", cancel)

    result = await service.execute_generated_step(
        parent_id,
        "github_followthrough",
        context=RoutineStepContext(
            "principal-1",
            "session-1",
            "routine:parent",
            7,
            external_mutation_granted=True,
            runtime_job_id=parent_id,
        ),
    )

    assert result == {
        "status": "blocked",
        "job_id": parent_id,
        "child_job_id": child_id,
        "m3_job_id": "ghfollow_1",
        "reason_code": "routine_parent_fence_stale",
        "recovery": "reconcile_or_cancel",
        "operator_action": "reconcile_or_cancel",
        "operator_visible": True,
        "learning": "no_learning",
    }
    cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_pause_cancels_m3_child_through_canonical_adapter(monkeypatch):
    """M4 owns the parent/child link; M3 owns cancellation of its job."""

    m4_child = {
        "job_id": "routine-child:publication-cancel",
        "status": "awaiting_approval",
        "revision": 4,
        "owner": {"principal_id": "principal-1"},
        "declared_authority": {
            "routine_id": "routine-1",
            "step_id": "github_followthrough",
            "session_id": "session-1",
        },
        "checkpoints": [
            {
                "checkpoint_id": "routine-child:prepared",
                "payload": {"m3_job_id": "ghfollow_pending"},
            }
        ],
    }
    cancelled_m4: list[str] = []
    cancelled_m3: list[tuple[str, str]] = []

    class FakeJobs:
        async def list_jobs(self, *, limit):
            assert limit == 100
            return [m4_child]

        async def get_job(self, job_id):
            return {"job_id": job_id, "status": "cancelled", "effects": []}

        async def cancel_job(self, job_id, **_kwargs):
            cancelled_m4.append(job_id)
            return {"job_id": job_id, "status": "cancelled"}

    class FakeFollowthrough:
        async def cancel(self, *, owner_principal_id, owner_session_id, job_id):
            cancelled_m3.append((owner_principal_id, owner_session_id, job_id))
            return {"job_id": job_id, "status": "cancelled"}

    import src.workflows.routines as routines_module

    monkeypatch.setattr(routines_module, "durable_job_repository", FakeJobs())
    monkeypatch.setattr(routines_module, "GitHubFollowthroughService", FakeFollowthrough)
    await RoutineService()._cancel_pending_jobs("routine-1", reason="routine_paused:user_request")

    assert cancelled_m3 == [("principal-1", "session-1", "ghfollow_pending")]
    assert cancelled_m4 == ["routine-child:publication-cancel"]


@pytest.mark.asyncio
async def test_pause_fences_adoption_pending_child_before_m3_admission_race(monkeypatch):
    """A publication admitted during child CAS is compensated before parent cancel."""

    child_id = "routine-child:publication-adoption-race"
    m3_id = "ghfollow_adoption-race"
    parent_id = "routine-invocation:publication-adoption-race"
    child = {
        "job_id": child_id,
        "status": "blocked",
        "revision": 5,
        "owner": {"principal_id": "principal-1"},
        "operator_session_id": "session-1",
        "declared_authority": {
            "routine_id": "routine-1",
            "step_id": "github_followthrough",
            "session_id": "session-1",
            "m3_job_id": m3_id,
        },
        "effects": [],
        "checkpoints": [
            {
                "checkpoint_id": "routine-child:adoption_pending",
                "payload": {
                    "m3_job_id": m3_id,
                    "publication_operation_uuid": "01234567-89ab-cdef-0123-456789abcdef",
                    "status": "prepare_pending",
                },
            }
        ],
        "lease": {"owner": None, "fencing_token": 3},
    }
    parent = {
        "job_id": parent_id,
        "job_kind": "routine_invocation",
        "status": "running",
        "revision": 7,
        "owner": {"principal_id": "principal-1"},
        "operator_session_id": "session-1",
        "lease": {"owner": "routine:parent", "fencing_token": 9},
        "declared_authority": {"routine_id": "routine-1"},
    }
    m3 = {
        "job_id": m3_id,
        "job_kind": "github_followthrough_v1",
        "status": "awaiting_approval",
        "owner": {"principal_id": "principal-1"},
        "operator_session_id": "session-1",
        "declared_authority": {
            "session_id": "session-1",
            "approval_id": "approval-adoption-race",
        },
        "effects": [],
    }
    approval = {"status": "pending"}
    events: list[tuple[str, str]] = []
    m3_visible = False

    class FakeJobs:
        async def list_jobs(self, *, limit):
            assert limit == 100
            return [child, parent]

        async def get_job(self, job_id):
            if job_id == m3_id:
                return dict(m3) if m3_visible else None
            if job_id == child_id:
                return dict(child)
            if job_id == parent_id:
                return dict(parent)
            return None

        async def cancel_job(self, job_id, **_kwargs):
            nonlocal m3_visible
            events.append(("cancel", job_id))
            if job_id == child_id:
                child["status"] = "cancelled"
                m3_visible = True
                return dict(child)
            if job_id == parent_id:
                assert child["status"] == "cancelled"
                assert m3["status"] == "cancelled"
                assert approval["status"] == "denied"
                parent["status"] = "cancelled"
                return dict(parent)
            raise AssertionError(f"unexpected cancellation: {job_id}")

    class FakeFollowthrough:
        async def cancel(self, *, owner_principal_id, owner_session_id, job_id):
            assert (owner_principal_id, owner_session_id, job_id) == (
                "principal-1",
                "session-1",
                m3_id,
            )
            events.append(("m3", job_id))
            m3["status"] = "cancelled"
            approval["status"] = "denied"
            return {"job_id": job_id, "status": "cancelled"}

    class FakeApprovalRepository:
        async def get(self, approval_id):
            assert approval_id == "approval-adoption-race"
            return SimpleNamespace(status=approval["status"])

    import src.workflows.routines as routines_module

    monkeypatch.setattr(routines_module, "durable_job_repository", FakeJobs())
    monkeypatch.setattr(routines_module, "GitHubFollowthroughService", FakeFollowthrough)
    monkeypatch.setattr(routines_module, "approval_repository", FakeApprovalRepository())
    failures = await RoutineService()._cancel_pending_jobs(
        "routine-1", reason="routine_paused:user_request"
    )

    assert failures == []
    assert events == [("cancel", child_id), ("m3", m3_id), ("cancel", parent_id)]
    assert approval["status"] == "denied"
    assert child["status"] == "cancelled"
    assert m3["status"] == "cancelled"
    assert parent["status"] == "cancelled"


@pytest.mark.asyncio
async def test_pause_preserves_watch_child_when_active_m1_watch_readback_is_missing(monkeypatch):
    child_id = "routine-child:watch-cancel-readback"
    m1_job_id = f"source-watch:watch-1:{child_id}"
    child = {
        "job_id": child_id,
        "status": "awaiting_approval",
        "revision": 4,
        "owner": {"principal_id": "principal-1"},
        "session_id": "session-1",
        "declared_authority": {
            "routine_id": "routine-1",
            "step_id": "guardian_watch_run",
            "source_watch_id": "watch-1",
            "session_id": "session-1",
        },
        "checkpoints": [],
    }
    parent = {
        "job_id": "routine-invocation-1",
        "job_kind": "routine_invocation",
        "status": "running",
        "revision": 2,
        "lease": {"owner": "routine:parent", "fencing_token": 7},
        "declared_authority": {"routine_id": "routine-1"},
    }
    cancelled: list[str] = []
    transitioned: list[tuple[str, str]] = []

    class FakeJobs:
        async def list_jobs(self, *, limit):
            assert limit == 100
            return [child, parent]

        async def get_job(self, job_id):
            if job_id == m1_job_id:
                return {"job_id": job_id, "status": "running"}
            return None

        async def cancel_job(self, job_id, **_kwargs):
            cancelled.append(job_id)
            return {"job_id": job_id, "status": "cancelled"}

        async def transition_job(self, job_id, status, **_kwargs):
            transitioned.append((job_id, status))
            return {"job_id": job_id, "status": status}

    class MissingWatch:
        async def get_watch(self, *_args, **_kwargs):
            return None

    import src.workflows.routines as routines_module

    monkeypatch.setattr(routines_module, "durable_job_repository", FakeJobs())
    monkeypatch.setattr(routines_module, "source_watch_service", MissingWatch())

    failures = await RoutineService()._cancel_pending_jobs("routine-1", reason="routine_paused:user_request")

    assert cancelled == []
    assert transitioned == [("routine-invocation-1", "blocked")]
    assert failures == [
        {
            "job_id": child_id,
            "step_id": "guardian_watch_run",
            "status": "blocked",
            "reason_code": "RuntimeError",
            "operator_action": "recover_or_cancel",
        }
    ]


@pytest.mark.asyncio
async def test_pause_cancel_reports_m3_failure_for_operator_reconciliation(monkeypatch):
    child = {
        "job_id": "routine-child:publication-failure",
        "status": "awaiting_approval",
        "revision": 4,
        "owner": {"principal_id": "principal-1"},
        "declared_authority": {
            "routine_id": "routine-1",
            "step_id": "github_followthrough",
            "session_id": "session-1",
        },
        "checkpoints": [
            {
                "checkpoint_id": "routine-child:prepared",
                "payload": {"m3_job_id": "ghfollow_pending"},
            }
        ],
    }

    class FakeJobs:
        async def list_jobs(self, *, limit):
            assert limit == 100
            return [child]

        async def get_job(self, _job_id):
            return None

        async def cancel_job(self, job_id, **_kwargs):
            return {"job_id": job_id, "status": "cancelled"}

    class FailingFollowthrough:
        async def cancel(self, *, owner_principal_id, owner_session_id, job_id):
            raise RuntimeError(f"cancel unavailable for {owner_principal_id}:{job_id}")

    import src.workflows.routines as routines_module

    monkeypatch.setattr(routines_module, "durable_job_repository", FakeJobs())
    monkeypatch.setattr(routines_module, "GitHubFollowthroughService", FailingFollowthrough)
    failures = await RoutineService()._cancel_pending_jobs("routine-1", reason="routine_paused:user_request")

    assert failures == [
        {
            "job_id": "routine-child:publication-failure",
            "step_id": "github_followthrough",
            "m3_job_id": "ghfollow_pending",
            "status": "blocked",
            "reason_code": "RuntimeError",
            "operator_action": "reconcile_or_cancel",
        }
    ]


@pytest.mark.asyncio
async def test_pause_remains_blocked_when_m3_approval_cleanup_is_unresolved(monkeypatch):
    child = {
        "job_id": "routine-child:publication-approval-cleanup",
        "status": "awaiting_approval",
        "revision": 4,
        "owner": {"principal_id": "principal-1"},
        "declared_authority": {
            "routine_id": "routine-1",
            "step_id": "github_followthrough",
            "session_id": "session-1",
        },
        "checkpoints": [
            {
                "checkpoint_id": "routine-child:prepared",
                "payload": {"m3_job_id": "ghfollow_cleanup"},
            }
        ],
    }
    parent = {
        "job_id": "routine-invocation:approval-cleanup",
        "job_kind": "routine_invocation",
        "status": "running",
        "revision": 3,
        "owner": {"principal_id": "principal-1"},
        "operator_session_id": "session-1",
        "lease": {"owner": "routine:parent", "fencing_token": 4},
        "declared_authority": {"routine_id": "routine-1"},
    }
    m3 = {
        "job_id": "ghfollow_cleanup",
        "status": "cancelled",
        "declared_authority": {"approval_id": "approval-cleanup"},
        "effects": [],
    }
    transitioned: list[tuple[str, str]] = []

    class FakeJobs:
        async def list_jobs(self, *, limit):
            assert limit == 100
            return [child, parent]

        async def get_job(self, job_id):
            return {child["job_id"]: child, parent["job_id"]: parent, m3["job_id"]: m3}.get(job_id)

        async def transition_job(self, job_id, status, **_kwargs):
            transitioned.append((job_id, status))
            assert job_id == parent["job_id"]
            assert status == "blocked"
            parent["status"] = status
            return dict(parent)

    class BlockedFollowthrough:
        async def cancel(self, *, owner_principal_id, owner_session_id, job_id):
            assert (owner_principal_id, owner_session_id, job_id) == (
                "principal-1",
                "session-1",
                "ghfollow_cleanup",
            )
            return {
                "job_id": job_id,
                "status": "blocked",
                "approval_cleanup": "unavailable",
            }

    import src.workflows.routines as routines_module

    monkeypatch.setattr(routines_module, "durable_job_repository", FakeJobs())
    monkeypatch.setattr(routines_module, "GitHubFollowthroughService", BlockedFollowthrough)
    failures = await RoutineService()._cancel_pending_jobs(
        "routine-1", reason="routine_revoked:operator_request"
    )

    assert failures == [
        {
            "job_id": child["job_id"],
            "step_id": "github_followthrough",
            "m3_job_id": "ghfollow_cleanup",
            "status": "blocked",
            "reason_code": "m3_approval_cleanup_unavailable",
            "operator_action": "reconcile_or_cancel",
        }
    ]
    assert transitioned == [(parent["job_id"], "blocked")]


@pytest.mark.asyncio
async def test_recovery_refuses_parent_fence_replacement_after_child_wait(monkeypatch):
    previous = {
        "job_id": "routine-invocation-1",
        "status": "running",
        "revision": 4,
        "lease": {"owner": "routine:routine-invocation-1", "fencing_token": 7},
        "declared_authority": {
            "routine_id": "routine-1",
            "routine_revision": 3,
            "principal": "principal-1",
            "session_id": "session-1",
        },
    }
    latest = {
        **previous,
        "lease": {"owner": "routine:routine-invocation-1", "fencing_token": 8},
    }

    class FakeJobs:
        async def get_job(self, _job_id):
            return latest

    service = RoutineService()
    import src.workflows.routines as routines_module

    monkeypatch.setattr(routines_module, "durable_job_repository", FakeJobs())
    monkeypatch.setattr(service, "_require_active_routine", lambda *args, **kwargs: _async_value(True))
    assert await service._reacquire_parent_after_child(
        previous,
        routine_id="routine-1",
        owner_principal_id="principal-1",
        owner_session_id="session-1",
    ) is None


def _invocation_cancel_fixture(*, m3_status: str, effect_status: str | None = None):
    owner = "principal-cancel"
    session = "session-cancel"
    routine_id = "routine-cancel"
    parent_id = "routine-invocation:cancel"
    invocation_uuid = "11111111-1111-4111-8111-111111111111"
    operation_uuid = str(
        uuid.uuid5(uuid.UUID(invocation_uuid), "seraph:guardian-routine:publication")
    )
    child_id = _child_job_id(invocation_uuid, "publication")
    m3_id = _expected_publication_job_id(owner, operation_uuid)
    package_digest = "a" * 64
    binding = {
        "routine_id": routine_id,
        "routine_revision": 4,
        "routine_version": 2,
        "package_digest": package_digest,
        "parent_invocation_job_id": parent_id,
        "publication_child_job_id": child_id,
        "invocation_uuid": invocation_uuid,
        "owner_principal_id": owner,
        "owner_session_id": session,
        "goal_id": "goal-cancel",
        "goal_revision": 3,
        "source_watch_id": "watch-cancel",
        "connection_id": "connection-cancel",
        "connection_revision": 5,
        "repository": "seraph-quest/seraph",
        "action": "create_issue",
        "operation_uuid": operation_uuid,
    }
    parent_authority = {
        "routine_id": routine_id,
        "routine_revision": 4,
        "routine_version": 2,
        "package_digest": package_digest,
        "session_id": session,
        "invocation_uuid": invocation_uuid,
        "source_watch_id": "watch-cancel",
        "github_connection_id": "connection-cancel",
        "github_connection_revision": 5,
        "github_repository": "seraph-quest/seraph",
        "github_action": "create_issue",
    }
    parent = {
        "job_id": parent_id,
        "job_kind": "routine_invocation",
        "status": "running",
        "owner": {"kind": "user", "principal_id": owner},
        "session_id": session,
        "operator_session_id": session,
        "goal_id": "goal-cancel",
        "goal_revision": 3,
        "revision": 8,
        "lease": {"owner": "routine:cancel", "fencing_token": 9},
        "declared_authority": parent_authority,
        "checkpoints": [
            {
                "checkpoint_id": "routine:publication_child_recorded",
                "payload": {
                    "step_id": "github_followthrough",
                    "child_job_id": child_id,
                    "m3_job_id": m3_id,
                    "publication_operation_uuid": operation_uuid,
                },
            }
        ],
        "effects": [],
    }
    child = {
        "job_id": child_id,
        "job_kind": "routine_github_followthrough_child",
        "status": "blocked",
        "owner": {"kind": "user", "principal_id": owner},
        "session_id": session,
        "operator_session_id": session,
        "parent_job_id": parent_id,
        "parent_fencing_token": 9,
        "revision": 4,
        "lease": {"owner": None, "expires_at": None, "fencing_token": 2},
        "declared_authority": {
            **parent_authority,
            "parent_job_id": parent_id,
            "routine_invocation_job_id": parent_id,
            "step_id": "github_followthrough",
            "m3_job_id": m3_id,
            "publication_operation_uuid": operation_uuid,
        },
        "checkpoints": [
            {
                "checkpoint_id": "routine-child:prepared",
                "payload": {
                    "m3_job_id": m3_id,
                    "approval_id": "approval-cancel",
                    "publication_operation_uuid": operation_uuid,
                    "status": m3_status,
                },
            }
        ],
        "effects": [],
    }
    m3 = {
        "job_id": m3_id,
        "job_kind": "github_followthrough_v1",
        "status": m3_status,
        "owner": {"kind": "user", "principal_id": owner},
        "session_id": session,
        "operator_session_id": session,
        "revision": 3,
        "declared_authority": {
            "session_id": session,
            "approval_id": "approval-cancel",
            "routine_binding": binding,
        },
        "effects": (
            [{"effect_type": "github_publication", "status": effect_status}]
            if effect_status
            else []
        ),
    }
    return {
        "owner": owner,
        "session": session,
        "routine_id": routine_id,
        "parent_id": parent_id,
        "child_id": child_id,
        "m3_id": m3_id,
        "parent": parent,
        "child": child,
        "m3": m3,
    }


def _adoption_pending_cancel_fixture():
    fixture = _invocation_cancel_fixture(m3_status="awaiting_approval")
    fixture["parent"]["checkpoints"] = []
    fixture["child"]["checkpoints"] = [
        {
            "checkpoint_id": "routine-child:adoption_pending",
            "payload": {
                "m3_job_id": fixture["m3_id"],
                "publication_operation_uuid": fixture["m3"]["declared_authority"]["routine_binding"]["operation_uuid"],
                "status": "prepare_pending",
            },
        }
    ]
    return fixture


@pytest.mark.asyncio
async def test_cancel_invocation_cancels_bound_approval_m3_before_parent_tree(monkeypatch):
    fixture = _invocation_cancel_fixture(m3_status="awaiting_approval")
    events: list[tuple[str, str]] = []

    class FakeJobs:
        async def get_job(self, job_id):
            return {
                fixture["parent_id"]: fixture["parent"],
                fixture["child_id"]: fixture["child"],
                fixture["m3_id"]: fixture["m3"],
            }.get(job_id)

        async def cancel_job_tree(self, job_id, *, reason):
            events.append(("tree", job_id))
            assert fixture["m3"]["status"] == "cancelled"
            fixture["parent"]["status"] = "cancelled"
            return [dict(fixture["parent"]), dict(fixture["child"])]

    class FakeFollowthrough:
        async def cancel(self, *, owner_principal_id, owner_session_id, job_id):
            assert owner_principal_id == fixture["owner"]
            assert owner_session_id == fixture["session"]
            assert job_id == fixture["m3_id"]
            events.append(("m3", job_id))
            fixture["m3"]["status"] = "cancelled"
            return {"job_id": job_id, "status": "cancelled"}

    class FakeApprovalRepository:
        async def get(self, approval_id):
            assert approval_id == "approval-cancel"
            return SimpleNamespace(status="denied")

    import src.workflows.routines as routines_module

    monkeypatch.setattr(routines_module, "durable_job_repository", FakeJobs())
    monkeypatch.setattr(routines_module, "GitHubFollowthroughService", FakeFollowthrough)
    monkeypatch.setattr(routines_module, "approval_repository", FakeApprovalRepository())
    result = await RoutineService().cancel_invocation_job_tree(
        fixture["parent_id"],
        routine_id=fixture["routine_id"],
        owner_principal_id=fixture["owner"],
        owner_session_id=fixture["session"],
        reason="operator_cancelled",
    )

    assert events == [("m3", fixture["m3_id"]), ("tree", fixture["parent_id"])]
    assert fixture["parent"]["status"] == "cancelled"
    assert fixture["m3"]["status"] == "cancelled"
    assert result[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_invocation_blocks_parent_when_m3_effect_is_unknown(monkeypatch):
    fixture = _invocation_cancel_fixture(
        m3_status="running",
        effect_status="dispatched",
    )
    events: list[tuple[str, str]] = []

    class FakeJobs:
        async def get_job(self, job_id):
            return {
                fixture["parent_id"]: fixture["parent"],
                fixture["child_id"]: fixture["child"],
                fixture["m3_id"]: fixture["m3"],
            }.get(job_id)

        async def transition_job(self, job_id, status, **_kwargs):
            events.append(("block", job_id))
            assert status == "blocked"
            fixture["parent"]["status"] = "blocked"
            return fixture["parent"]

        async def cancel_job_tree(self, *_args, **_kwargs):
            raise AssertionError("uncertain M3 effect must not cancel the parent tree")

    class FailingFollowthrough:
        async def cancel(self, *, owner_principal_id, owner_session_id, job_id):
            assert owner_principal_id == fixture["owner"]
            assert owner_session_id == fixture["session"]
            assert job_id == fixture["m3_id"]
            events.append(("m3", job_id))
            return {"job_id": job_id, "status": "unknown_external_effect"}

    import src.workflows.routines as routines_module

    monkeypatch.setattr(routines_module, "durable_job_repository", FakeJobs())
    monkeypatch.setattr(routines_module, "GitHubFollowthroughService", FailingFollowthrough)
    result = await RoutineService().cancel_invocation_job_tree(
        fixture["parent_id"],
        routine_id=fixture["routine_id"],
        owner_principal_id=fixture["owner"],
        owner_session_id=fixture["session"],
        reason="operator_cancelled",
    )

    assert events == [("m3", fixture["m3_id"]), ("block", fixture["parent_id"])]
    assert fixture["parent"]["status"] == "blocked"
    assert result[0]["status"] == "blocked"
    assert result[0]["recovery"] == "reconcile_or_cancel"
    assert result[0]["reason_code"] == "m3_external_effect_unresolved"


@pytest.mark.asyncio
async def test_cancel_invocation_settles_pre_admission_child_when_m3_is_absent(monkeypatch):
    fixture = _adoption_pending_cancel_fixture()
    events: list[tuple[str, str]] = []

    class FakeJobs:
        async def get_job(self, job_id):
            if job_id == fixture["m3_id"]:
                return None
            return {
                fixture["parent_id"]: fixture["parent"],
                fixture["child_id"]: fixture["child"],
            }.get(job_id)

        async def cancel_job(self, job_id, **_kwargs):
            events.append(("child", job_id))
            fixture["child"]["status"] = "cancelled"
            return fixture["child"]

        async def cancel_job_tree(self, job_id, *, reason):
            events.append(("tree", job_id))
            assert fixture["child"]["status"] == "cancelled"
            fixture["parent"]["status"] = "cancelled"
            return [dict(fixture["parent"]), dict(fixture["child"])]

    import src.workflows.routines as routines_module

    monkeypatch.setattr(routines_module, "durable_job_repository", FakeJobs())
    result = await RoutineService().cancel_invocation_job_tree(
        fixture["parent_id"],
        routine_id=fixture["routine_id"],
        owner_principal_id=fixture["owner"],
        owner_session_id=fixture["session"],
        reason="operator_cancelled",
    )

    assert events == [
        ("child", fixture["child_id"]),
        ("tree", fixture["parent_id"]),
    ]
    assert result[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_invocation_compensates_m3_admission_race_after_child_fence(monkeypatch):
    fixture = _adoption_pending_cancel_fixture()
    events: list[tuple[str, str]] = []
    m3_visible = False

    class FakeJobs:
        async def get_job(self, job_id):
            nonlocal m3_visible
            if job_id == fixture["m3_id"]:
                return fixture["m3"] if m3_visible else None
            return {
                fixture["parent_id"]: fixture["parent"],
                fixture["child_id"]: fixture["child"],
            }.get(job_id)

        async def cancel_job(self, job_id, **_kwargs):
            nonlocal m3_visible
            events.append(("child", job_id))
            fixture["child"]["status"] = "cancelled"
            m3_visible = True
            return fixture["child"]

        async def cancel_job_tree(self, job_id, *, reason):
            events.append(("tree", job_id))
            assert fixture["m3"]["status"] == "cancelled"
            fixture["parent"]["status"] = "cancelled"
            return [dict(fixture["parent"]), dict(fixture["child"])]

    class FakeFollowthrough:
        async def cancel(self, *, owner_principal_id, owner_session_id, job_id):
            assert owner_principal_id == fixture["owner"]
            assert owner_session_id == fixture["session"]
            assert job_id == fixture["m3_id"]
            events.append(("m3", job_id))
            fixture["m3"]["status"] = "cancelled"
            return {"job_id": job_id, "status": "cancelled"}

    class FakeApprovalRepository:
        async def get(self, approval_id):
            assert approval_id == "approval-cancel"
            return SimpleNamespace(status="denied")

    import src.workflows.routines as routines_module

    monkeypatch.setattr(routines_module, "durable_job_repository", FakeJobs())
    monkeypatch.setattr(routines_module, "GitHubFollowthroughService", FakeFollowthrough)
    monkeypatch.setattr(routines_module, "approval_repository", FakeApprovalRepository())
    result = await RoutineService().cancel_invocation_job_tree(
        fixture["parent_id"],
        routine_id=fixture["routine_id"],
        owner_principal_id=fixture["owner"],
        owner_session_id=fixture["session"],
        reason="operator_cancelled",
    )

    assert events == [
        ("child", fixture["child_id"]),
        ("m3", fixture["m3_id"]),
        ("tree", fixture["parent_id"]),
    ]
    assert result[0]["status"] == "cancelled"
