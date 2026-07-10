"""Tests for provider-backed semantic screenshot analysis."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("mocked_vlm_model_fabric_adoption")

from src.observer.screenshot_semantic_analysis import (
    analyze_screenshot_image,
    screenshot_semantic_analysis_accepting_background_work,
    screenshot_semantic_analysis_background_slots,
)


async def test_local_vlm_analyzer_posts_prompt_file_and_validates_response(tmp_path, monkeypatch):
    image = tmp_path / "capture.png"
    image.write_bytes(b"png bytes")
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "analysis": {
                    "schema_version": "seraph.screenshot_analysis.v1",
                    "prompt_version": "seraph.screenshot_analysis.prompt.v1",
                    "summary": "The user is reviewing a screenshot analysis flow.",
                    "detailed_observations": ["A Seraph test file is visible."],
                    "activity_type": "reviewing",
                    "project": "seraph",
                    "applications": ["editor"],
                    "visible_artifacts": ["test_screenshot_semantic_analysis.py"],
                    "key_visible_text": ["local-vlm"],
                    "user_intent": "Verify local VLM request wiring.",
                    "goal_alignment": {
                        "status": "aligned",
                        "goal_refs": ["screenshot intelligence loop"],
                        "evidence": ["The screenshot analyzer test is being edited."],
                        "needle_movement": "pushed",
                    },
                    "confidence": 0.91,
                    "sensitive_content_seen": False,
                    "privacy_notes": [],
                    "report_tags": ["vlm", "screenshots"],
                }
            }

    class FakeAsyncClient:
        def __init__(self, *, timeout, follow_redirects=False):
            self.timeout = timeout
            self.follow_redirects = follow_redirects

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, endpoint, *, data, files, headers):
            file_name, image_file, media_type = files["file"]
            calls.append(
                {
                    "endpoint": endpoint,
                    "data": data,
                    "file_name": file_name,
                    "file_bytes": image_file,
                    "media_type": media_type,
                    "headers": headers,
                    "timeout": self.timeout,
                    "follow_redirects": self.follow_redirects,
                }
            )
            return FakeResponse()

    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.screen_analysis_provider", "local-vlm")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.seraph_vlm_base_url", "")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.seraph_vlm_api_key", "")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.local_vlm_base_url", "http://gpu:8088")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.local_vlm_model", "gemma-4-test")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.local_vlm_api_key", "secret-token")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.local_vlm_timeout_seconds", 9)
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.httpx.AsyncClient", FakeAsyncClient)

    analysis = await analyze_screenshot_image(
        image,
        {
            "created_at": "2026-06-30T10:00:00+00:00",
            "image_sha256": "abc123",
            "file_format": "png",
            "width": 1,
            "height": 1,
        },
    )

    assert analysis is not None
    assert analysis.project == "seraph"
    assert analysis.goal_alignment.needle_movement == "pushed"
    assert calls[0]["endpoint"] == "http://gpu:8088/v1/analyze-file"
    assert calls[0]["data"]["model"] == "gemma-4-test"
    assert calls[0]["data"]["runtime_profile"] == "screenshot_fast"
    assert calls[0]["data"]["runtime_path"] == "screenshot_image_analysis"
    assert calls[0]["data"]["priority"] == "normal"
    assert calls[0]["data"]["reasoning"] == "off"
    assert '"enable_thinking":false' in calls[0]["data"]["profile_options"]
    assert "seraph.screenshot_analysis.v1" in calls[0]["data"]["prompt"]
    assert calls[0]["file_name"] == "capture.png"
    assert calls[0]["file_bytes"] == b"png bytes"
    assert calls[0]["media_type"] == "image/png"
    assert calls[0]["headers"] == {
        "Authorization": "Bearer secret-token",
        "X-Seraph-Priority": "normal",
        "X-Seraph-Reasoning": "off",
        "X-Seraph-Runtime-Path": "screenshot_image_analysis",
        "X-Seraph-Runtime-Profile": "screenshot_fast",
    }
    assert calls[0]["timeout"].connect <= 9
    assert calls[0]["timeout"].connect > 0
    assert calls[0]["follow_redirects"] is False


async def test_gpu_vlm_runtime_overrides_legacy_local_vlm_base_url(tmp_path, monkeypatch):
    image = tmp_path / "capture.png"
    image.write_bytes(b"png bytes")
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "analysis": {
                    "summary": "The GPU-hosted wrapper handled the screenshot.",
                    "activity_type": "reviewing",
                    "confidence": 0.8,
                }
            }

    class FakeAsyncClient:
        def __init__(self, *, timeout, follow_redirects=False):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, endpoint, *, data, files, headers):
            calls.append({"endpoint": endpoint, "headers": headers})
            return FakeResponse()

    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.screen_analysis_provider", "local-vlm")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.local_vlm_base_url", "http://127.0.0.1:8000")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.seraph_vlm_base_url", "http://192.168.1.26:8001")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.seraph_vlm_api_key", "gpu-token")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.local_vlm_model", "gpu-vlm")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.httpx.AsyncClient", FakeAsyncClient)

    analysis = await analyze_screenshot_image(image, {})

    assert analysis is not None
    assert calls[0]["endpoint"] == "http://192.168.1.26:8001/v1/analyze-file"
    assert calls[0]["headers"]["Authorization"] == "Bearer gpu-token"


async def test_local_vlm_analyzer_is_disabled_without_provider(tmp_path, monkeypatch):
    image = tmp_path / "capture.png"
    image.write_bytes(b"png bytes")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.screen_analysis_provider", "")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.local_vlm_base_url", "http://gpu:8088")

    analysis = await analyze_screenshot_image(image, {})

    assert analysis is None


async def test_local_vlm_analyzer_uses_persisted_screen_analysis_provider(tmp_path, monkeypatch):
    from src.observer.screen_analysis_settings import write_screen_analysis_settings
    from src.observer.screenshot_semantic_analysis import screenshot_semantic_analysis_enabled

    image = tmp_path / "capture.png"
    image.write_bytes(b"png bytes")
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "analysis": {
                    "summary": "The persisted screen-analysis settings are driving local VLM.",
                    "activity_type": "reviewing",
                    "confidence": 0.8,
                }
            }

    class FakeAsyncClient:
        def __init__(self, *, timeout, follow_redirects=False):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, endpoint, *, data, files, headers):
            calls.append({"endpoint": endpoint, "data": data, "headers": headers})
            return FakeResponse()

    monkeypatch.setattr("src.observer.screen_analysis_settings.settings.workspace_dir", str(tmp_path))
    monkeypatch.setattr("src.observer.screen_analysis_settings.settings.screen_analysis_provider", "")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.screen_analysis_provider", "")
    monkeypatch.setattr("src.observer.screen_analysis_settings.settings.local_vlm_model", "")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.local_vlm_model", "")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.seraph_vlm_base_url", "")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.local_vlm_base_url", "http://gpu:8088")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.httpx.AsyncClient", FakeAsyncClient)
    write_screen_analysis_settings(
        {
            "enabled": True,
            "provider": "local-vlm",
            "model": "gemma-from-ui",
            "archive_dir": str(tmp_path / "archive"),
        }
    )

    assert screenshot_semantic_analysis_enabled() is True

    analysis = await analyze_screenshot_image(image, {})

    assert analysis is not None
    assert calls[0]["endpoint"] == "http://gpu:8088/v1/analyze-file"
    assert calls[0]["data"]["model"] == "gemma-from-ui"


async def test_local_vlm_analyzer_honors_persisted_disabled_toggle(tmp_path, monkeypatch):
    from src.observer.screen_analysis_settings import write_screen_analysis_settings
    from src.observer.screenshot_semantic_analysis import screenshot_semantic_analysis_enabled

    image = tmp_path / "capture.png"
    image.write_bytes(b"png bytes")
    calls = []

    class FakeAsyncClient:
        def __init__(self, *, timeout, follow_redirects=False):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, endpoint, *, data, files, headers):
            calls.append({"endpoint": endpoint, "data": data, "headers": headers})
            raise AssertionError("disabled semantic analysis should not call the VLM")

    monkeypatch.setattr("src.observer.screen_analysis_settings.settings.workspace_dir", str(tmp_path))
    monkeypatch.setattr("src.observer.screen_analysis_settings.settings.screen_analysis_provider", "")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.screen_analysis_provider", "")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.local_vlm_base_url", "http://gpu:8088")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.httpx.AsyncClient", FakeAsyncClient)
    write_screen_analysis_settings(
        {
            "enabled": False,
            "provider": "local-vlm",
            "model": "gemma-from-ui",
            "archive_dir": str(tmp_path / "archive"),
        }
    )

    assert screenshot_semantic_analysis_enabled() is False

    analysis = await analyze_screenshot_image(image, {})

    assert analysis is None
    assert calls == []


async def test_local_vlm_background_capacity_requires_free_worker(monkeypatch):
    payloads = [
        {"active": 0, "queued": 0, "workers": 1, "background_workers": 1},
        {"active": 1, "queued": 0, "workers": 1, "background_workers": 1},
        {"active": 1, "queued": 1, "workers": 1, "background_workers": 1},
    ]
    calls = []

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, *, timeout, follow_redirects=False):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, endpoint):
            calls.append({"endpoint": endpoint, "timeout": self.timeout})
            if endpoint.endswith("/health/backend"):
                return FakeResponse({"status": "ok"})
            return FakeResponse(payloads.pop(0))

    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.screen_analysis_provider", "local-vlm")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.seraph_vlm_base_url", "")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.local_vlm_base_url", "http://gpu:8088")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.seraph_vlm_feeder_window", 2)
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.httpx.AsyncClient", FakeAsyncClient)

    assert await screenshot_semantic_analysis_accepting_background_work() is True
    assert await screenshot_semantic_analysis_accepting_background_work() is True
    assert await screenshot_semantic_analysis_accepting_background_work() is False
    assert calls == [
        {"endpoint": "http://gpu:8088/queue/status", "timeout": 2.0},
        {"endpoint": "http://gpu:8088/health/backend", "timeout": 2.0},
        {"endpoint": "http://gpu:8088/queue/status", "timeout": 2.0},
        {"endpoint": "http://gpu:8088/health/backend", "timeout": 2.0},
        {"endpoint": "http://gpu:8088/queue/status", "timeout": 2.0},
        {"endpoint": "http://gpu:8088/health/backend", "timeout": 2.0},
    ]


async def test_local_vlm_background_capacity_requires_backend_health(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, *, timeout, follow_redirects=False):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, endpoint):
            calls.append(endpoint)
            if endpoint.endswith("/health/backend"):
                return FakeResponse(502, {"status": "error"})
            return FakeResponse(200, {"queue": {"active": 0, "queued": 0, "workers": 1}})

    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.screen_analysis_provider", "local-vlm")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.seraph_vlm_base_url", "")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.local_vlm_base_url", "http://gpu:8088")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.seraph_vlm_feeder_window", 2)
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.httpx.AsyncClient", FakeAsyncClient)

    assert await screenshot_semantic_analysis_accepting_background_work() is False
    assert calls == ["http://gpu:8088/queue/status", "http://gpu:8088/health/backend"]


async def test_local_vlm_background_capacity_normalizes_nested_queue_payload(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, *, timeout, follow_redirects=False):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, endpoint):
            calls.append(endpoint)
            if endpoint.endswith("/health/backend"):
                return FakeResponse({"status": "ok"})
            return FakeResponse({"queue": {"active": 1, "queued": 0, "workers": 1}})

    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.screen_analysis_provider", "local-vlm")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.seraph_vlm_base_url", "")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.local_vlm_base_url", "http://gpu:8088")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.seraph_vlm_feeder_window", 2)
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.httpx.AsyncClient", FakeAsyncClient)

    assert await screenshot_semantic_analysis_background_slots() == 1
    assert await screenshot_semantic_analysis_accepting_background_work() is True
    assert calls == [
        "http://gpu:8088/queue/status",
        "http://gpu:8088/health/backend",
        "http://gpu:8088/queue/status",
        "http://gpu:8088/health/backend",
    ]
