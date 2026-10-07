"""NEAR evidence cannot impersonate another provider's durable reservation."""
import json
from dataclasses import replace
import uuid

import pytest

from tests.test_inference_accounting import accounting_db, setup_configuration, request
from src.model_fabric.accounting import capture_near_billing_evidence, current_near_accounting_operation_id
from src.model_fabric.near_text_billing import derive_near_inference_id, parse_near_billing_evidence
from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker, RemoteInferenceBindingError
from src.workflows.job_runtime import DurableJobRepository
from src.workflows.inference_accounting import InferenceAccountingError


def billing(operation):
    identity = derive_near_inference_id(body_id="closed-billing-fixture")
    provider = str(uuid.uuid5(uuid.NAMESPACE_DNS, "closed-billing-fixture"))
    return parse_near_billing_evidence(response_body=json.dumps({"requests": [{"requestId": provider,
        "costNanoUsd": 1001}]}).encode(), original_operation_id=operation, inference_identity=identity)


def test_billing_capture_requires_actual_near_callback():
    with pytest.raises(InferenceAccountingError, match="near_accounting_context_required"):
        current_near_accounting_operation_id()
    with pytest.raises(InferenceAccountingError, match="near_accounting_context_required"):
        capture_near_billing_evidence(billing("foreign-operation"))


@pytest.mark.asyncio
@pytest.mark.parametrize("unsealed", [False, True])
async def test_near_evidence_cannot_settle_openrouter_row(accounting_db, unsealed):
    setup_configuration()
    jobs = DurableJobRepository()
    await jobs.configure_inference_accounting(1000)
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    calls = []
    async def actual_callback():
        calls.append(1)
        return {"id": "gen-original", "usage": {"cost": "0.000007"}}
    await broker.execute(request("original-openrouter"), actual_callback)
    before = await jobs.inference_accounting_snapshot()
    evidence = {"cost_microusd": 0} if unsealed else billing("original-openrouter")
    with pytest.raises(InferenceAccountingError, match="near_billing_(evidence|operation)_invalid"):
        await jobs.settle_inference_cost("original-openrouter", near_billing_evidence=evidence)
    after = await jobs.inference_accounting_snapshot()
    assert after["ledger_digest"] == before["ledger_digest"]
    assert after["revision"] == before["revision"]
    assert after["committed_microusd"] == 7
    assert after["operations"][0]["provider_operation_id"] == "gen-original"
    assert calls == [1]


@pytest.mark.asyncio
async def test_active_near_policy_cannot_create_ephemeral_inference(accounting_db):
    from src.model_fabric.configuration import ModelFabricConfiguration, NearTextSetup, write_model_fabric_configuration
    from src.model_fabric.accounting import bind_accounting_profile
    from src.db.models import WorkflowRunState
    from sqlalchemy import select
    _root, _engine, factory = accounting_db
    write_model_fabric_configuration(ModelFabricConfiguration(status="ready", near_text=NearTextSetup(
        enabled=True, spend_ceiling_microusd=25000, request_cost_bound_microusd=1000,
        plaintext_egress_consent_revision=1)))
    jobs = DurableJobRepository()
    await jobs.configure_inference_accounting(25000)
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    near = replace(request("unlinked-near"), runtime_path="near_text_native")
    bind_accounting_profile(near.operation_id, "near.text")
    calls = []
    async def callback():
        calls.append(1)
        pytest.fail("unlinked NEAR must never reach its provider callback")
    with pytest.raises(RemoteInferenceBindingError, match="near_native_binding_required"):
        await broker.execute(near, callback)
    assert calls == []
    assert (await jobs.inference_accounting_snapshot())["operation_count"] == 0
    async with factory.accounting_sessions() as db:
        assert list((await db.scalars(select(WorkflowRunState))).all()) == []


@pytest.mark.asyncio
async def test_near_cannot_use_sync_or_stream_entrypoints():
    near = replace(request("wrong-entrypoint"), runtime_path="near_text_native")
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    calls = []
    def callback():
        calls.append(1)
    with pytest.raises(InferenceAccountingError, match="near_async_nonstreaming_required"):
        broker.execute_sync(near, callback)
    with pytest.raises(InferenceAccountingError, match="near_async_nonstreaming_required"):
        async for _ in broker.stream(near, callback):
            pytest.fail("streaming must be unavailable")
    assert calls == []


@pytest.mark.parametrize("url", ["https://secret@cloud-api.near.ai/v1", "https://elsewhere.invalid/v1"])
def test_archived_near_destination_cannot_bypass_fixed_privacy_boundary(url):
    from src.workspace.accounting_witness import _assert_credential_free_configuration
    from src.workspace.production import ProductionWorkspaceReconciliationError
    with pytest.raises(ProductionWorkspaceReconciliationError):
        _assert_credential_free_configuration({"near_text": {"api_base": url}})
