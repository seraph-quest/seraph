import logging
from pathlib import Path

from smolagents import tool

from config.settings import settings
from src.audit.runtime import log_integration_event_sync

logger = logging.getLogger(__name__)


def _filesystem_details(file_path: str, operation: str, **extra: object) -> dict[str, object]:
    return {
        "file_path": file_path,
        "operation": operation,
        **extra,
    }


def _safe_resolve(file_path: str) -> Path:
    """Resolve a file path ensuring it stays within the workspace directory."""
    workspace = Path(settings.workspace_dir).resolve()
    resolved = (workspace / file_path).resolve()
    if not str(resolved).startswith(str(workspace)):
        raise ValueError(f"Path traversal blocked: {file_path}")
    return resolved


@tool
def read_file(file_path: str) -> str:
    """Read the contents of a file within the workspace directory.

    Args:
        file_path: Relative path to the file within the workspace.

    Returns:
        The text contents of the file.
    """
    try:
        resolved = _safe_resolve(file_path)
    except ValueError as exc:
        log_integration_event_sync(
            integration_type="filesystem",
            name="workspace",
            outcome="blocked",
            details=_filesystem_details(file_path, "read", error=str(exc)),
        )
        raise

    if not resolved.exists():
        log_integration_event_sync(
            integration_type="filesystem",
            name="workspace",
            outcome="empty_result",
            details=_filesystem_details(file_path, "read", reason="missing_file"),
        )
        return f"Error: File not found: {file_path}"
    if not resolved.is_file():
        log_integration_event_sync(
            integration_type="filesystem",
            name="workspace",
            outcome="failed",
            details=_filesystem_details(file_path, "read", reason="not_a_file"),
        )
        return f"Error: Not a file: {file_path}"

    try:
        content = resolved.read_text(encoding="utf-8")
        log_integration_event_sync(
            integration_type="filesystem",
            name="workspace",
            outcome="succeeded",
            details=_filesystem_details(file_path, "read", length=len(content)),
        )
        return content
    except Exception as exc:
        log_integration_event_sync(
            integration_type="filesystem",
            name="workspace",
            outcome="failed",
            details=_filesystem_details(file_path, "read", error=str(exc)),
        )
        logger.exception("Failed to read file from workspace")
        return f"Error: Failed to read file: {exc}"


@tool
def write_file(file_path: str, content: str) -> str:
    """Write content to a file within the workspace directory. Creates parent directories if needed.

    Args:
        file_path: Relative path to the file within the workspace.
        content: The text content to write to the file.

    Returns:
        A confirmation message.
    """
    try:
        resolved = _safe_resolve(file_path)
    except ValueError as exc:
        log_integration_event_sync(
            integration_type="filesystem",
            name="workspace",
            outcome="blocked",
            details=_filesystem_details(file_path, "write", length=len(content), error=str(exc)),
        )
        raise

    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        log_integration_event_sync(
            integration_type="filesystem",
            name="workspace",
            outcome="succeeded",
            details=_filesystem_details(file_path, "write", length=len(content)),
        )
        return f"Successfully wrote {len(content)} characters to {file_path}"
    except Exception as exc:
        log_integration_event_sync(
            integration_type="filesystem",
            name="workspace",
            outcome="failed",
            details=_filesystem_details(file_path, "write", length=len(content), error=str(exc)),
        )
        logger.exception("Failed to write file into workspace")
        return f"Error: Failed to write file: {exc}"
