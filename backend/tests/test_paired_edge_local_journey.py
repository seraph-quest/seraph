"""Focused local receipts for persisted pairing state and edge ingress."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from threading import Barrier
from pathlib import Path
import sys

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

from config.settings import settings
from src.api import nodes
from src.auth.middleware import OperatorAuthMiddleware
from src.db.models import PairedEdgeArtifact
from src.extensions import paired_edge
from src.extensions.state import (
    ExtensionStateRevisionConflict,
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

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(attempt, ("rotating", "revoking")))
    assert outcomes == ["committed", "conflict"]
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
async def test_local_http_capture_artifact_readback_spool_restart_and_revoke(tmp_path, monkeypatch):
    """Exercise the daemon adapter against the real FastAPI ingress routes."""

    workspace = tmp_path / "workspace"
    _write_edge_extension(workspace)
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    monkeypatch.setattr(settings, "deployment_environment", "test")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", True)
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://test")

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=[PairedEdgeArtifact.__table__],
            )
        )

    @asynccontextmanager
    async def get_session():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr(nodes, "get_session", get_session)
    credentials: dict[str, str] = {}

    async def vault_store(key: str, value: str, description: str | None = None):
        credentials[key] = value
        return None

    async def vault_get(key: str) -> str | None:
        return credentials.get(key)

    monkeypatch.setattr(paired_edge.vault_repository, "store", vault_store)
    monkeypatch.setattr(paired_edge.vault_repository, "get", vault_get)

    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(nodes.router, prefix="/api")
    asgi_transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=asgi_transport, base_url="http://test") as client:
        pair_response = await client.post(
            "/api/nodes/pairings/pair",
            headers={"Origin": "http://test"},
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

        from paired_edge import PairedEdgeTransport

        spool_path = tmp_path / "edge-spool.json"
        transport = PairedEdgeTransport(
            origin="http://test",
            credential=credential,
            device_id="mac-local-1",
            pairing_id="pair-local-1",
            spool_path=spool_path,
            http_client=httpx.AsyncClient(transport=asgi_transport, base_url="http://test"),
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
            headers={"Authorization": f"Bearer {credential}", "Origin": "http://test"},
            json=wrong_size_payload,
        )
        assert wrong_size.status_code == 403
        assert wrong_size.json()["reason_code"] == "content_size_mismatch"
        wrong_hash_payload = dict(valid_payload, request_id="negative-hash", content_hash="sha256:" + "0" * 64)
        wrong_hash = await client.post(
            "/api/nodes/edge/upload",
            headers={"Authorization": f"Bearer {credential}", "Origin": "http://test"},
            json=wrong_hash_payload,
        )
        assert wrong_hash.status_code == 403
        assert wrong_hash.json()["reason_code"] == "content_hash_mismatch"
        out_of_order_payload = dict(valid_payload, request_id="negative-sequence", sequence=1)
        out_of_order = await client.post(
            "/api/nodes/edge/upload",
            headers={"Authorization": f"Bearer {credential}", "Origin": "http://test"},
            json=out_of_order_payload,
        )
        assert out_of_order.status_code == 409
        assert out_of_order.json()["status"] == "out_of_order"

        monkeypatch.setattr(settings, "operator_auth_secret", "operator-secret")
        bad_origin = await client.post(
            "/api/nodes/edge/upload",
            headers={"Authorization": f"Bearer {credential}", "Origin": "http://evil"},
            json=valid_payload,
        )
        assert bad_origin.status_code == 403
        monkeypatch.setattr(settings, "operator_auth_secret", "")

        wrong_auth = await client.post(
            "/api/nodes/edge/upload",
            headers={"Authorization": "Bearer wrong", "Origin": "http://test"},
            json=transport._payload(  # type: ignore[attr-defined]
                b"bad-auth",
                sequence=99,
                request_id="bad-auth-request",
                captured_at=datetime.now(timezone.utc),
            ),
        )
        assert wrong_auth.status_code == 401

        offline = PairedEdgeTransport(
            origin="http://test",
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
            origin="http://test",
            credential=credential,
            device_id="mac-local-1",
            pairing_id="pair-local-1",
            spool_path=spool_path,
            http_client=httpx.AsyncClient(transport=asgi_transport, base_url="http://test"),
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
            headers={"Origin": "http://test"},
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
    await engine.dispose()
