from __future__ import annotations

import asyncio

import httpx
import pytest

from src.security import http_transport
from src.security.http_transport import (
    MAX_FORM_BODY_BYTES,
    PinnedTransportError,
    _TransportLifecycleMarker,
    request_pinned_https,
)


PUBLIC_ADDRESS = "93.184.216.34"


async def public_resolver(_host: str, _port: int) -> list[str]:
    return [PUBLIC_ADDRESS]


@pytest.mark.asyncio
async def test_final_authority_callback_after_dns_blocks_contact():
    order = []
    async def resolver(_host, _port):
        order.append("dns")
        await asyncio.sleep(0)
        return [PUBLIC_ADDRESS]
    async def authority_check():
        order.append("authority")
        raise PermissionError("exact grant revoked while DNS awaited")
    async def forbidden(_request):
        order.append("contact")
        return httpx.Response(200, content=b"forbidden")
    with pytest.raises(PermissionError):
        await request_pinned_https("https://example.com/held", resolver=resolver,
            transport=httpx.MockTransport(forbidden), authority_check=authority_check)
    assert order == ["dns", "authority"]


@pytest.mark.asyncio
async def test_generic_wrapper_keeps_handoff_and_final_authority_callbacks():
    order = []
    async def handoff_check():
        order.append("handoff")
    async def resolver(_host, _port):
        order.append("dns")
        return [PUBLIC_ADDRESS]
    async def authority_check():
        order.append("authority")
    async def contact(_request):
        order.append("contact")
        return httpx.Response(200, content=b"bounded")
    response = await request_pinned_https("https://example.com/held",
        resolver=resolver, transport=httpx.MockTransport(contact),
        handoff_check=handoff_check, authority_check=authority_check)
    assert response.content == b"bounded"
    assert order == ["handoff", "dns", "handoff", "authority", "contact", "handoff", "handoff"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "addresses",
    [
        ["10.0.0.1"],
        [PUBLIC_ADDRESS, "10.0.0.1"],
        ["10.0.0.1", PUBLIC_ADDRESS],
    ],
)
async def test_private_or_mixed_dns_answers_reject_before_transport_contact(addresses):
    contacted = False

    async def resolver(_host: str, _port: int) -> list[str]:
        return addresses

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal contacted
        contacted = True
        return httpx.Response(200, content=b"unexpected")

    with pytest.raises(PinnedTransportError, match="non-global"):
        await request_pinned_https(
            "https://example.com/private-dns",
            resolver=resolver,
            transport=httpx.MockTransport(handler),
        )
    assert contacted is False


@pytest.mark.asyncio
async def test_production_branch_pins_numeric_connection_and_owns_host_sni(monkeypatch):
    captured: dict[str, object] = {}

    class StreamContext:
        def __init__(self, response: httpx.Response) -> None:
            self.response = response
            self.closed = False

        async def __aenter__(self) -> httpx.Response:
            return self.response

        async def __aexit__(self, exc_type, exc, tb) -> None:
            self.closed = True
            await self.response.aclose()

    class InterceptedClient:
        def __init__(self, **kwargs) -> None:
            captured["client_kwargs"] = kwargs
            self.response = httpx.Response(200, content=b"ok")
            self.stream_context = StreamContext(self.response)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def aclose(self) -> None:
            return None

        def stream(self, method, url, *, headers, content, extensions):
            captured["request"] = {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "content": content,
                "extensions": dict(extensions),
            }
            return self.stream_context

    monkeypatch.setattr(http_transport.httpx, "AsyncClient", InterceptedClient)

    response = await request_pinned_https(
        "https://example.com/route?x=1",
        resolver=public_resolver,
    )

    client_kwargs = captured["client_kwargs"]
    request = captured["request"]
    assert client_kwargs["transport"] is None
    assert client_kwargs["follow_redirects"] is False
    assert client_kwargs["trust_env"] is False
    assert request["method"] == "GET"
    assert request["url"] == f"https://{PUBLIC_ADDRESS}/route?x=1"
    assert request["headers"]["Host"] == "example.com"
    assert request["headers"]["Accept-Encoding"] == "identity"
    assert request["extensions"]["sni_hostname"] == "example.com"
    assert response.content == b"ok"
    assert captured["request"]["url"].startswith("https://" + PUBLIC_ADDRESS)


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, wait_for: asyncio.Event | None = None) -> None:
        self.chunks = chunks
        self.wait_for = wait_for
        self.closed = False
        self.started = False
        self.yielded = 0

    async def __aiter__(self):
        self.started = True
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk
        if self.wait_for is not None:
            await self.wait_for.wait()

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_form_body_is_exact_and_transport_owns_encoding_header():
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"ok")

    response = await request_pinned_https(
        "https://oauth2.googleapis.com/token",
        method="POST",
        headers={
            "cOnTeNt-TyPe": "application/x-www-form-urlencoded",
            "CONTENT-TYPE": "application/x-www-form-urlencoded; charset=UTF-8",
        },
        form_body=b"grant_type=refresh_token&client_id=client&refresh_token=1%2F%2Fopaque",
        resolver=public_resolver,
        transport=httpx.MockTransport(handler),
    )

    assert response.content == b"ok"
    assert len(seen) == 1
    assert seen[0].content == (
        b"grant_type=refresh_token&client_id=client&refresh_token=1%2F%2Fopaque"
    )
    assert seen[0].headers["content-type"] == "application/x-www-form-urlencoded"
    assert seen[0].headers.get_list("content-type") == ["application/x-www-form-urlencoded"]
    assert seen[0].headers["accept-encoding"] == "identity"
    assert seen[0].extensions["sni_hostname"] == "oauth2.googleapis.com"
    assert str(seen[0].url) == "https://oauth2.googleapis.com/token"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"method": "GET", "form_body": b"x"},
        {"method": "POST", "json_body": {"x": 1}, "form_body": b"x"},
        {"method": "POST", "form_body": b"x", "headers": {"Accept-Encoding": "gzip"}},
        {"method": "POST", "form_body": b"x", "headers": {"Content-Type": "application/json"}},
        {"method": "POST", "form_body": b"x" * (MAX_FORM_BODY_BYTES + 1)},
    ],
)
async def test_form_contract_rejects_before_dns(kwargs):
    called = False

    async def resolver(_host: str, _port: int) -> list[str]:
        nonlocal called
        called = True
        return [PUBLIC_ADDRESS]

    with pytest.raises(PinnedTransportError):
        await request_pinned_https(
            "https://oauth2.googleapis.com/token",
            resolver=resolver,
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            **kwargs,
        )
    assert called is False


@pytest.mark.asyncio
async def test_json_body_compatibility_has_no_form_cap():
    seen: list[httpx.Request] = []
    large_value = "x" * (MAX_FORM_BODY_BYTES + 1)

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"ok")

    response = await request_pinned_https(
        "https://example.com/json",
        method="POST",
        json_body={"value": large_value},
        resolver=public_resolver,
        transport=httpx.MockTransport(handler),
    )

    assert response.content == b"ok"
    assert len(seen[0].content) > MAX_FORM_BODY_BYTES
    assert seen[0].headers["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_encoded_response_is_rejected_before_body_iteration_and_closed():
    stream = TrackingStream([b"decoded body"])

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=stream,
        )

    with pytest.raises(PinnedTransportError, match="encoded"):
        await request_pinned_https(
            "https://example.com/encoded",
            resolver=public_resolver,
            transport=httpx.MockTransport(handler),
        )
    assert stream.started is False
    assert stream.yielded == 0
    assert stream.closed is True


@pytest.mark.asyncio
async def test_response_cap_checks_before_retaining_overflow_chunk_and_closes():
    stream = TrackingStream([b"1234", b"overflow"])

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    with pytest.raises(PinnedTransportError, match="bounded byte limit"):
        await request_pinned_https(
            "https://example.com/large",
            resolver=public_resolver,
            transport=httpx.MockTransport(handler),
            max_bytes=4,
        )
    assert stream.closed is True
    assert stream.yielded == 2


@pytest.mark.asyncio
async def test_slow_stream_is_bounded_by_one_total_deadline_and_closed():
    release = asyncio.Event()
    stream = TrackingStream([b"first"], wait_for=release)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    with pytest.raises(TimeoutError, match="timed out"):
        await request_pinned_https(
            "https://example.com/slow",
            resolver=public_resolver,
            transport=httpx.MockTransport(handler),
            timeout_seconds=0.01,
        )
    assert stream.closed is True


@pytest.mark.asyncio
async def test_cancellation_closes_stream_context():
    release = asyncio.Event()
    stream = TrackingStream([b"first"], wait_for=release)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    task = asyncio.create_task(
        request_pinned_https(
            "https://example.com/cancel",
            resolver=public_resolver,
            transport=httpx.MockTransport(handler),
            timeout_seconds=1,
        )
    )
    for _ in range(20):
        if stream.started:
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed is True


@pytest.mark.asyncio
async def test_redirect_and_caller_host_override_remain_blocked():
    async def redirect_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://example.com/next"})

    with pytest.raises(PinnedTransportError, match="redirects"):
        await request_pinned_https(
            "https://example.com/start",
            resolver=public_resolver,
            transport=httpx.MockTransport(redirect_handler),
        )

    with pytest.raises(PinnedTransportError, match="Host"):
        await request_pinned_https(
            "https://example.com/start",
            headers={"hOsT": "attacker.example"},
            resolver=public_resolver,
            transport=httpx.MockTransport(redirect_handler),
        )


@pytest.mark.asyncio
async def test_nonfinite_timeout_rejects_before_dns():
    called = False

    async def resolver(_host: str, _port: int) -> list[str]:
        nonlocal called
        called = True
        return [PUBLIC_ADDRESS]

    with pytest.raises(PinnedTransportError, match="finite positive"):
        await request_pinned_https(
            "https://example.com/start",
            timeout_seconds=float("nan"),
            resolver=resolver,
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        )
    assert called is False


@pytest.mark.asyncio
async def test_nonpositive_response_cap_rejects_before_dns():
    called = False

    async def resolver(_host: str, _port: int) -> list[str]:
        nonlocal called
        called = True
        return [PUBLIC_ADDRESS]

    with pytest.raises(PinnedTransportError, match="positive integer"):
        await request_pinned_https(
            "https://example.com/start",
            max_bytes=0,
            resolver=resolver,
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        )
    assert called is False


@pytest.mark.asyncio
async def test_lifecycle_marker_requires_real_httpx_close_for_verified_quiescence():
    marker = _TransportLifecycleMarker()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ok")

    response = await request_pinned_https(
        "https://example.com/lifecycle",
        resolver=public_resolver,
        transport=httpx.MockTransport(handler),
        _lifecycle_marker=marker,
    )

    assert response.content == b"ok"
    assert marker.snapshot() == {
        "status": "verified",
        "active_operations": 0,
        "unsettled_operations": 0,
        "requests_started": 1,
        "requests_settled": 1,
    }


@pytest.mark.asyncio
async def test_lifecycle_marker_reports_active_request_until_stream_and_close_finish():
    release = asyncio.Event()
    stream = TrackingStream([b"first"], wait_for=release)
    marker = _TransportLifecycleMarker()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    task = asyncio.create_task(
        request_pinned_https(
            "https://example.com/lifecycle-active",
            resolver=public_resolver,
            transport=httpx.MockTransport(handler),
            timeout_seconds=1,
            _lifecycle_marker=marker,
        )
    )
    for _ in range(20):
        if stream.started:
            break
        await asyncio.sleep(0)

    assert marker.snapshot() == {
        "status": "unknown",
        "active_operations": 1,
        "unsettled_operations": 1,
        "requests_started": 1,
        "requests_settled": 0,
    }
    release.set()
    await task
    assert marker.snapshot()["status"] == "verified"
    assert marker.snapshot()["active_operations"] == 0
    assert marker.snapshot()["unsettled_operations"] == 0


@pytest.mark.asyncio
async def test_lifecycle_marker_stays_unknown_when_httpx_close_fails():
    marker = _TransportLifecycleMarker()

    class CloseFailingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"ok", request=request)

        async def aclose(self) -> None:
            raise RuntimeError("close failed")

    with pytest.raises(RuntimeError, match="close failed"):
        await request_pinned_https(
            "https://example.com/lifecycle-close-failure",
            resolver=public_resolver,
            transport=CloseFailingTransport(),
            _lifecycle_marker=marker,
        )

    assert marker.snapshot() == {
        "status": "unknown",
        "active_operations": 1,
        "unsettled_operations": 1,
        "requests_started": 1,
        "requests_settled": 0,
    }


@pytest.mark.asyncio
async def test_lifecycle_marker_records_cancellation_only_after_client_close():
    release = asyncio.Event()
    stream = TrackingStream([b"first"], wait_for=release)
    marker = _TransportLifecycleMarker()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    task = asyncio.create_task(
        request_pinned_https(
            "https://example.com/lifecycle-cancel",
            resolver=public_resolver,
            transport=httpx.MockTransport(handler),
            timeout_seconds=1,
            _lifecycle_marker=marker,
        )
    )
    for _ in range(20):
        if stream.started:
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stream.closed is True
    assert marker.snapshot() == {
        "status": "verified",
        "active_operations": 0,
        "unsettled_operations": 0,
        "requests_started": 1,
        "requests_settled": 1,
    }
