"""Focused owner-bound browser result preview checks."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from config.settings import settings
from src.api.workflows import (
    _browser_preview_projection,
    get_board_bound_workflow_job,
    _safe_board_job_projection,
)
from src.artifacts.registry import artifact_id_for
from src.browser.task_runner import browser_artifact_path_for_job
from src.db.models import Goal, WorkBoardAttempt, WorkBoardTask, WorkflowRunState
from src.db.session_refs import ensure_sessions_exist


OWNER_PRINCIPAL = "operator:preview"
OWNER_SESSION = "session:preview"


def _result_payload(*, task_id: str, attempt_id: str, value: str = "<script>éplain</script>") -> dict:
    return {
        "schema_version": 1,
        "capability_id": "browser.public-task.v1",
        "task_id": task_id,
        "attempt_id": attempt_id,
        "final_url": "https://public.example/docs",
        "extracts": [
            {
                "action_index": 0,
                "kind": "extract",
                "selector": "main h1",
                "attribute": None,
                "value": value,
            }
        ],
        "checks": [
            {
                "action_index": 0,
                "kind": "text_contains",
                "selector_digest": hashlib.sha256(b"main h1").hexdigest(),
                "expected_digest": hashlib.sha256(b"plain").hexdigest(),
                "actual_digest": hashlib.sha256(b"plain").hexdigest(),
                "passed": True,
            }
        ],
        "request_count": 1,
    }


def _canonical_payload(payload: dict) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _preview_rows(tmp_path: Path, *, payload: dict | None = None):
    task_id = "preview-task"
    attempt_id = "preview-attempt"
    job_id = f"browser-task:{task_id}:{attempt_id}"
    body = _canonical_payload(payload or _result_payload(task_id=task_id, attempt_id=attempt_id))
    path = tmp_path / browser_artifact_path_for_job(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    artifact_id = artifact_id_for(
        file_path=path.relative_to(tmp_path).as_posix(),
        artifact_type="browser_public_task_result",
        producer="browser_public_task",
        run_id=job_id,
        content_sha256=digest,
    )
    readback_id = "readback-" + hashlib.sha256(
        json.dumps(
            {"job_id": job_id, "path": path.relative_to(tmp_path).as_posix(), "digest": digest},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:32]
    task = WorkBoardTask(
        task_id=task_id,
        owner_principal_id=OWNER_PRINCIPAL,
        owner_session_id=OWNER_SESSION,
        goal_id="goal-preview",
        goal_revision=1,
        title="Preview",
        idempotency_key=task_id,
        capability_id="browser.public-task.v1",
    )
    attempt = WorkBoardAttempt(
        task_id=task_id,
        attempt_id=attempt_id,
        workflow_run_id=job_id,
    )
    goal = Goal(
        id="goal-preview",
        title="Preview goal",
        revision=1,
        owner_principal_id=OWNER_PRINCIPAL,
        owner_session_id=OWNER_SESSION,
    )
    run = WorkflowRunState(
        run_identity=job_id,
        root_run_identity=job_id,
        workflow_name="browser_public_task",
        status="succeeded",
        owner_kind="service",
        owner_principal_id="service:browser-task",
        service_id="service:browser-task",
        session_id=OWNER_SESSION,
        operator_session_id=OWNER_SESSION,
        goal_id=task.goal_id,
        goal_revision=task.goal_revision,
        job_kind="browser_public_task",
        capability_version="1",
        artifact_receipts_json=json.dumps(
            [
                {
                    "artifact_id": artifact_id,
                    "artifact_type": "browser_public_task_result",
                    "file_path": path.relative_to(tmp_path).as_posix(),
                    "content_sha256": digest,
                    "size_bytes": len(body),
                    "exists": True,
                }
            ]
        ),
        effect_receipts_json=json.dumps(
            [
                {
                    "receipt_kind": "readback",
                    "effect_type": "browser_public_task_result",
                    "status": "succeeded",
                    "target_path": path.relative_to(tmp_path).as_posix(),
                    "target_digest": digest,
                    "content_sha256": digest,
                    "readback_id": readback_id,
                    "verified_at": "2026-09-30T12:00:00+00:00",
                    "details": {"verified": True, "size_bytes": len(body)},
                },
                {
                    "receipt_kind": "effect",
                    "effect_type": "browser_context_cleanup",
                    "status": "succeeded",
                    "details": {
                        "cleanup_status": "cleanup_verified",
                        "context_not_started": False,
                        "memory_status": "no_learning",
                    },
                },
            ]
        ),
    )
    return run, task, attempt, goal, body, digest, readback_id


def test_browser_preview_returns_typed_extract_without_raw_selector(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    run, task, attempt, goal, _body, digest, readback_id = _preview_rows(tmp_path)

    preview = _browser_preview_projection(run, task=task, attempt=attempt, goal=goal)

    assert preview is not None
    assert preview["content_sha256"] == digest
    assert preview["readback_id"] == readback_id
    assert preview["file_path"] == browser_artifact_path_for_job(run.run_identity)
    assert preview["extracts"] == [
        {
            "action_index": 0,
            "kind": "extract",
            "attribute": None,
            "value": "<script>éplain</script>",
            "selector_digest": hashlib.sha256(b'"main h1"').hexdigest(),
        }
    ]
    assert "selector" not in preview["extracts"][0]
    assert "final_url" not in preview


@pytest.mark.parametrize("mutation", ["tamper", "wrong_readback", "wrong_artifact"])
def test_browser_preview_fail_closed_for_receipt_or_file_tampering(tmp_path, monkeypatch, mutation):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    run, task, attempt, goal, body, digest, _readback_id = _preview_rows(tmp_path)
    path = tmp_path / browser_artifact_path_for_job(run.run_identity)
    if mutation == "tamper":
        path.write_bytes(body + b"tampered")
    elif mutation == "wrong_readback":
        effects = json.loads(run.effect_receipts_json)
        effects[0]["readback_id"] = "readback-foreign"
        run.effect_receipts_json = json.dumps(effects)
    else:
        artifacts = json.loads(run.artifact_receipts_json)
        artifacts[0]["artifact_id"] = "art_" + "f" * 24
        run.artifact_receipts_json = json.dumps(artifacts)

    assert _browser_preview_projection(run, task=task, attempt=attempt, goal=goal) is None


def test_browser_preview_requires_durable_session_and_rejects_unicode_surrogates(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    run, task, attempt, goal, _body, _digest, _readback_id = _preview_rows(tmp_path)
    run.session_id = "foreign-session"
    assert _browser_preview_projection(run, task=task, attempt=attempt, goal=goal) is None

    run, task, attempt, goal, _body, _digest, _readback_id = _preview_rows(
        tmp_path,
        payload=_result_payload(task_id="preview-task", attempt_id="preview-attempt", value="\ud800"),
    )
    assert _browser_preview_projection(run, task=task, attempt=attempt, goal=goal) is None


def test_browser_preview_rejects_final_component_and_parent_symlinks(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    run, task, attempt, goal, body, _digest, _readback_id = _preview_rows(tmp_path)
    path = tmp_path / browser_artifact_path_for_job(run.run_identity)
    outside = tmp_path / "outside.json"
    outside.write_bytes(body)
    path.unlink()
    path.symlink_to(outside)
    assert _browser_preview_projection(run, task=task, attempt=attempt, goal=goal) is None

    path.unlink()
    browser_dir = path.parent
    moved = tmp_path / "moved-browser"
    browser_dir.rename(moved)
    browser_dir.symlink_to(moved, target_is_directory=True)
    assert _browser_preview_projection(run, task=task, attempt=attempt, goal=goal) is None


def test_browser_preview_rejects_oversized_result(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    value = "x" * 65_000
    run, task, attempt, goal, _body, _digest, _readback_id = _preview_rows(
        tmp_path,
        payload=_result_payload(task_id="preview-task", attempt_id="preview-attempt", value=value),
    )
    # The canonical writer's 64 KiB body bound is authoritative even when the
    # receipt pair is internally consistent.
    assert _browser_preview_projection(run, task=task, attempt=attempt, goal=goal) is None


@pytest.mark.parametrize("mutation", [None, "foreign_id", "foreign_job", "foreign_path", "foreign_digest", "naive_time", "unverified", "missing_cleanup"])
def test_safe_browser_artifact_projection_joins_only_matching_readback(tmp_path, mutation):
    run, _task, _attempt, _goal, _body, _digest, readback_id = _preview_rows(tmp_path)
    effects = json.loads(run.effect_receipts_json)
    if mutation == "foreign_id":
        effects[0]["readback_id"] = "readback-foreign"
    elif mutation == "foreign_job":
        effects[0]["job_id"] = "browser-task:foreign:attempt"
    elif mutation == "foreign_path":
        effects[0]["target_path"] = "artifacts/work-board/browser/result-" + "f" * 32 + ".json"
    elif mutation == "foreign_digest":
        effects[0]["content_sha256"] = "f" * 64
    elif mutation == "naive_time":
        effects[0]["verified_at"] = "2026-09-30T17:00:00"
    elif mutation == "unverified":
        effects[0]["details"]["verified"] = False
    elif mutation == "missing_cleanup":
        effects = effects[:1]
    run.effect_receipts_json = json.dumps(effects)
    artifact = _safe_board_job_projection(run)["artifacts"][0]
    if mutation is None:
        assert artifact["readback_id"] == readback_id
        assert artifact["verified"] is True
        assert artifact["verified_at"]
    else:
        assert "readback_id" not in artifact


def test_safe_job_projection_preserves_only_verified_readback_identity():
    run = WorkflowRunState(
        run_identity="browser-task:projection:attempt",
        root_run_identity="browser-task:projection:attempt",
        workflow_name="browser_public_task",
        effect_receipts_json=json.dumps(
            [
                {
                    "receipt_kind": "readback",
                    "readback_id": "readback-safe",
                    "verified_at": "2026-09-30T12:00:00+00:00",
                    "status": "succeeded",
                    "details": {"verified": True},
                },
                {
                    "receipt_kind": "readback",
                    "readback_id": "readback-unsafe",
                    "verified_at": "not-a-time",
                    "status": "succeeded",
                    "details": {"verified": False},
                },
            ]
        ),
    )

    effects = _safe_board_job_projection(run)["effects"]

    assert effects[0]["readback_id"] == "readback-safe"
    assert effects[0]["verified_at"] == "2026-09-30T12:00:00+00:00"
    assert "readback_id" not in effects[1]
    assert "verified_at" not in effects[1]


@pytest.mark.asyncio
async def test_browser_preview_route_is_opt_in_and_owner_bound(async_db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    run, task, attempt, goal, _body, _digest, _readback_id = _preview_rows(tmp_path)
    monkeypatch.setattr(
        "src.api.workflows._require_authenticated_capability_operator",
        lambda _request: SimpleNamespace(
            principal=SimpleNamespace(principal_id=OWNER_PRINCIPAL),
            session_id=OWNER_SESSION,
        ),
    )
    async with async_db() as db:
        db.add(goal)
        await db.flush()
        db.add(task)
        await db.flush()
        # Production durable-job admission creates the placeholder session
        # before inserting its session-bound run state.  Keep this fixture on
        # the same FK and owner/session binding path.
        await ensure_sessions_exist(db, [OWNER_SESSION])
        db.add_all([attempt, run])
        await db.commit()

    request = SimpleNamespace()
    # Direct Python calls do not pass through FastAPI's dependency coercion;
    # Query(False) would otherwise be a truthy object in this unit test.
    metadata = await get_board_bound_workflow_job(
        run.run_identity,
        request,
        include_browser_result=False,
    )
    assert "browser_result" not in metadata["job"]

    response = await get_board_bound_workflow_job(
        run.run_identity,
        request,
        include_browser_result=True,
    )
    body = response["job"]
    assert body["browser_result_status"] == "available"
    assert body["browser_result"]["readback_id"].startswith("readback-")
    assert body["effects"][0]["readback_id"]
