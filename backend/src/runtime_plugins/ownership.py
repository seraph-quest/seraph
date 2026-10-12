"""Canonical composition control. Availability never supplies invocation authority."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import weakref
from contextvars import ContextVar

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
_REPORT_RUNTIME_TRACE_ACTIVE = ContextVar("native_memory_report_runtime_trace", default=None)
_REPORT_RUNTIME_TRACE_COLLECTORS = weakref.WeakValueDictionary()
_REPORT_RUNTIME_NUMERIC = weakref.WeakKeyDictionary()


def _native_memory_report_runtime_owner(db):
    from src.work_board.review import (_MEMORY_REPORT_PRINCIPAL_SCOPE,
        _MemoryReportDependencyNumericInventory, _MEMORY_REPORT_DEPENDENCY_NUMERIC,
        _MEMORY_REPORT_DEPENDENCY_NUMERIC_SEAL, _memory_report_principal_numeric_owner)
    from src.memory.header_bounds import HeaderBoundsError
    record = _MEMORY_REPORT_PRINCIPAL_SCOPE.get()
    if record is None:
        return None
    if (type(record) is not _MemoryReportDependencyNumericInventory
            or _MEMORY_REPORT_DEPENDENCY_NUMERIC.get(record) is not _MEMORY_REPORT_DEPENDENCY_NUMERIC_SEAL
            or record._seal is not _MEMORY_REPORT_DEPENDENCY_NUMERIC_SEAL
            or record._issued_id != id(record)):
        raise HeaderBoundsError("memory_report_runtime_numeric_owner_changed")
    return _memory_report_principal_numeric_owner(db, record.owner_principal_id)


class _NativeMemoryReportRuntimeTraceCollector:
    """Only the original REPORT Runtime14 certification's numerical trace."""
    def __init__(self, owner, prior):
        self.owner = owner
        self.prior = prior
        self.entries = []

    def _checked(self):
        from src.memory.header_bounds import HeaderBoundsError
        if (_REPORT_RUNTIME_TRACE_COLLECTORS.get(id(self)) is not self
                or _REPORT_RUNTIME_TRACE_ACTIVE.get() is not self):
            raise HeaderBoundsError("memory_report_runtime_numeric_trace_changed")
        if _native_memory_report_runtime_owner(self.owner.db) is not self.owner:
            raise HeaderBoundsError("memory_report_runtime_numeric_trace_changed")

    def _storage_debit(self, upper, stage):
        from src.memory.header_bounds import _MEMORY_CHARGE_TRACE
        token = _MEMORY_CHARGE_TRACE.set(self.prior)
        try:
            self.owner.budget.debit(upper, appearance=("native-memory-report-runtime-trace", stage, len(self.entries)))
        finally:
            _MEMORY_CHARGE_TRACE.reset(token)

    def append_charge(self, appearance, amount):
        from src.memory.header_bounds import HeaderBoundsError
        from src.memory.composition_headers import _common_trace_label_width
        self._checked()
        if type(amount) is not int or not 0 <= amount < 2**63:
            raise HeaderBoundsError("memory_report_runtime_numeric_trace_changed")
        pair = 3 + _common_trace_label_width(appearance) + 20
        self._storage_debit(pair, "entry-pair")
        self._storage_debit(pair + 1, "entry-list-slot")
        entry = (appearance, amount)
        self.entries.append(entry)
        if self.prior is not None and self.prior[0] is self.owner.budget:
            from src.memory.composition_headers import _CommonCertificationTraceCollector
            from src.runtime_plugins.memory_producer import _MemoryAuthorityTraceCollector, _NativeMemoryReportTraceCollector
            from src.workflows.job_runtime import _NativeMemoryClaimTraceCollector
            from src.workspace.accounting_witness import _NativeMemoryAccountingTraceCollector
            outer = self.prior[1]
            if type(outer) in (_CommonCertificationTraceCollector, _MemoryAuthorityTraceCollector,
                    _NativeMemoryReportTraceCollector, _NativeMemoryClaimTraceCollector,
                    _NativeMemoryAccountingTraceCollector, _NativeMemoryReportRuntimeTraceCollector):
                outer.append_charge(appearance, amount)
            elif type(outer) is list:
                outer.append(entry)
            else:
                raise HeaderBoundsError("memory_report_runtime_numeric_trace_changed")


async def _native_memory_report_runtime_certificate(db, owner):
    from src.memory.header_bounds import (_MEMORY_CHARGE_TRACE, HeaderBoundsError,
        HeaderCertificate, RUNTIME_COMPOSITION, _SEAL)
    from src.memory.composition_headers import _common_trace_label_width
    if (_native_memory_report_runtime_owner(db) is not owner
            or _REPORT_RUNTIME_TRACE_ACTIVE.get() is not None):
        raise HeaderBoundsError("memory_report_runtime_numeric_trace_changed")
    budget = owner.budget
    constructor = 2 + 2 + 3 * 4
    for name in ("owner", "prior", "entries"):
        constructor += _common_trace_label_width(name) + 1
    budget.debit(constructor, appearance=("native-memory-report-runtime-trace", "collector-construction"))
    budget.debit(2, appearance=("native-memory-report-runtime-trace", "collector-list"))
    budget.debit(4, appearance=("native-memory-report-runtime-trace", "collector-weak-reference"))
    budget.debit(27, appearance=("native-memory-report-runtime-trace", "collector-registry-entry"))
    collector = _NativeMemoryReportRuntimeTraceCollector(owner, _MEMORY_CHARGE_TRACE.get())
    _REPORT_RUNTIME_TRACE_COLLECTORS[id(collector)] = collector
    budget.debit(4, appearance=("native-memory-report-runtime-trace", "active-context-token"))
    active_token = _REPORT_RUNTIME_TRACE_ACTIVE.set(collector)
    trace_token = None
    try:
        collector._checked()
        budget.debit(4, appearance=("native-memory-report-runtime-trace", "trace-context-token"))
        budget.debit(11, appearance=("native-memory-report-runtime-trace", "trace-binding-tuple"))
        trace_token = _MEMORY_CHARGE_TRACE.set((budget, collector))
        certificate = await budget.certify(db, RUNTIME_COMPOSITION, tuple(DOMAINS), _header_budget=budget)
        collector._checked()
        if (type(certificate) is not HeaderCertificate or certificate._seal is not _SEAL
                or certificate._issued_id != id(certificate) or certificate.descriptor is not RUNTIME_COMPOSITION
                or certificate._db is not db or certificate._transaction is not owner.transaction
                or certificate._driver is not owner.driver or certificate._changes != owner.driver.total_changes
                or certificate.row_ids != tuple(DOMAINS)):
            raise HeaderBoundsError("memory_report_runtime_numeric_certificate_changed")
        retained = 2 + max(0, len(collector.entries) - 1)
        for appearance, amount in collector.entries:
            retained += 3 + _common_trace_label_width(appearance) + 20
        collector._storage_debit(retained, "retained-copy")
        collector._storage_debit(2 + 3 + 4 * 4 + retained, "retained-record")
        collector._storage_debit(27, "retained-registry-entry")
        _REPORT_RUNTIME_NUMERIC[owner] = (certificate, tuple(collector.entries), owner.budget, owner.issuer_task)
        return certificate
    finally:
        if trace_token is not None:
            _MEMORY_CHARGE_TRACE.reset(trace_token)
        _REPORT_RUNTIME_TRACE_ACTIVE.reset(active_token)
        _REPORT_RUNTIME_TRACE_COLLECTORS.pop(id(collector), None)


def original_native_memory_report_runtime_numeric_schedule(operation, prepared, *, certificate, facts, retention_widths):
    """ONE original Runtime14 issuer/read vector; capacity, never permission."""
    from src.runtime_plugins.memory_producer import _native_memory_report_dependency_numeric_inventory
    from src.memory.header_bounds import HeaderBoundsError
    owner = _native_memory_report_dependency_numeric_inventory(operation, prepared,
        certificate=certificate, facts=facts, retention_widths=retention_widths)
    retained = _REPORT_RUNTIME_NUMERIC.get(owner)
    if (retained is None or retained[2] is not owner.budget or retained[3] is not owner.issuer_task
            or retained[0]._db is not owner.db or retained[0]._transaction is not owner.transaction
            or retained[0]._driver is not owner.driver or retained[0]._changes != owner.driver.total_changes):
        raise HeaderBoundsError("memory_report_runtime_numeric_contribution_unavailable")
    return retained[1]


def _native_memory_report_runtime_allocation_upper(stage, *, binding=None):
    from src.memory.composition_headers import _common_trace_label_width
    from src.memory.header_bounds import HeaderBoundsError
    if stage == "inventory":
        names = ("inventory-query", "inventory-list", "inventory-domain-set",
            "inventory-required-set", "inventory-dictionary")
        repeated_name, repetitions = "inventory-dependency", 14
    elif stage == "dependencies" and type(binding) is RuntimeCompositionBinding:
        names = ()
        repeated_name, repetitions = "binding-dependency", len(binding.dependency_vector)
    else:
        raise HeaderBoundsError("memory_report_runtime_numeric_shape_changed")
    # The original grammar's intermediate tuples and each new debit label
    # are constructed only after this precharge.
    planned = 0
    for tag in ("native-memory-authority", "native-memory-report-runtime"):
        original = tag == "native-memory-authority"
        width = 2 + max(0, len(names) + repetitions - 1) if original else 0
        for name in names:
            label = 3 + _common_trace_label_width(tag) + _common_trace_label_width(name)
            width += 3 + label + 20 if original else label
        label = 3 + _common_trace_label_width(tag) + _common_trace_label_width(repeated_name)
        width += repetitions * (3 + label + 20 if original else label)
        planned += width
    return planned


def _native_memory_report_runtime_pay(db, owner, certificate, stage, *, binding=None):
    from src.memory.header_bounds import HeaderBoundsError, HeaderCertificate, RUNTIME_COMPOSITION, _SEAL
    from src.runtime_plugins.memory_producer import _native_memory_authority_entries
    if (_native_memory_report_runtime_owner(db) is not owner
            or type(certificate) is not HeaderCertificate or certificate._seal is not _SEAL
            or certificate._issued_id != id(certificate) or certificate.descriptor is not RUNTIME_COMPOSITION
            or certificate._db is not db or certificate._transaction is not owner.transaction
            or certificate._driver is not owner.driver or certificate._changes != owner.driver.total_changes
            or certificate.row_ids != tuple(DOMAINS)):
        raise HeaderBoundsError("memory_report_runtime_numeric_certificate_changed")
    planned = _native_memory_report_runtime_allocation_upper(stage, binding=binding)
    owner.budget.debit(planned, appearance=("native-memory-report-runtime", "allocation-plan", stage))
    entries = _native_memory_authority_entries(stage, certificate.upper_bytes,
        dependencies=len(binding.dependency_vector) if binding is not None else 0,
        row_uppers=certificate.row_upper_bytes)
    for (_, name), upper in entries:
        owner.budget.debit(upper, appearance=("native-memory-report-runtime", name))


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
            or self.native_branch not in {"base", "artifact", "workflow", "public_research", "programme_discovery", "direct_turn", "generic_turn"}
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
    if native_branch == "artifact" and origin_method in {"capabilities.list", "capabilities.describe", "connections.inspect", "memory.retrieve"}:
        return tuple(sorted({origin_method, "artifacts.stage", "artifacts.adopt", "artifacts.read", "audit.append"}))
    if native_branch == "artifact" and origin_method == "tasks.admit":
        return tuple(sorted({origin_method, "tasks.inspect", "tasks.cancel", "tasks.checkpoint", "tasks.settle",
                             "capabilities.invoke", "artifacts.read", "artifacts.stage", "artifacts.adopt"}))
    if native_branch in {"artifact", "workflow"} and origin_method in {"tasks.admit", "capabilities.invoke"}:
        return tuple(sorted({origin_method, "tasks.inspect", "tasks.cancel", "tasks.checkpoint", "tasks.settle",
                             "artifacts.read", "artifacts.stage", "artifacts.adopt"}))
    if native_branch == "programme_discovery" and origin_method == "research.executeAccepted":
        return tuple(sorted({origin_method, "authority.resolve", "tasks.inspect",
            "source-extraction.extract", "inference.request"}))
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
    fixed = {"memory.propose": {"audit"}, "memory.applyReviewed": {"audit"}, "memory.forget": {"audit"},
             "research.buildPlan": {"artifacts", "inference"},
             "research.executeAccepted": {"capabilities", "inference", "artifacts", "source-extraction"},
             "source-extraction.extract": {"artifacts", "research"},
             "agent-loop.startTurn": {"conversation", "capabilities", "inference", "audit"},
             "agent-loop.cancelTurn": {"conversation"}, "agent-loop.inspectTurn": {"conversation"},
             "conversation.cancel": {"agent-loop"}}
    names.update(fixed.get(method, ()))
    if native_branch == "artifact" and method in {"capabilities.list", "capabilities.describe", "connections.inspect", "memory.retrieve"}:
        names.update({"artifacts", "audit"})
    elif native_branch == "artifact" and method in {"tasks.admit", "tasks.checkpoint", "tasks.settle", "capabilities.invoke", "memory.propose", "memory.applyReviewed"}:
        names.add("artifacts")
    elif native_branch == "workflow" and method.startswith("tasks."):
        pass
    elif native_branch == "programme_discovery":
        names.update({"goals", "research", "artifacts", "inference", "source-extraction"})
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


async def inventory(db, *, header_budget=None):
    certificate = None
    report_owner = _native_memory_report_runtime_owner(db)
    if report_owner is not None:
        from src.memory.header_bounds import HeaderBoundsError
        if header_budget is not None and header_budget is not report_owner.budget:
            raise HeaderBoundsError("memory_report_runtime_numeric_frame_changed")
        header_budget = report_owner.budget
    if header_budget is not None:
        from src.memory.header_bounds import HeaderReadBudget, RUNTIME_COMPOSITION
        if type(header_budget) is not HeaderReadBudget:
            raise CompositionBindingError("composition_header_budget_required")
        if report_owner is not None:
            certificate = await _native_memory_report_runtime_certificate(db, report_owner)
        else:
            certificate = await header_budget.certify(db, RUNTIME_COMPOSITION, tuple(DOMAINS), _header_budget=header_budget)
    from .memory_producer import _native_memory_authority_owner_pay
    _native_memory_authority_owner_pay(db, "inventory", certificate=certificate)
    if report_owner is not None:
        _native_memory_report_runtime_pay(db, report_owner, certificate, "inventory")
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
                          reviewed_composition=None, header_budget=None):
    methods = method_closure(method, native_branch)
    domains = set(method_dependencies(method, native_branch=native_branch,
                                  goal_bound=goal_bound, programme_bound=programme_bound))
    for child in methods:
        domains.update(method_dependencies(child, goal_bound=goal_bound, programme_bound=programme_bound))
    domains = tuple(sorted(domains))
    rows = await inventory(db, header_budget=header_budget)
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


@dataclass(frozen=True)
class InvocationAvailability:
    available: bool
    reason_code: str | None

    def __post_init__(self):
        if type(self.available) is not bool or (self.available, self.reason_code) not in {
            (True, None), (False, "composition_inventory_absent")
        }:
            raise CompositionBindingError("composition_availability_invalid")


async def inspect_invocation_availability(db, *, method, native_branch="base", reviewed_composition):
    """Deny-only pre-ingress availability; never a binding or authority proof."""
    from pathlib import Path
    from sqlalchemy import text
    from config.settings import settings
    from src.runtime_plugins.composition import ReviewedComposition
    from src.workspace.production import (ProductionWorkspace, ProductionWorkspaceError,
        read_lifecycle_receipt, read_accounting_checkpoint)
    methods = method_closure(method, native_branch)
    selected = set(method_dependencies(method, native_branch=native_branch))
    for child in methods:
        selected.update(method_dependencies(child))
    if (not isinstance(reviewed_composition, ReviewedComposition)
        or any(type(value) is not str or _SHA.fullmatch(value) is None
            for value in (reviewed_composition.package_digest, reviewed_composition.composition_digest))):
        raise CompositionBindingError("composition_reviewed_host_required")
    if db.get_bind().dialect.name != "sqlite":
        raise CompositionBindingError("composition_availability_storage_unsupported")
    present = await db.scalar(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_composition_states'"))
    rows = list((await db.execute(select(RuntimeCompositionState))).scalars()) if present else []
    try:
        workspace = ProductionWorkspace(host_root=Path(settings.workspace_dir).resolve())
        receipt = read_lifecycle_receipt(workspace) or {}
        checkpoint = read_accounting_checkpoint(workspace) or {}
    except ProductionWorkspaceError as exc:
        raise CompositionBindingError("composition_availability_metadata_unavailable") from exc
    retained = receipt.get("runtime_composition")
    retained_checkpoint = any(checkpoint.get(key) is not None for key in ("composition_base", "composition_target"))
    if not rows:
        jobs = await db.scalar(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_run_states'"))
        bound = False
        if jobs:
            columns = (await db.execute(text("PRAGMA table_info(workflow_run_states)"))).all()
            if any(row[1] == "composition_binding_json" for row in columns):
                bound = await db.scalar(text("SELECT 1 FROM workflow_run_states WHERE composition_binding_json IS NOT NULL LIMIT 1")) is not None
        if retained is not None or retained_checkpoint or bound:
            raise CompositionBindingError("composition_retained_inventory_missing")
        return InvocationAvailability(False, "composition_inventory_absent")
    # Inspect all fourteen identities/schema, but only selected dependencies
    # gate availability. This produces no dependency vector or cached binding.
    current = await inventory(db)
    values = [{"runtime_domain": row.runtime_domain, "owner_kind": row.owner_kind,
        "epoch": row.epoch, "composition_digest": row.composition_digest,
        "state": row.state, "recovery_receipt_ref": row.recovery_receipt_ref}
        for row in sorted(rows, key=lambda item: item.runtime_domain)]
    if (type(retained) is not dict or retained.get("inventory") != values
        or retained.get("inventory_digest") != hashlib.sha256(_canonical(values).encode()).hexdigest()
        or (checkpoint.get("schema_version") == 2 and checkpoint.get("composition_target") != retained)):
        raise CompositionBindingError("composition_inventory_stale")
    if any(current[domain].state != "ready" for domain in selected):
        raise CompositionBindingError("composition_dependency_unavailable")
    return InvocationAvailability(True, None)


async def validate_invocation(db, binding, *, header_budget=None):
    if not isinstance(binding, RuntimeCompositionBinding):
        raise CompositionBindingError("composition_binding_required")
    rows = await inventory(db, header_budget=header_budget)
    from .memory_producer import _native_memory_authority_owner_pay
    _native_memory_authority_owner_pay(db, "dependencies", binding=binding)
    report_owner = _native_memory_report_runtime_owner(db)
    if report_owner is not None:
        from src.memory.header_bounds import HeaderBoundsError
        retained = _REPORT_RUNTIME_NUMERIC.get(report_owner)
        if retained is None:
            raise HeaderBoundsError("memory_report_runtime_numeric_contribution_unavailable")
        _native_memory_report_runtime_pay(db, report_owner, retained[0], "dependencies", binding=binding)
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


async def begin_native_writer(db, *, owner, fresh=False, header_budget=None, _native_memory_source=None):
    """Private native ingress/maintenance seam, before any Message/row insert."""
    from sqlalchemy import text
    from src.workspace.accounting_witness import prepare_composition_session
    if owner not in {"native_ingress", "composition_maintenance", "durable_jobs", "finite_service"}:
        raise CompositionBindingError("composition_native_writer_required")
    guard = db.info.get("composition_guard")
    if guard is None:
        guard = await prepare_composition_session(db, fresh=fresh, **(
            {"header_budget": header_budget} if header_budget is not None else {}))
    if header_budget is not None and guard is not None:
        from src.workspace.accounting_witness import CompositionSessionGuard
        if (type(guard) is not CompositionSessionGuard or guard.header_budget is not header_budget
                or guard._retention_closed):
            raise CompositionBindingError("composition_native_writer_budget_changed")
    if _native_memory_source is not None:
        from src.runtime_plugins.memory_producer import _validate_memory_completion_capacity_source
        from src.workspace.accounting_witness import CompositionSessionGuard
        source = _validate_memory_completion_capacity_source(
            _native_memory_source, _native_memory_source.claim)
        if (owner != "durable_jobs" or fresh or type(guard) is not CompositionSessionGuard
                or guard.db is not db or guard.header_budget is not header_budget
                or source.admission.header_budget is not header_budget
                or getattr(guard, "_native_memory_source_retention", None) is not source._retention):
            raise CompositionBindingError("native_memory_completion_writer_guard_changed")
    if db.in_transaction():
        raise CompositionBindingError("composition_native_writer_not_fresh")
    await db.execute(text("BEGIN IMMEDIATE"))
    if _native_memory_source is not None:
        # Only capacity enrollment follows the original owner's actual BEGIN;
        # existing current certificate/body/authority checks still run below.
        await source._retention.ledger.bind(db, "completion")
    if header_budget is not None:
        from src.memory.composition_headers import certify_composition_superset
        if _native_memory_source is not None:
            # The genuine completion Source/frame was checked before BEGIN
            # and bound to this original writer above; pay metadata on it too.
            await certify_composition_superset(db, header_budget, _header_budget=header_budget)
        else:
            await certify_composition_superset(db, header_budget)
    db.info["native_writer_started"] = True
    db.info["composition_writer_owner"] = owner
    if header_budget is not None and guard is not None:
        await (await db.connection()).run_sync(
            lambda connection: guard._capture_native_writer_snapshot(connection, header_budget, owner))
    return guard



async def _begin_native_writer_first(db, *, owner, header_budget):
    """Private retained-writer foundation; existing native callers stay unchanged."""
    from src.workspace.accounting_witness import _prepare_composition_writer_session
    return await _prepare_composition_writer_session(db, owner=owner, header_budget=header_budget)


def assert_session_physical_cleanup_allowed(db, session_id):
    """Closed retained membership must be checked before deleting private bytes."""
    if type(session_id) is not str or not session_id or len(session_id.encode()) > 512:
        raise CompositionBindingError("composition_session_cleanup_reference_invalid")
    guard = db.info.get("composition_guard")
    if guard is None:
        if db.info.get("composition_read_guard") is not None:
            raise CompositionBindingError("composition_native_writer_required")
        return  # Existing no-inventory lifecycle remains its current owner.
    if (not db.in_transaction() or not db.info.get("native_writer_started")
        or db.info.get("composition_writer_owner") != "native_ingress"):
        raise CompositionBindingError("composition_native_writer_required")
    if ("sessions", session_id) in guard.members:
        raise CompositionBindingError("composition_retained_session_cleanup_denied")
