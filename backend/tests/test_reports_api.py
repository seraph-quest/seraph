"""Read-only end-of-day report API: listing, detail, blocked projection, privacy."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from config.settings import settings
from src.db.models import MemoryEpisodeType
from src.memory.repository import memory_repository

BASE = datetime(2026, 11, 10, 22, 0, tzinfo=timezone.utc)


def _report(date: str, *, provider: str = "llm:openrouter:test-model", body: str | None = None) -> dict:
    return {
        "date": date,
        "timezone": "Europe/Berlin",
        "body": body or f"Report body for {date}\n\nGoals vs day: aligned.",
        "summary": {
            "date": date,
            "timezone": "Europe/Berlin",
            "total_observations": 4,
            "total_tracked_minutes": 95,
            "switch_count": 4,
            "by_activity": {"coding": 4200, "browsing": 1500},
            "by_project": {"seraph": 4200},
            "by_app": {"PrivateApp": 5700},
            "by_source": {"screenshot_folder": 5700},
            "source_observations": {"screenshot_folder": 4},
            "samples": ["PRIVATE raw screen sample text"],
            "screenshot_samples": ["PRIVATE screenshot sample"],
        },
        "screenshot_digests": {"count": 0, "observation_ids": []},
        "goal_alignment": [],
        "completed_goal_count": 1,
        "active_goal_count": 3,
        "analysis_provider": provider,
        "artifact_schema": "seraph.end_of_day_goal_report.v1",
    }


async def _store(report: dict, archive_dir: Path) -> str:
    from src.scheduler.jobs.end_of_day_goal_report import store_end_of_day_goal_report

    with patch.object(settings, "report_archive_dir", str(archive_dir)):
        return await store_end_of_day_goal_report(report)


async def _seed_report_episode(*, date: str, observed_at: datetime, provider: str = "llm:test", **overrides) -> str:
    metadata = {
        "kind": "end_of_day_goal_report",
        "date": date,
        "timezone": "UTC",
        "total_tracked_minutes": 30,
        "active_goal_count": 2,
        "completed_goal_count": 0,
        "analysis_provider": provider,
        "artifact_schema": "seraph.end_of_day_goal_report.v1",
        "artifacts": {},
    }
    metadata.update(overrides.pop("metadata", {}))
    episode = await memory_repository.create_episode(
        episode_type=overrides.pop("episode_type", MemoryEpisodeType.observer),
        summary=f"End-of-day goal report for {date}",
        content=f"Body {date}",
        source_tool_name=overrides.pop("source_tool_name", "end_of_day_goal_report"),
        metadata=metadata,
        observed_at=observed_at,
    )
    return episode.id


@pytest.fixture
def archive_dir(tmp_path, monkeypatch):
    path = tmp_path / "reports"
    monkeypatch.setattr(settings, "report_archive_dir", str(path))
    return path


@pytest.mark.asyncio
async def test_list_orders_newest_first_and_bounds_limit(client, archive_dir):
    ids = [
        await _seed_report_episode(date=f"2026-11-0{day}", observed_at=BASE + timedelta(days=day))
        for day in range(1, 6)
    ]

    response = await client.get("/api/reports/end-of-day", params={"limit": 3})
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["episode_id"] for item in items] == list(reversed(ids))[:3]
    assert [item["date"] for item in items] == ["2026-11-05", "2026-11-04", "2026-11-03"]
    first = items[0]
    assert first["status"] == "ready"
    assert first["blocked_reason"] is None
    assert first["analysis_provider"] == "llm:test"
    assert first["active_goal_count"] == 2
    assert first["completed_goal_count"] == 0
    assert first["total_tracked_minutes"] == 30
    assert first["timezone"] == "UTC"
    assert first["observed_at"].startswith("2026-11-15T22:00:00")
    assert first["created_at"]

    default = await client.get("/api/reports/end-of-day")
    assert default.status_code == 200
    assert len(default.json()["items"]) == 5

    for bad in (0, 101, -1):
        assert (await client.get("/api/reports/end-of-day", params={"limit": bad})).status_code == 422
    assert (await client.get("/api/reports/end-of-day", params={"limit": 100})).status_code == 200


@pytest.mark.asyncio
async def test_repository_limit_is_clamped_and_skips_lookalikes(async_db):
    report_id = await _seed_report_episode(date="2026-11-01", observed_at=BASE)
    # Newer look-alikes that must never be listed.
    await _seed_report_episode(
        date="2026-11-02", observed_at=BASE + timedelta(days=1), metadata={"kind": "something_else"}
    )
    await _seed_report_episode(
        date="2026-11-03",
        observed_at=BASE + timedelta(days=2),
        metadata={"artifact_schema": "seraph.end_of_day_report_test_email.v1"},
    )
    await _seed_report_episode(
        date="2026-11-04", observed_at=BASE + timedelta(days=3), source_tool_name="other_tool"
    )
    await _seed_report_episode(
        date="2026-11-05", observed_at=BASE + timedelta(days=4), episode_type=MemoryEpisodeType.conversation
    )

    episodes = await memory_repository.list_end_of_day_report_episodes(limit=1)
    assert [episode.id for episode in episodes] == [report_id]
    assert [e.id for e in await memory_repository.list_end_of_day_report_episodes(limit=0)] == [report_id]
    assert [e.id for e in await memory_repository.list_end_of_day_report_episodes(limit=10_000)] == [report_id]


@pytest.mark.asyncio
async def test_blocked_reports_are_projected_as_blocked(client, archive_dir):
    blocked_id = await _store(
        _report(
            "2026-11-09",
            provider="blocked:guardian_owner_unavailable",
            body="End-of-day report analysis was blocked by the guardian runtime (guardian_owner_unavailable).",
        ),
        archive_dir,
    )
    ready_id = await _store(_report("2026-11-10"), archive_dir)

    items = {item["episode_id"]: item for item in (await client.get("/api/reports/end-of-day")).json()["items"]}
    assert items[blocked_id]["status"] == "blocked"
    assert items[blocked_id]["blocked_reason"] == "guardian_owner_unavailable"
    assert items[blocked_id]["analysis_provider"] == "blocked:guardian_owner_unavailable"
    assert items[ready_id]["status"] == "ready"

    detail = (await client.get(f"/api/reports/end-of-day/{blocked_id}")).json()
    assert detail["status"] == "blocked"
    assert detail["blocked_reason"] == "guardian_owner_unavailable"
    assert "blocked by the guardian runtime" in detail["body"]


@pytest.mark.asyncio
async def test_detail_returns_body_summary_and_public_artifacts_without_paths(client, archive_dir):
    episode_id = await _store(_report("2026-11-10"), archive_dir)

    response = await client.get(f"/api/reports/end-of-day/{episode_id}")
    assert response.status_code == 200
    detail = response.json()
    assert detail["episode_id"] == episode_id
    assert detail["date"] == "2026-11-10"
    assert detail["timezone"] == "Europe/Berlin"
    assert detail["status"] == "ready"
    assert detail["analysis_provider"] == "llm:openrouter:test-model"
    assert detail["body"].startswith("Report body for 2026-11-10")
    assert detail["active_goal_count"] == 3
    assert detail["completed_goal_count"] == 1
    assert detail["total_tracked_minutes"] == 95
    assert detail["summary"] == {
        "source": "archive",
        "total_observations": 4,
        "total_tracked_minutes": 95,
        "switch_count": 4,
        "by_activity": {"coding": 4200, "browsing": 1500},
        "by_project": {"seraph": 4200},
        "by_source": {"screenshot_folder": 5700},
    }
    assert detail["artifacts"]["raw_artifact_path_exposed"] is False
    assert detail["artifacts"]["report_text_sha256"]
    assert detail["artifacts"]["report_json_sha256"]

    raw = response.text
    assert str(archive_dir) not in raw
    assert "report_json_path" not in raw and "report_text_path" not in raw
    assert "PRIVATE" not in raw
    assert "PrivateApp" not in raw


@pytest.mark.asyncio
async def test_detail_falls_back_to_metadata_when_archive_does_not_verify(client, archive_dir):
    episode_id = await _store(_report("2026-11-10"), archive_dir)
    json_files = list((archive_dir / "end-of-day").rglob("*.json"))
    assert len(json_files) == 1
    tampered = json.loads(json_files[0].read_text(encoding="utf-8"))
    tampered["report"]["summary"]["by_project"] = {"tampered": 1}
    json_files[0].write_text(json.dumps(tampered), encoding="utf-8")

    detail = (await client.get(f"/api/reports/end-of-day/{episode_id}")).json()
    assert detail["summary"]["source"] == "episode_metadata"
    assert detail["summary"]["total_tracked_minutes"] == 95
    assert detail["summary"]["total_observations"] == 4
    assert detail["summary"]["switch_count"] is None
    assert detail["summary"]["by_project"] == {}


@pytest.mark.asyncio
async def test_detail_ignores_archive_path_outside_report_root(client, archive_dir, tmp_path):
    outside = tmp_path / "outside.json"
    payload = json.dumps(
        {"artifact_schema": "seraph.end_of_day_goal_report.v1", "report": {"summary": {"by_project": {"leak": 9}}}}
    ).encode("utf-8")
    outside.write_bytes(payload)
    import hashlib

    episode_id = await _seed_report_episode(
        date="2026-11-01",
        observed_at=BASE,
        metadata={
            "artifacts": {
                "report_json_path": str(outside),
                "report_json_sha256": hashlib.sha256(payload).hexdigest(),
            }
        },
    )
    detail = (await client.get(f"/api/reports/end-of-day/{episode_id}")).json()
    assert detail["summary"]["source"] == "episode_metadata"
    assert "leak" not in json.dumps(detail)
    assert str(outside) not in json.dumps(detail)


@pytest.mark.asyncio
async def test_detail_404_for_unknown_and_non_report_episodes(client, archive_dir):
    assert (await client.get("/api/reports/end-of-day/does-not-exist")).status_code == 404

    other = await memory_repository.create_episode(
        episode_type=MemoryEpisodeType.observer,
        summary="Some other observer episode",
        content="not a report",
        source_tool_name="screenshot_observation_digest",
        metadata={"kind": "end_of_day_goal_report", "artifact_schema": "seraph.end_of_day_goal_report.v1"},
    )
    assert (await client.get(f"/api/reports/end-of-day/{other.id}")).status_code == 404

    wrong_schema = await _seed_report_episode(
        date="2026-11-01", observed_at=BASE, metadata={"artifact_schema": "seraph.end_of_day_report_test_email.v1"}
    )
    assert (await client.get(f"/api/reports/end-of-day/{wrong_schema}")).status_code == 404

    listed = (await client.get("/api/reports/end-of-day")).json()["items"]
    assert listed == []


@pytest.mark.asyncio
async def test_empty_store_lists_no_reports(client):
    response = await client.get("/api/reports/end-of-day")
    assert response.status_code == 200
    assert response.json() == {"items": [], "limit": 30}


@pytest.mark.asyncio
async def test_unauthenticated_requests_are_denied(client, archive_dir, monkeypatch):
    episode_id = await _seed_report_episode(date="2026-11-01", observed_at=BASE)
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "reports-negative-auth-secret")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")

    listing = await client.get("/api/reports/end-of-day")
    detail = await client.get(f"/api/reports/end-of-day/{episode_id}")
    assert listing.status_code == 401
    assert detail.status_code == 401
    assert "Body 2026-11-01" not in detail.text


async def test_oversized_stored_labels_are_bounded_not_rejected(client, archive_dir):
    # Critic finding 1: one long stored label must not make the cockpit reject
    # the whole list; the API bounds every string it projects.
    await _seed_report_episode(
        date="2026-11-09" + "x" * 100,
        observed_at=BASE,
        provider="llm:" + "m" * 1000,
        metadata={"timezone": "Z" * 500},
    )
    await _seed_report_episode(date="2026-11-08", observed_at=BASE - timedelta(days=1), provider="blocked:" + "r" * 1000)
    response = await client.get("/api/reports/end-of-day")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 2
    assert len(items[0]["analysis_provider"]) <= 256
    assert len(items[0]["date"]) <= 32
    assert len(items[0]["timezone"]) <= 64
    assert items[1]["status"] == "blocked"
    assert len(items[1]["blocked_reason"]) <= 256


def test_mix_keys_truncate_by_utf16_and_merge_collisions():
    from src.api.reports import _MIX_KEY_MAX, _seconds_mix

    emoji_key = "\U0001F600" * 100  # 200 UTF-16 units
    long_a = "p" * 130 + "a"
    long_b = "p" * 130 + "b"
    mix = _seconds_mix({emoji_key: 10, long_a: 5, long_b: 7})
    assert all(len(key.encode("utf-16-le")) // 2 <= _MIX_KEY_MAX for key in mix)
    assert mix["p" * _MIX_KEY_MAX] == 12
