"""Tests for the local Codex screen OCR provider."""

import json
import os
import platform
import stat
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin",
    reason="Daemon tests require macOS",
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ocr import create_provider
from ocr.codex_local import CodexLocalProvider


def test_factory_creates_codex_local_provider():
    provider = create_provider("codex-local", model="gpt-5.5")

    assert isinstance(provider, CodexLocalProvider)
    assert provider.name == "codex-local"


@pytest.mark.asyncio
async def test_codex_local_provider_parses_structured_json_and_cleans_temp_files(tmp_path):
    provider = CodexLocalProvider(model="gpt-5.5", temp_dir=str(tmp_path))

    async def fake_run(*, image_path: Path, output_path: Path, prompt: str) -> str:
        assert image_path.exists()
        assert image_path.read_bytes() == b"valid png bytes"
        assert output_path.exists()
        assert "frontmost app is: VS Code" in prompt
        return (
            '{"activity":"coding","project":"Seraph","summary":"Editing daemon OCR provider",'
            '"details":["daemon/ocr/codex_local.py","unit tests"],'
            '"sensitive_detected":false,"confidence":0.82}'
        )

    with patch.object(provider, "_run_codex", new=AsyncMock(side_effect=fake_run)) as mock_run:
        result = await provider.analyze_screen(b"valid png bytes", "VS Code")

    assert result.success is True
    assert result.data == {
        "activity": "coding",
        "project": "Seraph",
        "summary": "Editing daemon OCR provider",
        "details": ["daemon/ocr/codex_local.py", "unit tests"],
        "sensitive_detected": False,
        "confidence": 0.82,
    }
    mock_run.assert_awaited_once()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_codex_local_provider_preserves_capture_artifacts_when_enabled(tmp_path):
    temp_dir = tmp_path / "tmp"
    archive_dir = tmp_path / "archive"
    temp_dir.mkdir()
    provider = CodexLocalProvider(
        model="gpt-5.5",
        temp_dir=str(temp_dir),
        preserve_captures=True,
        archive_dir=str(archive_dir),
    )
    raw_output = (
        '{"activity":"coding","project":"Seraph","summary":"Reviewing preserved captures",'
        '"details":["screen artifact API"],'
        '"sensitive_detected":false,"confidence":0.91}'
    )

    with patch.object(provider, "_run_codex", new=AsyncMock(return_value=raw_output)):
        result = await provider.analyze_screen(b"valid png bytes", "VS Code")

    assert result.success is True
    artifacts = result.data["capture_artifacts"]
    image_path = Path(artifacts["image_path"])
    codex_output_path = Path(artifacts["codex_output_path"])
    analysis_path = Path(artifacts["analysis_path"])
    assert image_path.read_bytes() == b"valid png bytes"
    assert codex_output_path.read_text(encoding="utf-8") == raw_output
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    assert analysis["summary"] == "Reviewing preserved captures"
    assert analysis["confidence"] == 0.91
    assert stat.S_IMODE(archive_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(image_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(image_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(codex_output_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(analysis_path.stat().st_mode) == 0o600
    assert list(temp_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_codex_local_provider_cleans_temp_files_on_failure(tmp_path):
    provider = CodexLocalProvider(model="gpt-5.5", temp_dir=str(tmp_path))

    with patch.object(provider, "_run_codex", new=AsyncMock(side_effect=RuntimeError("boom"))):
        result = await provider.analyze_screen(b"valid png bytes", "VS Code")

    assert result.success is False
    assert result.error == "boom"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_codex_local_provider_redacts_secret_like_output(tmp_path):
    provider = CodexLocalProvider(model="gpt-5.5", temp_dir=str(tmp_path))

    with patch.object(
        provider,
        "_run_codex",
        new=AsyncMock(
            return_value=(
                '{"activity":"terminal","project":null,'
                '"summary":"Reviewing API_KEY=abc123 terminal output",'
                '"details":["bearer token-value"],'
                '"sensitive_detected":true,"confidence":1.4}'
            )
        ),
    ):
        result = await provider.analyze_screen(b"valid png bytes", "Terminal")

    assert result.success is True
    assert result.data["summary"] == "Reviewing [redacted] terminal output"
    assert result.data["details"] == ["[redacted]"]
    assert result.data["sensitive_detected"] is True
    assert result.data["confidence"] == 1.0


@pytest.mark.asyncio
async def test_codex_local_provider_rejects_non_json_output(tmp_path):
    provider = CodexLocalProvider(model="gpt-5.5", temp_dir=str(tmp_path))

    with patch.object(provider, "_run_codex", new=AsyncMock(return_value="not json")):
        result = await provider.analyze_screen(b"valid png bytes", "Safari")

    assert result.success is False
    assert "valid JSON" in str(result.error)
    assert list(tmp_path.iterdir()) == []
