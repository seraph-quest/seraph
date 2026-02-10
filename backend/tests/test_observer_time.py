from datetime import datetime
from unittest.mock import patch, MagicMock

from src.observer.sources.time_source import gather_time


def _mock_time(year=2025, month=6, day=2, hour=10, minute=0):
    """Patch datetime.now inside time_source to return a fixed time."""
    fixed = datetime(year, month, day, hour, minute)
    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now.return_value = fixed
    return patch("src.observer.sources.time_source.datetime", mock_dt)


def _mock_settings(**overrides):
    defaults = {"user_timezone": "UTC", "working_hours_start": 9, "working_hours_end": 17}
    defaults.update(overrides)
    return patch("src.observer.sources.time_source.settings", MagicMock(**defaults))


class TestTimeClassification:
    def test_morning(self):
        with _mock_time(hour=8), _mock_settings():
            assert gather_time()["time_of_day"] == "morning"

    def test_morning_boundary(self):
        with _mock_time(hour=5), _mock_settings():
            assert gather_time()["time_of_day"] == "morning"

    def test_afternoon(self):
        with _mock_time(hour=14), _mock_settings():
            assert gather_time()["time_of_day"] == "afternoon"

    def test_evening(self):
        with _mock_time(hour=19), _mock_settings():
            assert gather_time()["time_of_day"] == "evening"

    def test_night(self):
        with _mock_time(hour=23), _mock_settings():
            assert gather_time()["time_of_day"] == "night"

    def test_night_early_hours(self):
        with _mock_time(hour=3), _mock_settings():
            assert gather_time()["time_of_day"] == "night"


class TestWorkingHours:
    def test_weekday_during_hours(self):
        # Monday 10:00
        with _mock_time(day=2, hour=10), _mock_settings():
            assert gather_time()["is_working_hours"] is True

    def test_weekday_before_hours(self):
        # Monday 7:00
        with _mock_time(day=2, hour=7), _mock_settings():
            assert gather_time()["is_working_hours"] is False

    def test_weekday_after_hours(self):
        # Monday 20:00
        with _mock_time(day=2, hour=20), _mock_settings():
            assert gather_time()["is_working_hours"] is False

    def test_weekend(self):
        # Saturday 10:00
        with _mock_time(day=7, hour=10), _mock_settings():
            assert gather_time()["is_working_hours"] is False

    def test_custom_hours(self):
        # Monday 7:00 with early start
        with _mock_time(day=2, hour=7), _mock_settings(working_hours_start=6):
            assert gather_time()["is_working_hours"] is True


class TestDayOfWeek:
    def test_returns_day_name(self):
        # June 4, 2025 = Wednesday
        with _mock_time(day=4, hour=10), _mock_settings():
            assert gather_time()["day_of_week"] == "Wednesday"
