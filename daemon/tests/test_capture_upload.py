"""Daemon capture handoff: no cloud call, authenticated local upload (ADR-035).

Every HTTP request goes through a guard transport that fails the test on any
non-loopback URL, so a regression that reintroduces a cloud OCR call fails.
"""

import asyncio
import base64
import importlib
import json
import os
import platform
import sys
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin",
    reason="Daemon tests require macOS (PyObjC, Quartz)",
)

httpx = pytest.importorskip("httpx")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from capture_upload import LocalCaptureUploader, validate_local_backend_origin  # noqa: E402
from ocr import create_provider  # noqa: E402
from ocr.base import AnalysisResult  # noqa: E402
from seraph_daemon import ScreenAnalysisRuntime, periodic_capture_loop, poll_loop  # noqa: E402

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_REAL_ASYNC_CLIENT = httpx.AsyncClient


class _CloudCallAttempted(AssertionError):
    pass


class _GuardedBackend:
    """Local fake backend; any non-loopback request fails the test."""

    def __init__(self, *, upload_status: int = 201, settings_provider: str = "openrouter", stop_on_context: bool = True):
        self.requests: list[httpx.Request] = []
        self.upload_status = upload_status
        self.settings_provider = settings_provider
        self.stop_on_context = stop_on_context

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host not in _LOOPBACK:
            raise _CloudCallAttempted(f"daemon attempted a non-local call: {request.url}")
        self.requests.append(request)
        path = request.url.path
        if path == "/api/settings/screen-analysis":
            return httpx.Response(200, json={"enabled": True, "provider": self.settings_provider, "model": "openrouter/x/y"})
        if path == "/api/settings/capture-mode":
            return httpx.Response(200, json={"mode": "detailed"})
        if path == "/api/observer/notifications/next":
            return httpx.Response(200, json={"notification": None})
        if path == "/api/observer/captures":
            if self.upload_status >= 400:
                return httpx.Response(self.upload_status, json={"detail": {"code": "synthetic"}})
            return httpx.Response(
                self.upload_status,
                json={"status": "accepted", "reason_code": "capture_accepted", "observation_id": "obs-1"},
            )
        if path == "/api/observer/context":
            if self.stop_on_context:
                raise asyncio.CancelledError()
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    def client_factory(self):
        def factory(*args, **kwargs):
            kwargs.pop("transport", None)
            return _REAL_ASYNC_CLIENT(*args, transport=httpx.MockTransport(self.handler), **kwargs)

        return factory

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def uploads(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path == "/api/observer/captures"]


class _FakeAppleVision:
    name = "apple-vision"

    def __init__(self):
        self.calls = 0

    def is_available(self):
        return True

    async def analyze_screen(self, png_bytes, app_name):
        self.calls += 1
        return AnalysisResult(
            success=True,
            data={"activity": "other", "project": None, "summary": "def main", "details": []},
            duration_ms=3,
            text="def main():\n    pass",
        )

    async def close(self):
        return None


def _uploader(**overrides):
    kwargs = dict(
        backend_url="http://localhost:8004",
        credential="synthetic-edge-credential",
        device_id="mac-local-1",
        pairing_id="pair-local-1",
        sleep=_no_sleep,
    )
    kwargs.update(overrides)
    return LocalCaptureUploader(**kwargs)


async def _no_sleep(_seconds):
    return None


def _runtime(tmp_path, uploader):
    return ScreenAnalysisRuntime(
        enabled=True,
        provider="apple-vision",
        model=None,
        preserve_captures=False,
        archive_dir=str(tmp_path),
        blocklist_file=None,
        uploader=uploader,
    )


def test_openrouter_ocr_provider_is_removed():
    with pytest.raises(ValueError):
        create_provider(name="openrouter")
    with pytest.raises(ImportError):
        importlib.import_module("ocr.openrouter")


@pytest.mark.parametrize(
    "url",
    ["https://openrouter.ai", "http://example.com:8004", "http://10.0.0.5:8004", "http://user:pw@localhost:8004"],
)
def test_uploader_only_targets_loopback_backend(url):
    with pytest.raises(ValueError):
        validate_local_backend_origin(url)
    with pytest.raises(ValueError):
        _uploader(backend_url=url)


def test_uploader_requires_credential_and_pairing_identity(monkeypatch):
    with pytest.raises(ValueError):
        _uploader(credential=None)
    with pytest.raises(ValueError):
        _uploader(device_id="")
    for name in ("SERAPH_EDGE_CREDENTIAL", "SERAPH_EDGE_CREDENTIAL_FILE", "SERAPH_EDGE_DEVICE_ID", "SERAPH_EDGE_PAIRING_ID"):
        monkeypatch.delenv(name, raising=False)
    assert LocalCaptureUploader.from_environment(backend_url="http://localhost:8004") is None


def test_uploader_reads_credential_from_private_file(tmp_path, monkeypatch):
    credential_file = tmp_path / "edge-credential"
    credential_file.write_text("file-credential\n", encoding="utf-8")
    monkeypatch.delenv("SERAPH_EDGE_CREDENTIAL", raising=False)
    monkeypatch.setenv("SERAPH_EDGE_DEVICE_ID", "mac-local-1")
    monkeypatch.setenv("SERAPH_EDGE_PAIRING_ID", "pair-local-1")
    monkeypatch.setenv("SERAPH_EDGE_CREDENTIAL_FILE", str(credential_file))
    uploader = LocalCaptureUploader.from_environment(backend_url="http://127.0.0.1:8004")
    assert uploader is not None
    assert uploader._headers()["Authorization"] == "Bearer file-credential"


@pytest.mark.asyncio
async def test_upload_sends_authenticated_local_request_with_metadata():
    backend = _GuardedBackend()
    uploader = _uploader()
    async with backend.client_factory()() as client:
        result = await uploader.upload(
            client,
            b"\x89PNG\r\n\x1a\nbytes",
            app_name="Code",
            window_title="main.py",
            blocklist=set(),
            local_analysis={"provider": "apple-vision", "summary": "hello", "text": "hello world" + "x" * 9000,
                            "details": ["a", 3, "b"], "unexpected": "dropped"},
        )
    assert result.status == "accepted" and result.stored
    [request] = backend.uploads()
    assert str(request.url) == "http://localhost:8004/api/observer/captures"
    assert request.headers["authorization"] == "Bearer synthetic-edge-credential"
    # Native ingress: no Origin and no cookie (bearer credential only).
    assert "origin" not in request.headers
    assert "cookie" not in request.headers
    assert request.headers["x-seraph-node-device"] == "mac-local-1"
    assert request.headers["x-seraph-node-pairing"] == "pair-local-1"
    body = json.loads(request.content)
    assert body["app_name"] == "Code"
    assert body["window_title"] == "main.py"
    analysis = body["local_analysis"]
    assert analysis["provider"] == "apple-vision" and analysis["summary"] == "hello"
    assert len(analysis["text"]) == 8 * 1024
    assert analysis["details"] == ["a", "b"]
    assert "unexpected" not in analysis
    assert body["capture_id"].startswith("cap-")
    assert base64.b64decode(body["content_base64"]) == b"\x89PNG\r\n\x1a\nbytes"
    assert "captured_at" in body


@pytest.mark.asyncio
async def test_blocked_app_never_uploads():
    backend = _GuardedBackend()
    async with backend.client_factory()() as client:
        result = await _uploader().upload(
            client, b"png", app_name="1Password 8", window_title="vault", blocklist={"1password"}
        )
    assert result.status == "blocked"
    assert backend.requests == []


@pytest.mark.asyncio
async def test_retries_are_bounded_and_cool_down_prevents_hammering():
    now = [1000.0]
    backend = _GuardedBackend(upload_status=503)
    uploader = _uploader(clock=lambda: now[0])
    async with backend.client_factory()() as client:
        first = await uploader.upload(client, b"png", app_name="Code", window_title="", blocklist=set())
        assert first.status == "failed"
        assert len(backend.uploads()) == 2  # one retry, then stop
        skipped = await uploader.upload(client, b"png", app_name="Code", window_title="", blocklist=set())
        assert skipped.reason == "upload_cooldown"
        assert len(backend.uploads()) == 2  # no request during cool-down

        backend.upload_status = 403
        now[0] += 31
        rejected = await uploader.upload(client, b"png", app_name="Code", window_title="", blocklist=set())
        assert rejected.status == "failed" and rejected.http_status == 403
        assert len(backend.uploads()) == 3  # 4xx is not retried

        backend.upload_status = 429
        now[0] += 601
        limited = await uploader.upload(client, b"png", app_name="Code", window_title="", blocklist=set())
        assert limited.http_status == 429
        assert len(backend.uploads()) == 4
        assert uploader.status()["cooling_down"] is True


@pytest.mark.asyncio
async def test_poll_loop_uploads_locally_and_never_calls_the_cloud(tmp_path):
    backend = _GuardedBackend(settings_provider="openrouter")
    provider = _FakeAppleVision()
    runtime = _runtime(tmp_path, _uploader())
    with patch("httpx.AsyncClient", side_effect=backend.client_factory()), \
         patch("ocr.create_provider", return_value=provider) as create, \
         patch("blocklist.load_blocklist", return_value=set()), \
         patch("seraph_daemon._write_daemon_status"), \
         patch("seraph_daemon.get_idle_seconds", return_value=0.0), \
         patch("seraph_daemon.get_frontmost_app_name", return_value="Code"), \
         patch("seraph_daemon.get_window_title", return_value="main.py"), \
         patch("ocr.screenshot.capture_screen_png", return_value=b"\x89PNG\r\n\x1a\nscreen"):
        with pytest.raises(asyncio.CancelledError):
            await poll_loop("http://localhost:8004", interval=0.01, idle_timeout=300, verbose=False,
                            screen_runtime=runtime, worker_id="test-worker")

    # Backend "openrouter" names its governed route; the daemon stays local.
    assert create.call_args.kwargs["name"] == "apple-vision"
    assert "api_key" not in create.call_args.kwargs
    assert provider.calls == 1
    [upload] = backend.uploads()
    body = json.loads(upload.content)
    assert body["app_name"] == "Code" and body["window_title"] == "main.py"
    assert body["local_analysis"]["provider"] == "apple-vision"
    assert body["local_analysis"]["text"] == "def main():\n    pass"
    assert body["local_analysis"]["summary"] == "def main"
    context = [r for r in backend.requests if r.url.path == "/api/observer/context"][-1]
    context_body = json.loads(context.content)
    # The stored upload is the observation; the context post does not duplicate it.
    assert "observation" not in context_body
    assert context_body["active_window"] == "Code — main.py"


@pytest.mark.asyncio
async def test_poll_loop_keeps_json_observation_when_upload_unconfigured(tmp_path):
    backend = _GuardedBackend()
    provider = _FakeAppleVision()
    runtime = _runtime(tmp_path, None)
    with patch("httpx.AsyncClient", side_effect=backend.client_factory()), \
         patch("ocr.create_provider", return_value=provider), \
         patch("blocklist.load_blocklist", return_value=set()), \
         patch("seraph_daemon._write_daemon_status"), \
         patch("seraph_daemon.get_idle_seconds", return_value=0.0), \
         patch("seraph_daemon.get_frontmost_app_name", return_value="Code"), \
         patch("seraph_daemon.get_window_title", return_value="main.py"), \
         patch("ocr.screenshot.capture_screen_png", return_value=b"\x89PNG\r\n\x1a\nscreen"):
        with pytest.raises(asyncio.CancelledError):
            await poll_loop("http://localhost:8004", interval=0.01, idle_timeout=300, verbose=False,
                            screen_runtime=runtime, worker_id="test-worker")

    assert backend.uploads() == []
    context = [r for r in backend.requests if r.url.path == "/api/observer/context"][-1]
    assert json.loads(context.content)["observation"]["summary"] == "def main"


@pytest.mark.asyncio
async def test_periodic_capture_uploads_and_skips_blocked_apps(tmp_path):
    backend = _GuardedBackend()
    provider = _FakeAppleVision()
    runtime = _runtime(tmp_path, _uploader())
    with patch("httpx.AsyncClient", side_effect=backend.client_factory()), \
         patch("ocr.create_provider", return_value=provider), \
         patch("blocklist.load_blocklist", return_value=set()), \
         patch("seraph_daemon._write_daemon_status"), \
         patch("seraph_daemon.get_idle_seconds", return_value=0.0), \
         patch("seraph_daemon.get_frontmost_app_name", return_value="Code"), \
         patch("seraph_daemon.get_window_title", return_value="main.py"), \
         patch("ocr.screenshot.capture_screen_png", return_value=b"\x89PNG\r\n\x1a\nscreen"):
        with pytest.raises(asyncio.CancelledError):
            await periodic_capture_loop("http://localhost:8004", idle_timeout=300, verbose=False,
                                        screen_runtime=runtime)
    assert len(backend.uploads()) == 1

    blocked_backend = _GuardedBackend(stop_on_context=False)
    blocked_runtime = _runtime(tmp_path, _uploader())
    with patch("httpx.AsyncClient", side_effect=blocked_backend.client_factory()), \
         patch("ocr.create_provider", return_value=_FakeAppleVision()), \
         patch("blocklist.load_blocklist", return_value={"1password"}), \
         patch("seraph_daemon._write_daemon_status"), \
         patch("seraph_daemon.get_idle_seconds", return_value=0.0), \
         patch("seraph_daemon.get_frontmost_app_name", return_value="1Password 8"), \
         patch("seraph_daemon.get_window_title", return_value="vault"), \
         patch("ocr.screenshot.capture_screen_png") as blocked_capture, \
         patch("seraph_daemon.asyncio.sleep", side_effect=asyncio.CancelledError()):
        with pytest.raises(asyncio.CancelledError):
            await periodic_capture_loop("http://localhost:8004", idle_timeout=300, verbose=False,
                                        screen_runtime=blocked_runtime)
    assert blocked_backend.uploads() == []
    blocked_capture.assert_not_called()


@pytest.mark.asyncio
async def test_codex_local_structured_result_travels_with_the_upload(tmp_path):
    backend = _GuardedBackend()

    class _FakeCodex(_FakeAppleVision):
        name = "codex-local"

        async def analyze_screen(self, png_bytes, app_name):
            return AnalysisResult(
                success=True,
                data={"activity": "coding", "project": "seraph", "summary": "Editing upload", "details": ["file: x.py"]},
                duration_ms=5,
            )

    runtime = ScreenAnalysisRuntime(
        enabled=True, provider="codex-local", model=None, preserve_captures=False,
        archive_dir=str(tmp_path), blocklist_file=None, uploader=_uploader(),
    )
    with patch("httpx.AsyncClient", side_effect=backend.client_factory()), \
         patch("ocr.create_provider", return_value=_FakeCodex()), \
         patch("blocklist.load_blocklist", return_value=set()), \
         patch("seraph_daemon._write_daemon_status"), \
         patch("seraph_daemon.get_idle_seconds", return_value=0.0), \
         patch("seraph_daemon.get_frontmost_app_name", return_value="Code"), \
         patch("seraph_daemon.get_window_title", return_value="x.py"), \
         patch("ocr.screenshot.capture_screen_png", return_value=b"\x89PNG\r\n\x1a\nscreen"):
        with pytest.raises(asyncio.CancelledError):
            await poll_loop("http://localhost:8004", interval=0.01, idle_timeout=300, verbose=False,
                            screen_runtime=runtime, worker_id="test-worker")
    [upload] = backend.uploads()
    analysis = json.loads(upload.content)["local_analysis"]
    assert analysis == {
        "provider": "codex-local",
        "activity": "coding",
        "project": "seraph",
        "summary": "Editing upload",
        "details": ["file: x.py"],
    }
