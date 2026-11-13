"""ADR-035 NEAR TEE embedding slot and operator re-index (PR3d).

Provider-free: every NEAR request is answered by the intercepted PR3b fixture
transport (MockTransport on the pinned host, real-network backstop, litellm
refused). No live, paid or catalog network call; no re-index against real
data (canonical memories here are synthetic fixture rows).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from uuid import uuid4

import httpx
import pytest

from config.settings import settings
from src.model_fabric import configuration as fabric_configuration
from src.model_fabric.configuration import (
    NEAR_TEE_EMBEDDING_ALLOWLIST,
    NEAR_TEE_EMBEDDING_EVIDENCE_REQUIRED,
    NearTeeSetup,
    NearTeeSlot,
    near_tee_profile_for_slot,
    read_model_fabric_configuration,
    validate_near_tee_setup,
)
from src.model_fabric.near_tee_catalog import evaluate_catalog, parse_catalog_record
from src.model_fabric.near_tee_embeddings import (
    estimate_input_tokens,
    near_tee_embedding_vectors,
)
from tests.test_near_tee_text_slot import (  # noqa: F401
    _ABSENT,
    PROVIDER_KEY,
    NearFixture,
    _json,
    _login,
    catalog_record,
    isolated_broker,
)
from tests.test_openrouter_setup_api import keyless_openrouter, model_fabric_workspace  # noqa: F401


EMBED_MODEL = "Qwen/Qwen3-Embedding-0.6B"
DIMENSION = 8


def embedding_catalog(**metadata_changes):
    metadata = {"contextLength": 32768, "maxOutputLength": _ABSENT, "supportedFeatures": [], **metadata_changes}
    record = catalog_record(EMBED_MODEL, input_modalities=("text",), output_modalities=("embedding",), **metadata)
    record["inputCostPerToken"] = {"amount": 10, "scale": 9, "currency": "USD"}  # $0.01/M (ADR-035 row)
    record["outputCostPerToken"] = {"amount": 10, "scale": 9, "currency": "USD"}  # lead's read 2026-11-13
    return record


def real_embedding_record():
    """The lead's read of NEAR ``/v1/model/Qwen%2FQwen3-Embedding-0.6B`` on 2026-11-13:
    no ``isReady`` and no dimension field."""
    return {"modelId": EMBED_MODEL,
        "inputCostPerToken": {"amount": 10, "scale": 9, "currency": "USD"},
        "outputCostPerToken": {"amount": 10, "scale": 9, "currency": "USD"},
        "metadata": {"verifiable": True, "contextLength": 32768, "providerType": "vllm",
            "attestationSupported": True,
            "architecture": {"inputModalities": ["text"], "outputModalities": ["embedding"]},
            "maxOutputLength": 1024, "quantization": "bf16", "huggingFaceId": "Qwen/Qwen3-Embedding-0.6B",
            "datacenters": [{"country_code": "US"}]}}


def embedding_slot_payload(**changes):
    body = {"model_id": EMBED_MODEL, "enabled": True, "max_input_tokens": 8192, "max_output_tokens": 1,
        "max_batch_size": 2, "timeout_seconds": 20, "request_cost_bound_microusd": 5000, "catalog_ttl_hours": 24,
        "plaintext_provider_egress_acknowledged": True}
    body.update(changes)
    return body


class EmbeddingFixture(NearFixture):
    """The PR3b NEAR fixture plus ``/v1/embeddings`` and per-model catalog records."""

    def __init__(self):
        super().__init__()
        self.catalog = embedding_catalog()
        self.dimension = DIMENSION
        self.echo_model = None
        self.cost_nano = 1_000_000  # 1000 microUSD per request
        self.counter = 0
        self.omit_id = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode()
        if path != "/v1/embeddings":
            return super().handler(request)
        self.requests.append(request)
        assert request.url.host == "cloud-api.near.ai" and request.url.scheme == "https"
        body = json.loads(request.content)
        inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
        self.counter += 1
        self.body_id = f"embd-fixture-{self.counter}"
        # Deterministic, text-dependent vectors (distinct texts are not near-duplicates).
        data = [{"object": "embedding", "index": index,
            "embedding": [float(sum(text.encode()) % 17 + 1), float(len(text) % 5 + 1)] + [0.5] * (self.dimension - 2)}
            for index, text in enumerate(inputs)]
        payload = {"object": "list", "model": self.echo_model or body["model"], "data": data}
        if not self.omit_id:
            payload["id"] = self.body_id
        return _json(payload)


@pytest.fixture
def evidence_recorded(monkeypatch):
    """Lift the ADR-035 evidence gate for mechanics tests only (the gate's own
    fail-closed behaviour is proven separately with the production value)."""
    monkeypatch.setattr(fabric_configuration, "NEAR_TEE_EMBEDDING_ALIASING_EVIDENCE", "test-fixture-evidence")


@pytest.fixture(autouse=True)
def reset_vector_state():
    from src.memory.near_embedding_reindex import _reset_reindex_state
    from src.memory.vector_store import _reset_vector_store_state
    _reset_vector_store_state()
    _reset_reindex_state()
    yield
    _reset_vector_store_state()
    _reset_reindex_state()


# ── Pure contract checks ─────────────────────────────────────────


def _slot(**changes):
    values = dict(model_id=EMBED_MODEL, max_input_tokens=8192, max_output_tokens=1, max_batch_size=4,
        request_cost_bound_microusd=5000)
    values.update(changes)
    return NearTeeSlot(**values)


def test_embedding_slot_is_allowlisted_bounded_and_disabled_by_default():
    assert set(NEAR_TEE_EMBEDDING_ALLOWLIST) == {EMBED_MODEL}
    validate_near_tee_setup(NearTeeSetup(slots={"embedding": _slot()}))
    assert _slot().enabled is False
    for bad in (dict(model_id="Qwen/Qwen3.8-27B"), dict(model_id="openai/text-embedding-3-small"),
            dict(max_batch_size=None), dict(max_batch_size=65), dict(max_output_tokens=2),
            dict(temperature=0.2), dict(max_input_tokens=32_769)):
        with pytest.raises(ValueError):
            validate_near_tee_setup(NearTeeSetup(slots={"embedding": _slot(**bad)}))
    with pytest.raises(ValueError, match="embedding-only"):
        validate_near_tee_setup(NearTeeSetup(slots={"text": NearTeeSlot(model_id="Qwen/Qwen3.8-27B",
            max_input_tokens=8192, max_output_tokens=1024, request_cost_bound_microusd=20000, max_batch_size=4)}))
    profile = near_tee_profile_for_slot("embedding", _slot(enabled=True))
    assert profile.id == "near_tee.embedding" and profile.capabilities == ("embedding",)
    assert profile.transport_adapter == "openai_compatible_embeddings" and profile.task_classes == ("memory_embedding",)
    assert profile.api_key == ""
    from src.model_fabric.selector import active_provider_exclusion_reason, candidate_from_profile
    assert active_provider_exclusion_reason(profile) is None
    assert candidate_from_profile(profile).endpoint == "https://cloud-api.near.ai/v1/embeddings"
    assert active_provider_exclusion_reason(replace(profile, transport_adapter="openai_compatible_chat")) == \
        "near_tee_transport_adapter_not_allowed"


def test_evidence_gate_is_recorded_and_closed_by_default():
    # The production value: no source evidence is recorded in the repository.
    assert fabric_configuration.NEAR_TEE_EMBEDDING_ALIASING_EVIDENCE is None
    configured = replace(read_model_fabric_configuration(), status="ready", egress_revoked=False,
        near_tee=NearTeeSetup(slots={"embedding": _slot(enabled=True, purpose_consent_revision=1)}))
    assert fabric_configuration.near_tee_slot_block_reason(configured, "embedding") == NEAR_TEE_EMBEDDING_EVIDENCE_REQUIRED


def test_catalog_check_embedding_modality_cost_and_dimension():
    facts = parse_catalog_record(json.dumps(embedding_catalog()).encode(), model_id=EMBED_MODEL)
    # Input-only cost: 8192 x $0.01/M = 81.92 microUSD -> 82.
    assert evaluate_catalog(facts, slot_name="embedding", slot=_slot(enabled=True)) == (None, 82)
    assert evaluate_catalog(facts, slot_name="embedding", slot=_slot(enabled=True, request_cost_bound_microusd=81))[0] \
        == "near_tee_catalog_cost_bound_too_small"
    assert facts.embedding_dimension is None
    no_output_price = embedding_catalog()
    del no_output_price["outputCostPerToken"]
    facts_missing = parse_catalog_record(json.dumps(no_output_price).encode(), model_id=EMBED_MODEL)
    assert evaluate_catalog(facts_missing, slot_name="embedding", slot=_slot(enabled=True))[0] == "near_tee_catalog_price_missing"
    texty = parse_catalog_record(json.dumps(catalog_record(EMBED_MODEL, input_modalities=("text",),
        output_modalities=("text",))).encode(), model_id=EMBED_MODEL)
    assert evaluate_catalog(texty, slot_name="embedding", slot=_slot(enabled=True))[0] == "near_tee_catalog_modality_missing"
    for changes, reason in (({"providerType": "chutes"}, "near_tee_catalog_provider_not_vllm"),
            ({"attestationSupported": False}, "near_tee_catalog_not_attested"),
            ({"isReady": _ABSENT}, "near_tee_catalog_not_ready"),
            ({"contextLength": 4096}, "near_tee_catalog_context_too_small")):
        facts = parse_catalog_record(json.dumps(embedding_catalog(**changes)).encode(), model_id=EMBED_MODEL)
        assert evaluate_catalog(facts, slot_name="embedding", slot=_slot(enabled=True))[0] == reason
    stated = parse_catalog_record(json.dumps(embedding_catalog(embeddingDimension=1024)).encode(), model_id=EMBED_MODEL)
    assert stated.embedding_dimension == 1024
    from src.model_fabric.near_tee_catalog import NearTeeCatalogError
    with pytest.raises(NearTeeCatalogError):
        parse_catalog_record(json.dumps(embedding_catalog(embeddingDimension=1024, dimensions=768)).encode(),
            model_id=EMBED_MODEL)


def test_real_catalog_record_parses_but_is_not_ready():
    # ADR-035's isReady rule keeps the real record failing even after an
    # evidence amendment; that amendment must decide isReady handling.
    facts = parse_catalog_record(json.dumps(real_embedding_record()).encode(), model_id=EMBED_MODEL)
    assert facts.is_ready is None and facts.embedding_dimension is None
    assert facts.output_modalities == ("embedding",) and facts.context_length == 32768
    assert evaluate_catalog(facts, slot_name="embedding", slot=_slot(enabled=True)) == ("near_tee_catalog_not_ready", 82)


def test_vector_parser_checks_echo_count_dimension_and_normalizes():
    payload = {"model": EMBED_MODEL, "data": [{"index": 1, "embedding": [0.0, 2.0]}, {"index": 0, "embedding": [3.0, 4.0]}]}
    vectors, failure = near_tee_embedding_vectors(payload, model=EMBED_MODEL, expected_count=2, dimension=2)
    assert failure is None and vectors == [[0.6, 0.8], [0.0, 1.0]]
    assert near_tee_embedding_vectors({**payload, "model": "Qwen/Qwen3-Embedding-8B"}, model=EMBED_MODEL,
        expected_count=2, dimension=2) == (None, "near_tee_model_mismatch")
    assert near_tee_embedding_vectors(payload, model=EMBED_MODEL, expected_count=2, dimension=3) == \
        (None, "near_tee_embedding_dimension_mismatch")
    assert near_tee_embedding_vectors(payload, model=EMBED_MODEL, expected_count=3, dimension=2) == \
        (None, "near_tee_embedding_count_mismatch")
    zero = {"model": EMBED_MODEL, "data": [{"index": 0, "embedding": [0.0, 0.0]}]}
    assert near_tee_embedding_vectors(zero, model=EMBED_MODEL, expected_count=1, dimension=2)[1] == "near_tee_embedding_invalid"
    assert estimate_input_tokens(["héllo"]) == len("héllo".encode()) + 4


def test_namespace_identity_never_mixes_providers_models_or_dimensions():
    from src.memory.embedder import EMBEDDING_SCHEMA_VERSION, EmbeddingMetadata
    near = EmbeddingMetadata(EMBEDDING_SCHEMA_VERSION, "near_tee", EMBED_MODEL, 1024)
    assert near.namespace != EmbeddingMetadata(EMBEDDING_SCHEMA_VERSION, "openrouter", EMBED_MODEL, 1024).namespace
    assert near.namespace != EmbeddingMetadata(EMBEDDING_SCHEMA_VERSION, "near_tee", EMBED_MODEL, 768).namespace
    assert near.namespace != EmbeddingMetadata(EMBEDDING_SCHEMA_VERSION, "near_tee", "Qwen/Other", 1024).namespace


def test_reindex_batches_run_below_chat_priority():
    from src.model_fabric.gpu_admission import GpuPriority, priority_for_inference_context
    from src.model_fabric.caller_context import canonical_route_spec
    from types import SimpleNamespace
    assert canonical_route_spec("memory_embedding").gpu_priority == "normal"
    embedding = priority_for_inference_context(SimpleNamespace(runtime_path="memory_embedding", workload="background"))
    chat = priority_for_inference_context(SimpleNamespace(runtime_path="chat_agent", workload="interactive"))
    assert chat is GpuPriority.INTERACTIVE_CHAT and embedding.rank < chat.rank


# ── Settings, checks, dispatch and re-index (real vault/accounting/witness) ──


async def _configure(client, **slot_changes):
    lane = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1,
        "inference_lane": {"spend_ceiling_microusd": 1_000_000}})
    assert lane.status_code == 200, lane.text
    saved = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 3,
        "near_tee": {"api_key": PROVIDER_KEY, "slots": {"embedding": embedding_slot_payload(**slot_changes)}}})
    assert saved.status_code == 200, saved.text
    return saved.json()


async def _catalog_check(client):
    response = await client.post("/api/settings/model-fabric/near-tee/catalog-check", json={"expected_policy_revision": 5})
    assert response.status_code == 200, response.text
    return response.json()


async def _capability_check(client, capability):
    return await client.post("/api/settings/model-fabric/canary", json={"profile_id": "near_tee.embedding",
        "capability": capability, "timeout_seconds": 40, "proof_ttl_seconds": 600})


async def _ready(client, monkeypatch):
    near = EmbeddingFixture().install(monkeypatch)
    owner = await _login(client, monkeypatch)
    await _configure(client)
    await _catalog_check(client)
    for capability in ("embedding", "health", "latency_ms"):
        response = await _capability_check(client, capability)
        assert response.status_code == 200 and response.json()["outcome"] == "passed", response.text
    from src.auth.service import authenticate_session
    operator = await authenticate_session(owner["session_id"], touch=False)
    return near, operator


async def _seed_memories(count):
    from src.db.engine import get_session
    from src.db.models import Memory
    ids = []
    async with get_session() as db:
        for index in range(count):
            memory = Memory(content=f"Synthetic fixture memory number {index} about the garden project.")
            db.add(memory)
            ids.append(memory.id)
        await db.commit()
    return sorted(ids)


def _inference_requests(near):
    return [request for request in near.requests if request.method == "POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_evidence_gate_blocks_slot_checks_reindex_and_dispatch(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch):
    from src.approval.runtime import reset_runtime_context, set_runtime_context
    from src.auth.service import authenticate_session
    from src.memory.embedder import EmbeddingConfigurationError, embed_batch
    near = EmbeddingFixture().install(monkeypatch)
    owner = await _login(client, monkeypatch)
    await _configure(client)
    checked = await _catalog_check(client)
    # The catalog check reads the record and passes, but cannot clear the gate.
    assert checked["catalog_check"]["embedding"]["outcome"] == "passed"
    slot = checked["near_tee"]["slots"]["embedding"]
    assert slot["status"] == "blocked" and slot["reason_code"] == NEAR_TEE_EMBEDDING_EVIDENCE_REQUIRED
    assert slot["evidence_required"] is True
    assert checked["near_tee"]["embedding_index"]["recall_mode"] == "lexical"
    assert checked["near_tee"]["embedding_index"]["notice"].startswith("Semantic recall is unavailable")
    refused = await _capability_check(client, "embedding")
    assert refused.status_code == 409 and refused.json()["detail"] == NEAR_TEE_EMBEDDING_EVIDENCE_REQUIRED
    start = await client.post("/api/settings/model-fabric/near-tee/embedding-reindex", json={
        "expected_policy_revision": 5, "budget_microusd": 20000, "max_items": 10,
        "memory_egress_acknowledged": True, "request_uuid": str(uuid4())})
    assert start.status_code == 409 and start.json()["detail"] == NEAR_TEE_EMBEDDING_EVIDENCE_REQUIRED
    operator = await authenticate_session(owner["session_id"], touch=False)
    tokens = set_runtime_context(operator.session_id, "high_risk", trust_principal=operator.principal)
    try:
        with pytest.raises(EmbeddingConfigurationError) as raised:
            await asyncio.to_thread(embed_batch, ["fixture"], principal=operator.principal)
        assert raised.value.reason_code == NEAR_TEE_EMBEDDING_EVIDENCE_REQUIRED
    finally:
        reset_runtime_context(tokens)
    assert _inference_requests(near) == []  # only catalog GETs ever reached NEAR
    assert {request.url.host for request in near.requests} == {"cloud-api.near.ai"}


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_capability_check_measures_dimension_and_dispatch_is_bound(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, isolated_broker, evidence_recorded):
    from src.approval.runtime import reset_runtime_context, set_runtime_context
    from src.memory.embedder import (
        EmbeddingConfigurationError, EmbeddingResponseError, EmbeddingUnavailableError, embed_batch_near_tee,
        embedding_metadata,
    )
    from src.workflows.job_runtime import durable_job_repository

    near, operator = await _ready(client, monkeypatch)
    status = (await client.get("/api/settings/model-fabric")).json()["near_tee"]
    assert status["slots"]["embedding"]["status"] == "ready", status["slots"]["embedding"]
    probe_requests = _inference_requests(near)
    assert [request.url.raw_path.decode() for request in probe_requests] == ["/v1/embeddings", "/v1/billing/costs"] * 3
    assert all(request.headers["x-no-aliasing"] == "true" for request in probe_requests)
    tokens = set_runtime_context(operator.session_id, "high_risk", trust_principal=operator.principal)
    try:
        metadata = await asyncio.to_thread(embedding_metadata)
        assert metadata.provider == "near_tee" and metadata.model == EMBED_MODEL and metadata.dimension == DIMENSION
        before = len(near.requests)
        vectors, cost, used = await asyncio.to_thread(embed_batch_near_tee, ["alpha", "beta"],
            principal=operator.principal)
        assert used == metadata and cost == 1000 and len(vectors) == 2 and all(len(v) == DIMENSION for v in vectors)
        sent = near.requests[before:]
        assert [request.url.raw_path.decode() for request in sent] == ["/v1/embeddings", "/v1/billing/costs"]
        assert all(request.headers["x-no-aliasing"] == "true" for request in sent)
        assert all(request.headers["authorization"] == f"Bearer {PROVIDER_KEY}" for request in sent)
        assert json.loads(sent[0].content) == {"model": EMBED_MODEL, "input": ["alpha", "beta"], "encoding_format": "float"}
        snapshot = await durable_job_repository.inference_accounting_snapshot()
        rows = [row for row in snapshot["operations"] if row["runtime_path"] == "memory_embedding"]
        assert len(rows) == 1 and rows[0]["state"] == "settled" and rows[0]["profile_id"] == "near_tee.embedding"
        assert rows[0]["actual_cost_microusd"] == 1000

        # Over the slot's batch or input bound: rejected before contact.
        before = len(near.requests)
        with pytest.raises(EmbeddingConfigurationError, match="near_tee_embedding_batch_too_large"):
            await asyncio.to_thread(embed_batch_near_tee, ["a", "b", "c"], principal=operator.principal)
        with pytest.raises(EmbeddingConfigurationError, match="near_tee_input_too_large"):
            await asyncio.to_thread(embed_batch_near_tee, ["x" * 9000], principal=operator.principal)
        assert len(near.requests) == before

        # Model echo mismatch: cost settles, nothing adopted, lane free.
        near.echo_model = "Qwen/Qwen3-Embedding-8B"
        with pytest.raises(EmbeddingResponseError, match="near_tee_model_mismatch"):
            await asyncio.to_thread(embed_batch_near_tee, ["gamma"], principal=operator.principal)
        near.echo_model = None
        # A different dimension than the measured proof is rejected.
        near.dimension = DIMENSION + 1
        with pytest.raises(EmbeddingResponseError, match="near_tee_embedding_dimension_mismatch"):
            await asyncio.to_thread(embed_batch_near_tee, ["delta"], principal=operator.principal)
        near.dimension = DIMENSION
        snapshot = await durable_job_repository.inference_accounting_snapshot()
        assert snapshot["unknown_microusd"] == 0
        assert isolated_broker._active_operation_id is None

        # Missing billing: Unknown liability, serial lease stays blocked.
        near.billing_missing = True
        with pytest.raises(EmbeddingUnavailableError) as unknown:
            await asyncio.to_thread(embed_batch_near_tee, ["epsilon"], principal=operator.principal)
        assert unknown.value.reason_code == "near_tee_cost_liability"
        snapshot = await durable_job_repository.inference_accounting_snapshot()
        (row,) = [item for item in snapshot["operations"] if item["state"] == "unknown"]
        assert row["profile_id"] == "near_tee.embedding"
        receipt = isolated_broker.receipt_for(row["operation_id"])
        assert receipt.status == "blocked" and receipt.reconciliation_required
        assert receipt.active_operation_id == row["operation_id"]
    finally:
        reset_runtime_context(tokens)
    # Zero OpenRouter contact: the fixture refuses every other host.
    assert {request.url.host for request in near.requests} == {"cloud-api.near.ai"}


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_response_without_billing_identity_is_unknown(client, model_fabric_workspace, keyless_openrouter,
        monkeypatch, isolated_broker, evidence_recorded):
    from src.approval.runtime import reset_runtime_context, set_runtime_context
    from src.memory.embedder import EmbeddingUnavailableError, embed_batch_near_tee
    near, operator = await _ready(client, monkeypatch)
    near.omit_id = True
    tokens = set_runtime_context(operator.session_id, "high_risk", trust_principal=operator.principal)
    try:
        with pytest.raises(EmbeddingUnavailableError) as raised:
            await asyncio.to_thread(embed_batch_near_tee, ["alpha"], principal=operator.principal)
        assert raised.value.reason_code == "remote_inference_uncertain"
    finally:
        reset_runtime_context(tokens)
    assert isolated_broker._active_operation_id is not None  # lease held until fenced settlement


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_lexical_fallback_without_proof_or_index_makes_no_contact(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, evidence_recorded):
    from src.approval.runtime import reset_runtime_context, set_runtime_context
    from src.auth.service import authenticate_session
    from src.memory.vector_store import add_memory, search_with_status
    near = EmbeddingFixture().install(monkeypatch)
    owner = await _login(client, monkeypatch)
    await _configure(client)
    await _catalog_check(client)
    operator = await authenticate_session(owner["session_id"], touch=False)
    tokens = set_runtime_context(operator.session_id, "high_risk", trust_principal=operator.principal)
    try:
        # No capability proof: no namespace, lexical (degraded) recall, no contact.
        assert await asyncio.to_thread(search_with_status, "garden") == ([], True)
        assert await asyncio.to_thread(add_memory, "garden fact") == ""
        assert _inference_requests(near) == []
        for capability in ("embedding", "health", "latency_ms"):
            assert (await _capability_check(client, capability)).json()["outcome"] == "passed"
        before = len(near.requests)
        # Proof present but no completed re-index: still lexical, no contact.
        assert await asyncio.to_thread(search_with_status, "garden") == ([], True)
        assert await asyncio.to_thread(add_memory, "garden fact") == ""
        assert len(near.requests) == before
        status = (await client.get("/api/settings/model-fabric/near-tee/embedding-reindex")).json()
        assert status["recall_mode"] == "lexical" and status["index_complete"] is False
        assert status["measured_dimension"] == DIMENSION and status["automatic"] is False
    finally:
        reset_runtime_context(tokens)


async def _start(client, *, budget, max_items=100, acknowledged=True, revision=5):
    return await client.post("/api/settings/model-fabric/near-tee/embedding-reindex", json={
        "expected_policy_revision": revision, "budget_microusd": budget, "max_items": max_items,
        "memory_egress_acknowledged": acknowledged, "request_uuid": str(uuid4())})


async def _run_to_end(client, **kwargs):
    from src.memory.near_embedding_reindex import wait_for_reindex
    response = await _start(client, **kwargs)
    assert response.status_code == 200, response.text
    job_id = response.json()["job"]["job_id"]
    await wait_for_reindex(job_id)
    return (await client.get("/api/settings/model-fabric/near-tee/embedding-reindex")).json()


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_reindex_is_consented_budgeted_batched_resumable_and_idempotent(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, isolated_broker, evidence_recorded):
    from src.approval.runtime import reset_runtime_context, set_runtime_context
    from src.memory.vector_store import search_with_status
    from src.workflows.job_runtime import durable_job_repository

    near, operator = await _ready(client, monkeypatch)
    ids = await _seed_memories(5)
    # Explicit consent and a budget that covers at least one request reserve.
    assert (await _start(client, budget=20000, acknowledged=False)).status_code == 403
    assert (await _start(client, budget=4999)).status_code == 422
    assert (await _start(client, budget=2_000_000)).status_code == 422
    assert (await _start(client, budget=20000, revision=4)).status_code == 409
    assert _inference_requests(near)[6:] == []

    # Budget 6500 with a 5000 reserve and 1000 actual per batch: two batches, then stop.
    before = len(near.requests)
    stopped = await _run_to_end(client, budget=6500)
    job = stopped["job"]
    assert job["status"] == "blocked" and job["reason_code"] == "near_tee_reindex_budget_exhausted", job
    assert job["indexed_items"] == 4 and job["batches"] == 2 and job["spent_microusd"] == 2000
    assert stopped["recall_mode"] == "lexical" and stopped["index_complete"] is False
    embedding_posts = [request for request in near.requests[before:] if request.url.raw_path.decode() == "/v1/embeddings"]
    assert [len(json.loads(request.content)["input"]) for request in embedding_posts] == [2, 2]
    assert all(request.headers["x-no-aliasing"] == "true" for request in near.requests[before:])
    durable = await durable_job_repository.get_job(job["job_id"])
    assert durable["job_kind"] == "memory.near-embedding-reindex.v1"
    assert [item["checkpoint_id"] for item in durable["checkpoints"]] == [
        "near-reindex:1", "near-reindex:2", "near-reindex:final"]

    # Resume: a new run skips the four indexed memories and finishes the last.
    before = len(near.requests)
    resumed = await _run_to_end(client, budget=6500)
    assert resumed["job"]["status"] == "succeeded", resumed["job"]
    assert resumed["job"]["skipped_existing"] == 4 and resumed["job"]["indexed_items"] == 1
    assert resumed["index_complete"] is True and resumed["recall_mode"] == "semantic" and resumed["notice"] is None
    sent = [request for request in near.requests[before:] if request.url.raw_path.decode() == "/v1/embeddings"]
    assert len(sent) == 1 and len(json.loads(sent[0].content)["input"]) == 1

    # Idempotent: another run embeds nothing.
    before = len(near.requests)
    again = await _run_to_end(client, budget=6500)
    assert again["job"]["status"] == "succeeded" and again["job"]["indexed_items"] == 0
    assert len(near.requests) == before

    # Semantic recall now uses the NEAR namespace with a NEAR query embedding.
    tokens = set_runtime_context(operator.session_id, "high_risk", trust_principal=operator.principal)
    try:
        before = len(near.requests)
        results, degraded = await asyncio.to_thread(search_with_status, "garden project")
        assert degraded is False and {row["id"] for row in results} == set(ids)
        assert [request.url.raw_path.decode() for request in near.requests[before:]] == ["/v1/embeddings", "/v1/billing/costs"]
    finally:
        reset_runtime_context(tokens)
    # An archived canonical memory leaves the derived index on the next run (no contact).
    from src.db.engine import get_session
    from src.db.models import Memory, MemoryStatus
    from src.memory.embedder import embedding_metadata
    from src.memory.vector_store import namespace_row_texts
    async with get_session() as db:
        memory = await db.get(Memory, ids[0])
        memory.status = MemoryStatus.archived
        await db.commit()
    before = len(near.requests)
    pruned = await _run_to_end(client, budget=6500)
    assert pruned["job"]["status"] == "succeeded" and pruned["index_complete"] is True
    metadata = await asyncio.to_thread(embedding_metadata)
    assert set(await asyncio.to_thread(namespace_row_texts, metadata)) == set(ids[1:])
    assert len(near.requests) == before
    snapshot = await durable_job_repository.inference_accounting_snapshot()
    assert snapshot["unknown_microusd"] == 0
    assert {request.url.host for request in near.requests} == {"cloud-api.near.ai"}


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_reindex_cancel_and_unknown_stop_the_run(client, model_fabric_workspace, keyless_openrouter,
        monkeypatch, isolated_broker, evidence_recorded):
    from src.memory import near_embedding_reindex as reindex

    near, _operator = await _ready(client, monkeypatch)
    await _seed_memories(5)

    # Cancel before the first batch: the run stops with no inference.
    original = reindex.canonical_memory_items
    gate = asyncio.Event()

    async def slow_items():
        await gate.wait()
        return await original()
    monkeypatch.setattr(reindex, "canonical_memory_items", slow_items)
    started = await _start(client, budget=20000)
    job_id = started.json()["job"]["job_id"]
    assert started.json()["job"]["live"] is True
    before = len(near.requests)
    reindex._runners[job_id].cancel.set()
    gate.set()
    cancelled = await client.post("/api/settings/model-fabric/near-tee/embedding-reindex/cancel", json={"job_id": job_id})
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["job"]["status"] == "cancelled"
    assert len(near.requests) == before
    monkeypatch.setattr(reindex, "canonical_memory_items", original)

    # Unknown billing stops the run as a cost liability with the lease blocked.
    near.billing_missing = True
    stopped = await _run_to_end(client, budget=20000)
    assert stopped["job"]["status"] == "cost_liability", stopped["job"]
    assert stopped["job"]["reason_code"] == "near_tee_cost_liability"
    assert stopped["recall_mode"] == "lexical"
    assert isolated_broker._active_operation_id is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_reindex_over_cap_never_completes(client, model_fabric_workspace, keyless_openrouter,
        monkeypatch, isolated_broker, evidence_recorded):
    from src.memory import near_embedding_reindex as reindex
    await _ready(client, monkeypatch)
    await _seed_memories(5)
    monkeypatch.setattr(reindex, "MAX_REINDEX_ITEMS", 3)
    for _ in range(2):
        status = await _run_to_end(client, budget=20000)
        assert status["job"]["status"] == "succeeded", status["job"]
        assert status["index_complete"] is False and status["recall_mode"] == "lexical"
        assert status["index_reason_code"] == "near_tee_reindex_over_cap"


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_add_memory_vectors_count_and_completeness_decays(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, isolated_broker, evidence_recorded):
    from src.approval.runtime import reset_runtime_context, set_runtime_context
    from src.db.engine import get_session
    from src.db.models import Memory
    from src.memory.embedder import embedding_metadata
    from src.memory.vector_store import add_memory, namespace_row_texts, search_with_status

    near, operator = await _ready(client, monkeypatch)
    ids = await _seed_memories(3)
    assert (await _run_to_end(client, budget=20000))["recall_mode"] == "semantic"
    tokens = set_runtime_context(operator.session_id, "high_risk", trust_principal=operator.principal)
    try:
        # A consolidator-style write: canonical row plus an add_memory vector
        # keyed by Memory.embedding_id.
        text = "Synthetic fixture memory about the orchard plan."
        vector_id = await asyncio.to_thread(add_memory, text)
        assert vector_id
        async with get_session() as db:
            memory = Memory(content=text, embedding_id=vector_id)
            db.add(memory)
            await db.commit()
            new_id = memory.id
        status = (await client.get("/api/settings/model-fabric/near-tee/embedding-reindex")).json()
        assert status["recall_mode"] == "semantic", status
        # The add_memory row counts as covered: no re-embedding, no pruning.
        before = len(near.requests)
        again = await _run_to_end(client, budget=20000)
        assert again["job"]["indexed_items"] == 0 and again["index_complete"] is True
        assert len(near.requests) == before
        metadata = await asyncio.to_thread(embedding_metadata)
        stored = await asyncio.to_thread(namespace_row_texts, metadata)
        assert vector_id in stored and new_id not in stored and set(ids) <= set(stored)

        # Editing a canonical memory without a vector decays completeness.
        async with get_session() as db:
            memory = await db.get(Memory, ids[0])
            memory.content = "Edited synthetic fixture memory with new wording."
            await db.commit()
        # Decay happens at write time: search is lexical before anyone opens settings.
        before = len(near.requests)
        assert await asyncio.to_thread(search_with_status, "orchard") == ([], True)
        assert len(near.requests) == before  # lexical, no contact
        stale = (await client.get("/api/settings/model-fabric/near-tee/embedding-reindex")).json()
        assert stale["recall_mode"] == "lexical" and stale["index_reason_code"] == "near_tee_index_stale"
        assert (await _run_to_end(client, budget=20000))["recall_mode"] == "semantic"

        # A failed NEAR-mode add_memory write decays completeness too.
        near.echo_model = "Qwen/Qwen3-Embedding-8B"
        assert await asyncio.to_thread(add_memory, "Another synthetic fixture memory.") == ""
        near.echo_model = None
        failed = (await client.get("/api/settings/model-fabric/near-tee/embedding-reindex")).json()
        assert failed["recall_mode"] == "lexical" and failed["index_reason_code"] == "near_tee_index_vector_missing"
    finally:
        reset_runtime_context(tokens)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_omitted_slot_without_current_consent_never_blocks_another_save(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, evidence_recorded):
    from src.model_fabric.configuration import write_model_fabric_configuration
    from tests.test_near_tee_text_slot import slot_payload
    EmbeddingFixture().install(monkeypatch)
    await _login(client, monkeypatch)
    await _configure(client)
    current = read_model_fabric_configuration()
    stale_slot = replace(current.near_tee.slots["embedding"], purpose_consent_revision=None)
    write_model_fabric_configuration(replace(current, egress_revision=6,
        near_tee=replace(current.near_tee, slots={**current.near_tee.slots, "embedding": stale_slot})), expected_revision=5)
    saved = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 6,
        "near_tee": {"slots": {"text": slot_payload()}}})
    assert saved.status_code == 200, saved.text
    embedding = saved.json()["near_tee"]["slots"]["embedding"]
    assert embedding["enabled"] is True and embedding["consent_current"] is False
    assert embedding["status"] == "blocked" and embedding["reason_code"] == "near_tee_purpose_consent_required"
    assert saved.json()["near_tee"]["slots"]["text"]["consent_current"] is True


def test_state_file_writers_never_lose_updates(model_fabric_workspace):
    """Interleaved writers (threads) on the derived index state file."""
    import threading
    from src.memory import near_embedding_reindex as reindex
    from src.memory.embedder import EMBEDDING_SCHEMA_VERSION, EmbeddingMetadata
    spaces = [EmbeddingMetadata(EMBEDDING_SCHEMA_VERSION, "near_tee", f"fixture/model-{index}", 8) for index in range(4)]
    for metadata in spaces:
        # "indexing" stays live after a decay, so every mark must bump it.
        reindex._update_namespace(metadata, status="indexing")
    marks_per_thread = 25

    def writer(metadata):
        for _ in range(marks_per_thread):
            reindex.mark_namespace_incomplete(metadata, "fixture_decay")
            reindex._remember_job("fixture-job")

    def broadcaster():
        for _ in range(marks_per_thread):
            reindex.note_canonical_memory_change("fixture_change")

    threads = [threading.Thread(target=writer, args=(metadata,)) for metadata in spaces]
    threads.append(threading.Thread(target=broadcaster))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    state = reindex.read_index_state()
    assert state["last_job_id"] == "fixture-job"
    for metadata in spaces:
        entry = state["namespaces"][metadata.namespace]
        assert entry["decay_generation"] == 2 * marks_per_thread and entry["status"] == "indexing"
    leftovers = [path.name for path in reindex._state_path().parent.iterdir() if path.name.endswith(".tmp")]
    assert leftovers == []


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_decay_during_a_run_prevents_completion(client, model_fabric_workspace, keyless_openrouter,
        monkeypatch, isolated_broker, evidence_recorded):
    from src.db.engine import get_session
    from src.db.models import Memory
    from src.memory import vector_store
    await _ready(client, monkeypatch)
    await _seed_memories(2)
    original = vector_store.write_namespace_rows
    created = []

    def write_and_race(metadata, rows):
        result = original(metadata, rows)
        if not created:
            created.append(True)
            # A canonical memory arrives without a vector while the run is
            # indexing (the ORM hook records it on flush).
            async def add():
                async with get_session() as db:
                    db.add(Memory(content="Synthetic fixture memory created during the run."))
                    await db.commit()
            asyncio.run(add())
        return result
    monkeypatch.setattr(vector_store, "write_namespace_rows", write_and_race)
    status = await _run_to_end(client, budget=20000)
    assert status["job"]["status"] == "succeeded"
    assert status["index_complete"] is False and status["recall_mode"] == "lexical"
    assert status["index_reason_code"] == "near_tee_index_changed_during_run"
    monkeypatch.setattr(vector_store, "write_namespace_rows", original)
    resumed = await _run_to_end(client, budget=20000)
    assert resumed["index_complete"] is True and resumed["job"]["indexed_items"] == 1


def test_state_transactions_refuse_nesting_and_skip_writes_when_nothing_is_live(model_fabric_workspace):
    from src.memory import near_embedding_reindex as reindex
    # Hot path: nothing complete or indexing -> one stat, no file written.
    reindex.note_canonical_memory_change()
    assert not reindex._state_path().exists() and not reindex._active_marker_path().exists()
    with reindex._state_transaction() as outer:
        with pytest.raises(RuntimeError, match="nested"):
            with reindex._state_transaction():
                pass
        outer["last_job_id"] = "outer-only"
    assert reindex.read_index_state()["last_job_id"] == "outer-only"
    from src.memory.embedder import EMBEDDING_SCHEMA_VERSION, EmbeddingMetadata
    metadata = EmbeddingMetadata(EMBEDDING_SCHEMA_VERSION, "near_tee", EMBED_MODEL, 8)
    reindex._update_namespace(metadata, status="incomplete")
    assert not reindex._active_marker_path().exists()
    before = reindex._state_path().stat().st_mtime_ns
    reindex.note_canonical_memory_change()
    assert reindex._state_path().stat().st_mtime_ns == before
    reindex._update_namespace(metadata, status="complete")
    assert reindex._active_marker_path().exists()


def _complete_fixture_namespace():
    from src.memory import near_embedding_reindex as reindex
    from src.memory.embedder import EMBEDDING_SCHEMA_VERSION, EmbeddingMetadata
    metadata = EmbeddingMetadata(EMBEDDING_SCHEMA_VERSION, "near_tee", EMBED_MODEL, 8)
    reindex._update_namespace(metadata, status="complete", reason_code=None)
    assert reindex.namespace_complete(metadata.namespace)
    return metadata


def _assert_decayed(metadata):
    from src.memory import near_embedding_reindex as reindex
    entry = reindex.read_index_state()["namespaces"][metadata.namespace]
    assert entry["status"] == "incomplete" and entry["reason_code"] == "near_tee_index_stale", entry


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_every_canonical_memory_write_path_decays_after_commit(async_db, model_fabric_workspace):
    """ORM and Core ``update(Memory)`` paths of the memory repository all decay."""
    from src.db.engine import get_session
    from src.db.models import Memory, MemoryKind, MemoryStatus
    from src.memory.repository import memory_repository

    # create_memory without a vector.
    metadata = _complete_fixture_namespace()
    created = await memory_repository.create_memory(content="Synthetic fixture memory about tea.")
    memory_id = created.memory_id
    _assert_decayed(metadata)
    # A vector attached at creation does not decay.
    metadata = _complete_fixture_namespace()
    await memory_repository.create_memory(content="Synthetic fixture memory with a vector.",
        embedding_id="fixture-vector-id")
    from src.memory import near_embedding_reindex as reindex
    assert reindex.namespace_complete(metadata.namespace)

    # merge_memory: Core update(Memory) setting summary (~repository.py:4475).
    metadata = _complete_fixture_namespace()
    await memory_repository.merge_memory(memory_id, summary="Merged synthetic summary.")
    _assert_decayed(metadata)

    # update_memory_control_metadata: Core update of content (~:7392).
    metadata = _complete_fixture_namespace()
    await memory_repository.update_memory_control_metadata(memory_id, content="Edited synthetic content.")
    _assert_decayed(metadata)

    # Archiving does not decay; reactivation through rollback does (~:7477).
    await memory_repository.update_memory_control_metadata(memory_id, status=MemoryStatus.archived)
    metadata = _complete_fixture_namespace()
    async with get_session() as db:
        row = await db.get(Memory, memory_id)
        expected = (row.updated_at, row.metadata_json, row.status)
    assert reindex.namespace_complete(metadata.namespace)
    await memory_repository.rollback_memory_if_unchanged(memory_id, expected_updated_at=expected[0],
        expected_metadata_json=expected[1], expected_status=expected[2], confidence=0.5, importance=0.5,
        reinforcement=1.0, last_confirmed_at=expected[0], metadata_updates={})
    _assert_decayed(metadata)

    # sync_scoped_memory: create, then the values_builder CAS update (~:4591).
    scope = {"lesson_type": "fixture", "continuity_thread_id": "fixture-thread"}
    await memory_repository.sync_scoped_memory(kind=MemoryKind.procedural, scope=scope,
        content="Scoped synthetic fixture content one.")
    metadata = _complete_fixture_namespace()
    await memory_repository.sync_scoped_memory(kind=MemoryKind.procedural, scope=scope,
        content="Scoped synthetic fixture content two.")
    _assert_decayed(metadata)

    # A rolled-back write never decays.
    metadata = _complete_fixture_namespace()
    with pytest.raises(RuntimeError):
        async with get_session() as db:
            db.add(Memory(content="Never committed synthetic memory."))
            await db.flush()
            raise RuntimeError("fixture rollback")
    assert reindex.namespace_complete(metadata.namespace)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_first_run_has_no_unfenced_window(client, model_fabric_workspace, keyless_openrouter,
        monkeypatch, isolated_broker, evidence_recorded):
    """A change right after the first run captures its generation still blocks completion."""
    from src.db.engine import get_session
    from src.db.models import Memory
    from src.memory import near_embedding_reindex as reindex
    await _ready(client, monkeypatch)
    await _seed_memories(2)
    assert reindex.read_index_state()["namespaces"] == {}
    original = reindex.canonical_memory_items
    raced = []

    async def items_after_a_change():
        if not raced:
            raced.append(True)
            async with get_session() as db:
                db.add(Memory(content="Synthetic memory written right after the run started."))
        return await original()
    monkeypatch.setattr(reindex, "canonical_memory_items", items_after_a_change)
    status = await _run_to_end(client, budget=20000)
    assert status["index_complete"] is False
    assert status["index_reason_code"] == "near_tee_index_changed_during_run"
    assert (await _run_to_end(client, budget=20000))["index_complete"] is True
