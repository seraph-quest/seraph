"""Model-independent capability policy and authority boundary.

This module is deliberately a policy seam, rather than an execution runtime.
Callers provide a typed declaration, an authority issued by a trusted runtime,
and the concrete invocation they want to perform.  The seam intersects the
declaration with the global policy, checks the existing ``seraph.trust.v1``
contract, and returns a redacted decision receipt.  It does not execute tools,
open files, resolve credentials, or grant authority to model output.

The authority constructors in this module are convenience builders for trusted
callers.  They are not an authentication mechanism.  Production callers must
bind them to the authenticated ingress/service principal and the existing
approval repository before crossing an effect boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import ipaddress
import math
from pathlib import Path
import posixpath
import re
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from src.security.context_scan import scan_text_for_suspicious_context
from src.security.site_policy import evaluate_site_access
from src.security.trust_contract import (
    ApprovalBinding,
    AuditBinding,
    AuthorityGrant,
    ContentOrigin,
    DecisionEffect,
    DestinationClass,
    EgressClass,
    NO_OBJECT,
    NO_RESOURCE_LIMITS,
    NO_SECRET_SCOPE,
    NO_TRANSFORMATION,
    PrincipalType,
    RecoveryBinding,
    TrustDecision,
    TrustDestination,
    TrustOperation,
    TrustPrincipal,
    TrustProvenance,
    TrustRequest,
    TrustResource,
    authority_scope_digest,
    bind_approval,
    canonical_digest,
    evaluate_trust,
    is_canonical_digest,
)


CAPABILITY_POLICY_SCHEMA_VERSION = "seraph.capability-policy.v1"
_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,255}$")
_EGRESS_RANK = {
    EgressClass.LOCAL_ONLY: 0,
    EgressClass.CLOUD_ALLOWED_REDACTED: 1,
    EgressClass.CLOUD_ALLOWED_FULL: 2,
}
_DEFAULT_PORT_BY_SCHEME = {
    "http": 80,
    "https": 443,
}


def _value(value: object) -> str:
    return value.value if isinstance(value, Enum) else str(value)


def _enum(enum_type: type[Enum], value: object) -> Enum | None:
    try:
        return enum_type(_value(value))
    except (TypeError, ValueError):
        return None


def _strings(values: Iterable[object] | object | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    if not isinstance(values, Iterable):
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            continue
        item = raw.strip()
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return tuple(result)


def _valid_reference(value: object) -> bool:
    return isinstance(value, str) and bool(_REFERENCE_RE.fullmatch(value))


@dataclass(frozen=True)
class ResourceLimits:
    """Finite resource limits required at the capability effect boundary."""

    cpu_seconds: float | None = None
    memory_bytes: int | None = None
    pid_count: int | None = None
    output_bytes: int | None = None
    deadline_seconds: float | None = None

    @property
    def max_cpu_seconds(self) -> float | None:
        return self.cpu_seconds

    @property
    def max_memory_bytes(self) -> int | None:
        return self.memory_bytes

    @property
    def max_processes(self) -> int | None:
        return self.pid_count

    @property
    def max_output_bytes(self) -> int | None:
        return self.output_bytes

    @property
    def max_wall_seconds(self) -> float | None:
        return self.deadline_seconds

    def validation_reason(self) -> str | None:
        for value, reason in (
            (self.cpu_seconds, "cpu_limit_invalid"),
            (self.memory_bytes, "memory_limit_invalid"),
            (self.pid_count, "pid_limit_invalid"),
            (self.output_bytes, "output_limit_invalid"),
            (self.deadline_seconds, "deadline_limit_invalid"),
        ):
            if value is None:
                return "resource_limits_missing"
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return reason
            if not math.isfinite(float(value)) or value <= 0:
                return reason
        return None

    def as_dict(self) -> dict[str, int | float | None]:
        return {
            "cpu_seconds": self.cpu_seconds,
            "memory_bytes": self.memory_bytes,
            "pid_count": self.pid_count,
            "output_bytes": self.output_bytes,
            "deadline_seconds": self.deadline_seconds,
        }

    def digest(self) -> str:
        return canonical_digest(self.as_dict())

    def intersect(self, other: "ResourceLimits") -> "ResourceLimits":
        """Return the most restrictive finite value for every resource."""
        return ResourceLimits(
            cpu_seconds=_minimum(self.cpu_seconds, other.cpu_seconds),
            memory_bytes=_minimum(self.memory_bytes, other.memory_bytes),
            pid_count=_minimum(self.pid_count, other.pid_count),
            output_bytes=_minimum(self.output_bytes, other.output_bytes),
            deadline_seconds=_minimum(self.deadline_seconds, other.deadline_seconds),
        )


def _minimum(left: int | float | None, right: int | float | None) -> int | float | None:
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


@dataclass(frozen=True)
class CapabilityScope:
    """Allowlisted operation, data, destination, and credential scope."""

    operations: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    egress_class: EgressClass | str | None = None
    secret_scopes: tuple[str, ...] = ()
    network_hosts: tuple[str, ...] = ()
    network_paths: tuple[str, ...] = ()
    credential_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "operations", _strings(self.operations))
        object.__setattr__(self, "paths", _strings(self.paths))
        object.__setattr__(self, "sources", _strings(self.sources))
        object.__setattr__(self, "secret_scopes", _strings(self.secret_scopes))
        object.__setattr__(self, "network_hosts", _strings(self.network_hosts))
        object.__setattr__(self, "network_paths", _strings(self.network_paths))
        object.__setattr__(self, "credential_fields", _strings(self.credential_fields))

    @property
    def allowed_operations(self) -> tuple[str, ...]:
        return self.operations

    @property
    def allowed_paths(self) -> tuple[str, ...]:
        return self.paths

    @property
    def allowed_sources(self) -> tuple[str, ...]:
        return self.sources

    @property
    def allowed_egress(self) -> EgressClass | str | None:
        return self.egress_class

    @property
    def allowed_secret_scopes(self) -> tuple[str, ...]:
        return self.secret_scopes

    @property
    def allowed_network_hosts(self) -> tuple[str, ...]:
        return self.network_hosts

    @property
    def allowed_credential_fields(self) -> tuple[str, ...]:
        return self.credential_fields

    def as_dict(self) -> dict[str, object]:
        return {
            "operations": list(self.operations),
            "paths": list(self.paths),
            "sources": list(self.sources),
            "egress_class": _value(self.egress_class) if self.egress_class is not None else None,
            "secret_scopes": list(self.secret_scopes),
            "network_hosts": list(self.network_hosts),
            "network_paths": list(self.network_paths),
            "credential_fields": list(self.credential_fields),
        }

    def digest(self) -> str:
        return canonical_digest(self.as_dict())


@dataclass(frozen=True)
class CapabilityPolicy:
    """A capability declaration bound to one owner and version."""

    capability_id: str
    capability_version: str
    owner_id: str
    scope: CapabilityScope
    resource_limits: ResourceLimits | None
    principal_type: PrincipalType | str | None = None
    goal_id: str = ""
    grant_digest: str = ""
    approval_digest: str = ""
    requires_approval: bool = False
    requires_audit: bool = False
    revoked: bool = False
    expires_at: float | None = None
    policy_version: str = CAPABILITY_POLICY_SCHEMA_VERSION

    @property
    def version(self) -> str:
        return self.capability_version


@dataclass(frozen=True)
class GlobalCapabilityPolicy:
    """Explicit global ceiling. Empty allowlists deny all matching calls."""

    scope: CapabilityScope
    resource_limits: ResourceLimits | None
    allowed_capabilities: tuple[str, ...] = ()
    allowed_versions: tuple[str, ...] = ()
    revoked: bool = False
    expires_at: float | None = None
    policy_version: str = CAPABILITY_POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_capabilities", _strings(self.allowed_capabilities))
        object.__setattr__(self, "allowed_versions", _strings(self.allowed_versions))


GlobalPolicy = GlobalCapabilityPolicy


@dataclass(frozen=True)
class CapabilityAuthority:
    """Trusted-runtime grant for one exact owner/capability execution scope."""

    principal: TrustPrincipal
    owner_id: str
    capability_id: str
    capability_version: str
    scope: CapabilityScope
    resource_limits: ResourceLimits | None
    session_id: str
    job_id: str
    goal_id: str = ""
    grant_digest: str = ""
    approval_digest: str = ""
    issued_at: float = 0.0
    expires_at: float | None = None
    revoked: bool = False
    approval: ApprovalBinding | None = None


@dataclass(frozen=True)
class ClassifiedContent:
    """External observation/document content carried as data only."""

    source_id: str
    data_digest: str
    data_class: EgressClass | str = EgressClass.LOCAL_ONLY
    kind: str = "observation"
    finding_codes: tuple[str, ...] = ()
    instruction_authority: bool = False
    origin: ContentOrigin = ContentOrigin.EXTERNAL_UNTRUSTED

    def __post_init__(self) -> None:
        findings = list(_strings(self.finding_codes))
        if self.instruction_authority and "instruction_authority_claim" not in findings:
            findings.append("instruction_authority_claim")
        object.__setattr__(self, "finding_codes", tuple(findings))
        # External content is data regardless of what it claims.  Preserve the
        # attempted claim as a deterministic finding so the gate can reject it
        # without allowing an observation or memory entry to become authority.
        object.__setattr__(self, "instruction_authority", False)
        object.__setattr__(self, "origin", ContentOrigin.EXTERNAL_UNTRUSTED)

    @property
    def prompt_injection_detected(self) -> bool:
        return bool(self.finding_codes)

    def as_provenance(self) -> TrustProvenance:
        return TrustProvenance(
            origin=self.origin,
            source_id=self.source_id,
            data_digest=self.data_digest,
            egress_class=self.data_class,
            instruction_authority=False,
        )

    def as_receipt(self) -> dict[str, object]:
        return {
            "source_digest": canonical_digest({"source_id": self.source_id}),
            "data_digest": self.data_digest,
            "data_class": _value(self.data_class),
            "kind": self.kind,
            "finding_codes": list(self.finding_codes),
            "instruction_authority": False,
            "instruction_authority_claimed": "instruction_authority_claim" in self.finding_codes,
        }


ContentClassification = ClassifiedContent


def classify_untrusted_content(
    content: str,
    *,
    source_id: str,
    data_class: EgressClass | str = EgressClass.LOCAL_ONLY,
    kind: str = "observation",
) -> ClassifiedContent:
    """Classify hostile observations/documents without promoting their text."""
    findings = scan_text_for_suspicious_context(str(content or ""), include_fenced_blocks=True)
    return ClassifiedContent(
        source_id=source_id,
        data_digest=canonical_digest({"content": str(content or "")}),
        data_class=data_class,
        kind=kind,
        finding_codes=tuple(finding.code for finding in findings),
    )


def capability_grant_digest(authority: CapabilityAuthority) -> str:
    """Digest the complete grant metadata without raw payloads or credentials."""
    return canonical_digest(
        {
            "owner_id": authority.owner_id,
            "principal_id": authority.principal.principal_id,
            "principal_type": _value(authority.principal.principal_type),
            "capability_id": authority.capability_id,
            "capability_version": authority.capability_version,
            "scope": authority.scope.as_dict(),
            "resource_limits": authority.resource_limits.as_dict()
            if authority.resource_limits is not None
            else None,
            "session_id": authority.session_id,
            "job_id": authority.job_id,
            "goal_id": authority.goal_id,
            "issued_at": authority.issued_at,
            "expires_at": authority.expires_at,
        }
    )


def approval_binding_digest(approval: ApprovalBinding) -> str:
    """Digest an existing trust approval's binding fields."""
    return canonical_digest(
        {
            "approval_id": approval.approval_id,
            "request_digest": approval.request_digest,
            "policy_version": approval.policy_version,
            "destination_digest": approval.destination_digest,
            "capability_id": approval.capability_id,
            "capability_version": approval.capability_version,
            "data_digest": approval.data_digest,
            "secret_scope_digest": approval.secret_scope_digest,
            "resource_limits_digest": approval.resource_limits_digest,
            "transformation_digest": approval.transformation_digest,
            "authority_scope_digest": approval.authority_scope_digest,
            "resource_digest": approval.resource_digest,
            "session_id": approval.session_id,
            "job_id": approval.job_id,
            "request_id": approval.request_id,
            "attempt_id": approval.attempt_id,
            "replay_id": approval.replay_id,
            "decision_expires_at": approval.decision_expires_at,
            "expires_at": approval.expires_at,
            "consumed": approval.consumed,
        }
    )


def issue_capability_authority(
    policy: CapabilityPolicy,
    principal: TrustPrincipal,
    *,
    session_id: str,
    job_id: str,
    now: float,
    goal_id: str = "",
    expires_at: float | None = None,
) -> CapabilityAuthority:
    """Build a grant from an already authenticated principal and declaration."""
    authority = CapabilityAuthority(
        principal=principal,
        owner_id=policy.owner_id,
        capability_id=policy.capability_id,
        capability_version=policy.capability_version,
        scope=policy.scope,
        resource_limits=policy.resource_limits,
        session_id=session_id,
        job_id=job_id,
        goal_id=goal_id or policy.goal_id,
        issued_at=float(now),
        expires_at=expires_at if expires_at is not None else policy.expires_at,
    )
    return CapabilityAuthority(
        **{
            **authority.__dict__,
            "grant_digest": capability_grant_digest(authority),
        }
    )


@dataclass(frozen=True)
class CapabilityEnvelope:
    """Concrete invocation plus untrusted data and exact effect metadata."""

    capability_id: str
    capability_version: str
    operation: str
    authority: CapabilityAuthority | None
    session_id: str
    job_id: str
    request_id: str
    attempt_id: str
    replay_id: str
    data_digest: str
    resource_limits: ResourceLimits | None
    deadline_at: float | None
    owner_id: str = ""
    goal_id: str = ""
    path: str = ""
    source_id: str = ""
    egress_class: EgressClass | str | None = None
    secret_scopes: tuple[str, ...] = ()
    network_url: str = ""
    credential_field: str = ""
    resource_type: str = "capability"
    resource_id: str = ""
    object_digest: str | object = NO_OBJECT
    provenance: tuple[TrustProvenance, ...] = ()
    content: tuple[ClassifiedContent, ...] = ()
    audit: AuditBinding = field(default_factory=AuditBinding)
    principal: TrustPrincipal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "secret_scopes", _strings(self.secret_scopes))
        object.__setattr__(self, "provenance", tuple(self.provenance or ()))
        object.__setattr__(self, "content", tuple(self.content or ()))

    @property
    def request_identity(self) -> "CapabilityRequestIdentity":
        return CapabilityRequestIdentity(
            owner_id=self.owner_id,
            goal_id=self.goal_id,
            session_id=self.session_id,
            job_id=self.job_id,
            request_id=self.request_id,
            attempt_id=self.attempt_id,
            replay_id=self.replay_id,
        )

    @property
    def destination_digest(self) -> str:
        return canonical_digest({"network": _network_metadata(self.network_url)})

    @property
    def resource_digest(self) -> str:
        return canonical_digest(
            {
                "resource_type": self.resource_type,
                "resource_id": self.resource_id,
                "object_digest": self.object_digest,
            }
        )


    @classmethod
    def create(
        cls,
        *,
        authority: CapabilityAuthority,
        operation: str,
        resource_limits: ResourceLimits,
        deadline_at: float,
        now: float,
        path: str = "",
        source_id: str = "source:operator",
        egress_class: EgressClass | str = EgressClass.LOCAL_ONLY,
        secret_scopes: Iterable[str] = (),
        network_url: str = "",
        credential_field: str = "",
        goal_id: str = "",
        resource_type: str = "capability",
        resource_id: str = "",
        provenance: tuple[TrustProvenance, ...] = (),
        content: tuple[ClassifiedContent, ...] = (),
    ) -> "CapabilityEnvelope":
        scope_values = tuple(secret_scopes)
        resolved_resource_id = resource_id or authority.capability_id
        digest = canonical_digest(
            {
                "capability_id": authority.capability_id,
                "capability_version": authority.capability_version,
                "operation": operation,
                "path": path,
                "source_id": source_id,
                "network_url": _network_metadata(network_url),
                "credential_field": credential_field,
                "secret_scope": list(scope_values),
            }
        )
        actual_provenance = provenance or (
            TrustProvenance(
                origin=ContentOrigin.OPERATOR_INPUT,
                source_id=source_id,
                data_digest=digest,
                egress_class=egress_class,
                instruction_authority=False,
            ),
        )
        return cls(
            capability_id=authority.capability_id,
            capability_version=authority.capability_version,
            operation=operation,
            authority=authority,
            session_id=authority.session_id,
            job_id=authority.job_id,
            request_id=f"request:{uuid4().hex}",
            attempt_id=f"attempt:{uuid4().hex}",
            replay_id=f"replay:{uuid4().hex}",
            data_digest=digest,
            resource_limits=resource_limits,
            deadline_at=deadline_at,
            owner_id=authority.owner_id,
            goal_id=goal_id or authority.goal_id,
            path=path,
            source_id=source_id,
            egress_class=egress_class,
            secret_scopes=scope_values,
            network_url=network_url,
            credential_field=credential_field,
            resource_type=resource_type,
            resource_id=resolved_resource_id,
            provenance=actual_provenance,
            content=content,
            principal=authority.principal,
        )


@dataclass(frozen=True)
class CapabilityRequestIdentity:
    """Typed owner/job/attempt identity bound to one capability request."""

    owner_id: str
    goal_id: str
    session_id: str
    job_id: str
    request_id: str
    attempt_id: str
    replay_id: str

    def as_dict(self) -> dict[str, str]:
        return {
            "owner_id": self.owner_id,
            "goal_id": self.goal_id,
            "session_id": self.session_id,
            "job_id": self.job_id,
            "request_id": self.request_id,
            "attempt_id": self.attempt_id,
            "replay_id": self.replay_id,
        }


RequestIdentity = CapabilityRequestIdentity


CapabilityRequest = CapabilityEnvelope


@dataclass(frozen=True)
class BoundaryCheck:
    allowed: bool
    reason_code: str
    normalized: str = ""
    matched_scope: str = ""


def _path_has_symlink(path: Path) -> bool:
    current = path
    while True:
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
        if current == current.parent:
            return False
        current = current.parent


def filesystem_path_allowed(
    path: str,
    allowed_paths: Iterable[str],
    *,
    allow_symlinks: bool = False,
) -> BoundaryCheck:
    """Check an absolute path against declared roots without following links."""
    if not isinstance(path, str) or not path.strip():
        return BoundaryCheck(False, "filesystem_path_missing")
    candidate = Path(path)
    if not candidate.is_absolute():
        return BoundaryCheck(False, "filesystem_path_relative")
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return BoundaryCheck(False, "filesystem_path_unresolvable")
    if not allow_symlinks and _path_has_symlink(candidate):
        return BoundaryCheck(False, "filesystem_symlink_blocked", str(resolved))
    for raw_root in _strings(allowed_paths):
        root = Path(raw_root)
        if not root.is_absolute():
            continue
        try:
            root_resolved = root.resolve(strict=False)
            resolved.relative_to(root_resolved)
        except (OSError, RuntimeError, ValueError):
            continue
        if not allow_symlinks and _path_has_symlink(root):
            continue
        return BoundaryCheck(True, "filesystem_path_allowed", str(resolved), str(root_resolved))
    return BoundaryCheck(False, "filesystem_path_not_allowlisted", str(resolved))


def _normalize_host(host: str) -> str:
    value = host.strip().lower().rstrip(".")
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError:
        return value


def _host_matches(host: str, rule: str) -> bool:
    candidate = _normalize_host(host)
    normalized = _normalize_host(rule)
    if not normalized or normalized == "*":
        return False
    if normalized.startswith("*."):
        normalized = normalized[2:]
        return candidate.endswith(f".{normalized}") and candidate != normalized
    return candidate == normalized


def _private_host(host: str) -> bool:
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return False
    return parsed.is_private or parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified


def network_target_allowed(
    url: str,
    allowed_hosts: Iterable[str],
    *,
    allowed_paths: Iterable[str] = (),
    allowed_schemes: Iterable[str] = ("https",),
    allow_private: bool = False,
    resolve_dns: bool = True,
) -> BoundaryCheck:
    """Authorize a normalized HTTP(S) target before any connector request."""
    if not isinstance(url, str) or not url.strip():
        return BoundaryCheck(False, "network_destination_missing")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except (ValueError, UnicodeError):
        return BoundaryCheck(False, "network_destination_invalid")
    if not parsed.scheme or not host or parsed.username or parsed.password:
        return BoundaryCheck(False, "network_destination_invalid")
    scheme = parsed.scheme.lower()
    if scheme not in {str(item).lower() for item in allowed_schemes}:
        return BoundaryCheck(False, "network_scheme_not_allowlisted")
    normalized_host = _normalize_host(host)
    if _private_host(normalized_host) and not allow_private:
        return BoundaryCheck(False, "network_private_destination_blocked", normalized_host)
    matched_rule = next((rule for rule in _strings(allowed_hosts) if _host_matches(normalized_host, rule)), "")
    if not matched_rule:
        return BoundaryCheck(False, "network_host_not_allowlisted", normalized_host)
    path = unquote(parsed.path or "/")
    if "\x00" in path:
        return BoundaryCheck(False, "network_path_invalid", normalized_host)
    normalized_path = posixpath.normpath(path)
    if path.startswith("/") and not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"
    if any(part == ".." for part in path.split("/")):
        return BoundaryCheck(False, "network_path_traversal", normalized_host)
    path_rules = _strings(allowed_paths)
    if path_rules and not any(
        normalized_path == rule.rstrip("/") or normalized_path.startswith(f"{rule.rstrip('/')}/")
        for rule in path_rules
    ):
        return BoundaryCheck(False, "network_path_not_allowlisted", normalized_host)
    if port is not None:
        if not (1 <= port <= 65535):
            return BoundaryCheck(False, "network_port_invalid", normalized_host)
        # CapabilityScope currently declares hosts and paths, but has no port
        # field. Treat an explicit non-default port as an undeclared
        # destination instead of silently widening a host-only grant to another
        # service on the same host. Reject it before DNS/site-policy work so a
        # denied destination causes no resolver side effect.
        if port != _DEFAULT_PORT_BY_SCHEME.get(scheme):
            return BoundaryCheck(False, "network_port_not_allowlisted", normalized_host)
    site = evaluate_site_access(url, resolve_dns=resolve_dns)
    if not site.allowed:
        return BoundaryCheck(False, f"network_site_policy_{site.reason or 'blocked'}", normalized_host)
    if resolve_dns and not _private_host(normalized_host) and not site.resolved_addresses:
        # A hostname with no authoritative resolution cannot be safely pinned.
        # Literal public addresses are accepted when the local resolver returns
        # their address through site_policy.
        return BoundaryCheck(False, "network_destination_unresolved", normalized_host)
    return BoundaryCheck(True, "network_target_allowed", f"{scheme}://{normalized_host}{normalized_path}", matched_rule)


def credential_scope_allowed(
    *,
    field: str,
    destination_url: str,
    secret_scopes: Iterable[str],
    allowed_fields: Iterable[str],
    allowed_secret_scopes: Iterable[str],
    allowed_hosts: Iterable[str],
    allowed_paths: Iterable[str] = (),
) -> BoundaryCheck:
    """Authorize final-field credential injection through the network gate."""
    if not _strings(secret_scopes):
        return BoundaryCheck(True, "credential_scope_not_required")
    if not field or field not in _strings(allowed_fields):
        return BoundaryCheck(False, "credential_field_not_allowlisted")
    requested = set(_strings(secret_scopes))
    permitted = set(_strings(allowed_secret_scopes))
    if not requested.issubset(permitted):
        return BoundaryCheck(False, "credential_secret_scope_not_allowlisted")
    return network_target_allowed(
        destination_url,
        allowed_hosts,
        allowed_paths=allowed_paths,
        allowed_schemes=("https",),
        resolve_dns=True,
    )


def intersect_capability_scopes(left: CapabilityScope, right: CapabilityScope) -> CapabilityScope:
    """Compute a restrictive scope intersection with no wildcard expansion."""
    left_egress = _enum(EgressClass, left.egress_class)
    right_egress = _enum(EgressClass, right.egress_class)
    if left_egress is None or right_egress is None:
        egress: EgressClass | str | None = None
    else:
        egress = min((left_egress, right_egress), key=_EGRESS_RANK.__getitem__)
    return CapabilityScope(
        operations=tuple(sorted(set(left.operations) & set(right.operations))),
        paths=_intersect_roots(left.paths, right.paths),
        sources=tuple(sorted(set(left.sources) & set(right.sources))),
        egress_class=egress,
        secret_scopes=tuple(sorted(set(left.secret_scopes) & set(right.secret_scopes))),
        network_hosts=_intersect_hosts(left.network_hosts, right.network_hosts),
        network_paths=_intersect_prefixes(left.network_paths, right.network_paths),
        credential_fields=tuple(sorted(set(left.credential_fields) & set(right.credential_fields))),
    )


def _intersect_roots(left: Iterable[str], right: Iterable[str]) -> tuple[str, ...]:
    result: set[str] = set()
    for raw_left in _strings(left):
        for raw_right in _strings(right):
            try:
                left_path = Path(raw_left).resolve(strict=False)
                right_path = Path(raw_right).resolve(strict=False)
                left_path.relative_to(right_path)
                result.add(str(left_path))
                continue
            except (OSError, RuntimeError, ValueError):
                pass
            try:
                right_path.relative_to(left_path)
                result.add(str(right_path))
            except (OSError, RuntimeError, ValueError):
                continue
    return tuple(sorted(result))


def _intersect_prefixes(left: Iterable[str], right: Iterable[str]) -> tuple[str, ...]:
    result: set[str] = set()
    for raw_left in _strings(left):
        for raw_right in _strings(right):
            l = raw_left.rstrip("/") or "/"
            r = raw_right.rstrip("/") or "/"
            if l == r or l.startswith(f"{r}/"):
                result.add(l)
            elif r.startswith(f"{l}/"):
                result.add(r)
    return tuple(sorted(result))


def _intersect_hosts(left: Iterable[str], right: Iterable[str]) -> tuple[str, ...]:
    result: set[str] = set()
    for l in _strings(left):
        for r in _strings(right):
            if _host_matches(l, r):
                result.add(_normalize_host(l))
            elif _host_matches(r, l):
                result.add(_normalize_host(r))
    return tuple(sorted(result))


def intersect_authority_envelope(
    policy: CapabilityPolicy,
    global_policy: GlobalCapabilityPolicy,
) -> CapabilityPolicy:
    """Return the effective declaration after applying the global ceiling."""
    return CapabilityPolicy(
        capability_id=policy.capability_id,
        capability_version=policy.capability_version,
        owner_id=policy.owner_id,
        scope=intersect_capability_scopes(policy.scope, global_policy.scope),
        resource_limits=(
            policy.resource_limits.intersect(global_policy.resource_limits)
            if policy.resource_limits is not None and global_policy.resource_limits is not None
            else None
        ),
        principal_type=policy.principal_type,
        goal_id=policy.goal_id,
        grant_digest=policy.grant_digest,
        approval_digest=policy.approval_digest,
        requires_approval=policy.requires_approval,
        requires_audit=policy.requires_audit,
        revoked=policy.revoked or global_policy.revoked,
        expires_at=_minimum(policy.expires_at, global_policy.expires_at),
        policy_version=policy.policy_version,
    )


def _network_metadata(url: str) -> dict[str, object] | str:
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
        return {
            "scheme": parsed.scheme.lower(),
            "host_digest": canonical_digest({"host": _normalize_host(parsed.hostname or "")}),
            "port": parsed.port,
            "path_digest": canonical_digest({"path": unquote(parsed.path or "/")}),
        }
    except (ValueError, UnicodeError):
        return {"invalid": True}


def _scope_contains(scope: Iterable[str], value: str, *, kind: str) -> bool:
    values = _strings(scope)
    if kind == "host":
        return any(_host_matches(value, item) for item in values)
    return value in values


def _build_trust_request(envelope: CapabilityEnvelope, effective: CapabilityPolicy) -> TrustRequest:
    authority = envelope.authority
    if authority is None:
        raise ValueError("authority_missing")
    destination_class = DestinationClass.MANAGED_CONNECTOR if envelope.network_url else DestinationClass.LOCAL_RUNTIME
    destination = TrustDestination(
        destination_id=(
            _normalize_host(urlsplit(envelope.network_url).hostname or "")
            if envelope.network_url
            else f"capability:{envelope.capability_id}"
        ),
        destination_class=destination_class,
        endpoint=envelope.network_url,
    )
    resource = TrustResource(
        resource_type=envelope.resource_type,
        resource_id=envelope.resource_id,
        object_digest=envelope.object_digest,
    )
    secret_digest = (
        canonical_digest({"secret_scopes": sorted(envelope.secret_scopes)})
        if envelope.secret_scopes
        else NO_SECRET_SCOPE
    )
    # ``TrustRequest.data_digest`` is the exact invocation binding.  Keep the
    # caller's content digest inside it, but also bind every scope-bearing
    # value that can change the effect.  This makes an approval for one path,
    # source, destination, or credential field fail closed if a caller drifts
    # any of those values before execution.
    bound_data_digest = canonical_digest(
        {
            "declared_data_digest": envelope.data_digest,
            "path": envelope.path,
            "source_id": envelope.source_id,
            "network": _network_metadata(envelope.network_url),
            "credential_field": envelope.credential_field,
            "secret_scope_digest": secret_digest,
        }
    )
    limits_digest = envelope.resource_limits.digest() if envelope.resource_limits is not None else NO_RESOURCE_LIMITS
    scope_digest = authority_scope_digest(
        required_grant=AuthorityGrant.CAPABILITY_EXECUTE,
        capability_id=envelope.capability_id,
        destination=destination,
        resource=resource,
    )
    provenance = envelope.provenance
    if envelope.content:
        provenance = (*provenance, *(item.as_provenance() for item in envelope.content))
    audit = envelope.audit
    if effective.requires_audit and not audit.required:
        audit = AuditBinding(
            receipt_id=audit.receipt_id,
            required=True,
            persisted=audit.persisted,
            durable=audit.durable,
        )
    return TrustRequest(
        principal=authority.principal,
        provenance=tuple(provenance),
        destination=destination,
        operation=TrustOperation.CAPABILITY_CALL,
        required_grant=AuthorityGrant.CAPABILITY_EXECUTE,
        capability_id=envelope.capability_id,
        capability_version=envelope.capability_version,
        data_digest=bound_data_digest,
        secret_scope_digest=secret_digest,
        resource_limits_digest=limits_digest,
        transformation_digest=NO_TRANSFORMATION,
        authority_scope_digest=scope_digest,
        resource=resource,
        session_id=envelope.session_id,
        job_id=envelope.job_id,
        request_id=envelope.request_id,
        attempt_id=envelope.attempt_id,
        replay_id=envelope.replay_id,
        decision_expires_at=envelope.deadline_at or 0.0,
        egress_class=envelope.egress_class,
        policy_version="seraph.trust.v1",
        approval_required=effective.requires_approval,
        approval=authority.approval,
        audit=audit,
        recovery=RecoveryBinding(),
    )


@dataclass(frozen=True)
class CapabilityDecision:
    allowed: bool
    effect: DecisionEffect
    reason_code: str
    request_digest: str
    decision_id: str
    effective_policy: CapabilityPolicy | None
    receipt: dict[str, object]
    trust_decision: TrustDecision | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "effect": self.effect.value,
            "reason_code": self.reason_code,
            "request_digest": self.request_digest,
            "decision_id": self.decision_id,
            "receipt": self.receipt,
        }


def _decision(
    envelope: CapabilityEnvelope,
    *,
    reason: str,
    effect: DecisionEffect = DecisionEffect.DENY,
    effective_policy: CapabilityPolicy | None = None,
    trust_decision: TrustDecision | None = None,
) -> CapabilityDecision:
    request_digest = canonical_digest(_envelope_metadata(envelope))
    decision_id = f"cap_{canonical_digest({'request': request_digest, 'reason': reason})[:24]}"
    receipt = redacted_capability_receipt(
        envelope,
        allowed=effect is DecisionEffect.ALLOW,
        effect=effect,
        reason_code=reason,
        request_digest=request_digest,
        decision_id=decision_id,
        effective_policy=effective_policy,
    )
    return CapabilityDecision(
        allowed=effect is DecisionEffect.ALLOW,
        effect=effect,
        reason_code=reason,
        request_digest=request_digest,
        decision_id=decision_id,
        effective_policy=effective_policy,
        receipt=receipt,
        trust_decision=trust_decision,
    )


def _envelope_metadata(envelope: CapabilityEnvelope) -> dict[str, object]:
    authority = envelope.authority
    return {
        "schema_version": CAPABILITY_POLICY_SCHEMA_VERSION,
        "capability_id": envelope.capability_id,
        "capability_version": envelope.capability_version,
        "operation": envelope.operation,
        "owner_id": envelope.owner_id,
        "goal_id": envelope.goal_id,
        "session_id": envelope.session_id,
        "job_id": envelope.job_id,
        "request_id": envelope.request_id,
        "attempt_id": envelope.attempt_id,
        "replay_id": envelope.replay_id,
        "data_digest": envelope.data_digest,
        "path_digest": canonical_digest({"path": envelope.path}),
        "source_digest": canonical_digest({"source_id": envelope.source_id}),
        "egress_class": _value(envelope.egress_class),
        "secret_scope_digest": canonical_digest({"secret_scopes": sorted(envelope.secret_scopes)}),
        "network": _network_metadata(envelope.network_url),
        "credential_field": envelope.credential_field,
        "resource_type": envelope.resource_type,
        "resource_id": envelope.resource_id,
        "object_digest": envelope.object_digest,
        "limits_digest": envelope.resource_limits.digest() if envelope.resource_limits else None,
        "deadline_at": envelope.deadline_at,
        "authority_grant_digest": authority.grant_digest if authority else "",
        "authority_approval_digest": authority.approval_digest if authority else "",
        "audit": {
            "receipt_id": envelope.audit.receipt_id,
            "required": envelope.audit.required,
            "persisted": envelope.audit.persisted,
            "durable": envelope.audit.durable,
        },
        "content": [item.as_receipt() for item in envelope.content],
    }


def redacted_capability_receipt(
    envelope: CapabilityEnvelope,
    *,
    allowed: bool,
    effect: DecisionEffect,
    reason_code: str,
    request_digest: str,
    decision_id: str,
    effective_policy: CapabilityPolicy | None,
) -> dict[str, object]:
    """Build a receipt that never includes payload, path, URL, or secret values."""
    authority = envelope.authority
    return {
        "schema_version": CAPABILITY_POLICY_SCHEMA_VERSION,
        "allowed": allowed,
        "effect": effect.value,
        "reason_code": reason_code,
        "request_digest": request_digest,
        "decision_id": decision_id,
        "degradation": (
            "none"
            if effect is DecisionEffect.ALLOW
            else "approval_required"
            if effect is DecisionEffect.REQUIRE_APPROVAL
            else "blocked_before_effect"
        ),
        "capability_id": envelope.capability_id,
        "capability_version": envelope.capability_version,
        "principal": {
            "principal_id_digest": canonical_digest(
                {"principal_id": authority.principal.principal_id}
            )
            if authority is not None
            else None,
            "principal_type": _value(authority.principal.principal_type) if authority else None,
            "authenticated": bool(authority and authority.principal.authenticated),
            "revoked": bool(authority and (authority.principal.revoked or authority.revoked)),
        },
        "scope": {
            "operation": envelope.operation,
            "path_digest": canonical_digest({"path": envelope.path}),
            "source_digest": canonical_digest({"source_id": envelope.source_id}),
            "egress_class": _value(envelope.egress_class),
            "secret_scope_digest": canonical_digest({"secret_scopes": sorted(envelope.secret_scopes)}),
            "network_host_digest": (
                canonical_digest({"host": _normalize_host(urlsplit(envelope.network_url).hostname or "")})
                if envelope.network_url
                else None
            ),
            "credential_field": envelope.credential_field,
        },
        "identity": {
            "owner_digest": canonical_digest({"owner_id": envelope.owner_id}),
            "goal_digest": canonical_digest({"goal_id": envelope.goal_id}),
            "session_digest": canonical_digest({"session_id": envelope.session_id}),
            "job_digest": canonical_digest({"job_id": envelope.job_id}),
            "request_id": envelope.request_id,
            "attempt_id": envelope.attempt_id,
            "replay_id": envelope.replay_id,
        },
        "authority": {
            "grant_digest": authority.grant_digest if authority else "",
            "approval_digest": authority.approval_digest if authority else "",
            "approval_id_digest": (
                canonical_digest({"approval_id": authority.approval.approval_id})
                if authority is not None and authority.approval is not None
                else None
            ),
        },
        "destination_digest": envelope.destination_digest,
        "resource_digest": envelope.resource_digest,
        "audit": {
            "receipt_id_digest": (
                canonical_digest({"receipt_id": envelope.audit.receipt_id})
                if envelope.audit.receipt_id
                else None
            ),
            "required": envelope.audit.required
            or bool(effective_policy and effective_policy.requires_audit),
            "persisted": envelope.audit.persisted,
            "durable": envelope.audit.durable,
        },
        "resource_limits": {
            "requested_digest": envelope.resource_limits.digest() if envelope.resource_limits else None,
            "effective_digest": (
                effective_policy.resource_limits.digest()
                if effective_policy is not None and effective_policy.resource_limits is not None
                else None
            ),
            "deadline_at": envelope.deadline_at,
        },
        "content": {
            "classified_count": len(envelope.content),
            "prompt_injection_detected": any(item.prompt_injection_detected for item in envelope.content),
            "instruction_authority": False,
            "raw_content_stored": False,
            "secret_values_stored": False,
        },
        "recovery": "operator_review_or_fresh_authority",
    }


def _scope_reason(envelope: CapabilityEnvelope, scope: CapabilityScope) -> str | None:
    operation = str(envelope.operation or "")
    if not operation or operation not in scope.operations:
        return "operation_not_allowlisted"
    if envelope.path and not scope.paths:
        return "filesystem_path_not_allowlisted"
    if scope.paths and not filesystem_path_allowed(envelope.path, scope.paths).allowed:
        return filesystem_path_allowed(envelope.path, scope.paths).reason_code
    if scope.sources:
        if not envelope.source_id:
            return "source_missing"
        if not _scope_contains(scope.sources, envelope.source_id, kind="source"):
            return "source_not_allowlisted"
    elif envelope.source_id:
        return "source_not_allowlisted"
    requested_egress = _enum(EgressClass, envelope.egress_class)
    permitted_egress = _enum(EgressClass, scope.egress_class)
    if requested_egress is None:
        return "egress_class_missing"
    if permitted_egress is None or _EGRESS_RANK[requested_egress] > _EGRESS_RANK[permitted_egress]:
        return "egress_scope_exceeded"
    return None


def _identity_reason(envelope: CapabilityEnvelope, policy: CapabilityPolicy) -> str | None:
    authority = envelope.authority
    if authority is None:
        return "authority_missing"
    if not authority.owner_id or not envelope.owner_id:
        return "owner_missing"
    if authority.owner_id != policy.owner_id or envelope.owner_id != authority.owner_id:
        return "owner_mismatch"
    if authority.capability_id != policy.capability_id or envelope.capability_id != authority.capability_id:
        return "capability_mismatch"
    if authority.capability_version != policy.capability_version or envelope.capability_version != authority.capability_version:
        return "capability_version_mismatch"
    if envelope.session_id != authority.session_id or envelope.job_id != authority.job_id:
        return "execution_identity_mismatch"
    if authority.principal.session_id != authority.session_id or authority.principal.job_id != authority.job_id:
        return "principal_identity_mismatch"
    if envelope.goal_id != authority.goal_id or (policy.goal_id and envelope.goal_id != policy.goal_id):
        return "goal_mismatch"
    if envelope.principal is not None and envelope.principal != authority.principal:
        return "principal_mismatch"
    expected_grant = capability_grant_digest(authority)
    if not authority.grant_digest or authority.grant_digest != expected_grant:
        return "grant_digest_invalid"
    if policy.grant_digest and authority.grant_digest != policy.grant_digest:
        return "grant_digest_mismatch"
    if authority.approval is not None:
        if not authority.approval_digest or authority.approval_digest != approval_binding_digest(authority.approval):
            return "approval_digest_invalid"
    if policy.approval_digest and authority.approval_digest != policy.approval_digest:
        return "approval_digest_mismatch"
    return None


def _limits_reason(
    envelope: CapabilityEnvelope,
    effective: CapabilityPolicy,
    *,
    now: float,
) -> str | None:
    policy_limits = effective.resource_limits
    requested_limits = envelope.resource_limits
    if policy_limits is None or requested_limits is None:
        return "resource_limits_missing"
    if policy_limits.validation_reason() or requested_limits.validation_reason():
        return policy_limits.validation_reason() or requested_limits.validation_reason()
    for requested, permitted in (
        (requested_limits.cpu_seconds, policy_limits.cpu_seconds),
        (requested_limits.memory_bytes, policy_limits.memory_bytes),
        (requested_limits.pid_count, policy_limits.pid_count),
        (requested_limits.output_bytes, policy_limits.output_bytes),
        (requested_limits.deadline_seconds, policy_limits.deadline_seconds),
    ):
        if requested is None or permitted is None or requested > permitted:
            return "resource_limit_exceeded"
    if envelope.deadline_at is None:
        return "deadline_missing"
    if envelope.deadline_at <= now:
        return "deadline_expired"
    if envelope.deadline_at > now + float(requested_limits.deadline_seconds):
        return "deadline_exceeds_limit"
    return None


def authorize_capability(
    envelope: CapabilityEnvelope,
    policy: CapabilityPolicy,
    global_policy: GlobalCapabilityPolicy,
    *,
    now: float,
    replayed_attempt_ids: Iterable[str] = (),
    replayed_replay_ids: Iterable[str] = (),
    replayed_approval_ids: Iterable[str] = (),
    verified_audit_receipt_ids: Iterable[str] = (),
) -> CapabilityDecision:
    """Authorize one invocation before its caller crosses an effect boundary."""
    if not isinstance(global_policy, GlobalCapabilityPolicy):
        return _decision(envelope, reason="global_policy_missing")
    if policy.policy_version != CAPABILITY_POLICY_SCHEMA_VERSION or global_policy.policy_version != CAPABILITY_POLICY_SCHEMA_VERSION:
        return _decision(envelope, reason="unknown_authority_envelope_version")
    if policy.capability_id not in global_policy.allowed_capabilities and "*" not in global_policy.allowed_capabilities:
        return _decision(envelope, reason="capability_not_global_allowlisted")
    if global_policy.allowed_versions and envelope.capability_version not in global_policy.allowed_versions:
        return _decision(envelope, reason="capability_version_not_global_allowlisted")
    effective = intersect_authority_envelope(policy, global_policy)
    if policy.revoked or global_policy.revoked:
        return _decision(envelope, reason="policy_revoked", effective_policy=effective)
    if policy.expires_at is not None and policy.expires_at <= now:
        return _decision(envelope, reason="policy_expired", effective_policy=effective)
    if global_policy.expires_at is not None and global_policy.expires_at <= now:
        return _decision(envelope, reason="global_policy_expired", effective_policy=effective)
    authority = envelope.authority
    if authority is None:
        return _decision(envelope, reason="authority_missing", effective_policy=effective)
    if authority.revoked or authority.principal.revoked:
        return _decision(envelope, reason="authority_revoked", effective_policy=effective)
    if authority.expires_at is not None and authority.expires_at <= now:
        return _decision(envelope, reason="authority_expired", effective_policy=effective)
    if authority.issued_at > now:
        return _decision(envelope, reason="authority_not_yet_valid", effective_policy=effective)
    identity_reason = _identity_reason(envelope, policy)
    if identity_reason is not None:
        return _decision(envelope, reason=identity_reason, effective_policy=effective)
    if effective.principal_type is not None and _value(authority.principal.principal_type) != _value(effective.principal_type):
        return _decision(envelope, reason="principal_type_mismatch", effective_policy=effective)
    if not envelope.data_digest:
        return _decision(envelope, reason="data_digest_missing", effective_policy=effective)
    if not is_canonical_digest(envelope.data_digest):
        return _decision(envelope, reason="data_digest_invalid", effective_policy=effective)
    scope_reason = _scope_reason(envelope, effective.scope)
    if scope_reason is not None:
        return _decision(envelope, reason=scope_reason, effective_policy=effective)
    if effective.scope.sources and not envelope.source_id:
        return _decision(envelope, reason="source_missing", effective_policy=effective)
    if envelope.secret_scopes:
        credential_check = credential_scope_allowed(
            field=envelope.credential_field,
            destination_url=envelope.network_url,
            secret_scopes=envelope.secret_scopes,
            allowed_fields=effective.scope.credential_fields,
            allowed_secret_scopes=effective.scope.secret_scopes,
            allowed_hosts=effective.scope.network_hosts,
            allowed_paths=effective.scope.network_paths,
        )
        if not credential_check.allowed:
            return _decision(envelope, reason=credential_check.reason_code, effective_policy=effective)
    elif envelope.network_url:
        network_check = network_target_allowed(
            envelope.network_url,
            effective.scope.network_hosts,
            allowed_paths=effective.scope.network_paths,
            resolve_dns=True,
        )
        if not network_check.allowed:
            return _decision(envelope, reason=network_check.reason_code, effective_policy=effective)
    limits_reason = _limits_reason(envelope, effective, now=now)
    if limits_reason is not None:
        return _decision(envelope, reason=limits_reason, effective_policy=effective)
    if any(item.prompt_injection_detected for item in envelope.content):
        return _decision(envelope, reason="untrusted_prompt_injection_blocked", effective_policy=effective)
    if any(item.instruction_authority for item in envelope.content):
        return _decision(envelope, reason="untrusted_instruction_authority", effective_policy=effective)
    if not _valid_reference(envelope.request_id) or not _valid_reference(envelope.attempt_id) or not _valid_reference(envelope.replay_id):
        return _decision(envelope, reason="execution_reference_invalid", effective_policy=effective)
    if envelope.attempt_id in set(replayed_attempt_ids) or envelope.replay_id in set(replayed_replay_ids):
        return _decision(envelope, reason="execution_replayed", effective_policy=effective)
    try:
        trust_request = _build_trust_request(envelope, effective)
    except (ValueError, TypeError):
        return _decision(envelope, reason="trust_request_invalid", effective_policy=effective)
    trust_decision = evaluate_trust(
        trust_request,
        now=now,
        replayed_attempt_ids=replayed_attempt_ids,
        replayed_replay_ids=replayed_replay_ids,
        replayed_approval_ids=replayed_approval_ids,
        verified_audit_receipt_ids=verified_audit_receipt_ids,
    )
    if trust_decision.effect is DecisionEffect.REQUIRE_APPROVAL:
        return _decision(
            envelope,
            reason=trust_decision.reason_code,
            effect=DecisionEffect.REQUIRE_APPROVAL,
            effective_policy=effective,
            trust_decision=trust_decision,
        )
    if not trust_decision.allowed:
        return _decision(
            envelope,
            reason=trust_decision.reason_code,
            effective_policy=effective,
            trust_decision=trust_decision,
        )
    return _decision(
        envelope,
        reason="authority_envelope_allowed",
        effect=DecisionEffect.ALLOW,
        effective_policy=effective,
        trust_decision=trust_decision,
    )


def bind_capability_approval(
    envelope: CapabilityEnvelope,
    policy: CapabilityPolicy,
    *,
    approval_id: str,
    expires_at: float,
) -> CapabilityEnvelope:
    """Bind an existing trust approval to this exact invocation metadata."""
    if envelope.authority is None:
        raise ValueError("authority_missing")
    effective = policy
    request = _build_trust_request(envelope, effective)
    approval = bind_approval(request, approval_id=approval_id, expires_at=expires_at)
    authority = CapabilityAuthority(
        **{
            **envelope.authority.__dict__,
            "approval": approval,
            "approval_digest": approval_binding_digest(approval),
        }
    )
    return CapabilityEnvelope(**{**envelope.__dict__, "authority": authority, "principal": authority.principal})


# Descriptive aliases keep the seam discoverable to capability callers.
evaluate_capability = authorize_capability
check_filesystem_scope = filesystem_path_allowed
check_network_scope = network_target_allowed
check_credential_scope = credential_scope_allowed
