"""Actual scoped Source repair and separately approved publication; offline HTTP."""
import uuid
import os
from pathlib import Path
import subprocess
import sys
import socket
import json
from datetime import datetime, timezone

from cryptography.fernet import Fernet
import httpx
import pytest

from config.settings import settings
from src.approval.repository import approval_repository, approval_decision_digest
from src.extensions.github_consent import GitHubConsentRequest, PUBLICATION_ACTIONS
from src.extensions.github_followthrough import GitHubFollowthroughService
from src.vault.repository import vault_repository
from src.workflows.repo_publication import PrepareRequest, ReconcileRequest, RepoPublicationService
from tests.repo_publication_support import GitDataTransport
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.test_repo_work_task_publication import _actual_source_callback_journey


async def selected_publisher(flow, monkeypatch):
    owner, source = flow["owner"], flow["service"].repository_source_service
    monkeypatch.setattr("src.workflows.repo_publication.jobs", flow["jobs"])
    monkeypatch.setattr(settings, "vault_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr("src.vault.crypto._fernet", None)
    await vault_repository.store("source-publication-fixture", "fixture-token", owner_principal_id=owner.principal_id)
    transport = GitDataTransport(flow["repository"], base_branch="develop")
    async def resolver(_host, _port):
        return ["93.184.216.34"]
    adapter = GitHubFollowthroughService(resolver=resolver, transport=httpx.MockTransport(transport.handler))
    connection = await adapter.put_connection(owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id, repository="acme/example", vault_key="source-publication-fixture",
        mode="active", expected_revision=0,
        consent=GitHubConsentRequest(acknowledged=True, duration_seconds=900, actions=list(PUBLICATION_ACTIONS)))
    return RepoPublicationService(adapter=adapter, repository_source_service=source), adapter, connection, transport


def test_publication_supervisor_non_socket_control_fd_rejects_without_command(tmp_path):
    stage = tmp_path / "supervisor"
    stage.mkdir(mode=0o700)
    read_fd, write_fd = os.pipe()
    try:
        helper = Path(__file__).resolve().parents[1] / "src/execution/repo_publication_supervisor.py"
        result = subprocess.run([sys.executable, "-I", str(helper), str(stage / "absent-admission.json"),
            str(read_fd), str(write_fd)], pass_fds=(read_fd, write_fd), capture_output=True,
            timeout=5, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        assert result.returncode == 2
        assert b"Socket operation on non-socket" in result.stderr
        assert list(stage.iterdir()) == []
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_publication_supervisor_wrong_unix_socket_type_rejects_without_command(tmp_path):
    stage = tmp_path / "supervisor"
    stage.mkdir(mode=0o700)
    control, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        helper = Path(__file__).resolve().parents[1] / "src/execution/repo_publication_supervisor.py"
        result = subprocess.run([sys.executable, "-I", str(helper), str(stage / "absent-admission.json"),
            str(control.fileno()), str(peer.fileno())], pass_fds=(control.fileno(), peer.fileno()),
            capture_output=True, timeout=5, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        assert result.returncode == 2
        assert b"publication_supervisor_control_socket_invalid" in result.stderr
        assert list(stage.iterdir()) == []
    finally:
        control.close()
        peer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("three_iterations", [False, True])
async def test_actual_source_final_patch_separate_publication_approval_and_readback(accounting_db, monkeypatch, unknown, three_iterations):
    flow = await _actual_source_callback_journey(accounting_db, monkeypatch,
        three_iterations, "test_python", publication_profile=True)
    owner, jobs, source = flow["owner"], flow["jobs"], flow["service"].repository_source_service
    publisher, adapter, connection, transport = await selected_publisher(flow, monkeypatch)
    root = await jobs.get_job(flow["root_id"])
    proposal = flow["proposal"]
    request = PrepareRequest(repair_job_id=root["job_id"], expected_repair_revision=root["revision"],
        proposal_id=proposal["proposal_id"], expected_proposal_revision=proposal["revision"],
        expected_connection_revision=connection["revision"], base_branch="develop",
        expected_base_commit=transport.base, branch_name="feat/source-publication",
        commit_message="Apply separately approved cumulative repair", title="Apply cumulative repair",
        body="Actual Source repair with separate publication approval", idempotency_key=str(uuid.uuid4()))
    # Mutate the real final proposal after physical staging but before the
    # existing admission transaction. The same request must remain admissible
    # after restoration because the rejected writer created no publication.
    from src.db.models import (RepoRepairProposal, WorkflowRunState, RepoRepairSourcePacket,
        GitHubFollowthroughConnection, OperatorSession, Goal, ApprovalRequest)
    from src.workflows.job_runtime import DurableJobLeaseError, DurableJobError
    from sqlalchemy import select
    original_admit = jobs.admit_job
    drift_cases = ["proposal"]
    if three_iterations and not unknown:
        drift_cases += ["source", "connection", "consent", "owner", "goal"]
    async def mutate(kind, *, restore=None):
        async with flow["factory"].accounting_sessions() as db:
            if kind == "preview":
                row, field = await jobs._fetch(db, prepared["job_id"]), "declared_authority_json"
            elif kind == "approval":
                row, field = await db.get(ApprovalRequest, approval.id), "details_json"
            else:
                model, key, field = {
                    "proposal": (RepoRepairProposal, proposal["proposal_id"], "revision"),
                    "source": (RepoRepairSourcePacket, proposal["source_packet_id"], "source_manifest_digest"),
                    "connection": (GitHubFollowthroughConnection, connection["id"], "revision"),
                    "consent": (GitHubFollowthroughConnection, connection["id"], "consent_payload_digest"),
                    "owner": (OperatorSession, owner.session_id, "revoked_at"),
                    "goal": (Goal, proposal["goal_id"], "revision"),
                }[kind]
                row = await db.get(model, key)
            before = getattr(row, field)
            if restore is not None:
                replacement = restore[0]
            elif kind in {"preview", "approval"}:
                changed = json.loads(before)
                changed["preview_digest"] = "a" * 64
                replacement = json.dumps(changed)
            else:
                replacement = before + 1 if field == "revision" else datetime.now(timezone.utc) if kind == "owner" else "a" * 64
            setattr(row, field, replacement)
        return (before,)
    for kind in drift_cases:
        saved = []
        async def drift_before_writer(spec, **kwargs):
            saved.append(await mutate(kind))
            return await original_admit(spec, **kwargs)
        with monkeypatch.context() as boundary:
            boundary.setattr(jobs, "admit_job", drift_before_writer)
            with pytest.raises(DurableJobError):
                await publisher.prepare(request, owner.principal_id, owner.session_id)
        await mutate(kind, restore=saved[0])
        async with flow["factory"].accounting_sessions() as db:
            assert not list((await db.execute(select(WorkflowRunState).where(
                WorkflowRunState.job_kind == "engineering.repo-publication.v1"))).scalars())
    def forbidden_physical_read(*args, **kwargs):
        raise AssertionError("SQL publication authority must use staged facts only")
    async def sql_only_admit(spec, **kwargs):
        with monkeypatch.context() as boundary:
            boundary.setattr(source, "_read_private_artifact", forbidden_physical_read)
            boundary.setattr("src.workflows.repo_publication.read_file", forbidden_physical_read)
            boundary.setattr("src.execution.repo_publication_runtime.capture", forbidden_physical_read)
            boundary.setattr(vault_repository, "snapshot", forbidden_physical_read)
            return await original_admit(spec, **kwargs)
    with monkeypatch.context() as boundary:
        boundary.setattr(jobs, "admit_job", sql_only_admit)
        prepared = await publisher.prepare(request, owner.principal_id, owner.session_id)
    assert prepared["status"] == "awaiting_approval" and transport.calls == []
    assert prepared["approval_id"] != proposal["approval_id"]
    assert prepared["preview"]["source_projection"]["root_authority_digest"] == root["authority_digest"]
    assert prepared["preview"]["source_projection"]["iteration_authority_digest"] == proposal["authority_digest"]
    publication_binding = prepared["preview"]["repair_binding"]
    assert publication_binding["approved_input_patch_digest"] == proposal["patch_sha256"]
    assert publication_binding["patch_sha256"] != publication_binding["approved_input_patch_digest"]
    assert prepared["preview"]["tested_input"]["patch_sha256"] == publication_binding["approved_input_patch_digest"]
    assert publication_binding["patch_artifact_id"] == f"artifacts/repo-repair/{root['job_id']}/diff.patch"
    replay = await publisher.prepare(request, owner.principal_id, owner.session_id)
    assert replay["job_id"] == prepared["job_id"] and replay["preview_digest"] == prepared["preview_digest"]
    held = await publisher.execute(prepared["job_id"], owner.principal_id, owner.session_id)
    assert held["status"] == "awaiting_approval" and transport.calls == []
    approval = await approval_repository.get(prepared["approval_id"])
    await approval_repository.resolve_exact(approval.id, "approved",
        expected_digest=approval_decision_digest(approval), owner_principal_id=owner.principal_id,
        operator_session_id=owner.session_id)
    original_resume = jobs.resume_approved_job
    from dataclasses import replace
    for mode in ("missing", "copied"):
        async def invalid_witness(*args, **kwargs):
            witness = kwargs.pop("_repository_publication_witness")
            if mode == "copied":
                kwargs["_repository_publication_witness"] = replace(witness)
            return await original_resume(*args, **kwargs)
        with monkeypatch.context() as boundary:
            boundary.setattr(jobs, "resume_approved_job", invalid_witness)
            with pytest.raises(DurableJobLeaseError, match="actual registered repository publication Source witness required"):
                await publisher.execute(prepared["job_id"], owner.principal_id, owner.session_id)
        assert (await approval_repository.get(approval.id)).status == "approved"
        assert (await jobs.get_job(prepared["job_id"]))["status"] == "awaiting_approval"
        assert transport.calls == []
    for kind in drift_cases + (["preview", "approval"] if three_iterations and not unknown else []):
        saved = []
        async def drift_before_consumption(*args, **kwargs):
            saved.append(await mutate(kind))
            return await original_resume(*args, **kwargs)
        with monkeypatch.context() as boundary:
            boundary.setattr(jobs, "resume_approved_job", drift_before_consumption)
            with pytest.raises(DurableJobError):
                await publisher.execute(prepared["job_id"], owner.principal_id, owner.session_id)
        await mutate(kind, restore=saved[0])
        assert (await approval_repository.get(approval.id)).status == "approved"
        assert (await jobs.get_job(prepared["job_id"]))["status"] == "awaiting_approval"
        assert transport.calls == []
    transport.fail_after_pr = unknown
    async def sql_only_resume(*args, **kwargs):
        from src.workflows.job_runtime import DurableJobTransitionError
        with pytest.raises(DurableJobTransitionError, match="repository publication witness does not match this capability"):
            await jobs.transition_job(root["job_id"], "queued",
                _repository_publication_witness=kwargs["_repository_publication_witness"])
        assert (await approval_repository.get(approval.id)).status == "approved"
        with monkeypatch.context() as boundary:
            boundary.setattr(source, "_read_private_artifact", forbidden_physical_read)
            boundary.setattr("src.workflows.repo_publication.read_file", forbidden_physical_read)
            boundary.setattr("src.execution.repo_publication_runtime.capture", forbidden_physical_read)
            boundary.setattr(vault_repository, "snapshot", forbidden_physical_read)
            return await original_resume(*args, **kwargs)
    with monkeypatch.context() as boundary:
        boundary.setattr(jobs, "resume_approved_job", sql_only_resume)
        result = await publisher.execute(prepared["job_id"], owner.principal_id, owner.session_id)
    assert result["status"] == ("unknown_external_effect" if unknown else "succeeded"), result
    assert len(transport.pulls) == 1
    assert (await jobs.get_job(root["job_id"]))["revision"] == root["revision"]
    calls = len(transport.calls)
    replay = await publisher.execute(prepared["job_id"], owner.principal_id, owner.session_id)
    assert replay["status"] == result["status"] and len(transport.calls) == calls
    if unknown:
        current_connection = await adapter._get_connection_row(owner.principal_id)
        result = await publisher.reconcile(prepared["job_id"], owner.principal_id, owner.session_id,
            ReconcileRequest(pr_number=1, acknowledged_readback=True,
                expected_connection_revision=current_connection.revision))
        assert result["status"] == "succeeded"
        assert all(method == "GET" for method, _path, _body in transport.calls[calls:])


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["test_python", "test_node"])
async def test_ordinary_source_profiles_keep_actual_local_patch_when_publication_blocked(accounting_db, monkeypatch, language):
    from src.workflows.repo_repair_source import stage_repository_publication_witness
    from src.workflows.job_runtime import DurableJobLeaseError
    flow = await _actual_source_callback_journey(accounting_db, monkeypatch, True, language)
    publisher, _adapter, _connection, transport = await selected_publisher(flow, monkeypatch)
    root = await flow["jobs"].get_job(flow["root_id"])
    patch_path = flow["workspace"] / f"artifacts/repo-repair/{root['job_id']}/diff.patch"
    patch = patch_path.read_bytes()
    assert patch and root["status"] == "succeeded"
    with pytest.raises(DurableJobLeaseError, match="actual publication-tested runtime/input required"):
        await stage_repository_publication_witness(publisher.source_owner(), flow["jobs"],
            repair_job_id=root["job_id"], owner=flow["owner"])
    assert patch_path.read_bytes() == patch
    assert await flow["jobs"].get_job(root["job_id"]) == root
    assert transport.calls == []
