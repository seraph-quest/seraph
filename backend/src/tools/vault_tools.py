import asyncio
import concurrent.futures
import logging

from smolagents import tool

from src.approval.runtime import get_current_session_id
from src.audit.repository import audit_repository
from src.tools.policy import get_current_tool_policy_mode, get_tool_risk_level
from src.vault.repository import vault_repository

logger = logging.getLogger(__name__)


def _run(coro):
    """Run an async coroutine from sync context (for smolagents tools)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _log_secret_event(
    *,
    event_type: str,
    tool_name: str,
    summary: str,
    details: dict | None = None,
) -> None:
    """Best-effort audit logging for vault operations."""
    try:
        _run(audit_repository.log_event(
            event_type=event_type,
            summary=summary,
            session_id=get_current_session_id(),
            actor="agent",
            tool_name=tool_name,
            risk_level=get_tool_risk_level(tool_name),
            policy_mode=get_current_tool_policy_mode(),
            details=details,
        ))
    except Exception:
        logger.warning("Failed to log vault audit event for %s", tool_name, exc_info=True)


@tool
def store_secret(key: str, value: str, description: str = "") -> str:
    """Store an encrypted secret in the vault for later retrieval.

    Use this to persist API tokens, passwords, or other credentials across
    sessions. Secrets are encrypted at rest and only accessible via agent tools.

    IMPORTANT: NEVER display the secret value back to the user in chat.

    Args:
        key: Unique identifier for the secret (e.g. 'moltbook_token').
        value: The secret value to encrypt and store.
        description: Optional human-readable description of what this secret is for.

    Returns:
        Confirmation that the secret was stored (never includes the value).
    """
    _run(vault_repository.store(
        key=key,
        value=value,
        description=description or None,
    ))
    _log_secret_event(
        event_type="secret_store",
        tool_name="store_secret",
        summary=f"Stored secret key '{key}' in vault",
        details={"key": key, "has_description": bool(description)},
    )
    return f"Secret '{key}' stored securely in vault."


@tool
def get_secret(key: str) -> str:
    """Retrieve a decrypted secret from the vault.

    IMPORTANT: NEVER display the returned secret value in chat. Use it only
    in tool calls (e.g. as an Authorization header in http_request).

    Args:
        key: The key of the secret to retrieve (e.g. 'moltbook_token').

    Returns:
        The decrypted secret value, or a message if not found.
    """
    result = _run(vault_repository.get(key))
    if result is None:
        _log_secret_event(
            event_type="secret_access",
            tool_name="get_secret",
            summary=f"Attempted to access missing secret key '{key}'",
            details={"key": key, "found": False},
        )
        return f"Secret '{key}' not found in vault."
    _log_secret_event(
        event_type="secret_access",
        tool_name="get_secret",
        summary=f"Accessed secret key '{key}'",
        details={"key": key, "found": True},
    )
    return result


@tool
def list_secrets() -> str:
    """List all secret keys stored in the vault.

    Returns only key names and descriptions — never the secret values.

    Returns:
        Formatted list of stored secret keys with descriptions.
    """
    keys = _run(vault_repository.list_keys())
    _log_secret_event(
        event_type="secret_list",
        tool_name="list_secrets",
        summary=f"Listed vault secret keys ({len(keys)} total)",
        details={"count": len(keys)},
    )
    if not keys:
        return "No secrets stored in vault."
    lines = []
    for entry in keys:
        desc = f" — {entry['description']}" if entry["description"] else ""
        lines.append(f"- {entry['key']}{desc}")
    return "\n".join(lines)


@tool
def delete_secret(key: str) -> str:
    """Delete a secret from the vault.

    Args:
        key: The key of the secret to delete.

    Returns:
        Confirmation message.
    """
    success = _run(vault_repository.delete(key))
    if not success:
        _log_secret_event(
            event_type="secret_delete",
            tool_name="delete_secret",
            summary=f"Attempted to delete missing secret key '{key}'",
            details={"key": key, "deleted": False},
        )
        return f"Secret '{key}' not found in vault."
    _log_secret_event(
        event_type="secret_delete",
        tool_name="delete_secret",
        summary=f"Deleted secret key '{key}' from vault",
        details={"key": key, "deleted": True},
    )
    return f"Secret '{key}' deleted from vault."
