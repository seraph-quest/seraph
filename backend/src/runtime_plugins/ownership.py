"""Canonical composition ownership. Availability never supplies invocation authority.

One ``RuntimeCompositionState`` row exists per fixed runtime domain. A durable
job bound to Cordis services carries an immutable dependency vector of the
exact (owner, epoch, digest) rows that were ready at admission. Every later
admission, effect, checkpoint, private read and adoption rechecks that vector
against the current rows; the job CAS repeats it as an SQL predicate. A
cutover is a fenced compare-and-set that only ever increases the epoch, so a
rollback is another higher epoch and never revives an older binding.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import json
import re

from sqlalchemy import select, text, update
from src.db.models import AuditEvent, RuntimeCompositionState, WorkflowRunState

from .contracts import METHOD_DOMAINS

DOMAINS = tuple(f"seraph.{name}.v1" for name in (
    "authority", "goals", "tasks", "capabilities", "inference", "memory",
    "artifacts", "audit", "research", "conversation", "scheduler",
    "connections", "agent-loop", "source-extraction"))
STATES = frozenset({"ready", "draining", "blocked"})
OWNER_KINDS = frozenset({"legacy", "cordis"})
MANIFEST_VERSION = "runtime-service-methods.v1"
RECOVERY_EVENT = "runtime_composition_recovery"
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_REF = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_MAX_EPOCH = 2**63 - 1
_ACTIVE_STATUSES = ("running", "awaiting_approval", "paused")
# Not yet started: blocked visibly by an epoch change, never left stranded.
_UNSTARTED_STATUSES = ("accepted", "queued")


class CompositionBindingError(ValueError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def legacy_composition_digest(runtime_domain: str) -> str:
    """Stable identity of the existing in-process Python owner of one domain."""
    if runtime_domain not in DOMAINS:
        raise CompositionBindingError("composition_domain_unknown")
    return hashlib.sha256(_canonical({"schema": "seraph-legacy-owner.v1",
                                      "runtime_domain": runtime_domain}).encode()).hexdigest()


@dataclass(frozen=True)
class CompositionDependency:
    runtime_domain: str
    owner_kind: str
    epoch: int
    composition_digest: str

    def __post_init__(self):
        if (self.runtime_domain not in DOMAINS or self.owner_kind not in OWNER_KINDS
                or type(self.epoch) is not int or not 1 <= self.epoch <= _MAX_EPOCH
                or type(self.composition_digest) is not str or not _SHA.fullmatch(self.composition_digest)):
            raise CompositionBindingError("composition_binding_invalid")

    def payload(self):
        return dict(runtime_domain=self.runtime_domain, owner_kind=self.owner_kind,
                    epoch=self.epoch, composition_digest=self.composition_digest)


# Finite branch closures. A branch names the methods one original invocation
# may forward; it never grants the authority those methods check.
NATIVE_BRANCHES = frozenset({"base", "artifact", "workflow", "programme_planning", "programme_discovery"})
READ_ORIGINS = frozenset({"capabilities.list", "capabilities.describe", "connections.inspect", "memory.retrieve"})
ARTIFACT_CHILDREN = ("artifacts.stage", "artifacts.adopt", "artifacts.read", "audit.append")
WORKFLOW_CHILDREN = ("authority.resolve", "goals.read", "tasks.inspect", "tasks.checkpoint", "tasks.cancel")
# #1008 guardian research: the planner reads its staged request and stages the
# plan; the consumer reads staged sources, stages snapshots and reports the job.
PLANNING_CHILDREN = ("artifacts.read", "artifacts.stage")
DISCOVERY_CHILDREN = ("artifacts.read", "artifacts.stage", "tasks.inspect", "tasks.checkpoint")


def method_closure(origin_method: str, native_branch: str) -> tuple[str, ...]:
    if origin_method not in METHOD_DOMAINS:
        raise CompositionBindingError("composition_method_unsupported")
    if native_branch == "base":
        return (origin_method,)
    if native_branch == "artifact" and origin_method in READ_ORIGINS:
        return tuple(sorted({origin_method, *ARTIFACT_CHILDREN}))
    if native_branch == "workflow" and origin_method == "tasks.admit":
        return tuple(sorted({origin_method, *WORKFLOW_CHILDREN}))
    if native_branch == "programme_planning" and origin_method == "research.buildPlan":
        return tuple(sorted({origin_method, *PLANNING_CHILDREN}))
    if native_branch == "programme_discovery" and origin_method == "research.executeAccepted":
        return tuple(sorted({origin_method, *DISCOVERY_CHILDREN}))
    raise CompositionBindingError("composition_branch_unsupported")


def method_dependencies(method: str, *, goal_bound: bool = False) -> tuple[str, ...]:
    """Mandatory owners of one method; a missing owner denies the call."""
    domain = METHOD_DOMAINS.get(method)
    if domain is None or type(goal_bound) is not bool:
        raise CompositionBindingError("composition_method_unsupported")
    names = {"seraph.authority.v1", "seraph.tasks.v1", domain}
    if goal_bound or method == "goals.read":
        names.add("seraph.goals.v1")
    if method in {"artifacts.adopt", "audit.append"}:
        names.add("seraph.artifacts.v1")
    if method in {"memory.applyReviewed", "memory.forget"}:
        names.add("seraph.audit.v1")
    if method == "research.executeAccepted":
        # The consumer extracts through the composed source provider.
        names.add("seraph.source-extraction.v1")
    return tuple(sorted(names))


def closure_dependencies(origin_method: str, native_branch: str, *, goal_bound: bool = False) -> tuple[str, ...]:
    domains: set[str] = set()
    for method in method_closure(origin_method, native_branch):
        domains.update(method_dependencies(method, goal_bound=goal_bound))
    return tuple(sorted(domains))


def method_manifest_digest(origin_method: str, native_branch: str) -> str:
    methods = method_closure(origin_method, native_branch)
    source = {"schema_version": MANIFEST_VERSION, "origin_method": origin_method,
              "native_branch": native_branch, "allowed_child_methods": list(methods),
              "child_dependencies": [[method, list(method_dependencies(method))] for method in methods]}
    return hashlib.sha256(_canonical(source).encode()).hexdigest()


@dataclass(frozen=True)
class RuntimeCompositionBinding:
    runtime_domain: str
    origin_method: str
    native_branch: str
    allowed_child_methods: tuple[str, ...]
    dependency_vector: tuple[CompositionDependency, ...]
    host_package_digest: str | None = None
    host_composition_digest: str | None = None
    goal_bound: bool = False
    schema_version: int = 1

    def __post_init__(self):
        keys = tuple(item.runtime_domain for item in self.dependency_vector)
        if (type(self.schema_version) is not int or self.schema_version != 1
                or type(self.goal_bound) is not bool
                or METHOD_DOMAINS.get(self.origin_method) != self.runtime_domain
                or self.native_branch not in NATIVE_BRANCHES
                or self.allowed_child_methods != method_closure(self.origin_method, self.native_branch)
                or keys != tuple(sorted(set(keys)))
                or keys != closure_dependencies(self.origin_method, self.native_branch, goal_bound=self.goal_bound)):
            raise CompositionBindingError("composition_binding_invalid")
        if (self.host_package_digest is None) != (self.host_composition_digest is None) or any(
                value is not None and (type(value) is not str or _SHA.fullmatch(value) is None)
                for value in (self.host_package_digest, self.host_composition_digest)):
            raise CompositionBindingError("composition_host_binding_invalid")

    def allows(self, method: str) -> bool:
        return method in self.allowed_child_methods

    def epoch_for(self, method: str) -> int:
        if not self.allows(method):
            raise CompositionBindingError("composition_child_method_not_bound")
        domain = METHOD_DOMAINS[method]
        return next(item.epoch for item in self.dependency_vector if item.runtime_domain == domain)

    def payload(self):
        return dict(schema_version=1, runtime_domain=self.runtime_domain, origin_method=self.origin_method,
                    native_branch=self.native_branch, allowed_child_methods=list(self.allowed_child_methods),
                    method_manifest_version=MANIFEST_VERSION,
                    method_manifest_digest=method_manifest_digest(self.origin_method, self.native_branch),
                    host_package_digest=self.host_package_digest,
                    host_composition_digest=self.host_composition_digest, goal_bound=self.goal_bound,
                    dependency_vector=[item.payload() for item in self.dependency_vector])

    @property
    def binding_digest(self) -> str:
        return hashlib.sha256(_canonical(self.payload()).encode()).hexdigest()

    def to_json(self) -> str:
        return _canonical({**self.payload(), "binding_digest": self.binding_digest})

    @classmethod
    def from_json(cls, raw):
        def no_duplicates(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate key")
                value[key] = item
            return value
        try:
            if type(raw) is not str or len(raw.encode()) > 8192:
                raise ValueError("size")
            value = json.loads(raw, object_pairs_hook=no_duplicates)
            if type(value) is not dict or set(value) != {
                    "schema_version", "runtime_domain", "origin_method", "native_branch", "allowed_child_methods",
                    "dependency_vector", "binding_digest", "method_manifest_version", "method_manifest_digest",
                    "host_package_digest", "host_composition_digest", "goal_bound"}:
                raise ValueError("fields")
            if type(value["dependency_vector"]) is not list or len(value["dependency_vector"]) > len(DOMAINS):
                raise ValueError("vector")
            for item in value["dependency_vector"]:
                if type(item) is not dict or set(item) != {"runtime_domain", "owner_kind", "epoch", "composition_digest"}:
                    raise ValueError("entry")
            if type(value["allowed_child_methods"]) is not list or len(value["allowed_child_methods"]) > len(METHOD_DOMAINS):
                raise ValueError("methods")
            result = cls(value["runtime_domain"], value["origin_method"], value["native_branch"],
                         tuple(value["allowed_child_methods"]),
                         tuple(CompositionDependency(**item) for item in value["dependency_vector"]),
                         value["host_package_digest"], value["host_composition_digest"],
                         value["goal_bound"], value["schema_version"])
            if (value["method_manifest_version"] != MANIFEST_VERSION
                    or value["method_manifest_digest"] != method_manifest_digest(result.origin_method, result.native_branch)
                    or value["binding_digest"] != result.binding_digest):
                raise ValueError("digest")
            return result
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise CompositionBindingError("composition_binding_invalid") from exc


def _checked_row(row) -> CompositionDependency:
    dependency = CompositionDependency(row.runtime_domain, row.owner_kind, row.epoch, row.composition_digest)
    if row.state not in STATES or (row.recovery_receipt_ref is not None
                                   and (type(row.recovery_receipt_ref) is not str
                                        or not _REF.fullmatch(row.recovery_receipt_ref))):
        raise CompositionBindingError("composition_inventory_invalid")
    return dependency


async def inventory(db) -> dict[str, RuntimeCompositionState]:
    """Current rows; any malformed row fails closed for the whole inventory."""
    rows = list((await db.execute(select(RuntimeCompositionState)
                                  .execution_options(populate_existing=True))).scalars())
    by_domain = {}
    for row in rows:
        if row.runtime_domain not in DOMAINS or row.runtime_domain in by_domain:
            raise CompositionBindingError("composition_inventory_invalid")
        _checked_row(row)
        by_domain[row.runtime_domain] = row
    return by_domain


async def bind_invocation(db, *, method: str, native_branch: str = "base", goal_bound: bool = False,
                          reviewed_composition=None) -> RuntimeCompositionBinding:
    """Freeze the exact ready owners this invocation may depend on."""
    methods = method_closure(method, native_branch)
    domains = closure_dependencies(method, native_branch, goal_bound=goal_bound)
    rows = await inventory(db)
    if any(domain not in rows for domain in domains):
        raise CompositionBindingError("composition_dependency_missing")
    if any(rows[domain].state != "ready" for domain in domains):
        raise CompositionBindingError("composition_dependency_unavailable")
    host_package_digest = host_composition_digest = None
    if reviewed_composition is not None:
        from .composition import ReviewedComposition
        if type(reviewed_composition) is not ReviewedComposition:
            raise CompositionBindingError("composition_reviewed_host_required")
        host_package_digest = reviewed_composition.package_digest
        host_composition_digest = reviewed_composition.composition_digest
    return RuntimeCompositionBinding(METHOD_DOMAINS[method], method, native_branch, methods,
        tuple(_checked_row(rows[domain]) for domain in domains),
        host_package_digest, host_composition_digest, goal_bound)


async def validate_invocation(db, binding: RuntimeCompositionBinding) -> None:
    """Every bound owner must still be the identical ready owner/epoch/digest."""
    if type(binding) is not RuntimeCompositionBinding:
        raise CompositionBindingError("composition_binding_required")
    rows = await inventory(db)
    for dependency in binding.dependency_vector:
        row = rows.get(dependency.runtime_domain)
        if row is None:
            raise CompositionBindingError("composition_dependency_missing")
        if row.state != "ready" or _checked_row(row) != dependency:
            raise CompositionBindingError("composition_dependency_changed")


def composition_fence_predicates(raw_binding: str | None):
    """SQL predicates repeating the bound vector inside a durable job CAS."""
    from sqlalchemy import false
    from sqlalchemy.orm import aliased
    if raw_binding is None:
        return []
    try:
        binding = RuntimeCompositionBinding.from_json(raw_binding)
    except CompositionBindingError:
        return [false()]
    predicates = []
    for dependency in binding.dependency_vector:
        state = aliased(RuntimeCompositionState)
        predicates.append(select(state.runtime_domain).where(
            state.runtime_domain == dependency.runtime_domain,
            state.owner_kind == dependency.owner_kind,
            state.epoch == dependency.epoch,
            state.composition_digest == dependency.composition_digest,
            state.state == "ready").exists())
    return predicates


def seed_legacy_inventory_sync(connection) -> int:
    """Seed the fixed inventory only when the table is genuinely empty.

    A partial inventory is never repaired here: missing mandatory owners keep
    denying service invocations until an explicit fenced transition repairs it.
    """
    existing = connection.execute(text("SELECT COUNT(*) FROM runtime_composition_states")).scalar_one()
    if existing:
        return 0
    for domain in DOMAINS:
        connection.execute(text(
            "INSERT INTO runtime_composition_states "
            "(runtime_domain, owner_kind, epoch, composition_digest, state, recovery_receipt_ref) "
            "VALUES (:domain, 'legacy', 1, :digest, 'ready', NULL)"),
            {"domain": domain, "digest": legacy_composition_digest(domain)})
    return len(DOMAINS)


def _boot_proved(runtime_domain: str, owner_kind: str, composition_digest: str, boot_host) -> bool:
    if owner_kind == "legacy":
        return composition_digest == legacy_composition_digest(runtime_domain)
    from .bridge import CordisHost
    reviewed = getattr(boot_host, "reviewed", None)
    # The provider must be composed and running in this boot, not merely listed.
    running = [plugin for plugin in getattr(boot_host, "_plugins", ())
               if plugin.get("id") == runtime_domain and plugin.get("state") == "ready"
               and plugin.get("digest") == composition_digest]
    return (type(boot_host) is CordisHost and boot_host.admitting and reviewed is not None
            and (reviewed.plugin_digests or {}).get(runtime_domain) == composition_digest and len(running) == 1)


@asynccontextmanager
async def composition_writer(db):
    """Fresh immediate writer under the existing workspace maintenance fence."""
    from src.workspace.lifecycle import _LIFECYCLE_FENCE_DEPTH
    if _LIFECYCLE_FENCE_DEPTH.get() <= 0:
        raise CompositionBindingError("composition_maintenance_fence_required")
    if db.in_transaction():
        raise CompositionBindingError("composition_writer_not_fresh")
    dialect = getattr(getattr(db.get_bind(), "dialect", None), "name", "")
    if dialect == "sqlite":
        await db.execute(text("BEGIN IMMEDIATE"))
    yield db


def _phase(prior_epoch: int, epoch: int, state: str) -> str:
    if state == "ready":
        return "boot_verified"
    if epoch > prior_epoch:
        return "awaiting_boot"
    return "maintenance_quiescent"


async def transition_owner(db, *, runtime_domain: str, expected: CompositionDependency, expected_state: str,
                           owner_kind: str, epoch: int, composition_digest: str, state: str,
                           boot_host=None) -> str:
    """Exact fenced CAS of one domain owner; returns the recovery receipt id.

    Allowed moves: ready -> draining (same epoch, stop new admission);
    draining|blocked -> blocked at a strictly higher epoch (quiesced; owner may
    change only here; unstarted jobs bound to the prior owner become visibly
    blocked); blocked -> ready at the same epoch only with a boot proof: the
    fixed legacy digest, or for ``cordis`` the currently admitting host whose
    verified build reports exactly this domain's plugin digest. Epochs never
    decrease and an owner change always takes a new epoch, so racing cutovers
    from one expected row produce exactly one winner.
    """
    from src.workspace.lifecycle import _LIFECYCLE_FENCE_DEPTH
    if _LIFECYCLE_FENCE_DEPTH.get() <= 0 or not db.in_transaction():
        raise CompositionBindingError("composition_stopped_writer_required")
    incoming = CompositionDependency(runtime_domain, owner_kind, epoch, composition_digest)
    if (type(expected) is not CompositionDependency or expected.runtime_domain != runtime_domain
            or expected_state not in STATES or state not in STATES):
        raise CompositionBindingError("composition_expected_binding_required")
    changed_owner = (owner_kind, composition_digest) != (expected.owner_kind, expected.composition_digest)
    if epoch < expected.epoch or (changed_owner and epoch == expected.epoch):
        raise CompositionBindingError("composition_epoch_conflict")
    if state == "draining":
        allowed = expected_state == "ready" and epoch == expected.epoch
    elif state == "blocked":
        allowed = epoch > expected.epoch and expected_state in {"draining", "blocked"}
    else:
        allowed = (expected_state == "blocked" and epoch == expected.epoch
                   and _boot_proved(runtime_domain, owner_kind, composition_digest, boot_host))
    if not allowed:
        raise CompositionBindingError("composition_transition_not_allowed")
    rows = await inventory(db)
    prior = rows.get(runtime_domain)
    if prior is None or _checked_row(prior) != expected or prior.state != expected_state:
        raise CompositionBindingError("composition_owner_changed")
    stranded: list[str] = []
    if state == "blocked":
        # Quiesce: an epoch change never strands a live leased attempt that
        # was admitted against the prior owner.
        active = (await db.execute(select(WorkflowRunState.composition_binding_json).where(
            WorkflowRunState.composition_binding_json.is_not(None),
            WorkflowRunState.status.in_(_ACTIVE_STATUSES)))).scalars().all()
        for raw in active:
            try:
                bound = RuntimeCompositionBinding.from_json(raw)
            except CompositionBindingError:
                continue
            if expected in bound.dependency_vector:
                raise CompositionBindingError("composition_not_quiescent")
        unstarted = (await db.execute(select(WorkflowRunState.run_identity, WorkflowRunState.composition_binding_json)
                                      .where(WorkflowRunState.composition_binding_json.is_not(None),
                                             WorkflowRunState.status.in_(_UNSTARTED_STATUSES)))).all()
        for run_identity, raw in unstarted:
            try:
                bound = RuntimeCompositionBinding.from_json(raw)
            except CompositionBindingError:
                stranded.append(run_identity)
                continue
            if expected in bound.dependency_vector:
                stranded.append(run_identity)
        if stranded:
            from datetime import datetime, timezone
            await db.execute(update(WorkflowRunState).where(
                WorkflowRunState.run_identity.in_(stranded),
                WorkflowRunState.status.in_(_UNSTARTED_STATUSES)).values(
                    status="blocked", failure_reason="composition_owner_changed",
                    revision=WorkflowRunState.revision + 1, updated_at=datetime.now(timezone.utc))
                .execution_options(synchronize_session=False))
    proof = dict(schema_version=1, runtime_domain=runtime_domain, prior=expected.payload(),
                 prior_state=expected_state, target=incoming.payload(), state=state,
                 phase=_phase(expected.epoch, epoch, state),
                 prior_recovery_receipt_ref=prior.recovery_receipt_ref, blocked_unstarted_jobs=sorted(stranded))
    receipt = AuditEvent(session_id=None, actor="runtime", event_type=RECOVERY_EVENT,
                         summary="Runtime composition owner transition", details_json=_canonical(proof))
    db.add(receipt)
    await db.flush()
    prior_receipt = (RuntimeCompositionState.recovery_receipt_ref.is_(None) if prior.recovery_receipt_ref is None
                     else RuntimeCompositionState.recovery_receipt_ref == prior.recovery_receipt_ref)
    result = await db.execute(update(RuntimeCompositionState).where(
        RuntimeCompositionState.runtime_domain == runtime_domain,
        RuntimeCompositionState.owner_kind == expected.owner_kind,
        RuntimeCompositionState.epoch == expected.epoch,
        RuntimeCompositionState.composition_digest == expected.composition_digest,
        RuntimeCompositionState.state == expected_state, prior_receipt).values(
            **incoming.payload(), state=state, recovery_receipt_ref=receipt.id)
        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise CompositionBindingError("composition_owner_changed")
    return receipt.id
