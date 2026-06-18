import logging
import difflib
import hashlib
import json
from pathlib import Path

from smolagents import tool

from config.settings import settings
from src.artifacts.registry import build_artifact_record
from src.audit.runtime import log_integration_event_sync

logger = logging.getLogger(__name__)

_SECRET_PATH_PARTS = {
    ".aws",
    ".azure",
    ".config/gcloud",
    ".docker",
    ".gnupg",
    ".ssh",
}
_SECRET_FILE_NAMES = {
    ".env",
    ".env.dev",
    ".env.local",
    ".env.production",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "google_credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "known_hosts",
    "private_key",
}
_SECRET_FILE_SUFFIXES = {
    ".key",
    ".p12",
    ".pem",
    ".pfx",
}


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
    try:
        resolved.relative_to(workspace)
    except ValueError:
        raise ValueError(f"Path traversal blocked: {file_path}")
    return resolved


def _is_secret_like_workspace_path(file_path: str) -> bool:
    normalized = file_path.replace("\\", "/").strip().lower()
    if not normalized:
        return False
    path = Path(normalized)
    parts = set(path.parts)
    if parts & _SECRET_PATH_PARTS:
        return True
    name = path.name
    if name in _SECRET_FILE_NAMES:
        return True
    if any(name.endswith(suffix) for suffix in _SECRET_FILE_SUFFIXES):
        return True
    return any(token in name for token in ("credential", "secret", "token"))


def _assert_not_secret_like_path(file_path: str, operation: str) -> None:
    if not _is_secret_like_workspace_path(file_path):
        return
    raise ValueError(
        f"Secret-like workspace path blocked for {operation}: {file_path}. "
        "Use the vault or an explicit secret-management path instead."
    )


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _patch_receipt(
    *,
    file_path: str,
    operation: str,
    before: str,
    after: str,
    diff: str,
    occurrence_count: int,
    applied: bool,
    before_hash_guarded: bool,
) -> str:
    changed_lines = sum(1 for line in diff.splitlines() if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
    artifact = build_artifact_record(
        file_path=file_path,
        artifact_type="workspace_patch",
        producer=f"filesystem:{operation}",
        trust_boundary="workspace_write",
        recovery_hint="Apply the rollback restore_text hash through apply_workspace_patch after checking expected_before_sha256.",
        content=after,
    )
    return json.dumps(
        {
            "artifact_id": artifact["artifact_id"],
            "artifact": artifact,
            "operation": operation,
            "file_path": file_path,
            "applied": applied,
            "occurrence_count": occurrence_count,
            "changed_lines": changed_lines,
            "before_sha256": _sha256_text(before),
            "after_sha256": _sha256_text(after),
            "before_hash_guarded": before_hash_guarded,
            "rollback": {
                "tool": "apply_workspace_patch",
                "file_path": file_path,
                "requires_old_text": True,
                "expected_before_sha256": _sha256_text(after),
                "old_text_sha256": _sha256_text(after),
                "restore_text_sha256": _sha256_text(before),
            },
            "diff": diff,
        },
        sort_keys=True,
    )


def _replacement_diff(file_path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
        )
    )


def _replace_once(content: str, old_text: str, new_text: str, expected_occurrences: int) -> tuple[str, int]:
    if expected_occurrences != 1:
        raise ValueError("expected_occurrences must be 1 until indexed or replace-all patching is supported")
    if not old_text:
        raise ValueError("old_text must not be empty")
    occurrence_count = content.count(old_text)
    if occurrence_count != expected_occurrences:
        raise ValueError(
            f"Expected {expected_occurrences} occurrence(s) of old_text, found {occurrence_count}"
        )
    return content.replace(old_text, new_text, 1), occurrence_count


@tool
def read_file(file_path: str) -> str:
    """Read the contents of a file within the workspace directory.

    Args:
        file_path: Relative path to the file within the workspace.

    Returns:
        The text contents of the file.
    """
    try:
        _assert_not_secret_like_path(file_path, "read")
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
        _assert_not_secret_like_path(file_path, "write")
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


@tool
def preview_workspace_patch(
    file_path: str,
    old_text: str,
    new_text: str,
    expected_occurrences: int = 1,
) -> str:
    """Preview a bounded workspace text replacement and return a receipt with a unified diff.

    Args:
        file_path: Relative path to the file within the workspace.
        old_text: Exact text to replace.
        new_text: Replacement text.
        expected_occurrences: Required number of old_text occurrences before the preview is valid.

    Returns:
        A JSON receipt containing the diff, hashes, and application status.
    """
    try:
        _assert_not_secret_like_path(file_path, "preview_patch")
        resolved = _safe_resolve(file_path)
        before = resolved.read_text(encoding="utf-8")
        after, occurrence_count = _replace_once(before, old_text, new_text, expected_occurrences)
        diff = _replacement_diff(file_path, before, after)
        log_integration_event_sync(
            integration_type="filesystem",
            name="workspace",
            outcome="succeeded",
            details=_filesystem_details(
                file_path,
                "preview_patch",
                occurrence_count=occurrence_count,
                changed_lines=sum(1 for line in diff.splitlines() if line.startswith(("+", "-"))),
            ),
        )
        return _patch_receipt(
            file_path=file_path,
            operation="preview_patch",
            before=before,
            after=after,
            diff=diff,
            occurrence_count=occurrence_count,
            applied=False,
            before_hash_guarded=False,
        )
    except ValueError as exc:
        log_integration_event_sync(
            integration_type="filesystem",
            name="workspace",
            outcome="blocked",
            details=_filesystem_details(file_path, "preview_patch", error=str(exc)),
        )
        raise
    except Exception as exc:
        log_integration_event_sync(
            integration_type="filesystem",
            name="workspace",
            outcome="failed",
            details=_filesystem_details(file_path, "preview_patch", error=str(exc)),
        )
        logger.exception("Failed to preview workspace patch")
        return f"Error: Failed to preview workspace patch: {exc}"


@tool
def apply_workspace_patch(
    file_path: str,
    old_text: str,
    new_text: str,
    expected_occurrences: int = 1,
    expected_before_sha256: str = "",
) -> str:
    """Apply a bounded workspace text replacement and return a receipt with rollback hashes.

    Args:
        file_path: Relative path to the file within the workspace.
        old_text: Exact text to replace.
        new_text: Replacement text.
        expected_occurrences: Required number of old_text occurrences before the write is valid.
        expected_before_sha256: Optional SHA-256 hash guard for the current file content.

    Returns:
        A JSON receipt containing the diff, hashes, and application status.
    """
    try:
        _assert_not_secret_like_path(file_path, "apply_patch")
        resolved = _safe_resolve(file_path)
        before = resolved.read_text(encoding="utf-8")
        before_sha256 = _sha256_text(before)
        if expected_before_sha256 and expected_before_sha256 != before_sha256:
            raise ValueError("Current file content does not match expected_before_sha256")
        after, occurrence_count = _replace_once(before, old_text, new_text, expected_occurrences)
        diff = _replacement_diff(file_path, before, after)
        resolved.write_text(after, encoding="utf-8")
        log_integration_event_sync(
            integration_type="filesystem",
            name="workspace",
            outcome="succeeded",
            details=_filesystem_details(
                file_path,
                "apply_patch",
                occurrence_count=occurrence_count,
                changed_lines=sum(1 for line in diff.splitlines() if line.startswith(("+", "-"))),
                before_sha256=before_sha256,
                after_sha256=_sha256_text(after),
                before_hash_guarded=bool(expected_before_sha256),
            ),
        )
        return _patch_receipt(
            file_path=file_path,
            operation="apply_patch",
            before=before,
            after=after,
            diff=diff,
            occurrence_count=occurrence_count,
            applied=True,
            before_hash_guarded=bool(expected_before_sha256),
        )
    except ValueError as exc:
        log_integration_event_sync(
            integration_type="filesystem",
            name="workspace",
            outcome="blocked",
            details=_filesystem_details(file_path, "apply_patch", error=str(exc)),
        )
        raise
    except Exception as exc:
        log_integration_event_sync(
            integration_type="filesystem",
            name="workspace",
            outcome="failed",
            details=_filesystem_details(file_path, "apply_patch", error=str(exc)),
        )
        logger.exception("Failed to apply workspace patch")
        return f"Error: Failed to apply workspace patch: {exc}"
