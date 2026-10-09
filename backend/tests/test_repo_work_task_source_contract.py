"""Metadata parsing grants no authority; legacy artifacts retain their shape."""
import json

import pytest

from src.work_board.contracts import (GeneralTaskEnvelope, TaskStrategyBinding,
    WorkBoardInputArtifactCreate, WorkBoardOwner)
from src.work_board.input_artifacts import _validate_request
from src.work_board.repository import BoardError
from tests.test_general_task_contract import descriptor, request
from tests.test_repo_work_source import inspected_repository
from tests.test_repo_work_source import git


def test_legacy_envelope_canonical_shape_does_not_gain_null_repository_field():
    original = request()
    fields = {"schema_version": 1, "task_input": original.input.model_dump(mode="json"),
        "plan": original.plan.model_dump(mode="json"), "proposal_error": None,
        "descriptors": [descriptor().model_dump(mode="json")],
        "strategy": {"schema_version": 1, "status": "none", "method_id": None,
            "version": None, "digest": None, "typed_data": None, "reason": None},
        "evidence": [], "proposal_group": None, "proposal_provenance": None}
    envelope = GeneralTaskEnvelope.model_validate(fields)
    assert envelope.model_dump(mode="json") == fields
    assert json.loads(envelope.model_dump_json()) == fields


def test_old_unscoped_task_cannot_mint_source_metadata_on_replay():
    from src.workflows.repo_repair_source import prepare_repository_task_publication
    original = request()
    envelope = GeneralTaskEnvelope(task_input=original.input, plan=original.plan,
        descriptors=[descriptor()], strategy=TaskStrategyBinding(status="none"))
    with pytest.raises(BoardError) as denied:
        prepare_repository_task_publication(None, envelope,
            owner=WorkBoardOwner(principal_id="owner", session_id="session"),
            goal_revision=1, replay=True)
    assert denied.value.code == "repository_source_unscoped_replay"


@pytest.mark.asyncio
async def test_repository_extra_publication_scope_denies_before_io():
    from src.work_board.general_task import GeneralTaskService
    from tests.test_general_task_contract import Registry
    original = request(steps=[{"step_id": "repair", "tool_id": "repository_work",
        "input": {}, "output_contract": descriptor().output_schema}])
    service = GeneralTaskService(Registry())
    calls = []
    def forbidden_scope():
        calls.append("entered")
        raise AssertionError("additional scope must never enter")
    with pytest.raises(BoardError) as denied:
        await service.create(None, WorkBoardOwner(principal_id="owner", session_id="session"),
            original, publication_authority_scope=forbidden_scope)
    assert denied.value.code == "repository_publication_scope_incompatible"
    assert calls == []


@pytest.mark.asyncio
async def test_public_input_artifact_cannot_publish_caller_repository_field():
    request_data = WorkBoardInputArtifactCreate(schema_version=1, capability_id="agent.task.v1",
        goal_id="goal-1", goal_revision=1, idempotency_key="forged-source",
        input={"repository_source": {"binding_digest": "a" * 64}})
    # The public ingress must reject before any database/filesystem access.
    with pytest.raises(BoardError) as denied:
        await _validate_request(None, WorkBoardOwner(principal_id="owner", session_id="session"), request_data)
    assert denied.value.code == "repository_source_publication_required"


def test_actual_source_staging_retains_private_manifest_and_preserves_live_tree(inspected_repository):
    repository, service, work = inspected_repository
    owner = WorkBoardOwner(principal_id="owner", session_id="session")
    before = (repository / "calculator.py").read_bytes()
    binding = service.stage_task_source(work, owner=owner, goal_id="goal-1", goal_revision=1)
    payload = service._read_private_artifact(binding.source_artifact_ref,
        expected_digest=binding.source_artifact_digest)
    facts = json.loads(payload)
    assert facts["original_input"] == work.model_dump(mode="json")
    assert facts["snapshot_manifest"]["digest"] == binding.snapshot_digest
    assert facts["compiled_input"]["test_args"] == ["pytest", "-q", "tests/test_calculator.py"]
    assert "def add" not in payload.decode()
    assert (repository / "calculator.py").read_bytes() == before
    assert binding.original_root_id == owner.session_id
    assert binding.goal_revision == 1


def test_actual_git_executable_source_snapshot_preserves_only_execute_class(inspected_repository, tmp_path):
    import stat
    repository, service, work = inspected_repository
    source = repository / "calculator.py"
    source.chmod(0o755)
    git(repository, "add", "calculator.py")
    git(repository, "commit", "-m", "tracked executable")
    work = work.model_copy(update={"base_commit": git(repository, "rev-parse", "HEAD").decode().strip()})
    owner = WorkBoardOwner(principal_id="owner", session_id="session")
    binding = service.stage_task_source(work, owner=owner, goal_id="goal-1", goal_revision=1)
    facts = json.loads(service._read_private_artifact(binding.source_artifact_ref,
        expected_digest=binding.source_artifact_digest))
    assert next(item["mode"] for item in facts["git_manifest"]
        if item["path"] == "calculator.py") == "100755"
    snapshot = service.sandbox.snapshot_repository(repository, tmp_path / "private-copy",
        preserve_source_modes=True)
    assert stat.S_IMODE((tmp_path / "private-copy/calculator.py").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "private-copy/tests/test_calculator.py").stat().st_mode) == 0o600
    assert stat.S_IMODE(source.stat().st_mode) == 0o755
    source.chmod(0o644)
    from src.workflows.repo_repair import RepoRepairError
    with pytest.raises(RepoRepairError):
        service.stage_task_source(work, owner=owner, goal_id="goal-1", goal_revision=1)
