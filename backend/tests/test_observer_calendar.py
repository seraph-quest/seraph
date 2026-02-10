from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from src.observer.sources.calendar_source import gather_calendar


class TestCalendarSource:
    @pytest.mark.asyncio
    async def test_no_credentials_returns_empty(self):
        mock_path = MagicMock()
        mock_path.exists.return_value = False
        with patch("src.observer.sources.calendar_source.Path", return_value=mock_path), \
             patch("src.observer.sources.calendar_source.settings") as mock_s:
            mock_s.google_credentials_path = "/fake/creds.json"
            result = await gather_calendar()

        assert result["upcoming_events"] == []
        assert result["current_event"] is None

    @pytest.mark.asyncio
    async def test_with_events(self):
        from datetime import datetime

        mock_event = MagicMock()
        mock_event.summary = "Team Standup"
        mock_event.start = datetime(2025, 6, 2, 10, 0)
        mock_event.end = datetime(2025, 6, 2, 10, 30)
        mock_event.description = "Daily standup meeting"

        mock_calendar = MagicMock()
        mock_calendar.get_events.return_value = [mock_event]

        mock_path = MagicMock()
        mock_path.exists.return_value = True

        with patch("src.observer.sources.calendar_source.Path", return_value=mock_path), \
             patch("src.observer.sources.calendar_source.settings") as mock_s, \
             patch("src.observer.sources.calendar_source._fetch_events") as mock_fetch:
            mock_s.google_credentials_path = "/fake/creds.json"
            mock_fetch.return_value = {
                "upcoming_events": [
                    {"summary": "Team Standup", "start": "2025-06-02 10:00:00", "end": "2025-06-02 10:30:00"}
                ],
                "current_event": None,
            }
            result = await gather_calendar()

        assert len(result["upcoming_events"]) == 1
        assert result["upcoming_events"][0]["summary"] == "Team Standup"

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self):
        mock_path = MagicMock()
        mock_path.exists.return_value = True

        with patch("src.observer.sources.calendar_source.Path", return_value=mock_path), \
             patch("src.observer.sources.calendar_source.settings") as mock_s, \
             patch("src.observer.sources.calendar_source._fetch_events", side_effect=RuntimeError("fail")):
            mock_s.google_credentials_path = "/fake/creds.json"
            result = await gather_calendar()

        assert result["upcoming_events"] == []
        assert result["current_event"] is None
