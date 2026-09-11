"""Repository-backed capability proofs and sanitized inference-route receipts."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from src.db.engine import get_session
from src.db.models import (
    ModelCapabilityProofRecord,
    ModelRouteAttemptReceiptRecord,
    ModelRouteReceiptRecord,
)

from .contracts import EndpointClass, ModelRouteProof
from .proofs import validate_model_route_proof
from .receipts import (
    CostEstimate,
    ReceiptPersistenceResult,
    RouteAttemptReceipt,
    RouteReceipt,
    TokenUsage,
    endpoint_digest,
    safe_code,
)


SessionProvider = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True)
class ProofPersistenceResult:
    proof_hash: str
    status: str
    persisted: bool
    error_code: str | None = None


class ModelFabricRepository:
    def __init__(self, session_provider: SessionProvider | None = None):
        self._session_provider = session_provider

    def _session(self) -> AbstractAsyncContextManager[AsyncSession]:
        return (self._session_provider or get_session)()

    async def persist_capability_proof(
        self,
        proof: ModelRouteProof,
    ) -> ProofPersistenceResult:
        """Persist one exact proof, reporting storage failure without fabricating success."""
        validate_model_route_proof(proof)
        record = ModelCapabilityProofRecord(
            proof_hash=proof.proof_hash,
            profile_schema_version=proof.profile_schema_version,
            profile_contract_hash=proof.profile_contract_hash,
            profile_id=proof.profile_id,
            model=proof.model,
            endpoint=proof.endpoint,
            endpoint_digest=endpoint_digest(proof.endpoint),
            endpoint_class=proof.endpoint_class.value,
            adapter=proof.adapter,
            capability=proof.capability,
            canary_version=proof.canary_version,
            outcome=proof.outcome,
            checked_at=proof.checked_at,
            expires_at=proof.expires_at,
            proven_value_json=(
                json.dumps(proof.proven_value, separators=(",", ":"))
                if proof.proven_value is not None
                else None
            ),
            receipt_id=proof.probe_receipt_id,
            receipt_hash=proof.probe_receipt_hash,
        )
        try:
            async with self._session() as db:
                db.add(record)
                await db.flush()
        except Exception:
            return ProofPersistenceResult(
                proof_hash=proof.proof_hash,
                status="degraded",
                persisted=False,
                error_code="proof_persistence_failed",
            )
        return ProofPersistenceResult(proof.proof_hash, "persisted", True)

    async def latest_capability_proof(
        self,
        *,
        profile_schema_version: str,
        profile_contract_hash: str,
        profile_id: str,
        model: str,
        endpoint: str,
        endpoint_class: EndpointClass,
        adapter: str,
        capability: str,
    ) -> ModelRouteProof | None:
        """Return the newest valid proof for one exact binding, including expired proofs."""
        endpoint_hash = endpoint_digest(endpoint)
        async with self._session() as db:
            stmt = (
                select(ModelCapabilityProofRecord)
                .where(
                    ModelCapabilityProofRecord.profile_schema_version == profile_schema_version,
                    ModelCapabilityProofRecord.profile_contract_hash == profile_contract_hash,
                    ModelCapabilityProofRecord.profile_id == profile_id,
                    ModelCapabilityProofRecord.model == model,
                    ModelCapabilityProofRecord.endpoint_digest == endpoint_hash,
                    ModelCapabilityProofRecord.endpoint_class == endpoint_class.value,
                    ModelCapabilityProofRecord.adapter == adapter,
                    ModelCapabilityProofRecord.capability == capability,
                )
                .order_by(col(ModelCapabilityProofRecord.checked_at).desc())
            )
            result = await db.execute(stmt)
            records = result.scalars().all()
            for record in records:
                proof = _proof_from_record(record)
                try:
                    validate_model_route_proof(proof)
                except ValueError:
                    continue
                if proof.outcome != "passed":
                    continue
                receipt_result = await db.execute(
                    select(ModelRouteReceiptRecord).where(
                        ModelRouteReceiptRecord.receipt_id == proof.probe_receipt_id,
                        ModelRouteReceiptRecord.receipt_hash == proof.probe_receipt_hash,
                        ModelRouteReceiptRecord.outcome == "succeeded",
                    )
                )
                if receipt_result.scalar_one_or_none() is not None:
                    return proof
        return None

    async def latest_capability_proofs_for_profiles(
        self,
        profile_ids: tuple[str, ...],
    ) -> tuple[ModelRouteProof, ...]:
        """Return the newest valid exact proof per binding for a bounded profile set."""
        normalized = tuple(dict.fromkeys(safe_code(item, field_name="profile_id") for item in profile_ids))
        if not normalized:
            return ()
        async with self._session() as db:
            stmt = (
                select(ModelCapabilityProofRecord)
                .where(col(ModelCapabilityProofRecord.profile_id).in_(normalized))
                .order_by(col(ModelCapabilityProofRecord.checked_at).desc())
            )
            result = await db.execute(stmt)
            records = result.scalars().all()
            receipt_ids = tuple(dict.fromkeys(record.receipt_id for record in records))
            linked_receipts: dict[tuple[str, str], str] = {}
            if receipt_ids:
                receipt_result = await db.execute(
                    select(ModelRouteReceiptRecord).where(
                        col(ModelRouteReceiptRecord.receipt_id).in_(receipt_ids),
                    )
                )
                linked_receipts = {
                    (receipt.receipt_id, receipt.receipt_hash): receipt.outcome
                    for receipt in receipt_result.scalars().all()
                }
        latest: dict[tuple[str, str, str, str, str, str], ModelRouteProof] = {}
        for record in records:
            proof = _proof_from_record(record)
            try:
                validate_model_route_proof(proof)
            except ValueError:
                continue
            receipt_outcome = linked_receipts.get(
                (proof.probe_receipt_id, proof.probe_receipt_hash)
            )
            expected_receipt_outcome = (
                "succeeded"
                if proof.outcome == "passed"
                else "failed"
                if proof.outcome == "failed"
                else None
            )
            if receipt_outcome != expected_receipt_outcome:
                continue
            key = (
                proof.profile_contract_hash,
                proof.profile_id,
                proof.model,
                proof.endpoint,
                proof.adapter,
                proof.capability,
            )
            latest.setdefault(key, proof)
        return tuple(latest.values())

    async def persist_route_receipt(self, receipt: RouteReceipt) -> ReceiptPersistenceResult:
        """Atomically persist the final receipt and all of its attempt receipts."""
        record = _route_record(receipt)
        attempt_records = [_attempt_record(receipt.receipt_id, attempt) for attempt in receipt.attempts]
        try:
            async with self._session() as db:
                db.add(record)
                await db.flush()
                db.add_all(attempt_records)
                await db.flush()
        except Exception:
            return ReceiptPersistenceResult.degraded(receipt.receipt_id)
        return ReceiptPersistenceResult.success(receipt)

    async def latest_successful_route(self, *, workload: str) -> RouteReceipt | None:
        """Return the latest persisted successful actual route for one workload."""
        return await self.latest_route(runtime_path=workload, outcome="succeeded")

    async def latest_route(
        self,
        *,
        runtime_path: str,
        outcome: str | None = None,
    ) -> RouteReceipt | None:
        """Return the latest verified final receipt for one runtime path."""
        safe_code(runtime_path, field_name="runtime_path")
        async with self._session() as db:
            stmt = select(ModelRouteReceiptRecord).where(
                ModelRouteReceiptRecord.runtime_path == runtime_path,
            )
            if outcome is not None:
                safe_code(outcome, field_name="route outcome")
                stmt = stmt.where(ModelRouteReceiptRecord.outcome == outcome)
            stmt = stmt.order_by(col(ModelRouteReceiptRecord.finished_at).desc()).limit(1)
            result = await db.execute(stmt)
            record = result.scalar_one_or_none()
            if record is None:
                return None
            attempts_result = await db.execute(
                select(ModelRouteAttemptReceiptRecord)
                .where(ModelRouteAttemptReceiptRecord.route_receipt_id == record.receipt_id)
                .order_by(ModelRouteAttemptReceiptRecord.attempt_index)
            )
            attempts = tuple(_attempt_from_record(item) for item in attempts_result.scalars().all())
        receipt = _route_from_record(record, attempts)
        if receipt.receipt_hash != record.receipt_hash:
            return None
        return receipt

    async def latest_routes_for_runtime_paths(
        self,
        runtime_paths: tuple[str, ...],
        *,
        outcome: str | None = None,
    ) -> dict[str, RouteReceipt]:
        """Bulk-read the latest verified receipt per exact runtime path."""
        normalized = tuple(
            dict.fromkeys(safe_code(item, field_name="runtime_path") for item in runtime_paths)
        )
        if not normalized:
            return {}
        async with self._session() as db:
            stmt = select(ModelRouteReceiptRecord).where(
                col(ModelRouteReceiptRecord.runtime_path).in_(normalized)
            )
            if outcome is not None:
                safe_code(outcome, field_name="route outcome")
                stmt = stmt.where(ModelRouteReceiptRecord.outcome == outcome)
            result = await db.execute(
                stmt.order_by(col(ModelRouteReceiptRecord.finished_at).desc())
            )
            records = result.scalars().all()
            selected_records: dict[str, ModelRouteReceiptRecord] = {}
            for record in records:
                selected_records.setdefault(record.runtime_path, record)
            receipt_ids = tuple(record.receipt_id for record in selected_records.values())
            attempts_by_receipt: dict[str, list[RouteAttemptReceipt]] = {}
            if receipt_ids:
                attempts_result = await db.execute(
                    select(ModelRouteAttemptReceiptRecord)
                    .where(col(ModelRouteAttemptReceiptRecord.route_receipt_id).in_(receipt_ids))
                    .order_by(ModelRouteAttemptReceiptRecord.route_receipt_id, ModelRouteAttemptReceiptRecord.attempt_index)
                )
                for item in attempts_result.scalars().all():
                    attempts_by_receipt.setdefault(item.route_receipt_id, []).append(
                        _attempt_from_record(item)
                    )
        receipts: dict[str, RouteReceipt] = {}
        for runtime_path, record in selected_records.items():
            receipt = _route_from_record(
                record,
                tuple(attempts_by_receipt.get(record.receipt_id, ())),
            )
            if receipt.receipt_hash == record.receipt_hash:
                receipts[runtime_path] = receipt
        return receipts


def _route_record(receipt: RouteReceipt) -> ModelRouteReceiptRecord:
    return ModelRouteReceiptRecord(
        receipt_id=receipt.receipt_id,
        receipt_hash=receipt.receipt_hash,
        request_id=receipt.request_id,
        route_decision_id=receipt.route_decision_id,
        runtime_path=receipt.runtime_path,
        workload=receipt.workload,
        outcome=receipt.outcome,
        actual_profile_id=receipt.actual_profile_id,
        actual_model=receipt.actual_model,
        actual_adapter=receipt.actual_adapter,
        destination_class=receipt.destination_class,
        egress_class=receipt.egress_class,
        trust_decision_id=receipt.trust_decision_id,
        fallback_used=receipt.fallback_used,
        fallback_reason_code=receipt.fallback_reason_code,
        degradation_codes_json=json.dumps(list(receipt.degradation_codes), separators=(",", ":")),
        cost_kind=receipt.cost.kind,
        cost_amount=receipt.cost.amount,
        cost_currency=receipt.cost.currency,
        cost_source=receipt.cost.source,
        cost_source_updated_at=receipt.cost.source_updated_at,
        usage_input_tokens=receipt.usage.input_tokens,
        usage_output_tokens=receipt.usage.output_tokens,
        usage_total_tokens=receipt.usage.total_tokens,
        started_at=receipt.started_at,
        finished_at=receipt.finished_at,
        latency_ms=receipt.latency_ms,
    )


def _attempt_record(receipt_id: str, attempt: RouteAttemptReceipt) -> ModelRouteAttemptReceiptRecord:
    return ModelRouteAttemptReceiptRecord(
        route_receipt_id=receipt_id,
        attempt_id=attempt.attempt_id,
        attempt_index=attempt.attempt_index,
        profile_id=attempt.profile_id,
        model=attempt.model,
        endpoint=attempt.endpoint,
        endpoint_digest=endpoint_digest(attempt.endpoint),
        adapter=attempt.adapter,
        destination_class=attempt.destination_class,
        egress_class=attempt.egress_class,
        trust_decision_id=attempt.trust_decision_id,
        capability_proof_hashes_json=json.dumps(list(attempt.capability_proof_hashes), separators=(",", ":")),
        outcome=attempt.outcome,
        error_code=attempt.error_code,
        degradation_code=attempt.degradation_code,
        usage_input_tokens=attempt.usage.input_tokens,
        usage_output_tokens=attempt.usage.output_tokens,
        usage_total_tokens=attempt.usage.total_tokens,
        cost_kind=attempt.cost.kind,
        cost_amount=attempt.cost.amount,
        cost_currency=attempt.cost.currency,
        cost_source=attempt.cost.source,
        cost_source_updated_at=attempt.cost.source_updated_at,
        started_at=attempt.started_at,
        finished_at=attempt.finished_at,
        latency_ms=attempt.latency_ms,
    )


def _proof_from_record(record: ModelCapabilityProofRecord) -> ModelRouteProof:
    return ModelRouteProof(
        profile_schema_version=record.profile_schema_version,
        profile_contract_hash=record.profile_contract_hash,
        profile_id=record.profile_id,
        model=record.model,
        endpoint=record.endpoint,
        endpoint_class=EndpointClass(record.endpoint_class),
        adapter=record.adapter,
        capability=record.capability,
        canary_version=record.canary_version,
        outcome=record.outcome,
        checked_at=record.checked_at,
        expires_at=record.expires_at,
        proof_hash=record.proof_hash,
        probe_receipt_id=record.receipt_id,
        probe_receipt_hash=record.receipt_hash,
        proven_value=(
            json.loads(record.proven_value_json)
            if record.proven_value_json is not None
            else None
        ),
    )


def _route_from_record(
    record: ModelRouteReceiptRecord,
    attempts: tuple[RouteAttemptReceipt, ...],
) -> RouteReceipt:
    return RouteReceipt(
        receipt_id=record.receipt_id,
        request_id=record.request_id,
        route_decision_id=record.route_decision_id,
        runtime_path=record.runtime_path,
        workload=record.workload,
        outcome=record.outcome,
        actual_profile_id=record.actual_profile_id,
        actual_model=record.actual_model,
        actual_adapter=record.actual_adapter,
        destination_class=record.destination_class,
        egress_class=record.egress_class,
        trust_decision_id=record.trust_decision_id,
        fallback_used=record.fallback_used,
        fallback_reason_code=record.fallback_reason_code,
        degradation_codes=tuple(json.loads(record.degradation_codes_json)),
        cost=CostEstimate(
            kind=record.cost_kind,
            amount=record.cost_amount,
            currency=record.cost_currency,
            source=record.cost_source,
            source_updated_at=_db_aware(record.cost_source_updated_at) if record.cost_source_updated_at else None,
        ),
        usage=TokenUsage(
            input_tokens=record.usage_input_tokens,
            output_tokens=record.usage_output_tokens,
            total_tokens=record.usage_total_tokens,
        ),
        started_at=_db_aware(record.started_at),
        finished_at=_db_aware(record.finished_at),
        latency_ms=record.latency_ms,
        attempts=attempts,
    )


def _attempt_from_record(record: ModelRouteAttemptReceiptRecord) -> RouteAttemptReceipt:
    return RouteAttemptReceipt(
        attempt_id=record.attempt_id,
        attempt_index=record.attempt_index,
        profile_id=record.profile_id,
        model=record.model,
        endpoint=record.endpoint,
        adapter=record.adapter,
        destination_class=record.destination_class,
        egress_class=record.egress_class,
        trust_decision_id=record.trust_decision_id,
        capability_proof_hashes=tuple(json.loads(record.capability_proof_hashes_json)),
        outcome=record.outcome,
        error_code=record.error_code,
        degradation_code=record.degradation_code,
        usage=TokenUsage(
            input_tokens=record.usage_input_tokens,
            output_tokens=record.usage_output_tokens,
            total_tokens=record.usage_total_tokens,
        ),
        cost=CostEstimate(
            kind=record.cost_kind,
            amount=record.cost_amount,
            currency=record.cost_currency,
            source=record.cost_source,
            source_updated_at=_db_aware(record.cost_source_updated_at) if record.cost_source_updated_at else None,
        ),
        started_at=_db_aware(record.started_at),
        finished_at=_db_aware(record.finished_at),
        latency_ms=record.latency_ms,
    )


def _db_aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


model_fabric_repository = ModelFabricRepository()
