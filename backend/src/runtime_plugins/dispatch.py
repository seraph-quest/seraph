"""Finite native dispatch. A canonical job locator is never invocation authority.

Only an original protected invocation witness, recorded under the job's live
lease, admits a crossing. Each call re-resolves the current job, lease, fence,
deadline, composition owners and authority in one immediate SQLite writer
before any private bytes or effects. Methods without a native owner in this
slice return an explicit typed ``blocked`` result; there is no generic
function/SQL/URL router and no silent success.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from types import MappingProxyType
from collections.abc import Mapping

from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeoutError

from .contracts import METHOD_DOMAINS, blocked, ref, sha, succeeded, validate_request
from .protocol import ProtocolError, closed, integer

WITNESS_PREFIX = "runtime-service-invocation:"
WITNESS_FIELDS = frozenset({
    "schema_version", "invocation_ref", "claim_ref", "checkpoint_id", "origin_method", "native_branch",
    "allowed_child_methods", "binding_digest", "attempt_count", "lease_owner", "fence", "input_digest",
    "authority_digest", "run_fingerprint", "original_deadline_at", "package_digest",
    "host_composition_digest", "host_boot_nonce", "admission_replayed"})
CONTEXT_TAG = "cordis-service.v1"
READ_JOB_KIND = "runtime_service_read_v1"
TASK_JOB_KIND = "runtime_service_task_v1"

SUPPORTED_METHODS = frozenset({
    "authority.resolve", "goals.read", "tasks.admit", "tasks.inspect", "tasks.checkpoint", "tasks.cancel",
    "capabilities.list", "capabilities.describe", "connections.inspect", "memory.retrieve",
    "artifacts.stage", "artifacts.adopt", "artifacts.read", "audit.append"})
# Operator-visible reasons for every method without a native owner here.
BLOCKED_METHODS = MappingProxyType({
    "tasks.settle": "native_settlement_python_owner_only",
    "capabilities.invoke": "native_capability_invoke_not_bound",
    "inference.request": "native_inference_purpose_not_bound",
    "memory.propose": "native_memory_mutation_deferred",
    "memory.applyReviewed": "native_memory_mutation_deferred",
    "memory.forget": "native_memory_mutation_deferred",
    # Cordis owns research planning/execution (#1008); Python originates these
    # calls and never serves them as callbacks.
    "research.buildPlan": "native_research_plan_not_bound",
    "research.executeAccepted": "native_research_execution_not_bound",
    "conversation.accept": "native_turn_producer_not_bound",
    "conversation.append": "native_turn_producer_not_bound",
    "conversation.read": "native_turn_producer_not_bound",
    "conversation.cancel": "native_turn_producer_not_bound",
    "agent-loop.startTurn": "native_turn_producer_not_bound",
    "agent-loop.cancelTurn": "native_turn_producer_not_bound",
    "agent-loop.inspectTurn": "native_turn_producer_not_bound",
    "scheduler.register": "native_scheduler_not_bound",
    "scheduler.disable": "native_scheduler_not_bound",
    "scheduler.dispatchDue": "native_scheduler_not_bound",
    "connections.invokeBoundAdapter": "native_adapter_invoke_not_bound",
    "source-extraction.extract": "native_research_execution_not_bound",
})
assert SUPPORTED_METHODS | set(BLOCKED_METHODS) == set(METHOD_DOMAINS)
assert not SUPPORTED_METHODS & set(BLOCKED_METHODS)


STORAGE_BUSY = "native_owner_storage_busy"


def transient_storage_error(exc: BaseException) -> bool:
    """SQLite write contention or connection-pool exhaustion, nothing else."""
    if isinstance(exc, PoolTimeoutError):
        return True
    message = str(getattr(exc, "orig", exc)).lower()
    return isinstance(exc, OperationalError) and ("database is locked" in message or "database is busy" in message)


class NativeServiceBlocked(ValueError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class OriginalServiceInvocation:
    witness: Mapping
    binding: object
    host_boot_nonce: str

    @property
    def deadline_at(self) -> int:
        return self.witness["original_deadline_at"]


@dataclass(frozen=True)
class CalledServiceInvocation:
    original: OriginalServiceInvocation
    method: str
    composition_epoch: int
    deadline_at: int
    parent_request_id: str
    host_boot_nonce: str


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _aware(value):
    if value is None:
        return None
    if type(value) is str:
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=value.tzinfo or timezone.utc)


def _ms(value) -> int:
    if value is None:
        raise NativeServiceBlocked("native_original_deadline_missing")
    return int(_aware(value).timestamp() * 1000)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def operation_ref(job_id: str, method: str) -> str:
    return "cordis-op:" + hashlib.sha256(f"{job_id}:{method}".encode()).hexdigest()[:48]


def _checked_witness(value) -> dict:
    witness = dict(closed(dict(value) if isinstance(value, Mapping) else value, set(WITNESS_FIELDS)))
    integer(witness["schema_version"], 1, 1)
    integer(witness["attempt_count"], 1)
    integer(witness["fence"], 1)
    integer(witness["original_deadline_at"], 1)
    for key in ("invocation_ref", "lease_owner", "claim_ref", "checkpoint_id"):
        ref(witness[key])
    for key in ("input_digest", "authority_digest", "binding_digest", "package_digest",
                "host_composition_digest", "host_boot_nonce"):
        sha(witness[key])
    if (witness["origin_method"] not in METHOD_DOMAINS
            or witness["checkpoint_id"] != WITNESS_PREFIX + witness["claim_ref"]
            or type(witness["allowed_child_methods"]) is not list
            or any(item not in METHOD_DOMAINS for item in witness["allowed_child_methods"])
            or type(witness["admission_replayed"]) is not bool
            or type(witness["run_fingerprint"]) is not str):
        raise NativeServiceBlocked("native_original_invocation_changed")
    return witness


def _stored_witness(run, checkpoint_id: str) -> dict:
    receipts = json.loads(run.checkpoint_receipts_json or "[]")
    selected = [item for item in receipts if isinstance(item, dict) and item.get("checkpoint_id") == checkpoint_id]
    if len(selected) != 1 or selected[0].get("safe") is not True:
        raise NativeServiceBlocked("native_original_invocation_missing")
    try:
        witness = _checked_witness(selected[0].get("payload"))
    except ProtocolError as exc:
        raise NativeServiceBlocked("native_original_invocation_changed") from exc
    if selected[0].get("state_digest") != _digest(witness):
        raise NativeServiceBlocked("native_original_invocation_changed")
    return witness


# ---------------------------------------------------------------- private journal

def read_context(run) -> dict:
    """Decode only the closed private service context; never a public job dict."""
    try:
        value = json.loads(run.checkpoint_context_json)
        from src.workflows.job_runtime import _digest as job_digest
        if (type(value) is not dict or set(value) != {"schema_version", "context_tag", "candidate", "candidate_digest",
                                                      "claim_ref", "result", "operations"}
                or value["schema_version"] != 1 or value["context_tag"] != CONTEXT_TAG
                or value["candidate_digest"] != run.input_digest or job_digest(value["candidate"]) != run.input_digest
                or type(value["operations"]) is not list):
            raise ValueError("context")
        return value
    except (ValueError, TypeError, KeyError) as exc:
        raise NativeServiceBlocked("native_private_context_changed") from exc


def read_result_key() -> bytes:
    """Per-install MAC key for read-result digests that outlive a forget.

    Derived from the vault master key, which backups never archive, so a
    retained digest cannot be used to confirm a guessed forgotten summary.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    try:
        from src.vault.crypto import vault_key_material
        material = vault_key_material()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise NativeServiceBlocked("native_read_result_key_unavailable") from exc
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"seraph.cordis-read-result.v1").derive(material)


def sealed_read_digest(result: dict, *, key: bytes | None = None) -> str:
    import hmac
    return hmac.new(key or read_result_key(), b"result\0" + _canonical(result).encode("utf-8"), hashlib.sha256).hexdigest()


def sealed_record_digest(text: str, *, key: bytes | None = None) -> str:
    import hmac
    return hmac.new(key or read_result_key(), b"record\0" + text.encode("utf-8"), hashlib.sha256).hexdigest()


def retained_read_result(method: str, result: dict, *, key: bytes | None = None) -> dict:
    """The only form of a read result that may persist in a job row or artifact.

    Memory text is private and forgettable, so each record keeps only its
    reference and a keyed digest of the served text (for the completion
    recheck); capability and connection results are already metadata only.
    """
    if method != "memory.retrieve":
        return result
    key = key or read_result_key()
    return {**result, "value": {"records": [
        {"record_ref": item["record_ref"], "text_mac": sealed_record_digest(item["text"], key=key)}
        for item in result["value"]["records"]]}}


def sealed_read_result(context: dict) -> dict:
    """The read journey's sealed result: its digest and retained projection only.

    A legacy context carrying the full payload fails closed here. No release
    ever admitted read jobs before #1100, so no such row needs a scrub.
    """
    sealed = context["result"]
    if sealed is None:
        raise NativeServiceBlocked("native_artifact_source_unavailable")
    if (type(sealed) is not dict or set(sealed) != {"result_digest", "projection"}
            or type(sealed["result_digest"]) is not str or type(sealed["projection"]) is not dict):
        raise NativeServiceBlocked("native_private_context_changed")
    return sealed


def _run_candidate(run) -> dict:
    """The admitted candidate is private; only its digest is in the public job."""
    candidate = read_context(run)["candidate"]
    if type(candidate) is not dict:
        raise NativeServiceBlocked("native_candidate_changed")
    return candidate


async def _writer(db) -> None:
    """Serialize check and effect: authority, epoch and CAS share one writer."""
    if db.in_transaction():
        await db.rollback()
    if getattr(getattr(db.get_bind(), "dialect", None), "name", "") == "sqlite":
        await db.execute(text("BEGIN IMMEDIATE"))


async def cas_run(db, run, **values) -> int:
    """Lease/fence/goal/composition-fenced durable job CAS within the writer."""
    from src.db.models import WorkflowRunState
    from src.workflows.job_runtime import _append_parent_fence_condition
    now = datetime.now(timezone.utc)
    revision = int(run.revision or 1)
    conditions = [WorkflowRunState.run_identity == run.run_identity, WorkflowRunState.status == run.status,
                  WorkflowRunState.revision == revision, WorkflowRunState.fencing_token == run.fencing_token,
                  WorkflowRunState.lease_owner == run.lease_owner, WorkflowRunState.lease_expires_at > now]
    _append_parent_fence_condition(conditions, run, now=now)
    result = await db.execute(update(WorkflowRunState).execution_options(synchronize_session=False)
                              .where(*conditions)
                              .values(**values, updated_at=now, heartbeat_at=now, revision=revision + 1))
    if result.rowcount != 1:
        raise NativeServiceBlocked("native_original_job_cas_changed")
    return revision + 1


def checkpoint_receipt(checkpoint_id: str, state: dict, fence: int) -> dict:
    """Same receipt shape as ``DurableJobRepository.record_checkpoint``."""
    from src.workflows.job_runtime import _safe_structure
    return {"checkpoint_id": checkpoint_id, "state_digest": _digest(state), "state_keys": sorted(state),
            "safe": True, "recorded_at": datetime.now(timezone.utc).isoformat(), "fencing_token": fence,
            "payload": _safe_structure(state)}


def _append_receipt(raw: str | None, receipt: dict, *, key: str) -> str:
    existing = [item for item in json.loads(raw or "[]") if isinstance(item, dict)]
    if any(item.get(key) == receipt[key] for item in existing):
        raise NativeServiceBlocked("native_receipt_replay_denied")
    return _canonical([*existing, receipt])


# ---------------------------------------------------------------- dispatcher

class NativeServiceDispatcher:
    """Current Python owners remain the sole source of permission and effects."""
    supported_methods = SUPPORTED_METHODS

    def __init__(self, *, jobs=None):
        from src.workflows.job_runtime import durable_job_repository
        self.jobs = jobs or durable_job_repository
        # Operator-visible host status; counts only typed per-call failures.
        self.transient_failures = 0
        self.last_transient_reason: str | None = None

    async def current(self, db, scope: OriginalServiceInvocation, method: str):
        """Re-resolve the original attempt, owners and authority. Returns (run, binding, mode)."""
        from src.workflows.job_runtime import _assert_canonical_goal_fence
        from .ownership import RuntimeCompositionBinding, validate_invocation
        if type(scope) is not OriginalServiceInvocation:
            raise NativeServiceBlocked("native_original_scope_missing")
        run = await self.jobs._fetch(db, scope.witness["invocation_ref"])
        witness = _stored_witness(run, scope.witness["checkpoint_id"])
        if witness != dict(scope.witness):
            raise NativeServiceBlocked("native_original_scope_changed")
        if run.composition_binding_json is None:
            raise NativeServiceBlocked("native_composition_binding_missing")
        binding = RuntimeCompositionBinding.from_json(run.composition_binding_json)
        now = datetime.now(timezone.utc)
        lease_expiry = _aware(run.lease_expires_at)
        if (binding != scope.binding or binding.binding_digest != witness["binding_digest"]
                or not binding.allows(method) or witness["invocation_ref"] != run.run_identity
                or run.status != "running" or run.attempt_count != witness["attempt_count"]
                or run.lease_owner != witness["lease_owner"] or run.fencing_token != witness["fence"]
                or lease_expiry is None or lease_expiry <= now
                or run.input_digest != witness["input_digest"] or run.authority_digest != witness["authority_digest"]
                or run.run_fingerprint != witness["run_fingerprint"]
                or _ms(run.deadline_at) != witness["original_deadline_at"]
                or witness["original_deadline_at"] <= _now_ms()):
            raise NativeServiceBlocked("native_original_attempt_changed")
        await validate_invocation(db, binding)
        mode = await self.current_authority(db, run)
        await _assert_canonical_goal_fence(db, goal_id=run.goal_id, goal_revision=run.goal_revision,
                                           owner_kind=run.owner_kind, owner_principal_id=run.owner_principal_id,
                                           session_id=run.session_id, authority=run.declared_authority_json)
        return run, binding, mode

    async def current_authority(self, db, run) -> str:
        """Server-resolved discriminated authority; plugins supply none of it."""
        authority = json.loads(run.declared_authority_json or "{}")
        if isinstance(authority, dict) and authority.get("authority_type") == "goal_programme_planning_v1":
            # One goal-less planning job per discovery occurrence (#1008); the
            # current programme/grant/goal binding is its only authority.
            from src.guardian.goal_programmes import GoalProgrammeError, goal_programme_service, stage_programme_policy
            from src.work_board.research_parent import DISCOVERY_SERVICE, PLANNING_KIND, planning_authority
            try:
                planning = planning_authority(authority)
            except ValueError as exc:
                raise NativeServiceBlocked("native_programme_authority_invalid") from exc
            if (run.job_kind != PLANNING_KIND or run.owner_kind != "service"
                    or run.owner_principal_id != DISCOVERY_SERVICE or run.service_id != DISCOVERY_SERVICE
                    or run.session_id is not None or run.goal_id is not None
                    or planning.planning_job_id != run.run_identity):
                raise NativeServiceBlocked("native_programme_lineage_changed")
            try:
                await goal_programme_service.validate_current_binding(
                    db=db, binding=planning.programme_binding, policy=stage_programme_policy())
            except GoalProgrammeError as exc:
                raise NativeServiceBlocked("native_programme_authority_changed") from exc
            return "goal-programme"
        if isinstance(authority, dict) and authority.get("authority_type") == "goal_programme_discovery_v1":
            # Issuer Root is provenance only: its idle expiry never disables
            # standing work, while current programme/grant/goal scope,
            # revisions, expiry, revocation and budget are mandatory.
            from src.guardian.goal_programmes import GoalProgrammeError, goal_programme_service, stage_programme_policy
            from src.work_board.research_parent import DISCOVERY_SERVICE, discovery_authority
            try:
                native = discovery_authority(authority)
            except ValueError as exc:
                raise NativeServiceBlocked("native_programme_authority_invalid") from exc
            programme_binding = native.programme_binding
            if (run.owner_kind != "service" or run.owner_principal_id != DISCOVERY_SERVICE
                    or run.service_id != DISCOVERY_SERVICE or run.session_id is not None
                    or run.goal_id != programme_binding.goal_id
                    or run.goal_revision != programme_binding.goal_revision
                    or native.original_job_id != run.run_identity):
                raise NativeServiceBlocked("native_programme_lineage_changed")
            try:
                await goal_programme_service.validate_current_binding(
                    db=db, binding=programme_binding, policy=stage_programme_policy())
            except GoalProgrammeError as exc:
                raise NativeServiceBlocked("native_programme_authority_changed") from exc
            return "goal-programme"
        if run.owner_kind != "user" or not isinstance(authority, dict):
            raise NativeServiceBlocked("native_authority_mode_unsupported")
        from src.auth.service import AuthFailure, authenticate_principal
        from src.db.models import OperatorSession
        now = datetime.now(timezone.utc)
        root = await db.scalar(select(OperatorSession).where(
            OperatorSession.id == run.operator_session_id,
            OperatorSession.principal_id == run.owner_principal_id,
            OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
            OperatorSession.is_bearer_tombstone.is_(False),
            OperatorSession.idle_expires_at > now, OperatorSession.absolute_expires_at > now)
            .execution_options(populate_existing=True))
        if root is None or run.operator_session_id != run.session_id:
            raise NativeServiceBlocked("native_original_root_inactive")
        try:
            current = await authenticate_principal(run.owner_principal_id, db=db)
        except AuthFailure as exc:
            raise NativeServiceBlocked("native_original_root_inactive") from exc
        if current.session_id != run.operator_session_id or authority.get("grants") != sorted(
                str(getattr(grant, "value", grant)) for grant in current.principal.grants):
            raise NativeServiceBlocked("native_original_policy_changed")
        return "operator-root"

    async def dispatch(self, frame, *, called_scope):
        from src.auth.service import AuthFailure
        from src.workflows.job_runtime import DurableJobError
        from .ownership import CompositionBindingError
        method = frame["method"]
        payload = validate_request(method, frame["payload"])
        if type(called_scope) is not CalledServiceInvocation:
            return blocked("native_original_scope_missing")
        scope = called_scope.original
        try:
            # A callback is correlated to its pending original call by the
            # parent's exact identity; its own method is gated by the frozen
            # reviewed closure below, never by plugin choice.
            if (frame["invocation_ref"] != scope.witness["invocation_ref"]
                    or not scope.binding.allows(called_scope.method)
                    or called_scope.composition_epoch != scope.binding.epoch_for(called_scope.method)
                    or frame["composition_epoch"] != called_scope.composition_epoch
                    or frame["boot_nonce"] != scope.host_boot_nonce
                    or frame["boot_nonce"] != called_scope.host_boot_nonce
                    or frame["package_digest"] != scope.witness["package_digest"]
                    or frame["composition_digest"] != scope.witness["host_composition_digest"]
                    or frame["deadline_at"] != called_scope.deadline_at
                    or frame["deadline_at"] > scope.deadline_at):
                raise NativeServiceBlocked("native_original_frame_changed")
            if not scope.binding.allows(method):
                raise NativeServiceBlocked("native_method_not_in_closure")
            if method in BLOCKED_METHODS:
                # Typed and operator-visible; no authority or private bytes used.
                return blocked(BLOCKED_METHODS[method])
            if scope.binding.native_branch in {"programme_planning", "programme_discovery"}:
                # Guardian research callbacks (#1008) have their own Python owner.
                from .research_dispatch import dispatch_research
                return await dispatch_research(self, frame, payload, scope)
            if method in {"artifacts.stage", "artifacts.adopt", "artifacts.read", "audit.append"}:
                from .service_artifacts import dispatch_artifact_operation
                return await dispatch_artifact_operation(self, frame, payload, scope)
            if method == "tasks.cancel":
                return await self._cancel(payload, scope)
            async with self.jobs._session() as db:
                await _writer(db)
                run, binding, mode = await self.current(db, scope, method)
                result = await self._native(db, run, binding, mode, method, payload, scope)
                await db.commit()
                return result
        except NativeServiceBlocked as exc:
            return blocked(exc.reason_code)
        except CompositionBindingError as exc:
            return blocked(exc.reason_code)
        except (AuthFailure, DurableJobError):
            return blocked("native_original_job_authority_changed")
        except (ValueError, PermissionError, KeyError, TypeError):
            return blocked("native_original_binding_unavailable")
        except (OperationalError, PoolTimeoutError) as exc:
            # Transient SQLite contention is a per-call failure: every owner
            # writer above runs in one transaction that rolled back, so no
            # partial effect exists. Other storage faults still fence the boot.
            if not transient_storage_error(exc):
                raise
            self.transient_failures += 1
            self.last_transient_reason = STORAGE_BUSY
            return blocked(STORAGE_BUSY)

    async def _native(self, db, run, binding, mode, method, payload, scope):
        if method == "authority.resolve":
            return succeeded(method, {"authority_ref": f"{mode}:{run.authority_digest[:32]}",
                                      "revision": int(run.revision or 1), "mode": mode})
        if method == "goals.read":
            from src.db.models import Goal
            goal = await db.get(Goal, run.goal_id, populate_existing=True) if run.goal_id else None
            if goal is None:
                raise NativeServiceBlocked("native_goal_not_bound")
            description = goal.description or ""
            if len(goal.title.encode()) > 200 or len(description.encode()) > 8192:
                raise NativeServiceBlocked("native_goal_projection_unsupported")
            return succeeded(method, {"goal_ref": goal.id, "revision": int(goal.revision or 1),
                                      "title": goal.title, "description": description})
        if method in {"capabilities.list", "capabilities.describe", "connections.inspect", "memory.retrieve"}:
            from .read_admission import native_read_projection
            if run.job_kind != READ_JOB_KIND or binding.origin_method != method:
                raise NativeServiceBlocked("native_read_candidate_not_bound")
            candidate = _run_candidate(run)
            from .read_admission import wire_inputs
            if payload != wire_inputs(candidate, run.run_identity):
                raise NativeServiceBlocked("native_read_original_inputs_changed")
            context = read_context(run)
            if context["result"] is not None:
                raise NativeServiceBlocked("native_read_replay_denied")
            result = succeeded(method, await native_read_projection(db, run, candidate))
            # Only a keyed digest and a non-private projection rest in the job
            # row (#1100); the full result exists only in this call's response.
            key = read_result_key()
            sealed = sealed_read_digest(result, key=key)
            context["result"] = {"result_digest": sealed, "projection": retained_read_result(method, result, key=key)}
            receipt = checkpoint_receipt("cordis-read:result", {"method": method, "result_digest": sealed,
                                                                "no_learning": True}, run.fencing_token)
            await cas_run(db, run, checkpoint_context_json=_canonical(context),
                          checkpoint_receipts_json=_append_receipt(run.checkpoint_receipts_json, receipt,
                                                                   key="checkpoint_id"))
            return result
        if run.job_kind != TASK_JOB_KIND:
            raise NativeServiceBlocked("native_task_candidate_not_bound")
        candidate = _run_candidate(run)
        if method == "tasks.admit":
            if payload["request_ref"] != run.run_identity:
                raise NativeServiceBlocked("native_admission_reference_not_bound")
            return succeeded(method, {"job_ref": run.run_identity, "revision": int(run.revision or 1),
                                      "state": run.status, "replayed": scope.witness["admission_replayed"]})
        if method == "tasks.inspect":
            if payload["job_ref"] != run.run_identity:
                raise NativeServiceBlocked("native_job_reference_not_bound")
            artifacts = [item["artifact_id"] for item in json.loads(run.artifact_receipts_json or "[]")
                         if isinstance(item, dict) and item.get("artifact_id")]
            if len(artifacts) > 16:
                raise NativeServiceBlocked("native_artifact_inventory_exceeded")
            return succeeded(method, {"job_ref": run.run_identity, "revision": int(run.revision or 1),
                                      "state": run.status, "artifact_refs": artifacts})
        if method == "tasks.checkpoint":
            checkpoint_ref = payload["checkpoint_ref"]
            if checkpoint_ref not in candidate.get("checkpoint_refs", []):
                raise NativeServiceBlocked("native_checkpoint_reference_not_bound")
            receipt_ref = "cordis-task:" + checkpoint_ref
            if len(receipt_ref) > 128:
                raise NativeServiceBlocked("native_checkpoint_reference_unsupported")
            # The receipt records the job revision its own CAS commits, so the
            # owner can verify a child's answer against the durable row.
            revision = int(run.revision or 1) + 1
            receipt = checkpoint_receipt(receipt_ref, {"checkpoint_ref": checkpoint_ref, "revision": revision,
                                                       "no_learning": True}, run.fencing_token)
            if await cas_run(db, run, checkpoint_receipts_json=_append_receipt(
                    run.checkpoint_receipts_json, receipt, key="checkpoint_id")) != revision:
                raise NativeServiceBlocked("native_original_job_cas_changed")
            return succeeded(method, {"receipt_ref": receipt_ref, "revision": revision})
        raise NativeServiceBlocked("native_method_candidate_not_bound")

    async def _cancel(self, payload, scope):
        """Cancellation uses the canonical transition owner with a same-writer recheck."""
        async with self.jobs._session() as db:
            await _writer(db)
            run, _, _ = await self.current(db, scope, "tasks.cancel")
            if run.job_kind != TASK_JOB_KIND or payload["job_ref"] != run.run_identity:
                raise NativeServiceBlocked("native_job_reference_not_bound")
            if payload["expected_revision"] != int(run.revision or 1):
                raise NativeServiceBlocked("native_job_revision_changed")
            await db.rollback()

        async def recheck(db, _run):
            await self.current(db, scope, "tasks.cancel")

        job = await self.jobs.cancel_job(scope.witness["invocation_ref"], owner=scope.witness["lease_owner"],
                                         fencing_token=scope.witness["fence"],
                                         expected_revision=payload["expected_revision"],
                                         reason="cordis_service_cancelled", cancellation_authority_check=recheck)
        return succeeded("tasks.cancel", {"job_ref": job["job_id"], "revision": int(job["revision"]),
                                          "state": job["status"]})
