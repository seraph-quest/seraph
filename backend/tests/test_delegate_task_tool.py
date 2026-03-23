"""Tests for the delegate_task runtime tool."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.tools.delegate_task_tool import delegate_task


def _specialist(name: str, output: str):
    return SimpleNamespace(name=name, run=MagicMock(return_value=output))


class TestDelegateTask:
    @patch("src.agent.specialists.build_all_specialists")
    def test_delegate_task_routes_to_named_specialist(self, mock_build_all_specialists):
        file_worker = _specialist("file_worker", "patched file")
        mock_build_all_specialists.return_value = [
            _specialist("web_researcher", "researched"),
            file_worker,
        ]

        with patch("src.tools.delegate_task_tool.settings.use_delegation", True):
            result = delegate_task("Patch the workspace file.", specialist="files")

        assert result == "patched file"
        file_worker.run.assert_called_once_with("Patch the workspace file.", stream=False, reset=True)

    @patch("src.agent.specialists.build_all_specialists")
    def test_delegate_task_infers_specialist_from_task_keywords(self, mock_build_all_specialists):
        goal_planner = _specialist("goal_planner", "priorities updated")
        mock_build_all_specialists.return_value = [
            goal_planner,
            _specialist("web_researcher", "researched"),
        ]

        with patch("src.tools.delegate_task_tool.settings.use_delegation", True):
            result = delegate_task("Review my priorities and update the plan.")

        assert result == "priorities updated"
        goal_planner.run.assert_called_once_with("Review my priorities and update the plan.", stream=False, reset=True)

    @patch("src.agent.specialists.build_all_specialists")
    def test_delegate_task_errors_when_specialist_is_unknown(self, mock_build_all_specialists):
        mock_build_all_specialists.return_value = [
            _specialist("memory_keeper", "ok"),
            _specialist("web_researcher", "ok"),
        ]

        with patch("src.tools.delegate_task_tool.settings.use_delegation", True):
            result = delegate_task("Do the thing.", specialist="unknown")

        assert result.startswith("Error: Unknown specialist 'unknown'")
        assert "memory_keeper" in result
        assert "web_researcher" in result

    @patch("src.agent.specialists.build_all_specialists")
    def test_delegate_task_errors_when_it_cannot_infer_specialist(self, mock_build_all_specialists):
        mock_build_all_specialists.return_value = [
            _specialist("memory_keeper", "ok"),
            _specialist("web_researcher", "ok"),
        ]

        with patch("src.tools.delegate_task_tool.settings.use_delegation", True):
            result = delegate_task("Handle this.")

        assert result.startswith("Error: Unable to infer a specialist")
        assert "memory_keeper" in result
        assert "web_researcher" in result

    def test_delegate_task_errors_when_delegation_is_disabled(self):
        with patch("src.tools.delegate_task_tool.settings.use_delegation", False):
            result = delegate_task("Patch the workspace file.", specialist="files")

        assert result == "Error: Delegation runtime is disabled."

    @patch("src.agent.specialists.build_all_specialists")
    def test_delegate_task_blocks_nested_delegation(self, mock_build_all_specialists):
        nested = _specialist("file_worker", "")

        def _run(*_args, **_kwargs):
            return delegate_task("Nested task", specialist="files")

        nested.run.side_effect = _run
        mock_build_all_specialists.return_value = [nested]

        with patch("src.tools.delegate_task_tool.settings.use_delegation", True):
            result = delegate_task("Top-level task", specialist="files")

        assert result == "Error: Nested delegation is not allowed."
