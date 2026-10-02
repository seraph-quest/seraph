class ApprovalRequired(Exception):
    """Raised when a high-risk tool call needs explicit user approval."""

    def __init__(
        self,
        *,
        approval_id: str,
        session_id: str | None,
        tool_name: str,
        risk_level: str,
        summary: str,
        required_permissions: list[str] | None = None,
        local_host_execution_required: bool | None = None,
        executor_kind: str | None = None,
        executor_profile: str | None = None,
        executor_posture_digest: str | None = None,
        preparation_ready: bool | None = None,
        execution_ready: bool | None = None,
        operator_visible: bool | None = None,
        expires_at: float | None = None,
    ) -> None:
        super().__init__(summary)
        self.approval_id = approval_id
        self.session_id = session_id
        self.tool_name = tool_name
        self.risk_level = risk_level
        self.summary = summary
        # These are optional, bounded, server-owned display fields.  Generic
        # approvals leave them absent; the transport layer never infers them
        # from ``summary`` or private tool arguments.
        self.required_permissions = required_permissions
        self.local_host_execution_required = local_host_execution_required
        self.executor_kind = executor_kind
        self.executor_profile = executor_profile
        self.executor_posture_digest = executor_posture_digest
        self.preparation_ready = preparation_ready
        self.execution_ready = execution_ready
        self.operator_visible = operator_visible
        self.expires_at = expires_at
