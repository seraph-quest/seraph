"""Original Source recovery ownership; never an execution or replay grant.

Only originally registered v3 producers can enter the completion protocol.
Public request fields select an action and an optimistic revision, not proof.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import weakref
from types import MappingProxyType

from src.work_board.contracts import WorkBoardOwner
from src.workflows.job_runtime import DurableJobLeaseError


_ACTIONS = frozenset({"reconcile_original_cleanup", "settle_original_host_boot_cleanup"})
_FENCES = weakref.WeakKeyDictionary()
_FENCE_SEAL = object()
_COMPLETIONS = weakref.WeakKeyDictionary()
_REGISTRATION_KEYS = frozenset({"schema", "job_id", "iteration_id", "iteration_index",
    "repository_attempt_id", "owner_principal_id", "owner_session_id", "root_fence",
    "root_authority_digest", "original_source_digest", "native_binding", "execution_digest",
    "prepared_digest", "proposal_digest", "approval_digest", "proposal_predecessor",
    "process_binding", "ready", "ready_digest", "directory_path", "admission_digest",
    "guard_path", "native_host_binding", "original_deadline_at", "execution_deadline_at",
    "monotonic_deadline", "producer_sources", "stage_identity", "fixed_admission_plan_digest",
    "source_artifact_digest", "executor_posture_digest"})


def read_registered_repository_producer(run, *, iteration_index):
    """Closed original SQL metadata only; this never grants physical authority.

    Startup may preserve the exact original lineage using this grammar. A
    completion owner must additionally stage actual registered storage/closure.
    """
    from datetime import datetime
    source = _source()
    original, work, _, _, binding, task_source = source.read_repository_original(run)
    if (source.read_repository_inventory(run)["schema"] != "repository.checkpoint_inventory.v3"
            or type(iteration_index) is not int or not 1 <= iteration_index <= work.limits.max_iterations):
        raise RepositorySourceRecoveryError("original_producer_registration_missing")
    identity = source.iteration_identity(run.run_identity, original["repository_attempt_id"],
        source._source_digest(original["original_input"]), iteration_index)
    record = source._repository_record(run, "repository:producer:" + identity)
    execution = source._repository_record(run, "repository:execution:" + identity)
    prepared = source._repository_record(run, "repository:prepared:" + identity)
    if (type(record) is not dict or set(record) != _REGISTRATION_KEYS
            or len(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()) > 16 * 1024
            or execution is None or prepared is None):
        raise RepositorySourceRecoveryError("original_producer_registration_changed")
    expected = {"schema": "repository.original_producer.v1", "job_id": run.run_identity,
        "iteration_id": identity, "iteration_index": iteration_index,
        "repository_attempt_id": original["repository_attempt_id"],
        "owner_principal_id": run.owner_principal_id, "owner_session_id": run.operator_session_id,
        "root_fence": run.fencing_token, "root_authority_digest": run.authority_digest,
        "original_source_digest": source._source_digest(original),
        "native_binding": binding.model_dump(mode="json"),
        "execution_digest": source._source_digest(execution),
        "prepared_digest": source._source_digest(prepared),
        "process_binding": execution["process_binding"],
        "original_deadline_at": original["original_deadline_at"],
        "source_artifact_digest": task_source.source_artifact_digest,
        "executor_posture_digest": prepared["executor_posture_digest"]}
    if any(record[key] != value or type(record[key]) is not type(value) for key, value in expected.items()):
        raise RepositorySourceRecoveryError("original_producer_registration_changed")
    digests = ("root_authority_digest", "original_source_digest", "execution_digest", "prepared_digest",
        "proposal_digest", "approval_digest", "ready_digest", "admission_digest",
        "fixed_admission_plan_digest", "source_artifact_digest", "executor_posture_digest")
    if any(type(record[key]) is not str or not source._SHA.fullmatch(record[key]) for key in digests):
        raise RepositorySourceRecoveryError("original_producer_registration_changed")
    ready = record["ready"]
    host = record["native_host_binding"]
    ready_keys = {"admission_digest", "public_key", "nonce", "pid", "start_identity", "boot_id",
        "directory_identity", "guard_identity"}
    host_keys = {"schema", "machine_digest", "boot_id", "pid_namespace", "workspace_path",
        "workspace_identity", "guard_path", "guard_identity", "mount_namespace", "proc_identity", "proc_mount_sha256"}
    def identity_pair(value):
        return type(value) is list and len(value) == 2 and all(type(item) is int and item >= 0 for item in value)
    if (type(ready) is not dict or set(ready) != ready_keys
            or type(host) is not dict or set(host) != host_keys
            or host["schema"] != "repository.original_producer_host.v1"
            or type(ready["pid"]) is not int or ready["pid"] <= 0
            or type(ready["start_identity"]) is not str or not ready["start_identity"].isdigit()
            or type(ready["boot_id"]) is not str or not ready["boot_id"] or len(ready["boot_id"]) > 128
            or type(ready["nonce"]) is not str or not source._SHA.fullmatch(ready["nonce"])
            or ready["admission_digest"] != record["admission_digest"]
            or source._source_digest(ready) != record["ready_digest"]
            or host["boot_id"] != ready["boot_id"] or host["guard_path"] != record["guard_path"]
            or host["guard_identity"] != ready["guard_identity"]
            or any(not identity_pair(value) for value in (ready["directory_identity"], ready["guard_identity"],
                record["stage_identity"], host["workspace_identity"], host["pid_namespace"], host["mount_namespace"], host["proc_identity"]))
            or any(type(host[key]) is not str or not source._SHA.fullmatch(host[key]) for key in ("machine_digest", "proc_mount_sha256"))
            or any(type(value) is not str or not value.startswith("/") or "\x00" in value
                for value in (record["directory_path"], record["guard_path"], host["workspace_path"]))
            or type(record["producer_sources"]) is not dict
            or set(record["producer_sources"]) != {"repo_original_producer.py", "repo_original_producer_finalizer.py",
                "repo_supervisor.py", "repo_sandbox.py", "repo_node.py", "repo_worker.py"}
            or any(type(value) is not str or not source._SHA.fullmatch(value) for value in record["producer_sources"].values())
            or type(record["monotonic_deadline"]) not in {int, float}
            or not math.isfinite(record["monotonic_deadline"]) or record["monotonic_deadline"] <= 0):
        raise RepositorySourceRecoveryError("original_producer_registration_changed")
    predecessor = record["proposal_predecessor"]
    if (type(predecessor) is not dict or set(predecessor) != {"status", "revision", "last_receipt_id"}
            or predecessor["status"] != "execution_started" or type(predecessor["revision"]) is not int
            or predecessor["revision"] < 0 or predecessor["last_receipt_id"] is not None
                and (type(predecessor["last_receipt_id"]) is not str or len(predecessor["last_receipt_id"]) > 4096)):
        raise RepositorySourceRecoveryError("original_producer_registration_changed")
    try:
        key = base64.b64decode(ready["public_key"], validate=True)
        cutoff = datetime.fromisoformat(record["execution_deadline_at"])
        original_cutoff = datetime.fromisoformat(record["original_deadline_at"])
        if len(key) != 32 or cutoff.tzinfo is None or original_cutoff.tzinfo is None or cutoff > original_cutoff:
            raise ValueError("invalid original registration")
    except (ValueError, TypeError, AttributeError) as exc:
        raise RepositorySourceRecoveryError("original_producer_registration_changed") from exc
    return json.loads(json.dumps(record))


def _source():
    from src.workflows import repo_repair_source
    return repo_repair_source


class RepositorySourceRecoveryError(DurableJobLeaseError):
    """A fixed public reason; private receipt contents never become messages."""

    def __init__(self, code, *, status_code=409):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class _RepositoryRecoveryFence:
    service: object = field(repr=False)
    jobs: object = field(repr=False)
    job_id: str
    owner: WorkBoardOwner = field(repr=False)
    task: object = field(repr=False)
    lock: object = field(repr=False)
    seal: object = field(repr=False)


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class _OriginalRepositoryProducerCompletionWitness:
    """Registered only after current Source and actual original bundle checks."""


def assert_repository_completion_witness(witness, *, service=None, jobs=None):
    data = _COMPLETIONS.get(witness) if type(witness) is _OriginalRepositoryProducerCompletionWitness else None
    if (data is None or service is not None and data["service"] is not service
            or jobs is not None and data["jobs"] is not jobs):
        raise RepositorySourceRecoveryError("original_repository_completion_witness_required")
    result = data["result"]
    if (_source()._source_digest(result["original_producer_completion"]) != data["completion_digest"]
            or result["status"] != data["result_status"]
            or result["manifest"] != result["original_producer_completion"]["manifest"]
            or result["readback"] != result["manifest"]
            or {name: hashlib.sha256(raw).hexdigest() for name, raw in result["outputs"].items()}
                != data["output_digests"]):
        raise RepositorySourceRecoveryError("original_repository_completion_witness_changed")


def repository_completion_context(witness):
    """Actual staged owner context; a copied public mapping cannot call this."""
    assert_repository_completion_witness(witness)
    return _COMPLETIONS[witness]["context"]


def repository_completion_result(witness):
    """Literal verified original outputs, used only by existing final owners."""
    assert_repository_completion_witness(witness)
    return _COMPLETIONS[witness]["result"]


def repository_completion_post_cas(witness):
    assert_repository_completion_witness(witness)
    data = _COMPLETIONS[witness]
    if data.get("post_cas") is None:
        raise RepositorySourceRecoveryError("original_repository_completion_not_committed")
    return MappingProxyType(dict(data["post_cas"]))


def repository_completion_outcome(witness):
    assert_repository_completion_witness(witness)
    data = _COMPLETIONS[witness]
    return {"job_id": data["context"]["run"].run_identity,
        "iteration_id": data["post_cas"]["iteration_id"], "status": data["status"],
        "cleanup_proven": True, "manifest_artifact_ref": data["readback"]["artifact_ref"],
        "manifest_artifact_digest": data["readback"]["artifact_digest"], "no_learning": True}


def repository_completion_committed_rows(witness):
    assert_repository_completion_witness(witness)
    return _COMPLETIONS[witness]["committed_rows"]


@asynccontextmanager
async def _repository_recovery_fence(service, jobs, *, job_id, owner):
    """The owner holds one non-reentrant fence through staging and commit."""
    from src.model_fabric.effective_policy import configuration_mutation_lock
    from src.workflows.repo_repair import RepoRepairService
    if (type(service) is not RepoRepairService or service.jobs is not jobs
            or type(owner) is not WorkBoardOwner or not job_id):
        raise RepositorySourceRecoveryError("repository_source_recovery_unavailable")
    async with configuration_mutation_lock:
        fence = _RepositoryRecoveryFence(service, jobs, job_id, owner,
            asyncio.current_task(), configuration_mutation_lock, _FENCE_SEAL)
        _FENCES[fence] = True
        try:
            yield fence
        finally:
            _FENCES.pop(fence, None)


def assert_repository_recovery_fence(fence, *, service, jobs, job_id, owner):
    """Registry and exact owner/task identity, rather than lock-state alone."""
    from src.model_fabric.effective_policy import configuration_mutation_lock
    if (type(fence) is not _RepositoryRecoveryFence or fence not in _FENCES
            or fence.seal is not _FENCE_SEAL or fence.service is not service or fence.jobs is not jobs
            or fence.job_id != job_id or fence.owner != owner or fence.task is not asyncio.current_task()
            or fence.lock is not configuration_mutation_lock or not fence.lock.locked()):
        raise RepositorySourceRecoveryError("repository_source_recovery_fence_unavailable")


async def _load_recovery_original(service, jobs, *, job_id, owner, expected_job_revision, fence):
    """Current canonical metadata precedes any completion-file read."""
    from src.db.models import Goal, GoalStatus
    from src.workflows import repo_repair_source as source
    assert_repository_recovery_fence(fence, service=service, jobs=jobs, job_id=job_id, owner=owner)
    source._assert_task_publication_configuration(service)
    async with jobs._session() as db:
        run = await jobs._fetch(db, job_id)
        if (run.owner_principal_id, run.operator_session_id) != (owner.principal_id, owner.session_id):
            raise RepositorySourceRecoveryError("repository_source_recovery_owner_changed")
        if run.revision != expected_job_revision:
            raise RepositorySourceRecoveryError("repository_source_recovery_stale")
        original, work, compiled, group, binding, task_source = source.read_repository_original(run)
        inventory = source.read_repository_inventory(run)
        if inventory["schema"] != "repository.checkpoint_inventory.v3":
            raise RepositorySourceRecoveryError("original_producer_registration_missing")
        goal = await db.get(Goal, group.goal_id)
        source._assert_repository_original_limits(run, goal, source._repository_policy_limits())
        if goal is None or goal.status != GoalStatus.active:
            raise RepositorySourceRecoveryError("repository_source_recovery_goal_changed")
        reservation = jobs._repo_repair_reservation_state(run)
        if (reservation is None or reservation["status"] != "held"
                or not jobs._repo_repair_reservation_matches(reservation, job_id=job_id,
                    attempt_id=original["repository_attempt_id"], fence=run.fencing_token,
                    authority_digest=run.authority_digest)
                or reservation["execution_deadline_at"] != original["original_deadline_at"]):
            raise RepositorySourceRecoveryError("repository_source_recovery_hold_changed")
        return {"run": run, "original": original, "work": work, "compiled": compiled,
            "group": group, "binding": binding, "task_source": task_source, "inventory": inventory}


async def issue_repository_source_producer(service, jobs, job, *, owner):
    """Build callbacks for one actual Source-owned, single-use execution.

    The executor's private ready registry proves its actual Popen/channel
    observation. This owner supplies canonical commit-before-ACK and current
    authorization; a ready dataclass or signature is never sufficient.
    """
    from src.execution.repo_original_producer import (
        issue_original_producer_owner, assert_original_producer_ready,
        original_producer_observation, OriginalProducerRegistrationAck, OriginalProducerCommand,
        assert_original_producer_command, original_producer_sources, canonical, digest,
    )
    from src.db.models import RepoRepairProposal, ApprovalRequest
    from src.work_board.repository import _begin_sqlite_immediate
    from src.workflows.job_runtime import _as_utc, _utc_now
    from datetime import datetime
    source = _source()
    source.assert_repo_iteration_process_binding(job.iteration_binding, job)
    loop = asyncio.get_running_loop()
    actual_owner = None
    registration_digest = None
    next_ordinal = 1

    async def register(ready):
        nonlocal registration_digest
        assert_original_producer_ready(actual_owner, ready)
        observation = original_producer_observation(actual_owner, ready)
        admission = json.loads(observation.admission_json)
        if hashlib.sha256(observation.admission_json.encode()).hexdigest() != ready.admission_digest:
            raise RepositorySourceRecoveryError("original_producer_admission_changed")
        async with _repository_recovery_fence(service, jobs, job_id=job.job_id, owner=owner):
            context = await source._repository_precontact(service, jobs, job_id=job.job_id, owner=owner)
            run = context["run"]
            if source.read_repository_inventory(run)["schema"] != "repository.checkpoint_inventory.v3":
                raise RepositorySourceRecoveryError("original_producer_registration_missing")
            identity = job.iteration_binding.iteration_id
            execution = source._repository_record(run, "repository:execution:" + identity)
            prepared = source._repository_record(run, "repository:prepared:" + identity)
            if (execution is None or prepared is None
                    or execution.get("process_binding") != job.iteration_binding.projection()
                    or execution.get("patch_sha256") != hashlib.sha256(job.patch_bytes).hexdigest()
                    or prepared.get("iteration_index") != job.iteration_binding.iteration_index):
                raise RepositorySourceRecoveryError("original_producer_execution_changed")
            async with jobs._session() as db:
                proposal = await db.get(RepoRepairProposal, execution["proposal_id"])
                approval = await db.get(ApprovalRequest, execution["approval_id"])
                if (proposal is None or approval is None or proposal.status != "execution_started"
                        or approval.status != "consumed" or proposal.workflow_run_id != job.job_id
                        or proposal.authority_digest != job.authority_digest
                        or proposal.patch_sha256 != execution["patch_sha256"]
                        or approval.fingerprint != execution["approval_fingerprint"]):
                    raise RepositorySourceRecoveryError("original_producer_approval_changed")
                proposal_json = proposal.model_dump(mode="json")
                approval_json = approval.model_dump(mode="json")
            durable = admission["original_producer"]
            host_binding = json.loads(observation.host_binding_json)
            if (durable["directory"] != observation.directory_path
                    or durable["nonce"] != ready.nonce or durable["patch_sha256"] != execution["patch_sha256"]
                    or durable["job"]["job_id"] != job.job_id
                    or durable["job"]["attempt_id"] != job.attempt_id
                    or durable["job"]["fencing_token"] != job.fencing_token
                    or durable["job"]["authority_digest"] != job.authority_digest
                    or durable["job"]["base_digest"] != job.base_digest
                    or durable["job"]["execution_deadline_at"] != job.execution_deadline_at
                    or durable["sources"] != original_producer_sources()
                    or type(admission["deadline_at"]) not in {int, float}
                    or not math.isfinite(admission["deadline_at"])
                    or host_binding.get("schema") != "repository.original_producer_host.v1"
                    or host_binding.get("boot_id") != ready.boot_id
                    or host_binding.get("guard_path") != observation.guard_path
                    or host_binding.get("guard_identity") != list(ready.guard_identity)
                    or _utc_now() >= _as_utc(datetime.fromisoformat(job.execution_deadline_at))):
                raise RepositorySourceRecoveryError("original_producer_admission_changed")
            payload = {"schema": "repository.original_producer.v1", "job_id": job.job_id,
                "iteration_id": identity, "iteration_index": job.iteration_binding.iteration_index,
                "repository_attempt_id": context["original"]["repository_attempt_id"],
                "owner_principal_id": owner.principal_id, "owner_session_id": owner.session_id,
                "root_fence": run.fencing_token, "root_authority_digest": run.authority_digest,
                "original_source_digest": source._source_digest(context["original"]),
                "native_binding": context["binding"].model_dump(mode="json"),
                "execution_digest": source._source_digest(execution),
                "prepared_digest": source._source_digest(prepared),
                "proposal_digest": source._source_digest(proposal_json),
                "approval_digest": source._source_digest(approval_json),
                "proposal_predecessor": {key: proposal_json[key] for key in (
                    "status", "revision", "last_receipt_id")},
                "process_binding": job.iteration_binding.projection(),
                "ready": ready.projection(), "ready_digest": digest(canonical(ready.projection())),
                "directory_path": observation.directory_path, "admission_digest": ready.admission_digest,
                "guard_path": observation.guard_path, "native_host_binding": host_binding,
                "original_deadline_at": context["original"]["original_deadline_at"],
                "execution_deadline_at": job.execution_deadline_at,
                "monotonic_deadline": admission["deadline_at"],
                "producer_sources": durable["sources"], "stage_identity": durable["stage_identity"],
                "fixed_admission_plan_digest": source._source_digest({key: admission[key]
                    for key in admission if key != "environment"}),
                "source_artifact_digest": context["source"].source_artifact_digest,
                "executor_posture_digest": prepared["executor_posture_digest"]}
            async with jobs._session() as db:
                await _begin_sqlite_immediate(db)
                await source._recheck_repository_sql(db, context)
                if _utc_now() >= _as_utc(datetime.fromisoformat(job.execution_deadline_at)):
                    raise RepositorySourceRecoveryError("original_producer_cutoff_expired")
                current = await jobs._fetch(db, job.job_id)
                live_proposal = await db.get(RepoRepairProposal, execution["proposal_id"], populate_existing=True)
                live_approval = await db.get(ApprovalRequest, execution["approval_id"], populate_existing=True)
                if (live_proposal is None or live_approval is None
                        or live_proposal.model_dump(mode="json") != proposal_json
                        or live_approval.model_dump(mode="json") != approval_json):
                    raise RepositorySourceRecoveryError("original_producer_approval_changed")
                if source._repository_record(current, "repository:producer:" + identity) is not None:
                    raise RepositorySourceRecoveryError("original_producer_registration_single_use")
                source._append_repository_record(current, "repository:producer:" + identity, payload,
                    inventory=source.repository_checkpoint_inventory(current, context["work"]))
                read_registered_repository_producer(current, iteration_index=job.iteration_binding.iteration_index)
                current.revision += 1
                await db.commit()
            registration_digest = source._source_digest(payload)
            # ACK follows canonical readback, not merely a successful commit.
            fresh = await source._repository_precontact(service, jobs, job_id=job.job_id, owner=owner)
            if (fresh["run"].revision != run.revision + 1
                    or read_registered_repository_producer(fresh["run"],
                        iteration_index=job.iteration_binding.iteration_index) != payload):
                raise RepositorySourceRecoveryError("original_producer_registration_readback_changed")
            assert_original_producer_ready(actual_owner, ready)
            return OriginalProducerRegistrationAck(registration_digest, payload["ready_digest"])

    async def authorize(command):
        nonlocal next_ordinal
        assert_original_producer_command(actual_owner, command)
        if (type(command) is not OriginalProducerCommand or registration_digest is None
                or command.registration_digest != registration_digest or type(command.ordinal) is not int
                or command.ordinal != next_ordinal or type(command.argv_digest) is not str
                or not source._SHA.fullmatch(command.argv_digest)):
            raise RepositorySourceRecoveryError("original_producer_command_changed")
        async with _repository_recovery_fence(service, jobs, job_id=job.job_id, owner=owner):
            context = await source._repository_precontact(service, jobs, job_id=job.job_id, owner=owner)
            registered = source._repository_record(context["run"], "repository:producer:" + job.iteration_binding.iteration_id)
            if (registered is None or source._source_digest(registered) != registration_digest
                    or registered["producer_sources"] != original_producer_sources()):
                raise RepositorySourceRecoveryError("original_producer_registration_changed")
            async with jobs._session() as db:
                await _begin_sqlite_immediate(db)
                await source._recheck_repository_sql(db, context)
                if _utc_now() >= _as_utc(datetime.fromisoformat(job.execution_deadline_at)):
                    raise RepositorySourceRecoveryError("original_producer_cutoff_expired")
                await db.commit()
        next_ordinal += 1
        return True

    def register_ready(ready):
        return asyncio.run_coroutine_threadsafe(register(ready), loop).result(timeout=10)

    def authorize_command(command):
        return asyncio.run_coroutine_threadsafe(authorize(command), loop).result(timeout=10)

    actual_owner = await issue_original_producer_owner(service, jobs, job,
        register_ready=register_ready, authorize_command=authorize_command)
    return actual_owner


async def recover_original_repository_cleanup(service, jobs, *, job_id, owner,
                                               expected_job_revision, action):
    """Two closed actions; incomplete owner dependencies remain fail-closed."""
    if (type(expected_job_revision) is not int or expected_job_revision < 0
            or type(action) is not str or action not in _ACTIONS):
        raise RepositorySourceRecoveryError("repository_source_recovery_request_invalid")
    async with _repository_recovery_fence(service, jobs, job_id=job_id, owner=owner) as fence:
        await _load_recovery_original(service, jobs, job_id=job_id, owner=owner,
            expected_job_revision=expected_job_revision, fence=fence)
        # Registration, completion and physical-release owners must all be
        # integrated before this selected mode can authorize either action.
        # No legacy fallback, injected callback or public DTO supplies them.
        raise RepositorySourceRecoveryError("repository_source_recovery_unavailable", status_code=503)


async def publish_original_repository_completion(service, jobs, *, job_id, owner, iteration_index,
        producer_owner=None, actual_result=None, expected_job_revision=None):
    """One Source-owned publication for live and authentic original restart.

    Physical storage/guard staging precedes the IMMEDIATE writer. Neither a
    caller result nor canonical metadata can issue the private witness alone.
    """
    from datetime import datetime
    from sqlalchemy import select
    from src.db.models import (RepoRepairProposal, ApprovalRequest, InferenceCostReservation,
        OperatorSession, WorkBoardInputArtifact)
    from src.workflows.job_runtime import _canonical, _as_utc, _utc_now
    from src.work_board.repository import _begin_sqlite_immediate
    from src.workflows import repo_repair_stop as stop_owner
    from src.execution.repo_original_producer import (
        stage_original_producer_completion, original_producer_completion_result,
    )
    source = _source()
    async def accounting_rows_for_original(db, context):
        from src.workflows.inference_group_lookup import group_reservation_rows
        group = context["group"]
        rows = await group_reservation_rows(db, owner_id=group.owner_principal_id,
            group_id=group.group_id, group_digest=source._source_digest(group.model_dump(mode="json")),
            original_root_id=group.owner_session_id, original_deadline_at=group.original_deadline_at, group=group)
        root_rows = list((await db.scalars(select(InferenceCostReservation).where(
            InferenceCostReservation.job_id == job_id).limit(13))).all())
        if len(root_rows) > 12 or any(row.operation_id not in {item.operation_id for item in rows} for row in root_rows):
            raise RepositorySourceRecoveryError("original_producer_accounting_changed")
        return rows
    if (producer_owner is None) != (actual_result is None):
        raise RepositorySourceRecoveryError("original_repository_completion_owner_required")
    async with _repository_recovery_fence(service, jobs, job_id=job_id, owner=owner) as fence:
        source._assert_task_publication_configuration(service)
        async with jobs._session() as db:
            run = await jobs._fetch(db, job_id)
            if expected_job_revision is not None and (type(expected_job_revision) is not int
                    or expected_job_revision < 0 or run.revision != expected_job_revision):
                raise RepositorySourceRecoveryError("repository_source_recovery_stale")
            if (run.owner_principal_id, run.operator_session_id) != (owner.principal_id, owner.session_id):
                raise RepositorySourceRecoveryError("repository_source_recovery_owner_changed")
            registration = read_registered_repository_producer(run, iteration_index=iteration_index)
            existing_stop = source._repository_record(run, stop_owner.STOP_ID)
            expired = _utc_now() >= _as_utc(datetime.fromisoformat(registration["original_deadline_at"]))
        if existing_stop is not None or expired:
            reason = existing_stop["stop_reason"] if existing_stop else "deadline_exhausted"
            staged_stop = await stop_owner._context(service, jobs, job_id=job_id, owner=owner,
                limit_reason=reason if reason in stop_owner.AUTOMATIC_REASONS else None)
            staged_stop = await stop_owner._persist_repository_stop_intent_locked(service, jobs,
                context=staged_stop, owner=owner, reason=reason, fence=fence)
            context = staged_stop.data
        elif run.status == "unknown_external_effect":
            context = (await stop_owner._context(service, jobs, job_id=job_id, owner=owner)).data
        else:
            context = await source._repository_precontact(service, jobs, job_id=job_id, owner=owner)
        run = context["run"]
        registration = read_registered_repository_producer(run, iteration_index=iteration_index)
        identity = registration["iteration_id"]
        execution = source._repository_record(run, "repository:execution:" + identity)
        async with jobs._session() as db:
            proposal = await db.get(RepoRepairProposal, execution["proposal_id"])
            approval = await db.get(ApprovalRequest, execution["approval_id"])
            if proposal is None or approval is None:
                raise RepositorySourceRecoveryError("original_producer_approval_changed")
            proposal_json, approval_json = proposal.model_dump(mode="json"), approval.model_dump(mode="json")
            if (source._source_digest(proposal_json) != registration["proposal_digest"]
                    or source._source_digest(approval_json) != registration["approval_digest"]):
                raise RepositorySourceRecoveryError("original_producer_approval_changed")
            accounting = await accounting_rows_for_original(db, context)
            accounting_rows = {row.operation_id: _canonical(row.model_dump(mode="json")) for row in accounting}
        # This owner context remains active across literal artifact staging and
        # the exact writer. Restart guard freedom is not an unheld observation.
        with stage_original_producer_completion(registration, owner=producer_owner, result=actual_result) as physical:
            result = original_producer_completion_result(physical)
            manifest, outputs = result["manifest"], result["outputs"]
            body = result["original_producer_completion"]
            if (manifest.get("iteration_binding") != registration["process_binding"]
                    or manifest.get("stage_removed") is not True
                    or manifest.get("supervisor_transport", {}).get("transport_kind") != "original_producer_durable_v1"
                    or any(manifest.get("supervisor_transport", {}).get(key) is not True for key in (
                        "command_output_drained", "command_descriptors_closed", "original_children_waited", "no_spawn"))
                    or manifest.get("process_cleanup", {}).get("cleanup_proven") is not True
                    or manifest.get("process_cleanup", {}).get("oracle") != "linux_subreaper_waitpid_echild"):
                raise RepositorySourceRecoveryError("original_producer_cleanup_changed")
            complete = body["outcome"] in {"completed_requested_checks", "completed_requested_check_failure"}
            status = result["status"] if complete else "held_partial"
            if complete and status not in {"succeeded", "failed"}:
                raise RepositorySourceRecoveryError("original_producer_result_changed")
            command_results = source._repository_command_results(manifest,
                node=context["work"].language_profile == "test_node") if complete else []
            before = run.revision
            stop = source._repository_record(run, stop_owner.STOP_ID)
            unknown = source._repository_unknown_root_projection(run) if source._repository_record(
                run, "repository:stop-uncertainty-successor:v1") is not None else None
            cas = {"before_revision": before, "post_revision": before + 1, "iteration_id": identity,
                "producer_registration_digest": source._source_digest(registration),
                "producer_completion_digest": source._source_digest(body),
                "stop_digest": source._source_digest(stop) if stop else None,
                "unknown_projection_digest": source._source_digest({key: getattr(unknown, key)
                    for key in unknown.__dataclass_fields__}) if unknown else None,
                "rows_digest": source._source_digest([expected for _, _, expected in context["rows"]])}
            projection = {"iteration_binding": registration["process_binding"],
                "process_cleanup": manifest["process_cleanup"], "artifact_digests": {
                    name: hashlib.sha256(raw).hexdigest() for name, raw in outputs.items()},
                "source_completion_cas": cas}
            if producer_owner is not None:
                # Preserve the actual original live witness bytes required by
                # the existing final writer, rather than inventing its shape.
                projection = result["iteration_cleanup_witness"].projection()
            prefix = "artifacts/repo-repair/model/iteration-" + identity
            cleanup_ref, cleanup_digest = service._write_private_artifact(prefix + "-cleanup.json", _canonical(projection).encode())
            readback_ref, readback_digest = service._write_private_artifact(prefix + "-readback.json", outputs["readback.json"])
            diagnostics = {"iteration_id": identity, "stdout": outputs["pytest.stdout"].decode("utf-8", errors="replace"),
                "stderr": outputs["pytest.stderr"].decode("utf-8", errors="replace"),
                "stdout_raw_sha256": hashlib.sha256(outputs["pytest.stdout"]).hexdigest(),
                "stderr_raw_sha256": hashlib.sha256(outputs["pytest.stderr"]).hexdigest(),
                "cumulative_diff": outputs["diff.patch"].decode("utf-8", errors="strict"),
                "cumulative_diff_sha256": hashlib.sha256(outputs["diff.patch"]).hexdigest()}
            diagnostics_raw = _canonical(diagnostics).encode()
            diagnostics_ref, diagnostics_digest = service._write_private_artifact(prefix + "-diagnostics.json", diagnostics_raw)
            for ref, digest, raw in ((cleanup_ref, cleanup_digest, _canonical(projection).encode()),
                    (readback_ref, readback_digest, outputs["readback.json"]),
                    (diagnostics_ref, diagnostics_digest, diagnostics_raw)):
                if service._read_private_artifact(ref, expected_digest=digest) != raw:
                    raise RepositorySourceRecoveryError("original_producer_artifact_changed")
            assert_repository_recovery_fence(fence, service=service, jobs=jobs, job_id=job_id, owner=owner)
            async with jobs._session() as db:
                await _begin_sqlite_immediate(db)
                for model, key, expected in context["rows"]:
                    current_row = await db.get(model, key, populate_existing=True)
                    if current_row is None or _canonical(current_row.model_dump(mode="json")) != expected:
                        raise RepositorySourceRecoveryError("original_producer_completion_epoch_changed")
                    if isinstance(current_row, OperatorSession) and (current_row.revoked_at is not None
                            or _as_utc(current_row.idle_expires_at) <= _utc_now()
                            or _as_utc(current_row.absolute_expires_at) <= _utc_now()):
                        raise RepositorySourceRecoveryError("repository_source_recovery_owner_changed")
                    if isinstance(current_row, WorkBoardInputArtifact) and _as_utc(current_row.expires_at) <= _utc_now():
                        raise RepositorySourceRecoveryError("repository_source_recovery_source_expired")
                current = await jobs._fetch(db, job_id)
                current_unknown = source._repository_unknown_root_projection(current) if source._repository_record(
                    current, "repository:stop-uncertainty-successor:v1") is not None else None
                if (current.revision != before or read_registered_repository_producer(current,
                        iteration_index=iteration_index) != registration or current_unknown != unknown):
                    raise RepositorySourceRecoveryError("original_producer_completion_epoch_changed")
                current_accounting = await accounting_rows_for_original(db, context)
                if {row.operation_id: _canonical(row.model_dump(mode="json")) for row in current_accounting} != accounting_rows:
                    raise RepositorySourceRecoveryError("original_producer_accounting_changed")
                live_proposal = await db.get(RepoRepairProposal, execution["proposal_id"], populate_existing=True)
                live_approval = await db.get(ApprovalRequest, execution["approval_id"], populate_existing=True)
                if (live_proposal is None or live_approval is None or live_proposal.model_dump(mode="json") != proposal_json
                        or live_approval.model_dump(mode="json") != approval_json):
                    raise RepositorySourceRecoveryError("original_producer_approval_changed")
                inventory = source.repository_checkpoint_inventory(current, context["work"])
                source._append_repository_record(current, "repository:cleanup:" + identity,
                    {"artifact_ref": cleanup_ref, "artifact_digest": cleanup_digest, "cleanup_proven": True,
                        "iteration_id": identity, "status": status, "source_completion_cas": cas}, inventory=inventory)
                source._append_repository_record(current, "repository:readback:" + identity,
                    {"artifact_ref": readback_ref, "artifact_digest": readback_digest, "status": status,
                        "manifest_digest": source._source_digest(manifest), "patch_sha256": execution["patch_sha256"],
                        "diagnostics_artifact_ref": diagnostics_ref, "diagnostics_artifact_digest": diagnostics_digest,
                        "command_results": command_results, "source_completion_cas": cas}, inventory=inventory)
                current.revision += 1
                live_proposal.status = "execution_verified" if complete else "execution_partial"
                live_proposal.last_receipt_id = "repository:readback:" + identity
                live_proposal.revision += 1
                expected_journal = current.checkpoint_receipts_json
                expected_proposal = live_proposal.model_dump(mode="json")
                await db.commit()
                # Exact committed bytes are captured inside the same fence,
                # while the physical owner's guard is still held.
                committed_rows = []
                for model, key, original_row in context["rows"]:
                    row = await db.get(model, key, populate_existing=True)
                    if row is None:
                        raise RepositorySourceRecoveryError("original_producer_completion_readback_changed")
                    await db.refresh(row)
                    row_json = row.model_dump(mode="json")
                    if getattr(row, "run_identity", None) == job_id:
                        original_json = json.loads(original_row)
                        bookkeeping = {"revision", "checkpoint_receipts_json", "updated_at"}
                        if (row.revision != before + 1 or row.checkpoint_receipts_json != expected_journal
                                or {key: value for key, value in row_json.items() if key not in bookkeeping}
                                    != {key: value for key, value in original_json.items() if key not in bookkeeping}):
                            raise RepositorySourceRecoveryError("original_producer_completion_readback_changed")
                    elif _canonical(row_json) != original_row:
                        raise RepositorySourceRecoveryError("original_producer_completion_readback_changed")
                    committed_rows.append((model, key, _canonical(row.model_dump(mode="json"))))
                await db.refresh(live_proposal)
                if live_proposal.model_dump(mode="json") != expected_proposal:
                    raise RepositorySourceRecoveryError("original_producer_completion_readback_changed")
                committed_rows.append((RepoRepairProposal, execution["proposal_id"],
                    _canonical(live_proposal.model_dump(mode="json"))))
                committed_rows.append((ApprovalRequest, execution["approval_id"], _canonical(approval_json)))
                committed_rows.extend((InferenceCostReservation, key, raw) for key, raw in accounting_rows.items())
            witness = _OriginalRepositoryProducerCompletionWitness()
            _COMPLETIONS[witness] = {"service": service, "jobs": jobs, "context": context,
                "result": result, "post_cas": cas, "status": status,
                "committed_rows": tuple(committed_rows),
                "completion_digest": source._source_digest(body),
                "result_status": result["status"],
                "output_digests": {name: hashlib.sha256(raw).hexdigest() for name, raw in outputs.items()},
                "readback": {"artifact_ref": readback_ref, "artifact_digest": readback_digest}}
            if stop is not None:
                await source.stage_repository_completion_post_context(service, jobs, witness=witness, owner=owner, fence=fence)
            return witness
