"""Tests for periodic capture loop and fetch_capture_mode helper.

These tests depend on macOS-specific packages (PyObjC, Quartz) via seraph_daemon.
They are automatically skipped on non-macOS platforms.
"""

import asyncio
import json
import platform
import stat
import sys
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin",
    reason="Daemon tests require macOS (PyObjC, Quartz)",
)

httpx = pytest.importorskip("httpx")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from seraph_daemon import (
    ScreenAnalysisRuntime,
    _archive_provider_capture,
    fetch_capture_mode,
    periodic_capture_loop,
)
from capture_archive import ARCHIVE_FILE_RE
from capture_upload import CaptureUploadResult
from ocr.base import AnalysisResult


class _RuntimeSettingsResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _RuntimeSettingsClient:
    def __init__(self, payload):
        self.payload = payload

    async def get(self, _url):
        return _RuntimeSettingsResponse(self.payload)


class _FakeProvider:
    def __init__(self, name="apple-vision", available=True):
        self.name = name
        self.available = available
        self.closed = False

    def is_available(self):
        return self.available

    async def close(self):
        self.closed = True


def test_archive_provider_capture_stores_image_and_analysis_for_any_provider(tmp_path):
    artifacts = _archive_provider_capture(
        archive_dir=str(tmp_path),
        png_bytes=b"png bytes",
        app_name="Preview",
        provider_name="apple-vision",
        analysis={"activity": "reading", "summary": "Reading a PDF"},
    )

    image_path = Path(artifacts["image_path"])
    output_path = Path(artifacts["provider_output_path"])
    analysis_path = Path(artifacts["analysis_path"])
    assert artifacts["provider"] == "apple-vision"
    assert image_path.read_bytes() == b"png bytes"
    provider_payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert provider_payload["provider"] == "apple-vision"
    assert provider_payload["analysis"]["summary"] == "Reading a PDF"
    analysis_payload = json.loads(analysis_path.read_text(encoding="utf-8"))
    assert analysis_payload["activity"] == "reading"
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE(image_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(image_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(analysis_path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_screen_analysis_runtime_refreshes_provider_and_archive_from_settings(tmp_path):
    created_providers = []

    def fake_create_provider(**kwargs):
        provider = _FakeProvider(kwargs["name"])
        created_providers.append((kwargs, provider))
        return provider

    runtime = ScreenAnalysisRuntime(
        enabled=False,
        provider="apple-vision",
        model=None,
        preserve_captures=False,
        archive_dir=str(tmp_path / "fallback"),
        blocklist_file=None,
    )
    first_archive = tmp_path / "first"
    first_client = _RuntimeSettingsClient(
        {
            "enabled": True,
            "provider": "apple-vision",
            "model": "",
            "preserve_captures": True,
            "archive_dir": str(first_archive),
            "min_seconds_between_captures": 30,
            "max_daily_captures": 5,
            "archive_retention_days": 180,
            "archive_max_mb": 512,
        }
    )

    with patch("ocr.create_provider", side_effect=fake_create_provider), patch(
        "blocklist.load_blocklist", return_value={"SecretApp"}
    ):
        await runtime.refresh(first_client, "http://localhost:8004", force=True)

        assert runtime.provider is not None
        assert runtime.provider.name == "apple-vision"
        assert runtime.preserve_captures is True
        assert runtime.archive_dir == str(first_archive)
        assert runtime.min_seconds_between_captures == 30
        assert runtime.max_daily_captures == 5
        assert runtime.archive_retention_days == 180
        assert runtime.archive_max_mb == 512
        assert runtime.blocklist == {"SecretApp"}

        second_archive = tmp_path / "second"
        second_client = _RuntimeSettingsClient(
            {
                "enabled": True,
                "provider": "apple-vision",
                "model": "",
            "preserve_captures": False,
            "archive_dir": str(second_archive),
            "min_seconds_between_captures": 0,
            "max_daily_captures": 0,
            "archive_retention_days": 365,
            "archive_max_mb": 0,
        }
    )
        old_provider = runtime.provider
        await runtime.refresh(second_client, "http://localhost:8004", force=True)

    assert old_provider.closed is True
    assert runtime.provider is not None
    assert runtime.provider.name == "apple-vision"
    assert runtime.preserve_captures is False
    assert runtime.archive_dir == str(second_archive)


def test_screen_analysis_runtime_budget_records_and_skips(tmp_path):
    runtime = ScreenAnalysisRuntime(
        enabled=True,
        provider="apple-vision",
        model=None,
        preserve_captures=True,
        archive_dir=str(tmp_path),
        blocklist_file=None,
    )
    runtime._update_budget_settings(60, 1, 365, 0)

    assert runtime.budget_skip_reason(now=100.0) is None
    runtime.record_capture(now=100.0)
    assert runtime.budget_skip_reason(now=120.0) == "min_seconds_between_captures"
    assert runtime.budget_skip_reason(now=161.0) == "max_daily_captures"
    status = runtime.budget_status()
    assert status["daily_capture_count"] == 1
    assert status["max_daily_captures"] == 1


@pytest.mark.asyncio
async def test_screen_analysis_runtime_pauses_on_unavailable_settings_provider(tmp_path):
    providers = [_FakeProvider("apple-vision"), _FakeProvider("apple-vision", available=False)]

    def fake_create_provider(**_kwargs):
        return providers.pop(0)

    runtime = ScreenAnalysisRuntime(
        enabled=True,
        provider="apple-vision",
        model=None,
        preserve_captures=True,
        archive_dir=str(tmp_path / "fallback"),
        blocklist_file=None,
    )
    first_client = _RuntimeSettingsClient(
        {
            "enabled": True,
            "provider": "apple-vision",
            "model": "",
            "preserve_captures": True,
            "archive_dir": str(tmp_path / "first"),
        }
    )
    second_client = _RuntimeSettingsClient(
        {
            "enabled": True,
            "provider": "apple-vision",
            "model": "",
            "preserve_captures": True,
            "archive_dir": str(tmp_path / "second"),
        }
    )

    with patch("ocr.create_provider", side_effect=fake_create_provider), patch(
        "blocklist.load_blocklist", return_value=set()
    ):
        await runtime.refresh(first_client, "http://localhost:8004", force=True)
        old_provider = runtime.provider

        await runtime.refresh(second_client, "http://localhost:8004", force=True)

    assert old_provider is not None
    assert old_provider.closed is True
    assert runtime.provider is None
    assert runtime.enabled is False
    assert runtime.archive_dir == str(tmp_path / "second")


class TestFetchCaptureMode:
    @pytest.mark.asyncio
    async def test_returns_default_on_error(self):
        """Returns 'on_switch' when backend is unreachable."""
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        result = await fetch_capture_mode(mock_client, "http://localhost:9999")
        assert result == "on_switch"

    @pytest.mark.asyncio
    async def test_returns_default_on_non_200(self):
        """Returns 'on_switch' on non-200 response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        result = await fetch_capture_mode(mock_client, "http://localhost:9999")
        assert result == "on_switch"

    @pytest.mark.asyncio
    async def test_parses_valid_response(self):
        """Parses mode from valid JSON response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"mode": "balanced"}
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        result = await fetch_capture_mode(mock_client, "http://localhost:8004")
        assert result == "balanced"

    @pytest.mark.asyncio
    async def test_parses_detailed_mode(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"mode": "detailed"}
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        result = await fetch_capture_mode(mock_client, "http://localhost:8004")
        assert result == "detailed"


class TestPeriodicCaptureLoop:
    @pytest.mark.asyncio
    async def test_skips_when_on_switch(self):
        """No captures should happen when mode is on_switch."""
        posts = []

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"mode": "on_switch"}
            return resp

        async def mock_post(url, **kwargs):
            posts.append(kwargs.get("json", {}))
            return MagicMock(status_code=200)

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("seraph_daemon.get_frontmost_app_name", return_value="VS Code"), \
             patch("seraph_daemon.get_window_title", return_value="main.py"), \
             patch("seraph_daemon.get_idle_seconds", return_value=0.0), \
             patch("httpx.AsyncClient", return_value=mock_client):

            task = asyncio.create_task(
                periodic_capture_loop(
                    "http://localhost:8004",
                    idle_timeout=300,
                    verbose=False,
                    screen_runtime=None,
                )
            )
            await asyncio.sleep(0.3)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # No posts should have been made
        assert len(posts) == 0

    @pytest.mark.asyncio
    async def test_detailed_mode_posts_preserved_capture_artifacts(self, tmp_path):
        """Detailed mode should periodically analyze and preserve screenshots."""
        posts = []
        provider = SimpleNamespace(
            name="apple-vision",
            analyze_screen=AsyncMock(
                return_value=AnalysisResult(
                    success=True,
                    data={
                        "activity": "coding",
                        "project": "seraph",
                        "summary": "Editing Seraph settings.",
                        "details": ["settings"],
                    },
                    duration_ms=12,
                )
            ),
        )
        screen_runtime = SimpleNamespace(
            provider=provider,
            preserve_captures=True,
            archive_dir=str(tmp_path),
            blocklist=set(),
            refresh=AsyncMock(),
            budget_skip_reason=MagicMock(return_value=None),
            record_capture=MagicMock(),
            budget_status=MagicMock(return_value={}),
        )

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"mode": "detailed"}
            return resp

        async def mock_post(url, **kwargs):
            posts.append(kwargs.get("json", {}))
            raise asyncio.CancelledError()

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("seraph_daemon.get_frontmost_app_name", return_value="VS Code"), \
             patch("seraph_daemon.get_window_title", return_value="settings.py"), \
             patch("seraph_daemon.get_idle_seconds", return_value=0.0), \
             patch("ocr.screenshot.capture_screen_png", return_value=b"png bytes"), \
             patch("httpx.AsyncClient", return_value=mock_client):

            with pytest.raises(asyncio.CancelledError):
                await periodic_capture_loop(
                    "http://localhost:8004",
                    idle_timeout=300,
                    verbose=False,
                    screen_runtime=screen_runtime,
                )

        assert len(posts) == 1
        observation = posts[0]["observation"]
        assert observation["summary"] == "Editing Seraph settings."
        assert observation["analysis_usage"]["provider"] == "apple-vision"
        assert observation["analysis_usage"]["cost_posture"] == "local_on_device"
        assert observation["analysis_usage"]["duration_ms"] == 12
        assert observation["capture_artifacts"]["provider"] == "apple-vision"
        assert Path(observation["capture_artifacts"]["image_path"]).read_bytes() == b"png bytes"
        analysis_payload = json.loads(Path(observation["capture_artifacts"]["analysis_path"]).read_text(encoding="utf-8"))
        assert analysis_payload["analysis_usage"]["cost_posture"] == "local_on_device"
        assert provider.analyze_screen.await_count == 1
        screen_runtime.record_capture.assert_called_once()
        assert screen_runtime.refresh.await_count >= 1

    @pytest.mark.asyncio
    async def test_detailed_mode_posts_budget_skip_reason(self, tmp_path):
        posts = []
        provider = SimpleNamespace(name="apple-vision")
        runtime = ScreenAnalysisRuntime(
            enabled=True,
            provider="apple-vision",
            model=None,
            preserve_captures=True,
            archive_dir=str(tmp_path),
            blocklist_file=None,
        )
        runtime.provider = provider
        runtime.blocklist = set()
        runtime._update_budget_settings(0, 0, 365, 0)
        runtime.budget_skip_reason = MagicMock(return_value="max_daily_captures")
        runtime.refresh = AsyncMock()

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"mode": "detailed"}
            return resp

        async def mock_post(url, **kwargs):
            posts.append(kwargs.get("json", {}))
            raise asyncio.CancelledError()

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("seraph_daemon.get_frontmost_app_name", return_value="VS Code"), \
             patch("seraph_daemon.get_window_title", return_value="settings.py"), \
             patch("seraph_daemon.get_idle_seconds", return_value=0.0), \
             patch("ocr.screenshot.capture_screen_png", return_value=b"png bytes"), \
             patch("httpx.AsyncClient", return_value=mock_client):

            with pytest.raises(asyncio.CancelledError):
                await periodic_capture_loop(
                    "http://localhost:8004",
                    idle_timeout=300,
                    verbose=False,
                    screen_runtime=runtime,
                )

        assert posts[0]["observation"]["capture_skipped"] is True
        assert posts[0]["observation"]["capture_skip_reason"] == "max_daily_captures"
        assert not hasattr(provider, "analyze_screen")

    @pytest.mark.asyncio
    async def test_skips_when_idle(self):
        """No captures when user is idle even in detailed mode."""
        posts = []

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"mode": "detailed"}
            return resp

        async def mock_post(url, **kwargs):
            posts.append(kwargs.get("json", {}))
            return MagicMock(status_code=200)

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("seraph_daemon.get_frontmost_app_name", return_value="VS Code"), \
             patch("seraph_daemon.get_window_title", return_value="main.py"), \
             patch("seraph_daemon.get_idle_seconds", return_value=600.0), \
             patch("httpx.AsyncClient", return_value=mock_client):

            task = asyncio.create_task(
                periodic_capture_loop(
                    "http://localhost:8004",
                    idle_timeout=300,
                    verbose=False,
                    screen_runtime=None,
                )
            )
            await asyncio.sleep(0.3)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert len(posts) == 0


# ── Capture archive limits (#1263) ─────────────────────────────────────

from datetime import datetime, timedelta, timezone  # noqa: E402

_NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


class _FailingSettingsClient:
    async def get(self, _url):
        return SimpleNamespace(status_code=503, json=lambda: {})


def _old_group(root: Path, day: str, index: int, *, mtime: datetime, size: int = 300) -> list[Path]:
    stem = f"0900{index:02d}-code-{index:016x}"
    paths = [root / day / f"{stem}.png", root / day / f"{stem}.apple-vision.json", root / day / f"{stem}.analysis.json"]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * (size if path.suffix == ".png" else 50))
        os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    return paths


def test_archive_writer_names_match_the_pruner_pattern(tmp_path):
    artifacts = _archive_provider_capture(
        archive_dir=str(tmp_path),
        png_bytes=b"png bytes",
        app_name="Visual Studio Code — main.py",
        provider_name="apple-vision",
        analysis={"summary": "Editing"},
    )
    written = sorted(path for path in tmp_path.rglob("*") if path.is_file())
    assert len(written) == 3
    stems = set()
    for path in written:
        match = ARCHIVE_FILE_RE.fullmatch(path.name)
        assert match is not None, path.name
        stems.add(match.group("stem"))
    assert len(stems) == 1
    assert Path(artifacts["image_path"]) in written


def _uploading_runtime(tmp_path, upload_result, *, archive_max_mb=0):
    provider = SimpleNamespace(
        name="apple-vision",
        analyze_screen=AsyncMock(
            return_value=AnalysisResult(success=True, data={"summary": "Editing.", "activity": "coding"}, duration_ms=5)
        ),
    )
    async def post_context(client, payload):
        return await client.post("http://localhost:8004/api/observer/context", json=payload)

    uploader = SimpleNamespace(
        upload=AsyncMock(return_value=upload_result),
        status=MagicMock(return_value={}),
        origin="http://localhost:8004",
        post_context=post_context,
    )
    runtime = ScreenAnalysisRuntime(
        enabled=True,
        provider="apple-vision",
        model=None,
        preserve_captures=True,
        archive_dir=str(tmp_path / "archive"),
        blocklist_file=None,
        uploader=uploader,
        now=lambda: _NOW,
    )
    runtime.provider = provider
    runtime.enabled = True
    runtime.preserve_captures = True
    runtime.blocklist = set()
    runtime.refresh = AsyncMock()
    runtime._archive_limits_known = True
    runtime.archive_max_mb = archive_max_mb
    return runtime


async def _run_periodic_once(runtime) -> list[dict]:
    posts: list[dict] = []

    async def mock_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"mode": "detailed"}
        return resp

    async def mock_post(url, **kwargs):
        posts.append(kwargs.get("json", {}))
        raise asyncio.CancelledError()

    mock_client = AsyncMock()
    mock_client.get = mock_get
    mock_client.post = mock_post
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("seraph_daemon.get_frontmost_app_name", return_value="VS Code"), \
         patch("seraph_daemon.get_window_title", return_value="main.py"), \
         patch("seraph_daemon.get_idle_seconds", return_value=0.0), \
         patch("ocr.screenshot.capture_screen_png", return_value=b"png bytes"), \
         patch("httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(asyncio.CancelledError):
            await periodic_capture_loop("http://localhost:8004", idle_timeout=300, verbose=False, screen_runtime=runtime)
    return posts


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_status", ["accepted", "duplicate", "suppressed"])
async def test_capture_stored_by_backend_is_not_archived_again(tmp_path, monkeypatch, stored_status):
    monkeypatch.setenv("SERAPH_DAEMON_STATUS_FILE", str(tmp_path / "daemon-status.json"))
    runtime = _uploading_runtime(tmp_path, CaptureUploadResult(status=stored_status, reason="stored"))

    posts = await _run_periodic_once(runtime)

    assert len(posts) == 1
    assert "observation" not in posts[0]
    archive = tmp_path / "archive"
    assert not archive.exists() or not any(path.is_file() for path in archive.rglob("*"))


@pytest.mark.asyncio
async def test_failed_upload_is_archived_and_older_capture_pruned_under_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("SERAPH_DAEMON_STATUS_FILE", str(tmp_path / "daemon-status.json"))
    archive = tmp_path / "archive"
    old = _old_group(archive, "2026-11-16", 1, mtime=_NOW - timedelta(days=1), size=1024 * 1024)
    runtime = _uploading_runtime(
        tmp_path, CaptureUploadResult(status="failed", reason="backend_unreachable"), archive_max_mb=1
    )

    posts = await _run_periodic_once(runtime)

    artifacts = posts[0]["observation"]["capture_artifacts"]
    assert Path(artifacts["image_path"]).read_bytes() == b"png bytes"
    assert not any(path.exists() for path in old)
    assert runtime.budget_status()["archive_prune"]["trigger"] == "write"
    assert runtime.budget_status()["archive_prune"]["deleted_captures"] == 1


@pytest.mark.asyncio
async def test_archive_limits_come_only_from_backend_and_survive_an_outage(tmp_path, monkeypatch):
    monkeypatch.setenv("SERAPH_DAEMON_STATUS_FILE", str(tmp_path / "daemon-status.json"))
    archive = tmp_path / "archive"
    ancient = _old_group(archive, "2024-11-01", 1, mtime=_NOW - timedelta(days=2 * 365))
    runtime = ScreenAnalysisRuntime(
        enabled=True,
        provider="apple-vision",
        model=None,
        preserve_captures=True,
        archive_dir=str(archive),
        blocklist_file=None,
        now=lambda: _NOW,
    )

    with patch("ocr.create_provider", side_effect=lambda **_kwargs: _FakeProvider()), patch(
        "blocklist.load_blocklist", return_value=set()
    ):
        await runtime.refresh(_FailingSettingsClient(), "http://localhost:8004", force=True)
        # Limits unknown: even two-year-old files survive.
        assert all(path.exists() for path in ancient)
        assert runtime.budget_status()["archive_prune"] is None

        await runtime.refresh(
            _RuntimeSettingsClient(
                {
                    "enabled": True,
                    "provider": "apple-vision",
                    "preserve_captures": True,
                    "archive_dir": str(archive),
                    "archive_retention_days": 180,
                    "archive_max_mb": 64,
                }
            ),
            "http://localhost:8004",
            force=True,
        )
        assert not any(path.exists() for path in ancient)
        assert runtime.budget_status()["archive_prune"]["trigger"] == "settings"

        await runtime.refresh(_FailingSettingsClient(), "http://localhost:8004", force=True)

    assert runtime.archive_retention_days == 180
    assert runtime.archive_max_mb == 64


def test_day_rollover_prunes_groups_that_crossed_retention(tmp_path, monkeypatch):
    monkeypatch.setenv("SERAPH_DAEMON_STATUS_FILE", str(tmp_path / "daemon-status.json"))
    archive = tmp_path / "archive"
    clock = {"now": _NOW}
    group = _old_group(archive, "2026-10-18", 1, mtime=_NOW - timedelta(days=30, hours=-6))
    runtime = ScreenAnalysisRuntime(
        enabled=True,
        provider="apple-vision",
        model=None,
        preserve_captures=True,
        archive_dir=str(archive),
        blocklist_file=None,
        now=lambda: clock["now"],
    )
    runtime._archive_limits_known = True
    runtime._update_budget_settings(0, 0, 30, 0)

    runtime.budget_status()
    assert all(path.exists() for path in group)

    clock["now"] = _NOW + timedelta(days=1)
    status = runtime.budget_status()

    assert not any(path.exists() for path in group)
    assert status["archive_prune"]["trigger"] == "day_rollover"
    assert status["daily_capture_date"] == "2026-11-18"


@pytest.mark.asyncio
async def test_unconfirmed_upload_is_still_archived(tmp_path, monkeypatch):
    monkeypatch.setenv("SERAPH_DAEMON_STATUS_FILE", str(tmp_path / "daemon-status.json"))
    runtime = _uploading_runtime(tmp_path, CaptureUploadResult(status="unconfirmed", reason="upload_status_unconfirmed"))

    posts = await _run_periodic_once(runtime)

    artifacts = posts[0]["observation"]["capture_artifacts"]
    assert Path(artifacts["image_path"]).read_bytes() == b"png bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"detail": "Not authenticated"},
        {"enabled": True, "provider": "apple-vision", "archive_retention_days": 30},
        {"enabled": True, "provider": "apple-vision", "archive_max_mb": 8},
        {"enabled": True, "provider": "apple-vision", "archive_retention_days": "30", "archive_max_mb": 8},
        {"enabled": True, "provider": "apple-vision", "archive_retention_days": None, "archive_max_mb": 8},
    ],
)
async def test_200_without_both_integer_limits_keeps_last_limits_and_prunes_nothing(tmp_path, monkeypatch, payload):
    monkeypatch.setenv("SERAPH_DAEMON_STATUS_FILE", str(tmp_path / "daemon-status.json"))
    archive = tmp_path / "archive"
    ancient = _old_group(archive, "2024-11-01", 1, mtime=_NOW - timedelta(days=2 * 365))
    runtime = ScreenAnalysisRuntime(
        enabled=True,
        provider="apple-vision",
        model=None,
        preserve_captures=True,
        archive_dir=str(archive),
        blocklist_file=None,
        now=lambda: _NOW,
    )
    payload = {**payload, "archive_dir": str(archive)} if "detail" not in payload else payload

    with patch("ocr.create_provider", side_effect=lambda **_kwargs: _FakeProvider()), patch(
        "blocklist.load_blocklist", return_value=set()
    ):
        await runtime.refresh(_RuntimeSettingsClient(payload), "http://localhost:8004", force=True)

    assert all(path.exists() for path in ancient)
    assert runtime._archive_limits_known is False
    assert runtime.budget_status()["archive_prune"] is None
    assert runtime.archive_retention_days == 365
    assert runtime.archive_max_mb == 0


@pytest.mark.asyncio
async def test_200_without_limits_after_received_limits_keeps_them_without_pruning(tmp_path, monkeypatch):
    monkeypatch.setenv("SERAPH_DAEMON_STATUS_FILE", str(tmp_path / "daemon-status.json"))
    archive = tmp_path / "archive"
    runtime = ScreenAnalysisRuntime(
        enabled=True,
        provider="apple-vision",
        model=None,
        preserve_captures=True,
        archive_dir=str(archive),
        blocklist_file=None,
        now=lambda: _NOW,
    )
    base = {"enabled": True, "provider": "apple-vision", "preserve_captures": True, "archive_dir": str(archive)}
    with patch("ocr.create_provider", side_effect=lambda **_kwargs: _FakeProvider()), patch(
        "blocklist.load_blocklist", return_value=set()
    ):
        await runtime.refresh(
            _RuntimeSettingsClient({**base, "archive_retention_days": 180, "archive_max_mb": 64}),
            "http://localhost:8004",
            force=True,
        )
        first_prune = runtime.budget_status()["archive_prune"]
        # The backend's settings file became unreadable: it omits both limits.
        await runtime.refresh(
            _RuntimeSettingsClient({**base, "settings_unreadable": True, "min_seconds_between_captures": 5}),
            "http://localhost:8004",
            force=True,
        )

    assert runtime.archive_retention_days == 180
    assert runtime.archive_max_mb == 64
    assert runtime.min_seconds_between_captures == 5
    assert runtime.budget_status()["archive_prune"] is first_prune
