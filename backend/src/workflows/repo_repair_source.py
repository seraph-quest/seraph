"""Private source bindings for the fixed repository iteration owners.

These objects are deliberately not request models. A serialized projection is
evidence, and cannot recreate an execution or accounting witness.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
import hashlib
import json
import re
import weakref
import asyncio
import threading
from types import MappingProxyType
from typing import Any
from contextlib import asynccontextmanager
from contextvars import ContextVar

from src.work_board.contracts import (TaskProposalGroupV1,
    GeneralTaskNativeChildBindingV1, RepositoryTaskSourceBinding)

_SEAL = object()
_SHA = re.compile(r"^[0-9a-f]{64}$")
_task_publication = ContextVar("repository_task_publication", default=None)
_startup_fence = ContextVar("repository_startup_fence", default=None)
_UNCERTAINTY_COLUMNS = frozenset({"status", "failure_reason", "finished_at", "lease_owner",
    "lease_expires_at", "result_digest", "result_summary"})
_ROOT_BOOKKEEPING = frozenset({"updated_at", "revision", "checkpoint_receipts_json", "heartbeat_at"})


@asynccontextmanager
async def _repository_startup_mutation_fence():
    """One task-owned startup fence, shared by its accounting subpass."""
    import asyncio
    from src.model_fabric.effective_policy import configuration_mutation_lock
    task = asyncio.current_task()
    held = _startup_fence.get()
    if held is not None and held[0] is task:
        if not configuration_mutation_lock.locked():
            raise RuntimeError("repository startup fence lost")
        yield
        return
    async with configuration_mutation_lock:
        token = _startup_fence.set((task, object()))
        try:
            yield
        finally:
            _startup_fence.reset(token)


async def _repository_startup_protected_lineage(db):
    """Classify exact original IDs in the caller's IMMEDIATE SQL epoch.

    This is mutation deferral only. It cannot grant cleanup, execution, or
    financial settlement and never opens a physical artifact.
    """
    import asyncio
    import logging
    from sqlalchemy import select
    from src.db.models import WorkflowRunState
    from src.workflows.general_task_guard import child_binding, _history
    from src.workflows.job_runtime import DurableJobLeaseError, _binding
    from src.workflows.repo_repair_source_recovery import read_registered_repository_producer
    held = _startup_fence.get()
    if held is None or held[0] is not asyncio.current_task():
        raise DurableJobLeaseError("private repository startup mutation fence required")
    protected = set()
    after = 0
    while True:
        roots = list((await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.job_kind == "engineering.repo-repair.v1",
            WorkflowRunState.id > after).order_by(WorkflowRunState.id).limit(64))).scalars())
        if not roots:
            break
        for root in roots:
            after = root.id
            established = {root.run_identity}
            present = False
            try:
                history = _history(root)
                markers = [item for item in history if isinstance(item.get("checkpoint_id"), str)
                    and re.fullmatch(r"repository:producer:[0-9a-f]{64}", item["checkpoint_id"])]
                present = bool(markers)
                if not present:
                    continue
                if (root.capability_version != "1" or root.root_run_identity != root.run_identity
                        or root.parent_job_id or root.idempotency_scope != "original-repository-child"
                        or not isinstance(root.idempotency_key, str) or not root.idempotency_key):
                    raise DurableJobLeaseError("original startup Root mapping changed")
                expected_mapping = _binding(owner_principal_id=root.owner_principal_id,
                    goal_id=root.goal_id, goal_revision=root.goal_revision,
                    idempotency_scope="original-repository-child", dedupe_key=root.idempotency_key)
                mapped = (await db.execute(select(WorkflowRunState).where(
                    WorkflowRunState.idempotency_binding == expected_mapping))).scalar_one_or_none()
                if mapped is None or mapped.id != root.id or root.idempotency_binding != expected_mapping:
                    raise DurableJobLeaseError("original startup repository mapping changed")
                child = (await db.execute(select(WorkflowRunState).where(
                    WorkflowRunState.run_identity == root.idempotency_key))).scalar_one_or_none()
                binding = child_binding(child) if child is not None else None
                if (binding is None or binding.invocation_id != root.idempotency_key
                        or json.loads(child.arguments_json).get("tool_id") != "repository_work"
                        or (root.owner_principal_id, root.operator_session_id, root.session_id,
                            root.goal_id, root.goal_revision) !=
                           (binding.owner_principal_id, binding.original_root_id, binding.original_root_id,
                            binding.goal_id, binding.goal_revision)):
                    raise DurableJobLeaseError("original startup native mapping changed")
                established.add(child.run_identity)
                parent = (await db.execute(select(WorkflowRunState).where(
                    WorkflowRunState.run_identity == binding.parent_job_id))).scalar_one_or_none()
                if (parent is None or child.parent_job_id != parent.run_identity
                        or (parent.owner_principal_id, parent.operator_session_id,
                            parent.goal_id, parent.goal_revision) !=
                           (binding.owner_principal_id, binding.original_root_id,
                            binding.goal_id, binding.goal_revision)):
                    raise DurableJobLeaseError("original startup explicit parent changed")
                established.add(parent.run_identity)
                if (len({item["checkpoint_id"] for item in markers}) != len(markers)
                        or any(item.get("safe") is not True for item in markers)):
                    raise DurableJobLeaseError("original startup producer marker changed")
                original, work, _, _, original_binding, _ = read_repository_original(root)
                if original_binding != binding:
                    raise DurableJobLeaseError("original startup native provenance changed")
                inventory = _repository_record(root, "repository:inventory:v1")
                if inventory is None or inventory.get("schema") != "repository.checkpoint_inventory.v3":
                    raise DurableJobLeaseError("original startup registered inventory changed")
                for index in range(1, work.limits.max_iterations + 1):
                    identity = iteration_identity(root.run_identity, original["repository_attempt_id"],
                        _source_digest(original["original_input"]), index)
                    if _repository_record(root, "repository:producer:" + identity) is not None:
                        read_registered_repository_producer(root, iteration_index=index)
                read_repository_inventory(root)
            except Exception:
                # Corrupt present metadata cannot be interpreted as absence.
                # Only IDs already established by exact canonical mappings
                # are retained; no foreign IDs are guessed from corruption.
                if not present:
                    raise
                logging.getLogger(__name__).warning(
                    "Repository original startup provenance blocked for %s; exact lineage retained",
                    root.run_identity, exc_info=True)
            if present:
                protected.update(established)
    return frozenset(protected)


@asynccontextmanager
async def stage_repository_stop_original_producer_witnesses(service, jobs, *, context, fence):
    from src.workflows.repo_repair_stop import assert_repository_stop_context
    from src.workflows.repo_repair_source_recovery import (
        assert_repository_recovery_fence, stage_repository_original_stop_completion)
    assert_repository_stop_context(context, service=service, jobs=jobs)
    run, original, work = context["run"], context["original"], context["work"]
    assert_repository_recovery_fence(fence, service=service, jobs=jobs,
        job_id=run.run_identity, owner=context["owner"])
    dispatched = any(_repository_record(run, "repository:execution:" + iteration_identity(
        run.run_identity, original["repository_attempt_id"], _source_digest(original["original_input"]), index))
        is not None for index in range(1, work.limits.max_iterations + 1))
    if read_repository_inventory(run)["schema"] != "repository.checkpoint_inventory.v3" or not dispatched:
        yield None
        return
    async with stage_repository_original_stop_completion(service, jobs, context=context,
            owner=context["owner"], fence=fence) as witness:
        yield witness


async def _validate_repository_completion_post_context_sql(db, service, jobs, *, witness, context):
    """Consume only an authentic writer's exact already-committed successor."""
    from src.workflows.repo_repair_source_recovery import (
        assert_repository_completion_witness, repository_completion_context,
        repository_completion_post_cas, repository_completion_committed_rows)
    from src.workflows.job_runtime import DurableJobLeaseError, _canonical
    assert_repository_completion_witness(witness, service=service, jobs=jobs)
    before = repository_completion_context(witness)
    cas = repository_completion_post_cas(witness)
    run = context["run"]
    stop = _repository_record(run, "repository:stop-intent:v1")
    if (read_repository_inventory(run)["schema"] != "repository.checkpoint_inventory.v3"
            or run.run_identity != before["run"].run_identity
            or context["owner"] != before["owner"]
            or type(cas["before_revision"]) is not int or type(cas["post_revision"]) is not int
            or cas["post_revision"] != cas["before_revision"] + 1
            or before["run"].revision != cas["before_revision"]
            or run.revision != cas["post_revision"] or stop is None
            or _source_digest(stop) != cas["stop_digest"]
            or not _stop_static_rows_match(before["run"], stop, before["static_rows"])):
        raise DurableJobLeaseError("authenticated repository completion successor changed")
    committed = repository_completion_committed_rows(witness)
    if not committed:
        raise DurableJobLeaseError("authenticated repository completion rows missing")
    for model, key, expected in committed:
        current = await db.get(model, key, populate_existing=True)
        if current is None or _canonical(current.model_dump(mode="json")) != expected:
            raise DurableJobLeaseError("authenticated repository completion row changed")
    # Cleanup changes only Root bookkeeping. The original snapshot continues
    # to bind every non-Root byte and the Root's actual static values.
    return context["static_rows"] == before["static_rows"]


async def stage_repository_completion_post_context(service, jobs, *, witness, owner, fence):
    from src.workflows.repo_repair_source_recovery import (
        assert_repository_completion_witness, repository_completion_context,
        assert_repository_recovery_fence)
    from src.workflows.repo_repair_stop import _context
    assert_repository_completion_witness(witness, service=service, jobs=jobs)
    original = repository_completion_context(witness)
    assert_repository_recovery_fence(fence, service=service, jobs=jobs,
        job_id=original["run"].run_identity, owner=owner)
    return await _context(service, jobs, job_id=original["run"].run_identity,
        owner=owner, completion_witness=witness)


async def _validate_repository_knownpost_context_sql(db, service, jobs, *, stage, context):
    """Recheck one registered committed Unknown successor using actual rows."""
    from src.workflows.repo_repair_source_recovery import (
        assert_repository_knownpost_stage, repository_knownpost_stage,
        _verify_repository_knownpost_root)
    from src.workflows.repo_repair_stop import _static
    from src.workflows.job_runtime import DurableJobLeaseError, _canonical
    assert_repository_knownpost_stage(stage, service=service, jobs=jobs)
    binding = repository_knownpost_stage(stage)
    run = context["run"]
    if (context["owner"] is not binding["owner"]
            or run.run_identity != binding["job_id"]
            or _canonical(run.model_dump(mode="json")) != binding["root_json"]):
        raise DurableJobLeaseError("actual repository knownpost context changed")
    actual = await jobs._fetch(db, binding["job_id"])
    if _canonical(actual.model_dump(mode="json")) != binding["root_json"]:
        raise DurableJobLeaseError("actual repository knownpost Root changed")
    # This codec verifies only local JSON mappings and original SQL metadata.
    # It removes the exact two bound wrappers and decrements revision once;
    # no previous ORM row is constructed or restored.
    proof = _verify_repository_knownpost_root(actual, binding["registration"],
        binding["result"], binding["cleanup_envelope"])
    if proof != {key: binding[key] for key in (
            "root_key", "current_static_digest", "unknown_projection")}:
        raise DurableJobLeaseError("actual repository knownpost projection changed")
    stop = _repository_record(actual, "repository:stop-intent:v1")
    if stop is None or _source_digest(stop) != binding["stop_digest"]:
        raise DurableJobLeaseError("actual repository knownpost Stop changed")
    current = {}
    root_count = 0
    for model, key, expected in context["rows"]:
        row = await db.get(model, key, populate_existing=True)
        if row is None or _canonical(row.model_dump(mode="json")) != expected:
            raise DurableJobLeaseError("actual repository knownpost row changed")
        row_key = model.__tablename__ + ":" + str(key)
        if row_key in current:
            raise DurableJobLeaseError("actual repository knownpost row duplicated")
        current[row_key] = _static(row, context)
        if row_key == binding["root_key"]:
            root_count += 1
            if _canonical(row.model_dump(mode="json")) != binding["root_json"]:
                raise DurableJobLeaseError("actual repository knownpost Root row changed")
    expected = dict(stop["static_rows"])
    if binding["root_key"] not in expected or root_count != 1:
        raise DurableJobLeaseError("actual repository knownpost Root key changed")
    expected[binding["root_key"]] = proof["current_static_digest"]
    return current == context["static_rows"] == expected


async def stage_repository_knownpost_context(service, jobs, *, stage, owner, fence):
    """Stage current Source context only inside an authentic knownpost scope."""
    from src.workflows.repo_repair_source_recovery import (
        assert_repository_knownpost_stage, repository_knownpost_stage,
        assert_repository_recovery_fence)
    from src.workflows.repo_repair_stop import _context
    from src.workflows.job_runtime import DurableJobLeaseError
    assert_repository_knownpost_stage(stage, service=service, jobs=jobs, fence=fence)
    binding = repository_knownpost_stage(stage)
    if binding["owner"] is not owner or binding["fence"] is not fence:
        raise DurableJobLeaseError("actual repository knownpost owner fence required")
    assert_repository_recovery_fence(fence, service=service, jobs=jobs,
        job_id=binding["job_id"], owner=owner)
    return await _context(service, jobs, job_id=binding["job_id"], owner=owner,
        _knownpost_stage=stage)


@dataclass(frozen=True, slots=True)
class _RepositoryUnknownRootProjection:
    """Immutable SQL evidence, carrying no cleanup or publication authority."""
    root_json: str = field(repr=False)
    successor_json: str = field(repr=False)
    stop_digest: str
    predecessor_digest: str
    current_digest: str
    revision: int
    fencing_token: int


def _checked_uncertainty_columns(value):
    from pydantic import TypeAdapter
    from src.workflows.job_runtime import DurableJobLeaseError
    if type(value) is not dict or set(value) != _UNCERTAINTY_COLUMNS:
        raise DurableJobLeaseError("original uncertainty column projection required")
    for name, bound in (("lease_owner", 1024), ("failure_reason", 4096), ("result_summary", 4096)):
        item = value[name]
        if item is not None:
            try:
                if type(item) is not str or len(item.encode("utf-8")) > bound:
                    raise ValueError()
            except (ValueError, UnicodeError):
                raise DurableJobLeaseError("original uncertainty text projection changed")
    digest = value["result_digest"]
    if digest is not None and (type(digest) is not str or not _SHA.fullmatch(digest)):
        raise DurableJobLeaseError("original uncertainty result digest changed")
    for name in ("finished_at", "lease_expires_at"):
        item = value[name]
        if item is not None:
            try:
                adapter = TypeAdapter(datetime)
                if type(item) is not str or adapter.dump_python(adapter.validate_python(item), mode="json") != item:
                    raise ValueError()
            except (ValueError, TypeError):
                raise DurableJobLeaseError("original uncertainty timestamp projection changed")


def _validate_repository_unknown_root_projection(run, stop, successor):
    """Verify recorded pre-erasure values against the immutable original hash."""
    from src.workflows.job_runtime import DurableJobLeaseError, _canonical, _digest
    inventory = read_repository_inventory(run)
    keys = {"schema", "job_id", "stop_digest", "root_key", "predecessor_digest",
        "successor_digest", "authority_digest", "fencing_token", "from_revision", "to_revision",
        "predecessor_projection", "successor_projection"}
    root_key = type(run).__tablename__ + ":" + str(run.id)
    if (type(stop) is not dict or type(stop.get("static_rows")) is not dict
            or type(successor) is not dict or set(successor) != keys
            or successor["schema"] != "repository.stop_uncertainty_successor.v1"
            or inventory["schema"] not in {"repository.checkpoint_inventory.v2", "repository.checkpoint_inventory.v3"}
            or successor["job_id"] != run.run_identity or successor["root_key"] != root_key
            or successor["stop_digest"] != _source_digest(stop)
            or successor["authority_digest"] != run.authority_digest
            or type(successor["fencing_token"]) is not int or successor["fencing_token"] != run.fencing_token
            or type(successor["from_revision"]) is not int or successor["from_revision"] < 0
            or type(successor["to_revision"]) is not int or successor["to_revision"] < 0
            or successor["to_revision"] != successor["from_revision"] + 1
            or type(run.revision) is not int or run.revision != successor["to_revision"]):
        raise DurableJobLeaseError("original repository uncertainty successor changed")
    predecessor, following = successor["predecessor_projection"], successor["successor_projection"]
    _checked_uncertainty_columns(predecessor)
    _checked_uncertainty_columns(following)
    reasons = {"repository_callback_closure_unproven": "reconcile_original_callback",
        "repository_process_closure_unproven": "reconcile_original_process"}
    if (predecessor["status"] != "running" or type(predecessor["lease_owner"]) is not str
            or not predecessor["lease_owner"] or predecessor["lease_expires_at"] is None
            or following["status"] != "unknown_external_effect" or following["failure_reason"] not in reasons
            or following["finished_at"] is not None or following["lease_owner"] is not None
            or following["lease_expires_at"] is not None or following["result_summary"] != "result recorded"):
        raise DurableJobLeaseError("original uncertainty transition write set changed")
    original, work, *_ = read_repository_original(run)
    matching = [index for index in range(1, work.limits.max_iterations + 1)
        if following["result_digest"] == _digest({"no_learning": True,
            "operator_action": reasons[following["failure_reason"]],
            "iteration_id": iteration_identity(run.run_identity, original["repository_attempt_id"],
                _source_digest(original["original_input"]), index)})]
    if len(matching) != 1:
        raise DurableJobLeaseError("original uncertainty result projection changed")
    current = run.model_dump(mode="json")
    if {key: current[key] for key in _UNCERTAINTY_COLUMNS} != following:
        raise DurableJobLeaseError("current original uncertainty projection changed")
    old_candidate = {**current, **predecessor}
    predecessor_digest = _source_digest({key: item for key, item in old_candidate.items() if key not in _ROOT_BOOKKEEPING})
    current_digest = _source_digest({key: item for key, item in current.items() if key not in _ROOT_BOOKKEEPING})
    if (predecessor_digest != successor["predecessor_digest"]
            or predecessor_digest != stop["static_rows"].get(root_key)
            or current_digest != successor["successor_digest"]):
        raise DurableJobLeaseError("original uncertainty immutable Root changed")
    return _RepositoryUnknownRootProjection(_canonical(current), _canonical(successor),
        _source_digest(stop), predecessor_digest, current_digest, run.revision, run.fencing_token)


def _repository_unknown_root_projection(run):
    """Source-owned pure seam; actual rows only, no caller evidence or I/O."""
    from src.workflows.job_runtime import DurableJobLeaseError
    stop = _repository_record(run, "repository:stop-intent:v1")
    successor = _repository_record(run, "repository:stop-uncertainty-successor:v1")
    if stop is None or successor is None:
        raise DurableJobLeaseError("original Stop and uncertainty successor required")
    return _validate_repository_unknown_root_projection(run, stop, successor)


def _stop_static_rows_match(run, stop, current):
    """Read-only exact successor; never closure or release authority."""
    from src.workflows.job_runtime import DurableJobLeaseError
    successor = _repository_record(run, "repository:stop-uncertainty-successor:v1")
    if successor is None:
        return stop.get("static_rows") == current
    projection = _repository_unknown_root_projection(run)
    root_key = type(run).__tablename__ + ":" + str(run.id)
    if projection.stop_digest != _source_digest(stop) or projection.current_digest != current.get(root_key):
        raise DurableJobLeaseError("original repository uncertainty successor changed")
    expected = dict(stop["static_rows"])
    expected[root_key] = projection.current_digest
    return current == expected


async def _quarantine_original_uncertainty(service, jobs, *, job_id, owner, lease_owner, fencing_token, reason, result):
    """Only the existing original closure-uncertainty owners call this seam."""
    from src.model_fabric.effective_policy import configuration_mutation_lock
    from src.workflows.repo_repair_stop import stage_repository_uncertainty
    async with configuration_mutation_lock:
        current = await jobs.get_job(job_id)
        if current and current["status"] == "running":
            witness = await stage_repository_uncertainty(service, jobs, job_id=job_id, owner=owner)
            await jobs.transition_job(job_id, "unknown_external_effect",
                owner=lease_owner, fencing_token=fencing_token,
                expected_revision=current["revision"], expected_status="running", reason=reason,
                result=result, _repository_uncertainty_witness=witness)


def assert_repository_stop_witness(witness):
    from src.workflows.repo_repair_stop import assert_repository_stop_witness as verify
    return verify(witness)


async def validate_repository_stop_witness(db, witness, **current):
    from src.workflows.repo_repair_stop import validate_repository_stop_witness as verify
    return await verify(db, witness, **current)


async def complete_repository_stop_in_writer(db, witness, *, jobs):
    from src.workflows.repo_repair_stop import complete_repository_stop_in_writer as complete
    return await complete(db, witness, jobs=jobs)


def _source_digest(value):
    from src.security.trust_contract import canonical_digest
    return canonical_digest(value)


def _repository_policy_limits():
    """Stage the same canonical route bound used by the accounting owner.

    This reads configuration and must only run outside a SQL writer while the
    Source orchestration owns the configuration mutation fence.
    """
    from src.model_fabric.accounting import _policy_for_runtime
    from src.model_fabric.configuration import OPENROUTER_SETUP_V2_SCHEMA_VERSION, route_slot_for_task_class
    from src.model_fabric.caller_context import canonical_route_spec
    from src.workflows.job_runtime import DurableJobLeaseError
    configured, digest = _policy_for_runtime("strategist_agent")
    setup = configured.openrouter_setup
    bound = setup.request_cost_bound_microusd or setup.spend_ceiling_microusd
    if setup.schema_version == OPENROUTER_SETUP_V2_SCHEMA_VERSION:
        slot = route_slot_for_task_class(canonical_route_spec("strategist_agent").task_class)
        route = (setup.routes or {}).get(slot)
        if slot != "text" or route is None or not route.enabled:
            raise DurableJobLeaseError("original repository text route unavailable")
        bound = route.request_cost_bound_microusd
    if type(bound) is not int or bound <= 0 or not _SHA.fullmatch(digest):
        raise DurableJobLeaseError("original repository server bound required")
    return digest, bound


def _repository_original_limits(goal, policy):
    from src.goals.repository import deserialize_admission_budget
    from src.workflows.job_runtime import DurableJobLeaseError, _as_utc
    if goal is None:
        raise DurableJobLeaseError("original repository Goal required")
    budget = deserialize_admission_budget(goal)
    if budget is None:
        raise DurableJobLeaseError("original repository Goal budget required")
    def timestamp(value):
        return _as_utc(value).isoformat() if value is not None else None
    return {"original_goal_row_digest": _source_digest(goal.model_dump(mode="json")),
        "original_goal_id": goal.id, "original_goal_revision": goal.revision,
        "original_goal_owner_principal_id": goal.owner_principal_id,
        "original_goal_owner_session_id": goal.owner_session_id,
        "original_goal_limits": {"max_runtime_seconds": budget.max_runtime_seconds,
            "period_started_at": timestamp(budget.period_started_at),
            "period_expires_at": timestamp(budget.period_expires_at), "due_date": timestamp(goal.due_date)},
        "original_inference_policy_digest": policy[0], "original_server_bound_microusd": policy[1]}


def read_repository_inventory(run):
    """Closed immutable original limits, never reconstructed on recovery."""
    from src.workflows.job_runtime import DurableJobLeaseError
    original, work, _, _, binding, _ = read_repository_original(run)
    record = _repository_record(run, "repository:inventory:v1")
    expected_keys = {"schema", "identities", "max_records", "max_metadata_bytes_per_record",
        "original_limits", "original_limits_digest"}
    limits_keys = {"original_goal_row_digest", "original_goal_id", "original_goal_revision",
        "original_goal_owner_principal_id", "original_goal_owner_session_id", "original_goal_limits",
        "original_inference_policy_digest", "original_server_bound_microusd"}
    if not isinstance(record, dict) or set(record) != expected_keys:
        raise DurableJobLeaseError("original repository limits inventory required")
    limits = record["original_limits"]
    if (record["schema"] not in {"repository.checkpoint_inventory.v1", "repository.checkpoint_inventory.v2", "repository.checkpoint_inventory.v3"}
            or record["identities"] != repository_checkpoint_inventory(run, work)
            or type(record["max_records"]) is not int or record["max_records"] != 50
            or type(record["max_metadata_bytes_per_record"]) is not int or record["max_metadata_bytes_per_record"] != 16384
            or not isinstance(limits, dict) or set(limits) != limits_keys
            or record["original_limits_digest"] != _source_digest(limits)
            or any(not isinstance(limits[key], str) or not _SHA.fullmatch(limits[key]) for key in (
                "original_goal_row_digest", "original_inference_policy_digest"))
            or limits["original_goal_id"] != binding.goal_id
            or type(limits["original_goal_revision"]) is not int or limits["original_goal_revision"] != binding.goal_revision
            or limits["original_goal_owner_principal_id"] != binding.owner_principal_id
            or limits["original_goal_owner_session_id"] != binding.original_root_id
            or type(limits["original_server_bound_microusd"]) is not int or limits["original_server_bound_microusd"] <= 0):
        raise DurableJobLeaseError("original repository limits inventory changed")
    temporal = limits["original_goal_limits"]
    if (not isinstance(temporal, dict) or set(temporal) != {
            "max_runtime_seconds", "period_started_at", "period_expires_at", "due_date"}
            or type(temporal["max_runtime_seconds"]) is not int or not 1 <= temporal["max_runtime_seconds"] <= 900):
        raise DurableJobLeaseError("original repository Goal limits changed")
    for key in ("period_started_at", "period_expires_at", "due_date"):
        value = temporal[key]
        if value is not None:
            try:
                if not isinstance(value, str) or TaskProposalGroupV1.utc_timestamp(value).isoformat() != value:
                    raise ValueError()
            except (ValueError, TypeError):
                raise DurableJobLeaseError("original repository Goal temporal limit changed")
    return record


def _assert_repository_original_limits(run, goal, policy):
    from src.workflows.job_runtime import DurableJobLeaseError
    limits = read_repository_inventory(run)["original_limits"]
    if _repository_original_limits(goal, policy) != limits:
        raise DurableJobLeaseError("original repository Goal or inference policy changed")
    return limits


def _repository_goal_cutoff(limits, group):
    temporal = limits["original_goal_limits"]
    return min((group.issued_at + timedelta(seconds=temporal["max_runtime_seconds"]),
        *(TaskProposalGroupV1.utc_timestamp(temporal[key]) for key in ("period_expires_at", "due_date")
            if temporal[key] is not None)))


def _iterative_executor_preflight(service, compiled=None):
    from src.execution.repo_node import NodeRepoRepairExecutor
    from src.execution.repo_sandbox import LocalRepoRepairExecutor
    from src.workflows.job_runtime import DurableJobLeaseError
    if type(service.sandbox) is NodeRepoRepairExecutor:
        if compiled is None:
            raise DurableJobLeaseError("original compiled Node selection required")
        return service.sandbox.preflight({"repository_ref": str(service._workspace() / compiled.repository_path),
            "test_args": tuple(compiled.test_args), "allowed_paths": compiled.allowed_paths})
    if type(service.sandbox) is LocalRepoRepairExecutor:
        return service.sandbox.iterative_preflight()
    raise DurableJobLeaseError("fixed local iterative executor required")


def repository_checkpoint_inventory(run, work, *, _admission_schema=None):
    """Reserve the full fixed identity set before any repository effect.

    Private bodies/logs are artifacts; the canonical journal holds bounded
    digest metadata. Keeping the inventory on the original root is not a
    second capacity ledger.
    """
    original, *_ = read_repository_original(run)
    ids = ["repository:original:v1", "repository:inventory:v1", "repository:terminal:v1",
        "repository:stop-intent:v1"]
    for index in range(1, work.limits.max_iterations + 1):
        identity = iteration_identity(run.run_identity, original["repository_attempt_id"],
            _source_digest(original["original_input"]), index)
        ids.extend("repository:" + role + ":" + identity for role in (
            "prepared", "callback-start", "request", "response", "proposal", "patch",
            "approval", "execution", "cleanup", "readback", "accounting", "iteration"))
    ids.extend(("repo-repair-source-intent:" + run.run_identity,
                "repo-repair-source:" + run.run_identity,
                "repo-repair-execution-reservation", "repo-repair-execution-release"))
    reserved = _repository_record(run, "repository:inventory:v1")
    schema = _admission_schema if _admission_schema is not None else (
        reserved.get("schema") if reserved is not None else "repository.checkpoint_inventory.v1")
    if schema in {"repository.checkpoint_inventory.v2", "repository.checkpoint_inventory.v3"}:
        ids.append("repository:stop-uncertainty-successor:v1")
    elif schema != "repository.checkpoint_inventory.v1":
        from src.workflows.job_runtime import DurableJobLeaseError
        raise DurableJobLeaseError("original repository inventory version changed")
    if schema == "repository.checkpoint_inventory.v3":
        ids.extend("repository:producer:" + iteration_identity(run.run_identity,
            original["repository_attempt_id"], _source_digest(original["original_input"]), index)
            for index in range(1, work.limits.max_iterations + 1))
        ids.append("repository:physical-cleanup:v1")
    if len(ids) != len(set(ids)) or len(ids) > 50:
        from src.workflows.job_runtime import DurableJobTransitionError
        raise DurableJobTransitionError("repository fixed checkpoint capacity exceeded")
    return ids


def _repository_record(run, identity):
    from src.workflows.general_task_guard import _history
    from src.workflows.job_runtime import DurableJobLeaseError, _digest
    records = [item for item in _history(run) if item.get("checkpoint_id") == identity]
    if not records:
        return None
    if (len(records) != 1 or records[0].get("safe") is not True
            or not isinstance(records[0].get("payload"), dict)
            or records[0].get("state_digest") != _digest(records[0]["payload"])):
        raise DurableJobLeaseError("protected repository record changed")
    return records[0]["payload"]


class _RepositoryCompletionAppendPending:
    """Identity-only ticket for one of two Source-owned completion appends."""
    __slots__ = ("__weakref__",)


_COMPLETION_APPENDS = weakref.WeakKeyDictionary()
_COMPLETION_APPEND_STAGES = weakref.WeakKeyDictionary()


def _completion_append_state(pending, *, service=None, jobs=None, fence=None):
    from src.workflows.job_runtime import DurableJobLeaseError
    data = _COMPLETION_APPENDS.get(pending) if type(pending) is _RepositoryCompletionAppendPending else None
    if (data is None or data["thread"] != threading.get_ident()
            or data["task"] is not asyncio.current_task()
            or service is not None and data["service"] is not service
            or jobs is not None and data["jobs"] is not jobs
            or fence is not None and data["fence"] is not fence):
        raise DurableJobLeaseError("actual Source completion append ticket required")
    from src.workflows.repo_repair_source_recovery import assert_repository_completion_append_stage
    assert_repository_completion_append_stage(data["stage"], service=data["service"],
        jobs=data["jobs"], fence=data["fence"])
    return data


def _completion_append_pair(pending, *, service, jobs, fence):
    from src.workflows.job_runtime import DurableJobLeaseError
    if type(pending) is not tuple or len(pending) != 2:
        raise DurableJobLeaseError("exact two Source completion append tickets required")
    data = _completion_append_state(pending[0], service=service, jobs=jobs, fence=fence)
    if (data is not _completion_append_state(pending[1], service=service, jobs=jobs, fence=fence)
            or data["pending"] != pending):
        raise DurableJobLeaseError("Source completion append ticket pair changed")
    return data


@asynccontextmanager
async def stage_repository_completion_appends(service, jobs, *, stage, owner, fence):
    """Capture two exact Source constructor times in a genuine physical epoch."""
    from src.workflows.job_runtime import DurableJobLeaseError, _canonical, _utc_now
    from src.workflows.repo_repair_source_recovery import (
        assert_repository_completion_append_stage, repository_completion_append_stage,
        assert_repository_recovery_fence)
    assert_repository_completion_append_stage(stage, service=service, jobs=jobs, fence=fence)
    binding = repository_completion_append_stage(stage)
    assert_repository_recovery_fence(fence, service=service, jobs=jobs,
        job_id=binding["job_id"], owner=owner)
    if service.jobs is not jobs or binding["owner"] is not owner:
        raise DurableJobLeaseError("original completion append owner changed")
    if stage in _COMPLETION_APPEND_STAGES:
        raise DurableJobLeaseError("original completion append constructor already issued")
    from src.workflows.repo_repair_source_recovery import read_registered_repository_producer
    if (read_repository_inventory(binding["root"])["schema"] != "repository.checkpoint_inventory.v3"
            or _canonical(binding["root"].model_dump(mode="json")) != binding["root_json"]
            or read_registered_repository_producer(binding["root"], iteration_index=binding["iteration_index"])
                != binding["registration"]):
        raise DurableJobLeaseError("original completion append registration changed")
    root = json.loads(binding["root_json"])
    if (root["run_identity"] != binding["job_id"] or root["revision"] != binding["before_revision"]
            or root["checkpoint_receipts_json"] != binding["journal_prefix"]
            or binding["registration"]["iteration_id"] != binding["iteration_id"]):
        raise DurableJobLeaseError("original completion append Root changed")
    identities = tuple("repository:" + role + ":" + binding["iteration_id"] for role in ("cleanup", "readback"))
    history = json.loads(binding["journal_prefix"] or "[]")
    if (any(identity not in binding["inventory"] for identity in identities)
            or any(item.get("checkpoint_id") in identities for item in history)
            or len(history) + 2 > 50):
        raise DurableJobLeaseError("original completion append inventory changed")
    retry = binding["retry_metadata"]
    metadata = tuple({"checkpoint_id": identity, "safe": True, "created_at": _utc_now().isoformat()}
        for identity in identities) if retry is None else tuple(dict(item) for item in retry)
    if len(metadata) != 2:
        raise DurableJobLeaseError("original completion append metadata changed")
    for identity, item in zip(identities, metadata):
        try:
            valid_time = (isinstance(item["created_at"], str)
                and TaskProposalGroupV1.utc_timestamp(item["created_at"]).isoformat() == item["created_at"])
        except (KeyError, ValueError, TypeError):
            valid_time = False
        if (set(item) != {"checkpoint_id", "safe", "created_at"}
                or item.get("checkpoint_id") != identity or item.get("safe") is not True or not valid_time):
            raise DurableJobLeaseError("original completion append metadata changed")
    pending = (_RepositoryCompletionAppendPending(), _RepositoryCompletionAppendPending())
    data = {"service": service, "jobs": jobs, "stage": stage, "owner": owner, "fence": fence,
        "task": asyncio.current_task(), "thread": threading.get_ident(), "pending": pending,
        "metadata": tuple(_canonical(item) for item in metadata), "binding": binding,
        "root": root, "ordinal": 0, "payloads": None, "wrappers": None}
    for token in pending:
        _COMPLETION_APPENDS[token] = data
    _COMPLETION_APPEND_STAGES[stage] = True
    try:
        yield pending
    finally:
        for token in pending:
            _COMPLETION_APPENDS.pop(token, None)


def repository_completion_append_metadata(pending, *, service, jobs, fence):
    data = _completion_append_pair(pending, service=service, jobs=jobs, fence=fence)
    return tuple(MappingProxyType(json.loads(raw)) for raw in data["metadata"])


def bind_repository_completion_append_payloads(stage, pending, *, service, jobs, fence):
    """Derive the fixed payloads; caller mappings cannot select their contents."""
    from src.workflows.job_runtime import DurableJobLeaseError, _canonical, _digest
    data = _completion_append_pair(pending, service=service, jobs=jobs, fence=fence)
    if data["stage"] is not stage or data["payloads"] is not None or data["ordinal"]:
        raise DurableJobLeaseError("original completion append payload binding changed")
    binding = data["binding"]
    result, identity = binding["result"], binding["iteration_id"]
    outputs, manifest = result["outputs"], result["manifest"]
    prefix = "artifacts/repo-repair/model/iteration-" + identity
    metadata = [json.loads(raw) for raw in data["metadata"]]
    envelope = {"physical_projection": binding["physical_projection"],
        "source_completion_cas": binding["cas"], "source_append_metadata": metadata}
    envelope_raw = _canonical(envelope).encode()
    if len(envelope_raw) > 1048576:
        raise DurableJobLeaseError("original completion artifact exceeds its bound")
    diagnostics = {"iteration_id": identity,
        "stdout": outputs["pytest.stdout"].decode("utf-8", errors="replace"),
        "stderr": outputs["pytest.stderr"].decode("utf-8", errors="replace"),
        "stdout_raw_sha256": hashlib.sha256(outputs["pytest.stdout"]).hexdigest(),
        "stderr_raw_sha256": hashlib.sha256(outputs["pytest.stderr"]).hexdigest(),
        "cumulative_diff": outputs["diff.patch"].decode("utf-8", errors="strict"),
        "cumulative_diff_sha256": hashlib.sha256(outputs["diff.patch"]).hexdigest()}
    complete = result["original_producer_completion"]["outcome"] in {
        "completed_requested_checks", "completed_requested_check_failure"}
    payloads = ({"artifact_ref": "workspace-json:" + prefix + "-cleanup.json",
        "artifact_digest": hashlib.sha256(envelope_raw).hexdigest(), "cleanup_proven": True,
        "iteration_id": identity, "status": binding["status"], "source_completion_cas": binding["cas"]},
        {"artifact_ref": "workspace-json:" + prefix + "-readback.json", "artifact_digest": hashlib.sha256(outputs["readback.json"]).hexdigest(),
        "status": binding["status"], "manifest_digest": _source_digest(manifest),
        "patch_sha256": binding["execution"]["patch_sha256"],
        "diagnostics_artifact_ref": "workspace-json:" + prefix + "-diagnostics.json",
        "diagnostics_artifact_digest": hashlib.sha256(_canonical(diagnostics).encode()).hexdigest(),
        "command_results": _repository_command_results(manifest,
            node=binding["work"].language_profile == "test_node") if complete else [],
        "source_completion_cas": binding["cas"]})
    if any(len(_canonical(payload).encode()) > 16384 for payload in payloads):
        raise DurableJobLeaseError("original completion metadata exceeds its bound")
    data["payloads"] = tuple(_canonical(payload) for payload in payloads)
    data["wrappers"] = tuple(_canonical({**item, "payload": payload, "state_digest": _digest(payload)})
        for item, payload in zip(metadata, payloads))
    return tuple(json.loads(raw) for raw in data["payloads"])


def _append_repository_record(run, identity, payload, *, inventory, _completion_append=None):
    from src.workflows.general_task_guard import _history
    from src.workflows.job_runtime import DurableJobTransitionError, _canonical, _digest, _utc_now
    if _completion_append is not None:
        data = _completion_append_state(_completion_append)
        ordinal = data["ordinal"]
        if (ordinal >= 2 or data["pending"][ordinal] is not _completion_append
                or data["payloads"] is None or _canonical(payload) != data["payloads"][ordinal]
                or tuple(inventory) != tuple(data["binding"]["inventory"])):
            raise DurableJobTransitionError("original completion append sequence or payload changed")
        expected_root = dict(data["root"])
        expected_history = json.loads(data["binding"]["journal_prefix"] or "[]")
        if ordinal:
            expected_history.append(json.loads(data["wrappers"][0]))
            expected_root["checkpoint_receipts_json"] = _canonical(expected_history)
        wrapper = json.loads(data["wrappers"][ordinal])
        if (identity != wrapper["checkpoint_id"] or run.model_dump(mode="json") != expected_root
                or identity not in inventory or len(expected_history) >= 50):
            raise DurableJobTransitionError("original completion append Root or prefix changed")
        expected_history.append(wrapper)
        run.checkpoint_receipts_json = _canonical(expected_history)
        data["ordinal"] += 1
        return True
    if identity not in inventory:
        raise DurableJobTransitionError("unreserved repository checkpoint identity")
    existing = _repository_record(run, identity)
    if existing is not None:
        if existing != payload:
            raise DurableJobTransitionError("immutable repository checkpoint changed")
        return False
    history = _history(run)
    if len(history) >= 50 or len(set(inventory) | {item["checkpoint_id"] for item in history}) > 50:
        raise DurableJobTransitionError("repository checkpoint history is full; never evict")
    if len(_canonical(payload).encode()) > 16384:
        raise DurableJobTransitionError("repository metadata checkpoint exceeds its reserved bound")
    history.append({"checkpoint_id": identity, "safe": True, "payload": payload,
        "state_digest": _digest(payload), "created_at": _utc_now().isoformat()})
    run.checkpoint_receipts_json = _canonical(history)
    return True


async def append_repository_release_in_writer(db, jobs, run, *, original, work, witness, outcome_status):
    """Same-terminal-transaction release of the original canonical hold."""
    from src.workflows.job_runtime import DurableJobLeaseError, _utc_now
    from src.workflows.repo_repair_stop import assert_repository_stop_witness, _accounting
    from src.workflows.general_task_guard import _assert_repository_child_final_witness_shape
    if outcome_status not in {"succeeded", "failed", "cancelled"}:
        raise DurableJobLeaseError("original repository terminal release outcome required")
    if outcome_status == "succeeded":
        _assert_repository_child_final_witness_shape(witness)
        assert_repository_canonical_source(witness._source_binding._final_source)
        if witness.wait_witness.repository_job_id != run.run_identity:
            raise DurableJobLeaseError("original repository final release witness changed")
        accounting_context = {"group": read_repository_original(run)[3], "run": run,
            "work": work, "original": original}
    else:
        assert_repository_stop_witness(witness)
        if witness.closure.repository_job_id != run.run_identity:
            raise DurableJobLeaseError("original repository stop release witness changed")
        accounting_context = witness.context
    with db.no_autoflush:
        await _accounting(db, accounting_context)
    inventory = repository_checkpoint_inventory(run, work)
    reserved = _repository_record(run, "repository:inventory:v1")
    if reserved is None or reserved.get("identities") != inventory:
        raise DurableJobLeaseError("original repository release identity was not reserved before effects")
    hold = jobs._repo_repair_reservation_state(run)
    if (hold is None or hold.get("status") != "held"
            or not jobs._repo_repair_reservation_matches(hold, job_id=run.run_identity,
                attempt_id=original["repository_attempt_id"], fence=run.fencing_token,
                authority_digest=run.authority_digest)
            or hold.get("execution_deadline_at") != original["original_deadline_at"]):
        raise DurableJobLeaseError("exact original held repository reservation required")
    payload = {"kind": "repo_repair_execution_reservation", "status": "released",
        "job_id": run.run_identity, "attempt_id": original["repository_attempt_id"],
        "fence": run.fencing_token, "authority_digest": run.authority_digest,
        "execution_deadline_at": hold["execution_deadline_at"], "outcome_status": outcome_status,
        "cleanup_proven": True, "readback_verified": True, "operator_visible": True,
        "recorded_at": _utc_now().isoformat()}
    _append_repository_record(run, "repo-repair-execution-release", payload, inventory=inventory)


async def repository_source_root(db, *, job_id, owner):
    """Absence is legacy; malformed source evidence is never a fallback."""
    from sqlalchemy import select
    from src.db.models import WorkflowRunState
    from src.workflows.general_task_guard import _history
    from src.workflows.job_runtime import DurableJobLeaseError
    run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))
    if run is None:
        raise DurableJobLeaseError("repository root unavailable")
    if (run.owner_principal_id, run.operator_session_id) != (owner.principal_id, owner.session_id):
        raise DurableJobLeaseError("original repository owner changed")
    if not any(item.get("checkpoint_id") == "repository:original:v1" for item in _history(run)):
        return False
    read_repository_original(run)
    return True


async def prepare_repository_native_source(general_task_service, jobs, binding, *, child_owner, principal):
    """Prepare the one actual original root, before invoking any adapter."""
    from math import ceil
    from sqlalchemy import select
    from src.db.models import WorkflowRunState, WorkBoardAttempt, Goal
    from src.work_board.contracts import WorkBoardOwner, WorkBoardInputArtifactCreate, WorkBoardTaskCreate
    from src.work_board.input_artifacts import prepare_input_artifact
    from src.work_board.general_task_native import publish_positive_claim
    from src.workflows.general_task_guard import assert_general_task_child_current
    from src.workflows.job_runtime import (DurableJobIdentity, DurableJobSpec, _binding,
        _digest, _utc_now, _as_utc, DurableJobLeaseError)
    from src.workflows.repo_repair import RepoRepairService, RepoWorkInput
    from src.goals.repository import deserialize_admission_budget
    source = general_task_service.repository_source_service
    if type(source) is not RepoRepairService or not general_task_service.started:
        raise DurableJobLeaseError("actual current repository source service required")
    if (not principal or not principal.authenticated or principal.revoked
            or principal.principal_id != binding.owner_principal_id
            or principal.operator_session_id != binding.original_root_id):
        raise DurableJobLeaseError("original authenticated repository operator required")
    source.jobs = jobs
    key = _binding(owner_principal_id=binding.owner_principal_id, goal_id=binding.goal_id,
        goal_revision=binding.goal_revision, idempotency_scope="original-repository-child",
        dedupe_key=binding.invocation_id)
    async with jobs._session() as db:
        mapped = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.idempotency_binding == key))
        if mapped is not None:
            original, work, *_ = read_repository_original(mapped)
            if original["native_binding"] != binding.model_dump(mode="json"):
                raise DurableJobLeaseError("permanent repository mapping binding changed")
            await assert_general_task_child_current(db, await jobs._fetch(db, binding.invocation_id))
            root_id = mapped.run_identity
            return await _prepared_repository_projection(db, mapped, owner=WorkBoardOwner(
                principal_id=binding.owner_principal_id, session_id=binding.original_root_id))
    pending = await jobs.get_job(binding.invocation_id)
    if (pending is None or pending["status"] not in {"accepted", "queued"}
            or pending["attempt_count"] or pending["lease"]["fencing_token"]
            or pending["lease"]["owner"] or pending["effects"]):
        raise DurableJobLeaseError("original unclaimed repository child required")
    cutoff = min(binding.original_deadline_at, binding.native_deadline_at)
    remaining = (cutoff - _utc_now()).total_seconds()
    if remaining <= 0:
        raise DurableJobLeaseError("original repository cutoff expired")
    if pending["status"] == "accepted":
        await jobs.queue_job(binding.invocation_id)
    child = await jobs.claim_job(binding.invocation_id, owner=child_owner, lease_seconds=max(1, ceil(remaining)))
    await publish_positive_claim(jobs, binding, child_owner=child_owner,
        child_fence=child["lease"]["fencing_token"])
    from src.work_board.general_task_runtime_artifacts import read_current_native_tool_input, read_current_native_envelope
    async with jobs._session() as db:
        current_child = await jobs._fetch(db, binding.invocation_id)
        private = await read_current_native_tool_input(db, current_child)
        work = RepoWorkInput.model_validate(private.inputs)
        goal = await db.get(Goal, binding.goal_id)
        budget = deserialize_admission_budget(goal)
        capacity = budget.max_outstanding_jobs if budget else 1
        from src.db.models import WorkBoardTask
        parent = await jobs._fetch(db, binding.parent_job_id)
        parent_task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
        parent_attempt = await db.get(WorkBoardAttempt, binding.attempt_id)
        original_group = (await read_current_native_envelope(db, parent, parent_task, parent_attempt)).proposal_group
    owner = WorkBoardOwner(principal_id=binding.owner_principal_id, session_id=binding.original_root_id)
    repository = general_task_service.repository
    async with source.session_factory() as db:
        artifact = await prepare_input_artifact(db, owner, WorkBoardInputArtifactCreate(schema_version=1,
            capability_id="engineering.repo-repair.v1", goal_id=binding.goal_id, goal_revision=binding.goal_revision,
            input=work.model_dump(mode="json"), idempotency_key="repository-source:" + binding.invocation_id))
        created = await repository.create_task(db, owner, WorkBoardTaskCreate(
            title="Repair " + work.repository_ref[:100], goal_id=binding.goal_id, goal_revision=binding.goal_revision,
            capability_id="engineering.repo-repair.v1", input_artifact_id=artifact.artifact_id,
            idempotency_scope="original-repository-child", idempotency_key=binding.invocation_id,
            status="todo", requires_review=False))
        await db.commit()
    async with source.session_factory() as db:
        ready = await repository.promote_task_ready(db, created.task.task_id,
            expected_revision=created.task.task_revision, actor_principal_id=owner.principal_id,
            actor_session_id=owner.session_id)
        await db.commit()
    async with source.session_factory() as db:
        claimed = await repository.claim_ready_task(db, created.task.task_id,
            expected_revision=ready.task.task_revision, lease_owner=child_owner, lease_seconds=max(1, ceil(remaining)))
        await db.commit()
    async with source.session_factory() as db:
        check, scope = await prepare_repository_original_admission(source, db,
            native_invocation_id=binding.invocation_id, repository_task_id=created.task.task_id,
            repository_attempt_id=claimed.attempt.attempt_id)
    inputs = {"schema_version": 1, "capability_id": "engineering.repo-repair.v1",
        "input": work.model_dump(mode="json")}
    root_id = "repository:" + _source_digest([binding.invocation_id, "original-root"])
    deadline = min(cutoff, _as_utc(claimed.attempt.lease_expires_at),
        _utc_now() + timedelta(seconds=work.limits.max_total_seconds))
    if budget is None:
        raise DurableJobLeaseError("original repository Goal budget required")
    deadline = min(deadline, original_group.issued_at + timedelta(seconds=budget.max_runtime_seconds),
        *(_as_utc(value) for value in (goal.due_date, budget.period_expires_at) if value is not None))
    spec = DurableJobSpec(identity=DurableJobIdentity(root_id, "user", owner.principal_id,
        "engineering.repo-repair.v1", "1", "original-repository-child", binding.invocation_id),
        inputs=inputs, session_id=owner.session_id, operator_session_id=owner.session_id,
        goal_id=binding.goal_id, goal_revision=binding.goal_revision, deadline_at=deadline,
        resource_claims=("repo-repair-execution",), max_attempts=1, max_outstanding_jobs=capacity,
        budget_microusd=work.limits.max_cost_microusd, run_fingerprint=_digest(inputs),
        declared_authority={"principal": owner.principal_id, "owner_kind": "user", "session_id": owner.session_id,
            "capability_id": "engineering.repo-repair.v1", "budget_microusd": work.limits.max_cost_microusd})
    async with scope():
        admitted = await jobs.admit_job(spec, admission_authority_check=check)
    async with source.session_factory() as db:
        await repository.link_attempt_workflow_run(db, created.task.task_id, claimed.attempt.attempt_id,
            workflow_run_id=root_id, expected_revision=claimed.task.task_revision,
            board_fence=claimed.attempt.fencing_token, lease_owner=claimed.attempt.lease_owner,
            workflow_projection=admitted, expected_identity={
                "job_id": root_id, "owner_kind": "user", "owner_principal_id": owner.principal_id,
                "service_id": spec.service_id, "operator_session_id": owner.session_id, "session_id": owner.session_id,
                "goal_id": binding.goal_id, "goal_revision": binding.goal_revision,
                "job_kind": spec.identity.job_kind, "capability_version": "1", "input_digest": _digest(inputs),
                "authority_digest": _digest(spec.declared_authority), "run_fingerprint": spec.run_fingerprint,
                "idempotency_scope": spec.identity.idempotency_scope, "idempotency_key": binding.invocation_id})
        await db.commit()
    await jobs.queue_job(root_id)
    await jobs.claim_job(root_id, owner=child_owner, lease_seconds=max(1, ceil((deadline - _utc_now()).total_seconds())))
    from src.work_board.dispatcher import _reserve_repo_repair_execution_capacity
    async with jobs._session() as db:
        root = await jobs._fetch(db, root_id)
    source._iterative_lanes[root_id] = await _reserve_repo_repair_execution_capacity(
        jobs=jobs, job_id=root_id, attempt_id=claimed.attempt.attempt_id,
        workspace_root=str(source._workspace()), owner=root.lease_owner,
        fencing_token=root.fencing_token, authority_digest=root.authority_digest,
        execution_deadline_at=deadline.isoformat(), expected_revision=root.revision)
    return await prepare_repository_iteration(source, jobs, job_id=root_id, owner=owner, iteration_index=1)


async def _repository_precontact(service, jobs, *, job_id, owner):
    """Stage physical evidence before a caller enters its SQL writer."""
    from sqlalchemy import select
    from src.db.models import WorkBoardTask, WorkBoardAttempt, Goal, OperatorSession, WorkBoardInputArtifact
    from src.workflows.repo_repair import RepoRepairService
    from src.workflows.general_task_guard import assert_general_task_child_current, read_manifest
    from src.work_board.general_task_runtime_artifacts import verify_general_task_manifest
    from src.workflows.job_runtime import DurableJobLeaseError, _as_utc, _utc_now, _canonical
    from src.workflows.general_task_accounting import validate_group_owner
    if type(service) is not RepoRepairService:
        raise DurableJobLeaseError("actual current repository source owner required")
    async with jobs._session() as db:
        run = await jobs._fetch(db, job_id)
        original, work, compiled, group, binding, source = read_repository_original(run)
        if _repository_record(run, "repository:stop-intent:v1") is not None:
            raise DurableJobLeaseError("original repository stop prevents further execution")
        if (run.owner_principal_id, run.operator_session_id) != (owner.principal_id, owner.session_id):
            raise DurableJobLeaseError("original repository owner changed")
        goal = await db.get(Goal, group.goal_id)
        original_limits = _assert_repository_original_limits(run, goal, _repository_policy_limits())
        child = await jobs._fetch(db, binding.invocation_id)
        await assert_general_task_child_current(db, child)
        parent = await jobs._fetch(db, binding.parent_job_id)
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
        attempt = await db.get(WorkBoardAttempt, binding.attempt_id)
        envelope = await verify_general_task_manifest(db, parent, task, attempt, read_manifest(parent))
        if envelope.repository_source != source or envelope.proposal_group != group:
            raise DurableJobLeaseError("original scoped Task source changed")
        authority = await service._resolve_canonical_authority(db, owner=owner,
            work_board_task_id=original["repository_task_id"], work_board_attempt_id=original["repository_attempt_id"],
            workflow_run_id=job_id, goal_id=run.goal_id, goal_revision=run.goal_revision,
            lease_owner=run.lease_owner, fencing_token=run.fencing_token)
        if (run.status != "running" or child.status != "running"
                or run.failure_reason is not None or child.failure_reason is not None
                or _as_utc(run.deadline_at) <= _utc_now()
                or _as_utc(child.lease_expires_at) > _as_utc(run.deadline_at) and
                   _as_utc(child.lease_expires_at) > binding.native_deadline_at):
            raise DurableJobLeaseError("original running repository claims required")
        await validate_group_owner(db, group)
        originals = [run, child, parent, task, attempt, authority.task, authority.attempt,
            goal, await db.get(OperatorSession, group.owner_session_id),
            await db.get(WorkBoardInputArtifact, authority.task.input_artifact_id)]
        from sqlalchemy import inspect as inspect_mapper
        rows = []
        for row in originals:
            keys = tuple(getattr(row, column.key) for column in inspect_mapper(type(row)).primary_key)
            rows.append((type(row), keys[0] if len(keys) == 1 else keys,
                _canonical(row.model_dump(mode="json"))))
        source_facts = json.loads(service._read_private_artifact(source.source_artifact_ref,
            expected_digest=source.source_artifact_digest))
        service.recheck_task_source_snapshot(work, source_facts)
        return {"run": run, "child": child, "parent": parent, "original": original,
            "work": work, "compiled": compiled, "group": group, "binding": binding,
            "source": source, "authority": authority, "rows": rows, "original_limits": original_limits}


async def _recheck_repository_sql(db, context, *, allow_stop_cleanup=False):
    """Current original SQL rows only; never reopen an artifact in the writer."""
    from src.workflows.job_runtime import DurableJobLeaseError, _canonical, _as_utc, _utc_now
    from src.workflows.general_task_accounting import validate_group_owner
    for model, key, expected in context["rows"]:
        row = await db.get(model, key, populate_existing=True)
        matches = row is not None and _canonical(row.model_dump(mode="json")) == expected
        if not matches and allow_stop_cleanup and row is not None:
            old = json.loads(expected)
            actual = row.model_dump(mode="json")
            if getattr(row, "run_identity", None) == context["run"].run_identity:
                stop = _repository_record(row, "repository:stop-intent:v1")
                history = json.loads(row.checkpoint_receipts_json or "[]")
                known = {"revision", "updated_at", "checkpoint_receipts_json"}
                matches = bool(stop and stop.get("native_binding_digest") == _source_digest(
                    context["binding"].model_dump(mode="json"))
                    and row.revision == old["revision"] + 1
                    and [item for item in history if item["checkpoint_id"] != "repository:stop-intent:v1"] ==
                        json.loads(old["checkpoint_receipts_json"] or "[]")
                    and {k: v for k, v in actual.items() if k not in known} ==
                        {k: v for k, v in old.items() if k not in known})
            elif type(row).__name__ == "OperatorSession":
                known = {"last_seen_at", "idle_expires_at", "updated_at"}
                matches = ({k: v for k, v in actual.items() if k not in known} ==
                    {k: v for k, v in old.items() if k not in known}
                    and row.revoked_at is None and _as_utc(row.idle_expires_at) > _utc_now()
                    and _as_utc(row.absolute_expires_at) > _utc_now())
        if not matches:
            raise DurableJobLeaseError("original repository preparation authority changed")
    current_root = await db.get(type(context["run"]), context["run"].id, populate_existing=True)
    stopping = _repository_record(current_root, "repository:stop-intent:v1") is not None
    if stopping and not allow_stop_cleanup:
        raise DurableJobLeaseError("original repository stop prevents further execution")
    if allow_stop_cleanup and stopping:
        # Only the already-owned, positively closed physical producer calls
        # this branch. It writes cleanup evidence and grants no new effect.
        return
    await validate_group_owner(db, context["group"])
    for row in (context["run"], context["child"], context["authority"].attempt):
        if (_as_utc(row.lease_expires_at) is None or _as_utc(row.lease_expires_at) <= _utc_now()
                or getattr(row, "cancel_requested_at", None) is not None):
            raise DurableJobLeaseError("original repository lease or cancellation changed")
    if _as_utc(context["run"].deadline_at) <= _utc_now():
        raise DurableJobLeaseError("original repository cutoff expired")


async def _repository_remaining(db, context, *, allow_exhausted_readback=False):
    from sqlalchemy import select
    from src.db.models import InferenceCostReservation
    from src.workflows.general_task_accounting import entry_for, reservation_liability
    from src.workflows.inference_accounting import InferenceAccountingError
    rows = list((await db.execute(select(InferenceCostReservation))).scalars())
    members = []
    root_members = []
    group = context["group"]
    for row in rows:
        entry = entry_for(row)
        if entry and entry["group"]["group_id"] == group.group_id:
            if entry["group"] != group.model_dump(mode="json"):
                raise InferenceAccountingError("general_task_group_conflict")
            members.append(row)
        if row.job_id == context["run"].run_identity:
            if not entry or entry["role"] != "repository_iteration" or entry["group"] != group.model_dump(mode="json"):
                raise InferenceAccountingError("repository_original_member_invalid")
            root_members.append(row)
    if any(row.state == "unknown" for row in members + root_members):
        raise InferenceAccountingError("repository_original_unknown_liability")
    calls = group.max_inference_calls - len(members)
    cost = min(group.max_cost_microusd - sum(map(reservation_liability, members)),
        context["work"].limits.max_cost_microusd - sum(map(reservation_liability, root_members)))
    if calls < 0 or cost < 0 or (not allow_exhausted_readback and (calls == 0 or cost == 0)):
        raise InferenceAccountingError("repository_original_budget_exhausted")
    return {"remaining_inference_calls": calls, "remaining_cost_microusd": cost}


async def prepare_repository_iteration(service, jobs, *, job_id, owner, iteration_index):
    from src.model_fabric.effective_policy import configuration_mutation_lock
    from src.workflows.repo_repair_stop import repository_automatic_limit_reason, stop_repository_root
    async with configuration_mutation_lock:
        reason = await repository_automatic_limit_reason(service, jobs, job_id=job_id, owner=owner)
    if reason is not None:
        stopped = await stop_repository_root(service, jobs, job_id=job_id, owner=owner,
            general_task_service=None, reason=reason)
        return {"repository_job_id": job_id, "awaiting_repository_consent": False,
            "stop_pending": stopped["pending"], "stop_reason": reason,
            "recovery_action": "repository_stop_pending" if stopped["pending"] else "original_" + reason,
            "no_learning": True}
    return await _prepare_repository_iteration(service, jobs, job_id=job_id, owner=owner,
        iteration_index=iteration_index)


async def _prepare_repository_iteration(service, jobs, *, job_id, owner, iteration_index):
    from src.model_fabric.effective_policy import configuration_mutation_lock
    from src.workflows.repo_repair import RepoRepairModelOutput, _canonical_bytes
    from src.workflows.job_runtime import _as_utc, _digest, _canonical, DurableJobLeaseError
    from src.work_board.repository import _begin_sqlite_immediate
    from src.llm_runtime import build_model_kwargs
    from config.settings import settings
    async with configuration_mutation_lock:
        config = _assert_task_publication_configuration(service)
        context = await _repository_precontact(service, jobs, job_id=job_id, owner=owner)
        run, original, work = context["run"], context["original"], context["work"]
        if type(iteration_index) is not int or not 1 <= iteration_index <= work.limits.max_iterations:
            raise DurableJobLeaseError("original repository iteration bound exceeded")
        iteration = iteration_identity(job_id, original["repository_attempt_id"],
            _source_digest(original["original_input"]), iteration_index)
        if _repository_record(run, "repository:callback-start:" + iteration) is not None:
            raise DurableJobLeaseError("original callback has already started; never prepare replay")
        existing = _repository_record(run, "repository:prepared:" + iteration)
        if existing is not None:
            return await _prepared_repository_projection(None, run, owner=owner)
        prior_manifest = None
        prior_diagnostics = None
        if iteration_index != 1:
            prior_id = iteration_identity(job_id, original["repository_attempt_id"],
                _source_digest(original["original_input"]), iteration_index - 1)
            prior_cleanup = _repository_record(run, "repository:cleanup:" + prior_id)
            prior_readback = _repository_record(run, "repository:readback:" + prior_id)
            if (prior_cleanup is None or prior_readback is None
                    or prior_cleanup.get("cleanup_proven") is not True
                    or prior_cleanup.get("iteration_id") != prior_id
                    or prior_cleanup.get("status") != "failed" or prior_readback.get("status") != "failed"):
                raise DurableJobLeaseError("positive failed prior iteration cleanup/readback required")
            prior_manifest = json.loads(service._read_private_artifact(prior_readback["artifact_ref"],
                expected_digest=prior_readback["artifact_digest"]))
            prior_diagnostics = json.loads(service._read_private_artifact(prior_readback["diagnostics_artifact_ref"],
                expected_digest=prior_readback["diagnostics_artifact_digest"]))
            if (_source_digest(prior_manifest) != prior_readback["manifest_digest"]
                    or prior_manifest.get("status") != "failed"
                    or prior_diagnostics.get("iteration_id") != prior_id
                    or prior_manifest.get("diff_sha256") != prior_diagnostics.get("cumulative_diff_sha256")
                    or hashlib.sha256(prior_diagnostics["cumulative_diff"].encode()).hexdigest()
                        != prior_manifest.get("diff_sha256")):
                raise DurableJobLeaseError("actual prior tested cumulative diff readback changed")
        if job_id not in service._iterative_lanes:
            from src.work_board.dispatcher import _reserve_repo_repair_execution_capacity
            service._iterative_lanes[job_id] = await _reserve_repo_repair_execution_capacity(
                jobs=jobs, job_id=job_id, attempt_id=original["repository_attempt_id"],
                workspace_root=str(service._workspace()), owner=run.lease_owner,
                fencing_token=run.fencing_token, authority_digest=run.authority_digest,
                execution_deadline_at=original["original_deadline_at"], expected_revision=run.revision)
        # The actual packet publisher records intent, private artifact, row,
        # and verified readback on this same original root.
        packet = await service.inspect_and_prepare(context["compiled"], owner=owner,
            work_board_task_id=original["repository_task_id"], work_board_attempt_id=original["repository_attempt_id"],
            workflow_run_id=job_id, goal_id=run.goal_id, goal_revision=run.goal_revision, input_digest=run.input_digest)
        context = await _repository_precontact(service, jobs, job_id=job_id, owner=owner)
        run = context["run"]
        import asyncio
        preflight = await asyncio.to_thread(_iterative_executor_preflight, service, context["compiled"])
        if not preflight.ok or not preflight.posture_digest:
            raise DurableJobLeaseError("fixed iterative process supervision is blocked")
        packet_payload = json.loads(service._read_private_artifact(packet.artifact_ref,
            expected_digest=packet.artifact_sha256))
        input_tree_digest = packet_payload["base_snapshot_sha256"]
        stdout = stderr = ""
        if prior_manifest is not None:
            diff = prior_diagnostics["cumulative_diff"]
            if await service._scan_secrets(diff) != diff:
                raise DurableJobLeaseError("fresh source scan blocks prior cumulative patch secrets")
            tested_metadata = (prior_manifest["tested_file_hash_metadata"] if work.language_profile == "test_node"
                else prior_manifest["publication_test_input"]["tested_files"])
            metadata_scope = {}
            if work.language_profile == "test_node":
                selected_paths = sorted(item["path"] for item in packet_payload["files"])
                if selected_paths != sorted(context["compiled"].source_paths):
                    raise DurableJobLeaseError("original acknowledged source selection changed")
                tested_metadata = [item for item in tested_metadata if item["path"] in selected_paths]
                metadata_scope = {"tested_file_hash_metadata_scope": {
                    "kind": "original_acknowledged_source_paths_present_in_tested_tree",
                    "selected_source_paths": selected_paths,
                    "missing_selected_source_paths": sorted(set(selected_paths) -
                        {item["path"] for item in tested_metadata}),
                    "full_metadata_retained_in_physical_readback": True}}
            packet_payload = {**packet_payload,
                "source_bytes_provenance": "original_acknowledged_frozen_base",
                "prior_tested_iteration": {"iteration_id": prior_diagnostics["iteration_id"],
                    "cumulative_diff": diff, "cumulative_diff_sha256": prior_manifest["diff_sha256"],
                    "diff_provenance": "actual_supervised_checkout_cumulative_diff_against_original_base",
                    "tested_tree_digest": prior_manifest["after_digest"],
                    "tested_file_hash_metadata": tested_metadata,
                    "hashes_are_file_bodies": False, **metadata_scope}}
            input_tree_digest = prior_manifest["after_digest"]
            stdout, stderr = prior_diagnostics["stdout"], prior_diagnostics["stderr"]
        candidate = await service.prepare_iteration_egress(iteration_id=iteration,
            source_payload=packet_payload, stdout=stdout, stderr=stderr)
        kwargs = build_model_kwargs(temperature=0.2, max_tokens=4096, runtime_path="strategist_agent")
        model = service.model_factory(**kwargs)
        response_format = {"type": "json_schema", "json_schema": {
            "name": "seraph_repo_repair_proposal", "strict": True,
            "schema": RepoRepairModelOutput.model_json_schema()}}
        finalized = service.finalize_iteration_egress(candidate, model=model, response_format=response_format)
        prefix = "artifacts/repo-repair/model/iteration-" + iteration
        body_ref, body_digest = service._write_private_artifact(prefix + "-request.json", _canonical(finalized["request_body"]).encode())
        egress_ref, egress_digest = service._write_private_artifact(prefix + "-egress.json", _canonical_bytes(finalized["envelope"]))
        child, authority = context["child"], context["authority"]
        record = {"schema": "seraph.repository_precontact_preparation.v1",
            "iteration_index": iteration_index, "iteration_id": iteration, "input_tree_digest": input_tree_digest,
            "original_checkpoint_digest": _digest(original), "native_binding_digest": _source_digest(original["native_binding"]),
            "native_child_owner": child.lease_owner, "native_child_fence": child.fencing_token,
            "native_child_lease_expires_at": _as_utc(child.lease_expires_at).isoformat(),
            "repository_task_id": original["repository_task_id"], "repository_attempt_id": original["repository_attempt_id"],
            "repository_owner": run.lease_owner, "repository_fence": run.fencing_token,
            "repository_lease_expires_at": _as_utc(run.lease_expires_at).isoformat(),
            "repository_attempt_owner": authority.attempt.lease_owner, "repository_attempt_fence": authority.attempt.fencing_token,
            "repository_attempt_lease_expires_at": _as_utc(authority.attempt.lease_expires_at).isoformat(),
            "source_packet_id": packet.packet_id, "source_packet_artifact_digest": packet.artifact_sha256,
            "source_manifest_digest": packet.source_manifest_sha256,
            "original_source_binding_digest": context["source"].binding_digest,
            "original_group_digest": _source_digest(context["group"].model_dump(mode="json")),
            "executor_profile_digest": _source_digest(config.model_dump(mode="json")),
            "executor_posture_digest": preflight.posture_digest,
            "canonical_selector_digest": _source_digest(settings.repo_sandbox.model_dump(mode="json")),
            "request_body_artifact_ref": body_ref, "request_body_digest": finalized["serialized_request_sha256"],
            "request_route_digest": finalized["request_route_digest"],
            "egress_envelope_artifact_ref": egress_ref, "egress_envelope_digest": egress_digest,
            "diagnostics_digest": finalized["diagnostics_sha256"], "redaction_version": finalized["redaction_version"],
            "combined_input_bytes": finalized["combined_input_bytes"], "original_deadline_at": original["original_deadline_at"]}
        record["preparation_digest"] = _source_digest(record)
        async with jobs._session() as db:
            await _begin_sqlite_immediate(db)
            await _recheck_repository_sql(db, context)
            await _repository_remaining(db, context)
            current = await jobs._fetch(db, job_id)
            _append_repository_record(current, "repository:prepared:" + iteration, record,
                inventory=repository_checkpoint_inventory(current, work))
            current.revision += 1
            await db.commit()
        async with jobs._session() as db:
            return await _prepared_repository_projection(db, await jobs._fetch(db, job_id), owner=owner)


async def _prepared_repository_projection(db, run, *, owner):
    from src.workflows.job_runtime import DurableJobLeaseError
    original, work, *_ = read_repository_original(run)
    if (run.owner_principal_id, run.operator_session_id) != (owner.principal_id, owner.session_id):
        raise DurableJobLeaseError("original repository projection owner changed")
    records = []
    for index in range(1, work.limits.max_iterations + 1):
        iteration = iteration_identity(run.run_identity, original["repository_attempt_id"],
            _source_digest(original["original_input"]), index)
        prepared = _repository_record(run, "repository:prepared:" + iteration)
        if prepared is not None:
            records.append((prepared, _repository_record(run, "repository:callback-start:" + iteration)))
    if not records:
        raise DurableJobLeaseError("original repository preparation unavailable")
    prepared, started = records[-1]
    if (prepared.get("schema") != "seraph.repository_precontact_preparation.v1"
            or prepared.get("original_checkpoint_digest") != _source_digest(original)
            or prepared.get("preparation_digest") != _source_digest({key: value for key, value in prepared.items()
                if key != "preparation_digest"})):
        raise DurableJobLeaseError("original prepared repository identity changed")
    return {"awaiting_repository_consent": started is None, "native_execution": True,
        "native_child_id": original["native_binding"]["invocation_id"], "repository_job_id": run.run_identity,
        "iteration_index": prepared["iteration_index"], "iteration_id": prepared["iteration_id"],
        "preparation_digest": prepared["preparation_digest"], "contact_state": "not_started" if started is None else "started",
        "source_preview_path": "/api/workflows/repo-repair/" + run.run_identity + "/source-preview",
        "verified": False, "no_learning": True}


async def _repository_discovery_metadata(db, run, *, owner, service=None):
    """One SQL-only stop/discovery projection; grants no action or private read."""
    from sqlalchemy import select
    from src.db.models import (WorkflowRunState, WorkBoardTask, WorkBoardAttempt,
        WorkBoardInputArtifact, OperatorSession, Goal, RepoRepairProposal, InferenceCostReservation)
    from src.work_board.contracts import RepositoryReview, RepositoryNativeLimitEvidenceV1, RepositoryNativeStopClosureV1
    from src.workflows.general_task_guard import child_binding, read_general_task_native_cancel
    from src.workflows.job_runtime import DurableJobRepository, DurableJobLeaseError, _binding, _as_utc, _utc_now
    from src.workflows.repo_repair_stop import AUTOMATIC_REASONS, _static, _key, _accounting
    original, work, _, group, binding, task_source = read_repository_original(run)
    if (run.owner_principal_id, run.operator_session_id) != (owner.principal_id, owner.session_id):
        raise DurableJobLeaseError("original repository discovery owner changed")
    stop = _repository_record(run, "repository:stop-intent:v1")
    if stop is None:
        projection = await _prepared_repository_projection(db, run, owner=owner)
        RepositoryReview.model_validate({key: projection[key] for key in RepositoryReview.model_fields})
        return projection, None
    inventory = read_repository_inventory(run)
    reason = stop.get("stop_reason")
    automatic = reason in AUTOMATIC_REASONS
    expected_stop = {"schema", "stop_reason", "native_binding_digest", "static_rows",
        "snapshot_artifact_ref", "snapshot_artifact_digest", "no_learning"}
    if automatic:
        expected_stop |= {"limit_evidence", "limit_evidence_digest"}
    if (set(stop) != expected_stop or stop.get("schema") != "repository.stop_intent.v1"
            or reason not in {"operator_cancelled", "iterations_exhausted"} | AUTOMATIC_REASONS
            or stop.get("native_binding_digest") != _source_digest(binding.model_dump(mode="json"))
            or stop.get("no_learning") is not True or not isinstance(stop.get("static_rows"), dict)
            or stop.get("snapshot_artifact_ref") != "workspace-json:artifacts/repo-repair/stop-" + _source_digest(run.run_identity) + ".json"
            or stop.get("snapshot_artifact_digest") != _source_digest({"schema": "repository.stop_snapshot.v1",
                "static_rows": stop["static_rows"], "repository_job_id": run.run_identity,
                "source_checkpoint_digest": _source_digest(original)})):
        raise DurableJobLeaseError("original repository stop discovery binding changed")
    evidence = None
    if automatic:
        try:
            evidence_model = RepositoryNativeLimitEvidenceV1.model_validate(stop["limit_evidence"])
            evidence = evidence_model.model_dump(mode="json")
        except (ValueError, TypeError) as exc:
            raise DurableJobLeaseError("original repository stop limit metadata changed") from exc
        limits = inventory["original_limits"]
        goal_cutoff = _repository_goal_cutoff(limits, group)
        if (evidence != stop["limit_evidence"] or _source_digest(evidence) != stop["limit_evidence_digest"]
                or evidence["cause"] != reason or evidence["original_limits_digest"] != inventory["original_limits_digest"]
                or evidence["original_server_bound_microusd"] != limits["original_server_bound_microusd"]
                or evidence_model.original_deadline_at != _as_utc(run.deadline_at)
                or evidence["original_root_max_cost_microusd"] != work.limits.max_cost_microusd
                or evidence["original_group_max_cost_microusd"] != group.max_cost_microusd
                or evidence["original_group_max_calls"] != group.max_inference_calls
                or evidence_model.goal_cutoff_at != goal_cutoff
                or (reason == "goal_limit_exhausted" and evidence_model.original_deadline_at != evidence_model.goal_cutoff_at)):
            raise DurableJobLeaseError("original repository recorded limit binding changed")
    terminal = _repository_record(run, "repository:terminal:v1")
    hold = DurableJobRepository._repo_repair_reservation_state(run)
    # The repository terminal CAS retains its admitted fence. The original
    # C1 child cancellation invalidates that separate child's fence.
    fence = run.fencing_token
    if (hold is None or not DurableJobRepository._repo_repair_reservation_matches(hold,
            job_id=run.run_identity, attempt_id=original["repository_attempt_id"], fence=fence,
            authority_digest=run.authority_digest)
            or hold.get("execution_deadline_at") != original["original_deadline_at"]):
        raise DurableJobLeaseError("original repository discovery reservation changed")
    parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == binding.parent_job_id))
    child = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == binding.invocation_id))
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
    attempt = await db.get(WorkBoardAttempt, binding.attempt_id)
    repo_task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == original["repository_task_id"]))
    repo_attempt = await db.get(WorkBoardAttempt, original["repository_attempt_id"])
    session = await db.get(OperatorSession, owner.session_id)
    goal = await db.get(Goal, binding.goal_id)
    permanent = _binding(owner_principal_id=binding.owner_principal_id, goal_id=binding.goal_id,
        goal_revision=binding.goal_revision, idempotency_scope="original-repository-child", dedupe_key=binding.invocation_id)
    mapped = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.idempotency_binding == permanent))
    if (any(row is None for row in (parent, child, task, attempt, repo_task, repo_attempt, session, goal))
            or mapped is None or mapped.run_identity != run.run_identity or child_binding(child) != binding
            or json.loads(child.arguments_json).get("tool_id") != "repository_work"
            or session.principal_id != owner.principal_id or session.revoked_at is not None
            or session.replaced_by_id or session.is_bearer_tombstone
            or _as_utc(session.idle_expires_at) <= _utc_now() or _as_utc(session.absolute_expires_at) <= _utc_now()
            or _source_digest(goal.model_dump(mode="json")) != inventory["original_limits"]["original_goal_row_digest"]
            or (task.owner_principal_id, task.owner_session_id) != (owner.principal_id, owner.session_id)
            or (repo_task.owner_principal_id, repo_task.owner_session_id) != (owner.principal_id, owner.session_id)
            or attempt.task_id != task.task_id or attempt.workflow_run_id != parent.run_identity
            or repo_attempt.task_id != repo_task.task_id or repo_attempt.workflow_run_id != run.run_identity):
        raise DurableJobLeaseError("original repository stop discovery lineage changed")
    if terminal is None:
        if run.status not in {"running", "unknown_external_effect"} or hold["status"] != "held":
            raise DurableJobLeaseError("original repository Pending discovery state changed")
        if service is not None:
            lane = service._iterative_lanes.get(run.run_identity)
            if lane is not None and not lane.acquired:
                raise DurableJobLeaseError("original repository Pending physical handle changed")
        artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id)
        repo_artifact = await db.get(WorkBoardInputArtifact, repo_task.input_artifact_id)
        context = {"run": run, "binding": binding}
        rows = [run, parent, child, task, attempt, artifact, goal, session, repo_task, repo_attempt, repo_artifact]
        if (artifact is None or repo_artifact is None or attempt.ended_at is not None
                or attempt.cancel_requested_at is not None or repo_attempt.ended_at is not None
                or repo_attempt.cancel_requested_at is not None
                or not _stop_static_rows_match(run, stop,
                    {type(row).__tablename__ + ":" + str(_key(row)): _static(row, context) for row in rows})):
            raise DurableJobLeaseError("original repository Pending static snapshot changed")
    else:
        try:
            closure = RepositoryNativeStopClosureV1.model_validate(terminal["closure"])
        except (ValueError, TypeError, KeyError) as exc:
            raise DurableJobLeaseError("original repository terminal discovery closure changed") from exc
        artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id)
        repo_artifact = await db.get(WorkBoardInputArtifact, repo_task.input_artifact_id)
        artifact_context = {"run": run, "binding": binding}
        artifacts_unchanged = all(row is not None and stop["static_rows"].get(
            type(row).__tablename__ + ":" + str(_key(row))) == _static(row, artifact_context)
            for row in (artifact, repo_artifact))
        accounting_digest = await _accounting(db, {"group": group, "run": run, "work": work, "original": original})
        if (set(terminal) != {"schema", "closure", "no_learning"}
                or terminal["schema"] != "repository.stop_terminal.v1" or terminal["no_learning"] is not True
                or closure.original_binding != binding or closure.repository_job_id != run.run_identity
                or closure.repository_attempt_id != original["repository_attempt_id"] or closure.repository_fence != fence
                or closure.stop_reason != reason or closure.stop_intent_digest != _source_digest(stop)
                or closure.source_checkpoint_digest != _source_digest(original)
                or closure.original_input_digest != _source_digest(original["original_input"])
                or closure.original_group_digest != _source_digest(group.model_dump(mode="json"))
                or closure.all_original_accounting_digest != accounting_digest
                or closure.source_binding_digest != task_source.binding_digest
                or closure.original_deadline_at.isoformat() != original["original_deadline_at"]
                or (closure.limit_evidence.model_dump(mode="json") if closure.limit_evidence else None) != evidence
                or closure.limit_evidence_digest != stop.get("limit_evidence_digest")
                or run.status != ("cancelled" if reason == "operator_cancelled" else "failed")
                or run.finished_at is None or run.lease_owner is not None or run.lease_expires_at is not None
                or hold["status"] != "released" or hold.get("outcome_status") != run.status
                or read_general_task_native_cancel(parent, task, attempt)["state"] != "fully_cancelled"
                or child.status != "cancelled" or repo_attempt.ended_at is None
                or repo_attempt.lease_owner is not None or repo_attempt.lease_expires_at is not None
                or repo_attempt.outcome != "repository_" + reason or not artifacts_unchanged
                or repo_task.status != "blocked" or repo_task.block_reason != "repository_" + reason):
            raise DurableJobLeaseError("original repository terminal discovery evidence changed")
    prepared_exists = False
    other_exists = False
    for index in range(1, work.limits.max_iterations + 1):
        identity = iteration_identity(run.run_identity, original["repository_attempt_id"], _source_digest(original["original_input"]), index)
        prepared_exists |= _repository_record(run, "repository:prepared:" + identity) is not None
        other_exists |= any(_repository_record(run, "repository:" + kind + ":" + identity) is not None
            for kind in ("callback-start", "request", "response", "proposal", "patch", "approval",
                "execution", "cleanup", "readback", "accounting", "iteration"))
    if prepared_exists:
        projection = await _prepared_repository_projection(db, run, owner=owner)
    else:
        proposal = await db.scalar(select(RepoRepairProposal.proposal_id).where(
            RepoRepairProposal.workflow_run_id == run.run_identity).limit(1))
        accounting = await db.scalar(select(InferenceCostReservation.operation_id).where(
            InferenceCostReservation.job_id == run.run_identity).limit(1))
        if (not automatic or other_exists or proposal is not None or accounting is not None
                or (terminal is None and run.status != "running")):
            raise DurableJobLeaseError("original automatic not-prepared discovery is unproven")
        projection = {"awaiting_repository_consent": False, "native_execution": True,
            "native_child_id": binding.invocation_id, "repository_job_id": run.run_identity,
            "iteration_index": None, "iteration_id": None, "preparation_digest": None,
            "contact_state": "not_prepared", "source_preview_path": None, "verified": False, "no_learning": True}
    RepositoryReview.model_validate({key: projection[key] for key in RepositoryReview.model_fields})
    return projection, {"reason": reason, "pending": terminal is None, "limit_evidence": evidence,
        "limit_evidence_digest": stop.get("limit_evidence_digest")}


async def repository_review_projection(db, *, task, attempt, owner):
    from sqlalchemy import select
    from src.db.models import WorkflowRunState
    from src.workflows.general_task_guard import child_binding, read_manifest
    from src.workflows.job_runtime import _binding, DurableJobLeaseError
    if task.capability_id != "agent.task.v1" or attempt is None or not attempt.workflow_run_id:
        return None
    if (task.owner_principal_id, task.owner_session_id) != (owner.principal_id, owner.session_id):
        raise DurableJobLeaseError("original repository discovery owner changed")
    parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id))
    manifest = read_manifest(parent) if parent is not None else None
    if manifest is None:
        return None
    for identity in manifest.admitted_invocation_ids:
        child = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == identity))
        if child is None or json.loads(child.arguments_json).get("tool_id") != "repository_work":
            continue
        binding = child_binding(child)
        key = _binding(owner_principal_id=binding.owner_principal_id, goal_id=binding.goal_id,
            goal_revision=binding.goal_revision, idempotency_scope="original-repository-child", dedupe_key=identity)
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.idempotency_binding == key))
        if run is None:
            return None
        original, *_ = read_repository_original(run)
        if original["native_binding"] != binding.model_dump(mode="json") or binding.task_id != task.task_id:
            raise DurableJobLeaseError("original repository discovery binding changed")
        projection, _ = await _repository_discovery_metadata(db, run, owner=owner)
        return {name: projection[name] for name in ("native_child_id", "repository_job_id",
            "iteration_index", "iteration_id", "preparation_digest", "contact_state", "source_preview_path")}
    return None


async def repository_source_preview(service, jobs, *, job_id, owner):
    from src.model_fabric.effective_policy import configuration_mutation_lock
    from src.workflows.job_runtime import DurableJobLeaseError
    from src.workflows.repo_repair import _digest_bytes, _canonical_bytes
    async with configuration_mutation_lock:
        config = _assert_task_publication_configuration(service)
        context = await _repository_precontact(service, jobs, job_id=job_id, owner=owner)
        projection = await _prepared_repository_projection(None, context["run"], owner=owner)
        prepared = _repository_record(context["run"], "repository:prepared:" + projection["iteration_id"])
        body_bytes = service._read_private_artifact(prepared["request_body_artifact_ref"],
            expected_digest=prepared["request_body_digest"])
        body = json.loads(body_bytes)
        if (_source_digest(body) != prepared["request_body_digest"]
                or len(_canonical_bytes(body)) != prepared["combined_input_bytes"]
                or prepared["executor_profile_digest"] != _source_digest(config.model_dump(mode="json"))):
            raise DurableJobLeaseError("original finalized repository preparation changed")
        envelope_bytes = service._read_private_artifact(prepared["egress_envelope_artifact_ref"],
            expected_digest=prepared["egress_envelope_digest"])
        envelope = json.loads(envelope_bytes)
        if _digest_bytes(_canonical_bytes(envelope["diagnostics"])) != prepared["diagnostics_digest"]:
            raise DurableJobLeaseError("original repository diagnostics changed")
        async with jobs._session() as db:
            remaining = await _repository_remaining(db, context)
        from src.llm_runtime import build_model_kwargs
        kwargs = build_model_kwargs(temperature=0.2, max_tokens=4096, runtime_path="strategist_agent")
        return {"job_id": job_id, "status": context["run"].status, "revision": context["run"].revision,
            "recovery_action": "review_code_egress" if projection["contact_state"] == "not_started" else "refresh_repair_status",
            "repository_review": {key: value for key, value in projection.items() if key not in
                {"awaiting_repository_consent", "native_execution", "verified", "no_learning"}},
            "source_packet": {"packet_id": prepared["source_packet_id"], "state": "verified",
                "repository_ref": context["work"].repository_ref,
                "source_manifest_sha256": prepared["source_manifest_digest"],
                "artifact_sha256": prepared["source_packet_artifact_digest"],
                "selected_files": envelope["source_packet"]["files"], "omissions": []},
            "egress": {"runtime_path": "strategist_agent", "effective_profile_id": kwargs.get("runtime_profile"),
                "effective_upstream": "openrouter", "maximum_input_bytes": 65536, "maximum_output_tokens": 4096,
                "request_body": body, "request_body_digest": prepared["request_body_digest"],
                "request_route_digest": prepared["request_route_digest"],
                "egress_envelope_digest": prepared["egress_envelope_digest"],
                "diagnostics_digest": prepared["diagnostics_digest"], "diagnostics": envelope["diagnostics"],
                "redaction_version": prepared["redaction_version"], "combined_input_bytes": prepared["combined_input_bytes"],
                "original_deadline_at": prepared["original_deadline_at"], **remaining},
            "provider_contacted": False, "operator_visible": True}


async def repository_operator_projection(service, jobs, *, job_id, owner):
    """Owner-scoped canonical metadata; never read private source/model bytes."""
    from src.db.models import RepoRepairProposal, ApprovalRequest, InferenceCostReservation
    from sqlalchemy import select
    from src.workflows.job_runtime import DurableJobLeaseError, _as_utc
    async with jobs._session() as db:
        run = await jobs._fetch(db, job_id)
        original, work, compiled, group, binding, task_source = read_repository_original(run)
        if (run.owner_principal_id, run.operator_session_id) != (owner.principal_id, owner.session_id):
            raise DurableJobLeaseError("original repository metadata owner changed")
        # This projection grants no contact, process, resume or private read.
        # Each corresponding action independently stages its current source.
        projection, stop_metadata = await _repository_discovery_metadata(db, run, owner=owner, service=service)
        identity = projection["iteration_id"]
        proposal = await db.get(RepoRepairProposal, "repository-proposal:" + identity) if identity is not None else None
        approval = await db.get(ApprovalRequest, proposal.approval_id) if proposal is not None else None
        if proposal is not None and (proposal.workflow_run_id != job_id
                or (proposal.owner_principal_id, proposal.owner_session_id) != (owner.principal_id, owner.session_id)):
            raise DurableJobLeaseError("original repository proposal metadata owner changed")
        if approval is not None and (approval.owner_principal_id, approval.operator_session_id) != (owner.principal_id, owner.session_id):
            raise DurableJobLeaseError("original repository approval metadata owner changed")
        from src.workflows.repo_repair import RepoIteration
        iterations, iteration_states = [], []
        for index in range(1, work.limits.max_iterations + 1):
            iteration = iteration_identity(job_id, original["repository_attempt_id"],
                _source_digest(original["original_input"]), index)
            readback = _repository_record(run, "repository:readback:" + iteration)
            cleanup = _repository_record(run, "repository:cleanup:" + iteration)
            if readback is not None:
                prepared = _repository_record(run, "repository:prepared:" + iteration)
                executed = _repository_record(run, "repository:execution:" + iteration)
                if (prepared is None or executed is None or cleanup is None
                        or cleanup.get("cleanup_proven") is not True
                        or readback.get("status") not in {"succeeded", "failed"}):
                    raise DurableJobLeaseError("complete actual original iteration metadata required")
                iterations.append(RepoIteration(index=index, input_tree_digest=prepared["input_tree_digest"],
                    patch_digest=executed["patch_sha256"], command_refs=["repository:execution:" + iteration],
                    result_artifacts=["repository:cleanup:" + iteration,
                        "repository:readback:" + iteration]).model_dump(mode="json"))
                command_results = readback.get("command_results")
                if command_results is not None:
                    _validate_repository_command_results(command_results)
                iteration_states.append({"index": index, "iteration_id": iteration, "status": readback["status"],
                    "manifest_artifact_ref": readback["artifact_ref"], "manifest_artifact_digest": readback["artifact_digest"],
                    "cleanup_proven": True, "command_results": command_results,
                    "command_results_status": "unknown" if command_results is None else "recorded"})
        action = "review_code_egress" if projection["contact_state"] == "not_started" else "refresh_repair_status"
        stop = _repository_record(run, "repository:stop-intent:v1")
        terminal = _repository_record(run, "repository:terminal:v1")
        if stop is not None:
            from src.workflows.repo_repair_stop import AUTOMATIC_REASONS
            if stop.get("stop_reason") not in {"operator_cancelled", "iterations_exhausted"} | AUTOMATIC_REASONS:
                raise DurableJobLeaseError("original repository stop metadata is malformed")
            action = ("repository_stopped" if stop["stop_reason"] == "operator_cancelled" else "original_" + stop["stop_reason"]) if (
                terminal and terminal.get("schema") == "repository.stop_terminal.v1"
                and run.status in {"cancelled", "failed"}) else "repository_stop_pending"
        elif run.status == "unknown_external_effect":
            action = "reconcile_original_repository"
        elif run.status == "succeeded":
            action = "review_verified_local_patch"
        elif proposal is not None and proposal.status == "awaiting_approval":
            action = "review_patch_approval" if approval is None or approval.status == "pending" else "execute_approved_patch"
        elif (iteration_states and iteration_states[-1]["status"] == "failed"
                and iteration_states[-1]["index"] == work.limits.max_iterations):
            action = "original_iterations_exhausted"
        provider_contacted = await db.scalar(select(InferenceCostReservation.operation_id).where(
            InferenceCostReservation.job_id == job_id,
            InferenceCostReservation.contact_started_at.is_not(None)).limit(1)) is not None
        return {"job_id": job_id, "status": run.status, "revision": run.revision,
            "repository_review": {key: value for key, value in projection.items() if key not in
                {"awaiting_repository_consent", "native_execution", "verified", "no_learning"}},
            "patch_proposal": None if proposal is None else {"proposal_id": proposal.proposal_id,
                "revision": proposal.revision, "approval_id": proposal.approval_id, "status": proposal.status,
                "summary": json.loads(proposal.safe_metadata_json).get("summary", ""),
                "patch_artifact_ref": "workspace-json:" + proposal.patch_artifact_id,
                "patch_sha256": proposal.patch_sha256, "expires_at": _as_utc(proposal.expires_at).isoformat(),
                "allowed_paths": json.loads(proposal.allowed_paths_json), "test_args": json.loads(proposal.test_args_json)},
            "approval": None if approval is None else {"id": approval.id, "status": approval.status,
                "fingerprint": approval.fingerprint, "expires_at": _as_utc(approval.expires_at).isoformat()},
            "iterations": iterations, "iteration_states": iteration_states, "recovery_action": action,
            "provider_contacted": provider_contacted,
            "no_learning": True, "operator_visible": True,
            **({"repository_stop": stop_metadata} if stop_metadata is not None else {})}


def _validate_repository_command_results(values):
    from src.workflows.job_runtime import DurableJobLeaseError
    if (not isinstance(values, list) or not 1 <= len(values) <= 2
            or any(not isinstance(item, dict) or set(item) != {"check", "status", "exit_code"}
                or item["check"] not in {"build", "test"}
                or item["status"] not in {"succeeded", "failed", "timed_out", "cancelled", "unknown"}
                or (item["exit_code"] is not None and (type(item["exit_code"]) is not int
                    or not -(2 ** 31) <= item["exit_code"] < 2 ** 31))
                or (item["status"] == "succeeded" and item["exit_code"] != 0) for item in values)
            or len({item["check"] for item in values}) != len(values)):
        raise DurableJobLeaseError("actual bounded command metadata changed")


def _repository_command_results(manifest, *, node):
    commands = manifest["commands"] if node else [{**manifest, "script": "test"}]
    values = [{"check": entry["script"], "exit_code": entry.get("exit_code"),
        "status": "cancelled" if entry.get("cancelled") is True else
            "timed_out" if entry.get("timed_out") is True else
            "unknown" if entry.get("exit_code") is None else
            "succeeded" if entry["exit_code"] == 0 else "failed"} for entry in commands]
    _validate_repository_command_results(values)
    return values


@dataclass(frozen=True, slots=True)
class _RepositoryFirstStartTicket:
    source: Any
    jobs: Any
    binding: GeneralTaskNativeChildBindingV1
    job_id: str
    child_owner: str
    child_fence: int
    preparation_digest: str
    consent_id: str
    iteration_id: str
    used: bool = field(default=False, repr=False)
    _seal: object = field(default=None, repr=False, compare=False)


async def consume_repository_first_start(ticket, *, service, jobs, binding, child_owner):
    from src.workflows.job_runtime import DurableJobLeaseError, _as_utc, _utc_now
    if (type(ticket) is not _RepositoryFirstStartTicket or ticket._seal is not _SEAL or ticket.used
            or ticket.source is not service.repository_source_service or ticket.jobs is not jobs
            or ticket.binding != binding or ticket.child_owner != child_owner):
        raise DurableJobLeaseError("actual single-use original repository first start required")
    async with jobs._session() as db:
        child = await jobs._fetch(db, binding.invocation_id)
        run = await jobs._fetch(db, ticket.job_id)
        original, *_ = read_repository_original(run)
        intent = _repository_record(run, "repository:callback-start:" + ticket.iteration_id)
        if (intent is None or intent["preparation_digest"] != ticket.preparation_digest
                or intent["consent_id"] != ticket.consent_id or intent["native_child_owner"] != child_owner
                or original["native_binding"] != binding.model_dump(mode="json")
                or child.status != "running" or child.attempt_count != 1
                or child.lease_owner != child_owner or child.fencing_token != ticket.child_fence
                or _as_utc(child.lease_expires_at) <= _utc_now()
                or _as_utc(run.deadline_at) <= _utc_now()):
            raise DurableJobLeaseError("original repository first-start binding changed")
    object.__setattr__(ticket, "used", True)
    return await jobs.get_job(binding.invocation_id)


async def grant_repository_iteration_consent(service, jobs, *, job_id, owner, request,
        general_task_service, principal):
    from src.api.workflows import RepoRepairEgressConsentRequest
    from src.work_board.general_task import GeneralTaskService
    from src.workflows.repo_repair import RepoRepairService, RepoRepairError, _canonical_bytes
    from src.model_fabric.effective_policy import configuration_mutation_lock
    from src.work_board.repository import _begin_sqlite_immediate
    from src.workflows.job_runtime import _as_utc, _utc_now, _digest, _canonical, DurableJobLeaseError
    from src.db.models import RepoRepairEgressConsent
    from sqlalchemy import select
    from src.llm_runtime import build_model_kwargs
    if (type(general_task_service) is not GeneralTaskService or not general_task_service.started
            or type(request) is not RepoRepairEgressConsentRequest or not request.is_repository_iteration_variant
            or request.acknowledged_selected_source is not True or request.acknowledged_diagnostics is not True
            or not principal.authenticated or principal.revoked
            or principal.principal_id != owner.principal_id or principal.operator_session_id != owner.session_id):
        raise RepoRepairError("repository_iteration_consent_authority_invalid", "Current original source owner and exact acknowledgments required")
    source = general_task_service.repository_source_service
    if (type(source) is not RepoRepairService or type(service) is not RepoRepairService
            or source._workspace() != service._workspace() or source.sandbox.config != service.sandbox.config):
        raise RepoRepairError("repository_source_owner_changed", "Restore the actual original source service")
    source.jobs = jobs
    async with configuration_mutation_lock:
        config = _assert_task_publication_configuration(source)
        context = await _repository_precontact(source, jobs, job_id=job_id, owner=owner)
        run = context["run"]
        projection = await _prepared_repository_projection(None, run, owner=owner)
        prepared = _repository_record(run, "repository:prepared:" + request.expected_iteration_id)
        if prepared is None:
            raise RepoRepairError("repository_iteration_preparation_changed", "Refresh the exact original preparation")
        comparisons = {
            "iteration_index": request.expected_iteration_index, "iteration_id": request.expected_iteration_id,
            "preparation_digest": request.expected_preparation_digest, "request_body_digest": request.expected_request_body_digest,
            "request_route_digest": request.expected_request_route_digest, "egress_envelope_digest": request.expected_egress_envelope_digest,
            "diagnostics_digest": request.expected_diagnostics_digest, "redaction_version": request.expected_redaction_version,
            "source_packet_artifact_digest": request.source_packet_digest,
            "source_manifest_digest": request.expected_source_manifest_digest}
        if any(prepared.get(key) != value for key, value in comparisons.items()):
            raise RepoRepairError("repository_iteration_consent_binding_changed", "Review the current exact preparation")
        started = _repository_record(run, "repository:callback-start:" + request.expected_iteration_id)
        if started is not None:
            if started.get("request_key") != request.idempotency_key:
                raise RepoRepairError("repository_callback_already_started", "The original callback cannot start again")
            return {"job_id": job_id, "status": run.status, "consent_id": started["consent_id"],
                "idempotent_replay": True, "repository_review": projection, "operator_visible": True}
        if run.revision != request.expected_job_revision:
            raise RepoRepairError("repair_job_revision_stale", "Refresh the source preview")
        body = json.loads(source._read_private_artifact(prepared["request_body_artifact_ref"],
            expected_digest=prepared["request_body_digest"]))
        envelope = json.loads(source._read_private_artifact(prepared["egress_envelope_artifact_ref"],
            expected_digest=prepared["egress_envelope_digest"]))
        candidate = await source.prepare_iteration_egress(iteration_id=prepared["iteration_id"],
            source_payload=envelope["source_packet"], stdout=envelope["diagnostics"]["stdout"], stderr=envelope["diagnostics"]["stderr"])
        kwargs = build_model_kwargs(temperature=0.2, max_tokens=4096, runtime_path="strategist_agent")
        model = source.model_factory(**kwargs)
        finalized = source.finalize_iteration_egress(candidate, model=model, response_format=body["response_format"])
        import asyncio
        preflight = await asyncio.to_thread(_iterative_executor_preflight, source, context["compiled"])
        if (finalized["serialized_request_sha256"] != prepared["request_body_digest"]
                or finalized["request_route_digest"] != prepared["request_route_digest"]
                or finalized["redaction_version"] != prepared["redaction_version"]
                or len(_canonical_bytes(body)) != prepared["combined_input_bytes"]
                or str(kwargs.get("runtime_profile") or "") != request.expected_profile_id
                or _source_digest(config.model_dump(mode="json")) != prepared["executor_profile_digest"]
                or not preflight.ok or preflight.posture_digest != prepared["executor_posture_digest"]):
            raise RepoRepairError("repository_finalized_request_changed", "Source, diagnostics, route or finalized model request changed")
        adapter = getattr(general_task_service, "repository_work_adapter", None)
        if (getattr(adapter, "__self__", None) is not source
                or getattr(adapter, "__func__", None) is not RepoRepairService.native_iteration_adapter):
            raise RepoRepairError("repository_native_adapter_unavailable", "The fixed original source adapter is unavailable")
        expiry = min(_as_utc(run.deadline_at), context["group"].original_deadline_at,
            _as_utc(run.lease_expires_at), _as_utc(context["child"].lease_expires_at),
            _as_utc(context["authority"].attempt.lease_expires_at), _utc_now() + timedelta(minutes=30))
        digest_payload = {"preparation": prepared, "owner": owner.model_dump(mode="json"),
            "profile": request.expected_profile_id, "expires_at": expiry.isoformat(), "request_key": request.idempotency_key}
        async with jobs._session() as db:
            await _begin_sqlite_immediate(db)
            await _recheck_repository_sql(db, context)
            await _repository_remaining(db, context)
            current = await jobs._fetch(db, job_id)
            existing = await db.scalar(select(RepoRepairEgressConsent).where(
                RepoRepairEgressConsent.owner_principal_id == owner.principal_id,
                RepoRepairEgressConsent.owner_session_id == owner.session_id,
                RepoRepairEgressConsent.request_key == request.idempotency_key))
            if existing is not None:
                raise RepoRepairError("egress_consent_idempotency_conflict", "A consent key cannot change or restart its original callback")
            consent = RepoRepairEgressConsent(owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
                work_board_task_id=prepared["repository_task_id"], work_board_attempt_id=prepared["repository_attempt_id"],
                workflow_run_id=job_id, source_packet_id=prepared["source_packet_id"],
                source_digest=prepared["source_manifest_digest"], source_manifest_digest=prepared["source_manifest_digest"],
                goal_id=run.goal_id, goal_revision=run.goal_revision, input_digest=run.input_digest,
                effective_profile_id=request.expected_profile_id, effective_upstream="openrouter", runtime_path="strategist_agent",
                iteration_id=prepared["iteration_id"], egress_envelope_schema="seraph.repo_iteration_egress.v1",
                egress_envelope_artifact_ref=prepared["egress_envelope_artifact_ref"], egress_envelope_sha256=prepared["egress_envelope_digest"],
                diagnostics_sha256=prepared["diagnostics_digest"], redaction_version=prepared["redaction_version"],
                serialized_request_sha256=prepared["request_body_digest"], combined_input_bytes=prepared["combined_input_bytes"],
                diagnostics_acknowledged=True, maximum_input_bytes=65536, maximum_output_tokens=4096, expires_at=expiry,
                consent_digest=_source_digest(digest_payload), request_key=request.idempotency_key,
                request_digest=_source_digest(request.model_dump(mode="json")))
            db.add(consent)
            await db.flush()
            intent = {"schema": "seraph.repository_callback_start.v1", "iteration_id": prepared["iteration_id"],
                "preparation_digest": prepared["preparation_digest"], "consent_id": consent.id,
                "consent_digest": consent.consent_digest, "native_child_owner": context["child"].lease_owner,
                "native_child_fence": context["child"].fencing_token,
                "native_child_lease_expires_at": _as_utc(context["child"].lease_expires_at).isoformat(),
                "request_key": request.idempotency_key, "request_digest": consent.request_digest}
            _append_repository_record(current, "repository:callback-start:" + prepared["iteration_id"], intent,
                inventory=repository_checkpoint_inventory(current, context["work"]))
            current.revision += 1
            await db.commit()
        ticket = _RepositoryFirstStartTicket(source=source, jobs=jobs, binding=context["binding"], job_id=job_id,
            child_owner=context["child"].lease_owner, child_fence=context["child"].fencing_token,
            preparation_digest=prepared["preparation_digest"], consent_id=consent.id,
            iteration_id=prepared["iteration_id"], _seal=_SEAL)
    from src.work_board.general_task_native import run_native_step
    try:
        outcome, _artifact, _reference = await run_native_step(general_task_service, jobs, ticket.binding,
            child_owner=ticket.child_owner, principal=principal, repository_first_start=ticket)
    except BaseException:
        # A committed invocation intent is never replayed. Preserve the
        # physical lane and real callback handle until their owners prove
        # closure; a disconnected HTTP caller is not such proof.
        import asyncio
        async def quarantine_original():
            await _quarantine_original_uncertainty(source, jobs, job_id=job_id, owner=owner,
                lease_owner=context["run"].lease_owner, fencing_token=context["run"].fencing_token,
                reason="repository_callback_closure_unproven",
                result={"no_learning": True, "operator_action": "reconcile_original_callback",
                    "iteration_id": ticket.iteration_id})
        quarantine = asyncio.create_task(quarantine_original())
        source._iterative_model_callbacks["quarantine:" + ticket.iteration_id] = quarantine
        await asyncio.shield(quarantine)
        raise
    return {"job_id": job_id, "consent_id": consent.id, "repository_outcome": outcome, "operator_visible": True}


async def run_repository_iteration(service, *, jobs, binding, descriptor, inputs, step,
        child_owner, principal, fencing_token, approved_resume, original_deadline, phase_timeout,
        repository_first_start=None):
    """Actual fixed callback: one original, consented, accounted model phase."""
    import asyncio
    from dataclasses import replace
    from src.workflows.repo_repair import (RepoRepairError, _model_response_content, _parse_model_json,
        _digest_bytes, _canonical_bytes, _repair_test_args, REPO_REPAIR_APPROVAL_TOOL, REPO_REPAIR_APPROVAL_ACTION)
    from src.workflows.job_runtime import _as_utc, _utc_now, _digest, DurableJobLeaseError
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.repository import _begin_sqlite_immediate
    from src.db.models import InferenceCostReservation, RepoRepairProposal
    from src.model_fabric.accounting import bind_repository_iteration_accounting
    from src.model_fabric import bind_remote_inference_receipt
    from src.model_fabric.caller_context import build_canonical_inference_context
    from src.approval.runtime import set_runtime_context, reset_runtime_context, get_current_approval_mode
    from src.llm_runtime import build_model_kwargs
    if (approved_resume or type(repository_first_start) is not _RepositoryFirstStartTicket
            or repository_first_start._seal is not _SEAL or not repository_first_start.used
            or repository_first_start.source is not service or repository_first_start.binding != binding
            or repository_first_start.child_owner != child_owner or repository_first_start.child_fence != fencing_token):
        raise DurableJobLeaseError("actual original consumed repository start required")
    ticket = repository_first_start
    owner = WorkBoardOwner(principal_id=binding.owner_principal_id, session_id=binding.original_root_id)
    context = await _repository_precontact(service, jobs, job_id=ticket.job_id, owner=owner)
    run = context["run"]
    prepared = _repository_record(run, "repository:prepared:" + ticket.iteration_id)
    if prepared["preparation_digest"] != ticket.preparation_digest:
        raise DurableJobLeaseError("original callback preparation changed")
    body = json.loads(service._read_private_artifact(prepared["request_body_artifact_ref"],
        expected_digest=prepared["request_body_digest"]))
    witness_payload = {"group": context["group"].model_dump(mode="json"),
        "repository_job_id": ticket.job_id, "repository_attempt_id": prepared["repository_attempt_id"],
        "repository_fence": prepared["repository_fence"], "parent_task_id": binding.task_id,
        "parent_attempt_id": binding.attempt_id, "native_invocation_id": binding.invocation_id,
        "iteration_index": prepared["iteration_index"], "iteration_id": ticket.iteration_id,
        "operation_id": "remote:repo-work:" + ticket.iteration_id,
        "original_deadline_at": context["original"]["original_deadline_at"],
        "original_max_cost_microusd": context["original"]["original_max_cost_microusd"],
        "source_checkpoint_digest": _digest(context["original"])}
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        await _recheck_repository_sql(db, context)
        await _repository_remaining(db, context)
        current = await jobs._fetch(db, ticket.job_id)
        _append_repository_record(current, "repository:iteration:" + ticket.iteration_id,
            {"phase": "model_ready", "accounting_binding": witness_payload,
             "payload_digest": prepared["request_body_digest"]},
            inventory=repository_checkpoint_inventory(current, context["work"]))
        _append_repository_record(current, "repository:request:" + ticket.iteration_id,
            {"operation_id": witness_payload["operation_id"], "iteration_id": ticket.iteration_id,
             "request_body_digest": prepared["request_body_digest"],
             "request_route_digest": prepared["request_route_digest"],
             "request_artifact_ref": prepared["request_body_artifact_ref"],
             "consent_id": ticket.consent_id, "preparation_digest": ticket.preparation_digest},
            inventory=repository_checkpoint_inventory(current, context["work"]))
        current.revision += 1
        await db.commit()
    async with jobs._session() as db:
        canonical = await stage_repository_canonical_source(service, db,
            repository_job_id=ticket.job_id, native_invocation_id=binding.invocation_id, consent_id=ticket.consent_id)
    witness = RepositoryIterationAccountingWitness(**{**witness_payload, "group": context["group"],
        "original_deadline_at": _as_utc(context["run"].deadline_at)},
        _request_body_digest=prepared["request_body_digest"], _request_route_digest=prepared["request_route_digest"],
        _canonical_source=canonical, _seal=_SEAL)
    model = service.model_factory(**build_model_kwargs(temperature=0.2, max_tokens=4096,
        runtime_path="strategist_agent"))
    scoped_principal = replace(principal, job_id=ticket.job_id)
    inference_context = build_canonical_inference_context("strategist_agent", payload=body["messages"],
        output_tokens=4096, timeout_seconds=min(120.0, phase_timeout), principal=scoped_principal,
        session_id=scoped_principal.session_id, job_id=ticket.job_id,
        transformation_digest=_source_digest({"preparation": prepared["preparation_digest"], "consent": ticket.consent_id}),
        redaction_applied=True)
    tokens = set_runtime_context(scoped_principal.session_id, get_current_approval_mode(), trust_principal=scoped_principal)
    try:
        with bind_remote_inference_receipt(repository=jobs, job_id=ticket.job_id,
                owner=run.lease_owner, fencing_token=run.fencing_token, repository_iteration_witness=witness), \
                bind_repository_iteration_accounting(witness):
            callback = asyncio.create_task(asyncio.to_thread(model.generate, body["messages"],
                response_format=body["response_format"], request_context=inference_context))
            service._iterative_model_callbacks[ticket.iteration_id] = callback
            response = await asyncio.shield(callback)
    finally:
        reset_runtime_context(tokens)
    if not callback.done() or callback.cancelled() or callback.exception() is not None:
        raise DurableJobLeaseError("actual model callback return is unproven")
    content = _model_response_content(response)
    if await service._scan_secrets(content) != content:
        raise RepoRepairError("model_output_secret_detected", "Model output matched protected secret material")
    output = _parse_model_json(content)
    envelope = json.loads(service._read_private_artifact(prepared["egress_envelope_artifact_ref"],
        expected_digest=prepared["egress_envelope_digest"]))
    if (output.base_snapshot_sha256 != envelope["source_packet"]["base_snapshot_sha256"]
            or not set(output.allowed_paths).issubset(context["work"].allowed_paths)
            or _repair_test_args(tuple(output.test_args), output.allowed_paths) !=
                _repair_test_args(tuple(context["compiled"].test_args), context["compiled"].allowed_paths)):
        raise RepoRepairError("model_patch_authority_changed", "Model proposal changed the original base, paths or checks")
    patch = (output.patch_unified_diff.rstrip("\n") + "\n").encode()
    from src.execution.repo_sandbox import _patch_paths_from_diff
    _patch_paths_from_diff(patch, context["work"].allowed_paths)
    patch_digest = _digest_bytes(patch)
    response_ref, response_digest = service._write_private_artifact(
        "artifacts/repo-repair/model/iteration-" + ticket.iteration_id + "-response.json",
        _canonical_bytes({"content": content}))
    patch_ref, _ = service._write_private_artifact(
        "artifacts/repo-repair/patch/iteration-" + ticket.iteration_id + ".diff", patch)
    async with jobs._session() as db:
        cost = await db.get(InferenceCostReservation, witness.operation_id)
        if (cost is None or cost.state != "settled" or cost.contact_started_at is None
                or cost.payload_digest != prepared["request_body_digest"]
                or type(cost.actual_cost_microusd) is not int or cost.actual_cost_microusd < 0):
            raise DurableJobLeaseError("actual original model response accounting is not settled")
    approval_id = "repository-approval:" + ticket.iteration_id
    from src.workflows.repo_repair import (_sandbox_authority_payload,
        _proposal_authority_payload, _authority_digest, _repair_approval_fingerprint)
    posture = await asyncio.to_thread(_iterative_executor_preflight, service, context["compiled"])
    if not posture.ok or posture.posture_digest != prepared["executor_posture_digest"]:
        raise DurableJobLeaseError("fixed iterative executor is blocked")
    proposal = RepoRepairProposal(proposal_id="repository-proposal:" + ticket.iteration_id,
        operation_key="repository-proposal:" + ticket.iteration_id,
        owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
        work_board_task_id=prepared["repository_task_id"],
        work_board_attempt_id=prepared["repository_attempt_id"], workflow_run_id=ticket.job_id,
        goal_id=run.goal_id, goal_revision=run.goal_revision,
        repository_ref=context["work"].repository_ref,
        base_snapshot_digest=output.base_snapshot_sha256, source_packet_id=prepared["source_packet_id"],
        source_digest=prepared["source_manifest_digest"], model_runtime_path="strategist_agent",
        model_profile_id=cost.profile_id, model_request_digest=prepared["request_body_digest"],
        model_output_digest=response_digest, model_response_artifact_id=response_ref.removeprefix("workspace-json:"),
        model_response_artifact_sha256=response_digest, patch_artifact_id=patch_ref.removeprefix("workspace-json:"),
        patch_sha256=patch_digest, allowed_paths_json=json.dumps(sorted(output.allowed_paths)),
        test_args_json=json.dumps(list(_repair_test_args(tuple(output.test_args), output.allowed_paths))),
        request_digest=_source_digest({"original": context["original"], "iteration": ticket.iteration_id,
            "request": prepared["request_body_digest"], "response": response_digest, "patch": patch_digest}),
        approval_id=approval_id, status="awaiting_approval", expires_at=_as_utc(run.deadline_at),
        safe_metadata_json=json.dumps({"summary": output.summary, "expected_outcome": output.expected_outcome,
            "memory_status": "no_learning", "iteration_id": ticket.iteration_id,
            "source_checkpoint_digest": _digest(context["original"]),
            "sandbox": _sandbox_authority_payload(service.sandbox, posture)}))
    proposal.authority_digest = _authority_digest(_proposal_authority_payload(proposal))
    approval_fingerprint = _repair_approval_fingerprint(proposal, _as_utc(run.deadline_at))
    proposal.approval_fingerprint = approval_fingerprint
    from src.approval.repository import approval_repository
    approval = await approval_repository.get_or_create_pending(session_id=owner.session_id,
        tool_name=REPO_REPAIR_APPROVAL_TOOL, risk_level="high", request_id=approval_id,
        fingerprint=approval_fingerprint, summary="Review the exact bounded repository patch",
        details={"action": REPO_REPAIR_APPROVAL_ACTION, "approval_owner_principal_id": owner.principal_id,
            "approval_owner_operator_session_id": owner.session_id, "approval_execution_owner_principal_id": owner.principal_id,
            "approval_execution_session_id": owner.session_id, "approval_operator_principal_id": owner.principal_id,
            "approval_conversation_id": owner.session_id, "durable_job_id": ticket.job_id,
            "iteration_id": ticket.iteration_id, "proposal_id": proposal.proposal_id,
            "proposal_revision": proposal.revision, "authority_digest": proposal.authority_digest,
            **_sandbox_authority_payload(service.sandbox, posture),
            "patch_sha256": patch_digest,
            "expires_at": _as_utc(run.deadline_at).timestamp(), "memory_status": "no_learning"})
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        current = await jobs._fetch(db, ticket.job_id)
        inventory = repository_checkpoint_inventory(current, context["work"])
        if await db.get(RepoRepairProposal, proposal.proposal_id) is not None:
            raise DurableJobLeaseError("original iteration proposal is single use")
        db.add(proposal)
        await db.flush()
        closed = {"schema": "repository.model_response.v1", "operation_id": witness.operation_id,
            "request_body_digest": prepared["request_body_digest"], "response_artifact_ref": response_ref,
            "response_artifact_digest": response_digest, "callback_returned": True,
            "accounting_digest": _source_digest(cost.model_dump(mode="json")), "returned_at": _utc_now().isoformat()}
        closed["callback_quiescence_digest"] = _source_digest(closed)
        _append_repository_record(current, "repository:response:" + ticket.iteration_id, closed, inventory=inventory)
        _append_repository_record(current, "repository:accounting:" + ticket.iteration_id,
            {"operation_id": cost.operation_id, "accounting_digest": closed["accounting_digest"],
             "state": cost.state, "contact_started_at": _as_utc(cost.contact_started_at).isoformat(),
             "actual_cost_microusd": cost.actual_cost_microusd, "bound_microusd": cost.bound_microusd,
             "original_group_digest": prepared["original_group_digest"]}, inventory=inventory)
        _append_repository_record(current, "repository:patch:" + ticket.iteration_id,
            {"patch_artifact_ref": patch_ref, "patch_sha256": patch_digest,
             "allowed_paths": output.allowed_paths, "test_args": output.test_args}, inventory=inventory)
        _append_repository_record(current, "repository:approval:" + ticket.iteration_id,
            {"approval_id": approval.id, "fingerprint": approval_fingerprint,
             "expires_at": _as_utc(run.deadline_at).isoformat(), "patch_sha256": patch_digest}, inventory=inventory)
        current.revision += 1
        await db.commit()
    async with jobs._session() as db:
        canonical = await stage_repository_canonical_source(service, db,
            repository_job_id=ticket.job_id, native_invocation_id=binding.invocation_id, consent_id=ticket.consent_id)
    from src.workflows.general_task_guard import issue_repository_child_wait_witness
    wait = issue_repository_child_wait_witness(native_binding=binding, source_binding=canonical,
        repository_job_id=ticket.job_id, repository_attempt_id=prepared["repository_attempt_id"],
        repository_fence=prepared["repository_fence"], iteration_index=prepared["iteration_index"],
        iteration_id=ticket.iteration_id, source_checkpoint_digest=_digest(context["original"]),
        request_body_digest=prepared["request_body_digest"], response_readback_digest=response_digest,
        callback_quiescence_digest=closed["callback_quiescence_digest"])
    return {"wait_witness": wait}


async def certify_repository_callback_return(ticket, result, *, service, jobs):
    """Called by the fixed native owner only after its actual adapter await."""
    from src.workflows.job_runtime import DurableJobLeaseError, _utc_now, _digest
    from src.work_board.repository import _begin_sqlite_immediate
    from src.workflows.general_task_guard import (RepositoryChildWaitWitness,
        _assert_repository_child_wait_witness_shape, issue_repository_child_wait_witness)
    if (type(ticket) is not _RepositoryFirstStartTicket or ticket._seal is not _SEAL or not ticket.used
            or ticket.source is not service.repository_source_service or ticket.jobs is not jobs
            or not isinstance(result, dict) or set(result) != {"wait_witness"}):
        raise DurableJobLeaseError("actual original adapter return required")
    provisional = result["wait_witness"]
    _assert_repository_child_wait_witness_shape(provisional)
    callback = ticket.source._iterative_model_callbacks.get(ticket.iteration_id)
    if callback is None or not callback.done() or callback.cancelled() or callback.exception() is not None:
        raise DurableJobLeaseError("actual model callback is not quiescent")
    async with jobs._session() as db:
        canonical = await stage_repository_canonical_source(ticket.source, db,
            repository_job_id=ticket.job_id, native_invocation_id=ticket.binding.invocation_id,
            consent_id=ticket.consent_id)
    from src.workflows.job_runtime import _canonical
    canonical_ref, canonical_digest = ticket.source._write_private_artifact(
        "artifacts/repo-repair/model/iteration-" + ticket.iteration_id + "-canonical-source.json",
        _canonical(canonical.projection()).encode())
    readback = ticket.source._read_private_artifact(canonical_ref, expected_digest=canonical_digest)
    if readback != _canonical(canonical.projection()).encode():
        raise DurableJobLeaseError("literal original canonical snapshot readback changed")
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        run = await jobs._fetch(db, ticket.job_id)
        original, work, *_ = read_repository_original(run)
        response = _repository_record(run, "repository:response:" + ticket.iteration_id)
        if (response is None or response["response_artifact_digest"] != provisional.response_readback_digest
                or provisional.native_binding != ticket.binding):
            raise DurableJobLeaseError("original adapter response readback changed")
        closure = {"schema": "repository.adapter_return.v1", "iteration_id": ticket.iteration_id,
            "native_invocation_id": ticket.binding.invocation_id, "child_fence": ticket.child_fence,
            "preparation_digest": ticket.preparation_digest, "response_readback_digest": provisional.response_readback_digest,
            "provider_callback_digest": response["callback_quiescence_digest"],
            "canonical_source_artifact_ref": canonical_ref,
            "canonical_source_artifact_digest": canonical_digest,
            "original_parent_revision": canonical.parent_revision,
            "original_child_revision": canonical.child_revision,
            "parent_static_digest": _source_digest({k: v for k, v in json.loads(canonical.parent_row_json).items()
                if k not in {"revision", "updated_at", "checkpoint_receipts_json"}}),
            "child_static_digest": _source_digest({k: v for k, v in json.loads(canonical.child_row_json).items()
                if k not in {"revision", "status", "failure_reason", "updated_at", "heartbeat_at"}}),
            "returned_at": _utc_now().isoformat()}
        _append_repository_record(run, "repository:proposal:" + ticket.iteration_id, closure,
            inventory=repository_checkpoint_inventory(run, work))
        run.revision += 1
        await db.commit()
    async with jobs._session() as db:
        canonical = await stage_repository_canonical_source(ticket.source, db,
            repository_job_id=ticket.job_id, native_invocation_id=ticket.binding.invocation_id,
            consent_id=ticket.consent_id)
    return {"wait_witness": issue_repository_child_wait_witness(native_binding=ticket.binding,
        source_binding=canonical, repository_job_id=ticket.job_id,
        repository_attempt_id=provisional.repository_attempt_id, repository_fence=provisional.repository_fence,
        iteration_index=provisional.iteration_index, iteration_id=ticket.iteration_id,
        source_checkpoint_digest=_digest(original), request_body_digest=provisional.request_body_digest,
        response_readback_digest=provisional.response_readback_digest, callback_quiescence_digest=_source_digest(closure))}


async def validate_repository_child_wait_witness(db, witness, *, parent, task,
        attempt, child, manifest, staged_artifact=None, receipt=None, phase):
    """Check the actual adapter return and settled accounting in the C1 writer.

    Physical artifacts were staged by the source owner before this writer.
    Neither a provider response alone nor its provisional witness qualifies.
    """
    from sqlalchemy import select
    from src.db.models import WorkflowRunState, InferenceCostReservation, ApprovalRequest
    from src.workflows.job_runtime import DurableJobLeaseError, _digest, _as_utc, _utc_now
    from src.workflows.general_task_guard import _assert_repository_child_wait_witness_shape
    _assert_repository_child_wait_witness_shape(witness)
    source = witness._source_binding
    assert_repository_canonical_source(source)
    run = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == witness.repository_job_id))
    if phase != "wait" or run is None:
        raise DurableJobLeaseError("original repository contacted wait required")
    original, work, compiled, group, binding, task_source = read_repository_original(run)
    prepared = _repository_record(run, "repository:prepared:" + witness.iteration_id)
    response = _repository_record(run, "repository:response:" + witness.iteration_id)
    closure = _repository_record(run, "repository:proposal:" + witness.iteration_id)
    approval_binding = _repository_record(run, "repository:approval:" + witness.iteration_id)
    iteration = _repository_record(run, "repository:iteration:" + witness.iteration_id)
    if (any(record is None for record in (prepared, response, closure, approval_binding, iteration))
            or binding != witness.native_binding or binding.invocation_id != child.run_identity
            or binding.parent_job_id != parent.run_identity or binding.task_id != task.task_id
            or binding.attempt_id != attempt.attempt_id
            or _digest(original) != witness.source_checkpoint_digest
            or original["repository_attempt_id"] != witness.repository_attempt_id
            or run.fencing_token != witness.repository_fence
            or prepared["iteration_index"] != witness.iteration_index
            or prepared["request_body_digest"] != witness.request_body_digest
            or response["response_artifact_digest"] != witness.response_readback_digest
            or closure.get("schema") != "repository.adapter_return.v1"
            or closure.get("native_invocation_id") != child.run_identity
            or closure.get("child_fence") != child.fencing_token
            or closure.get("preparation_digest") != prepared["preparation_digest"]
            or closure.get("response_readback_digest") != witness.response_readback_digest
            or closure.get("provider_callback_digest") != response["callback_quiescence_digest"]
            or _source_digest(closure) != witness.callback_quiescence_digest):
        raise DurableJobLeaseError("actual original adapter callback closure changed")
    rows = list((await db.scalars(select(InferenceCostReservation))).all())
    cost = next((row for row in rows if row.operation_id == response["operation_id"]), None)
    if (cost is None or cost.state != "settled" or cost.contact_started_at is None
            or _source_digest(cost.model_dump(mode="json")) != response["accounting_digest"]):
        raise DurableJobLeaseError("original contacted response accounting changed")
    accounting = RepositoryIterationAccountingWitness(
        **{**iteration["accounting_binding"], "group": group,
           "original_deadline_at": _as_utc(run.deadline_at)},
        _canonical_source=source, _request_body_digest=witness.request_body_digest,
        _request_route_digest=prepared["request_route_digest"], _seal=_SEAL)
    await validate_repository_iteration_accounting(db, accounting, run, rows=rows,
        operation_id=cost.operation_id, payload_digest=cost.payload_digest,
        bound_microusd=cost.bound_microusd, deadline_at=cost.deadline_at, already_reserved=True)
    approval = await db.get(ApprovalRequest, approval_binding["approval_id"])
    if (approval is None or approval.status not in {"pending", "approved", "consumed"}
            or approval.fingerprint != approval_binding["fingerprint"]
            or approval.session_id != run.operator_session_id
            or _as_utc(run.deadline_at) <= _utc_now()):
        raise DurableJobLeaseError("exact original pending patch approval required")
    return {"iteration_id": witness.iteration_id, "callback_returned": True,
        "accounting_settled": True, "no_learning": True}


async def recover_repository_wait_witness(service, jobs, *, job_id, owner, iteration_index, _resumed=False):
    from src.model_fabric.effective_policy import configuration_mutation_lock
    async with configuration_mutation_lock:
        return await _recover_repository_wait_witness_held(service, jobs, job_id=job_id,
            owner=owner, iteration_index=iteration_index, _resumed=_resumed)


async def _recover_repository_wait_witness_held(service, jobs, *, job_id, owner, iteration_index, _resumed=False, _completion_witness=None):
    """Reissue the original contacted wait after its exact durable successor.

    This reads the source owner's immutable private snapshot and canonical
    journal. A serialized public witness cannot enter this issuer.
    """
    from dataclasses import replace
    from types import SimpleNamespace
    from src.model_fabric.effective_policy import configuration_mutation_lock
    from src.workflows.job_runtime import DurableJobLeaseError, _as_utc, _utc_now, _digest
    from src.workflows.general_task_guard import (read_manifest, _history,
        issue_repository_child_wait_witness, repository_child_wait_checkpoint_id,
        _repository_checkpoint_payload, assert_general_task_child_phase_current)
    from src.work_board.contracts import GENERAL_TASK_MANIFEST_KEY
    from src.work_board.general_task_runtime_artifacts import verify_general_task_manifest
    from src.db.models import WorkBoardTask, WorkBoardAttempt, Goal
    from sqlalchemy import select
    if _completion_witness is not None:
        from src.workflows.repo_repair_source_recovery import assert_repository_scoped_completion
        assert_repository_scoped_completion(_completion_witness, service=service, jobs=jobs, job_id=job_id, owner=owner)
    _assert_task_publication_configuration(service)
    async with jobs._session() as db:
        run = await jobs._fetch(db, job_id)
        original, work, compiled, group, binding, task_source = read_repository_original(run)
        _assert_repository_original_limits(run, await db.get(Goal, binding.goal_id), _repository_policy_limits())
        if (run.owner_principal_id, run.operator_session_id) != (owner.principal_id, owner.session_id):
            raise DurableJobLeaseError("original repository recovery owner changed")
        if (type(iteration_index) is not int or not 1 <= iteration_index <= work.limits.max_iterations
                or run.status != "running" or _as_utc(run.deadline_at) <= _utc_now()):
            raise DurableJobLeaseError("original repository recovery cutoff or state changed")
        identity = iteration_identity(job_id, original["repository_attempt_id"],
            _source_digest(original["original_input"]), iteration_index)
        closure = _repository_record(run, "repository:proposal:" + identity)
        prepared = _repository_record(run, "repository:prepared:" + identity)
        response = _repository_record(run, "repository:response:" + identity)
        if any(record is None for record in (closure, prepared, response)):
            raise DurableJobLeaseError("actual original callback return is unavailable")
        child = await jobs._fetch(db, binding.invocation_id)
        parent = await jobs._fetch(db, binding.parent_job_id)
        expected_status, expected_reason = ("running", None) if _resumed else ("paused", "repository_child_wait")
        revision_delta = 2 if _resumed else 1
        if (child.status != expected_status or child.failure_reason != expected_reason
                or parent.status != "paused" or parent.failure_reason != "general_task_native_wait"):
            raise DurableJobLeaseError("original contacted-wait recovery phase required")
        if (child.revision != closure["original_child_revision"] + revision_delta
                or parent.revision != closure["original_parent_revision"] + 1
                or _source_digest({k: v for k, v in parent.model_dump(mode="json").items()
                    if k not in {"revision", "updated_at", "checkpoint_receipts_json"}}) != closure["parent_static_digest"]
                or _source_digest({k: v for k, v in child.model_dump(mode="json").items()
                    if k not in {"revision", "status", "failure_reason", "updated_at", "heartbeat_at"}}) != closure["child_static_digest"]):
            raise DurableJobLeaseError("original contacted-wait metadata changed before private read")
        # Existing current native authority/Root/Goal checks precede any
        # read of the private recovery snapshot.
        await assert_general_task_child_phase_current(db, child)
        snapshot = json.loads(service._read_private_artifact(closure["canonical_source_artifact_ref"],
            expected_digest=closure["canonical_source_artifact_digest"]))
        canonical = _CanonicalRepositorySource(**snapshot, _seal=_SEAL)
        assert_repository_canonical_source(canonical)
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
        attempt = await db.get(WorkBoardAttempt, binding.attempt_id)
        envelope = await verify_general_task_manifest(db, parent, task, attempt, read_manifest(parent))
        if envelope.repository_source != task_source or envelope.proposal_group != group:
            raise DurableJobLeaseError("original inspected Task recovery binding changed")
        source_facts = json.loads(service._read_private_artifact(task_source.source_artifact_ref,
            expected_digest=task_source.source_artifact_digest))
        if service.recheck_task_source_snapshot(work, source_facts) != compiled:
            raise DurableJobLeaseError("original physical repository source changed")
        old_child, old_parent = json.loads(canonical.child_row_json), json.loads(canonical.parent_row_json)
        current_child, current_parent = child.model_dump(mode="json"), parent.model_dump(mode="json")
        child_changes = {"revision", "status", "failure_reason", "updated_at", "heartbeat_at"}
        parent_changes = {"revision", "updated_at", "checkpoint_receipts_json"}
        if (child.status != expected_status or child.failure_reason != expected_reason
                or child.revision != old_child["revision"] + revision_delta
                or parent.revision != old_parent["revision"] + 1
                or {k: v for k, v in current_child.items() if k not in child_changes} !=
                    {k: v for k, v in old_child.items() if k not in child_changes}
                or {k: v for k, v in current_parent.items() if k not in parent_changes} !=
                    {k: v for k, v in old_parent.items() if k not in parent_changes}
                or _as_utc(child.lease_expires_at) <= _utc_now()):
            raise DurableJobLeaseError("only the exact original contacted-wait successor may recover")
        witness = issue_repository_child_wait_witness(native_binding=binding, source_binding=canonical,
            repository_job_id=job_id, repository_attempt_id=original["repository_attempt_id"],
            repository_fence=run.fencing_token, iteration_index=iteration_index, iteration_id=identity,
            source_checkpoint_digest=_digest(original), request_body_digest=prepared["request_body_digest"],
            response_readback_digest=response["response_artifact_digest"],
            callback_quiescence_digest=_source_digest(closure))
        wait_id = repository_child_wait_checkpoint_id(binding, identity)
        expected_wait = _repository_checkpoint_payload(witness, phase="contacted_wait", checkpoint_id=wait_id)
        wait_record = _repository_record(parent, wait_id)
        if wait_record != expected_wait:
            raise DurableJobLeaseError("original canonical contacted wait changed")
        old_history = json.loads(canonical.parent_checkpoint_json)
        current_history = _history(parent)
        if ([item for item in current_history if item["checkpoint_id"] not in {wait_id, GENERAL_TASK_MANIFEST_KEY}] !=
                [item for item in old_history if item["checkpoint_id"] != GENERAL_TASK_MANIFEST_KEY]):
            raise DurableJobLeaseError("original contacted-wait retained history changed")
        previous = read_manifest(SimpleNamespace(checkpoint_receipts_json=canonical.parent_checkpoint_json))
        expected_manifest = previous.model_copy(update={"manifest_revision": previous.manifest_revision + 1,
            "required_checkpoint_ids": sorted({item["checkpoint_id"] for item in current_history
                if item["checkpoint_id"].startswith(("general:", "repository:"))})})
        if read_manifest(parent) != expected_manifest:
            raise DurableJobLeaseError("original contacted-wait manifest successor changed")
        def row_json(row):
            return json.dumps(row.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        recovered = replace(canonical, _contacted_wait_parent_json=row_json(parent),
            _contacted_wait_child_json=row_json(child))
        return replace(witness, _source_binding=recovered)


async def execute_repository_iteration(service, jobs, *, job_id, owner, request, principal):
    """Consume an exact manual patch approval and run the fixed CPU owner."""
    import asyncio
    from sqlalchemy import update
    from src.db.models import RepoRepairProposal, ApprovalRequest
    from src.auth.service import authenticate_principal
    from src.api.workflows import RepoRepairResumeRequest
    from src.model_fabric.effective_policy import configuration_mutation_lock
    from src.workflows.job_runtime import DurableJobLeaseError, _as_utc, _utc_now, _canonical
    from src.work_board.repository import _begin_sqlite_immediate
    from src.workflows.repo_repair import (_repair_approval_fingerprint, _proposal_sandbox_authority,
        _proposal_authority_payload, _authority_digest, REPO_REPAIR_APPROVAL_TOOL, REPO_REPAIR_APPROVAL_ACTION)
    from src.execution.repo_sandbox import RepoSandboxJob, assert_repo_iteration_cleanup_witness
    if type(request) is not RepoRepairResumeRequest:
        raise DurableJobLeaseError("closed repository patch resume request required")
    actual = await authenticate_principal(principal.principal_id)
    if actual.session_id != owner.session_id or principal.principal_id != owner.principal_id:
        raise DurableJobLeaseError("original authenticated repository patch owner required")
    async with jobs._session() as db:
        run = await jobs._fetch(db, job_id)
        original, work, compiled, group, binding, task_source = read_repository_original(run)
        proposal = await db.get(RepoRepairProposal, request.proposal_id)
        approval = await db.get(ApprovalRequest, request.approval_id)
        if (proposal is None or approval is None or proposal.workflow_run_id != job_id
                or proposal.owner_principal_id != owner.principal_id or proposal.owner_session_id != owner.session_id
                or proposal.approval_id != approval.id or proposal.revision != request.expected_proposal_revision
                or run.revision != request.expected_job_revision or proposal.status != "awaiting_approval"
                or approval.status != "approved" or approval.owner_principal_id != owner.principal_id
                or approval.operator_session_id != owner.session_id or approval.session_id != owner.session_id
                or approval.tool_name != REPO_REPAIR_APPROVAL_TOOL or approval.action != REPO_REPAIR_APPROVAL_ACTION
                or approval.expires_at is None or _as_utc(approval.expires_at) <= _utc_now()
                or _as_utc(proposal.expires_at) != _as_utc(run.deadline_at)
                or proposal.authority_digest != _authority_digest(_proposal_authority_payload(proposal))
                or approval.fingerprint != _repair_approval_fingerprint(proposal, _as_utc(approval.expires_at))):
            raise DurableJobLeaseError("exact current original manual patch approval required")
        identity = json.loads(proposal.safe_metadata_json)["iteration_id"]
        prepared = _repository_record(run, "repository:prepared:" + identity)
        if prepared is None or _repository_record(run, "repository:execution:" + identity) is not None:
            raise DurableJobLeaseError("repository process invocation is single use")
        child = await jobs._fetch(db, binding.invocation_id)
        needs_wake = child.status == "paused"
    if needs_wake:
        recovered = await recover_repository_wait_witness(service, jobs, job_id=job_id,
            owner=owner, iteration_index=prepared["iteration_index"])
        parent = await jobs.get_job(binding.parent_job_id)
        child_projection = await jobs.get_job(binding.invocation_id)
        await jobs.resume_repository_child_wait(binding.invocation_id,
            owner=child_projection["lease"]["owner"], expected_parent_revision=parent["revision"],
            expected_child_revision=child_projection["revision"], producer_witness=recovered)
    async with configuration_mutation_lock:
        _assert_task_publication_configuration(service)
        context = await _repository_precontact(service, jobs, job_id=job_id, owner=owner)
        if job_id not in service._iterative_lanes:
            from src.work_board.dispatcher import _reserve_repo_repair_execution_capacity
            service._iterative_lanes[job_id] = await _reserve_repo_repair_execution_capacity(
                jobs=jobs, job_id=job_id, attempt_id=original["repository_attempt_id"],
                workspace_root=str(service._workspace()), owner=context["run"].lease_owner,
                fencing_token=context["run"].fencing_token, authority_digest=context["run"].authority_digest,
                execution_deadline_at=original["original_deadline_at"], expected_revision=context["run"].revision)
            context = await _repository_precontact(service, jobs, job_id=job_id, owner=owner)
        posture = await asyncio.to_thread(_iterative_executor_preflight, service, context["compiled"])
        sandbox = _proposal_sandbox_authority(proposal)
        details = json.loads(approval.details_json)
        if (not posture.ok or posture.posture_digest != sandbox["executor_posture_digest"]
                or sandbox["executor_posture_digest"] != prepared["executor_posture_digest"]
                or sandbox.get("required_permissions") != ["local_host_execution"]
                or details.get("required_permissions") != ["local_host_execution"]
                or details.get("executor_kind") != "local" or details.get("proposal_id") != proposal.proposal_id
                or details.get("patch_sha256") != proposal.patch_sha256):
            raise DurableJobLeaseError("exact approved local host execution posture required")
        patch = service._read_private_artifact("workspace-json:" + proposal.patch_artifact_id,
            expected_digest=proposal.patch_sha256)
        cutoff = min(_as_utc(context["run"].deadline_at), _as_utc(context["child"].lease_expires_at),
            _as_utc(context["authority"].attempt.lease_expires_at))
        remaining = int((cutoff - _utc_now()).total_seconds())
        if remaining < 1:
            raise DurableJobLeaseError("original repository process cutoff expired")
        process_binding = RepoIterationProcessBinding(repository_job_id=job_id,
            repository_attempt_id=original["repository_attempt_id"], repository_fence=context["run"].fencing_token,
            iteration_index=prepared["iteration_index"], iteration_id=identity,
            original_deadline_at=_as_utc(context["run"].deadline_at),
            authority_digest=proposal.authority_digest, base_digest=proposal.base_snapshot_digest, _seal=_SEAL)
        job = RepoSandboxJob(job_id=job_id, repository_root=str(service._workspace() / compiled.repository_path),
            patch_bytes=patch, allowed_paths=tuple(json.loads(proposal.allowed_paths_json)),
            test_args=tuple(json.loads(proposal.test_args_json)), authority_digest=proposal.authority_digest,
            base_digest=proposal.base_snapshot_digest, deadline_seconds=min(180, remaining),
            limits_digest=sandbox["sandbox_limits_digest"], execution_deadline_at=cutoff.isoformat(),
            attempt_id=original["repository_attempt_id"], fencing_token=context["run"].fencing_token,
            expected_posture_digest=posture.posture_digest,
            expected_worker_source_sha256=posture.posture.get("worker_source_sha256", ""),
            expected_interpreter_sha256=posture.posture.get("interpreter_sha256", ""),
            expected_pytest_executable_sha256=posture.posture.get("pytest_executable_sha256", ""),
            expected_pytest_package_sha256=posture.posture.get("pytest_package_sha256", ""), iteration_binding=process_binding)
        async with jobs._session() as db:
            await _begin_sqlite_immediate(db)
            await _recheck_repository_sql(db, context)
            live = await db.get(RepoRepairProposal, proposal.proposal_id, populate_existing=True)
            if live is None or live.model_dump(mode="json") != proposal.model_dump(mode="json"):
                raise DurableJobLeaseError("original patch proposal changed before dispatch")
            changed = await db.execute(update(ApprovalRequest).where(
                ApprovalRequest.id == approval.id, ApprovalRequest.status == "approved",
                ApprovalRequest.fingerprint == approval.fingerprint, ApprovalRequest.owner_principal_id == owner.principal_id,
                ApprovalRequest.operator_session_id == owner.session_id, ApprovalRequest.expires_at > _utc_now()
                ).values(status="consumed", resolved_at=_utc_now()))
            if changed.rowcount != 1:
                raise DurableJobLeaseError("original patch approval consume CAS changed")
            current = await jobs._fetch(db, job_id)
            _append_repository_record(current, "repository:execution:" + identity,
                {"schema": "repository.process_intent.v1", "iteration_id": identity, "proposal_id": proposal.proposal_id,
                 "approval_id": approval.id, "approval_fingerprint": approval.fingerprint, "patch_sha256": proposal.patch_sha256,
                 "process_binding": process_binding.projection(), "request_key": request.idempotency_key},
                inventory=repository_checkpoint_inventory(current, work))
            current.revision += 1
            live.status = "execution_started"
            live.revision += 1
            await db.commit()
        context = await _repository_precontact(service, jobs, job_id=job_id, owner=owner)
        loop = asyncio.get_running_loop()
        async def dispatch_sql_guard():
            async with configuration_mutation_lock:
                _assert_task_publication_configuration(service)
                async with jobs._session() as db:
                    await _begin_sqlite_immediate(db)
                    await _recheck_repository_sql(db, context)
                    await db.commit()
        def before_dispatch():
            future = asyncio.run_coroutine_threadsafe(dispatch_sql_guard(), loop)
            future.result(timeout=min(10, max(1, remaining)))
        if read_repository_inventory(context["run"])["schema"] == "repository.checkpoint_inventory.v3":
            from src.workflows.repo_repair_source_recovery import issue_repository_source_producer
            producer_owner = await issue_repository_source_producer(service, jobs, job, owner=owner)
            # Registration advances the canonical epoch before ACK. Each real
            # command is authorized through its private channel after that
            # commit, rather than replaying this pre-registration snapshot.
            task = asyncio.create_task(asyncio.to_thread(service.sandbox.execute_job,
                job, producer_owner=producer_owner))
        else:
            producer_owner = None
            task = asyncio.create_task(asyncio.to_thread(service.sandbox.execute_job,
                job, before_dispatch=before_dispatch))
        service._iterative_process_callbacks[identity] = task
        service._iterative_process_jobs[identity] = job
    try:
        result = await asyncio.shield(task)
        assert_repo_iteration_cleanup_witness(result.get("iteration_cleanup_witness"), job)
    except BaseException:
        # The durable intent and owned transport survive HTTP cancellation.
        # An absent cleanup witness never releases the original root lane.
        service._iterative_lanes[job_id].quarantine(job_id)
        async def quarantine_original():
            await _quarantine_original_uncertainty(service, jobs, job_id=job_id, owner=owner,
                lease_owner=context["run"].lease_owner, fencing_token=context["run"].fencing_token,
                reason="repository_process_closure_unproven",
                result={"no_learning": True, "operator_action": "reconcile_original_process",
                    "iteration_id": identity})
        quarantine = asyncio.create_task(quarantine_original())
        service._iterative_process_callbacks["quarantine:" + identity] = quarantine
        await asyncio.shield(quarantine)
        raise
    cleanup = result.get("iteration_cleanup_witness")
    assert_repo_iteration_cleanup_witness(cleanup, job)
    if not task.done() or task.cancelled() or task.exception() is not None:
        raise DurableJobLeaseError("actual original process owner closure is unproven")
    if producer_owner is not None:
        from src.workflows.repo_repair_source_recovery import (
            publish_original_repository_completion, repository_completion_outcome)
        completion = await publish_original_repository_completion(service, jobs,
            job_id=job_id, owner=owner, iteration_index=prepared["iteration_index"],
            producer_owner=producer_owner, actual_result=result)
        outcome = repository_completion_outcome(completion)
        async with jobs._session() as db:
            stopping = _repository_record(await jobs._fetch(db, job_id), "repository:stop-intent:v1")
        if stopping is not None:
            from src.workflows.repo_repair_stop import stop_repository_root
            stopped = await stop_repository_root(service, jobs, job_id=job_id, owner=owner,
                general_task_service=None, reason=stopping["stop_reason"])
            outcome["stop_pending"] = stopped["pending"]
            outcome["recovery_action"] = "repository_stop_pending" if stopped["pending"] else "repository_stopped"
        elif outcome["status"] == "succeeded":
            outcome["original_child_final"] = await finalize_repository_iteration(service, jobs,
                job_id=job_id, owner=owner, iteration_index=prepared["iteration_index"],
                actual_cleanup=cleanup, actual_job=job, completion_witness=completion)
        elif outcome["status"] == "failed" and prepared["iteration_index"] < work.limits.max_iterations:
            outcome["repository_review"] = await prepare_repository_iteration(service, jobs,
                job_id=job_id, owner=owner, iteration_index=prepared["iteration_index"] + 1)
        elif outcome["status"] == "failed":
            from src.workflows.repo_repair_stop import stop_repository_root
            stopped = await stop_repository_root(service, jobs, job_id=job_id, owner=owner,
                general_task_service=None, reason="iterations_exhausted")
            outcome["stop_pending"] = stopped["pending"]
            outcome["recovery_action"] = "repository_stop_pending" if stopped["pending"] else "original_iterations_exhausted"
        return outcome
    projection = cleanup.projection()
    prefix = "artifacts/repo-repair/model/iteration-" + identity
    cleanup_ref, cleanup_digest = service._write_private_artifact(prefix + "-cleanup.json", _canonical(projection).encode())
    manifest_ref, manifest_digest = service._write_private_artifact(prefix + "-readback.json", result["outputs"]["readback.json"])
    if service._read_private_artifact(manifest_ref, expected_digest=manifest_digest) != result["outputs"]["readback.json"]:
        raise DurableJobLeaseError("literal original process readback changed")
    # These bytes come from the actual supervised command, rather than a
    # model summary. The next preparation rescans and redacts them afresh.
    outputs = result["outputs"]
    diagnostics = {"iteration_id": identity,
        "stdout": outputs["pytest.stdout"].decode("utf-8", errors="replace"),
        "stderr": outputs["pytest.stderr"].decode("utf-8", errors="replace"),
        "stdout_raw_sha256": hashlib.sha256(outputs["pytest.stdout"]).hexdigest(),
        "stderr_raw_sha256": hashlib.sha256(outputs["pytest.stderr"]).hexdigest(),
        "cumulative_diff": outputs["diff.patch"].decode("utf-8", errors="strict"),
        "cumulative_diff_sha256": hashlib.sha256(outputs["diff.patch"]).hexdigest()}
    if work.language_profile == "test_node":
        # Retain every actually executed requested command, including a
        # failed build that prevented the test command from starting.
        commands = result["manifest"]["commands"]
        diagnostics["stdout"] = "\n".join("[" + entry["script"] + " stdout]\n" + outputs[
            ("pytest" if entry["script"] == "test" else "build") + ".stdout"].decode("utf-8", errors="replace")
            for entry in commands)
        diagnostics["stderr"] = "\n".join("[" + entry["script"] + " stderr]\n" + outputs[
            ("pytest" if entry["script"] == "test" else "build") + ".stderr"].decode("utf-8", errors="replace")
            for entry in commands)
        diagnostics["command_output_sha256"] = {name: hashlib.sha256(raw).hexdigest()
            for name, raw in outputs.items() if name in {"pytest.stdout", "pytest.stderr", "build.stdout", "build.stderr"}}
    diagnostics_ref, diagnostics_digest = service._write_private_artifact(prefix + "-diagnostics.json",
        _canonical(diagnostics).encode())
    if service._read_private_artifact(diagnostics_ref, expected_digest=diagnostics_digest) != _canonical(diagnostics).encode():
        raise DurableJobLeaseError("literal original command diagnostics readback changed")
    command_results = _repository_command_results(result["manifest"], node=work.language_profile == "test_node")
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        await _recheck_repository_sql(db, context, allow_stop_cleanup=True)
        current = await jobs._fetch(db, job_id)
        inventory = repository_checkpoint_inventory(current, work)
        _append_repository_record(current, "repository:cleanup:" + identity,
            {"artifact_ref": cleanup_ref, "artifact_digest": cleanup_digest, "cleanup_proven": True,
             "iteration_id": identity, "status": result["status"]}, inventory=inventory)
        _append_repository_record(current, "repository:readback:" + identity,
            {"artifact_ref": manifest_ref, "artifact_digest": manifest_digest, "status": result["status"],
             "manifest_digest": _source_digest(result["manifest"]), "patch_sha256": proposal.patch_sha256,
             "diagnostics_artifact_ref": diagnostics_ref, "diagnostics_artifact_digest": diagnostics_digest,
             "command_results": command_results}, inventory=inventory)
        current.revision += 1
        live = await db.get(RepoRepairProposal, proposal.proposal_id)
        live.status = "execution_verified"
        live.last_receipt_id = "repository:readback:" + identity
        live.revision += 1
        await db.commit()
    outcome = {"job_id": job_id, "iteration_id": identity, "status": result["status"], "cleanup_proven": True,
        "manifest_artifact_ref": manifest_ref, "manifest_artifact_digest": manifest_digest, "no_learning": True}
    async with jobs._session() as db:
        stopping = _repository_record(await jobs._fetch(db, job_id), "repository:stop-intent:v1")
    if stopping is not None:
        from src.workflows.repo_repair_stop import stop_repository_root
        stopped = await stop_repository_root(service, jobs, job_id=job_id, owner=owner,
            general_task_service=None, reason=stopping["stop_reason"])
        outcome["stop_pending"] = stopped["pending"]
        outcome["recovery_action"] = "repository_stop_pending" if stopped["pending"] else "repository_stopped"
        return outcome
    if result["status"] == "succeeded":
        outcome["original_child_final"] = await finalize_repository_iteration(service, jobs,
            job_id=job_id, owner=owner, iteration_index=prepared["iteration_index"],
            actual_cleanup=cleanup, actual_job=job)
    elif result["status"] == "failed" and prepared["iteration_index"] < work.limits.max_iterations:
        outcome["repository_review"] = await prepare_repository_iteration(service, jobs,
            job_id=job_id, owner=owner, iteration_index=prepared["iteration_index"] + 1)
    elif result["status"] == "failed":
        from src.workflows.repo_repair_stop import stop_repository_root
        stopped = await stop_repository_root(service, jobs, job_id=job_id, owner=owner,
            general_task_service=None, reason="iterations_exhausted")
        outcome["stop_pending"] = stopped["pending"]
        outcome["recovery_action"] = "repository_stop_pending" if stopped["pending"] else "original_iterations_exhausted"
    return outcome


def _recovered_finalizer_slot(witness):
    from src.workflows.repo_repair_source_recovery import repository_completion_finalizer_state
    return repository_completion_finalizer_state(witness)


def _recovered_descriptor(kind, value):
    """Comparison data only; the existing completion supplies authority."""
    if kind == "artifact":
        raw = value["content"]
        raw = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        return {"file_path": value["file_path"], "artifact_type": value["artifact_type"],
            "content_sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
    if kind == "publish":
        return {"parent_id": value["parent_id"], "child_id": value["child_id"],
            "expected_parent_revision": value["expected_parent_revision"],
            "staged_artifact": value["staged_artifact"], "repository_final_witness": value["repository_final_witness"]}
    return value


async def _recovered_repository_terminal_events(db, *, task_id):
    from sqlalchemy import select
    from src.db.models import WorkBoardEvent
    return list((await db.scalars(select(WorkBoardEvent).where(
        WorkBoardEvent.task_id == task_id,
        WorkBoardEvent.kind == "attempt.repository_verified",
    ).order_by(WorkBoardEvent.event_id).limit(2).execution_options(populate_existing=True))).all())


async def _stage_recovered_repository_native_writer(jobs, state):
    """Read only the original native root and two bounded receipt roles before SQL."""
    from config.settings import settings
    from src.work_board.pipelines import root_binding
    from src.work_board.pipeline_contracts import digest
    from src.work_board.contracts import GeneralTaskArtifactRef
    from src.work_board.general_task_runtime_artifacts import read_native_artifact_reference
    from src.workflows.general_task_guard import (child_binding, read_manifest, _step_receipt,
        assert_general_task_child_current, verify_native_approval_transition)
    from src.workflows.job_runtime import DurableJobLeaseError, _canonical
    bound = state["bound"]
    binding = bound["context"]["binding"]
    workspace_dir = settings.workspace_dir
    physical_root = root_binding()
    if (digest(physical_root) != binding.live_root_digest
            or [physical_root["device"], physical_root["inode"]]
                != bound["registration"]["native_host_binding"]["workspace_identity"]):
        raise DurableJobLeaseError("recovered original native workspace changed")
    async with jobs._session() as db:
        child = await jobs._fetch(db, binding.invocation_id)
        parent = await jobs._fetch(db, binding.parent_job_id)
        for row in (child, parent):
            if state["rows"].get((type(row), row.id)) != _canonical(row.model_dump(mode="json")):
                raise DurableJobLeaseError("recovered original native staging rows changed")
        if child_binding(child) != binding:
            raise DurableJobLeaseError("recovered original native staging binding changed")
        await assert_general_task_child_current(db, child)
        manifest = read_manifest(parent)
        receipt = _step_receipt(manifest, binding.step_id)
        index = manifest.step_ids.index(binding.step_id)
        reference = GeneralTaskArtifactRef(artifact_id=manifest.step_receipt_artifact_ids[index],
            digest=manifest.step_receipt_digests[index], schema_version=manifest.step_receipt_schemas[index])
        receipts = {"claim": {"reference": reference, "receipt": receipt,
            "receipt_json": _canonical(receipt.model_dump(mode="json"))}}
        if (manifest.phase != "native_wait" or manifest.phase_revision != binding.phase_revision
                or manifest.phase_digest != binding.phase_digest):
            witness, _approval = await verify_native_approval_transition(db, child, parent)
            awaiting = read_native_artifact_reference(witness.awaiting_receipt,
                parent_job_id=parent.run_identity, creation_digest=binding.creation_digest)
            receipts["awaiting"] = {"reference": witness.awaiting_receipt, "receipt": awaiting,
                "receipt_json": _canonical(awaiting.model_dump(mode="json"))}
        if settings.workspace_dir != workspace_dir:
            raise DurableJobLeaseError("recovered original native staging configuration changed")
        return {"root": dict(physical_root), "workspace_dir": workspace_dir,
            "child_json": _canonical(child.model_dump(mode="json")),
            "parent_json": _canonical(parent.model_dump(mode="json")),
            "manifest_json": _canonical(manifest.model_dump(mode="json")), "receipts": receipts}


def _recovered_repository_native_stage(completion_witness, *, run, parent=None, manifest=None):
    """The active original Source phase supplies staged data, never caller authority."""
    from config.settings import settings
    from src.workflows.job_runtime import DurableJobLeaseError, _canonical
    from src.workflows.general_task_guard import child_binding
    state = _recovered_finalizer_slot(completion_witness)["state"]
    pending = state["pending"] if state is not None else None
    if (pending is None or not pending["entered"] or state["phase"] not in range(4)
            or pending["kind"] not in {"checkpoint", "artifact", "readback"}
            or pending.get("writer_run") is not run or pending["job_id"] != run.run_identity):
        raise DurableJobLeaseError("recovered original native writer stage required")
    staged = pending["native"]
    binding = state["bound"]["context"]["binding"]
    if (settings.workspace_dir != staged["workspace_dir"] or child_binding(run) != binding
            or _canonical(run.model_dump(mode="json")) != staged["child_json"]
            or parent is not None and _canonical(parent.model_dump(mode="json")) != staged["parent_json"]
            or manifest is not None and _canonical(manifest.model_dump(mode="json")) != staged["manifest_json"]):
        raise DurableJobLeaseError("recovered original native writer binding changed")
    return staged, binding


def _recovered_repository_native_root_digest(completion_witness, *, run):
    from src.work_board.pipeline_contracts import digest
    from src.workflows.general_task_guard import _VerifiedParentJournal, _PHASE_SQL_SEAL
    from src.workflows.job_runtime import DurableJobLeaseError
    staged, _binding = _recovered_repository_native_stage(completion_witness, run=run)
    verified = getattr(run, "_general_task_verified_parent_journal", None)
    parent = json.loads(staged["parent_json"])
    if (type(verified) is not _VerifiedParentJournal or verified._seal is not _PHASE_SQL_SEAL
            or verified.child_id != run.run_identity or verified.child_fence != run.fencing_token
            or verified.checkpoint_json != parent["checkpoint_receipts_json"]
            or verified.authority_json != parent["declared_authority_json"]):
        raise DurableJobLeaseError("recovered original native parent journal changed")
    return digest(staged["root"])


def _recovered_repository_native_claim_receipt(completion_witness, *, run, parent, manifest):
    from src.workflows.job_runtime import DurableJobLeaseError, _canonical
    staged, binding = _recovered_repository_native_stage(
        completion_witness, run=run, parent=parent, manifest=manifest)
    entry = staged["receipts"]["claim"]
    index = manifest.step_ids.index(binding.step_id)
    reference = entry["reference"]
    if ((reference.artifact_id, reference.digest, reference.schema_version) != (
            manifest.step_receipt_artifact_ids[index], manifest.step_receipt_digests[index],
            manifest.step_receipt_schemas[index])
            or _canonical(entry["receipt"].model_dump(mode="json")) != entry["receipt_json"]):
        raise DurableJobLeaseError("recovered original native claim receipt changed")
    return entry["receipt"]


def _recovered_repository_native_awaiting_receipt(completion_witness, *, run, parent, manifest, reference):
    from src.workflows.job_runtime import DurableJobLeaseError, _canonical
    staged, _binding = _recovered_repository_native_stage(
        completion_witness, run=run, parent=parent, manifest=manifest)
    entry = staged["receipts"].get("awaiting")
    if (entry is None or _canonical(reference.model_dump(mode="json")) != _canonical(
            entry["reference"].model_dump(mode="json"))
            or _canonical(entry["receipt"].model_dump(mode="json")) != entry["receipt_json"]):
        raise DurableJobLeaseError("recovered original native awaiting receipt changed")
    return entry["receipt"]


async def _expect_recovered_repository_final_writer(jobs, *, completion_witness, kind, job_id, descriptor):
    from src.workflows.repo_repair_source_recovery import repository_completion_scoped_binding
    from src.workflows.job_runtime import DurableJobLeaseError, _canonical
    bound = repository_completion_scoped_binding(completion_witness)
    slot = _recovered_finalizer_slot(completion_witness)
    state = slot["state"]
    if state is None:
        rows = {}
        for model, key, raw in bound["committed_rows"]:
            identity = (model, key)
            if identity in rows and rows[identity] != raw:
                raise DurableJobLeaseError("recovered finalizer initial row ambiguity")
            rows[identity] = raw
        state = {"rows": rows, "phase": 0, "pending": None, "bound": bound}
        slot["state"] = state
    sequence = ("checkpoint", "artifact", "readback", "readback", "publish",
        "artifact", "artifact", "artifact", "artifact", "readback", "terminal")
    binding = bound["context"]["binding"]
    targets = (binding.invocation_id,) * 5 + (bound["job_id"],) * 6
    phase = state["phase"]
    if (phase >= len(sequence) or sequence[phase] != kind or targets[phase] != job_id
            or state["pending"] is not None):
        raise DurableJobLeaseError("recovered finalizer write order changed")
    pending = {"kind": kind, "job_id": job_id, "descriptor": _recovered_descriptor(kind, descriptor),
        "entered": False, "after": None}
    if phase in range(4):
        pending["native"] = await _stage_recovered_repository_native_writer(jobs, state)
    if kind == "artifact":
        from src.artifacts.registry import build_artifact_record
        target = next(json.loads(raw) for raw in state["rows"].values()
            if json.loads(raw).get("run_identity") == job_id)
        record = build_artifact_record(file_path=descriptor["file_path"], artifact_type=descriptor["artifact_type"],
            content=descriptor["content"], producer=target["job_kind"], run_id=job_id, session_id=target["session_id"])
        pending["artifact_receipt"] = {key: record[key] for key in (
            "artifact_id", "artifact_type", "file_path", "producer", "content_sha256", "size_bytes", "exists")}
    if kind == "publish":
        from src.work_board.general_task_runtime_artifacts import verify_staged_task_artifact
        pending["publication_artifact"] = verify_staged_task_artifact(descriptor["staged_artifact"],
            parent_job_id=binding.parent_job_id, creation_digest=binding.creation_digest)
    if kind == "terminal":
        from src.db.models import WorkBoardTask
        from src.work_board.repository import _safe_metadata
        original = bound["context"]["original"]
        tasks = [json.loads(raw) for (model, _key), raw in state["rows"].items()
            if model is WorkBoardTask and json.loads(raw)["task_id"] == original["repository_task_id"]]
        if len(tasks) != 1:
            raise DurableJobLeaseError("recovered original terminal event task changed")
        task = tasks[0]
        pending["terminal_event"] = {"task_id": task["task_id"],
            "owner_principal_id": task["owner_principal_id"], "owner_session_id": task["owner_session_id"],
            "actor_principal_id": bound["owner"].principal_id, "actor_session_id": bound["owner"].session_id,
            "kind": "attempt.repository_verified", "metadata_json": _safe_metadata({
                "attempt_id": original["repository_attempt_id"], "workflow_run_id": bound["job_id"],
                "readback_id": "repository-final:" + bound["iteration_id"], "no_learning": True}),
            "mutation_idempotency_key": None, "mutation_request_digest": None}
        async with jobs._session() as db:
            if await _recovered_repository_terminal_events(db, task_id=task["task_id"]):
                raise DurableJobLeaseError("recovered original terminal event already exists")
    state["pending"] = pending


def _recovered_repository_publication_artifact(completion_witness, *, staged_artifact):
    from src.workflows.job_runtime import DurableJobLeaseError
    state = _recovered_finalizer_slot(completion_witness)["state"]
    pending = state["pending"] if state is not None else None
    if (pending is None or pending["kind"] != "publish" or not pending["entered"]
            or pending["descriptor"]["staged_artifact"] is not staged_artifact):
        raise DurableJobLeaseError("recovered original publication stage required")
    receipt, record = pending["publication_artifact"]
    return receipt, dict(record)


async def _recheck_recovered_finalizer_rows(db, jobs, state, witness):
    from src.workflows.repo_repair_source_recovery import (
        assert_repository_scoped_completion, _repository_original_accounting_rows)
    from src.workflows.job_runtime import DurableJobLeaseError, _canonical, _as_utc, _utc_now
    from src.db.models import WorkflowRunState, OperatorSession, WorkBoardInputArtifact, InferenceCostReservation
    from src.workflows.general_task_accounting import validate_group_owner
    bound = state["bound"]
    assert_repository_scoped_completion(witness, service=bound["fence"].service,
        jobs=jobs, job_id=bound["job_id"], owner=bound["owner"])
    for (model, key), expected in state["rows"].items():
        row = await db.get(model, key, populate_existing=True)
        if row is None or _canonical(row.model_dump(mode="json")) != expected:
            raise DurableJobLeaseError("recovered finalizer complete current rows changed")
        if isinstance(row, OperatorSession) and (row.revoked_at is not None or row.replaced_by_id or row.is_bearer_tombstone
                or _as_utc(row.idle_expires_at) <= _utc_now() or _as_utc(row.absolute_expires_at) <= _utc_now()):
            raise DurableJobLeaseError("recovered finalizer original operator expired")
        if isinstance(row, WorkBoardInputArtifact) and _as_utc(row.expires_at) <= _utc_now():
            raise DurableJobLeaseError("recovered finalizer original input expired")
    root = await jobs._fetch(db, bound["job_id"])
    context = bound["context"]
    if (root.status != "running" or _repository_record(root, "repository:stop-intent:v1") is not None
            or _as_utc(root.deadline_at) <= _utc_now()
            or _as_utc(root.lease_expires_at) is None or _as_utc(root.lease_expires_at) <= _utc_now()):
        raise DurableJobLeaseError("recovered finalizer original Root is not current Running authority")
    if _utc_now() >= context["binding"].native_deadline_at:
        raise DurableJobLeaseError("recovered finalizer original native cutoff expired")
    authority_attempt = context["authority"].attempt
    current_attempt = await db.get(type(authority_attempt), authority_attempt.attempt_id, populate_existing=True)
    if (current_attempt is None or current_attempt.cancel_requested_at is not None
            or _as_utc(current_attempt.lease_expires_at) is None
            or _as_utc(current_attempt.lease_expires_at) <= _utc_now()):
        raise DurableJobLeaseError("recovered finalizer original attempt authority expired")
    await validate_group_owner(db, context["group"])
    accounting = await _repository_original_accounting_rows(db, context, job_id=bound["job_id"])
    expected_accounting = {key: raw for (model, key), raw in state["rows"].items() if model is InferenceCostReservation}
    if {row.operation_id: _canonical(row.model_dump(mode="json")) for row in accounting} != expected_accounting:
        raise DurableJobLeaseError("recovered finalizer reservation membership changed")
    if any(row.job_id == bound["job_id"] and row.state != "settled" for row in accounting):
        raise DurableJobLeaseError("recovered finalizer original liability is unsettled")


async def verify_recovered_repository_final_writer(jobs, db, run, *, completion_witness, kind, descriptor):
    from src.workflows.job_runtime import DurableJobLeaseError, _canonical, _utc_now
    state = _recovered_finalizer_slot(completion_witness)["state"]
    pending = state["pending"] if state is not None else None
    if (pending is None or pending["entered"] or pending["kind"] != kind
            or pending["job_id"] != run.run_identity):
        raise DurableJobLeaseError("recovered finalizer pending original write required")
    actual = _recovered_descriptor(kind, descriptor)
    expected = pending["descriptor"]
    if kind == "publish":
        if (actual["repository_final_witness"] is not expected["repository_final_witness"]
                or actual["staged_artifact"] is not expected["staged_artifact"]
                or any(actual[k] != expected[k] for k in ("parent_id", "child_id", "expected_parent_revision"))):
            raise DurableJobLeaseError("recovered finalizer publication descriptor changed")
    elif _canonical(actual) != _canonical(expected):
        raise DurableJobLeaseError("recovered finalizer original descriptor changed")
    await _recheck_recovered_finalizer_rows(db, jobs, state, completion_witness)
    if kind == "terminal" and await _recovered_repository_terminal_events(
            db, task_id=pending["terminal_event"]["task_id"]):
        raise DurableJobLeaseError("recovered original terminal event absence changed")
    if kind == "publish":
        state["final_witness"] = descriptor["repository_final_witness"]
    if state.get("final_witness") is not None:
        from src.workflows.repo_repair_source_recovery import assert_repository_completion_final_source
        final = state["final_witness"]
        assert_repository_completion_final_source(final._source_binding._final_source, final_witness=final)
    pending["entered"], pending["since"], pending["writer_run"] = True, _utc_now(), run


def _recovered_publish_delta(old, current, pending, timestamp, *, child_id, parent_id):
    from src.workflows.job_runtime import _canonical, _digest
    from src.workflows.general_task_guard import (
        GENERAL_TASK_MANIFEST_KEY, GeneralTaskCurrentManifestV1, _repository_checkpoint_payload,
        repository_child_final_checkpoint_id)
    final = pending["descriptor"]["repository_final_witness"]
    if current.get("run_identity") == child_id:
        now = timestamp(current["updated_at"])
        return {**old, "revision": old["revision"] + 1, "status": "succeeded", "failure_reason": None,
            "lease_owner": None, "lease_expires_at": None, "finished_at": now,
            "result_digest": final.final_artifact_digest,
            "result_summary": "Repository cumulative repair physically verified", "updated_at": now, "heartbeat_at": now}
    if current.get("run_identity") != parent_id:
        return old
    history = json.loads(old["checkpoint_receipts_json"] or "[]")
    previous_payload = next(item["payload"] for item in history if item["checkpoint_id"] == GENERAL_TASK_MANIFEST_KEY)
    previous = GeneralTaskCurrentManifestV1.model_validate(previous_payload)
    staged = pending["descriptor"]["staged_artifact"]
    final_id = repository_child_final_checkpoint_id(final.wait_witness.native_binding, final.wait_witness.iteration_id)
    payload = _repository_checkpoint_payload(final, phase="final_verified", checkpoint_id=final_id)
    actual_history = json.loads(current["checkpoint_receipts_json"] or "[]")
    final_record = next(item for item in actual_history if item["checkpoint_id"] == final_id)
    record = {"checkpoint_id": final_id, "safe": True, "payload": payload, "state_digest": _digest(payload),
        "state_keys": sorted(payload), "fencing_token": previous.job_fence,
        "recorded_at": timestamp(final_record["recorded_at"])}
    retained = [item for item in history if item["checkpoint_id"] != final_id] + [record]
    refs = dict(zip(previous.step_ids, zip(previous.step_receipt_artifact_ids,
        previous.step_receipt_digests, previous.step_receipt_schemas)))
    refs[final.wait_witness.native_binding.step_id] = (staged.reference.artifact_id, staged.reference.digest, "StepReceipt.v1")
    steps = sorted(refs)
    manifest = GeneralTaskCurrentManifestV1.model_validate({**previous_payload,
        "manifest_revision": previous.manifest_revision + 1, "step_ids": steps,
        "step_receipt_artifact_ids": [refs[key][0] for key in steps],
        "step_receipt_digests": [refs[key][1] for key in steps], "step_receipt_schemas": [refs[key][2] for key in steps],
        "required_checkpoint_ids": sorted({item["checkpoint_id"] for item in retained
            if item["checkpoint_id"].startswith(("general:", "repository:"))})}).model_dump(mode="json")
    observed = next(item for item in actual_history if item["checkpoint_id"] == GENERAL_TASK_MANIFEST_KEY)
    manifest_record = {"checkpoint_id": GENERAL_TASK_MANIFEST_KEY, "state_digest": _digest(manifest),
        "state_keys": sorted(manifest), "safe": True, "payload": manifest, "fencing_token": previous.job_fence,
        "recorded_at": timestamp(observed["recorded_at"])}
    expected_history = [item for item in retained if item["checkpoint_id"] != GENERAL_TASK_MANIFEST_KEY] + [manifest_record]
    _, artifact_record = pending["publication_artifact"]
    artifacts = json.loads(old["artifact_receipts_json"] or "[]")
    observed_artifact = next(item for item in json.loads(current["artifact_receipts_json"] or "[]")
        if item["artifact_id"] == artifact_record["artifact_id"])
    expected_artifacts = [item for item in artifacts if item["artifact_id"] != artifact_record["artifact_id"]] + [
        {**artifact_record, "recorded_at": timestamp(observed_artifact["recorded_at"])}]
    return {**old, "revision": old["revision"] + 1, "updated_at": timestamp(current["updated_at"]),
        "checkpoint_receipts_json": _canonical(expected_history), "artifact_receipts_json": _canonical(expected_artifacts)}


def _recovered_terminal_delta(old, current, pending, timestamp, bound):
    from src.workflows.job_runtime import _canonical, _digest, DurableJobLeaseError
    original, descriptor = bound["context"]["original"], pending["descriptor"]
    if current.get("run_identity") == bound["job_id"]:
        history = json.loads(old["checkpoint_receipts_json"] or "[]")
        actual = json.loads(current["checkpoint_receipts_json"] or "[]")
        if len(actual) != len(history) + 2 or _canonical(actual[:-2]) != _canonical(history):
            raise DurableJobLeaseError("recovered original terminal append sequence changed")
        hold = next(item["payload"] for item in reversed(history)
            if item["checkpoint_id"] == "repo-repair-execution-reservation")
        release = {"kind": "repo_repair_execution_reservation", "status": "released", "job_id": bound["job_id"],
            "attempt_id": original["repository_attempt_id"], "fence": old["fencing_token"],
            "authority_digest": old["authority_digest"], "execution_deadline_at": hold["execution_deadline_at"],
            "outcome_status": "succeeded", "cleanup_proven": True, "readback_verified": True,
            "operator_visible": True, "recorded_at": timestamp(actual[-1]["payload"]["recorded_at"])}
        records = []
        for observed, identity, payload in ((actual[-2], "repository:terminal:v1", descriptor["terminal_payload"]),
                (actual[-1], "repo-repair-execution-release", release)):
            records.append({"checkpoint_id": identity, "safe": True, "payload": payload,
                "state_digest": _digest(payload), "created_at": timestamp(observed["created_at"])})
        return {**old, "revision": old["revision"] + 1, "updated_at": timestamp(current["updated_at"]),
            "checkpoint_receipts_json": _canonical(history + records), "status": "succeeded",
            "finished_at": timestamp(current["finished_at"]), "lease_owner": None, "lease_expires_at": None,
            "result_digest": _source_digest(descriptor["result_payload"]),
            "result_summary": "repository_cumulative_repair_verified"}
    if current.get("task_id") == original["repository_task_id"] and "task_revision" in current:
        now = timestamp(current["updated_at"])
        return {**old, "status": "done", "task_revision": old["task_revision"] + 1,
            "updated_at": now, "completed_at": now, "block_kind": None, "block_reason": None,
            "block_source_status": None,
            "result_refs_json": _canonical([{"job_id": bound["job_id"], "status": "succeeded", "no_learning": True}])}
    if current.get("attempt_id") == original["repository_attempt_id"]:
        now = timestamp(current["updated_at"])
        return {**old, "outcome": "repository_cumulative_repair_verified", "ended_at": now,
            "lease_owner": None, "lease_expires_at": None, "updated_at": now}
    return old


async def capture_recovered_repository_final_writer(jobs, db, run, *, completion_witness, kind,
        _terminal_event=None):
    """Freeze a verified finite SQL delta; publication is not yet observed."""
    from types import SimpleNamespace
    from src.workflows.job_runtime import DurableJobLeaseError, _canonical, _digest, _as_utc, _utc_now
    state = _recovered_finalizer_slot(completion_witness)["state"]
    pending = state["pending"] if state is not None else None
    if pending is None or not pending["entered"] or pending["kind"] != kind or pending["after"] is not None:
        raise DurableJobLeaseError("recovered finalizer write capture order changed")
    if kind not in {"checkpoint", "artifact", "readback", "publish", "terminal"}:
        raise DurableJobLeaseError("recovered finalizer finite publication delta is not integrated")
    def timestamp(value):
        parsed = _as_utc(datetime.fromisoformat(value))
        if parsed is None or not pending["since"] <= parsed <= _utc_now():
            raise DurableJobLeaseError("recovered finalizer writer timestamp changed")
        return value
    after = {}
    for identity, raw in state["rows"].items():
        model, key = identity
        actual = await db.get(model, key, populate_existing=True)
        if actual is None:
            raise DurableJobLeaseError("recovered finalizer row disappeared")
        current, old = actual.model_dump(mode="json"), json.loads(raw)
        expected = old
        if kind == "publish":
            binding = state["bound"]["context"]["binding"]
            expected = _recovered_publish_delta(old, current, pending, timestamp,
                child_id=binding.invocation_id, parent_id=binding.parent_job_id)
        elif kind == "terminal":
            expected = _recovered_terminal_delta(old, current, pending, timestamp, state["bound"])
        elif current.get("run_identity") == pending["job_id"]:
            expected = {**old, "revision": old["revision"] + 1,
                "updated_at": timestamp(current["updated_at"]), "heartbeat_at": current["updated_at"]}
            descriptor = pending["descriptor"]
            if kind == "checkpoint":
                history = json.loads(old["checkpoint_receipts_json"] or "[]")
                actual_history = json.loads(current["checkpoint_receipts_json"] or "[]")
                record = {"checkpoint_id": descriptor["checkpoint_id"], "state_digest": _digest(descriptor["state"]),
                    "state_keys": sorted(str(key) for key in descriptor["state"]), "safe": descriptor["safe"],
                    "recorded_at": timestamp(actual_history[-1]["recorded_at"]), "fencing_token": old["fencing_token"],
                    "payload": descriptor["checkpoint_payload"]}
                expected["checkpoint_receipts_json"] = _canonical([item for item in history
                    if item.get("checkpoint_id") != record["checkpoint_id"]] + [record])
            elif kind == "artifact":
                history = json.loads(old["artifact_receipts_json"] or "[]")
                actual_history = json.loads(current["artifact_receipts_json"] or "[]")
                record = {**pending["artifact_receipt"], "recorded_at": timestamp(actual_history[-1]["recorded_at"])}
                expected["artifact_receipts_json"] = _canonical([item for item in history
                    if item.get("artifact_id") != record["artifact_id"]] + [record])
            else:
                from src.workflows.job_runtime import _resolve_readback_observations, _job_effect_ledger, _safe_structure
                history = json.loads(old["effect_receipts_json"] or "[]")
                actual_history = json.loads(current["effect_receipts_json"] or "[]")
                observed = actual_history[-1]
                effect_id = "eff_" + _digest({"job_id": old["run_identity"], "receipt_kind": "readback",
                    "effect_type": descriptor["effect_type"], "target_path": descriptor["target_path"] or "",
                    "target_digest": descriptor["target_digest"] or "", "adapter_idempotency_key": ""})[:24]
                record = {"effect_id": effect_id, "receipt_kind": "readback", "effect_type": descriptor["effect_type"],
                    "target_path": descriptor["target_path"], "target_digest": descriptor["target_digest"],
                    "approval_id": None, "adapter_idempotency_key": None, "status": descriptor["status"],
                    "content_sha256": descriptor["content_sha256"], "details": _safe_structure(descriptor["details"]),
                    "recorded_at": timestamp(observed["recorded_at"]), "fencing_token": old["fencing_token"],
                    "readback_id": descriptor["readback_id"], "verified_at": descriptor["verified_at"],
                    "reconciled": True, "reconciliation_status": "resolved"}
                history = _resolve_readback_observations(history, record)
                history = [item for item in history if item.get("effect_id") != effect_id] + [record]
                expected["effect_receipts_json"] = _canonical(_job_effect_ledger(SimpleNamespace(**old), history))
        if _canonical(current) != _canonical(expected):
            raise DurableJobLeaseError("recovered finalizer exact original write delta changed")
        after[identity] = _canonical(current)
    if kind == "terminal":
        from src.db.models import WorkBoardEvent
        events = await _recovered_repository_terminal_events(db, task_id=pending["terminal_event"]["task_id"])
        if (type(_terminal_event) is not WorkBoardEvent or type(_terminal_event.event_id) is not int
                or _terminal_event.event_id <= 0 or len(events) != 1 or events[0] is not _terminal_event):
            raise DurableJobLeaseError("recovered original terminal event insertion changed")
        event = _terminal_event.model_dump(mode="json")
        expected = {**pending["terminal_event"], "event_id": _terminal_event.event_id,
            "created_at": timestamp(event["created_at"])}
        if _canonical(event) != _canonical(expected):
            raise DurableJobLeaseError("recovered original terminal event fields changed")
        after[(WorkBoardEvent, _terminal_event.event_id)] = _canonical(event)
    elif _terminal_event is not None:
        raise DurableJobLeaseError("recovered terminal event cannot enter another write")
    from src.db.models import OperatorSession, WorkBoardInputArtifact
    bound = state["bound"]
    if (_utc_now() >= bound["context"]["binding"].native_deadline_at
            or _utc_now() >= _as_utc(bound["context"]["run"].deadline_at)):
        raise DurableJobLeaseError("recovered finalizer original cutoff expired before commit")
    for (model, key), raw in after.items():
        if model is OperatorSession:
            value = json.loads(raw)
            if (_utc_now() >= _as_utc(datetime.fromisoformat(value["idle_expires_at"]))
                    or _utc_now() >= _as_utc(datetime.fromisoformat(value["absolute_expires_at"]))):
                raise DurableJobLeaseError("recovered finalizer operator expired before commit")
        elif model is WorkBoardInputArtifact:
            if _utc_now() >= _as_utc(datetime.fromisoformat(json.loads(raw)["expires_at"])):
                raise DurableJobLeaseError("recovered finalizer input expired before commit")
    pending["after"] = after


async def observe_recovered_repository_final_writer(jobs, *, completion_witness, kind):
    from src.workflows.job_runtime import DurableJobLeaseError, _canonical
    state = _recovered_finalizer_slot(completion_witness)["state"]
    pending = state["pending"] if state is not None else None
    if pending is None or pending["kind"] != kind or pending["after"] is None:
        raise DurableJobLeaseError("recovered finalizer committed write capture required")
    async with jobs._session() as db:
        for (model, key), expected in pending["after"].items():
            row = await db.get(model, key, populate_existing=True)
            if row is None or _canonical(row.model_dump(mode="json")) != expected:
                raise DurableJobLeaseError("recovered finalizer committed readback changed")
        if kind == "terminal":
            from src.db.models import WorkBoardEvent
            events = await _recovered_repository_terminal_events(db, task_id=pending["terminal_event"]["task_id"])
            expected_events = [raw for (model, _key), raw in pending["after"].items() if model is WorkBoardEvent]
            if len(events) != 1 or [_canonical(event.model_dump(mode="json")) for event in events] != expected_events:
                raise DurableJobLeaseError("recovered original terminal event committed membership changed")
    if "native" in pending:
        from config.settings import settings
        from src.work_board.pipelines import root_binding
        from src.work_board.general_task_runtime_artifacts import read_native_artifact_reference
        staged = pending["native"]
        binding = state["bound"]["context"]["binding"]
        if settings.workspace_dir != staged["workspace_dir"] or _canonical(root_binding()) != _canonical(staged["root"]):
            raise DurableJobLeaseError("recovered original native workspace postcommit changed")
        for entry in staged["receipts"].values():
            receipt = read_native_artifact_reference(entry["reference"],
                parent_job_id=binding.parent_job_id, creation_digest=binding.creation_digest)
            if _canonical(receipt.model_dump(mode="json")) != entry["receipt_json"]:
                raise DurableJobLeaseError("recovered original native receipt postcommit changed")
        _recovered_finalizer_slot(completion_witness)
    state["rows"], state["phase"], state["pending"] = pending["after"], state["phase"] + 1, None


async def _validate_recovered_repository_final_evidence(service, jobs, *, completion_witness, evidence):
    """Derive the original finite final evidence from actual rows and bytes."""
    from sqlalchemy import select
    from src.db.models import InferenceCostReservation
    from src.workflows.repo_repair_source_recovery import (
        assert_repository_scoped_completion, repository_completion_scoped_binding,
        repository_completion_result, repository_completion_physical_projection)
    from src.workflows.repo_repair import RepoIteration, RepoWorkVerifiedResult, _repair_test_args
    from src.work_board.general_task import canonical, digest
    from src.workflows.job_runtime import DurableJobLeaseError, _canonical
    bound = repository_completion_scoped_binding(completion_witness)
    job_id, owner = bound["job_id"], bound["owner"]
    assert_repository_scoped_completion(completion_witness, service=service, jobs=jobs,
        job_id=job_id, owner=owner)
    context = await _repository_precontact(service, jobs, job_id=job_id, owner=owner)
    run, original, binding = context["run"], context["original"], context["binding"]
    result = repository_completion_result(completion_witness)
    projection = repository_completion_physical_projection(completion_witness)
    index = bound["registration"]["iteration_index"]
    iterations, identities = [], []
    for ordinal in range(1, index + 1):
        identity = iteration_identity(job_id, original["repository_attempt_id"],
            _source_digest(original["original_input"]), ordinal)
        prepared = _repository_record(run, "repository:prepared:" + identity)
        execution = _repository_record(run, "repository:execution:" + identity)
        cleanup = _repository_record(run, "repository:cleanup:" + identity)
        readback = _repository_record(run, "repository:readback:" + identity)
        if (any(item is None for item in (prepared, execution, cleanup, readback))
                or cleanup.get("cleanup_proven") is not True
                or readback["status"] != ("succeeded" if ordinal == index else "failed")):
            raise DurableJobLeaseError("recovered final evidence requires the exact complete chain")
        identities.append(identity)
        iterations.append(RepoIteration(index=ordinal, input_tree_digest=prepared["input_tree_digest"],
            patch_digest=execution["patch_sha256"], command_refs=["repository:execution:" + identity],
            result_artifacts=["repository:cleanup:" + identity, "repository:readback:" + identity]))
    identity = identities[-1]
    readback = _repository_record(run, "repository:readback:" + identity)
    cleanup = _repository_record(run, "repository:cleanup:" + identity)
    execution = _repository_record(run, "repository:execution:" + identity)
    patch = _repository_record(run, "repository:patch:" + identity)
    manifest_bytes = service._read_private_artifact(readback["artifact_ref"], expected_digest=readback["artifact_digest"])
    manifest = json.loads(manifest_bytes)
    if (result["status"] != "succeeded" or _canonical(result["manifest"]) != _canonical(manifest)
            or manifest_bytes != result["outputs"]["readback.json"]
            or _source_digest(manifest) != readback["manifest_digest"]
            or projection["artifact_digests"].get("readback.json") != readback["artifact_digest"]):
        raise DurableJobLeaseError("recovered final evidence signed readback changed")
    output = RepoWorkVerifiedResult(iterations=iterations, patch_artifact_ref=patch["patch_artifact_ref"],
        final_readback_ref=readback["artifact_ref"]).model_dump(mode="json")
    raw = canonical({"step_id": binding.step_id, "output": output})
    sha = hashlib.sha256(raw).hexdigest()
    key = digest([binding.invocation_id, binding.plan_digest, binding.step_id])
    path = f"artifacts/work-board/general-tasks/{key}-{sha}.json"
    actual = service._read_private_artifact("workspace-json:" + path, expected_digest=sha)
    child = await jobs.get_job(binding.invocation_id)
    matches = [item for item in child["artifacts"] if item["file_path"] == path and item["content_sha256"] == sha]
    if actual != raw or len(matches) != 1:
        raise DurableJobLeaseError("recovered final evidence requires literal original child output")
    args = list(_repair_test_args(tuple(context["compiled"].test_args), context["compiled"].allowed_paths))
    if context["work"].language_profile == "test_node":
        commands = manifest.get("commands", [])
        scripts = [item["script"] for item in manifest["execution_plan"]["commands"]]
        passed = ([item.get("script") for item in commands] == scripts
            and list(manifest["execution_plan"]["selection"]) == args
            and all(item.get("exit_code") == 0 and item.get("timed_out") is False
                and item.get("cancelled") is False and item.get("leftover_descendant") is False
                and item.get("stdout_truncated") is False
                and item.get("cleanup", {}).get("cleanup_proven") is True for item in commands))
    else:
        passed = (manifest.get("exit_code") == 0 and manifest.get("timed_out") is False
            and manifest.get("stdout_truncated") is False and manifest.get("stderr_truncated") is False
            and manifest.get("test_args") == args)
    if (not passed or result["original_producer_completion"]["outcome"] != "completed_requested_checks"):
        raise DurableJobLeaseError("recovered final evidence requires every original successful check")
    diagnostics = json.loads(service._read_private_artifact(readback["diagnostics_artifact_ref"],
        expected_digest=readback["diagnostics_artifact_digest"]))
    diff_sha = hashlib.sha256(diagnostics["cumulative_diff"].encode("utf-8")).hexdigest()
    approved = service._read_private_artifact(patch["patch_artifact_ref"], expected_digest=patch["patch_sha256"])
    if (diff_sha != projection["artifact_digests"].get("diff.patch")
            or diff_sha != manifest.get("diff_sha256") or diff_sha != diagnostics.get("cumulative_diff_sha256")
            or hashlib.sha256(approved).hexdigest() != execution["patch_sha256"]):
        raise DurableJobLeaseError("recovered final evidence cumulative or approved patch changed")
    exits = {"test_args": args, "commands": manifest["commands"]} if context["work"].language_profile == "test_node" else {
        "test_args": args, "exit_code": manifest["exit_code"]}
    async with jobs._session() as db:
        costs = list((await db.scalars(select(InferenceCostReservation))).all())
        await _repository_remaining(db, context, allow_exhausted_readback=True)
    derived = {"final_patch_digest": execution["patch_sha256"], "final_manifest_digest": readback["manifest_digest"],
        "final_readback_digest": readback["artifact_digest"], "final_command_receipt_digest": _source_digest(execution),
        "final_cleanup_digest": cleanup["artifact_digest"],
        "final_accounting_digest": _source_digest([row.model_dump(mode="json") for row in costs if row.job_id == job_id]),
        "final_artifact_id": matches[0]["artifact_id"], "final_artifact_digest": sha,
        "requested_check_exits_digest": _source_digest(exits), "all_iteration_ids_digest": _source_digest(identities)}
    if _canonical(derived) != _canonical(evidence):
        raise DurableJobLeaseError("recovered original final evidence changed")
    return derived


async def finalize_repository_iteration(service, jobs, *, job_id, owner, iteration_index,
        actual_cleanup, actual_job, completion_witness=None):
    """Adopt only the actual supervised final readback into the original child."""
    from src.execution.repo_sandbox import assert_repo_iteration_cleanup_witness
    from src.model_fabric.effective_policy import configuration_mutation_lock
    assert_repo_iteration_cleanup_witness(actual_cleanup, actual_job)
    wait = await recover_repository_wait_witness(service, jobs, job_id=job_id, owner=owner,
        iteration_index=iteration_index, _resumed=True)
    async with configuration_mutation_lock:
        return await _finalize_repository_iteration_held(service, jobs, job_id=job_id, owner=owner,
            iteration_index=iteration_index, actual_cleanup=actual_cleanup, actual_job=actual_job,
            completion_witness=completion_witness, wait=wait)


async def finalize_recovered_repository_iteration(service, jobs, *, job_id, owner,
        iteration_index, completion_witness):
    """Consume only a scoped signed completion under its original Source fence."""
    from src.workflows.repo_repair_source_recovery import (
        assert_repository_scoped_completion, repository_completion_scoped_binding,
        repository_completion_recovered_wait)
    assert_repository_scoped_completion(completion_witness, service=service, jobs=jobs, job_id=job_id, owner=owner)
    binding = repository_completion_scoped_binding(completion_witness)
    if binding["job_id"] != job_id or binding["registration"]["iteration_index"] != iteration_index:
        from src.workflows.job_runtime import DurableJobLeaseError
        raise DurableJobLeaseError("recovered original finalizer identity changed")
    wait = await repository_completion_recovered_wait(completion_witness)
    return await _finalize_repository_iteration_held(service, jobs, job_id=job_id, owner=owner,
        iteration_index=iteration_index, actual_cleanup=None, actual_job=None,
        completion_witness=completion_witness, wait=wait, _recovered_completion=completion_witness)


async def _finalize_repository_iteration_held(service, jobs, *, job_id, owner, iteration_index,
        actual_cleanup, actual_job, completion_witness, wait, _recovered_completion=None):
    from dataclasses import replace
    from sqlalchemy import select
    from src.db.models import InferenceCostReservation
    from src.workflows.general_task_guard import issue_repository_child_final_witness
    from src.workflows.repo_repair import RepoIteration, RepoWorkVerifiedResult, _repair_test_args
    from src.work_board.contracts import GeneralTaskArtifactRef, GeneralTaskStepReceiptV1
    from src.work_board.general_task import write_step_artifact
    from src.work_board.general_task_runtime_artifacts import stage_task_artifact
    from src.workflows.job_runtime import _canonical, _as_utc, DurableJobLeaseError
    from src.model_fabric.effective_policy import configuration_mutation_lock
    from src.execution.repo_sandbox import assert_repo_iteration_cleanup_witness
    if _recovered_completion is not None:
        from src.workflows.repo_repair_source_recovery import (
            assert_repository_scoped_completion, repository_completion_physical_projection)
        assert_repository_scoped_completion(_recovered_completion, service=service, jobs=jobs, job_id=job_id, owner=owner)
        actual_projection = repository_completion_physical_projection(_recovered_completion)
    else:
        assert_repo_iteration_cleanup_witness(actual_cleanup, actual_job)
        actual_projection = actual_cleanup.projection()
    context = await _repository_precontact(service, jobs, job_id=job_id, owner=owner)
    run, original, work = context["run"], context["original"], context["work"]
    iterations, identities = [], []
    for index in range(1, iteration_index + 1):
        identity = iteration_identity(job_id, original["repository_attempt_id"],
            _source_digest(original["original_input"]), index)
        identities.append(identity)
        prepared = _repository_record(run, "repository:prepared:" + identity)
        executed = _repository_record(run, "repository:execution:" + identity)
        cleanup = _repository_record(run, "repository:cleanup:" + identity)
        readback = _repository_record(run, "repository:readback:" + identity)
        if (any(item is None for item in (prepared, executed, cleanup, readback))
                or cleanup.get("cleanup_proven") is not True
                or readback["status"] != ("succeeded" if index == iteration_index else "failed")):
            raise DurableJobLeaseError("complete actual original iteration chain required")
        iterations.append(RepoIteration(index=index, input_tree_digest=prepared["input_tree_digest"],
            patch_digest=executed["patch_sha256"], command_refs=["repository:execution:" + identity],
            result_artifacts=["repository:cleanup:" + identity, "repository:readback:" + identity]))
    identity = identities[-1]
    readback = _repository_record(run, "repository:readback:" + identity)
    cleanup_record = _repository_record(run, "repository:cleanup:" + identity)
    execution = _repository_record(run, "repository:execution:" + identity)
    patch = _repository_record(run, "repository:patch:" + identity)
    response = _repository_record(run, "repository:response:" + identity)
    manifest = json.loads(service._read_private_artifact(readback["artifact_ref"],
        expected_digest=readback["artifact_digest"]))
    cleanup_bytes = service._read_private_artifact(cleanup_record["artifact_ref"],
        expected_digest=cleanup_record["artifact_digest"])
    check_args = list(_repair_test_args(tuple(context["compiled"].test_args), context["compiled"].allowed_paths))
    check_exits = {"test_args": check_args, "exit_code": manifest.get("exit_code")}
    if work.language_profile == "test_node":
        commands = manifest.get("commands", [])
        expected_scripts = [entry["script"] for entry in manifest["execution_plan"]["commands"]]
        checks_passed = ([entry.get("script") for entry in commands] == expected_scripts
            and all(entry.get("exit_code") == 0 and entry.get("timed_out") is False
                and entry.get("cancelled") is False and entry.get("leftover_descendant") is False
                and entry.get("stdout_truncated") is False
                and entry.get("cleanup", {}).get("cleanup_proven") is True for entry in commands)
            and list(manifest["execution_plan"]["selection"]) == check_args)
        check_exits = {"test_args": check_args, "commands": commands}
    else:
        checks_passed = (manifest.get("exit_code") == 0 and manifest.get("timed_out") is False
            and manifest.get("stdout_truncated") is False and manifest.get("stderr_truncated") is False
            and manifest.get("test_args") == check_args)
    if read_repository_inventory(run)["schema"] == "repository.checkpoint_inventory.v3":
        from src.workflows.repo_repair_source_recovery import (
            assert_repository_completion_witness, repository_completion_result,
            repository_completion_post_cas, repository_completion_cleanup_envelope)
        assert_repository_completion_witness(completion_witness, service=service, jobs=jobs)
        actual_result = repository_completion_result(completion_witness)
        cas = repository_completion_post_cas(completion_witness)
        recorded_projection = json.loads(cleanup_bytes)
        actual_envelope = repository_completion_cleanup_envelope(completion_witness)
        from src.workflows.general_task_guard import _history
        from src.workflows.job_runtime import _digest
        metadata = actual_envelope.get("source_append_metadata", [])
        expected_ids = ["repository:cleanup:" + identity, "repository:readback:" + identity]
        expected_wrappers = [{**item, "payload": payload, "state_digest": _digest(payload)}
            for item, payload in zip(metadata, (cleanup_record, readback))]
        actual_wrappers = [item for item in _history(run) if item.get("checkpoint_id") in expected_ids]
        cleanup_matches = (cas["iteration_id"] == identity and cas["post_revision"] == run.revision
            and cleanup_record.get("source_completion_cas") == cas
            and readback.get("source_completion_cas") == cas
            and actual_result["manifest"] == manifest
            and _canonical(recorded_projection) == _canonical(actual_envelope)
            and set(actual_envelope) == {"physical_projection", "source_completion_cas", "source_append_metadata"}
            and _canonical(actual_envelope["physical_projection"]) == _canonical(actual_projection)
            and actual_envelope["source_completion_cas"] == cas
            and len(metadata) == 2 and [item.get("checkpoint_id") for item in metadata] == expected_ids
            and actual_wrappers == expected_wrappers)
    else:
        cleanup_matches = json.loads(cleanup_bytes) == actual_projection
    if (not cleanup_matches or not checks_passed
            or _source_digest(manifest) != readback["manifest_digest"]):
        raise DurableJobLeaseError("all requested original checks and actual cleanup must pass")
    output = RepoWorkVerifiedResult(iterations=iterations, patch_artifact_ref=patch["patch_artifact_ref"],
        final_readback_ref=readback["artifact_ref"]).model_dump(mode="json")
    binding = context["binding"]
    child_owner, child_fence = context["child"].lease_owner, context["child"].fencing_token
    async def output_authority(db, child):
        # The existing artifact writer publishes three known journals.
        # All ownership, status, lease, input and authority columns stay
        # exactly pinned while those journals advance.
        mutable = {"revision", "updated_at", "heartbeat_at", "checkpoint_receipts_json",
            "artifact_receipts_json", "readback_receipts_json"}
        rows = []
        for model, key, expected in context["rows"]:
            original_row = json.loads(expected)
            if original_row.get("run_identity") == binding.invocation_id:
                actual_row = child.model_dump(mode="json")
                if ({k: v for k, v in original_row.items() if k not in mutable} !=
                        {k: v for k, v in actual_row.items() if k not in mutable}):
                    raise DurableJobLeaseError("original final output child authority changed")
                expected = _canonical(actual_row)
            rows.append((model, key, expected))
        await _recheck_repository_sql(db, {**context, "rows": rows})
    artifact, verified = await write_step_artifact(jobs, job_id=binding.invocation_id,
        owner=child_owner, fence=child_fence, plan_digest=binding.plan_digest,
        step_id=binding.step_id, output=output, authority_check=output_authority,
        **({"_repository_completion_witness": _recovered_completion} if _recovered_completion is not None else {}))
    output_context = await _repository_precontact(service, jobs, job_id=job_id, owner=owner)
    async def readback_authority(db, child):
        await _recheck_repository_sql(db, output_context)
    from src.workflows.job_runtime import _utc_now
    child_readback = {"effect_type": "general_tool_call", "status": "succeeded", "target_path": artifact["file_path"],
        "target_digest": None, "content_sha256": artifact["content_sha256"],
        "readback_id": "repository-child-output:" + identity, "verified_at": _utc_now().isoformat(),
        "details": {"step_id": binding.step_id, "tool_id": "repository_work", "verified": True,
            "output_exists": True, "file_path": artifact["file_path"], "no_learning": True,
            "actual_process_cleanup_digest": cleanup_record["artifact_digest"]}}
    if _recovered_completion is not None:
        await _expect_recovered_repository_final_writer(jobs, completion_witness=_recovered_completion,
            kind="readback", job_id=binding.invocation_id, descriptor=child_readback)
    await jobs.record_readback(binding.invocation_id, **child_readback,
        owner=child_owner, fencing_token=child_fence, readback_authority_check=readback_authority,
        **({"_repository_completion_witness": _recovered_completion} if _recovered_completion is not None else {}))
    if _recovered_completion is not None:
        await observe_recovered_repository_final_writer(jobs, completion_witness=_recovered_completion, kind="readback")
    child = await jobs.get_job(binding.invocation_id)
    matches = [item for item in child["artifacts"] if item["file_path"] == artifact["file_path"]
        and item["content_sha256"] == artifact["content_sha256"]]
    if len(matches) != 1 or verified != output:
        raise DurableJobLeaseError("literal original child output readback required")
    reference = GeneralTaskArtifactRef(artifact_id=matches[0]["artifact_id"],
        digest=artifact["content_sha256"], schema_version="GeneralTaskOutput.v1")
    async with jobs._session() as db:
        if _recovered_completion is None:
            canonical = await stage_repository_canonical_source(service, db, repository_job_id=job_id,
                native_invocation_id=binding.invocation_id, consent_id=wait._source_binding.consent_id)
        else:
            from src.workflows.repo_repair_source_recovery import repository_completion_recovered_final_source
            canonical = await repository_completion_recovered_final_source(_recovered_completion)
        costs = list((await db.scalars(select(InferenceCostReservation))).all())
        await _repository_remaining(db, await _repository_precontact(service, jobs, job_id=job_id, owner=owner),
            allow_exhausted_readback=True)
    evidence = {"final_patch_digest": execution["patch_sha256"],
        "final_manifest_digest": readback["manifest_digest"], "final_readback_digest": readback["artifact_digest"],
        "final_command_receipt_digest": _source_digest(execution), "final_cleanup_digest": cleanup_record["artifact_digest"],
        "final_accounting_digest": _source_digest([row.model_dump(mode="json") for row in costs
            if row.job_id == job_id]), "final_artifact_id": reference.artifact_id,
        "final_artifact_digest": reference.digest, "requested_check_exits_digest": _source_digest(check_exits),
        "all_iteration_ids_digest": _source_digest(identities)}
    if _recovered_completion is None:
        final_source = replace(wait._source_binding, _final_source=canonical,
            _final_evidence_json=_canonical(evidence))
        final = issue_repository_child_final_witness(wait_witness=wait, source_binding=final_source, **evidence)
    else:
        from src.workflows.repo_repair_source_recovery import repository_completion_recovered_child_final
        final = await repository_completion_recovered_child_final(_recovered_completion, evidence=evidence)
    staged = stage_task_artifact(parent_job_id=binding.parent_job_id, creation_digest=binding.creation_digest,
        payload=GeneralTaskStepReceiptV1(step_id=binding.step_id, plan_revision=binding.plan_revision,
            invocation_id=binding.invocation_id, input_digest=binding.input_digest, contact_state="settled", status="verified",
            descriptor_digest=binding.descriptor_digest, selected_grant_digest=binding.selected_grant_digest,
            task_id=binding.task_id, attempt_id=binding.attempt_id, child_job_id=binding.invocation_id,
            child_attempt_count=1, child_fence=child_fence, parent_creation_digest=binding.creation_digest,
            phase_digest=binding.phase_digest, artifact_refs=[reference],
            effect_receipt_digest=_source_digest(child["effects"]),
            cleanup_receipt_digest=evidence["final_cleanup_digest"]))
    parent = await jobs.get_job(binding.parent_job_id)
    publication_descriptor = {"parent_id": binding.parent_job_id, "child_id": binding.invocation_id,
        "staged_artifact": staged, "repository_final_witness": final, "expected_parent_revision": parent["revision"]}
    if _recovered_completion is not None:
        await _expect_recovered_repository_final_writer(jobs, completion_witness=_recovered_completion,
            kind="publish", job_id=binding.invocation_id, descriptor=publication_descriptor)
    published = await jobs.publish_general_task_step_receipt(binding.parent_job_id, staged_artifact=staged,
        child_id=binding.invocation_id, owner=child_owner, fencing_token=child_fence,
        expected_parent_revision=parent["revision"], repository_final_witness=final,
        **({"_repository_completion_witness": _recovered_completion} if _recovered_completion is not None else {}))
    if _recovered_completion is not None:
        await observe_recovered_repository_final_writer(jobs, completion_witness=_recovered_completion, kind="publish")
    # The original root keeps its physical capacity until both canonical
    # child adoption and the root's actual readback are durably terminal.
    manifest_bytes = service._read_private_artifact(readback["artifact_ref"],
        expected_digest=readback["artifact_digest"])
    root_owner, root_fence = run.lease_owner, run.fencing_token
    manifest_path = readback["artifact_ref"].removeprefix("workspace-json:")
    async def record_final_artifact(*, file_path, artifact_type, content):
        if _recovered_completion is not None:
            await _expect_recovered_repository_final_writer(jobs, completion_witness=_recovered_completion,
                kind="artifact", job_id=job_id, descriptor={"file_path": file_path,
                    "artifact_type": artifact_type, "content": content})
        await jobs.record_artifact(job_id, file_path=file_path, artifact_type=artifact_type,
            content=content, owner=root_owner, fencing_token=root_fence,
            **({"_repository_completion_witness": _recovered_completion} if _recovered_completion is not None else {}))
        if _recovered_completion is not None:
            await observe_recovered_repository_final_writer(jobs, completion_witness=_recovered_completion, kind="artifact")
    await record_final_artifact(file_path=manifest_path, artifact_type="repo_repair_manifest", content=manifest_bytes)
    publication_artifacts = {}
    approved_patch_bytes = service._read_private_artifact(patch["patch_artifact_ref"],
        expected_digest=patch["patch_sha256"])
    diagnostics = json.loads(service._read_private_artifact(readback["diagnostics_artifact_ref"],
        expected_digest=readback["diagnostics_artifact_digest"]))
    patch_bytes = diagnostics["cumulative_diff"].encode("utf-8")
    tested_diff_digest = hashlib.sha256(patch_bytes).hexdigest()
    if (tested_diff_digest != manifest.get("diff_sha256")
            or tested_diff_digest != diagnostics.get("cumulative_diff_sha256")
            or tested_diff_digest != actual_projection["artifact_digests"].get("diff.patch")
            or hashlib.sha256(approved_patch_bytes).hexdigest() != execution["patch_sha256"]
            or work.language_profile == "test_python" and (
                manifest.get("patch_sha256") != execution["patch_sha256"]
                or manifest["publication_test_input"].get("patch_sha256") != execution["patch_sha256"])):
        raise DurableJobLeaseError("actual final cumulative publication patch changed")
    for filename, literal in (("manifest.json", manifest_bytes), ("readback.json", manifest_bytes),
            ("diff.patch", patch_bytes)):
        path = "artifacts/repo-repair/" + job_id + "/" + filename
        ref, digest = service._write_private_artifact(path, literal)
        if service._read_private_artifact(ref, expected_digest=digest) != literal:
            raise DurableJobLeaseError("literal final publication artifact readback changed")
        await record_final_artifact(file_path=path, artifact_type="repo_repair_" + filename, content=literal)
        publication_artifacts[filename] = {"path": path, "sha256": digest}
    from src.workflows.job_runtime import _utc_now
    root_readback_descriptor = {"target_path": publication_artifacts["readback.json"]["path"], "status": "succeeded",
        "effect_type": "repository_iteration_verified", "target_digest": None,
        "content_sha256": readback["artifact_digest"], "readback_id": "repository-final:" + identity,
        "verified_at": _utc_now().isoformat(), "details": {"verified": True, "output_exists": True, "no_learning": True,
            "iteration_id": identity, "original_child_id": binding.invocation_id}}
    if _recovered_completion is not None:
        await _expect_recovered_repository_final_writer(jobs, completion_witness=_recovered_completion,
            kind="readback", job_id=job_id, descriptor=root_readback_descriptor)
    root_readback = await jobs.record_readback(job_id, **root_readback_descriptor,
        owner=root_owner, fencing_token=root_fence,
        **({"_repository_completion_witness": _recovered_completion} if _recovered_completion is not None else {}))
    if _recovered_completion is not None:
        await observe_recovered_repository_final_writer(jobs, completion_witness=_recovered_completion, kind="readback")
    from src.work_board.repository import _begin_sqlite_immediate
    from src.workflows.general_task_guard import _cancel_cas_job
    from src.workflows.repo_repair_stop import _cas_repository_stop_board_row
    from src.work_board.repository import WorkBoardRepository
    from src.db.models import WorkBoardTask, WorkBoardAttempt
    terminal_payload = {"schema": "repository.final_verified.v1", "iteration_id": identity,
        "original_child_id": binding.invocation_id, "final_witness_digest": _source_digest(final.projection()),
        "manifest_artifact_digest": readback["artifact_digest"], "publication_artifacts": publication_artifacts,
        "approved_input_patch_digest": execution["patch_sha256"],
        "approved_input_patch_artifact_ref": patch["patch_artifact_ref"],
        "tested_cumulative_diff_digest": tested_diff_digest, "no_learning": True}
    result_payload = {"verified": True, "no_learning": True, "iteration_count": iteration_index,
        "manifest_artifact_ref": readback["artifact_ref"], "patch_artifact_ref": patch["patch_artifact_ref"],
        "original_child_id": binding.invocation_id}
    terminal_descriptor = {"terminal_payload": terminal_payload, "result_payload": result_payload}
    if _recovered_completion is not None:
        await _expect_recovered_repository_final_writer(jobs, completion_witness=_recovered_completion,
            kind="terminal", job_id=job_id, descriptor=terminal_descriptor)
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        current = await jobs._fetch(db, job_id)
        closed_child = await jobs._fetch(db, binding.invocation_id)
        if _recovered_completion is not None:
            await verify_recovered_repository_final_writer(jobs, db, current,
                completion_witness=_recovered_completion, kind="terminal", descriptor=terminal_descriptor)
        if (closed_child.status != "succeeded" or closed_child.result_digest != reference.digest
                or closed_child.fencing_token != child_fence or closed_child.lease_owner
                or current.status != "running" or current.fencing_token != root_fence):
            raise DurableJobLeaseError("actual original final adoption must precede root closure")
        original_journal = current.checkpoint_receipts_json
        _append_repository_record(current, "repository:terminal:v1", terminal_payload,
            inventory=repository_checkpoint_inventory(current, work))
        await append_repository_release_in_writer(db, jobs, current,
            original=original, work=work, witness=final, outcome_status="succeeded")
        updated_journal = current.checkpoint_receipts_json
        current.checkpoint_receipts_json = original_journal
        await _cancel_cas_job(db, current, {"checkpoint_receipts_json": updated_journal,
            "status": "succeeded", "finished_at": _utc_now(), "lease_owner": None,
            "lease_expires_at": None, "result_digest": _source_digest(result_payload),
            "result_summary": "repository_cumulative_repair_verified"})
        authority = context["authority"]
        repo_task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == original["repository_task_id"]))
        repo_attempt = await db.get(WorkBoardAttempt, original["repository_attempt_id"])
        if (repo_task is None or repo_attempt is None
                or _canonical(repo_task.model_dump(mode="json")) != _canonical(authority.task.model_dump(mode="json"))
                or _canonical(repo_attempt.model_dump(mode="json")) != _canonical(authority.attempt.model_dump(mode="json"))):
            raise DurableJobLeaseError("original repository final board epoch changed")
        now = _utc_now()
        await _cas_repository_stop_board_row(db, repo_task, {"status": "done",
            "task_revision": repo_task.task_revision + 1, "updated_at": now, "completed_at": now,
            "block_kind": None, "block_reason": None, "block_source_status": None,
            "result_refs_json": _canonical([{"job_id": job_id, "status": "succeeded", "no_learning": True}])})
        await _cas_repository_stop_board_row(db, repo_attempt, {
            "outcome": "repository_cumulative_repair_verified", "ended_at": now,
            "lease_owner": None, "lease_expires_at": None, "updated_at": now})
        terminal_event = await WorkBoardRepository._event(db, repo_task, owner, kind="attempt.repository_verified",
            metadata={"attempt_id": repo_attempt.attempt_id, "workflow_run_id": job_id,
                "readback_id": "repository-final:" + identity, "no_learning": True})
        if _recovered_completion is not None:
            await capture_recovered_repository_final_writer(jobs, db, await jobs._fetch(db, job_id),
                completion_witness=_recovered_completion, kind="terminal", _terminal_event=terminal_event)
        await db.commit()
    if _recovered_completion is not None:
        await observe_recovered_repository_final_writer(jobs, completion_witness=_recovered_completion, kind="terminal")
    terminal = await jobs.get_job(job_id)
    if _recovered_completion is None:
        lane = service._iterative_lanes.pop(job_id, None)
        if lane is None:
            raise DurableJobLeaseError("original physical root capacity owner disappeared")
        lane.clear_quarantine()
    else:
        assert_repository_scoped_completion(_recovered_completion, service=service, jobs=jobs, job_id=job_id, owner=owner)
        if terminal["status"] != "succeeded" or terminal["lease"]["fencing_token"] != root_fence:
            raise DurableJobLeaseError("recovered original terminal readback changed")
    published["repository_root"] = terminal
    published["repository_task_status"] = "done"
    return published


async def validate_repository_child_final_witness(db, witness, *, parent, task, attempt,
        child, manifest, staged_artifact=None, receipt=None, phase):
    """SQL-only adoption check for the actual original process owner."""
    from sqlalchemy import select
    from src.db.models import WorkflowRunState, InferenceCostReservation
    from src.workflows.job_runtime import DurableJobLeaseError, _as_utc
    from src.workflows.general_task_guard import _assert_repository_child_final_witness_shape
    _assert_repository_child_final_witness_shape(witness)
    source = witness._source_binding
    current_source = source._final_source
    assert_repository_canonical_source(current_source)
    if current_source._recovered_completion is not None:
        from src.workflows.repo_repair_source_recovery import assert_repository_completion_final_source
        assert_repository_completion_final_source(current_source, final_witness=witness)
    if phase != "final" or source._final_evidence_json is None:
        raise DurableJobLeaseError("actual original final source owner required")
    evidence = json.loads(source._final_evidence_json)
    if any(getattr(witness, key) != value for key, value in evidence.items()):
        raise DurableJobLeaseError("original final source evidence changed")
    run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == witness.wait_witness.repository_job_id))
    original, work, compiled, group, binding, task_source = read_repository_original(run)
    identity = witness.wait_witness.iteration_id
    readback = _repository_record(run, "repository:readback:" + identity)
    cleanup = _repository_record(run, "repository:cleanup:" + identity)
    execution = _repository_record(run, "repository:execution:" + identity)
    response = _repository_record(run, "repository:response:" + identity)
    iteration = _repository_record(run, "repository:iteration:" + identity)
    rows = list((await db.scalars(select(InferenceCostReservation))).all())
    cost = next((row for row in rows if row.operation_id == response["operation_id"]), None)
    if (binding != witness.wait_witness.native_binding or run.fencing_token != witness.wait_witness.repository_fence
            or readback.get("status") != "succeeded" or cleanup.get("cleanup_proven") is not True
            or readback["artifact_digest"] != witness.final_readback_digest
            or readback["manifest_digest"] != witness.final_manifest_digest
            or cleanup["artifact_digest"] != witness.final_cleanup_digest
            or _source_digest(execution) != witness.final_command_receipt_digest
            or execution["patch_sha256"] != witness.final_patch_digest
            or _source_digest([row.model_dump(mode="json") for row in rows if row.job_id == run.run_identity])
                != witness.final_accounting_digest or cost is None or cost.state != "settled"
            or cost.contact_started_at is None):
        raise DurableJobLeaseError("actual original final readback accounting or cleanup changed")
    accounting = RepositoryIterationAccountingWitness(**{**iteration["accounting_binding"], "group": group,
        "original_deadline_at": _as_utc(run.deadline_at)}, _canonical_source=current_source,
        _request_body_digest=cost.payload_digest,
        _request_route_digest=_repository_record(run, "repository:prepared:" + identity)["request_route_digest"], _seal=_SEAL)
    await validate_repository_iteration_accounting(db, accounting, run, rows=rows, operation_id=cost.operation_id,
        payload_digest=cost.payload_digest, bound_microusd=cost.bound_microusd, deadline_at=cost.deadline_at, already_reserved=True)
    return {"verified": True, "no_learning": True}


def _assert_task_publication_configuration(service, *, staged_config=None):
    from config.settings import settings
    from src.security.trust_contract import canonical_digest
    from src.workspace import canonical_workspace_root
    from src.work_board.repository import BoardError
    if staged_config is None:
        from src.execution.repo_sandbox import load_persisted_repo_sandbox_settings
        staged_config, error = load_persisted_repo_sandbox_settings()
        if error is not None:
            raise BoardError("repository_source_current_configuration_changed",
                "Restore the canonical private repository selector", status_code=409)
    if (canonical_digest(staged_config.model_dump(mode="json")) !=
            canonical_digest(service.sandbox.config.model_dump(mode="json"))
            or service._workspace() != canonical_workspace_root(settings.workspace_dir)):
        raise BoardError("repository_source_current_configuration_changed",
            "Use the current canonical repository profile and workspace", status_code=409)
    return staged_config


def prepare_repository_task_publication(service, envelope, *, owner, goal_revision, replay=False):
    """Inspect the fixed source before publication; never accept caller metadata.

    The scope stages physical checks before the SQLite writer. Its callback
    validates only the original SQL authority and captured configuration.
    """
    import asyncio
    from src.workflows.repo_repair import RepoRepairService, RepoWorkInput
    from src.work_board.repository import BoardError
    from src.security.trust_contract import canonical_digest
    if replay and envelope.repository_source is None:
        raise BoardError("repository_source_unscoped_replay", "Old Tasks cannot acquire repository source authority on replay", status_code=409)
    if type(service) is not RepoRepairService or envelope.plan is None:
        raise BoardError("repository_source_owner_required", "Current fixed repository source owner required", status_code=409)
    _assert_task_publication_configuration(service)
    steps = [step for step in envelope.plan.steps if step.tool_id == "repository_work"]
    if len(steps) != 1 or envelope.proposal_group is None:
        raise BoardError("repository_source_binding_invalid", "One original grouped repository step required", status_code=409)
    from src.native_tools.task_adapters import repository_work_descriptor
    fixed_descriptor = repository_work_descriptor()
    selected_descriptors = [item for item in envelope.descriptors if item.tool_id == "repository_work"]
    if selected_descriptors != [fixed_descriptor]:
        raise BoardError("repository_source_descriptor_changed", "Exact current fixed repository descriptor required", status_code=409)
    try:
        work = RepoWorkInput.model_validate(steps[0].input)
    except ValueError as exc:
        raise BoardError("repository_source_input_invalid", "Review the exact closed repository input", status_code=422) from exc
    group = envelope.proposal_group
    if (group.owner_principal_id != owner.principal_id or group.owner_session_id != owner.session_id
            or group.goal_id != envelope.task_input.goal_ref or group.goal_revision != goal_revision):
        raise BoardError("repository_source_binding_invalid", "Original owner and Goal required", status_code=409)
    if replay:
        binding = envelope.repository_source
        if binding is None:
            raise BoardError("repository_source_unscoped_replay", "Old Tasks cannot acquire repository source authority on replay", status_code=409)
    else:
        if envelope.repository_source is not None:
            raise BoardError("repository_source_caller_metadata", "Source metadata must be minted during original publication", status_code=409)
        binding = service.stage_task_source(work, owner=owner,
            goal_id=group.goal_id, goal_revision=goal_revision)
    projection = binding.model_dump(mode="json", exclude={"schema_version", "binding_digest"})
    if (binding.binding_digest != canonical_digest(projection)
            or binding.original_input_digest != canonical_digest(work.model_dump(mode="json"))
            or binding.owner_principal_id != owner.principal_id
            or binding.original_root_id != owner.session_id
            or binding.goal_id != group.goal_id or binding.goal_revision != goal_revision):
        raise BoardError("repository_source_binding_invalid", "Original source binding changed", status_code=409)
    witness = object()

    @asynccontextmanager
    async def scope():
        from src.model_fabric.effective_policy import configuration_mutation_lock
        from config.settings import settings
        async with configuration_mutation_lock:
            staged_config = _assert_task_publication_configuration(service)
            if repository_work_descriptor() != fixed_descriptor:
                raise BoardError("repository_source_descriptor_changed", "Original fixed descriptor policy changed", status_code=409)
            # Physical work precedes the publication writer, including replay.
            facts = json.loads(service._read_private_artifact(binding.source_artifact_ref,
                expected_digest=binding.source_artifact_digest))
            compiled = service.recheck_task_source_snapshot(work, facts)
            if (facts.get("original_input") != work.model_dump(mode="json")
                    or facts.get("compiled_input") != compiled.model_dump(mode="json")
                    or canonical_digest(service.sandbox.config.model_dump(mode="json")) != binding.executor_profile_digest
                    or facts.get("snapshot_manifest", {}).get("digest") != binding.snapshot_digest
                    or facts.get("owner_principal_id") != owner.principal_id
                    or facts.get("original_root_id") != owner.session_id
                    or facts.get("goal_id") != group.goal_id or facts.get("goal_revision") != goal_revision):
                raise BoardError("repository_source_changed", "Original physical source or execution profile changed", status_code=409)
            token = _task_publication.set((asyncio.current_task(), witness, staged_config,
                canonical_digest(settings.repo_sandbox.model_dump(mode="json"))))
            try:
                yield
            finally:
                _task_publication.reset(token)

    async def check(db):
        from src.workflows.general_task_accounting import validate_group_owner
        from config.settings import settings
        held = _task_publication.get()
        if held is None or held[:2] != (asyncio.current_task(), witness):
            raise BoardError("repository_source_scope_required", "Original source publication scope required", status_code=409)
        _assert_task_publication_configuration(service, staged_config=held[2])
        if canonical_digest(settings.repo_sandbox.model_dump(mode="json")) != held[3]:
            raise BoardError("repository_source_current_configuration_changed", "Original canonical settings changed", status_code=409)
        if repository_work_descriptor() != fixed_descriptor:
            raise BoardError("repository_source_descriptor_changed", "Original fixed descriptor policy changed", status_code=409)
        if canonical_digest(service.sandbox.config.model_dump(mode="json")) != binding.executor_profile_digest:
            raise BoardError("repository_source_changed", "Original execution profile changed", status_code=409)
        await validate_group_owner(db, group)
        _assert_task_publication_configuration(service, staged_config=held[2])
        if canonical_digest(settings.repo_sandbox.model_dump(mode="json")) != held[3]:
            raise BoardError("repository_source_current_configuration_changed", "Original canonical settings changed", status_code=409)

    return binding, check, scope


def read_repository_original(run):
    """Only the protected canonical journal supplies original compilation."""
    from src.workflows.general_task_guard import _history
    from src.workflows.job_runtime import _digest, DurableJobLeaseError
    from src.workflows.repo_repair import RepoWorkInput, RepoRepairInput
    records = [record for record in _history(run)
               if record.get("checkpoint_id") == "repository:original:v1"]
    if len(records) != 1:
        raise DurableJobLeaseError("original fixed repository source checkpoint required")
    record = records[0]
    payload = record.get("payload")
    if (record.get("safe") is not True or not isinstance(payload, dict)
            or record.get("state_digest") != _digest(payload)):
        raise DurableJobLeaseError("original fixed repository source checkpoint changed")
    required = {"schema_version", "repository_job_id", "repository_task_id", "repository_attempt_id",
        "repository_input_artifact_digest", "original_input", "compiled_input", "original_deadline_at",
        "original_max_cost_microusd", "group", "native_binding", "source_binding"}
    if set(payload) != required or payload["schema_version"] != "repository.original.v1":
        raise DurableJobLeaseError("original fixed repository source shape changed")
    try:
        work = RepoWorkInput.model_validate(payload["original_input"])
        compiled = RepoRepairInput.model_validate(payload["compiled_input"])
        group = TaskProposalGroupV1.model_validate(payload["group"])
        binding = GeneralTaskNativeChildBindingV1.model_validate(payload["native_binding"])
        source = RepositoryTaskSourceBinding.model_validate(payload["source_binding"])
        cutoff = TaskProposalGroupV1.utc_timestamp(payload["original_deadline_at"])
        from src.workflows.inference_accounting import _utc
        from src.security.trust_contract import canonical_digest
        if (payload["repository_job_id"] != run.run_identity
                or payload["repository_input_artifact_digest"] != run.input_digest
                or type(payload["original_max_cost_microusd"]) is not int
                or payload["original_max_cost_microusd"] != work.limits.max_cost_microusd
                or canonical_digest(work.model_dump(mode="json")) != source.original_input_digest
                or compiled.repository_path != work.repository_ref
                or compiled.allowed_paths != work.allowed_paths
                or cutoff > group.original_deadline_at or cutoff > binding.original_deadline_at
                or cutoff > binding.native_deadline_at
                or cutoff > _utc(run.started_at) + timedelta(seconds=work.limits.max_total_seconds)
                or _utc(run.deadline_at) != cutoff
                or group.owner_principal_id != binding.owner_principal_id
                or group.owner_session_id != binding.original_root_id
                or group.goal_id != binding.goal_id or group.goal_revision != binding.goal_revision
                or source.owner_principal_id != run.owner_principal_id
                or source.original_root_id != run.operator_session_id
                or source.goal_id != run.goal_id or source.goal_revision != run.goal_revision):
            raise ValueError("original source/cutoff changed")
    except (ValueError, TypeError, KeyError) as exc:
        raise DurableJobLeaseError("original fixed repository source binding changed") from exc
    return payload, work, compiled, group, binding, source


async def prepare_repository_original_admission(service, db, *, native_invocation_id,
        repository_task_id, repository_attempt_id):
    """Stage the actual original child before the existing admission writer.

    This is a private new-root producer, not immutable admission replay. The
    caller must retain its returned scope through admit_job's actual commit.
    No public projection, supplied group, or already-admitted root qualifies.
    """
    import asyncio
    from sqlalchemy import select
    from src.db.models import WorkflowRunState, WorkBoardTask, WorkBoardAttempt, WorkBoardInputArtifact, Goal
    from src.workflows.repo_repair import RepoRepairService
    from src.workflows.general_task_guard import child_binding, read_manifest, assert_general_task_child_current
    from src.work_board.general_task_runtime_artifacts import verify_general_task_manifest
    from src.workflows.job_runtime import DurableJobLeaseError, _utc_now, _as_utc, _canonical, _digest
    from src.security.trust_contract import canonical_digest
    from src.native_tools.task_adapters import repository_work_descriptor
    from src.work_board.general_task import digest as native_digest
    if type(service) is not RepoRepairService:
        raise DurableJobLeaseError("actual original repository source owner required")
    child = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == native_invocation_id))
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == repository_task_id))
    attempt = await db.get(WorkBoardAttempt, repository_attempt_id)
    if (child is None or task is None or attempt is None
            or attempt.task_id != task.task_id or attempt.workflow_run_id is not None
            or task.capability_id != "engineering.repo-repair.v1"
            or task.status != "running" or not attempt.lease_owner
            or attempt.lease_expires_at is None or _as_utc(attempt.lease_expires_at) <= _utc_now()
            or attempt.fencing_token < 1
            or attempt.ended_at is not None or attempt.cancel_requested_at is not None):
        raise DurableJobLeaseError("new original repository Task attempt required")
    # Reject existing-root attempts before any private physical source read.
    binding = child_binding(child)
    from src.workflows.job_runtime import _binding
    permanent_binding = _binding(owner_principal_id=binding.owner_principal_id,
        goal_id=binding.goal_id, goal_revision=binding.goal_revision,
        idempotency_scope="original-repository-child", dedupe_key=binding.invocation_id)
    existing_mapping = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.idempotency_binding == permanent_binding))
    if existing_mapping is not None:
        # This new-only producer cannot turn immutable generic admission replay
        # into current Source authority. A separate exact scoped recovery owns
        # the existing root; no private source artifact has been read here.
        raise DurableJobLeaseError("original native invocation already has its permanent repository mapping")
    await assert_general_task_child_current(db, child)
    parent = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == binding.parent_job_id))
    parent_task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
    parent_attempt = await db.get(WorkBoardAttempt, binding.attempt_id)
    goal = await db.get(Goal, binding.goal_id)
    manifest = read_manifest(parent) if parent is not None else None
    if parent_task is None or parent_attempt is None or manifest is None or goal is None:
        raise DurableJobLeaseError("original C1 source Task required")
    envelope = await verify_general_task_manifest(db, parent, parent_task, parent_attempt, manifest)
    source = envelope.repository_source
    group = envelope.proposal_group
    input_artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id)
    if (source is None or group is None or input_artifact is None
            or json.loads(child.arguments_json).get("tool_id") != "repository_work"
            or source.original_root_id != binding.original_root_id
            or (task.owner_principal_id, task.owner_session_id, task.goal_id, task.goal_revision) !=
                (binding.owner_principal_id, binding.original_root_id, binding.goal_id, binding.goal_revision)
            or input_artifact.bound_task_id != task.task_id
            or input_artifact.payload_sha256 != task.typed_input_digest
            or input_artifact.typed_input_ref != task.typed_input_ref
            or input_artifact.capability_id != task.capability_id or input_artifact.capability_version != "1"
            or input_artifact.owner_principal_id != task.owner_principal_id
            or input_artifact.owner_session_id != task.owner_session_id
            or input_artifact.goal_id != task.goal_id or input_artifact.goal_revision != task.goal_revision
            or input_artifact.state != "bound" or _as_utc(input_artifact.expires_at) <= _utc_now()
            or input_artifact.bound_task_revision > task.task_revision):
        raise DurableJobLeaseError("original source and repository Task artifact binding required")
    work = service._read_bound_work_input(input_artifact)
    if (canonical_digest(work.model_dump(mode="json")) != source.original_input_digest
            or native_digest(work.model_dump(mode="json")) != binding.input_digest):
        raise DurableJobLeaseError("original seven-field repository input changed")
    fixed_descriptor = repository_work_descriptor()
    if native_digest(fixed_descriptor.model_dump(mode="json")) != binding.descriptor_digest:
        raise DurableJobLeaseError("original fixed repository descriptor changed")
    originals = ((WorkflowRunState, child.id, child), (WorkflowRunState, parent.id, parent),
        (WorkBoardTask, parent_task.creation_sequence, parent_task),
        (WorkBoardAttempt, parent_attempt.attempt_id, parent_attempt),
        (WorkBoardTask, task.creation_sequence, task), (WorkBoardAttempt, attempt.attempt_id, attempt),
        (WorkBoardInputArtifact, input_artifact.artifact_id, input_artifact), (Goal, goal.id, goal))
    rows = tuple((model, key, _canonical(row.model_dump(mode="json"))) for model, key, row in originals)
    witness, entered = object(), ContextVar("repository_original_admission", default=None)
    compiled = None
    issued = False

    @asynccontextmanager
    async def scope():
        nonlocal compiled, issued
        from src.model_fabric.effective_policy import configuration_mutation_lock
        from config.settings import settings
        async with configuration_mutation_lock:
            issued = False
            # A preparation may have preceded another source admission. Recheck
            # its permanent mapping before reopening any private source bytes.
            # This read is outside the financial/admission writer; the exact
            # same predicate is checked again inside the new-row transaction.
            async with service.session_factory() as mapping_db:
                existing_mapping = await mapping_db.scalar(select(WorkflowRunState).where(
                    WorkflowRunState.idempotency_binding == permanent_binding))
                if existing_mapping is not None:
                    raise DurableJobLeaseError("original native invocation already has its permanent repository mapping")
            staged_config = _assert_task_publication_configuration(service)
            staged_policy = _repository_policy_limits()
            staged_limits = _repository_original_limits(goal, staged_policy)
            from src.execution.repo_original_producer import _enable_original_producer_service
            # Actual Source admission owns this native-host selection. It is
            # staged before SQL and cannot upgrade any pre-existing Root.
            try:
                _enable_original_producer_service(service, service.jobs)
            except (ValueError, OSError) as exc:
                from src.workflows.repo_repair import RepoRepairError
                raise RepoRepairError("repository_original_producer_prerequisites_blocked",
                    "The selected original producer mode requires native Linux supervision and signing support",
                    status_code=503) from exc
            facts = json.loads(service._read_private_artifact(source.source_artifact_ref,
                expected_digest=source.source_artifact_digest))
            compiled = service.recheck_task_source_snapshot(work, facts)
            if (facts.get("original_input") != work.model_dump(mode="json")
                    or canonical_digest(staged_config.model_dump(mode="json")) != source.executor_profile_digest
                    or repository_work_descriptor() != fixed_descriptor):
                raise DurableJobLeaseError("original inspected repository source changed")
            token = entered.set((asyncio.current_task(), witness, staged_config,
                canonical_digest(settings.repo_sandbox.model_dump(mode="json")), staged_limits, staged_policy))
            try:
                yield
                if not issued:
                    raise DurableJobLeaseError("repository admission replay requires its original scoped recovery")
            finally:
                entered.reset(token)

    async def check(current_db, run):
        nonlocal issued
        from src.workflows.general_task_accounting import validate_group_owner
        from config.settings import settings
        held = entered.get()
        if held is None or held[:2] != (asyncio.current_task(), witness) or compiled is None:
            raise DurableJobLeaseError("original repository source admission scope required")
        _assert_task_publication_configuration(service, staged_config=held[2])
        if (canonical_digest(settings.repo_sandbox.model_dump(mode="json")) != held[3]
                or repository_work_descriptor() != fixed_descriptor):
            raise DurableJobLeaseError("original repository configuration changed")
        for model, key, original in rows:
            current = await current_db.get(model, key, populate_existing=True)
            if current is None or _canonical(current.model_dump(mode="json")) != original:
                raise DurableJobLeaseError("original repository admission source row changed")
        await validate_group_owner(current_db, group)
        from sqlalchemy import func
        from src.workflows.job_runtime import _canonical_goal_max_outstanding, DURABLE_JOB_TERMINAL_STATUSES, DURABLE_JOB_RECORD_SCHEMA_VERSION
        goal_capacity = _canonical_goal_max_outstanding(goal) or 1
        outstanding = await current_db.scalar(select(func.count(WorkflowRunState.run_identity)).where(
            WorkflowRunState.goal_id == binding.goal_id,
            WorkflowRunState.parent_job_id.is_(None), WorkflowRunState.parent_run_identity.is_(None),
            WorkflowRunState.status.not_in(tuple(DURABLE_JOB_TERMINAL_STATUSES)),
            WorkflowRunState.record_schema_version >= DURABLE_JOB_RECORD_SCHEMA_VERSION))
        if int(outstanding or 0) >= goal_capacity:
            raise DurableJobLeaseError("original Goal outstanding capacity cannot admit repository root")
        # Permanent single use is the existing indexed canonical idempotency
        # binding, including terminal/Unknown rows, not an all-history scan.
        existing_mapping = await current_db.scalar(select(WorkflowRunState).where(
            WorkflowRunState.idempotency_binding == permanent_binding))
        if existing_mapping is not None:
            raise DurableJobLeaseError("original C1 invocation already owns its repository root")
        now = _utc_now()
        cutoff = _as_utc(run.deadline_at)
        if (child.status != "running" or child.attempt_count != 1 or child.fencing_token < 1
                or not child.lease_owner or _as_utc(child.lease_expires_at) <= now
                or _as_utc(attempt.lease_expires_at) <= now
                or run.job_kind != "engineering.repo-repair.v1" or run.capability_version != "1"
                or run.root_run_identity != run.run_identity or run.parent_job_id
                or run.idempotency_scope != "original-repository-child"
                or run.idempotency_key != binding.invocation_id
                or run.idempotency_binding != permanent_binding
                or run.input_digest != input_artifact.payload_sha256
                or (run.owner_principal_id, run.operator_session_id, run.goal_id, run.goal_revision) !=
                    (binding.owner_principal_id, binding.original_root_id, binding.goal_id, binding.goal_revision)
                or cutoff is None or not now < cutoff <= min(group.original_deadline_at,
                    _repository_goal_cutoff(held[4], group),
                    binding.original_deadline_at, binding.native_deadline_at,
                    _as_utc(child.lease_expires_at), _as_utc(attempt.lease_expires_at),
                    now + timedelta(seconds=work.limits.max_total_seconds))
                or json.loads(run.checkpoint_receipts_json or "[]")):
            raise DurableJobLeaseError("new original repository root bounds changed")
        payload = {"schema_version": "repository.original.v1", "repository_job_id": run.run_identity,
            "repository_task_id": task.task_id, "repository_attempt_id": attempt.attempt_id,
            "repository_input_artifact_digest": input_artifact.payload_sha256,
            "original_input": work.model_dump(mode="json"), "compiled_input": compiled.model_dump(mode="json"),
            "original_deadline_at": cutoff.isoformat(), "original_max_cost_microusd": work.limits.max_cost_microusd,
            "group": group.model_dump(mode="json"), "native_binding": binding.model_dump(mode="json"),
            "source_binding": source.model_dump(mode="json")}
        run.checkpoint_receipts_json = _canonical([{"checkpoint_id": "repository:original:v1",
            "state_digest": _digest(payload), "safe": True, "payload": payload,
            "created_at": now.isoformat()}])
        from src.execution.repo_original_producer import original_producer_service_enabled
        if not original_producer_service_enabled(service, service.jobs):
            raise DurableJobLeaseError("original producer admission owner changed")
        inventory_schema = "repository.checkpoint_inventory.v3"
        inventory = repository_checkpoint_inventory(run, work, _admission_schema=inventory_schema)
        _append_repository_record(run, "repository:inventory:v1",
            {"schema": inventory_schema, "identities": inventory,
             "max_records": 50, "max_metadata_bytes_per_record": 16384,
             "original_limits": held[4], "original_limits_digest": _source_digest(held[4])}, inventory=inventory)
        read_repository_inventory(run)
        issued = True

    return check, scope


def iteration_identity(repository_job_id: str, repository_attempt_id: str,
                       original_input_digest: str, index: int) -> str:
    if (not repository_job_id or not repository_attempt_id
            or not _SHA.fullmatch(original_input_digest) or type(index) is not int
            or not 1 <= index <= 3):
        raise ValueError("original repository binding and bounded index required")
    payload = ["seraph.repository_iteration.v1", repository_job_id,
               repository_attempt_id, original_input_digest, index]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RepoIterationProcessBinding:
    repository_job_id: str
    repository_attempt_id: str
    repository_fence: int
    iteration_index: int
    iteration_id: str
    original_deadline_at: datetime
    authority_digest: str
    base_digest: str
    _seal: object = field(default=None, repr=False, compare=False)

    def projection(self) -> dict[str, Any]:
        return {name: (value.isoformat() if isinstance(value, datetime) else value)
                for name in self.__dataclass_fields__ if not name.startswith("_")
                for value in [getattr(self, name)]}


def assert_repo_iteration_process_binding(binding, job, *, allow_expired_for_cleanup=False) -> None:
    from src.execution.repo_sandbox import RepoSandboxError
    if (type(binding) is not RepoIterationProcessBinding or binding._seal is not _SEAL
            or type(binding.iteration_index) is not int or not 1 <= binding.iteration_index <= 3
            or not _SHA.fullmatch(binding.iteration_id)
            or binding.repository_job_id != job.job_id
            or binding.repository_attempt_id != job.attempt_id
            or type(binding.repository_fence) is not int or binding.repository_fence < 1
            or binding.repository_fence != job.fencing_token
            or binding.authority_digest != job.authority_digest
            or binding.base_digest != job.base_digest
            or binding.original_deadline_at.tzinfo is None
            or (not allow_expired_for_cleanup and binding.original_deadline_at <= datetime.now(timezone.utc))
            or not job.execution_deadline_at):
        raise RepoSandboxError("original source-issued iteration binding required")
    try:
        command_deadline = datetime.fromisoformat(job.execution_deadline_at.replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise RepoSandboxError("fixed iteration command deadline required") from exc
    if command_deadline.tzinfo is None or command_deadline > binding.original_deadline_at:
        raise RepoSandboxError("command cannot extend the original repository cutoff")


@dataclass(frozen=True, slots=True)
class _CanonicalRepositorySource:
    """Immutable physical-source staging projected onto exact SQL fences."""
    parent_revision: int
    parent_checkpoint_json: str
    parent_authority_json: str
    parent_envelope_json: str
    task_revision: int
    parent_attempt_fence: int
    repository_attempt_fence: int
    child_revision: int
    child_fence: int
    child_authority_json: str
    child_arguments_json: str
    native_binding_json: str
    parent_row_json: str
    task_row_json: str
    parent_attempt_row_json: str
    repository_attempt_row_json: str
    child_row_json: str
    repository_task_row_json: str
    input_artifact_row_json: str
    consent_row_json: str
    consent_id: str
    goal_row_json: str = ""
    original_limits_json: str = ""
    _contacted_wait_parent_json: str | None = field(default=None, repr=False, compare=False)
    _contacted_wait_child_json: str | None = field(default=None, repr=False, compare=False)
    _final_source: Any = field(default=None, repr=False, compare=False)
    _final_evidence_json: str | None = field(default=None, repr=False, compare=False)
    _recovered_completion: Any = field(default=None, repr=False, compare=False)
    _seal: object = field(default=None, repr=False, compare=False)

    def projection(self) -> dict[str, Any]:
        assert_repository_canonical_source(self)
        return {name: getattr(self, name) for name in self.__dataclass_fields__
                if not name.startswith("_")}


def assert_repository_canonical_source(source) -> None:
    """Only the actual source owner can stamp a staged SQL/physical binding."""
    from src.workflows.job_runtime import DurableJobLeaseError
    if type(source) is not _CanonicalRepositorySource:
        raise DurableJobLeaseError("original producer-sealed repository canonical source required")
    if source._seal is not _SEAL:
        if source._recovered_completion is None or source._seal is not source._recovered_completion:
            raise DurableJobLeaseError("recovered canonical source seal changed")
        from src.workflows.repo_repair_source_recovery import assert_repository_completion_final_source
        assert_repository_completion_final_source(source)
    elif source._recovered_completion is not None:
        raise DurableJobLeaseError("recovered canonical source cannot use the live seal")
    if (any(not getattr(source, name) for name in (
                "parent_row_json", "task_row_json", "parent_attempt_row_json",
                "parent_envelope_json",
                "repository_attempt_row_json", "child_row_json", "repository_task_row_json",
                "input_artifact_row_json", "consent_row_json", "consent_id", "native_binding_json",
                "goal_row_json", "original_limits_json"))):
        raise DurableJobLeaseError("original producer-sealed repository canonical source required")


async def stage_repository_canonical_source(service, db, *, repository_job_id,
        native_invocation_id, consent_id):
    """Read actual physical artifacts before either canonical writer.

    This cannot bootstrap a root: the protected original handoff must already
    exist, with the same seven-field input and source-issued Task artifact.
    """
    from src.db.models import (WorkflowRunState, WorkBoardTask, WorkBoardAttempt,
        WorkBoardInputArtifact, RepoRepairEgressConsent, Goal)
    from src.workflows.repo_repair import RepoRepairService
    from src.work_board.contracts import WorkBoardOwner
    from src.workflows.general_task_guard import (child_binding, read_manifest,
        assert_general_task_child_current)
    from src.work_board.general_task_runtime_artifacts import verify_general_task_manifest
    from src.workflows.job_runtime import DurableJobLeaseError
    if type(service) is not RepoRepairService:
        raise DurableJobLeaseError("actual fixed repository source owner required")
    from sqlalchemy import select
    run = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == repository_job_id).execution_options(populate_existing=True))
    child = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == native_invocation_id).execution_options(populate_existing=True))
    if run is None or child is None:
        raise DurableJobLeaseError("original repository and native child required")
    original, work, compiled, group, binding, task_source = read_repository_original(run)
    goal = await db.get(Goal, binding.goal_id, populate_existing=True)
    original_limits = _assert_repository_original_limits(run, goal, _repository_policy_limits())
    if child_binding(child) != binding:
        raise DurableJobLeaseError("original repository native child changed")
    await assert_general_task_child_current(db, child)
    parent = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == binding.parent_job_id).execution_options(populate_existing=True))
    task = await db.scalar(select(WorkBoardTask).where(
        WorkBoardTask.task_id == binding.task_id).execution_options(populate_existing=True))
    attempt = await db.get(WorkBoardAttempt, binding.attempt_id, populate_existing=True)
    if parent is None or task is None or attempt is None:
        raise DurableJobLeaseError("original C1 Task source required")
    manifest = read_manifest(parent)
    if manifest is None:
        raise DurableJobLeaseError("original C1 manifest required")
    envelope = await verify_general_task_manifest(db, parent, task, attempt, manifest)
    if envelope.repository_source != task_source or envelope.proposal_group != group:
        raise DurableJobLeaseError("original scoped repository Task artifact required")
    repo_authority = await service._resolve_canonical_authority(db,
        owner=WorkBoardOwner(principal_id=run.owner_principal_id, session_id=run.operator_session_id),
        work_board_task_id=original["repository_task_id"],
        work_board_attempt_id=original["repository_attempt_id"], workflow_run_id=run.run_identity,
        goal_id=run.goal_id, goal_revision=run.goal_revision,
        lease_owner=run.lease_owner, fencing_token=run.fencing_token, consent_id=consent_id)
    if repo_authority.original_work_input != work or repo_authority.input != compiled:
        raise DurableJobLeaseError("original seven-field input or compilation changed")
    input_artifact = await db.get(WorkBoardInputArtifact, repo_authority.task.input_artifact_id)
    consent = await db.get(RepoRepairEgressConsent, consent_id, populate_existing=True)
    if (input_artifact is None or consent is None or consent.workflow_run_id != run.run_identity
            or not consent.iteration_id or consent.diagnostics_acknowledged is not True
            or consent.egress_envelope_schema != "seraph.repo_iteration_egress.v1"
            or not consent.diagnostics_sha256 or not consent.redaction_version
            or not consent.serialized_request_sha256
            or type(consent.combined_input_bytes) is not int
            or not 0 < consent.combined_input_bytes <= min(65536, consent.maximum_input_bytes)
            or consent.owner_principal_id != run.owner_principal_id
            or consent.owner_session_id != run.operator_session_id):
        raise DurableJobLeaseError("exact fresh repository diagnostics consent required")
    egress = json.loads(service._read_private_artifact(consent.egress_envelope_artifact_ref,
        expected_digest=consent.egress_envelope_sha256))
    from src.workflows.repo_repair import _digest_bytes, _canonical_bytes
    if (egress.get("schema") != consent.egress_envelope_schema
            or egress.get("iteration_id") != consent.iteration_id
            or _digest_bytes(_canonical_bytes(egress.get("diagnostics"))) != consent.diagnostics_sha256
            or egress.get("diagnostics", {}).get("redaction_version") != consent.redaction_version):
        raise DurableJobLeaseError("exact fresh repository diagnostics envelope changed")
    def row_json(row):
        return json.dumps(row.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    source = _CanonicalRepositorySource(
        parent_revision=parent.revision, parent_checkpoint_json=parent.checkpoint_receipts_json,
        parent_authority_json=parent.declared_authority_json, task_revision=task.task_revision,
        parent_envelope_json=json.dumps(envelope.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
        parent_attempt_fence=attempt.fencing_token,
        repository_attempt_fence=repo_authority.attempt.fencing_token,
        child_revision=child.revision, child_fence=child.fencing_token,
        child_authority_json=child.declared_authority_json, child_arguments_json=child.arguments_json,
        native_binding_json=json.dumps(binding.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
        parent_row_json=row_json(parent), task_row_json=row_json(task),
        parent_attempt_row_json=row_json(attempt), repository_attempt_row_json=row_json(repo_authority.attempt),
        child_row_json=row_json(child), repository_task_row_json=row_json(repo_authority.task),
        input_artifact_row_json=row_json(input_artifact), consent_row_json=row_json(consent),
        consent_id=consent.id, goal_row_json=row_json(goal),
        original_limits_json=json.dumps(original_limits, sort_keys=True, separators=(",", ":")), _seal=_SEAL)
    assert_repository_canonical_source(source)
    return source


@dataclass(frozen=True, slots=True)
class RepositoryIterationAccountingWitness:
    group: TaskProposalGroupV1
    repository_job_id: str
    repository_attempt_id: str
    repository_fence: int
    parent_task_id: str
    parent_attempt_id: str
    native_invocation_id: str
    iteration_index: int
    iteration_id: str
    operation_id: str
    original_deadline_at: datetime
    original_max_cost_microusd: int
    source_checkpoint_digest: str
    _request_body_digest: str | None = field(default=None, repr=False, compare=False)
    _request_route_digest: str | None = field(default=None, repr=False, compare=False)
    _canonical_source: _CanonicalRepositorySource | None = field(default=None, repr=False, compare=False)
    _seal: object = field(default=None, repr=False, compare=False)

    def projection(self) -> dict[str, Any]:
        result = {name: getattr(self, name) for name in self.__dataclass_fields__
                  if not name.startswith("_")}
        result["group"] = self.group.model_dump(mode="json")
        result["original_deadline_at"] = self.original_deadline_at.isoformat()
        return result


def assert_repository_iteration_witness(witness) -> None:
    from src.workflows.inference_accounting import InferenceAccountingError
    if (type(witness) is not RepositoryIterationAccountingWitness or witness._seal is not _SEAL
            or type(witness.group) is not TaskProposalGroupV1
            or type(witness.iteration_index) is not int or not 1 <= witness.iteration_index <= 3
            or not _SHA.fullmatch(witness.iteration_id)
            or not _SHA.fullmatch(witness.source_checkpoint_digest)
            or witness.operation_id != "remote:repo-work:" + witness.iteration_id
            or type(witness.repository_fence) is not int or witness.repository_fence < 1
            or type(witness.original_max_cost_microusd) is not int
            or witness.original_max_cost_microusd < 0
            or witness.original_deadline_at.tzinfo is None
            or witness.original_deadline_at > witness.group.original_deadline_at
            or any(not value for value in (witness.repository_job_id, witness.repository_attempt_id,
                witness.parent_task_id, witness.parent_attempt_id, witness.native_invocation_id))):
        raise InferenceAccountingError("repository_iteration_source_witness_required")


def repository_transport_route_digest(target: dict, runtime_path: str) -> str:
    from src.security.trust_contract import canonical_digest
    return canonical_digest({"runtime_path": runtime_path, "source": target.get("source"),
        "endpoint": str(target.get("api_base") or "").rstrip("/"),
        "profile": target.get("profile"), "model_id": target.get("model_id"),
        "options": target.get("options")})


def verify_repository_iteration_transport_body(body, *, target, runtime_path) -> None:
    """Only a genuine source context can require the fixed iteration body."""
    from src.model_fabric.accounting import current_repository_iteration_accounting_witness
    from src.security.trust_contract import canonical_digest
    from src.workflows.inference_accounting import InferenceAccountingError
    witness = current_repository_iteration_accounting_witness()
    if witness is None:
        return
    assert_repository_iteration_witness(witness)
    if (runtime_path != "strategist_agent" or target.get("source") != "primary"
            or str(target.get("api_base") or "").rstrip("/") != "https://openrouter.ai/api/v1"
            or witness._request_body_digest is None or witness._request_route_digest is None
            or canonical_digest(body) != witness._request_body_digest
            or repository_transport_route_digest(target, runtime_path) != witness._request_route_digest):
        raise InferenceAccountingError("repository_iteration_final_transport_changed")


async def validate_repository_iteration_accounting(db, witness, run, *, rows,
        operation_id, payload_digest, bound_microusd, deadline_at,
        already_reserved=False) -> dict[str, Any]:
    """Recheck private source and both original owners in the accounting writer."""
    from sqlalchemy import select
    from src.db.models import (WorkflowRunState, WorkBoardAttempt, WorkBoardTask,
        WorkBoardInputArtifact, RepoRepairEgressConsent, Goal)
    from src.workflows.general_task_guard import _history, child_binding, read_manifest
    from src.workflows.inference_accounting import InferenceAccountingError, _utc
    assert_repository_iteration_witness(witness)
    source = witness._canonical_source
    if (type(source) is not _CanonicalRepositorySource or source._seal is not _SEAL
            or any(not getattr(source, name) for name in (
                "parent_row_json", "task_row_json", "parent_attempt_row_json",
                "repository_attempt_row_json", "child_row_json", "repository_task_row_json",
                "input_artifact_row_json", "consent_row_json", "consent_id"))):
        raise InferenceAccountingError("repository_iteration_staged_source_required")
    now = datetime.now(timezone.utc)
    def blocked(code="repository_iteration_binding_changed"):
        raise InferenceAccountingError(code)
    goal = await db.get(Goal, witness.group.goal_id, populate_existing=True)
    limits = read_repository_inventory(run)["original_limits"]
    if (goal is None or json.dumps(goal.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) != source.goal_row_json
            or json.dumps(limits, sort_keys=True, separators=(",", ":")) != source.original_limits_json
            or bound_microusd != limits["original_server_bound_microusd"]):
        blocked("repository_original_limits_changed")
    if _repository_record(run, "repository:stop-intent:v1") is not None:
        existing = next((row for row in rows if row.operation_id == operation_id), None)
        if not already_reserved or existing is None or existing.contact_started_at is None:
            blocked("repository_stop_prevents_new_contact")
    contacted_wait = bool(source._contacted_wait_parent_json and source._contacted_wait_child_json)
    if contacted_wait and not already_reserved:
        blocked("repository_contacted_wait_cannot_reserve")
    if (run.run_identity != witness.repository_job_id or run.status != "running"
            or run.fencing_token != witness.repository_fence
            or not run.lease_owner or run.lease_expires_at is None
            or _utc(run.lease_expires_at) <= now or run.attempt_count != 1
            or operation_id != witness.operation_id or not _SHA.fullmatch(payload_digest)
            or type(bound_microusd) is not int or bound_microusd < 0
            or _utc(deadline_at) > witness.original_deadline_at
            or _utc(run.deadline_at) > witness.original_deadline_at
            or witness.original_deadline_at <= now):
        blocked()
    repo_attempt = await db.scalar(select(WorkBoardAttempt).where(
        WorkBoardAttempt.attempt_id == witness.repository_attempt_id))
    parent_attempt = await db.scalar(select(WorkBoardAttempt).where(
        WorkBoardAttempt.attempt_id == witness.parent_attempt_id))
    task = await db.scalar(select(WorkBoardTask).where(
        WorkBoardTask.task_id == witness.parent_task_id))
    child = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == witness.native_invocation_id))
    if (repo_attempt is None or repo_attempt.workflow_run_id != run.run_identity
            or parent_attempt is None or task is None or child is None
            or parent_attempt.task_id != task.task_id):
        blocked()
    repo_task = await db.scalar(select(WorkBoardTask).where(
        WorkBoardTask.task_id == repo_attempt.task_id))
    input_artifact = await db.get(WorkBoardInputArtifact, repo_task.input_artifact_id) if repo_task else None
    consent = await db.get(RepoRepairEgressConsent, source.consent_id)
    def row_json(row):
        return json.dumps(row.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    parent = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == parent_attempt.workflow_run_id))
    if (parent is None or repo_task is None or input_artifact is None or consent is None
            or parent.status != "paused"
            or row_json(parent) != (source._contacted_wait_parent_json or source.parent_row_json)
            or row_json(task) != source.task_row_json
            or row_json(parent_attempt) != source.parent_attempt_row_json
            or row_json(repo_attempt) != source.repository_attempt_row_json
            or row_json(child) != (source._contacted_wait_child_json or source.child_row_json)
            or row_json(repo_task) != source.repository_task_row_json
            or row_json(input_artifact) != source.input_artifact_row_json
            or row_json(consent) != source.consent_row_json
            or repo_attempt.ended_at is not None or repo_attempt.cancel_requested_at is not None
            or parent_attempt.ended_at is not None or parent_attempt.cancel_requested_at is not None
            or parent.revision != (json.loads(source._contacted_wait_parent_json)["revision"]
                if contacted_wait else source.parent_revision)
            or parent.checkpoint_receipts_json != (json.loads(source._contacted_wait_parent_json)["checkpoint_receipts_json"]
                if contacted_wait else source.parent_checkpoint_json)
            or parent.declared_authority_json != source.parent_authority_json
            or task.task_revision != source.task_revision
            or parent_attempt.fencing_token != source.parent_attempt_fence
            or repo_attempt.fencing_token != source.repository_attempt_fence
            or child.revision != (source.child_revision + 1 if contacted_wait else source.child_revision)
            or child.fencing_token != source.child_fence
            or child.declared_authority_json != source.child_authority_json
            or child.arguments_json != source.child_arguments_json
            or child.status != ("paused" if contacted_wait else "running") or child.attempt_count != 1
            or not child.lease_owner or child.lease_expires_at is None
            or _utc(child.lease_expires_at) <= now or _utc(child.deadline_at) <= now):
        blocked()
    binding = child_binding(child)
    manifest = read_manifest(parent)
    if (manifest is None or manifest.phase != "native_wait"
            or binding.task_id != task.task_id or binding.attempt_id != parent_attempt.attempt_id
            or binding.parent_job_id != parent.run_identity
            or binding.invocation_id not in manifest.admitted_invocation_ids
            or json.loads(source.native_binding_json) != binding.model_dump(mode="json")):
        blocked()
    from src.work_board.general_task import digest
    if (manifest.group_id != witness.group.group_id
            or manifest.group_digest != digest(witness.group.model_dump(mode="json"))):
        blocked("repository_iteration_original_group_changed")
    history = _history(run)
    originals = [item for item in history if item.get("checkpoint_id") == "repository:original:v1"]
    iterations = [item for item in history if item.get("checkpoint_id") ==
                  "repository:iteration:" + witness.iteration_id]
    if len(originals) != 1 or len(iterations) != 1:
        blocked("repository_iteration_source_checkpoint_missing")
    original, iteration = originals[0], iterations[0]
    original_payload, iteration_payload = original.get("payload"), iteration.get("payload")
    from src.workflows.job_runtime import _digest
    if (original.get("safe") is not True or iteration.get("safe") is not True
            or not isinstance(original_payload, dict) or not isinstance(iteration_payload, dict)
            or original.get("state_digest") != _digest(original_payload)
            or original.get("state_digest") != witness.source_checkpoint_digest
            or iteration.get("state_digest") != _digest(iteration_payload)
            or original_payload.get("native_binding") != binding.model_dump(mode="json")
            or original_payload.get("group") != witness.group.model_dump(mode="json")
            or original_payload.get("original_max_cost_microusd") != witness.original_max_cost_microusd
            or original_payload.get("original_deadline_at") != witness.original_deadline_at.isoformat()
            or iteration_payload.get("accounting_binding") != witness.projection()
            or iteration_payload.get("payload_digest") != payload_digest
            or iteration_payload.get("phase") != "model_ready"):
        blocked()
    members = [row for row in rows if row.job_id == witness.repository_job_id]
    existing = [row for row in members if row.operation_id == operation_id]
    if already_reserved != bool(existing) or len(existing) > 1:
        blocked("repository_iteration_reservation_identity_changed")
    if contacted_wait and (existing[0].state != "settled" or existing[0].contact_started_at is None):
        blocked("repository_contacted_wait_requires_settled_response")
    from src.workflows.general_task_accounting import reservation_liability, entry_for
    liability = 0
    for row in members:
        entry = entry_for(row)
        member_binding = entry.get("repository_binding") if entry else None
        if (not entry or entry.get("role") != "repository_iteration"
                or not isinstance(member_binding, dict)
                or member_binding.get("source_checkpoint_digest") != witness.source_checkpoint_digest
                or member_binding.get("repository_job_id") != witness.repository_job_id
                or member_binding.get("group") != witness.group.model_dump(mode="json")):
            blocked("repository_iteration_reservation_source_changed")
        if row.state == "unknown":
            blocked("repository_iteration_unknown")
        liability += reservation_liability(row)
    if liability + (0 if already_reserved else bound_microusd) > witness.original_max_cost_microusd:
        blocked("repository_iteration_original_cost_limit")
    return witness.projection()


# Optional publication carries private authority from this same inspected Source
# owner. Its JSON projection is only preview evidence, never an issuer input.
import weakref as _publication_weakref
_PUBLICATION_WITNESSES = _publication_weakref.WeakKeyDictionary()


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class _RepositoryPublicationWitness:
    service: object = field(repr=False)
    jobs: object = field(repr=False)
    rows: tuple = field(repr=False)
    projection_json: str = field(repr=False)
    original_json: str = field(repr=False)
    configuration_json: str = field(repr=False)

    def projection(self):
        return json.loads(self.projection_json)


def assert_repository_publication_witness(witness):
    from src.workflows.repo_repair import RepoRepairService
    from src.workflows.job_runtime import DurableJobLeaseError
    if (type(witness) is not _RepositoryPublicationWitness
            or _PUBLICATION_WITNESSES.get(witness) is not witness.service
            or type(witness.service) is not RepoRepairService or witness.service.jobs is not witness.jobs):
        raise DurableJobLeaseError("actual registered repository publication Source witness required")


async def repository_publication_source_present(jobs, repair_job_id):
    from sqlalchemy import select
    from src.db.models import WorkflowRunState
    async with jobs._session() as db:
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == repair_job_id))
        if run is None:
            return False
        original = _repository_record(run, "repository:original:v1")
        if original is None:
            from src.workflows.general_task_guard import _history
            from src.workflows.job_runtime import DurableJobLeaseError
            if any(item["checkpoint_id"].startswith("repository:") for item in _history(run)):
                raise DurableJobLeaseError("protected repository Source original is missing")
            return False
        read_repository_original(run)
        return True


async def stage_repository_publication_witness(service, jobs, *, repair_job_id, owner):
    from sqlalchemy import select, inspect as inspect_mapper
    from src.db.models import (WorkBoardTask, WorkBoardAttempt, WorkBoardInputArtifact, Goal,
        OperatorSession, RepoRepairProposal, RepoRepairSourcePacket, ApprovalRequest,
        GitHubFollowthroughConnection, WorkflowRunState)
    from src.workflows.repo_repair import RepoRepairService
    from src.workflows.job_runtime import DurableJobLeaseError, _canonical, _as_utc, _utc_now, _binding, _assert_canonical_goal_fence
    from src.workflows.general_task_guard import child_binding, read_manifest
    from src.work_board.general_task_runtime_artifacts import (_verify_native_manifest_data,
        read_native_artifact_reference)
    from src.work_board.input_artifacts import resolve_input_artifact_for_task
    from src.work_board.contracts import GeneralTaskEnvelope, GeneralTaskArtifactRef
    from src.work_board.general_task_native import current_plan
    from src.workflows.repo_repair_stop import _accounting
    from src.work_board.pipelines import root_binding
    if type(service) is not RepoRepairService or service.jobs is not jobs:
        raise DurableJobLeaseError("actual original publication Source owner required")
    _assert_task_publication_configuration(service)
    async with jobs._session() as db:
        run = await jobs._fetch(db, repair_job_id)
        original, work, compiled, group, binding, task_source = read_repository_original(run)
        terminal = _repository_record(run, "repository:terminal:v1")
        reservation = jobs._repo_repair_reservation_state(run)
        if (run.status != "succeeded" or run.lease_owner or run.lease_expires_at
                or (run.owner_principal_id, run.operator_session_id) != (owner.principal_id, owner.session_id)
                or terminal is None or terminal.get("schema") != "repository.final_verified.v1"
                or _repository_record(run, "repository:stop-intent:v1") is not None
                or reservation is None or reservation.get("status") != "released"):
            raise DurableJobLeaseError("actual final original repository success required for publication")
        permanent = _binding(owner_principal_id=binding.owner_principal_id, goal_id=binding.goal_id,
            goal_revision=binding.goal_revision, idempotency_scope="original-repository-child", dedupe_key=binding.invocation_id)
        mapped = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.idempotency_binding == permanent))
        if mapped is None or mapped.run_identity != run.run_identity:
            raise DurableJobLeaseError("original publication native mapping changed")
        parent = await jobs._fetch(db, binding.parent_job_id)
        child = await jobs._fetch(db, binding.invocation_id)
        c1task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
        c1attempt = await db.get(WorkBoardAttempt, binding.attempt_id)
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == original["repository_task_id"]))
        attempt = await db.get(WorkBoardAttempt, original["repository_attempt_id"])
        artifact = await db.get(WorkBoardInputArtifact, c1task.input_artifact_id) if c1task else None
        repo_artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id) if task else None
        goal = await db.get(Goal, binding.goal_id)
        await _assert_canonical_goal_fence(db, goal_id=binding.goal_id, goal_revision=binding.goal_revision,
            owner_kind="user", owner_principal_id=owner.principal_id, session_id=owner.session_id)
        _assert_repository_original_limits(run, goal, _repository_policy_limits())
        operator = await db.get(OperatorSession, owner.session_id)
        connection = await db.scalar(select(GitHubFollowthroughConnection).where(
            GitHubFollowthroughConnection.owner_principal_id == owner.principal_id))
        identity = terminal["iteration_id"]
        execution = _repository_record(run, "repository:execution:" + identity)
        readback = _repository_record(run, "repository:readback:" + identity)
        prepared = _repository_record(run, "repository:prepared:" + identity)
        proposal = await db.get(RepoRepairProposal, execution["proposal_id"]) if execution else None
        approval = await db.get(ApprovalRequest, execution["approval_id"]) if execution else None
        packet = await db.get(RepoRepairSourcePacket, proposal.source_packet_id) if proposal else None
        if (any(row is None for row in (c1task, c1attempt, task, attempt, artifact, repo_artifact, goal,
                operator, proposal, approval, packet, connection))
                or child.status != "succeeded" or child_binding(child) != binding
                or child.attempt_count != 1 or child.lease_owner
                or parent.status != "succeeded" or parent.lease_owner or parent.lease_expires_at
                or c1task.status.value != "done" or c1attempt.ended_at is None
                or c1attempt.cancel_requested_at or c1attempt.workflow_run_id != parent.run_identity
                or read_manifest(parent) is None or read_manifest(parent).phase != "assembly"
                or task.status.value != "done" or attempt.ended_at is None or attempt.cancel_requested_at
                or goal.revision != binding.goal_revision
                or (goal.owner_principal_id, goal.owner_session_id) != (owner.principal_id, owner.session_id)
                or operator.principal_id != owner.principal_id or operator.revoked_at or operator.replaced_by_id
                or operator.is_bearer_tombstone or _as_utc(operator.absolute_expires_at) <= _utc_now()
                or _as_utc(operator.idle_expires_at) <= _utc_now()
                or proposal.workflow_run_id != run.run_identity or proposal.status != "execution_verified"
                or approval.status != "consumed" or approval.fingerprint != execution["approval_fingerprint"]
                or proposal.approval_id != approval.id or readback is None or readback.get("status") != "succeeded"):
            raise DurableJobLeaseError("original publication current Source lineage changed")
        # Publication consumes retained terminal lineage, never a renewed live
        # native attempt. Keep the ordinary live-envelope verifier unchanged.
        resolved = await resolve_input_artifact_for_task(db, owner,
            artifact_id=c1task.input_artifact_id, goal_id=c1task.goal_id,
            goal_revision=c1task.goal_revision, capability_id="agent.task.v1",
            expected_task_id=c1task.task_id)
        envelope = await _verify_native_manifest_data(parent, c1task, c1attempt,
            read_manifest(parent), GeneralTaskEnvelope.model_validate(resolved.input))
        native_manifest = read_manifest(parent)
        plan = current_plan(native_manifest, envelope)
        settled_steps = set()
        for index, step_id in enumerate(native_manifest.step_ids):
            receipt = read_native_artifact_reference(GeneralTaskArtifactRef(
                artifact_id=native_manifest.step_receipt_artifact_ids[index],
                digest=native_manifest.step_receipt_digests[index], schema_version="StepReceipt.v1"),
                parent_job_id=parent.run_identity, creation_digest=native_manifest.creation_digest)
            if receipt.status != "verified" or receipt.contact_state != "settled":
                raise DurableJobLeaseError("actual completed C1 plan receipts required")
            settled_steps.add(step_id)
        latest_attempt = await db.scalar(select(WorkBoardAttempt).where(
            WorkBoardAttempt.task_id == c1task.task_id).order_by(WorkBoardAttempt.started_at.desc()).limit(1))
        if (settled_steps != {step.step_id for step in plan.steps} or latest_attempt is None
                or latest_attempt.attempt_id != c1attempt.attempt_id):
            raise DurableJobLeaseError("exact complete latest original C1 plan required")
        if envelope.repository_source != task_source or envelope.proposal_group != group:
            raise DurableJobLeaseError("original scoped publication Task changed")
        await _accounting(db, {"group": group, "run": run, "work": work, "original": original})
        rows = []
        for row in (run,parent,child,c1task,c1attempt,task,attempt,artifact,repo_artifact,goal,operator,
                proposal,approval,packet,connection):
            keys = tuple(getattr(row,col.key) for col in inspect_mapper(type(row)).primary_key)
            rows.append((type(row), keys[0] if len(keys)==1 else keys, _canonical(row.model_dump(mode="json"))))
    if _source_digest(root_binding()) != binding.live_root_digest:
        raise DurableJobLeaseError("original physical publication Root changed")
    from src.work_board.dispatcher import WorkBoardDispatcher
    parent_projection = await jobs.get_job(parent.run_identity)
    if not isinstance(parent_projection, dict) or parent_projection.get("status") != "succeeded":
        raise DurableJobLeaseError("actual final C1 publication parent required")
    from src.work_board.general_task import GeneralTaskService
    # This existing read-only verifier uses no receiver state and grants no
    # execution. The checked Source owner supplies its current canonical data.
    recovered_outputs, recovered_artifacts = GeneralTaskService.recovered_outputs(service,
        parent_projection, envelope.model_copy(update={"plan": plan}))
    final_artifact = recovered_artifacts.get(plan.steps[-1].step_id)
    parent_readback = WorkBoardDispatcher._workflow_readback(parent_projection or {}, parent.run_identity)
    references = json.loads(c1task.result_refs_json or "[]")
    verified_refs = [item for item in references if isinstance(item, dict)
        and isinstance(item.get("file_path"), str) and parent_readback is not None
        and item.get("content_sha256") == parent_readback["content_sha256"]]
    if (parent_readback is None or len(verified_refs) != 1
            or set(recovered_outputs) != {step.step_id for step in plan.steps}
            or final_artifact is None
            or any(key not in final_artifact or key not in verified_refs[0]
                or final_artifact[key] != verified_refs[0][key]
                for key in ("file_path", "content_sha256", "size_bytes"))
            or ("artifact_id" in final_artifact or "artifact_id" in verified_refs[0])
                and final_artifact.get("artifact_id") != verified_refs[0].get("artifact_id")
            or not any(effect.get("effect_type") == "board_child_readback"
                and effect.get("receipt_kind") == "readback" and effect.get("status") == "succeeded"
                and effect.get("content_sha256") == final_artifact["content_sha256"]
                and effect.get("target_path") == final_artifact["file_path"]
                and effect.get("details", {}).get("verified") is True
                for effect in parent_projection.get("effects", []))
            or final_artifact["content_sha256"] != parent_readback["content_sha256"]):
        raise DurableJobLeaseError("actual final C1 publication artifact readback required")
    service._read_private_artifact("workspace-json:" + verified_refs[0]["file_path"],
        expected_digest=parent_readback["content_sha256"])
    source_facts = json.loads(service._read_private_artifact(task_source.source_artifact_ref,
        expected_digest=task_source.source_artifact_digest))
    if service.recheck_task_source_snapshot(work, source_facts) != compiled:
        raise DurableJobLeaseError("original acknowledged publication source changed")
    artifacts = terminal.get("publication_artifacts")
    if not isinstance(artifacts,dict) or set(artifacts) != {"manifest.json","readback.json","diff.patch"}:
        raise DurableJobLeaseError("literal publication artifacts absent")
    bodies = {}
    registered = json.loads(run.artifact_receipts_json)
    for name, item in artifacts.items():
        if set(item) != {"path","sha256"} or item["path"] != f"artifacts/repo-repair/{repair_job_id}/{name}":
            raise DurableJobLeaseError("original publication artifact mapping changed")
        if len([row for row in registered if row.get("file_path")==item["path"] and row.get("content_sha256")==item["sha256"]]) != 1:
            raise DurableJobLeaseError("original publication artifact registration changed")
        bodies[name] = service._read_private_artifact("workspace-json:"+item["path"], expected_digest=item["sha256"])
    if bodies["manifest.json"] != bodies["readback.json"]:
        raise DurableJobLeaseError("literal publication manifest/readback differ")
    manifest = json.loads(bodies["manifest.json"])
    tested = manifest.get("publication_test_input")
    if (not isinstance(tested,dict) or tested.get("schema")!="seraph.repo-publication.tested-input.v1"
            or manifest.get("authority_digest")!=proposal.authority_digest
            or manifest.get("diff_sha256")!=hashlib.sha256(bodies["diff.patch"]).hexdigest()
            or terminal.get("tested_cumulative_diff_digest") != manifest.get("diff_sha256")
            or terminal.get("approved_input_patch_digest") != proposal.patch_sha256
            or manifest.get("patch_sha256") != proposal.patch_sha256
            or tested.get("patch_sha256") != proposal.patch_sha256
            or prepared is None or manifest.get("posture_digest") != prepared.get("executor_posture_digest")
            or str(service.sandbox.config.profile) != "repo-python-pytest-publication-v1"
            or not isinstance(tested.get("environment"),dict)
            or tested["environment"].get("available") is not True
            or tested.get("environment_unchanged") is not True
            or tested.get("tested_files") != tested.get("output_files")):
        raise DurableJobLeaseError("actual publication-tested runtime/input required")
    environment = tested["environment"]
    approved_bytes = service._read_private_artifact(terminal["approved_input_patch_artifact_ref"],
        expected_digest=terminal["approved_input_patch_digest"])
    if hashlib.sha256(approved_bytes).hexdigest() != proposal.patch_sha256:
        raise DurableJobLeaseError("literal original approved publication input changed")
    projection = {"repair_job_id":repair_job_id,"repair_task_id":task.task_id,"repair_attempt_id":attempt.attempt_id,
        "root_authority_digest":run.authority_digest,"iteration_authority_digest":proposal.authority_digest,
        "source_checkpoint_digest":_source_digest(original),"source_scope_digest":_source_digest(task_source.model_dump(mode="json")),
        "final_iteration_id":identity,"final_iteration_index":prepared["iteration_index"],
        "final_proposal_id":proposal.proposal_id,"final_proposal_revision":proposal.revision,
        "consumed_approval_id":approval.id,"native_invocation_id":child.run_identity,
        "terminal_certificate_digest":_source_digest(terminal),"publication_artifacts":artifacts,
        "runtime_proof_digest":_source_digest(environment["runtime_proof"]),
        "executor_kind": "local", "executor_posture_digest": prepared["executor_posture_digest"],
        "approved_input_patch_digest": terminal["approved_input_patch_digest"],
        "approved_input_patch_artifact_ref": terminal["approved_input_patch_artifact_ref"],
        "tested_cumulative_diff_digest": terminal["tested_cumulative_diff_digest"],
        "configuration_revision":environment["configuration_revision"]}
    witness = _RepositoryPublicationWitness(service,jobs,tuple(rows),_canonical(projection),_canonical(original),
        _canonical(service.sandbox.config.model_dump(mode="json")))
    _PUBLICATION_WITNESSES[witness]=service
    return witness


async def validate_repository_publication_in_writer(db, witness, *, publication_run=None,
        preview_digest=None, connection_id=None, connection_revision=None, consent_binding=None):
    from src.db.models import WorkflowRunState, GitHubFollowthroughConnection, ApprovalRequest
    from src.workflows.job_runtime import DurableJobLeaseError, _canonical, _as_utc, _utc_now
    if publication_run is None or publication_run.job_kind != "engineering.repo-publication.v1":
        raise DurableJobLeaseError("actual publication writer root required")
    authority = json.loads(publication_run.declared_authority_json)
    repair_binding = authority.get("repair_binding")
    dependencies = json.loads(publication_run.dependencies_json)
    if (not isinstance(repair_binding,dict) or dependencies != [repair_binding.get("repair_job_id")]):
        raise DurableJobLeaseError("original publication repair dependency changed")
    from sqlalchemy import select
    repair = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==dependencies[0]))
    if repair is None:
        raise DurableJobLeaseError("original publication repair missing")
    original = _repository_record(repair,"repository:original:v1")
    if witness is None and original is None:
        from src.workflows.general_task_guard import _history
        if any(item["checkpoint_id"].startswith("repository:") for item in _history(repair)):
            raise DurableJobLeaseError("protected repository Source original is missing")
        if "source_binding" in repair_binding:
            raise DurableJobLeaseError("Source projection on unscoped repair forbidden")
        return
    assert_repository_publication_witness(witness)
    projection = witness.projection()
    if (original is None or projection["repair_job_id"]!=repair.run_identity
            or repair_binding.get("source_binding")!=_source_digest(projection)
            or (publication_run.owner_principal_id,publication_run.operator_session_id)!=(repair.owner_principal_id,repair.operator_session_id)
            or _canonical(witness.service.sandbox.config.model_dump(mode="json"))!=witness.configuration_json):
        raise DurableJobLeaseError("current original publication Source binding changed")
    for model,key,expected in witness.rows:
        row=await db.get(model,key,populate_existing=True)
        if row is None or _canonical(row.model_dump(mode="json"))!=expected:
            raise DurableJobLeaseError("staged original publication Source row changed")
    if preview_digest is not None and authority.get("preview_digest")!=preview_digest:
        raise DurableJobLeaseError("exact publication preview changed")
    consent = consent_binding if consent_binding is not None else authority.get("github_consent")
    connection = await db.get(GitHubFollowthroughConnection,connection_id or authority.get("connection_id"))
    if (not isinstance(consent,dict) or connection is None or connection.mode!="active"
            or connection.id!=authority.get("connection_id") or connection.revision!=authority.get("connection_revision")
            or (connection_revision is not None and connection.revision!=connection_revision)
            or connection.owner_principal_id!=publication_run.owner_principal_id
            or connection.consent_revoked_at or connection.consent_id!=consent.get("consent_id")
            or connection.consent_payload_digest!=consent.get("consent_payload_digest")
            or connection.consent_owner_session_id!=publication_run.operator_session_id
            or _as_utc(connection.consent_expires_at)<=_utc_now()):
        raise DurableJobLeaseError("current exact publication connection consent changed")
    if publication_run.status=="awaiting_approval":
        approval=await db.get(ApprovalRequest,authority.get("approval_id"))
        details=json.loads(approval.details_json) if approval is not None else {}
        if (approval is None or approval.status not in {"pending","approved"}
                or approval.owner_principal_id!=publication_run.owner_principal_id
                or approval.operator_session_id!=publication_run.operator_session_id
                or details.get("preview_digest")!=authority.get("preview_digest")
                or details.get("durable_job_id")!=publication_run.run_identity
                or details.get("durable_authority_digest")!=publication_run.authority_digest
                or _as_utc(approval.expires_at)<=_utc_now()):
            raise DurableJobLeaseError("current Source publication approval required")
