"""Focused lifecycle and trust-boundary coverage for the public browser task.

The normal browser-runtime suite exercises the happy path and injected browser
fixture.  This module keeps the recovery boundaries that are easy to lose in
integration wiring close to the real durable repository and transport: a
persisted running root must not be replayed, a live Work Board fence must stop
the next action, and hostile request/response shapes must be rejected before
they become a browser effect.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

import src.api.work_board as work_board_api
from config.settings import settings
from src.browser.pinned_transport import (
    PinnedBrowserRequest,
    PinnedBrowserResponse,
    PinnedBrowserTransport,
    PinnedTransportError,
)
from src.browser.task_runner import BrowserTaskInput, BrowserTaskRunner
from src.db.models import Goal, WorkBoardAttempt, WorkBoardStatus, WorkBoardTask, WorkflowRunState
from src.goals.contracts import GoalAdmissionBudget
from src.goals.repository import serialize_admission_budget
from src.security.site_policy import SiteAccessDecision
from src.work_board.contracts import WorkBoardInputArtifactCreate, WorkBoardOwner
from src.work_board.input_artifacts import (
    bind_input_artifact,
    consume_input_artifact,
    prepare_input_artifact,
    resolve_input_artifact_for_task,
)
from src.work_board.dispatcher import WorkBoardDispatcher, registered_executor_id
from src.work_board.repository import WorkBoardRepository
from src.workflows.job_runtime import DurableJobRepository

from tests.test_browser_task_runtime import (
    FakeBrowser,
    FakeJobs,
    FakePage,
    _input,
)


def _digest(inputs: Any | None = None) -> str:
    """Build the server-owned digest for the typed browser input envelope."""

    model = BrowserTaskInput.model_validate(inputs if inputs is not None else _input())
    envelope = {
        "schema_version": 1,
        "capability_id": "browser.public-task.v1",
        "input": model.model_dump(mode="json", exclude_none=True),
    }
    return hashlib.sha256(
        json.dumps(envelope, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _responses() -> dict[str, PinnedBrowserResponse]:
    return {
        "https://fixture.example/docs": PinnedBrowserResponse(
            200,
            {"content-type": "text/html"},
            b"docs",
            "https://fixture.example/docs",
            "93.184.216.34",
        ),
        "https://fixture.example/reference": PinnedBrowserResponse(
            200,
            {"content-type": "text/html"},
            b"reference",
            "https://fixture.example/reference",
            "93.184.216.34",
        ),
    }


def _html_responses() -> dict[str, PinnedBrowserResponse]:
    """Return HTML bodies that exercise Chromium with the injected transport."""

    return {
        "https://fixture.example/docs": PinnedBrowserResponse(
            200,
            {"content-type": "text/html; charset=utf-8"},
            b"<!doctype html><main><h1>Docs</h1></main>",
            "https://fixture.example/docs",
            "93.184.216.34",
        ),
        "https://fixture.example/reference": PinnedBrowserResponse(
            200,
            {"content-type": "text/html; charset=utf-8"},
            b"<!doctype html><main><h1>Reference</h1></main>",
            "https://fixture.example/reference",
            "93.184.216.34",
        ),
    }


def _fixture_runner(
    *,
    jobs: Any,
    browser: Any,
    workspace_root: Path,
    runtime_controls: Any,
) -> BrowserTaskRunner:
    responses = _responses()

    async def fixture(request: PinnedBrowserRequest) -> PinnedBrowserResponse:
        return responses[request.url]

    async def fixture_policy(url: str, **_: Any) -> SiteAccessDecision:
        return SiteAccessDecision(allowed=True, hostname="fixture.example")

    async def fixture_resolver(*_: Any) -> list[str]:
        return ["93.184.216.34"]

    return BrowserTaskRunner(
        jobs=jobs,
        browser_launcher=lambda: browser,
        runtime_controls=runtime_controls,
        transport_factory=lambda: PinnedBrowserTransport(
            resolver=fixture_resolver,
            injected_fetch=fixture,
            site_policy=fixture_policy,
        ),
        workspace_root=workspace_root,
    )


def _run_args(
    *,
    task_id: str,
    attempt_id: str,
    admission_only: bool,
    inputs: Any | None = None,
) -> dict[str, Any]:
    typed_inputs = inputs if inputs is not None else _input()
    return {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "owner_principal_id": "operator-lifecycle",
        "owner_session_id": "session-lifecycle",
        "goal_id": "goal-lifecycle",
        "goal_revision": 1,
        "board_task_revision": 7,
        "board_fencing_token": 4,
        "input_artifact_id": f"artifact-{task_id}",
        "input_artifact_digest": _digest(typed_inputs),
        "inputs": typed_inputs,
        "runtime_seconds": 180,
        "admission_only": admission_only,
        "task_priority": 87,
        "admission_board_task_revision": 7,
    }


def _proc_snapshot() -> dict[int, dict[str, Any]]:
    """Return process identity data used to distinguish PID reuse."""

    snapshot: dict[int, dict[str, Any]] = {}
    proc_root = Path("/proc")
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text()
            closing = raw.rfind(")")
            if closing < 0:
                continue
            name = raw[raw.find("(") + 1 : closing]
            fields = raw[closing + 2 :].split()
            snapshot[int(entry.name)] = {
                "name": name,
                "state": fields[0],
                "parent": int(fields[1]),
                "start_ticks": int(fields[19]),
            }
        except (FileNotFoundError, PermissionError, ValueError, IndexError):
            continue
    return snapshot


def _process_descendants(snapshot: dict[int, dict[str, Any]], root_pid: int) -> set[int]:
    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, item in snapshot.items():
            if pid not in descendants and item["parent"] in descendants:
                descendants.add(pid)
                changed = True
    return descendants


@pytest.mark.skipif(
    os.environ.get("SERAPH_RUN_REAL_CHROMIUM_LIFECYCLE") != "1",
    reason="installed-Chromium worker-death proof is an explicit opt-in integration test",
)
def test_real_chromium_worker_death_reaps_exact_process_identities(tmp_path: Path) -> None:
    """A killed worker must not leave its Playwright driver or Chromium tree alive."""

    try:
        import playwright  # noqa: F401
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        pytest.skip("Playwright is not installed")

    ready_path = tmp_path / "ready.json"
    worker = r'''
import asyncio
import json
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright


async def main():
    ready = Path(sys.argv[1])
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            java_script_enabled=False,
            service_workers="block",
            accept_downloads=False,
        )
        page = await context.new_page()
        await page.set_content("<main>lifecycle fixture</main>")
        driver = getattr(getattr(playwright, "_impl_obj", None), "_connection", None)
        driver = getattr(getattr(driver, "_transport", None), "_proc", None)
        ready.write_text(json.dumps({
            "worker_pid": os.getpid(),
            "driver_pid": getattr(driver, "pid", None),
        }))
        await asyncio.Event().wait()


asyncio.run(main())
'''
    child = subprocess.Popen(
        [sys.executable, "-c", worker, str(ready_path)],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    identities: dict[int, tuple[int, int]] = {}
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and not ready_path.exists():
            time.sleep(0.05)
        assert ready_path.exists(), "Chromium worker did not publish its process identity"
        ready = json.loads(ready_path.read_text())
        assert ready["worker_pid"] == child.pid
        before = _proc_snapshot()
        descendants = _process_descendants(before, child.pid)
        for pid in descendants:
            item = before.get(pid)
            if item is not None:
                identities[pid] = (pid, item["start_ticks"])
        driver_pid = ready.get("driver_pid")
        assert driver_pid in identities, "Playwright driver identity was not observed"
        assert any("chrome" in str(before[pid]["name"]).lower() for pid in descendants if pid in before)

        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=5)
        cleanup_deadline = time.monotonic() + 10
        while time.monotonic() < cleanup_deadline:
            current = _proc_snapshot()
            if all(
                pid not in current or current[pid]["start_ticks"] != start_ticks
                for pid, start_ticks in identities.values()
            ):
                break
            time.sleep(0.1)
        current = _proc_snapshot()
        assert all(
            pid not in current or current[pid]["start_ticks"] != start_ticks
            for pid, start_ticks in identities.values()
        ), "a Playwright/Chromium process survived worker death"
    finally:
        if child.poll() is None:
            os.kill(child.pid, signal.SIGKILL)
            child.wait(timeout=5)


@pytest.mark.asyncio
async def test_persisted_running_root_with_canonical_goal_and_board_never_relaunches(
    async_db: Any,
    tmp_path: Path,
) -> None:
    """A dispatched running root is reconciled, never started in a second context."""

    task_id = "task-lifecycle-running"
    attempt_id = "attempt-lifecycle-running"
    job_id = f"browser-task:{task_id}:{attempt_id}"
    owner = "browser-task-service:" + attempt_id

    async with async_db() as db:
        db.add(
            Goal(
                id="goal-lifecycle",
                title="Browser lifecycle goal",
                status="active",
                revision=1,
                owner_principal_id="operator-lifecycle",
                owner_session_id="session-lifecycle",
            )
        )
        db.add(
            WorkBoardTask(
                task_id=task_id,
                owner_principal_id="operator-lifecycle",
                owner_session_id="session-lifecycle",
                goal_id="goal-lifecycle",
                goal_revision=1,
                title="Persisted browser task",
                body="lifecycle recovery",
                capability_id="browser.public-task.v1",
                input_artifact_id=f"artifact-{task_id}",
                typed_input_ref=f"artifact-{task_id}",
                typed_input_digest=_digest(),
                executor_id="browser-task",
                priority=87,
                idempotency_scope="browser-task",
                idempotency_key=task_id,
                status=WorkBoardStatus.ready,
                task_revision=7,
            )
        )

    async with async_db() as db:
        initial_goal = await db.get(Goal, "goal-lifecycle")
        initial_task = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        ).scalars().one_or_none()
        assert initial_goal is not None and initial_task is not None

    jobs = DurableJobRepository()
    launches = 0

    def launcher() -> Any:
        nonlocal launches
        launches += 1
        raise AssertionError("a persisted running root must not launch a second context")

    runner = BrowserTaskRunner(
        jobs=jobs,
        browser_launcher=launcher,
        runtime_controls=lambda **_: True,
        workspace_root=tmp_path,
    )
    admission = await runner.run(**_run_args(task_id=task_id, attempt_id=attempt_id, admission_only=True))
    assert admission["status"] == "admitted", admission
    assert admission["job_id"] == job_id
    assert admission["durable_status"] in {"accepted", "queued"}

    async with async_db() as db:
        db.add(
            WorkBoardAttempt(
                attempt_id=attempt_id,
                task_id=task_id,
                workflow_run_id=job_id,
                task_revision_at_claim=7,
                lease_owner=owner,
                fencing_token=4,
                executor_id="browser-task",
                started_at=datetime.now(timezone.utc),
            )
        )
        linked_task = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        ).scalars().one_or_none()
        assert linked_task is not None
        linked_task.task_revision = 8
        linked_task.status = WorkBoardStatus.running

    admitted_row = await jobs.get_job(job_id)
    assert admitted_row is not None
    queued = await jobs.queue_job(job_id, expected_revision=admitted_row["revision"])
    claimed = await jobs.claim_job(
        job_id,
        owner=owner,
        lease_seconds=180,
        expected_revision=queued["revision"],
        expected_fencing_token=queued["lease"]["fencing_token"],
    )
    checkpointed = await jobs.record_checkpoint(
        job_id,
        checkpoint_id="network-dispatch",
        state={"phase": "network-dispatch", "action_index": 0},
        owner=owner,
        fencing_token=claimed["lease"]["fencing_token"],
        expected_revision=claimed["revision"],
    )
    effected = await jobs.record_effect(
        job_id,
        effect_type="browser_request",
        effect_id="effect-lifecycle-request",
        status="dispatched",
        details={"action_index": 0},
        owner=owner,
        fencing_token=claimed["lease"]["fencing_token"],
        expected_revision=checkpointed["revision"],
    )
    assert effected["status"] == "running"

    execution_args = _run_args(task_id=task_id, attempt_id=attempt_id, admission_only=False)
    # The original claim revision remains 7 in the durable authority while
    # the live Work Board link has advanced to 8.  Re-entry must validate the
    # immutable admission revision and still refuse a second browser launch.
    execution_args["board_task_revision"] = 8
    result = await runner.run(
        **execution_args,
        durable_job_id=job_id,
        durable_lease_owner=owner,
        durable_fencing_token=claimed["lease"]["fencing_token"],
    )
    assert result["status"] == "unknown_external_effect", result
    assert result["reason_code"] == "durable_running_reentry"
    assert launches == 0

    persisted = await jobs.get_job(job_id)
    assert persisted is not None
    assert persisted["status"] == "unknown_external_effect"
    assert any(item.get("checkpoint_id") == "network-dispatch" for item in persisted["checkpoints"])
    assert any(item.get("effect_id") == "effect-lifecycle-request" for item in persisted["effects"])
    async with async_db() as db:
        goal = await db.get(Goal, "goal-lifecycle")
        board_task = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        ).scalars().one_or_none()
        attempt = await db.get(WorkBoardAttempt, attempt_id)
        durable = (
            await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))
        ).scalars().one()
    assert goal is not None and goal.status == "active"
    assert (
        board_task is not None
        and board_task.priority == 87
        and board_task.task_revision == 8
        and board_task.status == WorkBoardStatus.running
    )
    assert attempt is not None and attempt.workflow_run_id == job_id and attempt.task_revision_at_claim == 7
    assert durable.status == "unknown_external_effect" and durable.priority == 87


@pytest.mark.asyncio
async def test_live_runtime_revocation_blocks_before_the_next_action(tmp_path: Path) -> None:
    """A Work Board revocation between actions yields no later browser effect."""

    jobs = FakeJobs()
    browser = FakeBrowser(_responses())
    goto_calls = 0
    navigation_in_progress = False
    original_goto = browser.context.page.goto

    async def counting_goto(*args: Any, **kwargs: Any) -> Any:
        nonlocal goto_calls
        nonlocal navigation_in_progress
        goto_calls += 1
        navigation_in_progress = True
        try:
            return await original_goto(*args, **kwargs)
        finally:
            navigation_in_progress = False

    browser.context.page.goto = counting_goto  # type: ignore[method-assign]
    control_calls = 0

    async def controls(**_: Any) -> bool:
        nonlocal control_calls
        control_calls += 1
        # Initial navigation and the first action are allowed.  Once both
        # navigation boundaries have completed, the next action is rejected
        # before its checkpoint/network boundary.
        return goto_calls < 2 or navigation_in_progress

    runner = _fixture_runner(
        jobs=jobs,
        browser=browser,
        workspace_root=tmp_path,
        runtime_controls=controls,
    )
    admission = await runner.run(**_run_args(task_id="task-revocation", attempt_id="attempt-1", admission_only=True))
    assert admission["status"] == "admitted", admission
    result = await runner.run(**_run_args(task_id="task-revocation", attempt_id="attempt-1", admission_only=False))
    assert result["status"] != "succeeded", result
    assert result["reason_code"] == "board_fence_stale"
    assert goto_calls == 2, "revocation must prevent the second action navigation"
    assert control_calls >= 5


@pytest.mark.asyncio
async def test_forbidden_request_and_response_shapes_fail_closed_before_external_effect() -> None:
    calls: list[str] = []

    async def fixture(request: PinnedBrowserRequest) -> PinnedBrowserResponse:
        calls.append(request.url)
        return PinnedBrowserResponse(
            200,
            {"content-type": "text/html"},
            b"ok",
            request.url,
            "93.184.216.34",
        )

    async def resolve(*_: Any) -> list[str]:
        return ["93.184.216.34"]

    async def allowed(url: str, **_: Any) -> SiteAccessDecision:
        return SiteAccessDecision(allowed=True, hostname="fixture.example")

    def transport() -> PinnedBrowserTransport:
        return PinnedBrowserTransport(resolver=resolve, injected_fetch=fixture, site_policy=allowed)

    base = {
        "allowed_hosts": ["fixture.example"],
        "approved_url_prefixes": ["https://fixture.example/docs"],
    }
    blocked_requests = [
        (PinnedBrowserRequest("https://fixture.example/docs", "POST", {}, "document", True, 0), "method_blocked"),
        (PinnedBrowserRequest("https://fixture.example/docs", "GET", {"cookie": "sid=x"}, "document", True, 0), "header_blocked"),
        (PinnedBrowserRequest("https://fixture.example/docs", "GET", {"authorization": "Bearer x"}, "document", True, 0), "header_blocked"),
        (PinnedBrowserRequest("https://fixture.example/private", "GET", {}, "document", True, 0), "prefix_not_approved"),
    ]
    for request, code in blocked_requests:
        with pytest.raises(PinnedTransportError) as error:
            await transport().resolve_and_fetch(request, **base)
        assert error.value.code == code
    assert calls == [], "request policy violations must not invoke the fixture transport"

    async def forbidden_response(request: PinnedBrowserRequest) -> PinnedBrowserResponse:
        calls.append(request.url)
        return PinnedBrowserResponse(
            200,
            {"content-disposition": "attachment; filename=secret.txt"},
            b"secret",
            request.url,
            "93.184.216.34",
        )

    attachment_transport = PinnedBrowserTransport(
        resolver=resolve,
        injected_fetch=forbidden_response,
        site_policy=allowed,
    )
    with pytest.raises(PinnedTransportError) as attachment:
        await attachment_transport.resolve_and_fetch(
            PinnedBrowserRequest("https://fixture.example/docs", "GET", {}, "document", True, 0),
            **base,
        )
    assert attachment.value.code == "download_blocked"

    async def private_redirect(request: PinnedBrowserRequest) -> PinnedBrowserResponse:
        calls.append(request.url)
        return PinnedBrowserResponse(
            302,
            {"location": "https://127.0.0.1/private"},
            b"",
            request.url,
            "93.184.216.34",
            redirect_location="https://127.0.0.1/private",
        )

    redirect_transport = PinnedBrowserTransport(
        resolver=resolve,
        injected_fetch=private_redirect,
        site_policy=allowed,
    )
    with pytest.raises(PinnedTransportError) as redirect:
        await redirect_transport.resolve_and_fetch(
            PinnedBrowserRequest("https://fixture.example/docs", "GET", {}, "document", True, 0),
            **base,
        )
    assert redirect.value.code in {"redirect_not_approved", "private_address_blocked"}
    assert calls.count("https://fixture.example/docs") == 2


class _HugeLocator:
    first = None

    def __init__(self) -> None:
        self.first = self

    async def inner_text(self) -> str:
        return "x" * 65_536

    async def get_attribute(self, _: str) -> str:
        return "x" * 65_536


class _HugePage(FakePage):
    def locator(self, _: str) -> _HugeLocator:
        return _HugeLocator()


@pytest.mark.asyncio
async def test_expected_check_extract_limit_and_cleanup_failure_never_claim_success(tmp_path: Path) -> None:
    """Verification, aggregate output, and teardown failures stay non-success."""

    cases: list[tuple[str, Any, str]] = []
    expected = _input()
    expected["actions"][0]["expected_checks"][0]["value"] = "/outside"
    cases.append(("expected-check", expected, "expected_check_failed"))

    oversized = _input()
    cases.append(("extract-limit", oversized, "extract_output_limit"))

    for suffix, payload, expected_code in cases:
        jobs = FakeJobs()
        browser = FakeBrowser(_responses())
        if suffix == "extract-limit":
            browser.context.page = _HugePage(_responses())
        runner = _fixture_runner(
            jobs=jobs,
            browser=browser,
            workspace_root=tmp_path / suffix,
            runtime_controls=lambda **_: True,
        )
        admission = await runner.run(
            **_run_args(
                task_id=f"task-{suffix}",
                attempt_id="attempt-1",
                admission_only=True,
                inputs=payload,
            )
        )
        assert admission["status"] == "admitted", admission
        result = await runner.run(
            **_run_args(
                task_id=f"task-{suffix}",
                attempt_id="attempt-1",
                admission_only=False,
                inputs=payload,
            )
        )
        assert result["status"] != "succeeded", result
        assert result["reason_code"] == expected_code
        assert f"transition:succeeded" not in jobs.calls

    jobs = FakeJobs()
    browser = FakeBrowser(_responses(), close_error=RuntimeError("cleanup failure"))
    cleanup_workspace = tmp_path / "cleanup"
    cleanup_workspace.mkdir(parents=True, exist_ok=True)
    runner = _fixture_runner(
        jobs=jobs,
        browser=browser,
        workspace_root=cleanup_workspace,
        runtime_controls=lambda **_: True,
    )
    admission = await runner.run(**_run_args(task_id="task-cleanup", attempt_id="attempt-1", admission_only=True))
    assert admission["status"] == "admitted", admission
    result = await runner.run(**_run_args(task_id="task-cleanup", attempt_id="attempt-1", admission_only=False))
    assert result["status"] == "unknown_external_effect"
    assert result["reason_code"] == "browser_cleanup_failed"
    assert "transition:succeeded" not in jobs.calls


@pytest.mark.asyncio
async def test_runner_handshake_rejects_bad_digest_and_preserves_claim_revision() -> None:
    jobs = FakeJobs()
    runner = BrowserTaskRunner(jobs=jobs, runtime_controls=lambda **_: True)

    bad = await runner.run(
        **{
            **_run_args(task_id="task-bad-digest", attempt_id="attempt-1", admission_only=True),
            "input_artifact_digest": "not-a-sha256",
        }
    )
    assert bad["status"] == "blocked"
    assert bad["reason_code"] == "input_artifact_digest_invalid"
    assert jobs.calls == []

    stale_claim = await runner.run(
        **{
            **_run_args(task_id="task-stale-claim", attempt_id="attempt-1", admission_only=True),
            "admission_board_task_revision": 8,
            "board_task_revision": 7,
        }
    )
    assert stale_claim["status"] == "admitted"
    authority = jobs.rows[stale_claim["job_id"]]["declared_authority"]
    assert authority["priority"] == 87
    assert authority["board_task_revision"] == 7
    assert stale_claim["job_id"] == "browser-task:task-stale-claim:attempt-1"


@pytest.mark.asyncio
async def test_missing_runtime_controls_blocks_before_durable_admission() -> None:
    jobs = FakeJobs()
    runner = BrowserTaskRunner(jobs=jobs)

    receipt = await runner.run(
        **_run_args(task_id="task-no-controls", attempt_id="attempt-1", admission_only=True)
    )
    assert receipt["status"] == "blocked"
    assert receipt["reason_code"] == "runtime_control_required"
    assert jobs.calls == []


@pytest.mark.asyncio
async def test_changed_typed_input_with_original_artifact_digest_is_blocked(tmp_path: Path) -> None:
    """The artifact digest binds the exact typed input envelope, not just its ID."""

    original = _input()
    changed = _input()
    changed["actions"][1]["expected_checks"][0]["value"] = "Different reference"
    jobs = FakeJobs()
    browser = FakeBrowser(_responses())
    runner = _fixture_runner(
        jobs=jobs,
        browser=browser,
        workspace_root=tmp_path,
        runtime_controls=lambda **_: True,
    )
    admission = await runner.run(
        **_run_args(
            task_id="task-input-binding",
            attempt_id="attempt-1",
            admission_only=True,
            inputs=original,
        )
    )
    assert admission["status"] == "admitted", admission

    execution_args = _run_args(
        task_id="task-input-binding",
        attempt_id="attempt-1",
        admission_only=False,
        inputs=changed,
    )
    execution_args["input_artifact_digest"] = _digest(original)
    result = await runner.run(**execution_args)
    assert result["status"] == "blocked", result
    assert result["reason_code"] in {
        "input_artifact_digest_mismatch",
        "input_artifact_binding_stale",
        "input_payload_digest_mismatch",
    }
    assert "claim" not in jobs.calls and "transition:succeeded" not in jobs.calls


@pytest.mark.skipif(
    os.environ.get("SERAPH_RUN_REAL_BROWSER_VERTICAL_SLICE") != "1",
    reason="real Chromium plus canonical DB browser vertical slice is an explicit opt-in integration test",
)
@pytest.mark.asyncio
async def test_real_chromium_durable_board_vertical_slice_projects_verified_browser_receipt(
    async_db,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Exercise Chromium, the injected public transport, and the real board projection together.

    The existing runner tests intentionally use small browser/repository doubles.  This
    opt-in proof keeps the browser fixture transport deterministic while using the actual
    Playwright Chromium process, ``DurableJobRepository``, owner-bound typed input row,
    canonical Goal/Work Board rows, and ``_safe_task_payload`` projection.
    """

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("Playwright is not installed")

    # Use the repository's documented test authentication identity so the
    # dispatcher callback exercises the same active-session/goal/artifact
    # checks as production without manufacturing an auth row in this opt-in
    # integration fixture.
    owner = WorkBoardOwner(
        principal_id="operator:test-bypass",
        session_id="test-auth-bypass",
    )
    task_id = "task-real-browser-vertical"
    goal_id = "goal-real-browser-vertical"
    inputs = _input()
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))

    async with async_db() as db:
        db.add(
            Goal(
                id=goal_id,
                title="Real browser vertical slice",
                status="active",
                revision=1,
                owner_principal_id=owner.principal_id,
                owner_session_id=owner.session_id,
                admission_budget_json=serialize_admission_budget(
                    GoalAdmissionBudget(
                        max_outstanding_jobs=1,
                        max_attempts=2,
                        max_runtime_seconds=180,
                    )
                ),
            )
        )
        await db.flush()

        artifact_request = WorkBoardInputArtifactCreate(
            schema_version=1,
            capability_id="browser.public-task.v1",
            goal_id=goal_id,
            goal_revision=1,
            input=inputs,
            idempotency_key="browser-real-vertical-input",
        )
        artifact = await prepare_input_artifact(db, owner, artifact_request)
        assert artifact.typed_input_digest == _digest(inputs)

        task = WorkBoardTask(
            task_id=task_id,
            owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,
            origin_session_id=owner.session_id,
            goal_id=goal_id,
            goal_revision=1,
            title="Run the browser vertical slice",
            body="Read the public reference page and retain the verified artifact.",
            capability_id="browser.public-task.v1",
            input_artifact_id=artifact.artifact_id,
            typed_input_ref=artifact.typed_input_ref,
            typed_input_digest=artifact.typed_input_digest,
            executor_id=registered_executor_id("browser.public-task.v1"),
            priority=87,
            idempotency_scope="browser-public-task",
            idempotency_key=task_id,
            status=WorkBoardStatus.ready,
            task_revision=1,
        )
        db.add(task)
        await db.flush()
        resolved = await resolve_input_artifact_for_task(
            db,
            owner,
            artifact_id=artifact.artifact_id,
            goal_id=goal_id,
            goal_revision=1,
            capability_id="browser.public-task.v1",
            expected_task_id=task_id,
        )
        await bind_input_artifact(
            db,
            owner,
            artifact=resolved,
            task_id=task_id,
            task_revision=1,
        )
        await db.commit()

    jobs = DurableJobRepository()
    monkeypatch.setattr(work_board_api, "durable_job_repository", jobs)
    repository = WorkBoardRepository()
    dispatcher = WorkBoardDispatcher(repository=repository, jobs=jobs)

    # The board claim is the canonical revision/fence transition.  A hand
    # assembled attempt would hide the production CAS sequence and can make a
    # durable root look admissible at the wrong board revision.
    async with async_db() as db:
        claim = await repository.claim_ready_task(
            db,
            task_id,
            expected_revision=1,
            lease_owner=dispatcher.runner_id,
            lease_seconds=180,
            actor_principal_id=dispatcher.runner_id,
            actor_session_id=dispatcher.runner_session,
        )
    assert claim is not None
    assert claim.task.status is WorkBoardStatus.running
    assert claim.task.task_revision == 2
    assert claim.attempt.task_revision_at_claim == 1
    assert claim.attempt.fencing_token >= 1
    assert claim.attempt.lease_owner == dispatcher.runner_id

    responses = _html_responses()
    control_calls: list[dict[str, Any]] = []

    async def fixture(request: PinnedBrowserRequest) -> PinnedBrowserResponse:
        return responses[request.url]

    async def fixture_policy(url: str, **_: Any) -> SiteAccessDecision:
        assert url.startswith("https://fixture.example/")
        return SiteAccessDecision(allowed=True, hostname="fixture.example")

    async def fixture_resolver(*_: Any) -> list[str]:
        return ["93.184.216.34"]

    async def runtime_controls(**kwargs: Any) -> bool:
        control_calls.append(dict(kwargs))
        return await dispatcher._browser_assert_current(**kwargs)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        runner = BrowserTaskRunner(
            jobs=jobs,
            browser_launcher=lambda: browser,
            runtime_controls=runtime_controls,
            transport_factory=lambda: PinnedBrowserTransport(
                resolver=fixture_resolver,
                injected_fetch=fixture,
                site_policy=fixture_policy,
            ),
            workspace_root=tmp_path,
        )
        admission_args = {
            "task_id": task_id,
            "attempt_id": claim.attempt.attempt_id,
            "owner_principal_id": owner.principal_id,
            "owner_session_id": owner.session_id,
            "goal_id": goal_id,
            "goal_revision": 1,
            "board_task_revision": claim.task.task_revision,
            "board_fencing_token": claim.attempt.fencing_token,
            "admission_board_task_revision": claim.task.task_revision,
            "input_artifact_id": artifact.artifact_id,
            "input_artifact_digest": artifact.typed_input_digest,
            "inputs": inputs,
            "runtime_seconds": 180,
            "task_priority": 87,
            "effective_max_attempts": 2,
            "effective_max_outstanding_jobs": 1,
        }
        admitted = await runner.run(**admission_args, admission_only=True)
        assert admitted["status"] == "admitted", admitted
        admitted_projection = await jobs.get_job(admitted["job_id"])
        assert admitted_projection is not None
        expected_identity = dispatcher._browser_expected_identity(
            claim.task,
            claim.attempt,
            inputs,
            admitted_projection,
            180,
            max_attempts=2,
            max_outstanding_jobs=1,
        )
        async with async_db() as db:
            linked = await repository.link_attempt_workflow_run(
                db,
                task_id,
                claim.attempt.attempt_id,
                workflow_run_id=admitted["job_id"],
                expected_revision=claim.task.task_revision,
                board_fence=claim.attempt.fencing_token,
                lease_owner=dispatcher.runner_id,
                workflow_projection=admitted_projection,
                expected_identity=expected_identity,
                actor_principal_id=dispatcher.runner_id,
                actor_session_id=dispatcher.runner_session,
            )
        assert linked.task.task_revision == 3
        assert linked.attempt.workflow_run_id == admitted["job_id"]
        execution_args = {
            **admission_args,
            "attempt_id": linked.attempt.attempt_id,
            "board_task_revision": linked.task.task_revision,
            "admission_board_task_revision": claim.task.task_revision,
            "durable_job_id": admitted["job_id"],
        }
        executed = await runner.run(**execution_args, admission_only=False)

    assert executed["status"] == "succeeded", executed
    assert control_calls, "the real run must cross the owned runtime-control callback"
    assert executed["readback_id"]
    assert executed["artifact_ref"]
    assert executed["artifact_sha256"]

    projection = await jobs.get_job(executed["job_id"])
    assert projection is not None
    assert projection["status"] == "succeeded"
    lease = projection.get("lease") or {}
    fencing_token = int(lease.get("fencing_token") or 0)
    assert fencing_token > 0
    artifacts = projection.get("artifacts")
    effects = projection.get("effects")
    assert isinstance(artifacts, list) and any(
        item.get("artifact_type") == "browser_public_task_result"
        and item.get("exists") is True
        for item in artifacts
        if isinstance(item, dict)
    )
    assert isinstance(effects, list) and any(
        item.get("receipt_kind") == "readback"
        and item.get("status") == "succeeded"
        and item.get("details", {}).get("verified") is True
        for item in effects
        if isinstance(item, dict)
    )
    assert any(
        item.get("receipt_kind") == "effect"
        and item.get("effect_type") == "browser_context_cleanup"
        and item.get("details", {}).get("memory_status") == "no_learning"
        for item in effects
        if isinstance(item, dict)
    )

    proof = dispatcher._direct_readback(executed, projection, executed["job_id"])
    assert proof is not None
    assert proof["readback_id"] == executed["readback_id"]
    assert proof["content_sha256"] == executed["artifact_sha256"]

    # Consume and settle through the repository's fenced projection path. This
    # keeps the test on the same terminal boundary as the managed dispatcher;
    # direct status/attempt fabrication would only prove the durable runner.
    async with async_db() as db:
        current_task = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        ).scalars().one()
        resolved = await resolve_input_artifact_for_task(
            db,
            owner,
            artifact_id=artifact.artifact_id,
            goal_id=goal_id,
            goal_revision=1,
            capability_id="browser.public-task.v1",
            expected_task_id=task_id,
        )
        await consume_input_artifact(
            db,
            owner,
            task_id=task_id,
            task_revision=int(resolved.row.bound_task_revision or current_task.task_revision),
            artifact_id=resolved.row.artifact_id,
        )

    settled = await dispatcher._project(
        linked.task,
        linked.attempt,
        board_revision=linked.task.task_revision,
        status=WorkBoardStatus.done,
        outcome="verified",
        proof=proof,
        result_refs=[
            {
                "job_id": executed["job_id"],
                "workflow_run_id": executed["job_id"],
                "status": "succeeded",
                "verified": True,
            }
        ],
        artifact_refs=[
            {
                "artifact_id": executed["artifact_ref"],
                "file_path": executed["artifact_ref"],
                "content_sha256": executed["artifact_sha256"],
                "workflow_run_id": executed["job_id"],
                "verified": True,
            }
        ],
    )
    assert settled.task.status is WorkBoardStatus.done

    async with async_db() as db:
        task = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        ).scalars().one()
        attempt = (
            await db.execute(
                select(WorkBoardAttempt).where(
                    WorkBoardAttempt.task_id == task_id,
                    WorkBoardAttempt.attempt_id == linked.attempt.attempt_id,
                )
            )
        ).scalar_one()

        payload = await work_board_api._safe_task_payload(
            task,
            db=db,
            latest_attempt=attempt,
        )
        browser_execution = payload["latest_attempt"]["browser_execution"]
        assert browser_execution["capability_id"] == "browser.public-task.v1"
        assert browser_execution["job_id"] == executed["job_id"]
        assert browser_execution["durable_status"] == "succeeded"
        assert browser_execution["action_count"] == len(inputs["actions"])
        # The projection deliberately leaves a counter unknown when the
        # newest durable checkpoint does not carry that counter; accepting a
        # fabricated value here would hide the same stale-progress bug this
        # DTO is meant to prevent.
        assert browser_execution["action_index"] is None or browser_execution["action_index"] in range(-1, 8)
        assert browser_execution["request_count"] is None or browser_execution["request_count"] in range(0, 33)
        assert browser_execution["cleanup_status"] == "cleanup_verified"
        assert browser_execution["memory_status"] == "no_learning"
        assert browser_execution["readback_id"] == executed["readback_id"]
        assert browser_execution["artifact_id"]
        assert browser_execution["file_path"] == executed["artifact_ref"]
        assert browser_execution["content_sha256"] == executed["artifact_sha256"]
        artifact_path = tmp_path / browser_execution["file_path"]
        assert artifact_path.is_file()
        assert hashlib.sha256(artifact_path.read_bytes()).hexdigest() == browser_execution["content_sha256"]
