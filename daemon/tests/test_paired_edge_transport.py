"""Provider-free local HTTP and durable spool proof for a paired edge."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paired_edge import DurableEdgeSpool, PairedEdgeTransport, validate_edge_origin


def _offline_client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_disconnect_restart_drain_is_ordered_and_exactly_once(tmp_path):
    spool_path = tmp_path / "edge-spool.json"

    async def run() -> None:
        await _disconnect_restart_drain(spool_path)

    asyncio.run(run())


async def _disconnect_restart_drain(spool_path):
    first = PairedEdgeTransport(
        origin="https://127.0.0.1:8004",
        credential="one-time-test-credential",
        device_id="mac-test-1",
        pairing_id="pair-test-1",
        spool_path=spool_path,
        http_client=_offline_client(),
    )
    first_result = await first.capture(b"first", app="Safari")
    second_result = await first.capture(b"second", app="Safari")
    assert first_result.status == "retryable" and first_result.queued is True
    assert second_result.status == "retryable" and second_result.queued is True
    assert [item.sequence for item in first.spool.items] == [1, 2]
    await first.close()

    requests: list[httpx.Request] = []

    def online_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = request.read()
        assert request.headers["authorization"] == "Bearer one-time-test-credential"
        assert request.headers["origin"] == "https://127.0.0.1:8004"
        assert b'"content_base64"' in body
        return httpx.Response(
            201,
            json={
                "status": "accepted",
                "reason_code": "artifact_accepted",
                "artifact": {"artifact_id": f"edge_art_{len(requests)}"},
            },
            request=request,
        )

    restarted = PairedEdgeTransport(
        origin="https://127.0.0.1:8004",
        credential="one-time-test-credential",
        device_id="mac-test-1",
        pairing_id="pair-test-1",
        spool_path=spool_path,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(online_handler)),
    )
    results = await restarted.drain()
    assert [result.status for result in results] == ["accepted", "accepted"]
    assert [result.sequence for result in results] == [1, 2]
    assert restarted.spool.count == 0
    assert await restarted.drain() == []
    assert len(requests) == 2
    await restarted.close()


def test_retryable_server_response_is_durable_and_blocklist_precedes_upload(tmp_path):
    asyncio.run(_retryable_response_and_blocklist(tmp_path))


async def _retryable_response_and_blocklist(tmp_path):
    calls = 0

    def retryable_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"status": "retryable", "reason_code": "server_busy"}, request=request)

    transport = PairedEdgeTransport(
        origin="https://localhost:8004",
        credential="credential",
        device_id="device",
        pairing_id="pairing",
        spool_path=tmp_path / "spool.json",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(retryable_handler)),
    )
    blocked = await transport.capture(b"secret", app="1Password 7")
    assert blocked.status == "blocked"
    assert blocked.reason_code == "sensitive_app_blocked_before_upload"
    assert calls == 0

    queued = await transport.capture(b"safe", app="Safari")
    assert queued.status == "retryable"
    assert queued.queued is True
    assert transport.spool.count == 1
    await transport.close()


def test_spool_bounds_dedupe_and_retry_backoff_are_durable(tmp_path):
    path = tmp_path / "spool.json"
    now = datetime(2026, 9, 11, tzinfo=timezone.utc)
    spool = DurableEdgeSpool(path, max_count=1, max_bytes=5, backoff_seconds=2)
    assert spool.enqueue({"content": "a"}, request_id="r1", sequence=1, content_size=1, now=now)
    assert not spool.enqueue({"content": "a"}, request_id="r1", sequence=1, content_size=1, now=now)
    assert not spool.enqueue({"content": "b"}, request_id="r2", sequence=2, content_size=1, now=now)
    assert spool.retry("r1", error="offline", now=now)
    assert spool.ready(now=now) == []
    restarted = DurableEdgeSpool(path, max_count=1, max_bytes=5, backoff_seconds=2)
    assert restarted.count == 1
    assert restarted.items[0].retries == 1
    assert restarted.items[0].last_error == "offline"


def test_edge_origin_rejects_credentials_and_non_origin_paths():
    assert validate_edge_origin("HTTPS://127.0.0.1:8004/") == "https://127.0.0.1:8004"
    with pytest.raises(ValueError, match="https"):
        validate_edge_origin("HTTP://127.0.0.1:8004/")
    with pytest.raises(ValueError):
        validate_edge_origin("http://user:pass@127.0.0.1:8004")
    with pytest.raises(ValueError):
        validate_edge_origin("http://127.0.0.1:8004/core")


def test_credentialed_plaintext_is_rejected_but_explicit_local_synthetic_is_allowed(tmp_path):
    with pytest.raises(ValueError, match="https"):
        PairedEdgeTransport(
            origin="http://127.0.0.1:8004",
            credential="bearer-secret",
            device_id="device",
            pairing_id="pairing",
            spool_path=tmp_path / "secure.json",
            http_client=_offline_client(),
        )
    synthetic = PairedEdgeTransport(
        origin="http://127.0.0.1:8004",
        credential=None,
        device_id="device",
        pairing_id="pairing",
        spool_path=tmp_path / "synthetic.json",
        allow_insecure_test_transport=True,
        http_client=_offline_client(),
    )
    asyncio.run(synthetic.close())


def test_unauthorized_http_receipts_are_terminal_and_not_queued(tmp_path):
    async def run() -> None:
        for status_code in (401, 403):
            def denied_handler(request: httpx.Request, *, status_code=status_code) -> httpx.Response:
                return httpx.Response(status_code, json={"detail": {"code": "access_denied"}}, request=request)

            transport = PairedEdgeTransport(
                origin="https://localhost:8004",
                credential="credential",
                device_id="device",
                pairing_id="pairing",
                spool_path=tmp_path / f"denied-{status_code}.json",
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(denied_handler)),
            )
            result = await transport.capture(b"safe", app="Safari")
            assert result.status == "blocked"
            assert result.queued is False
            assert result.http_status == status_code
            assert transport.spool.count == 0
            await transport.close()

            spool_path = tmp_path / f"drain-denied-{status_code}.json"
            queued_transport = PairedEdgeTransport(
                origin="https://localhost:8004",
                credential="credential",
                device_id="device",
                pairing_id="pairing",
                spool_path=spool_path,
                http_client=_offline_client(),
            )
            queued = await queued_transport.capture(b"safe", app="Safari")
            assert queued.queued is True
            await queued_transport.close()
            drainer = PairedEdgeTransport(
                origin="https://localhost:8004",
                credential="credential",
                device_id="device",
                pairing_id="pairing",
                spool_path=spool_path,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(denied_handler)),
            )
            drained = await drainer.drain()
            assert drained[0].status == "blocked"
            assert drained[0].http_status == status_code
            assert drainer.spool.count == 0
            await drainer.close()

    asyncio.run(run())


def test_spooled_heartbeat_keeps_endpoint_and_sequence_order(tmp_path):
    async def run() -> None:
        offline = PairedEdgeTransport(
            origin="https://localhost:8004",
            credential="credential",
            device_id="device",
            pairing_id="pairing",
            spool_path=tmp_path / "ordered.json",
            http_client=_offline_client(),
        )
        capture = await offline.capture(b"safe", app="Safari")
        heartbeat = await offline.heartbeat()
        assert capture.queued and heartbeat.queued
        assert [(item.sequence, item.kind, item.endpoint) for item in offline.spool.items] == [
            (1, "capture", "/api/nodes/edge/upload"),
            (2, "heartbeat", "/api/nodes/edge/heartbeat"),
        ]
        await offline.close()

        paths: list[str] = []

        def online_handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return httpx.Response(201, json={"status": "accepted", "reason_code": "ok"}, request=request)

        online = PairedEdgeTransport(
            origin="https://localhost:8004",
            credential="credential",
            device_id="device",
            pairing_id="pairing",
            spool_path=tmp_path / "ordered.json",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(online_handler)),
        )
        results = await online.drain()
        assert [item.status for item in results] == ["accepted", "accepted"]
        assert paths == ["/api/nodes/edge/upload", "/api/nodes/edge/heartbeat"]
        assert online.spool.count == 0
        await online.close()

    asyncio.run(run())


def test_capture_queues_behind_existing_backlog_and_drain_stops_on_retryable(tmp_path):
    async def run() -> None:
        calls: list[int] = []

        def first_request_fails(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(503, json={"status": "accepted"}, request=request)

        transport = PairedEdgeTransport(
            origin="https://localhost:8004",
            credential="credential",
            device_id="device",
            pairing_id="pairing",
            spool_path=tmp_path / "backlog.json",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(first_request_fails)),
        )
        first = await transport.capture(b"first", app="Safari")
        second = await transport.capture(b"second", app="Safari")
        assert first.status == "retryable" and first.queued is True
        assert second.status == "retryable" and second.queued is True
        assert calls == [1]
        assert [item.sequence for item in transport.spool.items] == [1, 2]
        await transport.close()

        drain_calls: list[int] = []

        def retry_first(request: httpx.Request) -> httpx.Response:
            drain_calls.append(1)
            return httpx.Response(503, json={"status": "accepted"}, request=request)

        drainer = PairedEdgeTransport(
            origin="https://localhost:8004",
            credential="credential",
            device_id="device",
            pairing_id="pairing",
            spool_path=tmp_path / "backlog.json",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(retry_first)),
        )
        results = await drainer.drain()
        assert [(result.sequence, result.status) for result in results] == [(1, "retryable")]
        assert drain_calls == [1]
        assert [item.sequence for item in drainer.spool.items] == [1, 2]
        await drainer.close()

    asyncio.run(run())


def test_http_error_cannot_be_accepted_and_origin_denial_is_actionable(tmp_path):
    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                json={"status": "accepted", "detail": {"code": "origin_forbidden"}},
                request=request,
            )

        transport = PairedEdgeTransport(
            origin="https://localhost:8004",
            credential="credential",
            device_id="device",
            pairing_id="pairing",
            spool_path=tmp_path / "origin.json",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        result = await transport.capture(b"safe", app="Safari")
        assert result.status == "blocked"
        assert result.reason_code == "edge_origin_not_allowed_configure_operator_auth_allowed_origins"
        assert result.queued is False
        await transport.close()

    asyncio.run(run())


def test_http_terminal_receipt_preserves_revocation_but_not_success(tmp_path):
    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                410,
                json={"status": "revoked", "reason_code": "pairing_revoked"},
                request=request,
            )

        transport = PairedEdgeTransport(
            origin="https://localhost:8004",
            credential="credential",
            device_id="device",
            pairing_id="pairing",
            spool_path=tmp_path / "revoked.json",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        result = await transport.capture(b"safe", app="Safari")
        assert result.status == "revoked"
        assert result.queued is False
        await transport.close()

    asyncio.run(run())
