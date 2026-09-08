from contextvars import ContextVar, Token
from threading import Event


class RuntimeRevokedError(PermissionError):
    pass


_guard: ContextVar[Event | None] = ContextVar("operator_revocation_guard", default=None)


def set_revocation_guard(guard: Event) -> Token:
    return _guard.set(guard)


def reset_revocation_guard(token: Token) -> None:
    _guard.reset(token)


def assert_runtime_not_revoked() -> None:
    guard = _guard.get()
    if guard is not None and guard.is_set():
        raise RuntimeRevokedError("authenticated operator session was revoked")
