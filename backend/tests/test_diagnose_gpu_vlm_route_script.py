import importlib.util
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "diagnose_gpu_vlm_route.py"
_SPEC = importlib.util.spec_from_file_location("diagnose_gpu_vlm_route", _SCRIPT_PATH)
assert _SPEC is not None
diagnose_gpu_vlm_route = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(diagnose_gpu_vlm_route)
_is_direct_route_candidate = diagnose_gpu_vlm_route._is_direct_route_candidate


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
