"""#1179: one-shot removal of the retired voice-input quarantine directory."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.guardian import retired_audio_quarantine as quarantine


@pytest.fixture
def temp_base(tmp_path, monkeypatch):
    base = tmp_path / "tmpbase"
    base.mkdir()
    monkeypatch.setattr(quarantine.tempfile, "gettempdir", lambda: str(base))
    return base


def _populate(root: Path) -> None:
    job = root / "job-1"
    job.mkdir(parents=True)
    (job / "raw.webm").write_bytes(b"raw")
    (job / "normalized.wav").write_bytes(b"wav")
    (root / "stray.bin").write_bytes(b"x")


def test_default_root_is_fixed_under_resolved_tempdir(temp_base):
    assert quarantine.default_quarantine_root() == Path(os.path.realpath(temp_base)) / "seraph-audio"


def test_absent_root_is_a_noop(temp_base):
    assert quarantine.cleanup_retired_audio_quarantine_at_startup() == {"status": "absent", "files": 0, "directories": 0}


def test_removes_regular_files_then_empty_directories(temp_base):
    root = quarantine.default_quarantine_root()
    _populate(root)
    result = quarantine.cleanup_retired_audio_quarantine_at_startup()
    assert result == {"status": "removed", "files": 3, "directories": 2}
    assert not root.exists()
    assert temp_base.exists()


def test_symlinked_root_is_refused_and_target_untouched(temp_base, tmp_path):
    target = tmp_path / "elsewhere"
    _populate(target)
    (Path(os.path.realpath(temp_base)) / "seraph-audio").symlink_to(target, target_is_directory=True)
    result = quarantine.cleanup_retired_audio_quarantine_at_startup()
    assert result == {"status": "refused_symlink", "files": 0, "directories": 0}
    assert (target / "job-1" / "raw.webm").exists()


@pytest.mark.parametrize("kind", ["file", "dir"])
def test_any_symlink_entry_refuses_all_deletion(temp_base, tmp_path, kind):
    root = quarantine.default_quarantine_root()
    _populate(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_bytes(b"keep")
    if kind == "file":
        (root / "job-1" / "link").symlink_to(outside / "keep.txt")
    else:
        (root / "link-dir").symlink_to(outside, target_is_directory=True)
    result = quarantine.cleanup_retired_audio_quarantine_at_startup()
    assert result == {"status": "refused_symlink", "files": 0, "directories": 0}
    assert (root / "job-1" / "raw.webm").exists() and (outside / "keep.txt").exists()


def test_failure_never_raises(temp_base, monkeypatch):
    root = quarantine.default_quarantine_root()
    _populate(root)
    def boom(self, *args, **kwargs):
        raise PermissionError("denied")
    monkeypatch.setattr(Path, "unlink", boom)
    result = quarantine.cleanup_retired_audio_quarantine_at_startup()
    assert result["status"] == "failed:PermissionError"
