from unittest.mock import patch, MagicMock

import pytest

from src.observer.sources.calendar_source import gather_calendar


class TestCalendarSource:
    @pytest.mark.asyncio
    async def test_no_credentials_returns_empty(self):
        mock_path = MagicMock()
        mock_path.exists.return_value = False
        with patch("src.observer.sources.calendar_source._CREDENTIALS_PATH", mock_path):
            result = await gather_calendar()

        assert result["upcoming_events"] == []
        assert result["current_event"] is None

    @pytest.mark.asyncio
    async def test_with_events(self):
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

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self):
        mock_path = MagicMock()
        mock_path.exists.return_value = True

        with patch("src.observer.sources.calendar_source._CREDENTIALS_PATH", mock_path), \
             patch("src.observer.sources.calendar_source._fetch_events", side_effect=RuntimeError("fail")):
            result = await gather_calendar()

        assert result["upcoming_events"] == []
        assert result["current_event"] is None
