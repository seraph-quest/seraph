"""System-only recommendation publication and original attempt boundaries."""
import pytest
from unittest.mock import AsyncMock

from src.db.models import Goal, WorkBoardTask, WorkBoardStatus
from src.work_board.contracts import WorkBoardOwner, WorkBoardTaskCreate, WorkBoardTaskPatch, WorkBoardActionRequest
from src.work_board.repository import WorkBoardRepository, BoardError
from src.work_board.opportunity_preference_native import CAPABILITY
from src.work_board.triage import _validate_proposed_typed_inputs

OWNER = WorkBoardOwner(principal_id="operator:test-bypass", session_id="test-auth-bypass")


def test_actual_public_recommendation_dto_preserves_canonical_uuid_and_zero_feedback():
    from uuid import UUID
    from pydantic import ValidationError
    from src.guardian.opportunity_contracts import OpportunityRecommendationRequest as PublicRequest
    from src.work_board.opportunity_preference_native import _recommendation_request, digest
    wire = {"expected_opportunity_revision":1, "expected_feedback_revision":0,
        "idempotency_key":"12345678-1234-4234-9234-123456789abc"}
    public = PublicRequest.model_validate(wire)
    assert isinstance(public.idempotency_key,UUID)
    internal = _recommendation_request(public)
    assert internal.model_dump(mode="json") == wire
    assert digest(internal.model_dump(mode="json")) == digest(wire)
    with pytest.raises(ValidationError):
        _recommendation_request({**wire,"idempotency_key":public.idempotency_key})
    with pytest.raises(ValidationError):
        _recommendation_request({**wire,"extra":"unapproved"})


def test_recommendation_input_has_required_numeric_outer_and_typed_inner_schema():
    from pydantic import ValidationError
    from src.guardian.opportunity_preferences import OpportunityPreferenceInput
    from src.work_board.contracts import WorkBoardInputArtifactCreate
    cpu_input = OpportunityPreferenceInput(schema_version="seraph.opportunity.preference-input.v1",
        opportunity_id="original-opportunity", expected_opportunity_revision=1, expected_feedback_revision=0,
        request_uuid="12345678-1234-4234-9234-123456789abc", generation_cutoff_at="2026-10-06T00:00:00+00:00",
        population_digest="a"*64)
    fields = dict(capability_id=CAPABILITY, goal_id="original-goal", goal_revision=1,
        input=cpu_input.model_dump(mode="json"), idempotency_key="original-publication")
    with pytest.raises(ValidationError):
        WorkBoardInputArtifactCreate(**fields)
    envelope = WorkBoardInputArtifactCreate(schema_version=1, **fields)
    assert envelope.schema_version == 1
    assert OpportunityPreferenceInput.model_validate(envelope.input) == cpu_input
    assert envelope.input["expected_feedback_revision"] == 0
    with pytest.raises(ValidationError):
        WorkBoardInputArtifactCreate(schema_version=cpu_input.schema_version, **fields)


def _sealed_metadata():
    from datetime import datetime, timedelta, timezone
    from src.work_board.opportunity_preference_native import _authorization_mac
    cutoff = datetime(2026, 10, 6, tzinfo=timezone.utc)
    value = {"schema_version":1, "goal_revision":1,
        "request_uuid":"12345678-1234-4234-9234-123456789abc"}
    value.update({name:"bound-identity" for name in
        ("owner_principal_id", "original_root_id", "goal_id", "task_id", "input_artifact_id")})
    value.update({name:"a"*64 for name in
        ("request_body_digest", "task_create_digest", "input_sha256", "cpu_input_digest", "population_digest")})
    value.update(generation_cutoff_at=cutoff.isoformat(),
        original_idle_expires_at=(cutoff+timedelta(hours=1)).isoformat(),
        original_absolute_expires_at=(cutoff+timedelta(hours=2)).isoformat(),
        execution_deadline=(cutoff+timedelta(seconds=30)).isoformat(),
        preview_expires_at=(cutoff+timedelta(minutes=5)).isoformat(), key_id="b"*24)
    value["authorization_mac"] = _authorization_mac(value,"transient-original-hash",b"test-key")
    return value


def test_original_request_metadata_accepts_exact_closed_writer_shape():
    import json
    from src.work_board.opportunity_preference_native import _parse_request_metadata
    value = _sealed_metadata()
    assert _parse_request_metadata(json.dumps(value)) == value


@pytest.mark.parametrize("field,value", [
    ("schema_version",True), ("goal_revision",True), ("request_uuid","not-a-uuid"),
    ("input_sha256","A"*64), ("key_id","x"*24), ("owner_principal_id",""),
    ("generation_cutoff_at","2026-10-06T00:00:00"),
    ("original_idle_expires_at","2026-10-06T00:00:00+01:00"),
    ("execution_deadline","2026-10-06T00:00:31+00:00"),
    ("preview_expires_at","2026-10-06T00:05:01+00:00"),
    ("extra","unrecognized"),
])
def test_malformed_original_metadata_rejected_even_with_valid_mac(field,value):
    import json
    from src.work_board.opportunity_preference_native import _parse_request_metadata, _authorization_mac
    metadata = _sealed_metadata()
    metadata[field] = value
    metadata["authorization_mac"] = _authorization_mac(metadata,"transient-original-hash",b"test-key")
    with pytest.raises(BoardError) as denied:
        _parse_request_metadata(json.dumps(metadata))
    assert denied.value.code == "opportunity_recommendation_binding_invalid"


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "oversized", "nonfinite"])
def test_original_request_metadata_raw_shape_fails_closed(mutation):
    import json
    from src.work_board.opportunity_preference_native import _parse_request_metadata
    value = _sealed_metadata()
    raw = json.dumps(value)
    if mutation == "duplicate":
        raw = raw[:-1] + ', "schema_version":1}'
    elif mutation == "missing":
        del value["task_id"]
        raw = json.dumps(value)
    elif mutation == "oversized":
        raw += " "*8192
    else:
        raw = raw.replace('"schema_version": 1', '"schema_version": NaN')
    with pytest.raises(BoardError) as denied:
        _parse_request_metadata(raw)
    assert denied.value.code == "opportunity_recommendation_binding_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["idle_extension_crossing", "rotation", "revocation", "replacement", "tombstone", "mac_tamper"])
async def test_original_sealed_root_fails_closed_in_sql_writer(async_db, monkeypatch, mutation):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    import json
    from sqlalchemy import text
    from src.db.models import OperatorSession, WorkBoardEvent
    from src.work_board import opportunity_preference_native as native
    from src.memory import repository as memory_repo

    cutoff = datetime(2026,10,6,tzinfo=timezone.utc)
    key = b"isolated-original-publication-key"
    metadata = _sealed_metadata()
    metadata["key_id"] = memory_repo._m5_selection_binding_key_id(_signing_key=key)
    original_idle = cutoff + timedelta(hours=1)
    absolute = cutoff + timedelta(hours=2)
    operator = SimpleNamespace(_token_hash="original-token-hash", idle_expires_at=original_idle,
        absolute_expires_at=absolute)
    witness = SimpleNamespace(operator=operator, signing_key=key)
    async with async_db() as db:
        task = await _negative_task(db, CAPABILITY)
        root = OperatorSession(id=OWNER.session_id, principal_id=OWNER.principal_id,
            token_hash=operator._token_hash, idle_expires_at=original_idle, absolute_expires_at=absolute)
        db.add(root)
        metadata["authorization_mac"] = native._authorization_mac(metadata,operator._token_hash,key)
        event = WorkBoardEvent(task_id=task.task_id, owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id, actor_principal_id=OWNER.principal_id,
            actor_session_id=OWNER.session_id, kind=native.REQUESTED_EVENT,
            metadata_json=json.dumps(metadata))
        db.add(event)
        await db.commit()
        monkeypatch.setattr(native,"now",lambda: cutoff+timedelta(seconds=1))
        await native._recheck_original_commitment(db,task,event,witness)
        if mutation == "idle_extension_crossing":
            root.idle_expires_at = cutoff+timedelta(hours=3)
            monkeypatch.setattr(native,"now",lambda: original_idle+timedelta(microseconds=1))
        elif mutation == "rotation":
            root.token_hash = "new-token-hash"
        elif mutation == "revocation":
            root.revoked_at = cutoff
        elif mutation == "replacement":
            root.replaced_by_id = "replacement-root"
        elif mutation == "tombstone":
            root.is_bearer_tombstone = True
        else:
            metadata["authorization_mac"] = "0"*64
            event.metadata_json = json.dumps(metadata)
        await db.commit()
        # Key custody is unavailable inside the writer; only staged key bytes may be used.
        def forbidden_key():
            raise AssertionError("key I/O inside writer")
        monkeypatch.setattr(memory_repo,"_effect_mac_key",forbidden_key)
        await db.execute(text("BEGIN IMMEDIATE"))
        with pytest.raises(BoardError) as denied:
            await native._recheck_original_commitment(db,task,event,witness)
        assert denied.value.code == "opportunity_recommendation_root_stale"
        await db.rollback()


@pytest.mark.asyncio
async def test_preference_registration_preserves_ordinary_async_native_transition(async_db):
    from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec, durable_job_repository

    spec = DurableJobSpec(
        identity=DurableJobIdentity(job_id="ordinary-preference-regression", owner_kind="service",
            owner_principal_id="service:strategist", job_kind="strategist_tick", capability_version="1",
            idempotency_scope="ordinary-regression", idempotency_key="one"),
        inputs={"task": "ordinary"}, session_id="ordinary-session", resource_claims=("cpu",),
        declared_authority={"principal":"service:strategist", "service_id":"service:strategist"},
        service_id="service:strategist", max_attempts=1,
    )
    admitted = await durable_job_repository.admit_job(spec)
    queued = await durable_job_repository.queue_job(admitted["job_id"])
    assert queued["status"] == "queued"
    claimed = await durable_job_repository.claim_job(admitted["job_id"], owner="ordinary-runner")
    assert claimed["status"] == "running"
    assert claimed["job_id"] == admitted["job_id"]
    assert claimed["attempt_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("callback", [False, True])
@pytest.mark.parametrize("witness", [None, object()])
async def test_fixed_native_admission_requires_typed_authority_not_callback(async_db, callback, witness):
    from dataclasses import replace
    from tests.test_durable_job_runtime import _spec
    from src.workflows.job_runtime import durable_job_repository
    from src.db.models import WorkflowRunState
    from sqlalchemy import select
    base = _spec(job_id="forged-preference-admission", dedupe_key="forged")
    spec = replace(base, identity=replace(base.identity,job_kind=CAPABILITY), max_attempts=1)
    check = AsyncMock()
    with pytest.raises(BoardError) as denied:
        await durable_job_repository.admit_job(spec,
            admission_authority_check=check if callback else None,
            opportunity_preference_witness=witness)
    assert denied.value.code == "opportunity_recommendation_binding_invalid"
    check.assert_not_awaited()
    async with async_db() as db:
        assert await db.scalar(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == spec.identity.job_id)) is None


@pytest.mark.asyncio
async def test_fixed_claim_checks_typed_witness_inside_actual_sqlite_writer(async_db, monkeypatch):
    from tests.test_durable_job_runtime import _spec
    from src.workflows.job_runtime import durable_job_repository
    from src.db.models import WorkflowRunState
    from sqlalchemy import event, select
    from src.work_board import opportunity_preference_native as native
    admitted = await durable_job_repository.admit_job(_spec(job_id="claim-writer-negative",dedupe_key="claim-writer"))
    await durable_job_repository.queue_job(admitted["job_id"])
    async with async_db() as db:
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == admitted["job_id"]))
        # Corrupted legacy row must never be claimed; no positive preference result is seeded.
        run.job_kind = CAPABILITY
        await db.commit()
        engine = db.get_bind()
    statements = []
    def trace(_connection,_cursor,statement,_parameters,_context,_many):
        statements.append(statement.strip().upper())
    event.listen(engine,"before_cursor_execute",trace)
    original = native.recheck_native
    witness = native.NativeWitness(None,None)
    called = []
    async def checked(db,run,*,witness,terminal=False):
        assert "BEGIN IMMEDIATE" in statements
        called.append(True)
        await original(db,run,witness=witness,terminal=terminal)
    monkeypatch.setattr(native,"recheck_native",checked)
    try:
        with pytest.raises(BoardError) as denied:
            await durable_job_repository.claim_job(admitted["job_id"],owner="negative-runner",
                opportunity_preference_witness=witness)
        assert denied.value.code == "opportunity_recommendation_binding_invalid"
    finally:
        event.remove(engine,"before_cursor_execute",trace)
    assert called == [True]
    async with async_db() as db:
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == admitted["job_id"]))
        assert run.status == "queued"
        assert run.attempt_count == 0
        assert run.lease_owner is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["triage", "todo"])
@pytest.mark.parametrize("callback", [False, True])
async def test_generic_creation_cannot_publish_recommendation(async_db, status, callback):
    check = AsyncMock()
    request = WorkBoardTaskCreate(title="Unauthorized recommendation", goal_id="goal", goal_revision=1,
        capability_id=CAPABILITY, status=status, idempotency_key="untrusted", requires_review=False,
        typed_input_ref="artifacts/untrusted.json", typed_input_digest="a"*64)
    async with async_db() as db:
        with pytest.raises(BoardError) as denied:
            await WorkBoardRepository().create_task(db, OWNER, request,
                publication_authority_check=check if callback else None)
        assert denied.value.code == "opportunity_recommendation_system_only"
        check.assert_not_awaited()


async def _negative_task(db, capability=None, status=WorkBoardStatus.triage):
    # Deliberately corrupted persisted rows test fail-closed guards, never a successful source.
    db.add(Goal(id="goal", title="Negative fixture", owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id, revision=1))
    task = WorkBoardTask(owner_principal_id=OWNER.principal_id, owner_session_id=OWNER.session_id,
        title="Negative fixture", goal_id="goal", goal_revision=1,
        capability_id=capability, status=status, idempotency_scope="negative", idempotency_key="negative")
    db.add(task)
    await db.flush()
    return task


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_generic_patch_cannot_create_or_rebind_system_recommendation(async_db, existing):
    async with async_db() as db:
        task = await _negative_task(db, CAPABILITY if existing else None)
        changes = {"title":"Replacement"} if existing else {"capability_id":CAPABILITY}
        with pytest.raises(BoardError) as denied:
            await WorkBoardRepository().patch_task(db, OWNER, task.task_id,
                WorkBoardTaskPatch(expected_revision=task.task_revision, **changes))
        assert denied.value.code == "opportunity_recommendation_system_only"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["promote", "retry", "unblock"])
async def test_generic_actions_cannot_renew_original_recommendation(async_db, action):
    async with async_db() as db:
        task = await _negative_task(db, CAPABILITY)
        with pytest.raises(BoardError) as denied:
            await WorkBoardRepository().action_task(db, OWNER, task.task_id,
                WorkBoardActionRequest(expected_revision=task.task_revision, action=action,
                    resolution="Caller claims resolved" if action == "unblock" else None))
        assert denied.value.code in {"opportunity_recommendation_system_only", "opportunity_recommendation_original_attempt_required"}


def test_generic_proposal_cannot_select_system_capability():
    parent = WorkBoardTask(task_id="parent", title="Ordinary", goal_id="goal", goal_revision=1,
        owner_principal_id=OWNER.principal_id, owner_session_id=OWNER.session_id)
    with pytest.raises(BoardError) as denied:
        _validate_proposed_typed_inputs([{"capability_id":CAPABILITY}], parent=parent)
    assert denied.value.code == "opportunity_recommendation_system_only"


def test_system_parent_cannot_be_replanned_to_ordinary_capability():
    parent = WorkBoardTask(task_id="parent", title="Recommendation", goal_id="goal", goal_revision=1,
        owner_principal_id=OWNER.principal_id, owner_session_id=OWNER.session_id, capability_id=CAPABILITY)
    with pytest.raises(BoardError) as denied:
        _validate_proposed_typed_inputs([{"capability_id":"browser.public-task.v1"}], parent=parent)
    assert denied.value.code == "opportunity_recommendation_system_only"
