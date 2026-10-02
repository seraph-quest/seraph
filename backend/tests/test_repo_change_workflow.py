from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from config.settings import RepoSandboxSettings, settings
from src.api.workflows import (
    RepoChangeCancelRequest,
    RepoChangePreviewRequest,
    RepoChangeRetryRequest,
    _repo_change_patch_path,
    _repo_change_job_id,
    _repo_change_candidate_plan_matches,
    _repo_change_dispatch_payload,
    _repo_change_dispatch_contract,
    _repo_change_execution_authority,
    _preflight_approved_executor_for_cleanup,
    _build_repo_repair_executor_for_authority,
    _repo_change_local_finalize_pending,
    _repo_change_patch_read_blocked,
    _repo_change_reconcile_verified_local_result,
    _repo_change_read_patch,
    _repo_change_settle_cancelled_sandbox,
    _repo_change_verify_persisted_artifact,
    _repo_change_write_artifact,
    _execute_repo_change_claimed,
    _recover_repo_change_after_restart,
    _record_repo_change_goal_outcome,
    cancel_repo_change,
    preview_repo_change,
    retry_repo_change,
    _repo_change_safe_relative,
)
from src.execution.repo_sandbox import (
    RepoSandboxError,
    RepoSandboxLimits,
    RootfulDockerRepoSandbox,
    RootlessDockerRepoSandbox,
    executor_posture_digest,
    limits_digest,
)
from src.work_board.dispatcher import _assert_repo_repair_executor_authority
from src.workflows.job_runtime import DurableJobError


def test_repo_change_request_rejects_unknown_execution_controls():
    with pytest.raises(ValueError):
        RepoChangePreviewRequest(
            goal_id="goal/1",
            goal_revision=1,
            candidate_id="candidate/1",
            idempotency_key="uuid-1",
            repository_path="repos/example",
            patch_artifact_id="art_" + "a" * 24,
            patch_sha256="a" * 64,
            allowed_paths=["src/app.py"],
            test_args=["src/app.py"],
            command="rm -rf /",
        )


def test_repo_change_identity_is_deterministic_and_owner_bound():
    first = _repo_change_job_id("operator:one", "same-key")
    second = _repo_change_job_id("operator:one", "same-key")
    other_owner = _repo_change_job_id("operator:two", "same-key")
    assert first == second
    assert first != other_owner


def test_repo_repair_selector_drift_blocks_before_model_contact():
    posture = {"kind": "docker_rootful", "profile": "repo-python-pytest-v1"}
    preflight = SimpleNamespace(
        ok=True,
        posture=posture,
        posture_digest=executor_posture_digest(posture),
        as_receipt=lambda: {"ok": True, "posture": posture, "posture_digest": executor_posture_digest(posture)},
    )
    executor = SimpleNamespace(
        kind="docker_rootful",
        config=SimpleNamespace(
            profile="repo-python-pytest-v1",
            worker_image_digest="",
            docker_socket="/var/run/docker.sock",
        ),
        limits=RepoSandboxLimits(),
    )
    with pytest.raises(DurableJobError, match="executor_authority_changed"):
        _assert_repo_repair_executor_authority(
            {
                "executor_kind": "docker_rootless",
                "executor_profile": "docker_rootless:repo-python-pytest-v1",
                "executor_posture": {"kind": "docker_rootless", "profile": "repo-python-pytest-v1"},
                "executor_posture_digest": "a" * 64,
            },
            executor,
            preflight,
        )


def test_repo_repair_cleanup_rejects_fresh_posture_drift(monkeypatch: pytest.MonkeyPatch):
    expected_posture = {"kind": "docker_rootful", "profile": "repo-python-pytest-v1"}

    class ProbeConfig:
        def model_copy(self, *, update):
            return self

    class ProbeExecutor:
        kind = "docker_rootful"

        def preflight(self, authority):
            return SimpleNamespace(
                ok=True,
                executor_kind="docker_rootful",
                posture=expected_posture,
                posture_digest="b" * 64,
            )

    monkeypatch.setattr(
        "src.api.workflows.build_repo_repair_executor",
        lambda *, config: ProbeExecutor(),
    )
    with pytest.raises(RepoSandboxError, match="posture changed"):
        _preflight_approved_executor_for_cleanup(
            SimpleNamespace(kind="docker_rootful", config=ProbeConfig()),
            {
                "executor_kind": "docker_rootful",
                "executor_posture_digest": "a" * 64,
            },
        )


def test_repo_change_paths_block_escape():
    assert _repo_change_safe_relative("src/app.py", field_name="path") == "src/app.py"
    with pytest.raises(Exception):
        _repo_change_safe_relative("../secrets", field_name="path")
    with pytest.raises(Exception):
        _repo_change_safe_relative("/etc/passwd", field_name="path")


def test_cancel_reason_is_bounded():
    assert RepoChangeCancelRequest().reason == "operator_requested_stop"
    with pytest.raises(ValueError):
        RepoChangeCancelRequest(reason="x" * 161)


def test_patch_artifact_symlink_is_rejected_before_reading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    artifact_root = workspace / "artifacts" / "repo-change"
    artifact_root.mkdir(parents=True)
    outside = tmp_path / "outside.patch"
    outside.write_text("--- a/src/app.py\n+++ b/src/app.py\n", encoding="utf-8")
    artifact_id = "art_" + "a" * 24
    (artifact_root / f"{artifact_id}.patch").symlink_to(outside)
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    with pytest.raises(HTTPException) as error:
        _repo_change_patch_path(artifact_id)
    assert error.value.detail == {"code": "patch_artifact_symlink"}


def test_patch_artifact_read_is_bounded_before_allocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    artifact_root = workspace / "artifacts" / "repo-change"
    artifact_root.mkdir(parents=True)
    artifact_id = "art_" + "b" * 24
    patch_path = artifact_root / f"{artifact_id}.patch"
    patch_path.write_bytes(b"x" * (RepoSandboxLimits().max_patch_bytes + 1))
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    with pytest.raises(HTTPException) as error:
        _repo_change_read_patch(artifact_id)
    assert error.value.status_code == 413
    assert error.value.detail["code"] == "patch_artifact_too_large"


def test_patch_artifact_hardlink_is_rejected_before_reading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    artifact_root = workspace / "artifacts" / "repo-change"
    artifact_root.mkdir(parents=True)
    artifact_id = "art_" + "c" * 24
    source = artifact_root / "source.patch"
    source.write_bytes(b"--- a/src/app.py\n+++ b/src/app.py\n")
    os.link(source, artifact_root / f"{artifact_id}.patch")
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    with pytest.raises(HTTPException) as error:
        _repo_change_read_patch(artifact_id)
    assert error.value.status_code == 422
    assert error.value.detail == {"code": "patch_artifact_hardlink"}


def test_retry_contract_requires_a_typed_reconciliation_receipt():
    request = RepoChangeRetryRequest(
        reconciliation_receipt={
            "effect_id": "job-failure:repo-change-1",
            "effect_type": "job_failure",
            "target_path": "job:repo-change-1",
            "status": "read_back",
            "outcome": "no_external_effect",
        }
    )
    assert request.reconciliation_receipt["outcome"] == "no_external_effect"
    with pytest.raises(ValueError):
        RepoChangeRetryRequest(reconciliation_receipt={}, unexpected=True)


def test_candidate_proof_requires_the_active_watch_plan_revision():
    packet = SimpleNamespace(plan_revision=7)
    assert _repo_change_candidate_plan_matches(SimpleNamespace(plan_revision=7), packet)
    assert not _repo_change_candidate_plan_matches(SimpleNamespace(plan_revision=8), packet)


def test_restart_adoption_requires_the_durable_dispatch_contract():
    job_id = _repo_change_job_id("operator:one", "restart-key")
    token = RootlessDockerRepoSandbox._server_token(job_id)
    authority = {
        "base_digest": "b" * 64,
        "patch_sha256": "p" * 64,
        "image_digest": "image@sha256:" + "i" * 64,
        "limits_digest": "l" * 64,
    }
    job = {
        "job_id": job_id,
        "attempt_count": 1,
        "authority_digest": "a" * 64,
        "checkpoints": [{
            "payload": {
                "phase": "docker_dispatch_reserved",
                "job_id": job_id,
                "attempt": 1,
                "authority_digest": "a" * 64,
                **authority,
                "container_name": f"{token}-worker",
                "input_volume": f"{token}-input",
            }
        }],
    }
    valid, reason, _payload = _repo_change_dispatch_contract(job, authority)
    assert valid and reason == ""
    job["checkpoints"][0]["payload"]["attempt"] = 2
    valid, reason, _payload = _repo_change_dispatch_contract(job, authority)
    assert not valid and reason == "recovery_dispatch_contract_mismatch"
    assert _repo_change_dispatch_contract({**job, "checkpoints": []}, authority)[1] == "recovery_dispatch_fence_missing"


def test_dispatch_payload_uses_the_post_claim_attempt_count():
    current = {
        "job_id": "repo-change-" + "1" * 32,
        "attempt_count": 1,
        "authority_digest": "a" * 64,
    }
    claimed = {**current, "attempt_count": 2}
    payload = _repo_change_dispatch_payload(
        current=current,
        claimed=claimed,
        authority={
            "base_digest": "b" * 64,
            "patch_sha256": "p" * 64,
            "image_digest": "image@sha256:" + "i" * 64,
            "limits_digest": "l" * 64,
        },
        retry=False,
    )
    assert payload["attempt"] == 2
    valid, reason, _payload = _repo_change_dispatch_contract(
        {**claimed, "checkpoints": [{"payload": payload}]},
        {
            "base_digest": "b" * 64,
            "patch_sha256": "p" * 64,
            "image_digest": "image@sha256:" + "i" * 64,
            "limits_digest": "l" * 64,
        },
    )
    assert valid and reason == ""


def test_cancel_authority_keeps_original_dispatch_attempt_and_fence():
    job = {
        "job_id": "repo-change-" + "2" * 32,
        "declared_authority": {
            "executor_kind": "local",
            "attempt_id": "attempt-original",
            "authority_digest": "a" * 64,
        },
        "lease": {"owner": "service:recovery", "fencing_token": 99},
        "checkpoints": [
            {
                "payload": {
                    "phase": "executor_dispatch_reserved",
                    "attempt_id": "attempt-original",
                    "fencing_token": 7,
                }
            }
        ],
    }
    authority = _repo_change_execution_authority(job)
    assert authority["attempt_id"] == "attempt-original"
    assert authority["fencing_token"] == 7
    assert authority["fencing_token"] != job["lease"]["fencing_token"]


def test_dispatch_contract_recovers_redacted_payload_fence_from_server_envelope():
    job_id = "repo-change-" + "3" * 32
    token = RootlessDockerRepoSandbox._server_token(job_id)
    checkpoint = {
        "checkpoint_id": "executor_dispatch_reserved",
        # This top-level receipt field is the server-owned CAS envelope.
        "fencing_token": 7,
        "payload": {
            "phase": "executor_dispatch_reserved",
            "job_id": job_id,
            "attempt": 1,
            "attempt_id": "attempt-original",
            "fencing_token": "[redacted]",
            "authority_digest": "a" * 64,
            "base_digest": "b" * 64,
            "executor_kind": "local",
            "process_group_identity": f"{token}-local",
        },
    }
    job = {
        "job_id": job_id,
        "attempt_count": 1,
        "authority_digest": "a" * 64,
        "declared_authority": {
            "executor_kind": "local",
            "attempt_id": "attempt-original",
        },
        "checkpoints": [checkpoint],
    }
    authority = {
        "executor_kind": "local",
        "attempt_id": "attempt-original",
        "base_digest": "b" * 64,
    }

    valid, reason, payload = _repo_change_dispatch_contract(job, authority)
    assert valid and reason == ""
    assert payload is not None and payload["fencing_token"] == 7
    assert _repo_change_execution_authority(job, payload)["fencing_token"] == 7

    forged_payload = {**checkpoint["payload"], "fencing_token": 99}
    forged_job = {
        **job,
        "checkpoints": [{**checkpoint, "payload": forged_payload}],
    }
    forged_valid, forged_reason, _ = _repo_change_dispatch_contract(forged_job, authority)
    assert not forged_valid
    assert forged_reason == "recovery_dispatch_contract_mismatch"


def _rootful_executor_authority() -> tuple[RepoSandboxSettings, dict[str, object]]:
    image = "registry.example/seraph/repo-worker@sha256:" + "a" * 64
    config = RepoSandboxSettings(
        executor_kind="docker_rootful",
        enabled=True,
        docker_socket="unix:///run/docker.sock",
        worker_image_digest=image,
        profile="repo-python-pytest-v1",
    )
    executor = RootfulDockerRepoSandbox(config=config)
    posture = {"kind": "docker_rootful", "profile": config.profile}
    return config, {
        "executor_kind": "docker_rootful",
        "executor_profile": f"docker_rootful:{config.profile}",
        "sandbox_profile": config.profile,
        "sandbox_image_digest": image,
        "sandbox_limits_digest": limits_digest(executor.limits),
        "sandbox_socket_digest": hashlib.sha256(config.docker_socket.encode()).hexdigest(),
        "executor_posture": posture,
        "executor_posture_digest": executor_posture_digest(posture),
    }


def test_approved_rootful_executor_selection_is_provider_free(monkeypatch: pytest.MonkeyPatch):
    config, authority = _rootful_executor_authority()
    monkeypatch.setattr(
        "src.execution.repo_sandbox._effective_repo_sandbox_settings",
        lambda: config,
    )
    selected = _build_repo_repair_executor_for_authority(authority)
    assert isinstance(selected, RootfulDockerRepoSandbox)
    assert selected.kind == "docker_rootful"


def test_approved_rootful_executor_rejects_mutated_socket_before_contact(monkeypatch: pytest.MonkeyPatch):
    config, authority = _rootful_executor_authority()
    monkeypatch.setattr(
        "src.execution.repo_sandbox._effective_repo_sandbox_settings",
        lambda: config,
    )
    authority["sandbox_socket_digest"] = "f" * 64
    with pytest.raises(RepoSandboxError, match="socket changed"):
        _build_repo_repair_executor_for_authority(authority)


@pytest.mark.parametrize("existing_status", ["succeeded", "running"])
def test_preview_returns_deduped_existing_without_creating_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_status: str,
):
    workspace = tmp_path / "workspace"
    repository = workspace / "repos" / "example"
    repository.mkdir(parents=True)
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    patch = b"--- a/src/app.py\n+++ b/src/app.py\n"
    patch_sha256 = hashlib.sha256(patch).hexdigest()
    operator = SimpleNamespace(
        principal=SimpleNamespace(principal_id="operator:one"),
        session_id="session-1",
    )

    class FakeSandbox:
        config = SimpleNamespace(
            profile="repo-python-pytest-v1",
            worker_image_digest="image@sha256:" + "i" * 64,
        )
        limits = RepoSandboxLimits()

        def preflight(self):
            return SimpleNamespace(ok=True)

        def validate_snapshot_root(self, _path):
            return repository

        def snapshot_repository(self, _path, _staging):
            return SimpleNamespace(digest="b" * 64)

        @staticmethod
        def validate_image_digest(value):
            return value

    existing = {
        "status": existing_status,
        "job_id": "repo-change-" + "2" * 32,
        "receipt": {"kind": "job_admission", "status": "deduped", "terminal_noop": existing_status == "succeeded"},
        "result": {"readback_path": "artifacts/repo-change/readback.json"},
    }

    class FakeRepository:
        async def get_job(self, _job_id):
            return None

        async def admit_job(self, _spec):
            return existing

        async def queue_job(self, *_args, **_kwargs):
            raise AssertionError("a deduped terminal job must not be queued")

        async def claim_job(self, *_args, **_kwargs):
            raise AssertionError("a deduped terminal job must not be claimed")

    class FakeApprovals:
        async def get_or_create_pending(self, **_kwargs):
            raise AssertionError("a deduped terminal job must not create approval")

    monkeypatch.setattr("src.api.workflows.RootlessDockerRepoSandbox", FakeSandbox)
    monkeypatch.setattr("src.api.workflows.durable_job_repository", FakeRepository())
    monkeypatch.setattr("src.api.workflows.approval_repository", FakeApprovals())
    monkeypatch.setattr("src.api.workflows._require_authenticated_capability_operator", lambda _request: operator)

    async def candidate_proof(**_kwargs):
        return {
            "source_watch_id": "watch-1",
            "source_watch_job_id": "watch-job-1",
            "source_packet_id": "packet-1",
            "source_plan_revision": 1,
            "dossier_artifact_id": "artifact-1",
            "dossier_sha256": "d" * 64,
            "evidence_refs": ["packet-1"],
        }

    monkeypatch.setattr(
        "src.api.workflows._resolve_repo_change_candidate",
        candidate_proof,
    )
    monkeypatch.setattr("src.api.workflows._repo_change_read_patch", lambda _artifact_id: patch)

    request = RepoChangePreviewRequest(
        goal_id="goal-1",
        goal_revision=1,
        candidate_id="packet-1",
        idempotency_key="terminal-retry",
        repository_path="repos/example",
        patch_artifact_id="art_" + "a" * 24,
        patch_sha256=patch_sha256,
        allowed_paths=["src/app.py"],
        test_args=["src/app.py"],
    )
    result = asyncio.run(preview_repo_change(request, object()))
    assert result is existing
    assert result["status"] == existing_status
    assert result["receipt"]["status"] == "deduped"


def test_retry_route_records_fresh_preview_requirement_without_dispatch(monkeypatch: pytest.MonkeyPatch):
    job = {
        "job_id": "repo-change-" + "a" * 32,
        "status": "failed",
        "revision": 4,
        "attempt_count": 1,
        "owner": {"kind": "user", "principal_id": "operator:one", "service_id": None},
        "declared_authority": {"session_id": "session-1"},
        "checkpoints": [],
    }

    class FakeRepository:
        def __init__(self):
            self.record_calls = 0

        async def get_job(self, _job_id):
            return job

        async def record_recovery_checkpoint(self, _job_id, **kwargs):
            self.record_calls += 1
            job["revision"] += 1
            job["checkpoints"] = [{
                "checkpoint_id": "retry_requires_fresh_approval",
                "payload": kwargs["checkpoint_payload"],
            }]
            return job

    repository = FakeRepository()
    monkeypatch.setattr("src.api.workflows.durable_job_repository", repository)
    monkeypatch.setattr(
        "src.api.workflows._require_authenticated_capability_operator",
        lambda _request: SimpleNamespace(principal=SimpleNamespace(principal_id="operator:one"), session_id="session-1"),
    )
    request = RepoChangeRetryRequest(
        reconciliation_receipt={
            "effect_id": f"job-failure:{job['job_id']}",
            "effect_type": "job_failure",
            "target_path": f"job:{job['job_id']}",
            "status": "read_back",
            "outcome": "no_external_effect",
        }
    )
    first = asyncio.run(retry_repo_change(job["job_id"], request, object()))
    second = asyncio.run(retry_repo_change(job["job_id"], request, object()))
    assert first["reason_code"] == "retry_requires_fresh_approval"
    assert second["operator_action"] == "create_fresh_preview_and_approval"
    assert first["reconciliation_receipt_digest"] == second["reconciliation_receipt_digest"]
    assert job["checkpoints"][0]["payload"]["reconciliation_receipt"]["outcome"] == "no_external_effect"
    assert repository.record_calls == 1


def test_success_goal_outcome_uses_the_canonical_no_learning_receipt(monkeypatch: pytest.MonkeyPatch):
    captured = []

    async def capture(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr("src.api.workflows._persist_receipt_compat", capture)
    receipt = asyncio.run(
        _record_repo_change_goal_outcome(
            job={"job_id": "repo-change-" + "b" * 32, "goal_id": "goal-1", "goal_revision": 3, "candidate_id": "packet-1"},
            authority={"candidate_id": "packet-1", "base_digest": "b" * 64, "patch_sha256": "p" * 64, "evidence_refs": ["packet-1"]},
            artifact_ref="artifacts/repo-change/readback.json",
        )
    )
    assert receipt.execution_status == "succeeded"
    assert receipt.learning == "no_learning"
    assert captured[0]["event_type"] == "goal_loop_outcome"
    assert captured[0]["details"]["content_redacted"] is True


def test_local_finalize_failure_becomes_a_durable_blocked_recovery_receipt(monkeypatch: pytest.MonkeyPatch):
    job = {"job_id": "repo-change-" + "c" * 32, "status": "running", "revision": 9, "lease": {"owner": "service:repo-change", "fencing_token": 3}}
    transitions = []

    class FakeRepository:
        async def get_job(self, _job_id):
            return job

        async def transition_job(self, _job_id, status, **kwargs):
            transitions.append((status, kwargs))
            return {**job, "status": status}

    monkeypatch.setattr("src.api.workflows.durable_job_repository", FakeRepository())
    result = asyncio.run(
        _repo_change_local_finalize_pending(
            job_id=job["job_id"],
            owner="service:repo-change",
            fencing_token=3,
            revision=9,
            error_type="OSError",
        )
    )
    assert result["reason_code"] == "local_finalize_pending"
    assert transitions[0][0] == "blocked"
    assert transitions[0][1]["reason"] == "local_finalize_pending"
    assert transitions[0][1]["result"]["recovery_action"] == "local_finalize"


def test_approved_patch_read_failure_blocks_execution_job(monkeypatch: pytest.MonkeyPatch):
    job_id = "repo-change-" + "d" * 32
    job = {
        "job_id": job_id,
        "status": "running",
        "revision": 3,
        "attempt_count": 1,
        "lease": {"owner": "service:repo-change", "fencing_token": 4},
        "checkpoints": [],
    }
    authority = {
        "patch_artifact_id": "art_" + "a" * 24,
        "patch_sha256": "a" * 64,
        "allowed_paths": ["src/app.py"],
        "test_args": ["src/app.py"],
        "repository_ref": "repos/example",
        "image_digest": "image@sha256:" + "i" * 64,
        "limits_digest": "l" * 64,
    }

    class FakeRepository:
        def __init__(self):
            self.transitions = []

        async def get_job(self, _job_id):
            return dict(job)

        async def record_checkpoint(self, _job_id, **kwargs):
            job["revision"] += 1
            job["checkpoints"].append({"checkpoint_id": kwargs["checkpoint_id"]})
            return dict(job)

        async def transition_job(self, _job_id, status, **kwargs):
            self.transitions.append((status, kwargs))
            job["status"] = status
            job["revision"] += 1
            return dict(job)

    repository = FakeRepository()
    monkeypatch.setattr("src.api.workflows.durable_job_repository", repository)
    monkeypatch.setattr(
        "src.api.workflows._repo_change_read_patch",
        lambda _artifact_id: (_ for _ in ()).throw(
            HTTPException(status_code=422, detail={"code": "patch_artifact_symlink"})
        ),
    )
    result = asyncio.run(
        _execute_repo_change_claimed(
            current=job,
            authority=authority,
            claimed=job,
            approval_id="approval-1",
        )
    )
    assert result["status"] == "blocked"
    assert result["reason_code"] == "patch_artifact_symlink"
    assert result["operator_action"] == "retry_or_cancel"
    assert job["status"] == "blocked"
    assert repository.transitions[0][0] == "blocked"
    assert repository.transitions[0][1]["reason"] == "patch_artifact_symlink"
    assert repository.transitions[0][1]["result"]["operator_action"] == "retry_or_cancel"


def test_patch_read_failure_never_borrows_successor_lease(monkeypatch: pytest.MonkeyPatch):
    job_id = "repo-change-" + "1" * 32
    successor = {
        "job_id": job_id,
        "status": "running",
        "revision": 8,
        "lease": {"owner": "service:new-worker", "fencing_token": 9},
        "checkpoints": [],
    }

    class FakeRepository:
        async def get_job(self, _job_id):
            return successor

        async def transition_job(self, *_args, **_kwargs):
            raise AssertionError("a stale worker must not transition the successor")

    monkeypatch.setattr("src.api.workflows.durable_job_repository", FakeRepository())
    result = asyncio.run(
        _repo_change_patch_read_blocked(
            job_id=job_id,
            owner="service:old-worker",
            fencing_token=4,
            revision=7,
            exc=HTTPException(status_code=422, detail={"code": "patch_artifact_unavailable"}),
        )
    )
    assert result["status"] == "unknown_external_effect"
    assert result["reason_code"] == "repository_recovery_lease_changed"
    assert result["job"]["lease"] == successor["lease"]


@pytest.mark.asyncio
async def test_sandbox_cancellation_settles_before_and_after_dispatch(monkeypatch: pytest.MonkeyPatch):
    class FakeRepository:
        def __init__(self, job):
            self.job = job
            self.transitions = []

        async def get_job(self, _job_id):
            return dict(self.job)

        async def transition_job(self, _job_id, status, **kwargs):
            self.transitions.append((status, kwargs))
            self.job["status"] = status
            self.job["revision"] = int(self.job.get("revision", 0)) + 1
            return dict(self.job)

    before_job = {
        "job_id": "repo-change-" + "2" * 32,
        "status": "running",
        "revision": 3,
        "deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
        "checkpoints": [],
    }
    before_repository = FakeRepository(before_job)
    monkeypatch.setattr("src.api.workflows.durable_job_repository", before_repository)
    before_task = asyncio.create_task(asyncio.sleep(0))
    before = await _repo_change_settle_cancelled_sandbox(
        job_id=before_job["job_id"],
        owner="service:repo-change",
        fencing_token=4,
        task=before_task,
    )
    assert before["status"] == "cancelled"
    assert before["reason_code"] == "cancelled_before_dispatch"
    assert before_repository.transitions[0][0] == "cancelled"

    after_job = {
        "job_id": "repo-change-" + "3" * 32,
        "status": "running",
        "revision": 3,
        "deadline_at": (datetime.now(timezone.utc) + timedelta(milliseconds=20)).isoformat(),
        "checkpoints": [{"payload": {"phase": "docker_dispatch_reserved"}}],
    }
    after_repository = FakeRepository(after_job)
    monkeypatch.setattr("src.api.workflows.durable_job_repository", after_repository)
    release = asyncio.Event()

    async def non_cooperative_worker():
        await release.wait()

    after_task = asyncio.create_task(non_cooperative_worker())
    after = await _repo_change_settle_cancelled_sandbox(
        job_id=after_job["job_id"],
        owner="service:repo-change",
        fencing_token=4,
        task=after_task,
    )
    assert after["status"] == "unknown_external_effect"
    assert after["reason_code"] == "cancelled_after_dispatch"
    assert after["operator_action"] == "reconcile_or_cancel"
    assert after["thread_tracked"] is True
    assert after_repository.transitions[0][0] == "unknown_external_effect"
    release.set()
    await after_task


def test_claimed_boundary_cancellation_before_admission_is_terminal(monkeypatch: pytest.MonkeyPatch):
    job = {
        "job_id": "repo-change-" + "6" * 32,
        "status": "running",
        "revision": 3,
        "lease": {"owner": "service:repo-change", "fencing_token": 4},
        "checkpoints": [],
    }
    transitions = []

    class FakeRepository:
        async def get_job(self, _job_id):
            return dict(job)

        async def record_checkpoint(self, _job_id, **_kwargs):
            raise asyncio.CancelledError()

        async def transition_job(self, _job_id, status, **kwargs):
            transitions.append((status, kwargs))
            job["status"] = status
            job["revision"] += 1
            return dict(job)

    monkeypatch.setattr("src.api.workflows.durable_job_repository", FakeRepository())
    result = asyncio.run(
        _execute_repo_change_claimed(
            current=job,
            authority={},
            claimed=job,
        )
    )
    assert result["status"] == "cancelled"
    assert result["reason_code"] == "cancelled_before_dispatch"
    assert transitions[0][0] == "cancelled"


def test_claimed_boundary_cancellation_during_readback_is_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    job_id = "repo-change-" + "7" * 32
    patch = b"--- a/src/app.py\n+++ b/src/app.py\n"
    job = {
        "job_id": job_id,
        "status": "running",
        "revision": 3,
        "attempt_count": 1,
        "authority_digest": "a" * 64,
        "lease": {"owner": "service:repo-change", "fencing_token": 4},
        "checkpoints": [],
    }
    authority = {
        "base_digest": "b" * 64,
        "patch_artifact_id": "art_" + "a" * 24,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "allowed_paths": ["src/app.py"],
        "test_args": ["pytest", "src/app.py"],
        "repository_ref": "repos/example",
        "image_digest": "image@sha256:" + "i" * 64,
        "limits_digest": "l" * 64,
        "deadline_seconds": 180,
    }
    transitions = []

    class FakeRepository:
        async def get_job(self, _job_id):
            return dict(job)

        async def record_checkpoint(self, _job_id, **kwargs):
            job["revision"] += 1
            job["checkpoints"].append({"checkpoint_id": kwargs["checkpoint_id"], "payload": kwargs.get("checkpoint_payload")})
            return dict(job)

        async def record_artifact(self, _job_id, **_kwargs):
            job["revision"] += 1
            return dict(job)

        async def record_readback(self, _job_id, **_kwargs):
            raise AssertionError("readback receipt must not be written after cancellation")

        async def transition_job(self, _job_id, status, **kwargs):
            transitions.append((status, kwargs))
            job["status"] = status
            job["revision"] += 1
            return dict(job)

    def execute_job(_sandbox, sandbox_job, *, before_dispatch=None):
        if before_dispatch is not None:
            before_dispatch()
        return {
            "status": "succeeded",
            "manifest": {},
            "outputs": {"readback.json": b'{"status":"succeeded"}'},
            "checkpoint_phases": [],
        }

    monkeypatch.setattr("src.api.workflows.durable_job_repository", FakeRepository())
    monkeypatch.setattr("src.api.workflows.settings.workspace_dir", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr("src.api.workflows._repo_change_read_patch", lambda _artifact_id: patch)
    monkeypatch.setattr(RootlessDockerRepoSandbox, "execute_job", execute_job)

    def cancel_during_readback(_artifact_ref):
        raise asyncio.CancelledError()

    monkeypatch.setattr("src.api.workflows._repo_change_verify_persisted_artifact", cancel_during_readback)
    result = asyncio.run(
        _execute_repo_change_claimed(
            current=job,
            authority=authority,
            claimed=job,
            approval_id="approval-readback-cancel",
        )
    )
    assert result["status"] == "unknown_external_effect", result
    assert result["reason_code"] == "cancelled_after_dispatch"
    assert transitions[-1][0] == "unknown_external_effect"


def test_restart_cancellation_does_not_borrow_same_owner_successor_fence(monkeypatch: pytest.MonkeyPatch):
    job_id = "repo-change-" + "8" * 32
    successor = {
        "job_id": job_id,
        "status": "running",
        "revision": 8,
        "lease": {"owner": "service:repo-change:recovery", "fencing_token": 6},
        "checkpoints": [],
    }

    class FakeRepository:
        async def get_job(self, _job_id):
            return dict(successor)

        async def transition_job(self, *_args, **_kwargs):
            raise AssertionError("recovery cancellation must not transition a same-owner successor")

    async def cancelled_after_claim(*, job, operator, cancellation_claim):
        cancellation_claim.update(
            {
                "acquired": True,
                "owner": "service:repo-change:recovery",
                "fencing_token": 5,
            }
        )
        raise asyncio.CancelledError()

    monkeypatch.setattr("src.api.workflows.durable_job_repository", FakeRepository())
    monkeypatch.setattr("src.api.workflows._recover_repo_change_after_restart_inner", cancelled_after_claim)
    result = asyncio.run(
        _recover_repo_change_after_restart(
            job={"job_id": job_id, "status": "running", "lease": {"owner": "service:old", "fencing_token": 4}},
            operator=SimpleNamespace(),
        )
    )
    assert result["status"] == "running"
    assert result["reason_code"] == "cancellation_settlement_unavailable"
    assert result["job"]["lease"]["fencing_token"] == 6


def test_result_artifact_reopen_hash_rejects_tamper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    job_id = "repo-change-" + "4" * 32
    payload = b'{"status":"succeeded"}'
    relative = _repo_change_write_artifact(job_id, "readback.json", payload)
    assert _repo_change_verify_persisted_artifact(relative) == hashlib.sha256(payload).hexdigest()
    artifact = workspace / relative
    artifact.write_bytes(b'{"status":"tampered"}')
    assert _repo_change_verify_persisted_artifact(relative) != hashlib.sha256(payload).hexdigest()


def test_restart_readback_reconcile_rejects_private_mode_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    job_id = "repo-change-" + "f" * 32
    payload = b'{"status":"succeeded"}'
    relative = _repo_change_write_artifact(job_id, "readback.json", payload)
    digest = hashlib.sha256(payload).hexdigest()
    job = {
        "job_id": job_id,
        "status": "blocked",
        "revision": 4,
        "owner": {"kind": "user", "principal_id": "operator:one"},
        "effects": [
            {
                "receipt_kind": "readback",
                "status": "succeeded",
                "target_path": relative,
                "content_sha256": digest,
                "details": {"verified": True},
            }
        ],
    }
    finalized = False

    class FakeRepository:
        async def get_job(self, _job_id):
            return dict(job)

        async def finalize_reconciled_job(self, *_args, **_kwargs):
            nonlocal finalized
            finalized = True
            return {**job, "status": "succeeded"}

    monkeypatch.setattr("src.api.workflows.durable_job_repository", FakeRepository())
    artifact = workspace / relative
    artifact.chmod(0o644)

    result = asyncio.run(
        _repo_change_reconcile_verified_local_result(job=job, authority={})
    )

    assert result is None
    assert finalized is False
    assert artifact.read_bytes() == payload


def test_approved_patch_read_failure_blocks_restart_recovery_job(monkeypatch: pytest.MonkeyPatch):
    job_id = "repo-change-" + "e" * 32
    token = RootlessDockerRepoSandbox._server_token(job_id)
    authority = {
        "base_digest": "b" * 64,
        "patch_artifact_id": "art_" + "b" * 24,
        "patch_sha256": "p" * 64,
        "image_digest": "image@sha256:" + "i" * 64,
        "limits_digest": "l" * 64,
        "repository_ref": "repos/example",
        "allowed_paths": ["src/app.py"],
        "test_args": ["src/app.py"],
        "deadline_seconds": 180,
    }
    dispatch = {
        "phase": "docker_dispatch_reserved",
        "job_id": job_id,
        "attempt": 1,
        "authority_digest": "a" * 64,
        "base_digest": authority["base_digest"],
        "patch_sha256": authority["patch_sha256"],
        "image_digest": authority["image_digest"],
        "limits_digest": authority["limits_digest"],
        "container_name": f"{token}-worker",
        "input_volume": f"{token}-input",
    }
    job = {
        "job_id": job_id,
        "status": "running",
        "revision": 3,
        "attempt_count": 1,
        "authority_digest": "a" * 64,
        "declared_authority": authority,
        "lease": {"owner": "service:old", "fencing_token": 4, "expires_at": "2000-01-01T00:00:00+00:00"},
        "checkpoints": [{"payload": dispatch}],
    }

    class FakeRepository:
        def __init__(self):
            self.transitions = []
            self.job = job

        async def get_job(self, _job_id):
            return dict(self.job)

        async def transfer_lease(self, _job_id, **_kwargs):
            self.job["lease"] = {"owner": "service:repo-change", "fencing_token": 5}
            self.job["revision"] += 1
            return dict(self.job)

        async def record_checkpoint(self, _job_id, **kwargs):
            self.job["revision"] += 1
            self.job["checkpoints"].append({"checkpoint_id": kwargs["checkpoint_id"]})
            return dict(self.job)

        async def transition_job(self, _job_id, status, **kwargs):
            self.transitions.append((status, kwargs))
            self.job["status"] = status
            self.job["revision"] += 1
            return dict(self.job)

    repository = FakeRepository()
    monkeypatch.setattr("src.api.workflows.durable_job_repository", repository)
    monkeypatch.setattr(
        "src.api.workflows._repo_change_read_patch",
        lambda _artifact_id: (_ for _ in ()).throw(
            HTTPException(status_code=422, detail={"code": "patch_artifact_unavailable"})
        ),
    )
    result = asyncio.run(
        _recover_repo_change_after_restart(
            job=job,
            operator=SimpleNamespace(),
        )
    )
    assert result["status"] == "unknown_external_effect"
    assert result["reason_code"] == "patch_artifact_unavailable"
    assert result["operator_action"] == "reconcile_or_cancel"
    assert repository.job["status"] == "unknown_external_effect"
    # Recovery reserves the original root before touching the externally
    # dispatched worker; the patch error remains in the operator receipt.
    assert repository.transitions[0][1]["reason"] == "cancel_cleanup_pending"
    assert repository.transitions[0][1]["result"]["requested_reason"] == "patch_artifact_unavailable"


def test_restart_missing_worker_is_failed_with_output_lost_receipt(monkeypatch: pytest.MonkeyPatch):
    job_id = "repo-change-" + "9" * 32
    token = RootlessDockerRepoSandbox._server_token(job_id)
    patch = b"--- a/src/app.py\n+++ b/src/app.py\n"
    authority = {
        "base_digest": "b" * 64,
        "patch_artifact_id": "art_" + "d" * 24,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "image_digest": "image@sha256:" + "i" * 64,
        "limits_digest": "l" * 64,
        "repository_ref": "repos/example",
        "allowed_paths": ["src/app.py"],
        "test_args": ["src/app.py"],
        "deadline_seconds": 180,
    }
    dispatch = {
        "phase": "docker_dispatch_reserved",
        "job_id": job_id,
        "attempt": 1,
        "authority_digest": "a" * 64,
        "base_digest": authority["base_digest"],
        "patch_sha256": authority["patch_sha256"],
        "image_digest": authority["image_digest"],
        "limits_digest": authority["limits_digest"],
        "container_name": f"{token}-worker",
        "input_volume": f"{token}-input",
    }
    job = {
        "job_id": job_id,
        "status": "running",
        "revision": 3,
        "attempt_count": 1,
        "authority_digest": "a" * 64,
        "declared_authority": authority,
        "lease": {"owner": "service:old", "fencing_token": 4, "expires_at": "2000-01-01T00:00:00+00:00"},
        "checkpoints": [{"payload": dispatch}],
    }

    class FakeRepository:
        def __init__(self):
            self.job = {**job, "checkpoints": list(job["checkpoints"])}
            self.transitions = []

        async def get_job(self, _job_id):
            return dict(self.job)

        async def transfer_lease(self, _job_id, **_kwargs):
            self.job["lease"] = {"owner": "service:repo-change", "fencing_token": 5}
            self.job["revision"] += 1
            return dict(self.job)

        async def record_checkpoint(self, _job_id, **kwargs):
            self.job["revision"] += 1
            self.job["checkpoints"].append({"checkpoint_id": kwargs["checkpoint_id"], "state": kwargs["state"]})
            return dict(self.job)

        async def transition_job(self, _job_id, status, **kwargs):
            self.transitions.append((status, kwargs))
            self.job["status"] = status
            self.job["result"] = kwargs["result"]
            self.job["revision"] += 1
            return dict(self.job)

    repository = FakeRepository()
    monkeypatch.setattr("src.api.workflows.durable_job_repository", repository)
    monkeypatch.setattr("src.api.workflows._repo_change_read_patch", lambda _artifact_id: patch)

    def missing_worker(_runner, *_args, **_kwargs):
        return {
            "status": "failed",
            "reason": "output_lost",
            "reason_code": "output_lost",
            "checkpoint_phases": ["admitted", "worker_started"],
            "operator_visible": True,
        }

    monkeypatch.setattr(RootlessDockerRepoSandbox, "recover_job", missing_worker)
    result = asyncio.run(_recover_repo_change_after_restart(job=job, operator=SimpleNamespace()))

    assert result["status"] == "failed"
    assert result["reason_code"] == "output_lost"
    assert result["operator_action"] == "inspect_output_and_create_fresh_preview"
    assert result["recovery_action"] == "create_fresh_preview_and_approval"
    assert repository.job["status"] == "failed"
    assert repository.transitions[0][0] == "failed"
    assert repository.transitions[0][1]["reason"] == "output_lost"
    assert repository.transitions[0][1]["result"]["recovery_receipt"]["dispatch_fence"] is True


def test_cancel_without_dispatch_fence_keeps_unproven_cleanup_uncertain(monkeypatch: pytest.MonkeyPatch):
    job_id = "repo-change-" + "f" * 32
    job = {
        "job_id": job_id,
        "status": "running",
        "revision": 3,
        "owner": {"principal_id": "operator:one"},
        "declared_authority": {"session_id": "session-1"},
        "lease": {"owner": "service:repo-change", "fencing_token": 4},
        "checkpoints": [],
    }

    class FakeRepository:
        def __init__(self):
            self.transitions = []

        async def get_job(self, _job_id):
            return dict(job)

        async def record_checkpoint(self, _job_id, **_kwargs):
            job["revision"] += 1
            return dict(job)

        async def transition_job(self, _job_id, status, **kwargs):
            self.transitions.append((status, kwargs))
            job["status"] = status
            job["revision"] += 1
            return dict(job)

    class FakeSandbox:
        @staticmethod
        def _server_token(_job_id):
            return "server-token"

        def cancel(self, **_kwargs):
            assert job["status"] == "unknown_external_effect"
            return {"status": "unknown_external_effect", "reason": "cleanup_unproven"}

    repository = FakeRepository()
    monkeypatch.setattr("src.api.workflows.durable_job_repository", repository)
    monkeypatch.setattr("src.api.workflows.RootlessDockerRepoSandbox", FakeSandbox)
    monkeypatch.setattr(
        "src.api.workflows._require_authenticated_capability_operator",
        lambda _request: SimpleNamespace(
            principal=SimpleNamespace(principal_id="operator:one"),
            session_id="session-1",
        ),
    )
    result = asyncio.run(
        cancel_repo_change(
            job_id,
            RepoChangeCancelRequest(reason="stop-now"),
            object(),
        )
    )
    assert result["status"] == "unknown_external_effect"
    assert result["operator_action"] == "reconcile_or_cancel"
    assert repository.transitions[0][0] == "unknown_external_effect"
    assert repository.transitions[0][1]["result"]["cancellation_pending"] is True


def test_cancel_reserves_original_root_before_proven_cleanup(monkeypatch: pytest.MonkeyPatch):
    job_id = "repo-change-" + "a" * 32
    job = {
        "job_id": job_id,
        "status": "running",
        "revision": 3,
        "owner": {"principal_id": "operator:one"},
        "declared_authority": {"session_id": "session-1"},
        "lease": {"owner": "service:repo-change", "fencing_token": 9},
        "checkpoints": [],
    }

    class FakeRepository:
        def __init__(self):
            self.transitions = []

        async def get_job(self, _job_id):
            return dict(job)

        async def record_checkpoint(self, _job_id, **_kwargs):
            job["revision"] += 1
            return dict(job)

        async def transition_job(self, _job_id, status, **kwargs):
            self.transitions.append((status, kwargs))
            job["status"] = status
            job["lease"] = {} if status == "unknown_external_effect" else job.get("lease", {})
            job["revision"] += 1
            return dict(job)

    class FakeSandbox:
        @staticmethod
        def _server_token(_job_id):
            return "server-token"

        def cancel(self, **_kwargs):
            # The external cleanup sees the root only after its original
            # lease has been atomically reserved and cleared.
            assert job["status"] == "unknown_external_effect"
            assert job["lease"] == {}
            return {"status": "cancelled", "cleanup_proven": True}

    repository = FakeRepository()
    monkeypatch.setattr("src.api.workflows.durable_job_repository", repository)
    monkeypatch.setattr("src.api.workflows.RootlessDockerRepoSandbox", FakeSandbox)
    monkeypatch.setattr(
        "src.api.workflows._require_authenticated_capability_operator",
        lambda _request: SimpleNamespace(
            principal=SimpleNamespace(principal_id="operator:one"),
            session_id="session-1",
        ),
    )
    result = asyncio.run(
        cancel_repo_change(
            job_id,
            RepoChangeCancelRequest(reason="stop-now"),
            object(),
        )
    )
    assert result["status"] == "cancelled"
    assert [status for status, _kwargs in repository.transitions] == [
        "unknown_external_effect",
        "cancelled",
    ]


def test_cancel_blocked_dispatch_reserves_before_cleanup(monkeypatch: pytest.MonkeyPatch):
    job_id = "repo-change-" + "b" * 32
    token = RootlessDockerRepoSandbox._server_token(job_id)
    authority = {
        "session_id": "session-1",
        "base_digest": "b" * 64,
        "patch_sha256": "p" * 64,
        "image_digest": "image@sha256:" + "i" * 64,
        "limits_digest": "l" * 64,
    }
    dispatch = {
        "phase": "docker_dispatch_reserved",
        "job_id": job_id,
        "attempt": 1,
        "authority_digest": "a" * 64,
        "base_digest": authority["base_digest"],
        "patch_sha256": authority["patch_sha256"],
        "image_digest": authority["image_digest"],
        "limits_digest": authority["limits_digest"],
        "container_name": f"{token}-worker",
        "input_volume": f"{token}-input",
    }
    job = {
        "job_id": job_id,
        "status": "blocked",
        "revision": 3,
        "attempt_count": 1,
        "authority_digest": "a" * 64,
        "owner": {"principal_id": "operator:one"},
        "declared_authority": authority,
        "checkpoints": [{"payload": dispatch}],
    }

    class FakeRepository:
        def __init__(self):
            self.transitions = []

        async def get_job(self, _job_id):
            return dict(job)

        async def transition_job(self, _job_id, status, **kwargs):
            self.transitions.append((status, kwargs))
            job["status"] = status
            if status == "unknown_external_effect":
                job["lease"] = {}
            job["revision"] += 1
            return dict(job)

    class FakeSandbox:
        @staticmethod
        def _server_token(_job_id):
            return token

        def cancel(self, **_kwargs):
            assert job["status"] == "unknown_external_effect"
            assert not job.get("lease")
            return {"status": "cancelled", "cleanup_proven": True}

    repository = FakeRepository()
    monkeypatch.setattr("src.api.workflows.durable_job_repository", repository)
    monkeypatch.setattr("src.api.workflows.RootlessDockerRepoSandbox", FakeSandbox)
    monkeypatch.setattr(
        "src.api.workflows._require_authenticated_capability_operator",
        lambda _request: SimpleNamespace(
            principal=SimpleNamespace(principal_id="operator:one"),
            session_id="session-1",
        ),
    )
    result = asyncio.run(
        cancel_repo_change(
            job_id,
            RepoChangeCancelRequest(reason="stop-now"),
            object(),
        )
    )
    assert result["status"] == "cancelled"
    assert [status for status, _kwargs in repository.transitions] == [
        "unknown_external_effect",
        "cancelled",
    ]


def test_cancel_never_borrows_successor_lease(monkeypatch: pytest.MonkeyPatch):
    job_id = "repo-change-" + "c" * 32
    job = {
        "job_id": job_id,
        "status": "running",
        "revision": 3,
        "owner": {"principal_id": "operator:one"},
        "declared_authority": {"session_id": "session-1"},
        "lease": {"owner": "service:old", "fencing_token": 4},
        "checkpoints": [],
    }

    class FakeRepository:
        def __init__(self):
            self.transitions = []

        async def get_job(self, _job_id):
            return dict(job)

        async def record_checkpoint(self, _job_id, **_kwargs):
            # Simulate a lease transfer after the request's initial read.
            job["lease"] = {"owner": "service:new", "fencing_token": 5}
            job["revision"] += 1
            return dict(job)

        async def transition_job(self, _job_id, status, **kwargs):
            self.transitions.append((status, kwargs))
            raise AssertionError("the cancellation route must not transition a successor")

    class FakeSandbox:
        @staticmethod
        def _server_token(_job_id):
            return "server-token"

        def cancel(self, **_kwargs):
            raise AssertionError("the cancellation route must not clean up a successor")

    repository = FakeRepository()
    monkeypatch.setattr("src.api.workflows.durable_job_repository", repository)
    monkeypatch.setattr("src.api.workflows.RootlessDockerRepoSandbox", FakeSandbox)
    monkeypatch.setattr(
        "src.api.workflows._require_authenticated_capability_operator",
        lambda _request: SimpleNamespace(
            principal=SimpleNamespace(principal_id="operator:one"),
            session_id="session-1",
        ),
    )
    result = asyncio.run(
        cancel_repo_change(
            job_id,
            RepoChangeCancelRequest(reason="stop-now"),
            object(),
        )
    )
    assert result["status"] == "unknown_external_effect"
    assert result["reason_code"] == "repository_cancel_lease_changed"
    assert result["durable_status"] == "running"
    assert repository.transitions == []
