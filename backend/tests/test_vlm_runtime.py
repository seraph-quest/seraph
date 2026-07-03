from unittest.mock import patch

import httpx
import pytest

from config.settings import settings
from src.vlm_runtime import effective_vlm_status, probe_effective_vlm_runtime


@pytest.mark.asyncio
async def test_probe_effective_vlm_runtime_reports_health_backend_and_queue():
    calls = []

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, url):
            calls.append(url)
            if url.endswith("/health/backend"):
                return FakeResponse(200, {"status": "ok", "backend_status": 200, "model": "gemma"})
            if url.endswith("/queue/status"):
                return FakeResponse(
                    200,
                    {"queued": 2, "active": 1, "workers": 1, "background_workers": 1},
                )
            return FakeResponse(
                200,
                {
                    "status": "ok",
                    "model": "gemma",
                    "queue": {"queued": 2, "active": 1, "workers": 1, "background_workers": 1},
                },
            )

    with (
        patch.object(settings, "seraph_vlm_base_url", "http://192.168.1.26:8001"),
        patch("src.vlm_runtime.httpx.AsyncClient", FakeAsyncClient),
    ):
        probe = await probe_effective_vlm_runtime()
        status = effective_vlm_status(live_probe=probe)

    assert set(calls) == {
        "http://192.168.1.26:8001/health",
        "http://192.168.1.26:8001/health/backend",
        "http://192.168.1.26:8001/queue/status",
    }
    assert probe["checked"] is True
    assert probe["reachable"] is True
    assert probe["health"]["ok"] is True
    assert probe["backend_health"]["backend_status"] == 200
    assert probe["queue_status"]["ok"] is True
    assert probe["queue_status"]["queue"] == {
        "queued": 2,
        "active": 1,
        "workers": 1,
        "background_workers": 1,
    }
    assert status["live_probe"] == probe


@pytest.mark.asyncio
async def test_probe_effective_vlm_runtime_reports_connect_errors_without_secret():
    class FakeAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, url):
            raise httpx.ConnectError("cannot reach token-secret")

    with (
        patch.object(settings, "seraph_vlm_base_url", "http://192.168.1.26:8001"),
        patch.object(settings, "seraph_vlm_api_key", "token-secret"),
        patch("src.vlm_runtime.httpx.AsyncClient", FakeAsyncClient),
    ):
        probe = await probe_effective_vlm_runtime()

    assert probe["checked"] is True
    assert probe["reachable"] is False
    assert probe["health"]["error"] == "connect_error"
    assert probe["backend_health"]["error"] == "connect_error"
    assert probe["queue_status"]["error"] == "connect_error"
    assert "token-secret" not in str(probe)
