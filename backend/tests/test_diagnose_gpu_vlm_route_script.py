import importlib.util
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "diagnose_gpu_vlm_route.py"
_SPEC = importlib.util.spec_from_file_location("diagnose_gpu_vlm_route", _SCRIPT_PATH)
assert _SPEC is not None
diagnose_gpu_vlm_route = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(diagnose_gpu_vlm_route)
_is_direct_route_candidate = diagnose_gpu_vlm_route._is_direct_route_candidate
_get_chat_health = diagnose_gpu_vlm_route._get_chat_health


def test_diagnose_gpu_vlm_route_rejects_loopback_and_unspecified_addresses():
    assert _is_direct_route_candidate("http://127.0.0.1:8001") is False
    assert _is_direct_route_candidate("http://127.0.0.2:8001") is False
    assert _is_direct_route_candidate("http://localhost:8001") is False
    assert _is_direct_route_candidate("http://0.0.0.0:8001") is False
    assert _is_direct_route_candidate("http://[::1]:8001") is False
    assert _is_direct_route_candidate("http://[::ffff:127.0.0.1]:8001") is False


def test_diagnose_gpu_vlm_route_allows_lan_addresses():
    assert _is_direct_route_candidate("http://192.168.1.26:8001") is True
    assert _is_direct_route_candidate("http://jupyter.local:8001") is True


def test_diagnose_gpu_vlm_route_chat_health_requires_auth_ok():
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "status": "ok",
                "enabled": True,
                "auth_configured": True,
                "auth_ok": True,
                "model": "gemma",
            }

    class FakeClient:
        def get(self, url, headers=None):
            calls.append((url, headers or {}))
            return FakeResponse()

    result = _get_chat_health(FakeClient(), "http://192.168.1.26:8001", "secret-token")

    assert calls == [
        (
            "http://192.168.1.26:8001/health/chat",
            {"Authorization": "Bearer secret-token"},
        )
    ]
    assert result["ok"] is True
    assert result["auth_ok"] is True
    assert "secret-token" not in str(result)


def test_diagnose_gpu_vlm_route_chat_health_fails_auth_mismatch():
    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "status": "auth_failed",
                "enabled": True,
                "auth_configured": True,
                "auth_ok": False,
            }

    class FakeClient:
        def get(self, url, headers=None):
            return FakeResponse()

    result = _get_chat_health(FakeClient(), "http://192.168.1.26:8001", "wrong-token")

    assert result["ok"] is False
    assert result["status"] == "auth_failed"
    assert "wrong-token" not in str(result)
