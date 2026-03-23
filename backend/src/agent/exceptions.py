"""Agent/runtime exceptions that intentionally interrupt execution."""


class ClarificationRequired(Exception):
    """Raised when the agent needs a missing input before it can continue."""

    def __init__(
        self,
        *,
        question: str,
        reason: str = "",
        options: list[str] | None = None,
    ) -> None:
        self.question = question.strip()
        self.reason = reason.strip()
        self.options = [option.strip() for option in (options or []) if option and option.strip()]
        super().__init__(self.render_message())

    def render_message(self) -> str:
        message = self.question
        if self.reason:
            message += f"\n\nWhy I need this: {self.reason}"
        if self.options:
            joined = "\n".join(f"- {option}" for option in self.options)
            message += f"\n\nYou can answer with one of these or give your own:\n{joined}"
        return message
