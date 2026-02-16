import asyncio
import concurrent.futures

from smolagents import tool

from src.vault.repository import vault_repository


def _run(coro):
    """Run an async coroutine from sync context (for smolagents tools)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


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
        return f"Secret '{key}' not found in vault."
    return result


@tool
def list_secrets() -> str:
    """List all secret keys stored in the vault.

    Returns only key names and descriptions — never the secret values.

    Returns:
        Formatted list of stored secret keys with descriptions.
    """
    keys = _run(vault_repository.list_keys())
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
        return f"Secret '{key}' not found in vault."
    return f"Secret '{key}' deleted from vault."
