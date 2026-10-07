"""Native local repository-repair vertical receipts.

These tests deliberately keep the model boundary narrow: the governed
OpenRouter transport is replaced with a deterministic response, while the
input producer, dispatcher, durable job store, approval, selected executor,
worker process, artifacts, and readback all remain real.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any
import uuid

import pytest
import pytest_asyncio
from sqlmodel import select

from config.settings import RepoSandboxSettings, settings
from src.auth.service import create_session
from src.db.models import Goal, RepoRepairProposal as RepoRepairProposalRow, RepoRepairSourcePacket as RepoRepairSourcePacketRow, WorkBoardAttempt, WorkBoardTask, WorkBoardStatus
from src.execution.repo_sandbox import LocalRepoRepairExecutor, RepoSandboxJob, build_repo_repair_executor
from src.goals.contracts import GoalAdmissionBudget
from src.goals.repository import serialize_admission_budget
from src.model_fabric import remote_inference_admission_broker
from src.model_fabric.configuration import (
    ModelFabricConfiguration,
    OpenRouterSetup,
    openrouter_profile_for_setup,
    write_model_fabric_configuration,
)
from src.model_fabric.contracts import EndpointClass
from src.model_fabric.proofs import build_model_route_proof
from src.model_fabric.receipts import RouteReceipt
from src.model_fabric.repository import model_fabric_repository
from src.security.trust_contract import EgressClass
from src.work_board.contracts import WorkBoardInputArtifactCreate, WorkBoardOwner, WorkBoardTaskCreate
from src.work_board.dispatcher import WorkBoardDispatcher, registered_executor_id
from src.work_board.input_artifacts import prepare_input_artifact
from src.work_board.repository import WorkBoardRepository
from src.workflows.job_runtime import durable_job_repository
from tests.conftest import _PATCH_TARGETS
from tests.test_inference_accounting import accounting_db


CAPABILITY = "engineering.repo-repair.v1"


@pytest_asyncio.fixture
async def async_db(accounting_db, monkeypatch):
    """Route native APIs and workers to the existing canonical ledger fixture."""

    _root, _engine, factory = accounting_db
    sessions = factory.accounting_sessions
    for target in (*_PATCH_TARGETS, "src.workflows.repo_repair.get_session"):
        monkeypatch.setattr(target, sessions)
    yield sessions


def _configure_openrouter() -> Any:
    """Persist the real route contract and its bounded capability evidence."""

    setup = OpenRouterSetup(
        model_ids=("openrouter/anthropic/claude-sonnet-4",),
        capabilities=("text", "structured_output"),
        allowed_upstreams=("anthropic",),
        egress_class=EgressClass.CLOUD_ALLOWED_FULL,
        cloud_egress_acknowledged=True,
        spend_ceiling_microusd=25_000,
        request_cost_bound_microusd=100,
        credential_ref="env:OPENROUTER_API_KEY",
    )
    write_model_fabric_configuration(
        ModelFabricConfiguration(
            profiles=(openrouter_profile_for_setup(setup),),
            openrouter_setup=setup,
            status="ready",
        )
    )
    from src.llm_runtime import _provider_profile

    profile = _provider_profile("openrouter")
    assert profile is not None
    checked_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    probe_started = checked_at - timedelta(seconds=1)
    receipt = RouteReceipt(
        receipt_id=f"native-repair-probe-{uuid.uuid4().hex}",
        request_id=f"native-repair-request-{uuid.uuid4().hex}",
        route_decision_id=f"native-repair-decision-{uuid.uuid4().hex}",
        runtime_path="strategist_agent",
        workload="background",
        outcome="succeeded",
        egress_class=setup.egress_class.value,
        started_at=probe_started,
        finished_at=probe_started + timedelta(milliseconds=100),
        latency_ms=100,
        actual_profile_id=profile.id,
        actual_model=profile.model,
        actual_adapter=profile.transport_adapter,
        destination_class="remote",
        trust_decision_id=f"native-repair-trust-{uuid.uuid4().hex}",
    )

    async def persist_proofs() -> None:
        persisted = await model_fabric_repository.persist_route_receipt(receipt)
        assert persisted.persisted is True
        for capability, proven_value in (
            ("text", "present"),
            ("structured_output", "json"),
            ("health", "healthy"),
            ("latency_ms", 100),
        ):
            proof = build_model_route_proof(
                profile=profile,
                endpoint_class=EndpointClass.REMOTE,
                adapter=profile.transport_adapter,
                capability=capability,
                canary_version="native-repair-v1",
                outcome="passed",
                checked_at=checked_at.timestamp(),
                expires_at=checked_at.timestamp() + 3600,
                probe_receipt_id=receipt.receipt_id,
                probe_receipt_hash=receipt.receipt_hash,
                proven_value=proven_value,
            )
            persisted_proof = await model_fabric_repository.persist_capability_proof(proof)
            assert persisted_proof.persisted is True

    return persist_proofs


def _repair_input() -> dict[str, Any]:
    return {
        "repository_path": "repo",
        "problem_statement": "Fix the bounded test failure.",
        "acceptance_criteria": ["The focused test passes."],
        "source_paths": ["src/app.py"],
        "allowed_paths": ["src/app.py", "tests/test_app.py"],
        "test_args": ["pytest", "tests/test_app.py"],
    }


def _tree_receipt(root: Path) -> dict[str, Any]:
    """Hash every regular byte in a checkout, including its .git directory."""

    digest = hashlib.sha256()
    files = 0
    bytes_seen = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        payload = path.read_bytes()
        file_digest = hashlib.sha256(payload).hexdigest()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_digest.encode("ascii"))
        digest.update(b"\n")
        files += 1
        bytes_seen += len(payload)
    return {"sha256": digest.hexdigest(), "files": files, "bytes": bytes_seen}


def _pid_is_running(pid: int) -> bool:
    """Treat a missing or zombie process as quiescent for the child proof."""

    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
        state = raw.rsplit(")", 1)[1].split()[0]
    except (OSError, UnicodeDecodeError, ValueError, IndexError):
        return False
    return state != "Z"


async def _quiesce_tasks(*tasks: asyncio.Task[Any], timeout: float = 45.0) -> None:
    """Cancel and collect test-owned tasks without hiding their assertions."""

    for task in tasks:
        if not task.done():
            task.cancel()
    if not tasks:
        return
    with suppress(asyncio.CancelledError, asyncio.TimeoutError):
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout,
        )


async def _wait_for_task_set(
    *tasks: asyncio.Task[Any],
    timeout: float,
    description: str,
) -> tuple[list[Any], int]:
    """Observe real work while retaining bounded finally cleanup."""

    try:
        ticks = 0
        deadline = time.monotonic() + timeout
        while any(not task.done() for task in tasks) and time.monotonic() < deadline:
            ticks += 1
            await asyncio.sleep(0.01)
        assert all(task.done() for task in tasks), description
        return list(await asyncio.gather(*tasks)), ticks
    finally:
        await _quiesce_tasks(*tasks)


def _marker_pid(marker: Path) -> int | None:
    try:
        lines = marker.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None
    for line in reversed(lines):
        prefix, separator, value = line.rpartition(":")
        if separator and prefix in {"child", "parent"}:
            try:
                candidate = int(value)
            except ValueError:
                continue
            if candidate > 0:
                return candidate
    return None


def _write_safe_pending_projection(payload: dict[str, Any]) -> None:
    """Persist only the UI executor/approval contract from a real GET."""

    raw_posture = payload.get("executor_posture")
    posture = raw_posture if isinstance(raw_posture, dict) else {}
    raw_approval = payload.get("approval")
    approval = raw_approval if isinstance(raw_approval, dict) else {}
    raw_preflight = payload.get("preflight")
    preflight = raw_preflight if isinstance(raw_preflight, dict) else {}
    safe = {
        "status": payload.get("status"),
        "revision": payload.get("revision"),
        "recovery_action": payload.get("recovery_action"),
        "executor_kind": payload.get("executor_kind"),
        "executor_profile": payload.get("executor_profile"),
        "executor_posture": {
            key: posture.get(key)
            for key in (
                "kind",
                "profile",
                "isolation_claim",
                "network_isolation",
                "resource_enforcement",
                "limits_digest",
                "host_access",
                "local_host_execution_required",
            )
            if key in posture
        },
        "executor_posture_digest": payload.get("executor_posture_digest"),
        "required_permissions": list(payload.get("required_permissions") or []),
        "local_host_execution_required": payload.get("local_host_execution_required"),
        "preparation_ready": payload.get("preparation_ready"),
        "execution_ready": payload.get("execution_ready"),
        "preflight": {
            key: preflight.get(key)
            for key in ("status", "ok", "reason")
            if key in preflight
        },
        "approval": {
            key: approval.get(key)
            for key in (
                "status",
                "tool_name",
                "action",
                "expires_at",
                "executor_kind",
                "executor_profile",
                "executor_posture_digest",
                "required_permissions",
                "local_host_execution_required",
            )
            if key in approval
        },
        "memory_status": payload.get("memory_status"),
        "operator_visible": payload.get("operator_visible"),
    }
    receipt_path = Path("/tmp/seraph-887-native-pending-safe-projection.json")
    receipt_path.write_text(json.dumps(safe, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    receipt_path.chmod(0o600)


def _git_env(home: Path) -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(home / "gitconfig"),
    }


def _init_repository(
    workspace: Path,
    *,
    test_sleep_seconds: float = 0.8,
    nested_marker: Path | None = None,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    repository = workspace / "repo"
    (repository / "src").mkdir(parents=True)
    (repository / "tests").mkdir()
    (repository / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    if nested_marker is None:
        test_source = (
            "import time\n"
            "from pathlib import Path\n\n"
            "def test_value():\n"
            f"    time.sleep({float(test_sleep_seconds)!r})\n"
            "    assert Path('src/app.py').read_text(encoding='utf-8').strip() == 'VALUE = 2'\n"
        )
    else:
        nested_marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        nested_marker.write_text("not-started\n", encoding="utf-8")
        marker_literal = json.dumps(str(nested_marker))
        nested_child_source = (
            "import os,time\n"
            "from pathlib import Path\n"
            f"Path({marker_literal}).write_text(f'child:{{os.getpid()}}\\n', encoding='utf-8')\n"
            "time.sleep(60)\n"
        )
        test_source = (
            "import subprocess\n"
            "import sys\n"
            "import time\n"
            "from pathlib import Path\n\n"
            "def test_value():\n"
            f"    child = subprocess.Popen([sys.executable, '-c', {nested_child_source!r}], start_new_session=False)\n"
            f"    Path({marker_literal}).write_text(f'parent:{{child.pid}}\\n', encoding='utf-8')\n"
            f"    time.sleep({float(test_sleep_seconds)!r})\n"
            "    assert Path('src/app.py').read_text(encoding='utf-8').strip() == 'VALUE = 2'\n"
        )
    (repository / "tests" / "test_app.py").write_text(test_source, encoding="utf-8")
    home = workspace / "git-home"
    home.mkdir(mode=0o700)
    env = _git_env(home)
    commands = [
        ["/usr/bin/git", "-C", str(repository), "init", "--initial-branch=main"],
        ["/usr/bin/git", "-C", str(repository), "config", "user.email", "seraph-test@example.invalid"],
        ["/usr/bin/git", "-C", str(repository), "config", "user.name", "Seraph native test"],
        ["/usr/bin/git", "-C", str(repository), "add", "src/app.py", "tests/test_app.py"],
        ["/usr/bin/git", "-C", str(repository), "commit", "-m", "initial"],
    ]
    for command in commands:
        subprocess.run(command, check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    before_source = _tree_receipt(repository)
    before_git = _tree_receipt(repository / ".git")
    return repository, before_source, before_git


def _model_transport(transport_calls: list[dict[str, Any]]):
    """Return one deterministic response at the governed transport seam."""

    def proposal(base_digest: str) -> dict[str, Any]:
        return {
            "summary": "Update the bounded repository value.",
            "base_snapshot_sha256": base_digest,
            "patch_unified_diff": (
                "--- a/src/app.py\n"
                "+++ b/src/app.py\n"
                "@@ -1 +1 @@\n"
                "-VALUE = 1\n"
                "+VALUE = 2\n"
            ),
            "allowed_paths": ["src/app.py", "tests/test_app.py"],
            "test_args": ["pytest", "tests/test_app.py"],
            "expected_outcome": "The focused test passes.",
        }

    def transport(**kwargs: Any):
        body = kwargs.get("body")
        if not isinstance(body, dict):
            raise AssertionError("governed model transport did not receive an OpenAI-compatible body")
        messages = body.get("messages")
        base_digest = None
        if isinstance(messages, list):
            for message in messages:
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, str):
                    continue
                with suppress(TypeError, ValueError, json.JSONDecodeError):
                    decoded = json.loads(content)
                    if isinstance(decoded, dict) and isinstance(decoded.get("source_packet"), dict):
                        base_digest = decoded["source_packet"].get("base_snapshot_sha256")
                        break
        if not isinstance(base_digest, str) or len(base_digest) != 64:
            raise AssertionError("governed repair transport did not receive the canonical source packet")
        transport_calls.append(
            {
                "endpoint": str(getattr(getattr(kwargs.get("decision"), "selected", None), "endpoint", "")),
                "runtime_path": str(getattr(kwargs.get("context"), "runtime_path", "")),
                "model": str(body.get("model") or ""),
                "messages": len(messages) if isinstance(messages, list) else 0,
            }
        )
        content = json.dumps(proposal(base_digest), sort_keys=True)
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(role="assistant", content=content))]
        )
        return response, {"choices": [{"message": {"role": "assistant", "content": content}}], "usage": {"cost": "0.000002"}}

    return transport


async def _create_goal_task(
    *,
    async_db,
    repository: WorkBoardRepository,
    owner: WorkBoardOwner,
    goal_id: str,
    task_key: str,
    priority: int,
    request: dict[str, Any],
    now: datetime,
    scheduled_at: datetime | None = None,
) -> tuple[str, str]:
    budget = GoalAdmissionBudget(
        reviewed_grant=True,
        grant_id=f"grant:{task_key}",
        max_outstanding_jobs=1,
        max_attempts=1,
        max_runtime_seconds=120,
        period_started_at=now - timedelta(minutes=1),
        period_expires_at=now + timedelta(minutes=10),
        timezone="UTC",
    )
    async with async_db() as db:
        db.add(
            Goal(
                id=goal_id,
                title=f"Native repair goal {task_key}",
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
        capability_id=CAPABILITY,
        goal_id=goal_id,
        goal_revision=1,
        input=request,
        idempotency_key=f"input:{task_key}",
    )
    async with async_db() as db:
        metadata = await prepare_input_artifact(db, owner, typed_request, now=now)
    async with async_db() as db:
        mutation = await repository.create_task(
            db,
            owner,
            WorkBoardTaskCreate(
                title=f"Native repair {task_key}",
                goal_id=goal_id,
                goal_revision=1,
                status=WorkBoardStatus.todo,
                capability_id=CAPABILITY,
                input_artifact_id=metadata.artifact_id,
                executor_id=registered_executor_id(CAPABILITY),
                priority=priority,
                scheduled_at=scheduled_at,
                idempotency_key=f"task:{task_key}",
            ),
        )
        await db.commit()
    return mutation.task.task_id, metadata.artifact_id


async def _prepare_native_flow(
    client,
    async_db,
    tmp_path: Path,
    monkeypatch,
    *,
    low_due: bool = False,
    test_sleep_seconds: float = 0.8,
    nested_marker: Path | None = None,
):
    """Publish two real goals/tasks and pause the selected one for approval."""

    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700, exist_ok=True)
    repository_root, source_before, git_before = _init_repository(
        workspace,
        test_sleep_seconds=test_sleep_seconds,
        nested_marker=nested_marker,
    )
    workspace.chmod(0o700)
    sandbox_settings = RepoSandboxSettings(
        enabled=True,
        executor_kind="local",
        docker_socket="",
        worker_image_digest="",
    )
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    monkeypatch.setattr(settings, "repo_sandbox", sandbox_settings)
    monkeypatch.setattr(settings, "deployment_environment", "test")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "native-vertical-auth-secret")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(settings, "openrouter_provider_only", True)
    monkeypatch.setattr(settings, "openrouter_allowed_upstreams", "anthropic")
    monkeypatch.setattr(settings, "default_model", "openrouter/anthropic/claude-sonnet-4")
    persist_proofs = _configure_openrouter()
    await persist_proofs()
    await durable_job_repository.configure_inference_accounting(25_000)
    from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker

    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    monkeypatch.setattr("src.llm_runtime.gpu_admission_broker", broker)
    monkeypatch.setattr("src.model_fabric.execution.gpu_admission_broker", broker)
    monkeypatch.setattr(sys.modules[__name__], "remote_inference_admission_broker", broker)

    executor = build_repo_repair_executor()
    preflight = await asyncio.to_thread(executor.preflight)
    preflight_diagnostic = preflight.as_receipt()
    if not preflight.ok and preflight.reason == "local_runtime_unavailable":
        # Observe the original trust check without replacing its result.
        import importlib.util
        import stat

        runtime_diagnostic: dict[str, Any] = {"caller_uid": os.getuid()}
        try:
            await asyncio.to_thread(executor._local_runtime_identity)
        except (OSError, ValueError, RuntimeError) as exc:
            runtime_diagnostic["error"] = {
                "class": type(exc).__name__, "message": str(exc)[:512],
            }
        paths = {"interpreter_entry": Path(sys.executable).absolute()}
        try:
            pytest_executable = executor._local_pytest_executable()
            if pytest_executable is not None:
                paths["pytest_executable"] = Path(pytest_executable)
            spec = importlib.util.find_spec("pytest")
            if spec is not None and spec.origin not in {None, "built-in", "frozen"}:
                paths["pytest_package"] = Path(spec.origin)
        except (OSError, ValueError, ImportError) as exc:
            runtime_diagnostic["path_error"] = type(exc).__name__
        for label, path in paths.items():
            try:
                resolved = path.resolve(strict=True)
                metadata = resolved.stat()
                runtime_diagnostic[label] = {
                    "entry": str(path), "resolved": str(resolved),
                    "uid": metadata.st_uid, "gid": metadata.st_gid,
                    "nlink": metadata.st_nlink,
                    "mode": oct(stat.S_IMODE(metadata.st_mode)),
                    "regular_file": stat.S_ISREG(metadata.st_mode),
                    "executable": os.access(resolved, os.X_OK),
                }
            except OSError as exc:
                runtime_diagnostic[label] = {"error_class": type(exc).__name__}
        preflight_diagnostic["runtime_diagnostic"] = runtime_diagnostic
    assert preflight.ok, preflight_diagnostic
    assert preflight.executor_kind == "local"
    assert preflight.posture["isolation_claim"] == "none"
    assert preflight.posture["network_isolation"] == "not_verified"
    assert preflight.posture["resource_enforcement"] == "admission_and_wall_timeout_only"

    token, operator = await create_session()
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    client.cookies.set(settings.operator_auth_cookie_name, token)
    now = datetime.now(timezone.utc)
    request = _repair_input()
    repository = WorkBoardRepository()
    high_task_id, _ = await _create_goal_task(
        async_db=async_db,
        repository=repository,
        owner=owner,
        goal_id="goal:native-high",
        task_key="native-high",
        priority=90,
        request=request,
        now=now,
    )
    low_task_id, _ = await _create_goal_task(
        async_db=async_db,
        repository=repository,
        owner=owner,
        goal_id="goal:native-low",
        task_key="native-low",
        priority=10,
        request=request,
        now=now,
        scheduled_at=None if low_due else now + timedelta(hours=1),
    )
    transport_calls: list[dict[str, Any]] = []
    monkeypatch.setattr("src.llm_runtime._governed_openai_chat_completion", _model_transport(transport_calls))

    dispatcher = WorkBoardDispatcher(repository=repository, session_provider=async_db)
    first = await dispatcher.run_pass()
    expected_initial_claims = 2 if low_due else 1
    assert first["claimed"] == expected_initial_claims
    assert first["admitted"] == expected_initial_claims
    # Source inspection and code-egress review are preparation phases. They
    # may claim both due roots, but neither root has entered native execution
    # and the governed model transport remains untouched until exact consent.
    assert not transport_calls
    async with async_db() as db:
        selected_task = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == high_task_id))).scalar_one()
        waiting_task = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == low_task_id))).scalar_one()
        attempt = (
            await db.execute(
                select(WorkBoardAttempt)
                .where(WorkBoardAttempt.task_id == high_task_id)
                .order_by(WorkBoardAttempt.attempt_id.desc())
            )
        ).scalars().first()
        packet = (
            await db.execute(
                select(RepoRepairSourcePacketRow).where(RepoRepairSourcePacketRow.work_board_task_id == high_task_id)
            )
        ).scalar_one()
    assert selected_task.status is WorkBoardStatus.blocked
    assert selected_task.block_reason == "repo_repair_code_egress_review"
    if low_due:
        assert waiting_task.status is WorkBoardStatus.blocked
        assert waiting_task.block_reason == "repo_repair_code_egress_review"
    else:
        assert waiting_task.status is WorkBoardStatus.todo
    assert attempt is not None and attempt.workflow_run_id
    job_id = str(attempt.workflow_run_id)
    job = await durable_job_repository.get_job(job_id)
    assert job is not None
    assert job["status"] == "paused"
    assert not any(
        isinstance(item, dict)
        and isinstance(item.get("payload"), dict)
        and item["payload"].get("phase") in {"worker_started", "executor_dispatch_reserved"}
        for item in job["checkpoints"]
    )
    return {
        "workspace": workspace,
        "repository": repository_root,
        "source_before": source_before,
        "git_before": git_before,
        "sandbox_settings": sandbox_settings,
        "owner": owner,
        "repository_api": repository,
        "dispatcher": dispatcher,
        "transport_calls": transport_calls,
        "job_id": job_id,
        "attempt": attempt,
        "packet": packet,
        "job": job,
        "high_task_id": high_task_id,
        "low_task_id": low_task_id,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_local_native_repo_repair_api_vertical(client, async_db, tmp_path: Path, monkeypatch):
    flow = await _prepare_native_flow(client, async_db, tmp_path, monkeypatch)
    job_id = flow["job_id"]
    packet = flow["packet"]
    transport_calls = flow["transport_calls"]

    preview = await client.get(f"/api/workflows/repo-repair/{job_id}/source-preview")
    assert preview.status_code == 200, preview.text
    preview_payload = preview.json()
    assert preview_payload["provider_contacted"] is False
    assert preview_payload["source_packet"]["selected_files"][0]["path"] == "src/app.py"
    safe_status = await client.get(f"/api/workflows/repo-repair/{job_id}")
    assert safe_status.status_code == 200, safe_status.text
    assert "VALUE = 1" not in json.dumps(safe_status.json(), sort_keys=True)

    consent = await client.post(
        f"/api/workflows/repo-repair/{job_id}/code-egress-consent",
        json={
            "expected_job_revision": int(flow["job"]["revision"]),
            "source_packet_digest": packet.artifact_sha256,
            "expected_source_manifest_digest": packet.source_manifest_digest,
            "expected_profile_id": "openrouter",
            "acknowledged_selected_source": True,
            "idempotency_key": "native-egress-consent",
        },
        headers={"Origin": "http://localhost:3001"},
    )
    assert consent.status_code == 200, consent.text
    resumed = await flow["dispatcher"].run_pass()
    assert resumed["claimed"] == 0
    assert len(transport_calls) == 1
    assert transport_calls[0]["runtime_path"] == "strategist_agent"
    assert transport_calls[0]["endpoint"] == "https://openrouter.ai/api/v1/chat/completions"

    pending = await client.get(f"/api/workflows/repo-repair/{job_id}")
    assert pending.status_code == 200, pending.text
    pending_payload = pending.json()
    _write_safe_pending_projection(pending_payload)
    proposal = pending_payload["proposal"]
    approval_projection = pending_payload["approval"]
    assert pending_payload["status"] == "awaiting_approval"
    assert approval_projection["executor_kind"] == "local"
    assert approval_projection["required_permissions"] == ["local_host_execution"]
    assert approval_projection["local_host_execution_required"] is True
    assert pending_payload["memory_status"] == "no_learning"

    broker_status = await remote_inference_admission_broker.status()
    assert broker_status["active"] is None
    assert broker_status["queued"] == []
    durable_pending = await durable_job_repository.get_job(job_id)
    assert durable_pending is not None
    assert durable_pending["resource_claims"] == ["repo-repair-execution"]

    # Approval and authority are separate.  A resume before the exact approval
    # is rejected, while the safe projection remains source-free.
    before_approval = await client.post(
        f"/api/workflows/repo-repair/{job_id}/resume",
        json={
            "expected_job_revision": int(pending_payload["revision"]),
            "expected_proposal_revision": int(proposal["revision"]),
            "proposal_id": proposal["proposal_id"],
            "approval_id": proposal["approval_id"],
            "idempotency_key": "native-resume-before-approval",
        },
        headers={"Origin": "http://localhost:3001"},
    )
    assert before_approval.status_code == 409

    stale_revision = await client.post(
        f"/api/workflows/repo-repair/{job_id}/resume",
        json={
            "expected_job_revision": max(0, int(pending_payload["revision"]) - 1),
            "expected_proposal_revision": int(proposal["revision"]),
            "proposal_id": proposal["proposal_id"],
            "approval_id": proposal["approval_id"],
            "idempotency_key": "native-stale-revision",
        },
        headers={"Origin": "http://localhost:3001"},
    )
    assert stale_revision.status_code == 409

    approval = await client.post(
        f"/api/approvals/{proposal['approval_id']}/approve",
        headers={"Origin": "http://localhost:3001"},
    )
    assert approval.status_code == 200, approval.text
    assert approval.json()["status"] == "approved"

    resume = await client.post(
        f"/api/workflows/repo-repair/{job_id}/resume",
        json={
            "expected_job_revision": int(pending_payload["revision"]),
            "expected_proposal_revision": int(proposal["revision"]),
            "proposal_id": proposal["proposal_id"],
            "approval_id": proposal["approval_id"],
            "idempotency_key": "native-resume",
        },
        headers={"Origin": "http://localhost:3001"},
    )
    assert resume.status_code == 200, resume.text
    assert resume.json()["status"] in {"queued", "running"}

    # The endpoint and worker run in one event loop.  Keep a heartbeat alive
    # while the real child process executes its 0.8s test, proving the API loop
    # is not blocked by local subprocess work.
    run_task = asyncio.create_task(flow["dispatcher"].run_pass())
    completed_values, ticks = await _wait_for_task_set(
        run_task,
        timeout=45.0,
        description="native dispatcher did not settle within the bounded test window",
    )
    completed = completed_values[0]
    assert ticks >= 10, f"event loop heartbeat ran only {ticks} times"
    assert completed["claimed"] == 0
    assert len(transport_calls) == 1

    final = await client.get(f"/api/workflows/repo-repair/{job_id}")
    assert final.status_code == 200, final.text
    final_payload = final.json()
    assert final_payload["status"] == "succeeded"
    assert final_payload["executor_kind"] == "local"
    assert final_payload["executor_posture"]["isolation_claim"] == "none"
    assert final_payload["executor_posture"]["network_isolation"] == "not_verified"
    assert final_payload["memory_status"] == "no_learning"
    assert final_payload["execution"]["readback"]["verified"] is True
    assert final_payload["execution"]["artifacts"]
    assert all("VALUE = 1" not in json.dumps(item, sort_keys=True) for item in final_payload["execution"]["artifacts"])

    repository = flow["repository"]
    assert (repository / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert _tree_receipt(repository) == flow["source_before"]
    assert _tree_receipt(repository / ".git") == flow["git_before"]
    artifacts = final_payload["execution"]["artifacts"]
    readback_artifact = next(item for item in artifacts if item["artifact_type"] == "repo_change_readback_json")
    readback_path = flow["workspace"] / readback_artifact["file_path"]
    readback_bytes = readback_path.read_bytes()
    assert hashlib.sha256(readback_bytes).hexdigest() == readback_artifact["content_sha256"]
    readback = json.loads(readback_bytes.decode("utf-8"))
    assert readback["status"] == "succeeded"
    assert readback["backend_kind"] == "local"
    assert readback["source_original_unchanged"] is True
    assert readback["cleanup_proven"] is True
    staging = flow["workspace"] / "artifacts" / "repo-sandbox" / "staging"
    assert not any(staging.iterdir())
    final_job = await durable_job_repository.get_job(job_id)
    assert final_job is not None and final_job["status"] == "succeeded"
    checkpoint_ids = {str(item.get("checkpoint_id")) for item in final_job["checkpoints"] if isinstance(item, dict)}
    assert {"worker_started", "cleanup_verified", "readback_verified"}.issubset(checkpoint_ids)

    async with async_db() as db:
        task = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == flow["high_task_id"]))).scalar_one()
        waiting = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == flow["low_task_id"]))).scalar_one()
        proposal_row = (await db.execute(select(RepoRepairProposalRow).where(RepoRepairProposalRow.workflow_run_id == job_id))).scalar_one()
    assert task.status is WorkBoardStatus.done
    assert waiting.status is WorkBoardStatus.todo
    assert proposal_row.status == "consumed"

    receipt = {
        "job_id": job_id,
        "goal_id": final_payload["goal_id"],
        "attempt_id": flow["attempt"].attempt_id,
        "proposal_id": proposal["proposal_id"],
        "approval_id": proposal["approval_id"],
        "route": transport_calls[0],
        "transport_intercepted": True,
        "executor_kind": final_payload["executor_kind"],
        "local_posture": final_payload["executor_posture"],
        "posture_digest": final_payload["executor_posture_digest"],
        "source_sha256": flow["source_before"]["sha256"],
        "git_sha256": flow["git_before"]["sha256"],
        "artifact_ids": [item["artifact_id"] for item in artifacts],
        "readback_artifact_id": readback_artifact["artifact_id"],
        "readback_sha256": readback_artifact["content_sha256"],
        "readback_verified": bool(final_payload["execution"]["readback"]["verified"]),
        "cleanup_proven": bool(readback["cleanup_proven"]),
        "outcome": final_payload["status"],
        "memory_status": final_payload["memory_status"],
    }
    receipt_path = Path("/tmp/seraph-887-local-vertical-receipt.json")
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    receipt_path.chmod(0o600)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_local_native_two_goals_share_one_worker(
    client,
    async_db,
    tmp_path: Path,
    monkeypatch,
):
    """Two approved API roots on one checkout cannot run local workers together."""

    flow = await _prepare_native_flow(client, async_db, tmp_path, monkeypatch, low_due=True)
    dispatcher = flow["dispatcher"]
    high_job_id = flow["job_id"]
    high_packet = flow["packet"]
    transport_calls = flow["transport_calls"]

    async def grant_code_egress(job_id: str, job: dict[str, Any], packet: Any, key: str) -> None:
        response = await client.post(
            f"/api/workflows/repo-repair/{job_id}/code-egress-consent",
            json={
                "expected_job_revision": int(job["revision"]),
                "source_packet_digest": packet.artifact_sha256,
                "expected_source_manifest_digest": packet.source_manifest_digest,
                "expected_profile_id": "openrouter",
                "acknowledged_selected_source": True,
                "idempotency_key": key,
            },
            headers={"Origin": "http://localhost:3001"},
        )
        assert response.status_code == 200, response.text

    await grant_code_egress(high_job_id, flow["job"], high_packet, "native-high-egress-consent")
    first_resume = await dispatcher.run_pass()
    assert first_resume["claimed"] == 0
    assert len(transport_calls) == 1

    async with async_db() as db:
        low_task = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == flow["low_task_id"]))
        ).scalar_one()
        low_attempt = (
            await db.execute(
                select(WorkBoardAttempt)
                .where(WorkBoardAttempt.task_id == flow["low_task_id"])
                .order_by(WorkBoardAttempt.attempt_id.desc())
            )
        ).scalars().first()
        low_packet = (
            await db.execute(
                select(RepoRepairSourcePacketRow).where(
                    RepoRepairSourcePacketRow.work_board_task_id == flow["low_task_id"]
                )
            )
        ).scalars().first()
    assert low_task.status is WorkBoardStatus.blocked
    assert low_packet is not None
    assert low_packet.repository_ref == high_packet.repository_ref
    assert low_attempt is not None and low_attempt.workflow_run_id
    low_job_id = str(low_attempt.workflow_run_id)
    low_job = await durable_job_repository.get_job(low_job_id)
    assert low_job is not None

    await grant_code_egress(low_job_id, low_job, low_packet, "native-low-egress-consent")
    second_resume = await dispatcher.run_pass()
    assert second_resume["claimed"] == 0
    assert len(transport_calls) == 2

    high_pending = await client.get(f"/api/workflows/repo-repair/{high_job_id}")
    low_pending = await client.get(f"/api/workflows/repo-repair/{low_job_id}")
    assert high_pending.status_code == 200, high_pending.text
    assert low_pending.status_code == 200, low_pending.text
    high_payload = high_pending.json()
    low_payload = low_pending.json()
    assert high_payload["status"] == "awaiting_approval"
    assert low_payload["status"] == "awaiting_approval"
    assert high_payload["goal_id"] != low_payload["goal_id"]
    assert high_payload["proposal"]["approval_id"] != low_payload["proposal"]["approval_id"]
    assert high_payload["proposal"]["base_snapshot_digest"] == low_payload["proposal"]["base_snapshot_digest"]
    broker_status = await remote_inference_admission_broker.status()
    assert broker_status["active"] is None
    assert broker_status["queued"] == []

    async def approve_and_resume(payload: dict[str, Any], job_id: str, key: str) -> None:
        proposal = payload["proposal"]
        approval = await client.post(
            f"/api/approvals/{proposal['approval_id']}/approve",
            headers={"Origin": "http://localhost:3001"},
        )
        assert approval.status_code == 200, approval.text
        resume = await client.post(
            f"/api/workflows/repo-repair/{job_id}/resume",
            json={
                "expected_job_revision": int(payload["revision"]),
                "expected_proposal_revision": int(proposal["revision"]),
                "proposal_id": proposal["proposal_id"],
                "approval_id": proposal["approval_id"],
                "idempotency_key": key,
            },
            headers={"Origin": "http://localhost:3001"},
        )
        assert resume.status_code == 200, resume.text
        assert resume.json()["status"] in {"queued", "running"}

    await asyncio.gather(
        approve_and_resume(high_payload, high_job_id, "native-high-resume"),
        approve_and_resume(low_payload, low_job_id, "native-low-resume"),
    )

    # Two independent dispatcher instances race on the same canonical
    # workspace. The durable reservation must make exactly one native child
    # active; the other root gets a visible busy result while the child sleeps.
    dispatcher_a = WorkBoardDispatcher(
        repository=flow["repository_api"],
        session_provider=async_db,
        runner_id="native-worker-a",
    )
    dispatcher_b = WorkBoardDispatcher(
        repository=flow["repository_api"],
        session_provider=async_db,
        runner_id="native-worker-b",
    )
    pass_a = asyncio.create_task(dispatcher_a.run_pass())
    pass_b = asyncio.create_task(dispatcher_b.run_pass())
    results, ticks = await _wait_for_task_set(
        pass_a,
        pass_b,
        timeout=45.0,
        description="concurrent native dispatch did not settle",
    )
    assert ticks >= 10, f"event loop heartbeat ran only {ticks} times"
    assert all(result["claimed"] == 0 for result in results)

    high_final = await client.get(f"/api/workflows/repo-repair/{high_job_id}")
    low_final = await client.get(f"/api/workflows/repo-repair/{low_job_id}")
    assert high_final.status_code == 200, high_final.text
    assert low_final.status_code == 200, low_final.text
    final_payloads = [high_final.json(), low_final.json()]
    statuses = {payload["status"] for payload in final_payloads}
    assert statuses == {"succeeded", "blocked"}, final_payloads
    blocked_job_id = next(payload["job_id"] for payload in final_payloads if payload["status"] == "blocked")
    blocked_job = await durable_job_repository.get_job(blocked_job_id)
    assert blocked_job is not None
    assert blocked_job["failure_reason"] == "repo_repair_execution_busy"
    assert not any(
        isinstance(item, dict)
        and isinstance(item.get("payload"), dict)
        and item["payload"].get("phase") == "executor_dispatch_reserved"
        for item in blocked_job["checkpoints"]
    ), "the capacity-blocked root crossed the physical dispatch boundary"
    succeeded = next(payload for payload in final_payloads if payload["status"] == "succeeded")
    assert succeeded["execution"]["readback"]["verified"] is True
    assert succeeded["executor_kind"] == "local"
    assert flow["source_before"] == _tree_receipt(flow["repository"])
    assert flow["git_before"] == _tree_receipt(flow["repository"] / ".git")


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_local_native_api_cancel_reconciles_same_job(
    client,
    async_db,
    tmp_path: Path,
    monkeypatch,
):
    """Cancel a running native child through the authenticated board API."""

    nested_marker = tmp_path / "private-nested-child" / "worker.marker"
    flow = await _prepare_native_flow(
        client,
        async_db,
        tmp_path,
        monkeypatch,
        test_sleep_seconds=30.0,
        nested_marker=nested_marker,
    )
    job_id = flow["job_id"]
    packet = flow["packet"]
    transport_calls = flow["transport_calls"]

    consent = await client.post(
        f"/api/workflows/repo-repair/{job_id}/code-egress-consent",
        json={
            "expected_job_revision": int(flow["job"]["revision"]),
            "source_packet_digest": packet.artifact_sha256,
            "expected_source_manifest_digest": packet.source_manifest_digest,
            "expected_profile_id": "openrouter",
            "acknowledged_selected_source": True,
            "idempotency_key": "native-cancel-egress-consent",
        },
        headers={"Origin": "http://localhost:3001"},
    )
    assert consent.status_code == 200, consent.text
    await flow["dispatcher"].run_pass()
    assert len(transport_calls) == 1

    pending = await client.get(f"/api/workflows/repo-repair/{job_id}")
    assert pending.status_code == 200, pending.text
    pending_payload = pending.json()
    _write_safe_pending_projection(pending_payload)
    proposal = pending_payload["proposal"]
    approval = await client.post(
        f"/api/approvals/{proposal['approval_id']}/approve",
        headers={"Origin": "http://localhost:3001"},
    )
    assert approval.status_code == 200, approval.text
    resume = await client.post(
        f"/api/workflows/repo-repair/{job_id}/resume",
        json={
            "expected_job_revision": int(pending_payload["revision"]),
            "expected_proposal_revision": int(proposal["revision"]),
            "proposal_id": proposal["proposal_id"],
            "approval_id": proposal["approval_id"],
            "idempotency_key": "native-cancel-resume",
        },
        headers={"Origin": "http://localhost:3001"},
    )
    assert resume.status_code == 200, resume.text

    dispatch_task = asyncio.create_task(flow["dispatcher"].run_pass())
    child_pid = None
    task_before_payload = None
    cancel_sent = False
    try:
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline:
            child_pid = _marker_pid(nested_marker)
            if child_pid is not None and _pid_is_running(child_pid):
                break
            await asyncio.sleep(0.02)
        assert child_pid is not None, "native nested child did not publish its private running marker"
        assert _pid_is_running(child_pid), "native nested child was not running at the cancel boundary"

        task_before_cancel = await client.get(f"/api/work-board/tasks/{flow['high_task_id']}")
        assert task_before_cancel.status_code == 200, task_before_cancel.text
        task_before_payload = task_before_cancel.json()["task"]
        assert task_before_payload["status"] == "running"
        attempt_before = task_before_payload["latest_attempt"]
        assert attempt_before is not None
        original_attempt_id = attempt_before["attempt_id"]
        original_fence = int(attempt_before["fencing_token"])
        assert original_attempt_id
        assert original_fence > 0

        cancel = await client.post(
            f"/api/work-board/tasks/{flow['high_task_id']}/actions",
            json={
                "action": "cancel",
                "expected_revision": int(task_before_payload["task_revision"]),
            },
            headers={"Origin": "http://localhost:3001"},
        )
        assert cancel.status_code == 200, cancel.text
        cancel_sent = True
        cancel_payload = cancel.json()
        assert cancel_payload["task_id"] == flow["high_task_id"]
        assert cancel_payload["attempt_id"] == original_attempt_id
        assert cancel_payload["status"] == "blocked"
        assert cancel_payload["recovery_action"] in {"reconcile_external_effect", "retry"}

        # A fresh dispatcher must inspect the same linked root rather than create
        # a new attempt. It may settle an unproven cancellation or observe the
        # already-terminal cancel, but it must never call the model again.
        fresh_dispatcher = WorkBoardDispatcher(
            repository=flow["repository_api"],
            session_provider=async_db,
            runner_id="native-cancel-recovery",
        )
        fresh_recovery = await fresh_dispatcher.reconcile_linked_attempts()
        assert isinstance(fresh_recovery, list)
        assert len(transport_calls) == 1

        dispatch_result = await asyncio.wait_for(dispatch_task, timeout=45.0)
        assert isinstance(dispatch_result, dict)
        assert len(transport_calls) == 1

        # The durable dispatch checkpoint retains the original attempt/fence even
        # though cancellation used a later board lease to revoke ownership.
        final_job = await durable_job_repository.get_job(job_id)
        assert final_job is not None
        dispatch_checkpoint = next(
            item
            for item in final_job["checkpoints"]
            if isinstance(item, dict)
            and isinstance(item.get("payload"), dict)
            and item["payload"].get("phase") == "executor_dispatch_reserved"
        )
        dispatch_payload = dispatch_checkpoint["payload"]
        assert dispatch_payload["attempt_id"] == original_attempt_id
        # The public checkpoint payload redacts the fence, while the server-owned
        # checkpoint envelope retains the integer used for durable CAS. Compare
        # that envelope with the board attempt fence captured before cancellation.
        assert int(dispatch_checkpoint["fencing_token"]) == original_fence

        # The nested child and its staged checkout must be gone before accepting
        # either a terminal cancellation or an explicit unknown-effect recovery.
        cleanup_deadline = time.monotonic() + 45.0
        while time.monotonic() < cleanup_deadline and _pid_is_running(child_pid):
            await asyncio.sleep(0.02)
        assert not _pid_is_running(child_pid), "cancel returned while the nested child was still active"
        staging = flow["workspace"] / "artifacts" / "repo-sandbox" / "staging"
        assert not any(staging.iterdir())
        assert flow["source_before"] == _tree_receipt(flow["repository"])
        assert flow["git_before"] == _tree_receipt(flow["repository"] / ".git")
        assert len(transport_calls) == 1

        task_after = await client.get(f"/api/work-board/tasks/{flow['high_task_id']}")
        assert task_after.status_code == 200, task_after.text
        task_after_payload = task_after.json()["task"]
        assert task_after_payload["status"] == "blocked"
        assert task_after_payload["latest_attempt"]["attempt_id"] == original_attempt_id
        assert int(task_after_payload["latest_attempt"]["fencing_token"]) == original_fence
        assert task_after_payload["recovery_action"] in {"reconcile_external_effect", "retry"}

        status = str(final_job["status"])
        assert status in {"cancelled", "unknown_external_effect"}
        if status == "unknown_external_effect":
            reservation = next(
                item
                for item in final_job["checkpoints"]
                if isinstance(item, dict)
                and item.get("checkpoint_id") == "repo-repair-execution-reservation"
            )
            assert reservation["payload"]["status"] == "held"
            assert task_after_payload["recovery_action"] == "reconcile_external_effect"
    finally:
        if child_pid is not None and task_before_payload is not None and not cancel_sent:
            with suppress(Exception):
                emergency_cancel = await client.post(
                    f"/api/work-board/tasks/{flow['high_task_id']}/actions",
                    json={
                        "action": "cancel",
                        "expected_revision": int(task_before_payload["task_revision"]),
                    },
                    headers={"Origin": "http://localhost:3001"},
                )
                cancel_sent = emergency_cancel.status_code == 200
        await _quiesce_tasks(dispatch_task)
        if child_pid is not None:
            child_deadline = time.monotonic() + 45.0
            while time.monotonic() < child_deadline and _pid_is_running(child_pid):
                await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_local_native_cancel_and_fresh_instance_reconcile(tmp_path: Path, monkeypatch):
    """A real local child can be cancelled and reconciled by a fresh adapter."""

    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    repository, source_before, _ = _init_repository(workspace)
    sandbox_settings = RepoSandboxSettings(enabled=True, executor_kind="local", docker_socket="", worker_image_digest="")
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    monkeypatch.setattr(settings, "repo_sandbox", sandbox_settings)
    executor = LocalRepoRepairExecutor(config=sandbox_settings, workspace_dir=workspace)
    preflight = executor.preflight()
    assert preflight.ok, preflight.as_receipt()
    identity = preflight.info["runtime_identity"]
    patch = (
        b"--- a/src/app.py\n"
        b"+++ b/src/app.py\n"
        b"@@ -1 +1 @@\n"
        b"-VALUE = 1\n"
        b"+VALUE = 2\n"
    )
    with suppress(FileNotFoundError):
        (workspace / "tmp").mkdir(mode=0o700)
    snapshot = executor.snapshot_repository(repository, workspace / "tmp" / "cancel-snapshot")
    job = RepoSandboxJob(
        job_id="job:native-cancel",
        repository_root=str(repository),
        patch_bytes=patch,
        allowed_paths=("src/app.py", "tests/test_app.py"),
        test_args=("pytest", "tests/test_app.py"),
        authority_digest="a" * 64,
        base_digest=snapshot.digest,
        deadline_seconds=30,
        attempt_id="attempt:native-cancel",
        fencing_token=7,
        expected_posture_digest=str(preflight.posture_digest),
        expected_worker_source_sha256=identity["worker_source_sha256"],
        expected_interpreter_sha256=identity["interpreter_sha256"],
        expected_pytest_executable_sha256=identity["pytest_executable_sha256"],
        expected_pytest_package_sha256=identity["pytest_package_sha256"],
    )
    execution = asyncio.create_task(asyncio.to_thread(executor.execute_job, job))
    try:
        marker = None
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline:
            marker = executor._read_job_marker(job.job_id)
            if marker and marker.get("phase") == "worker_started":
                break
            await asyncio.sleep(0.01)
        assert marker and marker.get("phase") == "worker_started", marker
        cancel = await asyncio.to_thread(
            executor.cancel,
            job_id=job.job_id,
            authority={
                "job_id": job.job_id,
                "authority_digest": job.authority_digest,
                "attempt_id": job.attempt_id,
                "fencing_token": job.fencing_token,
            },
        )
        assert cancel["status"] == "cancel_requested"
        result = await asyncio.wait_for(execution, timeout=45.0)
    finally:
        await _quiesce_tasks(execution)
    assert result["status"] == "cancelled"
    assert result["cleanup"]["status"] == "cleanup_verified"
    assert (repository / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert _tree_receipt(repository) == source_before
    fresh = LocalRepoRepairExecutor(config=sandbox_settings, workspace_dir=workspace)
    reconciled = await asyncio.to_thread(
        fresh.reconcile,
        {"job_id": job.job_id, "authority_digest": job.authority_digest, "attempt_id": job.attempt_id, "fencing_token": job.fencing_token},
    )
    assert reconciled["status"] == "cancelled"
    assert reconciled["cleanup_proven"] is True
    assert not any((workspace / "artifacts" / "repo-sandbox" / "staging").iterdir())
