"""Tests for the clarify runtime tool."""

import pytest

from src.agent.exceptions import ClarificationRequired
from src.tools.clarify_tool import clarify


class TestClarifyTool:
    def test_clarify_raises_structured_interrupt(self):
        with pytest.raises(ClarificationRequired) as exc_info:
            clarify(
                question="Which city should I check?",
                reason="Weather depends on location.",
                options="Wroclaw, Warsaw",
            )

        exc = exc_info.value
        assert exc.question == "Which city should I check?"
        assert exc.reason == "Weather depends on location."
        assert exc.options == ["Wroclaw", "Warsaw"]
        assert "Which city should I check?" in exc.render_message()
        assert "Why I need this: Weather depends on location." in exc.render_message()
