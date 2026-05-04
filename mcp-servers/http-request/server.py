"""HTTP Request MCP Server — exposes a single http_request tool via FastMCP."""

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx
from fastmcp import FastMCP

mcp = FastMCP("http-request", host="0.0.0.0", port=9200)

_BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "[::1]"}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5


def _is_internal_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


def _resolved_addresses(hostname: str) -> tuple[str, ...]:
    addresses: list[str] = []
    seen: set[str] = set()
    try:
        results = socket.getaddrinfo(hostname, None)
    except OSError:
        return ()
    for result in results:
        sockaddr = result[4]
        if not sockaddr:
            continue
        address = str(sockaddr[0])
        if address and address not in seen:
            addresses.append(address)
            seen.add(address)
    return tuple(addresses)


def _is_internal_url(url: str, *, resolve_dns: bool = False) -> bool:
    """Check if a URL points to an internal/private network address."""
    parsed = urlparse(url)
    hostname = parsed.hostname or ""

    if hostname in _BLOCKED_HOSTS:
        return True

    if _is_internal_ip(hostname):
        return True

    if hostname.endswith((".local", ".internal", ".localhost")):
        return True

    if resolve_dns and any(_is_internal_ip(address) for address in _resolved_addresses(hostname)):
        return True

    return False


def _request_checked_redirects(
    client: httpx.Client,
    *,
    method: str,
    url: str,
    headers: dict[str, str] | None,
    body: str | None,
) -> httpx.Response | dict:
    current_url = url
    for _ in range(_MAX_REDIRECTS + 1):
        if _is_internal_url(current_url, resolve_dns=True):
            return {"error": "Requests to internal/private network addresses are blocked"}
        response = client.request(
            method=method,
            url=current_url,
            headers=headers,
            content=body,
            follow_redirects=False,
        )
        location = response.headers.get("location") if response.headers else None
        if response.status_code not in _REDIRECT_STATUSES or not location:
            return response
        next_url = urljoin(str(response.url or current_url), str(location))
        if _is_internal_url(next_url, resolve_dns=True):
            return {"error": "Redirects to internal/private network addresses are blocked"}
        current_url = next_url
        method = "GET" if response.status_code == 303 else method
        body = None if response.status_code == 303 else body
    return {"error": f"Too many redirects; maximum is {_MAX_REDIRECTS}"}


_ALLOWED_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"}


@mcp.tool()
def http_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: str | None = None,
    timeout: int = 30,
) -> dict:
    """Make an HTTP request to an external URL.

    Args:
        method: HTTP method (GET, POST, PUT, DELETE, PATCH, HEAD)
        url: The URL to request
        headers: Optional request headers
        body: Optional request body (string)
        timeout: Request timeout in seconds (1-60, default 30)
    """
    method = method.upper()
    if method not in _ALLOWED_METHODS:
        return {"error": f"Invalid method '{method}'. Allowed: {', '.join(sorted(_ALLOWED_METHODS))}"}

    if _is_internal_url(url, resolve_dns=True):
        return {"error": "Requests to internal/private network addresses are blocked"}

    timeout = max(1, min(60, timeout))

    try:
        with httpx.Client(timeout=timeout) as client:
            response = _request_checked_redirects(
                client,
                method=method,
                url=url,
                headers=headers,
                body=body,
            )
        if isinstance(response, dict):
            return response
        return {
            "status": response.status_code,
            "headers": dict(response.headers),
            "body": response.text[:50000],  # Cap response size
        }
    except httpx.TimeoutException:
        return {"error": f"Request timed out after {timeout}s"}
    except httpx.RequestError as e:
        return {"error": f"Request failed: {e}"}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
