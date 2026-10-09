"""Native local repository-repair vertical receipts.

These tests deliberately keep the model boundary narrow: the governed
OpenRouter transport is replaced with a deterministic response, while the
input producer, dispatcher, durable job store, approval, selected executor,
worker process, artifacts, and readback all remain real.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import threading
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


def _publication_link_escape_diagnostic(error: BaseException) -> dict[str, Any] | None:
    # Read only the original denial's whitelisted frame values, never probe
    # the rejected path or inspect unrelated exception/frame locals.
    from src.execution.repo_publication_runtime import RuntimeUnavailable, resolve_entry

    cause: BaseException | None = error
    for _ in range(4):
        if cause is None:
            break
        if isinstance(cause, RuntimeUnavailable) and str(cause) == "publication_runtime_link_escape":
            traceback = cause.__traceback__
            for _ in range(32):
                if traceback is None:
                    break
                frame = traceback.tb_frame
                if (frame.f_code is resolve_entry.__code__
                    and frame.f_globals.get("__name__") == "src.execution.repo_publication_runtime"
                    and frame.f_code.co_name == "resolve_entry"):
                    current = frame.f_locals.get("current")
                    roots = frame.f_locals.get("roots")
                    links = frame.f_locals.get("links")
                    def bounded_path(value):
                        return isinstance(value, str) and 0 < len(value) <= 4096 and "\x00" not in value
                    if (type(current) is not type(Path()) or not bounded_path(str(current))
                        or not isinstance(roots, list) or not 1 <= len(roots) <= 2
                        or any(type(root) is not type(Path()) or not bounded_path(str(root)) for root in roots)
                        or not isinstance(links, list) or len(links) > 8):
                        return None
                    for link in links:
                        if (not isinstance(link, dict) or set(link) != {"path", "target", "identity"}
                            or not bounded_path(link["path"]) or not bounded_path(link["target"])
                            or not isinstance(link["identity"], list) or len(link["identity"]) != 9
                            or any(type(value) is not int or not 0 <= value < 2 ** 64 for value in link["identity"])):
                            return None
                    return {"rejected_path": str(current), "allowed_roots": [str(root) for root in roots],
                            "verified_links": [{"path": link["path"], "target": link["target"],
                                                "identity": list(link["identity"])} for link in links]}
                traceback = traceback.tb_next
        cause = cause.__cause__
    return None


def _publication_loaded_library_diagnostic(error: BaseException) -> dict[str, Any] | None:
    from src.execution.repo_publication_runtime import RuntimeUnavailable, loaded_libpython

    cause: BaseException | None = error
    for _ in range(4):
        if cause is None:
            break
        if isinstance(cause, RuntimeUnavailable) and str(cause) == "publication_loaded_library_identity_unproven":
            traceback = cause.__traceback__
            for _ in range(32):
                if traceback is None:
                    break
                frame = traceback.tb_frame
                if (frame.f_code is loaded_libpython.__code__
                    and frame.f_globals.get("__name__") == "src.execution.repo_publication_runtime"
                    and frame.f_code.co_name == "loaded_libpython"):
                    path = frame.f_locals.get("path")
                    device = frame.f_locals.get("device")
                    before = frame.f_locals.get("before")
                    rows = frame.f_locals.get("rows")
                    if (type(path) is not type(Path()) or not 0 < len(str(path)) <= 4096 or "\x00" in str(path)
                        or not isinstance(device, str) or not 0 < len(device) <= 32 or "\x00" in device
                        or not isinstance(before, os.stat_result)
                        or any(type(value) is not int or not 0 <= value < 2 ** 64 for value in (before.st_dev, before.st_ino))
                        or not isinstance(rows, list) or len(rows) > 64):
                        return None
                    projected_rows = []
                    for row in rows:
                        if (not isinstance(row, list) or len(row) != 6
                            or any(not isinstance(value, str) for value in row)
                            or not 0 < len(row[3]) <= 32 or "\x00" in row[3]
                            or not 0 < len(row[4]) <= 20 or not row[4].isdigit() or int(row[4]) >= 2 ** 64
                            or not 0 < len(row[5]) <= 4096 or "\x00" in row[5]):
                            return None
                        projected_rows.append({"device": row[3], "inode": row[4], "path": row[5]})
                    return {"selected_path": str(path), "expected_device": device,
                            "opened_device": before.st_dev, "opened_inode": before.st_ino,
                            "mapped_libraries": projected_rows}
                traceback = traceback.tb_next
        cause = cause.__cause__
    return None


# Reviewed original worker literal sites; output is a candidate attribution only.
_PUBLICATION_WORKER_GUARD_CANDIDATES = (
    ('process-group inspection is unavailable', 'worker_input_guard_78', '_process_group_members', 78),
    ('worker process-group cleanup exceeded the wall deadline', 'worker_input_guard_110', '_wait_process_group_quiescent', 110),
    ('worker process cleanup is unproven', 'worker_input_guard_129', '_terminate_and_reap_process', 129),
    ('worker left a nested process running', 'worker_input_guard_153', '_reject_nested_process_group', 153),
    ('snapshot directory changed or contains a symlink', 'worker_input_guard_181', '_open_directory_descriptor', 181),
    ('trusted pytest package is unavailable', 'worker_input_guard_199', '_pytest_package_identity', 199),
    ('trusted pytest package is not a regular file', 'worker_input_guard_203', '_pytest_package_identity', 203),
    ('snapshot source path is invalid', 'worker_input_guard_227', '_open_source_regular_file', 227),
    ('snapshot source is not a single-link regular file', 'worker_input_guard_240', '_open_source_regular_file', 240),
    ('snapshot source identity changed before read', 'worker_input_guard_244', '_open_source_regular_file', 244),
    ('snapshot source descriptor could not be opened', 'worker_input_guard_254', '_open_source_regular_file', 254),
    ('job input could not be read', 'worker_input_guard_269', '_read_bounded_job_json', 269),
    ('job input exceeds the fixed input limit', 'worker_input_guard_271', '_read_bounded_job_json', 271),
    ('job input is not valid UTF-8 JSON', 'worker_input_guard_275', '_read_bounded_job_json', 275),
    ('job input must be an object', 'worker_input_guard_277', '_read_bounded_job_json', 277),
    ('snapshot source changed during read', 'worker_input_guard_283', '_assert_stable_file', 283),
    ('empty or NUL path', 'worker_input_guard_289', '_safe_relative', 289),
    ('path traversal is blocked', 'worker_input_guard_292', '_safe_relative', 292),
    ('invalid relative path', 'worker_input_guard_295', '_safe_relative', 295),
    ('snapshot depth limit exceeded', 'worker_input_guard_311', '_walk_tree', 311),
    ('duplicate snapshot path', 'worker_input_guard_319', '_walk_tree', 319),
    ('snapshot directory limit exceeded', 'worker_input_guard_330', '_walk_tree', 330),
    ('snapshot file limit exceeded', 'worker_input_guard_336', '_walk_tree', 336),
    ('snapshot byte limit exceeded', 'worker_input_guard_343', '_walk_tree', 343),
    ('snapshot source could not be read', 'worker_input_guard_367', 'tree_digest', 367),
    ('publication materialization exceeds file bound', 'worker_input_guard_388', '_publication_files', 388),
    ('snapshot source could not be copied', 'worker_input_guard_413', '_copy_snapshot', 413),
    ('test_args must be a non-empty list', 'worker_input_guard_422', '_validate_test_args', 422),
    ('test argument limit exceeded', 'worker_input_guard_424', '_validate_test_args', 424),
    ('invalid test argument', 'worker_input_guard_429', '_validate_test_args', 429),
    ('test path is outside allowed_paths', 'worker_input_guard_437', '_validate_test_args', 437),
    ('pytest must name an allowed test path', 'worker_input_guard_440', '_validate_test_args', 440),
    ('allowed_paths must be a non-empty list', 'worker_input_guard_447', '_validate_allowed_paths', 447),
    ('allowed_paths limit exceeded', 'worker_input_guard_449', '_validate_allowed_paths', 449),
    ('allowed_paths entries must be non-empty strings', 'worker_input_guard_453', '_validate_allowed_paths', 453),
    ('allowed_paths entry exceeds the fixed length limit', 'worker_input_guard_455', '_validate_allowed_paths', 455),
    ('allowed_paths entry exceeds the fixed length limit', 'worker_input_guard_458', '_validate_allowed_paths', 458),
    ('allowed_paths contains duplicate entries', 'worker_input_guard_460', '_validate_allowed_paths', 460),
    ('patch byte limit exceeded', 'worker_input_guard_467', '_validate_patch_paths', 467),
    ('patch must be UTF-8', 'worker_input_guard_472', '_validate_patch_paths', 472),
    ('patch has no supported file paths', 'worker_input_guard_483', '_validate_patch_paths', 483),
    ('changed path output exceeded limit', 'worker_input_guard_496', '_validate_changed_paths', 496),
    ('git changed path output is missing approved patch paths', 'worker_input_guard_499', '_validate_changed_paths', 499),
    ('git changed path output is malformed', 'worker_input_guard_502', '_validate_changed_paths', 502),
    ('git changed path output contains an empty path', 'worker_input_guard_506', '_validate_changed_paths', 506),
    ('git changed path is not UTF-8', 'worker_input_guard_510', '_validate_changed_paths', 510),
    ('git changed path output is missing approved patch paths', 'worker_input_guard_516', '_validate_changed_paths', 516),
    ('worker command is not in the fixed profile', 'worker_input_guard_534', '_run_fixed', 534),
    ('worker wall deadline expired before process spawn', 'worker_input_guard_570', '_run_fixed', 570),
    ('worker dispatch fence rejected process spawn', 'worker_input_guard_575', '_run_fixed', 575),
    ('worker process identity observation failed', 'worker_input_guard_582', '_run_fixed', 582),
    ('worker output cleanup exceeded the wall deadline', 'worker_input_guard_610', '_join_output_threads', 610),
    ('worker wall deadline expired before process completion', 'worker_input_guard_617', '_run_fixed', 617),
    ('worker terminal identity observation failed', 'worker_input_guard_627', '_run_fixed', 627),
    ('worker terminal identity observation failed', 'worker_input_guard_637', '_run_fixed', 637),
    ('output JSON limit exceeded', 'worker_input_guard_666', '_write_json', 666),
    ('local worker output directory is not private', 'worker_input_guard_679', '_open_private_output_directory', 679),
    ('local worker output name is invalid', 'worker_input_guard_690', '_write_private_output', 690),
    ('local worker output file is not private', 'worker_input_guard_711', '_write_private_output', 711),
    ('output JSON limit exceeded', 'worker_input_guard_726', '_write_worker_output_json', 726),
    ('unsupported worker profile', 'worker_input_guard_752', 'run_job', 752),
    ('worker backend kind is invalid', 'worker_input_guard_754', 'run_job', 754),
    ('Docker worker executable is fixed', 'worker_input_guard_756', 'run_job', 756),
    ('local worker executable is invalid', 'worker_input_guard_758', 'run_job', 758),
    ('worker roots must be absolute', 'worker_input_guard_764', 'run_job', 764),
    ('patch input could not be read', 'worker_input_guard_771', 'run_job', 771),
    ('patch exceeds the fixed input limit', 'worker_input_guard_773', 'run_job', 773),
    ('worker wall deadline expired before staging', 'worker_input_guard_783', 'run_job', 783),
    ('worker wall deadline expired during staging', 'worker_input_guard_786', 'run_job', 786),
    ('snapshot digest does not match the approved preview', 'worker_input_guard_791', 'run_job', 791),
    ('base digest does not match the approved preview', 'worker_input_guard_794', 'run_job', 794),
    ('worker image digest is required', 'worker_input_guard_797', 'run_job', 797),
    ('patch digest does not match the approved artifact', 'worker_input_guard_801', 'run_job', 801),
    ('local worker runtime identity changed', 'worker_input_guard_820', 'run_job', 820),
    ('git init failed', 'worker_input_guard_839', 'run_job', 839),
    ('git add failed', 'worker_input_guard_853', 'run_job', 853),
    ('git baseline failed', 'worker_input_guard_867', 'run_job', 867),
    ('patch check failed', 'worker_input_guard_881', 'run_job', 881),
    ('patch apply failed', 'worker_input_guard_894', 'run_job', 894),
    ('changed path staging failed', 'worker_input_guard_951', 'run_job', 951),
    ('changed path export failed', 'worker_input_guard_968', 'run_job', 968),
    ('diff export failed or exceeded limit', 'worker_input_guard_984', 'run_job', 984),
    ('diff export is missing approved patch paths', 'worker_input_guard_986', 'run_job', 986),
    ('actual bounded runtime execution proof invalid', 'worker_input_guard_1021', 'run_job', 1021),
)


def _publication_worker_receipt_projection(manifest, readback) -> dict[str, Any]:
    unavailable = {"guard_candidate": "worker_input_diagnostic_unavailable"}
    for receipt in (manifest, readback):
        if (type(receipt) is not dict or set(receipt) != {"profile", "status", "reason"}
            or any(type(receipt[key]) is not str for key in ("profile", "status", "reason"))
            or receipt["profile"] != "repo-python-pytest-v1" or receipt["status"] != "blocked"
            or not 0 < len(receipt["reason"]) <= 512 or "\x00" in receipt["reason"]):
            return unavailable
    if manifest != readback:
        return unavailable
    candidates = [row for row in _PUBLICATION_WORKER_GUARD_CANDIDATES if row[0] == manifest["reason"]]
    if not candidates:
        return {"guard_candidate": "worker_input_guard_unknown"}
    if len(candidates) != 1:
        return {"guard_candidate": "worker_input_guard_ambiguous"}
    _, candidate, function, line = candidates[0]
    return {"guard_candidate": candidate, "file": "src/execution/repo_worker.py",
            "function": function, "line": line}


from src.execution import repo_worker as _publication_worker_module
from src.execution.repo_publication_runtime import BOOTSTRAP as _PUBLICATION_BOOTSTRAP

_PUBLICATION_ORIGINAL_EXECUTE = LocalRepoRepairExecutor.execute_job
_PUBLICATION_ORIGINAL_EXECUTE_CODE = _PUBLICATION_ORIGINAL_EXECUTE.__code__
_PUBLICATION_ORIGINAL_RUN_JOB_CODE = _publication_worker_module.run_job.__code__
_PUBLICATION_ORIGINAL_RUN_FIXED = _publication_worker_module._run_fixed
_PUBLICATION_BOOTSTRAP_CANDIDATES = (
    (b"actual copied libpython binding unavailable\n", "copied_runtime_bootstrap_candidate_12"),
    (b"actual copied libpython bytes changed\n", "copied_runtime_bootstrap_candidate_14"),
    (b"trusted pytest origin unavailable\n", "copied_runtime_bootstrap_candidate_21"),
    (b"undeclared trusted runner import origin\n", "copied_runtime_bootstrap_candidate_24"),
)


def _publication_worker_blocked_diagnostic(error: BaseException) -> dict[str, Any]:
    # Only already decoded original denial-frame values; never reopen receipts.
    from src.execution.repo_sandbox import LocalRepoRepairExecutor, RepoSandboxError

    unavailable = {"guard_candidate": "worker_input_diagnostic_unavailable"}
    try:
        cause = error
        for _ in range(4):
            if cause is None:
                break
            if (type(cause) is RepoSandboxError
                and cause.args == ("local worker was blocked before terminal readback",)
                and cause.phase == "output_exported"
                and cause.terminal_status == "unknown_external_effect"):
                traceback = cause.__traceback__
                for _ in range(32):
                    if traceback is None:
                        break
                    frame = traceback.tb_frame
                    if (frame.f_code is _PUBLICATION_ORIGINAL_EXECUTE_CODE
                        and frame.f_globals.get("__name__") == "src.execution.repo_sandbox"
                        and frame.f_code.co_name == "execute_job" and traceback.tb_lineno == 3782):
                        return _publication_worker_receipt_projection(
                            frame.f_locals.get("raw_manifest"), frame.f_locals.get("raw_readback"))
                    traceback = traceback.tb_next
            cause = cause.__cause__
    except Exception:
        return unavailable
    return unavailable


def _publication_bootstrap_projection(result):
    if (type(result) is not tuple or len(result) != 4 or type(result[0]) is not int
        or type(result[1]) is not bytes or type(result[2]) is not bytes or type(result[3]) is not bool
        or result[3]):
        return "copied_runtime_bootstrap_unavailable"
    if result[0] == 0:
        return None
    if len(result[2]) <= 512:
        for stderr, candidate in _PUBLICATION_BOOTSTRAP_CANDIDATES:
            if result[2] == stderr:
                return candidate
    return "copied_runtime_bootstrap_unknown"


def _publication_bootstrap_call_matches(frame, argv):
    if (frame.f_code is not _PUBLICATION_ORIGINAL_RUN_JOB_CODE
        or frame.f_globals.get("__name__") != "src.execution.repo_worker"
        or frame.f_code.co_name != "run_job" or frame.f_lineno != 908
        or type(argv) is not list or not 8 <= len(argv) <= 128
        or any(type(value) is not str or len(value) > 8192 for value in argv)
        or argv[1:6] != ["-I", "-S", "-B", "-c", _PUBLICATION_BOOTSTRAP]):
        return False
    values = frame.f_locals
    root, proof = values.get("runtime_root"), values.get("runtime_readback_path")
    return (values.get("pytest_argv") is argv and type(values.get("publication_runtime")) is dict
            and type(root) is type(Path()) and type(proof) is type(Path())
            and argv[0] == str(root / "bin/python") and argv[7] == str(proof))


@contextmanager
def _publication_worker_diagnostic_context(monkeypatch):
    # Class patch is scoped to an explicit fixture journey, never autouse.
    local = threading.local()

    def observed_run_fixed(*args, **kwargs):
        matches = False
        try:
            argv = args[0] if args else kwargs.get("argv")
            matches = (getattr(local, "active", False)
                       and _publication_bootstrap_call_matches(sys._getframe(1), argv))
        except Exception:
            pass
        result = _PUBLICATION_ORIGINAL_RUN_FIXED(*args, **kwargs)
        if matches:
            try:
                candidate = _publication_bootstrap_projection(result)
                if candidate is not None:
                    local.bootstrap = (candidate if local.bootstrap is None
                                       else "copied_runtime_bootstrap_ambiguous")
            except Exception:
                local.bootstrap = "copied_runtime_bootstrap_unavailable"
        return result

    def observed_execute(self, *args, **kwargs):
        previous = (getattr(local, "active", False), getattr(local, "bootstrap", None))
        local.active, local.bootstrap = True, None
        try:
            return _PUBLICATION_ORIGINAL_EXECUTE(self, *args, **kwargs)
        except RepoSandboxError as exc:
            try:
                diagnostic = _publication_worker_blocked_diagnostic(exc)
            except Exception:
                diagnostic = {"guard_candidate": "worker_input_diagnostic_unavailable"}
            try:
                diagnostic = dict(diagnostic)
                diagnostic["bootstrap_candidate"] = local.bootstrap or "copied_runtime_bootstrap_unknown"
                print("PUBLICATION_WORKER_BLOCKED_DIAGNOSTIC=" + json.dumps(diagnostic, sort_keys=True))
            except Exception:
                pass
            raise
        finally:
            local.active, local.bootstrap = previous

    from src.execution.repo_sandbox import RepoSandboxError
    with monkeypatch.context() as scoped:
        scoped.setattr(LocalRepoRepairExecutor, "execute_job", observed_execute)
        scoped.setattr(_publication_worker_module, "_run_fixed", observed_run_fixed)
        yield


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
        import sysconfig

        entry = Path(sys.executable).absolute()
        base = Path(sys.base_prefix).absolute()
        library_dir = sysconfig.get_config_var("LIBDIR")
        library_name = sysconfig.get_config_var("LDLIBRARY")
        library = Path(library_dir) / library_name if library_dir and library_name else None
        allowed_roots = [base, entry.parent.parent]
        runtime_diagnostic: dict[str, Any] = {
            "caller_uid": os.getuid(),
            "sys_executable": sys.executable,
            "sys_base_prefix": sys.base_prefix,
            "sysconfig_libdir": library_dir,
            "sysconfig_ldlibrary": library_name,
            "sysconfig_stdlib": sysconfig.get_path("stdlib"),
            "allowed_roots": [str(root) for root in allowed_roots],
            "configured_library_path": str(library.absolute()) if library is not None else None,
            "configured_library_within_roots": library is not None and any(
                library.absolute().is_relative_to(root) for root in allowed_roots
            ),
        }
        try:
            await asyncio.to_thread(executor._local_runtime_identity)
        except (OSError, ValueError, RuntimeError) as exc:
            runtime_diagnostic["error"] = {
                "class": type(exc).__name__, "message": str(exc)[:512],
            }
            link_escape = _publication_link_escape_diagnostic(exc)
            if link_escape is not None:
                runtime_diagnostic["original_link_escape"] = link_escape
            loaded_library = _publication_loaded_library_diagnostic(exc)
            if loaded_library is not None:
                runtime_diagnostic["original_loaded_library"] = loaded_library
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
        print("local_runtime_diagnostic=" + json.dumps(runtime_diagnostic, sort_keys=True))
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


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize("failure_kind", ["link_escape", "empty_loaded_library_rows"])
async def test_blocked_runtime_preflight_exposes_diagnostic_without_admission(
    client, async_db, tmp_path: Path, monkeypatch, capsys, failure_kind,
):
    from src.execution.repo_sandbox import RepoSandboxPreflight
    from src.execution.repo_publication_runtime import RuntimeUnavailable, loaded_libpython, metadata, resolve_entry

    trusted_root = tmp_path / "diagnostic-runtime"
    trusted_root.mkdir()
    linked_entry = trusted_root / "python"
    rejected_path = tmp_path / "outside-unattested-python"
    linked_entry.symlink_to(rejected_path)
    link_identity = list(metadata(linked_entry.lstat()))
    library_path = trusted_root / "libpython-diagnostic.so"
    library_path.write_bytes(b"finite-diagnostic-library")
    library_metadata = library_path.stat()
    original_read_text = Path.read_text

    def blocked_preflight(self):
        return RepoSandboxPreflight(
            False, "blocked", "local_runtime_unavailable", executor_kind="local",
        )

    def unavailable_identity(self):
        try:
            if failure_kind == "link_escape":
                resolve_entry(linked_entry, [trusted_root])
            else:
                def empty_maps(path, *args, **kwargs):
                    return "" if path == Path("/proc/self/maps") else original_read_text(path, *args, **kwargs)
                with monkeypatch.context() as original_rows:
                    original_rows.setattr(Path, "read_text", empty_maps)
                    loaded_libpython(library_path)
        except RuntimeUnavailable as exc:
            raise RuntimeError("syntheticRuntimeUnavailable") from exc

    monkeypatch.setattr(LocalRepoRepairExecutor, "preflight", blocked_preflight)
    monkeypatch.setattr(LocalRepoRepairExecutor, "_local_runtime_identity", unavailable_identity)
    with pytest.raises(AssertionError) as failure:
        await _prepare_native_flow(client, async_db, tmp_path, monkeypatch)
    assert "local_runtime_unavailable" in str(failure.value)
    output = capsys.readouterr().out
    line = next(line for line in output.splitlines() if line.startswith("local_runtime_diagnostic="))
    diagnostic = json.loads(line.partition("=")[2])
    assert diagnostic["error"] == {
        "class": "RuntimeError", "message": "syntheticRuntimeUnavailable",
    }
    assert diagnostic["caller_uid"] == os.getuid()
    if failure_kind == "link_escape":
        assert diagnostic["original_link_escape"] == {
            "rejected_path": str(rejected_path), "allowed_roots": [str(trusted_root)],
            "verified_links": [{"path": str(linked_entry), "target": str(rejected_path),
                                "identity": link_identity}],
        }
        assert "original_loaded_library" not in diagnostic
    else:
        assert diagnostic["original_loaded_library"] == {
            "selected_path": str(library_path),
            "expected_device": f"{os.major(library_metadata.st_dev):02x}:{os.minor(library_metadata.st_dev):02x}",
            "opened_device": library_metadata.st_dev, "opened_inode": library_metadata.st_ino,
            "mapped_libraries": [],
        }
        assert "original_link_escape" not in diagnostic
    assert not rejected_path.exists()
    import sysconfig

    assert diagnostic["sys_executable"] == sys.executable
    assert diagnostic["sys_base_prefix"] == sys.base_prefix
    assert diagnostic["sysconfig_libdir"] == sysconfig.get_config_var("LIBDIR")
    assert diagnostic["sysconfig_ldlibrary"] == sysconfig.get_config_var("LDLIBRARY")
    assert diagnostic["sysconfig_stdlib"] == sysconfig.get_path("stdlib")
    assert diagnostic["allowed_roots"] == [
        str(Path(sys.base_prefix).absolute()), str(Path(sys.executable).absolute().parent.parent),
    ]
    library = Path(sysconfig.get_config_var("LIBDIR")) / sysconfig.get_config_var("LDLIBRARY")
    assert diagnostic["configured_library_path"] == str(library.absolute())
    assert diagnostic["configured_library_within_roots"] == any(
        library.absolute().is_relative_to(Path(root)) for root in diagnostic["allowed_roots"]
    )
    assert diagnostic["interpreter_entry"]["entry"] == str(Path(sys.executable).absolute())
    for label in ("interpreter_entry", "pytest_executable", "pytest_package"):
        assert diagnostic[label]["regular_file"] is True
        assert diagnostic[label]["nlink"] >= 1
        assert isinstance(diagnostic[label]["uid"], int)
        assert diagnostic[label]["mode"].startswith("0o")
    async with async_db() as db:
        assert (await db.execute(select(WorkBoardTask))).scalars().all() == []
        assert (await db.execute(select(WorkBoardAttempt))).scalars().all() == []
