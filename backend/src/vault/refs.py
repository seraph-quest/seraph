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
_issued_refs: dict[str, dict[str, tuple[str, float]]] = {}


def _prune_expired(now: float) -> None:
    expired_sessions: list[str] = []
    for session_id, refs in _issued_refs.items():
        expired_tokens = [
            token for token, (_, issued_at) in refs.items()
            if now - issued_at > _REF_TTL_SECONDS
        ]
        for token in expired_tokens:
            refs.pop(token, None)
        if not refs:
            expired_sessions.append(session_id)

    for session_id in expired_sessions:
        _issued_refs.pop(session_id, None)


def issue_secret_ref(session_id: str, secret_value: str) -> str:
    """Create an opaque reference bound to the current session."""
    token = uuid4().hex
    now = time.time()
    with _lock:
        _prune_expired(now)
        _issued_refs.setdefault(session_id, {})[token] = (secret_value, now)
    return f"{_REF_PREFIX}{token}"


def resolve_secret_refs(value, session_id: str | None):
    """Recursively resolve secret references inside nested tool arguments."""
    resolved, _secret_values = resolve_secret_refs_with_values(value, session_id)
    return resolved


def resolve_secret_refs_with_values(value, session_id: str | None) -> tuple[object, list[str]]:
    """Resolve secret references and return the raw values used for leak checks."""
    if session_id is None:
        if _contains_secret_ref(value):
            raise ValueError("Secret references require an active session.")
        return value, []

    if isinstance(value, str):
        return _resolve_secret_refs_in_string(value, session_id)
    if isinstance(value, list):
        resolved_items: list[object] = []
        secret_values: list[str] = []
        for item in value:
            resolved_item, item_secrets = resolve_secret_refs_with_values(item, session_id)
            resolved_items.append(resolved_item)
            secret_values.extend(item_secrets)
        return resolved_items, secret_values
    if isinstance(value, tuple):
        resolved_items = []
        secret_values: list[str] = []
        for item in value:
            resolved_item, item_secrets = resolve_secret_refs_with_values(item, session_id)
            resolved_items.append(resolved_item)
            secret_values.extend(item_secrets)
        return tuple(resolved_items), secret_values
    if isinstance(value, dict):
        resolved_dict: dict[object, object] = {}
        secret_values: list[str] = []
        for key, item in value.items():
            resolved_item, item_secrets = resolve_secret_refs_with_values(item, session_id)
            resolved_dict[key] = resolved_item
            secret_values.extend(item_secrets)
        return resolved_dict, secret_values
    return value, []


def _resolve_secret_refs_in_string(value: str, session_id: str) -> tuple[str, list[str]]:
    if _REF_PREFIX not in value:
        return value, []

    now = time.time()
    with _lock:
        _prune_expired(now)
        session_refs = _issued_refs.get(session_id, {}).copy()

    secret_values: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        token = match.group(1)
        secret_value = session_refs.get(token)
        if secret_value is None:
            raise ValueError("Secret reference is expired, unknown, or belongs to another session.")
        secret_values.append(secret_value[0])
        return secret_value[0]

    return _REF_RE.sub(_replace, value), secret_values


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
