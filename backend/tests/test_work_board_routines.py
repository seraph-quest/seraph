"""Focused M6 board-to-routine binding and fresh invocation receipts."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import yaml
from pydantic import ValidationError
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import select

from config.settings import settings
from src.db.models import (
    ApprovalRequest,
    GuardianRoutine,
    GuardianRoutineVersion,
    ProcedureV2Binding,
    GuardianDecisionPacket,
    Goal,
    SQLModel,
    WorkBoardAttempt,
    WorkBoardLink,
    WorkBoardRoutineBinding,
    WorkBoardEvent,
    WorkBoardTask,
    WorkBoardStatus,
    Session,
)
from src.db import engine as db_engine
import src.approval.repository as approval_repository_module
from src.extensions.capability_pack import CapabilityPackLifecycle, parse_capability_pack_manifest
from src.extensions.github_followthrough import _operation_id
from src.workflows.routines import (
    RoutineError,
    RoutineFromBoardCreateRequest,
    RoutineFromBoardPreviewRequest,
    RoutineFromRunRequest,
    RoutineInvokeRequest,
    RoutinePackageActivationRequest,
    RoutinePackageDecisionRequest,
    RoutineRollbackRequest,
    RoutineService,
    _preview_bucket,
    _bounded_runtime_seconds,
    _remaining_runtime_seconds,
    _dump,
    ROUTINE_INSTALL_TOOL,
    ROUTINE_INVOKE_TOOL,
    _safe_routine_provenance,
    _sha,
    _routine_pack_id,
)
import src.workflows.routines as routines_module
from src.work_board.repository import WorkBoardRepository


OWNER = "operator:m6"
SESSION = "session:m6"


@pytest_asyncio.fixture
async def m6_db(monkeypatch, tmp_path):
    """Create only the M6 persistence subset; full metadata is a slow control."""

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'm6.db'}",
        connect_args={"check_same_thread": False},
        pool_size=5,
        max_overflow=5,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    tables = [
        Session.__table__,
        ApprovalRequest.__table__,
        Goal.__table__,
        GuardianDecisionPacket.__table__,
        GuardianRoutine.__table__,
        GuardianRoutineVersion.__table__,
        ProcedureV2Binding.__table__,
        WorkBoardRoutineBinding.__table__,
        WorkBoardTask.__table__,
        WorkBoardAttempt.__table__,
        WorkBoardLink.__table__,
        WorkBoardEvent.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: SQLModel.metadata.create_all(sync, tables=tables))
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _get_session():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr(db_engine, "get_session", _get_session)
    monkeypatch.setattr(approval_repository_module, "get_session", _get_session)
    yield _get_session
    await engine.dispose()


@pytest.mark.asyncio
async def test_v2_routine_read_and_list_include_install_authority_metadata(m6_db, monkeypatch):
    """The live detail/list projection must serialize v2 approval metadata."""

    routine_id = "0123456789abcdef0123456789abcdef"
    version_id = "fedcba9876543210fedcba9876543210"
    install_job_id = f"routine-install:{routine_id}:v1"
    provenance = {
        "schema_version": 2,
        "template_id": "selected-meeting-prep",
        "plan_digest": "a" * 64,
        "source_proof_digest": "b" * 64,
        "source_refs": [],
        "parameter_schema": [],
    }
    async with m6_db() as db:
        db.add(Session(id=SESSION, owner_principal_id=OWNER))
        await db.flush()
        db.add(
            GuardianRoutine(
                id=routine_id,
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                name="Prepared v2 routine",
                state="prepared",
                revision=3,
                current_version=1,
            )
        )
        db.add(
            GuardianRoutineVersion(
                id=version_id,
                routine_id=routine_id,
                version=1,
                source_provenance_json=json.dumps(provenance, sort_keys=True),
                installed_package_digest="c" * 64,
            )
        )
        db.add(
            ProcedureV2Binding(
                binding_id="binding-v2-read",
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                idempotency_key="idempotency-v2-read",
                request_digest="d" * 64,
                deterministic_routine_id=routine_id,
                routine_name="Prepared v2 routine",
                template_id="selected-meeting-prep",
                version_id=version_id,
                preview_digest="e" * 64,
                preview_expires_at=datetime(2026, 10, 1, 12, 0),
                state="prepared",
                revision=2,
            )
        )
        db.add(
            ApprovalRequest(
                id="approval-v2",
                session_id=SESSION,
                owner_principal_id=OWNER,
                operator_session_id=SESSION,
                tool_name=ROUTINE_INSTALL_TOOL,
                status="pending",
                fingerprint="fingerprint-v2",
                summary="Install reviewed v2 routine",
                expires_at=datetime(2026, 9, 30, 12, 0),
                details_json=json.dumps(
                    {
                        "approval_id": "approval-v2",
                        "durable_approval_id": "approval-v2",
                        "durable_job_id": install_job_id,
                        "durable_owner_kind": "user",
                        "durable_owner_principal_id": OWNER,
                        "approval_owner_operator_session_id": SESSION,
                    },
                    sort_keys=True,
                ),
            )
        )

    async def get_job(job_id: str):
        assert job_id == install_job_id
        return {
            "job_id": install_job_id,
            "declared_authority": {
                "approval_id": " approval-v2 ",
                "principal": OWNER,
                "owner_kind": "user",
                "session_id": SESSION,
                "routine_id": routine_id,
                "routine_version": 1,
            },
        }

    monkeypatch.setattr(routines_module.durable_job_repository, "get_job", get_job)
    service = RoutineService()
    monkeypatch.setattr(
        service,
        "_package_readback",
        lambda *_args: {"status": "prepared", "digest": "c" * 64, "review_id": "review-v2"},
    )

    detail = await service.read(
        routine_id,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    listed = await service.list(owner_principal_id=OWNER, owner_session_id=SESSION)

    for payload in (detail, listed[0]):
        assert payload["id"] == routine_id
        assert payload["versions"][0]["procedure_binding"] == {
            "binding_id": "binding-v2-read",
            "state": "prepared",
            "revision": 2,
            "preview_digest": "e" * 64,
            "preview_expires_at": "2026-10-01T12:00:00Z",
            "install_job_id": install_job_id,
            "approval_id": "approval-v2",
            "install_approval_status": "expired",
            "install_approval_expires_at": "2026-09-30T12:00:00Z",
            "install_recovery_action": "create_fresh_preview",
        }


def _resolved_journey() -> dict[str, object]:
    source_refs = {
        "source_task_id": "source-task",
        "source_attempt_id": "source-attempt",
        "source_task_revision": 2,
        "source_watch_job_id": "source-job",
        "source_packet_id": "packet-id",
        "action_task_id": "action-task",
        "action_attempt_id": "action-attempt",
        "action_task_revision": 3,
        "source_m3_job_id": "action-job",
        "goal_id": "goal-m6",
        "goal_revision": 4,
        "source_task_artifact_refs": [
            {"artifact_id": "source-artifact", "content_sha256": "a" * 64}
        ],
        "action_task_artifact_refs": [
            {"artifact_id": "action-artifact", "content_sha256": "b" * 64}
        ],
        "source_attempt_receipt_refs": [
            {
                "readback_id": "source-readback",
                "workflow_run_id": "source-job",
                "content_sha256": "c" * 64,
                "status": "succeeded",
            }
        ],
        "action_attempt_receipt_refs": [
            {
                "readback_id": "action-readback",
                "workflow_run_id": "action-job",
                "content_sha256": "d" * 64,
                "status": "succeeded",
            }
        ],
    }
    provenance = {
        "source_watch_id": "watch-id",
        "plan_revision": 5,
        "source_watch_job_id": "source-job",
        "source_packet_id": "packet-id",
        "source_m3_job_id": "action-job",
        "source_task_artifact_refs": source_refs["source_task_artifact_refs"],
        "action_task_artifact_refs": source_refs["action_task_artifact_refs"],
        "source_attempt_receipt_refs": source_refs["source_attempt_receipt_refs"],
        "action_attempt_receipt_refs": source_refs["action_attempt_receipt_refs"],
        **source_refs,
    }
    return {
        "request": SimpleNamespace(
            source_watch_job_id="source-job",
            source_packet_id="packet-id",
            source_m3_job_id="action-job",
            name="Reviewed procedure",
        ),
        "source_refs": source_refs,
        "provenance": provenance,
        "packet": SimpleNamespace(id="packet-id"),
    }


def _board_create_request(
    service: RoutineService,
    resolved: dict[str, object],
    *,
    idempotency_key: str,
) -> RoutineFromBoardCreateRequest:
    source_refs = resolved["source_refs"]
    assert isinstance(source_refs, dict)
    preview_request = RoutineFromBoardPreviewRequest(
        source_task_id=str(source_refs["source_task_id"]),
        action_task_id=str(source_refs["action_task_id"]),
        expected_source_revision=int(source_refs["source_task_revision"]),
        expected_action_revision=int(source_refs["action_task_revision"]),
        name="Reviewed procedure",
        idempotency_key=idempotency_key,
    )
    preview = service._board_preview_response(
        resolved,
        preview_request,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
        bucket=_preview_bucket(),
    )
    return RoutineFromBoardCreateRequest(
        **preview_request.model_dump(),
        preview_digest=str(preview["preview_digest"]),
    )


@pytest.mark.asyncio
async def test_resolve_board_journey_reads_owner_bound_attempt_packet_and_readback_bindings(
    m6_db,
    monkeypatch,
):
    """The live resolver binds every reusable procedure input to board evidence."""

    service = RoutineService()
    source_task_id = "resolver-source-task"
    action_task_id = "resolver-action-task"
    source_job_id = "resolver-source-job"
    action_job_id = "resolver-action-job"
    source_attempt_id = "resolver-source-attempt"
    action_attempt_id = "resolver-action-attempt"
    packet_id = "resolver-packet"
    source_artifact = {
        "artifact_id": "resolver-source-artifact",
        "artifact_type": "guardian_decision_dossier",
        "content_sha256": "a" * 64,
    }
    action_artifact = {"artifact_id": "resolver-action-artifact", "content_sha256": "b" * 64}
    source_receipt = {
        "readback_id": "resolver-source-readback",
        "workflow_run_id": source_job_id,
        "content_sha256": "c" * 64,
        "status": "succeeded",
    }
    action_receipt = {
        "readback_id": "resolver-action-readback",
        "workflow_run_id": action_job_id,
        "content_sha256": "d" * 64,
        "status": "succeeded",
    }
    async with m6_db() as db:
        db.add(
            WorkBoardTask(
                task_id=source_task_id,
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                origin_session_id=SESSION,
                goal_id="resolver-goal",
                goal_revision=4,
                title="Research",
                idempotency_scope="test",
                idempotency_key=source_task_id,
                capability_id="guardian.research-watch.v1",
                status=WorkBoardStatus.done,
                task_revision=2,
                artifact_refs_json=json.dumps([source_artifact]),
            )
        )
        db.add(
            WorkBoardTask(
                task_id=action_task_id,
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                origin_session_id=SESSION,
                goal_id="resolver-goal",
                goal_revision=4,
                title="Action",
                idempotency_scope="test",
                idempotency_key=action_task_id,
                capability_id="work.github-followthrough.v1",
                status=WorkBoardStatus.done,
                task_revision=3,
                artifact_refs_json=json.dumps([action_artifact]),
            )
        )
        await db.flush()
        db.add(
            WorkBoardLink(
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                parent_task_id=source_task_id,
                child_task_id=action_task_id,
            )
        )
        db.add(
            WorkBoardAttempt(
                attempt_id=source_attempt_id,
                task_id=source_task_id,
                workflow_run_id=source_job_id,
                task_revision_at_claim=2,
                ended_at=datetime.now(timezone.utc),
                receipt_refs_json=json.dumps([source_receipt]),
            )
        )
        db.add(
            WorkBoardAttempt(
                attempt_id=action_attempt_id,
                task_id=action_task_id,
                workflow_run_id=action_job_id,
                task_revision_at_claim=3,
                ended_at=datetime.now(timezone.utc),
                receipt_refs_json=json.dumps([action_receipt]),
            )
        )
        db.add(
            GuardianDecisionPacket(
                id=packet_id,
                source_watch_id="resolver-watch",
                watch_id="resolver-watch",
                goal_id="resolver-goal",
                goal_revision=4,
                plan_revision=5,
                run_identity=source_job_id,
                input_digest="e" * 64,
                criteria_digest="f" * 64,
                status="succeeded",
                verification_status="passed",
                memory_status="no_learning",
                dossier_artifact_id=source_artifact["artifact_id"],
                dossier_sha256=source_artifact["content_sha256"],
                task_artifact_id="resolver-source-task-artifact",
                task_sha256="9" * 64,
            )
        )

    jobs = {
        source_job_id: {
            "job_id": source_job_id,
            "run_identity": source_job_id,
            "status": "succeeded",
            "artifacts": [
                {
                    "artifact_id": source_artifact["artifact_id"],
                    "artifact_type": "guardian_decision_dossier",
                    "content_sha256": source_artifact["content_sha256"],
                    "exists": True,
                }
            ],
            "effects": [
                {
                    "effect_id": "source-readback-effect",
                    "receipt_kind": "readback",
                    "status": "succeeded",
                    "reconciled": True,
                    "readback_id": source_receipt["readback_id"],
                    "content_sha256": source_receipt["content_sha256"],
                    "details": {"verified": True},
                }
            ],
            "result": {"learning": "no_learning"},
        },
        action_job_id: {
            "job_id": action_job_id,
            "run_identity": action_job_id,
            "status": "succeeded",
            "artifacts": [
                {
                    "artifact_id": action_artifact["artifact_id"],
                    "artifact_type": "github_followthrough_result",
                    "content_sha256": action_artifact["content_sha256"],
                    "exists": True,
                }
            ],
            "effects": [
                {
                    "effect_id": "action-readback-effect",
                    "receipt_kind": "readback",
                    "status": "succeeded",
                    "reconciled": True,
                    "readback_id": action_receipt["readback_id"],
                    "content_sha256": action_receipt["content_sha256"],
                    "details": {"verified": True},
                }
            ],
            "result": {"learning": "no_learning"},
        },
    }

    async def get_job(job_id):
        return jobs.get(job_id)

    monkeypatch.setattr("src.workflows.routines.durable_job_repository.get_job", get_job)
    resolved = _resolved_journey()

    async def source_proof(**_kwargs):
        return resolved["provenance"], SimpleNamespace(id=packet_id)

    monkeypatch.setattr(service, "_source_proof", source_proof)
    request = RoutineFromBoardPreviewRequest(
        source_task_id=source_task_id,
        action_task_id=action_task_id,
        expected_source_revision=2,
        expected_action_revision=3,
        name="Resolver procedure",
        idempotency_key="resolver-preview",
    )
    result = await service._resolve_board_journey(
        request,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )

    refs = result["source_refs"]
    assert refs["source_task_id"] == source_task_id
    assert refs["action_task_id"] == action_task_id
    assert refs["source_attempt_id"] == source_attempt_id
    assert refs["action_attempt_id"] == action_attempt_id
    assert refs["source_packet_id"] == packet_id
    assert refs["source_task_artifact_refs"] == [
        {
            "artifact_id": source_artifact["artifact_id"],
            "artifact_type": "guardian_decision_dossier",
            "workflow_run_id": source_job_id,
            "status": "succeeded",
            "content_sha256": source_artifact["content_sha256"],
        }
    ]
    assert refs["action_task_artifact_refs"] == [
        {
            "artifact_id": action_artifact["artifact_id"],
            "artifact_type": "github_followthrough_result",
            "workflow_run_id": action_job_id,
            "status": "succeeded",
            "content_sha256": action_artifact["content_sha256"],
        }
    ]
    assert refs["source_attempt_receipt_refs"] == [
        {
            "readback_id": source_receipt["readback_id"],
            "workflow_run_id": source_job_id,
            "content_sha256": source_receipt["content_sha256"],
            "status": "succeeded",
            "verified": True,
            "readback_status": "verified",
            "verification_status": "passed",
        }
    ]
    assert refs["action_attempt_receipt_refs"] == [
        {
            "readback_id": action_receipt["readback_id"],
            "workflow_run_id": action_job_id,
            "content_sha256": action_receipt["content_sha256"],
            "status": "succeeded",
            "verified": True,
            "readback_status": "verified",
            "verification_status": "passed",
        }
    ]

    with pytest.raises(RoutineError, match="board_journey_task_not_owned"):
        await service._resolve_board_journey(
            request,
            owner_principal_id="operator:other",
            owner_session_id=SESSION,
        )


@pytest.mark.parametrize(
    ("mismatch", "expected_error"),
    [
        ("source_artifact_digest", "board_journey_task_artifact_binding_mismatch"),
        ("source_artifact_run", "board_journey_task_artifact_binding_mismatch"),
        ("action_artifact_id", "board_journey_task_artifact_binding_mismatch"),
        ("source_readback_digest", "board_journey_attempt_readback_binding_mismatch"),
        ("action_readback_id", "board_journey_attempt_readback_binding_mismatch"),
        ("action_readback_status", "board_journey_evidence_missing"),
        ("source_readback_run", "board_journey_evidence_missing"),
        ("canonical_artifact_digest", "board_journey_task_artifact_binding_mismatch"),
        ("canonical_artifact_run", "board_journey_task_artifact_binding_mismatch"),
        ("canonical_artifact_status", "board_journey_task_artifact_binding_mismatch"),
        ("canonical_readback_digest", "board_journey_attempt_readback_binding_mismatch"),
        ("canonical_readback_run", "board_journey_attempt_readback_binding_mismatch"),
        ("canonical_readback_status", "board_journey_runs_not_verified"),
    ],
)
@pytest.mark.asyncio
async def test_resolve_board_journey_rejects_artifact_and_readback_binding_mismatches(
    mismatch,
    expected_error,
    m6_db,
    monkeypatch,
):
    """Every reusable board reference must match the exact durable receipt."""

    service = RoutineService()
    source_task_id = "mismatch-source-task"
    action_task_id = "mismatch-action-task"
    source_job_id = "mismatch-source-job"
    action_job_id = "mismatch-action-job"
    source_attempt_id = "mismatch-source-attempt"
    action_attempt_id = "mismatch-action-attempt"
    packet_id = "mismatch-packet"
    source_artifact = {
        "artifact_id": "mismatch-source-artifact",
        "content_sha256": "a" * 64,
    }
    action_artifact = {
        "artifact_id": "mismatch-action-artifact",
        "content_sha256": "b" * 64,
    }
    source_receipt = {
        "readback_id": "mismatch-source-readback",
        "workflow_run_id": source_job_id,
        "content_sha256": "c" * 64,
        "status": "succeeded",
    }
    action_receipt = {
        "readback_id": "mismatch-action-readback",
        "workflow_run_id": action_job_id,
        "content_sha256": "d" * 64,
        "status": "succeeded",
    }
    if mismatch == "source_artifact_digest":
        source_artifact["content_sha256"] = "e" * 64
    elif mismatch == "source_artifact_run":
        source_artifact["workflow_run_id"] = "another-source-job"
    elif mismatch == "action_artifact_id":
        action_artifact["artifact_id"] = "missing-action-artifact"
    elif mismatch == "source_readback_digest":
        source_receipt["content_sha256"] = "e" * 64
    elif mismatch == "action_readback_id":
        action_receipt["readback_id"] = "missing-action-readback"
    elif mismatch == "action_readback_status":
        action_receipt["status"] = "blocked"
    elif mismatch == "source_readback_run":
        source_receipt["workflow_run_id"] = "another-source-job"

    canonical_source_digest = "a" * 64
    canonical_packet_run = source_job_id
    canonical_packet_status = "succeeded"
    if mismatch == "canonical_artifact_digest":
        canonical_source_digest = "f" * 64
    elif mismatch == "canonical_artifact_status":
        canonical_packet_status = "blocked"

    async with m6_db() as db:
        db.add_all(
            [
                WorkBoardTask(
                    task_id=source_task_id,
                    owner_principal_id=OWNER,
                    owner_session_id=SESSION,
                    origin_session_id=SESSION,
                    goal_id="mismatch-goal",
                    goal_revision=4,
                    title="Research",
                    idempotency_scope="test",
                    idempotency_key=source_task_id,
                    capability_id="guardian.research-watch.v1",
                    status=WorkBoardStatus.done,
                    task_revision=2,
                    artifact_refs_json=json.dumps([source_artifact]),
                ),
                WorkBoardTask(
                    task_id=action_task_id,
                    owner_principal_id=OWNER,
                    owner_session_id=SESSION,
                    origin_session_id=SESSION,
                    goal_id="mismatch-goal",
                    goal_revision=4,
                    title="Action",
                    idempotency_scope="test",
                    idempotency_key=action_task_id,
                    capability_id="work.github-followthrough.v1",
                    status=WorkBoardStatus.done,
                    task_revision=3,
                    artifact_refs_json=json.dumps([action_artifact]),
                ),
            ]
        )
        await db.flush()
        db.add_all(
            [
                WorkBoardLink(
                    owner_principal_id=OWNER,
                    owner_session_id=SESSION,
                    parent_task_id=source_task_id,
                    child_task_id=action_task_id,
                ),
                WorkBoardAttempt(
                    attempt_id=source_attempt_id,
                    task_id=source_task_id,
                    workflow_run_id=source_job_id,
                    task_revision_at_claim=2,
                    ended_at=datetime.now(timezone.utc),
                    receipt_refs_json=json.dumps([source_receipt]),
                ),
                WorkBoardAttempt(
                    attempt_id=action_attempt_id,
                    task_id=action_task_id,
                    workflow_run_id=action_job_id,
                    task_revision_at_claim=3,
                    ended_at=datetime.now(timezone.utc),
                    receipt_refs_json=json.dumps([action_receipt]),
                ),
                GuardianDecisionPacket(
                    id=packet_id,
                    source_watch_id="mismatch-watch",
                    watch_id="mismatch-watch",
                    goal_id="mismatch-goal",
                    goal_revision=4,
                    plan_revision=5,
                    run_identity=canonical_packet_run,
                    input_digest="e" * 64,
                    criteria_digest="f" * 64,
                    status=canonical_packet_status,
                    verification_status="passed",
                    memory_status="no_learning",
                    dossier_artifact_id=source_artifact["artifact_id"],
                    dossier_sha256=canonical_source_digest,
                    task_artifact_id="mismatch-source-task-artifact",
                    task_sha256="9" * 64,
                ),
            ]
        )

    def job_projection(job_id, artifact, receipt):
        return {
            "job_id": job_id,
            "run_identity": job_id,
            "status": "succeeded",
            "artifacts": [{**artifact, "artifact_type": "verified-output", "exists": True}],
            "effects": [
                {
                    "effect_id": f"{job_id}-effect",
                    "receipt_kind": "readback",
                    "status": "succeeded",
                    "reconciled": True,
                    "readback_id": receipt["readback_id"],
                    "content_sha256": "c" * 64 if job_id == source_job_id else "d" * 64,
                    "details": {"verified": True},
                }
            ],
            "result": {"learning": "no_learning"},
        }

    jobs = {
        source_job_id: job_projection(
            source_job_id,
            {"artifact_id": "mismatch-source-artifact", "content_sha256": "a" * 64},
            {"readback_id": "mismatch-source-readback"},
        ),
        action_job_id: job_projection(
            action_job_id,
            {"artifact_id": "mismatch-action-artifact", "content_sha256": "b" * 64},
            {"readback_id": "mismatch-action-readback"},
        ),
    }
    if mismatch == "canonical_artifact_run":
        jobs[source_job_id]["run_identity"] = "another-source-job"
    elif mismatch == "canonical_readback_digest":
        jobs[source_job_id]["effects"][0]["content_sha256"] = "f" * 64
    elif mismatch == "canonical_readback_run":
        jobs[source_job_id]["effects"][0]["workflow_run_id"] = "another-source-job"
    elif mismatch == "canonical_readback_status":
        jobs[source_job_id]["effects"][0]["status"] = "blocked"

    async def get_job(job_id):
        return jobs.get(job_id)

    monkeypatch.setattr("src.workflows.routines.durable_job_repository.get_job", get_job)
    request = RoutineFromBoardPreviewRequest(
        source_task_id=source_task_id,
        action_task_id=action_task_id,
        expected_source_revision=2,
        expected_action_revision=3,
        name="Mismatch procedure",
        idempotency_key=f"mismatch-{mismatch}",
    )

    with pytest.raises(RoutineError, match=expected_error):
        await service._resolve_board_journey(
            request,
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
        )


@pytest.mark.asyncio
async def test_from_board_idempotent_prepared_routine(m6_db, monkeypatch):
    service = RoutineService()
    owner = {"owner_principal_id": OWNER, "owner_session_id": SESSION}
    resolved = _resolved_journey()

    async def resolve(*_args, **_kwargs):
        resolve_calls.append(1)
        return resolved

    resolve_calls: list[int] = []
    monkeypatch.setattr(service, "_resolve_board_journey", resolve)
    preview_request = RoutineFromBoardPreviewRequest(
        source_task_id="source-task",
        action_task_id="action-task",
        expected_source_revision=2,
        expected_action_revision=3,
        name="Reviewed procedure",
        idempotency_key="binding-key",
    )
    preview = service._board_preview_response(
        resolved,
        preview_request,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
        bucket=_preview_bucket(),
    )
    # The helper is module-level; use the current preview window rather than
    # persisting anything from the preview request itself.
    if not preview["preview_digest"]:
        pytest.fail("preview digest was empty")
    request = RoutineFromBoardCreateRequest(
        **preview_request.model_dump(),
        preview_digest=preview["preview_digest"],
    )
    calls: list[str] = []

    async def from_run(*_args, **_kwargs):
        calls.append(str(_kwargs["routine_id"]))
        return {"install_job_id": f"routine-install:{_kwargs['routine_id']}:v1"}

    async def read_binding(binding, **_kwargs):
        return {
            "routine_id": binding.routine_id or binding.deterministic_routine_id,
            "state": "prepared",
            "status": "prepared",
            "revision": binding.revision,
            "version": 1,
            "install_job_id": binding.install_job_id or f"routine-install:{binding.deterministic_routine_id}:v1",
        }

    monkeypatch.setattr(service, "from_run", from_run)
    monkeypatch.setattr(service, "_read_board_binding", read_binding)
    first = await service.create_from_board(request, **owner)
    second = await service.create_from_board(request, **owner)

    assert first["routine_id"] == second["routine_id"]
    assert first["state"] == second["state"] == "prepared"
    assert len(calls) == 1
    assert len(resolve_calls) == 2
    assert calls[0]
    async with m6_db() as db:
        rows = list((await db.execute(select(WorkBoardRoutineBinding))).scalars().all())
    assert len(rows) == 1
    assert rows[0].state == "prepared"


@pytest.mark.asyncio
async def test_pending_binding_without_routine_blocks_without_rerunning_from_run(m6_db, monkeypatch):
    service = RoutineService()
    resolved = _resolved_journey()

    async def resolve(*_args, **_kwargs):
        return resolved

    monkeypatch.setattr(service, "_resolve_board_journey", resolve)
    request = _board_create_request(service, resolved, idempotency_key="restart-without-routine")
    routine_id = "0123456789abcdef0123456789abcdef"
    async with m6_db() as db:
        db.add(
            WorkBoardRoutineBinding(
                binding_id="binding-no-routine",
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                idempotency_key=request.idempotency_key,
                preview_digest=request.preview_digest,
                source_task_id="source-task",
                action_task_id="action-task",
                routine_name="Reviewed procedure",
                deterministic_routine_id=routine_id,
                state="pending",
            )
        )

    async def must_not_call_from_run(*_args, **_kwargs):
        pytest.fail("restart recovery must not call from_run without a verified routine/job")

    monkeypatch.setattr(service, "from_run", must_not_call_from_run)
    with pytest.raises(RoutineError, match="routine_binding_recovery_required"):
        await service.create_from_board(
            request,
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
        )

    async with m6_db() as db:
        row = (
            await db.execute(
                select(WorkBoardRoutineBinding).where(
                    WorkBoardRoutineBinding.binding_id == "binding-no-routine"
                )
            )
        ).scalars().one()
    assert row.state == "blocked"
    assert row.recovery_reason == "routine_not_found"


@pytest.mark.asyncio
async def test_pending_binding_links_verified_routine_and_install_job_after_restart(m6_db, monkeypatch):
    service = RoutineService()
    resolved = _resolved_journey()

    async def resolve(*_args, **_kwargs):
        return resolved

    monkeypatch.setattr(service, "_resolve_board_journey", resolve)
    request = _board_create_request(service, resolved, idempotency_key="restart-before-link")
    routine_id = "abcdefabcdefabcdefabcdefabcdefab"
    install_job_id = f"routine-install:{routine_id}:v1"
    provenance = {
        **resolved["provenance"],
        "preview_digest": request.preview_digest,
        "deterministic_routine_id": routine_id,
    }
    async with m6_db() as db:
        db.add(
            GuardianRoutine(
                id=routine_id,
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                name="Reviewed procedure",
                state="prepared",
                revision=1,
            )
        )
        db.add(
            GuardianRoutineVersion(
                routine_id=routine_id,
                version=1,
                source_provenance_json=json.dumps(provenance, sort_keys=True),
            )
        )
        db.add(
            WorkBoardRoutineBinding(
                binding_id="binding-before-link",
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                idempotency_key=request.idempotency_key,
                preview_digest=request.preview_digest,
                source_task_id="source-task",
                action_task_id="action-task",
                routine_name="Reviewed procedure",
                deterministic_routine_id=routine_id,
                state="pending",
            )
        )

    async def verified_install_job(_job_id: str):
        return {
            "job_id": install_job_id,
            "job_kind": "routine_install",
            "status": "succeeded",
            "owner": {"principal_id": OWNER},
            "operator_session_id": SESSION,
            "declared_authority": {
                "routine_id": routine_id,
                "routine_version": 1,
                "source_packet_id": "packet-id",
                "source_m3_job_id": "action-job",
            },
        }

    monkeypatch.setattr(
        "src.workflows.routines.durable_job_repository.get_job",
        verified_install_job,
    )

    async def must_not_call_from_run(*_args, **_kwargs):
        pytest.fail("verified restart recovery must link existing rows, not call from_run")

    monkeypatch.setattr(service, "from_run", must_not_call_from_run)
    result = await service.create_from_board(
        request,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    assert result["state"] == "prepared"
    assert result["routine_id"] == routine_id
    async with m6_db() as db:
        row = (
            await db.execute(
                select(WorkBoardRoutineBinding).where(
                    WorkBoardRoutineBinding.binding_id == "binding-before-link"
                )
            )
        ).scalars().one()
    assert row.state == "prepared"
    assert row.routine_id == routine_id
    assert row.install_job_id == install_job_id
    assert row.revision == 2


@pytest.mark.asyncio
async def test_pending_binding_with_mismatched_install_job_blocks_recovery(m6_db, monkeypatch):
    service = RoutineService()
    resolved = _resolved_journey()

    async def resolve(*_args, **_kwargs):
        return resolved

    monkeypatch.setattr(service, "_resolve_board_journey", resolve)
    request = _board_create_request(service, resolved, idempotency_key="restart-job-mismatch")
    routine_id = "fedcbafedcbafedcbafedcbafedcbafe"
    install_job_id = f"routine-install:{routine_id}:v1"
    provenance = {
        **resolved["provenance"],
        "preview_digest": request.preview_digest,
        "deterministic_routine_id": routine_id,
    }
    async with m6_db() as db:
        db.add(
            GuardianRoutine(
                id=routine_id,
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                name="Reviewed procedure",
                state="prepared",
                revision=1,
            )
        )
        db.add(
            GuardianRoutineVersion(
                routine_id=routine_id,
                version=1,
                source_provenance_json=json.dumps(provenance, sort_keys=True),
            )
        )
        db.add(
            WorkBoardRoutineBinding(
                binding_id="binding-job-mismatch",
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                idempotency_key=request.idempotency_key,
                preview_digest=request.preview_digest,
                source_task_id="source-task",
                action_task_id="action-task",
                routine_name="Reviewed procedure",
                deterministic_routine_id=routine_id,
                state="pending",
            )
        )

    async def mismatched_install_job(_job_id: str):
        return {
            "job_id": install_job_id,
            "job_kind": "routine_install",
            "status": "succeeded",
            "owner": {"principal_id": OWNER},
            "operator_session_id": SESSION,
            "declared_authority": {
                "routine_id": "a-different-routine",
                "routine_version": 1,
                "source_packet_id": "packet-id",
                "source_m3_job_id": "action-job",
            },
        }

    monkeypatch.setattr(
        "src.workflows.routines.durable_job_repository.get_job",
        mismatched_install_job,
    )
    with pytest.raises(RoutineError, match="routine_binding_recovery_required"):
        await service.create_from_board(
            request,
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
        )

    async with m6_db() as db:
        row = (
            await db.execute(
                select(WorkBoardRoutineBinding).where(
                    WorkBoardRoutineBinding.binding_id == "binding-job-mismatch"
                )
            )
        ).scalars().one()
    assert row.state == "blocked"
    assert row.recovery_reason == "routine_binding_install_unknown"


@pytest.mark.asyncio
async def test_prepared_binding_readback_exposes_bound_install_approval(m6_db, monkeypatch):
    service = RoutineService()
    routine_id = "0123456789abcdef0123456789abcdef"
    preview_digest = "a" * 64
    provenance = {
        "preview_digest": preview_digest,
        "deterministic_routine_id": routine_id,
        "source_task_id": "source-task",
        "action_task_id": "action-task",
        "source_watch_job_id": "source-job",
        "source_packet_id": "packet-id",
        "source_m3_job_id": "action-job",
        "source_attempt_id": "source-attempt",
        "action_attempt_id": "action-attempt",
        "source_task_artifact_refs": [
            {"artifact_id": "source-artifact", "content_sha256": "a" * 64}
        ],
        "action_task_artifact_refs": [
            {"artifact_id": "action-artifact", "content_sha256": "b" * 64}
        ],
        "source_attempt_receipt_refs": [
            {
                "readback_id": "source-readback",
                "workflow_run_id": "source-job",
                "content_sha256": "c" * 64,
                "status": "succeeded",
            }
        ],
        "action_attempt_receipt_refs": [
            {
                "readback_id": "action-readback",
                "workflow_run_id": "action-job",
                "content_sha256": "d" * 64,
                "status": "succeeded",
            }
        ],
    }
    async with m6_db() as db:
        db.add(
            GuardianRoutine(
                id=routine_id,
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                name="Reviewed procedure",
                state="prepared",
                revision=1,
            )
        )
        db.add(
            GuardianRoutineVersion(
                routine_id=routine_id,
                version=1,
                source_provenance_json=json.dumps(provenance, sort_keys=True),
            )
        )
        db.add(
            WorkBoardRoutineBinding(
                binding_id="binding-approval",
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                idempotency_key="binding-key-approval",
                preview_digest=preview_digest,
                source_task_id="source-task",
                action_task_id="action-task",
                routine_name="Reviewed procedure",
                deterministic_routine_id=routine_id,
                routine_id=routine_id,
                install_job_id=f"routine-install:{routine_id}:v1",
                state="prepared",
            )
        )

    async def install_job(_job_id: str):
        return {
            "job_id": f"routine-install:{routine_id}:v1",
            "job_kind": "routine_install",
            "status": "awaiting_approval",
            "owner": {"principal_id": OWNER},
            "operator_session_id": SESSION,
            "declared_authority": {
                "routine_id": routine_id,
                "routine_version": 1,
                "source_packet_id": "packet-id",
                "source_m3_job_id": "action-job",
                "approval_id": "approval-bound-to-install-job",
            },
        }

    monkeypatch.setattr(
        "src.workflows.routines.durable_job_repository.get_job",
        install_job,
    )
    async with m6_db() as db:
        binding = (
            await db.execute(
                select(WorkBoardRoutineBinding).where(
                    WorkBoardRoutineBinding.binding_id == "binding-approval"
                )
            )
        ).scalars().one()
        db.expunge(binding)
    result = await service._read_board_binding(
        binding,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    assert result["approval_id"] == "approval-bound-to-install-job"


@pytest.mark.asyncio
async def test_second_invocation_uses_fresh_authority(m6_db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    service = RoutineService()
    async with m6_db() as db:
        db.add(
            Goal(
                id="goal-m6",
                title="M6 goal",
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                revision=4,
                status="active",
            )
        )
        db.add(
            Goal(
                id="goal-m6-second",
                title="M6 second goal",
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                revision=8,
                status="active",
            )
        )
        db.add(
            GuardianRoutine(
                id="0123456789abcdef0123456789abcdef",
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                name="Reviewed procedure",
                state="active",
                revision=7,
                current_version=1,
            )
        )
        db.add(
            GuardianRoutineVersion(
                routine_id="0123456789abcdef0123456789abcdef",
                version=1,
                installed_package_digest="package-digest",
                source_provenance_json=json.dumps(
                    {
                        "source_task_id": "source-task",
                        "source_attempt_id": "source-attempt",
                        "action_task_id": "action-task",
                        "action_attempt_id": "action-attempt",
                        "source_watch_job_id": "source-job",
                        "source_packet_id": "packet-id",
                        "source_m3_job_id": "action-job",
                    },
                    sort_keys=True,
                ),
            )
        )

    monkeypatch.setattr(
        RoutineService,
        "_package_readback",
        staticmethod(lambda *_args, **_kwargs: {"status": "active", "digest": "package-digest"}),
    )
    watches = {
        "watch-id": {"goal_id": "goal-m6", "goal_revision": 4, "plan_revision": 5, "state": "active"},
        "watch-id-second": {"goal_id": "goal-m6-second", "goal_revision": 8, "plan_revision": 9, "state": "active"},
        "watch-id-paused": {"goal_id": "goal-m6-second", "goal_revision": 8, "plan_revision": 9, "state": "paused"},
    }

    async def get_watch(watch_id, *, owner_principal_id, owner_session_id):
        assert owner_principal_id == OWNER
        assert owner_session_id == SESSION
        return watches.get(watch_id)

    monkeypatch.setattr("src.workflows.routines.source_watch_service.get_watch", get_watch)

    first_request = RoutineInvokeRequest(
        version=1,
        expected_routine_revision=7,
        goal_id="goal-m6",
        expected_goal_revision=4,
        source_watch_id="watch-id",
        expected_watch_revision=5,
        invocation_uuid="11111111-1111-4111-8111-111111111111",
    )
    second_request = RoutineInvokeRequest(
        version=1,
        expected_routine_revision=7,
        goal_id="goal-m6-second",
        expected_goal_revision=8,
        source_watch_id="watch-id-second",
        expected_watch_revision=9,
        invocation_uuid="22222222-2222-4222-8222-222222222222",
    )
    first = await service.invoke(
        "0123456789abcdef0123456789abcdef",
        first_request,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    duplicate = await service.invoke(
        "0123456789abcdef0123456789abcdef",
        first_request,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    second = await service.invoke(
        "0123456789abcdef0123456789abcdef",
        second_request,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    with pytest.raises(RoutineError, match="source_watch_not_active"):
        await service.invoke(
            "0123456789abcdef0123456789abcdef",
            second_request.model_copy(
                update={
                    "source_watch_id": "watch-id-paused",
                    "invocation_uuid": "33333333-3333-4333-8333-333333333333",
                }
            ),
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
        )

    assert first["task_id"] == duplicate["task_id"]
    assert duplicate["deduped"] is True
    assert first["task_id"] != second["task_id"]
    assert first["deduped"] is False
    assert second["deduped"] is False
    async with m6_db() as db:
        tasks = list((await db.execute(select(WorkBoardTask))).scalars().all())
    assert len(tasks) == 2
    assert {task.status for task in tasks} == {WorkBoardStatus.todo}
    by_goal = {task.goal_id: task for task in tasks}
    assert by_goal.keys() == {"goal-m6", "goal-m6-second"}
    for task, expected_goal, expected_revision, expected_watch in (
        (by_goal["goal-m6"], "goal-m6", 4, "watch-id"),
        (by_goal["goal-m6-second"], "goal-m6-second", 8, "watch-id-second"),
    ):
        assert task.goal_id == expected_goal
        assert task.goal_revision == expected_revision
        assert task.typed_input_ref.startswith("workspace-json:artifacts/work-board/routine-inputs/")
        path = tmp_path / task.typed_input_ref.removeprefix("workspace-json:")
        assert _sha(path.read_text()) == task.typed_input_digest
        payload = json.loads(path.read_text())["input"]
        assert payload["routine_id"] == "0123456789abcdef0123456789abcdef"
        assert payload["goal_id"] == expected_goal
        assert payload["expected_goal_revision"] == expected_revision
        assert payload["source_watch_id"] == expected_watch


@pytest.mark.asyncio
async def test_existing_invocation_replays_before_stale_authority(m6_db, monkeypatch, tmp_path):
    """A lost receipt is recoverable after authority changes, but rebinding is not."""

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    service = RoutineService()
    routine_id = "abcdef0123456789abcdef0123456789"
    async with m6_db() as db:
        db.add(
            Goal(
                id="goal-m6-replay",
                title="M6 replay goal",
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                revision=4,
                status="active",
            )
        )
        db.add(
            GuardianRoutine(
                id=routine_id,
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                name="Replay-safe procedure",
                state="active",
                revision=7,
                current_version=1,
            )
        )
        db.add(
            GuardianRoutineVersion(
                routine_id=routine_id,
                version=1,
                installed_package_digest="package-digest",
            )
        )

    monkeypatch.setattr(
        RoutineService,
        "_package_readback",
        staticmethod(lambda *_args, **_kwargs: {"status": "active", "digest": "package-digest"}),
    )

    async def get_watch(watch_id, *, owner_principal_id, owner_session_id):
        assert owner_principal_id == OWNER
        assert owner_session_id == SESSION
        return {"goal_id": "goal-m6-replay", "goal_revision": 4, "plan_revision": 5, "state": "active"} if watch_id == "watch-replay" else None

    monkeypatch.setattr("src.workflows.routines.source_watch_service.get_watch", get_watch)
    request = RoutineInvokeRequest(
        version=1,
        expected_routine_revision=7,
        goal_id="goal-m6-replay",
        expected_goal_revision=4,
        source_watch_id="watch-replay",
        expected_watch_revision=5,
        invocation_uuid="66666666-6666-4666-8666-666666666666",
    )
    first = await service.invoke(
        routine_id,
        request,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )

    async with m6_db() as db:
        routine = (await db.execute(select(GuardianRoutine).where(GuardianRoutine.id == routine_id))).scalars().one()
        routine.state = "paused"
        routine.revision = 8
        goal = (await db.execute(select(Goal).where(Goal.id == "goal-m6-replay"))).scalars().one()
        goal.status = "paused"
        goal.revision = 5

    async def stale_watch_must_not_be_read(*_args, **_kwargs):
        raise AssertionError("a committed invocation must replay before current watch authority checks")

    monkeypatch.setattr("src.workflows.routines.source_watch_service.get_watch", stale_watch_must_not_be_read)
    replay = await service.invoke(
        routine_id,
        request,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    assert replay["task_id"] == first["task_id"]
    assert replay["task_revision"] == first["task_revision"]
    assert replay["deduped"] is True

    conflicting_request = request.model_copy(update={"source_watch_id": "watch-rebound"})
    with pytest.raises(RoutineError) as error:
        await service.invoke(
            routine_id,
            conflicting_request,
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
        )
    assert error.value.code == "routine_invocation_idempotency_conflict"

    async with m6_db() as db:
        tasks = list((await db.execute(select(WorkBoardTask))).scalars().all())
    assert len(tasks) == 1
    assert tasks[0].task_id == first["task_id"]


@pytest.mark.asyncio
async def test_duplicate_invocation_digest_is_stable(m6_db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    service = RoutineService()
    async with m6_db() as db:
        db.add(
            Goal(
                id="goal-m6-dup",
                title="M6 duplicate goal",
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                revision=1,
            )
        )
    routine = GuardianRoutine(
        id="abcdefabcdefabcdefabcdefabcdefab",
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
        name="Duplicate-safe procedure",
        state="active",
        revision=1,
        current_version=1,
    )
    version = GuardianRoutineVersion(
        routine_id=routine.id,
        version=1,
        installed_package_digest="digest",
    )
    req = RoutineInvokeRequest(
        version=1,
        expected_routine_revision=1,
        goal_id="goal-m6-dup",
        expected_goal_revision=1,
        source_watch_id="watch",
        expected_watch_revision=1,
        invocation_uuid="33333333-3333-4333-8333-333333333333",
    )
    kwargs = {
        "routine": routine,
        "version": version,
        "req": req,
        "owner_principal_id": OWNER,
        "owner_session_id": SESSION,
        "provenance": {"source_task_id": "source", "action_task_id": "action"},
    }
    first = await service._create_board_invocation_task(**kwargs)
    second = await service._create_board_invocation_task(**kwargs)
    assert first["task_id"] == second["task_id"]
    assert second["deduped"] is True
    async with m6_db() as db:
        task = (await db.execute(select(WorkBoardTask))).scalars().one()
    path = tmp_path / task.typed_input_ref.removeprefix("workspace-json:")
    assert _sha(path.read_text()) == task.typed_input_digest


@pytest.mark.asyncio
async def test_invoke_requires_the_current_routine_version_before_admission(
    m6_db,
    monkeypatch,
    tmp_path,
):
    """A superseded version cannot create a board task or durable job."""

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    service = RoutineService()
    routine_id = "0123456789abcdef0123456789abcdef"
    async with m6_db() as db:
        db.add(
            Goal(
                id="goal-current-version",
                title="Current version goal",
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                revision=3,
                status="active",
            )
        )
        db.add(
            GuardianRoutine(
                id=routine_id,
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                name="Version selected procedure",
                state="active",
                revision=5,
                current_version=2,
            )
        )
        db.add(
            GuardianRoutineVersion(
                routine_id=routine_id,
                version=1,
                installed_package_digest="v1-package",
                source_provenance_json="{}",
            )
        )
        db.add(
            GuardianRoutineVersion(
                routine_id=routine_id,
                version=2,
                installed_package_digest="v2-package",
                source_provenance_json="{}",
            )
        )

    monkeypatch.setattr(
        RoutineService,
        "_package_readback",
        staticmethod(lambda _owner, _session, _routine, _version, digest: {"status": "active", "digest": digest}),
    )

    async def get_watch(watch_id, *, owner_principal_id, owner_session_id):
        assert owner_principal_id == OWNER
        assert owner_session_id == SESSION
        return (
            {"goal_id": "goal-current-version", "goal_revision": 3, "plan_revision": 4, "state": "active"}
            if watch_id == "watch-current-version"
            else None
        )

    monkeypatch.setattr("src.workflows.routines.source_watch_service.get_watch", get_watch)
    superseded = RoutineInvokeRequest(
        version=1,
        expected_routine_revision=5,
        goal_id="goal-current-version",
        expected_goal_revision=3,
        source_watch_id="watch-current-version",
        expected_watch_revision=4,
        invocation_uuid="88888888-8888-4888-8888-888888888888",
    )
    with pytest.raises(RoutineError) as error:
        await service.invoke(
            routine_id,
            superseded,
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
        )
    assert error.value.code == "routine_version_not_current"
    async with m6_db() as db:
        assert not list((await db.execute(select(WorkBoardTask))).scalars().all())

    current = superseded.model_copy(
        update={
            "version": 2,
            "invocation_uuid": "99999999-9999-4999-8999-999999999999",
        }
    )
    result = await service.invoke(
        routine_id,
        current,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    assert result["status"] == "queued"
    assert result["task_id"]
    async with m6_db() as db:
        tasks = list((await db.execute(select(WorkBoardTask))).scalars().all())
    assert len(tasks) == 1
    assert tasks[0].task_id == result["task_id"]


@pytest.mark.asyncio
async def test_concurrent_duplicate_invocation_keeps_winner_digest(m6_db, monkeypatch, tmp_path):
    """A UUID race cannot replace the winning task's typed-input bytes."""

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    service = RoutineService()
    async with m6_db() as db:
        db.add(
            Goal(
                id="goal-m6-race-one",
                title="M6 race goal one",
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                revision=1,
            )
        )
        db.add(
            Goal(
                id="goal-m6-race-two",
                title="M6 race goal two",
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                revision=2,
            )
        )
    routine = GuardianRoutine(
        id="fedcbafedcbafedcbafedcbafedcbafe",
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
        name="Concurrent-safe procedure",
        state="active",
        revision=1,
        current_version=1,
    )
    version = GuardianRoutineVersion(
        routine_id=routine.id,
        version=1,
        installed_package_digest="digest",
    )
    first_request = RoutineInvokeRequest(
        version=1,
        expected_routine_revision=1,
        goal_id="goal-m6-race-one",
        expected_goal_revision=1,
        source_watch_id="watch-one",
        expected_watch_revision=1,
        invocation_uuid="55555555-5555-4555-8555-555555555555",
    )
    second_request = first_request.model_copy(
        update={
            "goal_id": "goal-m6-race-two",
            "expected_goal_revision": 2,
            "source_watch_id": "watch-two",
            "invocation_uuid": first_request.invocation_uuid,
        }
    )
    kwargs = {
        "routine": routine,
        "version": version,
        "owner_principal_id": OWNER,
        "owner_session_id": SESSION,
        "provenance": {"source_task_id": "source", "action_task_id": "action"},
    }
    create_barrier = asyncio.Barrier(2)
    original_create_task = WorkBoardRepository.create_task

    async def synchronized_create_task(self, *args, **kwargs):
        await create_barrier.wait()
        return await original_create_task(self, *args, **kwargs)

    monkeypatch.setattr(WorkBoardRepository, "create_task", synchronized_create_task)
    first_result, second_result = await asyncio.gather(
        service._create_board_invocation_task(req=first_request, **kwargs),
        service._create_board_invocation_task(req=second_request, **kwargs),
        return_exceptions=True,
    )
    results = [first_result, second_result]
    successes = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, RoutineError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code in {"idempotency_conflict", "idempotency_payload_conflict"}

    async with m6_db() as db:
        tasks = list((await db.execute(select(WorkBoardTask))).scalars().all())
    assert len(tasks) == 1
    task = tasks[0]
    path = tmp_path / task.typed_input_ref.removeprefix("workspace-json:")
    assert task.typed_input_ref.endswith(f"-{task.typed_input_digest}.json")
    assert _sha(path.read_text()) == task.typed_input_digest
    payload = json.loads(path.read_text())["input"]
    assert payload["goal_id"] == task.goal_id
    assert payload["expected_goal_revision"] == task.goal_revision


@pytest.mark.asyncio
async def test_revoked_version_blocks_invocation(m6_db):
    service = RoutineService()
    async with m6_db() as db:
        db.add(
            GuardianRoutine(
                id="revoked-routine",
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                name="Revoked",
                state="revoked",
                revision=2,
            )
        )
    with pytest.raises(RoutineError, match="routine_revoked_terminal"):
        await service.invoke(
            "revoked-routine",
            RoutineInvokeRequest(
                version=1,
                expected_routine_revision=2,
                goal_id="goal",
                expected_goal_revision=1,
                source_watch_id="watch",
                expected_watch_revision=1,
                invocation_uuid="44444444-4444-4444-8444-444444444444",
            ),
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
        )


@pytest.mark.asyncio
async def test_rollback_keeps_routine_paused_when_child_cancellation_is_unresolved(m6_db, monkeypatch):
    service = RoutineService()
    routine_id = "rollback-quarantine-routine"
    async with m6_db() as db:
        db.add(
            GuardianRoutine(
                id=routine_id,
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                name="Rollback quarantine",
                state="active",
                revision=3,
                current_version=1,
            )
        )
        db.add(
            GuardianRoutineVersion(
                routine_id=routine_id,
                version=2,
                installed_package_digest="target-package",
            )
        )

    monkeypatch.setattr(
        RoutineService,
        "_package_readback",
        staticmethod(lambda *_args, **_kwargs: {"status": "active", "digest": "target-package"}),
    )
    observed_states: list[str] = []

    async def cancel_pending(*_args, **_kwargs):
        async with m6_db() as db:
            row = (await db.execute(select(GuardianRoutine).where(GuardianRoutine.id == routine_id))).scalars().one()
            observed_states.append(str(row.state))
        return [{"job_id": "routine-child:unresolved", "reason_code": "cancel_failed"}]

    monkeypatch.setattr(service, "_cancel_pending_jobs", cancel_pending)
    result = await service.rollback(
        routine_id,
        RoutineRollbackRequest(
            target_version=2,
            expected_routine_revision=3,
            reason="operator rollback",
        ),
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )

    assert result["status"] == "blocked"
    assert result["routine_state"] == "paused"
    assert observed_states == ["paused"]
    async with m6_db() as db:
        row = (await db.execute(select(GuardianRoutine).where(GuardianRoutine.id == routine_id))).scalars().one()
    assert row.state == "paused"
    assert row.revision == 4
    assert row.current_version == 1
    with pytest.raises(RoutineError, match="routine_not_active_or_stale"):
        await service._require_active_routine(
            routine_id,
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
            expected_revision=4,
        )


@pytest.mark.asyncio
async def test_successful_rollback_quarantines_then_allows_fresh_board_invocation(
    m6_db,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    service = RoutineService()
    routine_id = "rollback-success-routine"
    async with m6_db() as db:
        db.add(
            Goal(
                id="rollback-goal",
                title="Rollback goal",
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                revision=1,
                status="active",
            )
        )
        db.add(
            GuardianRoutine(
                id=routine_id,
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                name="Rollback success",
                state="active",
                revision=3,
                current_version=1,
            )
        )
        db.add(
            GuardianRoutineVersion(
                routine_id=routine_id,
                version=2,
                installed_package_digest="target-package",
                source_provenance_json="{}",
            )
        )

    monkeypatch.setattr(
        RoutineService,
        "_package_readback",
        staticmethod(lambda *_args, **_kwargs: {"status": "active", "digest": "target-package"}),
    )
    observed_states: list[str] = []

    async def cancel_pending(*_args, **_kwargs):
        async with m6_db() as db:
            row = (await db.execute(select(GuardianRoutine).where(GuardianRoutine.id == routine_id))).scalars().one()
            observed_states.append(str(row.state))
        return []

    monkeypatch.setattr(service, "_cancel_pending_jobs", cancel_pending)
    rolled_back = await service.rollback(
        routine_id,
        RoutineRollbackRequest(
            target_version=2,
            expected_routine_revision=3,
            reason="operator rollback",
        ),
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    assert rolled_back["state"] == "active"
    assert rolled_back["current_version"] == 2
    assert rolled_back["revision"] == 5
    assert observed_states == ["paused"]

    async def current_watch(_watch_id, *, owner_principal_id, owner_session_id):
        assert owner_principal_id == OWNER
        assert owner_session_id == SESSION
        return {"goal_id": "rollback-goal", "goal_revision": 1, "plan_revision": 1, "state": "active"}

    monkeypatch.setattr("src.workflows.routines.source_watch_service.get_watch", current_watch)
    invocation = await service.invoke(
        routine_id,
        RoutineInvokeRequest(
            version=2,
            expected_routine_revision=5,
            goal_id="rollback-goal",
            expected_goal_revision=1,
            source_watch_id="rollback-watch",
            expected_watch_revision=1,
            invocation_uuid="77777777-7777-4777-8777-777777777777",
        ),
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    assert invocation["status"] == "queued"
    assert invocation["task_id"]


@pytest.mark.asyncio
async def test_successful_rollback_preserves_an_operator_paused_routine(
    m6_db,
    monkeypatch,
):
    service = RoutineService()
    routine_id = "rollback-paused-routine"
    async with m6_db() as db:
        db.add(
            GuardianRoutine(
                id=routine_id,
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                name="Paused rollback",
                state="paused",
                revision=7,
                current_version=1,
            )
        )
        db.add(
            GuardianRoutineVersion(
                routine_id=routine_id,
                version=2,
                installed_package_digest="paused-target-package",
            )
        )

    monkeypatch.setattr(
        RoutineService,
        "_package_readback",
        staticmethod(
            lambda *_args, **_kwargs: {
                "status": "active",
                "digest": "paused-target-package",
            }
        ),
    )
    observed_states: list[str] = []

    async def cancel_pending(*_args, **_kwargs):
        async with m6_db() as db:
            row = (
                await db.execute(
                    select(GuardianRoutine).where(GuardianRoutine.id == routine_id)
                )
            ).scalars().one()
            observed_states.append(str(row.state))
        return []

    monkeypatch.setattr(service, "_cancel_pending_jobs", cancel_pending)
    rolled_back = await service.rollback(
        routine_id,
        RoutineRollbackRequest(
            target_version=2,
            expected_routine_revision=7,
            reason="operator rollback while paused",
        ),
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )

    assert rolled_back["state"] == "paused"
    assert rolled_back["current_version"] == 2
    assert rolled_back["revision"] == 9
    assert observed_states == ["paused"]
    async with m6_db() as db:
        row = (
            await db.execute(
                select(GuardianRoutine).where(GuardianRoutine.id == routine_id)
            )
        ).scalars().one()
    assert row.state == "paused"
    assert row.current_version == 2


def _verified_source_job(owner: str = OWNER, session: str = SESSION) -> dict[str, object]:
    return {
        "job_kind": "guardian_source_watch",
        "status": "succeeded",
        "owner": {"kind": "user", "principal_id": owner},
        "session_id": session,
        "operator_session_id": session,
        "declared_authority": {
            "goal_owner_principal_id": owner,
            "goal_owner_session_id": session,
            "session_id": session,
        },
        "effects": [
            {
                "receipt_kind": "readback",
                "status": "succeeded",
                "reconciled": True,
            }
        ],
    }


def _verified_m3_job(owner: str = OWNER, session: str = SESSION) -> dict[str, object]:
    return {
        "job_kind": "github_followthrough_v1",
        "status": "succeeded",
        "owner": {"kind": "user", "principal_id": owner},
        "session_id": session,
        "operator_session_id": session,
        "declared_authority": {
            "goal_owner_principal_id": owner,
            "goal_owner_session_id": session,
            "session_id": session,
        },
        "effects": [
            {
                "receipt_kind": "readback",
                "status": "succeeded",
                "reconciled": True,
            }
        ],
    }


def _mock_source_packet_session(monkeypatch, packet):
    """Keep _source_proof contract tests independent of SQLite startup."""

    class _ScalarRows:
        def scalars(self):
            return self

        def first(self):
            return packet

    class _PacketSession:
        async def execute(self, _statement):
            return _ScalarRows()

        def expunge(self, _row):
            return None

    @asynccontextmanager
    async def _get_session():
        yield _PacketSession()

    monkeypatch.setattr(db_engine, "get_session", _get_session)


@pytest.mark.asyncio
async def test_source_proof_rejects_cross_owner_verified_runs(monkeypatch):
    source_job = _verified_source_job()
    m3_job = _verified_m3_job(owner="operator:other", session="session:other")
    monkeypatch.setattr(
        "src.workflows.routines.durable_job_repository.get_job",
        AsyncMock(side_effect=[source_job, m3_job]),
    )

    with pytest.raises(RoutineError, match="routine_owner_mismatch"):
        await RoutineService()._source_proof(
            source_watch_job_id="source-job",
            source_packet_id="packet-source-proof",
            source_m3_job_id="m3-job",
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
        )


@pytest.mark.asyncio
async def test_source_proof_rejects_stale_watch_revision(monkeypatch):
    source_job = _verified_source_job()
    m3_job = _verified_m3_job()
    monkeypatch.setattr(
        "src.workflows.routines.durable_job_repository.get_job",
        AsyncMock(side_effect=[source_job, m3_job]),
    )
    packet = GuardianDecisionPacket(
        id="packet-source-proof",
        source_watch_id="watch-source-proof",
        watch_id="watch-source-proof",
        goal_id="goal-source-proof",
        goal_revision=2,
        plan_revision=4,
        run_identity="source-job",
        status="succeeded",
        verification_status="passed",
        proposal_text="proposal",
        task_text="task",
    )
    _mock_source_packet_session(monkeypatch, packet)
    monkeypatch.setattr(
        "src.workflows.routines.source_watch_service.get_watch",
        AsyncMock(
            return_value={
                "goal_id": "goal-source-proof",
                "owner_principal_id": OWNER,
                "owner_session_id": SESSION,
                "goal_revision": 2,
                "plan_revision": 3,
            }
        ),
    )

    with pytest.raises(RoutineError, match="source_watch_revision_stale"):
        await RoutineService()._source_proof(
            source_watch_job_id="source-job",
            source_packet_id="packet-source-proof",
            source_m3_job_id="m3-job",
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
        )


@pytest.mark.asyncio
async def test_source_proof_rejects_mismatched_prepared_dossier(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    dossier = tmp_path / "artifacts" / "dossier-source-proof.txt"
    dossier.parent.mkdir(parents=True)
    dossier.write_text("verified dossier", encoding="utf-8")
    dossier_sha = _sha("verified dossier")
    source_job = _verified_source_job()
    m3_job = _verified_m3_job()
    monkeypatch.setattr(
        "src.workflows.routines.durable_job_repository.get_job",
        AsyncMock(side_effect=[source_job, m3_job]),
    )
    packet = GuardianDecisionPacket(
        id="packet-source-proof-dossier",
        source_watch_id="watch-source-proof",
        watch_id="watch-source-proof",
        goal_id="goal-source-proof",
        goal_revision=2,
        plan_revision=4,
        run_identity="source-job",
        status="succeeded",
        verification_status="passed",
        proposal_text="proposal",
        task_text="task",
        dossier_path="artifacts/dossier-source-proof.txt",
        dossier_artifact_id="dossier-1",
        dossier_sha256=dossier_sha,
    )
    _mock_source_packet_session(monkeypatch, packet)
    monkeypatch.setattr(
        "src.workflows.routines.source_watch_service.get_watch",
        AsyncMock(
            return_value={
                "goal_id": "goal-source-proof",
                "owner_principal_id": OWNER,
                "owner_session_id": SESSION,
                "goal_revision": 2,
                "plan_revision": 4,
            }
        ),
    )
    prepared = SimpleNamespace(
        job_id="m3-job",
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
        dossier_artifact_id="forged-dossier",
        dossier_sha256=dossier_sha,
        source_watch_id="watch-source-proof",
        goal_id="goal-source-proof",
        goal_revision=2,
        plan_revision=4,
        operation_id="11111111-1111-4111-8111-111111111111",
        repository="owner/repo",
        connection_id="connection-1",
        connection_revision=1,
        action="create_issue",
        issue_number=None,
        title="title",
        body="body",
    )
    async def read_prepared(_self, _job):
        return prepared

    monkeypatch.setattr(
        "src.extensions.github_followthrough.GitHubFollowthroughService._read_prepared",
        read_prepared,
    )

    with pytest.raises(RoutineError, match="source_m3_dossier_binding_missing"):
        await RoutineService()._source_proof(
            source_watch_job_id="source-job",
            source_packet_id="packet-source-proof-dossier",
            source_m3_job_id="m3-job",
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("include_handoff_digest", [False, True])
async def test_source_proof_binds_routine_publication_and_safe_handoff_digests(
    monkeypatch,
    tmp_path,
    include_handoff_digest,
):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    dossier = tmp_path / "artifacts" / "dossier-routine-binding.txt"
    dossier.parent.mkdir(parents=True)
    dossier.write_text("verified routine dossier", encoding="utf-8")
    dossier_sha = _sha("verified routine dossier")
    packet = GuardianDecisionPacket(
        id="packet-routine-binding",
        source_watch_id="watch-routine-binding",
        watch_id="watch-routine-binding",
        goal_id="goal-routine-binding",
        goal_revision=2,
        plan_revision=4,
        run_identity="source-routine-binding",
        status="succeeded",
        verification_status="passed",
        proposal_text="proposal",
        task_text="task",
        dossier_path="artifacts/dossier-routine-binding.txt",
        dossier_artifact_id="dossier-routine-binding",
        dossier_sha256=dossier_sha,
    )
    _mock_source_packet_session(monkeypatch, packet)
    monkeypatch.setattr(
        "src.workflows.routines.source_watch_service.get_watch",
        AsyncMock(
            return_value={
                "goal_id": packet.goal_id,
                "owner_principal_id": OWNER,
                "owner_session_id": SESSION,
                "goal_revision": packet.goal_revision,
                "plan_revision": packet.plan_revision,
            }
        ),
    )
    operation_uuid = "22222222-2222-4222-8222-222222222222"
    operation_id = str(_operation_id(OWNER, uuid.UUID(operation_uuid)))
    binding = {
        "routine_id": "0123456789abcdef0123456789abcdef",
        "routine_revision": 7,
        "routine_version": 2,
        "package_digest": "d" * 64,
        "parent_invocation_job_id": "routine-invocation:source:1",
        "publication_child_job_id": "routine-child:source:1",
        "invocation_uuid": "11111111-1111-4111-8111-111111111111",
        "owner_principal_id": OWNER,
        "owner_session_id": SESSION,
        "goal_id": packet.goal_id,
        "goal_revision": packet.goal_revision,
        "source_watch_id": packet.source_watch_id,
        "connection_id": "connection-routine-binding",
        "connection_revision": 3,
        "repository": "owner/repo",
        "action": "create_issue",
        "operation_uuid": operation_uuid,
    }
    handoff_digest = "a" * 64
    authority = {
        "goal_owner_principal_id": OWNER,
        "goal_owner_session_id": SESSION,
        "session_id": SESSION,
        "source_watch_id": packet.source_watch_id,
        "dossier_artifact_id": packet.dossier_artifact_id,
        "dossier_sha256": dossier_sha,
        "routine_binding": binding,
    }
    if include_handoff_digest:
        authority["parent_handoff_digest"] = handoff_digest
    input_fields = {
        "operation_id": operation_id,
        "repository": binding["repository"],
        "connection_id": binding["connection_id"],
        "connection_revision": binding["connection_revision"],
        "action": binding["action"],
        "issue_number": None,
        "title_sha256": _sha("routine title"),
        "body_sha256": _sha("routine body"),
        "dossier_artifact_id": packet.dossier_artifact_id,
        "dossier_sha256": dossier_sha,
        "source_watch_id": packet.source_watch_id,
        "goal_id": packet.goal_id,
        "goal_revision": packet.goal_revision,
        "plan_revision": packet.plan_revision,
        "routine_binding": binding,
    }
    if include_handoff_digest:
        input_fields["parent_handoff_digest"] = handoff_digest
    m3_job = {
        **_verified_m3_job(),
        "input_digest": _sha(_dump(input_fields)),
        "declared_authority": authority,
    }
    monkeypatch.setattr(
        "src.workflows.routines.durable_job_repository.get_job",
        AsyncMock(side_effect=[_verified_source_job(), m3_job]),
    )
    prepared = SimpleNamespace(
        job_id="m3-routine-binding",
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
        dossier_artifact_id=packet.dossier_artifact_id,
        dossier_sha256=dossier_sha,
        source_watch_id=packet.source_watch_id,
        goal_id=packet.goal_id,
        goal_revision=packet.goal_revision,
        plan_revision=packet.plan_revision,
        operation_id=operation_id,
        repository=binding["repository"],
        connection_id=binding["connection_id"],
        connection_revision=binding["connection_revision"],
        action=binding["action"],
        issue_number=None,
        title="routine title",
        body="routine body",
    )

    async def read_prepared(_self, _job):
        return prepared

    monkeypatch.setattr(
        "src.extensions.github_followthrough.GitHubFollowthroughService._read_prepared",
        read_prepared,
    )

    provenance, verified_packet = await RoutineService()._source_proof(
        source_watch_job_id="source-routine-binding",
        source_packet_id=packet.id,
        source_m3_job_id="m3-routine-binding",
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )

    assert verified_packet.id == packet.id
    assert provenance["source_repository"] == "owner/repo"
    assert provenance["source_action"] == "create_issue"


def test_export_redacts_prior_approval_and_private_source():
    version = GuardianRoutineVersion(
        routine_id="routine",
        version=1,
        source_provenance_json=json.dumps(
            {
                "source_task_id": "source-task",
                "source_packet_id": "packet",
                "approval_id": "approval-secret",
                "private_source_body": "do not export",
                "credential_ref": "vault://secret",
            }
        ),
    )
    projected = RoutineService._version_json(version)
    encoded = json.dumps(projected)
    assert "approval-secret" not in encoded
    assert "do not export" not in encoded
    assert "vault://secret" not in encoded
    assert projected["source_provenance"] == {
        "source_packet_id": "packet",
        "source_task_id": "source-task",
    }


def test_board_provenance_keeps_bounded_ids_and_digests_only():
    projected = _safe_routine_provenance(
        {
            "source_task_artifact_refs": [
                {
                    "artifact_id": "artifact-source",
                    "content_sha256": "a" * 64,
                    "file_path": "private/source.txt",
                    "body": "private source body",
                }
            ],
            "action_task_artifact_refs": [
                {
                    "artifact_id": "artifact-action",
                    "target_digest": "b" * 64,
                    "target_path": "private/action.txt",
                    "credential_ref": "vault://secret",
                }
            ],
            "source_attempt_receipt_refs": [
                {
                    "readback_id": "readback-source",
                    "workflow_run_id": "source-run",
                    "content_sha256": "c" * 64,
                    "status": "succeeded",
                    "prompt": "private prompt",
                }
            ],
            "action_attempt_receipt_refs": [
                {
                    "verification_id": "verification-action",
                    "workflow_run_id": "action-run",
                    "content_sha256": "d" * 64,
                    "status": "succeeded",
                    "raw_response": "private response",
                }
            ],
        }
    )
    assert projected["source_task_artifact_refs"] == [
        {"artifact_id": "artifact-source", "content_sha256": "a" * 64}
    ]
    assert projected["action_task_artifact_refs"] == [
        {"artifact_id": "artifact-action", "target_digest": "b" * 64}
    ]
    assert projected["source_attempt_receipt_refs"] == [
        {
            "readback_id": "readback-source",
            "workflow_run_id": "source-run",
            "content_sha256": "c" * 64,
            "status": "succeeded",
        }
    ]
    assert projected["action_attempt_receipt_refs"] == [
        {
            "verification_id": "verification-action",
            "workflow_run_id": "action-run",
            "content_sha256": "d" * 64,
            "status": "succeeded",
        }
    ]
    encoded = json.dumps(projected)
    assert "private" not in encoded
    assert "vault://" not in encoded


@pytest.mark.asyncio
async def test_from_run_persists_redacted_board_evidence_provenance(m6_db, monkeypatch):
    service = RoutineService()
    packet = SimpleNamespace(goal_id="goal-m6", goal_revision=4, plan_revision=5)
    base_provenance = {
        "source_watch_job_id": "source-job",
        "source_packet_id": "packet-id",
        "source_m3_job_id": "action-job",
    }

    async def source_proof(**_kwargs):
        return base_provenance, packet

    async def admit(**kwargs):
        return {
            "status": "running",
            "job_id": kwargs["job_id"],
            "declared_authority": {"approval_id": "approval-not-persisted"},
        }

    async def hold(*_args, **_kwargs):
        return "approval-not-persisted"

    monkeypatch.setattr(service, "_source_proof", source_proof)
    monkeypatch.setattr(service, "_admit_user_job", admit)
    monkeypatch.setattr(service, "_hold_approval", hold)
    result = await service.from_run(
        RoutineFromRunRequest(
            source_watch_job_id="source-job",
            source_packet_id="packet-id",
            source_m3_job_id="action-job",
            name="Reviewed procedure",
        ),
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
        routine_id="1234567890abcdef1234567890abcdef",
        provenance_extra={
            "source_task_artifact_refs": [
                {
                    "artifact_id": "artifact-source",
                    "content_sha256": "a" * 64,
                    "file_path": "private/source.txt",
                    "private_body": "do not persist",
                }
            ],
            "action_task_artifact_refs": [
                {
                    "artifact_id": "artifact-action",
                    "target_digest": "b" * 64,
                    "target_path": "private/action.txt",
                    "credential_ref": "vault://secret",
                }
            ],
            "source_attempt_receipt_refs": [
                {
                    "readback_id": "readback-source",
                    "workflow_run_id": "source-job",
                    "content_sha256": "c" * 64,
                    "status": "succeeded",
                    "raw_content": "do not persist",
                }
            ],
            "action_attempt_receipt_refs": [
                {
                    "verification_id": "verification-action",
                    "workflow_run_id": "action-job",
                    "content_sha256": "d" * 64,
                    "status": "succeeded",
                    "prompt": "do not persist",
                }
            ],
        },
    )
    assert result["routine"]["id"] == "1234567890abcdef1234567890abcdef"
    async with m6_db() as db:
        version = (
            await db.execute(
                select(GuardianRoutineVersion).where(
                    GuardianRoutineVersion.routine_id == "1234567890abcdef1234567890abcdef"
                )
            )
        ).scalars().one()
    persisted = json.loads(version.source_provenance_json)
    assert persisted["source_task_artifact_refs"] == [
        {"artifact_id": "artifact-source", "content_sha256": "a" * 64}
    ]
    assert persisted["action_task_artifact_refs"] == [
        {"artifact_id": "artifact-action", "target_digest": "b" * 64}
    ]
    assert persisted["source_attempt_receipt_refs"] == [
        {
            "readback_id": "readback-source",
            "workflow_run_id": "source-job",
            "content_sha256": "c" * 64,
            "status": "succeeded",
        }
    ]
    assert persisted["action_attempt_receipt_refs"] == [
        {
            "verification_id": "verification-action",
            "workflow_run_id": "action-job",
            "content_sha256": "d" * 64,
            "status": "succeeded",
        }
    ]
    encoded = json.dumps(persisted)
    assert "private" not in encoded
    assert "vault://" not in encoded


@pytest.mark.parametrize("status", ["unknown", "intent", "dispatched"])
def test_board_journey_rejects_canonical_unresolved_effect_statuses(status):
    assert RoutineService._board_job_has_unresolved_effect({"status": status}) is True
    assert RoutineService._board_job_has_unresolved_effect(
        {"status": "succeeded", "effects": [{"status": status}]}
    ) is True


@pytest.mark.asyncio
async def test_routine_install_and_invoke_approvals_bind_visible_exact_scope(monkeypatch):
    service = RoutineService()
    approvals = []

    async def get_or_create_pending(**kwargs):
        approvals.append(kwargs)
        return SimpleNamespace(
            id=f"approval-{len(approvals)}",
            expires_at=datetime.now(timezone.utc),
        )

    async def bind_approval_id(*_args, **_kwargs):
        return {
            "authority_digest": "authority-bound",
            "lease": {"owner": "routine:job", "fencing_token": 3},
            "revision": 5,
        }

    async def update_pending_details(*_args, **_kwargs):
        return None

    async def transition_job(*_args, **_kwargs):
        return {"status": "awaiting_approval"}

    monkeypatch.setattr(routines_module.approval_repository, "get_or_create_pending", get_or_create_pending)
    monkeypatch.setattr(routines_module.approval_repository, "update_pending_details", update_pending_details)
    monkeypatch.setattr(routines_module.durable_job_repository, "bind_approval_id", bind_approval_id)
    monkeypatch.setattr(routines_module.durable_job_repository, "transition_job", transition_job)

    jobs = (
        (
            ROUTINE_INSTALL_TOOL,
            {
                "job_id": "routine-install:abcd:v1",
                "goal_id": "goal-install",
                "goal_revision": 4,
                "authority_digest": "install-authority",
                "revision": 4,
                "lease": {"owner": "routine:job", "fencing_token": 3},
                "declared_authority": {
                    "routine_id": "abcd",
                    "routine_version": 1,
                    "workflow_sha256": "a" * 64,
                    "runbook_sha256": "b" * 64,
                    "source_provenance_sha256": "c" * 64,
                    "source_packet_id": "packet-1",
                    "source_dossier_sha256": "d" * 64,
                    "source_repository": "seraph-quest/seraph-m6-fixture",
                    "source_action": "create_issue",
                    "source_target": "991864",
                    "private_source_text": "must not appear",
                },
            },
        ),
        (
            ROUTINE_INVOKE_TOOL,
            {
                "job_id": "routine-invocation:abcd:ef01",
                "goal_id": "goal-run",
                "goal_revision": 8,
                "authority_digest": "invoke-authority",
                "revision": 4,
                "lease": {"owner": "routine:job", "fencing_token": 3},
                "declared_authority": {
                    "routine_id": "abcd",
                    "routine_version": 2,
                    "routine_revision": 9,
                    "workflow_sha256": "e" * 64,
                    "runbook_sha256": "f" * 64,
                    "package_digest": "1" * 64,
                    "source_watch_id": "watch-2",
                    "source_watch_revision": 5,
                    "github_connection_id": "connection-3",
                    "github_connection_revision": 7,
                    "github_repository": "seraph-quest/seraph-m6-fixture",
                    "github_action": "create_issue",
                    "github_target": "991864",
                    "private_source_text": "must not appear",
                },
            },
        ),
    )
    for tool_name, job in jobs:
        await service._hold_approval(
            job,
            tool_name=tool_name,
            summary="review the bounded procedure operation",
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
        )

    assert len(approvals) == 2
    for call in approvals:
        details = call["details"]
        scope = details["approval_scope"]
        assert scope == details["approval_context"]
        assert call["fingerprint"] == routines_module.fingerprint_tool_call(
            call["tool_name"],
            {
                "job_id": details["durable_job_id"],
                "authority_digest": details["durable_authority_digest"],
            },
            approval_context=scope,
        )
        assert "private_source_text" not in json.dumps(details)
        changed_destination = {
            **scope,
            "github_target" if scope["action"] == "run_guardian_procedure" else "source_target": "991865",
        }
        assert call["fingerprint"] != routines_module.fingerprint_tool_call(
            call["tool_name"],
            {
                "job_id": details["durable_job_id"],
                "authority_digest": details["durable_authority_digest"],
            },
            approval_context=changed_destination,
        )
    assert approvals[0]["details"]["approval_scope"]["action"] == "install_guardian_procedure"
    assert approvals[0]["details"]["approval_scope"]["source_repository"] == "seraph-quest/seraph-m6-fixture"
    assert approvals[1]["details"]["approval_scope"]["action"] == "run_guardian_procedure"
    assert approvals[1]["details"]["approval_scope"]["github_target"] == "991864"


@pytest.mark.asyncio
async def test_board_routine_admission_uses_goal_runtime_and_caps_at_900(monkeypatch):
    service = RoutineService()
    admitted_specs = []
    claimed_leases = []

    async def admit_job(spec):
        admitted_specs.append(spec)
        return {"status": "accepted", "revision": 1, "fencing_token": 2}

    async def queue_job(_job_id, **kwargs):
        return {
            "status": "queued",
            "revision": int(kwargs.get("expected_revision") or 1) + 1,
            "fencing_token": 2,
            "lease": {"fencing_token": 2},
        }

    async def claim_job(_job_id, **kwargs):
        claimed_leases.append(kwargs["lease_seconds"])
        return {"status": "running", "lease": {"owner": "routine:job", "fencing_token": 2}}

    monkeypatch.setattr(routines_module.durable_job_repository, "admit_job", admit_job)
    monkeypatch.setattr(routines_module.durable_job_repository, "queue_job", queue_job)
    monkeypatch.setattr(routines_module.durable_job_repository, "claim_job", claim_job)

    for index, runtime in enumerate((300, 10_000), start=1):
        await service._admit_user_job(
            job_id=f"routine-invocation:{index}",
            job_kind="routine_invocation",
            idempotency_key=f"invocation:{index}",
            inputs={},
            authority={},
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
            goal_id="goal-runtime",
            goal_revision=1,
            plan_revision=1,
            candidate_id=None,
            work_board_idempotency_key=f"task:{index}:attempt:{index}",
            runtime_seconds=runtime,
        )

    deadlines = [int((spec.deadline_at - datetime.now(timezone.utc)).total_seconds()) for spec in admitted_specs]
    assert deadlines[0] in {299, 300}
    assert deadlines[1] in {899, 900}
    assert claimed_leases == [300, 900]
    assert _bounded_runtime_seconds(10_000) == 900
    remaining = _remaining_runtime_seconds({"deadline_at": datetime.now(timezone.utc) + timedelta(seconds=12)})
    assert 1 <= remaining <= 12


def test_board_procedure_name_validation_matches_generated_template_contract():
    with pytest.raises(ValidationError):
        RoutineFromBoardPreviewRequest(
            source_task_id="source-task",
            action_task_id="action-task",
            expected_source_revision=1,
            expected_action_revision=1,
            name="bad/name",
            idempotency_key="invalid-name",
        )


@pytest.mark.asyncio
async def test_deterministic_routine_rows_reconcile_after_partial_creation(m6_db):
    service = RoutineService()
    routine_id = "0123456789abcdef0123456789abcdef"
    routine = GuardianRoutine(
        id=routine_id,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
        name="Reviewed procedure",
        state="prepared",
        revision=1,
    )
    version = GuardianRoutineVersion(
        routine_id=routine_id,
        version=1,
        source_provenance_json=json.dumps({"source_packet_id": "packet-1"}, sort_keys=True),
        workflow_bytes="fixed workflow",
        workflow_sha256=_sha("fixed workflow"),
        runbook_bytes="fixed runbook",
        runbook_sha256=_sha("fixed runbook"),
        source_repository="seraph-quest/seraph-m6-fixture",
        source_action="create_issue",
        source_issue_number=991864,
    )

    first = await service._persist_or_verify_board_preparation(routine, version)
    second = await service._persist_or_verify_board_preparation(
        GuardianRoutine(
            id=routine_id,
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
            name="Reviewed procedure",
            state="prepared",
            revision=1,
        ),
        GuardianRoutineVersion(
            routine_id=routine_id,
            version=1,
            source_provenance_json=json.dumps({"source_packet_id": "packet-1"}, sort_keys=True),
            workflow_bytes="fixed workflow",
            workflow_sha256=_sha("fixed workflow"),
            runbook_bytes="fixed runbook",
            runbook_sha256=_sha("fixed runbook"),
            source_repository="seraph-quest/seraph-m6-fixture",
            source_action="create_issue",
            source_issue_number=991864,
        ),
    )
    assert first[0].id == second[0].id == routine_id
    assert first[1].workflow_sha256 == second[1].workflow_sha256 == _sha("fixed workflow")

    with pytest.raises(RoutineError, match="routine_creation_binding_conflict"):
        await service._persist_or_verify_board_preparation(
            GuardianRoutine(
                id=routine_id,
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                name="Different procedure",
                state="prepared",
                revision=1,
            ),
            GuardianRoutineVersion(
                routine_id=routine_id,
                version=1,
                source_provenance_json=json.dumps({"source_packet_id": "packet-1"}, sort_keys=True),
                workflow_bytes="different workflow",
                workflow_sha256=_sha("different workflow"),
                runbook_bytes="fixed runbook",
                runbook_sha256=_sha("fixed runbook"),
            ),
        )


@pytest.mark.asyncio
async def test_blocked_board_binding_retries_exact_preparation(m6_db, monkeypatch):
    service = RoutineService()
    resolved = _resolved_journey()
    request = _board_create_request(service, resolved, idempotency_key="blocked-create-retry")
    binding_id = "binding-blocked-retry"
    deterministic_id = __import__("uuid").uuid5(routines_module.BOARD_ROUTINE_NAMESPACE, binding_id).hex

    async def resolve(*_args, **_kwargs):
        return resolved

    monkeypatch.setattr(service, "_resolve_board_journey", resolve)
    async with m6_db() as db:
        db.add(
            WorkBoardRoutineBinding(
                binding_id=binding_id,
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                idempotency_key=request.idempotency_key,
                preview_digest=request.preview_digest,
                source_task_id=request.source_task_id,
                action_task_id=request.action_task_id,
                routine_name=request.name,
                deterministic_routine_id=deterministic_id,
                state="blocked",
                recovery_reason="routine_creation_failed",
                revision=2,
            )
        )

    from_run = AsyncMock(return_value={"install_job_id": f"routine-install:{deterministic_id}:v1"})
    monkeypatch.setattr(service, "from_run", from_run)
    reads = 0
    prepared = {
        "routine_id": deterministic_id,
        "state": "prepared",
        "status": "prepared",
        "revision": 2,
        "version": 1,
        "install_job_id": f"routine-install:{deterministic_id}:v1",
        "approval_id": "approval-recovered",
        "preview_digest": request.preview_digest,
        "binding_id": binding_id,
    }

    async def read_binding(*_args, **_kwargs):
        nonlocal reads
        reads += 1
        if reads == 1:
            raise RoutineError("routine_not_found")
        return dict(prepared)

    monkeypatch.setattr(service, "_read_board_binding", read_binding)
    result = await service.create_from_board(
        request,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )

    assert result["routine_id"] == deterministic_id
    assert result["install_job_id"] == f"routine-install:{deterministic_id}:v1"
    from_run.assert_awaited_once()
    assert from_run.await_args.kwargs["routine_id"] == deterministic_id
    async with m6_db() as db:
        row = (
            await db.execute(
                select(WorkBoardRoutineBinding).where(WorkBoardRoutineBinding.binding_id == binding_id)
            )
        ).scalars().one()
    assert row.state == "prepared"
    assert row.routine_id == deterministic_id


def _package_fixture_provenance() -> dict[str, object]:
    return {
        "source_watch_id": "package-watch",
        "goal_id": "package-goal",
        "goal_revision": 3,
        "plan_revision": 4,
        "source_task_id": "package-source-task",
        "source_attempt_id": "package-source-attempt",
        "source_task_revision": 2,
        "action_task_id": "package-action-task",
        "action_attempt_id": "package-action-attempt",
        "action_task_revision": 2,
        "source_task_artifact_refs": [{"artifact_id": "source-artifact", "content_sha256": "a" * 64}],
        "action_task_artifact_refs": [{"artifact_id": "action-artifact", "content_sha256": "b" * 64}],
        "source_attempt_receipt_refs": [{"readback_id": "source-readback", "content_sha256": "c" * 64}],
        "action_attempt_receipt_refs": [{"readback_id": "action-readback", "content_sha256": "d" * 64}],
    }


@pytest.mark.asyncio
async def test_routine_package_is_v2_deterministic_and_readback_binds_digest(m6_db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    routine_id = "0123456789abcdef0123456789abcdef"
    provenance = _package_fixture_provenance()
    provenance.update({
        "approval_id": "prior-approval-must-not-export",
        "credential_ref": "vault://private-token-reference",
        "private_source_body": "private source body must not export",
        "access_token": "sentinel-secret-value",
    })
    version = GuardianRoutineVersion(
        routine_id=routine_id,
        version=1,
        source_provenance_json=json.dumps(provenance, sort_keys=True),
        workflow_bytes="legacy workflow bytes",
        workflow_sha256=_sha("legacy workflow bytes"),
        runbook_bytes="legacy runbook bytes",
        runbook_sha256=_sha("legacy runbook bytes"),
    )
    async with m6_db() as db:
        db.add(
            GuardianRoutine(
                id=routine_id,
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                state="installed",
                revision=2,
                current_version=1,
            )
        )
        db.add(version)
        await db.flush()
        db.expunge(version)
    service = RoutineService()
    materialized = service._materialize_routine_package(routine_id, version, allow_create=True)
    manifest = parse_capability_pack_manifest(materialized["manifest_content"])
    assert manifest.schema_version == 2
    assert manifest.id == _routine_pack_id(routine_id, 1)
    assert manifest.contributes.workflows == []
    assert manifest.contributes.runbooks == ["runbooks/verified-guardian-procedure.yaml"]
    assert manifest.authority.tools == []
    assert manifest.authority.network is False
    runbook = yaml.safe_load(materialized["runbook_content"])
    assert runbook["bindings"]["workflow_sha256"] == version.workflow_sha256
    assert [step["tool"] for step in runbook["procedure"]["steps"]] == [
        "guardian_watch_run",
        "github_followthrough",
    ]
    lifecycle = CapabilityPackLifecycle()
    reviewed = lifecycle.review(
        manifest,
        root_path=materialized["root"],
        goal_id="package-goal",
        reviewed_by=OWNER,
    )
    assert reviewed["review"]["digest"] == materialized["digest"]
    async with m6_db() as db:
        row = (
            await db.execute(
                select(GuardianRoutineVersion).where(GuardianRoutineVersion.routine_id == routine_id)
            )
        ).scalars().one()
        row.installed_package_digest = materialized["digest"]
    readback = service._package_readback(OWNER, SESSION, routine_id, 1, materialized["digest"])
    assert readback["digest"] == materialized["digest"]
    assert readback["status"] == "reviewed"
    second = service._materialize_routine_package(routine_id, version, allow_create=False)
    assert second["digest"] == materialized["digest"]
    exported = await service.export_procedure(
        routine_id,
        1,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    exported_text = json.dumps(exported, sort_keys=True)
    assert exported["kind"] == "seraph.reviewed_procedure.v1"
    assert exported["package_digest"] == materialized["digest"]
    assert exported["runbook"]["procedure"]["steps"]
    assert "source-artifact" in exported_text
    for private_value in (
        "prior-approval-must-not-export",
        "vault://private-token-reference",
        "private source body must not export",
        "sentinel-secret-value",
    ):
        assert private_value not in exported_text
    with pytest.raises(RoutineError, match="routine_owner_session_mismatch"):
        await service.export_procedure(
            routine_id,
            1,
            owner_principal_id=OWNER,
            owner_session_id="session:other",
        )


@pytest.mark.asyncio
async def test_routine_package_mutation_stale_digest_and_owner_binding_block(m6_db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    routine_id = "abcdef0123456789abcdef0123456789"
    provenance = _package_fixture_provenance()
    version = GuardianRoutineVersion(
        routine_id=routine_id,
        version=1,
        source_provenance_json=json.dumps(provenance, sort_keys=True),
        workflow_bytes="workflow",
        workflow_sha256=_sha("workflow"),
        runbook_bytes="runbook",
        runbook_sha256=_sha("runbook"),
    )
    async with m6_db() as db:
        db.add(version)
        await db.flush()
        db.expunge(version)
    service = RoutineService()
    materialized = service._materialize_routine_package(routine_id, version, allow_create=True)
    lifecycle = CapabilityPackLifecycle()
    reviewed = lifecycle.review(
        materialized["manifest"],
        root_path=materialized["root"],
        goal_id="package-goal",
        reviewed_by=OWNER,
    )
    approval = lifecycle.prepare_operator_approval(
        materialized["pack_id"],
        action="activate",
        goal_id="package-goal",
        digest=materialized["digest"],
        version=materialized["manifest"].version,
        owner_principal_id=OWNER,
        session_id=SESSION,
        content_digest=materialized["digest"],
        authority_digest=materialized["manifest"].authority_digest,
    )
    lifecycle.resolve_operator_approval(
        materialized["pack_id"],
        approval["approval"]["approval_id"],
        decision="approved",
        owner_principal_id=OWNER,
        session_id=SESSION,
    )
    lifecycle.activate(
        materialized["manifest"],
        root_path=materialized["root"],
        goal_id="package-goal",
        review_id=reviewed["review"]["review_id"],
        approval_id=approval["approval"]["approval_id"],
        owner_principal_id=OWNER,
        session_id=SESSION,
        content_digest=materialized["digest"],
        authority_digest=materialized["manifest"].authority_digest,
    )
    wrong_owner = service._package_readback("operator:other", SESSION, routine_id, 1, materialized["digest"])
    assert wrong_owner["status"] == "blocked"
    (materialized["root"] / "runbooks" / "verified-guardian-procedure.yaml").write_text(
        (materialized["root"] / "runbooks" / "verified-guardian-procedure.yaml").read_text() + "\n# mutation\n",
        encoding="utf-8",
    )
    stale = service._package_readback(OWNER, SESSION, routine_id, 1, materialized["digest"])
    assert stale["status"] == "blocked"
    assert stale["reason"] == "routine_package_mutation_detected"
    with pytest.raises(RoutineError, match="routine_package_mutation_detected"):
        service._materialize_routine_package(routine_id, version, allow_create=False)


@pytest.mark.asyncio
async def test_routine_package_versions_have_independent_lifecycle_pointers(m6_db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    routine_id = "fedcba9876543210fedcba9876543210"
    service = RoutineService()
    versions: list[GuardianRoutineVersion] = []
    async with m6_db() as db:
        for version_number in (1, 2):
            row = GuardianRoutineVersion(
                routine_id=routine_id,
                version=version_number,
                source_provenance_json=json.dumps(_package_fixture_provenance(), sort_keys=True),
                workflow_bytes=f"workflow-{version_number}",
                workflow_sha256=_sha(f"workflow-{version_number}"),
                runbook_bytes=f"runbook-{version_number}",
                runbook_sha256=_sha(f"runbook-{version_number}"),
            )
            db.add(row)
            versions.append(row)
        await db.flush()
        for row in versions:
            db.expunge(row)
    first = service._materialize_routine_package(routine_id, versions[0], allow_create=True)
    second = service._materialize_routine_package(routine_id, versions[1], allow_create=True)
    assert first["pack_id"] != second["pack_id"]
    assert first["root"] != second["root"]
    lifecycle = CapabilityPackLifecycle()
    for materialized in (first, second):
        result = lifecycle.review(
            materialized["manifest"],
            root_path=materialized["root"],
            goal_id="package-goal",
            reviewed_by=OWNER,
        )
        assert result["review"]["pack_id"] == materialized["pack_id"]
        status = lifecycle.status(materialized["pack_id"], owner_principal_id=OWNER, session_id=SESSION)
        assert status["active"] is None
        assert status["available_versions"][0]["digest"] == materialized["digest"]


@pytest.mark.asyncio
async def test_routine_package_review_approval_and_activation_are_server_derived(m6_db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    routine_id = "00112233445566778899aabbccddeeff"
    provenance = {
        "source_watch_id": "review-watch",
        "goal_id": "review-goal",
        "goal_revision": 4,
        "plan_revision": 5,
    }
    version = GuardianRoutineVersion(
        routine_id=routine_id,
        version=1,
        source_provenance_json=json.dumps(provenance, sort_keys=True),
        workflow_bytes="workflow",
        workflow_sha256=_sha("workflow"),
        runbook_bytes="runbook",
        runbook_sha256=_sha("runbook"),
    )
    async with m6_db() as db:
        db.add(
            Goal(
                id="review-goal",
                title="review goal",
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                revision=4,
                status="active",
            )
        )
        db.add(
            GuardianRoutine(
                id=routine_id,
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                state="installed",
                revision=2,
                current_version=1,
            )
        )
        db.add(version)
        await db.flush()
        db.expunge(version)
    service = RoutineService()
    materialized = service._materialize_routine_package(routine_id, version, allow_create=True)
    async with m6_db() as db:
        row = (
            await db.execute(
                select(GuardianRoutineVersion).where(GuardianRoutineVersion.routine_id == routine_id)
            )
        ).scalars().one()
        row.installed_package_digest = materialized["digest"]

    async def current_watch(watch_id, *, owner_principal_id, owner_session_id):
        assert owner_principal_id == OWNER
        assert owner_session_id == SESSION
        return {"goal_id": "review-goal", "goal_revision": 4, "plan_revision": 5, "state": "active"}

    monkeypatch.setattr("src.workflows.routines.source_watch_service.get_watch", current_watch)
    reviewed = await service.review_package(
        routine_id,
        1,
        expected_routine_revision=2,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    assert reviewed["pack_id"] == _routine_pack_id(routine_id, 1)
    prepared = await service.prepare_package_approval(
        routine_id,
        1,
        expected_routine_revision=2,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    approval_id = prepared["approval"]["approval_id"]
    decided = await service.decide_package_approval(
        routine_id,
        1,
        approval_id,
        RoutinePackageDecisionRequest(expected_routine_revision=2, decision="approved"),
        expected_routine_revision=2,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    assert decided["approval"]["status"] == "approved"
    activated = await service.activate_package(
        routine_id,
        1,
        RoutinePackageActivationRequest(expected_routine_revision=2, approval_id=approval_id),
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    assert activated["status"] == "active"
    readback = service._package_readback(OWNER, SESSION, routine_id, 1, materialized["digest"])
    assert readback["status"] == "active"
    assert readback["digest"] == materialized["digest"]
    with pytest.raises(RoutineError, match="routine_not_found"):
        await service.review_package(
            routine_id,
            1,
            expected_routine_revision=2,
            owner_principal_id="operator:other",
            owner_session_id=SESSION,
        )
    async with m6_db() as db:
        goal = (await db.execute(select(Goal).where(Goal.id == "review-goal"))).scalars().one()
        goal.revision = 5
    with pytest.raises(RoutineError, match="routine_source_goal_stale"):
        await service.review_package(
            routine_id,
            1,
            expected_routine_revision=2,
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
        )


@pytest.mark.asyncio
async def test_revoked_routine_rejects_package_review_approval_and_activation_even_with_old_approval(
    m6_db,
    monkeypatch,
    tmp_path,
):
    """A pre-revoke package approval cannot authorize any post-revoke action."""

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    routine_id = "00112233445566778899aabbccddee11"
    provenance = {
        "source_watch_id": "revoke-review-watch",
        "goal_id": "revoke-review-goal",
        "goal_revision": 4,
        "plan_revision": 5,
    }
    version = GuardianRoutineVersion(
        routine_id=routine_id,
        version=1,
        source_provenance_json=json.dumps(provenance, sort_keys=True),
        workflow_bytes="workflow",
        workflow_sha256=_sha("workflow"),
        runbook_bytes="runbook",
        runbook_sha256=_sha("runbook"),
    )
    async with m6_db() as db:
        db.add(
            Goal(
                id="revoke-review-goal",
                title="revoke review goal",
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                revision=4,
                status="active",
            )
        )
        db.add(
            GuardianRoutine(
                id=routine_id,
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                state="installed",
                revision=2,
                current_version=1,
            )
        )
        db.add(version)
        await db.flush()
        db.expunge(version)

    service = RoutineService()
    materialized = service._materialize_routine_package(routine_id, version, allow_create=True)
    async with m6_db() as db:
        row = (
            await db.execute(
                select(GuardianRoutineVersion).where(GuardianRoutineVersion.routine_id == routine_id)
            )
        ).scalars().one()
        row.installed_package_digest = materialized["digest"]

    async def current_watch(watch_id, *, owner_principal_id, owner_session_id):
        assert watch_id == "revoke-review-watch"
        assert owner_principal_id == OWNER
        assert owner_session_id == SESSION
        return {"goal_id": "revoke-review-goal", "goal_revision": 4, "plan_revision": 5, "state": "active"}

    monkeypatch.setattr("src.workflows.routines.source_watch_service.get_watch", current_watch)
    await service.review_package(
        routine_id,
        1,
        expected_routine_revision=2,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    prepared = await service.prepare_package_approval(
        routine_id,
        1,
        expected_routine_revision=2,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
    approval_id = prepared["approval"]["approval_id"]

    async with m6_db() as db:
        routine = (
            await db.execute(select(GuardianRoutine).where(GuardianRoutine.id == routine_id))
        ).scalars().one()
        routine.state = "revoked"
        routine.revision = 3

    async def assert_revoked(operation):
        with pytest.raises(RoutineError, match="routine_revoked_terminal"):
            await operation()

    await assert_revoked(
        lambda: service.package_preview(
            routine_id,
            1,
            expected_routine_revision=3,
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
        )
    )
    await assert_revoked(
        lambda: service.review_package(
            routine_id,
            1,
            expected_routine_revision=3,
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
        )
    )
    await assert_revoked(
        lambda: service.prepare_package_approval(
            routine_id,
            1,
            expected_routine_revision=3,
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
        )
    )
    await assert_revoked(
        lambda: service.decide_package_approval(
            routine_id,
            1,
            approval_id,
            RoutinePackageDecisionRequest(expected_routine_revision=3, decision="approved"),
            expected_routine_revision=3,
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
        )
    )
    await assert_revoked(
        lambda: service.activate_package(
            routine_id,
            1,
            RoutinePackageActivationRequest(expected_routine_revision=3, approval_id=approval_id),
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
        )
    )


@pytest.mark.asyncio
async def test_procedure_export_route_uses_authenticated_owner_session(monkeypatch):
    operator = SimpleNamespace(
        principal=SimpleNamespace(principal_id=OWNER),
        session_id=SESSION,
    )
    monkeypatch.setattr(routines_module, "_operator", lambda _request: operator)
    export = AsyncMock(return_value={
        "kind": "seraph.reviewed_procedure.v1",
        "pack_id": "seraph.routine.test.v1",
        "version": 1,
    })
    monkeypatch.setattr(routines_module.routine_service, "export_procedure", export)

    result = await routines_module.export_routine_procedure(
        "routine-1",
        1,
        request=object(),
    )

    assert result["kind"] == "seraph.reviewed_procedure.v1"
    export.assert_awaited_once_with(
        "routine-1",
        1,
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
    )
