"""Authenticated daemon local capture upload (ADR-035, #1147).

Provider-free: no inference/provider/network calls. The governed analysis
gate is patched only to observe the pending marker it would queue.
"""

from __future__ import annotations

import base64
import io
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlmodel import select

from config.settings import settings
from src.api import nodes, observer
from src.auth.middleware import OperatorAuthMiddleware
from src.auth.service import create_session
from src.db.models import ScreenObservation
from src.observer import local_capture_upload, screenshot_folder_source
from src.observer.local_capture_upload import (
    DEFAULT_CAPTURE_BLOCKLIST,
    LOCAL_CAPTURE_UPLOAD_DIRNAME,
    _atomic_private_write,
    cleanup_local_capture_uploads,
)

DAEMON_DIR = Path(__file__).resolve().parents[2] / "daemon"
ORIGIN = "https://test"
DEVICE = "mac-local-1"
PAIRING = "pair-local-1"


def _png(color: tuple[int, int, int], *, size: tuple[int, int] = (32, 32), marker: int = 0) -> bytes:
    from PIL import Image

    image = Image.new("RGB", size, color)
    if marker:
        image.putpixel((0, 0), (marker % 256, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _striped_png(offset: int) -> bytes:
    from PIL import Image

    image = Image.new("RGB", (32, 32), (255, 255, 255))
    for x in range(32):
        for y in range(16):
            image.putpixel((x, y), (0, 0, 0))
    if offset:
        image.putpixel((31, 31), (offset % 256, offset % 256, offset % 256))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


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


@pytest.fixture
async def capture_env(tmp_path, monkeypatch, async_db):
    workspace = tmp_path / "workspace"
    _write_edge_extension(workspace)
    shots = tmp_path / "shots"
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    monkeypatch.setattr(settings, "deployment_environment", "test")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "local-capture-upload-test-secret")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", ORIGIN)
    monkeypatch.setenv("SERAPH_SCREENSHOT_FOLDER", str(shots))
    monkeypatch.setenv("SERAPH_SCREEN_ANALYSIS_ENABLED", "1")
    monkeypatch.setattr(nodes, "get_session", async_db)
    # Observe the queued marker only; no analysis or provider call happens.
    monkeypatch.setattr(screenshot_folder_source, "screenshot_semantic_analysis_enabled", lambda: True)
    local_capture_upload.upload_rate_limiter.reset()

    token, _operator = await create_session()
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(nodes.router, prefix="/api")
    app.include_router(observer.router, prefix="/api")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as operator_client:
        operator_client.cookies.set(settings.operator_auth_cookie_name, token)
        pair = await operator_client.post(
            "/api/nodes/pairings/pair",
            headers={"Origin": ORIGIN},
            json={
                "extension_id": "seraph.openclaw-device-bridge",
                "reference": "connectors/nodes/device.yaml",
                "device_id": DEVICE,
                "pairing_id": PAIRING,
                "label": "Local synthetic edge",
            },
        )
        assert pair.status_code == 200, pair.text
        credential = pair.json()["credential"]
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as daemon_client:
            yield {
                "client": daemon_client,
                "operator_client": operator_client,
                "credential": credential,
                "shots": shots.resolve(),
                "db": async_db,
            }
    local_capture_upload.upload_rate_limiter.reset()


def _headers(credential: str | None, **overrides: str) -> dict[str, str]:
    headers = {"Origin": ORIGIN, "X-Seraph-Node-Device": DEVICE, "X-Seraph-Node-Pairing": PAIRING}
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential}"
    headers.update(overrides)
    return headers


def _body(content: bytes, *, capture_id: str = "cap-0000000001", app: str = "Code", title: str = "main.py — seraph",
          captured_at: datetime | None = None, ocr_text: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "capture_id": capture_id,
        "app_name": app,
        "window_title": title,
        "captured_at": (captured_at or datetime.now(timezone.utc)).isoformat(),
        "content_base64": base64.b64encode(content).decode("ascii"),
    }
    if ocr_text is not None:
        payload["ocr_text"] = ocr_text
    return payload


async def _observations(db) -> list[ScreenObservation]:
    async with db() as session:
        return list((await session.execute(select(ScreenObservation))).scalars().all())


def _artifacts(observation: ScreenObservation) -> dict[str, object]:
    for item in json.loads(observation.details_json or "[]"):
        if item.startswith("capture_artifacts:"):
            return json.loads(item.removeprefix("capture_artifacts:"))
    raise AssertionError("capture_artifacts missing")


@pytest.mark.asyncio
async def test_upload_requires_paired_edge_credential(capture_env):
    client = capture_env["client"]
    operator_client = capture_env["operator_client"]
    body = _body(_png((10, 20, 30)))

    missing = await client.post("/api/observer/captures", headers=_headers(None), json=body)
    assert missing.status_code == 401
    wrong = await client.post("/api/observer/captures", headers=_headers("not-the-credential"), json=body)
    assert wrong.status_code == 401
    no_identity = await client.post(
        "/api/observer/captures",
        headers={"Origin": ORIGIN, "Authorization": f"Bearer {capture_env['credential']}"},
        json=body,
    )
    assert no_identity.status_code == 401
    wrong_device = await client.post(
        "/api/observer/captures",
        headers=_headers(capture_env["credential"], **{"X-Seraph-Node-Device": "other-device"}),
        json=body,
    )
    assert wrong_device.status_code == 403
    # A browser operator cookie is not daemon upload authority.
    cookie_only = await operator_client.post("/api/observer/captures", headers={"Origin": ORIGIN}, json=body)
    assert cookie_only.status_code == 401
    bad_origin = await client.post(
        "/api/observer/captures",
        headers=_headers(capture_env["credential"], Origin="http://evil"),
        json=body,
    )
    assert bad_origin.status_code == 403
    assert await _observations(capture_env["db"]) == []
    assert not (capture_env["shots"] / LOCAL_CAPTURE_UPLOAD_DIRNAME).exists()


@pytest.mark.asyncio
async def test_accepted_capture_preserves_metadata_private_storage_and_pending_analysis(capture_env):
    client = capture_env["client"]
    captured_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    content = _png((40, 80, 120))
    response = await client.post(
        "/api/observer/captures",
        headers=_headers(capture_env["credential"]),
        json=_body(content, captured_at=captured_at, ocr_text="def main():"),
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["status"] == "accepted"
    assert payload["capture_id"] == "cap-0000000001"

    [observation] = await _observations(capture_env["db"])
    assert observation.id == payload["observation_id"]
    assert observation.app_name == "Code"
    assert observation.window_title == "main.py — seraph"
    stored_ts = observation.timestamp if observation.timestamp.tzinfo else observation.timestamp.replace(tzinfo=timezone.utc)
    assert abs((stored_ts - captured_at).total_seconds()) < 1
    details = json.loads(observation.details_json or "[]")
    assert "daemon_capture_id:cap-0000000001" in details
    assert any(item.startswith("local_ocr:") and "def main()" in item for item in details)
    assert any(item.startswith("screenshot_analysis_status:") and "pending" in item for item in details)

    artifacts = _artifacts(observation)
    assert artifacts["provider"] == "screenshot_folder"
    assert artifacts["source"] == "daemon_local_upload"
    assert artifacts["created_at_source"] == "capture_metadata"
    assert artifacts["screenshot_folder"] == str(capture_env["shots"])
    image_path = Path(str(artifacts["image_path"]))
    upload_root = capture_env["shots"] / LOCAL_CAPTURE_UPLOAD_DIRNAME
    assert image_path.is_relative_to(upload_root)
    assert image_path.read_bytes() == content
    assert stat.S_IMODE(image_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(image_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(upload_root.stat().st_mode) == 0o700
    assert not list(upload_root.rglob("*.partial"))

    # The governed analysis job selects the upload exactly like folder images.
    candidates = await screenshot_folder_source._select_analysis_candidates_with_retry(limit=None)
    assert [item.id for item in candidates] == [observation.id]


@pytest.mark.asyncio
async def test_capture_id_is_idempotent_and_conflicts_on_different_bytes(capture_env):
    client = capture_env["client"]
    content = _png((1, 2, 3))
    first = await client.post("/api/observer/captures", headers=_headers(capture_env["credential"]), json=_body(content))
    again = await client.post("/api/observer/captures", headers=_headers(capture_env["credential"]), json=_body(content))
    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json()["status"] == "duplicate"
    assert again.json()["observation_id"] == first.json()["observation_id"]
    conflict = await client.post(
        "/api/observer/captures",
        headers=_headers(capture_env["credential"]),
        json=_body(_png((200, 2, 3))),
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "capture_id_conflict"
    assert len(await _observations(capture_env["db"])) == 1
    files = list((capture_env["shots"] / LOCAL_CAPTURE_UPLOAD_DIRNAME).rglob("*.png"))
    assert len(files) == 1 and files[0].read_bytes() == content


@pytest.mark.asyncio
async def test_size_and_type_caps(capture_env, monkeypatch):
    client = capture_env["client"]
    headers = _headers(capture_env["credential"])
    not_image = await client.post("/api/observer/captures", headers=headers, json=_body(b"plain text pretending"))
    assert not_image.status_code == 415
    gif = await client.post("/api/observer/captures", headers=headers, json=_body(b"GIF89a" + b"\x00" * 32, capture_id="cap-gif-0001"))
    assert gif.status_code == 415

    monkeypatch.setattr(local_capture_upload, "MAX_CAPTURE_BYTES", 64)
    too_big_decoded = await client.post(
        "/api/observer/captures", headers=headers, json=_body(_png((5, 5, 5)), capture_id="cap-big-00001")
    )
    assert too_big_decoded.status_code == 413
    monkeypatch.setattr(local_capture_upload, "MAX_REQUEST_BYTES", 128)
    too_big_request = await client.post(
        "/api/observer/captures", headers=headers, json=_body(_png((6, 6, 6)), capture_id="cap-big-00002")
    )
    assert too_big_request.status_code == 413
    assert await _observations(capture_env["db"]) == []


@pytest.mark.asyncio
async def test_blocklisted_app_rejected_without_storing(capture_env):
    response = await capture_env["client"].post(
        "/api/observer/captures",
        headers=_headers(capture_env["credential"]),
        json=_body(_png((9, 9, 9)), app="1Password 8"),
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "sensitive_app_blocked"
    assert await _observations(capture_env["db"]) == []
    assert not list((capture_env["shots"]).rglob("*.png"))


@pytest.mark.asyncio
async def test_disabled_screen_capture_and_rate_bound(capture_env, monkeypatch):
    client = capture_env["client"]
    headers = _headers(capture_env["credential"])
    monkeypatch.setenv("SERAPH_SCREEN_ANALYSIS_ENABLED", "0")
    disabled = await client.post("/api/observer/captures", headers=headers, json=_body(_png((7, 7, 7))))
    assert disabled.status_code == 403
    assert disabled.json()["detail"]["code"] == "screen_capture_disabled"
    monkeypatch.setenv("SERAPH_SCREEN_ANALYSIS_ENABLED", "1")

    monkeypatch.setattr(local_capture_upload.upload_rate_limiter, "max_events", 1)
    first = await client.post("/api/observer/captures", headers=headers, json=_body(_png((8, 8, 8))))
    limited = await client.post(
        "/api/observer/captures", headers=headers, json=_body(_png((90, 8, 8)), capture_id="cap-0000000002")
    )
    assert first.status_code == 201
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"


@pytest.mark.asyncio
async def test_shared_dedupe_suppresses_exact_and_near_duplicates(capture_env):
    client = capture_env["client"]
    headers = _headers(capture_env["credential"])
    base = _striped_png(0)
    first = await client.post("/api/observer/captures", headers=headers, json=_body(base, capture_id="cap-dedupe-01"))
    assert first.status_code == 201
    exact = await client.post("/api/observer/captures", headers=headers, json=_body(base, capture_id="cap-dedupe-02"))
    assert exact.status_code == 200
    assert exact.json()["status"] == "suppressed"
    assert exact.json()["reason_code"] == "exact_hash_duplicate"
    near = await client.post(
        "/api/observer/captures", headers=headers, json=_body(_striped_png(7), capture_id="cap-dedupe-03")
    )
    assert near.status_code == 200
    assert near.json()["reason_code"] == "near_visual_duplicate"

    [observation] = await _observations(capture_env["db"])
    run = next(
        json.loads(item.removeprefix("screenshot_visual_run:"))
        for item in json.loads(observation.details_json or "[]")
        if item.startswith("screenshot_visual_run:")
    )
    assert run["suppressed_count"] == 2
    assert run["suppressed_reasons"] == {"exact_hash_duplicate": 1, "near_visual_duplicate": 1}
    # Suppressed uploads keep no private copy.
    assert len(list((capture_env["shots"] / LOCAL_CAPTURE_UPLOAD_DIRNAME).rglob("*.png"))) == 1

    # Folder scans never re-ingest private uploads as "Screenshot Folder".
    scan = await screenshot_folder_source.scan_screenshot_folder(capture_env["shots"])
    assert scan.scanned == 0 and scan.ingested == 0
    assert len(await _observations(capture_env["db"])) == 1


def test_atomic_private_write_leaves_no_partial_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "cap.png"

    def boom(*_args, **_kwargs):
        raise OSError("rename failed")

    monkeypatch.setattr(local_capture_upload.os, "replace", boom)
    with pytest.raises(OSError):
        _atomic_private_write(target, b"\x89PNG\r\n\x1a\nxx")
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []

    monkeypatch.undo()
    _atomic_private_write(target, b"\x89PNG\r\n\x1a\nxx")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["cap.png"]


def test_cleanup_removes_only_expired_uploads(tmp_path):
    shots = tmp_path / "shots"
    upload_day = shots / LOCAL_CAPTURE_UPLOAD_DIRNAME / "2026-01-01"
    upload_day.mkdir(parents=True)
    old_capture = upload_day / "cap-old-0001.png"
    old_partial = upload_day / ".upload-x.partial"
    fresh_capture = shots / LOCAL_CAPTURE_UPLOAD_DIRNAME / "2026-11-13"
    fresh_capture.mkdir(parents=True)
    fresh = fresh_capture / "cap-new-0001.png"
    operator_image = shots / "my-own-screenshot.png"
    for path in (old_capture, old_partial, fresh, operator_image):
        path.write_bytes(b"x")
    ancient = (datetime.now(timezone.utc) - timedelta(days=400)).timestamp()
    for path in (old_capture, old_partial, operator_image):
        os.utime(path, (ancient, ancient))

    deleted = cleanup_local_capture_uploads(retention_days=90, screenshot_root=shots)

    assert deleted == 2
    assert not old_capture.exists() and not old_partial.exists()
    assert not upload_day.exists()
    assert fresh.exists()
    assert operator_image.exists()


def test_backend_blocklist_mirrors_daemon_defaults():
    sys.path.insert(0, str(DAEMON_DIR))
    try:
        from blocklist import DEFAULT_BLOCKLIST
    finally:
        sys.path.remove(str(DAEMON_DIR))
    assert set(DEFAULT_CAPTURE_BLOCKLIST) == set(DEFAULT_BLOCKLIST)
