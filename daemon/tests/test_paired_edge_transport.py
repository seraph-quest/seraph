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
        origin="http://127.0.0.1:8004",
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
        assert request.headers["origin"] == "http://127.0.0.1:8004"
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
        origin="http://127.0.0.1:8004",
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
        origin="http://localhost:8004",
        credential="credential",
        device_id="device",
        pairing_id="pairing",
        spool_path=tmp_path / "spool.json",
        blocklist={"bank"},
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(retryable_handler)),
    )
    blocked = await transport.capture(b"secret", app="My Bank")
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
    assert validate_edge_origin("HTTP://127.0.0.1:8004/") == "http://127.0.0.1:8004"
    with pytest.raises(ValueError):
        validate_edge_origin("http://user:pass@127.0.0.1:8004")
    with pytest.raises(ValueError):
        validate_edge_origin("http://127.0.0.1:8004/core")
