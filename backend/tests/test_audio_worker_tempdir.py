"""Regression tests for #1157: symlinked platform temp dirs at startup."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from src.guardian.audio_worker import AudioIngressWorker


def _symlinked_tempdir(tmp_path: Path) -> tuple[Path, Path]:
    real_base = tmp_path / "private" / "var" / "folders" / "T"
    real_base.mkdir(parents=True)
    link = tmp_path / "var"
    link.symlink_to(tmp_path / "private" / "var", target_is_directory=True)
    return link / "folders" / "T", real_base


def test_default_quarantine_root_resolves_symlinked_platform_tempdir(tmp_path):
    symlinked_tmp, real_base = _symlinked_tempdir(tmp_path)
    assert symlinked_tmp.exists() and any(
        Path(*symlinked_tmp.parts[: i + 1]).is_symlink() for i in range(len(symlinked_tmp.parts))
    )

    with patch("src.guardian.audio_worker.tempfile.gettempdir", return_value=str(symlinked_tmp)):
        worker = AudioIngressWorker()

    expected = Path(os.path.realpath(real_base)) / "seraph-audio"
    assert worker.quarantine_root == expected
    assert worker.quarantine_root.is_dir()
    assert not worker.quarantine_root.is_symlink()


def test_default_quarantine_root_still_rejects_symlink_below_resolved_base(tmp_path):
    symlinked_tmp, real_base = _symlinked_tempdir(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (real_base / "seraph-audio").symlink_to(elsewhere, target_is_directory=True)

    with patch("src.guardian.audio_worker.tempfile.gettempdir", return_value=str(symlinked_tmp)):
        with pytest.raises(ValueError, match="must not contain symlinks"):
            AudioIngressWorker()


def test_configured_quarantine_root_with_symlink_is_still_rejected(tmp_path):
    symlinked_tmp, _real_base = _symlinked_tempdir(tmp_path)

    with pytest.raises(ValueError, match="must not contain symlinks"):
        AudioIngressWorker(quarantine_root=symlinked_tmp / "seraph-audio")
