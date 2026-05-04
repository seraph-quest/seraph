"""Metadata registry for bundled native tools.

Native tools have static entries here. Workflow metadata remains dynamic and is
resolved through the workflow manager when requested.
"""

TOOL_NAME_ALIASES: dict[str, str] = {
    "shell_execute": "execute_code",
}


_EXECUTION_PROFILES: dict[str, dict] = {
    "workspace_read": {
        "operation_modes": ["inspect"],
        "session_model": "stateless_workspace_call",
        "persistence": "audit_receipt_only",
        "recovery_actions": ["retry_after_path_correction"],
        "artifact_contract": {"outputs": ["file_text"], "receipt_required": True},
        "provider_health": {"provider": "local_workspace", "state": "ready"},
        "interactive_controls": ["inspect"],
    },
    "workspace_write": {
        "operation_modes": ["mutate"],
        "session_model": "stateless_workspace_call",
        "persistence": "workspace_file_plus_audit_receipt",
        "recovery_actions": ["restore_from_receipt_hash", "retry_after_path_correction"],
        "artifact_contract": {"outputs": ["file_write_receipt"], "receipt_required": True},
        "provider_health": {"provider": "local_workspace", "state": "ready"},
        "interactive_controls": ["inspect", "approve"],
    },
    "workspace_patch": {
        "operation_modes": ["preview", "mutate"],
        "session_model": "stateless_workspace_call",
        "persistence": "workspace_file_plus_diff_receipt",
        "recovery_actions": ["preview", "apply", "guarded_reapply", "retry_after_occurrence_mismatch"],
        "artifact_contract": {
            "outputs": ["unified_diff", "before_after_hashes", "rollback_hint"],
            "receipt_required": True,
        },
        "provider_health": {"provider": "local_workspace", "state": "ready"},
        "interactive_controls": ["inspect", "approve", "repair", "compare"],
    },
    "sandbox": {
        "operation_modes": ["execute", "inspect"],
        "session_model": "ephemeral_sandbox_call",
        "persistence": "stdout_stderr_exit_receipt",
        "recovery_actions": ["retry_with_bounded_input", "inspect_error"],
        "artifact_contract": {"outputs": ["stdout", "stderr", "exit_code"], "receipt_required": True},
        "provider_health": {"provider": "snekbox", "state": "configured_by_runtime"},
        "interactive_controls": ["inspect", "approve"],
    },
    "process": {
        "operation_modes": ["execute", "stream", "background"],
        "session_model": "session_scoped_process_group",
        "persistence": "process_handle_plus_stream_receipts",
        "recovery_actions": ["read_output", "stop", "retry", "repair_command"],
        "artifact_contract": {"outputs": ["stdout", "stderr", "exit_code", "process_id"], "receipt_required": True},
        "provider_health": {"provider": "managed_process_runtime", "state": "policy_gated"},
        "interactive_controls": ["inspect", "approve", "pause", "stop", "retry", "repair"],
    },
    "browser": {
        "operation_modes": ["navigate", "extract", "snapshot"],
        "session_model": "session_scoped_browser_provider",
        "persistence": "audit_receipt_and_optional_snapshot",
        "recovery_actions": ["retry_navigation", "inspect_snapshot", "repair_selector"],
        "artifact_contract": {"outputs": ["html_text", "screenshot_ref", "browser_receipt"], "receipt_required": True},
        "provider_health": {"provider": "playwright_or_configured_browser_provider", "state": "policy_gated"},
        "interactive_controls": ["inspect", "approve", "pause", "retry"],
    },
    "external_http": {
        "operation_modes": ["fetch"],
        "session_model": "stateless_external_request",
        "persistence": "response_receipt",
        "recovery_actions": ["retry", "inspect_redirect_block"],
        "artifact_contract": {"outputs": ["status", "headers", "truncated_body"], "receipt_required": True},
        "provider_health": {"provider": "httpx_connector", "state": "ssrf_guarded"},
        "interactive_controls": ["inspect", "approve"],
    },
}


def canonical_tool_name(tool_name: str) -> str:
    """Return the canonical runtime tool name for a tool or legacy alias."""
    return TOOL_NAME_ALIASES.get(tool_name, tool_name)


TOOL_METADATA: dict[str, dict] = {
    # Phase 1 tools
    "web_search": {
        "description": "Search the web for information",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["external_read"],
    },
    "read_file": {
        "description": "Read a file from the workspace",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["workspace_read"],
        "execution": _EXECUTION_PROFILES["workspace_read"],
    },
    "write_file": {
        "description": "Write content to a file",
        "policy_modes": ["balanced", "full"],
        "execution_boundaries": ["workspace_write"],
        "execution": _EXECUTION_PROFILES["workspace_write"],
    },
    "preview_workspace_patch": {
        "description": "Preview a bounded workspace text replacement with diff and rollback hashes",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["workspace_read"],
        "execution": _EXECUTION_PROFILES["workspace_patch"],
    },
    "apply_workspace_patch": {
        "description": "Apply a bounded workspace text replacement with diff receipt and rollback hashes",
        "policy_modes": ["balanced", "full"],
        "execution_boundaries": ["workspace_read", "workspace_write"],
        "execution": _EXECUTION_PROFILES["workspace_patch"],
    },
    "fill_template": {
        "description": "Fill a text template with values",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["local_compute"],
    },
    "view_soul": {
        "description": "View the soul file",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["guardian_state_read"],
    },
    "update_soul": {
        "description": "Update a section of the soul file",
        "policy_modes": ["balanced", "full"],
        "execution_boundaries": ["guardian_state_write"],
    },
    "create_goal": {
        "description": "Create a new goal",
        "policy_modes": ["balanced", "full"],
        "execution_boundaries": ["guardian_state_write"],
    },
    "update_goal": {
        "description": "Update an existing goal",
        "policy_modes": ["balanced", "full"],
        "execution_boundaries": ["guardian_state_write"],
    },
    "get_goals": {
        "description": "List goals",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["guardian_state_read"],
    },
    "get_goal_progress": {
        "description": "Get goal progress dashboard",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["guardian_state_read"],
    },
    # Phase 2 tools
    "execute_code": {
        "description": "Execute bounded code in a sandboxed environment",
        "policy_modes": ["full"],
        "execution_boundaries": ["sandbox_execution"],
        "execution": _EXECUTION_PROFILES["sandbox"],
    },
    "delegate_task": {
        "description": "Delegate a bounded subtask to a specialist runtime",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["delegation"],
    },
    "clarify": {
        "description": "Request a missing input from the user before continuing",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["conversation"],
    },
    "todo": {
        "description": "Manage the current session's persisted task list",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["conversation_state"],
    },
    "session_search": {
        "description": "Search prior session history for bounded relevant snippets",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["conversation_history_read"],
    },
    "get_scheduled_jobs": {
        "description": "List persisted scheduled jobs and their runtime status",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["automation_state"],
    },
    "manage_scheduled_job": {
        "description": "Create, update, pause, resume, or delete persisted scheduled jobs",
        "policy_modes": ["balanced", "full"],
        "execution_boundaries": ["automation_state"],
    },
    "run_command": {
        "description": "Run an approved workspace-scoped command inside the runtime container",
        "policy_modes": ["full"],
        "execution_boundaries": ["container_process_execution", "workspace_scoped_paths"],
        "execution": _EXECUTION_PROFILES["process"],
    },
    "start_process": {
        "description": "Start an approved workspace-scoped background process inside the runtime container",
        "policy_modes": ["full"],
        "execution_boundaries": ["container_process_management", "background_execution", "session_process_partition"],
        "approval_behavior": "always",
        "execution": _EXECUTION_PROFILES["process"],
    },
    "list_processes": {
        "description": "List background processes started through the runtime process manager",
        "policy_modes": ["full"],
        "execution_boundaries": ["container_process_read", "session_process_partition"],
        "execution": _EXECUTION_PROFILES["process"],
    },
    "read_process_output": {
        "description": "Read recent stdout/stderr output for a managed background process",
        "policy_modes": ["full"],
        "execution_boundaries": ["container_process_read", "session_process_partition"],
        "execution": _EXECUTION_PROFILES["process"],
    },
    "stop_process": {
        "description": "Stop a managed background process inside the runtime container",
        "policy_modes": ["full"],
        "execution_boundaries": ["container_process_management", "session_process_partition"],
        "execution": _EXECUTION_PROFILES["process"],
    },
    "browse_webpage": {
        "description": "Browse and extract content from a webpage",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["external_read"],
        "execution": _EXECUTION_PROFILES["browser"],
    },
    "browser_session": {
        "description": "Manage structured browser sessions, page refs, and snapshots for the current session",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["external_read"],
        "execution": _EXECUTION_PROFILES["browser"],
    },
    "http_request": {
        "description": "Fetch an HTTP resource through the packaged request connector",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["external_read"],
        "execution": _EXECUTION_PROFILES["external_http"],
    },
    "source_capabilities": {
        "description": "Inspect the provider-neutral source access surfaces available to the current runtime",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["external_read"],
    },
    "collect_source_evidence": {
        "description": "Collect normalized evidence through provider-neutral source contracts",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["external_read"],
    },
    "plan_source_review": {
        "description": "Plan a provider-neutral source review routine from the currently available adapters",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["external_read"],
    },
    "plan_source_mutation": {
        "description": "Plan a connector-backed typed mutation path with explicit approval and audit scope",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["external_read"],
    },
    "execute_source_mutation": {
        "description": "Execute a bounded connector-backed typed mutation with explicit approval and audit scope",
        "policy_modes": ["full"],
        "execution_boundaries": ["external_mcp", "authenticated_external_source", "connector_mutation"],
    },
    "plan_source_report": {
        "description": "Plan a provider-neutral source report plus an optional authenticated publication path",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["external_read"],
    },
    "propose_capability_evolution": {
        "description": "Generate an eval-gated review candidate for a declarative skill, runbook, starter pack, or prompt pack",
        "policy_modes": ["balanced", "full"],
        "execution_boundaries": ["workspace_write", "local_compute"],
    },
    # Vault tools
    "store_secret": {
        "description": "Store an encrypted secret in the vault",
        "policy_modes": ["balanced", "full"],
        "execution_boundaries": ["secret_management"],
    },
    "get_secret": {
        "description": "Retrieve a secret from the vault",
        "policy_modes": ["full"],
        "execution_boundaries": ["secret_read"],
    },
    "get_secret_ref": {
        "description": "Create an opaque session-scoped reference for a secret",
        "policy_modes": ["full"],
        "execution_boundaries": ["secret_injection"],
    },
    "list_secrets": {
        "description": "List secret keys stored in the vault",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["secret_management"],
    },
    "delete_secret": {
        "description": "Delete a secret from the vault",
        "policy_modes": ["full"],
        "execution_boundaries": ["secret_management"],
    },
}


def get_tool_metadata(tool_name: str) -> dict | None:
    """Get metadata for a bundled native tool or a derived workflow tool."""
    metadata = TOOL_METADATA.get(canonical_tool_name(tool_name))
    if metadata is not None:
        return metadata
    try:
        from src.workflows.manager import workflow_manager

        return workflow_manager.get_tool_metadata(tool_name)
    except Exception:
        return None


def get_all_metadata() -> dict[str, dict]:
    """Return a copy of all bundled native tool metadata."""
    return TOOL_METADATA.copy()
