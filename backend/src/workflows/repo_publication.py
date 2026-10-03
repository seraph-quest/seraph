"""Exact tested repair → fresh approval → local commit → protected ready PR.

Uses canonical jobs/artifacts/approvals and the existing GitHub connection.
The succeeded repair is an immutable dependency, never a running parent.
"""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import uuid
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, case

from config.settings import settings
from src.approval.repository import approval_repository, approval_state_revision, fingerprint_tool_call, _approval_timestamp, _approval_expiry
from src.db import engine as db_engine
from src.db.models import Goal, RepoRepairProposal, RepoRepairSourcePacket, WorkBoardTask, WorkBoardAttempt, WorkflowRunState
from src.execution.repo_publication import PublicationError, SourceGit, branch, digest, equivalent, file_manifest, object_id, oid, posture, produce
from src.execution.repo_worker import _open_source_regular_file, _open_directory_descriptor, _descriptor_flags
from src.extensions.github_followthrough import GitHubFollowthroughService, _require_live_owner_session, _operator, _principal_id, _session_id
from src.extensions.github_consent import require_consent, require_readback, GitHubReadbackAuthority, PUBLICATION_ACTIONS, live_operator
from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec, durable_job_repository as jobs
from src.workspace import canonical_workspace_root
from src.extensions.github_capacity_closure import PublicationCloseRequest

CAPABILITY = "engineering.repo-publication.v1"
PERMISSIONS = ["local_host_execution", "github_git_objects_write", "github_new_branch_write", "github_ready_pr_write"]
router = APIRouter(prefix="/capabilities/github/repo-publication", tags=["repo-publication"])
logger = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc)


class PrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    repair_job_id: str = Field(min_length=1, max_length=160)
    expected_repair_revision: int = Field(gt=0)
    proposal_id: str = Field(min_length=1, max_length=160)
    expected_proposal_revision: int = Field(gt=0)
    expected_connection_revision: int = Field(gt=0)
    base_branch: str = Field(min_length=1, max_length=120)
    expected_base_commit: str = Field(min_length=40, max_length=40)
    branch_name: str = Field(min_length=6, max_length=120)
    commit_message: str = Field(min_length=1, max_length=2000)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=18000)
    idempotency_key: str = Field(min_length=36, max_length=36)

    @field_validator("title", "commit_message", "body")
    @classmethod
    def text(cls, value):
        if "\0" in value or "seraph-operation:" in value or not value.strip():
            raise ValueError("publication text invalid")
        return value


class ReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pr_number: int | None = Field(default=None, gt=0)
    acknowledged_readback: bool
    expected_connection_revision: int = Field(gt=0)

    @field_validator("acknowledged_readback")
    @classmethod
    def explicit_read(cls, value):
        if value is not True:
            raise ValueError("explicit original-root readback acknowledgment required")
        return value


def read_file(relative: str, *, maximum=16 * 1024 * 1024) -> bytes:
    root = Path(canonical_workspace_root(settings.workspace_dir))
    try:
        fd, before = _open_source_regular_file(root, relative)
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
        if len(raw) > maximum or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise PublicationError("artifact_changed")
        return raw
    except (OSError, ValueError) as exc:
        raise PublicationError("artifact_unavailable") from exc


def write_file(relative: str, raw: bytes) -> None:
    root = Path(canonical_workspace_root(settings.workspace_dir))
    parent = _open_directory_descriptor(root)
    try:
        for part in Path(relative).parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
            next_fd = os.open(part, _descriptor_flags(directory=True), dir_fd=parent)
            os.close(parent); parent = next_fd
        try:
            descriptor = os.open(Path(relative).name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent)
        except FileExistsError:
            if read_file(relative) != raw:
                raise PublicationError("artifact_identity_conflict")
            return
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        os.fsync(parent)
    finally:
        os.close(parent)


class RepoPublicationService:
    def __init__(self, *, adapter=None):
        self.adapter = adapter or GitHubFollowthroughService()

    async def live(self, principal, session):
        await live_operator(principal, session)

    async def owned(self, job_id, principal, session):
        await self.live(principal, session)
        current = await jobs.get_job(job_id)
        if not current or current.get("job_kind") != CAPABILITY or current.get("owner", {}).get("principal_id") != principal or current.get("operator_session_id") != session:
            raise PublicationError("publication_not_found", status_code=404)
        return current

    async def discover(self, repair_job_id, principal, session, *, offset=0):
        """Bounded original-root GET discovery; no consent or execution follows."""
        await self.live(principal, session)
        repair = await jobs.get_job(repair_job_id)
        if not repair or repair.get("job_kind") != "engineering.repo-repair.v1" or repair.get("owner", {}).get("principal_id") != principal or repair.get("operator_session_id") != session:
            raise PublicationError("repair_not_found", status_code=404)
        unresolved = {"unknown_external_effect", "blocked", "failed", "awaiting_approval", "queued", "running"}
        # Bound the canonical page BEFORE reading private previews. The exact
        # dependency and original root keep other repairs/owners out of it.
        async with db_engine.get_session() as db:
            rows = (await db.execute(select(WorkflowRunState).where(
                WorkflowRunState.job_kind == CAPABILITY,
                WorkflowRunState.owner_kind == "user",
                WorkflowRunState.owner_principal_id == principal,
                WorkflowRunState.operator_session_id == session,
                WorkflowRunState.session_id == session,
                WorkflowRunState.dependencies_json == json.dumps([repair_job_id], separators=(",", ":")),
            ).order_by(case((WorkflowRunState.status.in_(unresolved), 0), else_=1),
                WorkflowRunState.run_identity).offset(offset).limit(21))).scalars().all()
            identities = [row.run_identity for row in rows]
        result = []
        rejected = 0
        for identity in identities[:20]:
            try:
                current = await self.owned(identity, principal, session)
                if current.get("dependencies") != [repair_job_id]:
                    raise PublicationError("publication_dependency_changed")
                preview = self.preview(current)
                binding = (current.get("declared_authority") or {}).get("repair_binding")
                preview_binding = preview.get("repair_binding")
                # Canonical authority deliberately redacts the nested private
                # test-artifact projection. The private preview digest already
                # binds those bytes; compare its immutable scalar binding here.
                if not isinstance(binding, dict) or not isinstance(preview_binding, dict) or binding.get("repair_job_id") != repair_job_id or any(preview_binding.get(key) != value for key, value in binding.items() if key != "test_artifacts") or preview.get("owner_principal_id") != principal or preview.get("owner_session_id") != session:
                    raise PublicationError("publication_discovery_binding_changed")
                result.append(await self.view(current))
            except (PublicationError, KeyError, TypeError, ValueError):
                rejected += 1
        await self.live(principal, session)
        return {"repair_job_id": repair_job_id, "owner_principal_id": principal,
            "owner_session_id": session, "jobs": result, "limit": 20,
            "next_offset": offset + 20 if len(identities) > 20 and offset < 2000 else None,
            "scan_limit_reached": len(identities) > 20 and offset >= 2000,
            "rejected_count": rejected}

    async def repair(self, request, principal, session):
        await self.live(principal, session)
        root = await jobs.get_job(request.repair_job_id)
        if not root or root.get("job_kind") != "engineering.repo-repair.v1" or root.get("owner", {}).get("principal_id") != principal or root.get("operator_session_id") != session:
            raise PublicationError("repair_not_found", status_code=404)
        if root.get("status") != "succeeded" or root.get("revision") != request.expected_repair_revision:
            raise PublicationError("repair_success_revision_stale")
        async with db_engine.get_session() as db:
            proposal = await db.get(RepoRepairProposal, request.proposal_id)
            if not proposal or proposal.owner_principal_id != principal or proposal.owner_session_id != session or proposal.workflow_run_id != request.repair_job_id:
                raise PublicationError("proposal_not_found", status_code=404)
            task = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == proposal.work_board_task_id))).scalar_one_or_none()
            attempt = await db.get(WorkBoardAttempt, proposal.work_board_attempt_id)
            packet = await db.get(RepoRepairSourcePacket, proposal.source_packet_id)
            goal = await db.get(Goal, proposal.goal_id)
            if not all((task, attempt, packet, goal)) or proposal.revision != request.expected_proposal_revision or goal.revision != proposal.goal_revision or goal.owner_principal_id != principal or goal.owner_session_id != session:
                raise PublicationError("repair_binding_stale")
            if any(row.owner_principal_id != principal or row.owner_session_id != session for row in (task, packet)) or task.goal_id != goal.id or attempt.task_id != task.task_id or attempt.workflow_run_id != root["job_id"] or packet.workflow_run_id != root["job_id"] or task.goal_revision != goal.revision:
                raise PublicationError("repair_owner_or_goal_stale")
            binding = {"repair_job_id": root["job_id"], "repair_revision": root["revision"], "repair_authority_digest": root["authority_digest"], "task_id": task.task_id, "task_revision": task.task_revision, "attempt_id": attempt.attempt_id, "proposal_id": proposal.proposal_id, "proposal_revision": proposal.revision, "proposal_authority_digest": proposal.authority_digest, "source_packet_id": packet.id, "source_packet_revision": packet.revision, "source_manifest_digest": packet.source_manifest_digest, "base_snapshot_digest": packet.base_snapshot_digest, "goal_id": goal.id, "goal_revision": goal.revision, "repository_ref": proposal.repository_ref, "patch_artifact_id": proposal.patch_artifact_id, "patch_sha256": proposal.patch_sha256, "repair_executor": (root.get("declared_authority") or {}).get("executor_kind"), "repair_executor_posture_digest": (root.get("declared_authority") or {}).get("executor_posture_digest")}
            approval_id, approval_fingerprint = proposal.approval_id, proposal.approval_fingerprint
            binding["repair_finished_at"] = root.get("finished_at")
        historical = await approval_repository.get(approval_id or "")
        if not historical or historical.owner_principal_id != principal or historical.operator_session_id != session or historical.fingerprint != approval_fingerprint or historical.status not in {"consumed", "approved"}:
            raise PublicationError("repair_execution_approval_unproven")
        artifacts = {Path(item.get("file_path", "")).name: item for item in root.get("artifacts", []) if str(item.get("file_path", "")).startswith(f"artifacts/repo-repair/{root['job_id']}/")}
        required = {"manifest.json", "readback.json", "diff.patch"}
        if not required.issubset(artifacts):
            raise PublicationError("tested_artifacts_unavailable")
        contents = {name: read_file(artifacts[name]["file_path"]) for name in required}
        for name, raw in contents.items():
            if hashlib.sha256(raw).hexdigest() != artifacts[name].get("content_sha256"):
                raise PublicationError("tested_artifact_digest_changed")
        manifest = json.loads(contents["manifest.json"])
        readback = json.loads(contents["readback.json"])
        if manifest != readback or manifest.get("status") != "succeeded" or manifest.get("exit_code") != 0 or manifest.get("cleanup_proven") is not True:
            raise PublicationError("tested_execution_unproven")
        if manifest.get("job_id") != root["job_id"] or manifest.get("authority_digest") != root["authority_digest"] or manifest.get("posture_digest") != binding["repair_executor_posture_digest"]:
            raise PublicationError("tested_execution_binding_unproven")
        if not any(item.get("receipt_kind") == "readback" and item.get("status") == "succeeded" and item.get("target_path") == artifacts["readback.json"]["file_path"] and item.get("content_sha256") == hashlib.sha256(contents["readback.json"]).hexdigest() and (item.get("details") or {}).get("verified") is True for item in root.get("effects", [])):
            raise PublicationError("repair_readback_unproven")
        tested = manifest.get("publication_test_input")
        if not isinstance(tested, dict) or tested.get("schema") != "seraph.repo-publication.tested-input.v1" or tested.get("job_id") != root["job_id"] or tested.get("authority_digest") != root["authority_digest"] or tested.get("base_digest") != binding["base_snapshot_digest"] or tested.get("patch_sha256") != binding["patch_sha256"] or tested.get("exit_code") != 0 or tested.get("environment_unchanged") is not True or tested.get("environment", {}).get("available") is not True:
            raise PublicationError("tested_base_equivalence_unavailable")
        if tested.get("tested_files") != tested.get("output_files"):
            raise PublicationError("test_mutated_publication_inputs")
        patch = read_file(binding["patch_artifact_id"])
        if hashlib.sha256(patch).hexdigest() != binding["patch_sha256"] or manifest.get("diff_sha256") != hashlib.sha256(contents["diff.patch"]).hexdigest():
            raise PublicationError("patch_digest_changed")
        binding["test_artifacts"] = {name: {"file_path": artifacts[name]["file_path"], "sha256": hashlib.sha256(raw).hexdigest()} for name, raw in contents.items()}
        binding["tested_input_digest"] = digest(tested)
        return binding, tested, patch

    async def tested_runtime_current(self, binding, tested):
        from src.execution.repo_sandbox import build_repo_repair_executor
        from src.execution.repo_publication_runtime import RuntimeUnavailable, posture_projection
        executor = build_repo_repair_executor()
        preflight = await asyncio.to_thread(executor.preflight)
        environment = tested.get("environment") or {}
        try:
            tested_projection = posture_projection(environment.get("runtime_proof"), environment.get("configuration_revision"))
        except RuntimeUnavailable as exc:
            raise PublicationError("tested_runtime_or_configuration_changed") from exc
        if not preflight.ok or preflight.executor_kind != "local" or preflight.posture.get("profile") != "repo-python-pytest-publication-v1" or preflight.posture_digest != binding["repair_executor_posture_digest"] or any(preflight.posture.get(key) != value for key, value in tested_projection.items()):
            raise PublicationError("tested_runtime_or_configuration_changed")

    async def prepare(self, request, principal, session):
        branch(request.base_branch); branch(request.branch_name, feature=True); oid(request.expected_base_commit)
        try:
            key = uuid.UUID(request.idempotency_key)
        except ValueError as exc:
            raise PublicationError("idempotency_key_invalid", status_code=422) from exc
        operation = str(uuid.uuid5(uuid.NAMESPACE_URL, f"seraph:repo-publication:{principal}:{session}:{key}"))
        job_id = "repo-publication-" + uuid.UUID(operation).hex
        request_digest = digest(request.model_dump())
        old = await jobs.get_job(job_id)
        if old:
            old = await self.owned(job_id, principal, session)
            if old["declared_authority"].get("request_digest") != request_digest:
                raise PublicationError("idempotency_conflict")
            return await self.view(old)
        binding, tested, patch = await self.repair(request, principal, session)
        await self.tested_runtime_current(binding, tested)
        connection = await self.adapter._get_connection_row(principal)
        if not connection or connection.mode != "active" or connection.revision != request.expected_connection_revision:
            raise PublicationError("connection_not_current")
        consent_binding = await require_consent(connection, principal=principal, root=session,
            repository=connection.repository, revision=connection.revision, required_actions=PUBLICATION_ACTIONS)
        if (await self.adapter.get_connection(principal)).get("credential_configured") is not True:
            raise PublicationError("credential_not_configured")
        source_path = Path(canonical_workspace_root(settings.workspace_dir)) / binding["repository_ref"]
        source = SourceGit(source_path)
        if source.head() != request.expected_base_commit:
            raise PublicationError("source_commit_base_mismatch")
        tree, base_files, _ = source.tree(request.expected_base_commit)
        equivalent(base_files, tested["base_files"])
        equivalent(file_manifest(source_path), tested["base_files"])
        local_posture = posture()
        preview = {"schema": "seraph.repo-publication.preview.v1", "operation_id": operation, "job_id": job_id, "owner_principal_id": principal, "owner_session_id": session, "request": request.model_dump(), "repair_binding": binding, "tested_input": tested, "repository": connection.repository, "connection_id": connection.id, "connection_revision": connection.revision, "base_branch": request.base_branch, "base_commit": request.expected_base_commit, "base_tree": tree, "branch_name": request.branch_name, "commit_message": request.commit_message, "commit_date": now().replace(microsecond=0).isoformat(), "title": request.title, "body": request.body + f"\n\n<!-- seraph-operation:{operation} -->", "local_posture": local_posture, "local_posture_digest": digest(local_posture), "required_permissions": PERMISSIONS, "transport": "git_data_rest", "learning": "no_learning"}
        preview["github_consent"] = consent_binding
        try:
            preview["commit_date"] = datetime.fromisoformat(binding["repair_finished_at"]).replace(tzinfo=timezone.utc, microsecond=0).isoformat()
        except (TypeError, ValueError) as exc:
            raise PublicationError("repair_completion_time_unproven") from exc
        preview_digest = digest(preview)
        path = f"artifacts/repo-publication/{job_id}/preview.json"
        authority = {"session_id": session, "principal": principal, "connection_id": connection.id, "connection_revision": connection.revision, "repository": connection.repository, "request_digest": request_digest, "preview_path": path, "preview_digest": preview_digest, "required_permissions": PERMISSIONS, "local_host_execution_required": True, "executor_kind": "local", "executor_posture_digest": preview["local_posture_digest"], "repair_binding": binding, "github_consent": consent_binding}
        authority["budget_microusd"] = 0
        admitted = await jobs.admit_job(DurableJobSpec(identity=DurableJobIdentity(job_id=job_id, owner_kind="user", owner_principal_id=principal, job_kind=CAPABILITY, capability_version="1", idempotency_scope="repo-publication", idempotency_key=str(key)), inputs={"request_digest": request_digest, "preview_digest": preview_digest}, session_id=session, operator_session_id=session, conversation_id=session, goal_id=binding["goal_id"], goal_revision=binding["goal_revision"], dependencies=(request.repair_job_id,), declared_authority=authority, deadline_at=now() + timedelta(minutes=10), max_attempts=2, priority=50, budget_microusd=0, run_fingerprint=preview_digest))
        if admitted["status"] != "accepted":
            return await self.view(admitted)
        current = await jobs.queue_job(job_id, expected_revision=admitted["revision"])
        current = await jobs.claim_job(job_id, owner="repo-publication:" + job_id, expected_revision=current["revision"], lease_seconds=120)
        write_file(path, json.dumps(preview, sort_keys=True, separators=(",", ":")).encode())
        current = await jobs.record_artifact(job_id, file_path=path, artifact_type="repo_publication_preview", content=read_file(path), **self.fence(current))
        fingerprint = fingerprint_tool_call(CAPABILITY, {"preview_digest": preview_digest}, approval_context={"required_permissions": PERMISSIONS, "preview_digest": preview_digest, "local_posture_digest": preview["local_posture_digest"]})
        details = {"action": "repo_publication.publish", "required_permissions": PERMISSIONS, "local_host_execution_required": True, "executor_kind": "local", "executor_posture": local_posture, "executor_posture_digest": preview["local_posture_digest"], "preview_digest": preview_digest, "approval_scope": {"repository": connection.repository, "base_branch": request.base_branch, "base_commit": request.expected_base_commit, "branch_name": request.branch_name, "local_host_execution": local_posture, "remote_effects": PERMISSIONS[1:], "preview_digest": preview_digest, "tested_input_digest": binding["tested_input_digest"]}, "approval_owner_principal_id": principal, "approval_owner_operator_session_id": session, "operator_session_id": session, "approval_conversation_id": session, "durable_job_id": job_id, "durable_owner_kind": "user", "durable_owner_principal_id": principal, "durable_service_id": None, "durable_authority_digest": current["authority_digest"], "durable_goal_id": binding["goal_id"], "durable_goal_revision": binding["goal_revision"], "durable_plan_revision": None, "durable_capability_version": "1", "durable_budget_digest": current["budget_digest"], "approval_expires_at": (now() + timedelta(minutes=5)).timestamp()}
        approval = await approval_repository.get_or_create_pending(session_id=session, tool_name=CAPABILITY, risk_level="high", summary=f"Execute local Git and publish tested patch to {connection.repository}:{request.branch_name} as a ready PR", fingerprint=fingerprint, details=details)
        current = await jobs.bind_approval_id(job_id, approval.id, **self.fence(current))
        await approval_repository.update_pending_details(approval.id, owner_principal_id=principal, operator_session_id=session, updates={"durable_authority_digest": current["authority_digest"], "durable_approval_id": approval.id, "approval_operator_principal_id": principal, "approval_expires_at": _approval_expiry(approval.expires_at).timestamp()})
        current = await jobs.transition_job(job_id, "awaiting_approval", reason="repo_publication_fresh_approval_required", **self.fence(current))
        return await self.view(current)

    @staticmethod
    def fence(current):
        lease = current.get("lease") or {}
        return {"owner": lease.get("owner"), "fencing_token": lease.get("fencing_token"), "expected_revision": current["revision"]}

    def preview(self, current):
        authority = current.get("declared_authority") or {}
        value = json.loads(read_file(authority["preview_path"]))
        if digest(value) != authority.get("preview_digest"):
            raise PublicationError("preview_changed")
        return value

    async def view(self, current):
        preview = self.preview(current)
        approval = await approval_repository.get((current.get("declared_authority") or {}).get("approval_id", ""))
        result = None
        if current["status"] == "succeeded":
            path = f"artifacts/repo-publication/{current['job_id']}/result.json"
            raw = read_file(path)
            artifact = next((item for item in current.get("artifacts", []) if item.get("file_path") == path and item.get("artifact_type") == "repo_publication_result"), None)
            if not artifact or artifact.get("content_sha256") != hashlib.sha256(raw).hexdigest():
                raise PublicationError("publication_result_unverified")
            result = json.loads(raw)
            if result.get("job_id") != current["job_id"] or result.get("preview_digest") != digest(preview) or result.get("verification") != "passed":
                raise PublicationError("publication_result_unverified")
        return {"job_id": current["job_id"], "capability_id": CAPABILITY, "revision": current["revision"], "status": current["status"], "reason_code": current.get("failure_reason"), "preview": preview, "preview_digest": digest(preview), "approval_id": approval.id if approval else None, "approval_status": approval.status if approval else "unavailable", "approval_expires_at": _approval_timestamp(approval.expires_at) if approval else None, "effects": current.get("effects", []), "artifacts": current.get("artifacts", []), "result": result, "learning": "no_learning", "github_capacity_closure": current.get("github_capacity_closure"), "recovery_action": "observe" if current.get("github_capacity_closure") else "reconcile" if current["status"] in {"unknown_external_effect", "blocked"} else "inspect"}

    async def check(self, current, preview, *, writing=True, full_runtime=False):
        principal, session = preview["owner_principal_id"], preview["owner_session_id"]
        await self.live(principal, session)
        latest = await self.owned(current["job_id"], principal, session)
        if writing and (latest["status"] != "running" or latest.get("lease") != current.get("lease")):
            raise PublicationError("publication_lease_changed")
        if writing:
            expires = datetime.fromisoformat(str(latest.get("lease", {}).get("expires_at") or ""))
            deadline = datetime.fromisoformat(str(latest.get("deadline_at") or ""))
            if expires.replace(tzinfo=timezone.utc) <= now() or deadline.replace(tzinfo=timezone.utc) <= now():
                raise PublicationError("publication_deadline_expired")
        connection = await self.adapter._get_connection_row(principal)
        if not connection or connection.id != preview["connection_id"] or connection.repository != preview["repository"] or (writing and (connection.mode != "active" or connection.revision != preview["connection_revision"])):
            raise PublicationError("connection_authority_changed")
        if not writing:
            reservation = next((item.get("payload") for item in latest.get("checkpoints", []) if item.get("checkpoint_id") == "connection_reserved"), None)
            self.require(isinstance(reservation, dict) and type(current.get("_readback_revision")) is int, "publication_readback_acknowledgment_required")
            await require_readback(connection, principal=principal, root=session,
                original_binding=preview["github_consent"], expected_revision=current["_readback_revision"],
                job_id=current["job_id"], connection_fence=reservation["connection_fence"])
        if writing:
            await require_consent(connection, principal=principal, root=session,
                repository=preview["repository"], revision=preview["connection_revision"],
                required_actions=PUBLICATION_ACTIONS, binding=preview["github_consent"])
            reservation = next((item.get("payload") for item in latest.get("checkpoints", []) if item.get("checkpoint_id") == "connection_reserved"), None)
            if reservation and (connection.active_job_id != latest["job_id"] or connection.active_fence != reservation.get("connection_fence")):
                raise PublicationError("connection_reservation_changed")
            binding, tested, _ = await self.repair(PrepareRequest(**preview["request"]), principal, session)
            from src.execution.repo_sandbox import build_repo_repair_executor
            selected = build_repo_repair_executor()
            if selected.kind != "local" or digest(selected.config.model_dump(mode="json")) != tested["environment"]["configuration_revision"]:
                raise PublicationError("tested_runtime_or_configuration_changed")
            if full_runtime:
                await self.tested_runtime_current(binding, tested)
            if binding != preview["repair_binding"] or tested != preview["tested_input"] or digest(posture()) != preview["local_posture_digest"]:
                raise PublicationError("publication_source_or_posture_changed")
            source = SourceGit(Path(canonical_workspace_root(settings.workspace_dir)) / binding["repository_ref"])
            if source.head() != preview["base_commit"]:
                raise PublicationError("source_commit_base_mismatch")
            equivalent(file_manifest(source.root), tested["base_files"])
            approval = await approval_repository.get(latest["declared_authority"].get("approval_id", ""))
            if not approval or approval.status not in {"approved", "consumed"} or approval.owner_principal_id != principal or approval.operator_session_id != session or not approval.expires_at or approval.expires_at.replace(tzinfo=timezone.utc) <= now():
                raise PublicationError("publication_approval_not_current")
            details = json.loads(approval.details_json)
            if details.get("required_permissions") != PERMISSIONS or details.get("preview_digest") != digest(preview) or details.get("executor_posture_digest") != preview["local_posture_digest"]:
                raise PublicationError("publication_approval_binding_changed")
            if approval.status == "consumed" and not any(item.get("kind") == "approval_resume" and item.get("approval_id") == approval.id and item.get("authority_digest") == latest["authority_digest"] for item in latest.get("effects", [])):
                raise PublicationError("publication_approval_resume_unproven")
        return connection

    async def request(self, current, preview, path, *, method="GET", body=None, writing=True):
        async def authority():
            await self.check(current, preview, writing=writing)
        connection = await self.check(current, preview, writing=writing)
        from src.vault.repository import vault_repository
        snapshot = await vault_repository.snapshot(connection.vault_key, owner_principal_id=preview["owner_principal_id"])
        if snapshot is None or snapshot.binding_digest != preview["github_consent"]["vault_binding_digest"]:
            raise PublicationError("publication_vault_binding_changed")
        token = snapshot.value
        timeout = 20.0
        if writing:
            expiry = datetime.fromisoformat(current["lease"]["expires_at"]).replace(tzinfo=timezone.utc)
            deadline = datetime.fromisoformat(current["deadline_at"]).replace(tzinfo=timezone.utc)
            timeout = min(timeout, (min(expiry, deadline) - now()).total_seconds())
            if timeout <= 0:
                raise PublicationError("publication_deadline_expired")
        response = await self.adapter.request_repo_publication(path, method=method, token=token, json_body=body, authority_check=authority, timeout_seconds=timeout,
            consent_binding=preview["github_consent"], owner_principal_id=preview["owner_principal_id"], owner_session_id=preview["owner_session_id"], readback_authority=current.get("_readback_authority") if not writing else None)
        if response.status_code not in ({201} if method == "POST" else {200}):
            raise PublicationError("remote_response_unverified")
        try:
            return json.loads(response.content)
        except (ValueError, TypeError) as exc:
            raise PublicationError("remote_response_invalid") from exc

    async def effect(self, current, name, path, expected, preview, *, readback=False, details=None):
        return await jobs.record_effect(current["job_id"], effect_type="repo_publication_" + name, effect_id=current["job_id"] + ":" + name, target_path=path, target_digest=digest(expected), approval_id=current["declared_authority"].get("approval_id"), status="succeeded" if readback else "intent", receipt_kind="readback" if readback else "effect", content_sha256=digest(expected) if readback else None, readback_id=current["job_id"] + ":readback:" + name if readback else None, verified_at=now().isoformat() if readback else None, details={"verified": readback, **(details or {})}, **(self.fence(current) if current["status"] == "running" else {"expected_revision": current["revision"]}))

    async def write_then_read(self, current, preview, name, path, body, readpath, validate):
        current = await self.effect(current, name, path, body, preview)
        response = await self.request(current, preview, path, method="POST", body=body)
        read_path = readpath(response)
        read = await self.request(current, preview, read_path)
        validate(read)
        current = await self.effect(current, name, path, body, preview, readback=True, details={"remote_identity": read.get("sha") or read.get("number")})
        return current, read

    async def execute(self, job_id, principal, session):
        current = await self.owned(job_id, principal, session)
        if current["status"] in {"succeeded", "cancelled", "unknown_external_effect", "blocked", "failed"}:
            return await self.view(current)
        preview = self.preview(current)
        if current["status"] == "awaiting_approval":
            approval = await approval_repository.get(current["declared_authority"]["approval_id"])
            if not approval or approval.status != "approved":
                return await self.view(current)
            expiry = _approval_expiry(approval.expires_at)
            self.require(expiry is not None and expiry > now(), "publication_approval_not_current")
            expires = expiry.timestamp()
            fields = {"approval_id": approval.id, "authority_digest": current["authority_digest"], "goal_id": current["goal_id"], "goal_revision": current["goal_revision"], "plan_revision": current.get("plan_revision"), "capability_version": "1", "owner_kind": "user", "owner_principal_id": principal, "service_id": None, "budget_microusd": 0, "budget_digest": current["budget_digest"], "operator_principal_id": principal, "operator_session_id": session, "expires_at": expires}
            current = await jobs.resume_approved_job(job_id, approval_receipt={**fields, "status": "approved", "authenticated": True}, expected_revision=current["revision"], **fields)
        if current["status"] != "queued":
            return await self.view(current)
        current = await jobs.claim_job(job_id, owner="repo-publication:" + job_id, lease_seconds=120, expected_revision=current["revision"])
        reserved = None
        try:
            await self.check(current, preview, full_runtime=True)
            reserved = await self.adapter._reserve_connection(owner_principal_id=principal, connection_id=preview["connection_id"], expected_revision=preview["connection_revision"], job_id=job_id)
            current = await jobs.record_checkpoint(job_id, checkpoint_id="connection_reserved", state={"connection_fence": reserved}, checkpoint_payload={"connection_fence": reserved}, **self.fence(current))
            stage = Path(canonical_workspace_root(settings.workspace_dir)) / f"artifacts/repo-publication/{job_id}/producer"
            source = SourceGit(Path(canonical_workspace_root(settings.workspace_dir)) / preview["repair_binding"]["repository_ref"])
            local_expected = {"profile": preview["local_posture"], "base_commit": preview["base_commit"], "patch_sha256": preview["repair_binding"]["patch_sha256"]}
            current = await self.effect(current, "local_producer", str(stage.relative_to(Path(canonical_workspace_root(settings.workspace_dir)))), local_expected, preview)
            patch = read_file(preview["repair_binding"]["patch_artifact_id"])
            await self.check(current, preview, full_runtime=True)
            # Synchronous Git argv stays in a worker thread; async authority is
            # rechecked before and after bounded execution, never via a fake grant.
            loop = asyncio.get_running_loop()
            def before_command():
                asyncio.run_coroutine_threadsafe(self.check(current, preview), loop).result(timeout=15)
            from src.execution.repo_publication_supervisor import guard, admit, run as supervised_run, canonical as supervisor_bytes
            try:
                with guard(stage) as (_, directory, guard_fd):
                    approval = await approval_repository.get(current["declared_authority"]["approval_id"])
                    approval_expiry = _approval_expiry(approval.expires_at) if approval else None
                    self.require(approval_expiry is not None and approval_expiry > now(), "publication_approval_not_current")
                    remaining = min(30.0, (min(
                        approval_expiry,
                        datetime.fromisoformat(current["lease"]["expires_at"]).replace(tzinfo=timezone.utc),
                        datetime.fromisoformat(current["deadline_at"]).replace(tzinfo=timezone.utc),
                    ) - now()).total_seconds())
                    admission = admit(stage, source, preview, patch, {
                        "job_id": job_id, "root": session, "principal": principal,
                        "attempt": current["attempt_count"], "authority_digest": current["authority_digest"],
                        "input_digest": current["input_digest"], "run_fingerprint": current["run_fingerprint"],
                        "fence": current["lease"]["fencing_token"], "goal_id": current["goal_id"],
                        "goal_revision": current["goal_revision"], "preview_digest": digest(preview),
                    }, directory=directory, guard_fd=guard_fd, seconds_limit=remaining)
                    supervisor_binding = admission.checkpoint()
                    current = await jobs.record_checkpoint(job_id, checkpoint_id="publication_supervisor_admission", state={"admission_sha256": admission.digest}, checkpoint_payload=supervisor_binding, **self.fence(current))
                    await self.check(current, preview, full_runtime=True)
                    produced = await asyncio.to_thread(supervised_run, admission, before_command)
                    await self.check(current, preview, full_runtime=True)
                    private_proof = produced["supervisor_proof"]
                    proof_path = str((admission.path.parent / (admission.payload["token"] + ".terminal.json")).relative_to(Path(canonical_workspace_root(settings.workspace_dir))))
                    proof_bytes = supervisor_bytes(private_proof)
                    current = await jobs.record_artifact(job_id, file_path=proof_path, artifact_type="repo_publication_supervisor_terminal", content=proof_bytes, **self.fence(current))
                    current = await jobs.record_checkpoint(job_id, checkpoint_id="publication_supervisor_terminal", state={"quiescent": True}, checkpoint_payload={
                        "admission_sha256": admission.digest, "proof_path": proof_path,
                        "proof_sha256": hashlib.sha256(proof_bytes).hexdigest(), "status": private_proof["status"],
                        "quiescent": True, "stage_output": private_proof["stage_output"],
                    }, **self.fence(current))
            except ValueError as exc:
                raise PublicationError(str(exc).split(":", 1)[0]) from exc
            current = await self.effect(current, "local_producer", str(stage.relative_to(Path(canonical_workspace_root(settings.workspace_dir)))), local_expected, preview, readback=True, details={"local_commit": produced["local_commit"], "tree": produced["tree"]})
            local_checkpoint = {"local_commit": produced["local_commit"], "tree": produced["tree"], "changed_paths": produced["changed_paths"]}
            current = await jobs.record_checkpoint(job_id, checkpoint_id="local_commit", state=local_checkpoint, checkpoint_payload=local_checkpoint, **self.fence(current))
            prefix = f"/repos/{preview['repository']}"
            remote_base = await self.request(current, preview, prefix + "/git/ref/heads/" + preview["base_branch"])
            self.require(remote_base.get("object", {}).get("sha") == preview["base_commit"], "remote_base_changed")
            await self.verify_tree(current, preview, preview["base_commit"], preview["base_tree"], preview["tested_input"]["base_files"])
            for item in produced["files"]:
                if item["path"] not in produced["changed_paths"]:
                    continue
                raw = read_file(str((stage / item["path"]).relative_to(Path(canonical_workspace_root(settings.workspace_dir)))))
                expected_blob = object_id("blob", raw)
                body = {"content": base64.b64encode(raw).decode(), "encoding": "base64"}
                def blob_match(value, raw=raw, identity=expected_blob):
                    self.require(value.get("sha") == identity and value.get("encoding") == "base64" and base64.b64decode(value.get("content", "")) == raw, "remote_blob_mismatch")
                current, _ = await self.write_then_read(current, preview, "blob_" + hashlib.sha256(item["path"].encode()).hexdigest()[:16], prefix + "/git/blobs", body, lambda response: prefix + "/git/blobs/" + oid(response.get("sha")), blob_match)
            tree_body = {"base_tree": preview["base_tree"], "tree": [{"path": path, "mode": next((item["mode"] for item in produced["files"] if item["path"] == path), "100644"), "type": "blob", "sha": object_id("blob", (stage / path).read_bytes()) if (stage / path).exists() else None} for path in produced["changed_paths"]]}
            current, remote_tree = await self.write_then_read(current, preview, "tree", prefix + "/git/trees", tree_body, lambda response: prefix + "/git/trees/" + oid(response.get("sha")), lambda value: self.require(value.get("sha") == produced["tree"], "remote_tree_identity_mismatch"))
            person = {"name": "Seraph", "email": "seraph@localhost", "date": preview["commit_date"]}
            commit_body = {"message": preview["commit_message"], "tree": remote_tree["sha"], "parents": [preview["base_commit"]], "author": person, "committer": person}
            current, commit = await self.write_then_read(current, preview, "commit", prefix + "/git/commits", commit_body, lambda response: prefix + "/git/commits/" + oid(response.get("sha")), lambda value: self.verify_commit(value, preview, produced["tree"]))
            # Native Git and Git Data REST commits are distinct labeled IDs.
            current, _ = await self.write_then_read(current, preview, "branch", prefix + "/git/refs", {"ref": "refs/heads/" + preview["branch_name"], "sha": commit["sha"]}, lambda response: prefix + "/git/ref/heads/" + preview["branch_name"], lambda value: self.require(value.get("ref") == "refs/heads/" + preview["branch_name"] and value.get("object", {}).get("sha") == commit["sha"], "remote_branch_mismatch"))
            pr_body = {"title": preview["title"], "body": preview["body"], "head": preview["branch_name"], "base": preview["base_branch"], "draft": False}
            current, pr = await self.write_then_read(current, preview, "pr", prefix + "/pulls", pr_body, lambda response: prefix + "/pulls/" + str(self.positive(response.get("number"))), lambda value: self.verify_pr(value, preview, commit["sha"]))
            await self.verify_tree(current, preview, commit["sha"], produced["tree"], produced["files"])
            await self.verify_pr_diff(current, preview, pr["number"], produced["changed_paths"], produced["files"])
            current = await self.finalize(current, preview, pr["number"], commit["sha"], produced["local_commit"])
            await self.adapter._release_connection(connection_id=preview["connection_id"], owner_principal_id=principal, job_id=job_id, fence=reserved)
        except Exception as exc:
            logger.exception("Repository publication execution did not verify")
            latest = await jobs.get_job(job_id)
            if latest and latest["status"] == "running":
                unsafe = any(item.get("status") == "intent" for item in latest.get("effects", []) if item.get("effect_type", "").startswith("repo_publication_"))
                current = await jobs.transition_job(job_id, "unknown_external_effect" if unsafe else "blocked", reason=getattr(exc, "code", "publication_execution_unverified"), **self.fence(latest))
                if not unsafe and reserved:
                    await self.adapter._release_connection(connection_id=preview["connection_id"], owner_principal_id=principal, job_id=job_id, fence=reserved)
            else:
                current = latest or current
        return await self.view(current)

    @staticmethod
    def require(condition, code):
        if not condition:
            raise PublicationError(code)

    @staticmethod
    def positive(value):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise PublicationError("remote_id_invalid")
        return value

    def verify_pr(self, value, preview, commit):
        self.positive(value.get("number"))
        self.require(value.get("title") == preview["title"] and value.get("body") == preview["body"] and value.get("draft") is False and value.get("state") == "open" and value.get("head", {}).get("ref") == preview["branch_name"] and value.get("head", {}).get("sha") == commit and value.get("head", {}).get("repo", {}).get("full_name") == preview["repository"] and value.get("base", {}).get("ref") == preview["base_branch"] and value.get("base", {}).get("sha") == preview["base_commit"] and value.get("base", {}).get("repo", {}).get("full_name") == preview["repository"], "remote_pr_mismatch")

    def verify_commit(self, value, preview, tree):
        oid(value.get("sha"))
        self.require(value.get("tree", {}).get("sha") == tree and [parent.get("sha") for parent in value.get("parents", [])] == [preview["base_commit"]] and value.get("message") == preview["commit_message"], "remote_commit_mismatch")
        for field in ("author", "committer"):
            person = value.get(field) or {}
            try:
                actual = datetime.fromisoformat(person.get("date", "").replace("Z", "+00:00"))
                expected = datetime.fromisoformat(preview["commit_date"])
            except (ValueError, TypeError):
                raise PublicationError("remote_commit_identity_mismatch") from None
            self.require(person.get("name") == "Seraph" and person.get("email") == "seraph@localhost" and actual == expected, "remote_commit_identity_mismatch")

    async def verify_tree(self, current, preview, commit, tree, expected, *, writing=True):
        prefix = f"/repos/{preview['repository']}"
        value = await self.request(current, preview, prefix + "/git/commits/" + commit, writing=writing)
        self.require(value.get("sha") == commit and value.get("tree", {}).get("sha") == tree, "remote_commit_tree_mismatch")
        value = await self.request(current, preview, prefix + "/git/trees/" + tree + "?recursive=1", writing=writing)
        self.require(value.get("truncated") is False and isinstance(value.get("tree"), list), "remote_tree_truncated")
        files = []
        for item in value["tree"]:
            if item.get("type") == "tree" and item.get("mode") == "040000":
                continue
            self.require(item.get("type") == "blob" and item.get("mode") in {"100644", "100755"}, "remote_mode_unsupported")
            blob = await self.request(current, preview, prefix + "/git/blobs/" + oid(item.get("sha")), writing=writing)
            raw = base64.b64decode(blob.get("content", ""))
            self.require(blob.get("encoding") == "base64" and object_id("blob", raw) == item["sha"], "remote_blob_mismatch")
            files.append({"path": item.get("path"), "mode": item["mode"], "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        equivalent(sorted(files, key=lambda item: item["path"]), expected)

    async def finalize(self, current, preview, number, remote_commit, local_commit):
        await self.live(preview["owner_principal_id"], preview["owner_session_id"])
        await self.adapter._require_current_goal(current)
        output = {"schema": "seraph.repo-publication.result.v1", "job_id": current["job_id"], "repair_job_id": preview["repair_binding"]["repair_job_id"], "goal_id": current["goal_id"], "goal_revision": current["goal_revision"], "repository": preview["repository"], "pr_number": number, "browser_url": f"https://github.com/{preview['repository']}/pull/{number}", "remote_rest_commit": remote_commit, "local_git_commit": local_commit, "transport": "git_data_rest", "preview_digest": digest(preview), "verification": "passed", "usefulness": "unknown", "learning": "no_learning"}
        path = f"artifacts/repo-publication/{current['job_id']}/result.json"
        raw = json.dumps(output, sort_keys=True).encode()
        write_file(path, raw)
        if current["status"] == "running":
            current = await jobs.record_artifact(current["job_id"], file_path=path, artifact_type="repo_publication_result", content=raw, **self.fence(current))
            return await jobs.transition_job(current["job_id"], "succeeded", result=output, result_summary="Ready PR and tested tree independently verified; no learning", **self.fence(current))
        current = await jobs.record_recovery_artifact(current["job_id"], owner_kind="user", owner_principal_id=preview["owner_principal_id"], file_path=path, artifact_type="repo_publication_result", content=raw, expected_revision=current["revision"])
        return await jobs.finalize_reconciled_job(current["job_id"], owner_kind="user", owner_principal_id=preview["owner_principal_id"], expected_revision=current["revision"], result=output, result_summary="Ready PR readback reconciled; no learning")

    async def cancel(self, job_id, principal, session):
        current = await self.owned(job_id, principal, session)
        cancelled = await jobs.cancel_job(job_id, expected_revision=current["revision"], reason="operator_cancelled_publication")
        approval_id = current["declared_authority"].get("approval_id")
        approval = await approval_repository.get(approval_id or "")
        if approval and approval.status in {"pending", "approved"}:
            await approval_repository.revoke_unconsumed(approval_id,
                expected_revision=approval_state_revision(approval), owner_principal_id=principal,
                operator_session_id=session)
        return await self.view(cancelled)

    async def verify_pr_diff(self, current, preview, number, changed_paths, files, *, writing=True):
        expected = {item["path"]: item for item in files}
        actual = set()
        for page in range(1, 22):
            values = await self.request(current, preview,
                f"/repos/{preview['repository']}/pulls/{number}/files?per_page=100&page={page}", writing=writing)
            self.require(isinstance(values, list) and len(values) <= 100, "remote_pr_diff_invalid")
            for value in values:
                path, status = value.get("filename"), value.get("status")
                self.require(isinstance(path, str) and path not in actual and path in changed_paths and status in {"added", "removed", "modified", "renamed"}, "remote_pr_diff_mismatch")
                actual.add(path)
                if status == "renamed":
                    previous = value.get("previous_filename")
                    self.require(previous in changed_paths and previous not in actual, "remote_pr_diff_mismatch")
                    actual.add(previous)
                if status != "removed":
                    item = expected.get(path)
                    self.require(item is not None and oid(value.get("sha")) == object_id("blob", read_file(f"artifacts/repo-publication/{current['job_id']}/producer/{path}")), "remote_pr_diff_blob_mismatch")
            if len(values) < 100:
                self.require(actual == set(changed_paths), "remote_pr_diff_mismatch")
                return
        raise PublicationError("remote_pr_diff_truncated")

    async def reconcile(self, job_id, principal, session, request):
        current = await self.owned(job_id, principal, session)
        if current.get("github_capacity_closure"):
            return await self.observe_closed(current, request)
        preview = self.preview(current)
        if current["status"] == "succeeded":
            return await self.view(current)
        if current["status"] == "running":
            current = await jobs.recover_stale_job(job_id)
        self.require(current["status"] in {"unknown_external_effect", "blocked", "failed"}, "publication_reconciliation_not_required")
        self.require(any(effect.get("effect_type") == "repo_publication_pr" for effect in current.get("effects", [])) and any(effect.get("effect_type") == "repo_publication_local_producer" and effect.get("receipt_kind") == "readback" and effect.get("status") == "succeeded" and effect.get("details", {}).get("local_commit") for effect in current.get("effects", [])), "publication_effect_intent_unproven")
        current["_readback_revision"] = request.expected_connection_revision
        reservation = next(item["payload"] for item in current["checkpoints"] if item.get("checkpoint_id") == "connection_reserved")
        read_authority = GitHubReadbackAuthority(principal, session, job_id, CAPABILITY,
            request.expected_connection_revision, reservation["connection_fence"], preview["github_consent"])
        current["_readback_authority"] = read_authority
        await self.check(current, preview, writing=False)
        prefix = f"/repos/{preview['repository']}"
        ref = await self.request(current, preview, prefix + "/git/ref/heads/" + preview["branch_name"], writing=False)
        self.require(ref.get("ref") == "refs/heads/" + preview["branch_name"], "remote_branch_mismatch")
        commit_id = oid(ref.get("object", {}).get("sha"))
        commit = await self.request(current, preview, prefix + "/git/commits/" + commit_id, writing=False)
        self.verify_commit(commit, preview, oid(commit.get("tree", {}).get("sha")))
        await self.verify_tree(current, preview, commit_id, oid(commit.get("tree", {}).get("sha")), preview["tested_input"]["tested_files"], writing=False)
        if request.pr_number:
            pr = await self.request(current, preview, prefix + "/pulls/" + str(request.pr_number), writing=False)
        else:
            head = quote(preview["repository"].split("/")[0] + ":" + preview["branch_name"], safe="")
            values = await self.request(current, preview, prefix + "/pulls?state=all&head=" + head + "&base=" + quote(preview["base_branch"], safe="") + "&per_page=100", writing=False)
            self.require(isinstance(values, list) and len(values) < 100, "pr_identity_required")
            matches = [value for value in values if value.get("body") == preview["body"] and value.get("title") == preview["title"]]
            self.require(len(matches) == 1, "pr_identity_required")
            pr = await self.request(current, preview, prefix + "/pulls/" + str(self.positive(matches[0].get("number"))), writing=False)
        self.verify_pr(pr, preview, commit_id)
        local_checkpoint = next((item["payload"] for item in current.get("checkpoints", []) if item.get("checkpoint_id") == "local_commit"), None)
        self.require(isinstance(local_checkpoint, dict), "publication_local_commit_unproven")
        await self.verify_pr_diff(current, preview, pr["number"], local_checkpoint["changed_paths"], preview["tested_input"]["tested_files"], writing=False)
        unresolved = [item for item in current.get("effects", []) if item.get("status") == "intent" and item.get("effect_type") == "repo_publication_pr"]
        self.require(len(unresolved) == 1, "publication_effect_intent_unproven")
        prior = unresolved[0]
        verified_at = now().isoformat()
        observation = {"schema": "seraph.github-effect-observation.v1", "job_id": job_id,
            "attempt_count": current["attempt_count"], "authority_digest": current["authority_digest"],
            "effect_id": prior["effect_id"], "effect_type": prior["effect_type"],
            "target_path": prior["target_path"], "target_digest": prior["target_digest"],
            "adapter_idempotency_key": prior.get("adapter_idempotency_key"),
            "readback_id": prior["effect_id"] + ":reconciled", "verified_at": verified_at,
            "remote_readback": {"pr_number": pr["number"], "remote_rest_commit": commit_id, "pr_sha256": digest(pr), "verified": True}}
        pr_path = prefix + "/pulls/" + str(pr["number"])
        fresh_pr = await self.request(current, preview, pr_path, writing=False)
        self.verify_pr(fresh_pr, preview, commit_id)
        identity = {key: observation[key] for key in ("job_id", "attempt_count", "authority_digest", "effect_id", "effect_type", "target_path", "target_digest", "adapter_idempotency_key")}
        verified = await self.adapter.verified_get_receipt(read_authority=read_authority, path=pr_path, payload=fresh_pr, effect_identity=identity)
        observation["remote_readback"].update(readback_path=pr_path, payload_sha256=verified.payload_sha256)
        raw = json.dumps(observation, sort_keys=True, separators=(",", ":")).encode()
        current = await jobs.record_github_recovery_observation(job_id, read_authority=read_authority, verified_readback=verified,
            expected_revision=current["revision"], expected_attempt_count=current["attempt_count"],
            expected_authority_digest=current["authority_digest"], effect_id=prior["effect_id"],
            effect_type=prior["effect_type"], target_path=prior["target_path"], target_digest=prior["target_digest"],
            adapter_idempotency_key=prior.get("adapter_idempotency_key"), readback_id=observation["readback_id"],
            verified_at=verified_at, artifact_content=raw, artifact_sha256=hashlib.sha256(raw).hexdigest())
        if current["receipt"].get("blocked_current_goal"):
            view = await self.view(current)
            view["reason_code"] = "publication_current_goal_changed"
            view["observation_only"] = True
            return view
        # Exact recorded effects are settled with independently verified target
        # identities. A full matching PR/tree proves all recorded remote steps.
        for effect in list(current.get("effects", [])):
            if not str(effect.get("effect_type", "")).startswith("repo_publication_") or effect.get("status") != "intent":
                continue
            current = await jobs.record_readback(job_id, effect_id=effect["effect_id"], effect_type=effect["effect_type"], target_path=effect["target_path"], target_digest=effect["target_digest"], content_sha256=verified.semantic_payload_sha256 if effect["effect_id"] == prior["effect_id"] else effect["target_digest"], status="succeeded", readback_id=effect["effect_id"] + ":reconciled", verified_at=verified_at if effect["effect_id"] == prior["effect_id"] else now().isoformat(), details={"verified": True, "pr_number": pr["number"], "remote_rest_commit": commit_id, "reconciliation_owner_id": principal}, expected_revision=current["revision"])
        local = next((item.get("details", {}).get("local_commit") for item in current.get("effects", []) if item.get("effect_type") == "repo_publication_local_producer"), None)
        current = await self.finalize(current, preview, pr["number"], commit_id, local)
        connection = await self.adapter._get_connection_row(principal)
        if connection and connection.active_job_id == job_id:
            await self.adapter._release_connection(connection_id=connection.id, owner_principal_id=principal, job_id=job_id, fence=connection.active_fence)
        return await self.view(current)

    async def close_capacity(self, job_id, principal, session, request):
        from src.execution.repo_publication_supervisor import guard
        from src.extensions.github_capacity_closure import (ReadWindow, original_effects,
            effect_identity, _mint_complete_proof)
        from src.extensions.github_recovery import capture_binding, check_binding
        from src.workflows.repo_publication_closure import capture_inputs, collect_pr_boundary
        current = await self.owned(job_id, principal, session)
        repeated = await jobs.get_github_capacity_closure(job_id, request=request,
            principal=principal, root=session)
        if repeated is not None:
            return await self.view(repeated)
        self.require(current["revision"] == request.expected_job_revision and current["status"] in {"unknown_external_effect", "blocked", "failed"} and not current["lease"].get("owner") and not current["lease"].get("expires_at"), "publication_capacity_close_unleased_revision_required")
        preview = self.preview(current)
        authority = GitHubReadbackAuthority(principal, session, job_id, CAPABILITY,
            request.expected_connection_revision, request.expected_connection_fence,
            preview["github_consent"])
        stage = Path(canonical_workspace_root(settings.workspace_dir)) / f"artifacts/repo-publication/{job_id}/producer"
        with guard(stage) as (_, _, guard_fd):
            window = ReadWindow()
            snapshot = await authority.validate()
            async with db_engine.get_session() as db:
                run = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))).scalars().one()
                binding = await capture_binding(db, run, authority, snapshot)
                await check_binding(db, run, binding)
            # Positive terminal inspection precedes every external contact;
            # prefix completion may close a local-only interrupted attempt.
            _mint_complete_proof(current=current, binding=binding, positive_gets=[], guard_fd=guard_fd, window=window)
            remote = [item for item in original_effects(current) if item["effect_type"] != "repo_publication_local_producer"]
            inputs = capture_inputs(self, current, preview, request) if remote else None
            async def check():
                await authority.validate()
                latest = await self.owned(job_id, principal, session)
                self.require(latest["revision"] == current["revision"] and latest["effects"] == current["effects"] and latest["lease"] == current["lease"] and latest["authority_digest"] == current["authority_digest"], "publication_capacity_job_changed")
                window.remaining()
            async def get(path):
                await check()
                response = await self.adapter.request_repo_publication(path, method="GET",
                    token=snapshot.value, authority_check=check,
                    consent_binding=preview["github_consent"], owner_principal_id=principal,
                    owner_session_id=session, readback_authority=authority, read_window=window,
                    timeout_seconds=window.remaining())
                self.require(response.status_code == 200, "publication_capacity_positive_get_required")
                try:
                    payload = json.loads(response.content)
                except (ValueError, TypeError):
                    raise PublicationError("publication_capacity_invalid_response") from None
                return payload
            boundary = None
            if any(item["effect_type"] == "repo_publication_pr" for item in remote):
                # Validate immutable body and required exact locators before GET.
                for item in remote:
                    target, body, _ = inputs.expected(item)
                    self.require(item["target_path"] == target and item["target_digest"] == digest(body), "publication_closure_original_intent_changed")
                boundary = await collect_pr_boundary(inputs, self, get, authority, window)
            positives = []
            for item in remote:
                target, body, path = inputs.expected(item)
                self.require(item["target_path"] == target and item["target_digest"] == digest(body), "publication_closure_original_intent_changed")
                payload = await get(path)
                identity = effect_identity(current, item)
                actual = await self.adapter.verified_get_receipt(read_authority=authority,
                    path=path, payload=payload, effect_identity=identity,
                    publication_inputs=inputs, read_window=window, publication_boundary=boundary)
                self.require(actual.canonical_binding == binding, "publication_capacity_binding_changed")
                positives.append((actual, identity))
            await check()
            proof = _mint_complete_proof(current=current, binding=binding,
                positive_gets=positives, guard_fd=guard_fd, window=window)
            closed = await jobs.record_github_capacity_closure(job_id, request=request,
                read_authority=authority, proof=proof)
            return await self.view(closed)

    async def observe_closed(self, current, request):
        from src.execution.repo_publication_supervisor import guard
        from src.extensions.github_recovery import closed_authority, record_closed_observation
        from src.extensions.github_capacity_closure import ReadWindow, original_effects, effect_identity
        from src.workflows.repo_publication_closure import capture_inputs, collect_pr_boundary
        authority = await closed_authority(current, expected_revision=request.expected_connection_revision)
        async with db_engine.get_session() as db:
            run = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == current["job_id"]))).scalars().one()
            history = json.loads(run.github_capacity_closure_json)
        remotes = [item for item in original_effects(current) if item["effect_type"] != "repo_publication_local_producer"]
        self.require(bool(remotes), "publication_closed_local_only_inspection_history")
        prior = remotes[-1]
        old_request = PublicationCloseRequest(**history["request"])
        preview = self.preview(current)
        stage = Path(canonical_workspace_root(settings.workspace_dir)) / f"artifacts/repo-publication/{current['job_id']}/producer"
        with guard(stage):
            inputs = capture_inputs(self, current, preview, old_request)
            if request.pr_number is not None:
                self.require(prior["effect_type"] == "repo_publication_pr" and request.pr_number == inputs.pr_number, "pr_number_binding_conflict")
            window = ReadWindow()
            snapshot = await authority.validate()
            async def check():
                await authority.validate()
                latest = await self.owned(current["job_id"], authority.principal, authority.root)
                self.require(latest["revision"] == current["revision"] and latest.get("github_capacity_closure") == current["github_capacity_closure"], "publication_closed_observation_job_changed")
                window.remaining()
            async def get(path):
                response = await self.adapter.request_repo_publication(path, method="GET", token=snapshot.value,
                    authority_check=check, consent_binding=preview["github_consent"],
                    owner_principal_id=authority.principal, owner_session_id=authority.root,
                    readback_authority=authority, read_window=window, timeout_seconds=window.remaining())
                self.require(response.status_code == 200, "publication_closed_positive_get_required")
                return json.loads(response.content)
            boundary = await collect_pr_boundary(inputs, self, get, authority, window) if prior["effect_type"] == "repo_publication_pr" else None
            _, _, path = inputs.expected(prior)
            payload = await get(path)
            identity = effect_identity(current, prior)
            actual = await self.adapter.verified_get_receipt(read_authority=authority,
                path=path, payload=payload, effect_identity=identity, publication_inputs=inputs,
                read_window=window, publication_boundary=boundary)
            observed = await record_closed_observation(jobs, current, authority, actual, identity)
            result = await self.view(observed)
            result.update(observation_only=True, reason_code="publication_capacity_closed_observation_only")
            return result


async def invoke(request, method, *args):
    operator = _operator(request)
    try:
        return await getattr(RepoPublicationService(), method)(*args, _principal_id(operator), _session_id(operator))
    except PublicationError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc


@router.post("/prepare")
async def prepare_publication(body: PrepareRequest, request: Request):
    return await invoke(request, "prepare", body)


@router.post("/jobs/{job_id}/close-capacity")
async def close_publication_capacity(job_id: str, body: PublicationCloseRequest, request: Request):
    from src.workflows.job_runtime import DurableJobError
    from src.extensions.github_followthrough import GitHubFollowthroughError
    try:
        operator = _operator(request)
        return await RepoPublicationService().close_capacity(job_id, _principal_id(operator), _session_id(operator), body)
    except PublicationError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc
    except (DurableJobError, GitHubFollowthroughError, ValueError, OSError) as exc:
        code = getattr(exc, "code", "publication_capacity_close_unproved")
        raise HTTPException(status_code=409, detail={"code": code}) from exc


@router.get("/jobs/{job_id}")
async def get_publication(job_id: str, request: Request):
    operator = _operator(request)
    service = RepoPublicationService()
    return await service.view(await service.owned(job_id, _principal_id(operator), _session_id(operator)))


@router.get("/repairs/{repair_job_id}/jobs")
async def discover_publications(repair_job_id: str, request: Request,
                                offset: int = Query(default=0, ge=0, le=2000)):
    operator = _operator(request)
    try:
        return await RepoPublicationService().discover(repair_job_id, _principal_id(operator), _session_id(operator), offset=offset)
    except PublicationError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc


@router.get("/jobs/{job_id}/patch")
async def publication_patch(job_id: str, request: Request):
    operator = _operator(request)
    service = RepoPublicationService()
    current = await service.owned(job_id, _principal_id(operator), _session_id(operator))
    preview = service.preview(current)
    binding = preview["repair_binding"]
    raw = read_file(binding["patch_artifact_id"])
    if hashlib.sha256(raw).hexdigest() != binding["patch_sha256"]:
        raise HTTPException(status_code=409, detail={"code": "patch_digest_changed"})
    return {"patch": raw.decode("utf-8"), "patch_sha256": binding["patch_sha256"]}


@router.post("/jobs/{job_id}/execute")
async def execute_publication(job_id: str, request: Request):
    return await invoke(request, "execute", job_id)


@router.post("/jobs/{job_id}/cancel")
async def cancel_publication(job_id: str, request: Request):
    return await invoke(request, "cancel", job_id)


@router.post("/jobs/{job_id}/reconcile")
async def reconcile_publication(job_id: str, body: ReconcileRequest, request: Request):
    operator = _operator(request)
    try:
        return await RepoPublicationService().reconcile(job_id, _principal_id(operator), _session_id(operator), body)
    except PublicationError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=409, detail={"code": "publication_reconcile_proof_unavailable"}) from exc
