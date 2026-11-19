"""Actual HTTPX transport interception; no claim of complete research admission."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from tests.test_inference_accounting import accounting_db, request, setup_configuration
from src.llm_runtime import _governed_research_chat_completion, ProviderProfileConfigurationError
from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
from src.model_fabric.gpu_admission import GpuAdmissionUncertainError
from src.workflows.job_runtime import DurableJobRepository
from src.utils.clock import get_clock
from tests.support.clock import advance_until_done, fake_clock  # noqa: F401
from tests.support.sync import within

# ADR-035: pins the bounded research HTTP envelope over the intercepted historical OpenRouter route
pytestmark = pytest.mark.openrouter_legacy


class ResponseStream(httpx.AsyncByteStream):
    def __init__(self, content, *, delay=0, drop=False):
        self.content, self.delay, self.drop = content, delay, drop
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):
        if self.drop:
            raise httpx.ReadError("intercepted accepted POST dropped before response data")
        for offset in range(0, len(self.content), 4096):
            # The provider's pacing runs on the process clock, so under a fake
            # clock it moves only when the test advances time.
            await get_clock().sleep(self.delay)
            chunk = self.content[offset:offset+4096]
            self.yielded += len(chunk)
            yield chunk

    async def aclose(self):
        self.closed = True


def intercept(monkeypatch, stream, *, status=200, headers=None):
    calls = []
    def handler(http_request):
        calls.append(http_request)
        assert str(http_request.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert http_request.method == "POST"
        assert http_request.headers["accept-encoding"] == "identity"
        return httpx.Response(status, headers=headers, stream=stream)
    original = httpx.AsyncClient
    def client(**kwargs):
        assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        return original(**kwargs, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "AsyncClient", client)
    return calls


def arguments(*, seconds=1):
    return dict(decision=SimpleNamespace(allowed=True, selected=SimpleNamespace(
        adapter="openai_compatible_chat", endpoint="https://openrouter.ai/api/v1/chat/completions",
        profile=SimpleNamespace(provider_kind="openrouter"))),
        context=SimpleNamespace(deadline_at=get_clock().time()+seconds, fallback_allowed=False),
        body={"model": "openai/gpt-4o-mini", "messages": [{"role": "user", "content": "quoted evidence"}], "max_tokens": 1024},
        api_key="intercepted-test-key")


@pytest.mark.asyncio
async def test_bounded_research_http_positive_has_one_actual_post(monkeypatch):
    payload = {"choices": [{"message": {"content": "literal synthesis"}}], "usage": {"cost": "0.000002"}}
    stream = ResponseStream(json.dumps(payload).encode())
    calls = intercept(monkeypatch, stream)
    result, actual = await _governed_research_chat_completion(**arguments())
    assert result.choices[0].message.content == "literal synthesis" and actual == payload
    assert len(calls) == 1 and stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["oversized", "compressed", "truncated", "trickle", "drop", "5xx"])
async def test_actual_http_failure_is_one_post_unknown_and_bounded(accounting_db, monkeypatch, kind, fake_clock):
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    headers = None
    status = 200
    stream = ResponseStream(b"x" * (96 * 1024) if kind == "oversized" else b"{}")
    if kind == "compressed":
        headers = {"content-encoding": "gzip"}
    elif kind == "truncated":
        headers = {"content-length": "100"}
    elif kind == "trickle":
        # Three 4 KiB chunks 0.6 s apart (1.8 s) against a 1.5 s deadline: the
        # body keeps arriving and the absolute deadline still cuts it mid-body.
        stream = ResponseStream(b"x" * (12 * 1024), delay=0.6)
    elif kind == "drop":
        stream = ResponseStream(b"", drop=True)
    elif kind == "5xx":
        status = 503
    calls = intercept(monkeypatch, stream, status=status, headers=headers)
    # The deadline is absolute from here and admission spends part of it
    # before the POST: 0.12s let a slow runner expire it precontact (no POST,
    # the [trickle] flake, #1180). 1.5s reaches the POST and some body bytes.
    kwargs = arguments(seconds=1.5 if kind == "trickle" else 2)
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    # The deadline and the provider's pacing read the fake clock: time moves
    # only from one parked timer to the next, so the proof is the fake time
    # spent, not a wall-clock bound.
    executing = asyncio.ensure_future(
        broker.execute(request("response-"+kind), lambda: _governed_research_chat_completion(**kwargs)))
    if kind == "trickle":
        spent = await advance_until_done(fake_clock, executing, what=f"bounded research HTTP failure ({kind})")
    else:
        # Every other failure must end on its own: the clock is never advanced,
        # so a failure that waited for its deadline would hang to the liveness
        # bound instead of passing. (Advancing here raced the response close,
        # which still has awaits after the deadline timer is armed, #1223.)
        started_at = fake_clock.monotonic()
        await within(asyncio.wait({executing}), what=f"bounded research HTTP failure ({kind}) ending on its own")
        spent = fake_clock.monotonic() - started_at
    with pytest.raises(GpuAdmissionUncertainError):
        await executing
    # The absolute deadline cut the trickle mid-body; every other failure ends
    # on its own, with no time spent.
    assert spent == (1.5 if kind == "trickle" else 0)
    assert len(calls) == 1 and stream.closed
    assert stream.yielded <= 64 * 1024 + 8192
    if kind == "compressed":
        assert stream.yielded == 0
    if kind == "trickle":
        # Data was flowing when the deadline cut it, and it was cut early.
        assert 0 < stream.yielded < 12 * 1024
    snapshot = await repository.inference_accounting_snapshot()
    assert snapshot["accounting_continuity_verified"] is True
    assert snapshot["unpriced_requests"] == 1
    row = snapshot["operations"][0]
    assert row["state"] == "unknown" and row["contact_started_at"] is not None
    assert row["actual_cost_microusd"] is None


@pytest.mark.asyncio
async def test_research_fallback_flag_rejected_before_any_http(monkeypatch):
    stream = ResponseStream(b"{}")
    calls = intercept(monkeypatch, stream)
    kwargs = arguments()
    kwargs["context"].fallback_allowed = True
    with pytest.raises(ProviderProfileConfigurationError):
        await _governed_research_chat_completion(**kwargs)
    assert calls == []


@pytest.mark.asyncio
async def test_final_policy_check_consumes_original_transfer_deadline(monkeypatch, fake_clock):
    stream = ResponseStream(b"{}")
    calls = intercept(monkeypatch, stream)
    def slow_current_policy():
        fake_clock.advance(0.04)  # the final policy check outlasts the original deadline
    monkeypatch.setattr("src.model_fabric.accounting.assert_current_inference_policy", slow_current_policy)
    with pytest.raises(TimeoutError, match="model_fabric_deadline_exceeded"):
        await _governed_research_chat_completion(**arguments(seconds=0.02))
    assert calls == []
