from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.workflows.procedure_contracts import (
    ProcedureContractError,
    ProcedureV2Plan,
    build_procedure_plan,
    get_procedure_template,
    plan_digest,
    validate_procedure_plan,
)
from src.workflows.procedure_service import (
    GoalAdmissionBudgetSnapshot,
    ProcedureV2CreateRequest,
    ProcedureV2Error,
    ProcedureV2InvokeRequest,
    ProcedureV2PreviewRequest,
    ProcedureV2Service,
    ProcedureV2ScheduleRequest,
    V2InvocationDescriptor,
    V2VersionDescriptor,
    _validated_immutable_step_inputs,
    _verified_workspace_artifact,
    _procedure_create_request_digest,
    _cleanup_unpublished_procedure_artifact,
    _preview_expiry,
    goal_admission_budget_snapshot,
)
from src.goals.contracts import GoalAdmissionBudget
from src.workflows.routine_templates import render_runbook, render_workflow, validate_generated_files
from src.browser.task_runner import BrowserTaskInput, _browser_input_digests
from src.db import engine as db_engine
from src.db.models import (
    Goal,
    GovernedScheduleBinding,
    GuardianRoutineVersion,
    ProcedureV2Binding,
    Session,
    ScheduledJob,
    WorkBoardInputArtifact,
    WorkBoardStatus,
    WorkBoardTask,
)
from src.work_board.contracts import WorkBoardInputArtifactCreate, WorkBoardOwner
from src.work_board.input_artifacts import prepare_input_artifact
from src.work_board.repository import BoardError, WorkBoardRepository
from src.workflows.routines import (
    ROUTINE_PACK_RUNBOOK_REFERENCE,
    RoutineError,
    RoutineInstallRequest,
    RoutineService,
    _routine_pack_manifest_payload,
    _routine_pack_runbook_payload,
    _validate_routine_pack_runbook,
)
from src.workflows.job_runtime import durable_job_repository


def _step_input(step_id: str) -> dict[str, str]:
    return {
        "typed_input_ref": f"source/{step_id}",
        "typed_input_digest": (step_id.encode("utf-8").hex() * 64)[:64],
    }


@pytest.mark.parametrize(
    ("template_id", "step_ids"),
    [
        ("public-browser-check", ("public_browser_check",)),
        ("watch-and-public-browser", ("source_watch", "public_browser_check")),
        ("selected-meeting-prep", ("selected_meeting_prep",)),
    ],
)
def test_v2_plan_persisted_json_round_trip_is_strict(template_id: str, step_ids: tuple[str, ...]):
    plan = build_procedure_plan(
        template_id,
        step_inputs={step_id: _step_input(step_id) for step_id in step_ids},
    )

    restored = validate_procedure_plan(plan.model_dump(mode="json"))

    assert isinstance(restored, ProcedureV2Plan)
    assert tuple(step.step_id for step in restored.steps) == step_ids
    assert plan_digest(restored) == plan_digest(plan)


def test_v2_plan_json_boundary_rejects_scalar_coercion_and_step_drift():
    plan = build_procedure_plan(
        "public-browser-check",
        step_inputs={"public_browser_check": _step_input("public_browser_check")},
    ).model_dump(mode="json")

    scalar_drift = {**plan, "schema_version": "2"}
    with pytest.raises(ProcedureContractError, match="procedure_plan_invalid"):
        validate_procedure_plan(scalar_drift)

    step_drift = {**plan, "steps": [{**plan["steps"][0], "step_id": "source_watch"}]}
    with pytest.raises(ProcedureContractError, match="procedure_plan_invalid"):
        validate_procedure_plan(step_drift)


def _copied_browser_input() -> dict:
    return {
        "schema_version": 1,
        "start_url": "https://public.example/docs",
        "allowed_hosts": ["public.example"],
        "approved_url_prefixes": ["https://public.example/docs"],
        "actions": [
            {
                "kind": "extract",
                "selector": "main h1",
                "max_chars": 128,
                "expected_checks": [
                    {"kind": "text_contains", "selector": "main h1", "value": "docs"},
                ],
            }
        ],
        "final_expected_checks": [{"kind": "url_host", "value": "public.example"}],
    }


def test_private_copied_browser_input_is_digest_bound_and_tamper_evident():
    model = BrowserTaskInput.model_validate(_copied_browser_input())
    envelope_digest, model_digest, consent_digest = _browser_input_digests(model)
    raw = {
        "public_browser_check": {
            "browser_input": model.model_dump(mode="json", exclude_none=True),
            "browser_input_digest": model_digest,
            "input_envelope_digest": envelope_digest,
            "action_consent_digest": consent_digest,
        }
    }
    validated = _validated_immutable_step_inputs(
        raw,
        get_procedure_template("public-browser-check"),
    )
    assert validated["public_browser_check"]["browser_input_digest"] == model_digest
    raw["public_browser_check"]["browser_input_digest"] = "0" * 64
    with pytest.raises(ValueError, match="digest"):
        _validated_immutable_step_inputs(
            raw,
            get_procedure_template("public-browser-check"),
        )


def test_source_output_readback_requires_current_workspace_bytes(tmp_path, monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    output = tmp_path / "artifacts" / "work-board" / "browser" / "result.json"
    output.parent.mkdir(parents=True)
    output.write_text("verified output", encoding="utf-8")
    import hashlib

    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    relative = "artifacts/work-board/browser/result.json"
    assert _verified_workspace_artifact(relative, digest) is True
    output.write_text("tampered output", encoding="utf-8")
    assert _verified_workspace_artifact(relative, digest) is False
    output.unlink()
    assert _verified_workspace_artifact(relative, digest) is False


@pytest.mark.parametrize("template_id", [
    "public-browser-check",
    "watch-and-public-browser",
    "selected-meeting-prep",
])
def test_v2_generated_workflow_and_runbook_are_fixed(template_id: str):
    workflow = render_workflow(
        routine_id="0123456789abcdef0123456789abcdef",
        version=1,
        name="Reviewed procedure",
        template_id=template_id,
    )
    runbook = render_runbook(
        routine_id="0123456789abcdef0123456789abcdef",
        version=1,
        name="Reviewed procedure",
        template_id=template_id,
    )

    result = validate_generated_files(
        workflow=workflow,
        runbook=runbook,
        routine_id="0123456789abcdef0123456789abcdef",
        version=1,
        template_id=template_id,
    )

    assert result["valid"] is True
    assert result["schema_version"] == 2


def test_preview_expiry_is_a_stable_utc_minute_bucket():
    observed = datetime(2026, 10, 1, 12, 0, 31, tzinfo=timezone.utc)
    assert _preview_expiry(observed) == datetime(2026, 10, 1, 12, 15, tzinfo=timezone.utc)


def test_preview_request_rejects_unknown_fields_and_wrong_source_count():
    with pytest.raises(ValueError):
        ProcedureV2PreviewRequest.model_validate(
            {
                "template_id": "public-browser-check",
                "source_tasks": [{"task_id": "task", "expected_revision": 1}],
                "name": "A procedure",
                "idempotency_key": "key",
                "unexpected": True,
            }
        )


def _create_request(*, name: str = "A procedure", preview_digest: str = "a" * 64) -> ProcedureV2CreateRequest:
    return ProcedureV2CreateRequest.model_validate(
        {
            "template_id": "public-browser-check",
            "source_tasks": [{"task_id": "source-task", "expected_revision": 7}],
            "name": name,
            "idempotency_key": "prepare-key",
            "preview_digest": preview_digest,
        }
    )


async def _prepare_repository_v2_install(
    async_db,
    monkeypatch,
    tmp_path,
    *,
    idempotency_key: str,
):
    """Prepare one v2 version through the real routine/job/approval stores."""

    from config.settings import settings

    owner = "operator:v2-install"
    session_id = "session:v2-install"
    goal_id = "goal:v2-install"
    template_id = "selected-meeting-prep"
    source_task_id = "source-calendar-task"
    source_revision = 7
    spec = get_procedure_template(template_id)
    step = spec.steps[0]
    resolved = {
        "spec": spec,
        "plan": build_procedure_plan(
            template_id,
            step_inputs={step.step_id: _step_input(step.step_id)},
        ),
        "source_refs": [
            {
                "task_id": source_task_id,
                "task_revision": source_revision,
                "attempt_id": "source-calendar-attempt",
                "job_id": "source-calendar-job",
                "artifact_ids_and_hashes": [
                    {
                        "artifact_id": "source-calendar-artifact",
                        "sha256": "a" * 64,
                        "receipt_kind": "readback",
                        "status": "succeeded",
                        "workflow_run_id": "source-calendar-job",
                    }
                ],
                "capability_id": step.capability_id,
                "capability_version": step.capability_version,
                "goal_id": goal_id,
                "goal_revision": 1,
            }
        ],
        "immutable_step_inputs": {},
        "goal_id": goal_id,
        "goal_revision": 1,
    }
    resolved["plan_payload"] = resolved["plan"].model_dump(mode="json")

    async with db_engine.get_session() as db:
        db.add(Session(id=session_id, owner_principal_id=owner))
        db.add(
            Goal(
                id=goal_id,
                title="Procedure v2 install test goal",
                status="active",
                revision=1,
                owner_principal_id=owner,
                owner_session_id=session_id,
            )
        )

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    routine_service = RoutineService()
    service = ProcedureV2Service(routine_service)
    service._resolve_sources = AsyncMock(return_value=resolved)
    preview_request = ProcedureV2PreviewRequest.model_validate(
        {
            "template_id": template_id,
            "source_tasks": [{"task_id": source_task_id, "expected_revision": source_revision}],
            "name": "Reviewed calendar procedure",
            "idempotency_key": idempotency_key,
        }
    )
    preview = await service.preview_from_tasks(
        preview_request,
        owner_principal_id=owner,
        owner_session_id=session_id,
    )
    create_request = ProcedureV2CreateRequest.model_validate(
        {
            **preview_request.model_dump(mode="json"),
            "preview_digest": preview["preview_digest"],
        }
    )
    payload, status_code = await service.create_from_tasks(
        create_request,
        owner_principal_id=owner,
        owner_session_id=session_id,
    )
    assert status_code == 201
    assert payload["status"] == "prepared"
    assert payload["install_approval_status"] == "pending"
    assert payload["install_job_id"]
    return {
        "owner": owner,
        "session_id": session_id,
        "goal_id": goal_id,
        "payload": payload,
        "request": create_request,
        "routine_service": routine_service,
    }


@pytest.mark.asyncio
async def test_v2_repository_prepare_approve_install_uses_raw_provenance_digest(
    async_db,
    monkeypatch,
    tmp_path,
):
    from src.approval.repository import approval_repository

    prepared = await _prepare_repository_v2_install(
        async_db,
        monkeypatch,
        tmp_path,
        idempotency_key="repository-install-success",
    )
    payload = prepared["payload"]
    approval = await approval_repository.resolve(payload["approval_id"], "approved")
    assert approval is not None and approval.status == "approved"

    installed = await prepared["routine_service"].install(
        payload["routine_id"],
        RoutineInstallRequest(
            version=1,
            expected_routine_revision=1,
            approval_id=payload["approval_id"],
        ),
        owner_principal_id=prepared["owner"],
        owner_session_id=prepared["session_id"],
    )

    assert installed["state"] == "installed"
    assert installed["current_version"] == 1
    assert installed["package"]["status"] == "not_reviewed"
    assert installed["versions"][0]["installed_package_digest"]
    job = await durable_job_repository.get_job(payload["install_job_id"])
    assert job is not None and job["status"] == "succeeded"
    async with db_engine.get_session() as db:
        version = (
            await db.execute(
                select(GuardianRoutineVersion).where(
                    GuardianRoutineVersion.routine_id == payload["routine_id"],
                    GuardianRoutineVersion.version == 1,
                )
            )
        ).scalar_one()
        expected_raw_digest = hashlib.sha256(version.source_provenance_json.encode("utf-8")).hexdigest()
    assert job["declared_authority"]["source_provenance_sha256"] == expected_raw_digest


@pytest.mark.asyncio
async def test_v2_repository_install_rejects_tampered_provenance_before_package_write(
    async_db,
    monkeypatch,
    tmp_path,
):
    from src.approval.repository import approval_repository

    prepared = await _prepare_repository_v2_install(
        async_db,
        monkeypatch,
        tmp_path,
        idempotency_key="repository-install-tamper",
    )
    payload = prepared["payload"]
    approval = await approval_repository.resolve(payload["approval_id"], "approved")
    assert approval is not None and approval.status == "approved"

    async with db_engine.get_session() as db:
        version = (
            await db.execute(
                select(GuardianRoutineVersion).where(
                    GuardianRoutineVersion.routine_id == payload["routine_id"],
                    GuardianRoutineVersion.version == 1,
                )
            )
        ).scalar_one()
        tampered = json.loads(version.source_provenance_json)
        tampered["tampered_for_test"] = True
        version.source_provenance_json = json.dumps(
            tampered,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    with pytest.raises(RoutineError) as exc_info:
        await prepared["routine_service"].install(
            payload["routine_id"],
            RoutineInstallRequest(
                version=1,
                expected_routine_revision=1,
                approval_id=payload["approval_id"],
            ),
            owner_principal_id=prepared["owner"],
            owner_session_id=prepared["session_id"],
        )

    assert exc_info.value.code == "routine_install_binding_stale"
    job = await durable_job_repository.get_job(payload["install_job_id"])
    assert job is not None and job["status"] == "awaiting_approval"
    async with db_engine.get_session() as db:
        version = (
            await db.execute(
                select(GuardianRoutineVersion).where(
                    GuardianRoutineVersion.routine_id == payload["routine_id"],
                    GuardianRoutineVersion.version == 1,
                )
            )
        ).scalar_one()
        assert version.installed_package_digest is None


@pytest.mark.asyncio
async def test_committed_preparation_replays_before_expired_source_resolution(async_db, monkeypatch):
    req = _create_request()
    binding = ProcedureV2Binding(
        owner_principal_id="operator:test",
        owner_session_id="session:test",
        idempotency_key=req.idempotency_key,
        request_digest=_procedure_create_request_digest(req),
        source_refs_json=json.dumps([{"task_id": "source-task", "task_revision": 7}]),
        deterministic_routine_id="routine-prepared",
        routine_name=req.name,
        template_id=req.template_id,
        version_id="version-prepared",
        preview_digest=req.preview_digest,
        preview_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        state="prepared",
        revision=2,
    )
    async with db_engine.get_session() as db:
        db.add(binding)

    service = ProcedureV2Service(SimpleNamespace())
    service._resolve_sources = AsyncMock(side_effect=AssertionError("prepared replay resolved expired source"))
    service._binding_response = AsyncMock(return_value={"status": "prepared", "binding_id": binding.binding_id})

    payload, status_code = await service.create_from_tasks(
        req,
        owner_principal_id="operator:test",
        owner_session_id="session:test",
    )

    assert status_code == 200
    assert payload == {"status": "prepared", "binding_id": binding.binding_id}
    service._resolve_sources.assert_not_awaited()


@pytest.mark.asyncio
async def test_committed_preparation_rejects_same_key_request_drift_without_resolution(async_db):
    req = _create_request()
    binding = ProcedureV2Binding(
        owner_principal_id="operator:test",
        owner_session_id="session:test",
        idempotency_key=req.idempotency_key,
        request_digest=_procedure_create_request_digest(req),
        source_refs_json=json.dumps([{"task_id": "source-task", "task_revision": 7}]),
        deterministic_routine_id="routine-prepared-drift",
        routine_name=req.name,
        template_id=req.template_id,
        version_id="version-prepared-drift",
        preview_digest=req.preview_digest,
        preview_expires_at=datetime.now(timezone.utc),
        state="prepared",
        revision=2,
    )
    async with db_engine.get_session() as db:
        db.add(binding)

    service = ProcedureV2Service(SimpleNamespace())
    service._resolve_sources = AsyncMock(side_effect=AssertionError("drift resolved source"))
    drifted = req.model_copy(update={"name": "Different procedure"})

    with pytest.raises(ProcedureV2Error) as exc_info:
        await service.create_from_tasks(
            drifted,
            owner_principal_id="operator:test",
            owner_session_id="session:test",
        )

    assert exc_info.value.code == "procedure_binding_conflict"
    service._resolve_sources.assert_not_awaited()


@pytest.mark.asyncio
async def test_invocation_parameters_keep_strict_nested_goal_revision():
    descriptor = V2VersionDescriptor(
        routine_id="routine",
        version_id="version",
        version=1,
        owner_principal_id="operator:test",
        owner_session_id="session:test",
        routine_revision=1,
        template_id="public-browser-check",
        schema_version=2,
        plan_digest="a" * 64,
        source_proof_digest="b" * 64,
        preview_digest="c" * 64,
        installed_package_digest="d" * 64,
        plan={},
        source_refs=(),
        capability_versions=("1",),
        parameter_schema=(),
    )
    service = ProcedureV2Service(SimpleNamespace())
    with pytest.raises(ProcedureV2Error) as exc_info:
        await service._validate_parameters(
            descriptor,
            goal_id="goal",
            expected_goal_revision=1,
            parameters={"goal_id": "goal", "expected_goal_revision": "1"},
        )
    assert exc_info.value.code == "procedure_goal_binding_mismatch"


@pytest.mark.asyncio
async def test_invoke_receipt_uses_persisted_task_goal_on_idempotent_replay(async_db, monkeypatch, tmp_path):
    """Fresh and replayed invoke receipts expose the durable task identity."""

    from types import MappingProxyType

    from config.settings import settings

    owner = "operator:invoke-receipt"
    session_id = "session:invoke-receipt"
    goal_id = "goal:invoke-receipt"
    routine_id = "routine:invoke-receipt"
    invocation_uuid = "11111111-1111-4111-8111-111111111111"
    parameters = {"goal_id": goal_id, "expected_goal_revision": 3}
    version = V2VersionDescriptor(
        routine_id=routine_id,
        version_id="version:invoke-receipt",
        version=1,
        owner_principal_id=owner,
        owner_session_id=session_id,
        routine_revision=4,
        template_id="public-browser-check",
        schema_version=2,
        plan_digest="a" * 64,
        source_proof_digest="b" * 64,
        preview_digest="c" * 64,
        installed_package_digest="d" * 64,
        plan={},
        source_refs=(),
        capability_versions=("1",),
        parameter_schema=(),
    )
    descriptor = V2InvocationDescriptor(
        version=version,
        goal_id=goal_id,
        goal_revision=3,
        parameters=MappingProxyType(parameters),
        invocation_uuid=invocation_uuid,
        scope="procedure-v2:routine:invoke-receipt:version:invoke-receipt",
        executable_steps=MappingProxyType({}),
    )
    request = ProcedureV2InvokeRequest.model_validate(
        {
            "version": 1,
            "expected_routine_revision": 4,
            "goal_id": goal_id,
            "expected_goal_revision": 3,
            "parameters": parameters,
            "invocation_uuid": invocation_uuid,
        }
    )

    async with db_engine.get_session() as db:
        db.add(Session(id=session_id, owner_principal_id=owner))
        db.add(
            Goal(
                id=goal_id,
                title="Invoke receipt goal",
                status="active",
                revision=3,
                owner_principal_id=owner,
                owner_session_id=session_id,
            )
        )

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    service = ProcedureV2Service(SimpleNamespace())
    service.validate_v2_invocation_authority = AsyncMock(return_value=descriptor)
    service._assert_publication_authority = AsyncMock()

    fresh, fresh_status = await service.invoke_v2(
        routine_id,
        request,
        owner_principal_id=owner,
        owner_session_id=session_id,
    )
    assert fresh_status == 201
    assert fresh["goal_id"] == goal_id
    assert fresh["goal_revision"] == 3

    replay, replay_status = await service.invoke_v2(
        routine_id,
        request,
        owner_principal_id=owner,
        owner_session_id=session_id,
    )
    assert replay_status == 200
    assert replay["task_id"] == fresh["task_id"]
    assert replay["goal_id"] == fresh["goal_id"] == goal_id
    assert replay["goal_revision"] == fresh["goal_revision"] == 3

    async with db_engine.get_session() as db:
        task = (await db.execute(select(WorkBoardTask))).scalar_one()
        assert task.status is WorkBoardStatus.todo
        assert task.goal_id == fresh["goal_id"]
        assert task.goal_revision == fresh["goal_revision"]


def _publication_descriptor(*, owner: str, session_id: str, goal_id: str, routine_id: str, invocation_uuid: str):
    version = V2VersionDescriptor(
        routine_id=routine_id,
        version_id=f"version:{routine_id}",
        version=1,
        owner_principal_id=owner,
        owner_session_id=session_id,
        routine_revision=4,
        template_id="public-browser-check",
        schema_version=2,
        plan_digest="a" * 64,
        source_proof_digest="b" * 64,
        preview_digest="c" * 64,
        installed_package_digest="d" * 64,
        plan={},
        source_refs=(),
        capability_versions=("1",),
        parameter_schema=(),
    )
    parameters = {"goal_id": goal_id, "expected_goal_revision": 3}
    return V2InvocationDescriptor(
        version=version,
        goal_id=goal_id,
        goal_revision=3,
        parameters=MappingProxyType(parameters),
        invocation_uuid=invocation_uuid,
        scope=f"procedure-v2:{routine_id}:{version.version_id}",
        executable_steps=MappingProxyType({}),
    )


def _publication_request(*, goal_id: str, invocation_uuid: str) -> ProcedureV2InvokeRequest:
    return ProcedureV2InvokeRequest.model_validate(
        {
            "version": 1,
            "expected_routine_revision": 4,
            "goal_id": goal_id,
            "expected_goal_revision": 3,
            "parameters": {"goal_id": goal_id, "expected_goal_revision": 3},
            "invocation_uuid": invocation_uuid,
        }
    )


async def _seed_publication_goal(async_db, *, owner: str, session_id: str, goal_id: str):
    async with db_engine.get_session() as db:
        db.add(Session(id=session_id, owner_principal_id=owner))
        db.add(
            Goal(
                id=goal_id,
                title="Procedure publication test goal",
                status="active",
                revision=3,
                owner_principal_id=owner,
                owner_session_id=session_id,
            )
        )


@pytest.mark.asyncio
async def test_invoke_known_authority_failure_revokes_unpublished_artifact(
    async_db, monkeypatch, tmp_path
):
    from config.settings import settings

    owner_id = "operator:publication-known"
    session_id = "session:publication-known"
    goal_id = "goal:publication-known"
    invocation_uuid = "22222222-2222-4222-8222-222222222222"
    await _seed_publication_goal(async_db, owner=owner_id, session_id=session_id, goal_id=goal_id)
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))

    descriptor = _publication_descriptor(
        owner=owner_id,
        session_id=session_id,
        goal_id=goal_id,
        routine_id="routine:publication-known",
        invocation_uuid=invocation_uuid,
    )
    service = ProcedureV2Service(SimpleNamespace())
    service.validate_v2_invocation_authority = AsyncMock(return_value=descriptor)
    service._assert_publication_authority = AsyncMock(
        side_effect=BoardError("procedure_authority_stale", "private source should not escape", status_code=409)
    )

    with pytest.raises(ProcedureV2Error) as exc_info:
        await service.invoke_v2(
            descriptor.version.routine_id,
            _publication_request(goal_id=goal_id, invocation_uuid=invocation_uuid),
            owner_principal_id=owner_id,
            owner_session_id=session_id,
        )

    assert exc_info.value.code == "procedure_authority_stale"
    assert exc_info.value.recovery_action == "retry_with_new_invocation"
    assert "private source" not in str(exc_info.value)
    async with db_engine.get_session() as db:
        artifact = (await db.execute(select(WorkBoardInputArtifact))).scalar_one()
        assert artifact.state == "revoked"
        assert int(artifact.revision) == 3
        assert (await db.execute(select(WorkBoardTask))).scalars().all() == []
        assert not Path(str(settings.workspace_dir) + "/" + artifact.typed_input_ref.split(":", 1)[1]).exists()


@pytest.mark.asyncio
async def test_unknown_invoke_publication_outcome_keeps_exact_key_for_replay(
    async_db, monkeypatch, tmp_path
):
    from config.settings import settings

    owner_id = "operator:publication-unknown"
    session_id = "session:publication-unknown"
    goal_id = "goal:publication-unknown"
    invocation_uuid = "33333333-3333-4333-8333-333333333333"
    await _seed_publication_goal(async_db, owner=owner_id, session_id=session_id, goal_id=goal_id)
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))

    descriptor = _publication_descriptor(
        owner=owner_id,
        session_id=session_id,
        goal_id=goal_id,
        routine_id="routine:publication-unknown",
        invocation_uuid=invocation_uuid,
    )
    service = ProcedureV2Service(SimpleNamespace())
    service.validate_v2_invocation_authority = AsyncMock(return_value=descriptor)
    service._assert_publication_authority = AsyncMock()
    repository_create = WorkBoardRepository.create_task

    async def unknown_create(*args, **kwargs):
        raise RuntimeError("database commit outcome unknown")

    monkeypatch.setattr(WorkBoardRepository, "create_task", unknown_create)
    request = _publication_request(goal_id=goal_id, invocation_uuid=invocation_uuid)
    with pytest.raises(ProcedureV2Error) as exc_info:
        await service.invoke_v2(
            descriptor.version.routine_id,
            request,
            owner_principal_id=owner_id,
            owner_session_id=session_id,
        )
    assert exc_info.value.code == "procedure_publication_outcome_unknown"
    assert exc_info.value.recovery_action == "reconcile_publication"

    monkeypatch.setattr(WorkBoardRepository, "create_task", repository_create)
    replay, status_code = await service.invoke_v2(
        descriptor.version.routine_id,
        request,
        owner_principal_id=owner_id,
        owner_session_id=session_id,
    )
    assert status_code == 201
    assert replay["invocation_uuid"] == invocation_uuid
    async with db_engine.get_session() as db:
        artifact = (await db.execute(select(WorkBoardInputArtifact))).scalar_one()
        assert artifact.state == "bound"
        assert artifact.bound_task_id == replay["task_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_kind", ["task", "schedule"])
async def test_publication_cleanup_protects_concurrent_canonical_reference(
    async_db, monkeypatch, tmp_path, reference_kind
):
    from config.settings import settings
    owner_id = "operator:publication-reference"
    session_id = "session:publication-reference"
    goal_id = "goal:publication-reference"
    await _seed_publication_goal(async_db, owner=owner_id, session_id=session_id, goal_id=goal_id)
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    owner = WorkBoardOwner(principal_id=owner_id, session_id=session_id)
    request = WorkBoardInputArtifactCreate(
        schema_version=1,
        capability_id="guardian-routine.v2",
        goal_id=goal_id,
        goal_revision=3,
        input={
            "routine_id": "routine:publication-reference",
            "version": 1,
            "expected_routine_revision": 1,
            "goal_id": goal_id,
            "expected_goal_revision": 3,
            "parameters": {},
            "invocation_uuid": "ref-key",
        },
        idempotency_key="ref-key",
    )
    async with db_engine.get_session() as db:
        artifact = await prepare_input_artifact(db, owner, request)
    async with db_engine.get_session() as db:
        if reference_kind == "task":
            db.add(
                WorkBoardTask(
                    task_id="published-task-ref",
                    owner_principal_id=owner_id,
                    owner_session_id=session_id,
                    goal_id=goal_id,
                    goal_revision=3,
                    title="Published reference",
                    status=WorkBoardStatus.todo,
                    capability_id="guardian-routine.v2",
                    input_artifact_id=artifact.artifact_id,
                    idempotency_scope="publication-test",
                    idempotency_key="published-task-ref",
                )
            )
        else:
            db.add(
                GovernedScheduleBinding(
                    binding_id="published-schedule-ref",
                    scheduled_job_id="published-schedule-job",
                    owner_principal_id=owner_id,
                    owner_session_id=session_id,
                    goal_id=goal_id,
                    goal_revision=3,
                    input_artifact_id=artifact.artifact_id,
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                    schedule_idempotency_key="published-schedule-ref",
                )
            )

    cleanup = await _cleanup_unpublished_procedure_artifact(
        owner,
        artifact_id=artifact.artifact_id,
        expected_revision=artifact.revision,
    )
    assert cleanup.outcome == "protected"
    async with db_engine.get_session() as db:
        current = await db.get(WorkBoardInputArtifact, artifact.artifact_id)
        assert current is not None and current.state == "pending"
        path = Path(str(settings.workspace_dir) + "/" + current.typed_input_ref.split(":", 1)[1])
        assert path.exists()


def test_http_json_boundary_accepts_arrays_and_iso_expiry_without_relaxing_scalars():
    preview = ProcedureV2PreviewRequest.model_validate(
        {
            "template_id": "public-browser-check",
            "source_tasks": [{"task_id": "task", "expected_revision": 1}],
            "name": "A procedure",
            "idempotency_key": "key",
        }
    )
    assert isinstance(preview.source_tasks, list)

    schedule = ProcedureV2ScheduleRequest.model_validate(
        {
            "version": 1,
            "expected_routine_revision": 2,
            "goal_id": "goal",
            "expected_goal_revision": 3,
            "parameters": {"goal_id": "goal", "expected_goal_revision": 3},
            "cadence": {"kind": "hourly", "timezone": "UTC"},
            "expires_at": "2026-10-08T09:00:00Z",
            "idempotency_key": "schedule",
        }
    )
    assert schedule.expires_at.tzinfo is not None

    with pytest.raises(ValueError):
        ProcedureV2ScheduleRequest.model_validate(
            {
                "version": "1",
                "expected_routine_revision": 2,
                "goal_id": "goal",
                "expected_goal_revision": 3,
                "parameters": {"goal_id": "goal", "expected_goal_revision": 3},
                "cadence": {"kind": "hourly", "timezone": "UTC"},
                "expires_at": "2026-10-08T09:00:00Z",
                "idempotency_key": "schedule",
            }
        )


def test_goal_budget_snapshot_is_the_shared_canonical_digest():
    budget = GoalAdmissionBudget(
        reviewed_grant=True,
        grant_id="grant-1",
        max_outstanding_jobs=2,
        max_attempts=2,
        max_runtime_seconds=120,
        period_started_at=datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc),
        period_expires_at=datetime(2026, 10, 8, 0, 0, tzinfo=timezone.utc),
    )
    first = goal_admission_budget_snapshot(goal_id="goal-1", goal_revision=4, budget=budget)
    second = goal_admission_budget_snapshot(goal_id="goal-1", goal_revision=4, budget=budget.model_dump(mode="json"))

    assert isinstance(first, GoalAdmissionBudgetSnapshot)
    assert first.digest == second.digest
    assert first.digest != goal_admission_budget_snapshot(
        goal_id="goal-1",
        goal_revision=5,
        budget=budget,
    ).digest


def _schedule_budget_goal(raw_budget: str | None, *, proactive_enabled: bool = True) -> Goal:
    return Goal(
        id="goal-schedule-budget",
        title="Schedule budget goal",
        status="active",
        revision=4,
        proactive_enabled=proactive_enabled,
        owner_principal_id="operator:test",
        owner_session_id="session:test",
        admission_budget_json=raw_budget,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_budget", "expected_code"),
    [
        (None, "procedure_goal_budget_missing"),
        ("{not-json", "procedure_goal_budget_invalid"),
        (
            GoalAdmissionBudget(
                reviewed_grant=False,
                period_expires_at=datetime.now(timezone.utc) + timedelta(days=2),
            ).model_dump_json(),
            "procedure_goal_budget_missing_reviewed_grant",
        ),
        (
            GoalAdmissionBudget(
                reviewed_grant=True,
                grant_id="grant-expired",
                period_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            ).model_dump_json(),
            "procedure_goal_budget_period_expired",
        ),
        (
            GoalAdmissionBudget(reviewed_grant=True, grant_id="grant-no-expiry").model_dump_json(),
            "procedure_goal_budget_finite_expiry_required",
        ),
    ],
)
async def test_schedule_budget_rejection_has_no_binding_or_artifact(
    async_db,
    raw_budget: str | None,
    expected_code: str,
):
    async with db_engine.get_session() as db:
        db.add(_schedule_budget_goal(raw_budget))

    descriptor = SimpleNamespace(
        version=SimpleNamespace(template_id="public-browser-check", version_id="version-1")
    )
    service = ProcedureV2Service(SimpleNamespace())
    service.validate_v2_invocation_authority = AsyncMock(return_value=descriptor)
    service._prepare_schedule_artifact = AsyncMock(side_effect=AssertionError("budget rejection created artifact"))
    request = ProcedureV2ScheduleRequest.model_validate(
        {
            "version": 1,
            "expected_routine_revision": 1,
            "goal_id": "goal-schedule-budget",
            "expected_goal_revision": 4,
            "parameters": {"goal_id": "goal-schedule-budget", "expected_goal_revision": 4},
            "cadence": {"kind": "hourly", "timezone": "UTC"},
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "idempotency_key": f"schedule-{expected_code}",
        }
    )

    with pytest.raises(ProcedureV2Error) as exc_info:
        await service.schedule_v2(
            "routine-1",
            request,
            owner_principal_id="operator:test",
            owner_session_id="session:test",
        )

    assert exc_info.value.code == expected_code
    service._prepare_schedule_artifact.assert_not_awaited()
    async with db_engine.get_session() as db:
        assert (await db.execute(select(GovernedScheduleBinding))).scalars().all() == []
        assert (await db.execute(select(ScheduledJob))).scalars().all() == []
        assert (await db.execute(select(WorkBoardInputArtifact))).scalars().all() == []


@pytest.mark.asyncio
async def test_schedule_expiry_cannot_outlive_finite_goal_budget(async_db):
    budget = GoalAdmissionBudget(
        reviewed_grant=True,
        grant_id="grant-short",
        period_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    async with db_engine.get_session() as db:
        db.add(Session(id="session:test", owner_principal_id="operator:test"))
        db.add(_schedule_budget_goal(budget.model_dump_json()))

    descriptor = SimpleNamespace(
        version=SimpleNamespace(template_id="public-browser-check", version_id="version-1")
    )
    service = ProcedureV2Service(SimpleNamespace())
    service.validate_v2_invocation_authority = AsyncMock(return_value=descriptor)
    service._prepare_schedule_artifact = AsyncMock(side_effect=AssertionError("budget expiry created artifact"))
    request = ProcedureV2ScheduleRequest.model_validate(
        {
            "version": 1,
            "expected_routine_revision": 1,
            "goal_id": "goal-schedule-budget",
            "expected_goal_revision": 4,
            "parameters": {"goal_id": "goal-schedule-budget", "expected_goal_revision": 4},
            "cadence": {"kind": "hourly", "timezone": "UTC"},
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
            "idempotency_key": "schedule-beyond-budget",
        }
    )

    with pytest.raises(ProcedureV2Error) as exc_info:
        await service.schedule_v2(
            "routine-1",
            request,
            owner_principal_id="operator:test",
            owner_session_id="session:test",
        )

    assert exc_info.value.code == "procedure_schedule_expiry_exceeds_goal_budget"
    service._prepare_schedule_artifact.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_pins_goal_budget_digest_and_null_source_consent(async_db, monkeypatch):
    budget = GoalAdmissionBudget(
        reviewed_grant=True,
        grant_id="grant-schedule",
        max_outstanding_jobs=2,
        max_attempts=2,
        max_runtime_seconds=120,
        period_expires_at=datetime.now(timezone.utc) + timedelta(days=3),
    )
    async with db_engine.get_session() as db:
        db.add(Session(id="session:test", owner_principal_id="operator:test"))
        db.add(_schedule_budget_goal(budget.model_dump_json()))

    descriptor = SimpleNamespace(
        version=SimpleNamespace(
            template_id="public-browser-check",
            version_id="version-1",
            routine_id="routine-1",
            version=1,
            routine_revision=1,
        )
    )
    service = ProcedureV2Service(SimpleNamespace())
    service.validate_v2_invocation_authority = AsyncMock(return_value=descriptor)
    service._prepare_schedule_artifact = AsyncMock(
        return_value=SimpleNamespace(artifact_id="artifact-schedule", typed_input_digest="a" * 64)
    )
    monkeypatch.setattr(
        "src.scheduler.governed_schedules.action_spec",
        lambda _action: {"capability_id": "guardian.run_procedure.v2", "consent_kind": "goal_budget", "enabled": True},
    )
    request = ProcedureV2ScheduleRequest.model_validate(
        {
            "version": 1,
            "expected_routine_revision": 1,
            "goal_id": "goal-schedule-budget",
            "expected_goal_revision": 4,
            "parameters": {"goal_id": "goal-schedule-budget", "expected_goal_revision": 4},
            "cadence": {"kind": "hourly", "timezone": "UTC"},
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "idempotency_key": "schedule-pinned-budget",
        }
    )

    payload, status_code = await service.schedule_v2(
        "routine-1",
        request,
        owner_principal_id="operator:test",
        owner_session_id="session:test",
    )

    assert status_code == 201
    assert payload["status"] == "scheduled"
    assert payload["goal_id"] == "goal-schedule-budget"
    assert payload["goal_revision"] == 4
    assert payload["schedule_idempotency_key"] == request.idempotency_key
    expected_digest = goal_admission_budget_snapshot(
        goal_id="goal-schedule-budget",
        goal_revision=4,
        budget=budget,
    ).digest
    async with db_engine.get_session() as db:
        job = await db.get(ScheduledJob, payload["scheduled_job_id"])
        binding = await db.get(GovernedScheduleBinding, payload["binding_id"])
        assert job is not None and binding is not None
        action_spec = json.loads(job.action_spec_json)
        assert action_spec["consent_id"] is None
        assert action_spec["goal_budget_digest"] == expected_digest
        assert action_spec["goal_budget_max_outstanding_jobs"] == 2
        assert action_spec["goal_budget_max_attempts"] == 2
        assert action_spec["goal_budget_max_runtime_seconds"] == 120
        assert binding.read_consent_id is None
        assert binding.consent_digest == expected_digest
        assert payload["goal_revision"] == binding.goal_revision
        assert payload["schedule_idempotency_key"] == binding.schedule_idempotency_key

    replay, replay_status = await service.schedule_v2(
        "routine-1",
        request,
        owner_principal_id="operator:test",
        owner_session_id="session:test",
    )
    assert replay_status == 200
    for field in (
        "scheduled_job_id",
        "binding_id",
        "goal_id",
        "goal_revision",
        "schedule_idempotency_key",
        "input_digest",
    ):
        assert replay[field] == payload[field]

    conflicting_request = request.model_copy(
        update={
            "parameters": {
                "goal_id": "goal-schedule-budget",
                "expected_goal_revision": 4,
                "changed": True,
            }
        }
    )
    with pytest.raises(ProcedureV2Error) as conflict:
        await service.schedule_v2(
            "routine-1",
            conflicting_request,
            owner_principal_id="operator:test",
            owner_session_id="session:test",
        )
    assert conflict.value.code == "procedure_schedule_conflict"
    assert conflict.value.status_code == 409


@pytest.mark.asyncio
async def test_schedule_known_final_authority_failure_revokes_unpublished_artifact(
    async_db, monkeypatch, tmp_path
):
    from config.settings import settings

    owner_id = "operator:schedule-publication"
    session_id = "session:schedule-publication"
    goal_id = "goal:schedule-publication"
    budget = GoalAdmissionBudget(
        reviewed_grant=True,
        grant_id="grant-schedule-publication",
        max_outstanding_jobs=2,
        max_attempts=2,
        max_runtime_seconds=120,
        period_expires_at=datetime.now(timezone.utc) + timedelta(days=3),
    )
    async with db_engine.get_session() as db:
        db.add(Session(id=session_id, owner_principal_id=owner_id))
        db.add(
            Goal(
                id=goal_id,
                title="Schedule publication goal",
                status="active",
                revision=4,
                proactive_enabled=True,
                owner_principal_id=owner_id,
                owner_session_id=session_id,
                admission_budget_json=budget.model_dump_json(),
            )
        )
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(
        "src.scheduler.governed_schedules.action_spec",
        lambda _action: {"capability_id": "guardian.run_procedure.v2", "consent_kind": "goal_budget", "enabled": True},
    )
    parameters = {"goal_id": goal_id, "expected_goal_revision": 4}
    descriptor = SimpleNamespace(
        version=SimpleNamespace(
            template_id="public-browser-check",
            version_id="version:schedule-publication",
            routine_id="routine:schedule-publication",
            version=1,
            routine_revision=1,
        ),
        goal_id=goal_id,
        goal_revision=4,
        parameters=MappingProxyType(parameters),
    )
    service = ProcedureV2Service(SimpleNamespace())
    service.validate_v2_invocation_authority = AsyncMock(return_value=descriptor)
    original_validator = service._validate_schedule_goal_budget
    validator_calls = 0

    def reject_final_budget(goal, **kwargs):
        nonlocal validator_calls
        validator_calls += 1
        if validator_calls == 2:
            raise ProcedureV2Error(
                "procedure_goal_budget_changed",
                "private budget detail must not escape",
                recovery_action="refresh_goal_budget",
            )
        return original_validator(goal, **kwargs)

    service._validate_schedule_goal_budget = reject_final_budget
    request = ProcedureV2ScheduleRequest.model_validate(
        {
            "version": 1,
            "expected_routine_revision": 1,
            "goal_id": goal_id,
            "expected_goal_revision": 4,
            "parameters": parameters,
            "cadence": {"kind": "hourly", "timezone": "UTC"},
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "idempotency_key": "schedule-known-publication-failure",
        }
    )

    with pytest.raises(ProcedureV2Error) as exc_info:
        await service.schedule_v2(
            descriptor.version.routine_id,
            request,
            owner_principal_id=owner_id,
            owner_session_id=session_id,
        )

    assert exc_info.value.code == "procedure_goal_budget_changed"
    assert exc_info.value.recovery_action == "use_new_idempotency_key"
    assert "private budget detail" not in str(exc_info.value)
    async with db_engine.get_session() as db:
        artifact = (await db.execute(select(WorkBoardInputArtifact))).scalar_one()
        assert artifact.state == "revoked"
        assert (await db.execute(select(GovernedScheduleBinding))).scalars().all() == []
        assert (await db.execute(select(ScheduledJob))).scalars().all() == []


def test_v2_package_runbook_and_manifest_are_derived_from_the_plan():
    plan = build_procedure_plan(
        "public-browser-check",
        step_inputs={"public_browser_check": _step_input("public_browser_check")},
    )
    provenance = {
        "schema_version": 2,
        "template_id": "public-browser-check",
        "source_refs": [{"task_id": "task-1", "task_revision": 2}],
        "capability_versions": ["1"],
        "plan_digest": plan_digest(plan),
        "parameter_schema": [
            {"name": "goal_id", "kind": "goal_id", "required": True},
            {"name": "expected_goal_revision", "kind": "goal_revision", "required": True},
        ],
        "plan": plan.model_dump(mode="json"),
        "preview_digest": "a" * 64,
        "source_proof_digest": "b" * 64,
    }
    version = GuardianRoutineVersion(
        id="version-1",
        routine_id="0123456789abcdef0123456789abcdef",
        version=1,
        source_provenance_json=__import__("json").dumps(provenance, sort_keys=True, separators=(",", ":")),
        workflow_bytes="workflow",
        workflow_sha256="c" * 64,
        runbook_bytes="runbook",
        runbook_sha256="d" * 64,
    )

    runbook = _routine_pack_runbook_payload(
        routine_id=version.routine_id,
        version=version,
        provenance=provenance,
    )
    manifest = _routine_pack_manifest_payload(
        routine_id=version.routine_id,
        version=version.version,
        provenance=provenance,
    )

    assert runbook["starter_pack"] == "seraph.guardian-routine.v2"
    assert runbook["procedure"]["plan_digest"] == plan_digest(plan)
    assert manifest["authority"]["tools"] == ["public_browser_check"]
    assert ROUTINE_PACK_RUNBOOK_REFERENCE in manifest["contributes"]["runbooks"]
    validated = _validate_routine_pack_runbook(
        __import__("yaml").safe_dump(runbook, sort_keys=True, allow_unicode=False),
        routine_id=version.routine_id,
        version=version,
    )
    assert validated["procedure"]["template_id"] == "public-browser-check"
