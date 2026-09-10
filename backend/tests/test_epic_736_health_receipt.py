"""Keyless contract tests for the Epic #736 health receipt."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import epic_736_health as health  # noqa: E402


def _configured(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    values = {
        "WORKSPACE_DIR": str(workspace),
        "LLM_API_BASE": "https://openrouter.ai/api/v1",
        "DEFAULT_MODEL": "openrouter/z-ai/glm-5.3-flash",
        "OPENROUTER_PROVIDER_ONLY": "true",
        "OPENROUTER_ALLOW_FALLBACKS": "false",
        "OPENROUTER_REQUIRE_PARAMETERS": "true",
        "OPENROUTER_DATA_COLLECTION": "deny",
        "OPENROUTER_ALLOWED_UPSTREAMS": "z-ai",
        "MODEL_TEMPERATURE": "0.7",
        "MODEL_MAX_TOKENS": "4096",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    for key in (
        "LOCAL_MODEL", "LOCAL_LLM_API_BASE", "LOCAL_INFERENCE_URL", "OLLAMA_BASE_URL",
        "LM_STUDIO_BASE_URL", "SERAPH_VLM_MODE", "SERAPH_VLM_BASE_URL", "SERAPH_VLM_BACKEND_URL",
        "WHISPER_MODEL", "WHISPER_BASE_URL", "PIPER_MODEL", "PIPER_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_keyless_receipt_has_schema_and_logical_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    receipt, exit_code, logical = health.build_receipt()

    assert receipt["schema_version"] == 2
    assert receipt["epic"] == 736
    assert receipt["environment"] == "prod"
    assert receipt["overall_status"] == "degraded"
    assert exit_code == 2
    assert logical.startswith("operator-receipts/epic-736-health/")
    assert (tmp_path / logical).is_file()
    assert {item["status"] for item in receipt["checks"]} <= health.VALID_STATUSES
    assert any(item["id"] == "runtime.openrouter_text_receipt" and item["status"] == "skipped" for item in receipt["checks"])
    harness = next(item for item in receipt["checks"] if item["id"] == "research.harness_improvement")
    assert harness["status"] == "skipped"
    assert harness["required"] is False
    assert harness["evidence_mode"] == "excluded"
    assert not any(item["required"] and item["owner_issue"] == 771 for item in receipt["checks"])


def test_receipt_never_contains_key_or_raw_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, tmp_path)
    secret = "or-secret-test-value"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    receipt, _, logical = health.build_receipt()
    encoded = json.dumps(receipt)
    persisted = (tmp_path / logical).read_text()
    assert secret not in encoded
    assert secret not in persisted
    assert str(tmp_path) not in encoded
    assert str(tmp_path) not in persisted


def test_malformed_provider_config_fails_without_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, tmp_path)
    monkeypatch.setenv("DEFAULT_MODEL", "local model with spaces")
    receipt, exit_code, _ = health.build_receipt()
    assert receipt["overall_status"] == "failed"
    assert exit_code == 4
    assert next(item for item in receipt["checks"] if item["id"] == "runtime.openrouter_model_fabric")["status"] == "failed"


def test_provider_model_is_generic_and_not_tied_to_a_single_catalog_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, tmp_path)
    monkeypatch.setenv("DEFAULT_MODEL", "openrouter/another-provider/model-v2")
    receipt, exit_code, _ = health.build_receipt()
    check = next(item for item in receipt["checks"] if item["id"] == "runtime.openrouter_model_fabric")
    assert check["status"] == "pass"
    assert check["evidence_mode"] == "configuration"
    assert exit_code == 2


def test_local_inference_configuration_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, tmp_path)
    monkeypatch.setenv("LOCAL_LLM_API_BASE", "http://127.0.0.1:8000/v1")
    receipt, exit_code, _ = health.build_receipt()
    check = next(item for item in receipt["checks"] if item["id"] == "runtime.no_local_inference_dependency")
    assert check["status"] == "failed"
    assert exit_code == 4


def test_health_collection_is_network_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, tmp_path)

    def _forbidden_socket(*args, **kwargs):
        raise AssertionError("health receipt must not open a network socket")

    monkeypatch.setattr(socket, "socket", _forbidden_socket)
    receipt, _, _ = health.build_receipt()
    assert receipt["overall_status"] == "degraded"
