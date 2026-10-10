"""Original repository stop ownership; no execution or financial admission."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import weakref
from types import MappingProxyType

from sqlalchemy import select, update, inspect as inspect_mapper

from src.db.models import (WorkflowRunState, WorkBoardTask, WorkBoardAttempt,
    WorkBoardInputArtifact, Goal, OperatorSession, InferenceCostReservation)
from src.work_board.contracts import WorkBoardOwner, RepositoryNativeStopClosureV1
from src.workflows.job_runtime import DurableJobLeaseError, _canonical, _as_utc, _utc_now

STOP_ID = "repository:stop-intent:v1"
AUTOMATIC_REASONS = frozenset({"deadline_exhausted", "cost_exhausted", "shared_group_exhausted", "goal_limit_exhausted"})
_ISSUED = weakref.WeakKeyDictionary()
_STAGED = weakref.WeakKeyDictionary()
_UNCERTAINTY = weakref.WeakKeyDictionary()


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class _RepositoryStopContext:
    data: object = field(repr=False)

    def __getitem__(self, key):
        return self.data[key]


def assert_repository_stop_context(context, *, service, jobs=None):
    if (type(context) is not _RepositoryStopContext or _STAGED.get(context) is not service
            or (jobs is not None and service.jobs is not jobs)):
        raise DurableJobLeaseError("actual Source-staged repository Stop context required")
    stage = context.data.get("_knownpost_stage")
    if stage is not None:
        from src.workflows.repo_repair_source_recovery import assert_repository_knownpost_stage
        assert_repository_knownpost_stage(stage, service=service, jobs=service.jobs)


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class _RepositoryUncertainty:
    pass


async def stage_repository_uncertainty(service, jobs, *, job_id, owner):
    async with jobs._session() as db:
        run = await jobs._fetch(db, job_id)
        stop = _source()._repository_record(run, STOP_ID)
        if stop is None or _source().read_repository_inventory(run)["schema"] == "repository.checkpoint_inventory.v1":
            return None
    context = await _context(service, jobs, job_id=job_id, owner=owner)
    witness = _RepositoryUncertainty()
    _UNCERTAINTY[witness] = (jobs, context, _source()._source_digest(stop))
    return witness


async def append_repository_uncertainty_in_writer(db, jobs, run, *, witness, to_status, reason, result, values):
    """Exact original predecessor CAS; all physical staging already closed."""
    source = _source()
    stop = source._repository_record(run, STOP_ID)
    if witness is None:
        if stop is not None and source.read_repository_inventory(run)["schema"] in {
                "repository.checkpoint_inventory.v2", "repository.checkpoint_inventory.v3"}:
            raise DurableJobLeaseError("original pending uncertainty owner witness required")
        return
    if type(witness) is not _RepositoryUncertainty or witness not in _UNCERTAINTY:
        raise DurableJobLeaseError("actual original uncertainty owner required")
    actual_jobs, context, stop_digest = _UNCERTAINTY.pop(witness)
    reasons = {"repository_callback_closure_unproven": "reconcile_original_callback",
        "repository_process_closure_unproven": "reconcile_original_process"}
    if (actual_jobs is not jobs or context["run"].run_identity != run.run_identity
            or run.status != "running" or to_status != "unknown_external_effect" or reason not in reasons
            or stop is None or source._source_digest(stop) != stop_digest
            or source.read_repository_inventory(run)["schema"] not in {
                "repository.checkpoint_inventory.v2", "repository.checkpoint_inventory.v3"}
            or not isinstance(result, dict) or set(result) != {"no_learning", "operator_action", "iteration_id"}
            or result["no_learning"] is not True or result["operator_action"] != reasons[reason]):
        raise DurableJobLeaseError("original uncertainty projection changed")
    original, work, *_ = source.read_repository_original(run)
    identities = {source.iteration_identity(run.run_identity, original["repository_attempt_id"],
        source._source_digest(original["original_input"]), index) for index in range(1, work.limits.max_iterations + 1)}
    if result["iteration_id"] not in identities:
        raise DurableJobLeaseError("original uncertainty iteration changed")
    for kind, key, expected in context["rows"]:
        row = await db.get(kind, key)
        if row is None or _canonical(row.model_dump(mode="json")) != expected:
            raise DurableJobLeaseError("original uncertainty predecessor changed")
    hold = jobs._repo_repair_reservation_state(run)
    if (hold is None or hold["status"] != "held" or not jobs._repo_repair_reservation_matches(hold,
            job_id=run.run_identity, attempt_id=original["repository_attempt_id"],
            fence=run.fencing_token, authority_digest=run.authority_digest)
            or hold["execution_deadline_at"] != original["original_deadline_at"]):
        raise DurableJobLeaseError("original uncertainty held reservation changed")
    successor = run.model_copy(update={**values, "revision": run.revision + 1})
    predecessor_json, successor_json = run.model_dump(mode="json"), successor.model_dump(mode="json")
    payload = {"schema": "repository.stop_uncertainty_successor.v1", "job_id": run.run_identity,
        "stop_digest": stop_digest, "root_key": type(run).__tablename__ + ":" + str(_key(run)),
        "predecessor_digest": _static(run, context), "successor_digest": _static(successor, context),
        "authority_digest": run.authority_digest, "fencing_token": run.fencing_token,
        "from_revision": run.revision, "to_revision": run.revision + 1,
        "predecessor_projection": {key: predecessor_json[key] for key in source._UNCERTAINTY_COLUMNS},
        "successor_projection": {key: successor_json[key] for key in source._UNCERTAINTY_COLUMNS}}
    source._validate_repository_unknown_root_projection(successor, stop, payload)
    source._append_repository_record(run, "repository:stop-uncertainty-successor:v1", payload,
        inventory=source.repository_checkpoint_inventory(run, work))
    values["checkpoint_receipts_json"] = run.checkpoint_receipts_json


def _source():
    from src.workflows import repo_repair_source
    return repo_repair_source


def _key(row):
    values = tuple(getattr(row, column.key) for column in inspect_mapper(type(row)).primary_key)
    return values[0] if len(values) == 1 else values


def _committed_stop_row_json(row):
    from src.work_board.time import serialize_utc_datetime
    # SQLite reloads UTC columns without tzinfo; retain every field and microsecond.
    values = row.model_dump(mode="json")
    for key, value in row.model_dump(mode="python").items():
        if isinstance(value, datetime):
            values[key] = serialize_utc_datetime(value)
    return _canonical(values)


def _static(row, context):
    """Only original, explicitly known callback/cleanup successor columns."""
    source = _source()
    values = row.model_dump(mode="json")
    mutable = {"updated_at"}
    if isinstance(row, WorkflowRunState):
        if row.run_identity == context["run"].run_identity:
            mutable |= {"revision", "checkpoint_receipts_json", "heartbeat_at"}
        elif row.run_identity == context["binding"].parent_job_id:
            mutable |= {"revision", "checkpoint_receipts_json"}
        elif row.run_identity == context["binding"].invocation_id:
            mutable |= {"revision", "status", "failure_reason", "heartbeat_at"}
    if isinstance(row, OperatorSession):
        mutable |= {"last_seen_at", "idle_expires_at"}
    return source._source_digest({key: value for key, value in values.items() if key not in mutable})


async def _context(service, jobs, *, job_id, owner, limit_reason=None, completion_witness=None,
        _knownpost_stage=None):
    """Cleanup-only metadata first, then current original physical source."""
    from src.workflows.repo_repair import RepoRepairService
    from src.workflows.general_task_guard import _cancel_original, child_binding
    from src.workflows.job_runtime import _binding, _assert_canonical_goal_fence
    source = _source()
    if type(service) is not RepoRepairService or service.jobs is not jobs:
        raise DurableJobLeaseError("actual current repository stop Source and job owner required")
    if _knownpost_stage is not None:
        from src.workflows.repo_repair_source_recovery import (
            assert_repository_knownpost_stage, repository_knownpost_stage)
        assert_repository_knownpost_stage(_knownpost_stage, service=service, jobs=jobs)
        knownpost = repository_knownpost_stage(_knownpost_stage)
        if (completion_witness is not None or knownpost["job_id"] != job_id
                or knownpost["owner"] is not owner):
            raise DurableJobLeaseError("actual separate repository knownpost scope required")
    config = source._assert_task_publication_configuration(service)
    staged_policy = source._repository_policy_limits() if limit_reason is not None else None
    async with jobs._session() as db:
        run = await jobs._fetch(db, job_id)
        original, work, compiled, group, binding, task_source = source.read_repository_original(run)
        if (run.owner_principal_id, run.operator_session_id) != (owner.principal_id, owner.session_id):
            raise DurableJobLeaseError("original repository stop owner changed")
        if run.status not in {"running", "unknown_external_effect"}:
            raise DurableJobLeaseError("original repository stop is not nonterminal")
        permanent = _binding(owner_principal_id=binding.owner_principal_id, goal_id=binding.goal_id,
            goal_revision=binding.goal_revision, idempotency_scope="original-repository-child",
            dedupe_key=binding.invocation_id)
        mapped = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.idempotency_binding == permanent))
        if mapped is None or mapped.run_identity != job_id:
            raise DurableJobLeaseError("permanent original repository stop mapping changed")
        parent, task, attempt, manifest, artifact, goal, children = await _cancel_original(
            jobs, db, binding.parent_job_id, observation=True)
        child = next((item for item in children if item.run_identity == binding.invocation_id), None)
        root = await db.get(OperatorSession, owner.session_id)
        repo_task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == original["repository_task_id"]))
        repo_attempt = await db.get(WorkBoardAttempt, original["repository_attempt_id"])
        repo_artifact = await db.get(WorkBoardInputArtifact, repo_task.input_artifact_id) if repo_task else None
        if (child is None or child_binding(child) != binding
                or json.loads(child.arguments_json).get("tool_id") != "repository_work"
                or child.attempt_count != 1 or child.fencing_token <= 0
                or attempt.cancel_requested_at is not None or attempt.ended_at is not None
                or root is None or root.principal_id != owner.principal_id or root.revoked_at
                or root.replaced_by_id or root.is_bearer_tombstone
                or _as_utc(root.idle_expires_at) <= _utc_now() or _as_utc(root.absolute_expires_at) <= _utc_now()
                or goal is None or goal.revision != binding.goal_revision
                or repo_task is None or repo_attempt is None or repo_artifact is None
                or repo_attempt.task_id != repo_task.task_id or repo_attempt.workflow_run_id != job_id
                or repo_attempt.ended_at is not None or repo_attempt.cancel_requested_at is not None
                or (repo_task.owner_principal_id, repo_task.owner_session_id) != (owner.principal_id, owner.session_id)
                or repo_task.goal_id != binding.goal_id or repo_task.goal_revision != binding.goal_revision
                or run.input_digest != original["repository_input_artifact_digest"]):
            facts = {"child": child is not None and child_binding(child) == binding,
                "tool": child is not None and json.loads(child.arguments_json).get("tool_id") == "repository_work",
                "claim": child is not None and child.attempt_count == 1 and child.fencing_token > 0,
                "repository_artifact": repo_artifact is not None,
                "repository_owner": repo_task is not None and (repo_task.owner_principal_id, repo_task.owner_session_id) == (owner.principal_id, owner.session_id),
                "repository_goal": repo_task is not None and repo_task.goal_id == binding.goal_id and repo_task.goal_revision == binding.goal_revision,
                "repository_attempt_task": repo_attempt is not None and repo_task is not None and repo_attempt.task_id == repo_task.task_id,
                "parent_attempt_open": attempt.cancel_requested_at is None and attempt.ended_at is None,
                "repository_attempt_open": repo_attempt is not None and repo_attempt.ended_at is None and repo_attempt.cancel_requested_at is None,
                "repository_attempt_root": repo_attempt is not None and repo_attempt.workflow_run_id == job_id,
                "input": run.input_digest == original["repository_input_artifact_digest"],
                "goal": goal is not None and goal.revision == binding.goal_revision,
                "session": root is not None and root.principal_id == owner.principal_id and not root.revoked_at
                    and not root.replaced_by_id and not root.is_bearer_tombstone
                    and _as_utc(root.idle_expires_at) > _utc_now() and _as_utc(root.absolute_expires_at) > _utc_now()}
            raise DurableJobLeaseError("original repository stop metadata changed:" + ",".join(key for key, okay in facts.items() if not okay))
        await _assert_canonical_goal_fence(db, goal_id=binding.goal_id, goal_revision=binding.goal_revision,
            owner_kind="user", owner_principal_id=owner.principal_id, session_id=owner.session_id)
        limits = None
        if limit_reason is not None:
            limits = source._assert_repository_original_limits(run, goal, staged_policy)
        inventory = source._repository_record(run, "repository:inventory:v1")
        if inventory is None or inventory.get("identities") != source.repository_checkpoint_inventory(run, work):
            raise DurableJobLeaseError("original stop identity was not reserved before effects")
        context = {"run": run, "original": original, "work": work, "compiled": compiled,
            "group": group, "binding": binding, "task_source": task_source,
            "parent": parent, "task": task, "attempt": attempt, "child": child,
            "repo_task": repo_task, "repo_attempt": repo_attempt, "owner": owner,
            "goal": goal, "original_limits": limits, "manifest": manifest,
            "artifact": artifact}
        rows = [run, parent, child, task, attempt, artifact, goal, root, repo_task, repo_attempt, repo_artifact]
        context["rows"] = [(type(row), _key(row), _canonical(row.model_dump(mode="json"))) for row in rows]
        context["static_rows"] = {type(row).__tablename__ + ":" + str(_key(row)): _static(row, context) for row in rows}
        if limit_reason is not None:
            context["limit_evidence"] = await _limit_evidence(db, context, reason=limit_reason)
        stop = source._repository_record(run, STOP_ID)
        if _knownpost_stage is not None and stop is None:
            raise DurableJobLeaseError("actual repository knownpost Stop required")
        if stop is not None:
            if _knownpost_stage is not None:
                matches = await source._validate_repository_knownpost_context_sql(db,
                    service, jobs, stage=_knownpost_stage, context=context)
            elif completion_witness is None:
                matches = source._stop_static_rows_match(run, stop, context["static_rows"])
            else:
                matches = await source._validate_repository_completion_post_context_sql(db,
                    service, jobs, witness=completion_witness, context=context)
            if not matches:
                raise DurableJobLeaseError("original stop snapshot changed before private read")
        # A contacted callback's already-published static witnesses are checked
        # before its private canonical/source artifacts can be reopened.
        for index in range(1, work.limits.max_iterations + 1):
            identity = source.iteration_identity(job_id, original["repository_attempt_id"],
                source._source_digest(original["original_input"]), index)
            callback = source._repository_record(run, "repository:proposal:" + identity)
            if callback is not None:
                if (callback.get("native_invocation_id") != child.run_identity
                        or callback.get("child_fence") != child.fencing_token
                        or callback.get("parent_static_digest") != source._source_digest({k: v for k, v in parent.model_dump(mode="json").items()
                            if k not in {"revision", "updated_at", "checkpoint_receipts_json"}})
                        or callback.get("child_static_digest") != source._source_digest({k: v for k, v in child.model_dump(mode="json").items()
                            if k not in {"revision", "status", "failure_reason", "updated_at", "heartbeat_at"}})):
                    raise DurableJobLeaseError("original callback stop scope changed before private read")
        expired_cleanup = limit_reason in AUTOMATIC_REASONS and _utc_now() >= group.original_deadline_at
        context["expired_cleanup"] = expired_cleanup
        if not expired_cleanup:
            await _validate_repository_stop_input_sql(db, context)
    if staged_policy is not None:
        source._recheck_repository_policy_limits(staged_policy)
    # The original candidate reader is CLOSED before any physical artifact work.
    context["envelope"] = await _stage_repository_stop_context_artifacts(service, context, stop=stop,
        expired_cleanup=expired_cleanup)
    source._assert_task_publication_configuration(service, staged_config=config)
    if staged_policy is not None:
        source._recheck_repository_policy_limits(staged_policy)
    async with jobs._session() as db:
        await _validate_repository_stop_context_rows_sql(db, service, jobs, context=context,
            completion_witness=completion_witness, _knownpost_stage=_knownpost_stage,
            expired_cleanup=expired_cleanup)
    source._assert_task_publication_configuration(service, staged_config=config)
    if staged_policy is not None:
        source._recheck_repository_policy_limits(staged_policy)
    if _knownpost_stage is not None:
        assert_repository_knownpost_stage(_knownpost_stage, service=service, jobs=jobs)
        context["_knownpost_stage"] = _knownpost_stage
    staged = _RepositoryStopContext(MappingProxyType(context))
    _STAGED[staged] = service
    return staged


async def _validate_repository_stop_input_sql(db, context):
    from src.work_board.input_artifacts import _resolve_input_artifact_metadata_for_task
    task, parent, attempt = (context[key] for key in ("task", "parent", "attempt"))
    from src.workflows.general_task_guard import assert_original_parent_authority
    assert_original_parent_authority(parent)
    if (task.capability_id != "agent.task.v1" or attempt.task_id != task.task_id
            or attempt.workflow_run_id != parent.run_identity or attempt.ended_at is not None
            or attempt.cancel_requested_at is not None or task.owner_principal_id != parent.owner_principal_id
            or task.owner_session_id != parent.session_id or parent.operator_session_id != task.owner_session_id
            or task.goal_id != parent.goal_id or task.goal_revision != parent.goal_revision):
        raise DurableJobLeaseError("original native stop input binding changed")
    artifact = await _resolve_input_artifact_metadata_for_task(db, context["owner"],
        artifact_id=task.input_artifact_id, goal_id=task.goal_id, goal_revision=task.goal_revision,
        capability_id="agent.task.v1", expected_task_id=task.task_id)
    if (artifact.typed_input_ref != task.typed_input_ref
            or artifact.payload_sha256 != task.typed_input_digest
            or _canonical(artifact.model_dump(mode="json")) != _canonical(context["artifact"].model_dump(mode="json"))):
        raise DurableJobLeaseError("original native stop input reference changed")


async def _stage_repository_stop_context_artifacts(service, context, *, stop, expired_cleanup):
    from src.work_board.input_artifacts import (_metadata_digest, _safe_file_bytes,
        _payload_path, _decode_and_validate_payload)
    from src.work_board.general_task_runtime_artifacts import _verify_native_manifest_data
    from src.work_board.contracts import GeneralTaskEnvelope
    from src.work_board.pipelines import root_binding
    source = _source()
    run, original, work, compiled, group, binding, task_source, parent, task, attempt, artifact, manifest = (
        context[key] for key in ("run", "original", "work", "compiled", "group", "binding", "task_source",
            "parent", "task", "attempt", "artifact", "manifest"))
    if stop is not None:
        snapshot = json.loads(service._read_private_artifact(stop["snapshot_artifact_ref"],
            expected_digest=stop["snapshot_artifact_digest"]))
        if snapshot != {"schema": "repository.stop_snapshot.v1", "static_rows": stop["static_rows"],
                "repository_job_id": run.run_identity, "source_checkpoint_digest": source._source_digest(original)}:
            raise DurableJobLeaseError("literal original stop snapshot changed")
    if expired_cleanup and (artifact.state != "bound" or artifact.bound_task_id != task.task_id
            or artifact.capability_id != "agent.task.v1" or artifact.capability_version != "1"
            or artifact.typed_input_ref != task.typed_input_ref or artifact.payload_sha256 != task.typed_input_digest
            or _as_utc(artifact.expires_at) <= _utc_now() or _metadata_digest(artifact) != artifact.metadata_digest):
        raise DurableJobLeaseError("original expired-cleanup artifact metadata changed")
    payload = _safe_file_bytes(_payload_path(artifact),
        expected_digest=artifact.payload_sha256, expected_size=artifact.size_bytes)
    envelope = GeneralTaskEnvelope.model_validate(_decode_and_validate_payload(artifact, payload))
    await _verify_native_manifest_data(parent, task, attempt, manifest, envelope)
    if envelope.repository_source != task_source or envelope.proposal_group != group:
        raise DurableJobLeaseError("original scoped stop Task changed")
    if source._source_digest(root_binding()) != binding.live_root_digest:
        raise DurableJobLeaseError("original physical Root stop binding changed")
    source_facts = json.loads(service._read_private_artifact(task_source.source_artifact_ref,
        expected_digest=task_source.source_artifact_digest))
    if service.recheck_task_source_snapshot(work, source_facts) != compiled:
        raise DurableJobLeaseError("original physical repository stop source changed")
    return envelope


async def _validate_repository_stop_context_rows_sql(db, service, jobs, *, context,
        completion_witness=None, _knownpost_stage=None, expired_cleanup=False):
    from src.workflows.job_runtime import _assert_canonical_goal_fence
    from src.work_board.input_artifacts import _verify_general_proposal, _metadata_digest
    for model, key, expected in context["rows"]:
        actual = await db.get(model, key, populate_existing=True)
        if actual is None or _canonical(actual.model_dump(mode="json")) != expected:
            raise DurableJobLeaseError("original repository stop staging rows changed")
        if isinstance(actual, OperatorSession) and (actual.revoked_at or actual.replaced_by_id
                or actual.is_bearer_tombstone or _as_utc(actual.idle_expires_at) <= _utc_now()
                or _as_utc(actual.absolute_expires_at) <= _utc_now()):
            raise DurableJobLeaseError("original repository stop session expired")
    binding, owner = context["binding"], context["owner"]
    await _assert_canonical_goal_fence(db, goal_id=binding.goal_id, goal_revision=binding.goal_revision,
        owner_kind="user", owner_principal_id=owner.principal_id, session_id=owner.session_id)
    if not expired_cleanup:
        await _validate_repository_stop_input_sql(db, context)
        await _verify_general_proposal(db, context["artifact"], context["envelope"].model_dump(mode="json"))
    elif _metadata_digest(context["artifact"]) != context["artifact"].metadata_digest:
        raise DurableJobLeaseError("original expired-cleanup artifact metadata changed")
    run = context["run"]
    stop = _source()._repository_record(run, STOP_ID)
    if stop is not None:
        if _knownpost_stage is not None:
            matches = await _source()._validate_repository_knownpost_context_sql(db, service, jobs,
                stage=_knownpost_stage, context=context)
        elif completion_witness is not None:
            matches = await _source()._validate_repository_completion_post_context_sql(db, service, jobs,
                witness=completion_witness, context=context)
        else:
            matches = _source()._stop_static_rows_match(run, stop, context["static_rows"])
        if not matches:
            raise DurableJobLeaseError("original repository stop current successor changed")


async def _original_accounting_members(db, context):
    """Validate original liability without granting terminal settlement."""
    from src.workflows.general_task_accounting import entry_for, reservation_liability
    source = _source()
    group = context["group"]
    rows = list((await db.scalars(select(InferenceCostReservation))).all())
    members, root_members = [], []
    for row in rows:
        entry = entry_for(row)
        same_group = bool(entry and entry["group"]["group_id"] == group.group_id)
        same_root = row.job_id == context["run"].run_identity
        if same_group:
            if entry["group"] != group.model_dump(mode="json"):
                raise DurableJobLeaseError("original stop group accounting changed")
            members.append(row)
        if same_root:
            member = entry.get("repository_binding") if entry else None
            if (not same_group or entry["role"] != "repository_iteration" or not isinstance(member, dict)
                    or member["repository_job_id"] != context["run"].run_identity
                    or member["source_checkpoint_digest"] != source._source_digest(context["original"])):
                raise DurableJobLeaseError("original stop Root accounting changed")
            root_members.append(row)
    if (len(members) > group.max_inference_calls
            or sum(reservation_liability(row) for row in members) > group.max_cost_microusd
            or sum(reservation_liability(row) for row in root_members) > context["work"].limits.max_cost_microusd):
        raise DurableJobLeaseError("original stop accounting exceeded original bounds")
    return members


async def _accounting(db, context):
    members = await _original_accounting_members(db, context)
    if any(row.state not in {"settled", "released"} for row in members):
        raise DurableJobLeaseError("original stop retains unsettled or Unknown liability")
    return _source()._source_digest([row.model_dump(mode="json")
        for row in sorted(members, key=lambda row: row.operation_id)])


async def _limit_evidence(db, context, *, reason):
    """Canonical original cause; the caller stages policy outside this writer."""
    from src.workflows.general_task_accounting import entry_for, reservation_liability
    from src.work_board.contracts import RepositoryNativeLimitEvidenceV1
    source = _source()
    limits = source.read_repository_inventory(context["run"])["original_limits"]
    if limits != context["original_limits"]:
        raise DurableJobLeaseError("original stop limit facts changed")
    goal = await db.get(Goal, context["goal"].id, populate_existing=True)
    if goal is None or source._source_digest(goal.model_dump(mode="json")) != limits["original_goal_row_digest"]:
        raise DurableJobLeaseError("original stop Goal limit facts changed")
    # Negative expiry/limit intent retains contacted and Unknown debt. The
    # terminal owner still requires _accounting's settled-only proof.
    members = await _original_accounting_members(db, context)
    root_members = [row for row in members if row.job_id == context["run"].run_identity]
    root_cost, group_cost = sum(map(reservation_liability, root_members)), sum(map(reservation_liability, members))
    group, work = context["group"], context["work"]
    cutoff, goal_cutoff = _as_utc(context["run"].deadline_at), source._repository_goal_cutoff(limits, group)
    now, bound = _utc_now(), limits["original_server_bound_microusd"]
    causes = {
        "deadline_exhausted": now >= cutoff,
        "goal_limit_exhausted": now >= goal_cutoff and cutoff == goal_cutoff,
        "cost_exhausted": work.limits.max_cost_microusd - root_cost < bound,
        "shared_group_exhausted": len(members) >= group.max_inference_calls or group.max_cost_microusd - group_cost < bound
            or now >= group.original_deadline_at,
    }
    if reason is None:
        candidates = ["goal_limit_exhausted", "deadline_exhausted", "cost_exhausted", "shared_group_exhausted"]
        reason = next((candidate for candidate in candidates if causes[candidate]), None)
        if reason is None:
            return None
    if reason not in AUTOMATIC_REASONS or not causes[reason]:
        raise DurableJobLeaseError("actual original repository limit cause is unproven")
    return RepositoryNativeLimitEvidenceV1(original_limits_digest=source._source_digest(limits),
        original_deadline_at=cutoff, original_server_bound_microusd=bound,
        root_liability_microusd=root_cost, group_liability_microusd=group_cost, group_calls=len(members),
        original_root_max_cost_microusd=work.limits.max_cost_microusd,
        original_group_max_cost_microusd=group.max_cost_microusd, original_group_max_calls=group.max_inference_calls,
        goal_cutoff_at=goal_cutoff, cause=reason).model_dump(mode="json")


async def repository_automatic_limit_reason(service, jobs, *, job_id, owner):
    """Check current original metadata before any private Source body read."""
    source = _source()
    staged_policy = source._repository_policy_limits()
    async with jobs._session() as db:
        run = await jobs._fetch(db, job_id)
        original, work, compiled, group, binding, task_source = source.read_repository_original(run)
        if (run.owner_principal_id, run.operator_session_id) != (owner.principal_id, owner.session_id):
            raise DurableJobLeaseError("original repository limit owner changed")
        goal = await db.get(Goal, binding.goal_id)
        limits = source._assert_repository_original_limits(run, goal, staged_policy)
        context = {"run": run, "original": original, "work": work, "group": group,
            "goal": goal, "original_limits": limits}
        evidence = await _limit_evidence(db, context, reason=None)
    source._recheck_repository_policy_limits(staged_policy)
    return None if evidence is None else evidence["cause"]


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class _RepositoryStopWitness:
    service: object
    jobs: object
    context: dict = field(repr=False)
    closure: RepositoryNativeStopClosureV1
    original_completion: object = field(default=None, repr=False)
    fence: object = field(default=None, repr=False)


def assert_repository_stop_witness(witness):
    if (type(witness) is not _RepositoryStopWitness or _ISSUED.get(witness) is not witness.service
            or type(witness.context) is not _RepositoryStopContext or _STAGED.get(witness.context) is not witness.service):
        raise DurableJobLeaseError("actual source-owned repository stop witness required")
    if _source().read_repository_inventory(witness.context["run"])["schema"] == "repository.checkpoint_inventory.v3":
        from src.workflows.repo_repair_source_recovery import (
            assert_repository_recovery_fence, assert_repository_original_stop_completion)
        assert_repository_recovery_fence(witness.fence, service=witness.service, jobs=witness.jobs,
            job_id=witness.context["run"].run_identity, owner=witness.context["owner"])
        if witness.closure.process_quiescence_digest != _source()._source_digest([]):
            assert_repository_original_stop_completion(witness.original_completion,
                service=witness.service, jobs=witness.jobs, context=witness.context, fence=witness.fence)


async def _positive_witness(service, jobs, context, stop, *, original_completion=None, fence=None):
    if type(context) is not _RepositoryStopContext or _STAGED.get(context) is not service:
        raise DurableJobLeaseError("actual source-staged stop context required")
    source = _source()
    models, processes, lineage, identities = [], [], [], []
    run, work, original, binding = (context[key] for key in ("run", "work", "original", "binding"))
    if run.status != "running":
        raise DurableJobLeaseError("Unknown original repository stop requires reconciliation")
    latest_execution = max((index for index in range(1, work.limits.max_iterations + 1)
        if source._repository_record(run, "repository:execution:" + source.iteration_identity(run.run_identity,
            original["repository_attempt_id"], source._source_digest(original["original_input"]), index)) is not None), default=0)
    for index in range(1, work.limits.max_iterations + 1):
        identity = source.iteration_identity(run.run_identity, original["repository_attempt_id"],
            source._source_digest(original["original_input"]), index)
        started = source._repository_record(run, "repository:callback-start:" + identity)
        execution = source._repository_record(run, "repository:execution:" + identity)
        if started is not None:
            identities.append(identity)
            callback = service._iterative_model_callbacks.get(identity)
            if callback is not None and (not callback.done() or callback.cancelled() or callback.exception() is not None):
                raise DurableJobLeaseError("original model callback closure is pending")
            returned = source._repository_record(run, "repository:proposal:" + identity)
            response = source._repository_record(run, "repository:response:" + identity)
            approval = source._repository_record(run, "repository:approval:" + identity)
            if returned is None or response is None or approval is None:
                raise DurableJobLeaseError("positive original native callback return is missing")
            raw = service._read_private_artifact(response["response_artifact_ref"],
                expected_digest=response["response_artifact_digest"])
            canonical = service._read_private_artifact(returned["canonical_source_artifact_ref"],
                expected_digest=returned["canonical_source_artifact_digest"])
            if not raw or not isinstance(json.loads(canonical), dict):
                raise DurableJobLeaseError("literal original callback stop readback changed")
            models.append(returned)
            lineage.extend([response, approval])
        if execution is not None:
            callback = service._iterative_process_callbacks.get(identity)
            if callback is not None and (not callback.done() or callback.cancelled() or callback.exception() is not None):
                raise DurableJobLeaseError("original process callback closure is pending")
            cleanup = source._repository_record(run, "repository:cleanup:" + identity)
            readback = source._repository_record(run, "repository:readback:" + identity)
            if cleanup is None or readback is None or cleanup.get("cleanup_proven") is not True:
                raise DurableJobLeaseError("positive original process cleanup is missing")
            cleanup_body = json.loads(service._read_private_artifact(cleanup["artifact_ref"],
                expected_digest=cleanup["artifact_digest"]))
            manifest = json.loads(service._read_private_artifact(readback["artifact_ref"],
                expected_digest=readback["artifact_digest"]))
            transport = manifest.get("supervisor_transport", {})
            if source.read_repository_inventory(run)["schema"] == "repository.checkpoint_inventory.v3":
                from src.workflows.repo_repair_source_recovery import (
                    assert_repository_original_stop_completion, repository_original_stop_completion_result,
                    repository_original_stop_completion_cleanup_envelope)
                assert_repository_original_stop_completion(original_completion,
                    service=service, jobs=jobs, context=context, fence=fence)
                actual = repository_original_stop_completion_result(original_completion, iteration_id=identity)
                authenticated_cleanup = repository_original_stop_completion_cleanup_envelope(
                    original_completion, iteration_id=identity)
                if _canonical(cleanup_body) != _canonical(authenticated_cleanup):
                    raise DurableJobLeaseError("literal original cleanup envelope changed")
                if "physical_projection" in authenticated_cleanup:
                    if (set(authenticated_cleanup) != {"physical_projection", "source_completion_cas", "source_append_metadata"}
                            or authenticated_cleanup["source_completion_cas"] != cleanup.get("source_completion_cas")
                            or authenticated_cleanup["source_completion_cas"] != readback.get("source_completion_cas")):
                        raise DurableJobLeaseError("literal original cleanup envelope changed")
                    cleanup_body = authenticated_cleanup["physical_projection"]
                transport_proven = (actual["manifest"] == manifest
                    and actual["outputs"]["readback.json"] == service._read_private_artifact(
                        readback["artifact_ref"], expected_digest=readback["artifact_digest"]))
            else:
                transport_proven = all(transport.get(key) is True for key in ("stdin_closed", "stdout_eof",
                    "stderr_eof", "stdout_closed", "stderr_closed", "waited"))
            if (cleanup_body.get("iteration_binding") != execution["process_binding"]
                    or manifest.get("iteration_binding") != execution["process_binding"]
                    or cleanup_body.get("process_cleanup", {}).get("oracle") != "linux_subreaper_waitpid_echild"
                    or cleanup_body.get("process_cleanup", {}).get("cleanup_proven") is not True
                    or manifest.get("stage_removed") is not True
                    or cleanup_body.get("artifact_digests", {}).get("readback.json") != readback["artifact_digest"]
                    or not transport_proven
                    or source._source_digest(manifest) != readback["manifest_digest"]):
                raise DurableJobLeaseError("literal original process quiescence changed")
            if index == latest_execution:
                marker = service.sandbox._read_job_marker(run.run_identity)
                if (not isinstance(marker, dict) or marker.get("iteration_binding") != execution["process_binding"]
                        or marker.get("cleanup_proven") is not True
                        or marker.get("terminal_receipt", {}).get("readback_sha256") != readback["artifact_digest"]):
                    raise DurableJobLeaseError("original full physical stop marker binding changed")
                if source.read_repository_inventory(run)["schema"] == "repository.checkpoint_inventory.v3":
                    if marker.get("process_cleanup") != {
                            "transport_kind": "original_producer_durable_v1",
                            "completion_digest": source._source_digest(actual["original_producer_completion"])}:
                        raise DurableJobLeaseError("original producer marker differs from active signed completion")
                elif work.language_profile == "test_node":
                    keys = {"profile", "job_id", "token", "supervisor_pid", "supervisor_start", "status",
                        "reason", "commands", "diff_sha256", "cleanup_proven", "process_cleanup",
                        "iteration_binding", "after_digest", "tested_file_hash_metadata"}
                    full = {key: manifest[key] for key in keys if key in manifest}
                    compact = marker.get("process_cleanup", {})
                    if (compact.get("metadata_digest") != source._source_digest(full.get("tested_file_hash_metadata"))
                            or compact.get("supervisor_result_digest") != source._source_digest(full)
                            or {key: value for key, value in compact.items() if key not in {
                                "metadata_digest", "supervisor_result_digest"}} != {
                                key: value for key, value in full.items() if key != "tested_file_hash_metadata"}):
                        raise DurableJobLeaseError("compact marker differs from retained literal full result")
            processes.append({"cleanup": cleanup, "readback": readback})
            lineage.append(execution)
    async with jobs._session() as db:
        accounting_digest = await _accounting(db, context)
    if stop["stop_reason"] == "iterations_exhausted":
        if (len(processes) != work.limits.max_iterations or any(
                item["readback"]["status"] != "failed" for item in processes)):
            raise DurableJobLeaseError("actual original iteration exhaustion is unproven")
    closure = RepositoryNativeStopClosureV1(original_binding=binding, repository_job_id=run.run_identity,
        repository_attempt_id=original["repository_attempt_id"], repository_fence=run.fencing_token,
        original_input_digest=source._source_digest(original["original_input"]),
        source_checkpoint_digest=source._source_digest(original), original_group_digest=source._source_digest(context["group"].model_dump(mode="json")),
        original_deadline_at=_as_utc(run.deadline_at), original_claim_fence=context["child"].fencing_token,
        iteration_ids=identities, stop_reason=stop["stop_reason"], stop_intent_digest=source._source_digest(stop),
        model_quiescence_digest=source._source_digest(models), process_quiescence_digest=source._source_digest(processes),
        all_original_accounting_digest=accounting_digest, request_response_approval_digest=source._source_digest(lineage),
        source_binding_digest=context["task_source"].binding_digest,
        limit_evidence=context["limit_evidence"] if stop["stop_reason"] in AUTOMATIC_REASONS else None,
        limit_evidence_digest=source._source_digest(context["limit_evidence"]) if stop["stop_reason"] in AUTOMATIC_REASONS else None)
    witness = _RepositoryStopWitness(service, jobs, context, closure, original_completion, fence)
    _ISSUED[witness] = service
    return witness


async def validate_repository_stop_witness(db, witness, *, parent, task, attempt, children):
    assert_repository_stop_witness(witness)
    source = _source()
    context, closure = witness.context, witness.closure
    if (parent.run_identity != closure.original_binding.parent_job_id or task.task_id != closure.original_binding.task_id
            or attempt.attempt_id != closure.original_binding.attempt_id
            or {child.run_identity for child in children}.isdisjoint({closure.original_binding.invocation_id})):
        raise DurableJobLeaseError("original stop C1 writer binding changed")
    for model, key, expected in context["rows"]:
        row = await db.get(model, key, populate_existing=True)
        if row is None or _canonical(row.model_dump(mode="json")) != expected:
            raise DurableJobLeaseError("original stop SQL epoch changed")
    run = await witness.jobs._fetch(db, closure.repository_job_id)
    stop = source._repository_record(run, STOP_ID)
    if (stop is None or source._source_digest(stop) != closure.stop_intent_digest
            or await _accounting(db, context) != closure.all_original_accounting_digest):
        raise DurableJobLeaseError("original stop intent or accounting changed")
    if closure.stop_reason in AUTOMATIC_REASONS:
        evidence = await _limit_evidence(db, context, reason=closure.stop_reason)
        if (evidence != closure.limit_evidence.model_dump(mode="json")
                or source._source_digest(evidence) != closure.limit_evidence_digest
                or stop.get("limit_evidence") != evidence
                or stop.get("limit_evidence_digest") != closure.limit_evidence_digest):
            raise DurableJobLeaseError("original stop canonical limit cause changed")
    return {closure.original_binding.invocation_id: closure}


async def complete_repository_stop_in_writer(db, witness, *, jobs):
    assert_repository_stop_witness(witness)
    from src.workflows.general_task_guard import _cancel_cas_job, read_general_task_native_cancel
    from src.work_board.repository import WorkBoardRepository
    source = _source()
    context, closure = witness.context, witness.closure
    parent = await jobs._fetch(db, closure.original_binding.parent_job_id)
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == closure.original_binding.task_id)
        .execution_options(populate_existing=True))
    attempt = await db.get(WorkBoardAttempt, closure.original_binding.attempt_id, populate_existing=True)
    if read_general_task_native_cancel(parent, task, attempt)["state"] != "fully_cancelled":
        raise DurableJobLeaseError("original stop retains other native callback debt")
    run = await jobs._fetch(db, closure.repository_job_id)
    original_journal = run.checkpoint_receipts_json
    source._append_repository_record(run, "repository:terminal:v1",
        {"schema": "repository.stop_terminal.v1", "closure": closure.model_dump(mode="json"),
         "no_learning": True}, inventory=source.repository_checkpoint_inventory(run, context["work"]))
    terminal = "cancelled" if closure.stop_reason == "operator_cancelled" else "failed"
    await source.append_repository_release_in_writer(db, jobs, run,
        original=context["original"], work=context["work"], witness=witness, outcome_status=terminal)
    updated_journal = run.checkpoint_receipts_json
    # The existing CAS compares the untouched original journal.
    run.checkpoint_receipts_json = original_journal
    await _cancel_cas_job(db, run, {"checkpoint_receipts_json": updated_journal, "status": terminal,
        "failure_reason": "repository_" + closure.stop_reason, "finished_at": _utc_now(),
        "lease_owner": None, "lease_expires_at": None,
        "result_digest": source._source_digest({"no_learning": True, "stop_reason": closure.stop_reason,
            "iterations": closure.iteration_ids, "verified": False}),
        "result_summary": "repository_" + closure.stop_reason})
    repo_task = await db.get(WorkBoardTask, _key(context["repo_task"]), populate_existing=True)
    repo_attempt = await db.get(WorkBoardAttempt, context["repo_attempt"].attempt_id, populate_existing=True)
    # These are the original Source-staged rows. Both changes remain in the
    # existing C1 writer; no projection helper may start/commit another writer.
    for row, staged in ((repo_task, context["repo_task"]), (repo_attempt, context["repo_attempt"])):
        if row is None or _canonical(row.model_dump(mode="json")) != _canonical(staged.model_dump(mode="json")):
            raise DurableJobLeaseError("original repository stop board epoch changed")
    now = _utc_now()
    changes = ((repo_task, {"status": "blocked", "block_reason": "repository_" + closure.stop_reason,
                           "task_revision": repo_task.task_revision + 1, "updated_at": now}),
               (repo_attempt, {"outcome": "repository_" + closure.stop_reason, "ended_at": now,
                               "lease_owner": None, "lease_expires_at": None, "updated_at": now}))
    for row, values in changes:
        await _cas_repository_stop_board_row(db, row, values)
    await WorkBoardRepository._event(db, repo_task, context["owner"], kind="attempt.repository_stopped",
        metadata={"attempt_id": repo_attempt.attempt_id, "workflow_run_id": run.run_identity,
            "stop_reason": closure.stop_reason, "no_learning": True})


async def _cas_repository_stop_board_row(db, row, values):
    model = type(row)
    if model not in {WorkBoardTask, WorkBoardAttempt}:
        raise DurableJobLeaseError("original repository stop board row required")
    predicates = []
    for column in inspect_mapper(model).columns:
        actual = getattr(row, column.key)
        attribute = getattr(model, column.key)
        predicates.append(attribute.is_(None) if actual is None else attribute == actual)
    changed = await db.execute(update(model).where(*predicates).values(**values)
        .execution_options(synchronize_session=False))
    if int(changed.rowcount or 0) != 1:
        raise DurableJobLeaseError("original repository stop board CAS lost")
    await db.refresh(row)


async def _persist_repository_stop_intent_locked(service, jobs, *, context, owner, reason, fence):
    """Commit and re-read original intent under the actual owner's live fence."""
    from src.workflows.repo_repair_source_recovery import assert_repository_recovery_fence
    from src.work_board.repository import _begin_sqlite_immediate, WorkBoardRepository
    source = _source()
    if type(context) is not _RepositoryStopContext or _STAGED.get(context) is not service:
        raise DurableJobLeaseError("actual source-staged stop context required")
    job_id = context["run"].run_identity
    assert_repository_recovery_fence(fence, service=service, jobs=jobs, job_id=job_id, owner=owner)
    if context["owner"] != owner or reason not in {"operator_cancelled", "iterations_exhausted"} | AUTOMATIC_REASONS:
        raise DurableJobLeaseError("closed original repository stop reason required")
    existing_stop = source._repository_record(context["run"], STOP_ID)
    if existing_stop is None:
        snapshot = {"schema": "repository.stop_snapshot.v1", "static_rows": context["static_rows"],
            "repository_job_id": job_id, "source_checkpoint_digest": source._source_digest(context["original"])}
        encoded = _canonical(snapshot).encode()
        if len(encoded) > 1024 * 1024:
            raise DurableJobLeaseError("original stop snapshot exceeds protected artifact bound")
        snapshot_ref, snapshot_digest = service._write_private_artifact(
            "artifacts/repo-repair/stop-" + source._source_digest(job_id) + ".json", encoded)
        if service._read_private_artifact(snapshot_ref, expected_digest=snapshot_digest) != encoded:
            raise DurableJobLeaseError("literal original stop snapshot readback changed")
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        for model, key, expected in context["rows"]:
            row = await db.get(model, key, populate_existing=True)
            if row is None or _canonical(row.model_dump(mode="json")) != expected:
                raise DurableJobLeaseError("original stop intent SQL epoch changed")
        run = await jobs._fetch(db, job_id)
        stop = source._repository_record(run, STOP_ID)
        if stop is None:
            stop = {"schema": "repository.stop_intent.v1", "stop_reason": reason,
                "native_binding_digest": source._source_digest(context["binding"].model_dump(mode="json")),
                "static_rows": context["static_rows"], "snapshot_artifact_ref": snapshot_ref,
                "snapshot_artifact_digest": snapshot_digest, "no_learning": True}
            if reason in AUTOMATIC_REASONS:
                stop.update(limit_evidence=context["limit_evidence"],
                    limit_evidence_digest=source._source_digest(context["limit_evidence"]))
            source._append_repository_record(run, STOP_ID, stop,
                inventory=source.repository_checkpoint_inventory(run, context["work"]))
            run.revision += 1
        elif stop.get("stop_reason") != reason:
            raise DurableJobLeaseError("original stop reason cannot be replaced")
        event = await WorkBoardRepository._event(db, context["task"], owner,
            kind="attempt.repository_stop_requested", metadata={"attempt_id": context["attempt"].attempt_id,
                "repository_job_id": job_id, "stop_reason": reason, "no_learning": True})
        repository_event = await WorkBoardRepository._event(db, context["repo_task"], owner,
            kind="attempt.repository_stop_requested", metadata={"attempt_id": context["repo_attempt"].attempt_id,
                "repository_job_id": job_id, "stop_reason": reason, "no_learning": True})
        await db.commit()
    # The committed intent must be observed before any completion adoption.
    assert_repository_recovery_fence(fence, service=service, jobs=jobs, job_id=job_id, owner=owner)
    fresh = await _context(service, jobs, job_id=job_id, owner=owner,
        limit_reason=reason if reason in AUTOMATIC_REASONS else None)
    staged = _RepositoryStopContext(MappingProxyType({**fresh.data, "stop_events": (event, repository_event)}))
    _STAGED[staged] = service
    return staged


async def _complete_repository_stop_held(service, jobs, *, context, stop, original_completion, fence):
    """Original Stop issuer/writer/readback while its actual physical scope is held."""
    from src.workflows.repo_repair_source_recovery import assert_repository_recovery_fence
    from src.work_board.repository import BoardAttemptProjection
    from src.db.models import WorkBoardEvent
    assert_repository_stop_context(context, service=service, jobs=jobs)
    job_id, owner = context["run"].run_identity, context["owner"]
    assert_repository_recovery_fence(fence, service=service, jobs=jobs, job_id=job_id, owner=owner)
    witness = await _positive_witness(service, jobs, context, stop,
        original_completion=original_completion, fence=fence)
    try:
        if job_id not in service._iterative_lanes:
            # The original SQL hold is independent of physical proof.
            run = context["run"]
            held = jobs._repo_repair_reservation_state(run)
            if (held is None or held.get("status") != "held"
                    or not jobs._repo_repair_reservation_matches(held, job_id=job_id,
                        attempt_id=context["original"]["repository_attempt_id"],
                        fence=run.fencing_token, authority_digest=run.authority_digest)
                    or held.get("execution_deadline_at") != context["original"]["original_deadline_at"]):
                raise DurableJobLeaseError("original stop recovery held reservation changed")
            if original_completion is None:
                # Legacy/undispatched closure has no producer guard.
                from src.workflows.repair_capacity import try_acquire_repo_repair_capacity
                lane = try_acquire_repo_repair_capacity(service._workspace(), job_id=job_id)
                if lane is None:
                    raise DurableJobLeaseError("original stop recovery physical capacity is still owned")
                lane.bind_owner(job_id=job_id, attempt_id=context["original"]["repository_attempt_id"],
                    fence_token=run.fencing_token, authority_digest=run.authority_digest)
                service._iterative_lanes[job_id] = lane
            else:
                # The active registered producer scope already holds
                # this Root's exact guard; never acquire a second flock.
                from src.workflows.repo_repair_source_recovery import assert_repository_original_stop_completion
                assert_repository_original_stop_completion(original_completion,
                    service=service, jobs=jobs, context=context, fence=fence)
        result = await jobs.cancel_general_task_native_parent(context["binding"].parent_job_id,
            operator_owner=owner, expected_task_revision=context["task"].task_revision,
            repository_stop_witness=witness)
        async with jobs._session() as db:
            run = await jobs._fetch(db, job_id)
            # Reuse the original terminal projection's complete canonical
            # Root/Task/Attempt/C1/Goal/accounting/release predicates.
            await _source()._repository_discovery_metadata(db, run, owner=owner, service=service)
            for key in ("task", "attempt", "event"):
                expected = result[key]
                actual = await db.get(type(expected), _key(expected), populate_existing=True)
                if actual is None or _committed_stop_row_json(actual) != _committed_stop_row_json(expected):
                    raise DurableJobLeaseError("committed original native Stop readback changed")
            repo_task = await db.get(WorkBoardTask, _key(context["repo_task"]), populate_existing=True)
            repo_attempt = await db.get(WorkBoardAttempt, _key(context["repo_attempt"]), populate_existing=True)
            events = list((await db.scalars(select(WorkBoardEvent).where(
                WorkBoardEvent.task_id == repo_task.task_id,
                WorkBoardEvent.kind == "attempt.repository_stopped").limit(2))).all())
            if len(events) != 1:
                raise DurableJobLeaseError("committed original repository stop action receipt missing")
            repository_event = events[0]
            if (type(repository_event.event_id) is not int or repository_event.event_id <= 0
                    or repository_event.owner_principal_id != repo_task.owner_principal_id
                    or repository_event.owner_session_id != repo_task.owner_session_id
                    or repository_event.mutation_idempotency_key is not None
                    or repository_event.mutation_request_digest is not None
                    or repository_event.actor_principal_id != owner.principal_id
                    or repository_event.actor_session_id != owner.session_id
                    or json.loads(repository_event.metadata_json) != {
                        "attempt_id": repo_attempt.attempt_id, "workflow_run_id": job_id,
                        "stop_reason": stop["stop_reason"], "no_learning": True}):
                raise DurableJobLeaseError("committed original repository stop action receipt changed")
        if original_completion is not None:
            from src.workflows.repo_repair_source_recovery import recheck_repository_original_stop_physical
            recheck_repository_original_stop_physical(original_completion, service=service,
                jobs=jobs, context=context, fence=fence)
        await _stage_repository_stop_context_artifacts(service, context.data, stop=stop,
            expired_cleanup=context["expired_cleanup"])
        _source()._assert_task_publication_configuration(service)
        return {"pending": False,
            "projection": BoardAttemptProjection(result["task"], result["attempt"], result["event"]),
            "repository_projection": BoardAttemptProjection(repo_task, repo_attempt, repository_event),
            "job_id": job_id, "stop_reason": stop["stop_reason"], "no_learning": True}
    finally:
        _ISSUED.pop(witness, None)


async def stop_repository_root(service, jobs, *, job_id, owner, general_task_service, reason="operator_cancelled"):
    from src.work_board.repository import BoardAttemptProjection
    source = _source()
    if reason not in {"operator_cancelled", "iterations_exhausted"} | AUTOMATIC_REASONS:
        raise DurableJobLeaseError("closed original repository stop reason required")
    from src.workflows.repo_repair_source_recovery import _repository_recovery_fence
    async with _repository_recovery_fence(service, jobs, job_id=job_id, owner=owner) as fence:
        source._assert_task_publication_configuration(service)
        context = await _context(service, jobs, job_id=job_id, owner=owner,
            limit_reason=reason if reason in AUTOMATIC_REASONS else None)
        context = await _persist_repository_stop_intent_locked(service, jobs,
            context=context, owner=owner, reason=reason, fence=fence)
        stop = source._repository_record(context["run"], STOP_ID)
        event, repository_event = context["stop_events"]
    # Signalling and waiting happen after the short configuration/SQL fence.
    # Only actual Source-owned jobs may supply the exact supervisor authority.
    import asyncio
    for identity, callback in tuple(service._iterative_process_callbacks.items()):
        job = service._iterative_process_jobs.get(identity)
        if job is None or job.job_id != job_id or callback.done():
            continue
        await asyncio.to_thread(service.sandbox.cancel, job_id=job_id,
            authority={"job_id": job_id, "attempt_id": job.attempt_id,
                "fencing_token": job.fencing_token, "authority_digest": job.authority_digest})
        await asyncio.wait({callback}, timeout=1)
    try:
        async with _repository_recovery_fence(service, jobs, job_id=job_id, owner=owner) as fence:
            source._assert_task_publication_configuration(service)
            context = await _context(service, jobs, job_id=job_id, owner=owner,
                limit_reason=reason if reason in AUTOMATIC_REASONS else None)
            async with source.stage_repository_stop_original_producer_witnesses(
                    service, jobs, context=context, fence=fence) as original_completion:
                result = await _complete_repository_stop_held(service, jobs, context=context, stop=stop,
                    original_completion=original_completion, fence=fence)
    except DurableJobLeaseError:
        lane = service._iterative_lanes.get(job_id)
        if lane is not None:
            lane.quarantine(job_id)
        return {"pending": True, "projection": BoardAttemptProjection(context["task"], context["attempt"], event),
            "repository_projection": BoardAttemptProjection(context["repo_task"], context["repo_attempt"], repository_event),
            "job_id": job_id, "stop_reason": reason, "no_learning": True}
    lane = service._iterative_lanes.pop(job_id, None)
    if lane is not None:
        lane.clear_quarantine()
    return result


async def repository_stop_for_parent(service, jobs, *, parent_id, general_task_service, owner=None,
        request_stop=False):
    """Find only the permanent original-child mapping; never mint a root."""
    from src.workflows.general_task_guard import child_binding
    from src.workflows.job_runtime import _binding
    source = _source()
    async with jobs._session() as db:
        children = list((await db.scalars(select(WorkflowRunState).where(
            WorkflowRunState.parent_job_id == parent_id))).all())
        roots = []
        for child in children:
            if json.loads(child.arguments_json).get("tool_id") != "repository_work":
                continue
            binding = child_binding(child)
            key = _binding(owner_principal_id=binding.owner_principal_id, goal_id=binding.goal_id,
                goal_revision=binding.goal_revision, idempotency_scope="original-repository-child",
                dedupe_key=binding.invocation_id)
            root = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.idempotency_binding == key))
            if root is not None:
                original = source.read_repository_original(root)[0]
                if original["native_binding"] != binding.model_dump(mode="json"):
                    raise DurableJobLeaseError("permanent original repository stop mapping mismatch")
                if request_stop or source._repository_record(root, STOP_ID) is not None:
                    roots.append((root.run_identity, binding))
        if not roots:
            return None
        if len(roots) != 1:
            raise DurableJobLeaseError("one original repository stop root required")
        job_id, binding = roots[0]
        actual_owner = owner or WorkBoardOwner(principal_id=binding.owner_principal_id,
            session_id=binding.original_root_id)
    return await stop_repository_root(service, jobs, job_id=job_id, owner=actual_owner,
        general_task_service=general_task_service)
