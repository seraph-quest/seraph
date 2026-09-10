"""Keyless contract tests for the Epic #736 health receipt."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import socket
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import epic_736_health as health  # noqa: E402


def _configured(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    values = {
        "WORKSPACE_DIR": str(workspace),
        "BACKEND_DATA_PATH_PROD": str(workspace),
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
    monkeypatch.setenv("OPENROUTER_ALLOWED_UPSTREAMS", "another-provider")
    receipt, exit_code, _ = health.build_receipt()
    check = next(item for item in receipt["checks"] if item["id"] == "runtime.openrouter_model_fabric")
    assert check["status"] == "pass"
    assert check["evidence_mode"] == "configuration"
    assert exit_code == 2


def test_provider_model_must_match_configured_upstream_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, tmp_path)
    monkeypatch.setenv("DEFAULT_MODEL", "openrouter/another-provider/model-v2")
    receipt, exit_code, _ = health.build_receipt()
    check = next(item for item in receipt["checks"] if item["id"] == "runtime.openrouter_model_fabric")
    assert check["status"] == "failed"
    assert exit_code == 4


def test_provider_model_uses_canonical_openrouter_syntax(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, tmp_path)
    for model in ("foo", "https://evil.invalid/model", "openrouter/provider/model/extra"):
        monkeypatch.setenv("DEFAULT_MODEL", model)
        check = health._openrouter_config_check()
        assert check["status"] == "failed", model


def test_local_inference_configuration_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, tmp_path)
    monkeypatch.setenv("LOCAL_LLM_API_BASE", "http://127.0.0.1:8000/v1")
    receipt, exit_code, _ = health.build_receipt()
    check = next(item for item in receipt["checks"] if item["id"] == "runtime.no_local_inference_dependency")
    assert check["status"] == "failed"
    assert exit_code == 4


def test_unprobed_optional_capabilities_are_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, tmp_path)
    receipt, exit_code, _ = health.build_receipt()
    checks = {item["id"]: item for item in receipt["checks"]}
    for identifier in ("memory.embedding_capability", "edge.mac", "voice.audio", "telegram"):
        assert checks[identifier]["status"] == "skipped"
        assert checks[identifier]["evidence_mode"] == "external_unverified"
    assert exit_code == 2


def test_excluded_stable_criteria_remain_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, tmp_path)
    receipt, _, _ = health.build_receipt()
    checks = {item["id"]: item for item in receipt["checks"]}
    expected = {
        "research.harness_improvement": 771,
        "evolution.staged_candidate": 771,
        "evolution.hidden_evaluation": 771,
        "evolution.scoped_canary_rollback": 771,
        "evaluation.comparator_scope_coverage": 754,
    }
    assert set(expected) <= checks.keys()
    for identifier, owner in expected.items():
        assert checks[identifier]["owner_issue"] == owner
        assert checks[identifier]["required"] is False
        assert checks[identifier]["status"] == "skipped"
        assert checks[identifier]["evidence_mode"] == "excluded"
        assert checks[identifier]["artifact_refs"] == [f"exclusion:{identifier}"]
    assert {item["id"] for item in receipt["exclusions"]} == set(expected)


def test_matrix_matches_python_evidence_contract() -> None:
    matrix = yaml.safe_load((ROOT / "scripts/epic_736_health_matrix.yaml").read_text())
    assert set(matrix["status_vocabulary"]) == health.VALID_STATUSES
    assert set(matrix["evidence_mode_vocabulary"]) == health.VALID_EVIDENCE_MODES
    required_fields = set(matrix["criterion_fields"])
    assert required_fields <= set(matrix["criteria"][0])
    python_contract = {
        criterion.identifier: (criterion.owner_issue, criterion.required, criterion.evidence_mode)
        for criterion in health.CRITERIA
    }
    matrix_contract = {
        criterion["id"]: (criterion["owner_issue"], criterion["required"], criterion["evidence_mode"])
        for criterion in matrix["criteria"]
    }
    assert matrix_contract == python_contract
    assert all(required_fields <= set(criterion) for criterion in matrix["criteria"])
    exclusions = {
        item["id"]: (item["owner_issue"], item["status"], item["replacement"])
        for item in matrix["exclusion_mapping"]
    }
    assert exclusions["research.harness_improvement"] == (771, "excluded", "deferred_outside_epic_736")


def test_required_child_criteria_are_mapped_and_source_check_is_separate() -> None:
    expected = {
        "conversation.identity_outbox": 750,
        "native_software.loop": 748,
        "edge.paired_transport": 749,
        "audio.capture_decode_persistence": 751,
        "telegram.durable_transport": 752,
        "capability_pack.lifecycle": 755,
    }
    criteria = {criterion.identifier: criterion for criterion in health.CRITERIA}
    assert set(expected) <= criteria.keys()
    for identifier, owner in expected.items():
        criterion = criteria[identifier]
        assert criterion.owner_issue == owner
        assert criterion.required is True
        assert criterion.evidence_mode == "integration"
        assert criterion.paths
        check = health._contract_check(criterion)
        assert check["evidence_mode"] == "static"
        if any(not (health.ROOT / path).exists() for path in criterion.paths):
            assert check["status"] == "blocked"


def _child_evidence_payload(criterion_id: str, *, timestamp: datetime | None = None) -> dict[str, object]:
    criterion = next(item for item in health.CRITERIA if item.identifier == criterion_id)
    payload: dict[str, object] = {
        "schema_version": health.CHILD_EVIDENCE_SCHEMA_VERSION,
        "child_issue": criterion.owner_issue,
        "criterion_id": criterion_id,
        "commit": health._safe_commit(),
        "command": "uv run pytest -q tests/test_native_software_engineering.py",
        "result": "pass",
        "timestamp": health._timestamp(timestamp or datetime.now(timezone.utc)),
    }
    payload["hash"] = health._child_evidence_digest(payload)
    return payload


def _write_child_evidence(directory: Path, payload: object, name: str = "child.json") -> Path:
    directory.mkdir()
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _integration_check(receipt: dict[str, object], criterion_id: str) -> dict[str, object]:
    checks = receipt["checks"]
    assert isinstance(checks, list)
    return next(item for item in checks if item["id"] == criterion_id and item["evidence_mode"] == "integration")


def test_missing_child_evidence_is_unknown_and_never_source_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, tmp_path)
    receipt, exit_code, _ = health.build_receipt()
    behavior = _integration_check(receipt, "native_software.loop")
    source = next(item for item in receipt["checks"] if item["id"] == "native_software.loop.source")
    assert behavior["status"] == "unknown"
    assert behavior["required"] is True
    assert source["status"] == "pass"
    assert source["required"] is False
    assert receipt["overall_status"] == "degraded"
    assert exit_code == 2


def test_malformed_child_evidence_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, tmp_path)
    evidence_dir = tmp_path / "child-evidence"
    _write_child_evidence(evidence_dir, "not-json")
    receipt, exit_code, _ = health.build_receipt(evidence_dir)
    behavior = _integration_check(receipt, "native_software.loop")
    assert behavior["status"] == "unknown"
    assert receipt["child_evidence"]["invalid_count"] >= 1
    assert exit_code == 2


@pytest.mark.parametrize("case", ["stale", "wrong_commit", "tampered_hash"])
def test_stale_or_wrong_commit_child_evidence_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    _configured(monkeypatch, tmp_path)
    payload = _child_evidence_payload(
        "native_software.loop",
        timestamp=datetime.now(timezone.utc) - timedelta(days=2) if case == "stale" else None,
    )
    if case == "wrong_commit":
        payload["commit"] = "0" * 40
        payload["hash"] = health._child_evidence_digest(payload)
    elif case == "tampered_hash":
        payload["hash"] = "0" * 64
    evidence_dir = tmp_path / "child-evidence"
    _write_child_evidence(evidence_dir, payload)
    receipt, exit_code, _ = health.build_receipt(evidence_dir)
    assert _integration_check(receipt, "native_software.loop")["status"] == "unknown"
    assert exit_code == 2


def test_current_passing_child_evidence_satisfies_only_behavior_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, tmp_path)
    evidence_dir = tmp_path / "child-evidence"
    payload = _child_evidence_payload("native_software.loop")
    _write_child_evidence(evidence_dir, payload)
    receipt, exit_code, _ = health.build_receipt(evidence_dir)
    behavior = _integration_check(receipt, "native_software.loop")
    source = next(item for item in receipt["checks"] if item["id"] == "native_software.loop.source")
    assert behavior["status"] == "pass"
    assert behavior["evidence_mode"] == "integration"
    assert behavior["artifact_refs"][0].startswith("child-evidence:")
    assert source["required"] is False
    assert exit_code == 2


def test_nonpassing_child_result_cannot_become_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, tmp_path)
    evidence_dir = tmp_path / "child-evidence"
    payload = _child_evidence_payload("native_software.loop")
    payload["result"] = "degraded"
    payload["hash"] = health._child_evidence_digest(payload)
    _write_child_evidence(evidence_dir, payload)
    receipt, exit_code, _ = health.build_receipt(evidence_dir)
    assert _integration_check(receipt, "native_software.loop")["status"] == "degraded"
    assert exit_code == 2


def test_optional_blocked_status_cannot_be_healthy() -> None:
    assert health._overall([{"required": False, "status": "blocked"}]) == ("degraded", 2)


def test_canonical_workspace_rejects_symlink_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    _configured(monkeypatch, link)
    with pytest.raises(RuntimeError, match="canonical production workspace"):
        health.build_receipt()


def test_canonical_workspace_rejects_ambiguous_bind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bind = tmp_path / "bind"
    workspace = tmp_path / "workspace"
    bind.mkdir()
    workspace.mkdir()
    _configured(monkeypatch, workspace)
    monkeypatch.setenv("BACKEND_DATA_PATH_PROD", str(bind))
    with pytest.raises(RuntimeError, match="canonical production workspace"):
        health.build_receipt()


def test_receipt_timestamp_collision_keeps_both_atomic_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, tmp_path)
    generated = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    first = health._write_receipt({"marker": "first"}, generated)
    second = health._write_receipt({"marker": "second"}, generated)
    assert first != second
    receipts = sorted((tmp_path / "operator-receipts/epic-736-health").glob("*.json"))
    assert len(receipts) == 2
    assert {json.loads(path.read_text())["marker"] for path in receipts} == {"first", "second"}


def test_concurrent_receipt_timestamp_collision_keeps_every_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, tmp_path)
    generated = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda marker: health._write_receipt({"marker": marker}, generated), range(8)))

    assert len(set(paths)) == 8
    receipts = sorted((tmp_path / "operator-receipts/epic-736-health").glob("*.json"))
    assert len(receipts) == 8
    assert {json.loads(path.read_text())["marker"] for path in receipts} == set(range(8))


def test_cli_redacts_secret_from_stdout_and_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _configured(monkeypatch, tmp_path)
    secret = "or-secret-cli-value"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    assert health.main(["--format", "json"]) == 2
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert json.loads(captured.out)["schema_version"] == 2


def test_health_collection_is_network_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch, tmp_path)

    def _forbidden_socket(*args, **kwargs):
        raise AssertionError("health receipt must not open a network socket")

    monkeypatch.setattr(socket, "socket", _forbidden_socket)
    receipt, _, _ = health.build_receipt()
    assert receipt["overall_status"] == "degraded"
