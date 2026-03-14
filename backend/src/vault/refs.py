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
    if session_id is None:
        return value

    if isinstance(value, str):
        return _resolve_secret_refs_in_string(value, session_id)
    if isinstance(value, list):
        return [resolve_secret_refs(item, session_id) for item in value]
    if isinstance(value, tuple):
        return tuple(resolve_secret_refs(item, session_id) for item in value)
    if isinstance(value, dict):
        return {key: resolve_secret_refs(item, session_id) for key, item in value.items()}
    return value


def _resolve_secret_refs_in_string(value: str, session_id: str) -> str:
    if _REF_PREFIX not in value:
        return value

    with _lock:
        session_refs = _issued_refs.get(session_id, {}).copy()

    def _replace(match: re.Match[str]) -> str:
        token = match.group(1)
        secret_value = session_refs.get(token)
        if secret_value is None:
            return match.group(0)
        return secret_value[0]

    return _REF_RE.sub(_replace, value)
