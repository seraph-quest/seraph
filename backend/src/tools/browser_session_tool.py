"""Structured browser-session runtime tool."""

from __future__ import annotations

from smolagents import tool

from config.settings import settings
from src.approval.runtime import get_current_session_id
from src.browser.sessions import browser_session_runtime
from src.extensions.browser_providers import select_active_browser_provider
from src.extensions.registry import ExtensionRegistry, default_manifest_roots_for_workspace
from src.extensions.state import connector_enabled_overrides, load_extension_state_payload
from src.tools.browser_tool import browse_webpage


def _resolve_browser_provider(requested_name: str = "") -> tuple[dict[str, str], str | None]:
    requested = requested_name.strip()
    state_payload = load_extension_state_payload()
    state_by_id = state_payload.get("extensions")
    snapshot = ExtensionRegistry(
        manifest_roots=default_manifest_roots_for_workspace(settings.workspace_dir),
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    ).snapshot()
    provider = select_active_browser_provider(
        snapshot.list_contributions("browser_providers"),
        state_by_id=state_by_id if isinstance(state_by_id, dict) else None,
        enabled_overrides=connector_enabled_overrides(state_by_id if isinstance(state_by_id, dict) else None),
        requested_name=requested or None,
    )
    if provider is None:
        if requested:
            return {}, f"Error: Browser provider '{requested}' is not enabled and configured."
        return {
            "provider_name": "local-browser",
            "provider_kind": "local",
            "execution_mode": "local_runtime",
        }, None
    execution_mode = "remote_provider" if provider.provider_kind != "local" else "local_runtime"
    if provider.provider_kind != "local":
        execution_mode = "local_fallback"
    return {
        "provider_name": provider.name,
        "provider_kind": provider.provider_kind,
        "execution_mode": execution_mode,
    }, None


def _browser_capture_failed(content: str) -> bool:
    return str(content or "").startswith("Error:")


def _require_owner_session_id() -> str | None:
    session_id = get_current_session_id()
    if not session_id:
        return None
    return session_id


def _render_session_list(owner_session_id: str) -> str:
    sessions = browser_session_runtime.list_sessions(owner_session_id=owner_session_id)
    if not sessions:
        return "No browser sessions are open."
    lines = []
    for item in sessions:
        lines.append(
            f"- {item['session_id']} · {item['provider_name']} ({item['provider_kind']}) · "
            f"{item['url']} · {item['snapshot_count']} snapshots · mode={item['execution_mode']}"
        )
    return "\n".join(lines)


@tool
def browser_session(
    action: str = "list",
    url: str = "",
    session_id: str = "",
    ref: str = "",
    provider: str = "",
    capture: str = "extract",
) -> str:
    """Manage structured browser sessions and page references.

    Args:
        action: One of open, list, read, snapshot, or close.
        url: URL for open.
        session_id: Session id for read, snapshot, or close.
        ref: Snapshot reference for read.
        provider: Optional browser provider name.
        capture: extract, html, or screenshot.
    """
    normalized_action = action.strip().lower()
    normalized_capture = capture.strip().lower()
    if normalized_capture not in {"extract", "html", "screenshot"}:
        return "Error: capture must be 'extract', 'html', or 'screenshot'."

    owner_session_id = _require_owner_session_id()
    if owner_session_id is None:
        return "Error: browser_session requires an active session."

    if normalized_action == "list":
        return _render_session_list(owner_session_id)

    provider_info, provider_error = _resolve_browser_provider(provider)
    if provider_error:
        return provider_error

    if normalized_action == "open":
        if not url.strip():
            return "Error: browser_session open requires a URL."
        content = browse_webpage(url.strip(), action=normalized_capture)
        if _browser_capture_failed(content):
            return content
        payload = browser_session_runtime.open_session(
            owner_session_id=owner_session_id,
            url=url.strip(),
            provider_name=provider_info["provider_name"],
            provider_kind=provider_info["provider_kind"],
            execution_mode=provider_info["execution_mode"],
            capture=normalized_capture,
            content=content,
        )
        fallback_note = (
            f" Remote provider '{provider_info['provider_name']}' is staged; this session used the local browser runtime as the current execution mode."
            if provider_info["execution_mode"] == "local_fallback"
            else ""
        )
        return (
            f"Opened browser session {payload['session_id']} using {provider_info['provider_name']} "
            f"({provider_info['provider_kind']}). Latest ref: {payload['latest_ref']}.{fallback_note}\n\n"
            f"{payload['content']}"
        )

    if normalized_action == "snapshot":
        if not session_id.strip():
            return "Error: browser_session snapshot requires a session_id."
        session = browser_session_runtime.get_session(session_id.strip(), owner_session_id=owner_session_id)
        if session is None:
            return f"Error: Browser session '{session_id}' was not found."
        content = browse_webpage(str(session["url"]), action=normalized_capture)
        if _browser_capture_failed(content):
            return content
        payload = browser_session_runtime.snapshot_session(
            owner_session_id=owner_session_id,
            session_id=session_id.strip(),
            capture=normalized_capture,
            content=content,
        )
        assert payload is not None
        return f"Captured snapshot {payload['latest_ref']} for session {session_id}.\n\n{payload['content']}"

    if normalized_action == "read":
        if ref.strip():
            payload = browser_session_runtime.read_ref(ref.strip(), owner_session_id=owner_session_id)
            if payload is None:
                return f"Error: Browser ref '{ref}' was not found."
            return (
                f"{payload['ref']} · {payload['provider_name']} ({payload['provider_kind']}) · "
                f"mode={payload['execution_mode']} · "
                f"{payload['url']}\n\n{payload['content']}"
            )
        if session_id.strip():
            payload = browser_session_runtime.get_session(session_id.strip(), owner_session_id=owner_session_id)
            if payload is None:
                return f"Error: Browser session '{session_id}' was not found."
            snapshots = payload.get("snapshots") or []
            latest_ref = snapshots[-1]["ref"] if snapshots else ""
            if latest_ref:
                ref_payload = browser_session_runtime.read_ref(str(latest_ref), owner_session_id=owner_session_id)
                if ref_payload is not None:
                    return (
                        f"{ref_payload['ref']} · {ref_payload['provider_name']} ({ref_payload['provider_kind']}) · "
                        f"mode={ref_payload['execution_mode']} · "
                        f"{ref_payload['url']}\n\n{ref_payload['content']}"
                    )
            return f"Browser session '{session_id}' has no captured snapshots yet."
        return "Error: browser_session read requires a session_id or ref."

    if normalized_action == "close":
        if not session_id.strip():
            return "Error: browser_session close requires a session_id."
        payload = browser_session_runtime.close_session(session_id.strip(), owner_session_id=owner_session_id)
        if payload is None:
            return f"Error: Browser session '{session_id}' was not found."
        return f"Closed browser session {session_id}."

    return "Error: Unsupported browser_session action. Use open, list, read, snapshot, or close."
