"""Serialized/caller-selected data cannot acquire iteration authority."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from src.execution.repo_sandbox import RepoSandboxError
from src.workflows.inference_accounting import InferenceAccountingError
from src.workflows.repo_repair_source import (
    RepoIterationProcessBinding, assert_repo_iteration_process_binding,
    assert_repository_iteration_witness, iteration_identity,
)
from src.workflows.repo_repair import RepoRepairService, RepoRepairError, RepoWorkInput
from src.db.models import WorkBoardInputArtifact
from tests.test_repo_work_contracts import selection


def test_original_attempt_input_and_index_each_select_a_distinct_identity():
    identities = {iteration_identity("root", "attempt", "a" * 64, index)
                  for index in range(1, 4)}
    assert len(identities) == 3
    assert iteration_identity("root", "attempt", "a" * 64, 1) in identities
    assert iteration_identity("root", "other-attempt", "a" * 64, 1) not in identities
    assert iteration_identity("root", "attempt", "b" * 64, 1) not in identities


@pytest.mark.parametrize("index", [0, 4, True, 1.0, "1"])
def test_iteration_identity_cannot_expand_or_coerce_original_sequence(index):
    with pytest.raises(ValueError):
        iteration_identity("root", "attempt", "a" * 64, index)


def test_constructing_process_dto_and_replaying_projection_grants_no_authority():
    cutoff = datetime.now(timezone.utc) + timedelta(seconds=10)
    binding = RepoIterationProcessBinding("root", "attempt", 1, 1, "a" * 64,
        cutoff, "b" * 64, "c" * 64)
    job = SimpleNamespace(job_id="root", attempt_id="attempt", fencing_token=1,
        authority_digest="b" * 64, base_digest="c" * 64,
        execution_deadline_at=cutoff.isoformat())
    for candidate in (binding, binding.projection(), SimpleNamespace(**binding.projection())):
        with pytest.raises(RepoSandboxError, match="source-issued"):
            assert_repo_iteration_process_binding(candidate, job)


@pytest.mark.parametrize("candidate", [None, {}, {"role": "repository_iteration"},
    SimpleNamespace(operation_id="remote:repo-work:" + "a" * 64)])
def test_accounting_context_rejects_unsealed_caller_data(candidate):
    with pytest.raises(InferenceAccountingError, match="source_witness_required"):
        assert_repository_iteration_witness(candidate)


def test_actual_original_input_artifact_preserves_limits_and_blocks_changed_bytes(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    service = RepoRepairService(workspace_dir=str(workspace))
    work = RepoWorkInput.model_validate(selection())
    reference, digest = service._write_private_artifact(
        "artifacts/repo-repair/source/original-work.json",
        json.dumps({"schema_version": 1, "capability_id": "engineering.repo-repair.v1",
         "input": work.model_dump(mode="json")}, sort_keys=True).encode())
    row = WorkBoardInputArtifact(artifact_id="input-original", owner_principal_id="owner",
        owner_session_id="session", goal_id="goal", goal_revision=1,
        capability_id="engineering.repo-repair.v1", capability_version="1",
        idempotency_key="original", payload_sha256=digest, typed_input_ref=reference,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1))
    assert service._read_bound_work_input(row) == work
    assert service._read_bound_work_input(row).limits == work.limits
    path = workspace / reference.removeprefix("workspace-json:")
    path.write_text("{}")
    with pytest.raises(RepoRepairError):
        service._read_bound_work_input(row)
