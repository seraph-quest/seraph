"""Execution context shared with tool wrappers during agent runs."""

from contextvars import ContextVar, Token

_current_session_id: ContextVar[str | None] = ContextVar("approval_session_id", default=None)
_current_approval_mode: ContextVar[str] = ContextVar("approval_mode", default="high_risk")


def set_runtime_context(session_id: str | None, approval_mode: str) -> tuple[Token, Token]:
    """Set session/approval context for the current agent execution."""
    return (
        _current_session_id.set(session_id),
        _current_approval_mode.set(approval_mode),
    )


def reset_runtime_context(tokens: tuple[Token, Token]) -> None:
    """Restore the previous execution context."""
    session_token, approval_token = tokens
    _current_session_id.reset(session_token)
    _current_approval_mode.reset(approval_token)


def get_current_session_id() -> str | None:
    return _current_session_id.get()


def get_current_approval_mode() -> str:
    return _current_approval_mode.get()
