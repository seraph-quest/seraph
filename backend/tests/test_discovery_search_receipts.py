"""Actual owned loopback HTTP bodies through the pinned search adapter."""
from dataclasses import FrozenInstanceError, asdict
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import time

import httpx
import pytest

from src.guardian.discovery_search import DiscoverySearch, DiscoverySearchBlocked, SEARCH_URL
from src.guardian.research_plan_contracts import SearchManifestV1


BODY = b'<a class="result__a" href="https://example.com/public">Public evidence</a>'
RUN_ID = "c8440f78-5b26-468f-8866-7ed6c797fbf6"


@pytest.fixture
def actual_search_http():
    controls = {"bodies": [BODY], "delay": 0}
    contacts = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            assert self.path == "/html/" and self.headers["Host"] == "html.duckduckgo.com"
            assert "Authorization" not in self.headers and "Cookie" not in self.headers
            body = self.rfile.read(int(self.headers["Content-Length"]))
            index = len(contacts)
            contacts.append(body)
            raw = controls["bodies"][min(index, len(controls["bodies"]) - 1)]
            time.sleep(controls["delay"])
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The timed-out owned client is already closed.

    class OwnedServer(ThreadingHTTPServer):
        daemon_threads = False

    server = OwnedServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server.server_port, controls, contacts
    finally:
        server.shutdown()
        server.server_close()  # Wait for every actual response worker.
        thread.join(timeout=5)
        assert not thread.is_alive()


class LocalResponse(httpx.AsyncByteStream):
    def __init__(self, response, client):
        self.response, self.client = response, client

    async def __aiter__(self):
        async for chunk in self.response.aiter_raw():
            yield chunk

    async def aclose(self):
        await self.response.aclose()
        await self.client.aclose()


def search_adapter(port):
    class LocalTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert str(request.url) == SEARCH_URL
            assert request.extensions["sni_hostname"] == "html.duckduckgo.com"
            client = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(), trust_env=False,
                follow_redirects=False, timeout=5)
            outgoing = client.build_request("POST", f"http://127.0.0.1:{port}/html/",
                headers=request.headers, content=request.content)
            try:
                response = await client.send(outgoing, stream=True)
            except BaseException:
                await client.aclose()
                raise
            return httpx.Response(response.status_code, headers=response.headers,
                stream=LocalResponse(response, client))

    async def resolver(host, port):
        assert (host, port) == ("html.duckduckgo.com", 443)
        return ["93.184.216.34"]
    return DiscoverySearch(resolver=resolver, transport=LocalTransport())


async def allowed():
    pass


@pytest.mark.asyncio
async def test_actual_bodies_have_distinct_receipts_and_unchanged_closed_manifest(actual_search_http):
    port, controls, contacts = actual_search_http
    controls["bodies"] = [BODY, BODY + b"\n"]
    observed = []
    async def receipt(value):
        observed.append(value)
    result = await search_adapter(port).search(["public query one", "public query two"],
        run_id=RUN_ID, authority_check=allowed, response_receipt=receipt)
    assert len(contacts) == 2 and result.response_receipts == tuple(observed)
    manifest = SearchManifestV1.model_validate(result.manifest)
    assert len(manifest.results) == 1 and set(result.manifest) == {"run_id", "query_digest", "results"}
    for index, body in enumerate(controls["bodies"]):
        value = observed[index]
        assert value.query_index == index and value.byte_count == len(body)
        assert value.response_digest == hashlib.sha256(body).hexdigest()
        assert value.query_digest == hashlib.sha256(["public query one", "public query two"][index].encode()).hexdigest()
        assert set(asdict(value)) == {"query_index", "query_digest", "response_digest", "byte_count"}
        assert "public query" not in repr(value) and "result__a" not in repr(value)
    assert observed[0].response_digest != observed[1].response_digest
    with pytest.raises(FrozenInstanceError):
        observed[0].byte_count = 0


@pytest.mark.asyncio
async def test_narrow_bytes_are_per_request_not_combined_stage_budget(actual_search_http):
    port, _, contacts = actual_search_http
    result = await search_adapter(port).search(["first public query", "second public query"],
        run_id=RUN_ID, authority_check=allowed, max_search_bytes=len(BODY))
    assert len(contacts) == 2 and len(result.response_receipts) == 2
    assert sum(receipt.byte_count for receipt in result.response_receipts) == 2 * len(BODY)


@pytest.mark.asyncio
async def test_narrow_seconds_are_per_request_with_original_wall_intersection(actual_search_http):
    port, controls, contacts = actual_search_http
    controls["delay"] = 0.6
    start = time.monotonic()
    result = await search_adapter(port).search(["first public query", "second public query"],
        run_id=RUN_ID, authority_check=allowed, max_search_seconds=1,
        remaining_seconds=lambda: 5 - (time.monotonic() - start))
    assert len(contacts) == 2 and len(result.response_receipts) == 2
    assert time.monotonic() - start >= 1.2  # Aggregate one-second semantics would deny this.


@pytest.mark.asyncio
async def test_narrow_byte_overflow_has_no_complete_body_receipt(actual_search_http):
    port, _, contacts = actual_search_http
    observed = []
    async def receipt(value): observed.append(value)
    with pytest.raises(DiscoverySearchBlocked, match="search_response_byte_cap"):
        await search_adapter(port).search(["bounded public query"], run_id=RUN_ID,
            authority_check=allowed, max_search_bytes=len(BODY) - 1, response_receipt=receipt)
    assert len(contacts) == 1 and observed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining", [5, 0.15])
async def test_narrow_time_or_original_wall_stops_actual_request_without_receipt(actual_search_http, remaining):
    port, controls, contacts = actual_search_http
    controls["delay"] = 1.5
    observed = []
    async def receipt(value): observed.append(value)
    start = time.monotonic()
    with pytest.raises(DiscoverySearchBlocked, match="search_timeout"):
        await search_adapter(port).search(["bounded public query"], run_id=RUN_ID,
            authority_check=allowed, max_search_seconds=1, remaining_seconds=remaining,
            response_receipt=receipt)
    assert time.monotonic() - start < min(1, remaining) + 0.6
    assert len(contacts) == 1 and observed == []


@pytest.mark.asyncio
async def test_actual_captcha_receipt_precedes_finite_parser_denial(actual_search_http):
    port, controls, contacts = actual_search_http
    raw = b'<form id="challenge-form">CAPTCHA</form>'
    controls["bodies"] = [raw]
    observed = []
    async def receipt(value): observed.append(value)
    with pytest.raises(DiscoverySearchBlocked) as failure:
        await search_adapter(port).search(["bounded public query"], run_id=RUN_ID,
            authority_check=allowed, response_receipt=receipt)
    assert failure.value.reason == "search_captcha" and len(contacts) == 1
    assert len(observed) == 1 and observed[0].response_digest == hashlib.sha256(raw).hexdigest()
    assert observed[0].byte_count == len(raw)


@pytest.mark.asyncio
async def test_original_receipt_owner_denial_prevents_parse_and_next_contact(actual_search_http):
    port, _, contacts = actual_search_http
    observed = []
    async def receipt(value):
        observed.append(value)
        raise PermissionError("original owner changed")
    with pytest.raises(PermissionError, match="original owner changed"):
        await search_adapter(port).search(["first public query", "second public query"],
            run_id=RUN_ID, authority_check=allowed, response_receipt=receipt)
    assert len(contacts) == 1 and len(observed) == 1
