"""Shared fixed-endpoint NEAR HTTPS client mechanics (ADR-025, ADR-035).

One owner for the transport rules both NEAR adapters use: the pinned
``https://cloud-api.near.ai/v1`` base, verified TLS, ``trust_env=False``, no
redirects, identity encoding, byte-capped reads, duplicate-key JSON
rejection, the ``x-no-aliasing`` request header and the bounded
``/v1/billing/costs`` readback. It has no SDK, no fallback and no hidden
retry beyond the documented two billing attempts, and makes no attestation
or confidentiality claim: the NEAR gateway receives plaintext.

This module performs no accounting, consent or policy decision itself; the
callers keep those inside their admitted broker lease.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import time
from typing import Any

import httpx


NEAR_API_BASE = "https://cloud-api.near.ai/v1"
NO_ALIASING_HEADER = "x-no-aliasing"
BILLING_PATH = "/billing/costs"
BILLING_RESPONSE_CAP = 16384
BILLING_WINDOW_SECONDS = 15.0
BILLING_ATTEMPTS = 2


def strict_json_object(raw: bytes) -> object:
    """Parse UTF-8 JSON, rejecting duplicate keys and non-finite constants."""

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError("invalid_constant")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)


def near_http_client() -> httpx.AsyncClient:
    """The one client shape every NEAR request uses.

    Bound to a name so the dispatch inventory sees the client's one reviewed
    transfer in ``read_near_response``.
    """
    client = httpx.AsyncClient(verify=True, trust_env=False, follow_redirects=False)
    return client


def near_request_headers(*, key: str | None, no_aliasing: bool, json_body: bool) -> dict[str, str]:
    headers = {"Accept-Encoding": "identity"}
    if json_body:
        headers["Content-Type"] = "application/json"
    if key is not None:
        headers["Authorization"] = "Bearer " + key
    if no_aliasing:
        headers[NO_ALIASING_HEADER] = "true"
    return headers


async def read_near_response(
    client: httpx.AsyncClient,
    path: str,
    *,
    body: bytes | None,
    key: str | None,
    timeout: float,
    cap: int,
    no_aliasing: bool,
    error: Callable[[str], Exception],
    prefix: str,
    method: str = "POST",
) -> tuple[bytes, Any]:
    """One bounded request to the pinned base; returns raw bytes and headers.

    Every failure is a content-safe ``error(code)``; provider bodies are never
    echoed. A transport failure after the request may have reached NEAR is
    ``<prefix>_transport_uncertain``.
    """
    if not path.startswith("/") or "//" in path or "?" in path or "#" in path:
        raise error(prefix + "_request_path_invalid")
    if timeout <= 0:
        raise error(prefix + "_deadline_expired")
    headers = near_request_headers(key=key, no_aliasing=no_aliasing, json_body=body is not None)
    try:
        async with asyncio.timeout(timeout):
            async with client.stream(method, NEAR_API_BASE + path, content=body,
                    headers=headers, timeout=timeout) as response:
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise error(prefix + "_response_encoding_invalid")
                if response.status_code != 200:
                    raise error(prefix + "_http_error")
                raw = bytearray()
                async for chunk in response.aiter_raw():
                    if len(raw) + len(chunk) > cap:
                        raise error(prefix + "_response_too_large")
                    raw.extend(chunk)
                return bytes(raw), response.headers
    except (httpx.HTTPError, TimeoutError):
        raise error(prefix + "_transport_uncertain") from None


STREAM_LINE_CAP = 65536


async def stream_near_events(
    client: httpx.AsyncClient,
    path: str,
    *,
    body: bytes,
    key: str,
    stream_deadline: float,
    cap: int,
    error: Callable[[str], Exception],
    prefix: str,
    on_open: Callable[[Any], None],
):
    """Bounded server-sent-events read from the pinned base (ADR-035 streaming).

    Yields each ``data:`` payload parsed as strict JSON (duplicate keys and
    non-finite constants rejected) and returns normally only at ``[DONE]``.
    ``stream_deadline`` is an event-loop time. Only this generator's own
    awaits (sending the request, each network read) are interrupted by it:
    time the caller spends while the generator is suspended at a yield
    still counts against the deadline but is never cut mid-work, so a
    stalled provider is ended at the next read before the caller's billing
    window. Bytes are buffered across reads and decoded per event, so a
    UTF-8 character split between reads stays intact. Identity encoding,
    HTTP 200 and ``text/event-stream`` are required; redirects are never
    followed; raw bytes and line length are capped. Every failure is a
    content-safe ``error(code)``. ``on_open`` receives the response headers
    as soon as a response exists, before its status, encoding or content
    type is checked: any response means the request reached NEAR (contact),
    so the caller treats a later failure as contacted, never as no-contact.
    """
    loop = asyncio.get_running_loop()

    def remaining() -> float:
        value = stream_deadline - loop.time()
        if value <= 0:
            raise error(prefix + "_stream_deadline")
        return value

    headers = near_request_headers(key=key, no_aliasing=True, json_body=True)
    headers["Accept"] = "text/event-stream"
    request = client.build_request("POST", NEAR_API_BASE + path, content=body, headers=headers,
        timeout=remaining())
    try:
        async with asyncio.timeout(remaining()):
            response = await client.send(request, stream=True, follow_redirects=False)
    except (httpx.HTTPError, TimeoutError):
        raise error(prefix + "_transport_uncertain") from None
    try:
        on_open(response.headers)
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise error(prefix + "_response_encoding_invalid")
        if response.status_code != 200:
            raise error(prefix + "_http_error")
        if response.headers.get("content-type", "").split(";")[0].strip().lower() != "text/event-stream":
            raise error(prefix + "_stream_content_type_invalid")
        iterator = response.aiter_raw().__aiter__()
        total = 0
        buffer = bytearray()
        data_lines: list[bytes] = []
        while True:
            try:
                async with asyncio.timeout(remaining()):
                    chunk = await iterator.__anext__()
            except StopAsyncIteration:
                break
            except TimeoutError:
                raise error(prefix + "_stream_deadline") from None
            except httpx.HTTPError:
                raise error(prefix + "_stream_interrupted") from None
            total += len(chunk)
            if total > cap:
                raise error(prefix + "_response_too_large")
            buffer.extend(chunk)
            while (index := buffer.find(b"\n")) >= 0:
                line = bytes(buffer[:index]).rstrip(b"\r")
                del buffer[:index + 1]
                if line:
                    if line.startswith(b"data:"):
                        data_lines.append(line[5:].removeprefix(b" "))
                    # Comments and other SSE fields carry no answer data.
                    continue
                if not data_lines:
                    continue
                data = b"\n".join(data_lines)
                data_lines = []
                if data.strip() == b"[DONE]":
                    return
                try:
                    payload = strict_json_object(data)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    raise error(prefix + "_stream_invalid") from None
                yield payload
            if len(buffer) > STREAM_LINE_CAP:
                raise error(prefix + "_stream_line_too_large")
        if data_lines and b"\n".join(data_lines).strip() == b"[DONE]":
            return
        # The provider closed without the terminal marker.
        raise error(prefix + "_stream_incomplete")
    finally:
        await response.aclose()


async def read_near_billing(
    client: httpx.AsyncClient,
    *,
    key: str,
    identity: object,
    operation_id: str,
    deadline_at: float,
    no_aliasing: bool,
    error: Callable[[str], Exception],
    prefix: str,
    capture: Callable[[object], None],
):
    """Bounded ``/v1/billing/costs`` readback inside the caller's lease.

    At most two attempts one second apart, within 15 seconds and the original
    deadline. Returns parser-created evidence or ``None`` (an Unknown cost);
    it never estimates a price or replays inference.
    """
    from .near_text_billing import NearTextBillingError, parse_near_billing_evidence

    billing_deadline = min(deadline_at, time.time() + BILLING_WINDOW_SECONDS)
    body = json.dumps({"requestIds": [identity.provider_request_id]}, separators=(",", ":")).encode()
    error_type = type(error("probe"))
    for index in range(BILLING_ATTEMPTS):
        if index:
            if billing_deadline - time.time() <= 1:
                return None
            await asyncio.sleep(1)
        try:
            raw, _ = await read_near_response(client, BILLING_PATH, body=body, key=key,
                timeout=billing_deadline - time.time(), cap=BILLING_RESPONSE_CAP,
                no_aliasing=no_aliasing, error=error, prefix=prefix)
            evidence = parse_near_billing_evidence(response_body=raw,
                original_operation_id=operation_id, inference_identity=identity)
            capture(evidence)
            return evidence
        except (error_type, NearTextBillingError):
            continue
    return None
