"""Internal same-boot candidate; public actions remain acceptance-gated 503."""
from pathlib import Path

import pytest
from pydantic import ValidationError

from config.settings import settings
from src.workflows import repo_repair_source as source
from src.workflows import repo_repair_source_recovery as recovery
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.test_repo_source_recovered_finalizer import _signed_ownerless_fixture, _rows


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["test_python", "test_node"])
@pytest.mark.parametrize("failed", [False, True])
async def test_genuine_sameboot_candidate_selects_original_and_preserves_public_gate(
        accounting_db, monkeypatch, repository_admission_signer, language, failed):
    if language == "test_node":
        # Keep the original publication fixture's server-selected runtime alive
        # after its inner patch exits and the cold recovery service is created.
        node = Path("/home/pawel/repos/seraph/.agent-worktrees/986-a1-host/.agent-evidence/986/a1-upstream/node-v24.13.1-linux-x64/bin/node")
        assert node.is_file(), "Required cached fixed Node24 runtime must exist; no ambient fallback or download"
        monkeypatch.setattr(settings, "repo_sandbox", settings.repo_sandbox.model_copy(
            update={"node_runtime_path": str(node)}))
    flow = await _signed_ownerless_fixture(accounting_db, monkeypatch, language, failed=failed)
    jobs, service, owner = flow["jobs"], flow["service"], flow["kwargs"]["owner"]
    if language == "test_node":
        assert service.sandbox.config.node_runtime_path == str(node)
    job_id = flow["kwargs"]["job_id"]
    async with jobs._session() as db:
        root = await jobs._fetch(db, job_id)
        revision = root.revision
    before = await _rows(jobs)
    for invalid in (True, -1, "0", revision + 1):
        with pytest.raises(recovery.RepositorySourceRecoveryError):
            await recovery._reconcile_original_repository_cleanup(service, jobs,
                job_id=job_id, owner=owner, expected_job_revision=invalid)
        assert await _rows(jobs) == before
    for bad_owner in (owner.model_copy(update={"principal_id": "foreign"}),
                      owner.model_copy(update={"session_id": "foreign"})):
        with pytest.raises(recovery.RepositorySourceRecoveryError, match="owner_changed"):
            await recovery._reconcile_original_repository_cleanup(service, jobs,
                job_id=job_id, owner=bad_owner, expected_job_revision=revision)
        assert await _rows(jobs) == before
    # Current authentic public actions remain unavailable, without invoking the
    # internal candidate or trusting test receipts as an enablement switch.
    for action in ("reconcile_original_cleanup", "settle_original_host_boot_cleanup"):
        with pytest.raises(recovery.RepositorySourceRecoveryError) as denied:
            await recovery.recover_original_repository_cleanup(service, jobs,
                job_id=job_id, owner=owner, expected_job_revision=revision, action=action)
        assert denied.value.status_code == 503
        assert await _rows(jobs) == before
    result = await recovery._reconcile_original_repository_cleanup(service, jobs,
        job_id=job_id, owner=owner, expected_job_revision=revision)
    packet = recovery.RepositorySourceRecoveryProjection.model_validate(result["source_recovery"])
    assert result["job_id"] == job_id and result["no_learning"] is True
    assert packet.public_actions == "unavailable"
    assert packet.original_result == ("failed" if failed else "succeeded")
    assert packet.state == ("continuation_ready" if failed else "original_cleanup_committed")
    assert packet.physical_hold is failed
    async with jobs._session() as db:
        root = await jobs._fetch(db, job_id)
        assert root.status == ("running" if failed else "succeeded")
        assert result["revision"] == root.revision
        identity = flow["registration"]["iteration_id"]
        assert source._repository_record(root, "repository:cleanup:" + identity)["cleanup_proven"] is True
        if failed:
            assert result["repository_review"]["iteration_index"] == 2
            assert result["repository_review"]["contact_state"] == "not_started"
            next_id = result["repository_review"]["iteration_id"]
            assert source._repository_record(root, "repository:execution:" + next_id) is None
            assert source._repository_record(root, "repository:callback-start:" + next_id) is None
        else:
            assert source._repository_record(root, "repository:terminal:v1")["no_learning"] is True


@pytest.mark.asyncio
async def test_genuine_unknown_candidate_remains_nonmutating_held(
        accounting_db, monkeypatch, repository_admission_signer):
    flow = await _signed_ownerless_fixture(accounting_db, monkeypatch, "test_python", unknown=True)
    jobs, owner = flow["jobs"], flow["kwargs"]["owner"]
    job_id = flow["kwargs"]["job_id"]
    async with jobs._session() as db:
        revision = (await jobs._fetch(db, job_id)).revision
    before = await _rows(jobs)
    result = await recovery._reconcile_original_repository_cleanup(flow["service"], jobs,
        job_id=job_id, owner=owner, expected_job_revision=revision)
    assert await _rows(jobs) == before
    assert result["status"] == "unknown_external_effect"
    assert result["source_recovery"] == {
        "state": "held_unknown", "reason": "original_stop_pending", "physical_hold": True,
        "original_result": None, "public_actions": "unavailable"}


@pytest.mark.parametrize("change", [
    {"physical_hold": "held"}, {"physical_hold": 1}, {"state": "recovered"},
    {"original_result": "unknown"}, {"public_actions": "enabled"},
    {"reason": "raw/private/path"}, {"reason": "x" * 129}, {"witness": {}},
])
def test_recovery_metadata_rejects_noncanonical_or_unbounded_values(change):
    packet = {"state": "held_unknown", "reason": "original_completion_unproven",
        "physical_hold": None, "original_result": None, "public_actions": "unavailable"}
    with pytest.raises(ValidationError):
        recovery.RepositorySourceRecoveryProjection.model_validate({**packet, **change})
