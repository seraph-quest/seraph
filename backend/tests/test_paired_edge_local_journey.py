"""Focused local receipts for persisted pairing state and edge ingress."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from pathlib import Path
import sys

import httpx
import pytest
from fastapi import FastAPI
from sqlmodel import select

from config.settings import settings
from src.api import nodes
from src.auth.middleware import OperatorAuthMiddleware
from src.auth.service import create_session
from src.db.models import PairedEdgeArtifact
from src.extensions import paired_edge
from src.extensions.state import (
    ExtensionStateRevisionConflict,
    ExtensionStateBusy,
    load_extension_state_payload,
    save_extension_state_payload,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "daemon"))


def test_extension_state_revision_is_an_interprocess_cas(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    initial = {"extensions": {"edge": {"status": "unpaired"}}}
    assert save_extension_state_payload(initial, expected_revision=0) == 1

    stale = {"extensions": {"edge": {"status": "rotating"}}}
    with pytest.raises(ExtensionStateRevisionConflict) as conflict:
        save_extension_state_payload(stale, expected_revision=0)
    assert conflict.value.expected == 0
    assert conflict.value.actual == 1
    assert load_extension_state_payload()["revision"] == 1


def test_concurrent_mutations_cannot_both_commit_same_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    assert save_extension_state_payload({"extensions": {}}, expected_revision=0) == 1
    barrier = Barrier(2)

    def attempt(status: str) -> str:
        barrier.wait()
        try:
            save_extension_state_payload(
                {"extensions": {"edge": {"status": status}}},
                expected_revision=1,
            )
            return "committed"
        except ExtensionStateRevisionConflict:
            return "conflict"
        except ExtensionStateBusy as exc:
            assert str(exc) == "extension_state_busy"
            return "busy"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(attempt, ("rotating", "revoking")))
    assert outcomes in (["committed", "conflict"], ["busy", "committed"])
    persisted = json.loads((tmp_path / "extensions-state.json").read_text(encoding="utf-8"))
    assert persisted["revision"] == 2


def _write_edge_extension(workspace: Path) -> None:
    package = workspace / "extensions" / "paired-edge"
    nodes_dir = package / "connectors" / "nodes"
    nodes_dir.mkdir(parents=True)
    (package / "manifest.yaml").write_text(
        "id: seraph.openclaw-device-bridge\n"
        "version: 2026.9.11\n"
        "display_name: Paired edge test package\n"
        "kind: connector-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.4.11\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  node_adapters:\n"
        "    - connectors/nodes/device.yaml\n",
        encoding="utf-8",
    )
    (nodes_dir / "device.yaml").write_text(
        "name: paired-device\n"
        "description: Provider-free paired edge.\n"
        "adapter_kind: device\n"
        "enabled: false\n"
        "capabilities:\n"
        "  - capture\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_local_http_capture_artifact_readback_spool_restart_and_revoke(tmp_path, monkeypatch, async_db):
    """Exercise the daemon adapter against the real FastAPI ingress routes."""

    workspace = tmp_path / "workspace"
    _write_edge_extension(workspace)
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    monkeypatch.setattr(settings, "deployment_environment", "test")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "paired-edge-local-test-secret")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "https://test")

    get_session = async_db
    monkeypatch.setattr(nodes, "get_session", get_session)
    token, operator = await create_session()

    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(nodes.router, prefix="/api")
    asgi_transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=asgi_transport, base_url="https://test") as client:
        client.cookies.set(settings.operator_auth_cookie_name, token)
        pair_response = await client.post(
            "/api/nodes/pairings/pair",
            headers={"Origin": "https://test"},
            json={
                "extension_id": "seraph.openclaw-device-bridge",
                "reference": "connectors/nodes/device.yaml",
                "device_id": "mac-local-1",
                "pairing_id": "pair-local-1",
                "label": "Local synthetic edge",
            },
        )
        assert pair_response.status_code == 200, pair_response.text
        pair_payload = pair_response.json()
        credential = pair_payload["credential"]
        assert pair_payload["credential_ref"].startswith("vault://")
        assert credential not in (workspace / "extensions-state.json").read_text(encoding="utf-8")
        entry = load_extension_state_payload()["extensions"]["seraph.openclaw-device-bridge"]["node_pairings"]["connectors/nodes/device.yaml"]
        assert entry["owner_principal_id"] == operator.principal.principal_id
        secret = await paired_edge.vault_repository.snapshot(
            entry["credential_vault_key"], owner_principal_id=operator.principal.principal_id
        )
        assert secret is not None and secret.value == credential
        assert await paired_edge.vault_repository.snapshot(
            entry["credential_vault_key"], owner_principal_id="operator:foreign"
        ) is None


        from paired_edge import PairedEdgeTransport

        spool_path = tmp_path / "edge-spool.json"
        transport = PairedEdgeTransport(
            origin="https://test",
            credential=credential,
            device_id="mac-local-1",
            pairing_id="pair-local-1",
            spool_path=spool_path,
            http_client=httpx.AsyncClient(transport=asgi_transport, base_url="https://test"),
        )
        accepted = await transport.capture(
            b"synthetic-png-bytes",
            app="Safari",
            window_title="Local capture",
        )
        assert accepted.status == "accepted"
        assert accepted.artifact_id and accepted.artifact_id.startswith("edge_art_")
        metadata = await client.get(f"/api/nodes/edge/artifacts/{accepted.artifact_id}")
        content = await client.get(f"/api/nodes/edge/artifacts/{accepted.artifact_id}/content")
        assert metadata.status_code == 200
        assert metadata.json()["server_owned"] is True
        assert metadata.json()["source_path"] is None
        assert content.status_code == 200
        assert content.content == b"synthetic-png-bytes"
        assert content.headers["x-seraph-artifact-id"] == accepted.artifact_id

        valid_payload = transport._payload(  # type: ignore[attr-defined]
            b"negative-check",
            sequence=2,
            request_id="negative-check",
            captured_at=datetime.now(timezone.utc),
        )
        wrong_size_payload = dict(valid_payload, content_size=999)
        wrong_size = await client.post(
            "/api/nodes/edge/upload",
            headers={"Authorization": f"Bearer {credential}", "Origin": "https://test"},
            json=wrong_size_payload,
        )
        assert wrong_size.status_code == 403
        assert wrong_size.json()["reason_code"] == "content_size_mismatch"
        wrong_hash_payload = dict(valid_payload, request_id="negative-hash", content_hash="sha256:" + "0" * 64)
        wrong_hash = await client.post(
            "/api/nodes/edge/upload",
            headers={"Authorization": f"Bearer {credential}", "Origin": "https://test"},
            json=wrong_hash_payload,
        )
        assert wrong_hash.status_code == 403
        assert wrong_hash.json()["reason_code"] == "content_hash_mismatch"
        out_of_order_payload = dict(valid_payload, request_id="negative-sequence", sequence=1)
        out_of_order = await client.post(
            "/api/nodes/edge/upload",
            headers={"Authorization": f"Bearer {credential}", "Origin": "https://test"},
            json=out_of_order_payload,
        )
        assert out_of_order.status_code == 409
        assert out_of_order.json()["status"] == "out_of_order"

        bad_origin = await client.post(
            "/api/nodes/edge/upload",
            headers={"Authorization": f"Bearer {credential}", "Origin": "http://evil"},
            json=valid_payload,
        )
        assert bad_origin.status_code == 403

        wrong_auth = await client.post(
            "/api/nodes/edge/upload",
            headers={"Authorization": "Bearer wrong", "Origin": "https://test"},
            json=transport._payload(  # type: ignore[attr-defined]
                b"bad-auth",
                sequence=99,
                request_id="bad-auth-request",
                captured_at=datetime.now(timezone.utc),
            ),
        )
        assert wrong_auth.status_code == 401

        offline = PairedEdgeTransport(
            origin="https://test",
            credential=credential,
            device_id="mac-local-1",
            pairing_id="pair-local-1",
            spool_path=spool_path,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: (_ for _ in ()).throw(httpx.ConnectError("offline", request=request))
                )
            ),
        )
        queued = await offline.capture(b"queued-after-disconnect", app="Safari")
        assert queued.status == "retryable" and queued.queued is True
        await offline.close()

        restarted = PairedEdgeTransport(
            origin="https://test",
            credential=credential,
            device_id="mac-local-1",
            pairing_id="pair-local-1",
            spool_path=spool_path,
            http_client=httpx.AsyncClient(transport=asgi_transport, base_url="https://test"),
        )
        drained = await restarted.drain()
        assert len(drained) == 1
        assert drained[0].status == "accepted"
        assert restarted.spool.count == 0
        assert await restarted.drain() == []

        async with get_session() as db:
            artifacts = (await db.execute(select(PairedEdgeArtifact))).scalars().all()
        assert len(artifacts) == 2

        revoke_response = await client.post(
            "/api/nodes/pairings/revoke",
            headers={"Origin": "https://test"},
            json={
                "extension_id": "seraph.openclaw-device-bridge",
                "reference": "connectors/nodes/device.yaml",
                "reason": "synthetic local revocation",
            },
        )
        assert revoke_response.status_code == 200, revoke_response.text
        rejected_after_revoke = await transport.capture(b"must-not-persist", app="Safari")
        assert rejected_after_revoke.status == "revoked"
        assert rejected_after_revoke.queued is False
        await transport.close()
        await restarted.close()
