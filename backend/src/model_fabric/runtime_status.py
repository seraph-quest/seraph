"""Process-local sanitized observations that cannot be recovered from failed persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock

from .receipts import safe_code


@dataclass(frozen=True)
class ReceiptPersistenceObservation:
    runtime_path: str
    status: str
    error_code: str | None
    receipt_id: str
    observed_at: str


_LOCK = Lock()
_LATEST_BY_RUNTIME_PATH: dict[str, ReceiptPersistenceObservation] = {}


def publish_receipt_persistence(
    *,
    runtime_path: str,
    status: str,
    error_code: str | None,
    receipt_id: str,
) -> ReceiptPersistenceObservation:
    """Publish bounded receipt-storage state without prompts, endpoints, or secrets."""
    safe_runtime_path = safe_code(runtime_path, field_name="runtime_path")
    safe_status = safe_code(status, field_name="persistence status")
    safe_error = safe_code(error_code, field_name="persistence error code") if error_code else None
    safe_receipt_id = str(receipt_id or "")[:128]
    observation = ReceiptPersistenceObservation(
        runtime_path=safe_runtime_path,
        status=safe_status,
        error_code=safe_error,
        receipt_id=safe_receipt_id,
        observed_at=datetime.now(timezone.utc).isoformat(),
    )
    with _LOCK:
        _LATEST_BY_RUNTIME_PATH[safe_runtime_path] = observation
    return observation


def latest_receipt_persistence(runtime_path: str) -> ReceiptPersistenceObservation | None:
    safe_runtime_path = safe_code(runtime_path, field_name="runtime_path")
    with _LOCK:
        return _LATEST_BY_RUNTIME_PATH.get(safe_runtime_path)


def clear_receipt_persistence_observations() -> None:
    """Test-only reset for process-local observations."""
    with _LOCK:
        _LATEST_BY_RUNTIME_PATH.clear()
