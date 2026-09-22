"""Pinned, bounded HTTPS transport for public source reads.

The caller supplies a resolver in tests.  Production requests resolve once,
reject non-global addresses, and connect to the selected numeric address
while retaining the original hostname for Host and TLS SNI.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit
from typing import Any, Awaitable, Callable, Iterable, Mapping

import httpx


MAX_RESPONSE_BYTES = 256 * 1024
MAX_REDIRECTS = 0
DEFAULT_TIMEOUT_SECONDS = 10.0


class PinnedTransportError(ValueError):
    """A source request failed a transport boundary."""


@dataclass(frozen=True)
class PinnedResponse:
    url: str
    status_code: int
    headers: dict[str, str]
    content: bytes
    pinned_address: str


Resolver = Callable[[str, int], Awaitable[Iterable[str]] | Iterable[str]]


async def default_resolver(hostname: str, port: int) -> list[str]:
    records = await asyncio.to_thread(
        socket.getaddrinfo,
        hostname,
        port,
        type=socket.SOCK_STREAM,
    )
    addresses: list[str] = []
    for record in records:
        sockaddr = record[4]
        if sockaddr:
            addresses.append(str(sockaddr[0]))
    return list(dict.fromkeys(addresses))


def _parse_public_url(url: str) -> SplitResult:
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise PinnedTransportError("source must be an HTTPS URL with a hostname")
    if parsed.username or parsed.password:
        raise PinnedTransportError("source URL credentials are not allowed")
    if parsed.port not in (None, 443):
        raise PinnedTransportError("source URL must use HTTPS port 443")
    if parsed.fragment:
        raise PinnedTransportError("source URL fragments are not allowed")
    return parsed


def parse_public_https_url(url: str) -> SplitResult:
    """Validate and parse one public HTTPS source URL at admission time."""

    return _parse_public_url(url)


def _global_address(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:
        raise PinnedTransportError("resolver returned an invalid address") from exc
    if not parsed.is_global:
        raise PinnedTransportError("source resolved to a non-global address")
    return parsed


async def _resolve(resolver: Resolver, hostname: str, port: int) -> list[str]:
    values = resolver(hostname, port)
    if hasattr(values, "__await__"):
        values = await values  # type: ignore[assignment]
    result = [str(value) for value in values]
    if not result:
        raise PinnedTransportError("source hostname did not resolve")
    # Validate every returned address, rather than selecting a safe address
    # while silently accepting an attacker-controlled private answer.
    for value in result:
        _global_address(value)
    return result


async def fetch_pinned_https(
    url: str,
    *,
    resolver: Resolver = default_resolver,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> PinnedResponse:
    """Fetch one public HTTPS response without redirects or ambient proxies."""

    return await request_pinned_https(
        url,
        method="GET",
        resolver=resolver,
        transport=transport,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
        headers={"Accept": "text/plain, text/html, application/xhtml+xml"},
    )


async def request_pinned_https(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    json_body: Any | None = None,
    resolver: Resolver = default_resolver,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    connect_timeout_seconds: float | None = None,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> PinnedResponse:
    """Issue one bounded GET/POST over the same pinned HTTPS boundary.

    ``transport`` is an explicit constructor seam for local tests. It is never
    read from configuration, and production clients always connect to the
    resolver-selected numeric address while retaining the logical hostname for
    Host and TLS SNI.
    """

    parsed = _parse_public_url(url)
    normalized_method = str(method or "GET").upper()
    if normalized_method not in {"GET", "POST"}:
        raise PinnedTransportError("only GET and POST are supported")
    if json_body is not None and normalized_method != "POST":
        raise PinnedTransportError("JSON request bodies are only allowed for POST")
    resolve_timeout = float(timeout_seconds)
    if connect_timeout_seconds is not None:
        resolve_timeout = min(resolve_timeout, float(connect_timeout_seconds))
    try:
        addresses = await asyncio.wait_for(
            _resolve(resolver, parsed.hostname or "", parsed.port or 443),
            timeout=resolve_timeout,
        )
    except asyncio.TimeoutError as exc:
        raise TimeoutError("source DNS resolution timed out") from exc
    pinned = addresses[0]
    # ASGI/mock transports need the logical URL so tests can route it.  A real
    # network client uses the pinned address and an explicit Host header.
    if transport is None:
        pinned_host = f"[{pinned}]" if ":" in pinned else pinned
        request_url = urlunsplit(
            ("https", pinned_host, parsed.path or "/", parsed.query, "")
        )
        request_headers = {"Host": parsed.hostname or ""}
    else:
        request_url = url
        request_headers = {}
    for key, value in (headers or {}).items():
        if str(key).lower() == "host":
            raise PinnedTransportError("Host is owned by the pinned transport")
        request_headers[str(key)] = str(value)
    request_headers.setdefault("Accept", "*/*")
    request_content: bytes | None = None
    if json_body is not None:
        request_content = json.dumps(
            json_body,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    async with httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(
            float(timeout_seconds),
            connect=(
                min(float(timeout_seconds), float(connect_timeout_seconds))
                if connect_timeout_seconds is not None
                else float(timeout_seconds)
            ),
        ),
    ) as client:
        response = await client.request(
            normalized_method,
            request_url,
            headers=request_headers,
            content=request_content,
            extensions={"sni_hostname": parsed.hostname or ""},
        )
        if 300 <= response.status_code < 400:
            raise PinnedTransportError("redirects are disabled for source watches")
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > int(max_bytes):
                raise PinnedTransportError("source response exceeds the bounded byte limit")
    return PinnedResponse(
        url=url,
        status_code=response.status_code,
        headers={str(k).lower(): str(v) for k, v in response.headers.items()},
        content=bytes(content),
        pinned_address=pinned,
    )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_RESPONSE_BYTES",
    "PinnedResponse",
    "PinnedTransportError",
    "default_resolver",
    "fetch_pinned_https",
    "parse_public_https_url",
    "request_pinned_https",
]
