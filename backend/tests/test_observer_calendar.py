from unittest.mock import patch, MagicMock

import pytest

from src.audit.repository import audit_repository
from src.observer.sources.calendar_source import gather_calendar


class TestCalendarSource:
    @pytest.mark.asyncio
    async def test_no_credentials_returns_empty(self, async_db):
        mock_path = MagicMock()
        mock_path.exists.return_value = False
        with patch("src.observer.sources.calendar_source._CREDENTIALS_PATH", mock_path):
            result = await gather_calendar()

        assert result["upcoming_events"] == []
        assert result["current_event"] is None
        events = await audit_repository.list_events(limit=5)
        assert any(
            event["event_type"] == "integration_unavailable"
            and event["tool_name"] == "observer_source:calendar"
            and event["details"]["reason"] == "missing_credentials"
            for event in events
        )

    @pytest.mark.asyncio
    async def test_with_events(self, async_db):
        mock_path = MagicMock()
        mock_path.exists.return_value = True

        with patch("src.observer.sources.calendar_source._CREDENTIALS_PATH", mock_path), \
             patch("src.observer.sources.calendar_source._fetch_events") as mock_fetch:
            mock_fetch.return_value = {
                "upcoming_events": [
                    {"summary": "Team Standup", "start": "2025-06-02 10:00:00", "end": "2025-06-02 10:30:00"}
                ],
                "current_event": None,
            }
            result = await gather_calendar()

        assert len(result["upcoming_events"]) == 1
        assert result["upcoming_events"][0]["summary"] == "Team Standup"
        events = await audit_repository.list_events(limit=5)
        assert any(
            event["event_type"] == "integration_succeeded"
            and event["tool_name"] == "observer_source:calendar"
            and event["details"]["upcoming_event_count"] == 1
            for event in events
        )

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self, async_db):
        mock_path = MagicMock()
        mock_path.exists.return_value = True

        with patch("src.observer.sources.calendar_source._CREDENTIALS_PATH", mock_path), \
             patch("src.observer.sources.calendar_source._fetch_events", side_effect=RuntimeError("fail")):
            result = await gather_calendar()

        assert result["upcoming_events"] == []
        assert result["current_event"] is None
        events = await audit_repository.list_events(limit=5)
        assert any(
            event["event_type"] == "integration_failed"
            and event["tool_name"] == "observer_source:calendar"
            and event["details"]["error"] == "fail"
            for event in events
        )

    @pytest.mark.asyncio
    async def test_empty_calendar_logs_empty_result(self, async_db):
        mock_path = MagicMock()
        mock_path.exists.return_value = True

        with patch("src.observer.sources.calendar_source._CREDENTIALS_PATH", mock_path), \
             patch("src.observer.sources.calendar_source._fetch_events", return_value={
                 "upcoming_events": [],
                 "current_event": None,
             }):
            result = await gather_calendar()

        assert result["upcoming_events"] == []
        assert result["current_event"] is None
        events = await audit_repository.list_events(limit=5)
        assert any(
            event["event_type"] == "integration_empty_result"
            and event["tool_name"] == "observer_source:calendar"
            and event["details"]["upcoming_event_count"] == 0
            for event in events
        )
