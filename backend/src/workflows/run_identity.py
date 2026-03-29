"""Helpers for stable workflow run identity encoding."""

from __future__ import annotations


def build_workflow_run_identity(
    session_id: str | None,
    tool_name: str,
    run_fingerprint: str,
    *,
    run_discriminator: str | None = None,
) -> str:
    base = f"{session_id or 'global'}:{tool_name}:{run_fingerprint or 'none'}"
    if isinstance(run_discriminator, str) and run_discriminator.strip():
        return f"{base}:{run_discriminator.strip()}"
    return base


def parse_workflow_run_identity(
    run_identity: str,
) -> tuple[str | None, str, str, str | None]:
    parts = run_identity.rsplit(":", 3)
    if len(parts) == 4:
        session_key, tool_name, run_fingerprint, run_discriminator = parts
    elif len(parts) == 3:
        session_key, tool_name, run_fingerprint = parts
        run_discriminator = None
    else:
        raise ValueError(run_identity)
    return (
        None if session_key == "global" else session_key,
        tool_name,
        run_fingerprint,
        run_discriminator or None,
    )
