"""Pinned, bounded HTTPS transport for public source reads.

The caller supplies a resolver in tests.  Production requests resolve once,
reject non-global addresses, and connect to the selected numeric address
while retaining the original hostname for Host and TLS SNI.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import socket
import threading
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit
from typing import Any, Awaitable, Callable, Iterable, Mapping

import httpx


MAX_RESPONSE_BYTES = 256 * 1024
MAX_FORM_BODY_BYTES = 16 * 1024
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
    location_header_count: int = 0


class _TransportLifecycleMarker:
    """Server-owned proof of one request's transport settlement.

    The marker deliberately contains counters only.  It never stores URLs,
    headers, response data, or credentials.  A request becomes settled only
    after the owning HTTPX client's awaited ``aclose`` returns successfully,
    or when DNS/validation failed before a client could exist.  Callers use
    the adapter's aggregate ``transport_quiescence`` projection instead of
    receiving this mutable object.
    """

    __slots__ = (
        "_lock",
        "_requests_started",
        "_requests_settled",
        "_active_operations",
        "_unsettled_operations",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests_started = 0
        self._requests_settled = 0
        self._active_operations = 0
        self._unsettled_operations = 0

    def request_started(self) -> None:
        with self._lock:
            self._requests_started += 1
            self._active_operations += 1
            self._unsettled_operations += 1

    def request_settled(self) -> None:
        with self._lock:
            if self._active_operations <= 0 or self._unsettled_operations <= 0:
                raise RuntimeError("transport lifecycle settled more than once")
            self._active_operations -= 1
            self._unsettled_operations -= 1
            self._requests_settled += 1

    def request_no_contact(self) -> None:
        """Settle an operation that failed before an HTTPX client existed."""

        self.request_settled()

    def snapshot(self) -> dict[str, int | str]:
        with self._lock:
            active = self._active_operations
            unsettled = self._unsettled_operations
            started = self._requests_started
            settled = self._requests_settled
        return {
            "status": "verified" if active == 0 and unsettled == 0 and started == settled else "unknown",
            "active_operations": active,
            "unsettled_operations": unsettled,
            "requests_started": started,
            "requests_settled": settled,
        }


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
    authority_check: Callable[[], Awaitable[None]] | None = None,
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
        authority_check=authority_check,
    )


async def request_pinned_https(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    json_body: Any | None = None,
    form_body: bytes | None = None,
    resolver: Resolver = default_resolver,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    connect_timeout_seconds: float | None = None,
    max_bytes: int = MAX_RESPONSE_BYTES,
    _lifecycle_marker: _TransportLifecycleMarker | None = None,
    authority_check: Callable[[], Awaitable[None]] | None = None,
    handoff_check: Callable[[], Awaitable[None]] | None = None,
    observe_redirect_response: bool = False,
) -> PinnedResponse:
    """The existing general HTTPS surface remains bounded GET/POST only."""
    if str(method or "GET").upper() not in {"GET", "POST"}:
        raise PinnedTransportError("only GET and POST are supported")
    return await _request_pinned_https(url, method=method, headers=headers,
        json_body=json_body, form_body=form_body, resolver=resolver,
        transport=transport, timeout_seconds=timeout_seconds,
        connect_timeout_seconds=connect_timeout_seconds, max_bytes=max_bytes,
        _lifecycle_marker=_lifecycle_marker, authority_check=authority_check,
        handoff_check=handoff_check, observe_redirect_response=observe_redirect_response)


async def request_pinned_calendar_patch(
    *, calendar_id: str, event_id: str, etag: str, start: Mapping[str, str],
    end: Mapping[str, str], marker_key: str, marker_value: str, access_token: str,
    authority_validate: Callable[[], Awaitable[None]],
    authority_check: Callable[[], Awaitable[None]],
    lifecycle_marker: _TransportLifecycleMarker,
    resolver: Resolver = default_resolver,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> PinnedResponse:
    """One structured conditional Calendar PATCH, without a URL/method input.

    Route and literal schema validation plus a current canonical permission
    check precede DNS. The second native callback atomically spends the sole
    dispatch slot immediately before the actual stream. Shared internals own
    pinning, zero redirects/retries and positive awaited client settlement.
    """
    from urllib.parse import quote, urlencode
    import re
    from src.integrations.calendar_reschedule_contract import provider_id, text, proposed_times
    if not callable(authority_validate) or not callable(authority_check) or not isinstance(lifecycle_marker, _TransportLifecycleMarker):
        raise PinnedTransportError("native Calendar authority and transport ownership are required")
    calendar_id, event_id = provider_id(calendar_id, 1024), provider_id(event_id)
    etag, access_token = text(etag,512), text(access_token,8192)
    if type(start) is not dict or type(end) is not dict:
        raise PinnedTransportError("literal Calendar event times are required")
    proposed_times(start,end)
    if type(marker_key) is not str or re.fullmatch(r"seraphReschedule_[0-9a-f]{24}",marker_key) is None or type(marker_value) is not str or re.fullmatch(r"[0-9a-f]{64}",marker_value) is None:
        raise PinnedTransportError("the exact Calendar correlation property is required")
    # Freeze caller mappings before any await; callback code cannot alter the
    # approved wire bytes through shared mutable input references.
    body = {"start":dict(start), "end":dict(end), "extendedProperties":{"private":{marker_key:marker_value}}}
    url = "https://www.googleapis.com/calendar/v3/calendars/"+quote(calendar_id,safe="")+"/events/"+quote(event_id,safe="")+"?"+urlencode({"sendUpdates":"none","conferenceDataVersion":0,"supportsAttachments":"false"})
    await authority_validate()
    return await _request_pinned_https(url, method="PATCH", json_body=body,
        headers={"Accept":"application/json","Authorization":"Bearer "+access_token,"If-Match":etag},
        resolver=resolver, transport=transport, timeout_seconds=timeout_seconds,
        max_bytes=64*1024, _lifecycle_marker=lifecycle_marker,
        authority_check=authority_check)


async def _request_pinned_https(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    json_body: Any | None = None,
    form_body: bytes | None = None,
    resolver: Resolver = default_resolver,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    connect_timeout_seconds: float | None = None,
    max_bytes: int = MAX_RESPONSE_BYTES,
    _lifecycle_marker: _TransportLifecycleMarker | None = None,
    authority_check: Callable[[], Awaitable[None]] | None = None,
    handoff_check: Callable[[], Awaitable[None]] | None = None,
    observe_redirect_response: bool = False,
) -> PinnedResponse:
    """Private shared transport for general reads/posts and the fixed PATCH.

    ``transport`` is an explicit constructor seam for local tests. It is never
    read from configuration, and production clients always connect to the
    resolver-selected numeric address while retaining the logical hostname for
    Host and TLS SNI.
    """

    normalized_method = str(method or "GET").upper()
    if type(observe_redirect_response) is not bool:
        raise PinnedTransportError("redirect observation requires a strict boolean")
    if normalized_method not in {"GET", "POST", "PATCH"}:
        raise PinnedTransportError("only GET and POST are supported")
    if normalized_method == "PATCH":
        # Defense in depth even within the private implementation: PATCH
        # cannot become a general method/URL escape hatch for other adapters.
        from urllib.parse import unquote, parse_qsl
        import re
        from src.integrations.calendar_reschedule_contract import provider_id, text, proposed_times
        route = _parse_public_url(url)
        segments = re.fullmatch(r"/calendar/v3/calendars/([^/]+)/events/([^/]+)",route.path)
        if route.hostname != "www.googleapis.com" or route.port not in {None,443} or segments is None or parse_qsl(route.query,keep_blank_values=True) != [("sendUpdates","none"),("conferenceDataVersion","0"),("supportsAttachments","false")]:
            raise PinnedTransportError("only the exact conditional Calendar event route permits PATCH")
        provider_id(unquote(segments[1]),1024); provider_id(unquote(segments[2]))
        if type(json_body) is not dict or set(json_body)!={"start","end","extendedProperties"} or form_body is not None:
            raise PinnedTransportError("only the exact conditional Calendar event body permits PATCH")
        proposed_times(json_body["start"],json_body["end"])
        properties = json_body["extendedProperties"]
        if type(properties) is not dict or set(properties)!={"private"} or type(properties["private"]) is not dict or len(properties["private"])!=1:
            raise PinnedTransportError("one Calendar private correlation property is required")
        key, value = next(iter(properties["private"].items()))
        if type(key) is not str or re.fullmatch(r"seraphReschedule_[0-9a-f]{24}",key) is None or type(value) is not str or re.fullmatch(r"[0-9a-f]{64}",value) is None:
            raise PinnedTransportError("the exact Calendar correlation property is required")
        if not isinstance(_lifecycle_marker,_TransportLifecycleMarker) or not callable(authority_check):
            raise PinnedTransportError("native Calendar contact ownership is required")
        wire_headers = {str(key).lower():value for key,value in (headers or {}).items()}
        if set(wire_headers)!={"accept","authorization","if-match"} or wire_headers["accept"]!="application/json" or type(wire_headers["authorization"]) is not str or not wire_headers["authorization"].startswith("Bearer "):
            raise PinnedTransportError("the exact Calendar headers are required")
        text(wire_headers["if-match"],512); text(wire_headers["authorization"][7:],8192)
    if json_body is not None and normalized_method not in {"POST", "PATCH"}:
        raise PinnedTransportError("JSON request bodies are only allowed for POST")
    if json_body is not None and form_body is not None:
        raise PinnedTransportError("JSON and form request bodies are mutually exclusive")
    if form_body is not None:
        if type(form_body) is not bytes:
            raise PinnedTransportError("form request bodies must be bytes")
        if normalized_method != "POST":
            raise PinnedTransportError("form request bodies are only allowed for POST")
        if len(form_body) > MAX_FORM_BODY_BYTES:
            raise PinnedTransportError("form request body exceeds the bounded byte limit")
    try:
        bounded_timeout = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise PinnedTransportError("timeout must be a finite positive number") from exc
    if not math.isfinite(bounded_timeout) or bounded_timeout <= 0:
        raise PinnedTransportError("timeout must be a finite positive number")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise PinnedTransportError("max_bytes must be a positive integer")
    validated_headers: list[tuple[str, str]] = []
    form_content_types: list[str] = []
    for key, value in (headers or {}).items():
        normalized_key = str(key).lower()
        normalized_value = str(value)
        if normalized_key == "host":
            raise PinnedTransportError("Host is owned by the pinned transport")
        if normalized_key == "accept-encoding":
            if normalized_value.strip().lower() != "identity":
                raise PinnedTransportError(
                    "Accept-Encoding is owned by the pinned transport"
                )
            continue
        if normalized_key == "content-type":
            form_content_types.append(normalized_value)
            if form_body is not None:
                # The form body owns this header.  Validation above still
                # inspects every caller spelling/value, but only one canonical
                # header is emitted below.
                continue
        validated_headers.append((str(key), normalized_value))
    if form_body is not None and any(
        value.split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded"
        for value in form_content_types
    ):
        raise PinnedTransportError("form request body requires form content type")
    bounded_connect_timeout: float | None = None
    if connect_timeout_seconds is not None:
        try:
            bounded_connect_timeout = float(connect_timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise PinnedTransportError("connect timeout must be a finite positive number") from exc
        if not math.isfinite(bounded_connect_timeout) or bounded_connect_timeout <= 0:
            raise PinnedTransportError("connect timeout must be a finite positive number")

    parsed = _parse_public_url(url)
    if observe_redirect_response and (normalized_method != "POST"
        or parsed.hostname != "codeberg.org" or parsed.path != "/user/login" or parsed.query):
        raise PinnedTransportError("redirect observation is restricted to fixed Forgejo login")
    resolve_timeout = bounded_timeout
    if connect_timeout_seconds is not None:
        resolve_timeout = min(resolve_timeout, bounded_connect_timeout)

    if _lifecycle_marker is not None:
        _lifecycle_marker.request_started()
    client: httpx.AsyncClient | None = None
    client_created = False
    # The outer timeout covers DNS, connection, response headers, streaming,
    # and response/context cleanup.  HTTPX's per-read timeout alone would let
    # a peer keep a request alive indefinitely by sending tiny chunks.
    try:
        async with asyncio.timeout(bounded_timeout):
            if handoff_check is not None:
                await handoff_check()
            try:
                addresses = await asyncio.wait_for(
                    _resolve(resolver, parsed.hostname or "", parsed.port or 443),
                    timeout=resolve_timeout,
                )
            except asyncio.TimeoutError as exc:
                raise TimeoutError("source DNS resolution timed out") from exc
            pinned = addresses[0]
            if handoff_check is not None:
                await handoff_check()
            # ASGI/mock transports need the logical URL so tests can route it.
            # A real network client uses the pinned address and an explicit
            # Host header.
            if transport is None:
                pinned_host = f"[{pinned}]" if ":" in pinned else pinned
                request_url = urlunsplit(
                    ("https", pinned_host, parsed.path or "/", parsed.query, "")
                )
                request_headers = {"Host": parsed.hostname or ""}
            else:
                request_url = url
                request_headers = {}
            for key, value in validated_headers:
                request_headers[key] = value
            request_headers.setdefault("Accept", "*/*")
            # Disable HTTPX's automatic decompression.  A caller may opt into
            # the same identity value, but cannot weaken this boundary.
            request_headers["Accept-Encoding"] = "identity"
            request_content: bytes | None = None
            if json_body is not None:
                request_content = json.dumps(
                    json_body,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                request_headers.setdefault("Content-Type", "application/json")
            elif form_body is not None:
                request_headers["Content-Type"] = "application/x-www-form-urlencoded"
                request_content = form_body
            client = httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                trust_env=False,
                timeout=httpx.Timeout(
                    bounded_timeout,
                    connect=(
                        min(bounded_timeout, bounded_connect_timeout)
                        if bounded_connect_timeout is not None
                        else bounded_timeout
                    ),
                ),
            )
            client_created = True
            try:
                if authority_check is not None:
                    await authority_check()
                async with client.stream(
                    normalized_method,
                    request_url,
                    headers=request_headers,
                    content=request_content,
                    extensions={"sni_hostname": parsed.hostname or ""},
                ) as response:
                    if handoff_check is not None:
                        await handoff_check()
                    if 300 <= response.status_code < 400 and not (
                        observe_redirect_response and response.status_code in {302, 303}
                    ):
                        raise PinnedTransportError("redirects are disabled for source watches")
                    content_encoding = response.headers.get("content-encoding", "").strip().lower()
                    if content_encoding not in {"", "identity"}:
                        raise PinnedTransportError(
                            "encoded source responses are not allowed"
                        )
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        if handoff_check is not None:
                            await handoff_check()
                        # Check before extending so an overflow chunk is never
                        # retained in the bounded response buffer.
                        if len(content) + len(chunk) > max_bytes:
                            raise PinnedTransportError(
                                "source response exceeds the bounded byte limit"
                            )
                        content.extend(chunk)
                    response_headers = {
                        str(k).lower(): str(v) for k, v in response.headers.items()
                    }
                    location_header_count = len(response.headers.get_list("location"))
            finally:
                # Do not infer settlement from response/stream state or an
                # ``is_closed`` property.  The marker is advanced only after
                # the real awaited client close returns successfully.
                await client.aclose()
                if _lifecycle_marker is not None:
                    _lifecycle_marker.request_settled()
    except asyncio.TimeoutError as exc:
        if _lifecycle_marker is not None and not client_created:
            _lifecycle_marker.request_no_contact()
        raise TimeoutError("source request timed out") from exc
    except BaseException:
        # DNS and validation failures before an HTTPX client exists are known
        # no-contact terminations.  A constructed client whose close failed,
        # was cancelled, or never completed remains unresolved forever until
        # a server-owned recovery path proves settlement.
        if _lifecycle_marker is not None and not client_created:
            _lifecycle_marker.request_no_contact()
        raise
    return PinnedResponse(
        url=url,
        status_code=response.status_code,
        headers=response_headers,
        content=bytes(content),
        pinned_address=pinned,
        location_header_count=location_header_count,
    )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_FORM_BODY_BYTES",
    "MAX_RESPONSE_BYTES",
    "PinnedResponse",
    "PinnedTransportError",
    "default_resolver",
    "fetch_pinned_https",
    "parse_public_https_url",
    "request_pinned_https",
]
