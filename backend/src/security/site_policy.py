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


def _memory_policy_slots(count: int) -> int:
    # Original nominal width-four opaque references, not int64 scalar values.
    # Existing builtin identities are retained, not serialized or recreated.
    return 2 + max(0, count - 1) + 4 * count


def _memory_policy_string(length: int) -> int:
    return 6 * length + 2


def _memory_policy_dimensions_match(value: str, dimensions: tuple) -> bool:
    from src.memory.header_bounds import _native_memory_utf8_width
    if type(value) is not str or not _memory_policy_dimensions_valid(dimensions):
        return False
    commas = colons = slashes = dots = hyphens = 0
    ascii_only = plain_dns = True
    whitespace = percent = brackets = at_sign = question = hash_mark = False
    has_letter = non_hex_letter = False
    for character in value:
        code = ord(character)
        letter = 65 <= code <= 90 or 97 <= code <= 122
        if code > 127:
            ascii_only = False
        if not (48 <= code <= 57 or letter or character in ".-"):
            plain_dns = False
        commas += character == ","
        colons += character == ":"
        slashes += character == "/"
        dots += character == "."
        hyphens += character == "-"
        whitespace = whitespace or character.isspace()
        percent = percent or character == "%"
        brackets = brackets or character in "[]"
        at_sign = at_sign or character == "@"
        question = question or character == "?"
        hash_mark = hash_mark or character == "#"
        has_letter = has_letter or letter
        non_hex_letter = non_hex_letter or (letter and character not in "abcdefABCDEFxX")
    width = _native_memory_utf8_width(value)
    return (dimensions[0] == width and dimensions[1] == 6 * width + 2
        and dimensions[2] == ascii_only and dimensions[3] == commas
        and dimensions[4] == colons and dimensions[5] == slashes
        and dimensions[6] == dots and dimensions[7] == hyphens
        and dimensions[8] == plain_dns and dimensions[9] == len(value)
        and dimensions[10] == whitespace and dimensions[11] == percent
        and dimensions[12] == brackets and dimensions[13] == at_sign
        and dimensions[14] == question and dimensions[15] == hash_mark
        and dimensions[16] == ("://" in value) and dimensions[17] == has_letter
        and dimensions[18] == non_hex_letter)


def _memory_policy_dimensions_valid(dimensions: tuple) -> bool:
    if type(dimensions) is not tuple or len(dimensions) != 19:
        return False
    for index in range(19):
        if index in (0, 1, 3, 4, 5, 6, 7, 9):
            if type(dimensions[index]) is not int or dimensions[index] < 0:
                return False
        elif type(dimensions[index]) is not bool:
            return False
    return dimensions[1] == 6 * dimensions[0] + 2


def _memory_policy_known_upper(host: tuple, allow: tuple, block: tuple) -> int:
    """Closed original plain-DNS logical appearances; interpreter heap is excluded.

    Inputs are numeric facts only. Authentication and current-input comparison
    remain at the original REPORT owner/evaluation boundary. This is neither a
    policy decision nor a permission to use some other input shape.
    """
    from src.memory.header_bounds import HeaderBoundsError
    if (not _memory_policy_dimensions_valid(host)
        or not _memory_policy_dimensions_valid(allow)
        or not _memory_policy_dimensions_valid(block)
        or not host[2] or not host[8] or not host[18] or host[6] == 3
        or host[0] != host[9] or host[0] == 0
        or not allow[2] or not allow[8] or allow[0] != allow[9]
        or allow[3] != 0 or allow[0] == 0
        or block[0] != 0 or block[9] != 0
        or host[3] or host[4] or host[5] or host[10] or host[11] or host[12]
        or host[13] or host[14] or host[15] or host[16]
        or allow[3] or allow[4] or allow[5] or allow[10] or allow[11] or allow[12]
        or allow[13] or allow[14] or allow[15] or allow[16]):
        raise HeaderBoundsError("memory_report_site_policy_numeric_unavailable")
    h, a = host[0], allow[0]
    string = _memory_policy_string
    slots = _memory_policy_slots
    # strip, https-prefix, parser lstrip/replace/slices/scheme lower,
    # hostname partition/lower/concatenation and final owner lower.
    u = h + 8
    normalize = string(h) + string(u)
    normalize += string(u) + string(0) + 3 * (string(u) + string(0))
    normalize += 2 * string(1) + string(5) + 5 * string(1)
    normalize += string(5) + string(5) + string(h + 2) + string(2)
    normalize += string(h) + string(0) + slots(2)
    # Two original _coerce_args calls: args/slice/(_noop,)/return.
    normalize += 2 * (slots(2) + slots(1) + slots(1) + slots(3))
    # Existing wrapper args/key plus the original tuple-valued results.
    # No invented serialization for the C LRU link/dictionary internals.
    normalize += slots(3) + slots(6) + 2 * slots(5) + 2 * slots(6)
    # Generic namedtuple class calls add five/six argument tuples. Their
    # generated __new__ calls tuple.__new__(cls, fields): its VARARGS wrapper
    # adds a two-reference argument tuple and a one-reference slice each.
    normalize += slots(5) + slots(6) + 2 * (slots(2) + slots(1))
    # _hostinfo has three partition triples; hostname adds its % triple.
    normalize += 4 * slots(3) + slots(2) + 4 * (string(h) + 2 * string(0))
    normalize += string(h) + string(h) + string(h) + string(h)
    # Both original rule parsers run even with an empty blocklist.
    # split/list/set/tuple containers and per-rule strip/lower/lstrip.
    rules = (slots(1) + string(a) + slots(0) + slots(0)
        + 3 * string(a) + 5 + 5 + slots(1))
    rules += slots(1) + string(0) + slots(0) + slots(0) + 2 * string(0) + slots(0)
    # The original builtin tuple(normalized) uses tuple_vectorcall even on
    # the unspecialized CALL path; no separate one-argument tuple is made.
    # IPv4 rejects non-four octets; IPv6 rejects one colon component.
    # Original split/partition containers, error repr,
    # error message and error-argument tuple appearances remain charged.
    ip = string(h) + slots(host[6] + 1) + 6 * (h - host[6]) + 2 * (host[6] + 1)
    ip += string(h + 2) + string(h + 2 + len('Expected 4 octets in ')) + slots(1)
    ip += slots(3) + slots(2) + string(h) + 2 * string(0) + slots(1) + string(h)
    ip += (slots(2) + 16) + string(h + 2) + string(h + 2 + len('At least 3 parts expected in ')) + slots(1)
    ip += string(h + 2) + string(h + 2 + len(' does not appear to be an IPv4 or IPv6 address')) + slots(1)
    # Original native inet_aton fails for a non-hex DNS letter. Its CPython
    # error text is fixed; retain the original error args/instance storage.
    ip += string(len('illegal IP address string passed to inet_aton')) + slots(1)
    # Three original except-type tuples (two ip_address catches and one
    # inet_aton catch), return tuple, and explicit any() generator bookkeeping.
    # Interpreter frames/tracebacks/iterators are not encoded values.
    internal = ip + 3 * slots(2) + slots(2) + 32
    # Original IPv4Address(address)/IPv6Address(address) generic class calls.
    internal += 2 * slots(1)
    matching = string(a + 1)  # Original endswith dot-prefix string.
    # Python keyword dictionary (largest five-field call) and six-field
    # dataclass attribute map: source literal keys, separators, retained refs.
    # kwargs matching branch keys total61 ASCII chars; object keys total67.
    decision = (2 + 4 + 5 + 6 * 61 + 2 * 5 + 4 * 5)
    decision += (2 + 5 + 6 + 6 * 67 + 2 * 6 + 4 * 6)
    decision += string(h) + string(a) + string(len("not_allowlisted")) + 5 + 5 + slots(0)
    return normalize + rules + internal + matching + decision


def _memory_report_site_policy_future_upper(budget) -> int:
    """All original host calls, without assuming the any() short circuit.

    Numeric planning only, on the same original issued REPORT8 inventory.
    This does not evaluate policy, issue permission or activate an effect.
    """
    from src.runtime_plugins.task_capability import _report_memory_admission_payer
    from src.work_board.review import _MEMORY_REPORT_PRINCIPAL_SCOPE, _memory_report_principal_numeric_owner
    from src.memory.header_bounds import HeaderBoundsError
    if budget is None:
        raise HeaderBoundsError("memory_report_admission_numeric_owner_changed")
    _report_memory_admission_payer(budget)
    record = _MEMORY_REPORT_PRINCIPAL_SCOPE.get()
    _memory_report_principal_numeric_owner(record.db, record.owner_principal_id)
    retained = getattr(record, "_site_policy_numeric_inputs", None)
    if (type(retained) is not tuple or len(retained) != 3
        or type(retained[0]) is not tuple or not 1 <= len(retained[0]) <= 8
        or not _memory_policy_dimensions_match(settings.browser_site_allowlist, retained[1])
        or not _memory_policy_dimensions_match(settings.browser_site_blocklist, retained[2])):
        raise HeaderBoundsError("memory_report_site_policy_numeric_owner_changed")
    upper = 0
    for dimensions in retained[0]:
        upper += _memory_policy_known_upper(dimensions, retained[1], retained[2])
    return upper


def _memory_policy_prepaid_upper(value: str, budget) -> int:
    """Authenticate the existing REPORT8 payer and its actual current inputs."""
    from src.runtime_plugins.task_capability import _report_memory_admission_payer
    from src.work_board.review import _MEMORY_REPORT_PRINCIPAL_SCOPE, _memory_report_principal_numeric_owner
    from src.memory.header_bounds import HeaderBoundsError
    if budget is None:
        raise HeaderBoundsError("memory_report_admission_numeric_owner_changed")
    _report_memory_admission_payer(budget)
    record = _MEMORY_REPORT_PRINCIPAL_SCOPE.get()
    _memory_report_principal_numeric_owner(record.db, record.owner_principal_id)
    retained = getattr(record, "_site_policy_numeric_inputs", None)
    if (type(retained) is not tuple or len(retained) != 3
        or type(retained[0]) is not tuple or not 1 <= len(retained[0]) <= 8
        or not _memory_policy_dimensions_match(settings.browser_site_allowlist, retained[1])
        or not _memory_policy_dimensions_match(settings.browser_site_blocklist, retained[2])):
        raise HeaderBoundsError("memory_report_site_policy_numeric_owner_changed")
    for dimensions in retained[0]:
        if _memory_policy_dimensions_match(value, dimensions):
            return _memory_policy_known_upper(dimensions, retained[1], retained[2])
    raise HeaderBoundsError("memory_report_site_policy_numeric_owner_changed")


def evaluate_site_access(url_or_hostname: str, *, resolve_dns: bool = False,
                         _memory_header_budget=None) -> SiteAccessDecision:
    if _memory_header_budget is not None:
        from src.memory.header_bounds import HeaderBoundsError
        from src.runtime_plugins.task_capability import _report_memory_allocation_pay
        if resolve_dns:
            raise HeaderBoundsError("memory_report_site_policy_numeric_unavailable")
        upper = _memory_policy_prepaid_upper(url_or_hostname, _memory_header_budget)
        _report_memory_allocation_pay(_memory_header_budget, "site-policy-original-plain-dns", upper)
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
