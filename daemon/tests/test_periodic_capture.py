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
    def __init__(self, name="codex-local", available=True):
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
        openrouter_api_key=None,
        blocklist_file=None,
    )
    first_archive = tmp_path / "first"
    first_client = _RuntimeSettingsClient(
        {
            "enabled": True,
            "provider": "codex-local",
            "model": "gpt-5.5",
            "preserve_captures": True,
            "archive_dir": str(first_archive),
        }
    )

    with patch("ocr.create_provider", side_effect=fake_create_provider), patch(
        "blocklist.load_blocklist", return_value={"SecretApp"}
    ):
        await runtime.refresh(first_client, "http://localhost:8004", force=True)

        assert runtime.provider is not None
        assert runtime.provider.name == "codex-local"
        assert runtime.preserve_captures is True
        assert runtime.archive_dir == str(first_archive)
        assert runtime.blocklist == {"SecretApp"}

        second_archive = tmp_path / "second"
        second_client = _RuntimeSettingsClient(
            {
                "enabled": True,
                "provider": "apple-vision",
                "model": "",
                "preserve_captures": False,
                "archive_dir": str(second_archive),
            }
        )
        old_provider = runtime.provider
        await runtime.refresh(second_client, "http://localhost:8004", force=True)

    assert old_provider.closed is True
    assert runtime.provider is not None
    assert runtime.provider.name == "apple-vision"
    assert runtime.preserve_captures is False
    assert runtime.archive_dir == str(second_archive)


@pytest.mark.asyncio
async def test_screen_analysis_runtime_pauses_on_unavailable_settings_provider(tmp_path):
    providers = [_FakeProvider("codex-local"), _FakeProvider("openrouter", available=False)]

    def fake_create_provider(**_kwargs):
        return providers.pop(0)

    runtime = ScreenAnalysisRuntime(
        enabled=True,
        provider="codex-local",
        model=None,
        preserve_captures=True,
        archive_dir=str(tmp_path / "fallback"),
        openrouter_api_key=None,
        blocklist_file=None,
    )
    first_client = _RuntimeSettingsClient(
        {
            "enabled": True,
            "provider": "codex-local",
            "model": "gpt-5.5",
            "preserve_captures": True,
            "archive_dir": str(tmp_path / "first"),
        }
    )
    second_client = _RuntimeSettingsClient(
        {
            "enabled": True,
            "provider": "openrouter",
            "model": "google/gemini-2.5-flash-lite",
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
            name="codex-local",
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
        assert observation["capture_artifacts"]["provider"] == "codex-local"
        assert Path(observation["capture_artifacts"]["image_path"]).read_bytes() == b"png bytes"
        assert provider.analyze_screen.await_count == 1
        assert screen_runtime.refresh.await_count >= 1

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
