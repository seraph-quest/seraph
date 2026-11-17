"""Bounded age and size pruning of the private daemon capture upload store (#1263).

Filesystem only, under ``tmp_path``. File times are fixed with ``os.utime`` and
``now`` is injected; no clock is read and nothing waits.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.observer import local_capture_upload
from src.observer.local_capture_upload import (
    LOCAL_CAPTURE_UPLOAD_DIRNAME,
    prune_local_capture_uploads,
)

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def _store(shots: Path) -> Path:
    root = shots / LOCAL_CAPTURE_UPLOAD_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write(path: Path, size: int, *, age: timedelta) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = (NOW - age).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def _five_captures(shots: Path) -> list[Path]:
    day = _store(shots) / "2026-11-17"
    # Oldest first: cap-0 is five minutes old, cap-4 one minute old.
    return [_write(day / f"cap-{index}.png", 400, age=timedelta(minutes=5 - index)) for index in range(5)]


def test_cap_removes_oldest_until_at_or_under_cap(tmp_path):
    files = _five_captures(tmp_path)

    result = prune_local_capture_uploads(retention_days=None, max_bytes=1000, screenshot_root=tmp_path, now=NOW)

    assert [path.exists() for path in files] == [False, False, False, True, True]
    assert result.deleted_files == 3
    assert result.deleted_for_size == 3
    assert result.deleted_expired == 0
    assert result.freed_bytes == 1200
    assert result.remaining_bytes == 800
    assert result.remaining_files == 2
    assert result.over_cap is False
    assert result.skipped_reason is None


def test_zero_cap_without_retention_removes_nothing(tmp_path):
    files = _five_captures(tmp_path)

    result = prune_local_capture_uploads(retention_days=None, max_bytes=0, screenshot_root=tmp_path, now=NOW)

    assert all(path.exists() for path in files)
    assert result.deleted_files == 0
    assert result.freed_bytes == 0


def test_operator_images_are_neither_counted_nor_deleted(tmp_path):
    day = _store(tmp_path) / "2026-11-17"
    stored = _write(day / "cap-a.png", 400, age=timedelta(minutes=1))
    operator_root = _write(tmp_path / "Screenshot 2026-11-16.png", 10_000, age=timedelta(days=30))
    sibling = _write(tmp_path / "sibling" / "mine.png", 10_000, age=timedelta(days=30))

    result = prune_local_capture_uploads(retention_days=None, max_bytes=500, screenshot_root=tmp_path, now=NOW)

    assert stored.exists()
    assert operator_root.read_bytes() == b"x" * 10_000
    assert sibling.read_bytes() == b"x" * 10_000
    assert result.deleted_files == 0
    assert result.remaining_bytes == 400
    assert result.over_cap is False


def test_symlinks_are_not_followed_counted_or_removed(tmp_path):
    store = _store(tmp_path)
    outside = tmp_path / "outside"
    target_dir = outside / "day"
    victim_in_dir = _write(target_dir / "victim.png", 5_000, age=timedelta(days=400))
    victim_file = _write(outside / "operator.png", 5_000, age=timedelta(days=400))
    (store / "2026-01-01").symlink_to(target_dir, target_is_directory=True)
    real_day = store / "2026-11-17"
    real_day.mkdir()
    (real_day / "linked.png").symlink_to(victim_file)
    kept = _write(real_day / "cap-a.png", 100, age=timedelta(minutes=1))

    result = prune_local_capture_uploads(retention_days=1, max_bytes=50, screenshot_root=tmp_path, now=NOW)

    assert victim_in_dir.read_bytes() == b"x" * 5_000
    assert victim_file.read_bytes() == b"x" * 5_000
    assert (store / "2026-01-01").is_symlink()
    assert (real_day / "linked.png").is_symlink()
    # Only the real capture was a candidate; it is deleted for size.
    assert not kept.exists()
    assert result.deleted_files == 1
    assert result.freed_bytes == 100
    assert result.remaining_bytes == 0


def test_symlinked_store_root_is_refused(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    victim = _write(elsewhere / "2026-11-17" / "victim.png", 5_000, age=timedelta(days=400))
    tmp_path.joinpath(LOCAL_CAPTURE_UPLOAD_DIRNAME).symlink_to(elsewhere, target_is_directory=True)

    result = prune_local_capture_uploads(retention_days=1, max_bytes=10, screenshot_root=tmp_path, now=NOW)

    assert result.skipped_reason == "capture_store_unsafe"
    assert result.deleted_files == 0
    assert victim.exists()


def test_equal_mtimes_are_deleted_in_path_order(tmp_path):
    store = _store(tmp_path)
    names = ["2026-11-17/cap-b.png", "2026-11-17/cap-a.png", "2026-11-16/cap-z.png"]
    paths = [_write(store / name, 300, age=timedelta(minutes=1)) for name in names]

    result = prune_local_capture_uploads(retention_days=None, max_bytes=300, screenshot_root=tmp_path, now=NOW)

    assert result.deleted_for_size == 2
    # Path order: 2026-11-16/cap-z, then 2026-11-17/cap-a; 2026-11-17/cap-b stays.
    assert [path.exists() for path in paths] == [True, False, False]


def test_partial_is_age_pruned_but_never_size_pruned(tmp_path):
    day = _store(tmp_path) / "2026-11-17"
    fresh_partial = _write(day / ".upload-fresh.partial", 900, age=timedelta(minutes=10))
    expired_partial = _write(day / ".upload-old.partial", 900, age=timedelta(days=40))
    capture = _write(day / "cap-a.png", 100, age=timedelta(minutes=1))

    result = prune_local_capture_uploads(retention_days=30, max_bytes=500, screenshot_root=tmp_path, now=NOW)

    assert not expired_partial.exists()
    assert fresh_partial.exists()
    # The in-flight write still counts toward the total, so the capture goes
    # and the store reports it is still over the cap.
    assert not capture.exists()
    assert result.deleted_expired == 1
    assert result.deleted_for_size == 1
    assert result.remaining_bytes == 900
    assert result.over_cap is True


def test_age_pass_runs_before_size_pass(tmp_path):
    store = _store(tmp_path)
    expired = _write(store / "2026-09-01" / "cap-old.png", 400, age=timedelta(days=60))
    middle = _write(store / "2026-11-17" / "cap-mid.png", 400, age=timedelta(minutes=3))
    newest = _write(store / "2026-11-17" / "cap-new.png", 400, age=timedelta(minutes=1))

    result = prune_local_capture_uploads(retention_days=30, max_bytes=800, screenshot_root=tmp_path, now=NOW)

    # The expired file is removed by age and does not count toward the cap,
    # so the two remaining files fit exactly.
    assert not expired.exists()
    assert middle.exists() and newest.exists()
    assert result.deleted_expired == 1
    assert result.deleted_for_size == 0
    assert result.remaining_bytes == 800
    assert result.over_cap is False
    assert not (store / "2026-09-01").exists()


def test_delete_bound_reports_over_cap_and_next_run_finishes(tmp_path):
    files = _five_captures(tmp_path)

    first = prune_local_capture_uploads(
        retention_days=None, max_bytes=400, screenshot_root=tmp_path, now=NOW, max_files=2
    )
    assert first.deleted_files == 2
    assert first.over_cap is True
    assert [path.exists() for path in files] == [False, False, True, True, True]

    second = prune_local_capture_uploads(
        retention_days=None, max_bytes=400, screenshot_root=tmp_path, now=NOW, max_files=2
    )
    assert second.deleted_files == 2
    assert second.over_cap is False
    assert [path.exists() for path in files] == [False, False, False, False, True]


def test_scan_bound_reports_over_cap(tmp_path):
    _five_captures(tmp_path)

    result = prune_local_capture_uploads(
        retention_days=None, max_bytes=10_000, screenshot_root=tmp_path, now=NOW, max_scan=3
    )

    assert result.deleted_files == 0
    assert result.over_cap is True


def test_file_vanishing_before_unlink_counts_as_freed(tmp_path, monkeypatch):
    files = _five_captures(tmp_path)
    real_unlink = os.unlink
    raced: list[str] = []

    def racing_unlink(path, *args, **kwargs):
        if not raced:
            raced.append(str(path))
            real_unlink(path)
            raise FileNotFoundError(path)
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(local_capture_upload.os, "unlink", racing_unlink)

    result = prune_local_capture_uploads(retention_days=None, max_bytes=1000, screenshot_root=tmp_path, now=NOW)

    assert raced == [str(files[0])]
    assert result.deleted_files == 3
    assert result.freed_bytes == 1200
    assert [path.exists() for path in files] == [False, False, False, True, True]


def test_unlink_error_keeps_bytes_and_continues(tmp_path, monkeypatch):
    files = _five_captures(tmp_path)
    real_unlink = os.unlink

    def refusing_unlink(path, *args, **kwargs):
        if Path(path) == files[0]:
            raise PermissionError(path)
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(local_capture_upload.os, "unlink", refusing_unlink)

    result = prune_local_capture_uploads(retention_days=None, max_bytes=1000, screenshot_root=tmp_path, now=NOW)

    # The undeletable oldest file stays counted; the next oldest go instead.
    assert [path.exists() for path in files] == [True, False, False, False, True]
    assert result.deleted_files == 3
    assert result.remaining_bytes == 800
    assert result.over_cap is False
