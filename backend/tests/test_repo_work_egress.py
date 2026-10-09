import hashlib
import json

import pytest

from src.workflows.repo_repair import RepoRepairError, RepoRepairService


@pytest.mark.asyncio
async def test_exact_diagnostics_redaction_and_full_serialized_wrapper_bound():
    service = RepoRepairService(secret_scanner=lambda text: text.replace("secret-token", "[redacted secret]"))
    prepared = await service.prepare_iteration_egress(iteration_id="a" * 64,
        source_payload={"source": "print('safe')"}, stdout="failed secret-token", stderr="trace secret-token")
    serialized = json.dumps(prepared["messages"], sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode()
    assert b"secret-token" not in serialized
    assert prepared["envelope"]["diagnostics"]["stdout"] == "failed [redacted secret]"
    assert prepared["combined_input_bytes"] == len(serialized)
    assert prepared["serialized_request_sha256"] == hashlib.sha256(serialized).hexdigest()
    assert prepared["combined_input_bytes"] > len(json.dumps(prepared["envelope"]).encode())
    with pytest.raises(RepoRepairError, match="wrappers"):
        await service.prepare_iteration_egress(iteration_id="a" * 64,
            source_payload={"source": "print('safe')"}, stdout="failed secret-token", stderr="trace secret-token",
            original_input_byte_limit=prepared["combined_input_bytes"] - 1)


@pytest.mark.asyncio
async def test_source_and_diagnostics_jointly_exceed_original_cap():
    service = RepoRepairService(secret_scanner=lambda text: text)
    with pytest.raises(RepoRepairError, match="original input bound"):
        await service.prepare_iteration_egress(iteration_id="a" * 64,
            source_payload={"source": "x" * 32768}, stdout="y" * 32768, stderr="")


@pytest.mark.asyncio
async def test_redaction_failure_cannot_produce_consent_bytes():
    service = RepoRepairService(secret_scanner=lambda text: "[redaction unavailable]")
    with pytest.raises(RepoRepairError, match="redaction"):
        await service.prepare_iteration_egress(iteration_id="a" * 64,
            source_payload={}, stdout="private diagnostic", stderr="")
