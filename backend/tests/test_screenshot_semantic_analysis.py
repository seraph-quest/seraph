"""Tests for provider-backed semantic screenshot analysis."""

from __future__ import annotations

from src.observer.screenshot_semantic_analysis import analyze_screenshot_image


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
        def __init__(self, *, timeout):
            self.timeout = timeout

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
                    "file_bytes": image_file.read(),
                    "media_type": media_type,
                    "headers": headers,
                    "timeout": self.timeout,
                }
            )
            return FakeResponse()

    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.screen_analysis_provider", "local-vlm")
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
    assert "seraph.screenshot_analysis.v1" in calls[0]["data"]["prompt"]
    assert calls[0]["file_name"] == "capture.png"
    assert calls[0]["file_bytes"] == b"png bytes"
    assert calls[0]["media_type"] == "image/png"
    assert calls[0]["headers"] == {"Authorization": "Bearer secret-token"}
    assert calls[0]["timeout"] == 9


async def test_local_vlm_analyzer_is_disabled_without_provider(tmp_path, monkeypatch):
    image = tmp_path / "capture.png"
    image.write_bytes(b"png bytes")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.screen_analysis_provider", "")
    monkeypatch.setattr("src.observer.screenshot_semantic_analysis.settings.local_vlm_base_url", "http://gpu:8088")

    analysis = await analyze_screenshot_image(image, {})

    assert analysis is None
