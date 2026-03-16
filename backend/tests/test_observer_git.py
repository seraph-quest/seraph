import time
import asyncio
from unittest.mock import patch, MagicMock

from src.audit.repository import audit_repository
from src.observer.sources.git_source import gather_git


def _reflog_line(message: str, seconds_ago: int = 0) -> str:
    ts = int(time.time()) - seconds_ago
    return f"abc1234 def5678 User Name <user@example.com> {ts} +0000\t{message}"


class TestGitSource:
    def test_no_git_dir_returns_none(self, tmp_path, async_db):
        with patch("src.observer.sources.git_source.settings") as mock_s:
            mock_s.observer_git_repo_path = str(tmp_path)
            mock_s.workspace_dir = str(tmp_path)
            result = gather_git()

        assert result is None
        async def _fetch():
            events = await audit_repository.list_events(limit=5)
            return [e for e in events if e["event_type"] == "integration_unavailable"]
        events = asyncio.run(_fetch())
        assert any(
            event["tool_name"] == "observer_source:git"
            and event["details"]["reason"] == "missing_git_dir"
            for event in events
        )

    def test_recent_reflog_entries(self, tmp_path, async_db):
        git_dir = tmp_path / ".git" / "logs"
        git_dir.mkdir(parents=True)
        reflog = git_dir / "HEAD"
        reflog.write_text(
            _reflog_line("commit: fix bug", seconds_ago=300) + "\n"
            + _reflog_line("commit: add feature", seconds_ago=60) + "\n"
        )

        with patch("src.observer.sources.git_source.settings") as mock_s:
            mock_s.observer_git_repo_path = str(tmp_path)
            mock_s.workspace_dir = str(tmp_path)
            result = gather_git()

        assert result is not None
        assert result["recent_git_activity"] is not None
        assert len(result["recent_git_activity"]) == 2
        # Most recent first
        assert "add feature" in result["recent_git_activity"][0]["message"]
        async def _fetch():
            events = await audit_repository.list_events(limit=5)
            return [e for e in events if e["event_type"] == "integration_succeeded"]
        events = asyncio.run(_fetch())
        assert any(
            event["tool_name"] == "observer_source:git"
            and event["details"]["recent_activity_count"] == 2
            for event in events
        )

    def test_old_entries_ignored(self, tmp_path, async_db):
        git_dir = tmp_path / ".git" / "logs"
        git_dir.mkdir(parents=True)
        reflog = git_dir / "HEAD"
        # Entry from 2 hours ago
        reflog.write_text(_reflog_line("commit: old work", seconds_ago=7200) + "\n")

        with patch("src.observer.sources.git_source.settings") as mock_s:
            mock_s.observer_git_repo_path = str(tmp_path)
            mock_s.workspace_dir = str(tmp_path)
            result = gather_git()

        assert result is not None
        assert result["recent_git_activity"] is None
        async def _fetch():
            events = await audit_repository.list_events(limit=5)
            return [e for e in events if e["event_type"] == "integration_empty_result"]
        events = asyncio.run(_fetch())
        assert any(
            event["tool_name"] == "observer_source:git"
            and event["details"]["recent_activity_count"] == 0
            for event in events
        )

    def test_max_three_entries(self, tmp_path):
        git_dir = tmp_path / ".git" / "logs"
        git_dir.mkdir(parents=True)
        reflog = git_dir / "HEAD"
        lines = [_reflog_line(f"commit: change {i}", seconds_ago=i * 60) for i in range(5)]
        reflog.write_text("\n".join(lines) + "\n")

        with patch("src.observer.sources.git_source.settings") as mock_s:
            mock_s.observer_git_repo_path = str(tmp_path)
            mock_s.workspace_dir = str(tmp_path)
            result = gather_git()

        assert result is not None
        assert len(result["recent_git_activity"]) == 3

    def test_falls_back_to_workspace_dir(self, tmp_path):
        git_dir = tmp_path / ".git" / "logs"
        git_dir.mkdir(parents=True)
        (git_dir / "HEAD").write_text(_reflog_line("commit: test", seconds_ago=30) + "\n")

        with patch("src.observer.sources.git_source.settings") as mock_s:
            mock_s.observer_git_repo_path = ""
            mock_s.workspace_dir = str(tmp_path)
            result = gather_git()

        assert result is not None
        assert result["recent_git_activity"] is not None

    def test_no_reflog_file_returns_none(self, tmp_path, async_db):
        (tmp_path / ".git").mkdir()

        with patch("src.observer.sources.git_source.settings") as mock_s:
            mock_s.observer_git_repo_path = str(tmp_path)
            mock_s.workspace_dir = str(tmp_path)
            result = gather_git()

        assert result is None
        async def _fetch():
            events = await audit_repository.list_events(limit=5)
            return [e for e in events if e["event_type"] == "integration_unavailable"]
        events = asyncio.run(_fetch())
        assert any(
            event["tool_name"] == "observer_source:git"
            and event["details"]["reason"] == "missing_reflog"
            for event in events
        )

    def test_read_error_logs_failure(self, tmp_path, async_db):
        git_dir = tmp_path / ".git" / "logs"
        git_dir.mkdir(parents=True)
        reflog = git_dir / "HEAD"
        reflog.write_text(_reflog_line("commit: test", seconds_ago=30) + "\n")

        with patch("src.observer.sources.git_source.settings") as mock_s, \
             patch("pathlib.Path.read_text", side_effect=OSError("nope")):
            mock_s.observer_git_repo_path = str(tmp_path)
            mock_s.workspace_dir = str(tmp_path)
            result = gather_git()

        assert result is None
        async def _fetch():
            events = await audit_repository.list_events(limit=5)
            return [e for e in events if e["event_type"] == "integration_failed"]
        events = asyncio.run(_fetch())
        assert any(
            event["tool_name"] == "observer_source:git"
            and event["details"]["error"] == "nope"
            for event in events
        )
