"""Actual private CSV adoption and fixed native document child, no inference."""
import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from tests.test_inference_accounting import accounting_db
from tests.test_general_documents import fixture_bytes, read_request
from tests.test_document_build_native_capacity import build_admission_lifecycle
from tests.test_general_task_planner import prepare, forbid_external_inference
from tests.test_work_board_m6_provider_free_journey import _goal


async def claimed_document_parent(service, dispatcher, sessions, task, owner):
    from src.work_board.contracts import GeneralTaskEnvelope, WorkBoardActionRequest
    from src.work_board.general_task_runtime_artifacts import initial_native_manifest
    from src.workflows.job_runtime import _digest
    repository, jobs = service.repository, dispatcher.jobs
    async with sessions() as db:
        promoted = await repository.action_task(db, owner, task.task_id,
            WorkBoardActionRequest(action="promote", expected_revision=task.task_revision))
    task = promoted.task
    async with sessions() as db:
        ready = await repository.promote_task_ready(db, task.task_id,
            expected_revision=task.task_revision, actor_principal_id=dispatcher.runner_id,
            actor_session_id=dispatcher.runner_session)
    async with sessions() as db:
        claimed = await repository.claim_ready_task(db, task.task_id,
            expected_revision=ready.task.task_revision, lease_owner=dispatcher.runner_id)
    task, attempt = claimed.task, claimed.attempt
    spec, inputs, *_ = dispatcher._build_spec(task, attempt)
    admitted = await jobs.admit_job(spec)
    async with sessions() as db:
        linked = await repository.link_attempt_workflow_run(db, task.task_id, attempt.attempt_id,
            workflow_run_id=spec.identity.job_id, expected_revision=task.task_revision,
            board_fence=attempt.fencing_token, lease_owner=attempt.lease_owner,
            workflow_projection=admitted, expected_identity={
                "job_id": spec.identity.job_id, "owner_kind": spec.identity.owner_kind,
                "owner_principal_id": spec.identity.owner_principal_id, "service_id": spec.service_id,
                "operator_session_id": spec.operator_session_id, "session_id": spec.session_id,
                "goal_id": spec.goal_id, "goal_revision": spec.goal_revision,
                "job_kind": spec.identity.job_kind, "capability_version": spec.identity.capability_version,
                "input_digest": _digest(spec.inputs), "authority_digest": _digest(spec.declared_authority),
                "run_fingerprint": spec.run_fingerprint, "idempotency_scope": spec.identity.idempotency_scope,
                "idempotency_key": spec.identity.idempotency_key})
    await jobs.queue_job(spec.identity.job_id)
    claimed = await jobs.claim_job(spec.identity.job_id,
        owner=dispatcher.runner_id + ":" + attempt.attempt_id)
    envelope = GeneralTaskEnvelope.model_validate(inputs)
    async with sessions() as db:
        parent = await jobs._fetch(db, spec.identity.job_id)
        manifest = initial_native_manifest(parent, linked.task, linked.attempt, envelope)
    current = await jobs.replace_general_task_manifest(spec.identity.job_id, manifest=manifest,
        owner=claimed["lease"]["owner"], fencing_token=claimed["lease"]["fencing_token"],
        expected_revision=claimed["revision"])
    return envelope, current


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "owner", "root", "source_ref", "descriptor", "input", "fence", "capability", "kind", "version", "depth", "tool_input_ref", "late_source"])
async def test_actual_document_child_private_readback_and_precontact_denials(accounting_db, monkeypatch, change, build_admission_lifecycle):
    from src.api import documents
    from src.auth.service import create_session, authenticate_token
    from src.db.models import WorkBoardTask
    from src.native_tools.registry import ToolRegistry
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.document_preparation import PreparationCreate, propose, invoke
    from src.work_board.general_task import GeneralTaskService
    from src.work_board.general_task_native import admit_native_step, publish_positive_claim, run_native_step
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.work_board import document_pairs as sources
    from src.workflows.job_runtime import _digest
    from src.vault import crypto
    from config.settings import settings
    jobs, _ = await prepare(accounting_db, monkeypatch)
    token, operator = await create_session()
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    await build_admission_lifecycle.start()
    workspace, _, factory = accounting_db
    from src.work_board.channel_capture import staged_captured_source_identity
    @asynccontextmanager
    async def sessions():
        with staged_captured_source_identity():
            async with factory.accounting_sessions() as db:
                yield db
    monkeypatch.setattr(crypto, "_fernet", None)
    monkeypatch.setattr(settings, "vault_encryption_key", "")
    monkeypatch.setattr(documents, "get_session", sessions)
    goal = _goal("native-document", "Prepare one selected private citation")
    goal.owner_principal_id, goal.owner_session_id = owner.principal_id, owner.session_id
    async with sessions() as db:
        db.add(goal)
    registry = ToolRegistry(); registry.start()
    service = GeneralTaskService(registry); service.start()
    dispatcher = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
    app = FastAPI()
    @app.middleware("http")
    async def auth(request, call_next):
        request.state.operator = await authenticate_token(token, touch=False)
        return await call_next(request)
    app.include_router(documents.router, prefix="/api")
    try:
        async with documents.lifespan(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
                raw = fixture_bytes("csv")
                created = await client.post("/api/documents/sources", json={"format": "csv",
                    "source": {"size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
                    "goal_id": goal.id, "goal_revision": 1, "idempotency_key": "native-source", "no_learning": True})
                assert created.status_code == 200, created.text
                state = created.json(); identifier = state["artifact_id"]
                uploaded = await client.put(f"/api/documents/sources/{identifier}/content",
                    params={"expected_revision": state["revision"]}, content=raw,
                    headers={"content-type": "application/octet-stream"})
                assert uploaded.status_code == 200, uploaded.text
                sealed = await client.post(f"/api/documents/sources/{identifier}/seal",
                    params={"expected_revision": uploaded.json()["revision"]})
                assert sealed.status_code == 200, sealed.text
                parsed = await client.post("/api/documents/read", json=read_request("csv", identifier).model_dump())
                assert parsed.status_code == 200 and parsed.json()["status"] == "succeeded", parsed.text
                assert parsed.json()["cleanup"] == "wait_reaped"
                leaf = parsed.json()["evidence"]["sections"][0]["table_cells"][0]
                state = (await client.get(f"/api/documents/sources/{identifier}")).json()
                async with sessions() as db:
                    result = await propose(db, owner, operator, service, PreparationCreate(
                        artifact_ref=state["artifact_ref"], expected_source_revision=state["revision"],
                        citation_refs=[leaf["source_ref"]], acknowledge_local_use=True,
                        idempotency_key="native-prepare"))
                async with sessions() as db:
                    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == result.task.task_id))
                    await service.validate_acceptance(db, owner, task.task_id, task.task_revision)
                envelope, current = await claimed_document_parent(service, dispatcher, sessions, task, owner)
                descriptor = envelope.descriptors[0]
                root_output = await registry.invoke(descriptor, envelope.plan.steps[0].input,
                    principal=replace(operator.principal, job_id=current["job"]["job_id"]),
                    job_id=current["job"]["job_id"], fencing_token=current["job"]["lease"]["fencing_token"])
                assert root_output == {"source_binding": envelope.task_input.document_source.model_dump(mode="json"),
                    "no_learning": True, "provider_contacts": 0}
                binding, _ = await admit_native_step(jobs, current["job"]["job_id"],
                    owner=current["job"]["lease"]["owner"], fence=current["job"]["lease"]["fencing_token"],
                    step=envelope.plan.steps[0], descriptor=descriptor, inputs=envelope.plan.steps[0].input)
                principal = replace(operator.principal, job_id=binding.invocation_id)
                if change is not None and change != "late_source":
                    await jobs.queue_job(binding.invocation_id)
                    claimed = await jobs.claim_job(binding.invocation_id, owner="document-negative")
                    fence = claimed["lease"]["fencing_token"]
                    await publish_positive_claim(jobs, binding, child_owner="document-negative", child_fence=fence)
                    async with sessions() as db:
                        child = await jobs._fetch(db, binding.invocation_id)
                        if change == "owner": principal = replace(principal, principal_id="foreign-owner")
                        elif change == "root": principal = replace(principal, operator_session_id="foreign-root")
                        elif change == "fence": fence += 1
                        elif change == "kind": child.job_kind = "agent.task.v1"
                        elif change == "version": child.capability_version = "2"
                        elif change == "depth": child.branch_depth = 2
                        elif change == "tool_input_ref":
                            arguments = json.loads(child.arguments_json)
                            arguments["typed_input_ref"] = "general-task-input:art_" + "b" * 24
                            child.arguments_json = json.dumps(arguments)
                            child.input_digest = _digest(arguments)
                        elif change in {"descriptor", "input", "capability"}:
                            authority = json.loads(child.declared_authority_json)
                            if change == "capability": authority["capability_id"] = "agent.task.v1"
                            else: authority["general_task_child_binding"][change + "_digest"] = "a" * 64
                            child.declared_authority_json = json.dumps(authority)
                            child.authority_digest = _digest(authority)
                        else:
                            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
                            task.typed_input_ref = "workspace-json:foreign-source.json"
                    reads = []
                    original_read = sources.read_private
                    def monitored_read(*args, **kwargs):
                        reads.append(args[0]); return original_read(*args, **kwargs)
                    monkeypatch.setattr(sources, "read_private", monitored_read)
                    from src.work_board.repository import BoardError
                    from src.workflows.job_runtime import DurableJobLeaseError, DurableJobTransitionError
                    with pytest.raises((BoardError, DurableJobLeaseError, DurableJobTransitionError)):
                        await invoke(principal, binding.invocation_id, fence, envelope.plan.steps[0].input)
                    assert reads == []
                    assert (await jobs.inference_accounting_snapshot())["operation_count"] == 0
                    return
                if change == "late_source":
                    original_document = registry._invoke_document
                    async def changed_source(*args, **kwargs):
                        output = await original_document(*args, **kwargs)
                        from src.db.models import WorkBoardInputArtifact
                        from src.work_board.input_artifacts import _metadata_digest
                        async with sessions() as db:
                            source = await db.get(WorkBoardInputArtifact, identifier)
                            source.revision += 1
                            source.metadata_digest = _metadata_digest(source)
                        return output
                    monkeypatch.setattr(registry, "_invoke_document", changed_source)
                    from src.work_board.repository import BoardError
                    with pytest.raises(BoardError) as denied:
                        await run_native_step(service, jobs, binding,
                            child_owner="document-native-child", principal=operator.principal)
                    assert denied.value.code == "document_pair_revision_conflict"
                    child = await jobs.get_job(binding.invocation_id)
                    assert child["status"] != "succeeded"
                    assert not any(item["artifact_type"] == "general_task_step" for item in child["artifacts"])
                    return
                output, artifact, reference = await run_native_step(service, jobs, binding,
                    child_owner="document-native-child", principal=operator.principal)
                assert output["source_binding"] == envelope.task_input.document_source.model_dump(mode="json")
                assert output["no_learning"] and output["provider_contacts"] == 0
                assert artifact["content_sha256"] == reference.digest
                assert leaf["text"] not in (workspace / artifact["file_path"]).read_text()
                child = await jobs.get_job(binding.invocation_id)
                assert child["status"] == "succeeded" and child["attempt_count"] == 1
                from src.work_board.document_preparation import resolve
                async with sessions() as db:
                    _, selected = await resolve(db, owner, envelope.task_input.document_source, goal_id=goal.id)
                assert selected[0]["text"] == leaf["text"]
                assert (await jobs.inference_accounting_snapshot())["operation_count"] == 0
    finally:
        service.stop(); registry.stop()
