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
    ) -> None:
        super().__init__(summary)
        self.approval_id = approval_id
        self.session_id = session_id
        self.tool_name = tool_name
        self.risk_level = risk_level
        self.summary = summary
