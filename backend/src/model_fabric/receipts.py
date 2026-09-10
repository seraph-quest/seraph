"""Sanitized, immutable route receipt values for the model fabric."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit


_SAFE_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ROUTE_OUTCOMES = frozenset({"succeeded", "failed", "denied", "timed_out"})
_COST_KINDS = frozenset({"estimated", "unknown", "local_resource"})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def sanitized_endpoint(value: str) -> str:
    """Strip credentials, query parameters, and fragments from an endpoint."""
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("model endpoint must be an absolute HTTP(S) URL")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path.rstrip("/"), "", ""))


def endpoint_digest(value: str) -> str:
    return hashlib.sha256(sanitized_endpoint(value).encode("utf-8")).hexdigest()


def canonical_hash(payload: object) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def safe_code(value: str, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if _SAFE_CODE.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a bounded safe identifier")
    return normalized


def _aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class CostEstimate:
    """Nullable, sourced cost metadata; local compute is never represented as free."""

    kind: str = "unknown"
    amount: float | None = None
    currency: str | None = None
    source: str | None = None
    source_updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.kind not in _COST_KINDS:
            raise ValueError("unsupported cost estimate kind")
        if self.kind == "estimated":
            if self.amount is None or self.amount < 0:
                raise ValueError("estimated cost requires a non-negative amount")
            if not self.currency or not self.source or self.source_updated_at is None:
                raise ValueError("estimated cost requires currency, source, and source timestamp")
            safe_code(self.currency, field_name="cost currency")
            safe_code(self.source, field_name="cost source")
            _aware(self.source_updated_at, field_name="cost source timestamp")
        elif any(value is not None for value in (self.amount, self.currency, self.source, self.source_updated_at)):
            raise ValueError("unknown and local_resource costs cannot claim a monetary amount")

    def as_safe_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "amount": self.amount,
            "currency": self.currency,
            "source": self.source,
            "source_updated_at": self.source_updated_at.isoformat() if self.source_updated_at else None,
        }


@dataclass(frozen=True)
class TokenUsage:
    """Provider-reported usage; absent values stay null rather than becoming zero."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    def __post_init__(self) -> None:
        for value in (self.input_tokens, self.output_tokens, self.total_tokens):
            if value is not None and (not isinstance(value, int) or value < 0):
                raise ValueError("token usage values must be non-negative integers or null")

    def as_safe_dict(self) -> dict[str, int | None]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class RouteAttemptReceipt:
    attempt_id: str
    attempt_index: int
    profile_id: str
    model: str
    endpoint: str
    adapter: str
    destination_class: str
    egress_class: str
    trust_decision_id: str
    capability_proof_hashes: tuple[str, ...]
    outcome: str
    started_at: datetime
    finished_at: datetime
    latency_ms: int
    error_code: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    cost: CostEstimate = field(default_factory=CostEstimate)
    degradation_code: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "attempt_id", "profile_id", "adapter", "destination_class", "egress_class", "trust_decision_id"
        ):
            safe_code(getattr(self, field_name), field_name=field_name)
        if not self.model or len(self.model) > 256:
            raise ValueError("model must be present and bounded")
        object.__setattr__(self, "endpoint", sanitized_endpoint(self.endpoint))
        if self.outcome not in _ROUTE_OUTCOMES:
            raise ValueError("unsupported route-attempt outcome")
        if self.attempt_index < 0 or self.latency_ms < 0:
            raise ValueError("attempt index and latency must be non-negative")
        started = _aware(self.started_at, field_name="started_at")
        finished = _aware(self.finished_at, field_name="finished_at")
        if finished < started:
            raise ValueError("finished_at cannot precede started_at")
        object.__setattr__(self, "started_at", started)
        object.__setattr__(self, "finished_at", finished)
        if self.error_code is not None:
            safe_code(self.error_code, field_name="error_code")
        if self.degradation_code is not None:
            safe_code(self.degradation_code, field_name="degradation_code")
        for proof_hash in self.capability_proof_hashes:
            if re.fullmatch(r"[0-9a-f]{64}", proof_hash) is None:
                raise ValueError("capability proof hashes must be lowercase SHA-256 values")

    def as_safe_dict(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "attempt_index": self.attempt_index,
            "profile_id": self.profile_id,
            "model": self.model,
            "endpoint": self.endpoint,
            "endpoint_digest": endpoint_digest(self.endpoint),
            "adapter": self.adapter,
            "destination_class": self.destination_class,
            "egress_class": self.egress_class,
            "trust_decision_id": self.trust_decision_id,
            "capability_proof_hashes": list(self.capability_proof_hashes),
            "outcome": self.outcome,
            "error_code": self.error_code,
            "degradation_code": self.degradation_code,
            "usage": self.usage.as_safe_dict(),
            "cost": self.cost.as_safe_dict(),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True)
class RouteReceipt:
    receipt_id: str
    request_id: str
    route_decision_id: str
    runtime_path: str
    workload: str
    outcome: str
    egress_class: str
    started_at: datetime
    finished_at: datetime
    latency_ms: int
    attempts: tuple[RouteAttemptReceipt, ...] = ()
    actual_profile_id: str | None = None
    actual_model: str | None = None
    actual_adapter: str | None = None
    destination_class: str | None = None
    trust_decision_id: str | None = None
    fallback_used: bool = False
    fallback_reason_code: str | None = None
    degradation_codes: tuple[str, ...] = ()
    cost: CostEstimate = field(default_factory=CostEstimate)
    usage: TokenUsage = field(default_factory=TokenUsage)

    def __post_init__(self) -> None:
        for field_name in (
            "receipt_id", "request_id", "route_decision_id", "runtime_path", "workload", "egress_class"
        ):
            safe_code(getattr(self, field_name), field_name=field_name)
        if self.outcome not in _ROUTE_OUTCOMES:
            raise ValueError("unsupported final route outcome")
        started = _aware(self.started_at, field_name="started_at")
        finished = _aware(self.finished_at, field_name="finished_at")
        if finished < started or self.latency_ms < 0:
            raise ValueError("final route timing is invalid")
        object.__setattr__(self, "started_at", started)
        object.__setattr__(self, "finished_at", finished)
        optional_codes = {
            "actual_profile_id": self.actual_profile_id,
            "actual_adapter": self.actual_adapter,
            "destination_class": self.destination_class,
            "trust_decision_id": self.trust_decision_id,
            "fallback_reason_code": self.fallback_reason_code,
        }
        for field_name, value in optional_codes.items():
            if value is not None:
                safe_code(value, field_name=field_name)
        for code in self.degradation_codes:
            safe_code(code, field_name="degradation_code")
        if self.outcome == "succeeded" and not self.actual_profile_id:
            raise ValueError("successful route receipt requires an actual profile")
        if self.actual_model is not None and len(self.actual_model) > 256:
            raise ValueError("actual model must be bounded")
        indexes = [attempt.attempt_index for attempt in self.attempts]
        if len(indexes) != len(set(indexes)):
            raise ValueError("route attempt indexes must be unique")

    def as_safe_dict(self) -> dict[str, object]:
        return {
            "receipt_id": self.receipt_id,
            "request_id": self.request_id,
            "route_decision_id": self.route_decision_id,
            "runtime_path": self.runtime_path,
            "workload": self.workload,
            "outcome": self.outcome,
            "egress_class": self.egress_class,
            "actual_profile_id": self.actual_profile_id,
            "actual_model": self.actual_model,
            "actual_adapter": self.actual_adapter,
            "destination_class": self.destination_class,
            "trust_decision_id": self.trust_decision_id,
            "fallback_used": self.fallback_used,
            "fallback_reason_code": self.fallback_reason_code,
            "degradation_codes": list(self.degradation_codes),
            "cost": self.cost.as_safe_dict(),
            "usage": self.usage.as_safe_dict(),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "latency_ms": self.latency_ms,
            "attempts": [attempt.as_safe_dict() for attempt in self.attempts],
        }

    @property
    def receipt_hash(self) -> str:
        return canonical_hash(self.as_safe_dict())


@dataclass(frozen=True)
class ReceiptPersistenceResult:
    receipt_id: str
    status: str
    persisted: bool
    receipt_hash: str | None = None
    error_code: str | None = None

    @classmethod
    def success(cls, receipt: RouteReceipt) -> "ReceiptPersistenceResult":
        return cls(
            receipt_id=receipt.receipt_id,
            status="persisted",
            persisted=True,
            receipt_hash=receipt.receipt_hash,
        )

    @classmethod
    def degraded(cls, receipt_id: str, *, error_code: str = "receipt_persistence_failed") -> "ReceiptPersistenceResult":
        return cls(
            receipt_id=receipt_id,
            status="degraded",
            persisted=False,
            error_code=safe_code(error_code, field_name="persistence error code"),
        )
