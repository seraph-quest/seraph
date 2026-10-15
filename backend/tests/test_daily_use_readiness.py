"""Provider-free daily-use readiness regressions for issue #1068.

No test here contacts a provider: admission is decided by the canonical
selector, and scheduled jobs are replaced with recorders that fail if invoked.
"""

import asyncio
import json
import time
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.settings import settings
from src.agent.direct_chat import (
    OPENROUTER_CHAT_ROUTE_BLOCKED_MESSAGE,
    openrouter_route_block_reason,
    openrouter_route_block_recovery,
    openrouter_route_blocked_message,
    route_error_from,
)
from src.api import model_fabric_settings as model_fabric_api
from src.model_fabric import candidate_from_profile, select_route
from src.model_fabric.caller_context import build_canonical_inference_context
from src.model_fabric.configuration import (
    DEFAULT_OPENROUTER_CONTEXT_WINDOW_TOKENS,
    OPENROUTER_SETUP_V2_SCHEMA_VERSION,
    ModelFabricConfiguration,
    OpenRouterRoute,
    OpenRouterSetup,
    _configuration_from_payload,
    _configuration_payload,
    openrouter_policy_for_setup,
    openrouter_profiles_for_setup,
)
from src.model_fabric.contracts import ModelCapability, NoCompliantModelRouteError
from src.model_fabric.proofs import build_model_route_proof
from src.scheduler import background_inference
from src.scheduler.engine import _async_job_wrapper
from src.security.trust_contract import AuthorityGrant, EgressClass, PrincipalType, TrustPrincipal

# The capability set the Settings panel now proposes for a new text route.
DEFAULT_TEXT_CAPABILITIES = ("text", "tool_use", "structured_output", "streaming")
# Selector tests use one fixed clock for profile pricing time, proofs, request
# deadline and the decision, so wall-clock drift cannot change the result.
NOW = 1_800_000_000.0


def _route(**changes) -> OpenRouterRoute:
    values = {
        "model_id": "openrouter/anthropic/claude-sonnet-4",
        "enabled": True,
        "capabilities": DEFAULT_TEXT_CAPABILITIES,
        "allowed_upstreams": ("anthropic",),
        "temperature": 0.7,
        "max_output_tokens": 4096,
        "timeout_seconds": 120.0,
        "zero_data_retention": False,
        "request_cost_bound_microusd": 50_000,
    }
    values.update(changes)
    return OpenRouterRoute(**values)


def _setup(route: OpenRouterRoute | None = None) -> OpenRouterSetup:
    return OpenRouterSetup(
        schema_version=OPENROUTER_SETUP_V2_SCHEMA_VERSION,
        routes={"text": route or _route(), "vision": None, "embedding": None},
        purpose_consents={},
        egress_class=EgressClass.CLOUD_ALLOWED_FULL,
        cloud_egress_acknowledged=True,
        spend_ceiling_microusd=5_000_000,
    )


def _proofs(profile, *, now: float, expires_at: float | None = None):
    candidate = candidate_from_profile(profile)
    values = {capability: "supported" for capability in profile.capabilities}
    values.update({"health": "healthy", "latency_ms": 900})
    return tuple(
        build_model_route_proof(
            profile=profile,
            endpoint_class=candidate.endpoint_class,
            adapter=candidate.adapter,
            capability=capability,
            canary_version="seraph.manual-canary.v1",
            outcome="passed",
            checked_at=now - 60,
            expires_at=expires_at if expires_at is not None else now + 86_000,
            probe_receipt_id=f"probe-{capability}",
            probe_receipt_hash="c" * 64,
            proven_value=value,
        )
        for capability, value in values.items()
    )


PRINCIPAL = TrustPrincipal(
    principal_id="operator-daily-use",
    principal_type=PrincipalType.OPERATOR,
    grants=(AuthorityGrant.MODEL_INFERENCE,),
    session_id="session-daily-use",
)


def _context(setup, *, payload, streaming=False, extra=()):
    principal = PRINCIPAL
    with patch(
        "src.model_fabric.caller_context.effective_workload_policy",
        return_value=openrouter_policy_for_setup(setup, "chat_agent"),
    ):
        return build_canonical_inference_context(
            "chat_agent",
            payload=payload,
            output_tokens=512,
            timeout_seconds=settings.agent_chat_timeout,
            principal=principal,
            session_id=principal.session_id,
            streaming=streaming,
            extra_capabilities=extra,
        )


def _fixed_context(setup, **kwargs):
    return replace(_context(setup, **kwargs), deadline_at=NOW + 600)


def _text_profile(setup):
    (profile,) = openrouter_profiles_for_setup(setup)
    return replace(profile, cost_source_updated_at=NOW - 60)


def test_default_text_route_admits_streaming_chat_and_tool_using_agent_turns():
    setup = _setup()
    profile = _text_profile(setup)
    now = NOW
    proofs = _proofs(profile, now=now)
    # About 60 KB of conversation and tool schema: far above the old
    # output-cap-derived 4096-token window, within the decoupled default.
    payload = {"messages": [{"role": "user", "content": "x" * 60_000}]}

    streaming = _fixed_context(setup, payload=payload, streaming=True)
    agent = _fixed_context(setup, payload=payload, extra=(ModelCapability.TOOL_USE,))
    assert "streaming" in streaming.requirements.capabilities
    assert "tool_use" in agent.requirements.capabilities
    assert streaming.requirements.context_tokens > 4096

    for context in (streaming, agent):
        decision = select_route(context, (candidate_from_profile(profile),), proofs, now=now)
        assert decision.allowed, decision.rejections


def test_old_narrow_default_is_rejected_for_both_turn_kinds():
    setup = _setup(_route(capabilities=("text", "structured_output")))
    profile = _text_profile(setup)
    now = NOW
    proofs = _proofs(profile, now=now)
    for kwargs in ({"streaming": True}, {"extra": (ModelCapability.TOOL_USE,)}):
        context = _fixed_context(setup, payload={"message": "hi"}, **kwargs)
        decision = select_route(context, (candidate_from_profile(profile),), proofs, now=now)
        assert not decision.allowed
        assert decision.rejections[0].reason_code == "capability_not_declared"


def test_context_window_is_explicit_or_defaulted_not_output_cap():
    defaulted = _text_profile(_setup(_route(max_output_tokens=1024)))
    assert defaulted.context_window_tokens == DEFAULT_OPENROUTER_CONTEXT_WINDOW_TOKENS
    explicit = _text_profile(_setup(_route(context_window_tokens=200_000)))
    assert explicit.context_window_tokens == 200_000
    with pytest.raises(ValueError, match="context window"):
        _text_profile(_setup(_route(context_window_tokens=10)))


def test_stored_route_without_context_window_reads_and_round_trips_unchanged():
    configuration = ModelFabricConfiguration(openrouter_setup=_setup(), updated_at="2026-10-15T00:00:00+00:00")
    payload = _configuration_payload(configuration)
    stored_route = payload["openrouter_setup"]["routes"]["text"]
    assert "context_window_tokens" not in stored_route
    assert "context_window_tokens" not in payload["openrouter_setup"]
    reread = _configuration_from_payload(json.loads(json.dumps(payload)))
    assert reread.openrouter_setup.routes["text"].context_window_tokens is None
    assert _configuration_payload(reread) == payload

    explicit = replace(configuration, openrouter_setup=_setup(_route(context_window_tokens=131_072)))
    explicit_payload = _configuration_payload(explicit)
    assert explicit_payload["openrouter_setup"]["routes"]["text"]["context_window_tokens"] == 131_072
    assert _configuration_from_payload(explicit_payload).openrouter_setup.routes["text"].context_window_tokens == 131_072


def test_expired_proof_yields_specific_rejection_and_recovery_message():
    setup = _setup()
    profile = _text_profile(setup)
    now = NOW
    expired = _proofs(profile, now=now, expires_at=now - 1)
    context = _fixed_context(setup, payload={"message": "hi"}, streaming=True)
    decision = select_route(context, (candidate_from_profile(profile),), expired, now=now)
    reason = decision.rejections[0].reason_code
    assert reason == "proof_expired:health"

    error = NoCompliantModelRouteError(tuple(item.reason_code for item in decision.rejections))
    assert str(error) == "no_compliant_route"
    message = openrouter_route_blocked_message(error)
    assert "health capability proof for the text route expired" in message
    assert "Prove route" in message and "billed" in message
    assert openrouter_route_block_recovery(openrouter_route_block_reason(error))[1] == "prove_route"

    missing = select_route(context, (candidate_from_profile(profile),), (), now=now)
    assert missing.rejections[0].reason_code == "proof_missing:health"
    assert openrouter_route_block_recovery("proof_missing:health")[1] == "prove_route"


def test_unknown_route_reason_keeps_configuration_guidance():
    assert openrouter_route_blocked_message(NoCompliantModelRouteError()) == OPENROUTER_CHAT_ROUTE_BLOCKED_MESSAGE
    assert openrouter_route_block_recovery("capability_not_declared")[1] == "declare_capability"
    assert openrouter_route_block_recovery("profile_limit_unknown_or_insufficient:context_tokens")[1] == "raise_context_window"


def test_route_error_is_unwrapped_from_agent_generation_error():
    from smolagents.utils import AgentGenerationError

    route = NoCompliantModelRouteError(("proof_expired:tool_use",))
    try:
        try:
            raise route
        except NoCompliantModelRouteError as exc:
            raise AgentGenerationError("Error in generating model output", MagicMock()) from exc
    except AgentGenerationError as wrapped:
        assert route_error_from(wrapped) is route
    assert route_error_from(RuntimeError("other")) is None


def test_canary_proof_ttl_accepts_daily_maximum_only():
    request = model_fabric_api.CapabilityCanaryRequest(profile_id="openrouter.text", capability="text", proof_ttl_seconds=86_400)
    assert request.proof_ttl_seconds == 86_400
    with pytest.raises(ValueError):
        model_fabric_api.CapabilityCanaryRequest(profile_id="openrouter.text", capability="text", proof_ttl_seconds=86_401)


@pytest.fixture
def background_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    background_inference._last_status.clear()
    yield tmp_path
    background_inference._last_status.clear()


def test_background_jobs_default_on_with_estimates(background_workspace):
    status = background_inference.background_inference_status()
    assert status["error_code"] is None
    jobs = {job["job_id"]: job for job in status["jobs"]}
    assert set(jobs) >= {"strategist_tick", "memory_consolidation", "daily_briefing", "evening_review",
        "end_of_day_goal_report", "screenshot_observation_digest"}
    assert all(job["enabled"] for job in jobs.values())
    assert jobs["strategist_tick"]["approx_calls_per_day"] == round(1440 / settings.strategist_interval_min, 2)
    assert jobs["daily_briefing"]["approx_calls_per_day"] == 1.0


def test_background_toggles_persist_and_unreadable_file_fails_closed(background_workspace):
    background_inference.write_background_inference_settings({"strategist_tick": False})
    enabled, error = background_inference.read_background_inference_settings()
    assert error is None and enabled["strategist_tick"] is False and enabled["daily_briefing"] is True
    with pytest.raises(ValueError):
        background_inference.write_background_inference_settings({"not_a_job": False})

    background_inference.background_inference_settings_path().write_text("{broken", encoding="utf-8")
    enabled, error = background_inference.read_background_inference_settings()
    assert error == background_inference.SETTINGS_UNREADABLE
    assert not any(enabled.values())


@pytest.mark.parametrize("job_id", [job.job_id for job in background_inference.BACKGROUND_INFERENCE_JOBS if job.scope == "job"])
def test_disabled_background_job_makes_zero_model_calls(background_workspace, job_id):
    background_inference.write_background_inference_settings({job_id: False})
    transport_calls = []

    async def job():
        # Stands in for the real job body: any run would reach the transport.
        transport_calls.append(job_id)

    async def run():
        audit = AsyncMock()
        with patch("src.audit.runtime.log_scheduler_job_event", audit):
            await _async_job_wrapper(job, asyncio.get_running_loop(), job_id=job_id, allow_model_inference=True)()
        return audit

    audit = asyncio.run(run())
    assert transport_calls == []
    audit.assert_awaited_once_with(job_name=job_id, outcome="skipped", details={"reason": "disabled_by_operator"})
    status = {job["job_id"]: job for job in background_inference.background_inference_status()["jobs"]}
    assert status[job_id]["enabled"] is False
    assert status[job_id]["last_skip"]["reason"] == "disabled_by_operator"

    background_inference.write_background_inference_settings({job_id: True})
    asyncio.run(run())
    assert transport_calls == [job_id]


def test_disabled_end_of_day_analysis_makes_zero_model_calls(background_workspace):
    from src.scheduler.jobs import end_of_day_goal_report as report_job

    background_inference.write_background_inference_settings({"end_of_day_goal_report": False})
    completion = AsyncMock(side_effect=AssertionError("provider transport must not be reached"))
    with patch.object(report_job, "completion_with_fallback", completion), \
            patch.object(settings, "end_of_day_report_llm_enabled", True):
        from src.scheduler.screen_llm_policy import screen_derived_llm_decision

        decision = asyncio.run(screen_derived_llm_decision("end_of_day_goal_report"))
    assert decision.allowed is False
    assert decision.reason == "disabled_by_operator"
    completion.assert_not_called()
    status = {job["job_id"]: job for job in background_inference.background_inference_status()["jobs"]}
    assert status["end_of_day_goal_report"]["last_skip"]["reason"] == "disabled_by_operator"


def test_route_error_ignores_an_earlier_handled_denial_in_implicit_context():
    # A route error handled earlier in the turn must not be reported as the
    # reason for an unrelated later failure raised while handling it.
    try:
        try:
            raise NoCompliantModelRouteError(("proof_expired:health",))
        except NoCompliantModelRouteError:
            raise RuntimeError("tool failed")
    except RuntimeError as unrelated:
        assert unrelated.__context__ is not None
        assert route_error_from(unrelated) is None


def test_unreviewed_overrun_names_review_recovery_and_mid_turn_is_honest():
    error = NoCompliantModelRouteError(("provider_charge_exceeded_reservation",))
    message, action = openrouter_route_block_recovery(openrouter_route_block_reason(error))
    assert action == "review_cost_overrun"
    assert "billed above its request cost bound" in message
    assert "raise the request cost bound to at least the billed charge shown there" in message
    assert message.endswith("No model call was made.")

    mid_turn = openrouter_route_blocked_message(error, provider_contacted=True)
    assert "No model call was made" not in mid_turn
    assert "Earlier steps of this turn had already called the model" in mid_turn
    generic_mid_turn = openrouter_route_blocked_message(NoCompliantModelRouteError(), provider_contacted=True)
    assert "before any provider request" not in generic_mid_turn


def test_accounting_overrun_denial_keeps_its_reason_on_the_route_error():
    from src.llm_runtime import _route_denial_error
    from src.workflows.inference_accounting import InferenceAccountingError

    accounting = InferenceAccountingError("provider_charge_exceeded_reservation")
    error = _route_denial_error([], accounting)
    assert error.reason_codes == ("provider_charge_exceeded_reservation",)
    assert error.__cause__ is accounting
    assert openrouter_route_block_recovery(openrouter_route_block_reason(error))[1] == "review_cost_overrun"
    # Selector denials still win, and unrelated errors add no reason.
    assert _route_denial_error(["proof_missing:text"], accounting).reason_codes == ("proof_missing:text",)
    assert _route_denial_error([], RuntimeError("boom")).reason_codes == ()


def test_request_contact_registry_is_scoped_to_the_registered_request():
    from src import llm_runtime

    llm_runtime._register_request("agent-ws:contact-test")
    token = llm_runtime.set_current_llm_request_id("agent-ws:contact-test")
    try:
        assert llm_runtime.request_provider_contacted("agent-ws:contact-test") is False
        llm_runtime._note_provider_contact()
        assert llm_runtime.request_provider_contacted("agent-ws:contact-test") is True
        assert llm_runtime.request_provider_contacted("agent-ws:other") is False
    finally:
        llm_runtime.reset_current_llm_request_id(token)
        llm_runtime._finish_request("agent-ws:contact-test")
    assert llm_runtime.request_provider_contacted("agent-ws:contact-test") is False


def _live_profile_and_proofs(setup):
    """Real-clock profile and fresh proofs for paths that call the selector unpinned."""
    (profile,) = openrouter_profiles_for_setup(setup)
    proofs = {proof.capability: proof for proof in _proofs(profile, now=time.time())}
    return profile, proofs


@pytest.mark.asyncio
async def test_real_direct_chat_stream_requests_what_the_default_text_route_admits():
    from src.agent import direct_chat

    setup = _setup()
    profile, proofs = _live_profile_and_proofs(setup)
    captured = {}

    async def intercepted_transport_owner(**kwargs):
        # The owned boundary below direct chat; nothing beyond it runs.
        captured.update(kwargs)
        yield "hello"

    with (
        patch.object(direct_chat, "stream_completion_with_fallback", intercepted_transport_owner),
        patch.object(direct_chat, "_uses_openrouter_profile", return_value=True),
        patch("src.model_fabric.caller_context.get_current_trust_principal", return_value=PRINCIPAL),
        patch("src.model_fabric.caller_context.effective_workload_policy",
              return_value=openrouter_policy_for_setup(setup, "chat_agent")),
        patch("src.agent.session.session_manager.get_task_continuity_context", AsyncMock(return_value="")),
    ):
        deltas = [delta async for delta in direct_chat.stream_direct_local_chat(
            "hello there", runtime_path="chat_agent", is_onboarding=False, session_id=PRINCIPAL.session_id)]

    assert deltas == ["hello"]
    context = captured["request_context"]
    assert "streaming" in context.requirements.capabilities
    decision = select_route(context, (candidate_from_profile(profile),), tuple(proofs.values()))
    assert decision.allowed, decision.rejections


@pytest.mark.asyncio
async def test_direct_chat_stream_reports_unreviewed_overrun_as_no_contact_block():
    from src.agent import direct_chat
    from src.workflows.inference_accounting import InferenceAccountingError

    setup = _setup()

    async def refused(**_kwargs):
        raise InferenceAccountingError("provider_charge_exceeded_reservation")
        yield  # pragma: no cover

    with (
        patch.object(direct_chat, "stream_completion_with_fallback", refused),
        patch.object(direct_chat, "_uses_openrouter_profile", return_value=True),
        patch("src.model_fabric.caller_context.get_current_trust_principal", return_value=PRINCIPAL),
        patch("src.model_fabric.caller_context.effective_workload_policy",
              return_value=openrouter_policy_for_setup(setup, "chat_agent")),
        patch("src.agent.session.session_manager.get_task_continuity_context", AsyncMock(return_value="")),
        pytest.raises(NoCompliantModelRouteError) as raised,
    ):
        async for _delta in direct_chat.stream_direct_local_chat(
                "hello there", runtime_path="chat_agent", is_onboarding=False, session_id=PRINCIPAL.session_id):
            pass
    assert raised.value.reason_codes == ("provider_charge_exceeded_reservation",)


def test_real_agent_generate_tool_turn_is_admitted_by_the_default_text_route(monkeypatch):
    """Run FallbackLiteLLMModel.generate through the real canonical preflight.

    Only the proof store, the admission broker and the governed transport are
    intercepted; the request context, capabilities and route selection are real.
    """
    from types import SimpleNamespace

    from src import llm_runtime
    from src.model_fabric.repository import model_fabric_repository

    monkeypatch.setattr(settings, "openrouter_api_key", "fixture-openrouter-key")
    setup = _setup()
    profile, proofs = _live_profile_and_proofs(setup)
    requested_capabilities = []
    captured = {}

    async def latest_capability_proof(*, capability, **_kwargs):
        requested_capabilities.append(capability)
        return proofs.get(capability)

    def transport(*, decision, context, body, api_key):
        captured.update(decision=decision, context=context, body=body)
        return (
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]),
            {"choices": [{"message": {"content": "ok"}}], "usage": {"cost": "0.000002"}},
        )

    tools = [{"type": "function", "function": {"name": "read_goals", "description": "Read goals",
        "parameters": {"type": "object", "properties": {}}}}]
    with (
        patch.object(llm_runtime, "provider_profiles", return_value={profile.id: profile}),
        patch.object(llm_runtime, "_fallback_targets", return_value=[]),
        patch.object(llm_runtime, "get_current_trust_principal", return_value=PRINCIPAL),
        patch("src.model_fabric.caller_context.effective_workload_policy",
              return_value=openrouter_policy_for_setup(setup, "chat_agent")),
        patch.object(model_fabric_repository, "latest_capability_proof", side_effect=latest_capability_proof),
        patch.object(llm_runtime, "_execute_sync_with_gpu_admission", side_effect=lambda **kw: kw["operation"]()),
        patch.object(llm_runtime, "_new_route_receipt_session", return_value=None),
        patch.object(llm_runtime, "_can_log_request", return_value=False),
        patch.object(llm_runtime, "_governed_openai_chat_completion", side_effect=transport),
    ):
        model = llm_runtime.FallbackLiteLLMModel(
            model_id=profile.routing_model, api_base=profile.api_base, api_key="fixture-openrouter-key",
            runtime_profile=profile.id, runtime_path="chat_agent", max_tokens=512,
        )
        model.generate([{"role": "user", "content": "what are my goals?"}], tools_to_call_from=tools)

    assert "tool_use" in captured["context"].requirements.capabilities
    assert "tool_use" in requested_capabilities
    assert captured["decision"].allowed
    assert captured["body"]["tools"] == tools


@pytest.mark.parametrize("trigger", ["post_response", "workflow_completed", "pre_compaction", "session_end"])
def test_disabled_memory_consolidation_gates_every_flush_trigger(background_workspace, trigger):
    from src.memory import flush

    background_inference.write_background_inference_settings({"memory_consolidation": False})
    consolidate = AsyncMock(side_effect=AssertionError("consolidation model call must not run"))
    fingerprint = AsyncMock(side_effect=AssertionError("disabled flush must not read the session"))
    with patch("src.memory.consolidator.consolidate_session", consolidate), \
            patch.object(flush, "_build_session_flush_fingerprint", fingerprint):
        if trigger == "workflow_completed":
            # The workflow manager's synchronous entry point.
            assert flush.flush_session_memory_sync(session_id="session-1", trigger=trigger, workflow_name="wf") is False
        else:
            assert asyncio.run(flush.flush_session_memory("session-1", trigger=trigger)) is False
    consolidate.assert_not_called()
    status = {job["job_id"]: job for job in background_inference.background_inference_status()["jobs"]}
    assert status["memory_consolidation"]["last_skip"]["reason"] == "disabled_by_operator"


def test_enabled_memory_consolidation_still_flushes(background_workspace):
    from src.memory import flush

    flush._reset_memory_flush_state()
    result = MagicMock(should_cache_fingerprint=True)
    consolidate = AsyncMock(return_value=result)
    fingerprint = AsyncMock(return_value=flush.SessionFlushFingerprint(cache_key="session-1:t", fingerprint="f1"))
    with patch("src.memory.consolidator.consolidate_session", consolidate), \
            patch.object(flush, "_build_session_flush_fingerprint", fingerprint):
        assert asyncio.run(flush.flush_session_memory("session-1", trigger="post_response")) is True
    consolidate.assert_awaited_once()
    flush._reset_memory_flush_state()


def test_overrun_message_names_the_minimum_bound_and_reproving():
    from src.agent.direct_chat import microusd_dollars

    message, action = openrouter_route_block_recovery(
        "provider_charge_exceeded_reservation", minimum_bound_microusd=612_340)
    assert action == "review_cost_overrun"
    assert "at least $0.61234 (612,340 µUSD, the largest unreviewed charge)" in message
    assert "press Prove route again" in message
    assert microusd_dollars(1_000_000) == "1" and microusd_dollars(50_000) == "0.05"


@pytest.mark.asyncio
async def test_overrun_minimum_is_read_only_for_overrun_blocks():
    from src.agent.direct_chat import route_block_overrun_minimum
    from src.workflows.job_runtime import durable_job_repository

    snapshot = AsyncMock(return_value={"status": "blocked", "overrun_max_cost_microusd": 612_340})
    with patch.object(durable_job_repository, "inference_accounting_snapshot", snapshot):
        assert await route_block_overrun_minimum(
            NoCompliantModelRouteError(("provider_charge_exceeded_reservation",))) == 612_340
        assert await route_block_overrun_minimum(NoCompliantModelRouteError(("proof_missing:text",))) is None
    snapshot.assert_awaited_once()
    with patch.object(durable_job_repository, "inference_accounting_snapshot", AsyncMock(side_effect=RuntimeError("x"))):
        assert await route_block_overrun_minimum(
            NoCompliantModelRouteError(("provider_charge_exceeded_reservation",))) is None
