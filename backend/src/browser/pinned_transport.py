"""Pinned, read-only HTTPS transport for the public browser capability.

The existing source-watch transport is intentionally not used here: the
browser capability has a different contract.  Every request is resolved and
validated before a numeric-address request is made, and Playwright receives
the response through ``route.fulfill``.  No ambient Chromium resolver or
``route.continue_`` fall-through is permitted.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future as ConcurrentFuture, ThreadPoolExecutor
from functools import partial
import hashlib
import ipaddress
import inspect
import json
import re
import socket
import threading
from collections.abc import Awaitable, Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Callable, Literal
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

import httpx

from src.security.site_policy import SiteAccessDecision, evaluate_site_access


BROWSER_MAX_RESPONSE_BYTES = 256 * 1024
BROWSER_MAX_REDIRECTS = 3
BROWSER_REQUEST_TIMEOUT_SECONDS = 10.0
BROWSER_ALLOWED_METHODS = frozenset({"GET", "HEAD"})
BROWSER_ALLOWED_ATTRIBUTES = frozenset(
    {"href", "title", "aria-label", "alt", "datetime", "src"}
)
BROWSER_BLOCKED_RESOURCE_TYPES = frozenset(
    {
        "beacon",
        "eventsource",
        "fetch",
        "script",
        "serviceworker",
        "sharedworker",
        "websocket",
        "worker",
        "xhr",
    }
)
BROWSER_SAFE_RESPONSE_HEADERS = frozenset(
    {
        "cache-control",
        "content-language",
        "content-location",
        "content-type",
        "etag",
        "expires",
        "last-modified",
        "location",
        "pragma",
        "vary",
    }
)
BROWSER_FORBIDDEN_REQUEST_HEADERS = frozenset(
    {"authorization", "cookie", "proxy-authorization"}
)
_ENCODED_PATH_BOUNDARY = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")

# DNS and the configured site policy both have synchronous production paths.
# Keep their blocking work out of asyncio's shared default executor: a burst of
# browser resources must not consume workers used by chat, tools, or model
# admission.  Two slots are deliberate; request-level limits remain the
# authoritative bound above this small process-wide blocking lane.
_BLOCKING_TRANSPORT_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="seraph-browser-transport",
)
_BLOCKING_FUTURES_LOCK = threading.Lock()


async def _run_blocking(
    callback: Callable[..., Any],
    *args: Any,
    timeout_seconds: float,
    pending: set[ConcurrentFuture[Any]] | None = None,
    **kwargs: Any,
) -> Any:
    """Run one bounded blocking transport operation in the shared lane.

    Cancelling the asyncio wrapper cannot stop a function already running in a
    worker thread.  The underlying future therefore remains tracked until it
    completes, while queued work can still be cancelled by transport cleanup.
    """

    future = _BLOCKING_TRANSPORT_EXECUTOR.submit(partial(callback, *args, **kwargs))
    if pending is not None:
        with _BLOCKING_FUTURES_LOCK:
            pending.add(future)

        def discard(completed: ConcurrentFuture[Any]) -> None:
            with _BLOCKING_FUTURES_LOCK:
                pending.discard(completed)

        future.add_done_callback(discard)
    wrapped = asyncio.wrap_future(future)
    try:
        return await asyncio.wait_for(wrapped, timeout=max(0.001, float(timeout_seconds)))
    except asyncio.TimeoutError:
        # ``Future.cancel`` is effective for work still in the executor queue;
        # an already-running call remains bounded by the OS/library timeout
        # and occupies one of the two dedicated slots until it returns.
        future.cancel()
        raise


def _blocking_default_resolver(hostname: str, port: int) -> list[str]:
    records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for record in records:
        sockaddr = record[4]
        if sockaddr:
            addresses.append(str(sockaddr[0]))
    return list(dict.fromkeys(addresses))


async def _dedicated_default_resolver(
    hostname: str,
    port: int,
    *,
    pending: set[ConcurrentFuture[Any]] | None = None,
) -> list[str]:
    return await _run_blocking(
        _blocking_default_resolver,
        hostname,
        port,
        timeout_seconds=BROWSER_REQUEST_TIMEOUT_SECONDS,
        pending=pending,
    )


class PinnedTransportError(ValueError):
    """A request failed a browser transport boundary."""

    def __init__(self, message: str, *, code: str = "transport_blocked") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PinnedBrowserRequest:
    """Safe request metadata passed from the Playwright route guard."""

    url: str
    method: Literal["GET", "HEAD"]
    headers: Mapping[str, str]
    resource_type: str
    is_navigation: bool
    redirect_count: int


@dataclass(frozen=True, slots=True)
class PinnedBrowserResponse:
    """Bounded response returned by one pinned HTTP hop."""

    status_code: int
    headers: Mapping[str, str]
    content: bytes
    request_url: str
    pinned_address: str
    redirect_location: str | None = None

    @property
    def byte_count(self) -> int:
        return len(self.content)


Resolver = Callable[[str, int], Awaitable[Iterable[str]] | Iterable[str]]
InjectedFetch = Callable[
    [PinnedBrowserRequest], Awaitable[PinnedBrowserResponse] | PinnedBrowserResponse
]
SitePolicy = Callable[[str], SiteAccessDecision]


async def _invoke_callback(callback: Callable[..., Any], *args: Any) -> Any:
    """Run a route hook while accepting sync test/adapter callbacks."""

    result = callback(*args)
    if inspect.isawaitable(result):
        return await result
    return result


async def _evaluate_site_policy(
    policy: SitePolicy,
    url: str,
    *,
    timeout_seconds: float,
    pending: set[ConcurrentFuture[Any]] | None = None,
) -> SiteAccessDecision:
    """Run DNS-aware policy evaluation without blocking the event loop.

    The production policy performs synchronous DNS work.  Injected tests may
    expose either the historical one-argument callback or the DNS-aware
    keyword.  Inspect the callable once instead of catching a ``TypeError``
    raised from inside policy logic, where fallback would hide a real bug.
    """

    try:
        signature = inspect.signature(policy)
        parameters = signature.parameters.values()
        accepts_dns = "resolve_dns" in signature.parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
        )
    except (TypeError, ValueError):
        # A callable without an inspectable signature is treated as the
        # current production contract; an invalid invocation becomes a typed
        # policy failure below rather than a second unbounded attempt.
        accepts_dns = True

    kwargs = {"resolve_dns": True} if accepts_dns else {}
    try:
        if inspect.iscoroutinefunction(policy) or inspect.iscoroutinefunction(getattr(policy, "__call__", None)):
            decision = policy(url, **kwargs)  # type: ignore[call-arg]
            decision = await asyncio.wait_for(decision, timeout=timeout_seconds)
        else:
            decision = await _run_blocking(
                policy,
                url,
                timeout_seconds=timeout_seconds,
                pending=pending,
                **kwargs,
            )
            if inspect.isawaitable(decision):
                decision = await asyncio.wait_for(decision, timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise PinnedTransportError("site policy evaluation timed out", code="site_policy_timeout") from exc
    except PinnedTransportError:
        raise
    except Exception as exc:
        raise PinnedTransportError("site policy evaluation failed", code="site_policy_failed") from exc
    if not isinstance(decision, SiteAccessDecision):
        raise PinnedTransportError("site policy returned an invalid decision", code="site_policy_invalid")
    return decision


def _normalize_host(value: str) -> str:
    candidate = str(value or "").strip().lower().rstrip(".")
    if not candidate:
        raise PinnedTransportError("host rule is empty", code="host_rule_invalid")
    try:
        candidate = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise PinnedTransportError("host rule is not valid IDNA", code="host_rule_invalid") from exc
    if any(character in candidate for character in "/?#:@"):
        raise PinnedTransportError("host rules must contain only hostnames", code="host_rule_invalid")
    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return candidate
    if parsed.version == 6:
        return parsed.compressed
    return str(parsed)


def normalize_allowed_hosts(values: Collection[str]) -> tuple[str, ...]:
    """Return exact normalized host rules, preserving deterministic order."""

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        host = _normalize_host(value)
        if host in seen:
            raise PinnedTransportError(
                "allowed_hosts contains a duplicate host",
                code="host_rule_duplicate",
            )
        seen.add(host)
        normalized.append(host)
    if not normalized:
        raise PinnedTransportError("at least one allowed host is required", code="host_rule_empty")
    if len(normalized) > 8:
        raise PinnedTransportError("at most eight allowed hosts are supported", code="host_rule_limit")
    return tuple(normalized)


def parse_public_https_url(url: str) -> SplitResult:
    original = str(url or "")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in original):
        raise PinnedTransportError("URL contains a control character", code="url_invalid")
    raw = original.strip()
    parsed = urlsplit(raw)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise PinnedTransportError(
            "browser URLs must be HTTPS URLs with a hostname",
            code="url_invalid",
        )
    if parsed.username or parsed.password:
        raise PinnedTransportError("URL credentials are not allowed", code="url_credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise PinnedTransportError("URL port is invalid", code="url_port") from exc
    if port not in (None, 443):
        raise PinnedTransportError("browser URLs must use HTTPS port 443", code="url_port")
    if parsed.fragment:
        raise PinnedTransportError("URL fragments are not allowed", code="url_fragment")
    path = parsed.path or "/"
    if "\\" in path:
        raise PinnedTransportError("URL path backslashes are not allowed", code="url_path_ambiguous")
    if _INVALID_PERCENT_ESCAPE.search(path):
        raise PinnedTransportError("URL path contains an invalid escape", code="url_path_ambiguous")
    if _ENCODED_PATH_BOUNDARY.search(path) or any(segment in {".", ".."} for segment in path.split("/")):
        raise PinnedTransportError("URL path contains an ambiguous traversal segment", code="url_path_ambiguous")
    if len(raw) > 2 * 1024:
        raise PinnedTransportError("URL exceeds the 2 KiB limit", code="url_limit")
    return parsed


def safe_url(url: str) -> str:
    """Return a URL suitable for receipts without credentials or queries."""

    parsed = parse_public_https_url(url)
    host = _normalize_host(parsed.hostname or "")
    netloc = f"[{host}]" if ":" in host else host
    return urlunsplit(("https", netloc, parsed.path or "/", "", ""))


def url_digest(url: str) -> str:
    parsed = parse_public_https_url(url)
    host = _normalize_host(parsed.hostname or "")
    netloc = f"[{host}]" if ":" in host else host
    canonical = urlunsplit(
        ("https", netloc, parsed.path or "/", parsed.query, "")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _prefix_matches(url: str, prefix: str) -> bool:
    target = parse_public_https_url(url)
    approved = parse_public_https_url(prefix)
    target_host = _normalize_host(target.hostname or "")
    approved_host = _normalize_host(approved.hostname or "")
    target_port = target.port or 443
    approved_port = approved.port or 443
    if target.scheme.lower() != approved.scheme.lower() or target_port != approved_port:
        return False
    if target_host != approved_host:
        return False
    if target.query != approved.query:
        return False
    target_path = target.path or "/"
    approved_path = approved.path or "/"
    if target_path == approved_path:
        return True
    boundary = approved_path.rstrip("/")
    if not boundary:
        return True
    return target_path.startswith(boundary + "/")


def normalize_approved_prefixes(values: Collection[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        parsed = parse_public_https_url(value)
        host = _normalize_host(parsed.hostname or "")
        netloc = f"[{host}]" if ":" in host else host
        normalized_value = urlunsplit(
            (
                "https",
                netloc,
                parsed.path or "/",
                parsed.query,
                "",
            )
        )
        if normalized_value in seen:
            raise PinnedTransportError(
                "approved_url_prefixes contains a duplicate prefix",
                code="prefix_duplicate",
            )
        seen.add(normalized_value)
        normalized.append(normalized_value)
    if not normalized:
        raise PinnedTransportError(
            "at least one approved URL prefix is required",
            code="prefix_empty",
        )
    if len(normalized) > 8:
        raise PinnedTransportError("at most eight URL prefixes are supported", code="prefix_limit")
    return tuple(normalized)


async def _resolve_all(
    resolver: Resolver,
    hostname: str,
    port: int,
    *,
    timeout_seconds: float = BROWSER_REQUEST_TIMEOUT_SECONDS,
    pending: set[ConcurrentFuture[Any]] | None = None,
) -> list[str]:
    is_async = inspect.iscoroutinefunction(resolver) or inspect.iscoroutinefunction(
        getattr(resolver, "__call__", None)
    )
    if is_async:
        values = resolver(hostname, port)
    else:
        values = await _run_blocking(
            resolver,
            hostname,
            port,
            timeout_seconds=timeout_seconds,
            pending=pending,
        )
    if hasattr(values, "__await__"):
        values = await values  # type: ignore[assignment]
    addresses = [str(value) for value in values]
    if not addresses:
        raise PinnedTransportError("hostname did not resolve", code="dns_empty")
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise PinnedTransportError("resolver returned an invalid address", code="dns_invalid") from exc
        if not parsed.is_global:
            raise PinnedTransportError(
                "every resolved address must be globally routable",
                code="dns_non_global",
            )
    return addresses


def _safe_response_headers(headers: Mapping[str, Any], *, body_size: int) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in headers.items():
        name = str(key).lower().strip()
        if name not in BROWSER_SAFE_RESPONSE_HEADERS:
            continue
        if name == "content-location":
            try:
                value = safe_url(str(value))
            except PinnedTransportError:
                continue
        result[name] = str(value)
    result["content-length"] = str(body_size)
    return result


def _reject_encoded_response(headers: Mapping[str, Any]) -> None:
    """Reject decoder-managed bodies before HTTPX starts iterating them."""

    for key, value in headers.items():
        if str(key).strip().lower() != "content-encoding":
            continue
        encoding = str(value or "").strip().lower()
        if encoding and encoding != "identity":
            raise PinnedTransportError(
                "encoded response bodies are not supported",
                code="response_encoding_blocked",
            )


def _response_from_value(value: Any, *, request: PinnedBrowserRequest, address: str) -> PinnedBrowserResponse:
    if isinstance(value, PinnedBrowserResponse):
        response = value
    elif isinstance(value, Mapping):
        response = PinnedBrowserResponse(
            status_code=int(value.get("status_code", value.get("status", 200))),
            headers={str(key): str(item) for key, item in (value.get("headers") or {}).items()},
            content=(value.get("content") or value.get("body") or b""),
            request_url=str(value.get("request_url") or request.url),
            pinned_address=str(value.get("pinned_address") or address),
            redirect_location=value.get("redirect_location") or (value.get("headers") or {}).get("location"),
        )
    else:
        raise PinnedTransportError("injected fetch returned an invalid response", code="response_invalid")
    content = response.content
    if isinstance(content, str):
        content = content.encode("utf-8")
    if not isinstance(content, bytes):
        raise PinnedTransportError("response body is not bytes", code="response_invalid")
    if len(content) > BROWSER_MAX_RESPONSE_BYTES:
        raise PinnedTransportError("response exceeds the bounded byte limit", code="response_limit")
    headers = {str(key).lower(): str(item) for key, item in response.headers.items()}
    _reject_encoded_response(headers)
    redirect_location = response.redirect_location or headers.get("location")
    return PinnedBrowserResponse(
        status_code=int(response.status_code),
        headers=headers,
        content=content,
        request_url=response.request_url or request.url,
        pinned_address=response.pinned_address or address,
        redirect_location=redirect_location,
    )


class PinnedBrowserTransport:
    """Resolve, policy-check, fetch, and fulfill one browser request."""

    def __init__(
        self,
        *,
        resolver: Resolver = None,  # type: ignore[assignment]
        injected_fetch: InjectedFetch | None = None,
        site_policy: SitePolicy = evaluate_site_access,
        timeout_seconds: float = BROWSER_REQUEST_TIMEOUT_SECONDS,
        max_response_bytes: int = BROWSER_MAX_RESPONSE_BYTES,
    ) -> None:
        # Keep the synchronous resolver on the dedicated transport executor;
        # ``_resolve_all`` dispatches it there rather than touching asyncio's
        # shared default pool.
        self.resolver = resolver or _blocking_default_resolver
        self.injected_fetch = injected_fetch
        self.site_policy = site_policy
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = int(max_response_bytes)
        self._pending_blocking: set[ConcurrentFuture[Any]] = set()
        self._network_slots = asyncio.Semaphore(2)
        self._closed = False
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_response_bytes <= 0 or self.max_response_bytes > BROWSER_MAX_RESPONSE_BYTES:
            raise ValueError("max_response_bytes exceeds the browser transport bound")

    def cancel_pending_blocking(self) -> None:
        """Cancel queued policy/DNS work during browser-context cleanup."""

        self._closed = True
        with _BLOCKING_FUTURES_LOCK:
            pending = tuple(self._pending_blocking)
        for future in pending:
            future.cancel()

    async def resolve_and_fetch(
        self,
        request: PinnedBrowserRequest,
        *,
        allowed_hosts: Collection[str],
        approved_url_prefixes: Collection[str],
    ) -> PinnedBrowserResponse:
        if self._closed:
            raise PinnedTransportError("transport is closed", code="transport_closed")
        parsed = parse_public_https_url(request.url)
        normalized_method = str(request.method).upper()
        if normalized_method not in BROWSER_ALLOWED_METHODS:
            raise PinnedTransportError("only GET and HEAD are allowed", code="method_blocked")
        normalized_hosts = normalize_allowed_hosts(allowed_hosts)
        normalized_prefixes = normalize_approved_prefixes(approved_url_prefixes)
        host = _normalize_host(parsed.hostname or "")
        if host not in normalized_hosts:
            raise PinnedTransportError("host is not explicitly approved", code="host_not_approved")
        if not any(_prefix_matches(request.url, prefix) for prefix in normalized_prefixes):
            raise PinnedTransportError("URL is outside approved prefixes", code="prefix_not_approved")
        decision = await _evaluate_site_policy(
            self.site_policy,
            request.url,
            timeout_seconds=self.timeout_seconds,
            pending=self._pending_blocking,
        )
        if not decision.allowed:
            raise PinnedTransportError(
                "URL is blocked by the configured site policy",
                code="site_policy_blocked",
            )
        try:
            addresses = await asyncio.wait_for(
                _resolve_all(
                    self.resolver,
                    parsed.hostname or "",
                    parsed.port or 443,
                    timeout_seconds=self.timeout_seconds,
                    pending=self._pending_blocking,
                ),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise PinnedTransportError("DNS resolution timed out", code="dns_timeout") from exc
        address = addresses[0]
        request_headers = {str(key).lower(): str(value) for key, value in request.headers.items()}
        forbidden = BROWSER_FORBIDDEN_REQUEST_HEADERS.intersection(request_headers)
        if forbidden:
            raise PinnedTransportError("credential or cookie request headers are forbidden", code="header_blocked")
        if self.injected_fetch is not None:
            async with self._network_slots:
                if self._closed:
                    raise PinnedTransportError("transport is closed", code="transport_closed")
                try:
                    if inspect.iscoroutinefunction(self.injected_fetch) or inspect.iscoroutinefunction(
                        getattr(self.injected_fetch, "__call__", None)
                    ):
                        value = self.injected_fetch(request)
                        if hasattr(value, "__await__"):
                            value = await asyncio.wait_for(value, timeout=self.timeout_seconds)  # type: ignore[assignment]
                    else:
                        value = await _run_blocking(
                            self.injected_fetch,
                            request,
                            timeout_seconds=self.timeout_seconds,
                            pending=self._pending_blocking,
                        )
                        if hasattr(value, "__await__"):
                            value = await asyncio.wait_for(value, timeout=self.timeout_seconds)
                except asyncio.TimeoutError as exc:
                    raise PinnedTransportError("network request timed out", code="network_timeout") from exc
            response = _response_from_value(value, request=request, address=address)
        else:
            pinned_host = f"[{address}]" if ":" in address else address
            request_url = urlunsplit(
                ("https", pinned_host, parsed.path or "/", parsed.query, "")
            )
            outbound_headers = {
                key: value
                for key, value in request_headers.items()
                if key not in {"host", "content-length", "connection", "transfer-encoding"}
            }
            outbound_headers["host"] = parsed.hostname or ""
            # Do not let HTTPX negotiate gzip/deflate before the browser byte
            # cap sees the body. Any server that still sends an encoding is
            # rejected before ``aiter_bytes`` can invoke its decoder.
            outbound_headers["accept-encoding"] = "identity"
            async with self._network_slots:
                if self._closed:
                    raise PinnedTransportError("transport is closed", code="transport_closed")
                try:
                    async with httpx.AsyncClient(
                        follow_redirects=False,
                        trust_env=False,
                        timeout=httpx.Timeout(self.timeout_seconds),
                    ) as client:
                        # ``AsyncClient.request`` eagerly buffers the response
                        # body before returning.  Use HTTPX's streaming
                        # context so the browser byte cap is enforced while
                        # reading and the response is closed on overflow,
                        # cancellation, or any transport error.
                        async with client.stream(
                            normalized_method,
                            request_url,
                            headers=outbound_headers,
                            extensions={"sni_hostname": parsed.hostname or ""},
                        ) as response_obj:
                            _reject_encoded_response(response_obj.headers)
                            body = bytearray()
                            async for chunk in response_obj.aiter_bytes():
                                body.extend(chunk)
                                if len(body) > self.max_response_bytes:
                                    raise PinnedTransportError(
                                        "response exceeds the bounded byte limit",
                                        code="response_limit",
                                    )
                            response = PinnedBrowserResponse(
                                status_code=response_obj.status_code,
                                headers={str(key).lower(): str(value) for key, value in response_obj.headers.items()},
                                content=bytes(body),
                                request_url=request.url,
                                pinned_address=address,
                                redirect_location=response_obj.headers.get("location"),
                            )
                except httpx.TimeoutException as exc:
                    raise PinnedTransportError("network request timed out", code="network_timeout") from exc
                except httpx.HTTPError as exc:
                    raise PinnedTransportError("network request failed", code="network_failed") from exc
        if response.byte_count > self.max_response_bytes:
            raise PinnedTransportError("response exceeds the bounded byte limit", code="response_limit")
        # An injected fixture is allowed to provide a body, never a pinning
        # identity. The resolver-selected address remains authoritative.
        response = PinnedBrowserResponse(
            status_code=response.status_code,
            headers=response.headers,
            content=response.content,
            request_url=response.request_url,
            pinned_address=address,
            redirect_location=response.redirect_location,
        )
        if "set-cookie" in {key.lower() for key in response.headers}:
            raise PinnedTransportError("response cookies are forbidden", code="response_cookie_blocked")
        disposition = str(response.headers.get("content-disposition", "")).lower()
        if "attachment" in disposition:
            raise PinnedTransportError("downloads are forbidden", code="download_blocked")
        if 300 <= response.status_code < 400 and request.redirect_count >= BROWSER_MAX_REDIRECTS:
            raise PinnedTransportError("redirect limit exceeded", code="redirect_limit")
        if 300 <= response.status_code < 400 and response.redirect_location:
            redirect_url = urljoin(request.url, response.redirect_location)
            redirect_parsed = parse_public_https_url(redirect_url)
            redirect_host = _normalize_host(redirect_parsed.hostname or "")
            if redirect_host not in normalized_hosts or not any(
                _prefix_matches(redirect_url, prefix) for prefix in normalized_prefixes
            ):
                raise PinnedTransportError(
                    "redirect target is outside approved consent",
                    code="redirect_not_approved",
                )
            redirect_decision = await _evaluate_site_policy(
                self.site_policy,
                redirect_url,
                timeout_seconds=self.timeout_seconds,
                pending=self._pending_blocking,
            )
            if not redirect_decision.allowed:
                raise PinnedTransportError(
                    "redirect target is blocked by site policy",
                    code="redirect_site_policy_blocked",
                )
            await asyncio.wait_for(
                _resolve_all(
                    self.resolver,
                    redirect_parsed.hostname or "",
                    redirect_parsed.port or 443,
                    timeout_seconds=self.timeout_seconds,
                    pending=self._pending_blocking,
                ),
                timeout=self.timeout_seconds,
            )
        return response

    async def install_route_guard(
        self,
        context: Any,
        *,
        allowed_hosts: Collection[str],
        approved_url_prefixes: Collection[str],
        before_request: Callable[[], Awaitable[None]],
        on_receipt: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> None:
        normalized_hosts = normalize_allowed_hosts(allowed_hosts)
        normalized_prefixes = normalize_approved_prefixes(approved_url_prefixes)

        async def handler(route: Any, request: Any) -> None:
            url = str(getattr(request, "url", ""))
            method = str(getattr(request, "method", "GET")).upper()
            resource_type = str(getattr(request, "resource_type", "document") or "document").lower()
            is_navigation = bool(
                request.is_navigation_request()
                if hasattr(request, "is_navigation_request")
                else resource_type == "document"
            )
            redirect_count = 0
            current = getattr(request, "redirected_from", None)
            while current is not None and redirect_count <= BROWSER_MAX_REDIRECTS:
                redirect_count += 1
                current = getattr(current, "redirected_from", None)
            try:
                headers_value = request.all_headers() if hasattr(request, "all_headers") else getattr(request, "headers", {})
                if hasattr(headers_value, "__await__"):
                    headers_value = await headers_value
                headers = {str(key).lower(): str(value) for key, value in (headers_value or {}).items()}
                if method not in BROWSER_ALLOWED_METHODS:
                    raise PinnedTransportError("only GET and HEAD are allowed", code="method_blocked")
                if resource_type in BROWSER_BLOCKED_RESOURCE_TYPES:
                    raise PinnedTransportError("resource type is blocked", code="resource_blocked")
                await _invoke_callback(before_request)
                response = await self.resolve_and_fetch(
                    PinnedBrowserRequest(
                        url=url,
                        method=method,  # type: ignore[arg-type]
                        headers=headers,
                        resource_type=resource_type,
                        is_navigation=is_navigation,
                        redirect_count=redirect_count,
                    ),
                    allowed_hosts=normalized_hosts,
                    approved_url_prefixes=normalized_prefixes,
                )
                safe_headers = _safe_response_headers(response.headers, body_size=response.byte_count)
                await route.fulfill(
                    status=int(response.status_code),
                    headers=safe_headers,
                    body=response.content,
                )
                await _invoke_callback(
                    on_receipt,
                    {
                        "status": int(response.status_code),
                        "method": method,
                        "url": safe_url(url),
                        "url_digest": url_digest(url),
                        "redirect": 300 <= int(response.status_code) < 400,
                        "resource_type": resource_type,
                        "pinned_address_digest": hashlib.sha256(
                            response.pinned_address.encode("utf-8")
                        ).hexdigest(),
                    }
                )
            except Exception as exc:
                try:
                    await route.abort()
                finally:
                    await _invoke_callback(
                        on_receipt,
                        {
                            "status": "blocked",
                            "method": method,
                            "url": _safe_url_or_digest(url),
                            "url_digest": _safe_url_digest(url),
                            "resource_type": resource_type,
                            "reason_code": getattr(exc, "code", "transport_blocked"),
                        }
                    )

        await context.route("**/*", handler)


def _safe_url_digest(url: str) -> str | None:
    try:
        return url_digest(url)
    except Exception:
        return None


def _safe_url_or_digest(url: str) -> str | None:
    try:
        return safe_url(url)
    except Exception:
        return _safe_url_digest(url)


__all__ = [
    "BROWSER_ALLOWED_ATTRIBUTES",
    "BROWSER_MAX_REDIRECTS",
    "PinnedBrowserRequest",
    "PinnedBrowserResponse",
    "PinnedTransportError",
    "PinnedBrowserTransport",
    "normalize_allowed_hosts",
    "normalize_approved_prefixes",
    "parse_public_https_url",
    "safe_url",
    "url_digest",
]


class ProfiledPreparationTransport:
    """One registered document contact, then an enforced offline DOM phase.

    Deliberately separate from the v1 transport: no method relaxation or caller
    URL is introduced. The constructor seam is trusted test wiring only.
    """

    def __init__(self, *, request=None, source_digest=None):
        from src.security.http_transport import _TransportLifecycleMarker, request_pinned_https
        from .interaction_contracts import SOURCE_SHA256
        self._request = request or request_pinned_https
        self.source_digest = source_digest or SOURCE_SHA256
        self.lifecycle = _TransportLifecycleMarker()
        self.phase = "document"
        self.contact_started = False
        self.denials = 0
        self.failure_reason = None

    def check(self, url, method, body, resource_type):
        from .interaction_contracts import DOCUMENT_URL, InteractionError
        if (self.phase != "document" or url != DOCUMENT_URL or method != "GET"
            or body not in (None, b"") or resource_type != "document"):
            raise InteractionError("browser_request_contract_denied")

    async def install(self, context, page, *, authority, contact_intent, contact_result):
        from .interaction_contracts import InteractionError

        async def handler(route, request):
            try:
                body = request.post_data_buffer
                self.check(request.url, request.method, body, request.resource_type)
                if request.frame != page.main_frame or self.contact_started:
                    raise InteractionError("browser_document_contact_already_spent")
                if any(k.lower() in {"cookie", "authorization", "proxy-authorization"}
                       for k in request.headers):
                    raise InteractionError("browser_ambient_credentials_denied")
                if not evaluate_site_access(request.url).allowed:
                    raise InteractionError("browser_site_policy_denied")
                await authority()
                # Persist possible-contact intent before spending the only slot.
                await contact_intent()
                self.contact_started = True

                async def recheck():
                    self.check(request.url, request.method, body, request.resource_type)
                    if not evaluate_site_access(request.url).allowed:
                        raise InteractionError("browser_site_policy_denied")
                    await authority()

                response = await self._request(request.url, method="GET",
                    max_bytes=65536, timeout_seconds=10,
                    _lifecycle_marker=self.lifecycle,
                    authority_check=recheck, handoff_check=recheck)
                await recheck()
                headers = {k.lower(): v for k, v in response.headers.items()}
                if (response.status_code != 200 or "set-cookie" in headers
                    or "location" in headers
                    or headers.get("content-type", "").split(";", 1)[0] != "text/html"):
                    raise InteractionError("browser_profile_document_response_denied")
                if (len(response.content) > 65536
                    or hashlib.sha256(response.content).hexdigest() != self.source_digest):
                    raise InteractionError("browser_profile_source_version_changed")
                await contact_result(hashlib.sha256(response.content).hexdigest())
                await route.fulfill(status=200, body=response.content,
                    headers={"content-type": "text/html; charset=utf-8",
                             "content-security-policy": "default-src 'none'; form-action 'none'"})
            except Exception as exc:
                self.denials += 1
                self.failure_reason = getattr(exc, "code", "browser_profile_transport_denied")
                await route.abort("blockedbyclient")

        async def deny_socket(route):
            self.denials += 1
            await route.close()

        if not callable(getattr(context, "route_web_socket", None)):
            raise InteractionError("browser_websocket_guard_unavailable")
        await context.route_web_socket("**/*", deny_socket)
        await context.route("**/*", handler)

    def preparation(self):
        self.phase = "preparation"

    def quiescent(self):
        return self.lifecycle.snapshot()["status"] == "verified"
