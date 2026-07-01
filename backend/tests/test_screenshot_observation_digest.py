"""Tests for rolling screenshot observation digests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import select

import src.db.models  # noqa: F401
from src.db.models import MemoryEpisode, ScreenObservation


@pytest.fixture(autouse=True)
def allow_remote_screen_llm_for_existing_digest_tests():
    with patch("src.scheduler.screen_llm_policy.settings.screen_derived_llm_allow_remote", True):
        yield


@pytest.mark.asyncio
async def test_screenshot_digest_groups_window_and_links_evidence(async_db):
    from src.scheduler.jobs.screenshot_observation_digest import build_screenshot_observation_digest

    start = datetime(2026, 6, 30, 9, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 30, 9, 30, tzinfo=timezone.utc)
    async with async_db() as db:
        db.add(
            _observation(
                timestamp=datetime(2026, 6, 30, 9, 5, tzinfo=timezone.utc),
                image_sha256="hash-one",
                summary="Working on Seraph screenshot digest with api_key=super-secret-token",
                analysis={
                    "summary": "Implemented rolling screenshot digest.",
                    "activity_type": "coding",
                    "project": "seraph",
                    "key_visible_text": ["digest job", "api_key=super-secret-token"],
                    "confidence": 0.8,
                    "goal_alignment": {
                        "status": "aligned",
                        "evidence": ["Digest code is visible."],
                        "needle_movement": "pushed",
                    },
                },
            )
        )
        db.add(
            _observation(
                timestamp=datetime(2026, 6, 30, 9, 20, tzinfo=timezone.utc),
                image_sha256="hash-two",
                summary="Reviewing blocked provider retry",
                analysis={
                    "summary": "Reviewed failed provider retry.",
                    "activity_type": "reviewing",
                    "project": "seraph",
                    "key_visible_text": ["provider retry"],
                    "confidence": 0.6,
                    "goal_alignment": {
                        "status": "blocked",
                        "evidence": ["Provider unavailable."],
                        "needle_movement": "blocked",
                    },
                },
            )
        )
        db.add(
            _observation(
                timestamp=datetime(2026, 6, 30, 9, 45, tzinfo=timezone.utc),
                image_sha256="hash-outside",
                summary="Outside the digest window",
            )
        )

    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content="LLM digest: provider retry was blocked."))]

    with patch(
        "src.scheduler.jobs.screenshot_observation_digest.completion_with_fallback",
        new=AsyncMock(return_value=response),
    ) as completion:
        result = await build_screenshot_observation_digest(window_start=start, window_end=end)

    assert result.status == "created"
    assert result.observation_count == 2
    completion.assert_awaited_once()
    prompt = completion.await_args.kwargs["messages"][0]["content"]
    assert completion.await_args.kwargs["runtime_path"] == "screenshot_observation_digest"
    assert completion.await_args.kwargs["local_runtime_only"] is False
    assert "api_key=super-secret-token" in prompt
    async with async_db() as db:
        episodes = list((await db.execute(select(MemoryEpisode))).scalars().all())
    assert len(episodes) == 1
    episode = episodes[0]
    metadata = json.loads(episode.metadata_json or "{}")
    assert metadata["kind"] == "screenshot_observation_digest"
    assert metadata["observation_count"] == 2
    assert len(metadata["observation_ids"]) == 2
    assert episode.content == "LLM digest: provider retry was blocked."


@pytest.mark.asyncio
async def test_screenshot_digest_blocks_without_local_profile_or_remote_opt_in(async_db):
    from src.scheduler.jobs.screenshot_observation_digest import build_screenshot_observation_digest

    start = datetime(2026, 6, 30, 9, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 30, 9, 30, tzinfo=timezone.utc)
    async with async_db() as db:
        db.add(
            _observation(
                timestamp=datetime(2026, 6, 30, 9, 5, tzinfo=timezone.utc),
                image_sha256="hash-private",
                summary="Private screenshot-derived observation",
            )
        )

    with patch("src.scheduler.screen_llm_policy.settings.screen_derived_llm_allow_remote", False), patch(
        "src.scheduler.jobs.screenshot_observation_digest.completion_with_fallback",
        new=AsyncMock(),
    ) as completion:
        result = await build_screenshot_observation_digest(window_start=start, window_end=end)

    assert result.status == "blocked"
    completion.assert_not_awaited()
    async with async_db() as db:
        episodes = list((await db.execute(select(MemoryEpisode))).scalars().all())
    assert episodes == []


@pytest.mark.asyncio
async def test_screenshot_digest_blocks_generic_local_profile_even_with_safe_proof(async_db):
    from src.scheduler.jobs.screenshot_observation_digest import build_screenshot_observation_digest

    start = datetime(2026, 6, 30, 9, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 30, 9, 30, tzinfo=timezone.utc)
    async with async_db() as db:
        db.add(
            _observation(
                timestamp=datetime(2026, 6, 30, 9, 5, tzinfo=timezone.utc),
                image_sha256="hash-generic-local",
                summary="Private screenshot-derived observation",
            )
        )

    with patch("src.scheduler.screen_llm_policy.settings.screen_derived_llm_allow_remote", False), patch(
        "src.scheduler.screen_llm_policy.settings.runtime_profile_preferences",
        "screenshot_observation_digest=local",
    ), patch(
        "src.scheduler.screen_llm_policy.settings.local_model",
        "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF",
    ), patch(
        "src.scheduler.screen_llm_policy.settings.local_llm_api_base",
        "http://127.0.0.1:8000/v1",
    ), patch(
        "src.scheduler.screen_llm_policy.latest_local_runtime_profile_proof",
        return_value={"safe_for_single_backend_profile_routing": True},
    ), patch(
        "src.scheduler.jobs.screenshot_observation_digest.completion_with_fallback",
        new=AsyncMock(),
    ) as completion:
        result = await build_screenshot_observation_digest(window_start=start, window_end=end)

    assert result.status == "blocked"
    completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_screenshot_digest_is_idempotent_and_updates_when_window_changes(async_db):
    from src.scheduler.jobs.screenshot_observation_digest import build_screenshot_observation_digest

    start = datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 30, 10, 30, tzinfo=timezone.utc)
    async with async_db() as db:
        db.add(
            _observation(
                timestamp=datetime(2026, 6, 30, 10, 5, tzinfo=timezone.utc),
                image_sha256="hash-a",
                summary="First digest observation",
            )
        )

    first_response = MagicMock()
    first_response.choices = [MagicMock(message=MagicMock(content="LLM digest v1"))]
    second_response = MagicMock()
    second_response.choices = [MagicMock(message=MagicMock(content="LLM digest v2"))]
    completion = AsyncMock(side_effect=[first_response, second_response])

    with patch(
        "src.scheduler.jobs.screenshot_observation_digest.completion_with_fallback",
        new=completion,
    ):
        created = await build_screenshot_observation_digest(window_start=start, window_end=end)
        unchanged = await build_screenshot_observation_digest(window_start=start, window_end=end)
    async with async_db() as db:
        db.add(
            _observation(
                timestamp=datetime(2026, 6, 30, 10, 15, tzinfo=timezone.utc),
                image_sha256="hash-b",
                summary="Second digest observation",
            )
        )
    with patch(
        "src.scheduler.jobs.screenshot_observation_digest.completion_with_fallback",
        new=completion,
    ):
        updated = await build_screenshot_observation_digest(window_start=start, window_end=end)

    assert created.status == "created"
    assert unchanged.status == "unchanged"
    assert updated.status == "updated"
    assert created.episode_id == unchanged.episode_id == updated.episode_id
    assert completion.await_count == 2
    async with async_db() as db:
        episodes = list((await db.execute(select(MemoryEpisode))).scalars().all())
    assert len(episodes) == 1
    metadata = json.loads(episodes[0].metadata_json or "{}")
    assert metadata["observation_count"] == 2


@pytest.mark.asyncio
async def test_screenshot_digest_respects_content_size_limit(async_db, monkeypatch):
    from src.scheduler.jobs.screenshot_observation_digest import build_screenshot_observation_digest

    start = datetime(2026, 6, 30, 11, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 30, 11, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "src.scheduler.jobs.screenshot_observation_digest.settings.screenshot_observation_digest_max_chars",
        500,
    )
    async with async_db() as db:
        for index in range(12):
            db.add(
                _observation(
                    timestamp=datetime(2026, 6, 30, 11, index, tzinfo=timezone.utc),
                    image_sha256=f"hash-{index}",
                    summary=f"Long digest observation {index} " + ("useful context " * 20),
                )
            )

    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content="LLM digest ignores Python size shaping."))]

    with patch(
        "src.scheduler.jobs.screenshot_observation_digest.completion_with_fallback",
        new=AsyncMock(return_value=response),
    ) as completion:
        result = await build_screenshot_observation_digest(window_start=start, window_end=end)

    assert result.status == "created"
    prompt = completion.await_args.kwargs["messages"][0]["content"]
    assert len(prompt) >= 500
    async with async_db() as db:
        episode = (await db.execute(select(MemoryEpisode))).scalar_one()
    assert episode.content == "LLM digest ignores Python size shaping."


def _observation(
    *,
    timestamp: datetime,
    image_sha256: str,
    summary: str,
    analysis: dict | None = None,
) -> ScreenObservation:
    analysis_payload = {
        "schema_version": "seraph.screenshot_analysis.v1",
        "prompt_version": "seraph.screenshot_analysis.prompt.v1",
        "summary": summary,
        "detailed_observations": [],
        "activity_type": "coding",
        "project": "seraph",
        "applications": ["editor"],
        "visible_artifacts": [],
        "key_visible_text": [],
        "user_intent": "unknown",
        "goal_alignment": {
            "status": "unknown",
            "goal_refs": [],
            "evidence": [],
            "needle_movement": "unknown",
        },
        "confidence": 0.5,
        "sensitive_content_seen": False,
        "privacy_notes": [],
        "report_tags": [],
    }
    if analysis:
        analysis_payload.update(analysis)
    details = [
        f"screenshot_folder_image_sha256:{image_sha256}",
        "capture_artifacts:"
        + json.dumps(
            {
                "id": image_sha256[:16],
                "provider": "screenshot_folder",
                "source": "local_image_directory",
                "created_at": timestamp.isoformat(),
                "screenshot_folder": "/tmp/screenshots",
                "image_path": f"/tmp/screenshots/{image_sha256}.png",
                "image_sha256": image_sha256,
                "image_bytes": 10,
                "file_format": "png",
                "width": 1,
                "height": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        "screenshot_analysis:" + json.dumps(analysis_payload, sort_keys=True, separators=(",", ":")),
        "screenshot_analysis_status:"
        + json.dumps(
            {
                "status": "succeeded",
                "provider": "local-vlm",
                "model": "gemma",
                "schema_version": "seraph.screenshot_analysis.v1",
                "prompt_version": "seraph.screenshot_analysis.prompt.v1",
                "recorded_at": timestamp.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    ]
    return ScreenObservation(
        timestamp=timestamp,
        app_name="Screenshot Folder",
        window_title=f"{image_sha256}.png",
        activity_type="screen",
        project=None,
        summary=summary,
        details_json=json.dumps(details),
        blocked=False,
    )
