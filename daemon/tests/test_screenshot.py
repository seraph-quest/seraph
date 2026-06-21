"""Tests for macOS screenshot diagnostics."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr import screenshot


def test_screencapture_permission_failure_logs_operator_warning(caplog):
    screenshot._warned_screencapture_failed = False
    failed = subprocess.CompletedProcess(
        args=["screencapture"],
        returncode=1,
        stdout=b"",
        stderr=b"",
    )

    with patch("subprocess.run", return_value=failed), caplog.at_level(logging.WARNING):
        assert screenshot._capture_via_screencapture() is None

    assert "macOS screencapture failed with code 1" in caplog.text
    assert "No screenshot artifact was created" in caplog.text
    assert "Screen Recording / Screen & System Audio Recording" in caplog.text


def test_screencapture_permission_warning_is_not_spammed(caplog):
    screenshot._warned_screencapture_failed = False
    failed = subprocess.CompletedProcess(
        args=["screencapture"],
        returncode=1,
        stdout=b"",
        stderr=b"permission denied",
    )

    with patch("subprocess.run", return_value=failed), caplog.at_level(logging.WARNING):
        assert screenshot._capture_via_screencapture() is None
        assert screenshot._capture_via_screencapture() is None

    assert caplog.text.count("macOS screencapture failed") == 1
    assert "permission denied" not in caplog.text
