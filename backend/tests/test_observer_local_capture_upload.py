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
    # Native daemon ingress: no Origin, no cookie.
    headers = {"X-Seraph-Node-Device": DEVICE, "X-Seraph-Node-Pairing": PAIRING}
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential}"
    headers.update(overrides)
    return headers


def _body(content: bytes, *, capture_id: str = "cap-0000000001", app: str = "Code", title: str = "main.py — seraph",
          captured_at: datetime | None = None, local_analysis: dict | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "capture_id": capture_id,
        "app_name": app,
        "window_title": title,
        "captured_at": (captured_at or datetime.now(timezone.utc)).isoformat(),
        "content_base64": base64.b64encode(content).decode("ascii"),
    }
    if local_analysis is not None:
        payload["local_analysis"] = local_analysis
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
        headers={"Authorization": f"Bearer {capture_env['credential']}"},
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
        json=_body(
            content,
            captured_at=captured_at,
            local_analysis={"provider": "apple-vision", "summary": "def main():", "text": "def main():\n    pass"},
        ),
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
    assert any(item.startswith("local_analysis:") and "def main()" in item for item in details)
    assert observation.summary == "def main():"
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
@pytest.mark.parametrize("provider", ["codex-local", "openrouter"])
async def test_removed_or_remote_local_analysis_provider_is_rejected(capture_env, provider):
    # ADR-035: codex-local (a cloud-backed Codex CLI) was removed on
    # 2026-11-14; only on-device apple-vision analysis may ride an upload.
    response = await capture_env["client"].post(
        "/api/observer/captures",
        headers=_headers(capture_env["credential"]),
        json=_body(_png((12, 34, 56)), local_analysis={"provider": provider, "summary": "x"}),
    )
    assert response.status_code == 422
    assert await _observations(capture_env["db"]) == []
    assert not list((capture_env["shots"]).rglob("*.png"))


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


async def _set_pairing_policy(**changes) -> None:
    from src.extensions.state import load_extension_state_payload, save_extension_state_payload

    payload = load_extension_state_payload()
    entry = payload["extensions"]["seraph.openclaw-device-bridge"]["node_pairings"]["connectors/nodes/device.yaml"]
    entry["policy"].update(changes)
    save_extension_state_payload(payload, expected_revision=int(payload.get("revision") or 0))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"allowed_capabilities": ["presence.read"]}, "capability_scope_not_allowed"),
        ({"allowed_data_purposes": ["presence"]}, "data_purpose_not_allowed"),
        ({"allowed_media_types": ["image/jpeg"]}, "media_type_not_allowed_by_pairing_policy"),
        ({"max_content_bytes": 64}, "content_size_exceeds_pairing_policy"),
    ],
)
async def test_pairing_policy_is_enforced(capture_env, changes, code):
    await _set_pairing_policy(**changes)
    response = await capture_env["client"].post(
        "/api/observer/captures", headers=_headers(capture_env["credential"]), json=_body(_png((3, 4, 5)))
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == code
    assert await _observations(capture_env["db"]) == []


@pytest.mark.asyncio
async def test_origin_rules_for_native_ingress(capture_env):
    client = capture_env["client"]
    credential = capture_env["credential"]
    # Native: no Origin, no cookie -> accepted on the bearer credential.
    ok = await client.post("/api/observer/captures", headers=_headers(credential), json=_body(_png((11, 12, 13))))
    assert ok.status_code == 201, ok.text
    # Any Origin present must still be an allowed origin.
    foreign = await client.post(
        "/api/observer/captures",
        headers=_headers(credential, Origin="http://evil"),
        json=_body(_png((14, 12, 13)), capture_id="cap-origin-0002"),
    )
    assert foreign.status_code == 403
    assert foreign.json()["detail"]["code"] == "origin_forbidden"
    # A cookie-bearing request without Origin keeps the browser rule.
    with_cookie = await client.post(
        "/api/observer/captures",
        headers=_headers(credential, Cookie="seraph_session=x"),
        json=_body(_png((15, 12, 13)), capture_id="cap-origin-0003"),
    )
    assert with_cookie.status_code == 403
    assert with_cookie.json()["detail"]["code"] == "mutation_origin_required"
    # Missing credential is never waived by the Origin exemption.
    no_credential = await client.post(
        "/api/observer/captures", headers=_headers(None), json=_body(_png((16, 12, 13)), capture_id="cap-origin-0004")
    )
    assert no_credential.status_code == 401
    # Host allow-list still applies.
    bad_host = await client.post(
        "/api/observer/captures",
        headers=_headers(credential, Host="evil.example"),
        json=_body(_png((17, 12, 13)), capture_id="cap-origin-0005"),
    )
    assert bad_host.status_code == 403


@pytest.mark.asyncio
async def test_pixel_limit_checked_from_header(capture_env, monkeypatch):
    monkeypatch.setattr(local_capture_upload, "MAX_IMAGE_PIXELS", 100)
    response = await capture_env["client"].post(
        "/api/observer/captures", headers=_headers(capture_env["credential"]), json=_body(_png((1, 1, 1)))
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "image_dimensions_too_large"
    truncated = await capture_env["client"].post(
        "/api/observer/captures",
        headers=_headers(capture_env["credential"]),
        json=_body(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, capture_id="cap-trunc-0001"),
    )
    assert truncated.status_code == 415


@pytest.mark.asyncio
async def test_idempotency_lookup_is_exact(capture_env):
    client = capture_env["client"]
    headers = _headers(capture_env["credential"])
    longer = await client.post("/api/observer/captures", headers=headers,
                               json=_body(_png((20, 0, 0)), capture_id="cap-00000000012"))
    prefix = await client.post("/api/observer/captures", headers=headers,
                               json=_body(_png((0, 200, 0)), capture_id="cap-0000000001"))
    wildcard_seed = await client.post("/api/observer/captures", headers=headers,
                                      json=_body(_png((0, 0, 220)), capture_id="capXwildcard01"))
    wildcard = await client.post("/api/observer/captures", headers=headers,
                                 json=_body(_png((120, 120, 0)), capture_id="cap_wildcard01"))
    assert [r.status_code for r in (longer, prefix, wildcard_seed, wildcard)] == [201, 201, 201, 201]
    assert len({r.json()["observation_id"] for r in (longer, prefix, wildcard_seed, wildcard)}) == 4


@pytest.mark.asyncio
async def test_symlinked_capture_store_is_refused_and_not_cleaned(capture_env, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    victim = elsewhere / "victim.png"
    victim.write_bytes(b"x")
    ancient = (datetime.now(timezone.utc) - timedelta(days=400)).timestamp()
    os.utime(victim, (ancient, ancient))
    capture_env["shots"].mkdir(parents=True, exist_ok=True)
    (capture_env["shots"] / LOCAL_CAPTURE_UPLOAD_DIRNAME).symlink_to(elsewhere, target_is_directory=True)

    response = await capture_env["client"].post(
        "/api/observer/captures", headers=_headers(capture_env["credential"]), json=_body(_png((30, 30, 30)))
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "capture_store_unsafe"
    assert cleanup_local_capture_uploads(retention_days=1, screenshot_root=capture_env["shots"]) == 0
    assert victim.exists()
    assert [p.name for p in elsewhere.iterdir()] == ["victim.png"]


@pytest.mark.asyncio
async def test_uploaded_local_analysis_feeds_digest_report_and_status(capture_env):
    from zoneinfo import ZoneInfo

    from src.api.settings import _screenshot_folder_pipeline_summary
    from src.scheduler.jobs import end_of_day_goal_report, screenshot_observation_digest as digest

    captured_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    response = await capture_env["client"].post(
        "/api/observer/captures",
        headers=_headers(capture_env["credential"]),
        json=_body(
            _png((50, 60, 70)),
            captured_at=captured_at,
            local_analysis={
                "provider": "apple-vision",
                "activity": "coding",
                "project": "seraph",
                "summary": "Reviewing the capture upload diff",
                "details": ["file: local_capture_upload.py"],
                "text": "def ingest_local_capture(",
            },
        ),
    )
    assert response.status_code == 201, response.text

    start = captured_at - timedelta(minutes=5)
    end = captured_at + timedelta(minutes=5)
    observations = await digest._screenshot_observations(start, end)
    assert [obs.id for obs in observations] == [response.json()["observation_id"]]
    payload = digest._digest_payload(start, end, observations, digest_key="k")
    [record] = payload["records"]
    assert record["analysis"] == {}
    assert record["activity_type"] == "coding" and record["project"] == "seraph"
    assert record["local_analysis"]["text"] == "def ingest_local_capture("
    assert record["local_analysis"]["summary"] == "Reviewing the capture upload diff"
    assert "content_base64" not in json.dumps(payload)

    summary = await end_of_day_goal_report._screen_summary_for_local_day(captured_at.date(), ZoneInfo("UTC"))
    assert "Reviewing the capture upload diff" in summary["samples"]
    assert summary["by_project"].get("seraph") is not None

    status = await _screenshot_folder_pipeline_summary(capture_env["shots"])
    assert status["total_observation_count"] == 1
