from unittest.mock import patch

import httpx
import pytest

from config.settings import settings
from src.vlm_runtime import effective_vlm_status, probe_effective_vlm_runtime


@pytest.mark.asyncio
async def test_probe_effective_vlm_runtime_reports_health_backend_queue_and_chat_proxy():
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

        async def get(self, url, headers=None):
            calls.append(("GET", url, headers or {}))
            if url.endswith("/health/backend"):
                return FakeResponse(200, {"status": "ok", "backend_status": 200, "model": "gemma"})
            if url.endswith("/queue/status"):
                return FakeResponse(
                    200,
                    {"queued": 2, "active": 1, "workers": 1, "background_workers": 1},
                )
            if url.endswith("/health/chat"):
                return FakeResponse(
                    200,
                    {
                        "status": "ok",
                        "enabled": True,
                        "auth_configured": True,
                        "auth_ok": True,
                        "model": "gemma",
                    },
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
        patch.object(settings, "seraph_vlm_api_key", "secret-token"),
        patch.object(settings, "local_vlm_model", "unsloth/gemma-4-26B-A4B-it-qat-GGUF"),
        patch("src.vlm_runtime.httpx.AsyncClient", FakeAsyncClient),
    ):
        probe = await probe_effective_vlm_runtime()
        status = effective_vlm_status(live_probe=probe)

    assert {(call[0], call[1]) for call in calls} == {
        ("GET", "http://192.168.1.26:8001/health"),
        ("GET", "http://192.168.1.26:8001/health/backend"),
        ("GET", "http://192.168.1.26:8001/queue/status"),
        ("GET", "http://192.168.1.26:8001/health/chat"),
    }
    chat_call = next(call for call in calls if call[1].endswith("/health/chat"))
    assert chat_call[2]["Authorization"] == "Bearer secret-token"
    assert probe["checked"] is True
    assert probe["reachable"] is True
    assert probe["health"]["ok"] is True
    assert probe["backend_health"]["backend_status"] == 200
    assert probe["queue_status"]["ok"] is True
    assert probe["chat_proxy"]["ok"] is True
    assert probe["chat_proxy"]["auth_ok"] is True
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

        async def get(self, url, headers=None):
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
    assert probe["chat_proxy"]["error"] == "connect_error"
    assert "token-secret" not in str(probe)


@pytest.mark.asyncio
async def test_probe_effective_vlm_runtime_marks_disabled_chat_proxy_unreachable():
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

        async def get(self, url, headers=None):
            if url.endswith("/health/backend"):
                return FakeResponse(200, {"status": "ok", "backend_status": 200, "model": "gemma"})
            if url.endswith("/queue/status"):
                return FakeResponse(200, {"queued": 0, "active": 0, "workers": 1})
            if url.endswith("/health/chat"):
                return FakeResponse(
                    200,
                    {
                        "status": "disabled",
                        "enabled": False,
                        "auth_configured": True,
                        "auth_ok": False,
                    },
                )
            return FakeResponse(200, {"status": "ok", "model": "gemma"})

    with (
        patch.object(settings, "seraph_vlm_base_url", "http://192.168.1.26:8001"),
        patch.object(settings, "seraph_vlm_api_key", "secret-token"),
        patch("src.vlm_runtime.httpx.AsyncClient", FakeAsyncClient),
    ):
        probe = await probe_effective_vlm_runtime()

    assert probe["health"]["ok"] is True
    assert probe["backend_health"]["ok"] is True
    assert probe["queue_status"]["ok"] is True
    assert probe["chat_proxy"]["ok"] is False
    assert probe["chat_proxy"]["status_code"] == 200
    assert probe["chat_proxy"]["error"] == "disabled"
    assert probe["reachable"] is False
    assert "secret-token" not in str(probe)


@pytest.mark.asyncio
async def test_probe_effective_vlm_runtime_marks_chat_auth_mismatch_unreachable():
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

        async def get(self, url, headers=None):
            if url.endswith("/health/backend"):
                return FakeResponse(200, {"status": "ok", "backend_status": 200, "model": "gemma"})
            if url.endswith("/queue/status"):
                return FakeResponse(200, {"queued": 0, "active": 0, "workers": 1})
            if url.endswith("/health/chat"):
                return FakeResponse(
                    200,
                    {
                        "status": "auth_failed",
                        "enabled": True,
                        "auth_configured": True,
                        "auth_ok": False,
                    },
                )
            return FakeResponse(200, {"status": "ok", "model": "gemma"})

    with (
        patch.object(settings, "seraph_vlm_base_url", "http://192.168.1.26:8001"),
        patch.object(settings, "seraph_vlm_api_key", "wrong-secret"),
        patch("src.vlm_runtime.httpx.AsyncClient", FakeAsyncClient),
    ):
        probe = await probe_effective_vlm_runtime()

    assert probe["health"]["ok"] is True
    assert probe["backend_health"]["ok"] is True
    assert probe["queue_status"]["ok"] is True
    assert probe["chat_proxy"]["ok"] is False
    assert probe["chat_proxy"]["error"] == "auth_failed"
    assert probe["reachable"] is False
    assert "wrong-secret" not in str(probe)
