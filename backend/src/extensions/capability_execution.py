"""Small execution choke point for governed local capabilities.

The existing authority envelope decides whether a capability invocation may
cross an effect boundary.  This module keeps the effect boundary explicit and
reusable for local adapters: it never asks a model for permission, resolves a
secret, or performs network I/O.  Durable jobs own persistence and recovery;
the host only adds an idempotent in-process guard and a redacted execution
receipt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

from src.security.authority_envelope import (
    CapabilityDecision,
    CapabilityEnvelope,
    CapabilityPolicy,
    GlobalCapabilityPolicy,
    authorize_capability,
)
from src.security.trust_contract import DecisionEffect


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_error(exc: BaseException) -> str:
    """Return a stable error category without persisting exception content."""

    return f"{type(exc).__module__}.{type(exc).__name__}"


@dataclass(frozen=True, slots=True)
class CapabilityExecutionReceipt:
    """Operator-safe result for one admitted effect."""

    status: str
    effect_key: str
    decision_id: str
    decision_reason: str
    request_digest: str
    output_digest: str | None = None
    output_type: str | None = None
    output_size: int | None = None
    error_type: str | None = None
    duplicate_of: str | None = None
    receipt_id: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "seraph.capability-execution.v1",
            "status": self.status,
            "effect_key": self.effect_key,
            "decision_id": self.decision_id,
            "decision_reason": self.decision_reason,
            "request_digest": self.request_digest,
            "output_digest": self.output_digest,
            "output_type": self.output_type,
            "output_size": self.output_size,
            "error_type": self.error_type,
            "duplicate_of": self.duplicate_of,
            "receipt_id": self.receipt_id,
            "details": dict(self.details),
        }


class CapabilityEffectLedger:
    """A narrow duplicate-effect guard for one host process.

    The durable job repository remains authoritative across restarts.  This
    ledger prevents a caller that retries the same admitted effect in one
    process from invoking the adapter twice while that durable reconciliation
    runs.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._completed: dict[str, str] = {}

    def claim_or_duplicate(self, effect_key: str) -> tuple[bool, str | None]:
        with self._lock:
            if effect_key in self._completed:
                existing = self._completed[effect_key]
                return False, existing or None
            # An empty marker reserves the key.  It is removed if execution
            # fails so an explicit durable retry can reconcile and try again.
            self._completed[effect_key] = ""
            return True, None

    def complete(self, effect_key: str, receipt_id: str) -> None:
        with self._lock:
            self._completed[effect_key] = receipt_id

    def release(self, effect_key: str) -> None:
        with self._lock:
            self._completed.pop(effect_key, None)

    def reset_for_tests(self) -> None:
        with self._lock:
            self._completed.clear()


class CapabilityExecutionHost:
    """Run a local effect only after the model-independent authority decision."""

    def __init__(self, *, ledger: CapabilityEffectLedger | None = None) -> None:
        self.ledger = ledger or CapabilityEffectLedger()

    def execute(
        self,
        envelope: CapabilityEnvelope,
        policy: CapabilityPolicy,
        global_policy: GlobalCapabilityPolicy,
        effect: Callable[[], Any],
        *,
        now: float | None = None,
        effect_key: str | None = None,
        replayed_attempt_ids: Iterable[str] = (),
        replayed_replay_ids: Iterable[str] = (),
        replayed_approval_ids: Iterable[str] = (),
        verified_audit_receipt_ids: Iterable[str] = (),
    ) -> CapabilityExecutionReceipt:
        evaluated_at = time.time() if now is None else float(now)
        decision: CapabilityDecision = authorize_capability(
            envelope,
            policy,
            global_policy,
            now=evaluated_at,
            replayed_attempt_ids=replayed_attempt_ids,
            replayed_replay_ids=replayed_replay_ids,
            replayed_approval_ids=replayed_approval_ids,
            verified_audit_receipt_ids=verified_audit_receipt_ids,
        )
        key = effect_key or f"{envelope.job_id}:{envelope.attempt_id}:{envelope.replay_id}"
        if decision.effect is DecisionEffect.REQUIRE_APPROVAL:
            return self._receipt(
                status="blocked",
                key=key,
                decision=decision,
                reason="approval_required",
            )
        if not decision.allowed:
            return self._receipt(
                status="blocked",
                key=key,
                decision=decision,
                reason=decision.reason_code,
            )

        claimed, duplicate_of = self.ledger.claim_or_duplicate(key)
        if not claimed:
            return self._receipt(
                status="deduplicated",
                key=key,
                decision=decision,
                reason="effect_already_completed",
                duplicate_of=duplicate_of or key,
            )

        try:
            output = effect()
        except BaseException as exc:
            self.ledger.release(key)
            return self._receipt(
                status="failed",
                key=key,
                decision=decision,
                reason="effect_failed",
                error_type=_safe_error(exc),
            )

        output_digest = _digest(output)
        receipt = self._receipt(
            status="succeeded",
            key=key,
            decision=decision,
            reason="effect_executed",
            output_digest=output_digest,
            output_type=type(output).__name__,
            output_size=len(output) if isinstance(output, (str, bytes, list, tuple, dict, set)) else None,
        )
        self.ledger.complete(key, receipt.receipt_id)
        return receipt

    @staticmethod
    def _receipt(
        *,
        status: str,
        key: str,
        decision: CapabilityDecision,
        reason: str,
        output_digest: str | None = None,
        output_type: str | None = None,
        output_size: int | None = None,
        error_type: str | None = None,
        duplicate_of: str | None = None,
    ) -> CapabilityExecutionReceipt:
        receipt_id = f"cap-exec-{_digest({'key': key, 'decision': decision.decision_id, 'status': status, 'reason': reason})[:24]}"
        return CapabilityExecutionReceipt(
            status=status,
            effect_key=key,
            decision_id=decision.decision_id,
            decision_reason=reason,
            request_digest=decision.request_digest,
            output_digest=output_digest,
            output_type=output_type,
            output_size=output_size,
            error_type=error_type,
            duplicate_of=duplicate_of,
            receipt_id=receipt_id,
            details={
                "authorization_reason": decision.reason_code,
                "authority_receipt": decision.receipt,
                "raw_output_stored": False,
                "raw_error_stored": False,
            },
        )


capability_execution_host = CapabilityExecutionHost()


__all__ = [
    "CapabilityEffectLedger",
    "CapabilityExecutionHost",
    "CapabilityExecutionReceipt",
    "capability_execution_host",
]
