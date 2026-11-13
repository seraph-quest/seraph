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
    """The one client shape every NEAR request uses."""
    return httpx.AsyncClient(verify=True, trust_env=False, follow_redirects=False)


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
