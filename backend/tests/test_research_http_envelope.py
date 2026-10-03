"""Actual HTTPX transport interception; no claim of complete research admission."""
import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from tests.test_inference_accounting import accounting_db, request, setup_configuration
from src.llm_runtime import _governed_research_chat_completion, ProviderProfileConfigurationError
from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
from src.model_fabric.gpu_admission import GpuAdmissionUncertainError
from src.workflows.job_runtime import DurableJobRepository


class ResponseStream(httpx.AsyncByteStream):
    def __init__(self, content, *, delay=0, drop=False):
        self.content, self.delay, self.drop = content, delay, drop
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):
        if self.drop:
            raise httpx.ReadError("intercepted accepted POST dropped before response data")
        for offset in range(0, len(self.content), 4096):
            await asyncio.sleep(self.delay)
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
        adapter="openai_compatible_chat", endpoint="https://openrouter.ai/api/v1/chat/completions")),
        context=SimpleNamespace(deadline_at=time.time()+seconds, fallback_allowed=False),
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
async def test_actual_http_failure_is_one_post_unknown_and_bounded(accounting_db, monkeypatch, kind):
    setup_configuration()
    repository = DurableJobRepository()
    await repository.configure_inference_accounting(1000)
    headers = None
    status = 200
    stream = ResponseStream(b"x" * (96 * 1024) if kind == "oversized" else b"{}")
    if kind == "compressed":
        headers = {"content-encoding": "gzip"}
    elif kind == "truncated":
        headers = {"content-length": "100"}
    elif kind == "trickle":
        stream = ResponseStream(b"x" * (12 * 1024), delay=0.08)
    elif kind == "drop":
        stream = ResponseStream(b"", drop=True)
    elif kind == "5xx":
        status = 503
    calls = intercept(monkeypatch, stream, status=status, headers=headers)
    kwargs = arguments(seconds=0.12 if kind == "trickle" else 2)
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    started = time.monotonic()
    with pytest.raises(GpuAdmissionUncertainError):
        await broker.execute(request("response-"+kind), lambda: _governed_research_chat_completion(**kwargs))
    assert time.monotonic()-started < 3
    assert len(calls) == 1 and stream.closed
    assert stream.yielded <= 64 * 1024 + 8192
    if kind == "compressed":
        assert stream.yielded == 0
    snapshot = await repository.inference_accounting_snapshot()
    assert snapshot["accounting_continuity_verified"] is True
    assert snapshot["unknown_microusd"] == 100
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
async def test_final_policy_check_consumes_original_transfer_deadline(monkeypatch):
    stream = ResponseStream(b"{}")
    calls = intercept(monkeypatch, stream)
    def slow_current_policy():
        time.sleep(0.04)
    monkeypatch.setattr("src.model_fabric.accounting.assert_current_inference_policy", slow_current_policy)
    with pytest.raises(TimeoutError, match="model_fabric_deadline_exceeded"):
        await _governed_research_chat_completion(**arguments(seconds=0.02))
    assert calls == []
