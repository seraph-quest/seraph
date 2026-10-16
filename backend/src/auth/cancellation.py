from contextvars import ContextVar, Token
from threading import Event

from src.agent.exceptions import AgentControlFlowInterrupt


class RuntimeRevokedError(PermissionError, AgentControlFlowInterrupt):
    """The authenticated operator session was revoked while work was running.

    It is also an agent control-flow interrupt: raised inside a tool, it
    leaves the agent loop at once instead of becoming an observation (#1116).
    """


_guard: ContextVar[Event | None] = ContextVar("operator_revocation_guard", default=None)


def set_revocation_guard(guard: Event) -> Token:
    return _guard.set(guard)


def reset_revocation_guard(token: Token) -> None:
    _guard.reset(token)


def assert_runtime_not_revoked() -> None:
    guard = _guard.get()
    if guard is not None and guard.is_set():
        raise RuntimeRevokedError("authenticated operator session was revoked")
