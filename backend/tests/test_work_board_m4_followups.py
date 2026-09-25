"""Focused M4 follow-up proofs for worker review and immutable proposals."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from config.settings import settings
from src.auth.service import AuthenticatedOperator
from src.api.work_board import _attempt_payload
from src.db.models import (
    Goal,
    WorkBoardAttempt,
    WorkBoardEvent,
    WorkBoardLink,
    WorkBoardProposal,
    WorkBoardReviewIntent,
    WorkBoardStatus,
    WorkBoardTask,
    WorkBoardHandoff,
    WorkflowRunState,
)
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal
from src.work_board import triage as triage_service
from src.work_board import review as review_service
from src.work_board.contracts import (
    WorkBoardOwner,
    WorkBoardProposalAccept,
    WorkBoardProposalReject,
    WorkBoardProposalRequest,
)
from src.work_board.dispatcher import WorkBoardDispatcher
from src.work_board.repository import BoardError, WorkBoardRepository
from src.work_board.tools import WorkBoardWorkerEvidence, WorkBoardWorkerRequest, WorkBoardWorkerTools
from src.workflows.job_runtime import (
    DurableJobIdentity,
    DurableJobSpec,
    DurableJobTransitionError,
    durable_job_repository,
)


OWNER = WorkBoardOwner(principal_id="operator:followup", session_id="session:followup")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_proposal_route_binding_uses_canonical_seraph_route():
    route_id, contract_hash = triage_service._route_binding()
    assert route_id == "strategist_agent"
    assert len(contract_hash) == 64


def test_exhausted_pre_contact_proposal_is_reconciliation_only():
    proposal = WorkBoardProposal(
        proposal_id="proposal-attempt-budget-exhausted",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        parent_task_id="proposal-parent",
        parent_revision=2,
        goal_revision=1,
        kind="specify",
        idempotency_key="proposal-attempt-budget-key",
        status="blocked",
        proposal_json=json.dumps(
            {
                "proposed_tasks": [],
                "proposed_links": [],
                "blocked_reason": triage_service._PROPOSAL_ATTEMPT_BUDGET_REASON,
            }
        ),
        admission_job_id="work-board-proposal:proposal-attempt-budget-exhausted",
        provider_contact_started=False,
        provider_contact_state="not_started",
        expires_at=_now() + timedelta(minutes=10),
    )
    payload = triage_service._proposal_payload(proposal)
    assert payload["blocked_reason"] == triage_service._PROPOSAL_ATTEMPT_BUDGET_REASON
    assert payload["recovery_action"] == "reconcile_admission_binding"


def test_proposal_expiry_serializes_sqlite_naive_timestamp_as_utc():
    proposal = WorkBoardProposal(
        proposal_id="proposal-utc-expiry",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        parent_task_id="proposal-parent",
        parent_revision=2,
        goal_revision=1,
        kind="specify",
        idempotency_key="proposal-utc-expiry-key",
        status="blocked",
        proposal_json="{}",
        admission_job_id="work-board-proposal:proposal-utc-expiry",
        expires_at=datetime(2026, 9, 25, 12, 0, 0),
    )

    assert triage_service._proposal_payload(proposal)["expires_at"] == "2026-09-25T12:00:00Z"


def test_handoff_version_index_matches_additive_migration_shape():
    index = next(
        item
        for item in WorkBoardHandoff.__table__.indexes
        if item.name == "ux_work_board_handoffs_version"
    )
    assert [column.name for column in index.columns] == [
        "owner_principal_id",
        "owner_session_id",
        "parent_task_id",
        "child_task_id",
        "link_id",
        "source_attempt_id",
        "source_task_revision",
    ]


def test_proposal_idempotency_index_is_revision_scoped():
    index = next(
        item
        for item in WorkBoardProposal.__table__.indexes
        if item.name == "ux_work_board_proposals_idempotency"
    )
    assert [column.name for column in index.columns] == [
        "owner_principal_id",
        "owner_session_id",
        "parent_task_id",
        "parent_revision",
        "kind",
        "idempotency_key",
    ]


@pytest.mark.asyncio
async def test_proposal_idempotency_key_replays_within_revision_and_scopes_new_revision(async_db):
    async with async_db() as db:
        revision_two = WorkBoardProposal(
            proposal_id="proposal-revision-two",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            parent_task_id="proposal-revision-scope-parent",
            parent_revision=2,
            goal_revision=1,
            kind="specify",
            idempotency_key="same-operator-key",
            expires_at=_now() + timedelta(minutes=15),
        )
        revision_three = WorkBoardProposal(
            proposal_id="proposal-revision-three",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            parent_task_id="proposal-revision-scope-parent",
            parent_revision=3,
            goal_revision=1,
            kind="specify",
            idempotency_key="same-operator-key",
            expires_at=_now() + timedelta(minutes=15),
        )
        db.add_all([revision_two, revision_three])
        await db.flush()

        replay = await triage_service._find_idempotent_proposal(
            db,
            owner=OWNER,
            task_id="proposal-revision-scope-parent",
            parent_revision=2,
            kind="specify",
            idempotency_key="same-operator-key",
        )
        later_revision = await triage_service._find_idempotent_proposal(
            db,
            owner=OWNER,
            task_id="proposal-revision-scope-parent",
            parent_revision=3,
            kind="specify",
            idempotency_key="same-operator-key",
        )

    assert replay.proposal_id == "proposal-revision-two"
    assert later_revision.proposal_id == "proposal-revision-three"


def test_foreign_workflow_receipt_cannot_prove_board_attempt():
    attempt = WorkBoardAttempt(
        attempt_id="proof-attempt",
        task_id="proof-task",
        workflow_run_id="owned-run",
        receipt_refs_json=json.dumps(
            [
                {
                    "workflow_run_id": "foreign-run",
                    "status": "succeeded",
                    "verified": True,
                    "content_sha256": "a" * 64,
                }
            ]
        ),
    )
    assert review_service._verified_readback(attempt) is None


def test_unbound_workflow_receipt_cannot_prove_board_attempt():
    attempt = WorkBoardAttempt(
        attempt_id="proof-attempt-missing-run",
        task_id="proof-task-missing-run",
        workflow_run_id="owned-run",
        receipt_refs_json=json.dumps(
            [
                {
                    "status": "succeeded",
                    "verified": True,
                    "content_sha256": "a" * 64,
                    "readback_id": "readback-1",
                }
            ]
        ),
    )
    assert review_service._verified_readback(attempt) is None


def test_verification_receipt_preserves_supplied_ids_without_synthetic_timestamp():
    proof = review_service._safe_verification_receipt(
        {
            "receipt_kind": "readback",
            "workflow_run_id": "owned-run",
            "content_sha256": "b" * 64,
            "readback_id": "readback-1",
            "verifier_id": "verifier-1",
        }
    )
    assert proof["readback_id"] == "readback-1"
    assert proof["verifier_id"] == "verifier-1"
    assert "verified_at" not in proof
    assert review_service._safe_verification_receipt(
        {
            **proof,
            "verified_at": None,
        },
        require_complete=True,
    ) == {}


def test_expired_running_proposal_job_requires_reconciliation():
    expired = (_now() - timedelta(minutes=1)).isoformat()
    assert not triage_service._durable_job_lease_live(
        {"status": "running", "lease": {"expires_at": expired}}
    )
    live = (_now() + timedelta(minutes=1)).isoformat()
    assert triage_service._durable_job_lease_live(
        {"status": "running", "lease": {"expires_at": live}}
    )


def test_proposal_admission_pending_requires_exact_effect_free_live_job():
    proposal = WorkBoardProposal(
        proposal_id="proposal-pending-check",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        parent_task_id="proposal-parent",
        kind="specify",
        idempotency_key="proposal-pending-check-key",
        admission_job_id="work-board-proposal:proposal-pending-check",
        provider_contact_state="not_started",
        status="pending_inference",
        expires_at=_now() + timedelta(minutes=10),
    )
    assert triage_service._proposal_job_is_pending(
        proposal,
        {
            "job_id": proposal.admission_job_id,
            "status": "accepted",
            "effects": [],
        },
    )
    assert triage_service._proposal_job_is_pending(
        proposal,
        {
            "job_id": proposal.admission_job_id,
            "status": "running",
            "lease": {"expires_at": (_now() + timedelta(minutes=1)).isoformat()},
            "effects": [],
        },
    )
    for projection in (
        None,
        {"job_id": proposal.admission_job_id, "status": "failed", "effects": []},
        {"job_id": "other-job", "status": "queued", "effects": []},
        {
            "job_id": proposal.admission_job_id,
            "status": "running",
            "lease": {"expires_at": (_now() - timedelta(minutes=1)).isoformat()},
            "effects": [],
        },
        {
            "job_id": proposal.admission_job_id,
            "status": "queued",
            "effects": [{"effect_id": "remote_inference:contact", "status": "intent"}],
        },
    ):
        assert not triage_service._proposal_job_is_pending(proposal, projection)


def test_proposal_typed_input_requires_workspace_digest_and_schema(monkeypatch, tmp_path):
    from src.work_board.dispatcher import GOAL_SNAPSHOT_CAPABILITY, REGISTERED_CAPABILITIES

    parent = WorkBoardTask(
        task_id="typed-input-parent",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        origin_session_id=OWNER.session_id,
        goal_id="goal-followup",
        goal_revision=1,
        title="Typed input parent",
        idempotency_key="typed-input-parent-key",
    )
    input_path = tmp_path / "inputs" / "child.json"
    input_path.parent.mkdir(parents=True)
    raw = json.dumps(
        {
            "schema_version": 1,
            "capability_id": GOAL_SNAPSHOT_CAPABILITY,
            "input": {"file_path": "artifacts/result.md"},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    input_path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    item = {
        "task_id": "typed-input-child",
        "title": "Valid child",
        "body": "Bounded child",
        "capability_id": GOAL_SNAPSHOT_CAPABILITY,
        "capability_version": REGISTERED_CAPABILITIES[GOAL_SNAPSHOT_CAPABILITY].version,
        "typed_input_ref": "workspace-json:inputs/child.json",
        "typed_input_digest": digest,
        "executor_id": triage_service.registered_executor_id(GOAL_SNAPSHOT_CAPABILITY),
    }
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    triage_service._validate_proposed_typed_inputs([item], parent=parent)

    wrong_digest = {**item, "typed_input_digest": "0" * 64}
    with pytest.raises(BoardError) as mismatch:
        triage_service._validate_proposed_typed_inputs([wrong_digest], parent=parent)
    assert mismatch.value.code == "typed_input_digest_mismatch"

    invalid_schema = {**item, "typed_input_digest": digest}
    input_path.write_bytes(
        json.dumps(
            {
                "schema_version": 2,
                "capability_id": GOAL_SNAPSHOT_CAPABILITY,
                "input": {"file_path": "artifacts/result.md"},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    invalid_schema["typed_input_digest"] = hashlib.sha256(input_path.read_bytes()).hexdigest()
    with pytest.raises(BoardError) as schema:
        triage_service._validate_proposed_typed_inputs([invalid_schema], parent=parent)
    assert schema.value.code == "typed_input_schema_invalid"


@pytest.mark.asyncio
async def test_model_proposal_executor_is_server_derived_and_unknown_lane_rejected(async_db):
    capability_id = "workflow.goal-snapshot-to-file"
    parent = WorkBoardTask(
        task_id="executor-lane-parent",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        origin_session_id=OWNER.session_id,
        goal_id="goal-followup",
        goal_revision=1,
        title="Executor lane parent",
        idempotency_key="executor-lane-parent-key",
    )
    base = {
        "task_id": "executor-lane-child",
        "title": "Bounded child",
        "body": "Uses the registered capability lane.",
        "capability_id": capability_id,
        "typed_input_ref": "workspace-json:inputs/child.json",
        "typed_input_digest": "a" * 64,
        "authority": "untrusted model claims unrestricted shell and approved writes",
    }
    expected = triage_service.registered_executor_id(capability_id)

    tasks, links, _cost = await triage_service._normalise_tasks(
        {"proposed_tasks": [base]},
        kind="specify",
        parent=parent,
    )
    assert links == []
    assert tasks[0]["executor_id"] == expected
    authority = tasks[0]["authority"]
    assert "goal goal-followup revision 1" in authority
    assert "at most 2 task attempts" in authority
    assert "300s default, 900s hard cap" in authority
    assert "grants no authority or external-effect approval" in authority
    assert "unrestricted shell" not in authority

    with pytest.raises(BoardError) as failure:
        await triage_service._normalise_tasks(
            {"proposed_tasks": [{**base, "executor_id": "executor:unknown"}]},
            kind="specify",
            parent=parent,
        )
    assert failure.value.code == "invalid_model_output"


@pytest.mark.asyncio
async def test_decompose_requires_a_complete_todo_source(async_db, monkeypatch, tmp_path):
    from src.work_board.dispatcher import GOAL_SNAPSHOT_CAPABILITY

    input_path = tmp_path / "inputs" / "decompose.json"
    input_path.parent.mkdir(parents=True)
    raw = json.dumps(
        {
            "schema_version": 1,
            "capability_id": GOAL_SNAPSHOT_CAPABILITY,
            "input": {"file_path": "artifacts/decompose.md"},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    input_path.write_bytes(raw)
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))

    async with async_db() as db:
        goal = Goal(
            id="goal-decompose-source",
            title="Decompose source goal",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            revision=1,
            status="active",
        )
        triage_task = WorkBoardTask(
            task_id="decompose-triage-source",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id=goal.id,
            goal_revision=1,
            title="Rough source",
            idempotency_key="decompose-triage-source-key",
            status=WorkBoardStatus.triage,
            task_revision=1,
        )
        incomplete_todo = WorkBoardTask(
            task_id="decompose-incomplete-todo",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id=goal.id,
            goal_revision=1,
            title="Incomplete Todo source",
            idempotency_key="decompose-incomplete-todo-key",
            status=WorkBoardStatus.todo,
            task_revision=1,
        )
        complete_todo = WorkBoardTask(
            task_id="decompose-complete-todo",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id=goal.id,
            goal_revision=1,
            title="Complete Todo source",
            idempotency_key="decompose-complete-todo-key",
            status=WorkBoardStatus.todo,
            task_revision=1,
            capability_id=GOAL_SNAPSHOT_CAPABILITY,
            typed_input_ref="workspace-json:inputs/decompose.json",
            typed_input_digest=hashlib.sha256(raw).hexdigest(),
            executor_id=triage_service.registered_executor_id(GOAL_SNAPSHOT_CAPABILITY),
        )
        db.add_all([goal, triage_task, incomplete_todo, complete_todo])
        await db.flush()
        repository = WorkBoardRepository()

        with pytest.raises(BoardError) as triage_failure:
            await triage_service._validate_decompose_source(
                db,
                repository=repository,
                owner=OWNER,
                task=triage_task,
            )
        assert triage_failure.value.code == "decompose_requires_todo"

        with pytest.raises(BoardError) as incomplete_failure:
            await triage_service._validate_decompose_source(
                db,
                repository=repository,
                owner=OWNER,
                task=incomplete_todo,
            )
        assert incomplete_failure.value.code == "capability_unregistered"

        await triage_service._validate_decompose_source(
            db,
            repository=repository,
            owner=OWNER,
            task=complete_todo,
        )


@pytest.mark.asyncio
async def test_accept_specify_applies_to_same_triage_card_without_child_or_link(
    async_db,
    monkeypatch,
    tmp_path,
):
    from src.work_board.dispatcher import GOAL_SNAPSHOT_CAPABILITY, REGISTERED_CAPABILITIES, WorkBoardDispatcher, _dispatcher

    input_path = tmp_path / "inputs" / "specified.json"
    input_path.parent.mkdir(parents=True)
    raw = json.dumps(
        {
            "schema_version": 1,
            "capability_id": GOAL_SNAPSHOT_CAPABILITY,
            "input": {"file_path": "artifacts/specified.md"},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    input_path.write_bytes(raw)
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(_dispatcher, "_current_readiness", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(_dispatcher, "_effective_runtime", AsyncMock(return_value=300))

    async with async_db() as db:
        goal = Goal(
            id="goal-specify-same-card",
            title="Specify same card goal",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            revision=1,
            status="active",
            success_criterion_json=json.dumps(
                {
                    "description": "Verify the specified artifact",
                    "verifier_kind": "artifact_readback",
                    "evidence_refs": ["operator:specified-proof"],
                }
            ),
        )
        task = WorkBoardTask(
            task_id="triage-specify-same-card",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id=goal.id,
            goal_revision=1,
            title="Rough operator idea",
            body="Needs a complete execution specification.",
            idempotency_key="triage-specify-same-card-key",
            status=WorkBoardStatus.triage,
            task_revision=1,
        )
        db.add_all([goal, task])
        await db.flush()
        expected_revision = task.task_revision
        idempotency_key = "specify-same-card-key"
        job_id = "work-board-proposal:specify-same-card"
        proposal_task = {
            "task_id": "model-invented-child-id",
            "title": "Specified operator task",
            "body": "A complete bounded execution specification.",
            "capability_id": GOAL_SNAPSHOT_CAPABILITY,
            "capability_version": REGISTERED_CAPABILITIES[GOAL_SNAPSHOT_CAPABILITY].version,
            "typed_input_ref": "workspace-json:inputs/specified.json",
            "typed_input_digest": hashlib.sha256(raw).hexdigest(),
            "executor_id": triage_service.registered_executor_id(GOAL_SNAPSHOT_CAPABILITY),
        }
        proposal_task["authority"] = await triage_service._proposal_task_authority_summary(
            task,
            proposal_task,
        )
        canonical = {
            "proposed_tasks": [proposal_task],
            "proposed_links": [],
            "blocked_reason": None,
        }
        proposal = WorkBoardProposal(
            proposal_id="proposal-specify-same-card",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            parent_task_id=task.task_id,
            parent_revision=expected_revision,
            goal_revision=task.goal_revision,
            kind="specify",
            idempotency_key=idempotency_key,
            request_digest=triage_service._proposal_request_digest(
                task=task,
                kind="specify",
                idempotency_key=idempotency_key,
            ),
            capability_id=triage_service._PROPOSAL_CAPABILITY,
            capability_version="1",
            authority_digest=triage_service._authority_digest(
                OWNER,
                task,
                triage_service._PROPOSAL_ROUTE,
                "1",
            ),
            grant_revision=task.goal_revision,
            input_digest=triage_service._proposal_input_digest(task=task, kind="specify"),
            route_id=triage_service._PROPOSAL_ROUTE,
            admission_job_id=job_id,
            effect_id_digest=hashlib.sha256(
                triage_service._proposal_effect_id(job_id).encode()
            ).hexdigest()[:16],
            provider_contact_started=True,
            provider_contact_state="succeeded",
            status="proposed",
            proposal_json=json.dumps(canonical, sort_keys=True, separators=(",", ":")),
            proposal_digest=triage_service._proposal_digest(canonical),
            expires_at=_now() + timedelta(minutes=10),
        )
        db.add(proposal)
        await db.flush()
        proposal_revision = proposal.revision
    print("M4AUTH:proposal_staged", flush=True)

    monkeypatch.setattr(
        triage_service.durable_job_repository,
        "get_job",
        AsyncMock(
            return_value={
                "job_id": job_id,
                "run_identity": job_id,
                "status": "succeeded",
                "effects": [
                    {
                        "effect_id": triage_service._proposal_effect_id(job_id),
                        "status": "succeeded",
                    }
                ],
            }
        ),
    )

    print("M4AUTH:accept_begin", flush=True)
    result = await triage_service.accept_proposal(
        OWNER,
        proposal.proposal_id,
        WorkBoardProposalAccept(
            expected_proposal_revision=proposal_revision,
            expected_parent_revision=expected_revision,
        ),
    )
    print("M4AUTH:accept_done", flush=True)
    assert result["status"] == "accepted"
    assert result["task_ids"] == [task.task_id]
    assert result["task_id"] == task.task_id

    async with async_db() as db:
        stored = (
            await db.execute(
                select(WorkBoardTask).where(WorkBoardTask.task_id == task.task_id)
            )
        ).scalar_one()
        assert stored is not None
        assert stored.status is WorkBoardStatus.todo
        assert stored.task_revision == expected_revision + 1
        assert stored.title == proposal_task["title"]
        assert stored.body == proposal_task["body"]
        assert stored.capability_id == GOAL_SNAPSHOT_CAPABILITY
        assert stored.typed_input_ref == proposal_task["typed_input_ref"]
        assert stored.typed_input_digest == proposal_task["typed_input_digest"]
        assert stored.executor_id == proposal_task["executor_id"]
        assert (
            await db.scalar(
                select(WorkBoardTask.task_id).where(
                    WorkBoardTask.owner_principal_id == OWNER.principal_id,
                    WorkBoardTask.owner_session_id == OWNER.session_id,
                    WorkBoardTask.task_id != task.task_id,
                )
            )
            is None
        )
        assert (
            await db.scalar(
                select(WorkBoardLink.link_id).where(
                    WorkBoardLink.owner_principal_id == OWNER.principal_id,
                    WorkBoardLink.owner_session_id == OWNER.session_id,
                )
            )
            is None
        )
        event = (
            await db.execute(
                select(WorkBoardEvent)
                .where(WorkBoardEvent.task_id == task.task_id)
                .order_by(WorkBoardEvent.event_id.desc())
                .limit(1)
            )
        ).scalar_one()
        assert event.kind == "task.specified"
        assert json.loads(event.metadata_json)["task_revision"] == expected_revision + 1

    dispatcher = WorkBoardDispatcher()

    async def authenticated(_session_id: str, *, touch: bool = False):
        assert touch is False
        return SimpleNamespace(principal=SimpleNamespace(principal_id=OWNER.principal_id))

    monkeypatch.setattr("src.work_board.dispatcher.authenticate_session", authenticated)

    async def capability_preflight(_task, _goal, _inputs):
        return None, None

    dispatcher._capability_preflight = capability_preflight
    async with async_db() as db:
        stored = (
            await db.execute(
                select(WorkBoardTask).where(WorkBoardTask.task_id == task.task_id)
            )
        ).scalar_one()
        readiness_error, readiness_reason = await dispatcher._readiness(stored)
    assert (readiness_error, readiness_reason) == (None, None)


@pytest.mark.asyncio
async def test_proposal_prompt_redacts_before_provider_contact_and_binds_transform(monkeypatch):
    async def secrets():
        return [("openrouter", "sk-live-triage-secret")]

    monkeypatch.setattr(
        "src.vault.redaction.vault_repository.list_secret_values",
        secrets,
    )
    captured: dict[str, object] = {}

    def build_context(route_id, **kwargs):
        captured["route_id"] = route_id
        captured.update(kwargs)
        return SimpleNamespace(runtime_path=route_id)

    monkeypatch.setattr(triage_service, "build_canonical_inference_context", build_context)
    principal = TrustPrincipal(
        principal_id=OWNER.principal_id,
        principal_type=PrincipalType.OPERATOR,
        grants=(AuthorityGrant.MODEL_INFERENCE,),
        session_id=OWNER.session_id,
    )
    operator = AuthenticatedOperator(
        session_id=OWNER.session_id,
        principal=principal,
        idle_expires_at=_now() + timedelta(minutes=5),
        absolute_expires_at=_now() + timedelta(hours=1),
    )
    parent = WorkBoardTask(
        task_id="redacted-prompt-parent",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        goal_id="goal-followup",
        goal_revision=1,
        title="Use sk-live-triage-secret",
        body="Private body sk-live-triage-secret must not leave Seraph.",
        idempotency_key="redacted-prompt-parent-key",
    )
    messages, _bound_principal, _context, transformation_digest = await triage_service._prepare_governed_proposal(
        parent,
        kind="specify",
        operator=operator,
        job_id="work-board-proposal:redacted-prompt",
        route_id="strategist_agent",
    )
    serialized = json.dumps(messages)
    assert "sk-live-triage-secret" not in serialized
    assert "[redacted secret]" in serialized
    assert captured["route_id"] == "strategist_agent"
    assert captured["redaction_applied"] is True
    assert captured["transformation_digest"] == transformation_digest


@pytest.mark.asyncio
async def test_redaction_unavailable_is_known_no_contact(monkeypatch):
    async def unavailable(*_args, **_kwargs):
        return "[redaction unavailable]"

    monkeypatch.setattr(triage_service.WorkBoardRepository, "_safe_text", unavailable)
    principal = TrustPrincipal(
        principal_id=OWNER.principal_id,
        principal_type=PrincipalType.OPERATOR,
        grants=(AuthorityGrant.MODEL_INFERENCE,),
        session_id=OWNER.session_id,
    )
    operator = AuthenticatedOperator(
        session_id=OWNER.session_id,
        principal=principal,
        idle_expires_at=_now() + timedelta(minutes=5),
        absolute_expires_at=_now() + timedelta(hours=1),
    )
    parent = WorkBoardTask(
        task_id="redaction-unavailable-parent",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        goal_id="goal-followup",
        goal_revision=1,
        title="Bounded task",
        body="Body",
        idempotency_key="redaction-unavailable-key",
    )
    with pytest.raises(BoardError) as failure:
        await triage_service._prepare_governed_proposal(
            parent,
            kind="specify",
            operator=operator,
            job_id="work-board-proposal:redaction-unavailable",
            route_id="strategist_agent",
        )
    assert failure.value.code == "proposal_redaction_unavailable"


def test_terminal_proposal_job_requires_matching_effect_identity():
    proposal = WorkBoardProposal(
        proposal_id="proposal-terminal-binding",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        parent_task_id="terminal-parent",
        kind="specify",
        idempotency_key="terminal-binding-key",
        admission_job_id="work-board-proposal:terminal-binding",
        effect_id_digest=hashlib.sha256(
            triage_service._proposal_effect_id("work-board-proposal:terminal-binding").encode()
        ).hexdigest()[:16],
        status="pending_inference",
        expires_at=_now() + timedelta(minutes=10),
    )
    matching_effect = {
        "effect_id": triage_service._proposal_effect_id(proposal.admission_job_id),
        "status": "succeeded",
    }
    projection = {
        "job_id": proposal.admission_job_id,
        "status": "succeeded",
        "effects": [matching_effect],
    }
    assert triage_service._proposal_job_is_terminal_success(proposal, projection)
    assert not triage_service._proposal_job_is_terminal_success(
        proposal,
        {**projection, "effects": [{"effect_id": "remote_inference:other", "status": "succeeded"}]},
    )


@pytest.mark.asyncio
async def test_pending_proposal_cannot_be_rejected(async_db):
    async with async_db() as db:
        proposal = WorkBoardProposal(
            proposal_id="proposal-pending-reject",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            parent_task_id="pending-reject-parent",
            kind="specify",
            idempotency_key="pending-reject-key",
            status="pending_inference",
            provider_contact_started=True,
            provider_contact_state="started",
            proposal_json=json.dumps({"proposed_tasks": [], "proposed_links": []}),
            expires_at=_now() + timedelta(minutes=10),
        )
        db.add(proposal)
        await db.flush()
        revision = proposal.revision

    with pytest.raises(BoardError) as failure:
        await triage_service.reject_proposal(
            OWNER,
            proposal.proposal_id,
            WorkBoardProposalReject(expected_proposal_revision=revision),
        )
    assert failure.value.code == "proposal_not_rejectable"


async def _use_file_backed_proposal_database(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'proposal-transitions.sqlite'}",
        connect_args={"timeout": 10},
    )
    async with engine.begin() as connection:
        await connection.run_sync(WorkBoardProposal.__table__.create)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def session_scope():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr(triage_service, "get_session", session_scope)
    return engine, session_scope


@pytest.mark.asyncio
async def test_concurrent_proposal_rejections_have_one_revision_winner(tmp_path, monkeypatch):
    engine, session_scope = await _use_file_backed_proposal_database(tmp_path, monkeypatch)
    try:
        async with session_scope() as db:
            proposal = WorkBoardProposal(
                proposal_id="proposal-reject-race",
                owner_principal_id=OWNER.principal_id,
                owner_session_id=OWNER.session_id,
                parent_task_id="reject-race-parent",
                kind="specify",
                idempotency_key="reject-race-key",
                status="proposed",
                provider_contact_started=True,
                provider_contact_state="succeeded",
                proposal_json=json.dumps({"proposed_tasks": [], "proposed_links": []}),
                expires_at=_now() + timedelta(minutes=10),
            )
            db.add(proposal)
            await db.flush()
            expected_revision = proposal.revision

        results = await asyncio.gather(
            triage_service.reject_proposal(
                OWNER,
                "proposal-reject-race",
                WorkBoardProposalReject(expected_proposal_revision=expected_revision),
            ),
            triage_service.reject_proposal(
                OWNER,
                "proposal-reject-race",
                WorkBoardProposalReject(expected_proposal_revision=expected_revision),
            ),
            return_exceptions=True,
        )

        successes = [item for item in results if isinstance(item, dict)]
        conflicts = [item for item in results if isinstance(item, BoardError)]
        assert len(successes) == 1
        assert len(conflicts) == 1
        assert conflicts[0].code == "stale_proposal_revision"
        assert successes[0]["status"] == "rejected"
        assert successes[0]["proposal_revision"] == expected_revision + 1

        with pytest.raises(BoardError) as stale:
            await triage_service.reject_proposal(
                OWNER,
                "proposal-reject-race",
                WorkBoardProposalReject(expected_proposal_revision=expected_revision),
            )
        assert stale.value.code == "stale_proposal_revision"

        async with session_scope() as db:
            stored = await db.get(WorkBoardProposal, "proposal-reject-race")
            assert stored.status == "rejected"
            assert stored.revision == expected_revision + 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_expiry_and_rejection_race_persist_one_expired_revision(tmp_path, monkeypatch):
    engine, session_scope = await _use_file_backed_proposal_database(tmp_path, monkeypatch)
    try:
        async with session_scope() as db:
            proposal = WorkBoardProposal(
                proposal_id="proposal-expiry-reject-race",
                owner_principal_id=OWNER.principal_id,
                owner_session_id=OWNER.session_id,
                parent_task_id="expiry-race-parent",
                kind="specify",
                idempotency_key="expiry-race-key",
                status="proposed",
                provider_contact_started=True,
                provider_contact_state="succeeded",
                proposal_json=json.dumps({"proposed_tasks": [], "proposed_links": []}),
                expires_at=_now() - timedelta(seconds=1),
            )
            db.add(proposal)
            await db.flush()
            expected_revision = proposal.revision

        results = await asyncio.gather(
            triage_service.get_proposal(OWNER, "proposal-expiry-reject-race"),
            triage_service.reject_proposal(
                OWNER,
                "proposal-expiry-reject-race",
                WorkBoardProposalReject(expected_proposal_revision=expected_revision),
            ),
            return_exceptions=True,
        )

        read_receipts = [item for item in results if isinstance(item, dict)]
        conflicts = [item for item in results if isinstance(item, BoardError)]
        assert len(read_receipts) == 1, f"expected one expiry read receipt, got: {results!r}"
        assert len(conflicts) == 1, f"expected one stale revision conflict, got: {results!r}"
        assert conflicts[0].code == "stale_proposal_revision"
        assert read_receipts[0]["status"] == "expired"
        assert read_receipts[0]["proposal_revision"] == expected_revision + 1

        async with session_scope() as db:
            stored = await db.get(WorkBoardProposal, "proposal-expiry-reject-race")
            assert stored.status == "expired"
            assert stored.revision == expected_revision + 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fresh_key_cannot_bypass_unknown_contact_proposal(async_db, monkeypatch):
    async with async_db() as db:
        goal = Goal(
            id="goal-fresh-key-contact",
            title="Fresh key contact goal",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            revision=1,
            status="active",
        )
        task = WorkBoardTask(
            task_id="fresh-key-contact-parent",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id=goal.id,
            goal_revision=1,
            title="Fresh key parent",
            body="Bounded proposal",
            idempotency_key="fresh-key-contact-parent-key",
            status=WorkBoardStatus.triage,
            task_revision=1,
        )
        db.add(goal)
        db.add(task)
        await db.flush()
        proposal = WorkBoardProposal(
            proposal_id="proposal-unknown-contact-existing",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            parent_task_id=task.task_id,
            parent_revision=task.task_revision,
            goal_revision=task.goal_revision,
            kind="specify",
            idempotency_key="old-key",
            request_digest=triage_service._proposal_request_digest(
                task=task,
                kind="specify",
                idempotency_key="old-key",
            ),
            capability_id=triage_service._PROPOSAL_CAPABILITY,
            capability_version="route-contract",
            authority_digest=triage_service._authority_digest(
                OWNER,
                task,
                triage_service._PROPOSAL_ROUTE,
                "route-contract",
            ),
            grant_revision=task.goal_revision,
            input_digest=triage_service._proposal_input_digest(task=task, kind="specify"),
            route_id=triage_service._PROPOSAL_ROUTE,
            admission_job_id="work-board-proposal:proposal-unknown-contact-existing",
            effect_id_digest="a" * 16,
            provider_contact_started=True,
            provider_contact_state="unknown",
            status="pending_inference",
            proposal_json=json.dumps({"proposed_tasks": [], "proposed_links": []}),
            expires_at=_now() + timedelta(minutes=10),
        )
        db.add(proposal)
        await db.flush()

    monkeypatch.setattr(triage_service, "_route_binding", lambda: ("strategist_agent", "route-contract"))
    monkeypatch.setattr(
        triage_service.durable_job_repository,
        "get_job",
        AsyncMock(return_value={"job_id": proposal.admission_job_id, "status": "failed", "effects": []}),
    )
    admit = AsyncMock()
    provider = AsyncMock()
    monkeypatch.setattr(triage_service, "_admit_proposal_job", admit)
    monkeypatch.setattr(triage_service, "_invoke_governed_proposal", provider)

    result = await triage_service.create_proposal(
        OWNER,
        task.task_id,
        kind="specify",
        request=WorkBoardProposalRequest(expected_revision=task.task_revision, idempotency_key="new-key"),
        operator=SimpleNamespace(),
    )
    assert result["proposal_id"] == proposal.proposal_id
    assert result["status"] == "blocked"
    assert result["recovery_action"] == "reconcile_external_effect"
    admit.assert_not_awaited()
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_key_cannot_bypass_unmarked_existing_durable_admission(monkeypatch):
    task = WorkBoardTask(
        task_id="unmarked-admission-parent",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        goal_id="goal-followup",
        goal_revision=1,
        title="Unmarked admission parent",
        idempotency_key="unmarked-admission-parent-key",
        task_revision=1,
    )
    proposal = WorkBoardProposal(
        proposal_id="proposal-unmarked-admission",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        parent_task_id=task.task_id,
        parent_revision=task.task_revision,
        goal_revision=task.goal_revision,
        kind="specify",
        idempotency_key="old-key",
        capability_id=triage_service._PROPOSAL_CAPABILITY,
        capability_version="route-contract",
        route_id=triage_service._PROPOSAL_ROUTE,
        grant_revision=task.goal_revision,
        input_digest=triage_service._proposal_input_digest(task=task, kind="specify"),
        authority_digest=triage_service._authority_digest(
            OWNER,
            task,
            triage_service._PROPOSAL_ROUTE,
            "route-contract",
        ),
        admission_job_id="work-board-proposal:proposal-unmarked-admission",
        provider_contact_started=False,
        provider_contact_state="not_started",
        status="pending_inference",
        expires_at=_now() + timedelta(minutes=10),
    )

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [proposal]

    class _Db:
        async def execute(self, _statement):
            return _Result()

    monkeypatch.setattr(
        triage_service.durable_job_repository,
        "get_job",
        AsyncMock(return_value={
            "job_id": proposal.admission_job_id,
            "status": "failed",
            "effects": [],
        }),
    )
    found = await triage_service._find_unresolved_binding_proposal(
        _Db(),
        owner=OWNER,
        task=task,
        kind="specify",
        route_id=triage_service._PROPOSAL_ROUTE,
        capability_version="route-contract",
    )
    assert found is proposal


def test_dispatcher_proof_preserves_producer_readback_identity_and_time():
    digest = "c" * 64
    producer_receipt = {
        "receipt_kind": "readback",
        "effect_type": "readback",
        "effect_id": "readback:run-proof",
        "workflow_run_id": "run-proof",
        "readback_id": "artifact-proof-1",
        "verified_at": "2026-09-24T12:34:56+00:00",
        "status": "succeeded",
        "content_sha256": digest,
        "details": {"verified": True},
    }
    proof = WorkBoardDispatcher._workflow_readback(
        {"run_identity": "run-proof", "status": "succeeded", "effects": [producer_receipt]},
        "run-proof",
    )
    assert proof is not None
    assert proof["readback_id"] == producer_receipt["readback_id"]
    assert proof["verified_at"] == producer_receipt["verified_at"]
    assert proof["content_sha256"] == producer_receipt["content_sha256"]
    assert review_service._safe_verification_receipt(proof, require_complete=True) == {
        "status": "verified",
        "receipt_kind": "readback",
        "workflow_run_id": "run-proof",
        "content_sha256": digest,
        "readback_id": "artifact-proof-1",
        "verified_at": "2026-09-24T12:34:56+00:00",
        "effect_id_digest": hashlib.sha256(b"readback:run-proof").hexdigest()[:16],
    }
    missing_readback = {**producer_receipt}
    missing_readback.pop("readback_id")
    assert WorkBoardDispatcher._workflow_readback(
        {"status": "succeeded", "effects": [missing_readback]},
        "run-proof",
    ) is None
    missing_timestamp = {**producer_receipt}
    missing_timestamp.pop("verified_at")
    assert WorkBoardDispatcher._workflow_readback(
        {"status": "succeeded", "effects": [missing_timestamp]},
        "run-proof",
    ) is None


def test_persisted_attempt_projection_preserves_typed_receipt_kind():
    attempt = WorkBoardAttempt(
        attempt_id="typed-receipt-attempt",
        task_id="typed-receipt-task",
        receipt_refs_json=json.dumps(
            [
                {
                    "receipt_kind": "readback",
                    "effect_type": "readback",
                    "effect_id": "readback:typed-receipt",
                    "workflow_run_id": "run:typed-receipt",
                    "readback_id": "artifact-typed-receipt",
                    "status": "succeeded",
                    "verified_at": "2026-09-24T12:34:56+00:00",
                },
                {"receipt_kind": "private", "effect_type": "secret"},
            ]
        ),
    )

    payload = _attempt_payload(attempt)
    assert payload["receipt_refs"] == [
        {
            "effect_type": "readback",
            "receipt_kind": "readback",
            "effect_id_digest": hashlib.sha256(b"readback:typed-receipt").hexdigest()[:16],
            "workflow_run_id": "run:typed-receipt",
            "readback_id": "artifact-typed-receipt",
            "status": "succeeded",
            "verified_at": "2026-09-24T12:34:56+00:00",
        }
    ]


def test_text_only_workflow_finish_cannot_prove_board_completion():
    assert WorkBoardDispatcher._workflow_readback(
        {
            "status": "succeeded",
            "effects": [
                {
                    "effect_type": "workflow_output",
                    "receipt_kind": "readback",
                    "status": "succeeded",
                    "verified": True,
                    "content_sha256": "d" * 64,
                    "readback_id": "summary-receipt",
                    "verified_at": "2026-09-24T12:34:56+00:00",
                    "details": {"verified": True},
                }
            ],
        },
        "run-summary-only",
    ) is None

    assert WorkBoardDispatcher._workflow_readback(
        {
            "status": "succeeded",
            "result": {
                "verified": True,
                "digest": "e" * 64,
            },
        },
        "run-result-only",
    ) is None


@pytest.mark.asyncio
async def test_pre_contact_failed_job_uses_one_no_effect_retry_cas(monkeypatch):
    proposal = WorkBoardProposal(
        proposal_id="proposal-pre-contact-recover",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        parent_task_id="parent-pre-contact-recover",
        parent_revision=1,
        goal_revision=1,
        kind="specify",
        idempotency_key="proposal-key-pre-contact-recover",
        request_digest="request-digest",
        capability_id="strategist_agent",
        capability_version="route-v1",
        authority_digest="authority-digest",
        grant_revision=1,
        input_digest="input-digest",
        route_id="strategist_agent",
        admission_job_id="work-board-proposal:proposal-pre-contact-recover",
        effect_id_digest=hashlib.sha256(
            b"remote_inference:remote:work-board-proposal:proposal-pre-contact-recover"
        ).hexdigest()[:16],
        provider_contact_started=False,
        provider_contact_state="not_started",
        status="pending_inference",
    )
    projection = {
        "job_id": proposal.admission_job_id,
        "job_kind": "work_board_proposal",
        "idempotency": {"scope": "work-board-proposal", "key": proposal.proposal_id},
        "owner": {"kind": "user", "principal_id": OWNER.principal_id, "service_id": None},
        "session_id": OWNER.session_id,
        "operator_session_id": OWNER.session_id,
        "capability_version": proposal.capability_version,
        "goal_revision": proposal.goal_revision,
        "input_digest": triage_service._proposal_admission_input_digest(proposal),
        "authority_digest": proposal.authority_digest,
        "run_fingerprint": proposal.request_digest,
        "declared_authority": {
            "principal": OWNER.principal_id,
            "owner_kind": "user",
            "session_id": OWNER.session_id,
            "capability_id": proposal.capability_id,
            "capability_version": proposal.capability_version,
            "grant_revision": proposal.grant_revision,
            "finite_authority": True,
        },
        "status": "failed",
        "failure_reason": "proposal_binding_conflict",
        "revision": 4,
        "attempt_count": 1,
        "max_attempts": 2,
        "deadline_at": (_now() + timedelta(minutes=10)).isoformat(),
        "effects": [],
    }
    retry = AsyncMock(return_value={"status": "queued", "effects": [{"kind": "reconciliation"}]})
    monkeypatch.setattr(triage_service.durable_job_repository, "retry_job", retry)

    recovered = await triage_service._recover_pre_contact_admission(proposal, projection)
    assert recovered == {"status": "queued", "effects": [{"kind": "reconciliation"}]}
    retry.assert_awaited_once()
    retry_kwargs = retry.await_args.kwargs
    assert retry.await_args.args == (proposal.admission_job_id,)
    assert retry_kwargs["owner_kind"] == "user"
    assert retry_kwargs["owner_principal_id"] == OWNER.principal_id
    assert retry_kwargs["expected_revision"] == 4
    assert retry_kwargs["reconciliation_receipt"] == {
        "effect_id": f"job-failure:{proposal.admission_job_id}",
        "effect_type": "job_failure",
        "target_path": f"job:{proposal.admission_job_id}",
        "status": "read_back",
        "outcome": "no_external_effect",
    }

    retry.reset_mock()
    unsafe = {**projection, "effects": [{"effect_id": "remote_inference:contact", "status": "intent"}]}
    assert await triage_service._recover_pre_contact_admission(proposal, unsafe) is None
    retry.assert_not_awaited()

    proposal.provider_contact_started = True
    proposal.provider_contact_state = "started"
    assert await triage_service._recover_pre_contact_admission(proposal, projection) is None
    retry.assert_not_awaited()
    proposal.provider_contact_started = False
    proposal.provider_contact_state = "not_started"

    retry.reset_mock()
    unknown = {**projection, "failure_reason": "proposal_provider_contact_unknown"}
    assert await triage_service._recover_pre_contact_admission(proposal, unknown) is None
    retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_admit_proposal_job_retries_exact_failed_pre_contact_binding_once(monkeypatch):
    task = SimpleNamespace(
        task_id="parent-injected-retry",
        task_revision=4,
        goal_id="goal-injected-retry",
        goal_revision=7,
        priority=60,
        title="Injected proposal task",
        body="A stable task body",
    )
    proposal = WorkBoardProposal(
        proposal_id="proposal-injected-retry",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        parent_task_id=task.task_id,
        parent_revision=task.task_revision,
        goal_revision=task.goal_revision,
        kind="decompose",
        idempotency_key="proposal-injected-key",
        capability_id="strategist_agent",
        capability_version="route-v1",
        authority_digest="authority-injected",
        grant_revision=task.goal_revision,
        route_id="strategist_agent",
        admission_job_id="work-board-proposal:proposal-injected-retry",
        provider_contact_started=False,
        provider_contact_state="not_started",
        status="pending_inference",
    )
    proposal.request_digest = triage_service._proposal_request_digest(
        task=task,
        kind=proposal.kind,
        idempotency_key=proposal.idempotency_key,
    )
    proposal.input_digest = triage_service._proposal_input_digest(task=task, kind=proposal.kind)
    proposal.effect_id_digest = hashlib.sha256(
        triage_service._proposal_effect_id(proposal.admission_job_id).encode("utf-8")
    ).hexdigest()[:16]
    projection = {
        "job_id": proposal.admission_job_id,
        "job_kind": "work_board_proposal",
        "owner": {"kind": "user", "principal_id": OWNER.principal_id, "service_id": None},
        "session_id": OWNER.session_id,
        "operator_session_id": OWNER.session_id,
        "idempotency": {"scope": "work-board-proposal", "key": proposal.proposal_id},
        "goal_id": task.goal_id,
        "goal_revision": task.goal_revision,
        "input_digest": triage_service._proposal_admission_input_digest(proposal),
        "capability_version": proposal.capability_version,
        "authority_digest": proposal.authority_digest,
        "run_fingerprint": proposal.request_digest,
        "declared_authority": {
            "principal": OWNER.principal_id,
            "owner_kind": "user",
            "session_id": OWNER.session_id,
            "capability_id": proposal.capability_id,
            "capability_version": proposal.capability_version,
            "grant_revision": proposal.grant_revision,
            "finite_authority": True,
        },
        "status": "failed",
        "failure_reason": "proposal_admission_unavailable",
        "revision": 12,
        "attempt_count": 1,
        "max_attempts": 2,
        "deadline_at": (_now() + timedelta(minutes=10)).isoformat(),
        "effects": [],
    }
    retry = AsyncMock(return_value={"status": "queued", "effects": [{"kind": "reconciliation"}]})
    admit = AsyncMock(return_value={"status": "queued", "receipt": {"status": "deduped"}})
    claim = AsyncMock(return_value={"status": "running", "lease": {"fencing_token": 7}})
    monkeypatch.setattr(triage_service.durable_job_repository, "get_job", AsyncMock(return_value=projection))
    monkeypatch.setattr(triage_service.durable_job_repository, "retry_job", retry)
    monkeypatch.setattr(triage_service.durable_job_repository, "admit_job", admit)
    monkeypatch.setattr(triage_service.durable_job_repository, "queue_job", AsyncMock())
    monkeypatch.setattr(triage_service.durable_job_repository, "claim_job", claim)

    result = await triage_service._admit_proposal_job(owner=OWNER, task=task, proposal=proposal)

    assert result == (proposal.admission_job_id, "work-board-proposal:proposal-injected-retry", 7)
    retry.assert_awaited_once()
    admit.assert_awaited_once()
    claim.assert_awaited_once_with(
        proposal.admission_job_id,
        owner="work-board-proposal:proposal-injected-retry",
        lease_seconds=120,
    )


@pytest.mark.asyncio
async def test_pre_contact_recovery_can_reclaim_one_expired_attempt_without_effects(async_db):
    """The bounded recovery gets one new fence, never a provider retry budget."""

    job_id = "work-board-proposal:pre-contact-reclaim"
    admitted = await durable_job_repository.admit_job(
        DurableJobSpec(
            identity=DurableJobIdentity(
                job_id=job_id,
                owner_kind="service",
                owner_principal_id="service:work-board",
                job_kind="work_board_proposal",
                capability_version="route-v1",
                idempotency_scope="work-board-proposal",
                idempotency_key="proposal-pre-contact-reclaim",
            ),
            inputs={"proposal_id": "proposal-pre-contact-reclaim"},
            session_id=OWNER.session_id,
            operator_session_id=OWNER.session_id,
                declared_authority={
                    "principal": "service:work-board",
                    "service_id": "service:work-board",
                    "approval_id": "proposal-pre-contact-reclaim",
                },
                max_attempts=2,
                service_id="service:work-board",
            )
    )
    await durable_job_repository.queue_job(job_id)
    first = await durable_job_repository.claim_job(job_id, owner="proposal-runner:first", lease_seconds=1)
    assert first["status"] == "running"
    assert first["attempt_count"] == 1

    recovered = await durable_job_repository.recover_stale_job(
        job_id,
        now=_now() + timedelta(seconds=2),
    )
    assert recovered["status"] == "blocked"
    assert recovered["effects"] == []
    queued = await durable_job_repository.transition_job(
        job_id,
        "queued",
        expected_state="blocked",
        expected_revision=recovered["revision"],
        reason="proposal_pre_contact_crash_recovered",
    )
    assert queued["status"] == "queued"
    second = await durable_job_repository.claim_job(job_id, owner="proposal-runner:second", lease_seconds=60)
    assert second["status"] == "running"
    assert second["attempt_count"] == 2
    assert second["lease"]["fencing_token"] > first["lease"]["fencing_token"]
    assert second["effects"] == []

    second_recovered = await durable_job_repository.recover_stale_job(
        job_id,
        now=_now() + timedelta(seconds=61),
    )
    assert second_recovered["status"] == "blocked"
    await durable_job_repository.transition_job(
        job_id,
        "queued",
        expected_state="blocked",
        expected_revision=second_recovered["revision"],
        reason="proposal_pre_contact_crash_recovered",
    )
    with pytest.raises(DurableJobTransitionError, match="attempt budget"):
        await durable_job_repository.claim_job(job_id, owner="proposal-runner:third", lease_seconds=60)


@pytest.mark.asyncio
async def test_worker_rejects_expired_authoritative_workflow_lease():
    task = WorkBoardTask(
        task_id="worker-expired-task",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        task_revision=2,
        status=WorkBoardStatus.running,
        executor_id="executor:followup",
    )
    attempt = WorkBoardAttempt(
        attempt_id="worker-expired-attempt",
        task_id=task.task_id,
        fencing_token=3,
        lease_owner=task.executor_id,
        lease_expires_at=_now() + timedelta(minutes=1),
        executor_id=task.executor_id,
        workflow_run_id="worker-expired-run",
    )

    class _Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class _Db:
        def __init__(self):
            self.results = iter((_Result(task), _Result(attempt)))

        async def execute(self, _statement):
            return next(self.results)

    class _ExpiredJobs:
        async def get_job(self, _job_id):
            return {
                "status": "running",
                "lease": {
                    "owner": "workflow-worker",
                    "fencing_token": 8,
                    "expires_at": (_now() - timedelta(seconds=1)).isoformat(),
                },
            }

    worker = WorkBoardWorkerTools(jobs=_ExpiredJobs())
    with pytest.raises(BoardError) as failure:
        await worker._bound(
            _Db(),
            WorkBoardWorkerRequest(
                task_id=task.task_id,
                attempt_id=attempt.attempt_id,
                expected_task_revision=task.task_revision,
                board_fencing_token=attempt.fencing_token,
                workflow_run_id=attempt.workflow_run_id,
                workflow_fencing_token=8,
            ),
        )
    assert failure.value.code == "stale_workflow_fence"


@pytest.mark.asyncio
async def test_worker_rejects_missing_board_attempt_lease():
    task = WorkBoardTask(
        task_id="worker-missing-board-lease-task",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        task_revision=2,
        status=WorkBoardStatus.running,
        executor_id="executor:followup",
    )
    attempt = WorkBoardAttempt(
        attempt_id="worker-missing-board-lease-attempt",
        task_id=task.task_id,
        fencing_token=3,
        lease_owner=task.executor_id,
        lease_expires_at=None,
        executor_id=task.executor_id,
        workflow_run_id="worker-missing-board-lease-run",
    )

    class _Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class _Db:
        def __init__(self):
            self.results = iter((_Result(task), _Result(attempt)))

        async def execute(self, _statement):
            return next(self.results)

    class _LiveJobs:
        async def get_job(self, _job_id):
            return {
                "status": "running",
                "lease": {
                    "owner": "workflow-worker",
                    "fencing_token": 8,
                    "expires_at": (_now() + timedelta(minutes=1)).isoformat(),
                },
            }

    worker = WorkBoardWorkerTools(jobs=_LiveJobs())
    with pytest.raises(BoardError) as failure:
        await worker._bound(
            _Db(),
            WorkBoardWorkerRequest(
                task_id=task.task_id,
                attempt_id=attempt.attempt_id,
                expected_task_revision=task.task_revision,
                board_fencing_token=attempt.fencing_token,
                workflow_run_id=attempt.workflow_run_id,
                workflow_fencing_token=8,
            ),
        )
    assert failure.value.code == "stale_fence"


def test_handoff_receipts_omit_destination_paths_and_hash_effect_ids():
    refs = review_service._handoff_receipt_refs(
        [
            {
                "artifact_id": "artifact-1",
                "file_path": "workspace/output.json",
                "target_path": "workspace/output.json",
                "effect_id": "private-effect-id",
                "status": "succeeded",
            }
        ]
    )
    assert refs == [
        {
            "artifact_id": "artifact-1",
            "effect_id_digest": hashlib.sha256(b"private-effect-id").hexdigest()[:16],
            "status": "succeeded",
        }
    ]


@pytest.mark.asyncio
async def test_legacy_duplicate_proposal_key_returns_reconciliation_conflict():
    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return self

        def all(self):
            return list(self._rows)

    class _Db:
        async def execute(self, _statement):
            return _Result([object(), object()])

    with pytest.raises(BoardError) as failure:
        await triage_service._find_idempotent_proposal(
            _Db(),
            owner=OWNER,
            task_id="legacy-parent",
            parent_revision=4,
            kind="specify",
            idempotency_key="legacy-key",
        )
    assert failure.value.code == "proposal_idempotency_reconciliation_required"


@pytest.mark.asyncio
async def test_model_proposal_identifier_scalars_reject_vault_secrets(monkeypatch):
    async def secrets():
        return [("openrouter", "sk-live-followup-secret")]

    monkeypatch.setattr(
        "src.vault.redaction.vault_repository.list_secret_values",
        secrets,
    )
    parent = WorkBoardTask(
        task_id="m4-redaction-identifiers-parent",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        idempotency_key="m4-redaction-identifiers-parent-key",
        title="Redaction parent",
        goal_id="goal-followup",
        goal_revision=1,
    )
    base = {
        "task_id": "child-safe",
        "title": "Safe title",
        "body": "Safe body",
        "capability_id": "workflow.goal-snapshot-to-file",
        "executor_id": triage_service.registered_executor_id("workflow.goal-snapshot-to-file"),
        "typed_input_ref": "input-ref",
        "typed_input_digest": "a" * 64,
    }
    for field in ("task_id", "capability_id", "executor_id", "typed_input_ref"):
        candidate = dict(base)
        candidate[field] = "sk-live-followup-secret"
        with pytest.raises(BoardError) as failure:
            await triage_service._normalise_tasks(
                {"proposed_tasks": [candidate]},
                kind="specify",
                parent=parent,
            )
        assert failure.value.code == "invalid_model_output"


@pytest.mark.asyncio
async def test_proposal_acceptance_requires_exact_live_capability_authority_preview(monkeypatch):
    from src.work_board.dispatcher import _dispatcher

    parent = WorkBoardTask(
        task_id="authority-preview-parent",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        goal_id="goal-authority-preview",
        goal_revision=4,
        title="Authority preview parent",
        idempotency_key="authority-preview-parent-key",
    )
    capability_id = "workflow.goal-snapshot-to-file"
    task = {
        "task_id": "authority-preview-child",
        "title": "Read back the bounded artifact",
        "body": "Create one artifact and verify its readback.",
        "capability_id": capability_id,
        "capability_version": "1",
        "typed_input_ref": "workspace-json:inputs/authority-preview.json",
        "typed_input_digest": "a" * 64,
        "executor_id": triage_service.registered_executor_id(capability_id),
    }
    monkeypatch.setattr(_dispatcher, "_current_readiness", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(_dispatcher, "_effective_runtime", AsyncMock(return_value=240))
    task["authority"] = await triage_service._proposal_task_authority_summary(parent, task)

    assert "Capability-specific authority requirements:" in task["authority"]
    assert "configured success criterion, verifier, and evidence" in task["authority"]
    assert "Current provider-free preflight: READY" in task["authority"]
    assert "240s effective goal/job runtime" in task["authority"]
    assert "Accepting creates Todo only" in task["authority"]
    await triage_service._validate_proposal_authority_preview([task], parent=parent)

    monkeypatch.setattr(
        _dispatcher,
        "_current_readiness",
        AsyncMock(return_value=("workflow_not_loaded_or_disabled", "The workflow is unavailable")),
    )
    with pytest.raises(BoardError) as stale:
        await triage_service._validate_proposal_authority_preview([task], parent=parent)
    assert stale.value.code == "proposal_authority_preview_missing"

    task["authority"] = "model says all writes are approved"
    with pytest.raises(BoardError) as missing:
        await triage_service._validate_proposal_authority_preview(
            [{**task, "authority": "model says all writes are approved"}],
            parent=parent,
        )
    assert missing.value.code == "proposal_authority_preview_missing"


@pytest.mark.asyncio
async def test_proposal_authority_preview_is_specific_to_registered_capability(monkeypatch):
    from src.work_board.dispatcher import _dispatcher

    parent = WorkBoardTask(
        task_id="authority-preview-github-parent",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        goal_id="goal-authority-preview",
        goal_revision=4,
        title="Authority preview parent",
        idempotency_key="authority-preview-github-parent-key",
    )
    item = {
        "task_id": "authority-preview-github-child",
        "title": "Write one approved GitHub update",
        "body": "Apply the fixed update and independently read it back.",
        "capability_id": "work.github-followthrough.v1",
        "capability_version": "1",
        "typed_input_ref": "workspace-json:inputs/github.json",
        "typed_input_digest": "b" * 64,
        "executor_id": triage_service.registered_executor_id("work.github-followthrough.v1"),
    }
    monkeypatch.setattr(
        _dispatcher,
        "_current_readiness",
        AsyncMock(return_value=("credential_not_configured", "The GitHub credential is not configured")),
    )
    monkeypatch.setattr(_dispatcher, "_effective_runtime", AsyncMock(return_value=300))
    summary = await triage_service._proposal_task_authority_summary(parent, item)
    assert "Active owner GitHub connection with configured credential and matching revision" in summary
    assert "external-mutation grant" in summary
    assert "exact destination approval" in summary
    assert "Current provider-free preflight: BLOCKED code=credential_not_configured" in summary
    assert "Accepting creates Todo only" in summary


async def _running_worker_fixture(db):
    db.add(
        Goal(
            id="goal-followup",
            title="Follow-up goal",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            revision=1,
            status="active",
        )
    )
    await db.flush()
    task = WorkBoardTask(
        task_id="m4-worker-review",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        origin_session_id=OWNER.session_id,
        goal_id="goal-followup",
        goal_revision=1,
        title="Worker review",
        idempotency_key="m4-worker-review-key",
        capability_id="workflow.goal-snapshot-to-file",
        executor_id="executor:followup",
        status=WorkBoardStatus.running,
        requires_review=True,
        reviewer_id=OWNER.principal_id,
        task_revision=4,
    )
    attempt = WorkBoardAttempt(
        attempt_id="m4-worker-attempt",
        task_id=task.task_id,
        workflow_run_id="m4-worker-run",
        task_revision_at_claim=4,
        lease_owner=task.executor_id,
        lease_expires_at=_now() + timedelta(minutes=5),
        fencing_token=7,
        executor_id=task.executor_id,
        started_at=_now(),
        receipt_refs_json=json.dumps(
            [
                {
                    "artifact_id": "artifact-followup",
                    "workflow_run_id": "m4-worker-run",
                    "status": "running",
                }
            ]
        ),
    )
    db.add(task)
    await db.flush()
    db.add(attempt)
    await db.flush()
    return task, attempt


class _RunningJobs:
    def __init__(self) -> None:
        self.status = "running"

    async def get_job(self, _job_id: str):
        return {
            "job_id": "m4-worker-run",
            "status": self.status,
            "revision": 8,
            "lease": {
                "owner": "workflow-worker",
                "fencing_token": 11,
                "expires_at": (_now() + timedelta(minutes=5)).isoformat(),
            },
            "effects": [],
        }


@pytest.mark.asyncio
async def test_worker_review_intent_is_active_fenced_and_dispatcher_projected(async_db):
    async with async_db() as db:
        task, attempt = await _running_worker_fixture(db)

    jobs = _RunningJobs()
    worker = WorkBoardWorkerTools(
        repository=WorkBoardRepository(),
        jobs=jobs,
        session_provider=async_db,
    )
    request = WorkBoardWorkerEvidence(
        **WorkBoardWorkerRequest(
            task_id=task.task_id,
            attempt_id=attempt.attempt_id,
            expected_task_revision=task.task_revision,
            board_fencing_token=attempt.fencing_token,
            workflow_run_id=attempt.workflow_run_id,
            workflow_fencing_token=11,
        ).model_dump(),
        # A live worker requests review as intent only.  It cannot relabel a
        # generic artifact as verification evidence before readback exists.
        evidence_refs=[],
    )

    receipt = await worker.request_review(request)
    assert receipt["status"] == "review_requested"
    assert receipt["projection"] == "dispatcher_verification_required"

    async with async_db() as db:
        stored_task = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task.task_id))
        ).scalar_one()
        intent = (
            await db.execute(
                select(WorkBoardReviewIntent).where(
                    WorkBoardReviewIntent.task_id == task.task_id,
                    WorkBoardReviewIntent.attempt_id == attempt.attempt_id,
                )
            )
        ).scalar_one()
        assert stored_task.status is WorkBoardStatus.running
        assert stored_task.task_revision == task.task_revision
        assert intent.fencing_token == attempt.fencing_token
        assert intent.task_revision == task.task_revision
        assert intent.status == "pending"

    digest = hashlib.sha256(b"verified-worker-artifact").hexdigest()
    dispatcher = WorkBoardDispatcher(
        repository=WorkBoardRepository(),
        jobs=jobs,
        session_provider=async_db,
    )
    await dispatcher._project(
        task,
        attempt,
        board_revision=task.task_revision,
        status=WorkBoardStatus.review,
        outcome="verified",
        proof={
            "source": "workflow_run",
            "status": "succeeded",
            "verified": True,
            "workflow_run_id": attempt.workflow_run_id,
            "content_sha256": digest,
            "readback_id": "artifact-followup",
            "verified_at": _now().isoformat(),
        },
        result_refs=[
            {
                "artifact_id": "artifact-followup",
                "workflow_run_id": attempt.workflow_run_id,
                "status": "succeeded",
                "verified": True,
                "content_sha256": digest,
            }
        ],
        lease_owner=attempt.lease_owner,
    )

    async with async_db() as db:
        stored_task = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task.task_id))
        ).scalar_one()
        intent = (
            await db.execute(
                select(WorkBoardReviewIntent).where(
                    WorkBoardReviewIntent.task_id == task.task_id,
                    WorkBoardReviewIntent.attempt_id == attempt.attempt_id,
                )
            )
        ).scalar_one()
        assert stored_task.status is WorkBoardStatus.review
        assert intent.status == "projected"


@pytest.mark.asyncio
async def test_worker_review_stale_fence_and_duplicate_are_bounded(async_db):
    async with async_db() as db:
        task, attempt = await _running_worker_fixture(db)
    jobs = _RunningJobs()
    worker = WorkBoardWorkerTools(repository=WorkBoardRepository(), jobs=jobs, session_provider=async_db)
    base = WorkBoardWorkerRequest(
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        expected_task_revision=task.task_revision,
        board_fencing_token=attempt.fencing_token,
        workflow_run_id=attempt.workflow_run_id,
        workflow_fencing_token=11,
    )
    with pytest.raises(BoardError) as stale:
        await worker.request_review(
            WorkBoardWorkerEvidence(
                **base.model_copy(update={"board_fencing_token": attempt.fencing_token - 1}).model_dump(),
                evidence_refs=[],
            )
        )
    assert stale.value.code == "stale_fence"

    first = await worker.request_review(
        WorkBoardWorkerEvidence(**base.model_dump(), evidence_refs=[])
    )
    replay = await worker.request_review(
        WorkBoardWorkerEvidence(**base.model_dump(), evidence_refs=[])
    )
    assert first["idempotent_replay"] is False
    assert replay["idempotent_replay"] is True
    async with async_db() as db:
        count = await db.scalar(
            select(func.count(WorkBoardReviewIntent.intent_id)).where(
                WorkBoardReviewIntent.task_id == task.task_id,
                WorkBoardReviewIntent.attempt_id == attempt.attempt_id,
            )
        )
        assert count == 1


@pytest.mark.asyncio
async def test_pre_contact_proposal_binding_cannot_change_under_same_key(async_db, monkeypatch):
    async with async_db() as db:
        task = WorkBoardTask(
            task_id="m4-proposal-binding",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id="goal-followup",
            goal_revision=1,
            title="Proposal binding",
            idempotency_key="m4-proposal-binding-task",
            status=WorkBoardStatus.triage,
            task_revision=1,
        )
        db.add(task)
        await db.flush()

    calls = iter(["missing", "available"])

    def route_binding():
        current = next(calls)
        if current == "missing":
            raise BoardError("openrouter_route_unavailable", "route unavailable", status_code=409)
        return "strategist_agent", "new-route-contract"

    monkeypatch.setattr(triage_service, "_route_binding", route_binding)
    monkeypatch.setattr(
        triage_service.durable_job_repository,
        "get_job",
        AsyncMock(return_value=None),
    )
    admit = AsyncMock()
    monkeypatch.setattr(triage_service, "_admit_proposal_job", admit)

    first = await triage_service.create_proposal(
        OWNER,
        task.task_id,
        kind="specify",
        request=WorkBoardProposalRequest(expected_revision=1, idempotency_key="same-key"),
        operator=SimpleNamespace(),
    )
    assert first["status"] == "blocked"

    with pytest.raises(BoardError) as conflict:
        await triage_service.create_proposal(
            OWNER,
            task.task_id,
            kind="specify",
            request=WorkBoardProposalRequest(expected_revision=1, idempotency_key="same-key"),
            operator=SimpleNamespace(),
        )
    assert conflict.value.code == "proposal_binding_conflict"
    admit.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_proposal_text_is_redacted_before_persistence(async_db, monkeypatch):
    async def secrets():
        return [("openrouter", "sk-live-followup-secret")]

    monkeypatch.setattr(
        "src.vault.redaction.vault_repository.list_secret_values",
        secrets,
    )
    parent = WorkBoardTask(
        task_id="m4-redaction-parent",
        owner_principal_id=OWNER.principal_id,
        owner_session_id=OWNER.session_id,
        idempotency_key="m4-redaction-parent-key",
        title="Redaction parent",
        goal_id="goal-followup",
        goal_revision=1,
    )
    tasks, _links, _cost = await triage_service._normalise_tasks(
        {
            "proposed_tasks": [
                {
                    "task_id": "child-redacted",
                    "title": "Use sk-live-followup-secret",
                    "body": "Credential sk-live-followup-secret must never persist",
                    "capability_id": "workflow.goal-snapshot-to-file",
                    "executor_id": triage_service.registered_executor_id("workflow.goal-snapshot-to-file"),
                    "typed_input_ref": "input-ref",
                    "typed_input_digest": "a" * 64,
                    "authority": "sk-live-followup-secret",
                }
            ]
        },
        kind="specify",
        parent=parent,
    )
    encoded = json.dumps(tasks)
    assert "sk-live-followup-secret" not in encoded


@pytest.mark.asyncio
async def test_review_cannot_be_generic_blocked_and_lose_expiry(async_db):
    from src.work_board import review as review_service

    async with async_db() as db:
        task = WorkBoardTask(
            task_id="m4-review-block",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id="goal-followup",
            goal_revision=1,
            title="Review block",
            idempotency_key="m4-review-block-key",
            status=WorkBoardStatus.review,
            requires_review=True,
            reviewer_id=OWNER.principal_id,
            review_expires_at=_now() + timedelta(days=1),
        )
        db.add(task)
        await db.flush()
        with pytest.raises(BoardError) as failure:
            await review_service.block_task(
                db,
                OWNER,
                task.task_id,
                expected_revision=task.task_revision,
                block_kind="operator",
                reason="operator pause",
            )
        assert failure.value.code == "review_typed_recovery_required"


@pytest.mark.asyncio
async def test_stale_verified_handoff_is_rejected_after_newer_failed_attempt(async_db):
    async with async_db() as db:
        goal = Goal(
            id="goal-stale-handoff",
            title="Stale handoff goal",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            revision=1,
            status="active",
        )
        parent = WorkBoardTask(
            task_id="stale-handoff-parent",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id=goal.id,
            goal_revision=1,
            title="Parent",
            idempotency_key="stale-handoff-parent-key",
            status=WorkBoardStatus.done,
            task_revision=4,
        )
        child = WorkBoardTask(
            task_id="stale-handoff-child",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id=goal.id,
            goal_revision=1,
            title="Child",
            idempotency_key="stale-handoff-child-key",
            status=WorkBoardStatus.todo,
            task_revision=1,
        )
        child_backfill = WorkBoardTask(
            task_id="stale-handoff-child-backfill",
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            origin_session_id=OWNER.session_id,
            goal_id=goal.id,
            goal_revision=1,
            title="Backfill child",
            idempotency_key="stale-handoff-child-backfill-key",
            status=WorkBoardStatus.todo,
            task_revision=1,
        )
        db.add_all([goal, parent, child, child_backfill])
        await db.flush()

        old_run = WorkflowRunState(
            run_identity="stale-handoff-old-run",
            root_run_identity="stale-handoff-old-run",
            workflow_name="stale-handoff",
            operator_session_id=OWNER.session_id,
            status="succeeded",
            owner_kind="user",
            owner_principal_id=OWNER.principal_id,
            goal_id=goal.id,
            goal_revision=1,
        )
        old_attempt = WorkBoardAttempt(
            attempt_id="stale-handoff-old-attempt",
            task_id=parent.task_id,
            workflow_run_id=old_run.run_identity,
            task_revision_at_claim=3,
            executor_id="executor:old",
            fencing_token=1,
            started_at=_now() - timedelta(minutes=2),
            ended_at=_now() - timedelta(minutes=1),
            outcome="succeeded",
            receipt_refs_json=json.dumps([{
                "workflow_run_id": old_run.run_identity,
                "receipt_kind": "readback",
                "effect_type": "readback",
                "readback_id": "readback-old",
                "content_sha256": "a" * 64,
                "status": "succeeded",
                "verified": True,
                "verified_at": (_now() - timedelta(minutes=1)).isoformat(),
            }]),
            created_at=_now() - timedelta(minutes=2),
        )
        new_run = WorkflowRunState(
            run_identity="stale-handoff-new-run",
            root_run_identity="stale-handoff-new-run",
            workflow_name="stale-handoff",
            operator_session_id=OWNER.session_id,
            status="failed",
            owner_kind="user",
            owner_principal_id=OWNER.principal_id,
            goal_id=goal.id,
            goal_revision=1,
        )
        new_attempt = WorkBoardAttempt(
            attempt_id="stale-handoff-new-attempt",
            task_id=parent.task_id,
            workflow_run_id=new_run.run_identity,
            task_revision_at_claim=4,
            executor_id="executor:new",
            fencing_token=2,
            started_at=_now() - timedelta(seconds=30),
            ended_at=_now() - timedelta(seconds=1),
            outcome="failed",
            receipt_refs_json=json.dumps([{
                "workflow_run_id": new_run.run_identity,
                "status": "failed",
                "verified": False,
            }]),
            created_at=_now() - timedelta(seconds=30),
        )
        db.add_all([old_run, old_attempt, new_run, new_attempt])
        await db.flush()
        old_link = WorkBoardLink(
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            parent_task_id=parent.task_id,
            child_task_id=child.task_id,
        )
        db.add(old_link)
        await db.flush()
        with pytest.raises(BoardError) as materialize_error:
            await review_service.materialize_handoff_for_link(db, OWNER, parent, child, old_link)
        assert materialize_error.value.code == "handoff_materialization_required"

        old_proof = await review_service._verified_workflow_readback(db, parent, old_attempt)
        assert old_proof is not None
        old_handoff = await review_service._persist_one_handoff(
            db,
            OWNER,
            parent,
            child,
            old_attempt,
            proof=old_proof,
            link=old_link,
        )
        assert old_link.current_handoff_id == old_handoff.handoff_id
        assert not await review_service.current_handoff_is_verified(
            db,
            OWNER,
            parent,
            child,
            old_link,
        )

        backfill_link = WorkBoardLink(
            owner_principal_id=OWNER.principal_id,
            owner_session_id=OWNER.session_id,
            parent_task_id=parent.task_id,
            child_task_id=child_backfill.task_id,
        )
        db.add(backfill_link)
        await db.flush()
        assert await review_service.backfill_verified_handoffs(db) == 0
        assert child_backfill.status is WorkBoardStatus.blocked
        assert child_backfill.block_reason == review_service._HANDOFF_RECONCILIATION_REASON
