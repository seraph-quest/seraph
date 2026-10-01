"""Focused contract tests for the governed repository-repair preparation seam."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlmodel import select

from config.settings import settings
from src.execution.repo_sandbox import RootlessDockerRepoSandbox
from src.auth.service import create_session
from src.db.models import (
    ApprovalRequest,
    Goal,
    OperatorSession,
    RepoRepairEgressConsent,
    RepoRepairProposal as RepoRepairProposalRow,
    RepoRepairSourcePacket as RepoRepairSourcePacketRow,
    Session,
    WorkBoardAttempt,
    WorkBoardInputArtifact,
    WorkBoardStatus,
    WorkBoardTask,
    WorkflowRunState,
)
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal, canonical_digest
from src.goals.contracts import GoalAdmissionBudget
from src.goals.repository import serialize_admission_budget
from src.work_board.contracts import (
    WorkBoardInputArtifactCreate,
    WorkBoardOwner,
    WorkBoardTaskCreate,
)
from src.work_board.dispatcher import WorkBoardDispatcher, registered_executor_id
from src.work_board.input_artifacts import (
    prepare_input_artifact,
    resolve_input_artifact_for_task,
)
from src.work_board.repository import WorkBoardRepository
from src.workflows.repo_repair import (
    REPO_REPAIR_MAX_CONSENT_TTL,
    RepoRepairError,
    RepoRepairInput,
    RepoRepairService,
    RepoRepairModelOutput,
    _repair_approval_fingerprint,
)
from src.workflows.job_runtime import durable_job_repository


def _repair_input(**overrides):
    value = {
        "repository_path": "repo",
        "problem_statement": "Fix the bounded test failure.",
        "acceptance_criteria": ["The focused test passes."],
        "source_paths": ["src/app.py"],
        "allowed_paths": ["src/app.py", "tests/test_app.py"],
        "test_args": ["pytest", "tests/test_app.py"],
    }
    value.update(overrides)
    return value


async def _seed_canonical_repair(
    db,
    workspace: Path,
    request: dict,
    *,
    owner: WorkBoardOwner,
    task_id: str,
    attempt_id: str,
    run_id: str,
    goal_id: str,
    now: datetime,
    lease_owner: str = "worker:repo-repair",
    fencing_token: int = 1,
):
    """Seed the real task/attempt/input/job/session rows used by the service."""

    intent = RepoRepairInput.model_validate(request)
    payload = json.dumps(
        {
            "schema_version": 1,
            "capability_id": "engineering.repo-repair.v1",
            "input": intent.model_dump(mode="json"),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload_digest = hashlib.sha256(payload).hexdigest()
    input_dir = workspace / "artifacts" / "work-board" / "input"
    input_dir.mkdir(parents=True, mode=0o700)
    for private_dir in (workspace / "artifacts", workspace / "artifacts" / "work-board", input_dir):
        private_dir.chmod(0o700)
    input_ref = f"workspace-json:artifacts/work-board/input/{task_id}.json"
    input_path = workspace / input_ref.removeprefix("workspace-json:")
    input_path.write_bytes(payload)
    input_path.chmod(0o600)
    db.add(
        OperatorSession(
            id=owner.session_id,
            token_hash=hashlib.sha256(f"token:{run_id}".encode()).hexdigest(),
            created_at=now - timedelta(minutes=1),
            last_seen_at=now,
            idle_expires_at=now + timedelta(hours=1),
            absolute_expires_at=now + timedelta(hours=1),
        )
    )
    db.add(
        Goal(
            id=goal_id,
            title="Repository repair test goal",
            status="active",
            revision=1,
            owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,
            admission_budget_json="{}",
        )
    )
    db.add(Session(id=owner.session_id, owner_principal_id=owner.principal_id, title="Repair test session"))
    await db.flush()
    db.add(
        WorkBoardInputArtifact(
            artifact_id=f"input:{task_id}",
            owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,
            goal_id=goal_id,
            goal_revision=1,
            capability_id="engineering.repo-repair.v1",
            capability_version="1",
            idempotency_key=f"input:{task_id}",
            payload_sha256=payload_digest,
            typed_input_ref=input_ref,
            size_bytes=len(payload),
            state="bound",
            bound_task_id=task_id,
            bound_task_revision=1,
            expires_at=now + timedelta(hours=1),
        )
    )
    db.add(
        WorkBoardTask(
            task_id=task_id,
            owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,
            origin_session_id=owner.session_id,
            goal_id=goal_id,
            goal_revision=1,
            title="Repository repair test task",
            body="",
            capability_id="engineering.repo-repair.v1",
            input_artifact_id=f"input:{task_id}",
            typed_input_ref=input_ref,
            typed_input_digest=payload_digest,
            idempotency_key=f"task:{task_id}",
            status=WorkBoardStatus.running,
            task_revision=1,
        )
    )
    durable_root = WorkflowRunState(
            run_identity=run_id,
            root_run_identity=run_id,
            workflow_name="repo-repair-test",
            tool_name="engineering.repo-repair.v1",
            session_id=owner.session_id,
            operator_session_id=owner.session_id,
            status="running",
            owner_kind="user",
            owner_principal_id=owner.principal_id,
            goal_id=goal_id,
            goal_revision=1,
            lease_owner=lease_owner,
            lease_expires_at=now + timedelta(hours=1),
            fencing_token=fencing_token,
            input_digest=payload_digest,
            job_kind="repo_repair",
            capability_version="engineering.repo-repair.v1",
            idempotency_scope="repo-repair-test",
            idempotency_key=f"job:{run_id}",
            idempotency_binding=f"repo-repair-test:{run_id}",
            run_fingerprint=payload_digest,
            declared_authority_json=json.dumps(
                {
                    "session_id": owner.session_id,
                    "owner_principal_id": owner.principal_id,
                    "goal_id": goal_id,
                    "goal_revision": 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    db.add(durable_root)
    await db.flush()
    attempt = WorkBoardAttempt(
            attempt_id=attempt_id,
            task_id=task_id,
            workflow_run_id=run_id,
            task_revision_at_claim=1,
            lease_owner=lease_owner,
            lease_expires_at=now + timedelta(hours=1),
            heartbeat_at=now,
            fencing_token=fencing_token,
            executor_id=lease_owner,
            started_at=now - timedelta(seconds=1),
        )
    db.add(attempt)
    await db.flush()
    return payload_digest


def test_repo_repair_input_rejects_protected_paths_and_unknown_fields():
    with pytest.raises(ValueError):
        RepoRepairInput.model_validate(_repair_input(source_paths=[".env"]))
    with pytest.raises(ValueError):
        RepoRepairInput.model_validate(_repair_input(source_paths=["src\\app.py"]))
    with pytest.raises(ValueError):
        RepoRepairInput.model_validate(_repair_input(source_paths=["src/key.pem"]))
    with pytest.raises(ValueError):
        RepoRepairInput.model_validate(_repair_input(unexpected="caller-authority"))


def test_repo_repair_input_requires_source_paths_inside_allowlist():
    with pytest.raises(ValueError, match="contained"):
        RepoRepairInput.model_validate(
            _repair_input(source_paths=["src/other.py"], allowed_paths=["src/app.py"])
        )


@pytest.mark.asyncio
async def test_repo_repair_real_input_producer_reaches_private_source_review(
    async_db, tmp_path: Path, monkeypatch
):
    """The shared input producer feeds the governed repair dispatcher.

    This deliberately starts with the public WorkBoard producer rather than
    seeding an input row or claiming the task directly.  The public dispatcher
    then performs readiness, admission, and source inspection before pausing
    for the explicit code-egress decision and any model call.
    """

    workspace = tmp_path / "workspace"
    (workspace / "repo" / "src").mkdir(parents=True)
    (workspace / "repo" / "tests").mkdir()
    (workspace / "repo" / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (workspace / "repo" / "tests" / "test_app.py").write_text(
        "def test_value():\n    assert True\n", encoding="utf-8"
    )
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(settings, "deployment_environment", "test")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "repo-repair-producer-auth-secret")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    _token, operator = await create_session()
    owner = WorkBoardOwner(
        principal_id=operator.principal.principal_id,
        session_id=operator.session_id,
    )
    goal_id = "goal:producer"
    request = _repair_input()
    budget = GoalAdmissionBudget(
        reviewed_grant=True,
        grant_id="grant:repo-repair-producer",
        max_outstanding_jobs=1,
        max_attempts=1,
        max_runtime_seconds=120,
        period_started_at=now - timedelta(minutes=1),
        period_expires_at=now + timedelta(minutes=10),
        timezone="UTC",
    )

    monkeypatch.setattr(
        RootlessDockerRepoSandbox,
        "preflight",
        lambda _sandbox: SimpleNamespace(
            ok=True,
            status="verified",
            reason="",
            as_receipt=lambda: {
                "ok": True,
                "status": "verified",
                "reason": "",
                "operator_visible": True,
            },
        ),
    )

    async with async_db() as db:
        db.add(
            Goal(
                id=goal_id,
                title="Producer repair goal",
                status="active",
                revision=1,
                owner_principal_id=owner.principal_id,
                owner_session_id=owner.session_id,
                admission_budget_json=serialize_admission_budget(budget),
            )
        )
        await db.commit()

    typed_request = WorkBoardInputArtifactCreate(
        schema_version=1,
        capability_id="engineering.repo-repair.v1",
        goal_id=goal_id,
        goal_revision=1,
        input=request,
        idempotency_key="producer-input",
    )
    async with async_db() as db:
        metadata = await prepare_input_artifact(db, owner, typed_request, now=now)

    payload_path = workspace / metadata.typed_input_ref.removeprefix("workspace-json:")
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert set(payload) == {"schema_version", "capability_id", "input"}
    assert payload["schema_version"] == 1
    assert payload["capability_id"] == "engineering.repo-repair.v1"
    assert payload["input"] == RepoRepairInput.model_validate(request).model_dump(mode="json")
    assert metadata.capability_version == "1"

    repository = WorkBoardRepository()
    async with async_db() as db:
        mutation = await repository.create_task(
            db,
            owner,
            WorkBoardTaskCreate(
                title="Inspect repository repair",
                goal_id=goal_id,
                goal_revision=1,
                status=WorkBoardStatus.todo,
                capability_id="engineering.repo-repair.v1",
                input_artifact_id=metadata.artifact_id,
                executor_id=registered_executor_id("engineering.repo-repair.v1"),
                idempotency_key="producer-task",
            ),
        )
        await db.commit()
    assert mutation.task.executor_id == registered_executor_id("engineering.repo-repair.v1")
    task_id = mutation.task.task_id

    async with async_db() as db:
        resolved = await resolve_input_artifact_for_task(
            db,
            owner,
            artifact_id=metadata.artifact_id,
            goal_id=goal_id,
            goal_revision=1,
            capability_id="engineering.repo-repair.v1",
            expected_task_id=task_id,
            now=now,
        )
    assert resolved.input == RepoRepairInput.model_validate(request).model_dump(mode="json")

    dispatcher = WorkBoardDispatcher(
        repository=repository,
        session_provider=async_db,
    )
    result = await dispatcher.run_pass()
    assert result["claimed"] == 1
    assert result["admitted"] == 1

    async with async_db() as db:
        task = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))).scalar_one()
        attempt = (
            await db.execute(
                select(WorkBoardAttempt)
                .where(WorkBoardAttempt.task_id == task_id)
                .order_by(WorkBoardAttempt.attempt_id.desc())
            )
        ).scalars().first()
        assert attempt is not None and attempt.workflow_run_id
        packet = (
            await db.execute(
                select(RepoRepairSourcePacketRow).where(
                    RepoRepairSourcePacketRow.workflow_run_id == attempt.workflow_run_id
                )
            )
        ).scalar_one()
    assert task.status is WorkBoardStatus.blocked
    assert task.block_reason == "repo_repair_code_egress_review"
    assert packet.state == "verified"
    assert packet.owner_principal_id == owner.principal_id
    assert packet.owner_session_id == owner.session_id


@pytest.mark.asyncio
async def test_repo_repair_requires_exact_capability_before_inspection(async_db, tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "repo" / "src").mkdir(parents=True)
    (workspace / "repo" / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    service = RepoRepairService(
        sandbox=RootlessDockerRepoSandbox(),
        secret_scanner=lambda value: value,
        workspace_dir=str(workspace),
    )
    owner = WorkBoardOwner(principal_id="operator:single", session_id="session:capability")
    request = _repair_input()
    async with async_db() as db:
        await _seed_canonical_repair(
            db,
            workspace,
            request,
            owner=owner,
            task_id="task:capability",
            attempt_id="attempt:capability",
            run_id="job:capability",
            goal_id="goal:capability",
            now=datetime.now(timezone.utc),
        )
        task = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == "task:capability"))).scalar_one()
        assert task is not None
        task.capability_id = None
        with pytest.raises(RepoRepairError) as blocked:
            await service.inspect_and_prepare(
                request,
                owner=owner,
                work_board_task_id="task:capability",
                work_board_attempt_id="attempt:capability",
                workflow_run_id="job:capability",
                goal_id="goal:capability",
                goal_revision=1,
                db=db,
            )
        assert blocked.value.code == "repair_task_capability_invalid"


@pytest.mark.asyncio
async def test_repo_repair_rejects_wrong_stored_input_envelope(async_db, tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "repo" / "src").mkdir(parents=True)
    (workspace / "repo" / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    service = RepoRepairService(
        sandbox=RootlessDockerRepoSandbox(),
        secret_scanner=lambda value: value,
        workspace_dir=str(workspace),
    )
    owner = WorkBoardOwner(principal_id="operator:single", session_id="session:envelope")
    request = _repair_input()
    now = datetime.now(timezone.utc)
    async with async_db() as db:
        await _seed_canonical_repair(
            db,
            workspace,
            request,
            owner=owner,
            task_id="task:envelope",
            attempt_id="attempt:envelope",
            run_id="job:envelope",
            goal_id="goal:envelope",
            now=now,
        )
        input_row = await db.get(WorkBoardInputArtifact, "input:task:envelope")
        task = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == "task:envelope"))).scalar_one()
        durable_root = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == "job:envelope"))).scalar_one()
        assert input_row is not None and task is not None and durable_root is not None
        input_path = workspace / input_row.typed_input_ref.removeprefix("workspace-json:")
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        payload["capability_id"] = "engineering.other.v1"
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        input_path.write_bytes(encoded)
        input_path.chmod(0o600)
        input_row.payload_sha256 = digest
        task.typed_input_digest = digest
        durable_root.input_digest = digest
        with pytest.raises(RepoRepairError) as blocked:
            await service.inspect_and_prepare(
                request,
                owner=owner,
                work_board_task_id="task:envelope",
                work_board_attempt_id="attempt:envelope",
                workflow_run_id="job:envelope",
                goal_id="goal:envelope",
                goal_revision=1,
                db=db,
            )
        assert blocked.value.code == "input_artifact_invalid"


@pytest.mark.asyncio
async def test_repo_repair_reads_shared_schema_one_input_envelope(async_db, tmp_path: Path, monkeypatch):
    """The service consumes the shared M3 three-key artifact envelope.

    Capability version remains server authority on the task and input rows;
    it is intentionally not a fourth payload key.
    """

    workspace = tmp_path / "workspace"
    (workspace / "repo" / "src").mkdir(parents=True)
    (workspace / "repo" / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    now = datetime.now(timezone.utc)
    service = RepoRepairService(
        sandbox=RootlessDockerRepoSandbox(),
        secret_scanner=lambda value: value,
        workspace_dir=str(workspace),
        clock=lambda: now,
    )
    owner = WorkBoardOwner(principal_id="operator:single", session_id="session:shared-envelope")
    async with async_db() as db:
        await _seed_canonical_repair(
            db,
            workspace,
            _repair_input(),
            owner=owner,
            task_id="task:shared-envelope",
            attempt_id="attempt:shared-envelope",
            run_id="job:shared-envelope",
            goal_id="goal:shared-envelope",
            now=now,
        )
        packet = await service.inspect_and_prepare(
            _repair_input(),
            owner=owner,
            work_board_task_id="task:shared-envelope",
            work_board_attempt_id="attempt:shared-envelope",
            workflow_run_id="job:shared-envelope",
            goal_id="goal:shared-envelope",
            goal_revision=1,
            db=db,
        )
        assert packet.input_digest
        input_row = await db.get(WorkBoardInputArtifact, "input:task:shared-envelope")
        assert input_row is not None
        assert input_row.capability_id == "engineering.repo-repair.v1"
        assert input_row.capability_version == "1"
        payload = json.loads(
            (workspace / input_row.typed_input_ref.removeprefix("workspace-json:")).read_text(encoding="utf-8")
        )
        assert set(payload) == {"schema_version", "capability_id", "input"}
        assert "capability_version" not in payload


def test_model_output_is_closed_schema_and_digest_bound():
    digest = "a" * 64
    output = RepoRepairModelOutput.model_validate(
        {
            "summary": "A bounded proposal.",
            "base_snapshot_sha256": digest,
            "patch_unified_diff": "--- a/src/app.py\n+++ b/src/app.py\n@@\n-VALUE = 1\n+VALUE = 2\n",
            "allowed_paths": ["src/app.py"],
            "test_args": ["pytest", "tests/test_app.py"],
            "expected_outcome": "The focused test passes.",
        }
    )
    assert output.base_snapshot_sha256 == digest
    with pytest.raises(ValueError):
        RepoRepairModelOutput.model_validate(
            {
                "summary": "proposal",
                "base_snapshot_sha256": digest,
                "patch_unified_diff": "--- a/src/app.py\n+++ b/src/app.py\n",
                "allowed_paths": ["src/app.py"],
                "test_args": ["pytest", "tests/test_app.py"],
                "expected_outcome": "ok",
                "authority": "caller supplied",
            }
        )


def test_private_artifact_writer_rejects_existing_shared_directory(tmp_path: Path):
    workspace = tmp_path / "workspace"
    artifact_root = workspace / "artifacts" / "repo-repair" / "source"
    artifact_root.mkdir(parents=True)
    artifact_root.chmod(0o755)
    service = RepoRepairService(workspace_dir=str(workspace))
    with pytest.raises(RepoRepairError) as blocked:
        service._write_private_artifact(
            "artifacts/repo-repair/source/example.json",
            b"{}",
        )
    assert blocked.value.code == "private_artifact_unavailable"


def test_private_artifact_writer_rejects_existing_broad_permissions(tmp_path: Path):
    workspace = tmp_path / "workspace"
    artifact_root = workspace / "artifacts" / "repo-repair" / "source"
    artifact_root.mkdir(parents=True, mode=0o700)
    for parent in (workspace, workspace / "artifacts", workspace / "artifacts" / "repo-repair", artifact_root):
        parent.chmod(0o700)
    target = artifact_root / "existing.json"
    target.write_bytes(b"{}")
    target.chmod(0o644)
    service = RepoRepairService(workspace_dir=str(workspace))
    with pytest.raises(RepoRepairError) as blocked:
        service._write_private_artifact("artifacts/repo-repair/source/existing.json", b"{}")
    assert blocked.value.code == "private_artifact_permissions_invalid"


@pytest.mark.asyncio
async def test_inspect_persists_private_owner_bound_packet(async_db, tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    repository = workspace / "repo"
    (repository / "src").mkdir(parents=True)
    (repository / "tests").mkdir()
    (repository / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "tests" / "test_app.py").write_text("def test_value():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))

    async def scan(value: str) -> str:
        return value

    service = RepoRepairService(
        sandbox=RootlessDockerRepoSandbox(),
        secret_scanner=scan,
        workspace_dir=str(workspace),
    )
    owner = WorkBoardOwner(principal_id="operator:single", session_id="session:test-repair")
    async with async_db() as db:
        await _seed_canonical_repair(
            db,
            workspace,
            _repair_input(),
            owner=owner,
            task_id="task:repair",
            attempt_id="attempt:repair",
            run_id="job:repair",
            goal_id="goal:repair",
            now=datetime.now(timezone.utc),
        )
        packet = await service.inspect_and_prepare(
            _repair_input(),
            owner=owner,
            work_board_task_id="task:repair",
            work_board_attempt_id="attempt:repair",
            workflow_run_id="job:repair",
            goal_id="goal:repair",
            goal_revision=1,
            db=db,
        )
        assert packet.state == "verified"
        assert packet.owner_principal_id == owner.principal_id
        assert packet.owner_session_id == owner.session_id
        assert packet.artifact_ref.startswith("workspace-json:artifacts/repo-repair/source/")
        artifact = workspace / packet.artifact_ref.removeprefix("workspace-json:")
        assert artifact.stat().st_mode & 0o077 == 0
        assert packet.artifact_sha256
        assert "VALUE = 1" in artifact.read_text(encoding="utf-8")

        replay = await service.inspect_and_prepare(
            _repair_input(),
            owner=owner,
            work_board_task_id="task:repair",
            work_board_attempt_id="attempt:repair",
            workflow_run_id="job:repair",
            goal_id="goal:repair",
            goal_revision=1,
            db=db,
        )
        assert replay.packet_id == packet.packet_id
        assert replay.artifact_sha256 == packet.artifact_sha256


@pytest.mark.asyncio
async def test_inspect_reconciles_packet_after_row_publication_failure(async_db, tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "repo" / "src").mkdir(parents=True)
    (workspace / "repo" / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    service = RepoRepairService(
        sandbox=RootlessDockerRepoSandbox(),
        secret_scanner=lambda value: value,
        workspace_dir=str(workspace),
    )
    owner = WorkBoardOwner(principal_id="operator:single", session_id="session:packet-recovery")
    request = _repair_input()
    async with async_db() as db:
        await _seed_canonical_repair(
            db,
            workspace,
            request,
            owner=owner,
            task_id="task:packet-recovery",
            attempt_id="attempt:packet-recovery",
            run_id="job:packet-recovery",
            goal_id="goal:packet-recovery",
            now=datetime.now(timezone.utc),
        )
        original_flush = db.flush
        fail_flush = True

        async def fail_packet_flush(*args, **kwargs):
            nonlocal fail_flush
            if fail_flush:
                fail_flush = False
                raise RuntimeError("injected source packet row failure")
            return await original_flush(*args, **kwargs)

        monkeypatch.setattr(db, "flush", fail_packet_flush)
        with pytest.raises(RepoRepairError) as uncertain:
            await service.inspect_and_prepare(
                request,
                owner=owner,
                work_board_task_id="task:packet-recovery",
                work_board_attempt_id="attempt:packet-recovery",
                workflow_run_id="job:packet-recovery",
                goal_id="goal:packet-recovery",
                goal_revision=1,
                db=db,
            )
        assert uncertain.value.code == "repair_source_publication_unknown"
        await db.rollback()
        monkeypatch.setattr(db, "flush", original_flush)
        packet = await service.inspect_and_prepare(
            request,
            owner=owner,
            work_board_task_id="task:packet-recovery",
            work_board_attempt_id="attempt:packet-recovery",
            workflow_run_id="job:packet-recovery",
            goal_id="goal:packet-recovery",
            goal_revision=1,
            db=db,
        )
        assert packet.state == "verified"
        assert (workspace / packet.artifact_ref.removeprefix("workspace-json:")).is_file()
        projection = await durable_job_repository.get_job("job:packet-recovery")
        assert projection is not None
        assert any(
            item.get("checkpoint_id") == "repo-repair-source-intent:job:packet-recovery"
            for item in projection["checkpoints"]
        )


@pytest.mark.asyncio
async def test_inspect_fails_closed_on_secret_scan_and_writes_no_packet(async_db, tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    repository = workspace / "repo"
    (repository / "src").mkdir(parents=True)
    (repository / "src" / "app.py").write_text("TOKEN = 'should-not-leave'\n", encoding="utf-8")
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))

    async def scan(_value: str) -> str:
        return "[REDACTED]"

    service = RepoRepairService(
        sandbox=RootlessDockerRepoSandbox(),
        secret_scanner=scan,
        workspace_dir=str(workspace),
    )
    owner = WorkBoardOwner(principal_id="operator:single", session_id="session:secret")
    async with async_db() as db:
        await _seed_canonical_repair(
            db,
            workspace,
            _repair_input(allowed_paths=["src/app.py"], test_args=["pytest", "src/app.py"]),
            owner=owner,
            task_id="task:repair",
            attempt_id="attempt:repair-secret",
            run_id="job:secret",
            goal_id="goal:repair",
            now=datetime.now(timezone.utc),
        )
        with pytest.raises(RepoRepairError) as blocked:
            await service.inspect_and_prepare(
                _repair_input(allowed_paths=["src/app.py"], test_args=["pytest", "src/app.py"]),
                owner=owner,
                work_board_task_id="task:repair",
                work_board_attempt_id="attempt:repair-secret",
                workflow_run_id="job:secret",
                goal_id="goal:repair",
                goal_revision=1,
                db=db,
            )
        assert blocked.value.code == "source_secret_detected"
        assert not list((workspace / "artifacts" / "repo-repair" / "source").glob("*.json"))


@pytest.mark.asyncio
async def test_egress_consent_is_finite_and_exactly_idempotent(async_db, tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    repository = workspace / "repo"
    (repository / "src").mkdir(parents=True)
    (repository / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))

    async def scan(value: str) -> str:
        return value

    now = datetime.now(timezone.utc)
    service = RepoRepairService(
        sandbox=RootlessDockerRepoSandbox(),
        secret_scanner=scan,
        workspace_dir=str(workspace),
        clock=lambda: now,
    )
    owner = WorkBoardOwner(principal_id="operator:single", session_id="session:consent")
    async with async_db() as db:
        input_value = _repair_input(allowed_paths=["src/app.py"], test_args=["pytest", "src/app.py"])
        await _seed_canonical_repair(
            db,
            workspace,
            input_value,
            owner=owner,
            task_id="task:repair",
            attempt_id="attempt:repair-consent",
            run_id="job:repair",
            goal_id="goal:repair",
            now=now,
        )
        packet = await service.inspect_and_prepare(
            input_value,
            owner=owner,
            work_board_task_id="task:repair",
            work_board_attempt_id="attempt:repair-consent",
            workflow_run_id="job:repair",
            goal_id="goal:repair",
            goal_revision=1,
            db=db,
        )
        consent = await service.grant_egress_consent(
            owner=owner,
            work_board_task_id="task:repair",
            work_board_attempt_id="attempt:repair-consent",
            workflow_run_id="job:repair",
            packet=packet,
            effective_profile_id="openrouter:strategist",
            effective_upstream="openrouter",
            request_key="request:repair",
            expires_at=now + timedelta(minutes=5),
            db=db,
        )
        replay = await service.grant_egress_consent(
            owner=owner,
            work_board_task_id="task:repair",
            work_board_attempt_id="attempt:repair-consent",
            workflow_run_id="job:repair",
            packet=packet,
            effective_profile_id="openrouter:strategist",
            effective_upstream="openrouter",
            request_key="request:repair",
            expires_at=now + timedelta(minutes=5),
            db=db,
        )
        assert replay.id == consent.id
        assert consent.maximum_input_bytes == 64 * 1024
        assert consent.maximum_output_tokens == 4096

        with pytest.raises(RepoRepairError) as conflict:
            await service.grant_egress_consent(
                owner=owner,
                work_board_task_id="task:repair",
                work_board_attempt_id="attempt:repair-consent",
                workflow_run_id="job:repair",
                packet=packet,
                effective_profile_id="openrouter:other",
                effective_upstream="openrouter",
                request_key="request:repair",
                expires_at=now + timedelta(minutes=5),
                db=db,
            )
        assert conflict.value.code == "egress_consent_idempotency_conflict"
        with pytest.raises(RepoRepairError, match="30-minute"):
            await service.grant_egress_consent(
                owner=owner,
                work_board_task_id="task:repair",
                work_board_attempt_id="attempt:repair-consent",
                workflow_run_id="job:repair",
                packet=packet,
                effective_profile_id="openrouter:strategist",
                effective_upstream="openrouter",
                request_key="request:repair-2",
                expires_at=now + REPO_REPAIR_MAX_CONSENT_TTL + timedelta(seconds=1),
                db=db,
            )


@pytest.mark.asyncio
async def test_egress_consent_cannot_outlive_active_attempt_or_goal_window(async_db, tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "repo" / "src").mkdir(parents=True)
    (workspace / "repo" / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    now = datetime.now(timezone.utc)
    service = RepoRepairService(
        sandbox=RootlessDockerRepoSandbox(),
        secret_scanner=lambda value: value,
        workspace_dir=str(workspace),
        clock=lambda: now,
    )
    owner = WorkBoardOwner(principal_id="operator:single", session_id="session:expiry")
    request = _repair_input()
    async with async_db() as db:
        await _seed_canonical_repair(
            db,
            workspace,
            request,
            owner=owner,
            task_id="task:expiry",
            attempt_id="attempt:expiry",
            run_id="job:expiry",
            goal_id="goal:expiry",
            now=now,
        )
        packet = await service.inspect_and_prepare(
            request,
            owner=owner,
            work_board_task_id="task:expiry",
            work_board_attempt_id="attempt:expiry",
            workflow_run_id="job:expiry",
            goal_id="goal:expiry",
            goal_revision=1,
            db=db,
        )
        attempt = await db.get(WorkBoardAttempt, "attempt:expiry")
        root = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == "job:expiry"))).scalar_one()
        assert attempt is not None and root is not None
        attempt.lease_expires_at = now + timedelta(minutes=2)
        root.lease_expires_at = now + timedelta(minutes=2)
        root.deadline_at = now + timedelta(minutes=2)
        with pytest.raises(RepoRepairError) as blocked:
            await service.grant_egress_consent(
                owner=owner,
                work_board_task_id="task:expiry",
                work_board_attempt_id="attempt:expiry",
                workflow_run_id="job:expiry",
                packet=packet,
                effective_profile_id="openrouter",
                effective_upstream="openrouter",
                request_key="request:expiry",
                expires_at=now + timedelta(minutes=3),
                db=db,
            )
        assert blocked.value.code == "egress_consent_expired"


@pytest.mark.asyncio
async def test_inspect_rejects_changed_request_against_server_input(async_db, tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "repo" / "src").mkdir(parents=True)
    (workspace / "repo" / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    service = RepoRepairService(
        sandbox=RootlessDockerRepoSandbox(),
        secret_scanner=lambda value: value,
        workspace_dir=str(workspace),
    )
    owner = WorkBoardOwner(principal_id="operator:single", session_id="session:forged-input")
    canonical = _repair_input()
    async with async_db() as db:
        await _seed_canonical_repair(
            db,
            workspace,
            canonical,
            owner=owner,
            task_id="task:forged-input",
            attempt_id="attempt:forged-input",
            run_id="job:forged-input",
            goal_id="goal:forged-input",
            now=datetime.now(timezone.utc),
        )
        with pytest.raises(RepoRepairError) as blocked:
            await service.inspect_and_prepare(
                _repair_input(problem_statement="caller changed the reviewed problem"),
                owner=owner,
                work_board_task_id="task:forged-input",
                work_board_attempt_id="attempt:forged-input",
                workflow_run_id="job:forged-input",
                goal_id="goal:forged-input",
                goal_revision=1,
                db=db,
            )
        assert blocked.value.code == "repair_input_authority_changed"


@pytest.mark.asyncio
async def test_inspect_rejects_revoked_operator_session(async_db, tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "repo" / "src").mkdir(parents=True)
    (workspace / "repo" / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    now = datetime.now(timezone.utc)
    service = RepoRepairService(
        sandbox=RootlessDockerRepoSandbox(),
        secret_scanner=lambda value: value,
        workspace_dir=str(workspace),
        clock=lambda: now,
    )
    owner = WorkBoardOwner(principal_id="operator:single", session_id="session:revoked")
    async with async_db() as db:
        await _seed_canonical_repair(
            db,
            workspace,
            _repair_input(),
            owner=owner,
            task_id="task:revoked",
            attempt_id="attempt:revoked",
            run_id="job:revoked",
            goal_id="goal:revoked",
            now=now,
        )
        operator_session = await db.get(OperatorSession, owner.session_id)
        assert operator_session is not None
        operator_session.revoked_at = now
        await db.flush()
        with pytest.raises(RepoRepairError) as blocked:
            await service.inspect_and_prepare(
                _repair_input(),
                owner=owner,
                work_board_task_id="task:revoked",
                work_board_attempt_id="attempt:revoked",
                workflow_run_id="job:revoked",
                goal_id="goal:revoked",
                goal_revision=1,
                db=db,
            )
        assert blocked.value.code == "operator_session_invalid"


@pytest.mark.asyncio
async def test_inspect_rejects_goal_revision_drift(async_db, tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "repo" / "src").mkdir(parents=True)
    (workspace / "repo" / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    now = datetime.now(timezone.utc)
    service = RepoRepairService(
        sandbox=RootlessDockerRepoSandbox(),
        secret_scanner=lambda value: value,
        workspace_dir=str(workspace),
        clock=lambda: now,
    )
    owner = WorkBoardOwner(principal_id="operator:single", session_id="session:goal-drift")
    async with async_db() as db:
        await _seed_canonical_repair(
            db,
            workspace,
            _repair_input(),
            owner=owner,
            task_id="task:goal-drift",
            attempt_id="attempt:goal-drift",
            run_id="job:goal-drift",
            goal_id="goal:goal-drift",
            now=now,
        )
        goal = await db.get(Goal, "goal:goal-drift")
        assert goal is not None
        goal.revision = 2
        await db.flush()
        with pytest.raises(RepoRepairError) as blocked:
            await service.inspect_and_prepare(
                _repair_input(),
                owner=owner,
                work_board_task_id="task:goal-drift",
                work_board_attempt_id="attempt:goal-drift",
                workflow_run_id="job:goal-drift",
                goal_id="goal:goal-drift",
                goal_revision=1,
                db=db,
            )
        assert blocked.value.code == "repair_goal_authority_invalid"


@pytest.mark.asyncio
async def test_inspect_rejects_reclaimed_attempt(async_db, tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "repo" / "src").mkdir(parents=True)
    (workspace / "repo" / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    now = datetime.now(timezone.utc)
    service = RepoRepairService(
        sandbox=RootlessDockerRepoSandbox(),
        secret_scanner=lambda value: value,
        workspace_dir=str(workspace),
        clock=lambda: now,
    )
    owner = WorkBoardOwner(principal_id="operator:single", session_id="session:reclaimed")
    async with async_db() as db:
        await _seed_canonical_repair(
            db,
            workspace,
            _repair_input(),
            owner=owner,
            task_id="task:reclaimed",
            attempt_id="attempt:reclaimed",
            run_id="job:reclaimed",
            goal_id="goal:reclaimed",
            now=now,
        )
        attempt = await db.get(WorkBoardAttempt, "attempt:reclaimed")
        assert attempt is not None
        attempt.ended_at = now
        await db.flush()
        with pytest.raises(RepoRepairError) as blocked:
            await service.inspect_and_prepare(
                _repair_input(),
                owner=owner,
                work_board_task_id="task:reclaimed",
                work_board_attempt_id="attempt:reclaimed",
                workflow_run_id="job:reclaimed",
                goal_id="goal:reclaimed",
                goal_revision=1,
                db=db,
            )
        assert blocked.value.code == "repair_attempt_authority_invalid"


@pytest.mark.asyncio
async def test_generate_blocks_before_model_contact_when_sandbox_preflight_fails(
    async_db, tmp_path: Path, monkeypatch
):
    workspace = tmp_path / "workspace"
    (workspace / "repo" / "src").mkdir(parents=True)
    (workspace / "repo" / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    now = datetime.now(timezone.utc)
    sandbox = RootlessDockerRepoSandbox()
    sandbox.preflight = lambda: SimpleNamespace(
        ok=False,
        status="blocked",
        reason="resource_controller_unavailable:cpu",
        as_receipt=lambda: {
            "ok": False,
            "status": "blocked",
            "reason": "resource_controller_unavailable:cpu",
            "operator_visible": True,
        },
    )
    model_calls = 0

    def model_factory(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        return SimpleNamespace(generate=lambda *_args, **_kwargs: "{}")

    service = RepoRepairService(
        sandbox=sandbox,
        model_factory=model_factory,
        secret_scanner=lambda value: value,
        session_factory=async_db,
        workspace_dir=str(workspace),
        clock=lambda: now,
    )
    owner = WorkBoardOwner(principal_id="operator:single", session_id="session:preflight")
    request = _repair_input()
    async with async_db() as db:
        await _seed_canonical_repair(
            db,
            workspace,
            request,
            owner=owner,
            task_id="task:preflight",
            attempt_id="attempt:preflight",
            run_id="job:preflight",
            goal_id="goal:preflight",
            now=now,
        )
        packet = await service.inspect_and_prepare(
            request,
            owner=owner,
            work_board_task_id="task:preflight",
            work_board_attempt_id="attempt:preflight",
            workflow_run_id="job:preflight",
            goal_id="goal:preflight",
            goal_revision=1,
            db=db,
        )
        await db.commit()
        consent = await service.grant_egress_consent(
            owner=owner,
            work_board_task_id="task:preflight",
            work_board_attempt_id="attempt:preflight",
            workflow_run_id="job:preflight",
            packet=packet,
            effective_profile_id="openrouter",
            effective_upstream="openrouter",
            request_key="request:preflight",
            expires_at=now + timedelta(minutes=5),
            db=db,
        )
        await db.commit()
        principal = TrustPrincipal(
            principal_id=owner.principal_id,
            principal_type=PrincipalType.OPERATOR,
            grants=(AuthorityGrant.MODEL_INFERENCE,),
            session_id=owner.session_id,
            job_id="job:preflight",
            operator_session_id=owner.session_id,
        )
        with pytest.raises(RepoRepairError) as blocked:
            await service.generate_proposal(
                packet,
                request,
                owner=owner,
                principal=principal,
                lease_owner="worker:repo-repair",
                fencing_token=1,
                consent=consent,
            )
        assert blocked.value.code == "repo_sandbox_preflight_blocked"
        assert "resource_controller_unavailable:cpu" in str(blocked.value)
        assert model_calls == 0
        projection = await durable_job_repository.get_job("job:preflight")
        assert projection is not None
        checkpoint = next(
            item for item in projection["checkpoints"] if item.get("checkpoint_id") == "repo-repair-preflight:job:preflight"
        )
        assert checkpoint["payload"]["receipt"]["reason"] == "resource_controller_unavailable:cpu"


@pytest.mark.asyncio
async def test_generate_adopts_private_response_checkpoint_without_second_model_contact(
    async_db, tmp_path: Path, monkeypatch
):
    workspace = tmp_path / "workspace"
    (workspace / "repo" / "src").mkdir(parents=True)
    (workspace / "repo" / "tests").mkdir()
    (workspace / "repo" / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (workspace / "repo" / "tests" / "test_app.py").write_text(
        "def test_value():\n    assert True\n", encoding="utf-8"
    )
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    now = datetime.now(timezone.utc)
    sandbox = RootlessDockerRepoSandbox()
    sandbox.preflight = lambda: SimpleNamespace(
        ok=True,
        status="verified",
        reason="",
        as_receipt=lambda: {"ok": True, "status": "verified", "reason": "", "operator_visible": True},
    )
    model_calls = 0
    response_holder = {
        "summary": "Change the bounded value.",
        "base_snapshot_sha256": None,
        "patch_unified_diff": "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n",
        "allowed_paths": ["src/app.py", "tests/test_app.py"],
        "test_args": ["pytest", "tests/test_app.py"],
        "expected_outcome": "The focused test passes.",
    }

    class _Model:
        def generate(self, *_args, **_kwargs):
            nonlocal model_calls
            model_calls += 1
            return json.dumps(response_holder, sort_keys=True)

    service = RepoRepairService(
        sandbox=sandbox,
        secret_scanner=lambda value: value,
        session_factory=async_db,
        workspace_dir=str(workspace),
        clock=lambda: now,
    )
    owner = WorkBoardOwner(principal_id="operator:single", session_id="session:response-recovery")
    request = _repair_input()
    async with async_db() as db:
        await _seed_canonical_repair(
            db,
            workspace,
            request,
            owner=owner,
            task_id="task:response-recovery",
            attempt_id="attempt:response-recovery",
            run_id="job:response-recovery",
            goal_id="goal:response-recovery",
            now=now,
        )
        packet = await service.inspect_and_prepare(
            request,
            owner=owner,
            work_board_task_id="task:response-recovery",
            work_board_attempt_id="attempt:response-recovery",
            workflow_run_id="job:response-recovery",
            goal_id="goal:response-recovery",
            goal_revision=1,
            db=db,
        )
        response_holder["base_snapshot_sha256"] = packet.base_snapshot_sha256
        await db.commit()
        consent = await service.grant_egress_consent(
            owner=owner,
            work_board_task_id="task:response-recovery",
            work_board_attempt_id="attempt:response-recovery",
            workflow_run_id="job:response-recovery",
            packet=packet,
            effective_profile_id="openrouter",
            effective_upstream="openrouter",
            request_key="request:response-recovery",
            expires_at=now + timedelta(minutes=5),
            db=db,
        )
        await db.commit()
        principal = TrustPrincipal(
            principal_id=owner.principal_id,
            principal_type=PrincipalType.OPERATOR,
            grants=(AuthorityGrant.MODEL_INFERENCE,),
            session_id=owner.session_id,
            job_id="job:response-recovery",
            operator_session_id=owner.session_id,
        )
        original_record_checkpoint = service._record_checkpoint
        fail_final_checkpoint = True

        async def fail_after_private_response(**kwargs):
            nonlocal fail_final_checkpoint
            if (
                fail_final_checkpoint
                and kwargs.get("checkpoint_id") == "repo-repair-response:job:response-recovery"
            ):
                fail_final_checkpoint = False
                raise RepoRepairError(
                    "repair_checkpoint_unavailable",
                    "injected uncertain response checkpoint commit",
                    status_code=503,
                )
            return await original_record_checkpoint(**kwargs)

        service._record_checkpoint = fail_after_private_response
        with pytest.raises(RepoRepairError) as uncertain_checkpoint:
            await service.generate_proposal(
                packet,
                request,
                owner=owner,
                principal=principal,
                lease_owner="worker:repo-repair",
                fencing_token=1,
                consent=consent,
                model=_Model(),
            )
        assert uncertain_checkpoint.value.code == "repair_checkpoint_unavailable"
        assert model_calls == 1
        orphan_root = workspace / "artifacts" / "repo-repair" / "model"
        orphan_responses = sorted(orphan_root.glob("job:response-recovery-*.json"))
        assert len(orphan_responses) == 1
        orphan_payload = orphan_responses[0].read_bytes()
        intent_projection = await durable_job_repository.get_job("job:response-recovery")
        assert intent_projection is not None
        response_intent = next(
            item
            for item in intent_projection["checkpoints"]
            if item.get("checkpoint_id") == "repo-repair-response-intent:job:response-recovery"
        )
        assert response_intent["payload"]["response_artifact_sha256"] == hashlib.sha256(orphan_payload).hexdigest()
        assert response_intent["payload"]["response_artifact_ref"].endswith(
            f"-{hashlib.sha256(orphan_payload).hexdigest()}.json"
        )
        service._record_checkpoint = original_record_checkpoint

        orphan_responses[0].unlink()
        with pytest.raises(RepoRepairError) as missing_orphan:
            await service.generate_proposal(
                packet,
                request,
                owner=owner,
                principal=principal,
                lease_owner="worker:repo-repair",
                fencing_token=1,
                consent=consent,
                model=_Model(),
            )
        assert missing_orphan.value.code == "repair_response_recovery_required"
        assert model_calls == 1
        orphan_responses[0].write_bytes(orphan_payload)
        orphan_responses[0].chmod(0o600)

        duplicate_digest = hashlib.sha256(b"ambiguous response").hexdigest()
        duplicate_path = orphan_root / f"job:response-recovery-{duplicate_digest}.json"
        duplicate_path.write_bytes(b"ambiguous response")
        duplicate_path.chmod(0o600)
        with pytest.raises(RepoRepairError) as ambiguous_orphan:
            await service.generate_proposal(
                packet,
                request,
                owner=owner,
                principal=principal,
                lease_owner="worker:repo-repair",
                fencing_token=1,
                consent=consent,
                model=_Model(),
            )
        assert ambiguous_orphan.value.code == "repair_response_recovery_required"
        assert model_calls == 1
        duplicate_path.unlink()

        proposal = await service.generate_proposal(
            packet,
            request,
            owner=owner,
            principal=principal,
            lease_owner="worker:repo-repair",
            fencing_token=1,
            consent=consent,
            model=_Model(),
        )
        assert proposal.status == "awaiting_approval"
        assert model_calls == 1
        response_path = workspace / proposal.model_response_artifact_id
        assert response_path.is_file()

        persisted = await db.get(RepoRepairProposalRow, proposal.proposal_id)
        assert persisted is not None
        await db.delete(persisted)
        await db.commit()
        assert (
            await db.execute(
                select(RepoRepairProposalRow).where(RepoRepairProposalRow.proposal_id == proposal.proposal_id)
            )
        ).scalar_one_or_none() is None

        fail_patch_flush = True

        @asynccontextmanager
        async def failing_session():
            async with async_db() as failure_db:
                original_flush = failure_db.flush

                async def fail_patch_publication_flush(*args, **kwargs):
                    nonlocal fail_patch_flush
                    if fail_patch_flush:
                        fail_patch_flush = False
                        raise RuntimeError("injected proposal row publication failure")
                    return await original_flush(*args, **kwargs)

                monkeypatch.setattr(failure_db, "flush", fail_patch_publication_flush)
                yield failure_db

        original_session_factory = service.session_factory
        service.session_factory = failing_session
        with pytest.raises(RepoRepairError) as uncertain_patch:
            await service.generate_proposal(
                packet,
                request,
                owner=owner,
                principal=principal,
                lease_owner="worker:repo-repair",
                fencing_token=1,
                consent=consent,
                model=_Model(),
            )
        assert uncertain_patch.value.code == "repair_patch_publication_unknown"
        service.session_factory = original_session_factory
        recovered = await service.generate_proposal(
            packet,
            request,
            owner=owner,
            principal=principal,
            lease_owner="worker:repo-repair",
            fencing_token=1,
            consent=consent,
            model=_Model(),
        )
        assert recovered.status == "awaiting_approval"
        assert recovered.model_response_artifact_id == proposal.model_response_artifact_id
        assert model_calls == 1
        projection = await durable_job_repository.get_job("job:response-recovery")
        assert projection is not None
        assert any(
            item.get("checkpoint_id") == "repo-repair-response:job:response-recovery"
            for item in projection["checkpoints"]
        )
        resolved_row = await db.get(RepoRepairProposalRow, recovered.proposal_id)
        assert resolved_row is not None
        resolved_row.approval_id = "approval:response-recovery"
        approval_expires_at = recovered.expires_at
        approval_fingerprint = _repair_approval_fingerprint(resolved_row, approval_expires_at)
        resolved_row.approval_fingerprint = approval_fingerprint
        db.add(
            ApprovalRequest(
                id="approval:response-recovery",
                session_id=owner.session_id,
                owner_principal_id=owner.principal_id,
                operator_session_id=owner.session_id,
                action="repo_repair.resolve",
                tool_name="engineering.repo-repair.v1",
                status="approved",
                fingerprint=approval_fingerprint,
                expires_at=approval_expires_at,
            )
        )
        await db.commit()
        with pytest.raises(RepoRepairError) as missing_approval:
            await service.resolve_proposal(
                recovered.proposal_id,
                owner=owner,
                db=db,
                current_repository_digest=recovered.base_snapshot_digest,
            )
        assert missing_approval.value.code == "approval_not_current"
        with pytest.raises(RepoRepairError) as foreign_packet:
            await service.resolve_proposal(
                recovered.proposal_id,
                owner=owner,
                db=db,
                source_packet=packet.model_copy(update={"packet_id": "foreign:packet"}),
                current_repository_digest=recovered.base_snapshot_digest,
                approval={"id": "approval:response-recovery", "status": "approved"},
            )
        assert foreign_packet.value.code == "source_packet_binding_changed"
        approval_row = await db.get(ApprovalRequest, "approval:response-recovery")
        assert approval_row is not None
        approval_row.status = "pending"
        await db.commit()
        with pytest.raises(RepoRepairError) as forged_approval:
            await service.resolve_proposal(
                recovered.proposal_id,
                owner=owner,
                db=db,
                current_repository_digest=recovered.base_snapshot_digest,
                approval={"id": "approval:response-recovery", "status": "approved"},
            )
        assert forged_approval.value.code == "approval_not_current"
        approval_row.status = "approved"
        approval_row.tool_name = "engineering.repo-change.v1"
        await db.commit()
        with pytest.raises(RepoRepairError) as foreign_tool:
            await service.resolve_proposal(
                recovered.proposal_id,
                owner=owner,
                db=db,
                current_repository_digest=recovered.base_snapshot_digest,
                approval={"id": "approval:response-recovery", "status": "approved"},
            )
        assert foreign_tool.value.code == "approval_not_current"
        approval_row.tool_name = "engineering.repo-repair.v1"
        approval_row.action = "repo_repair.other"
        await db.commit()
        with pytest.raises(RepoRepairError) as foreign_action:
            await service.resolve_proposal(
                recovered.proposal_id,
                owner=owner,
                db=db,
                current_repository_digest=recovered.base_snapshot_digest,
                approval={"id": "approval:response-recovery", "status": "approved"},
            )
        assert foreign_action.value.code == "approval_not_current"
        approval_row.action = "repo_repair.resolve"
        await db.commit()
        original_authority_digest = resolved_row.authority_digest
        resolved_row.authority_digest = "a" * 64
        await db.commit()
        with pytest.raises(RepoRepairError) as drifted_authority:
            await service.resolve_proposal(
                recovered.proposal_id,
                owner=owner,
                db=db,
                current_repository_digest=recovered.base_snapshot_digest,
                approval={"id": "approval:response-recovery", "status": "approved"},
            )
        assert drifted_authority.value.code == "proposal_authority_changed"
        resolved_row.authority_digest = original_authority_digest
        await db.commit()
        resolved = await service.resolve_proposal(
            recovered.proposal_id,
            owner=owner,
            db=db,
            current_repository_digest=recovered.base_snapshot_digest,
            approval={"id": "approval:response-recovery", "status": "approved"},
        )
        assert resolved.proposal_id == recovered.proposal_id
        await db.delete(resolved_row)
        await db.commit()
        response_path.write_text("tampered", encoding="utf-8")
        with pytest.raises(RepoRepairError) as tampered:
            await service.generate_proposal(
                packet,
                request,
                owner=owner,
                principal=principal,
                lease_owner="worker:repo-repair",
                fencing_token=1,
                consent=consent,
                model=_Model(),
            )
        assert tampered.value.code == "private_artifact_digest_changed"
        assert model_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["session_revoked", "goal_revised"])
async def test_generate_rechecks_authority_after_preflight_before_model_contact(
    async_db, tmp_path: Path, monkeypatch, mutation: str
):
    """A mid-flight session/Goal change must prevent provider contact."""

    workspace = tmp_path / "workspace"
    (workspace / "repo" / "src").mkdir(parents=True)
    (workspace / "repo" / "tests").mkdir()
    (workspace / "repo" / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (workspace / "repo" / "tests" / "test_app.py").write_text("def test_value():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    now = datetime.now(timezone.utc)
    sandbox = RootlessDockerRepoSandbox()
    sandbox.preflight = lambda: SimpleNamespace(
        ok=True,
        status="verified",
        reason="",
        as_receipt=lambda: {"ok": True, "status": "verified", "reason": "", "operator_visible": True},
    )
    model_calls = 0

    class _Model:
        def generate(self, *_args, **_kwargs):
            nonlocal model_calls
            model_calls += 1
            return "{}"

    service = RepoRepairService(
        sandbox=sandbox,
        secret_scanner=lambda value: value,
        session_factory=async_db,
        workspace_dir=str(workspace),
        clock=lambda: now,
    )
    owner = WorkBoardOwner(principal_id="operator:single", session_id=f"session:authority-race-{mutation}")
    request = _repair_input()
    async with async_db() as db:
        await _seed_canonical_repair(
            db,
            workspace,
            request,
            owner=owner,
            task_id=f"task:authority-race-{mutation}",
            attempt_id=f"attempt:authority-race-{mutation}",
            run_id=f"job:authority-race-{mutation}",
            goal_id=f"goal:authority-race-{mutation}",
            now=now,
        )
        packet = await service.inspect_and_prepare(
            request,
            owner=owner,
            work_board_task_id=f"task:authority-race-{mutation}",
            work_board_attempt_id=f"attempt:authority-race-{mutation}",
            workflow_run_id=f"job:authority-race-{mutation}",
            goal_id=f"goal:authority-race-{mutation}",
            goal_revision=1,
            db=db,
        )
        await db.commit()
        consent = await service.grant_egress_consent(
            owner=owner,
            work_board_task_id=f"task:authority-race-{mutation}",
            work_board_attempt_id=f"attempt:authority-race-{mutation}",
            workflow_run_id=f"job:authority-race-{mutation}",
            packet=packet,
            effective_profile_id="openrouter",
            effective_upstream="openrouter",
            request_key=f"request:authority-race-{mutation}",
            expires_at=now + timedelta(minutes=5),
            db=db,
        )
        await db.commit()
        principal = TrustPrincipal(
            principal_id=owner.principal_id,
            principal_type=PrincipalType.OPERATOR,
            grants=(AuthorityGrant.MODEL_INFERENCE,),
            session_id=owner.session_id,
            job_id=f"job:authority-race-{mutation}",
            operator_session_id=owner.session_id,
        )
        original_checkpoint = service._record_checkpoint
        changed = False

        async def mutate_after_preflight(**kwargs):
            nonlocal changed
            result = await original_checkpoint(**kwargs)
            if not changed and kwargs.get("checkpoint_id") == f"repo-repair-preflight:job:authority-race-{mutation}":
                changed = True
                # Use a second real SQLite session to model another actor's
                # committed revocation/revision while the proposal is in
                # flight; the next boundary must not trust the caller map.
                async with async_db() as race_db:
                    if mutation == "session_revoked":
                        session_row = await race_db.get(OperatorSession, owner.session_id)
                        assert session_row is not None
                        session_row.revoked_at = now
                    else:
                        goal_row = await race_db.get(Goal, f"goal:authority-race-{mutation}")
                        assert goal_row is not None
                        goal_row.revision = 2
                    await race_db.commit()
            return result

        service._record_checkpoint = mutate_after_preflight
        with pytest.raises(RepoRepairError) as blocked:
            await service.generate_proposal(
                packet,
                request,
                owner=owner,
                principal=principal,
                lease_owner="worker:repo-repair",
                fencing_token=1,
                consent=consent,
                model=_Model(),
                db=db,
            )
        expected_codes = {"operator_session_invalid", "repair_task_authority_invalid", "repair_goal_authority_invalid"}
        if mutation == "goal_revised":
            # The durable checkpoint writer may observe the same revision drift
            # first and reject its transition; that is still a bounded
            # pre-contact stop with no provider/publication.
            expected_codes.add("repair_checkpoint_unavailable")
        assert blocked.value.code in expected_codes
        assert changed is True
        assert model_calls == 0


@pytest.mark.asyncio
async def test_generate_rejects_route_drift_before_model_contact(async_db, tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "repo" / "src").mkdir(parents=True)
    (workspace / "repo" / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    now = datetime.now(timezone.utc)
    sandbox = RootlessDockerRepoSandbox()
    sandbox.preflight = lambda: SimpleNamespace(
        ok=True,
        status="verified",
        reason="",
        as_receipt=lambda: {"ok": True, "status": "verified", "reason": "", "operator_visible": True},
    )
    model_calls = 0

    class _Model:
        def generate(self, *_args, **_kwargs):
            nonlocal model_calls
            model_calls += 1
            return "{}"

    service = RepoRepairService(
        sandbox=sandbox,
        secret_scanner=lambda value: value,
        session_factory=async_db,
        workspace_dir=str(workspace),
        clock=lambda: now,
    )
    owner = WorkBoardOwner(principal_id="operator:single", session_id="session:route-drift")
    request = _repair_input()
    async with async_db() as db:
        await _seed_canonical_repair(
            db,
            workspace,
            request,
            owner=owner,
            task_id="task:route-drift",
            attempt_id="attempt:route-drift",
            run_id="job:route-drift",
            goal_id="goal:route-drift",
            now=now,
        )
        packet = await service.inspect_and_prepare(
            request,
            owner=owner,
            work_board_task_id="task:route-drift",
            work_board_attempt_id="attempt:route-drift",
            workflow_run_id="job:route-drift",
            goal_id="goal:route-drift",
            goal_revision=1,
            db=db,
        )
        await db.commit()
        consent = await service.grant_egress_consent(
            owner=owner,
            work_board_task_id="task:route-drift",
            work_board_attempt_id="attempt:route-drift",
            workflow_run_id="job:route-drift",
            packet=packet,
            effective_profile_id="openrouter:strategist",
            effective_upstream="openrouter",
            request_key="request:route-drift",
            expires_at=now + timedelta(minutes=5),
            db=db,
        )
        await db.commit()
        principal = TrustPrincipal(
            principal_id=owner.principal_id,
            principal_type=PrincipalType.OPERATOR,
            grants=(AuthorityGrant.MODEL_INFERENCE,),
            session_id=owner.session_id,
            job_id="job:route-drift",
            operator_session_id=owner.session_id,
        )
        consent.state = "revoked"
        await db.commit()
        with pytest.raises(RepoRepairError) as revoked:
            await service.generate_proposal(
                packet,
                request,
                owner=owner,
                principal=principal,
                lease_owner="worker:repo-repair",
                fencing_token=1,
                consent=consent,
                model=_Model(),
            )
        assert revoked.value.code == "egress_consent_authority_invalid"
        consent.state = "active"
        await db.commit()
        with pytest.raises(RepoRepairError) as blocked:
            await service.generate_proposal(
                packet,
                request,
                owner=owner,
                principal=principal,
                lease_owner="worker:repo-repair",
                fencing_token=1,
                consent=consent,
                model=_Model(),
            )
        assert blocked.value.code == "model_route_consent_mismatch"
        assert model_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["goal", "session", "attempt_cancelled", "attempt_fenced"])
async def test_generate_quarantines_response_when_authority_changes_after_model_contact(
    async_db, tmp_path: Path, monkeypatch, mutation: str
):
    """A post-contact authority change keeps one remote effect recoverable.

    The provider response may already exist when a Goal revision, operator
    session, or attempt lease changes.  The stale response must never become a
    proposal or patch, while its exact response evidence remains private and
    the durable remote intent prevents a second model call on a retry.
    """

    suffix = mutation
    job_id = f"job:post-contact-{suffix}"
    task_id = f"task:post-contact-{suffix}"
    attempt_id = f"attempt:post-contact-{suffix}"
    goal_id = f"goal:post-contact-{suffix}"
    session_id = f"session:post-contact-{suffix}"
    operation_id = f"remote:{job_id}"
    workspace = tmp_path / "workspace"
    (workspace / "repo" / "src").mkdir(parents=True)
    (workspace / "repo" / "tests").mkdir()
    (workspace / "repo" / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (workspace / "repo" / "tests" / "test_app.py").write_text(
        "def test_value():\n    assert True\n", encoding="utf-8"
    )
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    now = datetime.now(timezone.utc)
    sandbox = RootlessDockerRepoSandbox()
    sandbox.preflight = lambda: SimpleNamespace(
        ok=True,
        status="verified",
        reason="",
        as_receipt=lambda: {"ok": True, "status": "verified", "reason": "", "operator_visible": True},
    )
    model_calls = 0
    response_holder = {
        "summary": "Change the bounded value.",
        "base_snapshot_sha256": None,
        "patch_unified_diff": "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n",
        "allowed_paths": ["src/app.py", "tests/test_app.py"],
        "test_args": ["pytest", "tests/test_app.py"],
        "expected_outcome": "The focused test passes.",
    }

    service = RepoRepairService(
        sandbox=sandbox,
        secret_scanner=lambda value: value,
        session_factory=async_db,
        workspace_dir=str(workspace),
        clock=lambda: now,
    )
    owner = WorkBoardOwner(principal_id="operator:single", session_id=session_id)
    request = _repair_input()

    class _Model:
        def generate(self, *_args, **_kwargs):
            nonlocal model_calls
            model_calls += 1
            asyncio.run(
                durable_job_repository.record_remote_inference_intent(
                    operation_id=operation_id,
                    job_id=job_id,
                    owner_id=owner.principal_id,
                    runtime_path="strategist_agent",
                    profile_id="openrouter",
                    capability_version="1",
                    owner="worker:repo-repair",
                    fencing_token=1,
                )
            )
            return json.dumps(response_holder, sort_keys=True)

    async with async_db() as db:
        await _seed_canonical_repair(
            db,
            workspace,
            request,
            owner=owner,
            task_id=task_id,
            attempt_id=attempt_id,
            run_id=job_id,
            goal_id=goal_id,
            now=now,
        )
        packet = await service.inspect_and_prepare(
            request,
            owner=owner,
            work_board_task_id=task_id,
            work_board_attempt_id=attempt_id,
            workflow_run_id=job_id,
            goal_id=goal_id,
            goal_revision=1,
            db=db,
        )
        response_holder["base_snapshot_sha256"] = packet.base_snapshot_sha256
        await db.commit()
        consent = await service.grant_egress_consent(
            owner=owner,
            work_board_task_id=task_id,
            work_board_attempt_id=attempt_id,
            workflow_run_id=job_id,
            packet=packet,
            effective_profile_id="openrouter",
            effective_upstream="openrouter",
            request_key=f"request:post-contact-{suffix}",
            expires_at=now + timedelta(minutes=5),
            db=db,
        )
        consent_id = str(consent.id)
        await db.commit()
        principal = TrustPrincipal(
            principal_id=owner.principal_id,
            principal_type=PrincipalType.OPERATOR,
            grants=(AuthorityGrant.MODEL_INFERENCE,),
            session_id=owner.session_id,
            job_id=job_id,
            operator_session_id=owner.session_id,
        )
        original_recheck = service._recheck_generation_authority
        recheck_calls = 0

        async def mutate_after_model(**kwargs):
            nonlocal recheck_calls
            recheck_calls += 1
            if recheck_calls == 2:
                if mutation == "goal":
                    goal = await db.get(Goal, goal_id)
                    assert goal is not None
                    goal.revision = 2
                elif mutation == "session":
                    session = await db.get(OperatorSession, session_id)
                    assert session is not None
                    session.revoked_at = now
                else:
                    attempt = await db.get(WorkBoardAttempt, attempt_id)
                    assert attempt is not None
                    if mutation == "attempt_cancelled":
                        attempt.cancel_requested_at = now
                    else:
                        attempt.fencing_token = 2
                await db.commit()
            return await original_recheck(**kwargs)

        service._recheck_generation_authority = mutate_after_model
        with pytest.raises(RepoRepairError) as stale:
            await service.generate_proposal(
                packet,
                request,
                owner=owner,
                principal=principal,
                lease_owner="worker:repo-repair",
                fencing_token=1,
                consent=consent,
                model=_Model(),
                db=db,
            )
        expected_code = {
            "goal": "repair_goal_authority_invalid",
            "session": "operator_session_invalid",
            "attempt_cancelled": "repair_attempt_authority_invalid",
            "attempt_fenced": "repair_lease_stale",
        }[mutation]
        assert stale.value.code in {expected_code, "repair_authority_changed"}
        assert model_calls == 1
        assert (
            await db.execute(
                select(RepoRepairProposalRow).where(
                    RepoRepairProposalRow.workflow_run_id == job_id
                )
            )
        ).scalar_one_or_none() is None
        projection = await durable_job_repository.get_job(job_id)
        assert projection is not None
        effects = projection["effects"]
        remote_effect = next(
            item for item in effects if item.get("effect_id") == f"remote_inference:{operation_id}"
        )
        assert remote_effect["status"] == "intent"
        response_intent = next(
            item
            for item in projection["checkpoints"]
            if item.get("checkpoint_id") == f"repo-repair-response-intent:{job_id}"
        )
        assert response_intent["payload"]["operation_id"] == operation_id
        assert response_intent["payload"]["learning"] == "no_learning"
        response_files = sorted(
            (workspace / "artifacts" / "repo-repair" / "model").glob(f"{job_id}-*.json")
        )
        assert len(response_files) == 1

        service._recheck_generation_authority = original_recheck
        retry_consent = await db.get(RepoRepairEgressConsent, consent_id)
        assert retry_consent is not None
        with pytest.raises(RepoRepairError) as replay:
            await service.generate_proposal(
                packet,
                request,
                owner=owner,
                principal=principal,
                lease_owner="worker:repo-repair",
                fencing_token=1,
                consent=retry_consent,
                model=_Model(),
                db=db,
            )
        assert replay.value.code == expected_code
        assert model_calls == 1


def test_repo_repair_principal_type_is_explicit_for_model_route():
    principal = TrustPrincipal(
        principal_id="operator:test-repair",
        principal_type=PrincipalType.OPERATOR,
        grants=(AuthorityGrant.MODEL_INFERENCE,),
        session_id="execution:repair",
        job_id="job:repair",
        operator_session_id="session:test-repair",
    )
    assert principal.job_id == "job:repair"
