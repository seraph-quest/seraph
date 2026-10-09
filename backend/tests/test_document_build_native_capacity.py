"""Authentic private build/C1 admissions and zero-contact shared-slot claims."""
import asyncio
import json

import pytest
import pytest_asyncio
from sqlalchemy import select

from tests.test_inference_accounting import accounting_db
from tests.test_general_task_planner import forbid_external_inference
from tests.test_document_build_storage import setup, SPEC
from src.db.models import WorkBoardTask, WorkBoardAttempt
from src.native_tools.task_adapters import ToolRegistry
from src.work_board.general_task import GeneralTaskService
from src.work_board.contracts import WorkBoardActionRequest, GeneralTaskEnvelope, WorkBoardTaskPatch
from src.work_board.dispatcher import WorkBoardDispatcher
from src.work_board import document_build_storage as storage
from src.work_board import document_build_native as native
from src.work_board.general_task_native import initialize_interpreter, admit_native_step
from src.workflows.job_runtime import _digest


@pytest_asyncio.fixture
async def build_admission_lifecycle(accounting_db):
    """Keep the real historical admission owner live for native build callers."""
    from src.work_board.historical_method import historical_method_service
    try:
        yield historical_method_service
    finally:
        await historical_method_service.stop()


@pytest.mark.parametrize("field,bad",[("generation",True),("stdin_closed",1),
    ("parser_witness_nlink",True),("stdout_size",native._STDOUT_MAX+1),("invented_authority",True)])
def test_supervision_codec_denies_untyped_or_extended_metadata(field,bad):
    from pydantic import ValidationError
    receipt = native.supervision_maximum()["receipt"]
    receipt[field] = bad
    with pytest.raises(ValidationError):
        native.DocumentSupervisionV1.model_validate(receipt)


async def test_original_late_cancel_supervision_preserves_frozen_native_rows(accounting_db,monkeypatch,
        forbid_external_inference, build_admission_lifecycle):
    from src.db.models import WorkflowRunState
    from src.work_board.general_task_native import run_native_step
    from src.work_board.repository import BoardError
    from src.work_board import dispatcher as dispatch_module
    _token,operator,owner,goal = await setup(accounting_db,monkeypatch)
    await build_admission_lifecycle.start()
    sessions = accounting_db[2].accounting_sessions
    registry = ToolRegistry(); registry.start()
    service = GeneralTaskService(registry); service.start()
    dispatcher = WorkBoardDispatcher(session_provider=sessions,general_tasks=service)
    monkeypatch.setattr(dispatch_module,"_dispatcher",dispatcher)
    waited,release = asyncio.Event(),asyncio.Event()
    original_wait = asyncio.subprocess.Process.wait
    original_publish = storage.publish_supervision
    publication_checks = []
    running = None
    async def actual_wait(process):
        result = await original_wait(process)
        waited.set()
        await release.wait()
        return result
    monkeypatch.setattr(asyncio.subprocess.Process,"wait",actual_wait)
    try:
        binding,identifier = await admitted_build(accounting_db,service,dispatcher,operator,owner,goal,"late-cancel")
        async def publish(db,row,value,producer):
            objects = [(await dispatcher.jobs._fetch(db,identifier)) for identifier in
                (binding.parent_job_id,binding.invocation_id)]
            objects += [await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id)),
                await db.get(WorkBoardAttempt,binding.attempt_id)]
            before = json.dumps([obj.model_dump(mode="json") for obj in objects],sort_keys=True)
            await original_publish(db,row,value,producer)
            assert json.dumps([obj.model_dump(mode="json") for obj in objects],sort_keys=True) == before
            assert value["supervision"]["receipt"]["supervisor_wait_reaped"] is True
            publication_checks.append(True)
        monkeypatch.setattr(storage,"publish_supervision",publish)
        running = asyncio.create_task(run_native_step(service,dispatcher.jobs,binding,
            child_owner="late-original",principal=operator.principal))
        await asyncio.wait_for(waited.wait(),15)
        async with sessions() as db:
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
        cancelled = await dispatcher.jobs.cancel_general_task_native_parent(binding.parent_job_id,
            operator_owner=owner,expected_task_revision=task.task_revision)
        assert cancelled["cancellation"]["state"] != "fully_cancelled"
        release.set()
        with pytest.raises(Exception):
            await running
        assert publication_checks == [True]
        async with sessions() as db:
            row,value = await storage.owned(db,owner,identifier)
            assert native._supervision(value)["child_job_id"] == binding.invocation_id
            assert value["live_writer"] and row.document_reserved_bytes == storage.CHARGE
            assert not value.get("output")
    finally:
        release.set()
        if running is not None and not running.done():
            await asyncio.gather(running,return_exceptions=True)
        service.stop(); registry.stop()


@pytest.mark.parametrize("case",["restart_missing_outer","witness_tamper","witness_fifo","physical_release"])
async def test_actual_supervisor_cleanup_never_settles_unknown_callback(accounting_db,monkeypatch,
        forbid_external_inference,case, build_admission_lifecycle):
    from src.db.models import Goal
    from src.work_board.general_task_native import run_native_step, retain_native_failure
    from src.work_board.repository import BoardError
    from src.workflows.job_runtime import DurableJobRepository
    from src.work_board import dispatcher as dispatch_module
    _token,operator,owner,goal = await setup(accounting_db,monkeypatch)
    await build_admission_lifecycle.start()
    sessions = accounting_db[2].accounting_sessions
    registry = ToolRegistry(); registry.start()
    service = GeneralTaskService(registry); service.start()
    dispatcher = WorkBoardDispatcher(session_provider=sessions,general_tasks=service)
    monkeypatch.setattr(dispatch_module,"_dispatcher",dispatcher)
    def interrupted_publication(*args,**kwargs):
        raise OSError("actual producer interrupted before encrypted output reservation")
    monkeypatch.setattr(storage,"prepare_publications",interrupted_publication)
    if case == "restart_missing_outer":
        async def interrupted_outer(*args,**kwargs):
            raise OSError("actual producer interrupted before outer receipt commit")
        monkeypatch.setattr(storage,"publish_supervision",interrupted_outer)
    try:
        binding,identifier = await admitted_build(accounting_db,service,dispatcher,operator,owner,goal,"cleanup-"+case)
        with pytest.raises(OSError):
            await run_native_step(service,dispatcher.jobs,binding,child_owner="actual-cleanup",principal=operator.principal)
        await retain_native_failure(service,dispatcher.jobs,binding,child_owner="actual-cleanup")
        child_before = await dispatcher.jobs.get_job(binding.invocation_id)
        parent_before = await dispatcher.jobs.get_job(binding.parent_job_id)
        assert any(item["status"] == "unknown" for item in child_before["effects"])
        async with sessions() as db:
            row,value = await storage.owned(db,owner,identifier)
            revision,metadata_digest = row.revision,row.metadata_digest
            assert value["live_writer"] and row.document_reserved_bytes == storage.CHARGE
            if case != "restart_missing_outer":
                body = native._supervision(value)
                assert body["stdin_closed"] and body["stdout_eof"] and body["supervisor_wait_reaped"]
                assert body["stdout_size"] > 12 and body["supervisor_exit"] == 0
                if case in {"witness_tamper","witness_fifo"}:
                    witness = storage.sources.source_path(row,value,"spec").with_name(body["parser_witness_name"])
                    if case == "witness_tamper":
                        raw = witness.read_bytes(); witness.write_bytes(raw[:-1]+b" ")
                    else:
                        import os
                        witness.unlink(); os.mkfifo(witness,0o600)
            else:
                assert "supervision" not in value
            if case == "physical_release":
                await db.delete(await db.get(Goal,goal.id))
        restarted_jobs = DurableJobRepository()
        if case == "physical_release":
            result = await native.reconcile_reap(restarted_jobs,owner,identifier,operator,
                expected_revision=revision,expected_metadata_digest=metadata_digest)
            assert result["cleanup_proven"] and result["build_revision"] == revision+1
        else:
            from time import monotonic
            started = monotonic()
            with pytest.raises((BoardError,ValueError)):
                await native.reconcile_reap(restarted_jobs,owner,identifier,operator,
                    expected_revision=revision,expected_metadata_digest=metadata_digest)
            if case == "witness_fifo":
                assert monotonic()-started < 2, "FIFO must be rejected before any blocking read"
        assert await restarted_jobs.get_job(binding.invocation_id) == child_before
        assert await restarted_jobs.get_job(binding.parent_job_id) == parent_before
        async with sessions() as db:
            row,value = await storage.owned(db,owner,identifier)
            assert row.document_reserved_bytes == storage.CHARGE and not value.get("output")
            if case == "physical_release":
                assert value["live_writer"] is None and native._authenticated_reap(value,value["renderer_binding"])
                child = await restarted_jobs._fetch(db,binding.invocation_id)
                assert await native.held_capacity(db,child) is False
                with pytest.raises(BoardError):
                    await native.validate_outputless_retirement(db,owner,row,value)
            else:
                assert row.revision == revision and row.metadata_digest == metadata_digest and value["live_writer"]
    finally:
        service.stop(); registry.stop()


async def test_genuine_preclaim_reserves_complete_cleanup_headroom(accounting_db, monkeypatch,
        forbid_external_inference, build_admission_lifecycle):
    _token, operator, owner, goal = await setup(accounting_db, monkeypatch)
    await build_admission_lifecycle.start()
    sessions = accounting_db[2].accounting_sessions
    registry = ToolRegistry(); registry.start()
    service = GeneralTaskService(registry); service.start()
    dispatcher = WorkBoardDispatcher(session_provider=sessions,general_tasks=service)
    measurements = []
    original = native.assert_prelaunch_headroom
    def measure(row,value,capacity):
        projected = native.prelaunch_metadata_projection(row,value,capacity)
        encoded = storage.sources.canonical(projected)
        measurements.append(len(encoded))
        print("GENUINE_FULL_METADATA_MAX",len(encoded),"SUPERVISION_MAX",native.SUPERVISION_MAX_BYTES)
        original(row,value,capacity)
    monkeypatch.setattr(native,"assert_prelaunch_headroom",measure)
    try:
        binding,identifier = await admitted_build(accounting_db,service,dispatcher,operator,owner,goal,"headroom")
        claim = await native.stage_claim(service,dispatcher.jobs,binding)
        before = await dispatcher.jobs.get_job(binding.invocation_id)
        try:
            result = await dispatcher.jobs.claim_job(binding.invocation_id,owner="bounded-headroom",claim_authority_check=claim)
        except native.DocumentBuildPreclaimHeld:
            assert await dispatcher.jobs.get_job(binding.invocation_id) == before
            raise
        assert measurements and max(measurements) <= 8192
        assert result["attempt_count"] == 1
        async with sessions() as db:
            row,value = await storage.owned(db,owner,identifier)
            assert value["supervision_max_bytes"] == native.SUPERVISION_MAX_BYTES
            assert row.document_reserved_bytes == 24*1024*1024
            assert value.get("supervision") is None
    finally:
        service.stop(); registry.stop()


async def admitted_build(accounting_db, service, dispatcher, operator, owner, goal, key, *, priority=50):
    from contextlib import asynccontextmanager
    original_sessions = accounting_db[2].accounting_sessions
    @asynccontextmanager
    async def sessions():
        from src.work_board.channel_capture import staged_captured_source_identity
        with staged_captured_source_identity():
            async with original_sessions() as db:
                yield db
    jobs = dispatcher.jobs
    async with sessions() as db:
        created = await storage.create(db, owner, operator, storage.BuildCreate(
            goal_id=goal.id, goal_revision=1, spec=SPEC, idempotency_key=key))
    async with sessions() as db:
        preview = await storage.preview(db, owner, operator, created["build_id"], descriptor=native.descriptor())
        mutation = await storage.prepare(db, owner, operator, service, created["build_id"], storage.BuildPrepare(
            expected_revision=created["revision"], review=preview["review"], idempotency_key=key))
        task_id = mutation.task.task_id
        if priority != mutation.task.priority:
            mutation = await service.repository.patch_task(db, owner, task_id, WorkBoardTaskPatch(
                expected_revision=mutation.task.task_revision, priority=priority))
    async with sessions() as db:
        preview = await storage.preview(db, owner, operator, created["build_id"], descriptor=native.descriptor())
        task = await service.repository.get_task(db, owner, task_id)
        row, value = await storage.owned(db, owner, created["build_id"])
        root = await storage.authority(db, owner, row, value, metadata_only=True)
        expected = storage._review_binding(row, value, native.descriptor(), root=root,
            expires_at=preview["review"]["binding"]["expires_at"], task=task)
        assert preview["review"]["binding"] == expected
        await service.validate_acceptance(db, owner, task_id, task.task_revision,
            document_build_review=preview["review"])
        promoted = await service.repository.action_task(db, owner, task_id, WorkBoardActionRequest(
            action="promote", expected_revision=task.task_revision, document_build_review=preview["review"]))
    async with sessions() as db:
        ready = await service.repository.promote_task_ready(db, task_id, expected_revision=promoted.task.task_revision,
            actor_principal_id=dispatcher.runner_id, actor_session_id=dispatcher.runner_session)
    async with sessions() as db:
        claimed = await service.repository.claim_ready_task(db, task_id,
            expected_revision=ready.task.task_revision, lease_owner=dispatcher.runner_id)
    task, attempt = claimed.task, claimed.attempt
    spec, inputs, *_rest = dispatcher._build_spec(task, attempt)
    admitted = await jobs.admit_job(spec)
    async with sessions() as db:
        linked = await service.repository.link_attempt_workflow_run(db, task_id, attempt.attempt_id,
            workflow_run_id=spec.identity.job_id, expected_revision=task.task_revision,
            board_fence=attempt.fencing_token, lease_owner=attempt.lease_owner, workflow_projection=admitted,
            expected_identity={"job_id": spec.identity.job_id, "owner_kind": spec.identity.owner_kind,
                "owner_principal_id": spec.identity.owner_principal_id, "service_id": spec.service_id,
                "operator_session_id": spec.operator_session_id, "session_id": spec.session_id,
                "goal_id": spec.goal_id, "goal_revision": spec.goal_revision,
                "job_kind": spec.identity.job_kind, "capability_version": spec.identity.capability_version,
                "input_digest": _digest(spec.inputs), "authority_digest": _digest(spec.declared_authority),
                "run_fingerprint": spec.run_fingerprint, "idempotency_scope": spec.identity.idempotency_scope,
                "idempotency_key": spec.identity.idempotency_key})
    await jobs.queue_job(spec.identity.job_id)
    parent = await jobs.claim_job(spec.identity.job_id, owner=dispatcher.runner_id + ":" + attempt.attempt_id)
    await initialize_interpreter(jobs, spec.identity.job_id, owner=parent["lease"]["owner"], fence=1, service=service)
    envelope = GeneralTaskEnvelope.model_validate(inputs)
    binding, _result = await admit_native_step(jobs, spec.identity.job_id, owner=parent["lease"]["owner"], fence=1,
        step=envelope.plan.steps[0], descriptor=envelope.descriptors[0], inputs=envelope.plan.steps[0].input, service=service)
    await jobs.queue_job(binding.invocation_id)
    return binding, created["build_id"]


@pytest.mark.parametrize("case", ["held", "priority", "forged_callback", "copied_callback"])
async def test_real_original_build_preclaim_never_mutates_denied_child(accounting_db, monkeypatch, case, build_admission_lifecycle):
    from src.work_board.repository import BoardError
    _token, operator, owner, goal = await setup(accounting_db, monkeypatch)
    await build_admission_lifecycle.start()
    registry = ToolRegistry(); registry.start()
    service = GeneralTaskService(registry); service.start()
    dispatcher = WorkBoardDispatcher(session_provider=accounting_db[2].accounting_sessions, general_tasks=service)
    jobs = dispatcher.jobs
    try:
        first, _build_id = await admitted_build(accounting_db, service, dispatcher, operator, owner, goal, "first", priority=10)
        second, _second_id = await admitted_build(accounting_db, service, dispatcher, operator, owner, goal, "second", priority=90)
        first_claim = await native.stage_claim(service, jobs, first)
        if case == "held":
            won = await jobs.claim_job(first.invocation_id, owner="actual-build-first", claim_authority_check=first_claim)
            assert won["attempt_count"] == 1 and won["lease"]["fencing_token"] == 1
        second_claim = await native.stage_claim(service, jobs, second)
        before = await jobs.get_job(second.invocation_id)
        parent_before = await jobs.get_job(second.parent_job_id)
        if case in {"forged_callback", "copied_callback"}:
            from dataclasses import replace
            async def fake_callback(_db, _run):
                raise AssertionError("an arbitrary callback cannot execute")
            callback = replace(second_claim) if case == "copied_callback" else fake_callback
            with pytest.raises(BoardError, match="exact private build claim"):
                await jobs.claim_job(second.invocation_id, owner="denied", claim_authority_check=callback)
        else:
            with pytest.raises(native.DocumentBuildPreclaimHeld) as denied:
                await jobs.claim_job(second.invocation_id, owner="denied", claim_authority_check=second_claim)
            waiting = await native.queued_wait_result(jobs, second, denied.value)
            assert waiting["unknown_effect"] is False and waiting["status"] == "queued"
        after = await jobs.get_job(second.invocation_id)
        assert after == before
        assert await jobs.get_job(second.parent_job_id) == parent_before
        assert after["status"] == "queued" and after["attempt_count"] == 0
        assert after["lease"]["fencing_token"] == 0 and after["lease"]["owner"] is None
        assert after["effects"] == [] and after["checkpoints"] == []
        async with jobs._session() as db:
            row, value = await storage.owned(db, owner, _second_id)
            assert value["live_writer"] is None and row.document_reserved_bytes == storage.CHARGE
    finally:
        service.stop(); registry.stop()


@pytest.mark.parametrize("priorities", [(10, 90), (90, 10)])
async def test_independent_original_build_claim_race_preserves_task_priority(accounting_db, monkeypatch,
        forbid_external_inference, priorities, build_admission_lifecycle):
    from src.workflows.job_runtime import DurableJobRepository
    from src.work_board.repository import BoardError
    _token, operator, owner, goal = await setup(accounting_db, monkeypatch)
    await build_admission_lifecycle.start()
    registry = ToolRegistry(); registry.start()
    service = GeneralTaskService(registry=registry); service.start()
    jobs = DurableJobRepository()
    dispatcher = WorkBoardDispatcher(jobs=jobs, session_provider=accounting_db[2].accounting_sessions,
        general_tasks=service)
    try:
        first, first_id = await admitted_build(accounting_db, service, dispatcher, operator, owner, goal,
            "race-first", priority=priorities[0])
        second, second_id = await admitted_build(accounting_db, service, dispatcher, operator, owner, goal,
            "race-second", priority=priorities[1])
        first_claim = await native.stage_claim(service, jobs, first)
        second_claim = await native.stage_claim(service, jobs, second)
        results = await asyncio.gather(
            jobs.claim_job(first.invocation_id, owner="race-first", claim_authority_check=first_claim),
            jobs.claim_job(second.invocation_id, owner="race-second", claim_authority_check=second_claim),
            return_exceptions=True)
        winner = 0 if priorities[0] < priorities[1] else 1
        assert isinstance(results[winner], dict) and results[winner]["attempt_count"] == 1
        assert isinstance(results[1-winner], (native.DocumentBuildPreclaimHeld, BoardError))
        loser = (first, second)[1-winner]
        row = await jobs.get_job(loser.invocation_id)
        assert row["status"] == "queued" and row["attempt_count"] == 0 and row["lease"]["fencing_token"] == 0
        assert row["effects"] == [] and row["checkpoints"] == []
        async with jobs._session() as db:
            builds = [await storage.owned(db, owner, identifier) for identifier in (first_id, second_id)]
            assert builds[winner][1]["live_writer"] is not None
            assert builds[1-winner][1]["live_writer"] is None
    finally:
        service.stop(); registry.stop()


@pytest.mark.parametrize("first_owner", ["build", "source"])
async def test_actual_source_process_and_original_build_share_one_slot(accounting_db, monkeypatch,
        forbid_external_inference, first_owner, build_admission_lifecycle):
    from hashlib import sha256
    from src.work_board import document_pairs as sources, dispatcher as dispatch_module
    from src.work_board.documents import DocumentService, DocumentSourceReserve, DocumentReadInput
    from src.work_board.repository import BoardError
    _token, operator, owner, goal = await setup(accounting_db, monkeypatch)
    await build_admission_lifecycle.start()
    sessions = accounting_db[2].accounting_sessions
    registry = ToolRegistry(); registry.start()
    service = GeneralTaskService(registry); service.start()
    dispatcher = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
    monkeypatch.setattr(dispatch_module, "_dispatcher", dispatcher)
    document_service = DocumentService(); await document_service.start()
    ready, release = asyncio.Event(), asyncio.Event()
    reading = None
    try:
        raw = b"Name,Count\r\nAlpha,2\r\n"
        async with sessions() as db:
            source = await sources.reserve(db, owner, DocumentSourceReserve(format="csv",
                source={"size_bytes": len(raw), "sha256": sha256(raw).hexdigest()}, goal_id=goal.id,
                goal_revision=1, idempotency_key="shared-source", no_learning=True))
        async def stream():
            yield raw
        async with sessions() as db:
            source = await sources.upload(db, owner, source["artifact_id"], source["revision"], "source", stream(),
                capability=sources.SOURCE_CAPABILITY, upload_profile=document_service._upload_profile)
        async with sessions() as db:
            source = await sources.complete(db, owner, source["artifact_id"], source["revision"], capability=sources.SOURCE_CAPABILITY)
        binding, build_id = await admitted_build(accounting_db, service, dispatcher, operator, owner, goal, "shared-build")
        original_parse = document_service.parse
        async def paused_parse(*args, **kwargs):
            callback = kwargs["on_ready"]
            async def observed(handshake):
                await callback(handshake)
                ready.set()
                await release.wait()
            return await original_parse(*args, **{**kwargs, "on_ready": observed})
        monkeypatch.setattr(document_service, "parse", paused_parse)
        request = DocumentReadInput(artifact_ref="document-source:"+source["artifact_id"], format="csv")
        async def read_source():
            from src.work_board.channel_capture import staged_captured_source_identity
            with staged_captured_source_identity():
                async with sessions() as db:
                    return await document_service.read(db, owner, request, operator=operator)
        if first_owner == "build":
            claim = await native.stage_claim(service, dispatcher.jobs, binding)
            await dispatcher.jobs.claim_job(binding.invocation_id, owner="build-slot-owner", claim_authority_check=claim)
            with pytest.raises(BoardError) as denied:
                await read_source()
            assert denied.value.code == "document_parser_capacity_held"
            assert not ready.is_set()
        else:
            reading = asyncio.create_task(read_source())
            await asyncio.wait_for(ready.wait(), 10)
            before = await dispatcher.jobs.get_job(binding.invocation_id)
            claim = await native.stage_claim(service, dispatcher.jobs, binding)
            with pytest.raises(native.DocumentBuildPreclaimHeld):
                await dispatcher.jobs.claim_job(binding.invocation_id, owner="build-denied", claim_authority_check=claim)
            assert await dispatcher.jobs.get_job(binding.invocation_id) == before
            async with sessions() as db:
                row, value = await sources.owned(db, owner, source["artifact_id"], capability=sources.SOURCE_CAPABILITY)
                assert value["live_writer"] and value["parser_binding"]["parser_pid"] > 0
                _row, build = await storage.owned(db, owner, build_id)
                assert build["live_writer"] is None
            release.set()
            result = await reading
            assert result["status"] == "succeeded" and result["cleanup"] == "wait_reaped"
    finally:
        release.set()
        if reading is not None and not reading.done():
            await reading
        await document_service.stop()
        service.stop(); registry.stop()


async def admitted_comparison(accounting_db, dispatcher, owner, goal, upload_profile, *, priority):
    from hashlib import sha256
    from datetime import timedelta
    from tests.document_compare_fixtures import invoice_pdf
    from src.work_board import document_pairs as pairs, document_compare_native as comparison
    from src.work_board.document_compare_contracts import DocumentPairReserve
    from src.work_board.contracts import WorkBoardTaskCreate
    from src.work_board.dispatcher import _parse_typed_input
    sessions = accounting_db[2].accounting_sessions
    pdf = invoice_pdf(); csv = b"SKU,QTY,UNIT_PRICE\r\nPEN-01,2,3.50\r\nBOOK-02,1,12.00\r\n"
    async with sessions() as db:
        pair = await pairs.reserve(db, owner, DocumentPairReserve(schema_version=1,
            operation="compare-line-totals-by-sku", goal_id=goal.id, goal_revision=1,
            idempotency_key="mixed-comparison", pdf={"size_bytes":len(pdf), "sha256":sha256(pdf).hexdigest()},
            csv={"size_bytes":len(csv), "sha256":sha256(csv).hexdigest()}, no_learning=True))
    for slot, raw in (("pdf",pdf), ("csv",csv)):
        async def stream():
            yield raw
        async with sessions() as db:
            pair = await pairs.upload(db, owner, pair["artifact_id"], pair["revision"], slot,
                stream(), upload_profile=upload_profile)
    async with sessions() as db:
        pair = await pairs.complete(db, owner, pair["artifact_id"], pair["revision"])
    repository = dispatcher.repository
    async with sessions() as db:
        created = await repository.create_task(db, owner, WorkBoardTaskCreate(title="Compare exact local invoice",
            goal_id=goal.id, goal_revision=1, capability_id=comparison.CAPABILITY,
            input_artifact_id=pair["artifact_id"], status="todo", priority=priority,
            requires_review=False, idempotency_scope="mixed-capacity", idempotency_key="comparison"))
    async with sessions() as db:
        ready = await repository.promote_task_ready(db, created.task.task_id, expected_revision=created.task.task_revision,
            actor_principal_id=dispatcher.runner_id, actor_session_id=dispatcher.runner_session)
    async with sessions() as db:
        claimed = await repository.claim_ready_task(db, ready.task.task_id,
            expected_revision=ready.task.task_revision, lease_owner=dispatcher.runner_id)
    task, attempt = claimed.task, claimed.attempt
    inputs = _parse_typed_input(task)
    admitted = await comparison.execute(task, attempt, inputs, jobs=dispatcher.jobs, runner=dispatcher.runner_id,
        deadline=comparison.now()+timedelta(seconds=70), admission_only=True)
    async with sessions() as db:
        linked = await repository.link_attempt_workflow_run(db, task.task_id, attempt.attempt_id,
            workflow_run_id=admitted["job_id"], expected_revision=task.task_revision, board_fence=attempt.fencing_token,
            lease_owner=attempt.lease_owner, workflow_projection=admitted,
            expected_identity=dispatcher._direct_expected_identity(task, attempt, inputs, admitted))
        task = linked.task
        attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == attempt.attempt_id))
    await dispatcher.jobs.queue_job(admitted["job_id"])
    return task, attempt, inputs


@pytest.mark.parametrize("first_owner", ["build", "comparison"])
async def test_authentic_comparison_build_priority_is_reciprocal(accounting_db, monkeypatch,
        forbid_external_inference, first_owner, build_admission_lifecycle):
    from datetime import timedelta
    from src.work_board import document_compare_native as comparison, dispatcher as dispatch_module
    from src.work_board.documents import DocumentService
    _token, operator, owner, goal = await setup(accounting_db, monkeypatch)
    await build_admission_lifecycle.start()
    from tests.test_document_build_storage import _goal
    comparison_goal = _goal("mixed-comparison-goal", "Original bounded comparison goal")
    comparison_goal.owner_principal_id, comparison_goal.owner_session_id = owner.principal_id, owner.session_id
    async with accounting_db[2].accounting_sessions() as db:
        db.add(comparison_goal)
    registry = ToolRegistry(); registry.start()
    service = GeneralTaskService(registry); service.start()
    dispatcher = WorkBoardDispatcher(session_provider=accounting_db[2].accounting_sessions, general_tasks=service)
    monkeypatch.setattr(dispatch_module, "_dispatcher", dispatcher)
    document_service = DocumentService(); await document_service.start()
    try:
        build, _id = await admitted_build(accounting_db, service, dispatcher, operator, owner, goal,
            "mixed-build", priority=10 if first_owner == "build" else 90)
        task, attempt, inputs = await admitted_comparison(accounting_db, dispatcher, owner, comparison_goal,
            document_service._upload_profile, priority=10 if first_owner == "comparison" else 90)
        if first_owner == "build":
            queued = await comparison.execute(task, attempt, inputs, jobs=dispatcher.jobs, runner=dispatcher.runner_id,
                deadline=comparison.now()+timedelta(seconds=70), admission_only=False)
            assert queued["status"] == "queued" and queued["reason_code"] == "document_higher_priority_ready"
            assert queued["attempt_count"] == 0 and "document-child" not in comparison.checkpoints(queued)
            claim = await native.stage_claim(service, dispatcher.jobs, build)
            assert (await dispatcher.jobs.claim_job(build.invocation_id, owner="mixed-build", claim_authority_check=claim))["attempt_count"] == 1
        else:
            claim = await native.stage_claim(service, dispatcher.jobs, build)
            before = await dispatcher.jobs.get_job(build.invocation_id)
            with pytest.raises(native.DocumentBuildPreclaimHeld):
                await dispatcher.jobs.claim_job(build.invocation_id, owner="mixed-denied", claim_authority_check=claim)
            assert await dispatcher.jobs.get_job(build.invocation_id) == before
            completed = await comparison.execute(task, attempt, inputs, jobs=dispatcher.jobs, runner=dispatcher.runner_id,
                deadline=comparison.now()+timedelta(seconds=70), admission_only=False)
            assert completed["status"] == "succeeded" and comparison.cleanup_proven(task, attempt, completed)
            claim = await native.stage_claim(service, dispatcher.jobs, build)
            assert (await dispatcher.jobs.claim_job(build.invocation_id, owner="mixed-build", claim_authority_check=claim))["attempt_count"] == 1
    finally:
        await document_service.stop()
        service.stop(); registry.stop()


async def test_authentic_document_child_checkpoint_survives_generic_history(accounting_db,
        monkeypatch, forbid_external_inference, build_admission_lifecycle):
    """Generic calls cannot mint or erase a real process's recovery witness."""
    from copy import deepcopy
    from src.db.models import AuditEvent
    from src.work_board.general_task_native import run_native_step, retain_native_failure
    from src.workflows.job_runtime import DurableJobTransitionError, _bounded_checkpoint_receipts
    from src.work_board import dispatcher as dispatch_module
    _token, operator, owner, goal = await setup(accounting_db, monkeypatch)
    await build_admission_lifecycle.start()
    sessions = accounting_db[2].accounting_sessions
    registry = ToolRegistry(); registry.start()
    service = GeneralTaskService(registry); service.start()
    dispatcher = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
    monkeypatch.setattr(dispatch_module, "_dispatcher", dispatcher)
    def interrupted_publication(*args, **kwargs):
        raise OSError("actual producer interrupted after authentic supervision")
    monkeypatch.setattr(storage, "prepare_publications", interrupted_publication)
    try:
        binding, identifier = await admitted_build(accounting_db, service, dispatcher,
            operator, owner, goal, "protected-child-history")
        with pytest.raises(OSError, match="authentic supervision"):
            await run_native_step(service, dispatcher.jobs, binding,
                child_owner="history-owner", principal=operator.principal)
        before = await dispatcher.jobs.get_job(binding.invocation_id)
        original_child = next(item for item in before["checkpoints"]
            if item["checkpoint_id"] == "document-child")
        assert original_child["payload"]["supervisor_pid"] > 0
        # The reserved generic name fails before opening the SQL writer.
        with monkeypatch.context() as guard:
            def forbidden_writer():
                raise AssertionError("a generic child checkpoint must not open a writer")
            guard.setattr(dispatcher.jobs, "_session", forbidden_writer)
            with pytest.raises(DurableJobTransitionError, match="fixed native owner"):
                await dispatcher.jobs.record_checkpoint(binding.invocation_id,
                    checkpoint_id="document-child", state={"forged": True},
                    checkpoint_payload={"supervisor_pid": 1}, owner="history-owner",
                    fencing_token=before["lease"]["fencing_token"])
        assert await dispatcher.jobs.get_job(binding.invocation_id) == before
        with pytest.raises(DurableJobTransitionError, match="malformed document"):
            _bounded_checkpoint_receipts([*before["checkpoints"], deepcopy(original_child)])
        with pytest.raises(DurableJobTransitionError, match="malformed document"):
            _bounded_checkpoint_receipts([{**original_child, "payload": None}])
        for index in range(55):
            await dispatcher.jobs.record_checkpoint(binding.invocation_id,
                checkpoint_id=f"ordinary-history-{index}", state={"cursor": index},
                owner="history-owner", fencing_token=before["lease"]["fencing_token"])
        retained = await dispatcher.jobs.get_job(binding.invocation_id)
        assert len(retained["checkpoints"]) == 50
        assert next(item for item in retained["checkpoints"]
            if item["checkpoint_id"] == "document-child") == original_child
        assert any(item["checkpoint_id"] == "document-capacity" for item in retained["checkpoints"])
        await retain_native_failure(service, dispatcher.jobs, binding, child_owner="history-owner")
        child_before = await dispatcher.jobs.get_job(binding.invocation_id)
        parent_before = await dispatcher.jobs.get_job(binding.parent_job_id)
        assert any(item["status"] == "unknown" for item in child_before["effects"])
        async with sessions() as db:
            row, value = await storage.owned(db, owner, identifier)
            revision, digest = row.revision, row.metadata_digest
            body = native._supervision(value)
            assert body["supervisor_wait_reaped"] and body["stdout_eof"] and body["stdin_closed"]
            audits = list((await db.execute(select(AuditEvent).where(
                AuditEvent.tool_name == "document_build"))).scalars())
            assert {event.event_type for event in audits} >= {"tool_call", "tool_failed"}
            assert all(event.summary == "Local document build " + event.event_type for event in audits)
        result = await native.reconcile_reap(dispatcher.jobs, owner, identifier, operator,
            expected_revision=revision, expected_metadata_digest=digest)
        assert result["cleanup_proven"] and result["build_revision"] == revision + 1
        assert await dispatcher.jobs.get_job(binding.invocation_id) == child_before
        assert await dispatcher.jobs.get_job(binding.parent_job_id) == parent_before
        async with sessions() as db:
            row, value = await storage.owned(db, owner, identifier)
            assert not value.get("output") and value["live_writer"] is None
            assert native._authenticated_reap(value, value["renderer_binding"])
            actual = await dispatcher.jobs._fetch(db, binding.invocation_id)
            assert await native.held_capacity(db, actual) is False
    finally:
        service.stop(); registry.stop()
