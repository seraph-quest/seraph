"""Bounded pruning of the daemon's local capture archive (#1263).

Pure filesystem tests under ``tmp_path``: no PyObjC, so they run on any OS.
File times are fixed with ``os.utime`` and ``now`` is injected.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from capture_archive import ARCHIVE_FILE_RE, prune_capture_archive  # noqa: E402

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def _file(path: Path, size: int, *, age: timedelta) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = (NOW - age).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def _capture(root: Path, day: str, index: int, *, age: timedelta, png: int = 300, legacy: bool = False) -> list[Path]:
    stem = f"1200{index:02d}-code-{index:016x}"
    sidecar = f"{stem}.codex.txt" if legacy else f"{stem}.apple-vision.json"
    return [
        _file(root / day / f"{stem}.png", png, age=age),
        _file(root / day / sidecar, 50, age=age),
        _file(root / day / f"{stem}.analysis.json", 50, age=age),
    ]


def test_cap_removes_oldest_capture_groups_whole(tmp_path):
    groups = [_capture(tmp_path, "2026-11-17", index, age=timedelta(minutes=10 - index)) for index in range(4)]

    result = prune_capture_archive(str(tmp_path), max_bytes=800, retention_days=None, now=NOW)

    # Each group is 400 bytes: two remain, the two oldest go with their sidecars.
    assert [all(path.exists() for path in group) for group in groups] == [False, False, True, True]
    assert not any(path.exists() for group in groups[:2] for path in group)
    assert result.status == "ok"
    assert result.deleted_captures == 2
    assert result.deleted_files == 6
    assert result.freed_bytes == 800
    assert result.remaining_bytes == 800
    assert result.over_cap is False


def test_zero_cap_removes_nothing(tmp_path):
    groups = [_capture(tmp_path, "2026-11-17", index, age=timedelta(days=900)) for index in range(3)]

    result = prune_capture_archive(str(tmp_path), max_bytes=0, retention_days=None, now=NOW)

    assert all(path.exists() for group in groups for path in group)
    assert result.status == "skipped"
    assert result.deleted_captures == 0


def test_retention_removes_groups_older_than_cutoff_and_keeps_the_boundary(tmp_path):
    expired = _capture(tmp_path, "2026-08-01", 1, age=timedelta(days=31))
    boundary = _capture(tmp_path, "2026-10-18", 2, age=timedelta(days=30))
    fresh = _capture(tmp_path, "2026-11-17", 3, age=timedelta(hours=1))

    result = prune_capture_archive(str(tmp_path), max_bytes=0, retention_days=30, now=NOW)

    assert not any(path.exists() for path in expired)
    assert all(path.exists() for path in boundary + fresh)
    assert not (tmp_path / "2026-08-01").exists()
    assert result.deleted_captures == 1
    assert result.deleted_files == 3


def test_unknown_files_are_neither_counted_nor_deleted(tmp_path):
    day = tmp_path / "2026-11-17"
    others = [
        _file(day / "notes.txt", 5_000, age=timedelta(days=900)),
        _file(day / "Screenshot 2026-11-16.png", 5_000, age=timedelta(days=900)),
        _file(tmp_path / "not-a-day" / "120000-code-0000000000000001.png", 5_000, age=timedelta(days=900)),
        _file(tmp_path / "120000-code-0000000000000002.png", 5_000, age=timedelta(days=900)),
    ]
    group = _capture(tmp_path, "2026-11-17", 3, age=timedelta(minutes=1))

    result = prune_capture_archive(str(tmp_path), max_bytes=500, retention_days=1, now=NOW)

    assert all(path.read_bytes() == b"x" * 5_000 for path in others)
    assert all(path.exists() for path in group)
    assert result.deleted_captures == 0
    assert result.remaining_bytes == 400


def test_symlinked_day_and_file_are_untouched(tmp_path):
    archive = tmp_path / "archive"
    outside = tmp_path / "outside"
    linked_group = _capture(outside, "real-day", 1, age=timedelta(days=900))
    victim = _file(outside / "operator.png", 5_000, age=timedelta(days=900))
    archive.mkdir()
    (archive / "2026-01-01").symlink_to(outside / "real-day", target_is_directory=True)
    day = archive / "2026-11-17"
    day.mkdir()
    (day / "120000-code-00000000000000aa.png").symlink_to(victim)

    result = prune_capture_archive(str(archive), max_bytes=1, retention_days=1, now=NOW)

    assert all(path.exists() for path in linked_group)
    assert victim.read_bytes() == b"x" * 5_000
    assert (archive / "2026-01-01").is_symlink()
    assert (day / "120000-code-00000000000000aa.png").is_symlink()
    assert result.deleted_captures == 0
    assert result.remaining_bytes == 0


def test_missing_root_and_home_root_are_skipped(tmp_path, monkeypatch):
    missing = prune_capture_archive(str(tmp_path / "missing"), max_bytes=1, retention_days=1, now=NOW)
    assert missing.status == "skipped"
    assert missing.reason == "archive_missing"

    home = tmp_path / "home"
    group = _capture(home, "2026-01-01", 1, age=timedelta(days=900))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    refused = prune_capture_archive(str(home), max_bytes=1, retention_days=1, now=NOW)
    assert refused.status == "skipped"
    assert refused.reason == "archive_root_refused"
    assert all(path.exists() for path in group)


def test_delete_bound_reports_over_cap(tmp_path):
    groups = [_capture(tmp_path, "2026-11-17", index, age=timedelta(minutes=10 - index)) for index in range(4)]

    result = prune_capture_archive(str(tmp_path), max_bytes=400, retention_days=None, now=NOW, max_delete=1)

    assert result.deleted_captures == 1
    assert result.over_cap is True
    assert [all(path.exists() for path in group) for group in groups] == [False, True, True, True]


def test_legacy_codex_sidecar_is_grouped_with_its_png(tmp_path):
    legacy = _capture(tmp_path, "2026-11-01", 1, age=timedelta(days=16), legacy=True)
    current = _capture(tmp_path, "2026-11-17", 2, age=timedelta(minutes=1))

    result = prune_capture_archive(str(tmp_path), max_bytes=400, retention_days=None, now=NOW)

    assert not any(path.exists() for path in legacy)
    assert all(path.exists() for path in current)
    assert result.deleted_files == 3


def test_archive_file_pattern_accepts_writer_names_and_rejects_others():
    stem = "235959-visual-studio-code-0123456789abcdef"
    for name in (f"{stem}.png", f"{stem}.analysis.json", f"{stem}.apple-vision.json", f"{stem}.codex.txt"):
        assert ARCHIVE_FILE_RE.fullmatch(name), name
    for name in ("Screenshot 2026.png", f"{stem}.jpg", f"x{stem}.png", "235959-code-0123.png"):
        assert ARCHIVE_FILE_RE.fullmatch(name) is None, name


def test_age_pass_cut_short_by_delete_bound_reports_over_cap(tmp_path):
    groups = [_capture(tmp_path, "2026-01-01", index, age=timedelta(days=300, minutes=index)) for index in range(3)]

    result = prune_capture_archive(str(tmp_path), max_bytes=0, retention_days=30, now=NOW, max_delete=2)

    assert result.deleted_captures == 2
    assert sum(all(path.exists() for path in group) for group in groups) == 1
    assert result.over_cap is True


def test_truncated_scan_never_deletes_a_partial_group(tmp_path):
    # Names sort: <stem>.analysis.json, <stem>.apple-vision.json, <stem>.png.
    # Scan entries: 1 day dir + 3 files of group 0 + 2 files of group 1 = 6,
    # so group 1's PNG is never seen.
    groups = [_capture(tmp_path, "2026-01-01", index, age=timedelta(days=300 - index)) for index in range(2)]

    result = prune_capture_archive(str(tmp_path), max_bytes=1, retention_days=30, now=NOW, max_scan=6)

    assert not any(path.exists() for path in groups[0])
    assert all(path.exists() for path in groups[1])
    assert result.deleted_captures == 1
    assert result.over_cap is True


def test_only_day_directories_emptied_by_this_run_are_removed(tmp_path):
    already_empty = tmp_path / "2026-01-02"
    already_empty.mkdir()
    group = _capture(tmp_path, "2026-01-01", 1, age=timedelta(days=300))

    prune_capture_archive(str(tmp_path), max_bytes=0, retention_days=30, now=NOW)

    assert not any(path.exists() for path in group)
    assert not (tmp_path / "2026-01-01").exists()
    assert already_empty.is_dir()
