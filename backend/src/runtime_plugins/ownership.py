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
from types import MappingProxyType

from sqlalchemy import select, text, update
from src.db.models import AuditEvent, RuntimeCompositionState, WorkflowRunState

from .contracts import METHOD_DOMAINS

# Every domain an inventory held before #1024, frozen as a literal: the
# Python-hosted state domain is introduced only onto exactly this set.
PRE_STATE_DOMAINS = (
    "seraph.authority.v1", "seraph.goals.v1", "seraph.tasks.v1", "seraph.capabilities.v1", "seraph.inference.v1",
    "seraph.memory.v1", "seraph.artifacts.v1", "seraph.audit.v1", "seraph.research.v1", "seraph.conversation.v1",
    "seraph.scheduler.v1", "seraph.connections.v1", "seraph.agent-presets.v1", "seraph.agent-loop.v1",
    "seraph.guardian.v1", "seraph.delegation.v1", "seraph.source-extraction.v1", "seraph.board-workers.v1")
STATE_DOMAIN = "seraph.state.v1"
# The WorkBoard dispatcher lifecycle (#1025): a domain of its own so that a
# dispatch owner change never bumps ``seraph.tasks.v1`` (the job ledger every
# method depends on). Introduced only onto exactly ``PRE_DISPATCH_DOMAINS``.
DISPATCH_DOMAIN = "seraph.dispatch.v1"
SCHEDULER_DOMAIN = "seraph.scheduler.v1"
PRE_DISPATCH_DOMAINS = (*PRE_STATE_DOMAINS, STATE_DOMAIN)
DOMAINS = (*PRE_DISPATCH_DOMAINS, DISPATCH_DOMAIN)
# Python always hosts these owners (#1024 operator decision): their rows are
# always the legacy owner with the fixed legacy digest, never Cordis.
PYTHON_HOSTED_DOMAINS = frozenset({STATE_DOMAIN, "seraph.authority.v1", "seraph.audit.v1"})
# Domains added after an install's inventory was first seeded, with the issue
# that introduced them. Boot inserts each missing one once as its legacy owner
# (``introduce_domains_sync``); a missing original domain is never repaired.
DOMAIN_INTRODUCTIONS = {"seraph.agent-presets.v1": "1020", "seraph.guardian.v1": "1020",
                        "seraph.delegation.v1": "1020", "seraph.board-workers.v1": "1020"}
PRESETS_DOMAIN = "seraph.agent-presets.v1"
STATES = frozenset({"ready", "draining", "blocked"})
OWNER_KINDS = frozenset({"legacy", "cordis"})
# Binding schemas 1 and 2 (#1019, #1020) use the v1 method manifest; schema 3
# (#1024) adds the state owner to every method under the v2 manifest.
MANIFEST_VERSION_V1 = "runtime-service-methods.v1"
MANIFEST_VERSION = "runtime-service-methods.v2"
RECOVERY_EVENT = "runtime_composition_recovery"
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_REF = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_MAX_EPOCH = 2**63 - 1
_ACTIVE_STATUSES = ("running", "awaiting_approval", "paused")
# Not yet started: blocked visibly by an epoch change, never left stranded.
_UNSTARTED_STATUSES = ("accepted", "queued")
# Unresolved effects: an undecodable binding here also refuses quiescence (#1024).
_LIABILITY_STATUSES = ("unknown_external_effect", "cost_liability")


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
NATIVE_BRANCHES = frozenset({"base", "artifact", "workflow", "programme_planning", "programme_discovery", "governed",
                             "agent_loop", "guardian", "delegation", "board_workers"})
READ_ORIGINS = frozenset({"capabilities.list", "capabilities.describe", "connections.inspect", "memory.retrieve"})
ARTIFACT_CHILDREN = ("artifacts.stage", "artifacts.adopt", "artifacts.read", "audit.append")
WORKFLOW_CHILDREN = ("authority.resolve", "goals.read", "tasks.inspect", "tasks.checkpoint", "tasks.cancel")
# #1008 guardian research: the planner reads its staged request and stages the
# plan; the consumer reads staged sources, stages snapshots and reports the job.
PLANNING_CHILDREN = ("artifacts.read", "artifacts.stage")
DISCOVERY_CHILDREN = ("artifacts.read", "artifacts.stage", "tasks.inspect", "tasks.checkpoint")
# #1018 governed capability/inference services: one Python-originated call,
# executed by the existing governed owner; the provider forwards only itself.
GOVERNED_ORIGINS = frozenset({"capabilities.invoke", "inference.request", "connections.invokeBoundAdapter"})
# #1019 agent-loop step: the sequencer reads its sealed step projection and
# stages finite decisions; Python executes every effect between steps.
AGENT_LOOP_CHILDREN = ("artifacts.read", "tasks.checkpoint", "tasks.inspect")


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
    if native_branch == "governed" and origin_method in GOVERNED_ORIGINS:
        return (origin_method,)
    if native_branch == "agent_loop" and origin_method == "agent-loop.startTurn":
        return tuple(sorted({origin_method, *AGENT_LOOP_CHILDREN}))
    if native_branch == "delegation" and origin_method == "delegation.startChild":
        # #1020 PR2 child admission: the same sealed-projection children as a chat step.
        return tuple(sorted({origin_method, *AGENT_LOOP_CHILDREN}))
    if native_branch == "guardian" and origin_method == "guardian.startTurn":
        # #1020 unattended step: the same sealed-projection children as a chat step.
        return tuple(sorted({origin_method, *AGENT_LOOP_CHILDREN}))
    if native_branch == "board_workers" and origin_method == "board-workers.startTurn":
        # #1020 WorkBoard draft step: the same sealed-projection children.
        return tuple(sorted({origin_method, *AGENT_LOOP_CHILDREN}))
    raise CompositionBindingError("composition_branch_unsupported")


# Binding schema 1 is #1019's: no method depended on the presets owner. A chat step
# admitted then is read (inspect, recovery, the job CAS) exactly as it was written.
# Schema 2 (#1020) adds the presets owner to every turn step, and an origin
# introduced by #1020 exists only from schema 2. Schema 3 (#1024) makes every
# method depend on the Python-hosted state owner under manifest v2; every new
# admission writes schema 3. A schema 1 or 2 binding still decodes and
# re-encodes byte-identically, so its job CAS predicates keep matching.
BINDING_SCHEMA = 3
BINDING_SCHEMAS = frozenset({1, 2, BINDING_SCHEMA})
TURN_STEP_METHODS = frozenset({"agent-loop.startTurn", "guardian.startTurn", "board-workers.startTurn"})
SCHEMA_2_ORIGINS = frozenset({"guardian.startTurn", "delegation.startChild", "board-workers.startTurn"})


# Frozen literal copy of the rules every binding schema 1 and 2 was written
# under (manifest v1, goal_bound=False), captured from 055ae90d; never derived
# from the live rules, so later rule edits cannot change how an old binding
# decodes. Schema 1 (#1019) differs only in that turn steps did not bind the
# presets owner. Methods introduced after #1024 exist in schema 3 only.
METHOD_DEPENDENCIES_V1 = MappingProxyType({
    "agent-loop.cancelTurn": ("seraph.agent-loop.v1", "seraph.authority.v1", "seraph.tasks.v1"),
    "agent-loop.inspectTurn": ("seraph.agent-loop.v1", "seraph.authority.v1", "seraph.tasks.v1"),
    "agent-loop.startTurn": ("seraph.agent-loop.v1", "seraph.agent-presets.v1", "seraph.authority.v1", "seraph.tasks.v1"),
    "artifacts.adopt": ("seraph.artifacts.v1", "seraph.authority.v1", "seraph.tasks.v1"),
    "artifacts.read": ("seraph.artifacts.v1", "seraph.authority.v1", "seraph.tasks.v1"),
    "artifacts.stage": ("seraph.artifacts.v1", "seraph.authority.v1", "seraph.tasks.v1"),
    "audit.append": ("seraph.artifacts.v1", "seraph.audit.v1", "seraph.authority.v1", "seraph.tasks.v1"),
    "authority.resolve": ("seraph.authority.v1", "seraph.tasks.v1"),
    "board-workers.startTurn": ("seraph.agent-presets.v1", "seraph.authority.v1", "seraph.board-workers.v1", "seraph.tasks.v1"),
    "capabilities.describe": ("seraph.authority.v1", "seraph.capabilities.v1", "seraph.tasks.v1"),
    "capabilities.invoke": ("seraph.artifacts.v1", "seraph.audit.v1", "seraph.authority.v1", "seraph.capabilities.v1", "seraph.tasks.v1"),
    "capabilities.list": ("seraph.authority.v1", "seraph.capabilities.v1", "seraph.tasks.v1"),
    "connections.inspect": ("seraph.authority.v1", "seraph.connections.v1", "seraph.tasks.v1"),
    "connections.invokeBoundAdapter": ("seraph.artifacts.v1", "seraph.audit.v1", "seraph.authority.v1", "seraph.connections.v1", "seraph.tasks.v1"),
    "conversation.accept": ("seraph.authority.v1", "seraph.conversation.v1", "seraph.tasks.v1"),
    "conversation.append": ("seraph.authority.v1", "seraph.conversation.v1", "seraph.tasks.v1"),
    "conversation.cancel": ("seraph.authority.v1", "seraph.conversation.v1", "seraph.tasks.v1"),
    "conversation.read": ("seraph.authority.v1", "seraph.conversation.v1", "seraph.tasks.v1"),
    "delegation.startChild": ("seraph.agent-loop.v1", "seraph.agent-presets.v1", "seraph.authority.v1", "seraph.delegation.v1", "seraph.tasks.v1"),
    "goals.read": ("seraph.authority.v1", "seraph.goals.v1", "seraph.tasks.v1"),
    "guardian.startTurn": ("seraph.agent-presets.v1", "seraph.authority.v1", "seraph.guardian.v1", "seraph.tasks.v1"),
    "inference.request": ("seraph.artifacts.v1", "seraph.audit.v1", "seraph.authority.v1", "seraph.inference.v1", "seraph.tasks.v1"),
    "memory.applyReviewed": ("seraph.audit.v1", "seraph.authority.v1", "seraph.memory.v1", "seraph.tasks.v1"),
    "memory.forget": ("seraph.audit.v1", "seraph.authority.v1", "seraph.memory.v1", "seraph.tasks.v1"),
    "memory.propose": ("seraph.authority.v1", "seraph.memory.v1", "seraph.tasks.v1"),
    "memory.retrieve": ("seraph.authority.v1", "seraph.memory.v1", "seraph.tasks.v1"),
    "research.buildPlan": ("seraph.authority.v1", "seraph.research.v1", "seraph.tasks.v1"),
    "research.executeAccepted": ("seraph.authority.v1", "seraph.research.v1", "seraph.source-extraction.v1", "seraph.tasks.v1"),
    "scheduler.disable": ("seraph.authority.v1", "seraph.scheduler.v1", "seraph.tasks.v1"),
    "scheduler.dispatchDue": ("seraph.authority.v1", "seraph.scheduler.v1", "seraph.tasks.v1"),
    "scheduler.register": ("seraph.authority.v1", "seraph.scheduler.v1", "seraph.tasks.v1"),
    "source-extraction.extract": ("seraph.authority.v1", "seraph.source-extraction.v1", "seraph.tasks.v1"),
    "tasks.admit": ("seraph.authority.v1", "seraph.tasks.v1"),
    "tasks.cancel": ("seraph.authority.v1", "seraph.tasks.v1"),
    "tasks.checkpoint": ("seraph.authority.v1", "seraph.tasks.v1"),
    "tasks.inspect": ("seraph.authority.v1", "seraph.tasks.v1"),
    "tasks.settle": ("seraph.authority.v1", "seraph.tasks.v1"),
})
METHOD_DEPENDENCIES_V1_SCHEMA1 = MappingProxyType({**METHOD_DEPENDENCIES_V1,
    "agent-loop.startTurn": ("seraph.agent-loop.v1", "seraph.authority.v1", "seraph.tasks.v1"),
    "board-workers.startTurn": ("seraph.authority.v1", "seraph.board-workers.v1", "seraph.tasks.v1"),
    "guardian.startTurn": ("seraph.authority.v1", "seraph.guardian.v1", "seraph.tasks.v1"),
})


def manifest_version_for(schema_version: int) -> str:
    """The method manifest a binding schema was written under."""
    return MANIFEST_VERSION if schema_version >= BINDING_SCHEMA else MANIFEST_VERSION_V1


def method_dependencies(method: str, *, goal_bound: bool = False, presets: bool = True,
                        state: bool = True) -> tuple[str, ...]:
    """Mandatory owners of one method; a missing owner denies the call."""
    domain = METHOD_DOMAINS.get(method)
    if domain is None or type(goal_bound) is not bool:
        raise CompositionBindingError("composition_method_unsupported")
    if not state:
        # Schema 1 and 2 bindings: the frozen manifest-v1 tables, never the live rules.
        frozen = (METHOD_DEPENDENCIES_V1 if presets else METHOD_DEPENDENCIES_V1_SCHEMA1).get(method)
        if frozen is None:
            raise CompositionBindingError("composition_method_unsupported")
        return tuple(sorted({*frozen, "seraph.goals.v1"})) if goal_bound else frozen
    # Schema 3 (manifest v2): every method depends on the Python-hosted state owner (#1024).
    names = {STATE_DOMAIN, "seraph.authority.v1", "seraph.tasks.v1", domain}
    if goal_bound or method == "goals.read":
        names.add("seraph.goals.v1")
    if method in {"artifacts.adopt", "audit.append"}:
        names.add("seraph.artifacts.v1")
    if method in {"memory.applyReviewed", "memory.forget"}:
        names.add("seraph.audit.v1")
    if method in GOVERNED_ORIGINS:
        # Governed effects need the artifact and audit owners explicitly (#1018).
        names.update({"seraph.artifacts.v1", "seraph.audit.v1"})
    if method in TURN_STEP_METHODS and presets:
        # Every turn step is checked against the reviewed presets (#1020).
        names.add(PRESETS_DOMAIN)
    if method == "delegation.startChild":
        # A delegated child is a chat-loop turn under its reviewed child preset (#1020 PR2).
        names.update({PRESETS_DOMAIN, "seraph.agent-loop.v1"})
    if method == "research.executeAccepted":
        # The consumer extracts through the composed source provider.
        names.add("seraph.source-extraction.v1")
    return tuple(sorted(names))


def closure_dependencies(origin_method: str, native_branch: str, *, goal_bound: bool = False,
                         presets: bool = True, state: bool = True) -> tuple[str, ...]:
    domains: set[str] = set()
    for method in method_closure(origin_method, native_branch):
        domains.update(method_dependencies(method, goal_bound=goal_bound, presets=presets, state=state))
    return tuple(sorted(domains))


def method_manifest_digest(origin_method: str, native_branch: str, *, presets: bool = True,
                           state: bool = True) -> str:
    methods = method_closure(origin_method, native_branch)
    source = {"schema_version": MANIFEST_VERSION if state else MANIFEST_VERSION_V1, "origin_method": origin_method,
              "native_branch": native_branch, "allowed_child_methods": list(methods),
              "child_dependencies": [[method, list(method_dependencies(method, presets=presets, state=state))]
                                     for method in methods]}
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
        if (type(self.schema_version) is not int or self.schema_version not in BINDING_SCHEMAS
                or (self.schema_version == 1 and self.origin_method in SCHEMA_2_ORIGINS)
                or type(self.goal_bound) is not bool
                or METHOD_DOMAINS.get(self.origin_method) != self.runtime_domain
                or self.native_branch not in NATIVE_BRANCHES
                or self.allowed_child_methods != method_closure(self.origin_method, self.native_branch)
                or keys != tuple(sorted(set(keys)))
                or keys != closure_dependencies(self.origin_method, self.native_branch, goal_bound=self.goal_bound,
                                                presets=self.presets_bound, state=self.state_bound)):
            raise CompositionBindingError("composition_binding_invalid")
        if (self.host_package_digest is None) != (self.host_composition_digest is None) or any(
                value is not None and (type(value) is not str or _SHA.fullmatch(value) is None)
                for value in (self.host_package_digest, self.host_composition_digest)):
            raise CompositionBindingError("composition_host_binding_invalid")

    @property
    def presets_bound(self) -> bool:
        """Whether this binding's schema has turn steps depend on the presets owner (#1020)."""
        return self.schema_version >= 2

    @property
    def state_bound(self) -> bool:
        """Whether this binding's schema has every method depend on the state owner (#1024)."""
        return self.schema_version >= BINDING_SCHEMA

    @property
    def method_manifest_version(self) -> str:
        return manifest_version_for(self.schema_version)

    def allows(self, method: str) -> bool:
        return method in self.allowed_child_methods

    def epoch_for(self, method: str) -> int:
        if not self.allows(method):
            raise CompositionBindingError("composition_child_method_not_bound")
        domain = METHOD_DOMAINS[method]
        return next(item.epoch for item in self.dependency_vector if item.runtime_domain == domain)

    def payload(self):
        return dict(schema_version=self.schema_version, runtime_domain=self.runtime_domain,
                    origin_method=self.origin_method,
                    native_branch=self.native_branch, allowed_child_methods=list(self.allowed_child_methods),
                    method_manifest_version=self.method_manifest_version,
                    method_manifest_digest=method_manifest_digest(self.origin_method, self.native_branch,
                                                                  presets=self.presets_bound,
                                                                  state=self.state_bound),
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
            if (value["method_manifest_version"] != result.method_manifest_version
                    or value["method_manifest_digest"] != method_manifest_digest(
                        result.origin_method, result.native_branch, presets=result.presets_bound,
                        state=result.state_bound)
                    or value["binding_digest"] != result.binding_digest):
                raise ValueError("digest")
            return result
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise CompositionBindingError("composition_binding_invalid") from exc


def _checked_row(row) -> CompositionDependency:
    dependency = CompositionDependency(row.runtime_domain, row.owner_kind, row.epoch, row.composition_digest)
    if row.runtime_domain in PYTHON_HOSTED_DOMAINS and (
            row.owner_kind != "legacy" or row.composition_digest != legacy_composition_digest(row.runtime_domain)):
        # A tampered or restored row can never hand a Python-hosted owner to Cordis.
        raise CompositionBindingError("composition_inventory_invalid")
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
        host_package_digest, host_composition_digest, goal_bound, BINDING_SCHEMA)


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


async def introduce_domain(db, domain: str) -> None:
    """The async form of one introduction, inside the caller's composition writer."""
    from datetime import datetime, timezone
    from uuid import uuid4
    if domain not in DOMAIN_INTRODUCTIONS:
        raise CompositionBindingError("composition_domain_unknown")
    await db.execute(text(
        "INSERT INTO runtime_composition_states "
        "(runtime_domain, owner_kind, epoch, composition_digest, state, recovery_receipt_ref) "
        "VALUES (:domain, 'legacy', 1, :digest, 'ready', NULL)"),
        {"domain": domain, "digest": legacy_composition_digest(domain)})
    await db.execute(text(
        "INSERT INTO audit_events (id, session_id, actor, event_type, tool_name, risk_level, policy_mode, summary, "
        "details_json, created_at) VALUES (:id, NULL, 'system', 'runtime_composition_domain_introduced', NULL, "
        "'low', 'full', :summary, :details, :at)"),
        {"id": str(uuid4()), "summary": f"Runtime domain {domain} introduced as its legacy owner",
         "details": _canonical({"runtime_domain": domain, "owner_kind": "legacy", "epoch": 1,
                                "introduced_by": DOMAIN_INTRODUCTIONS[domain]}),
         "at": datetime.now(timezone.utc).replace(tzinfo=None)})


def introduce_domains_sync(connection) -> list[str]:
    """Insert each introduced domain missing from a non-empty inventory once, as its legacy owner.

    An empty inventory is seeded whole by ``seed_legacy_inventory_sync``. An
    existing one keeps every row it has; only domains named in
    ``DOMAIN_INTRODUCTIONS`` are added, each audited. A missing original
    domain still denies its consumers until a fenced transition repairs it.
    """
    from datetime import datetime, timezone
    from uuid import uuid4
    existing = {row[0] for row in connection.execute(text("SELECT runtime_domain FROM runtime_composition_states"))}
    if not existing:
        return []
    added = []
    for domain, issue in DOMAIN_INTRODUCTIONS.items():
        if domain in existing:
            continue
        connection.execute(text(
            "INSERT INTO runtime_composition_states "
            "(runtime_domain, owner_kind, epoch, composition_digest, state, recovery_receipt_ref) "
            "VALUES (:domain, 'legacy', 1, :digest, 'ready', NULL)"),
            {"domain": domain, "digest": legacy_composition_digest(domain)})
        connection.execute(text(
            "INSERT INTO audit_events (id, session_id, actor, event_type, tool_name, risk_level, policy_mode, summary, "
            "details_json, created_at) VALUES (:id, NULL, 'system', 'runtime_composition_domain_introduced', NULL, "
            "'low', 'full', :summary, :details, :at)"),
            {"id": str(uuid4()), "summary": f"Runtime domain {domain} introduced as its legacy owner",
             "details": _canonical({"runtime_domain": domain, "owner_kind": "legacy", "epoch": 1,
                                    "introduced_by": issue}),
             "at": datetime.now(timezone.utc).replace(tzinfo=None)})
        added.append(domain)
    return added


def introduce_state_domain_sync(connection) -> bool:
    """Add the Python-hosted ``seraph.state.v1`` row to an exact pre-#1024 inventory.

    A chained exact-prefix step after ``introduce_domains_sync`` (#1024
    addendum), never insert-if-missing: it runs only when the inventory is
    exactly ``PRE_STATE_DOMAINS``, inserts one legacy row (epoch 1, the fixed
    legacy digest, ``ready``) plus one audit event, and changes no existing
    row. An empty table is seeded whole by ``seed_legacy_inventory_sync``; any
    other partial or unknown inventory is left alone and keeps failing closed.
    It runs inside ``init_db``'s immediate writer, so also on the first start
    after a host-CLI restore of a pre-#1024 backup. Forward-only: an older
    binary rejects the extra row with ``composition_inventory_invalid``. An
    exact-set inventory with any row that does not decode raises
    ``composition_inventory_invalid`` (failing ``init_db`` and so the state
    provider) and inserts nothing.
    """
    from datetime import datetime, timezone
    from uuid import uuid4
    from types import SimpleNamespace
    rows = [SimpleNamespace(**row._mapping) for row in connection.execute(text(
        "SELECT runtime_domain, owner_kind, epoch, composition_digest, state, recovery_receipt_ref "
        "FROM runtime_composition_states"))]
    names = [row.runtime_domain for row in rows]
    if len(names) != len(PRE_STATE_DOMAINS) or set(names) != set(PRE_STATE_DOMAINS):
        return False
    # The exact prior set must also decode in full (owner, epoch, digest, state,
    # receipt, and legacy owners for authority and audit) before anything is
    # added: a corrupt inventory is refused typed and nothing is inserted.
    try:
        for row in rows:
            _checked_row(row)
    except CompositionBindingError as exc:
        raise CompositionBindingError("composition_inventory_invalid") from exc
    inserted = connection.execute(text(
        "INSERT INTO runtime_composition_states "
        "(runtime_domain, owner_kind, epoch, composition_digest, state, recovery_receipt_ref) "
        "SELECT :domain, 'legacy', 1, :digest, 'ready', NULL WHERE NOT EXISTS "
        "(SELECT 1 FROM runtime_composition_states WHERE runtime_domain = :domain)"),
        {"domain": STATE_DOMAIN, "digest": legacy_composition_digest(STATE_DOMAIN)}).rowcount
    if inserted != 1:
        return False
    connection.execute(text(
        "INSERT INTO audit_events (id, session_id, actor, event_type, tool_name, risk_level, policy_mode, summary, "
        "details_json, created_at) VALUES (:id, NULL, 'system', 'runtime_composition_domain_introduced', NULL, "
        "'low', 'full', :summary, :details, :at)"),
        {"id": str(uuid4()), "summary": f"Runtime domain {STATE_DOMAIN} introduced as its legacy owner",
         "details": _canonical({"runtime_domain": STATE_DOMAIN, "owner_kind": "legacy", "epoch": 1,
                                "introduced_by": "1024", "prior_domains": len(PRE_STATE_DOMAINS)}),
         "at": datetime.now(timezone.utc).replace(tzinfo=None)})
    return True


def introduce_dispatch_domain_sync(connection) -> bool:
    """Add ``seraph.dispatch.v1`` to an exact post-#1024 inventory (#1025).

    The next chained exact-prefix step after ``introduce_state_domain_sync``:
    it runs only when the inventory is exactly ``PRE_DISPATCH_DOMAINS`` and
    every row decodes, inserts one legacy row (epoch 1, the fixed legacy
    digest, ``ready``) plus one audit event, and changes no existing row. Any
    other partial or unknown inventory is left alone and keeps failing closed.
    Forward-only: an older binary rejects the extra row with
    ``composition_inventory_invalid``.
    """
    from datetime import datetime, timezone
    from uuid import uuid4
    from types import SimpleNamespace
    rows = [SimpleNamespace(**row._mapping) for row in connection.execute(text(
        "SELECT runtime_domain, owner_kind, epoch, composition_digest, state, recovery_receipt_ref "
        "FROM runtime_composition_states"))]
    names = [row.runtime_domain for row in rows]
    if len(names) != len(PRE_DISPATCH_DOMAINS) or set(names) != set(PRE_DISPATCH_DOMAINS):
        return False
    try:
        for row in rows:
            _checked_row(row)
    except CompositionBindingError as exc:
        raise CompositionBindingError("composition_inventory_invalid") from exc
    inserted = connection.execute(text(
        "INSERT INTO runtime_composition_states "
        "(runtime_domain, owner_kind, epoch, composition_digest, state, recovery_receipt_ref) "
        "SELECT :domain, 'legacy', 1, :digest, 'ready', NULL WHERE NOT EXISTS "
        "(SELECT 1 FROM runtime_composition_states WHERE runtime_domain = :domain)"),
        {"domain": DISPATCH_DOMAIN, "digest": legacy_composition_digest(DISPATCH_DOMAIN)}).rowcount
    if inserted != 1:
        return False
    connection.execute(text(
        "INSERT INTO audit_events (id, session_id, actor, event_type, tool_name, risk_level, policy_mode, summary, "
        "details_json, created_at) VALUES (:id, NULL, 'system', 'runtime_composition_domain_introduced', NULL, "
        "'low', 'full', :summary, :details, :at)"),
        {"id": str(uuid4()), "summary": f"Runtime domain {DISPATCH_DOMAIN} introduced as its legacy owner",
         "details": _canonical({"runtime_domain": DISPATCH_DOMAIN, "owner_kind": "legacy", "epoch": 1,
                                "introduced_by": "1025", "prior_domains": len(PRE_DISPATCH_DOMAINS)}),
         "at": datetime.now(timezone.utc).replace(tzinfo=None)})
    return True


def domain_ready_predicate(runtime_domain: str, owner_kind: str, epoch: int, composition_digest: str):
    """Standalone ``EXISTS`` fence for non-job writes (#1025): the exact owner is ready at ``epoch``."""
    from sqlalchemy.orm import aliased
    state = aliased(RuntimeCompositionState)
    return select(state.runtime_domain).where(
        state.runtime_domain == runtime_domain, state.owner_kind == owner_kind, state.epoch == epoch,
        state.composition_digest == composition_digest, state.state == "ready").exists()


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
    if runtime_domain in PYTHON_HOSTED_DOMAINS and owner_kind != "legacy":
        # Python always hosts state, authority and audit (#1024).
        raise CompositionBindingError("composition_owner_not_transferable")
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
        active = (await db.execute(select(WorkflowRunState.status, WorkflowRunState.composition_binding_json).where(
            WorkflowRunState.composition_binding_json.is_not(None),
            WorkflowRunState.status.in_((*_ACTIVE_STATUSES, *_LIABILITY_STATUSES))))).all()
        for status, raw in active:
            try:
                bound = RuntimeCompositionBinding.from_json(raw)
            except CompositionBindingError as exc:
                # An undecodable binding on live or liability work is never
                # skipped (#1024): the operator resolves that one job first.
                raise CompositionBindingError("composition_not_quiescent") from exc
            if status in _ACTIVE_STATUSES and expected in bound.dependency_vector:
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
