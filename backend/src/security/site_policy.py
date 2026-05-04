"""Hostname allow/block policy shared by browser and web-search tools."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

from config.settings import settings

_BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}


@dataclass(frozen=True)
class SiteAccessDecision:
    allowed: bool
    hostname: str
    reason: str | None = None
    matched_rule: str | None = None
    allowlist_active: bool = False
    resolved_addresses: tuple[str, ...] = ()


def _normalize_rule(raw_rule: str) -> str:
    rule = raw_rule.strip().lower()
    if not rule:
        return ""
    if "://" in rule:
        rule = (urlparse(rule).hostname or "").lower()
    if rule.startswith("*."):
        rule = rule[2:]
    return rule.lstrip(".")


def _parse_rules(raw_rules: str) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_rule in raw_rules.split(","):
        rule = _normalize_rule(raw_rule)
        if not rule or rule in seen:
            continue
        normalized.append(rule)
        seen.add(rule)
    return tuple(normalized)


def _configured_allowlist() -> tuple[str, ...]:
    return _parse_rules(settings.browser_site_allowlist)


def _configured_blocklist() -> tuple[str, ...]:
    return _parse_rules(settings.browser_site_blocklist)


def _normalize_hostname(value: str) -> str:
    candidate = value.strip()
    parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
    return (parsed.hostname or "").lower()


def _hostname_matches_rule(hostname: str, rule: str) -> bool:
    return hostname == rule or hostname.endswith(f".{rule}")


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


def _is_internal_hostname(hostname: str, *, resolve_dns: bool = False) -> tuple[bool, tuple[str, ...]]:
    if hostname in _BLOCKED_HOSTS:
        return True, ()
    if _is_internal_ip(hostname):
        return True, ()
    try:
        ipv4 = ipaddress.ip_address(socket.inet_aton(hostname))
        if ipv4.is_private or ipv4.is_loopback or ipv4.is_link_local:
            return True, ()
    except (OSError, ValueError):
        pass
    if hostname.endswith((".local", ".internal", ".localhost")):
        return True, ()
    addresses = _resolved_addresses(hostname) if resolve_dns else ()
    if any(_is_internal_ip(address) for address in addresses):
        return True, addresses
    return False, addresses


def evaluate_site_access(url_or_hostname: str, *, resolve_dns: bool = False) -> SiteAccessDecision:
    hostname = _normalize_hostname(url_or_hostname)
    allowlist = _configured_allowlist()
    blocklist = _configured_blocklist()

    if not hostname:
        return SiteAccessDecision(
            allowed=False,
            hostname="",
            reason="invalid_hostname",
            allowlist_active=bool(allowlist),
        )

    is_internal, resolved_addresses = _is_internal_hostname(hostname, resolve_dns=resolve_dns)
    if is_internal:
        return SiteAccessDecision(
            allowed=False,
            hostname=hostname,
            reason="internal_private",
            allowlist_active=bool(allowlist),
            resolved_addresses=resolved_addresses,
        )

    for rule in blocklist:
        if _hostname_matches_rule(hostname, rule):
            return SiteAccessDecision(
                allowed=False,
                hostname=hostname,
                reason="blocklisted_domain",
                matched_rule=rule,
                allowlist_active=bool(allowlist),
                resolved_addresses=resolved_addresses,
            )

    if allowlist:
        for rule in allowlist:
            if _hostname_matches_rule(hostname, rule):
                return SiteAccessDecision(
                    allowed=True,
                    hostname=hostname,
                    matched_rule=rule,
                    allowlist_active=True,
                    resolved_addresses=resolved_addresses,
                )
        return SiteAccessDecision(
            allowed=False,
            hostname=hostname,
            reason="not_allowlisted",
            allowlist_active=True,
            resolved_addresses=resolved_addresses,
        )

    return SiteAccessDecision(
        allowed=True,
        hostname=hostname,
        allowlist_active=False,
        resolved_addresses=resolved_addresses,
    )


def site_policy_summary(url_or_hostname: str) -> dict[str, str | bool]:
    decision = evaluate_site_access(url_or_hostname)
    return {
        "hostname": decision.hostname,
        "site_policy_reason": decision.reason or "",
        "site_policy_rule": decision.matched_rule or "",
        "site_allowlist_active": decision.allowlist_active,
    }
