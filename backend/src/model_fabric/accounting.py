"""Durable accounting around the existing broker's provider callback boundary."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from decimal import Decimal
from typing import Any, Mapping

from .effective_policy import current_inference_policy
from src.workflows.inference_accounting import InferenceAccountingError


_current_usage: ContextVar[dict[str, object] | None] = ContextVar("inference_account_usage", default=None)
_current_policy_digest: ContextVar[str | None] = ContextVar("inference_contact_policy_digest", default=None)
_profile_bindings: ContextVar[dict[str, str]] = ContextVar("inference_accounting_profiles", default={})
_current_runtime: ContextVar[str | None] = ContextVar("inference_accounting_runtime", default=None)
_near_billing: ContextVar[dict[str, object] | None] = ContextVar("near_billing_evidence", default=None)
_near_contact: ContextVar[object | None] = ContextVar("near_contact_authority", default=None)


@contextmanager
def bind_near_contact_authority(witness):
    from src.work_board.near_text_native import NearContactWitness
    if not isinstance(witness, NearContactWitness):
        raise InferenceAccountingError("near_contact_authority_required")
    token = _near_contact.set(witness)
    try:
        yield
    finally:
        _near_contact.reset(token)


def _policy_for_runtime(runtime_path):
    if runtime_path == "near_text_native":
        from .effective_policy import current_near_text_policy
        return current_near_text_policy()
    return current_inference_policy()


def current_near_accounting_operation_id() -> str:
    collector = _near_billing.get()
    if _current_runtime.get() != "near_text_native" or collector is None:
        raise InferenceAccountingError("near_accounting_context_required")
    return collector["operation_id"]


def capture_near_billing_evidence(evidence) -> None:
    from .near_text_billing import validate_near_billing_evidence
    operation_id = current_near_accounting_operation_id()
    validated = validate_near_billing_evidence(evidence, original_operation_id=operation_id)
    collector = _near_billing.get()
    if collector.get("evidence") is not None and collector["evidence"] != validated:
        raise InferenceAccountingError("near_billing_evidence_conflict")
    collector["evidence"] = validated


def bind_accounting_profile(operation_id: str, profile_id: str):
    bindings = dict(_profile_bindings.get())
    bindings[operation_id] = profile_id
    _profile_bindings.set(bindings)


def capture_inference_usage(payload: object) -> None:
    """Capture only cost/operation fields; never retain prompts or responses."""
    collector = _current_usage.get()
    if collector is None or not isinstance(payload, Mapping):
        return
    if isinstance(payload.get("id"), str):
        collector["id"] = payload["id"]
    usage = payload.get("usage")
    if isinstance(usage, Mapping):
        collector["usage"] = {key: usage[key] for key in ("cost", "currency") if key in usage}
        details = usage.get("cost_details")
        if isinstance(details, Mapping) and "upstream_inference_cost" in details:
            collector["usage"]["cost_details"] = {"upstream_inference_cost": str(details["upstream_inference_cost"])[:64]}
    if "currency" in payload:
        collector["currency"] = payload["currency"]


def capture_response_usage(response: object) -> None:
    try:
        raw = getattr(response, "text", None)
        payload = json.loads(raw, parse_float=Decimal) if isinstance(raw, str) else response.json()
    except (AttributeError, TypeError, ValueError):
        return
    capture_inference_usage(payload)


def assert_current_inference_policy() -> None:
    """Final transport and late-adoption fence over the owning settings store."""
    expected = _current_policy_digest.get()
    if expected is not None:
        from src.auth.cancellation import assert_runtime_not_revoked
        assert_runtime_not_revoked()
        _configured, actual = _policy_for_runtime(_current_runtime.get())
        if expected != actual:
            raise InferenceAccountingError("provider_policy_revision_changed")


@dataclass
class _AccountingHandle:
    request: Any
    repository: Any
    job_id: str
    owner: str
    fence: int
    policy_digest: str
    sequence: int
    ephemeral: bool
    contacted: bool = False
    committed_denial: object | None = None


class DurableInferenceBrokerMixin:
    """No independent executor: delegates scheduling to the existing broker."""

    async def enqueue(self, request, **kwargs):
        receipt = await super().enqueue(request, **kwargs)
        with self._condition:
            sequence = getattr(self, "_durable_order", {}).get(request.operation_id)
            operation = self._operations.get(request.operation_id)
            if sequence is not None and operation is not None and operation.status == "queued":
                operation.sequence = sequence
        return receipt

    def _enqueue_locked(self, request, *args, **kwargs):
        receipt = super()._enqueue_locked(request, *args, **kwargs)
        sequence = getattr(self, "_durable_order", {}).get(request.operation_id)
        operation = self._operations.get(request.operation_id)
        if sequence is not None and operation is not None and operation.status == "queued":
            operation.sequence = sequence
        return receipt

    async def _prepare_accounting(self, request):
        from .remote_inference_admission import current_remote_inference_receipt_binding
        from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec, durable_job_repository

        created_job = None
        try:
            if not isinstance(request.owner_id, str) or not request.owner_id.startswith(("operator:", "service:")) or len(request.owner_id) > 256:
                raise InferenceAccountingError("accounting_job_authority_invalid")
            from .configuration import read_model_fabric_configuration
            persisted = read_model_fabric_configuration()
            if persisted.status == "degraded":
                raise InferenceAccountingError(persisted.error_code or "provider_policy_continuity_unavailable")
            configured, policy_digest = _policy_for_runtime(request.runtime_path)
            near = request.runtime_path == "near_text_native"
            setup = configured.near_text if near else configured.openrouter_setup
            bound = setup.request_cost_bound_microusd or setup.spend_ceiling_microusd
            from .configuration import OPENROUTER_SETUP_V2_SCHEMA_VERSION, route_slot_for_task_class
            if near:
                if _profile_bindings.get().get(request.operation_id) != "near.text":
                    raise InferenceAccountingError("accounting_profile_binding_invalid")
                bound = setup.request_cost_bound_microusd
            elif setup.schema_version == OPENROUTER_SETUP_V2_SCHEMA_VERSION:
                from .caller_context import canonical_route_spec
                profile_id = _profile_bindings.get().get(request.operation_id)
                if request.runtime_path == "capability_probe":
                    slot = next((slot for slot in ("text", "vision", "embedding") if profile_id == f"openrouter.{slot}"), None)
                    if slot is None:
                        raise InferenceAccountingError("accounting_profile_binding_invalid")
                else:
                    slot = route_slot_for_task_class(canonical_route_spec(request.runtime_path).task_class)
                route = (setup.routes or {}).get(slot)
                if route is None or not route.enabled or profile_id != f"openrouter.{slot}" or slot != "text" and (setup.purpose_consents or {}).get(slot) != configured.egress_revision:
                    raise InferenceAccountingError("accounting_profile_binding_invalid")
                bound = route.request_cost_bound_microusd
            if type(bound) is not int or bound <= 0:
                raise InferenceAccountingError("accounting_server_bound_required")
            request = replace(request, estimated_cost_microusd=bound)
            binding = current_remote_inference_receipt_binding()
            ephemeral = binding is None or not binding.job_id
            if near and ephemeral:
                raise InferenceAccountingError("near_native_binding_required")
            if near and _near_contact.get() is None:
                raise InferenceAccountingError("near_contact_authority_required")
            repository = binding.repository if not ephemeral else durable_job_repository
            if not ephemeral:
                native = await repository.get_job(binding.job_id)
                if native["job_kind"] == "readonly_research_child":
                    if request.job_id != binding.job_id:
                        raise InferenceAccountingError("accounting_job_binding_invalid")
                    from src.workflows.research_accounting import rebind_funded_child
                    reservation = await rebind_funded_child(repository, request=request,
                        owner=binding.owner, fencing_token=binding.fencing_token,
                        policy_digest=policy_digest,
                        profile_id=_profile_bindings.get().get(request.operation_id, setup.profile_id),
                        bound=bound)
                    return _AccountingHandle(request, repository, binding.job_id,
                        binding.owner, binding.fencing_token, policy_digest,
                        reservation["sequence"], False)
            snapshot = await repository.inference_accounting_snapshot()
            if snapshot["status"] != "ready":
                raise InferenceAccountingError(str(snapshot.get("reason_code") or "accounting_continuity_unavailable"))
            from .configuration import deployment_spend_ceiling
            if snapshot["ceiling_microusd"] != deployment_spend_ceiling(configured):
                raise InferenceAccountingError("accounting_settings_revision_unavailable")
            if snapshot.get("overrun_max_cost_microusd", 0) > 0:
                raise InferenceAccountingError("provider_charge_exceeded_reservation")
            if ephemeral:
                if request.owner_id.startswith("operator:") and not request.session_id:
                    raise InferenceAccountingError("accounting_job_authority_invalid")
                job_id = "inference:" + hashlib.sha256(request.operation_id.encode()).hexdigest()[:40]
                owner = "model-fabric:" + hashlib.sha256(request.operation_id.encode()).hexdigest()[:24]
                owner_kind = "user" if request.owner_id.startswith("operator:") else "service"
                spec = DurableJobSpec(
                    identity=DurableJobIdentity(job_id=job_id, owner_kind=owner_kind,
                        owner_principal_id=request.owner_id, job_kind="model_inference_ephemeral_v1",
                        capability_version="governed-inference-v1", idempotency_scope="model-inference",
                        idempotency_key=request.operation_id),
                    inputs={"payload_digest": request.data_digest, "runtime_path": request.runtime_path},
                    session_id=request.session_id or None, priority=min(100, request.priority.rank * 20),
                    declared_authority={"principal": request.owner_id, **({"service_id": request.owner_id} if owner_kind == "service" else {})},
                    service_id=request.owner_id if owner_kind == "service" else None,
                    deadline_at=datetime.fromtimestamp(request.deadline_at, timezone.utc),
                    resource_claims=("remote_inference",), max_attempts=1,
                )
                existing = await repository.admit_job(spec)
                if existing["status"] != "accepted":
                    raise InferenceAccountingError("accounting_operation_already_reserved")
                await repository.queue_job(job_id)
                claimed = await repository.claim_job(job_id, owner=owner, lease_seconds=300)
                fence = claimed["lease"]["fencing_token"]
                created_job = (repository, job_id, owner, fence)
                request = replace(request, job_id=job_id)
            else:
                job_id, owner, fence = binding.job_id, binding.owner, binding.fencing_token
                if request.job_id != job_id:
                    raise InferenceAccountingError("accounting_job_binding_invalid")
            reservation = await repository.reserve_inference_cost(
                operation_id=request.operation_id, job_id=job_id, owner_id=request.owner_id,
                payload_digest=request.data_digest, policy_digest=policy_digest,
                runtime_path=request.runtime_path,
                profile_id=_profile_bindings.get().get(request.operation_id, setup.profile_id),
                bound_microusd=bound, owner_ceiling_microusd=request.owner_budget_microusd,
                priority=request.priority.rank, deadline_at=request.deadline_at,
                owner=owner, fencing_token=fence,
            )
            return _AccountingHandle(request, repository, job_id, owner, fence, policy_digest,
                reservation["sequence"], ephemeral)
        except Exception as exc:
            from .remote_inference_admission import RemoteInferenceBindingError
            if created_job is not None:
                repository, job_id, owner, fence = created_job
                await repository.transition_job(job_id, "blocked", owner=owner, fencing_token=fence,
                    reason=getattr(exc, "code", "accounting_admission_blocked"),
                    result_summary="Inference admission blocked before provider contact; no_learning")
            raise RemoteInferenceBindingError(getattr(exc, "code", "accounting_admission_blocked")) from exc

    async def _contact_accounting(self, handle):
        from src.workflows.inference_accounting import InferenceProviderContactDenied
        try:
            contact_kwargs = {"near_contact_witness": _near_contact.get()} if handle.request.runtime_path == "near_text_native" else {}
            await handle.repository.contact_inference_provider(handle.request.operation_id,
                owner=handle.owner, fencing_token=handle.fence, policy_digest=handle.policy_digest, **contact_kwargs)
        except InferenceProviderContactDenied as error:
            error.bind_broker_handle(handle)
            handle.committed_denial = error
            raise
        handle.contacted = True
        # Recheck after the durable transaction's await, before invoking the
        # callback. Individual HTTP adapters check again at their final post.
        assert_current_inference_policy()

    async def _finish_accounting(self, handle, *, payload=None, reason=None):
        if handle.committed_denial is not None:
            from src.workflows.inference_accounting import _completed_denial_quiescence
            proof = _completed_denial_quiescence(handle.committed_denial, handle.request, self)
            if proof is not None:
                await handle.repository.record_provider_denial_quiescence(proof)
        near = handle.request.runtime_path == "near_text_native"
        billing = (_near_billing.get() or {}).get("evidence") if near else None
        billing_kwargs = {"near_billing_evidence": billing} if near else {}
        row = await handle.repository.settle_inference_cost(handle.request.operation_id,
            payload=None if near else payload, **billing_kwargs,
            reason=reason or ("provider_account_usage" if handle.contacted else "blocked_before_contact"))
        adoption_allowed = True
        try:
            adoption_allowed = _policy_for_runtime(handle.request.runtime_path)[1] == handle.policy_digest
            if handle.request.owner_id.startswith("operator:"):
                from src.auth.service import authenticate_session
                operator = await authenticate_session(handle.request.session_id, touch=False)
                adoption_allowed = adoption_allowed and operator.principal.principal_id == handle.request.owner_id
        except Exception:
            adoption_allowed = False
        if near:
            snapshot = await handle.repository.inference_accounting_snapshot(job_id=handle.job_id)
            adoption_allowed = (adoption_allowed and billing is not None and row["state"] == "settled"
                and row.get("provider_operation_id") == billing.provider_request_id
                and row.get("actual_cost_microusd") == billing.cost_microusd
                and snapshot.get("accounting_continuity_verified") is True
                and snapshot.get("status") == "ready"
                and snapshot.get("overrun_max_cost_microusd", 0) == 0)
        if handle.ephemeral:
            state = ("succeeded" if adoption_allowed else "blocked") if row["state"] == "settled" else "cost_liability" if handle.contacted else "blocked"
            if state == "succeeded":
                # Actual persisted ledger readback, not a seeded receipt.
                snapshot = await handle.repository.inference_accounting_snapshot(job_id=handle.job_id)
                persisted = next((item for item in snapshot.get("operations", []) if item["operation_id"] == handle.request.operation_id), None)
                if snapshot.get("accounting_continuity_verified") is not True or persisted is None or persisted["state"] != "settled":
                    raise InferenceAccountingError("accounting_settlement_readback_failed")
                await handle.repository.record_readback(handle.job_id,
                    target_path="inference_accounting:" + handle.request.operation_id,
                    status="succeeded", effect_type="inference_accounting",
                    target_digest=hashlib.sha256(str(persisted["actual_cost_microusd"]).encode()).hexdigest(),
                    verified_at=datetime.now(timezone.utc).isoformat(),
                    details={"verified": True, "operation_id": handle.request.operation_id, "actual_cost_microusd": persisted["actual_cost_microusd"], "memory_status": "no_learning"},
                    owner=handle.owner, fencing_token=handle.fence)
            await handle.repository.transition_job(handle.job_id, state,
                owner=handle.owner, fencing_token=handle.fence,
                reason=row.get("recovery_reason") or (None if adoption_allowed else "inference_result_authority_changed"),
                result={"operation_id": handle.request.operation_id, "state": row["state"], "memory_status": "no_learning"},
                result_summary="Governed inference accounting receipt; no_learning")
        return {**row, "result_adoption_allowed": adoption_allowed}

    def _restore_order(self, handle):
        with self._condition:
            if not hasattr(self, "_durable_order"):
                self._durable_order = {}
            self._durable_order[handle.request.operation_id] = handle.sequence

    async def execute(self, request, operation, **kwargs):
        if not self.durable_accounting:
            return await super().execute(request, operation, **kwargs)
        handle = await self._prepare_accounting(request)
        usage: dict[str, object] = {}
        usage_token = _current_usage.set(usage)
        policy_token = _current_policy_digest.set(handle.policy_digest)
        runtime_token = _current_runtime.set(handle.request.runtime_path)
        billing_token = _near_billing.set({"operation_id": handle.request.operation_id} if handle.request.runtime_path == "near_text_native" else None)
        completed = False

        async def callback():
            await self._contact_accounting(handle)
            result = await operation()
            capture_inference_usage(result)
            if isinstance(result, tuple):
                for item in result:
                    capture_inference_usage(item)
            return result

        try:
            self._restore_order(handle)
            result = await super().execute(handle.request, callback, **kwargs)
            settlement = await self._finish_accounting(handle, payload=usage)
            completed = True
            if not settlement["result_adoption_allowed"]:
                raise InferenceAccountingError("inference_result_authority_changed")
            assert_current_inference_policy()
            return result
        finally:
            try:
                if not completed:
                    await asyncio.shield(self._finish_accounting(handle, payload=usage))
            finally:
                _current_usage.reset(usage_token)
                _current_policy_digest.reset(policy_token)
                _current_runtime.reset(runtime_token)
                _near_billing.reset(billing_token)

    def execute_sync(self, request, operation, **kwargs):
        if request.runtime_path == "near_text_native":
            raise InferenceAccountingError("near_async_nonstreaming_required")
        if not self.durable_accounting:
            return super().execute_sync(request, operation, **kwargs)
        from .execution import _run_awaitable_sync

        handle = _run_awaitable_sync(self._prepare_accounting(request))
        usage: dict[str, object] = {}
        usage_token = _current_usage.set(usage)
        policy_token = _current_policy_digest.set(handle.policy_digest)
        completed = False

        def callback():
            _run_awaitable_sync(self._contact_accounting(handle))
            result = operation()
            capture_inference_usage(result)
            if isinstance(result, tuple):
                for item in result:
                    capture_inference_usage(item)
            return result

        try:
            self._restore_order(handle)
            result = super().execute_sync(handle.request, callback, **kwargs)
            settlement = _run_awaitable_sync(self._finish_accounting(handle, payload=usage))
            completed = True
            if not settlement["result_adoption_allowed"]:
                raise InferenceAccountingError("inference_result_authority_changed")
            assert_current_inference_policy()
            return result
        finally:
            try:
                if not completed:
                    _run_awaitable_sync(self._finish_accounting(handle, payload=usage))
            finally:
                _current_usage.reset(usage_token)
                _current_policy_digest.reset(policy_token)

    async def stream(self, request, operation, **kwargs):
        if request.runtime_path == "near_text_native":
            raise InferenceAccountingError("near_async_nonstreaming_required")
        if not self.durable_accounting:
            async for item in super().stream(request, operation, **kwargs):
                yield item
            return
        handle = await self._prepare_accounting(request)
        usage: dict[str, object] = {}
        usage_token = _current_usage.set(usage)
        policy_token = _current_policy_digest.set(handle.policy_digest)
        completed = False

        async def callback():
            await self._contact_accounting(handle)
            async for item in operation():
                capture_inference_usage(item)
                assert_current_inference_policy()
                yield item

        try:
            self._restore_order(handle)
            async for item in super().stream(handle.request, callback, **kwargs):
                yield item
            settlement = await self._finish_accounting(handle, payload=usage)
            completed = True
            if not settlement["result_adoption_allowed"]:
                raise InferenceAccountingError("inference_result_authority_changed")
            assert_current_inference_policy()
        finally:
            try:
                if not completed:
                    await asyncio.shield(self._finish_accounting(handle, payload=usage))
            finally:
                _current_usage.reset(usage_token)
                _current_policy_digest.reset(policy_token)
