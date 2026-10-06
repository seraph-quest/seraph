"""Negative plan bindings and writer atomicity; no seeded execution success."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from src.db.models import (
    Goal, GuardianOpportunity, Session, WorkflowRunState,
    WorkBoardEvent, WorkBoardProposal, WorkBoardStatus, WorkBoardTask,
)
from src.guardian import opportunity_plans as plans
from src.guardian.opportunity_contracts import OpportunityError
from src.work_board import triage
from src.workflows.job_runtime import (
    DurableJobIdentity, DurableJobSpec, durable_job_repository,
)
from tests.test_research_native_vertical import real_auth


async def admitted_plan(async_db):
    """Use canonical admission, deliberately stopping before queue/contact."""
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    inputs = {"opportunity_id": "opportunity", "opportunity_revision": 1,
              "parent_task_id": "parent", "parent_revision": 1}
    opportunity = GuardianOpportunity(
        id="opportunity", owner_principal_id="owner", original_root_id="root",
        goal_id="goal", goal_revision=1, policy_revision=1, watch_id="watch",
        watch_revision=1, source_packet_id="packet", source_digest="a" * 64,
        source_token_json="{}", dedupe_key="plan", expires_at=deadline,
        assessment_deadline_at=deadline, status="proposed", proposal_id="proposal",
    )
    parent = WorkBoardTask(task_id="parent", owner_principal_id="owner",
        owner_session_id="root", goal_id="goal", goal_revision=1,
        idempotency_key="parent-key", status=WorkBoardStatus.triage, task_revision=1)
    proposal = WorkBoardProposal(
        proposal_id="proposal", opportunity_id=opportunity.id, opportunity_revision=1,
        owner_principal_id="owner", owner_session_id="root", parent_task_id="parent",
        kind="opportunity_plan", idempotency_key="generation-key", admission_job_id="job",
        capability_version="strategist-v1", authority_digest="b" * 64,
        request_digest="c" * 64, expires_at=deadline,
        proposal_json=json.dumps({"generation_binding": {"inputs": inputs}}),
    )
    async with async_db() as db:
        db.add(Session(id="root", owner_principal_id="owner"))
        db.add(Goal(id="goal", title="Original Goal", owner_principal_id="owner",
                    owner_session_id="root", revision=1))
        db.add(opportunity)
        db.add(parent)
        db.add(proposal)
        await db.commit()
        assert isinstance(parent.creation_sequence, int)
        assert str(parent.creation_sequence) != parent.task_id
    spec = DurableJobSpec(
        identity=DurableJobIdentity(job_id="job", owner_kind="user",
            owner_principal_id="owner", job_kind="work_board_proposal",
            capability_version=proposal.capability_version,
            idempotency_scope="work-board-proposal", idempotency_key="proposal"),
        inputs=inputs, session_id="root", conversation_id="root", operator_session_id="root",
        goal_id="goal", goal_revision=1, declared_authority=triage._proposal_job_authority(proposal),
        run_fingerprint=proposal.request_digest, deadline_at=deadline,
    )
    admitted = await durable_job_repository.admit_job(spec)
    assert admitted["status"] == "accepted"
    return spec


@pytest.mark.parametrize("target,field,value", [
    ("run", "goal_id", "foreign-goal"),
    ("parent", "goal_id", "foreign-goal"),
    ("opportunity", "goal_id", "foreign-goal"),
    ("run", "goal_revision", 2),
    ("run", "owner_principal_id", "foreign-owner"),
    ("parent", "owner_principal_id", "foreign-owner"),
    ("opportunity", "original_root_id", "foreign-root"),
    ("run", "operator_session_id", "foreign-root"),
    ("run", "capability_version", "strategist-v2"),
    ("run", "authority_digest", "d" * 64),
    ("run", "declared_authority_json", json.dumps({"principal": "owner", "finite_authority": True})),
    ("run", "idempotency_scope", "foreign-scope"),
    ("run", "idempotency_key", "foreign-key"),
    ("run", "input_digest", "e" * 64),
    ("run", "run_fingerprint", "f" * 64),
    ("proposal", "authority_digest", "d" * 64),
    ("proposal", "kind", "specify"),
])
async def test_native_binding_rejects_single_field_substitution(async_db, target, field, value):
    await admitted_plan(async_db)
    async with async_db() as db:
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == "job"))
        proposal = await db.get(WorkBoardProposal, "proposal")
        parent = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == "parent"))
        opportunity = await db.get(GuardianOpportunity, "opportunity")
        assert await plans.assert_linked_plan_native(db, run) is proposal
        assert run.status == "accepted" and not proposal.provider_contact_started
        setattr({"run": run, "proposal": proposal, "parent": parent, "opportunity": opportunity}[target], field, value)
        with pytest.raises(OpportunityError, match="proposal_binding_conflict"):
            await plans.assert_linked_plan_native(db, run)
        assert parent.status == WorkBoardStatus.triage
        assert opportunity.status == "proposed"
        assert proposal.status == "pending_inference"


async def test_same_pk_report_conversion_retains_native_inputs_but_not_success(async_db):
    spec = await admitted_plan(async_db)
    duplicate = await durable_job_repository.admit_job(spec)
    assert duplicate["receipt"]["status"] == "deduped"
    async with async_db() as db:
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == "job"))
        proposal = await db.get(WorkBoardProposal, "proposal")
        opportunity = await db.get(GuardianOpportunity, "opportunity")
        proposal.kind = "public-evidence-pipeline.v1"
        assert await plans.assert_linked_plan_native(db, run) is proposal
        with pytest.raises(OpportunityError, match="proposal_native_readback_required"):
            await plans._assert_generated_native_sql(db, proposal, opportunity)
        assert run.status == "accepted"
        assert run.effect_receipts_json == "[]"
        assert len((await db.scalars(select(WorkflowRunState))).all()) == 1


async def test_writer_rolls_back_parent_queue_when_opportunity_cas_is_stale(async_db):
    await admitted_plan(async_db)
    owner = SimpleNamespace(principal_id="owner", session_id="root")
    with pytest.raises(OpportunityError, match="proposal_stale"):
        async with async_db() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            proposal = await db.get(WorkBoardProposal, "proposal")
            opportunity = await db.get(GuardianOpportunity, "opportunity")
            # Persisted status no longer permits planning, while the caller's
            # detached read remains the originally reviewed proposed status.
            await db.execute(text("UPDATE guardian_opportunities SET status='expired' WHERE id='opportunity'"))
            await plans._queue_original_parent(db, owner, proposal, 1)
            await plans._mark_planned(db, opportunity, proposal)
    async with async_db() as db:
        parent = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == "parent"))
        opportunity = await db.get(GuardianOpportunity, "opportunity")
        assert parent.status == WorkBoardStatus.triage and parent.task_revision == 1
        assert parent.pipeline_operation_id is None
        assert opportunity.status == "proposed" and opportunity.revision == 1
        assert (await db.get(WorkBoardProposal, "proposal")).status == "pending_inference"
        assert not (await db.scalars(select(WorkBoardEvent))).all()


@pytest.mark.parametrize("revision,status", [(2, WorkBoardStatus.triage), (1, WorkBoardStatus.todo)])
async def test_parent_queue_cas_rejects_stale_revision_or_duplicate_accept(async_db, revision, status):
    await admitted_plan(async_db)
    async with async_db() as db:
        parent = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == "parent"))
        parent.task_revision, parent.status = revision, status
        await db.flush()
        proposal = await db.get(WorkBoardProposal, "proposal")
        with pytest.raises(OpportunityError, match="proposal_stale"):
            await plans._queue_original_parent(db, SimpleNamespace(principal_id="owner", session_id="root"), proposal, 1)
        assert parent.task_revision == revision and parent.status == status


@pytest.mark.parametrize("mutation", ["authority", "steps", "citation_digest", "citation_range", "unknown_source"])
def test_model_output_cannot_supply_authority_steps_or_uncited_input(mutation):
    from tests.test_guardian_opportunity_plans import evidence
    from src.guardian.opportunity_contracts import digest
    raw = dict(schema_version="seraph.opportunity.plan.v1", blueprint_id="public-browser-check",
        title="Check source", reason="Read cited release", citations=[dict(source_id="public",
            start_line=1, end_line=1, span_sha256=digest(b"Public release"))])
    if mutation == "authority":
        raw["authority"] = {"allowed_operations": ["shell_execute"]}
    elif mutation == "steps":
        raw["steps"] = [{"capability_id": "shell_execute", "command": "echo unsafe"}]
    elif mutation == "citation_digest":
        raw["citations"][0]["span_sha256"] = "f" * 64
    elif mutation == "citation_range":
        raw["citations"][0]["end_line"] = 2
    else:
        raw["citations"][0]["source_id"] = "invented"
    with pytest.raises(ValueError):
        plans.validate_plan_result(json.dumps(raw), evidence(), ("public-browser-check",))


@pytest.mark.parametrize("action", ["advance", "accept"])
@pytest.mark.parametrize("code,status", [("source_stale", 409), ("pipeline_source_permission", 403)])
async def test_authenticated_pipeline_source_error_is_closed_http_failure(
    async_db, real_auth, monkeypatch, action, code, status,
):
    """HTTP mapping only: inject source-staging denial, never native success."""
    import httpx
    from fastapi import FastAPI
    from src.api import auth, work_board
    from src.auth.middleware import OperatorAuthMiddleware

    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(work_board.router, prefix="/api")
    observed = []

    async def denied_source(db, opportunity):
        observed.append(opportunity.id)
        raise OpportunityError(code, status)

    async def linked_advance(db, owner, operation_id, expected_revision):
        assert operation_id == "route-operation" and expected_revision == 1
        assert owner.principal_id == logged_in["principal_id"]
        assert owner.session_id == logged_in["session_id"]
        return await plans.stage_plan_source(db, SimpleNamespace(id="route-opportunity"))

    monkeypatch.setattr(plans, "stage_plan_source", denied_source)
    # The real source authority/fetch contract has its own vertical tests.
    # This route dependency isolates the exception emitted by its staging seam.
    monkeypatch.setattr(work_board.pipeline_service, "advance", linked_advance)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test", headers={"origin": "http://localhost:3001"}) as client:
        path = f"/api/work-board/pipelines/route-operation/{action}"
        body = {"expected_revision": 1}
        if action == "accept":
            body.update(expected_parent_revision=1, expected_digest="a" * 64)
        unauthenticated = await client.post(path, json=body)
        assert unauthenticated.status_code == 401
        assert observed == []
        login = await client.post("/api/auth/login", json={"password": "research-vertical-private-secret"})
        assert login.status_code == 200, login.text
        logged_in = login.json()
        assert logged_in["principal_id"].startswith("operator:root:")
        async with async_db() as db:
            db.add(WorkBoardProposal(proposal_id="route-operation", opportunity_id="route-opportunity",
                owner_principal_id=logged_in["principal_id"], owner_session_id=logged_in["session_id"],
                parent_task_id="route-parent", kind="public-evidence-pipeline.v1", idempotency_key="route-key",
                expires_at=datetime.now(timezone.utc)+timedelta(minutes=5)))
            db.add(GuardianOpportunity(id="route-opportunity", owner_principal_id=logged_in["principal_id"],
                original_root_id=logged_in["session_id"], goal_id="route-goal", goal_revision=1,
                policy_revision=1, watch_id="route-watch", watch_revision=1, source_packet_id="route-packet",
                source_digest="a" * 64, source_token_json="{}", dedupe_key="route-opportunity",
                expires_at=datetime.now(timezone.utc)+timedelta(minutes=5),
                assessment_deadline_at=datetime.now(timezone.utc)+timedelta(minutes=5)))
        result = await client.post(path, json=body)
        assert result.status_code == status, result.text
        assert result.json() == {"detail": {"code": code}}
        assert observed == ["route-opportunity"]
    async with async_db() as db:
        assert not (await db.scalars(select(WorkBoardTask))).all()
        assert not (await db.scalars(select(WorkflowRunState))).all()
        assert not (await db.scalars(select(WorkBoardEvent))).all()
        proposal = await db.get(WorkBoardProposal, "route-operation")
        opportunity = await db.get(GuardianOpportunity, "route-opportunity")
        assert proposal.status == "pending_inference" and proposal.revision == 1
        assert not proposal.provider_contact_started
        assert opportunity.status == "queued" and opportunity.revision == 1
