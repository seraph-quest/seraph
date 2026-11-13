"""ADR-035 NEAR TEE vision slot and the screen pipeline (PR4).

Provider-free: every NEAR request (catalog, capability checks, inference,
billing) is answered by the intercepted PR3b fixture transport, which fails
the test on any other host (OpenRouter included); real network transports
and litellm are refused outright. No eval, no live or paid call.
"""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
import time

import pytest

from config.settings import settings
from src.model_fabric.configuration import (
    NEAR_TEE_VISION_ALLOWLIST,
    NearTeeSetup,
    NearTeeSlot,
    near_tee_profile_for_slot,
    read_model_fabric_configuration,
    validate_near_tee_setup,
    write_model_fabric_configuration,
)
from src.model_fabric.near_tee_catalog import evaluate_catalog, parse_catalog_record, read_catalog_receipts, write_catalog_receipts
from src.model_fabric.near_tee_vision import estimate_image_tokens, estimate_vision_input_tokens
from tests.test_near_tee_text_slot import (  # noqa: F401  (fixtures)
    PROVIDER_KEY,
    TEXT_MODEL,
    NearFixture,
    _ABSENT,
    _login,
    catalog_record,
    isolated_broker,
    slot_payload,
)
from tests.test_openrouter_setup_api import keyless_openrouter, model_fabric_workspace  # noqa: F401


VISION_MODEL = "Qwen/Qwen3-VL-30B-A3B-Instruct"


def vision_payload(**changes):
    body = slot_payload(model_id=VISION_MODEL, max_input_tokens=16384, max_output_tokens=2048,
        screenshot_plaintext_egress_acknowledged=True)
    body.update(changes)
    return body


def vision_catalog(**changes):
    values = {"contextLength": 16384, "maxOutputLength": 8192, "supportedFeatures": ["structured_outputs", "logprobs"]}
    values.update(changes)
    return catalog_record(VISION_MODEL, **values)


def analysis_json(summary="Editing the NEAR vision adapter in the editor."):
    return json.dumps({"schema_version": "seraph.screenshot_analysis.v1",
        "prompt_version": "seraph.screenshot_analysis.prompt.v1", "summary": summary,
        "activity_type": "coding", "project": "seraph", "applications": ["Code"], "confidence": 0.8})


class VisionFixture(NearFixture):
    """The PR3b NEAR fixture with one catalog record per canonical model."""

    def __init__(self):
        super().__init__()
        self.catalogs = {TEXT_MODEL: catalog_record(), VISION_MODEL: vision_catalog()}
        self.answer = {"role": "assistant", "content": analysis_json(),
            "reasoning_content": "PRIVATE-VISION-REASONING-MUST-NOT-LEAK"}

    def handler(self, request):
        path = request.url.raw_path.decode()
        if request.method == "GET" and path.startswith("/v1/model/"):
            model = next(model for model in self.catalogs if path == "/v1/model/" + model.replace("/", "%2F"))
            self.catalog = self.catalogs[model]
        return super().handler(request)

    def chat_bodies(self):
        return [json.loads(request.content) for request in self.requests
            if request.url.raw_path.decode() == "/v1/chat/completions"]


def _vision_slot(**changes):
    values = dict(model_id=VISION_MODEL, max_input_tokens=16384, max_output_tokens=2048,
        request_cost_bound_microusd=20000, enabled=False)
    values.update(changes)
    return NearTeeSlot(**values)


def _png(size=(64, 48), color=(40, 90, 160)) -> bytes:
    """A deterministic structured image per color, so captures are not visual duplicates."""
    import random
    from PIL import Image
    image = Image.new("RGB", (64, 48), color)
    rng = random.Random(sum(color) * 7919 + color[0])
    for x in range(0, 64, 8):
        for y in range(0, 48, 8):
            shade = rng.choice((0, 255))
            for dx in range(8):
                for dy in range(8):
                    image.putpixel((x + dx, y + dy), (shade, shade, shade))
    if size != (64, 48):
        image = image.resize(size)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ── Pure contract checks ─────────────────────────────────────────


def test_vision_allowlist_profile_and_catalog_rules():
    assert set(NEAR_TEE_VISION_ALLOWLIST) == {"z-ai/glm-5.3-flash", "Qwen/Qwen3.8-27B", VISION_MODEL,
        "deepseek-ai/DeepSeek-V4.1-Flash-1017"}
    for model in NEAR_TEE_VISION_ALLOWLIST:
        validate_near_tee_setup(NearTeeSetup(slots={"vision": _vision_slot(model_id=model, max_input_tokens=8192)}))
    for model in ("Qwen/Qwen3.6-35B-A3B-FP8", "openai/gpt-4o", "z-ai/glm-5.3"):
        with pytest.raises(ValueError, match="near_tee_model_not_allowlisted"):
            validate_near_tee_setup(NearTeeSetup(slots={"vision": _vision_slot(model_id=model)}))
    with pytest.raises(ValueError):  # above the VL model's 16,384 context design basis
        validate_near_tee_setup(NearTeeSetup(slots={"vision": _vision_slot(max_input_tokens=16385)}))
    profile = near_tee_profile_for_slot("vision", _vision_slot(enabled=True))
    assert profile.id == "near_tee.vision" and profile.capabilities == ("vision", "structured_output")
    assert profile.task_classes == ("vision_analysis",) and profile.api_base == "https://cloud-api.near.ai/v1"
    text = near_tee_profile_for_slot("text", NearTeeSlot(model_id=TEXT_MODEL, max_input_tokens=8192,
        max_output_tokens=1024, request_cost_bound_microusd=20000))
    assert text.capabilities == ("text", "structured_output", "streaming", "tool_use") and "vision_analysis" not in text.task_classes

    slot = _vision_slot(enabled=True)
    facts = parse_catalog_record(json.dumps(vision_catalog()).encode(), model_id=VISION_MODEL)
    # 16384 x 0.04/M + 2048 x 1.35/M = 655.36 + 2764.8 microUSD -> 3421
    assert evaluate_catalog(facts, slot_name="vision", slot=slot) == (None, 3421)
    # A text-only model cannot serve the vision slot (no image input modality).
    text_only = parse_catalog_record(json.dumps(vision_catalog(input_modalities=("text",))).encode(), model_id=VISION_MODEL)
    assert evaluate_catalog(text_only, slot_name="vision", slot=slot)[0] == "near_tee_catalog_modality_missing"
    # Incomplete DeepSeek metadata (isReady absent) fails closed.
    deepseek = catalog_record("deepseek-ai/DeepSeek-V4.1-Flash-1017", isReady=_ABSENT)
    facts = parse_catalog_record(json.dumps(deepseek).encode(), model_id="deepseek-ai/DeepSeek-V4.1-Flash-1017")
    assert evaluate_catalog(facts, slot_name="vision", slot=replace(slot,
        model_id="deepseek-ai/DeepSeek-V4.1-Flash-1017"))[0] == "near_tee_catalog_not_ready"
    # A listed per-image price joins the cost bound; a malformed one fails closed.
    priced = vision_catalog()
    priced["costPerImage"] = {"amount": 2000, "scale": 9, "currency": "USD"}  # $0.000002 -> 2 microUSD
    facts = parse_catalog_record(json.dumps(priced).encode(), model_id=VISION_MODEL)
    assert evaluate_catalog(facts, slot_name="vision", slot=slot) == (None, 3423)
    assert evaluate_catalog(facts, slot_name="vision", slot=replace(slot, request_cost_bound_microusd=3422))[0] \
        == "near_tee_catalog_cost_bound_too_small"
    priced["costPerImage"] = {"amount": "x", "scale": 9}
    facts = parse_catalog_record(json.dumps(priced).encode(), model_id=VISION_MODEL)
    assert evaluate_catalog(facts, slot_name="vision", slot=slot)[0] == "near_tee_catalog_response_invalid"


def test_image_tokens_are_counted_conservatively():
    assert estimate_image_tokens(28, 28) == 1 + 64
    assert estimate_image_tokens(2880, 1800) == 103 * 65 + 64
    assert estimate_vision_input_tokens("abc", width=29, height=28) == 1 + 2 + 64
    from src.model_fabric.near_tee import NearTeeDenied
    with pytest.raises(NearTeeDenied):
        estimate_image_tokens(0, 10)


def test_vision_priority_stays_below_chat_and_serial_bound_is_one():
    from types import SimpleNamespace
    from src.model_fabric.gpu_admission import GpuPriority, priority_for_inference_context
    vision = SimpleNamespace(runtime_path="screenshot_image_analysis", workload="vision")
    chat = SimpleNamespace(runtime_path="chat_agent", workload="interactive")
    assert priority_for_inference_context(vision) is GpuPriority.SCREENSHOT_BACKGROUND
    assert GpuPriority.INTERACTIVE_CHAT.rank > GpuPriority.SCREENSHOT_BACKGROUND.rank
    from src.model_fabric.configuration import InferenceLaneConfig, validate_inference_lane
    with pytest.raises(ValueError):
        validate_inference_lane(InferenceLaneConfig(spend_ceiling_microusd=1000, max_inflight=2))


# ── Settings, catalog/capability checks and the screen pipeline ─────────


async def _lane(client):
    lane = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 1,
        "inference_lane": {"spend_ceiling_microusd": 1_000_000}})
    assert lane.status_code == 200, lane.text


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_vision_save_needs_separate_screenshot_consent_and_keeps_text(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch):
    near = VisionFixture().install(monkeypatch)
    await _lane(client)
    text = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 3,
        "near_tee": {"api_key": PROVIDER_KEY, "slots": {"text": slot_payload()}}})
    assert text.status_code == 200, text.text
    unlisted = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 5,
        "near_tee": {"slots": {"vision": vision_payload(model_id="Qwen/Qwen3.6-35B-A3B-FP8")}}})
    assert unlisted.status_code == 422 and unlisted.json()["detail"] == "near_tee_model_not_allowlisted"
    # The text plaintext acknowledgement alone never grants screenshot egress.
    no_screens = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 5,
        "near_tee": {"slots": {"vision": vision_payload(screenshot_plaintext_egress_acknowledged=False)}}})
    assert no_screens.status_code == 403 and no_screens.json()["detail"] == "near_tee_screenshot_acknowledgement_required"
    misplaced = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 5,
        "near_tee": {"slots": {"text": slot_payload(screenshot_plaintext_egress_acknowledged=True)}}})
    assert misplaced.status_code == 422
    saved = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 5,
        "near_tee": {"slots": {"vision": vision_payload()}}})
    assert saved.status_code == 200, saved.text
    tee = saved.json()["near_tee"]
    # The omitted text slot is kept with its still-current consent.
    assert tee["slots"]["text"]["model_id"] == TEXT_MODEL and tee["slots"]["text"]["consent_current"] is True
    vision = tee["slots"]["vision"]
    assert vision["consent_current"] is True and vision["profile_id"] == "near_tee.vision"
    assert vision["reason_code"] == "near_tee_catalog_receipt_missing"
    assert "plaintext" in tee["screenshot_disclosure"] and "not verified by Seraph" in tee["screenshot_disclosure"]
    assert tee["check_capabilities"]["vision"] == ["health", "latency_ms", "structured_output", "vision"]
    # The slot is available but screen analysis stays off until the operator
    # chooses it; the daemon is always told there is no cloud provider.
    from src.observer.screenshot_semantic_analysis import screenshot_semantic_analysis_enabled
    screen = (await client.get("/api/settings/screen-analysis")).json()
    assert screen["provider"] == "" and screen["near_tee_vision_available"] is True
    assert screen["daemon_cloud_provider"] is None and screen["daemon_capture_handoff"] == "local_upload"
    assert screenshot_semantic_analysis_enabled() is False
    put = await client.put("/api/settings/screen-analysis", json={"provider": "near_tee"})
    assert put.status_code == 200 and put.json()["provider"] == "near_tee" and put.json()["model"] == VISION_MODEL
    assert screenshot_semantic_analysis_enabled() is True
    stored = json.loads((model_fabric_workspace / "screen-analysis-settings.json").read_text())
    assert stored["provider"] == "near_tee" and stored["model"] == "" and "daemon_cloud_provider" not in stored
    # An unrelated save keeps the choice; an explicit "" opts out while the slot stays configured.
    assert (await client.put("/api/settings/screen-analysis", json={"enabled": True})).json()["provider"] == "near_tee"
    off = await client.put("/api/settings/screen-analysis", json={"provider": ""})
    assert off.json()["provider"] == "" and screenshot_semantic_analysis_enabled() is False
    assert (await client.get("/api/settings/model-fabric")).json()["near_tee"]["slots"]["vision"]["enabled"] is True

    # An explicit false screenshot acknowledgement on the unchanged, consented
    # vision slot withdraws its consent (saved, then blocked); text is untouched.
    withdrawn = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 7,
        "near_tee": {"slots": {"vision": vision_payload(plaintext_provider_egress_acknowledged=False,
            screenshot_plaintext_egress_acknowledged=False)}}})
    assert withdrawn.status_code == 200, withdrawn.text
    slots = withdrawn.json()["near_tee"]["slots"]
    assert slots["vision"]["consent_current"] is False
    assert slots["vision"]["reason_code"] == "near_tee_purpose_consent_required"
    assert slots["text"]["consent_current"] is True
    # Omitting the field on the next unchanged save does not resurrect it.
    resaved = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 9,
        "near_tee": {"slots": {"text": slot_payload(plaintext_provider_egress_acknowledged=False)}}})
    assert resaved.status_code == 200 and resaved.json()["near_tee"]["slots"]["vision"]["consent_current"] is False
    assert near.requests == []  # settings never contact NEAR


async def _ready_vision(client, monkeypatch, near, *, structured=True):
    """Lane, vision slot, catalog check and vision capability checks (all intercepted)."""
    from src.auth.service import authenticate_session
    owner = await _login(client, monkeypatch)
    await _lane(client)
    saved = await client.put("/api/settings/model-fabric", json={"expected_policy_revision": 3,
        "near_tee": {"api_key": PROVIDER_KEY, "slots": {"vision": vision_payload()}}})
    assert saved.status_code == 200, saved.text
    if not structured:
        near.catalogs[VISION_MODEL] = vision_catalog(supportedFeatures=["logprobs"])
    checked = await client.post("/api/settings/model-fabric/near-tee/catalog-check", json={"expected_policy_revision": 5})
    assert checked.status_code == 200, checked.text
    receipt = checked.json()["catalog_check"]["vision"]
    assert receipt["outcome"] == "passed" and receipt["structured_outputs"] is structured
    assert "image" in receipt["input_modalities"] and receipt["tee_verified"] is False
    for capability in ("vision", "structured_output", "health", "latency_ms"):
        response = await client.post("/api/settings/model-fabric/canary", json={"profile_id": "near_tee.vision",
            "capability": capability, "timeout_seconds": 40, "proof_ttl_seconds": 600})
        assert response.status_code == 200, response.text
        assert response.json()["outcome"] == "passed", response.text
    refused = await client.post("/api/settings/model-fabric/canary", json={"profile_id": "near_tee.vision",
        "capability": "text", "timeout_seconds": 40, "proof_ttl_seconds": 600})
    assert refused.status_code == 422
    canary_vision, canary_structured = near.chat_bodies()[:2]
    assert canary_vision["messages"][0]["content"][1]["type"] == "image_url"
    # The structured_output check runs the runtime's request mode for this receipt.
    assert canary_structured["messages"][0]["content"][1]["type"] == "image_url"
    if structured:
        assert canary_structured["response_format"]["type"] == "json_schema"
        assert canary_structured["response_format"]["json_schema"]["strict"] is True
    else:
        assert "response_format" not in canary_structured
    status = (await client.get("/api/settings/model-fabric")).json()["near_tee"]["slots"]["vision"]
    assert status["status"] == "ready", status
    chosen = await client.put("/api/settings/screen-analysis", json={"provider": "near_tee"})
    assert chosen.status_code == 200 and chosen.json()["provider"] == "near_tee", chosen.text
    return await authenticate_session(owner["session_id"], touch=False)


async def _upload(shots, monkeypatch, *, size=(64, 48), capture_id="cap-vision-0001", color=(40, 90, 160)):
    from src.observer.local_capture_upload import ingest_local_capture
    monkeypatch.setenv("SERAPH_SCREENSHOT_FOLDER", str(shots))
    result = await ingest_local_capture(content=_png(size, color), app_name="Code", window_title="near_tee_vision.py",
        captured_at=datetime.now(timezone.utc) - timedelta(minutes=1), capture_id=capture_id,
        local_analysis={"provider": "apple-vision", "text": "def prepare_near_tee_vision_call("},
        uploader={"device_id": "mac-local-1", "pairing_id": "pair-local-1"})
    assert result.status == "accepted", result
    return result.observation_id


async def _details(observation_id):
    from src.db.engine import get_session
    from src.db.models import ScreenObservation
    from src.observer.screenshot_semantic_analysis import (
        semantic_analysis_from_details, semantic_analysis_status_from_details,
    )
    async with get_session() as db:
        observation = await db.get(ScreenObservation, observation_id)
    details = json.loads(observation.details_json or "[]")
    return observation, semantic_analysis_from_details(details), semantic_analysis_status_from_details(details) or {}


async def _analyze(operator):
    from src.approval.runtime import reset_runtime_context, set_runtime_context
    from src.observer.screenshot_folder_source import analyze_pending_screenshot_folder_observations
    tokens = set_runtime_context(operator.session_id, "high_risk", trust_principal=operator.principal)
    try:
        return await analyze_pending_screenshot_folder_observations(limit=5, concurrency=1)
    finally:
        reset_runtime_context(tokens)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_uploaded_capture_is_analyzed_on_near_vision_and_reaches_the_digest(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, tmp_path):
    from src.scheduler.jobs import screenshot_observation_digest as digest
    from src.workflows.job_runtime import durable_job_repository

    near = VisionFixture().install(monkeypatch)
    operator = await _ready_vision(client, monkeypatch, near)
    observation_id = await _upload(tmp_path / "shots", monkeypatch)
    _observation, analysis, status = await _details(observation_id)
    assert analysis is None and status["status"] == "pending"
    assert status["provider"] == "near_tee" and status["model"] == VISION_MODEL

    before = len(near.requests)
    result = await _analyze(operator)
    assert (result.analyzed, result.failed) == (1, 0)
    sent = near.requests[before:]
    assert [request.url.raw_path.decode() for request in sent] == ["/v1/chat/completions", "/v1/billing/costs"]
    assert all(request.headers["x-no-aliasing"] == "true" for request in sent)
    assert all(request.headers["authorization"] == f"Bearer {PROVIDER_KEY}" for request in sent)
    body = json.loads(sent[0].content)
    assert body["model"] == VISION_MODEL and body["stream"] is False and body["n"] == 1
    assert body["max_tokens"] == 2048 and body["temperature"] == 0.0
    [message] = body["messages"]
    text_part, image_part = message["content"]
    assert text_part["type"] == "text" and "seraph.screenshot_analysis.v1" in text_part["text"]
    assert image_part["type"] == "image_url"
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")
    assert base64.b64decode(image_part["image_url"]["url"].split(",", 1)[1]) == _png()
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["name"] == "seraph_screenshot_analysis_v1"
    assert len(sent[0].content) <= 8 * 1024 * 1024

    observation, analysis, status = await _details(observation_id)
    assert status["status"] == "succeeded" and status["profile_id"] == "near_tee.vision"
    assert status["image"] == {"original_width": 64, "original_height": 48, "sent_width": 64, "sent_height": 48,
        "sent_media_type": "image/png", "reencoded": False}
    assert status["tee_hosting"].startswith("TEE-hosted per NEAR catalog as of ")
    assert analysis["summary"] == "Editing the NEAR vision adapter in the editor."
    assert "PRIVATE-VISION-REASONING" not in (observation.details_json or "")
    snapshot = await durable_job_repository.inference_accounting_snapshot()
    rows = [row for row in snapshot["operations"] if row["runtime_path"] == "screenshot_image_analysis"]
    assert len(rows) == 1 and rows[0]["state"] == "settled" and rows[0]["profile_id"] == "near_tee.vision"
    assert rows[0]["actual_cost_microusd"] == 1500 and snapshot["unknown_microusd"] == 0

    # The analysed upload reaches the digest record; local text is no longer the content.
    start = observation.timestamp.replace(tzinfo=timezone.utc) - timedelta(minutes=5)
    observations = await digest._screenshot_observations(start, start + timedelta(minutes=10))
    payload = digest._digest_payload(start, start + timedelta(minutes=10), observations, digest_key="k")
    [record] = payload["records"]
    assert record["analysis"]["summary"] == "Editing the NEAR vision adapter in the editor."
    assert record["app_name"] == "Code" and record["window_title"] == "near_tee_vision.py"
    assert "local_analysis" not in record and "base64" not in json.dumps(payload)
    assert {request.url.host for request in near.requests} == {"cloud-api.near.ai"}  # zero OpenRouter contact


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_prompt_json_path_and_known_cost_failures_do_not_crash(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, tmp_path, isolated_broker):
    from src.workflows.job_runtime import durable_job_repository

    near = VisionFixture().install(monkeypatch)
    operator = await _ready_vision(client, monkeypatch, near, structured=False)
    shots = tmp_path / "shots"

    # Prompt-JSON: no response_format; a fenced JSON answer still parses strictly.
    near.answer = {"role": "assistant", "content": "```json\n" + analysis_json("Prompt JSON path.") + "\n```"}
    near.body_id = "chatcmpl-prompt-json"
    first = await _upload(shots, monkeypatch, capture_id="cap-vision-0002", color=(10, 200, 30))
    assert (await _analyze(operator)).analyzed == 1
    assert "response_format" not in near.chat_bodies()[-1]
    assert (await _details(first))[1]["summary"] == "Prompt JSON path."

    # Invalid JSON: analysis error recorded, cost settled, lane free, no crash.
    near.answer = {"role": "assistant", "content": "I think the user is coding.",
        "reasoning_content": '{"summary": "reasoning must never be parsed as the answer"}'}
    near.body_id = "chatcmpl-invalid"
    second = await _upload(shots, monkeypatch, capture_id="cap-vision-0003", color=(200, 10, 30))
    result = await _analyze(operator)
    assert (result.analyzed, result.failed) == (0, 1)
    _obs, analysis, status = await _details(second)
    assert analysis is None and status["status"] == "failed"
    assert status["reason"] == "near_tee_vision_output_invalid:prompt_json"
    from src.model_fabric.repository import model_fabric_repository
    route = await model_fabric_repository.latest_route(runtime_path="screenshot_image_analysis")
    assert route.outcome == "failed"
    assert route.attempts[-1].error_code == "near_tee_vision_output_invalid:prompt_json"
    assert isolated_broker._active_operation_id is None

    # Model echo mismatch: contact happened, cost settles, nothing adopted.
    near.answer = {"role": "assistant", "content": analysis_json()}
    near.echo_model = "Qwen/Qwen3-VL-30B-A3B"
    near.body_id = "chatcmpl-echo"
    third = await _upload(shots, monkeypatch, capture_id="cap-vision-0004", color=(90, 10, 230))
    assert (await _analyze(operator)).failed == 1
    assert (await _details(third))[2]["reason"] == "near_tee_model_mismatch"
    near.echo_model = None
    snapshot = await durable_job_repository.inference_accounting_snapshot()
    rows = [row for row in snapshot["operations"] if row["runtime_path"] == "screenshot_image_analysis"]
    assert [row["state"] for row in rows] == ["settled"] * 3 and snapshot["unknown_microusd"] == 0

    # Missing billing: Unknown liability, blocked status, serial lease stays blocked.
    near.billing_missing = True
    near.body_id = "chatcmpl-unknown"
    fourth = await _upload(shots, monkeypatch, capture_id="cap-vision-0005", color=(5, 5, 5))
    await _analyze(operator)
    near.billing_missing = False
    _obs, analysis, status = await _details(fourth)
    assert analysis is None and status["status"] == "blocked"
    snapshot = await durable_job_repository.inference_accounting_snapshot()
    unknown = [row for row in snapshot["operations"] if row["state"] == "unknown"]
    assert len(unknown) == 1 and unknown[0]["profile_id"] == "near_tee.vision"
    receipt = isolated_broker.receipt_for(unknown[0]["operation_id"])
    assert receipt.status == "blocked" and receipt.reconciliation_required
    assert receipt.active_operation_id == unknown[0]["operation_id"]
    assert {request.url.host for request in near.requests} == {"cloud-api.near.ai"}


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_precontact_denials_block_without_contact(client, model_fabric_workspace, keyless_openrouter,
        monkeypatch, tmp_path):
    from src.workflows.job_runtime import durable_job_repository

    near = VisionFixture().install(monkeypatch)
    operator = await _ready_vision(client, monkeypatch, near)
    shots = tmp_path / "shots"
    before = len(near.requests)

    # A slot timeout plus the billing window beyond the background job budget
    # would let the job cancel a contacted request: blocked before contact.
    monkeypatch.setattr(settings, "screenshot_folder_analysis_job_timeout_seconds", 30)
    budget = await _upload(shots, monkeypatch, capture_id="cap-vision-0010", color=(11, 12, 13))
    await _analyze(operator)
    assert (await _details(budget))[2]["reason"] == "remote_inference_blocked:near_tee_vision_timeout_exceeds_job_budget"
    monkeypatch.setattr(settings, "screenshot_folder_analysis_job_timeout_seconds", 120)

    # Input over max_input_tokens (image counted conservatively): blocked before contact.
    from src.observer import screenshot_semantic_analysis as analysis_module
    monkeypatch.setattr(analysis_module, "NEAR_TEE_VISION_MAX_LONG_SIDE", 8192)  # no downscale for this case
    large = await _upload(shots, monkeypatch, size=(4000, 4000), capture_id="cap-vision-0006", color=(1, 2, 3))
    await _analyze(operator)
    assert (await _details(large))[2]["status"] == "blocked"
    assert (await _details(large))[2]["reason"] == "remote_inference_blocked:near_tee_vision_input_too_large"

    # Stale catalog receipt: blocked before contact.
    receipts = read_catalog_receipts()
    receipts["vision"]["checked_at"] -= 25 * 3600
    receipts["vision"]["expires_at"] = time.time() - 1
    write_catalog_receipts(receipts)
    stale = await _upload(shots, monkeypatch, capture_id="cap-vision-0007", color=(4, 5, 6))
    await _analyze(operator)
    assert (await _details(stale))[2]["reason"] == "remote_inference_blocked:near_tee_catalog_receipt_stale"

    # Consent gone (global revoke): the route is unavailable and pending work is blocked.
    current = read_model_fabric_configuration()
    write_model_fabric_configuration(replace(current, egress_revision=current.egress_revision + 1,
        egress_revoked=True), expected_revision=current.egress_revision)
    from src.observer.screenshot_semantic_analysis import screenshot_semantic_analysis_enabled
    assert screenshot_semantic_analysis_enabled() is False
    revoked = await _upload(shots, monkeypatch, capture_id="cap-vision-0008", color=(7, 8, 9))
    assert (await _details(revoked))[2]["status"] == "blocked"
    await _analyze(operator)
    assert (await _details(revoked))[2]["status"] == "blocked"

    assert len(near.requests) == before  # no NEAR (or other) contact for any denial
    snapshot = await durable_job_repository.inference_accounting_snapshot()
    assert [row for row in snapshot["operations"] if row["runtime_path"] == "screenshot_image_analysis"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_large_capture_is_downscaled_for_near_and_stored_unchanged(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, tmp_path):
    from PIL import Image

    near = VisionFixture().install(monkeypatch)
    operator = await _ready_vision(client, monkeypatch, near)
    observation_id = await _upload(tmp_path / "shots", monkeypatch, size=(5120, 2880), capture_id="cap-vision-0020",
        color=(120, 30, 60))
    # 5120x2880 would need 183*103+64 image tokens, over the 16,384 limit.
    assert estimate_image_tokens(5120, 2880) > 16384
    assert (await _analyze(operator)).analyzed == 1
    body = near.chat_bodies()[-1]
    assert len(json.dumps(body).encode()) <= 8 * 1024 * 1024
    url = body["messages"][0]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    sent = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert sent.size == (2048, 1152) and not sent.info.get("exif")
    observation, analysis, status = await _details(observation_id)
    assert analysis is not None
    assert status["image"] == {"original_width": 5120, "original_height": 2880, "sent_width": 2048,
        "sent_height": 1152, "sent_media_type": "image/jpeg", "reencoded": True}
    assert "base64" not in (observation.details_json or "")
    # The stored private capture is unchanged.
    artifacts = next(json.loads(item.removeprefix("capture_artifacts:"))
        for item in json.loads(observation.details_json) if item.startswith("capture_artifacts:"))
    assert Image.open(artifacts["image_path"]).size == (5120, 2880)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_structured_proof_mode_must_match_the_current_receipt(client, model_fabric_workspace,
        keyless_openrouter, monkeypatch, tmp_path):
    from src.model_fabric.configuration import near_tee_profile_for_slot as profile_for
    from src.model_fabric.contracts import transport_endpoint
    from src.model_fabric.repository import model_fabric_repository
    from src.model_fabric.selector import candidate_from_profile

    near = VisionFixture().install(monkeypatch)
    operator = await _ready_vision(client, monkeypatch, near, structured=True)
    profile = profile_for("vision", read_model_fabric_configuration().near_tee.slots["vision"])
    candidate = candidate_from_profile(profile)
    proof = await model_fabric_repository.latest_capability_proof(profile_schema_version=profile.schema_version,
        profile_contract_hash=profile.contract_hash, profile_id=profile.id, model=profile.model,
        endpoint=candidate.endpoint, endpoint_class=candidate.endpoint_class, adapter=candidate.adapter,
        capability="structured_output")
    assert proof.proven_value == "vision_json_schema" and candidate.endpoint == transport_endpoint(profile)
    # NEAR's catalog stops listing structured_outputs: the json_schema proof no
    # longer matches the prompt-JSON mode the runtime would now use.
    near.catalogs[VISION_MODEL] = vision_catalog(supportedFeatures=["logprobs"])
    rechecked = await client.post("/api/settings/model-fabric/near-tee/catalog-check",
        json={"expected_policy_revision": read_model_fabric_configuration().egress_revision})
    assert rechecked.status_code == 200
    assert rechecked.json()["near_tee"]["slots"]["vision"]["reason_code"] == "proof_mode_mismatch:structured_output"
    before = len(near.requests)
    observation_id = await _upload(tmp_path / "shots", monkeypatch, capture_id="cap-vision-0030", color=(70, 20, 200))
    await _analyze(operator)
    status = (await _details(observation_id))[2]
    assert status["status"] == "blocked"
    assert status["reason"] == "remote_inference_blocked:near_tee_vision_structured_proof_mode_mismatch"
    assert len(near.requests) == before  # no contact
    # Re-running the structured_output check in the new mode restores dispatch.
    redo = await client.post("/api/settings/model-fabric/canary", json={"profile_id": "near_tee.vision",
        "capability": "structured_output", "timeout_seconds": 40, "proof_ttl_seconds": 600})
    assert redo.json()["outcome"] == "passed"
    assert "response_format" not in near.chat_bodies()[-1]
    redone = await model_fabric_repository.latest_capability_proof(profile_schema_version=profile.schema_version,
        profile_contract_hash=profile.contract_hash, profile_id=profile.id, model=profile.model,
        endpoint=candidate.endpoint, endpoint_class=candidate.endpoint_class, adapter=candidate.adapter,
        capability="structured_output")
    assert redone.proven_value == "vision_prompt_json"
    assert (await client.get("/api/settings/model-fabric")).json()["near_tee"]["slots"]["vision"]["status"] == "ready"


def test_decode_bomb_dimensions_are_refused_before_decode():
    import pytest as _pytest

    from src.observer.screenshot_semantic_analysis import (
        NEAR_TEE_VISION_MAX_DECODE_PIXELS,
        ScreenshotSemanticAnalysisError,
        _prepare_vision_image,
    )

    side = int(NEAR_TEE_VISION_MAX_DECODE_PIXELS ** 0.5) + 10
    with _pytest.raises(ScreenshotSemanticAnalysisError, match="image_dimensions_too_large"):
        _prepare_vision_image(b"\x89PNG not decoded", "image/png", side, side)
