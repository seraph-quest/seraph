"""Session-scoped opaque references for secret values."""

from __future__ import annotations

import re
import time
from threading import Lock
from uuid import uuid4

_REF_PREFIX = "secret://"
_REF_RE = re.compile(r"secret://([0-9a-f]{32})")
_REF_TTL_SECONDS = 3600

_lock = Lock()
_issued_refs: dict[str, dict[str, dict[str, object]]] = {}


def _prune_expired(now: float) -> None:
    expired_sessions: list[str] = []
    for session_id, refs in _issued_refs.items():
        expired_tokens = []
        for token, ref in refs.items():
            issued_at = float(ref.get("issued_at", 0.0))
            ttl_seconds = float(ref.get("ttl_seconds", _REF_TTL_SECONDS))
            if now - issued_at > ttl_seconds:
                expired_tokens.append(token)
        for token in expired_tokens:
            refs.pop(token, None)
        if not refs:
            expired_sessions.append(session_id)

    for session_id in expired_sessions:
        _issued_refs.pop(session_id, None)


def issue_secret_ref(
    session_id: str,
    secret_value: str,
    *,
    tool_name: str | None = None,
    field_name: str | None = None,
    destination_host: str | None = None,
    destination_scheme: str | None = None,
    destination_port: int | None = None,
    purpose: str | None = None,
    secret_version: str | None = None,
    revocation_epoch: int = 0,
    one_time: bool = False,
    ttl_seconds: int = _REF_TTL_SECONDS,
) -> str:
    """Create an opaque reference bound to the current session and required scope."""
    normalized_tool = _normalize_scope_value(tool_name)
    normalized_field = _normalize_scope_value(field_name)
    normalized_host = _normalize_scope_value(destination_host)
    normalized_scheme = _normalize_scope_value(destination_scheme)
    if not normalized_tool or not normalized_field or not normalized_host or not normalized_scheme:
        raise ValueError("Secret references require tool, field, and destination scope.")

    token = uuid4().hex
    now = time.time()
    scope = {
        "tool_name": normalized_tool,
        "field_name": normalized_field,
        "destination_host": normalized_host,
        "destination_scheme": normalized_scheme,
        "destination_port": int(destination_port) if destination_port is not None else None,
        "purpose": _normalize_scope_value(purpose),
        "secret_version": _normalize_scope_value(secret_version),
        "revocation_epoch": int(revocation_epoch),
        "one_time": bool(one_time),
    }
    with _lock:
        _prune_expired(now)
        _issued_refs.setdefault(session_id, {})[token] = {
            "secret_value": secret_value,
            "issued_at": now,
            "ttl_seconds": int(ttl_seconds),
            "scope": scope,
            "used": False,
        }
    return f"{_REF_PREFIX}{token}"


def resolve_secret_refs(value, session_id: str | None):
    """Recursively resolve secret references inside nested tool arguments."""
    resolved, _secret_values = resolve_secret_refs_with_values(value, session_id)
    return resolved


def resolve_secret_refs_with_values(
    value,
    session_id: str | None,
    *,
    tool_name: str | None = None,
    field_name: str | None = None,
    destination_host: str | None = None,
    destination_scheme: str | None = None,
    destination_port: int | None = None,
    purpose: str | None = None,
    secret_version: str | None = None,
    revocation_epoch: int = 0,
) -> tuple[object, list[str]]:
    """Resolve secret references and return the raw values used for leak checks."""
    if session_id is None:
        if _contains_secret_ref(value):
            raise ValueError("Secret references require an active session.")
        return value, []

    if isinstance(value, str):
        return _resolve_secret_refs_in_string(
            value,
            session_id,
            tool_name=tool_name,
            field_name=field_name,
            destination_host=destination_host,
            destination_scheme=destination_scheme,
            destination_port=destination_port,
            purpose=purpose,
            secret_version=secret_version,
            revocation_epoch=revocation_epoch,
        )
    if isinstance(value, list):
        resolved_items: list[object] = []
        secret_values: list[str] = []
        for item in value:
            resolved_item, item_secrets = resolve_secret_refs_with_values(
                item,
                session_id,
                tool_name=tool_name,
                field_name=field_name,
                destination_host=destination_host,
                destination_scheme=destination_scheme,
                destination_port=destination_port,
                purpose=purpose,
                secret_version=secret_version,
                revocation_epoch=revocation_epoch,
            )
            resolved_items.append(resolved_item)
            secret_values.extend(item_secrets)
        return resolved_items, secret_values
    if isinstance(value, tuple):
        resolved_items = []
        secret_values: list[str] = []
        for item in value:
            resolved_item, item_secrets = resolve_secret_refs_with_values(
                item,
                session_id,
                tool_name=tool_name,
                field_name=field_name,
                destination_host=destination_host,
                destination_scheme=destination_scheme,
                destination_port=destination_port,
                purpose=purpose,
                secret_version=secret_version,
                revocation_epoch=revocation_epoch,
            )
            resolved_items.append(resolved_item)
            secret_values.extend(item_secrets)
        return tuple(resolved_items), secret_values
    if isinstance(value, dict):
        resolved_dict: dict[object, object] = {}
        secret_values: list[str] = []
        for key, item in value.items():
            resolved_item, item_secrets = resolve_secret_refs_with_values(
                item,
                session_id,
                tool_name=tool_name,
                field_name=field_name,
                destination_host=destination_host,
                destination_scheme=destination_scheme,
                destination_port=destination_port,
                purpose=purpose,
                secret_version=secret_version,
                revocation_epoch=revocation_epoch,
            )
            resolved_dict[key] = resolved_item
            secret_values.extend(item_secrets)
        return resolved_dict, secret_values
    return value, []


def _resolve_secret_refs_in_string(
    value: str,
    session_id: str,
    *,
    tool_name: str | None = None,
    field_name: str | None = None,
    destination_host: str | None = None,
    destination_scheme: str | None = None,
    destination_port: int | None = None,
    purpose: str | None = None,
    secret_version: str | None = None,
    revocation_epoch: int = 0,
) -> tuple[str, list[str]]:
    if _REF_PREFIX not in value:
        return value, []

    now = time.time()
    with _lock:
        _prune_expired(now)
        session_refs = _issued_refs.get(session_id, {})

    secret_values: list[str] = []
    consumed_tokens: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        token = match.group(1)
        ref = session_refs.get(token)
        if ref is None:
            raise ValueError("Secret reference is expired, unknown, or belongs to another session.")
        scope = ref.get("scope")
        if not isinstance(scope, dict):
            scope = {}
        _assert_scope_matches(
            scope,
            tool_name=tool_name,
            field_name=field_name,
            destination_host=destination_host,
            destination_scheme=destination_scheme,
            destination_port=destination_port,
            purpose=purpose,
            secret_version=secret_version,
            revocation_epoch=revocation_epoch,
        )
        if bool(scope.get("one_time")) and bool(ref.get("used")):
            raise ValueError("Secret reference is already used.")
        secret_value = str(ref["secret_value"])
        secret_values.append(secret_value)
        if bool(scope.get("one_time")):
            consumed_tokens.append(token)
        return secret_value

    resolved = _REF_RE.sub(_replace, value)
    if consumed_tokens:
        with _lock:
            refs = _issued_refs.get(session_id, {})
            for token in consumed_tokens:
                if token in refs:
                    refs[token]["used"] = True
    return resolved, secret_values


def _normalize_scope_value(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    return normalized or None


def _assert_scope_matches(
    scope: dict[str, object],
    *,
    tool_name: str | None,
    field_name: str | None,
    destination_host: str | None,
    destination_scheme: str | None,
    destination_port: int | None,
    purpose: str | None,
    secret_version: str | None,
    revocation_epoch: int,
) -> None:
    expected = {
        "tool_name": _normalize_scope_value(tool_name),
        "field_name": _normalize_scope_value(field_name),
        "destination_host": _normalize_scope_value(destination_host),
        "destination_scheme": _normalize_scope_value(destination_scheme),
        "purpose": _normalize_scope_value(purpose),
        "secret_version": _normalize_scope_value(secret_version),
    }
    for key, actual in expected.items():
        scoped_value = scope.get(key)
        if scoped_value is not None and scoped_value != actual:
            label = key.replace("_", " ")
            raise ValueError(f"Secret reference {label} scope mismatch.")
    scoped_port = scope.get("destination_port")
    if scoped_port is not None and int(scoped_port) != int(destination_port or 0):
        raise ValueError("Secret reference destination port scope mismatch.")
    scoped_epoch = int(scope.get("revocation_epoch") or 0)
    if scoped_epoch < int(revocation_epoch):
        raise ValueError("Secret reference is revoked by a newer revocation epoch.")


def _contains_secret_ref(value) -> bool:
    if isinstance(value, str):
        return _REF_PREFIX in value
    if isinstance(value, list):
        return any(_contains_secret_ref(item) for item in value)
    if isinstance(value, tuple):
        return any(_contains_secret_ref(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_secret_ref(item) for item in value.values())
    return False
