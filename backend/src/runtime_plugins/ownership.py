"""Canonical composition control. Availability never supplies invocation authority."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re

from sqlmodel import select
from sqlalchemy import update
from src.db.models import RuntimeCompositionState

DOMAINS = tuple(f"seraph.{name}.v1" for name in (
    "authority", "goals", "tasks", "capabilities", "inference", "memory",
    "artifacts", "audit", "research", "conversation", "scheduler",
    "connections", "agent-loop", "source-extraction"))
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_REF = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_RESTORE = re.compile(r"restore:([0-9a-f]{64})(?::([0-9a-f]{32}))?\Z")


def restore_audit_reference(reference):
    if type(reference) is not str or (match := _RESTORE.fullmatch(reference)) is None:
        raise CompositionBindingError("composition_restore_marker_invalid")
    return match.group(2)


def restored_recovery_reference(closure_digest, original_reference):
    if type(closure_digest) is not str or not _SHA.fullmatch(closure_digest):
        raise CompositionBindingError("composition_restore_marker_invalid")
    original = None
    if original_reference is not None:
        if type(original_reference) is not str:
            raise CompositionBindingError("composition_restore_marker_invalid")
        original = restore_audit_reference(original_reference) if original_reference.startswith("restore:") else original_reference
        if original is not None and not re.fullmatch(r"[0-9a-f]{32}", original):
            raise CompositionBindingError("composition_restore_marker_invalid")
    return "restore:" + closure_digest + (":" + original if original is not None else "")
_METHODS = {
    "authority": ("resolve",), "goals": ("read",),
    "tasks": ("admit", "inspect", "cancel", "checkpoint", "settle"),
    "capabilities": ("list", "describe", "invoke"), "inference": ("request",),
    "memory": ("retrieve", "propose", "applyReviewed", "forget"),
    "artifacts": ("read", "stage", "adopt"), "audit": ("append",),
    "research": ("buildPlan", "executeAccepted"),
    "conversation": ("accept", "append", "read", "cancel"),
    "scheduler": ("register", "disable", "dispatchDue"),
    "connections": ("inspect", "invokeBoundAdapter"),
    "agent-loop": ("startTurn", "cancelTurn", "inspectTurn"),
    "source-extraction": ("extract",),
}
METHOD_DOMAINS = {f"{name}.{method}": f"seraph.{name}.v1"
                  for name, methods in _METHODS.items() for method in methods}


class CompositionBindingError(ValueError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class CompositionDependency:
    runtime_domain: str
    owner_kind: str
    epoch: int
    composition_digest: str

    def __post_init__(self):
        if (self.runtime_domain not in DOMAINS or self.owner_kind not in {"legacy", "cordis"}
            or type(self.epoch) is not int or not 1 <= self.epoch <= 2**63 - 1
            or type(self.composition_digest) is not str or not _SHA.fullmatch(self.composition_digest)):
            raise CompositionBindingError("composition_binding_invalid")

    def payload(self):
        return dict(runtime_domain=self.runtime_domain, owner_kind=self.owner_kind,
                    epoch=self.epoch, composition_digest=self.composition_digest)


@dataclass(frozen=True)
class RuntimeCompositionBinding:
    runtime_domain: str
    origin_method: str
    native_branch: str
    allowed_child_methods: tuple[str, ...]
    dependency_vector: tuple[CompositionDependency, ...]
    host_package_digest: str | None = None
    host_composition_digest: str | None = None
    schema_version: int = 1

    def __post_init__(self):
        keys = tuple(item.runtime_domain for item in self.dependency_vector)
        if (type(self.schema_version) is not int or self.schema_version != 1
            or METHOD_DOMAINS.get(self.origin_method) != self.runtime_domain
            or self.native_branch not in {"base", "artifact", "workflow", "public_research", "direct_turn", "generic_turn"}
            or self.allowed_child_methods != method_closure(self.origin_method, self.native_branch)
            or not 1 <= len(keys) <= 14 or keys != tuple(sorted(set(keys)))
            or not {self.runtime_domain, "seraph.authority.v1", "seraph.tasks.v1"}.issubset(keys)):
            raise CompositionBindingError("composition_binding_invalid")
        if (self.host_package_digest is None) != (self.host_composition_digest is None) or any(
                value is not None and (type(value) is not str or _SHA.fullmatch(value) is None)
                for value in (self.host_package_digest, self.host_composition_digest)):
            raise CompositionBindingError("composition_host_binding_invalid")
        required = set(method_dependencies(self.origin_method, native_branch=self.native_branch))
        for method in self.allowed_child_methods:
            required.update(method_dependencies(method))
        if set(keys) not in (required, required | {"seraph.goals.v1"},
                             required | {"seraph.goals.v1", "seraph.inference.v1"}):
            raise CompositionBindingError("composition_dependency_manifest_changed")

    @property
    def called_epoch(self):
        return next(item.epoch for item in self.dependency_vector if item.runtime_domain == self.runtime_domain)

    def allows(self, method):
        return method in self.allowed_child_methods

    def epoch_for(self, method):
        if not self.allows(method):
            raise CompositionBindingError("composition_child_method_not_bound")
        domain = METHOD_DOMAINS[method]
        return next(item.epoch for item in self.dependency_vector if item.runtime_domain == domain)

    def payload(self):
        return dict(schema_version=1, runtime_domain=self.runtime_domain, origin_method=self.origin_method,
                    native_branch=self.native_branch,
                    allowed_child_methods=list(self.allowed_child_methods),
                    method_manifest_version="runtime-service-methods.v1",
                    method_manifest_digest=method_manifest_digest(self.origin_method, self.native_branch),
                    host_package_digest=self.host_package_digest,
                    host_composition_digest=self.host_composition_digest,
                    dependency_vector=[item.payload() for item in self.dependency_vector])

    @property
    def binding_digest(self):
        return hashlib.sha256(_canonical(self.payload()).encode()).hexdigest()

    def to_json(self):
        return _canonical({**self.payload(), "binding_digest": self.binding_digest})

    @classmethod
    def from_json(cls, text):
        try:
            def no_duplicates(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError("duplicate key")
                    value[key] = item
                return value
            if type(text) is not str or len(text.encode()) > 8192:
                raise ValueError("size")
            value = json.loads(text, object_pairs_hook=no_duplicates)
            if set(value) != {"schema_version", "runtime_domain", "origin_method", "native_branch", "allowed_child_methods", "dependency_vector", "binding_digest",
                              "method_manifest_version", "method_manifest_digest", "host_package_digest", "host_composition_digest"}:
                raise ValueError("fields")
            if type(value["dependency_vector"]) is not list or len(value["dependency_vector"]) > 14:
                raise ValueError("vector")
            for item in value["dependency_vector"]:
                if type(item) is not dict or set(item) != {"runtime_domain", "owner_kind", "epoch", "composition_digest"}:
                    raise ValueError("entry")
            if type(value["allowed_child_methods"]) is not list or len(value["allowed_child_methods"]) > 34:
                raise ValueError("methods")
            result = cls(value["runtime_domain"], value["origin_method"], value["native_branch"], tuple(value["allowed_child_methods"]),
                         tuple(CompositionDependency(**item) for item in value["dependency_vector"]),
                         value["host_package_digest"], value["host_composition_digest"], value["schema_version"])
            if (value["method_manifest_version"] != "runtime-service-methods.v1" or
                    value["method_manifest_digest"] != method_manifest_digest(result.origin_method, result.native_branch)):
                raise ValueError("manifest")
            if value["binding_digest"] != result.binding_digest:
                raise ValueError("digest")
            return result
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise CompositionBindingError("composition_binding_invalid") from exc


def method_closure(origin_method, native_branch):
    if origin_method not in METHOD_DOMAINS:
        raise CompositionBindingError("composition_method_unsupported")
    if native_branch == "base":
        return (origin_method,)
    if native_branch in {"artifact", "workflow"} and origin_method in {"tasks.admit", "capabilities.invoke"}:
        return tuple(sorted({origin_method, "tasks.inspect", "tasks.cancel", "tasks.checkpoint", "tasks.settle",
                             "artifacts.read", "artifacts.stage", "artifacts.adopt"}))
    if native_branch == "public_research" and origin_method == "research.buildPlan":
        return (origin_method,)
    if native_branch == "public_research" and origin_method == "research.executeAccepted":
        return tuple(sorted({origin_method, "tasks.inspect", "tasks.cancel", "tasks.checkpoint", "tasks.settle",
            "artifacts.read", "artifacts.stage", "artifacts.adopt", "source-extraction.extract",
            "inference.request", "audit.append"}))
    # Turn execution producer must freeze its real native method manifest;
    # construction availability alone cannot grant an invented tool closure.
    if native_branch in {"direct_turn", "generic_turn"} and origin_method in {"conversation.accept", "agent-loop.startTurn"}:
        return tuple(sorted({origin_method, "agent-loop.startTurn", "agent-loop.cancelTurn", "agent-loop.inspectTurn",
            "conversation.read", "conversation.append", "conversation.cancel", "inference.request", "audit.append"}))
    raise CompositionBindingError("composition_branch_unsupported")


def method_manifest_digest(origin_method, native_branch):
    methods = method_closure(origin_method, native_branch)
    source = {"schema_version": "runtime-service-methods.v1", "origin_method": origin_method,
        "native_branch": native_branch, "allowed_child_methods": methods,
        "origin_dependencies": method_dependencies(origin_method, native_branch=native_branch),
        "child_dependencies": [[method, method_dependencies(method)] for method in methods]}
    return hashlib.sha256(_canonical(source).encode()).hexdigest()


def method_dependencies(method, *, native_branch="base", goal_bound=False, programme_bound=False):
    domain = METHOD_DOMAINS.get(method)
    if domain is None or type(goal_bound) is not bool or type(programme_bound) is not bool:
        raise CompositionBindingError("composition_method_unsupported")
    names = {"authority", "tasks", domain.split(".")[1]}
    if goal_bound or programme_bound:
        names.add("goals")
    if programme_bound:
        names.add("inference")
    fixed = {"memory.applyReviewed": {"audit"}, "memory.forget": {"audit"},
             "research.buildPlan": {"artifacts", "inference"},
             "research.executeAccepted": {"capabilities", "inference", "artifacts", "source-extraction"},
             "source-extraction.extract": {"artifacts", "research"},
             "agent-loop.startTurn": {"conversation", "capabilities", "inference", "audit"},
             "agent-loop.cancelTurn": {"conversation"}, "agent-loop.inspectTurn": {"conversation"},
             "conversation.cancel": {"agent-loop"}}
    names.update(fixed.get(method, ()))
    if native_branch == "artifact" and method in {"tasks.admit", "tasks.checkpoint", "tasks.settle", "capabilities.invoke", "memory.propose", "memory.applyReviewed"}:
        names.add("artifacts")
    elif native_branch == "workflow" and method.startswith("tasks."):
        pass
    elif native_branch == "public_research" and method in {"capabilities.invoke", "tasks.admit", "tasks.settle", "tasks.cancel", "artifacts.adopt"}:
        names.update({"research", "capabilities", "inference", "artifacts", "source-extraction"})
    elif native_branch == "public_research" and method in {"source-extraction.extract", "research.buildPlan",
            "research.executeAccepted", "artifacts.read", "artifacts.stage"}:
        names.update({"research", "artifacts", "inference"})
    elif native_branch in {"direct_turn", "generic_turn"} and method in {"conversation.accept", "agent-loop.startTurn"}:
        names.update({"agent-loop", "conversation", "capabilities", "inference", "audit"})
        if native_branch == "generic_turn":
            names.update({"memory", "goals"})
    elif native_branch != "base":
        raise CompositionBindingError("composition_branch_unsupported")
    return tuple(sorted(f"seraph.{name}.v1" for name in names))


async def inventory(db):
    rows = list((await db.execute(select(RuntimeCompositionState))).scalars())
    if len(rows) != 14 or {row.runtime_domain for row in rows} != set(DOMAINS):
        raise CompositionBindingError("composition_inventory_incomplete")
    for row in rows:
        CompositionDependency(row.runtime_domain, row.owner_kind, row.epoch, row.composition_digest)
        if row.state not in {"ready", "draining", "blocked"} or (row.recovery_receipt_ref is not None
            and (type(row.recovery_receipt_ref) is not str or not _REF.fullmatch(row.recovery_receipt_ref))):
            raise CompositionBindingError("composition_inventory_invalid")
    return {row.runtime_domain: row for row in rows}


async def bind_invocation(db, *, method, native_branch="base", goal_bound=False, programme_bound=False,
                          reviewed_composition=None):
    methods = method_closure(method, native_branch)
    domains = set(method_dependencies(method, native_branch=native_branch,
                                  goal_bound=goal_bound, programme_bound=programme_bound))
    for child in methods:
        domains.update(method_dependencies(child, goal_bound=goal_bound, programme_bound=programme_bound))
    domains = tuple(sorted(domains))
    rows = await inventory(db)
    if any(rows[domain].state != "ready" for domain in domains):
        raise CompositionBindingError("composition_dependency_unavailable")
    host_package_digest = host_composition_digest = None
    if reviewed_composition is not None:
        from src.runtime_plugins.composition import ReviewedComposition
        if not isinstance(reviewed_composition, ReviewedComposition):
            raise CompositionBindingError("composition_reviewed_host_required")
        host_package_digest = reviewed_composition.package_digest
        host_composition_digest = reviewed_composition.composition_digest
    return RuntimeCompositionBinding(METHOD_DOMAINS[method], method, native_branch, methods,
        tuple(CompositionDependency(domain, rows[domain].owner_kind, rows[domain].epoch,
                                    rows[domain].composition_digest) for domain in domains),
        host_package_digest, host_composition_digest)


async def validate_invocation(db, binding):
    if not isinstance(binding, RuntimeCompositionBinding):
        raise CompositionBindingError("composition_binding_required")
    rows = await inventory(db)
    for dependency in binding.dependency_vector:
        row = rows[dependency.runtime_domain]
        if row.state != "ready" or dependency != CompositionDependency(row.runtime_domain,
                row.owner_kind, row.epoch, row.composition_digest):
            raise CompositionBindingError("composition_dependency_changed")


async def validate_run(db, run):
    value = getattr(run, "composition_binding_json", None)
    if value is not None:
        await validate_invocation(db, RuntimeCompositionBinding.from_json(value))


async def initialize_fresh_deployment(db, *, composition_digests):
    """Called only by stopped fresh-deployment initialization, never migration.

    Missing/restored inventories retain their failure; ordinary startup cannot
    reseed an existing deployment or rewrite legacy jobs to epoch one.
    """
    from src.workspace.lifecycle import _LIFECYCLE_FENCE_DEPTH
    from src.db.models import WorkflowRunState, Message
    if (_LIFECYCLE_FENCE_DEPTH.get() <= 0 or not db.info.get("native_writer_started")
        or db.info.get("composition_guard") is None):
        raise CompositionBindingError("composition_stopped_writer_required")
    if set(composition_digests) != set(DOMAINS):
        raise CompositionBindingError("composition_manifest_incomplete")
    if (await db.scalar(select(RuntimeCompositionState.runtime_domain).limit(1)) is not None
        or await db.scalar(select(WorkflowRunState.id).limit(1)) is not None
        or await db.scalar(select(Message.id).limit(1)) is not None
        or db.info.get("composition_base_witness") is not None):
        raise CompositionBindingError("composition_fresh_deployment_required")
    for domain in DOMAINS:
        dependency = CompositionDependency(domain, "legacy", 1, composition_digests[domain])
        db.add(RuntimeCompositionState(**dependency.payload(), state="ready"))
    await db.flush()
    return await inventory(db)


def checked_recovery_proof(event_type, details_json):
    try:
        proof = json.loads(details_json)
        if event_type != "runtime_composition_recovery" or type(proof) is not dict or set(proof) != {
                "schema_version", "runtime_domain", "prior", "target", "state", "phase", "prior_recovery_receipt_ref"}:
            raise ValueError
        if type(proof["schema_version"]) is not int or proof["schema_version"] != 1:
            raise ValueError
        for key in ("prior", "target"):
            value = proof[key]
            if type(value) is not dict or set(value) != {"runtime_domain", "owner_kind", "epoch", "composition_digest"}:
                raise ValueError
            dependency = CompositionDependency(**value)
            if dependency.runtime_domain != proof["runtime_domain"]:
                raise ValueError
        if proof["state"] not in {"ready", "draining", "blocked"} or proof["phase"] not in {"boot_verified", "awaiting_boot", "maintenance_quiescent"}:
            raise ValueError
        if ((proof["state"] == "ready") != (proof["phase"] == "boot_verified")
            or (proof["phase"] == "awaiting_boot" and (proof["state"] != "blocked" or proof["target"]["epoch"] <= proof["prior"]["epoch"]))
            or proof["target"]["epoch"] < proof["prior"]["epoch"]):
            raise ValueError
        reference = proof["prior_recovery_receipt_ref"]
        if reference is not None and (type(reference) is not str or not _REF.fullmatch(reference)):
            raise ValueError
        if reference is not None and reference.startswith("restore:"):
            restore_audit_reference(reference)
        return proof
    except (ValueError, TypeError, KeyError) as exc:
        raise CompositionBindingError("composition_recovery_receipt_changed") from exc


async def transition_owner(db, *, runtime_domain, expected: CompositionDependency,
                           owner_kind, epoch, composition_digest, state, recovery_receipt_ref):
    """Exact stopped-runtime CAS; rollback is another higher owner epoch.

    A recovery reference resolves an existing native audit receipt, not a
    caller/child assertion of readiness or physical quiescence.
    """
    from src.workspace.lifecycle import _LIFECYCLE_FENCE_DEPTH
    from src.db.models import AuditEvent
    if (_LIFECYCLE_FENCE_DEPTH.get() <= 0 or not db.info.get("native_writer_started")
        or db.info.get("composition_guard") is None):
        raise CompositionBindingError("composition_stopped_writer_required")
    incoming = CompositionDependency(runtime_domain, owner_kind, epoch, composition_digest)
    if not isinstance(expected, CompositionDependency) or expected.runtime_domain != runtime_domain:
        raise CompositionBindingError("composition_expected_binding_required")
    rows = await inventory(db)
    prior = rows[runtime_domain]
    changed_owner = (owner_kind, composition_digest) != (expected.owner_kind, expected.composition_digest)
    if (epoch < expected.epoch or (changed_owner and epoch <= expected.epoch)
        or (epoch > expected.epoch and state != "blocked") or state not in {"ready", "draining", "blocked"}):
        raise CompositionBindingError("composition_epoch_conflict")
    if type(recovery_receipt_ref) is not str or not _REF.fullmatch(recovery_receipt_ref):
        raise CompositionBindingError("composition_recovery_receipt_required")
    receipt = await db.get(AuditEvent, recovery_receipt_ref)
    try:
        proof = checked_recovery_proof(receipt.event_type, receipt.details_json) if receipt is not None else None
    except (ValueError, TypeError):
        proof = None
    expected_proof = dict(schema_version=1, runtime_domain=runtime_domain,
        prior=expected.payload(), target=incoming.payload(), state=state,
        prior_recovery_receipt_ref=prior.recovery_receipt_ref,
        phase=("boot_verified" if state == "ready" else "awaiting_boot" if epoch > expected.epoch else "maintenance_quiescent"))
    if receipt is None or receipt.event_type != "runtime_composition_recovery" or proof != expected_proof:
        raise CompositionBindingError("composition_recovery_receipt_changed")
    if state == "ready" and epoch == expected.epoch and (prior.state != "blocked"
            or not prior.recovery_receipt_ref):
        raise CompositionBindingError("composition_boot_transition_required")
    if state == "ready":
        previous_receipt = await db.get(AuditEvent, prior.recovery_receipt_ref)
        try:
            previous_proof = json.loads(previous_receipt.details_json) if previous_receipt is not None else None
        except (TypeError, ValueError):
            previous_proof = None
        if (previous_receipt is None or previous_receipt.event_type != "runtime_composition_recovery"
            or type(previous_proof) is not dict or previous_proof.get("phase") != "awaiting_boot"
            or previous_proof.get("target") != expected.payload() or previous_proof.get("state") != "blocked"):
            raise CompositionBindingError("composition_original_boot_receipt_required")
    result = await db.execute(update(RuntimeCompositionState).where(
        RuntimeCompositionState.runtime_domain == runtime_domain,
        RuntimeCompositionState.owner_kind == expected.owner_kind,
        RuntimeCompositionState.epoch == expected.epoch,
        RuntimeCompositionState.composition_digest == expected.composition_digest,
        RuntimeCompositionState.state == prior.state,
        RuntimeCompositionState.recovery_receipt_ref == prior.recovery_receipt_ref).values(
            **incoming.payload(), state=state, recovery_receipt_ref=recovery_receipt_ref)
        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise CompositionBindingError("composition_owner_changed")


async def begin_native_writer(db, *, owner, fresh=False):
    """Private native ingress/maintenance seam, before any Message/row insert."""
    from sqlalchemy import text
    from src.workspace.accounting_witness import prepare_composition_session
    if owner not in {"native_ingress", "composition_maintenance", "durable_jobs", "finite_service"}:
        raise CompositionBindingError("composition_native_writer_required")
    guard = db.info.get("composition_guard")
    if guard is None:
        guard = await prepare_composition_session(db, fresh=fresh)
    if db.in_transaction():
        raise CompositionBindingError("composition_native_writer_not_fresh")
    await db.execute(text("BEGIN IMMEDIATE"))
    db.info["native_writer_started"] = True
    db.info["composition_writer_owner"] = owner
    return guard
