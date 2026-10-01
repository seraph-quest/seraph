"""End-to-end proof for one native ``guardian-routine.v2`` Browser leaf.

This module deliberately uses the real SQLite repositories, the real durable
parent/child records, and a real local Chromium process.  Only public fixture
HTTP is intercepted; no model/provider or external network call is used.
"""

from __future__ import annotations

from dataclasses import replace
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from config.settings import settings
from src.artifacts.registry import build_artifact_record
from src.browser.pinned_transport import (
    PinnedBrowserRequest,
    PinnedBrowserResponse,
    PinnedBrowserTransport,
)
from src.browser.task_runner import browser_artifact_path_for_job
from src.db.models import (
    Goal,
    GuardianDecisionPacket,
    GuardianSourceBaseline,
    GuardianSourceWatch,
    Session,
    WorkBoardAttempt,
    WorkBoardInputArtifact,
    WorkBoardStatus,
    WorkBoardTask,
)
from src.goals.contracts import GoalAdmissionBudget
from src.goals.repository import serialize_admission_budget
from src.work_board.contracts import (
    WorkBoardInputArtifactCreate,
    WorkBoardOwner,
    WorkBoardTaskCreate,
)
from src.work_board.dispatcher import WorkBoardDispatcher, registered_executor_id
from src.work_board.input_artifacts import prepare_input_artifact
from src.work_board.repository import WorkBoardRepository
from src.workflows.job_runtime import (
    DurableJobAdmissionDenied,
    DurableJobError,
    DurableJobIdentity,
    DurableJobLeaseError,
    DurableJobRepository,
    DurableJobSpec,
    DurableJobTransitionError,
    durable_job_repository,
    _safe_structure,
)
from src.workflows.procedure_contracts import build_procedure_plan, plan_digest
from src.workflows.procedure_v2_runtime import (
    ProcedureV2Runtime,
    ProcedureV2RuntimeError,
    _watch_child_refs,
    deterministic_child_job_id,
)

from tests.test_browser_task_lifecycle import _html_responses
from tests.test_browser_task_runtime import _input


pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("value", ["7", True, -1, 1.5])
async def test_watch_parent_fencing_counter_redaction_requires_typed_integer(value: Any) -> None:
    """Only the internal non-secret integer Watch fence survives projection."""

    assert _safe_structure({"watch_parent_fencing_token": value}) == {
        "watch_parent_fencing_token": "[redacted]"
    }
    assert _safe_structure({"watch_bearer_token": "opaque-secret"}) == {
        "watch_bearer_token": "[redacted]"
    }
    assert _safe_structure({"watch_parent_fencing_token": 7}) == {
        "watch_parent_fencing_token": 7
    }


def _replay_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _descriptor(*, source_ref: str, source_digest: str, goal_id: str, invocation_uuid: str) -> dict[str, Any]:
    plan = build_procedure_plan(
        "public-browser-check",
        step_inputs={
            "public_browser_check": {
                "typed_input_ref": source_ref,
                "typed_input_digest": source_digest,
            }
        },
    ).model_dump(mode="json")
    return {
        "routine_id": "routine-browser-proof-v2",
        "version": 1,
        "goal_id": goal_id,
        "goal_revision": 1,
        "routine_revision": 1,
        "installed_package_digest": "b" * 64,
        "source_proof_digest": "a" * 64,
        "scope": "procedure-v2:public-browser-check",
        "invocation_uuid": invocation_uuid,
        "parameters": {"goal_id": goal_id, "expected_goal_revision": 1},
        "plan": plan,
        "plan_digest": plan_digest(plan),
        "executable_steps": {
            "public_browser_check": {
                "typed_input_ref": source_ref,
                "typed_input_digest": source_digest,
            }
        },
    }


def _browser_replay_projection(*, workspace_root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, str]:
    parent_job_id = "procedure-v2:replay-proof:1:parent"
    child_job_id = deterministic_child_job_id(
        parent_job_id,
        "public-browser-check",
        1,
        "public_browser_check",
    )
    artifact_ref = browser_artifact_path_for_job(child_job_id)
    artifact_path = workspace_root / artifact_ref
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_bytes = b'{"verified":true,"source":"replay-proof"}'
    artifact_path.write_bytes(artifact_bytes)
    artifact_sha = hashlib.sha256(artifact_bytes).hexdigest()
    artifact_id = "art_" + hashlib.sha256(
        "|".join(
            (
                "browser_public_task",
                "browser_public_task_result",
                child_job_id,
                artifact_ref,
                artifact_sha,
            )
        ).encode("utf-8")
    ).hexdigest()[:24]
    readback_id = "readback-" + _replay_digest(
        {"job_id": child_job_id, "path": artifact_ref, "digest": artifact_sha}
    )[:32]
    goal_id = "goal-replay-proof"
    input_digest = "a" * 64
    descriptor = _descriptor(
        source_ref="workspace-json:artifacts/work-board/inputs/replay.json",
        source_digest=input_digest,
        goal_id=goal_id,
        invocation_uuid="replay-proof",
    )
    parent_authority = {
        "template_id": "public-browser-check",
        "routine_version": 1,
        "plan_digest": descriptor["plan_digest"],
        "principal": "operator:replay",
        "session_id": "session:replay",
    }
    parent = {
        "job_id": parent_job_id,
        "run_identity": parent_job_id,
        "status": "running",
        "job_kind": "guardian_routine_v2",
        "capability_version": "guardian-routine.v2",
        "goal_id": goal_id,
        "goal_revision": 1,
        "owner": {"principal_id": "operator:replay"},
        "operator_session_id": "session:replay",
        "lease": {"owner": "parent-runner", "fencing_token": 7},
        "declared_authority": parent_authority,
    }
    child_authority = {
        "routine_parent_job_id": parent_job_id,
        "routine_parent_fencing_token": 7,
        "routine_step_id": "public_browser_check",
        "capability_id": "browser.public-task.v1",
        "capability_version": "1",
        "board_task_id": "browser-task-replay",
        "board_attempt_id": "browser-attempt-replay",
        "board_task_revision": 3,
        "board_fencing_token": 11,
        "input_artifact_id": "browser-input-replay",
        "input_artifact_digest": input_digest,
    }
    child = {
        "job_id": child_job_id,
        "run_identity": child_job_id,
        "root_run_identity": parent_job_id,
        "parent_run_identity": parent_job_id,
        "parent_job_id": parent_job_id,
        "parent_fencing_token": 7,
        "status": "succeeded",
        "job_kind": "browser_public_task",
        "capability_version": "1",
        "goal_id": goal_id,
        "goal_revision": 1,
        "declared_authority": child_authority,
        "artifacts": [
            {
                "artifact_id": artifact_id,
                "artifact_type": "browser_public_task_result",
                "producer": "browser_public_task",
                "file_path": artifact_ref,
                "content_sha256": artifact_sha,
                "exists": True,
            }
        ],
        "effects": [
            {
                "job_id": child_job_id,
                "effect_type": "browser_public_task_result",
                "receipt_kind": "readback",
                "status": "succeeded",
                "readback_id": readback_id,
                "verified_at": "2026-10-01T00:00:00+00:00",
                "target_path": artifact_ref,
                "target_digest": artifact_sha,
                "content_sha256": artifact_sha,
                "details": {"verified": True},
            },
            {
                "job_id": child_job_id,
                "effect_type": "browser_context_cleanup",
                "receipt_kind": "effect",
                "status": "succeeded",
                "details": {
                    "cleanup_status": "cleanup_verified",
                    "context_not_started": False,
                    "memory_status": "no_learning",
                },
            },
        ],
    }
    checkpoint = {
        "checkpoint_id": "procedure-v2:step:public_browser_check:admitted",
        "safe": True,
        "payload": {
            "step_id": "public_browser_check",
            "child_job_id": child_job_id,
            "child_task_id": "browser-task-replay",
            "child_attempt_id": "browser-attempt-replay",
            "child_task_revision": 3,
            "child_admission_task_revision": 3,
            "child_fencing_token": 11,
            "child_input_artifact_id": "browser-input-replay",
            "child_input_artifact_digest": input_digest,
            "child_capability_id": "browser.public-task.v1",
            "child_capability_version": "1",
        },
    }
    step = descriptor["plan"]["steps"][0]
    return parent, child, checkpoint, child_job_id, step


async def _create_parent_claim(
    *,
    async_db: Any,
    repository: WorkBoardRepository,
    owner: WorkBoardOwner,
    goal_id: str,
    parent_input: Mapping[str, Any],
    parent_artifact_id: str,
    dispatcher: WorkBoardDispatcher,
) -> Any:
    async with async_db() as db:
        mutation = await repository.create_task(
            db,
            owner,
            WorkBoardTaskCreate(
                title="Run reviewed Browser procedure",
                body="Execute the fixed public Browser leaf and retain its readback.",
                goal_id=goal_id,
                goal_revision=1,
                status=WorkBoardStatus.todo,
                capability_id="guardian-routine.v2",
                input_artifact_id=parent_artifact_id,
                executor_id=registered_executor_id("guardian-routine.v2"),
                priority=90,
                idempotency_scope="guardian-routine-v2",
                idempotency_key="parent-browser-proof",
                origin_thread_id=owner.session_id,
            ),
            origin_session_id=owner.session_id,
        )
        promoted = await repository.promote_task_ready(
            db,
            mutation.task.task_id,
            expected_revision=mutation.task.task_revision,
            actor_principal_id=dispatcher.runner_id,
            actor_session_id=dispatcher.runner_session,
        )
        assert promoted is not None
        claim = await repository.claim_ready_task(
            db,
            mutation.task.task_id,
            expected_revision=promoted.task.task_revision,
            lease_owner=dispatcher.runner_id,
            lease_seconds=180,
            actor_principal_id=dispatcher.runner_id,
            actor_session_id=dispatcher.runner_session,
        )
    assert claim is not None
    assert claim.task.status is WorkBoardStatus.running
    assert claim.attempt.ended_at is None
    return claim


@pytest.mark.skipif(
    os.environ.get("SERAPH_RUN_REAL_PROCEDURE_V2_BROWSER") != "1",
    reason="real Chromium procedure proof is an explicit opt-in integration test",
)
async def test_real_parent_to_native_browser_leaf_uses_durable_child_and_readback(
    async_db,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reviewed parent invokes one native Browser root end to end."""

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("Playwright is not installed")

    owner = WorkBoardOwner(
        principal_id="operator:test-bypass",
        session_id="test-auth-bypass",
    )
    goal_id = "goal-procedure-v2-browser-proof"
    parent_input = {
        "routine_id": "routine-browser-proof-v2",
        "version": 1,
        "expected_routine_revision": 1,
        "goal_id": goal_id,
        "expected_goal_revision": 1,
        "parameters": {"goal_id": goal_id, "expected_goal_revision": 1},
        "invocation_uuid": str(uuid.uuid4()),
    }
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))

    async with async_db() as db:
        db.add(
            Goal(
                id=goal_id,
                title="Procedure v2 browser proof",
                status="active",
                revision=1,
                owner_principal_id=owner.principal_id,
                owner_session_id=owner.session_id,
                admission_budget_json=serialize_admission_budget(
                    GoalAdmissionBudget(
                        max_outstanding_jobs=1,
                        max_attempts=1,
                        max_runtime_seconds=180,
                    )
                ),
            )
        )
        await db.flush()
        source = await prepare_input_artifact(
            db,
            owner,
            WorkBoardInputArtifactCreate(
                schema_version=1,
                capability_id="browser.public-task.v1",
                goal_id=goal_id,
                goal_revision=1,
                input=_input(),
                idempotency_key="browser-proof-source",
            ),
        )
        parent_artifact = await prepare_input_artifact(
            db,
            owner,
            WorkBoardInputArtifactCreate(
                schema_version=1,
                capability_id="guardian-routine.v2",
                goal_id=goal_id,
                goal_revision=1,
                input=parent_input,
                idempotency_key="browser-proof-parent",
            ),
        )

    jobs = DurableJobRepository()
    repository = WorkBoardRepository()
    dispatcher = WorkBoardDispatcher(
        repository=repository,
        jobs=jobs,
        session_provider=async_db,
    )
    claim = await _create_parent_claim(
        async_db=async_db,
        repository=repository,
        owner=owner,
        goal_id=goal_id,
        parent_input=parent_input,
        parent_artifact_id=parent_artifact.artifact_id,
        dispatcher=dispatcher,
    )

    descriptor = _descriptor(
        source_ref=source.typed_input_ref,
        source_digest=source.typed_input_digest,
        goal_id=goal_id,
        invocation_uuid=parent_input["invocation_uuid"],
    )

    import src.browser.task_runner as task_runner_module
    from src.workflows import procedure_v2_runtime as runtime_module

    async def resolve_descriptor(*_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        return descriptor

    monkeypatch.setattr(runtime_module.procedure_v2_runtime, "resolver", resolve_descriptor)

    real_runner = task_runner_module.BrowserTaskRunner
    responses = _html_responses()

    async def fixture_fetch(request: PinnedBrowserRequest) -> PinnedBrowserResponse:
        return responses[request.url]

    async def fixture_policy(url: str, **_: Any) -> Any:
        from src.security.site_policy import SiteAccessDecision

        assert url.startswith("https://fixture.example/")
        return SiteAccessDecision(allowed=True, hostname="fixture.example")

    async def fixture_resolver(*_: Any) -> list[str]:
        return ["93.184.216.34"]

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        def runner_factory(**kwargs: Any) -> Any:
            kwargs["browser_launcher"] = lambda: browser
            kwargs["transport_factory"] = lambda: PinnedBrowserTransport(
                resolver=fixture_resolver,
                injected_fetch=fixture_fetch,
                site_policy=fixture_policy,
            )
            kwargs["workspace_root"] = tmp_path
            return real_runner(**kwargs)

        monkeypatch.setattr(task_runner_module, "BrowserTaskRunner", runner_factory)
        result = await dispatcher._admit_execute_direct(
            claim,
            parent_input,
            runtime_seconds=180,
        )

    assert result["admitted"] is True, result
    assert result["completed"] is True, result

    parent_job_id = f"procedure-v2:{parent_input['routine_id']}:{parent_input['version']}:{parent_input['invocation_uuid']}"
    child_job_id = deterministic_child_job_id(
        parent_job_id,
        "public-browser-check",
        1,
        "public_browser_check",
    )
    parent_projection = await jobs.get_job(parent_job_id)
    child_projection = await jobs.get_job(child_job_id)
    assert parent_projection is not None and parent_projection["status"] == "succeeded"
    assert child_projection is not None and child_projection["status"] == "succeeded"
    assert child_projection["root_run_identity"] == parent_job_id
    assert child_projection["parent_job_id"] == parent_job_id
    assert child_projection["parent_fencing_token"] > 0
    assert child_projection["declared_authority"]["routine_step_id"] == "public_browser_check"

    async with async_db() as db:
        tasks = list(
            (
                await db.execute(
                    select(WorkBoardTask).where(
                        WorkBoardTask.owner_principal_id == owner.principal_id,
                        WorkBoardTask.owner_session_id == owner.session_id,
                    )
                )
            ).scalars().all()
        )
        attempts = list(
            (
                await db.execute(
                    select(WorkBoardAttempt).where(
                        WorkBoardAttempt.task_id.in_([task.task_id for task in tasks]),
                    )
                )
            ).scalars().all()
        )
        parent_tasks = [task for task in tasks if task.capability_id == "guardian-routine.v2"]
        child_tasks = [task for task in tasks if task.capability_id == "browser.public-task.v1"]

    assert len(parent_tasks) == 1
    assert len(child_tasks) == 1
    assert parent_tasks[0].status is WorkBoardStatus.done
    assert child_tasks[0].status is WorkBoardStatus.done
    child_attempt = next(item for item in attempts if item.task_id == child_tasks[0].task_id)
    assert child_attempt.workflow_run_id == child_job_id
    assert child_attempt.ended_at is not None

    child_artifact = child_projection["artifacts"][-1]
    assert child_artifact["artifact_type"] == "browser_public_task_result"
    assert child_artifact["exists"] is True
    artifact_path = tmp_path / child_artifact["file_path"]
    assert artifact_path.is_file()
    assert child_projection["effects"]
    assert any(
        effect.get("receipt_kind") == "readback"
        and effect.get("status") == "succeeded"
        and effect.get("details", {}).get("verified") is True
        for effect in child_projection["effects"]
        if isinstance(effect, Mapping)
    )
    assert any(
        effect.get("effect_type") == "browser_context_cleanup"
        and effect.get("status") == "succeeded"
        and effect.get("details", {}).get("memory_status") == "no_learning"
        for effect in child_projection["effects"]
        if isinstance(effect, Mapping)
    )
    admitted_checkpoint = next(
        checkpoint
        for checkpoint in parent_projection.get("checkpoints", [])
        if isinstance(checkpoint, Mapping)
        and checkpoint.get("checkpoint_id") == "procedure-v2:step:public_browser_check:admitted"
    )
    admitted_payload = admitted_checkpoint["payload"]
    assert admitted_payload["child_job_id"] == child_job_id
    assert admitted_payload["child_task_id"] == child_tasks[0].task_id
    assert admitted_payload["child_attempt_id"] == child_attempt.attempt_id
    assert admitted_payload["child_input_artifact_id"] == child_tasks[0].input_artifact_id
    assert admitted_payload["child_capability_id"] == "browser.public-task.v1"
    settled_checkpoint = next(
        checkpoint
        for checkpoint in parent_projection.get("checkpoints", [])
        if isinstance(checkpoint, Mapping)
        and checkpoint.get("checkpoint_id") == "procedure-v2:step:public_browser_check:settled"
    )
    settled_payload = settled_checkpoint["payload"]
    assert settled_payload["child_task_id"] == child_tasks[0].task_id
    assert settled_payload["child_attempt_id"] == child_attempt.attempt_id
    assert settled_payload["artifact_ref"] == child_artifact["file_path"]
    assert settled_payload["readback_id"]


@pytest.mark.parametrize("tamper", ["missing", "bytes", "readback", "cleanup"])
async def test_native_browser_replay_requires_intact_artifact_readback_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str | None,
) -> None:
    """A cached child is replayable only with native terminal proof intact."""

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    parent, child, checkpoint, child_job_id, step = _browser_replay_projection(workspace_root=tmp_path)
    if tamper == "missing":
        (tmp_path / child["artifacts"][0]["file_path"]).unlink()
    elif tamper == "bytes":
        (tmp_path / child["artifacts"][0]["file_path"]).write_bytes(b"tampered")
    elif tamper == "readback":
        child["effects"][0]["readback_id"] = "readback-tampered"
    elif tamper == "cleanup":
        child["effects"] = child["effects"][:1]

    runtime = ProcedureV2Runtime(jobs=object())

    async def canonical_replay_binding(**_kwargs: Any) -> Mapping[str, Any]:
        return {"verified": True}

    runtime.replay_binding_verifier = canonical_replay_binding
    proof = await runtime._native_terminal_replay_proof(
        parent=parent,
        child=child,
        checkpoint=checkpoint,
        step=step,
        expected_child_id=child_job_id,
    )
    if tamper is None:
        assert proof is not None
        assert proof["readback_id"].startswith("readback-")
    else:
        assert proof is None


async def test_native_browser_restart_replay_does_not_call_leaf_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verified cached native success is adopted without repeating browser I/O."""

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    parent, child, checkpoint, child_job_id, step = _browser_replay_projection(workspace_root=tmp_path)
    parent["checkpoints"] = [checkpoint]
    descriptor = _descriptor(
        source_ref="workspace-json:artifacts/work-board/inputs/replay.json",
        source_digest="a" * 64,
        goal_id="goal-replay-proof",
        invocation_uuid="replay-proof",
    )

    class FakeJobs:
        leaf_calls = 0

        async def get_job(self, job_id: str) -> Mapping[str, Any] | None:
            if job_id == parent["job_id"]:
                return parent
            if job_id == child_job_id:
                return child
            return None

        async def record_effect(self, *_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
            return {**parent, "revision": 2}

        async def record_readback(self, *_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
            return {**parent, "revision": 3}

        async def transition_job(self, *_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
            return {**parent, "status": "succeeded", "revision": 4}

    jobs = FakeJobs()

    async def unexpected_leaf(*_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        jobs.leaf_calls += 1
        raise AssertionError("restart replay repeated the native leaf")

    runtime = ProcedureV2Runtime(
        jobs=jobs,
        leaf_executors={"browser.public-task.v1": unexpected_leaf},
    )

    async def canonical_replay_binding(**_kwargs: Any) -> Mapping[str, Any]:
        return {"verified": True}

    runtime.replay_binding_verifier = canonical_replay_binding
    result = await runtime.execute_parent(parent["job_id"], descriptor=descriptor)
    assert result["status"] == "succeeded"
    assert result["outcomes"][0]["replayed"] is True
    assert jobs.leaf_calls == 0


async def test_native_replay_requires_live_board_validator_before_terminal_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale/cancelled parent cannot adopt a cached native child."""

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    parent, child, checkpoint, child_job_id, step = _browser_replay_projection(workspace_root=tmp_path)
    calls: list[str] = []

    async def cancelled_parent(**_kwargs: Any) -> None:
        calls.append("revalidated")
        return None

    runtime = ProcedureV2Runtime(jobs=object())
    runtime.replay_binding_verifier = cancelled_parent
    assert await runtime._native_terminal_replay_proof(
        parent=parent,
        child=child,
        checkpoint=checkpoint,
        step=step,
        expected_child_id=child_job_id,
    ) is None
    assert calls == ["revalidated"]


async def test_native_replay_without_canonical_board_verifier_fails_closed_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing dispatcher trust seam cannot adopt a cached native result."""

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    parent, child, checkpoint, child_job_id, step = _browser_replay_projection(workspace_root=tmp_path)
    from src.browser.task_runner import BrowserTaskRunner

    contacted = False

    def unexpected_browser_readback(*_args: Any, **_kwargs: Any) -> None:
        nonlocal contacted
        contacted = True
        raise AssertionError("missing canonical verifier reached native readback")

    monkeypatch.setattr(BrowserTaskRunner, "_terminal_replay_proof", unexpected_browser_readback)
    runtime = ProcedureV2Runtime(jobs=object())
    assert await runtime._native_terminal_replay_proof(
        parent=parent,
        child=child,
        checkpoint=checkpoint,
        step=step,
        expected_child_id=child_job_id,
    ) is None
    assert contacted is False


async def test_browser_replay_revalidates_real_sqlite_board_rows_and_cancel_before_io(
    async_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart adoption fails closed when canonical Board rows change or cancel."""

    from src.work_board.dispatcher import WorkBoardDispatcher

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    parent, child, checkpoint, child_job_id, step = _browser_replay_projection(workspace_root=tmp_path)
    owner = "operator:replay"
    session = "session:replay"
    goal_id = "goal-replay-proof"
    parent_task_id = "routine-task-replay"
    parent_attempt_id = "routine-attempt-replay"
    child_task_id = "browser-task-replay"
    child_attempt_id = "browser-attempt-replay"
    artifact_id = "browser-input-replay"
    input_digest = "a" * 64
    now = datetime.now(timezone.utc)
    parent["declared_authority"].update(
        {
            "board_task_id": parent_task_id,
            "board_attempt_id": parent_attempt_id,
            "board_task_revision": 3,
            "board_fencing_token": 11,
        }
    )
    parent["inputs"] = {
        "plan": parent["declared_authority"].get("plan") or _descriptor(
            source_ref="workspace-json:artifacts/work-board/inputs/replay.json",
            source_digest=input_digest,
            goal_id=goal_id,
            invocation_uuid="replay-proof",
        )["plan"],
        "plan_digest": parent["declared_authority"]["plan_digest"],
        "executable_steps": {
            "public_browser_check": {
                "typed_input_ref": "workspace-json:artifacts/work-board/inputs/replay.json",
                "typed_input_digest": input_digest,
            }
        },
    }

    class Jobs:
        def __init__(self) -> None:
            self.parent_reads = 0

        async def get_job(self, job_id: str) -> Mapping[str, Any] | None:
            if job_id == parent["job_id"]:
                self.parent_reads += 1
                return parent
            return None

    async with async_db() as db:
        db.add(
            Goal(
                id=goal_id,
                title="Replay proof goal",
                status="active",
                revision=1,
                owner_principal_id=owner,
                owner_session_id=session,
            )
        )
        db.add(
            WorkBoardTask(
                task_id=parent_task_id,
                owner_principal_id=owner,
                owner_session_id=session,
                origin_session_id=session,
                goal_id=goal_id,
                goal_revision=1,
                title="Routine replay parent",
                capability_id="guardian-routine.v2",
                executor_id="seraph-work-board:guardian-routine.v2",
                idempotency_key=parent_task_id,
                status=WorkBoardStatus.running,
                task_revision=3,
            )
        )
        await db.flush()
        db.add(
            WorkBoardAttempt(
                attempt_id=parent_attempt_id,
                task_id=parent_task_id,
                # The parent Board attempt may be linked to the durable root
                # by the production admission transaction. This focused
                # validator fixture proves the Board fence without creating
                # a second durable workflow row.
                workflow_run_id=None,
                task_revision_at_claim=3,
                lease_owner="browser-replay-runner",
                lease_expires_at=now + timedelta(minutes=5),
                fencing_token=11,
                executor_id="seraph-work-board:guardian-routine.v2",
            )
        )
        db.add(
            WorkBoardInputArtifact(
                artifact_id=artifact_id,
                owner_principal_id=owner,
                owner_session_id=session,
                goal_id=goal_id,
                goal_revision=1,
                capability_id="browser.public-task.v1",
                capability_version="1",
                idempotency_key="replay-input",
                payload_sha256=input_digest,
                typed_input_ref="workspace-json:artifacts/work-board/inputs/replay.json",
                state="consumed",
                bound_task_id=child_task_id,
                bound_task_revision=3,
                expires_at=now + timedelta(hours=1),
            )
        )
        db.add(
            WorkBoardTask(
                task_id=child_task_id,
                owner_principal_id=owner,
                owner_session_id=session,
                origin_session_id=session,
                goal_id=goal_id,
                goal_revision=1,
                title="Browser replay child",
                capability_id="browser.public-task.v1",
                input_artifact_id=artifact_id,
                typed_input_ref="workspace-json:artifacts/work-board/inputs/replay.json",
                typed_input_digest=input_digest,
                executor_id="seraph-work-board:browser.public-task.v1",
                idempotency_key=child_task_id,
                status=WorkBoardStatus.done,
                task_revision=3,
            )
        )
        await db.flush()
        db.add(
            WorkBoardAttempt(
                attempt_id=child_attempt_id,
                task_id=child_task_id,
                task_revision_at_claim=3,
                fencing_token=11,
                executor_id="seraph-work-board:browser.public-task.v1",
                ended_at=now,
                outcome="verified",
            )
        )

    dispatcher = WorkBoardDispatcher(
        session_provider=async_db,
        jobs=Jobs(),
        runner_id="browser-replay-runner",
    )
    valid = await dispatcher._validate_v2_replay_binding(
        parent=parent,
        child=child,
        checkpoint=checkpoint,
        step=step,
        expected_child_id=child_job_id,
    )
    assert valid == {"verified": True, "parent_board_revalidated": True, "child_board_revalidated": True}

    async with async_db() as db:
        row = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == child_task_id))).scalars().one()
        row.typed_input_digest = "b" * 64
    with pytest.raises(DurableJobError, match="procedure_child_binding_stale"):
        await dispatcher._validate_v2_replay_binding(
            parent=parent,
            child=child,
            checkpoint=checkpoint,
            step=step,
            expected_child_id=child_job_id,
        )

    async with async_db() as db:
        row = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == child_task_id))).scalars().one()
        row.typed_input_digest = input_digest
        artifact = (await db.execute(select(WorkBoardInputArtifact).where(WorkBoardInputArtifact.artifact_id == artifact_id))).scalars().one()
        await db.delete(artifact)
    with pytest.raises(DurableJobError, match="procedure_child_binding_missing"):
        await dispatcher._validate_v2_replay_binding(
            parent=parent,
            child=child,
            checkpoint=checkpoint,
            step=step,
            expected_child_id=child_job_id,
        )

    async with async_db() as db:
        attempt = (await db.execute(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == parent_attempt_id))).scalars().one()
        attempt.cancel_requested_at = datetime.now(timezone.utc)
    with pytest.raises(DurableJobError, match="procedure_parent_board_binding_stale"):
        await dispatcher._validate_v2_replay_binding(
            parent=parent,
            child=child,
            checkpoint=checkpoint,
            step=step,
            expected_child_id=child_job_id,
        )


async def test_missing_recorded_native_child_quarantines_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    parent, _child, checkpoint, child_job_id, _step = _browser_replay_projection(workspace_root=tmp_path)
    parent["checkpoints"] = [checkpoint]
    descriptor = _descriptor(
        source_ref="workspace-json:artifacts/work-board/inputs/replay.json",
        source_digest="a" * 64,
        goal_id="goal-replay-proof",
        invocation_uuid="replay-proof",
    )

    class MissingChildJobs:
        async def get_job(self, job_id: str) -> Mapping[str, Any] | None:
            return parent if job_id == parent["job_id"] else None

        async def transition_job(self, *_args: Any, **kwargs: Any) -> Mapping[str, Any]:
            return {
                **parent,
                "status": kwargs.get("to_status", "unknown_external_effect"),
                "failure_reason": kwargs.get("reason"),
            }

    runtime = ProcedureV2Runtime(jobs=MissingChildJobs())
    result = await runtime.execute_parent(parent["job_id"], descriptor=descriptor)
    assert result["status"] == "unknown_external_effect"
    assert result["reason_code"] == "procedure_child_missing"
    assert result["child_job_id"] == child_job_id


def _watch_replay_projection() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], str]:
    parent_id = "procedure-v2:watch-proof:1:parent"
    occurrence_id = deterministic_child_job_id(parent_id, "source-watch", 1, "source_watch")
    watch_id = "watch-replay-proof"
    child_id = f"source-watch:{watch_id}:{occurrence_id}"
    owner = "operator:watch-replay"
    session = "session:watch-replay"
    goal_id = "goal-watch-replay"
    step = {
        "step_id": "source_watch",
        "capability_id": "guardian.research-watch.v1",
        "capability_version": "1",
        "typed_input_ref": "workspace-json:procedure/watch.json",
        "typed_input_digest": "c" * 64,
    }
    child = {
        "job_id": child_id,
        "run_identity": child_id,
        "root_run_identity": parent_id,
        "parent_run_identity": parent_id,
        "parent_job_id": parent_id,
        "parent_fencing_token": 9,
        "status": "succeeded",
        "job_kind": "guardian_source_watch",
        "capability_version": "1",
        "goal_id": goal_id,
        "goal_revision": 2,
        "plan_revision": 4,
        "inputs": {"watch_id": watch_id, "occurrence_id": occurrence_id},
        "declared_authority": {
            "capability_id": "guardian.research-watch.v1",
            "capability_version": "1",
            "watch_id": watch_id,
            "occurrence_id": occurrence_id,
            "goal_id": goal_id,
            "goal_revision": 2,
            "goal_owner_principal_id": owner,
            "goal_owner_session_id": session,
            "plan_revision": 4,
            "routine_parent_job_id": parent_id,
            "routine_parent_fencing_token": 9,
            "routine_step_id": "source_watch",
        },
    }
    parent = {
        "job_id": parent_id,
        "run_identity": parent_id,
        "status": "running",
        "lease": {"owner": "parent-runner", "fencing_token": 9},
    }
    refs = _watch_child_refs(
        {**child, "_v2_watch_binding": {"parent_job_id": parent_id, "parent_fencing_token": 9, "step_id": "source_watch", "occurrence_id": occurrence_id}},
        {"status": "succeeded", "packet_id": "packet-watch-replay", "artifact_refs": [{"artifact_id": "art-watch", "file_path": "guardian/source-watches/watch-replay-proof/packets/packet-watch-replay.md", "content_sha256": "d" * 64, "artifact_type": "guardian_decision_dossier", "verified": True}]},
    )
    checkpoint = {"checkpoint_id": "procedure-v2:step:source_watch:admitted", "safe": True, "payload": refs}
    return parent, child, checkpoint, step, child_id


async def test_native_watch_replay_uses_typed_packet_proof_without_board_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, child, checkpoint, step, child_id = _watch_replay_projection()
    calls: list[dict[str, Any]] = []
    from src.guardian.source_watch import source_watch_service

    async def verify(**kwargs: Any) -> Mapping[str, Any]:
        calls.append(kwargs)
        return {"status": "succeeded", "packet_id": "packet-watch-replay", "verified": True, "no_change": False}

    monkeypatch.setattr(source_watch_service, "verify_procedure_replay", verify)
    runtime = ProcedureV2Runtime(jobs=object())

    async def canonical_replay_binding(**_kwargs: Any) -> Mapping[str, Any]:
        return {"verified": True}

    runtime.replay_binding_verifier = canonical_replay_binding
    proof = await runtime._native_terminal_replay_proof(
        parent=parent,
        child=child,
        checkpoint=checkpoint,
        step=step,
        expected_child_id=child_id,
    )
    assert proof == {"status": "succeeded", "packet_id": "packet-watch-replay", "verified": True, "no_change": False}
    assert calls and calls[0]["watch_id"] == "watch-replay-proof"
    assert calls[0]["expected_plan_revision"] == 4


@pytest.mark.parametrize("cancel_after_no_change_proof", [False, True])
async def test_watch_no_change_replay_skips_browser_without_leaf_contact(
    monkeypatch: pytest.MonkeyPatch,
    cancel_after_no_change_proof: bool,
) -> None:
    """Restart adoption re-proves Watch before skip, including cancellation."""

    from src.guardian.source_watch import source_watch_service

    parent, child, _checkpoint, _old_step, _old_child_id = _watch_replay_projection()
    parent_id = parent["job_id"]
    watch_id = "watch-replay-proof"
    occurrence_id = deterministic_child_job_id(
        parent_id,
        "watch-and-public-browser",
        1,
        "source_watch",
    )
    child_id = f"source-watch:{watch_id}:{occurrence_id}"
    child = {
        **child,
        "job_id": child_id,
        "run_identity": child_id,
        "root_run_identity": parent_id,
        "parent_run_identity": parent_id,
        "parent_job_id": parent_id,
        "inputs": {"watch_id": watch_id, "occurrence_id": occurrence_id},
        "declared_authority": {
            **dict(child["declared_authority"]),
            "occurrence_id": occurrence_id,
        },
    }
    watch_binding = {
        "parent_job_id": parent_id,
        "parent_fencing_token": 9,
        "step_id": "source_watch",
        "occurrence_id": occurrence_id,
    }
    checkpoint = {
        "checkpoint_id": "procedure-v2:step:source_watch:admitted",
        "safe": True,
        "payload": _watch_child_refs(
            {**child, "_v2_watch_binding": watch_binding},
            {
                "status": "skipped_verified",
                "packet_id": "packet-watch-replay",
                "artifact_refs": [],
                "no_change": True,
            },
        ),
    }
    plan = build_procedure_plan(
        "watch-and-public-browser",
        step_inputs={
            "source_watch": {
                "typed_input_ref": "workspace-json:procedure/watch.json",
                "typed_input_digest": "c" * 64,
            },
            "public_browser_check": {
                "typed_input_ref": "workspace-json:procedure/browser.json",
                "typed_input_digest": "d" * 64,
            },
        },
    ).model_dump(mode="json")
    descriptor = {
        "routine_id": "routine-watch-replay-v2",
        "version": 1,
        "goal_id": "goal-watch-replay",
        "goal_revision": 2,
        "routine_revision": 1,
        "plan": plan,
        "plan_digest": plan_digest(plan),
        "executable_steps": {
            "source_watch": {"watch_id": watch_id, "expected_plan_revision": 4},
            "public_browser_check": {
                "typed_input_ref": "workspace-json:procedure/browser.json",
                "typed_input_digest": "d" * 64,
            },
        },
    }
    parent = {
        **parent,
        "goal_id": "goal-watch-replay",
        "goal_revision": 2,
        "job_kind": "guardian_routine_v2",
        "capability_version": "guardian-routine.v2",
        "owner": {"principal_id": "operator:watch-replay"},
        "operator_session_id": "session:watch-replay",
        "deadline_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "revision": 1,
        "declared_authority": {
            "template_id": "watch-and-public-browser",
            "plan_digest": descriptor["plan_digest"],
            "principal": "operator:watch-replay",
            "session_id": "session:watch-replay",
        },
        "checkpoints": [checkpoint],
    }

    calls: list[dict[str, Any]] = []

    async def verify(**kwargs: Any) -> Mapping[str, Any]:
        calls.append(kwargs)
        return {
            "status": "skipped_verified",
            "packet_id": "packet-watch-replay",
            "verified": True,
            "no_change": True,
        }

    monkeypatch.setattr(source_watch_service, "verify_procedure_replay", verify)

    class FakeJobs:
        leaf_calls = 0

        async def get_job(self, job_id: str) -> Mapping[str, Any] | None:
            if job_id == parent_id:
                return parent
            if job_id == child_id:
                return child
            return None

        async def record_checkpoint(self, _job_id: str, *, checkpoint_id: str, checkpoint_payload: Mapping[str, Any], **_: Any) -> Mapping[str, Any]:
            parent["checkpoints"] = [
                item
                for item in parent.get("checkpoints", [])
                if item.get("checkpoint_id") != checkpoint_id
            ]
            parent["checkpoints"].append(
                {"checkpoint_id": checkpoint_id, "safe": True, "payload": dict(checkpoint_payload)}
            )
            parent["revision"] = int(parent.get("revision") or 0) + 1
            return parent

        async def record_effect(self, *_args: Any, **_: Any) -> Mapping[str, Any]:
            parent["revision"] = int(parent.get("revision") or 0) + 1
            return {**parent, "revision": parent["revision"]}

        async def record_readback(self, *_args: Any, **_: Any) -> Mapping[str, Any]:
            parent["revision"] = int(parent.get("revision") or 0) + 1
            return {**parent, "revision": parent["revision"]}

        async def transition_job(self, _job_id: str, to_status: str, **_: Any) -> Mapping[str, Any]:
            parent["status"] = to_status
            parent["revision"] = int(parent.get("revision") or 0) + 1
            return parent

    jobs = FakeJobs()

    async def unexpected_browser(*_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        jobs.leaf_calls += 1
        raise AssertionError("replayed Watch no-change must not execute Browser")

    runtime = ProcedureV2Runtime(
        jobs=jobs,
        leaf_executors={"browser.public-task.v1": unexpected_browser},
    )

    async def canonical_replay_binding(**_kwargs: Any) -> Mapping[str, Any]:
        return {"verified": True}

    runtime.replay_binding_verifier = canonical_replay_binding
    guard_calls = 0

    async def parent_guard(**_kwargs: Any) -> bool:
        nonlocal guard_calls
        guard_calls += 1
        return not (cancel_after_no_change_proof and guard_calls >= 2)

    runtime.runtime_controls = parent_guard
    result = await runtime.execute_parent(parent_id, descriptor=descriptor)
    assert jobs.leaf_calls == 0
    assert len(calls) == 2
    if cancel_after_no_change_proof:
        assert result["status"] == "unknown_external_effect"
        assert result["reason_code"] == "procedure_parent_authority_stale"
        assert parent["status"] == "unknown_external_effect"
        assert not any(
            item.get("checkpoint_id") == "procedure-v2:step:public_browser_check:skipped"
            for item in parent["checkpoints"]
        )
    else:
        assert result["status"] == "succeeded"
        assert result["outcomes"][0]["replayed"] is True
        assert result["outcomes"][0]["no_change"] is True
        assert result["outcomes"][1] == {
            "step_id": "public_browser_check",
            "status": "skipped",
            "reason_code": "watch_no_material_change",
            "skipped": True,
            "watch_child_job_id": child_id,
            "memory_status": "no_learning",
        }
        assert any(
            item.get("checkpoint_id") == "procedure-v2:step:public_browser_check:skipped"
            for item in parent["checkpoints"]
        )
        assert not any(
            item.get("checkpoint_id") == "procedure-v2:step:public_browser_check:admitted"
            for item in parent["checkpoints"]
        )


async def test_native_watch_replay_rejects_tampered_identity_and_deleted_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, child, checkpoint, step, child_id = _watch_replay_projection()
    from src.guardian.source_watch import source_watch_service

    contacted = False

    async def verify(**_kwargs: Any) -> Mapping[str, Any]:
        nonlocal contacted
        contacted = True
        raise AssertionError("tampered Watch replay contacted its adapter")

    monkeypatch.setattr(source_watch_service, "verify_procedure_replay", verify)
    checkpoint["payload"]["watch_id"] = "watch-forged"
    assert await ProcedureV2Runtime(jobs=object())._native_terminal_replay_proof(
        parent=parent,
        child=child,
        checkpoint=checkpoint,
        step=step,
        expected_child_id=child_id,
    ) is None
    assert contacted is False


async def test_watch_admission_rejects_revision_alias_before_native_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.guardian.source_watch import source_watch_service

    contacted = False

    async def run_watch(*_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        nonlocal contacted
        contacted = True
        raise AssertionError("legacy revision alias contacted Source Watch")

    monkeypatch.setattr(source_watch_service, "run_watch", run_watch)

    class Jobs:
        async def get_job(self, _job_id: str) -> Mapping[str, Any]:
            return {
                "job_id": "parent-watch-alias",
                "run_identity": "parent-watch-alias",
                "status": "running",
                "lease": {"owner": "runner", "fencing_token": 1},
                "deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
            }

    runtime = ProcedureV2Runtime(jobs=Jobs())
    parent = {
        "job_id": "parent-watch-alias",
        "status": "running",
        "lease": {"owner": "runner", "fencing_token": 1},
        "deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
        "owner": {"principal_id": "operator:watch", "kind": "user"},
        "operator_session_id": "session:watch",
    }
    step = {"step_id": "source_watch", "capability_id": "guardian.research-watch.v1", "capability_version": "1"}
    descriptor = {"executable_steps": {"source_watch": {"watch_id": "watch", "expected_watch_revision": 3}}}
    with pytest.raises(ProcedureV2RuntimeError, match="watch_input_binding_invalid"):
        await runtime._admit_child(parent, step=step, descriptor=descriptor, child_id="child")
    assert contacted is False


def _watch_replay_job_and_rows(*, watch_id: str, job_id: str, owner: str, session: str, goal_id: str, plan_revision: int = 4) -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace, dict[str, Any]]:
    from src.guardian.source_watch import _dump, _sha

    watch = SimpleNamespace(
        id=watch_id,
        goal_id=goal_id,
        owner_principal_id=owner,
        owner_session_id=session,
        state="active",
        goal_revision=1,
        plan_revision=plan_revision,
        sources_json="[]",
        source_set_digest=_sha(_dump([])),
        criteria_digest="criteria-replay",
    )
    goal = SimpleNamespace(
        id=goal_id,
        owner_principal_id=owner,
        owner_session_id=session,
        revision=1,
        status="active",
    )
    packet = SimpleNamespace(
        id="packet-watch-material",
        source_watch_id=watch_id,
        watch_id=watch_id,
        goal_id=goal_id,
        goal_revision=1,
        plan_revision=plan_revision,
        run_identity=job_id,
        criteria_digest="criteria-replay",
        proposal_text="verified dossier",
        task_text="verified task",
        dossier_sha256=None,
        task_sha256=None,
        status="succeeded",
        verification_status="passed",
        outcome_json="{}",
        observed_checkpoint_json='{"schema":"seraph.guardian.source-observation.v1","sources":[]}',
    )
    occurrence_id = "occurrence"
    expected_job_id = f"source-watch:{watch_id}:{occurrence_id}"
    assert job_id == expected_job_id
    job = {
        "job_id": job_id,
        "run_identity": job_id,
        "job_kind": "guardian_source_watch",
        "status": "succeeded",
        "goal_id": goal_id,
        "goal_revision": 1,
        "plan_revision": plan_revision,
        "inputs": {"watch_id": watch_id, "occurrence_id": occurrence_id},
        "declared_authority": {
            "watch_id": watch_id,
            "occurrence_id": occurrence_id,
            "goal_id": goal_id,
            "goal_revision": 1,
            "plan_revision": plan_revision,
            "goal_owner_principal_id": owner,
            "goal_owner_session_id": session,
            "source_set_digest": watch.source_set_digest,
            "criteria_digest": watch.criteria_digest,
        },
        "checkpoints": [],
        "effects": [],
    }
    return watch, goal, packet, job


async def test_source_watch_material_replay_rechecks_files_and_rejects_tamper_or_delete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.guardian.source_watch as source_watch_module

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    watch_id = "watch-material-readback"
    job_id = "source-watch:watch-material-readback:occurrence"
    watch, goal, packet, job = _watch_replay_job_and_rows(
        watch_id=watch_id,
        job_id=job_id,
        owner="operator:watch-material",
        session="session:watch-material",
        goal_id="goal-watch-material",
    )
    dossier_path, task_path = source_watch_module.SourceWatchService._packet_output_paths(watch_id, packet.id)
    for path, content in ((dossier_path, packet.proposal_text), (task_path, packet.task_text)):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    dossier_record = build_artifact_record(
        file_path=dossier_path,
        artifact_type="guardian_decision_dossier",
        producer=source_watch_module.CAPABILITY_ID,
        run_id=job_id,
        session_id=watch.owner_session_id,
        content=packet.proposal_text,
        trust_boundary="local_workspace",
    )
    task_record = build_artifact_record(
        file_path=task_path,
        artifact_type="guardian_local_task",
        producer=source_watch_module.CAPABILITY_ID,
        run_id=job_id,
        session_id=watch.owner_session_id,
        content=packet.task_text,
        trust_boundary="local_workspace",
    )
    job["effects"] = [
        {
            "receipt_kind": "readback",
            "status": "succeeded",
            "target_path": item["file_path"],
            "readback_id": f"readback:{index}",
            "details": {"verified": True},
        }
        for index, item in enumerate((dossier_record, task_record), start=1)
    ]

    class Result:
        def __init__(self, item: Any):
            self.item = item

        def scalars(self) -> "Result":
            return self

        def first(self) -> Any:
            return self.item

    class DB:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, _statement: Any) -> Result:
            self.calls += 1
            return Result((watch, goal, packet)[self.calls - 1])

    @asynccontextmanager
    async def get_session():
        yield DB()

    monkeypatch.setattr(source_watch_module.db_engine, "get_session", get_session)
    monkeypatch.setattr(source_watch_module.durable_job_repository, "get_job", lambda _job_id: _async_result(job))
    service = source_watch_module.SourceWatchService()
    refs = [
        {**dict(dossier_record), "verified": True},
        {**dict(task_record), "verified": True},
    ]
    verified = await service.verify_procedure_replay(
        watch_id=watch_id,
        job_id=job_id,
        expected_plan_revision=4,
        occurrence_id="occurrence",
        owner_principal_id=watch.owner_principal_id,
        owner_session_id=watch.owner_session_id,
        goal_id=watch.goal_id,
        goal_revision=1,
        terminal_status="succeeded",
        packet_id=packet.id,
        artifact_refs=refs,
        no_change=False,
    )
    assert verified and verified["verified"] is True
    (tmp_path / dossier_path).write_text("tampered")
    assert await service.verify_procedure_replay(
        watch_id=watch_id,
        job_id=job_id,
        expected_plan_revision=4,
        occurrence_id="occurrence",
        owner_principal_id=watch.owner_principal_id,
        owner_session_id=watch.owner_session_id,
        goal_id=watch.goal_id,
        goal_revision=1,
        terminal_status="succeeded",
        packet_id=packet.id,
        artifact_refs=refs,
        no_change=False,
    ) is None
    (tmp_path / dossier_path).unlink()
    assert await service.verify_procedure_replay(
        watch_id=watch_id,
        job_id=job_id,
        expected_plan_revision=4,
        occurrence_id="occurrence",
        owner_principal_id=watch.owner_principal_id,
        owner_session_id=watch.owner_session_id,
        goal_id=watch.goal_id,
        goal_revision=1,
        terminal_status="succeeded",
        packet_id=packet.id,
        artifact_refs=refs,
        no_change=False,
    ) is None


async def test_source_watch_no_change_replay_uses_packet_without_external_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.guardian.source_watch as source_watch_module

    watch_id = "watch-no-change-replay"
    job_id = "source-watch:watch-no-change-replay:occurrence"
    watch, goal, packet, job = _watch_replay_job_and_rows(
        watch_id=watch_id,
        job_id=job_id,
        owner="operator:watch-no-change",
        session="session:watch-no-change",
        goal_id="goal-watch-no-change",
    )
    packet.status = "no_change"
    packet.verification_status = "not_applicable"
    packet.outcome_json = '{"status":"no_change","memory_status":"no_learning"}'
    job["effects"] = [
        {
            "receipt_kind": "readback",
            "status": "succeeded",
            "target_path": f"source-watch:{job_id}",
            "readback_id": "readback:no-change",
            "details": {"verified": True},
        }
    ]
    class Result:
        def __init__(self, item: Any) -> None:
            self.item = item

        def scalars(self) -> "Result":
            return self

        def first(self) -> Any:
            return self.item

    class DB:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, _statement: Any) -> Result:
            self.calls += 1
            return Result((watch, goal, packet)[self.calls - 1])

    @asynccontextmanager
    async def get_session():
        yield DB()

    monkeypatch.setattr(source_watch_module.db_engine, "get_session", get_session)
    monkeypatch.setattr(source_watch_module.durable_job_repository, "get_job", lambda _job_id: _async_result(job))
    result = await source_watch_module.SourceWatchService().verify_procedure_replay(
        watch_id=watch_id,
        job_id=job_id,
        expected_plan_revision=4,
        occurrence_id="occurrence",
        owner_principal_id=watch.owner_principal_id,
        owner_session_id=watch.owner_session_id,
        goal_id=watch.goal_id,
        goal_revision=1,
        terminal_status="skipped_verified",
        packet_id=packet.id,
        artifact_refs=[],
        no_change=True,
    )
    assert result and result["no_change"] is True


async def _seed_sqlite_watch_replay(
    async_db: Any,
    *,
    watch_id: str,
    goal_id: str,
    owner: str,
    session: str,
    baseline_text: str,
) -> None:
    from src.guardian.source_watch import _dump, _sha, parse_sources

    source = parse_sources(
        [{"source_key": "local", "kind": "workspace_text", "target": "m6/native-watch.txt"}]
    )[0]
    source_projection = [
        {
            "source_key": source.source_key,
            "kind": source.kind,
            "target": source.target,
            "label": source.label,
            "priority": source.priority,
            "identity_digest": source.identity_digest,
        }
    ]
    criteria = {
        "include_terms": [],
        "exclude_terms": [],
        "min_changed_lines": 1,
        "min_changed_chars": 1,
        "max_material_sources": 1,
    }
    goal = Goal(
        id=goal_id,
        title="Native Watch replay goal",
        status="active",
        proactive_enabled=True,
        revision=1,
        owner_principal_id=owner,
        owner_session_id=session,
        admission_budget_json=json.dumps(
            {
                "reviewed_grant": True,
                "grant_id": "native-watch-replay-grant",
                "max_outstanding_jobs": 2,
                "max_attempts": 1,
                "max_runtime_seconds": 300,
            }
        ),
    )
    watch = GuardianSourceWatch(
        id=watch_id,
        goal_id=goal_id,
        owner_principal_id=owner,
        owner_session_id=session,
        state="active",
        capability_id="guardian.research-watch.v1",
        capability_version="1",
        goal_revision=1,
        plan_revision=1,
        sources_json=_dump(source_projection),
        criteria_json=_dump(criteria),
        schedule_spec_json=_dump({"cron": "*/30 * * * *", "timezone": "UTC"}),
        read_authority_json=_dump({"source_keys": [source.source_key], "grant_id": "native-watch-replay-grant"}),
        write_authority_json=_dump({"grant_id": "native-watch-replay-grant"}),
        write_mode="standing_reviewed",
        scheduled_job_id=f"scheduled:{watch_id}",
        source_set_digest=_sha(_dump(source_projection)),
        criteria_digest=_sha(_dump(criteria)),
    )
    baseline = GuardianSourceBaseline(
        watch_id=watch_id,
        source_key=source.source_key,
        kind=source.kind,
        target=source.target,
        identity_digest=source.identity_digest,
        baseline_text=baseline_text,
        baseline_sha256=_sha(baseline_text),
        state="ready",
    )
    async with async_db() as db:
        db.add(Session(id=session, owner_principal_id=owner))
        db.add(goal)
        db.add(watch)
        db.add(baseline)


async def test_native_watch_sqlite_material_restart_replay_rejects_tamper_and_delete(
    async_db: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The real Watch job/packet/artifact rows support safe restart adoption."""

    import src.guardian.source_watch as source_watch_module

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    watch_id = "watch-native-sqlite-material"
    goal_id = "goal-native-sqlite-material"
    owner = "operator:native-sqlite-material"
    session = "session:native-sqlite-material"
    await _seed_sqlite_watch_replay(
        async_db,
        watch_id=watch_id,
        goal_id=goal_id,
        owner=owner,
        session=session,
        baseline_text="old material",
    )

    async def fetcher(_source: Any) -> tuple[str, dict[str, Any]]:
        return "new material", {}

    service = source_watch_module.SourceWatchService(fetcher=fetcher)
    occurrence_id = "native-material-occurrence"
    executed = await service.run_watch(
        watch_id,
        occurrence_id=occurrence_id,
        expected_plan_revision=1,
        expected_owner_session_id=session,
    )
    assert executed["status"] == "succeeded"
    assert executed["job_id"] == f"source-watch:{watch_id}:{occurrence_id}"
    refs = executed["artifact_refs"]
    assert len(refs) == 2

    restarted = source_watch_module.SourceWatchService(fetcher=lambda *_args: (_ for _ in ()).throw(AssertionError("replay fetched source")))
    verified = await restarted.verify_procedure_replay(
        watch_id=watch_id,
        job_id=executed["job_id"],
        expected_plan_revision=1,
        occurrence_id=occurrence_id,
        owner_principal_id=owner,
        owner_session_id=session,
        goal_id=goal_id,
        goal_revision=1,
        terminal_status="succeeded",
        packet_id=executed["packet_id"],
        artifact_refs=refs,
        no_change=False,
    )
    assert verified and verified["verified"] is True

    output = tmp_path / refs[0]["file_path"]
    original = output.read_text()
    output.write_text(original + "\ntampered")
    assert await restarted.verify_procedure_replay(
        watch_id=watch_id,
        job_id=executed["job_id"],
        expected_plan_revision=1,
        occurrence_id=occurrence_id,
        owner_principal_id=owner,
        owner_session_id=session,
        goal_id=goal_id,
        goal_revision=1,
        terminal_status="succeeded",
        packet_id=executed["packet_id"],
        artifact_refs=refs,
        no_change=False,
    ) is None
    output.unlink()
    assert await restarted.verify_procedure_replay(
        watch_id=watch_id,
        job_id=executed["job_id"],
        expected_plan_revision=1,
        occurrence_id=occurrence_id,
        owner_principal_id=owner,
        owner_session_id=session,
        goal_id=goal_id,
        goal_revision=1,
        terminal_status="succeeded",
        packet_id=executed["packet_id"],
        artifact_refs=refs,
        no_change=False,
    ) is None


async def test_native_watch_sqlite_no_change_restart_replay_requires_verified_receipt(
    async_db: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.guardian.source_watch as source_watch_module

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    watch_id = "watch-native-sqlite-no-change"
    goal_id = "goal-native-sqlite-no-change"
    owner = "operator:native-sqlite-no-change"
    session = "session:native-sqlite-no-change"
    await _seed_sqlite_watch_replay(
        async_db,
        watch_id=watch_id,
        goal_id=goal_id,
        owner=owner,
        session=session,
        baseline_text="unchanged",
    )

    calls = 0

    async def fetcher(_source: Any) -> tuple[str, dict[str, Any]]:
        nonlocal calls
        calls += 1
        return "unchanged", {}

    service = source_watch_module.SourceWatchService(fetcher=fetcher)
    occurrence_id = "native-no-change-occurrence"
    executed = await service.run_watch(
        watch_id,
        occurrence_id=occurrence_id,
        expected_plan_revision=1,
        expected_owner_session_id=session,
    )
    assert executed["status"] == "no_change"
    assert calls == 1
    restarted = source_watch_module.SourceWatchService(fetcher=lambda *_args: (_ for _ in ()).throw(AssertionError("replay fetched source")))
    verified = await restarted.verify_procedure_replay(
        watch_id=watch_id,
        job_id=executed["job_id"],
        expected_plan_revision=1,
        occurrence_id=occurrence_id,
        owner_principal_id=owner,
        owner_session_id=session,
        goal_id=goal_id,
        goal_revision=1,
        terminal_status="skipped_verified",
        packet_id=executed["packet_id"],
        artifact_refs=[],
        no_change=True,
    )
    assert verified and verified["no_change"] is True


async def test_native_watch_parent_board_guard_blocks_before_source_contact(
    async_db: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cancelled/re-fenced routine parent stops a Watch before scanning."""

    import src.guardian.source_watch as source_watch_module

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    watch_id = "watch-native-parent-guard"
    goal_id = "goal-native-parent-guard"
    owner = "operator:native-parent-guard"
    session = "session:native-parent-guard"
    await _seed_sqlite_watch_replay(
        async_db,
        watch_id=watch_id,
        goal_id=goal_id,
        owner=owner,
        session=session,
        baseline_text="old material",
    )
    parent = await durable_job_repository.admit_job(
        _native_durable_parent_spec(
            job_id="native-watch-parent-guard-root",
            goal_id=goal_id,
            owner=owner,
            session=session,
        )
    )
    parent = await durable_job_repository.queue_job(parent["job_id"])
    parent = await durable_job_repository.claim_job(parent["job_id"], owner="native-parent-guard-runner")
    parent_fence = int(parent["lease"]["fencing_token"])
    source_calls = 0

    async def fetcher(_source: Any) -> tuple[str, dict[str, Any]]:
        nonlocal source_calls
        source_calls += 1
        return "new material", {}

    guard_calls = 0

    async def cancelled_parent() -> bool:
        nonlocal guard_calls
        guard_calls += 1
        return False

    result = await source_watch_module.SourceWatchService(fetcher=fetcher).run_watch(
        watch_id,
        occurrence_id="native-parent-guard-occurrence",
        expected_plan_revision=1,
        expected_owner_session_id=session,
        routine_parent_job_id=parent["job_id"],
        routine_parent_fencing_token=parent_fence,
        routine_step_id="source_watch",
        routine_parent_guard=cancelled_parent,
    )
    assert result["status"] == "blocked"
    assert result["reason_code"] == "routine_parent_board_binding_stale"
    assert guard_calls >= 1
    assert source_calls == 0
    child = await durable_job_repository.get_job(result["job_id"])
    assert child is not None
    assert child["status"] == "failed"


async def test_dispatcher_watch_parent_guard_rechecks_real_board_cancellation(
    async_db: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The production guard rejects a cancelled canonical parent attempt."""

    from src.work_board.dispatcher import WorkBoardDispatcher

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    owner = "operator:test-bypass"
    session = "test-auth-bypass"
    goal_id = "goal-dispatcher-watch-guard"
    task_id = "routine-task-dispatcher-watch-guard"
    attempt_id = "routine-attempt-dispatcher-watch-guard"
    artifact_idempotency = "dispatcher-watch-guard-input"
    input_payload = {
        "routine_id": "routine-browser-proof-v2",
        "version": 1,
        "expected_routine_revision": 1,
        "goal_id": goal_id,
        "expected_goal_revision": 1,
        "parameters": {},
        "invocation_uuid": "dispatcher-watch-guard",
    }
    now = datetime.now(timezone.utc)
    async with async_db() as db:
        db.add(
            Goal(
                id=goal_id,
                title="Dispatcher Watch guard goal",
                status="active",
                revision=1,
                owner_principal_id=owner,
                owner_session_id=session,
            )
        )
        await db.flush()
        metadata = await prepare_input_artifact(
            db,
            WorkBoardOwner(principal_id=owner, session_id=session),
            WorkBoardInputArtifactCreate(
                schema_version=1,
                capability_id="guardian-routine.v2",
                goal_id=goal_id,
                goal_revision=1,
                input=input_payload,
                idempotency_key=artifact_idempotency,
            ),
        )
        artifact = await db.get(WorkBoardInputArtifact, metadata.artifact_id)
        assert artifact is not None
        artifact.state = "bound"
        artifact.bound_task_id = task_id
        artifact.bound_task_revision = 3
        from src.work_board.input_artifacts import _metadata_digest

        artifact.revision += 1
        artifact.metadata_digest = _metadata_digest(artifact)
        db.add(
            WorkBoardTask(
                task_id=task_id,
                owner_principal_id=owner,
                owner_session_id=session,
                origin_session_id=session,
                goal_id=goal_id,
                goal_revision=1,
                title="Routine Watch parent",
                capability_id="guardian-routine.v2",
                input_artifact_id=metadata.artifact_id,
                typed_input_ref=metadata.typed_input_ref,
                typed_input_digest=metadata.typed_input_digest,
                executor_id="seraph-work-board:guardian-routine.v2",
                idempotency_key=task_id,
                status=WorkBoardStatus.running,
                task_revision=3,
            )
        )
        await db.flush()
        db.add(
            WorkBoardAttempt(
                attempt_id=attempt_id,
                task_id=task_id,
                workflow_run_id=None,
                task_revision_at_claim=3,
                lease_owner="service:work-board",
                lease_expires_at=now + timedelta(minutes=5),
                fencing_token=11,
                executor_id="seraph-work-board:guardian-routine.v2",
            )
        )

    parent_id = "procedure-v2:dispatcher-watch-guard:1:parent"
    child_id = "source-watch:watch-dispatcher-guard:occurrence"
    parent = {
        "job_id": parent_id,
        "run_identity": parent_id,
        "status": "running",
        "goal_id": goal_id,
        "goal_revision": 1,
        "owner": {"principal_id": owner},
        "operator_session_id": session,
        "lease": {"owner": "parent-runner", "fencing_token": 9},
        "declared_authority": {
            "principal": owner,
            "session_id": session,
            "board_task_id": task_id,
            "board_attempt_id": attempt_id,
            "board_task_revision": 3,
            "board_fencing_token": 11,
        },
    }
    child = {
        "job_id": child_id,
        "run_identity": child_id,
        "declared_authority": {
            "routine_parent_job_id": parent_id,
            "routine_parent_fencing_token": 9,
            "routine_step_id": "source_watch",
        },
    }

    class Jobs:
        async def get_job(self, job_id: str) -> Mapping[str, Any] | None:
            return child if job_id == child_id else parent if job_id == parent_id else None

    dispatcher = WorkBoardDispatcher(
        session_provider=async_db,
        jobs=Jobs(),
        runner_id="service:work-board",
    )
    assert await dispatcher._browser_assert_current(
        task_id=task_id,
        attempt_id=attempt_id,
        owner_principal_id=owner,
        owner_session_id=session,
        board_task_revision=3,
        board_fencing_token=11,
        input_artifact_id=metadata.artifact_id,
        durable_job_id=child_id,
        routine_parent_job_id=parent_id,
        routine_parent_fencing_token=9,
        routine_step_id="source_watch",
    ) is True

    async with async_db() as db:
        attempt = (await db.execute(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == attempt_id))).scalar_one()
        attempt.cancel_requested_at = datetime.now(timezone.utc)
    assert await dispatcher._browser_assert_current(
        task_id=task_id,
        attempt_id=attempt_id,
        owner_principal_id=owner,
        owner_session_id=session,
        board_task_revision=3,
        board_fencing_token=11,
        input_artifact_id=metadata.artifact_id,
        durable_job_id=child_id,
        routine_parent_job_id=parent_id,
        routine_parent_fencing_token=9,
        routine_step_id="source_watch",
    ) is False


async def _async_result(value: Any) -> Any:
    return value


def _native_durable_parent_spec(*, job_id: str, goal_id: str, owner: str, session: str) -> DurableJobSpec:
    return DurableJobSpec(
        identity=DurableJobIdentity(
            job_id=job_id,
            owner_kind="user",
            owner_principal_id=owner,
            job_kind="guardian_routine_v2",
            capability_version="guardian-routine.v2",
            idempotency_scope="guardian-routine-v2",
            idempotency_key=job_id,
        ),
        inputs={"routine": "browser-proof"},
        session_id=session,
        operator_session_id=session,
        goal_id=goal_id,
        goal_revision=1,
        priority=90,
        declared_authority={
            "principal": owner,
            "owner_kind": "user",
            "session_id": session,
            "goal_id": goal_id,
            "goal_revision": 1,
            "capability_id": "guardian-routine.v2",
            "template_id": "public-browser-check",
            "routine_version": 1,
        },
        max_attempts=1,
        max_outstanding_jobs=2,
    )


def _native_durable_child_spec(
    *,
    job_id: str,
    parent_job_id: str,
    parent_fence: int,
    goal_id: str,
    owner: str,
    session: str,
    mutation: str | None = None,
) -> DurableJobSpec:
    authority: dict[str, Any] = {
        "principal": "service:browser-task",
        "owner_kind": "service",
        "service_id": "service:browser-task",
        "session_id": session,
        "goal_owner_principal_id": owner,
        "goal_owner_session_id": session,
        "goal_id": goal_id,
        "goal_revision": 1,
        "capability_id": "browser.public-task.v1",
        "capability_version": "1",
        "routine_parent_job_id": parent_job_id,
        "routine_parent_fencing_token": parent_fence,
        "routine_parent_goal_id": goal_id,
        "routine_parent_goal_revision": 1,
        "routine_parent_owner_principal_id": owner,
        "routine_parent_owner_session_id": session,
        "routine_step_id": "public_browser_check",
    }
    values: dict[str, Any] = {
        "parent_fencing_token": parent_fence,
        "goal_revision": 1,
        "session_id": session,
        "operator_session_id": session,
        "max_outstanding_jobs": 2,
    }
    if mutation == "owner":
        authority["routine_parent_owner_principal_id"] = "operator:forged"
    elif mutation == "session":
        authority["routine_parent_owner_session_id"] = "session:forged"
    elif mutation == "goal":
        authority["routine_parent_goal_id"] = "goal:forged"
    elif mutation == "revision":
        values["goal_revision"] = 2
        authority["routine_parent_goal_revision"] = 2
    elif mutation == "fence":
        values["parent_fencing_token"] = parent_fence - 1
        authority["routine_parent_fencing_token"] = parent_fence - 1
    elif mutation == "step":
        authority["routine_step_id"] = "forged_step"
    elif mutation == "max_outstanding":
        values["max_outstanding_jobs"] = 3
    base = DurableJobSpec(
        identity=DurableJobIdentity(
            job_id=job_id,
            owner_kind="service",
            owner_principal_id="service:browser-task",
            job_kind="browser_public_task",
            capability_version="1",
            idempotency_scope="guardian-routine-v2-leaf",
            idempotency_key=job_id,
        ),
        inputs={"typed_input": "browser-proof"},
        session_id=session,
        operator_session_id=session,
        parent_job_id=parent_job_id,
        parent_fencing_token=parent_fence,
        goal_id=goal_id,
        goal_revision=1,
        priority=90,
        declared_authority=authority,
        max_attempts=1,
        max_outstanding_jobs=2,
        service_id="service:browser-task",
    )
    return replace(base, **values)


@pytest.mark.parametrize("mutation", ["owner", "session", "goal", "revision", "fence", "step", "max_outstanding"])
async def test_native_child_sqlite_admission_rejects_forged_context_without_row(
    async_db,
    mutation: str,
) -> None:
    owner = "operator:native-context"
    session = "session:native-context"
    goal_id = "goal-native-context-cap2"
    async with async_db() as db:
        db.add(
            Goal(
                id=goal_id,
                title="Native context cap2",
                status="active",
                revision=1,
                owner_principal_id=owner,
                owner_session_id=session,
                admission_budget_json=serialize_admission_budget(
                    GoalAdmissionBudget(max_outstanding_jobs=2, max_attempts=1, max_runtime_seconds=180)
                ),
            )
        )
        await db.flush()

    parent = await durable_job_repository.admit_job(
        _native_durable_parent_spec(
            job_id="native-context-parent",
            goal_id=goal_id,
            owner=owner,
            session=session,
        )
    )
    parent = await durable_job_repository.queue_job(parent["job_id"])
    parent = await durable_job_repository.claim_job(parent["job_id"], owner="native-parent-runner")
    parent_fence = int(parent["lease"]["fencing_token"])
    if mutation is None:
        pytest.fail("the context matrix requires a mutation")
    child_id = f"native-context-child-{mutation}"
    child = _native_durable_child_spec(
        job_id=child_id,
        parent_job_id=parent["job_id"],
        parent_fence=parent_fence,
        goal_id=goal_id,
        owner=owner,
        session=session,
        mutation=mutation,
    )
    with pytest.raises((DurableJobLeaseError, DurableJobTransitionError)):
        await durable_job_repository.admit_job(child)
    assert await durable_job_repository.get_job(child_id) is None


async def test_native_child_sqlite_admits_legitimate_goal_cap2_without_second_root_slot(async_db) -> None:
    owner = "operator:native-cap2"
    session = "session:native-cap2"
    goal_id = "goal-native-cap2-valid"
    async with async_db() as db:
        db.add(
            Goal(
                id=goal_id,
                title="Native cap2 valid",
                status="active",
                revision=1,
                owner_principal_id=owner,
                owner_session_id=session,
                admission_budget_json=serialize_admission_budget(
                    GoalAdmissionBudget(max_outstanding_jobs=2, max_attempts=1, max_runtime_seconds=180)
                ),
            )
        )
        await db.flush()

    parent = await durable_job_repository.admit_job(
        _native_durable_parent_spec(
            job_id="native-cap2-parent",
            goal_id=goal_id,
            owner=owner,
            session=session,
        )
    )
    parent = await durable_job_repository.queue_job(parent["job_id"])
    parent = await durable_job_repository.claim_job(parent["job_id"], owner="native-cap2-runner")
    child = await durable_job_repository.admit_job(
        _native_durable_child_spec(
            job_id="native-cap2-child",
            parent_job_id=parent["job_id"],
            parent_fence=int(parent["lease"]["fencing_token"]),
            goal_id=goal_id,
            owner=owner,
            session=session,
        )
    )
    assert child["status"] == "accepted"
    assert child["parent_run_identity"] == parent["job_id"]


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("step", "native leaf parent context is stale"),
        ("owner", "native leaf parent context is incomplete"),
        ("goal", "native leaf goal binding is stale"),
        ("fence", "native leaf parent context is stale"),
    ],
)
async def test_native_leaf_forged_parent_context_is_rejected_without_contact(
    mutation: str,
    expected: str,
) -> None:
    """The child budget exemption cannot be forged by a leaf caller."""

    class FakeJobs:
        async def get_job(self, _job_id: str) -> Mapping[str, Any]:
            return {
                "job_id": "parent",
                "run_identity": "parent",
                "status": "running",
                "lease": {"owner": "guardian", "fencing_token": 4},
                "deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
                "owner": {"principal_id": "operator", "kind": "user"},
            }

    runtime = ProcedureV2Runtime(jobs=FakeJobs())
    contacted = False

    async def leaf(**_: Any) -> Mapping[str, Any]:
        nonlocal contacted
        contacted = True
        return {"status": "succeeded"}

    runtime.leaf_admitters = {"browser.public-task.v1": leaf}
    parent = {
        "job_id": "parent",
        "status": "running",
        "lease": {"owner": "guardian", "fencing_token": 4},
        "deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
        "owner": {"principal_id": "operator"},
        "operator_session_id": "session",
    }
    step = {
        "step_id": "public_browser_check",
        "capability_id": "browser.public-task.v1",
        "capability_version": "1",
        "typed_input_ref": "workspace-json:artifacts/work-board/inputs/source.json",
        "typed_input_digest": "a" * 64,
    }
    descriptor = {
        "plan": {
            "schema_version": 2,
            "template_id": "public-browser-check",
            "steps": [step],
            "parameters": [],
            "verifier": "leaf_readbacks",
            "limits": {"max_steps": 2, "max_total_seconds": 300},
        },
        "executable_steps": {"public_browser_check": {}},
    }
    with pytest.raises(ProcedureV2RuntimeError, match="procedure_leaf_board_binding_unavailable|procedure_parent"):
        await runtime._admit_child(
            parent,
            step=step,
            descriptor=descriptor,
            child_id="child",
        )
    assert contacted is False


async def test_expired_parent_blocks_before_leaf_contact() -> None:
    contacted = False

    class FakeJobs:
        async def get_job(self, _job_id: str) -> Mapping[str, Any]:
            return {
                "job_id": "parent",
                "run_identity": "parent",
                "status": "running",
                "lease": {"owner": "guardian", "fencing_token": 1},
            }

    runtime = ProcedureV2Runtime(jobs=FakeJobs())

    async def leaf(**_: Any) -> Mapping[str, Any]:
        nonlocal contacted
        contacted = True
        return {"status": "succeeded"}

    runtime.leaf_admitters = {"browser.public-task.v1": leaf}
    parent = {
        "job_id": "parent",
        "status": "running",
        "lease": {"owner": "guardian", "fencing_token": 1},
        "deadline_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        "owner": {"principal_id": "operator"},
        "operator_session_id": "session",
    }
    step = {
        "step_id": "public_browser_check",
        "capability_id": "browser.public-task.v1",
        "capability_version": "1",
        "typed_input_ref": "workspace-json:artifacts/work-board/inputs/source.json",
        "typed_input_digest": "a" * 64,
    }
    descriptor = {"executable_steps": {"public_browser_check": {}}}
    with pytest.raises(ProcedureV2RuntimeError, match="procedure_parent_deadline_expired"):
        await runtime._admit_child(parent, step=step, descriptor=descriptor, child_id="child")
    assert contacted is False


@pytest.mark.skipif(
    os.environ.get("SERAPH_RUN_REAL_PROCEDURE_V2_BROWSER") != "1",
    reason="the composite Watch procedure proof is an explicit opt-in integration test",
)
async def test_real_dispatcher_procedure_watch_persists_native_watch_and_parent_readbacks(
    async_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the production dispatcher through a native Watch and local browser leaf.

    The Watch source is a real workspace file and its packet/dossier/task
    outputs are written and read back by ``SourceWatchService``.  The browser
    leaf uses the real local Chromium runner with only fixture HTTP injected.
    The assertion keeps the native Watch root separate from the parent Board
    task: there is one durable parent, one adapter-owned Watch root, and one
    browser Board child.
    """

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("Playwright is not installed")

    import src.browser.task_runner as task_runner_module
    from src.workflows import procedure_v2_runtime as runtime_module

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    watch_id = "watch-procedure-dispatcher-native"
    goal_id = "goal-procedure-dispatcher-native"
    owner = "operator:test-bypass"
    session = "test-auth-bypass"
    await _seed_sqlite_watch_replay(
        async_db,
        watch_id=watch_id,
        goal_id=goal_id,
        owner=owner,
        session=session,
        baseline_text="old material",
    )
    source_path = tmp_path / "m6/native-watch.txt"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("new material\nwith a verified local update\n", encoding="utf-8")

    budget = GoalAdmissionBudget(
        reviewed_grant=True,
        grant_id="native-watch-replay-grant",
        max_outstanding_jobs=2,
        max_attempts=1,
        max_runtime_seconds=180,
        period_started_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        period_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        timezone="UTC",
    )
    async with async_db() as db:
        goal = await db.get(Goal, goal_id)
        assert goal is not None
        goal.admission_budget_json = serialize_admission_budget(budget)
        await db.flush()

    owner_binding = WorkBoardOwner(principal_id=owner, session_id=session)
    browser_input = _input()
    async with async_db() as db:
        browser_source = await prepare_input_artifact(
            db,
            owner_binding,
            WorkBoardInputArtifactCreate(
                schema_version=1,
                capability_id="browser.public-task.v1",
                goal_id=goal_id,
                goal_revision=1,
                input=browser_input,
                idempotency_key="procedure-watch-browser-input",
            ),
        )
    plan = build_procedure_plan(
        "watch-and-public-browser",
        step_inputs={
            "source_watch": {
                "typed_input_ref": "workspace-json:procedure/watch.json",
                "typed_input_digest": "c" * 64,
            },
            "public_browser_check": {
                "typed_input_ref": browser_source.typed_input_ref,
                "typed_input_digest": browser_source.typed_input_digest,
            },
        },
    ).model_dump(mode="json")
    descriptor = {
        "routine_id": "routine-watch-dispatcher-v2",
        "version": 1,
        "goal_id": goal_id,
        "goal_revision": 1,
        "routine_revision": 1,
        "installed_package_digest": "b" * 64,
        "source_proof_digest": "a" * 64,
        "scope": "procedure-v2:watch-and-public-browser",
        "invocation_uuid": "watch-dispatcher-invocation",
        "parameters": {
            "goal_id": goal_id,
            "expected_goal_revision": 1,
            "source_watch_id": watch_id,
            "expected_watch_revision": 1,
        },
        "plan": plan,
        "plan_digest": plan_digest(plan),
        "executable_steps": {
            "source_watch": {
                "watch_id": watch_id,
                "expected_plan_revision": 1,
            },
            "public_browser_check": browser_input,
        },
    }
    parent_input = {
        "routine_id": descriptor["routine_id"],
        "version": 1,
        "expected_routine_revision": 1,
        "goal_id": goal_id,
        "expected_goal_revision": 1,
        "parameters": descriptor["parameters"],
        "invocation_uuid": descriptor["invocation_uuid"],
    }

    async with async_db() as db:
        parent_artifact = await prepare_input_artifact(
            db,
            owner_binding,
            WorkBoardInputArtifactCreate(
                schema_version=1,
                capability_id="guardian-routine.v2",
                goal_id=goal_id,
                goal_revision=1,
                input=parent_input,
                idempotency_key="procedure-watch-parent-input",
            ),
        )

    dispatcher = WorkBoardDispatcher(session_provider=async_db, runner_id="procedure-native-dispatcher")
    claim = await _create_parent_claim(
        async_db=async_db,
        repository=dispatcher.repository,
        owner=owner_binding,
        goal_id=goal_id,
        parent_input=parent_input,
        parent_artifact_id=parent_artifact.artifact_id,
        dispatcher=dispatcher,
    )

    async def resolve_descriptor(*_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        return descriptor

    monkeypatch.setattr(runtime_module.procedure_v2_runtime, "resolver", resolve_descriptor)

    responses = _html_responses()

    async def fixture_fetch(request: PinnedBrowserRequest) -> PinnedBrowserResponse:
        return responses[request.url]

    async def fixture_policy(url: str, **_: Any) -> Any:
        from src.security.site_policy import SiteAccessDecision

        assert url.startswith("https://fixture.example/")
        return SiteAccessDecision(allowed=True, hostname="fixture.example")

    async def fixture_resolver(*_: Any) -> list[str]:
        return ["93.184.216.34"]

    real_runner = task_runner_module.BrowserTaskRunner
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        def runner_factory(**kwargs: Any) -> Any:
            kwargs["browser_launcher"] = lambda: browser
            kwargs["transport_factory"] = lambda: PinnedBrowserTransport(
                resolver=fixture_resolver,
                injected_fetch=fixture_fetch,
                site_policy=fixture_policy,
            )
            kwargs["workspace_root"] = tmp_path
            return real_runner(**kwargs)

        monkeypatch.setattr(task_runner_module, "BrowserTaskRunner", runner_factory)
        result = await dispatcher._admit_execute_direct(claim, parent_input, runtime_seconds=180)
        await browser.close()

    assert result["admitted"] is True, result
    assert result["completed"] is True, result
    parent_job_id = (
        f"procedure-v2:{descriptor['routine_id']}:1:{descriptor['invocation_uuid']}"
    )
    parent_projection = await durable_job_repository.get_job(parent_job_id)
    assert parent_projection is not None
    assert parent_projection["status"] == "succeeded"
    parent_effect = next(
        effect
        for effect in parent_projection.get("effects", [])
        if isinstance(effect, Mapping)
        and effect.get("effect_type") == "guardian_routine_v2_parent"
    )
    assert parent_effect.get("details", {}).get("verified") is True
    watch_settled = next(
        checkpoint
        for checkpoint in parent_projection.get("checkpoints", [])
        if checkpoint.get("checkpoint_id") == "procedure-v2:step:source_watch:settled"
    )
    watch_child_id = watch_settled["payload"]["child_job_id"]
    watch_projection = await durable_job_repository.get_job(watch_child_id)
    assert watch_projection is not None
    assert watch_projection["status"] == "succeeded"
    assert watch_child_id.startswith(f"source-watch:{watch_id}:")
    assert any(
        effect.get("receipt_kind") == "readback"
        and effect.get("status") == "succeeded"
        and effect.get("details", {}).get("verified") is True
        for effect in watch_projection.get("effects", [])
        if isinstance(effect, Mapping)
    )

    async with async_db() as db:
        packet = (
            await db.execute(
                select(GuardianDecisionPacket).where(
                    GuardianDecisionPacket.watch_id == watch_id,
                    GuardianDecisionPacket.run_identity == watch_child_id,
                )
            )
        ).scalar_one()
        assert packet.status == "succeeded"
        assert packet.verification_status == "passed"
        assert packet.dossier_path and (tmp_path / packet.dossier_path).is_file()
        assert packet.task_path and (tmp_path / packet.task_path).is_file()
        parent_task = (
            await db.execute(
                select(WorkBoardTask).where(WorkBoardTask.task_id == claim.task.task_id)
            )
        ).scalar_one()
        assert parent_task.status is WorkBoardStatus.done

    assert watch_settled["payload"]["watch_packet_id"] == packet.id
    assert watch_settled["payload"]["watch_terminal_status"] == "succeeded"
    assert watch_settled["payload"]["watch_artifact_refs"]

    browser_settled = next(
        checkpoint
        for checkpoint in parent_projection.get("checkpoints", [])
        if checkpoint.get("checkpoint_id") == "procedure-v2:step:public_browser_check:settled"
    )
    assert browser_settled["payload"]["status"] == "succeeded"
    browser_projection = await durable_job_repository.get_job(browser_settled["payload"]["child_job_id"])
    assert browser_projection is not None
    assert browser_projection["status"] == "succeeded"
    assert any(
        effect.get("receipt_kind") == "readback"
        and effect.get("status") == "succeeded"
        and effect.get("details", {}).get("verified") is True
        for effect in browser_projection.get("effects", [])
        if isinstance(effect, Mapping)
    )
    assert any(
        effect.get("effect_type") == "browser_context_cleanup"
        and effect.get("status") == "succeeded"
        for effect in browser_projection.get("effects", [])
        if isinstance(effect, Mapping)
    )


async def test_real_dispatcher_watch_no_change_skips_browser_leaf(
    async_db: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified Watch no-change result records a skip without a Browser child."""

    from src.workflows import procedure_v2_runtime as runtime_module

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    watch_id = "watch-procedure-dispatcher-no-change"
    goal_id = "goal-procedure-dispatcher-no-change"
    owner = "operator:test-bypass"
    session = "test-auth-bypass"
    await _seed_sqlite_watch_replay(
        async_db,
        watch_id=watch_id,
        goal_id=goal_id,
        owner=owner,
        session=session,
        baseline_text="old material",
    )
    source_path = tmp_path / "m6/native-watch.txt"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("old material", encoding="utf-8")

    budget = GoalAdmissionBudget(
        reviewed_grant=True,
        grant_id="native-watch-replay-grant",
        max_outstanding_jobs=2,
        max_attempts=1,
        max_runtime_seconds=180,
        period_started_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        period_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        timezone="UTC",
    )
    async with async_db() as db:
        goal = await db.get(Goal, goal_id)
        assert goal is not None
        goal.admission_budget_json = serialize_admission_budget(budget)
        await db.flush()

    owner_binding = WorkBoardOwner(principal_id=owner, session_id=session)
    browser_input = _input()
    async with async_db() as db:
        browser_source = await prepare_input_artifact(
            db,
            owner_binding,
            WorkBoardInputArtifactCreate(
                schema_version=1,
                capability_id="browser.public-task.v1",
                goal_id=goal_id,
                goal_revision=1,
                input=browser_input,
                idempotency_key="procedure-watch-no-change-browser-input",
            ),
        )
    plan = build_procedure_plan(
        "watch-and-public-browser",
        step_inputs={
            "source_watch": {
                "typed_input_ref": "workspace-json:procedure/watch.json",
                "typed_input_digest": "c" * 64,
            },
            "public_browser_check": {
                "typed_input_ref": browser_source.typed_input_ref,
                "typed_input_digest": browser_source.typed_input_digest,
            },
        },
    ).model_dump(mode="json")
    descriptor = {
        "routine_id": "routine-watch-dispatcher-no-change-v2",
        "version": 1,
        "goal_id": goal_id,
        "goal_revision": 1,
        "routine_revision": 1,
        "installed_package_digest": "b" * 64,
        "source_proof_digest": "a" * 64,
        "scope": "procedure-v2:watch-and-public-browser",
        "invocation_uuid": "watch-no-change-invocation",
        "parameters": {
            "goal_id": goal_id,
            "expected_goal_revision": 1,
            "source_watch_id": watch_id,
            "expected_watch_revision": 1,
        },
        "plan": plan,
        "plan_digest": plan_digest(plan),
        "executable_steps": {
            "source_watch": {"watch_id": watch_id, "expected_plan_revision": 1},
            "public_browser_check": browser_input,
        },
    }
    parent_input = {
        "routine_id": descriptor["routine_id"],
        "version": 1,
        "expected_routine_revision": 1,
        "goal_id": goal_id,
        "expected_goal_revision": 1,
        "parameters": descriptor["parameters"],
        "invocation_uuid": descriptor["invocation_uuid"],
    }
    async with async_db() as db:
        parent_artifact = await prepare_input_artifact(
            db,
            owner_binding,
            WorkBoardInputArtifactCreate(
                schema_version=1,
                capability_id="guardian-routine.v2",
                goal_id=goal_id,
                goal_revision=1,
                input=parent_input,
                idempotency_key="procedure-watch-no-change-parent-input",
            ),
        )

    dispatcher = WorkBoardDispatcher(session_provider=async_db, runner_id="procedure-no-change-dispatcher")
    claim = await _create_parent_claim(
        async_db=async_db,
        repository=dispatcher.repository,
        owner=owner_binding,
        goal_id=goal_id,
        parent_input=parent_input,
        parent_artifact_id=parent_artifact.artifact_id,
        dispatcher=dispatcher,
    )

    async def resolve_descriptor(*_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
        return descriptor

    monkeypatch.setattr(runtime_module.procedure_v2_runtime, "resolver", resolve_descriptor)
    real_execute_leaf = dispatcher._execute_v2_leaf_adapter

    async def reject_browser_leaf(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
        step = args[2] if len(args) > 2 else kwargs.get("step")
        if isinstance(step, Mapping) and step.get("capability_id") == "browser.public-task.v1":
            raise AssertionError("verified Watch no-change must not execute a Browser leaf")
        return await real_execute_leaf(*args, **kwargs)

    monkeypatch.setattr(dispatcher, "_execute_v2_leaf_adapter", reject_browser_leaf)
    result = await dispatcher._admit_execute_direct(claim, parent_input, runtime_seconds=180)
    assert result == {"admitted": True, "completed": True, "blocked": False}

    parent_job_id = f"procedure-v2:{descriptor['routine_id']}:1:{descriptor['invocation_uuid']}"
    parent_projection = await durable_job_repository.get_job(parent_job_id)
    assert parent_projection is not None
    assert parent_projection["status"] == "succeeded"
    watch_settled = next(
        checkpoint
        for checkpoint in parent_projection.get("checkpoints", [])
        if checkpoint.get("checkpoint_id") == "procedure-v2:step:source_watch:settled"
    )
    assert watch_settled["payload"]["watch_terminal_status"] == "skipped_verified"
    assert watch_settled["payload"]["watch_no_change"] is True
    assert watch_settled["payload"]["watch_parent_fencing_token"] == 1
    browser_skip = next(
        checkpoint
        for checkpoint in parent_projection.get("checkpoints", [])
        if checkpoint.get("checkpoint_id") == "procedure-v2:step:public_browser_check:skipped"
    )
    assert browser_skip["payload"] == {
        "step_id": "public_browser_check",
        "status": "skipped",
        "reason_code": "watch_no_material_change",
        "skipped": True,
        "watch_child_job_id": watch_settled["payload"]["child_job_id"],
        "memory_status": "no_learning",
    }
    assert not any(
        checkpoint.get("checkpoint_id") == "procedure-v2:step:public_browser_check:admitted"
        for checkpoint in parent_projection.get("checkpoints", [])
    )
    async with async_db() as db:
        tasks = list(
            (
                await db.execute(
                    select(WorkBoardTask).where(
                        WorkBoardTask.owner_principal_id == owner,
                        WorkBoardTask.owner_session_id == session,
                    )
                )
            ).scalars().all()
        )
    assert [task.capability_id for task in tasks] == ["guardian-routine.v2"]
