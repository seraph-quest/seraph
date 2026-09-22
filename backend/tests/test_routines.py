from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.models import WorkflowRunState
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
        await guardian_watch_run("routine-invocation:job", context=None)


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
