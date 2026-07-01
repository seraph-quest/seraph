#!/usr/bin/env python3
"""Backfill rolling screenshot-observation digests for stored observations."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone

from sqlmodel import col, func, select

from src.db.engine import get_session
from src.db.models import ScreenObservation
from src.scheduler.jobs.screenshot_observation_digest import build_screenshot_observation_digest


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _floor_window(value: datetime, minutes: int) -> datetime:
    timestamp = _ensure_utc(value)
    minute = (timestamp.minute // minutes) * minutes
    return timestamp.replace(minute=minute, second=0, microsecond=0)


async def _observation_bounds() -> tuple[datetime | None, datetime | None, int]:
    async with get_session() as db:
        result = await db.execute(
            select(
                func.min(ScreenObservation.timestamp),
                func.max(ScreenObservation.timestamp),
                func.count(),
            ).where(col(ScreenObservation.app_name) == "Screenshot Folder")
        )
        minimum, maximum, count = result.one()
    return minimum, maximum, int(count or 0)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-minutes", type=int, default=30)
    args = parser.parse_args()
    window_minutes = max(1, min(args.window_minutes, 240))

    minimum, maximum, count = await _observation_bounds()
    if minimum is None or maximum is None or count <= 0:
        print(json.dumps({"status": "empty", "count": count}))
        return 0

    start = _floor_window(_ensure_utc(minimum), window_minutes)
    end = _floor_window(_ensure_utc(maximum), window_minutes) + timedelta(minutes=window_minutes)
    cursor = start
    results = []
    while cursor < end:
        window_end = cursor + timedelta(minutes=window_minutes)
        result = await build_screenshot_observation_digest(window_start=cursor, window_end=window_end)
        results.append(
            {
                "status": result.status,
                "window_start": result.window_start.isoformat(),
                "window_end": result.window_end.isoformat(),
                "observation_count": result.observation_count,
                "episode_id": result.episode_id,
            }
        )
        cursor = window_end

    print(
        json.dumps(
            {
                "status": "ok",
                "observation_count": count,
                "window_start": start.isoformat(),
                "window_end": end.isoformat(),
                "windows": results,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
