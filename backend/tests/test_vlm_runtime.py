from unittest.mock import patch

import pytest

from config.settings import settings
from src.vlm_runtime import (
    deferred_vlm_live_probe,
    direct_local_chat_route_error,
    effective_vlm_status,
    probe_effective_vlm_runtime,
)


@pytest.mark.asyncio
async def test_probe_effective_vlm_runtime_is_disabled_without_network_access():
    """The compatibility probe must never reach a local GPU/VLM endpoint."""
    with (
        patch.object(settings, "seraph_vlm_base_url", "http://192.168.1.26:8001"),
        patch.object(settings, "seraph_vlm_api_key", "secret-token"),
    ):
        probe = await probe_effective_vlm_runtime()

    assert probe == {
        "checked": False,
        "reachable": False,
        "reason": "local_vlm_disabled_openrouter_only",
        "health": {"checked": False, "ok": False, "status_code": None, "error": ""},
        "backend_health": {"checked": False, "ok": False, "status_code": None, "error": ""},
        "queue_status": {"checked": False, "ok": False, "status_code": None, "error": ""},
        "chat_proxy": {"checked": False, "ok": False, "status_code": None, "error": ""},
    }
    assert "secret-token" not in str(probe)


def test_effective_vlm_status_is_explicitly_inactive_even_when_legacy_config_exists():
    with (
        patch.object(settings, "seraph_vlm_base_url", "http://192.168.1.26:8001"),
        patch.object(settings, "seraph_vlm_backend_url", "http://192.168.1.26:8000"),
        patch.object(settings, "seraph_vlm_api_key", "secret-token"),
        patch.object(settings, "seraph_vlm_feeder_window", 2),
    ):
        status = effective_vlm_status(live_probe=deferred_vlm_live_probe())

    assert status["active"] is False
    assert status["disabled_reason"] == "local_vlm_disabled_openrouter_only"
    assert status["configured"] is True
    assert status["base_url"] == "http://192.168.1.26:8001"
    assert status["backend_url"] == "http://192.168.1.26:8000"
    assert status["live_probe"]["reason"] == "deferred_fast_metadata"
    assert "secret-token" not in str(status)


@pytest.mark.asyncio
async def test_direct_local_chat_route_error_blocks_legacy_callers():
    assert await direct_local_chat_route_error() == "local_vlm_disabled_openrouter_only"


@pytest.mark.asyncio
async def test_direct_local_chat_route_error_allows_canonical_openrouter_path_without_vlm():
    with patch("src.vlm_runtime.probe_effective_vlm_runtime", side_effect=AssertionError("must not probe VLM")):
        assert await direct_local_chat_route_error(runtime_path="chat_agent") is None


@pytest.mark.asyncio
async def test_direct_local_chat_route_error_keeps_unknown_route_fail_closed():
    assert await direct_local_chat_route_error(runtime_path="legacy_unknown_route") == (
        "local_vlm_disabled_openrouter_only"
    )
