from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
import src.browser.pinned_transport as pinned_transport_module
import src.browser.task_runner as task_runner_module

from src.browser.pinned_transport import (
    PinnedBrowserRequest,
    PinnedBrowserResponse,
    PinnedBrowserTransport,
    PinnedTransportError,
)
from src.browser.task_runner import (
    BrowserTaskInput,
    BrowserVerificationError,
    BrowserTaskRunner,
)
from src.db.models import Goal
from src.security.site_policy import SiteAccessDecision
from src.workflows.job_runtime import DurableJobAdmissionDenied, DurableJobRepository


def _input(*, query: str = "") -> dict[str, Any]:
    suffix = f"?{query}" if query else ""
    return {
        "schema_version": 1,
        "start_url": f"https://fixture.example/docs{suffix}",
        "allowed_hosts": ["fixture.example"],
        "approved_url_prefixes": [
            f"https://fixture.example/docs{suffix}",
            f"https://fixture.example/reference{suffix}",
        ],
        "actions": [
            {
                "kind": "navigate",
                "url": f"https://fixture.example/reference{suffix}",
                "expected_checks": [
                    {"kind": "url_path_prefix", "value": "/reference"},
                ],
            },
            {
                "kind": "extract",
                "selector": "main h1",
                "max_chars": 65_536,
                "expected_checks": [
                    {"kind": "text_contains", "selector": "main h1", "value": "Reference"},
                ],
            },
        ],
        "final_expected_checks": [
            {"kind": "url_host", "value": "fixture.example"},
        ],
    }


def _policy(url: str, **_: Any) -> SiteAccessDecision:
    return SiteAccessDecision(allowed=True, hostname="fixture.example")


_ARTIFACT_DIGEST = task_runner_module._browser_input_digests(  # type: ignore[attr-defined]
    BrowserTaskInput.model_validate(_input())
)[0]


class FakeJobs:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    async def admit_job(self, spec: Any) -> dict[str, Any]:
        self.calls.append("admit")
        row = self.rows.get(spec.identity.job_id)
        if row is not None:
            return row
        row = {
            "job_id": spec.identity.job_id,
            "run_identity": spec.identity.job_id,
            "root_run_identity": spec.identity.job_id,
            "status": "accepted",
            "revision": 1,
            "session_id": spec.session_id,
            "operator_session_id": spec.operator_session_id,
            "owner": {
                "kind": spec.identity.owner_kind,
                "principal_id": spec.identity.owner_principal_id,
                "service_id": spec.service_id,
            },
            "job_kind": spec.identity.job_kind,
            "capability_version": spec.identity.capability_version,
            "priority": spec.priority,
            "goal_id": spec.goal_id,
            "goal_revision": spec.goal_revision,
            "input_digest": hashlib.sha256(str(spec.inputs).encode()).hexdigest(),
            "declared_authority": dict(spec.declared_authority),
            "lease": {"owner": None, "fencing_token": 0},
            "idempotency": {
                "scope": spec.identity.idempotency_scope,
                "key": spec.identity.idempotency_key,
            },
        }
        self.rows[spec.identity.job_id] = row
        return dict(row)

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        self.calls.append("get")
        row = self.rows.get(job_id)
        return dict(row) if row is not None else None

    async def queue_job(self, job_id: str, **_: Any) -> dict[str, Any]:
        self.calls.append("queue")
        row = self.rows[job_id]
        row["status"] = "queued"
        row["revision"] += 1
        return dict(row)

    async def claim_job(self, job_id: str, *, owner: str, **_: Any) -> dict[str, Any]:
        self.calls.append("claim")
        row = self.rows[job_id]
        row["status"] = "running"
        row["revision"] += 1
        row["lease"] = {"owner": owner, "fencing_token": 1}
        return dict(row)

    async def assert_active_lease(self, job_id: str, *, owner: str, fencing_token: int) -> dict[str, Any]:
        self.calls.append("assert_lease")
        row = self.rows[job_id]
        assert row["status"] == "running"
        assert row["lease"] == {"owner": owner, "fencing_token": fencing_token}
        return dict(row)

    async def record_checkpoint(self, job_id: str, **_: Any) -> dict[str, Any]:
        self.calls.append("checkpoint")
        row = self.rows[job_id]
        row["revision"] += 1
        return dict(row)

    async def record_artifact(self, job_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("artifact")
        row = self.rows[job_id]
        row["revision"] += 1
        file_path = kwargs["file_path"]
        digest = hashlib.sha256(kwargs["content"]).hexdigest()
        artifact_id = "art_" + hashlib.sha256(
            "|".join(
                (
                    "browser_public_task",
                    kwargs["artifact_type"],
                    job_id,
                    file_path,
                    digest,
                )
            ).encode("utf-8")
        ).hexdigest()[:24]
        row.setdefault("artifacts", []).append(
            {
                "artifact_id": artifact_id,
                "artifact_type": kwargs["artifact_type"],
                "file_path": file_path,
                "producer": "browser_public_task",
                "content_sha256": digest,
                "exists": True,
            }
        )
        return dict(row)

    async def record_readback(self, job_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("readback")
        row = self.rows[job_id]
        row["revision"] += 1
        row.setdefault("effects", []).append({"receipt_kind": "readback", "effect_type": "browser_public_task_result", **kwargs})
        return dict(row)

    async def record_effect(self, job_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("effect")
        row = self.rows[job_id]
        row["revision"] += 1
        row.setdefault("effects", []).append({"receipt_kind": "effect", **kwargs, "status": kwargs.get("status", "succeeded")})
        return dict(row)

    async def transition_job(self, job_id: str, status: str, **_: Any) -> dict[str, Any]:
        self.calls.append(f"transition:{status}")
        row = self.rows[job_id]
        row["status"] = status
        row["revision"] += 1
        return dict(row)


class CheckpointRecordingJobs(FakeJobs):
    """Fake durable repository retaining checkpoint payloads for bound tests."""

    def __init__(self) -> None:
        super().__init__()
        self.checkpoints: list[dict[str, Any]] = []

    async def record_checkpoint(self, job_id: str, **kwargs: Any) -> dict[str, Any]:
        self.checkpoints.append(dict(kwargs))
        return await super().record_checkpoint(job_id, **kwargs)


class FakeRoute:
    def __init__(self) -> None:
        self.fulfilled: dict[str, Any] | None = None
        self.aborted = False
        self.continued = False

    async def fulfill(self, **kwargs: Any) -> None:
        self.fulfilled = kwargs

    async def abort(self, **_: Any) -> None:
        self.aborted = True

    async def continue_(self, **_: Any) -> None:
        self.continued = True


@dataclass
class FakeRequest:
    url: str
    method: str = "GET"
    resource_type: str = "document"
    redirected_from: Any = None

    def is_navigation_request(self) -> bool:
        return self.resource_type == "document"

    async def all_headers(self) -> dict[str, str]:
        return {}


class FakeLocator:
    def __init__(self, text: str = "Reference") -> None:
        self.text = text
        self.first = self

    async def inner_text(self) -> str:
        return self.text

    async def get_attribute(self, _: str) -> str:
        return "https://fixture.example/reference"


class FakePage:
    def __init__(self, responses: dict[str, PinnedBrowserResponse]) -> None:
        self.responses = responses
        self.url = ""
        self.route_handler: Any = None
        self.handlers: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    async def goto(self, url: str, **_: Any) -> SimpleNamespace:
        route = FakeRoute()
        self.url = url
        await self.route_handler(route, FakeRequest(url))
        if route.aborted:
            raise RuntimeError("request aborted")
        assert route.fulfilled is not None
        return SimpleNamespace(status=route.fulfilled["status"])

    def locator(self, _: str) -> FakeLocator:
        return FakeLocator()


class FakeContext:
    def __init__(self, responses: dict[str, PinnedBrowserResponse]) -> None:
        self.responses = responses
        self.routes: list[Any] = []
        self.handlers: dict[str, Any] = {}
        self.page = FakePage(responses)

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler

    async def route(self, _: str, handler: Any) -> None:
        self.routes.append(handler)
        self.page.route_handler = handler

    async def new_page(self) -> FakePage:
        return self.page

    async def close(self) -> None:
        return None


class FakeBrowser:
    def __init__(
        self,
        responses: dict[str, PinnedBrowserResponse],
        *,
        close_error: Exception | None = None,
    ) -> None:
        self.context = FakeContext(responses)
        self.closed = False
        self.close_error = close_error

    async def new_context(self, **_: Any) -> FakeContext:
        return self.context

    async def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


@pytest.mark.parametrize(
    "payload",
    [
        {"unknown": True},
        {"max_chars": 65_537},
    ],
)
def test_browser_input_is_strict(payload: dict[str, Any]) -> None:
    value = _input()
    if "max_chars" in payload:
        value["actions"][1]["max_chars"] = payload["max_chars"]
    else:
        value["unknown"] = True
    with pytest.raises(ValidationError):
        BrowserTaskInput.model_validate(value)


def test_query_is_part_of_exact_prefix_consent() -> None:
    value = _input(query="version=1")
    BrowserTaskInput.model_validate(value)
    value["start_url"] = "https://fixture.example/docs?version=2"
    with pytest.raises(ValidationError):
        BrowserTaskInput.model_validate(value)


@pytest.mark.parametrize(
    "bad_path",
    [
        "/docs/../private",
        "/docs/./reference",
        "/docs/%2e%2e/private",
        "/docs/%2Fprivate",
        "/docs\\private",
    ],
)
def test_ambiguous_path_segments_cannot_cross_prefix_boundaries(bad_path: str) -> None:
    value = _input()
    value["start_url"] = f"https://fixture.example{bad_path}"
    with pytest.raises(ValidationError):
        BrowserTaskInput.model_validate(value)


def test_harmless_encoded_space_remains_valid() -> None:
    value = _input()
    value["start_url"] = "https://fixture.example/docs/reference%20page"
    value["approved_url_prefixes"] = ["https://fixture.example/docs"]
    value["actions"] = [
        {
            "kind": "extract",
            "selector": "main h1",
            "max_chars": 20,
            "expected_checks": [
                {"kind": "text_contains", "selector": "main h1", "value": "Reference"},
            ],
        }
    ]
    BrowserTaskInput.model_validate(value)


def test_prefix_hosts_must_be_explicitly_allowed() -> None:
    value = _input()
    value["approved_url_prefixes"] = ["https://other.example/docs"]
    with pytest.raises(ValidationError):
        BrowserTaskInput.model_validate(value)


def test_page_url_must_stay_inside_approved_prefixes() -> None:
    model = BrowserTaskInput.model_validate(_input())
    with pytest.raises(BrowserVerificationError, match="outside approved prefixes"):
        BrowserTaskRunner._assert_page_url_consented(
            SimpleNamespace(url="https://fixture.example/private"),
            model,
        )


@pytest.mark.asyncio
async def test_preflight_is_provider_free_and_does_not_launch_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    launched = False

    def launcher() -> Any:
        nonlocal launched
        launched = True
        raise AssertionError("preflight must not launch Chromium")

    monkeypatch.setattr(task_runner_module, "_playwright_browser_executable_present", lambda: True)
    runner = BrowserTaskRunner(
        jobs=FakeJobs(),
        browser_launcher=launcher,
        transport_factory=lambda: PinnedBrowserTransport(
            resolver=lambda *_: ["93.184.216.34"],
            site_policy=_policy,
        ),
    )
    receipt = await runner.preflight(_input())
    assert receipt["status"] == "ready", receipt
    assert receipt["checked_hosts"] == ["fixture.example"]
    assert receipt["playwright_installed"] is True
    assert launched is False


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("SERAPH_RUN_REAL_BROWSER") != "1",
    reason="real Chromium receipt is an explicit integration proof",
)
async def test_real_chromium_injected_fixture_uses_one_context_and_verified_readback(tmp_path: Path) -> None:
    """Exercise the route guard through installed Chromium without public network access."""

    from playwright.async_api import async_playwright

    jobs = FakeJobs()
    responses = {
        "https://fixture.example/docs": PinnedBrowserResponse(
            200,
            {"content-type": "text/html; charset=utf-8"},
            b"<html><main><h1>Docs</h1></main></html>",
            "https://fixture.example/docs",
            "93.184.216.34",
        ),
        "https://fixture.example/reference": PinnedBrowserResponse(
            200,
            {"content-type": "text/html; charset=utf-8"},
            b"<html><main><h1>Reference</h1></main></html>",
            "https://fixture.example/reference",
            "93.184.216.34",
        ),
    }

    async def fixture(request: PinnedBrowserRequest) -> PinnedBrowserResponse:
        return responses[request.url]

    class BrowserHandle:
        def __init__(self, playwright: Any, browser: Any) -> None:
            self.playwright = playwright
            self.browser = browser
            self.context_count = 0

        async def new_context(self, **kwargs: Any) -> Any:
            self.context_count += 1
            return await self.browser.new_context(**kwargs)

        async def close(self) -> None:
            await self.browser.close()
            await self.playwright.stop()

    handle: BrowserHandle | None = None

    async def launcher() -> BrowserHandle:
        nonlocal handle
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        handle = BrowserHandle(playwright, browser)
        return handle

    runner = BrowserTaskRunner(
        jobs=jobs,
        browser_launcher=launcher,
        runtime_controls=lambda **_: True,
        transport_factory=lambda: PinnedBrowserTransport(
            resolver=lambda *_: ["93.184.216.34"],
            injected_fetch=fixture,
            site_policy=_policy,
        ),
        workspace_root=tmp_path,
    )
    result = await runner.run(
        task_id="task-real",
        attempt_id="attempt-1",
        owner_principal_id="operator-1",
        owner_session_id="session-1",
        goal_id="goal-1",
        goal_revision=1,
        board_task_revision=2,
        board_fencing_token=3,
        input_artifact_id="art-real",
        input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),
        runtime_seconds=180,
        admission_only=True,
        task_priority=50,
    )
    assert result["status"] == "admitted"
    result = await runner.run(
        task_id="task-real",
        attempt_id="attempt-1",
        owner_principal_id="operator-1",
        owner_session_id="session-1",
        goal_id="goal-1",
        goal_revision=1,
        board_task_revision=2,
        board_fencing_token=3,
        input_artifact_id="art-real",
        input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),
        runtime_seconds=180,
        admission_only=False,
        task_priority=50,
    )
    assert result["status"] == "succeeded", result
    assert result["readback_id"]
    assert result["artifact_sha256"]
    assert handle is not None and handle.context_count == 1
    artifact = tmp_path / result["artifact_ref"]
    assert artifact.exists()
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == result["artifact_sha256"]
    receipt_path = os.environ.get("SERAPH_BROWSER_RECEIPT_PATH")
    if receipt_path:
        Path(receipt_path).write_text(json.dumps(result, sort_keys=True), encoding="utf-8")


@pytest.mark.asyncio
async def test_transport_fulfills_without_continue_and_overwrites_fixture_pin() -> None:
    receipts: list[dict[str, Any]] = []

    async def fixture(_: PinnedBrowserRequest) -> PinnedBrowserResponse:
        return PinnedBrowserResponse(
            status_code=200,
            headers={"content-type": "text/html"},
            content=b"<h1>ok</h1>",
            request_url="https://fixture.example/docs",
            pinned_address="198.51.100.7",
        )

    transport = PinnedBrowserTransport(
        resolver=lambda *_: ["93.184.216.34"],
        injected_fetch=fixture,
        site_policy=_policy,
    )
    context = FakeContext({})
    await transport.install_route_guard(
        context,
        allowed_hosts=["fixture.example"],
        approved_url_prefixes=["https://fixture.example/docs"],
        before_request=lambda: asyncio.sleep(0),
        on_receipt=lambda receipt: _append(receipts, receipt),
    )
    route = FakeRoute()
    await context.routes[0](route, FakeRequest("https://fixture.example/docs"))
    assert route.fulfilled is not None
    assert route.fulfilled["body"] == b"<h1>ok</h1>"
    assert route.continued is False
    assert route.aborted is False
    assert receipts[0]["pinned_address_digest"] == hashlib.sha256(b"93.184.216.34").hexdigest()


@pytest.mark.asyncio
async def test_transport_streams_and_closes_response_before_body_cap_overflow(monkeypatch) -> None:
    class StreamingResponse:
        def __init__(self) -> None:
            self.status_code = 200
            self.headers = {"content-type": "text/html"}
            self.chunks = [b"123", b"45", b"must-not-be-read"]
            self.read_chunks = 0
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: Any) -> None:
            self.closed = True

        async def aiter_bytes(self):
            for chunk in self.chunks:
                self.read_chunks += 1
                yield chunk

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.stream_args: tuple[Any, ...] | None = None
            self.stream_kwargs: dict[str, Any] | None = None
            self.response = StreamingResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        def stream(self, *args: Any, **kwargs: Any):
            self.stream_args = args
            self.stream_kwargs = kwargs
            return self.response

    client_kwargs: dict[str, Any] = {}
    client = FakeClient()
    monkeypatch.setattr(
        pinned_transport_module.httpx,
        "AsyncClient",
        lambda **kwargs: (client_kwargs.update(kwargs) or client),
    )
    transport = PinnedBrowserTransport(
        resolver=lambda *_: ["93.184.216.34"],
        site_policy=_policy,
        max_response_bytes=4,
    )
    request = PinnedBrowserRequest(
        "https://fixture.example/docs",
        "GET",
        {},
        "document",
        True,
        0,
    )

    with pytest.raises(PinnedTransportError) as raised:
        await transport.resolve_and_fetch(
            request,
            allowed_hosts=["fixture.example"],
            approved_url_prefixes=["https://fixture.example/docs"],
        )

    assert raised.value.code == "response_limit"
    assert client.response.closed is True
    assert client.response.read_chunks == 2
    assert client_kwargs["trust_env"] is False
    assert client_kwargs["follow_redirects"] is False
    assert client.stream_args == ("GET", "https://93.184.216.34/docs")
    assert client.stream_kwargs is not None
    assert client.stream_kwargs["headers"]["host"] == "fixture.example"
    assert client.stream_kwargs["extensions"] == {"sni_hostname": "fixture.example"}


@pytest.mark.asyncio
async def test_transport_rejects_encoded_response_before_body_iteration(monkeypatch) -> None:
    class EncodedResponse:
        status_code = 200
        headers = {"content-type": "text/html", "content-encoding": "gzip"}

        def __init__(self) -> None:
            self.closed = False
            self.read_chunks = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: Any) -> None:
            self.closed = True

        async def aiter_bytes(self):
            self.read_chunks += 1
            raise AssertionError("encoded response must be rejected before body iteration")
            yield b"unreachable"

    class FakeClient:
        def __init__(self, **_: Any) -> None:
            self.response = EncodedResponse()
            self.stream_kwargs: dict[str, Any] | None = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        def stream(self, *_args: Any, **kwargs: Any):
            self.stream_kwargs = kwargs
            return self.response

    client = FakeClient()
    monkeypatch.setattr(
        pinned_transport_module.httpx,
        "AsyncClient",
        lambda **kwargs: client,
    )
    async def resolver(_: str, __: int) -> list[str]:
        return ["93.184.216.34"]

    async def site_policy(url: str) -> SiteAccessDecision:
        return _policy(url)

    transport = PinnedBrowserTransport(resolver=resolver, site_policy=site_policy)

    with pytest.raises(PinnedTransportError) as raised:
        await transport.resolve_and_fetch(
            PinnedBrowserRequest("https://fixture.example/docs", "GET", {}, "document", True, 0),
            allowed_hosts=["fixture.example"],
            approved_url_prefixes=["https://fixture.example/docs"],
        )

    assert raised.value.code == "response_encoding_blocked"
    assert client.response.closed is True
    assert client.response.read_chunks == 0
    assert client.stream_kwargs is not None
    assert client.stream_kwargs["headers"]["accept-encoding"] == "identity"


@pytest.mark.asyncio
async def test_transport_rejects_mixed_global_and_private_dns_answers() -> None:
    transport = PinnedBrowserTransport(
        resolver=lambda *_: ["93.184.216.34", "127.0.0.1"],
        injected_fetch=lambda _: PinnedBrowserResponse(
            status_code=200,
            headers={},
            content=b"ok",
            request_url="https://fixture.example/docs",
            pinned_address="93.184.216.34",
        ),
        site_policy=_policy,
    )
    with pytest.raises(PinnedTransportError, match="globally routable"):
        await transport.resolve_and_fetch(
            PinnedBrowserRequest("https://fixture.example/docs", "GET", {}, "document", True, 0),
            allowed_hosts=["fixture.example"],
            approved_url_prefixes=["https://fixture.example/docs"],
        )


@pytest.mark.asyncio
async def test_slow_site_policy_is_bounded_off_event_loop() -> None:
    def slow_policy(_: str) -> SiteAccessDecision:
        time.sleep(0.05)
        return SiteAccessDecision(allowed=True, hostname="fixture.example")

    transport = PinnedBrowserTransport(
        resolver=lambda *_: ["93.184.216.34"],
        injected_fetch=lambda _: PinnedBrowserResponse(
            status_code=200,
            headers={},
            content=b"ok",
            request_url="https://fixture.example/docs",
            pinned_address="93.184.216.34",
        ),
        site_policy=slow_policy,
        timeout_seconds=0.005,
    )
    policy_task = asyncio.create_task(
        transport.resolve_and_fetch(
            PinnedBrowserRequest("https://fixture.example/docs", "GET", {}, "document", True, 0),
            allowed_hosts=["fixture.example"],
            approved_url_prefixes=["https://fixture.example/docs"],
        )
    )
    responsive_task = asyncio.create_task(asyncio.sleep(0.001, result="responsive"))
    policy_result, responsive_result = await asyncio.gather(
        policy_task,
        responsive_task,
        return_exceptions=True,
    )
    assert isinstance(policy_result, PinnedTransportError)
    assert policy_result.code == "site_policy_timeout"
    assert responsive_result == "responsive"


@pytest.mark.asyncio
async def test_blocking_transport_lane_is_bounded_and_does_not_starve_default_executor() -> None:
    active = 0
    peak = 0
    lock = threading.Lock()

    def slow_resolver(_: str, __: int) -> list[str]:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.03)
            return ["93.184.216.34"]
        finally:
            with lock:
                active -= 1

    transport = PinnedBrowserTransport(
        resolver=slow_resolver,
        injected_fetch=lambda request: PinnedBrowserResponse(
            status_code=200,
            headers={},
            content=b"ok",
            request_url=request.url,
            pinned_address="93.184.216.34",
        ),
        site_policy=_policy,
        timeout_seconds=1.0,
    )

    requests = [
        transport.resolve_and_fetch(
            PinnedBrowserRequest("https://fixture.example/docs", "GET", {}, "document", True, 0),
            allowed_hosts=["fixture.example"],
            approved_url_prefixes=["https://fixture.example/docs"],
        )
        for _ in range(4)
    ]
    default_executor_work = asyncio.get_running_loop().run_in_executor(None, lambda: "default-responsive")
    results = await asyncio.gather(*requests)

    assert all(item.status_code == 200 for item in results)
    assert await default_executor_work == "default-responsive"
    assert peak <= 2


@pytest.mark.asyncio
async def test_slow_sync_injected_network_is_bounded_and_async_fixture_stays_direct() -> None:
    active = 0
    peak = 0
    lock = threading.Lock()

    def slow_fetch(request: PinnedBrowserRequest) -> PinnedBrowserResponse:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.03)
            return PinnedBrowserResponse(200, {}, b"ok", request.url, "93.184.216.34")
        finally:
            with lock:
                active -= 1

    transport = PinnedBrowserTransport(
        resolver=lambda *_: ["93.184.216.34"],
        injected_fetch=slow_fetch,
        site_policy=_policy,
        timeout_seconds=1.0,
    )
    request = PinnedBrowserRequest("https://fixture.example/docs", "GET", {}, "document", True, 0)
    results = await asyncio.gather(
        *(
            transport.resolve_and_fetch(
                request,
                allowed_hosts=["fixture.example"],
                approved_url_prefixes=["https://fixture.example/docs"],
            )
            for _ in range(4)
        )
    )
    assert all(item.status_code == 200 for item in results)
    assert peak <= 2


@pytest.mark.asyncio
async def test_admission_is_effect_free_and_does_not_launch_browser(tmp_path: Path) -> None:
    jobs = FakeJobs()
    launched = False

    def launcher() -> Any:
        nonlocal launched
        launched = True
        raise AssertionError("admission must not launch Chromium")

    runner = BrowserTaskRunner(
        jobs=jobs,
        browser_launcher=launcher,
        runtime_controls=lambda **_: True,
        workspace_root=tmp_path,
    )
    receipt = await runner.run(
        task_id="task-1",
        attempt_id="attempt-1",
        owner_principal_id="operator-1",
        owner_session_id="session-1",
        goal_id="goal-1",
        goal_revision=1,
        board_task_revision=2,
        board_fencing_token=3,
        input_artifact_id="art_input1",
        input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),
        runtime_seconds=180,
        admission_only=True,
        task_priority=50,
    )
    assert receipt["status"] == "admitted"
    assert receipt["job_id"] == "browser-task:task-1:attempt-1"
    assert launched is False
    assert jobs.calls == ["admit"]


@pytest.mark.asyncio
async def test_production_execution_requires_live_runtime_controls(tmp_path: Path) -> None:
    jobs = FakeJobs()
    runner = BrowserTaskRunner(jobs=jobs, workspace_root=tmp_path)
    receipt = await runner.run(
        task_id="task-no-runtime-controls",
        attempt_id="attempt-1",
        owner_principal_id="operator-1",
        owner_session_id="session-1",
        goal_id="goal-1",
        goal_revision=1,
        board_task_revision=2,
        board_fencing_token=3,
        input_artifact_id="art_no_runtime_controls",
        input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),
        runtime_seconds=180,
        admission_only=True,
        task_priority=50,
    )
    assert receipt["status"] == "blocked"
    assert receipt["reason_code"] == "runtime_control_required"
    assert jobs.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "reason_code"),
    [
        ("goal_id", None, "goal_id_invalid"),
        ("goal_revision", None, "goal_revision_invalid"),
        ("input_artifact_digest", None, "input_artifact_digest_invalid"),
        ("input_artifact_digest", "invalid", "input_artifact_digest_invalid"),
    ],
)
async def test_runner_rejects_incomplete_canonical_binding_before_admission(
    tmp_path: Path,
    field: str,
    value: Any,
    reason_code: str,
) -> None:
    jobs = FakeJobs()
    runner = BrowserTaskRunner(
        jobs=jobs,
        runtime_controls=lambda **_: True,
        workspace_root=tmp_path,
    )
    kwargs: dict[str, Any] = {
        "task_id": "task-binding-input",
        "attempt_id": "attempt-1",
        "owner_principal_id": "operator-1",
        "owner_session_id": "session-1",
        "goal_id": "goal-1",
        "goal_revision": 1,
        "board_task_revision": 2,
        "board_fencing_token": 3,
        "input_artifact_id": "art-binding-input",
        "input_artifact_digest": _ARTIFACT_DIGEST,
        "inputs": _input(),
        "runtime_seconds": 180,
        "admission_only": True,
        "task_priority": 50,
    }
    kwargs[field] = value

    receipt = await runner.run(**kwargs)

    assert receipt["status"] == "blocked"
    assert receipt["reason_code"] == reason_code
    assert jobs.calls == []


@pytest.mark.asyncio
async def test_runner_rechecks_runtime_authority_before_admission(tmp_path: Path) -> None:
    jobs = FakeJobs()
    runner = BrowserTaskRunner(
        jobs=jobs,
        runtime_controls=lambda **_: False,
        workspace_root=tmp_path,
    )
    receipt = await runner.run(
        task_id="task-revoked-before-admit",
        attempt_id="attempt-1",
        owner_principal_id="operator-1",
        owner_session_id="session-1",
        goal_id="goal-1",
        goal_revision=1,
        board_task_revision=2,
        board_fencing_token=3,
        input_artifact_id="art-revoked-before-admit",
        input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),
        runtime_seconds=180,
        admission_only=True,
        task_priority=50,
    )
    assert receipt["status"] == "blocked"
    assert receipt["reason_code"] == "board_fence_stale"
    assert jobs.calls == []


@pytest.mark.asyncio
async def test_persisted_running_root_requires_reconciliation_before_relaunch(tmp_path: Path) -> None:
    jobs = FakeJobs()
    launched = False

    def launcher() -> Any:
        nonlocal launched
        launched = True
        raise AssertionError("a persisted running root must not launch a second browser")

    runner = BrowserTaskRunner(
        jobs=jobs,
        browser_launcher=launcher,
        runtime_controls=lambda **_: True,
        workspace_root=tmp_path,
    )
    admitted = await runner.run(
        task_id="task-running-recovery",
        attempt_id="attempt-1",
        owner_principal_id="operator-1",
        owner_session_id="session-1",
        goal_id="goal-1",
        goal_revision=1,
        board_task_revision=2,
        board_fencing_token=3,
        input_artifact_id="art_running_recovery",
        input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),
        runtime_seconds=180,
        admission_only=True,
        task_priority=50,
    )
    row = jobs.rows[admitted["job_id"]]
    row["status"] = "running"
    row["lease"] = {"owner": "service:browser-task:attempt-1", "fencing_token": 1}
    receipt = await runner.run(
        task_id="task-running-recovery",
        attempt_id="attempt-1",
        owner_principal_id="operator-1",
        owner_session_id="session-1",
        goal_id="goal-1",
        goal_revision=1,
        board_task_revision=2,
        board_fencing_token=3,
        input_artifact_id="art_running_recovery",
        input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),
        runtime_seconds=180,
        admission_only=False,
        task_priority=50,
    )
    assert receipt["status"] == "unknown_external_effect"
    assert receipt["reason_code"] == "durable_running_reentry"
    assert launched is False
    assert "claim" not in jobs.calls


@pytest.mark.asyncio
async def test_real_durable_browser_admission_binds_canonical_goal_owner(
    async_db, tmp_path: Path
) -> None:
    """The service browser root must carry the operator goal owner fence."""

    owner_principal_id = "operator:browser-real-admission"
    owner_session_id = "session:browser-real-admission"
    goal_id = "goal-browser-real-admission"
    async with async_db() as db:
        db.add(
            Goal(
                id=goal_id,
                title="Browser durable admission",
                status="active",
                revision=1,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )
        )

    jobs = DurableJobRepository()
    runner = BrowserTaskRunner(jobs=jobs, runtime_controls=lambda **_: True, workspace_root=tmp_path)
    receipt = await runner.run(
        task_id="task-real-browser-admission",
        attempt_id="attempt-real-browser-admission",
        owner_principal_id=owner_principal_id,
        owner_session_id=owner_session_id,
        goal_id=goal_id,
        goal_revision=1,
        board_task_revision=2,
        board_fencing_token=3,
        input_artifact_id="art-real-browser-admission",
        input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),
        runtime_seconds=180,
        admission_only=True,
        task_priority=50,
        effective_max_attempts=1,
        effective_max_outstanding_jobs=1,
    )

    assert receipt["status"] == "admitted"
    projection = await jobs.get_job(receipt["job_id"])
    assert projection is not None
    authority = projection["declared_authority"]
    assert authority["goal_owner_principal_id"] == owner_principal_id
    assert authority["goal_owner_session_id"] == owner_session_id
    assert authority["priority"] == 50

    # Reload the durable projection through the real repository before the
    # execution call.  SQLite commonly returns the persisted deadline without
    # its UTC offset; the runner then owns queue/claim and must publish a
    # verified terminal result.
    responses = {
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

    async def fixture(request: PinnedBrowserRequest) -> PinnedBrowserResponse:
        return responses[request.url]

    browser = FakeBrowser(responses)
    execution_runner = BrowserTaskRunner(
        jobs=DurableJobRepository(),
        browser_launcher=lambda: browser,
        runtime_controls=lambda **_: True,
        transport_factory=lambda: PinnedBrowserTransport(
            resolver=lambda *_: ["93.184.216.34"],
            injected_fetch=fixture,
            site_policy=_policy,
        ),
        workspace_root=tmp_path,
    )
    reloaded = await execution_runner.jobs.get_job(receipt["job_id"])
    assert reloaded is not None
    execution = await execution_runner.run(
        task_id="task-real-browser-admission",
        attempt_id="attempt-real-browser-admission",
        owner_principal_id=owner_principal_id,
        owner_session_id=owner_session_id,
        goal_id=goal_id,
        goal_revision=1,
        board_task_revision=2,
        admission_board_task_revision=2,
        board_fencing_token=3,
        input_artifact_id="art-real-browser-admission",
        input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),
        runtime_seconds=180,
        admission_only=False,
        task_priority=50,
        effective_max_attempts=1,
        effective_max_outstanding_jobs=1,
        durable_job_id=receipt["job_id"],
    )
    assert execution["status"] == "succeeded", execution
    assert execution["cleanup_status"] == "cleanup_verified"
    assert browser.closed is True


@pytest.mark.asyncio
async def test_budget_denial_is_typed_prelaunch_block_without_unknown_effect(tmp_path: Path) -> None:
    class BudgetDeniedJobs(FakeJobs):
        async def admit_job(self, _spec: Any) -> dict[str, Any]:
            self.calls.append("admit")
            raise DurableJobAdmissionDenied("goal_budget_outstanding_limit", goal_id="goal-1")

    launched = False

    def launcher() -> Any:
        nonlocal launched
        launched = True
        raise AssertionError("budget denial must happen before browser launch")

    runner = BrowserTaskRunner(
        jobs=BudgetDeniedJobs(),
        browser_launcher=launcher,
        runtime_controls=lambda **_: True,
        workspace_root=tmp_path,
    )
    result = await runner.run(
        task_id="task-budget-denied",
        attempt_id="attempt-1",
        owner_principal_id="operator-1",
        owner_session_id="session-1",
        goal_id="goal-1",
        goal_revision=1,
        board_task_revision=2,
        board_fencing_token=3,
        input_artifact_id="art-budget-denied",
        input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),
        runtime_seconds=180,
        admission_only=True,
        task_priority=50,
        effective_max_attempts=2,
        effective_max_outstanding_jobs=1,
    )
    assert result["status"] == "blocked"
    assert result["durable_status"] == "blocked"
    assert result["reason_code"] == "goal_budget_outstanding_limit"
    assert result["cleanup_status"] == "not_needed"
    assert launched is False


@pytest.mark.asyncio
async def test_unknown_admission_denial_preserves_reconciliation_boundary(tmp_path: Path) -> None:
    class UnknownDeniedJobs(FakeJobs):
        async def admit_job(self, _spec: Any) -> dict[str, Any]:
            raise DurableJobAdmissionDenied("future_transaction_boundary")

    runner = BrowserTaskRunner(
        jobs=UnknownDeniedJobs(),
        runtime_controls=lambda **_: True,
        workspace_root=tmp_path,
    )
    result = await runner.run(
        task_id="task-unknown-admission-denial",
        attempt_id="attempt-1",
        owner_principal_id="operator-1",
        owner_session_id="session-1",
        goal_id="goal-1",
        goal_revision=1,
        board_task_revision=2,
        board_fencing_token=3,
        input_artifact_id="art-unknown-admission-denial",
        input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),
        runtime_seconds=180,
        admission_only=True,
        task_priority=50,
        effective_max_attempts=2,
        effective_max_outstanding_jobs=1,
    )
    assert result["status"] == "unknown_external_effect"
    assert result["reason_code"] == "runner_unexpected_failure"
    assert result["cleanup_status"] == "cleanup_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline", ["not-an-iso-deadline", "2000-01-01T00:00:00"])
async def test_prelaunch_deadline_failure_is_no_context_and_does_not_launch(
    tmp_path: Path,
    deadline: str,
) -> None:
    jobs = FakeJobs()
    launched = False

    def launcher() -> Any:
        nonlocal launched
        launched = True
        raise AssertionError("deadline failure must happen before browser launch")

    runner = BrowserTaskRunner(
        jobs=jobs,
        browser_launcher=launcher,
        runtime_controls=lambda **_: True,
        workspace_root=tmp_path,
    )
    args = {
        "task_id": f"task-prelaunch-deadline-{deadline[:4]}",
        "attempt_id": "attempt-1",
        "owner_principal_id": "operator-1",
        "owner_session_id": "session-1",
        "goal_id": "goal-1",
        "goal_revision": 1,
        "board_task_revision": 2,
        "board_fencing_token": 3,
        "input_artifact_id": f"art-prelaunch-deadline-{deadline[:4]}",
        "input_artifact_digest": _ARTIFACT_DIGEST,
        "inputs": _input(),
        "runtime_seconds": 30,
        "task_priority": 50,
    }
    assert (await runner.run(**args, admission_only=True))["status"] == "admitted"
    job_id = f"browser-task:{args['task_id']}:{args['attempt_id']}"
    jobs.rows[job_id]["deadline_at"] = deadline
    result = await runner.run(**args, admission_only=False)

    assert result["status"] == "blocked"
    assert result["durable_status"] == "blocked"
    assert result["reason_code"] == "browser_runtime_deadline"
    assert result["cleanup_status"] == "not_needed"
    assert launched is False
    assert "transition:succeeded" not in jobs.calls
    effects = jobs.rows[job_id].get("effects", [])
    assert any(
        effect.get("effect_type") == "browser_context_cleanup"
        and effect.get("status") == "succeeded"
        and effect.get("details", {}).get("context_not_started") is True
        for effect in effects
    )


@pytest.mark.asyncio
async def test_effective_goal_limits_are_server_bound_and_stale_limits_block_recovery(tmp_path: Path) -> None:
    jobs = FakeJobs()
    runner = BrowserTaskRunner(jobs=jobs, runtime_controls=lambda **_: True, workspace_root=tmp_path)
    admitted = await runner.run(
        task_id="task-limits",
        attempt_id="attempt-1",
        owner_principal_id="operator-1",
        owner_session_id="session-1",
        goal_id="goal-1",
        goal_revision=1,
        board_task_revision=2,
        board_fencing_token=3,
        input_artifact_id="art_limits",
        input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),
        runtime_seconds=180,
        admission_only=True,
        task_priority=50,
        effective_max_attempts=1,
        effective_max_outstanding_jobs=2,
    )
    assert admitted["status"] == "admitted"
    authority = jobs.rows[admitted["job_id"]]["declared_authority"]
    assert authority["limits"]["max_attempts"] == 1
    assert authority["limits"]["max_outstanding_jobs"] == 2

    authority["limits"]["max_attempts"] = 2
    execution = await runner.run(
        task_id="task-limits",
        attempt_id="attempt-1",
        owner_principal_id="operator-1",
        owner_session_id="session-1",
        goal_id="goal-1",
        goal_revision=1,
        board_task_revision=2,
        board_fencing_token=3,
        input_artifact_id="art_limits",
        input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),
        runtime_seconds=180,
        admission_only=False,
        task_priority=50,
        admission_board_task_revision=2,
        effective_max_attempts=1,
        effective_max_outstanding_jobs=2,
    )
    assert execution["status"] == "blocked"
    assert execution["reason_code"] == "authority_limits_stale"


@pytest.mark.asyncio
async def test_execution_rejects_changed_admitted_input_digest_before_launch(tmp_path: Path) -> None:
    jobs = FakeJobs()
    launched = False

    def launcher() -> Any:
        nonlocal launched
        launched = True
        raise AssertionError("changed durable input must not launch Chromium")

    runner = BrowserTaskRunner(
        jobs=jobs,
        browser_launcher=launcher,
        runtime_controls=lambda **_: True,
        workspace_root=tmp_path,
    )
    admitted = await runner.run(
        task_id="task-input-binding",
        attempt_id="attempt-1",
        owner_principal_id="operator-1",
        owner_session_id="session-1",
        goal_id="goal-1",
        goal_revision=1,
        board_task_revision=2,
        board_fencing_token=3,
        input_artifact_id="art-input-binding",
        input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),
        runtime_seconds=180,
        admission_only=True,
        task_priority=50,
    )
    assert admitted["status"] == "admitted"
    jobs.rows[admitted["job_id"]]["declared_authority"]["browser_input_digest"] = "b" * 64

    result = await runner.run(
        task_id="task-input-binding",
        attempt_id="attempt-1",
        owner_principal_id="operator-1",
        owner_session_id="session-1",
        goal_id="goal-1",
        goal_revision=1,
        board_task_revision=2,
        board_fencing_token=3,
        input_artifact_id="art-input-binding",
        input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),
        runtime_seconds=180,
        admission_only=False,
        task_priority=50,
    )
    assert result["status"] == "blocked"
    assert result["reason_code"] == "durable_artifact_mismatch"
    assert launched is False
    assert "claim" not in jobs.calls


@pytest.mark.asyncio
async def test_execution_rejects_changed_admitted_action_budget_before_launch(tmp_path: Path) -> None:
    jobs = FakeJobs()
    launched = False

    def launcher() -> Any:
        nonlocal launched
        launched = True
        raise AssertionError("changed durable action budget must not launch Chromium")

    runner = BrowserTaskRunner(
        jobs=jobs,
        browser_launcher=launcher,
        runtime_controls=lambda **_: True,
        workspace_root=tmp_path,
    )
    admitted = await runner.run(
        task_id="task-action-budget-binding",
        attempt_id="attempt-1",
        owner_principal_id="operator-1",
        owner_session_id="session-1",
        goal_id="goal-1",
        goal_revision=1,
        board_task_revision=2,
        board_fencing_token=3,
        input_artifact_id="art-action-budget-binding",
        input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),
        runtime_seconds=180,
        admission_only=True,
        task_priority=50,
    )
    assert admitted["status"] == "admitted"
    jobs.rows[admitted["job_id"]]["declared_authority"]["action_count"] = 1

    result = await runner.run(
        task_id="task-action-budget-binding",
        attempt_id="attempt-1",
        owner_principal_id="operator-1",
        owner_session_id="session-1",
        goal_id="goal-1",
        goal_revision=1,
        board_task_revision=2,
        board_fencing_token=3,
        input_artifact_id="art-action-budget-binding",
        input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),
        runtime_seconds=180,
        admission_only=False,
        task_priority=50,
    )
    assert result["status"] == "blocked"
    assert result["reason_code"] == "durable_artifact_mismatch"
    assert launched is False
    assert "claim" not in jobs.calls


@pytest.mark.asyncio
async def test_execution_uses_one_context_and_persists_artifact_readback(tmp_path: Path) -> None:
    jobs = FakeJobs()
    responses = {
        "https://fixture.example/docs": PinnedBrowserResponse(200, {"content-type": "text/html"}, b"docs", "https://fixture.example/docs", "93.184.216.34"),
        "https://fixture.example/reference": PinnedBrowserResponse(200, {"content-type": "text/html"}, b"reference", "https://fixture.example/reference", "93.184.216.34"),
    }
    browser = FakeBrowser(responses)

    async def fixture(request: PinnedBrowserRequest) -> PinnedBrowserResponse:
        return responses[request.url]

    runner = BrowserTaskRunner(
        jobs=jobs,
        browser_launcher=lambda: browser,
        runtime_controls=lambda **_: True,
        transport_factory=lambda: PinnedBrowserTransport(
            resolver=lambda *_: ["93.184.216.34"],
            injected_fetch=fixture,
            site_policy=_policy,
        ),
        workspace_root=tmp_path,
    )
    admission = await runner.run(
        task_id="task-2",
        attempt_id="attempt-1",
        owner_principal_id="operator-1",
        owner_session_id="session-1",
        goal_id="goal-1",
        goal_revision=1,
        board_task_revision=2,
        board_fencing_token=3,
        input_artifact_id="art_input2",
        input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),
        runtime_seconds=180,
        admission_only=True,
        task_priority=50,
    )
    assert admission["status"] == "admitted"
    result = await runner.run(
        task_id="task-2",
        attempt_id="attempt-1",
        owner_principal_id="operator-1",
        owner_session_id="session-1",
        goal_id="goal-1",
        goal_revision=1,
        board_task_revision=3,
        board_fencing_token=3,
        input_artifact_id="art_input2",
        input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),
        runtime_seconds=180,
        admission_only=False,
        task_priority=50,
        admission_board_task_revision=2,
    )
    assert result["status"] == "succeeded", result
    assert result["readback_id"]
    assert result["artifact_sha256"]
    assert jobs.calls.count("claim") == 1
    assert "artifact" in jobs.calls and "readback" in jobs.calls
    assert browser.closed is True
    assert list((tmp_path / "artifacts/work-board/browser").glob("*.json"))

    replay = await runner.run(
        task_id="task-2",
        attempt_id="attempt-1",
        owner_principal_id="operator-1",
        owner_session_id="session-1",
        goal_id="goal-1",
        goal_revision=1,
        board_task_revision=3,
        board_fencing_token=3,
        input_artifact_id="art_input2",
        input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),
        runtime_seconds=180,
        admission_only=False,
        task_priority=50,
        admission_board_task_revision=2,
    )
    assert replay["status"] == "succeeded"
    assert replay["reason_code"] == "terminal_replay"
    assert replay["artifact_ref"] == result["artifact_ref"]
    assert replay["artifact_sha256"] == result["artifact_sha256"]
    assert replay["readback_id"] == result["readback_id"]


def test_terminal_replay_requires_canonical_current_artifact_and_cleanup_proof() -> None:
    runner = BrowserTaskRunner(jobs=FakeJobs())
    job_id = "browser-task:task-replay:attempt-1"
    path = task_runner_module.browser_artifact_path_for_job(job_id)
    digest = "b" * 64
    artifact_id = "art_" + hashlib.sha256(
        "|".join(("browser_public_task", "browser_public_task_result", job_id, path, digest)).encode("utf-8")
    ).hexdigest()[:24]
    readback_id = "readback-" + task_runner_module._digest(
        {"job_id": job_id, "path": path, "digest": digest}
    )[:32]
    projection = {
        "job_id": job_id,
        "run_identity": job_id,
        "root_run_identity": job_id,
        "job_kind": "browser_public_task",
        "artifacts": [
            {
                "artifact_id": artifact_id,
                "artifact_type": "browser_public_task_result",
                "producer": "browser_public_task",
                "file_path": path,
                "content_sha256": digest,
                "exists": True,
            }
        ],
        "effects": [
            {
                "receipt_kind": "readback",
                "effect_type": "browser_public_task_result",
                "status": "succeeded",
                "target_path": path,
                "target_digest": digest,
                "content_sha256": digest,
                "readback_id": readback_id,
                "verified_at": "2026-09-30T12:00:00+00:00",
                "details": {"verified": True},
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
        ],
    }
    proof = runner._terminal_replay_proof(projection, expected_job_id=job_id)
    assert proof is not None
    assert proof["cleanup_status"] == "cleanup_verified"

    invalid_artifact = json.loads(json.dumps(projection))
    invalid_artifact["artifacts"][0]["artifact_type"] = "workspace_file"
    assert runner._terminal_replay_proof(invalid_artifact, expected_job_id=job_id) is None

    invalid_readback = json.loads(json.dumps(projection))
    invalid_readback["effects"][0]["readback_id"] = "readback-foreign"
    assert runner._terminal_replay_proof(invalid_readback, expected_job_id=job_id) is None

    invalid_cleanup = json.loads(json.dumps(projection))
    invalid_cleanup["effects"][1]["details"]["cleanup_status"] = "not_needed"
    invalid_cleanup["effects"][1]["details"]["context_not_started"] = False
    assert runner._terminal_replay_proof(invalid_cleanup, expected_job_id=job_id) is None


def test_browser_artifact_writer_is_descriptor_relative_and_atomic(tmp_path: Path) -> None:
    job_id = "browser-task:artifact-write:attempt-1"
    relative = task_runner_module.browser_artifact_path_for_job(job_id)
    payload = b'{"schema_version":1,"value":"safe"}'
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"sentinel")

    task_runner_module._write_browser_artifact_bytes(
        relative,
        payload,
        workspace_root=tmp_path,
    )
    assert task_runner_module.read_browser_artifact_bytes(
        relative,
        workspace_root=tmp_path,
    ) == payload

    final_path = tmp_path / relative
    final_path.unlink()
    final_path.symlink_to(outside)
    # Replacing a final symlink must replace the name, never write through it.
    task_runner_module._write_browser_artifact_bytes(
        relative,
        payload,
        workspace_root=tmp_path,
    )
    assert final_path.read_bytes() == payload
    assert outside.read_bytes() == b"sentinel"

    temporary_name = f".{final_path.name}.{hashlib.sha256(payload).hexdigest()[:12]}.tmp"
    temporary_path = final_path.parent / temporary_name
    temporary_path.symlink_to(outside)
    with pytest.raises(OSError):
        task_runner_module._write_browser_artifact_bytes(
            relative,
            payload,
            workspace_root=tmp_path,
        )
    assert outside.read_bytes() == b"sentinel"

    moved_parent = tmp_path / "moved-browser"
    final_path.parent.rename(moved_parent)
    final_path.parent.symlink_to(moved_parent, target_is_directory=True)
    with pytest.raises(OSError):
        task_runner_module._write_browser_artifact_bytes(
            relative,
            payload,
            workspace_root=tmp_path,
        )
    assert (moved_parent / final_path.name).read_bytes() == payload


@pytest.mark.asyncio
async def test_runtime_budget_caps_each_navigation_before_teardown_reserve(tmp_path: Path) -> None:
    jobs = FakeJobs()
    responses = {
        "https://fixture.example/docs": PinnedBrowserResponse(200, {"content-type": "text/html"}, b"docs", "https://fixture.example/docs", "93.184.216.34"),
        "https://fixture.example/reference": PinnedBrowserResponse(200, {"content-type": "text/html"}, b"reference", "https://fixture.example/reference", "93.184.216.34"),
    }
    browser = FakeBrowser(responses)
    timeouts: list[int] = []
    original_goto = browser.context.page.goto

    async def bounded_goto(url: str, **kwargs: Any) -> Any:
        timeouts.append(int(kwargs["timeout"]))
        return await original_goto(url, **kwargs)

    browser.context.page.goto = bounded_goto  # type: ignore[method-assign]

    async def fixture(request: PinnedBrowserRequest) -> PinnedBrowserResponse:
        return responses[request.url]

    runner = BrowserTaskRunner(
        jobs=jobs,
        browser_launcher=lambda: browser,
        runtime_controls=lambda **_: True,
        transport_factory=lambda: PinnedBrowserTransport(
            resolver=lambda *_: ["93.184.216.34"],
            injected_fetch=fixture,
            site_policy=_policy,
        ),
        workspace_root=tmp_path,
    )
    args = {
        "task_id": "task-budget",
        "attempt_id": "attempt-1",
        "owner_principal_id": "operator-1",
        "owner_session_id": "session-1",
        "goal_id": "goal-1",
        "goal_revision": 1,
        "board_task_revision": 2,
        "board_fencing_token": 3,
        "input_artifact_id": "art-budget",
        "input_artifact_digest": _ARTIFACT_DIGEST,
        "inputs": _input(),
        "runtime_seconds": 30,
        "task_priority": 50,
    }
    assert (await runner.run(**args, admission_only=True))["status"] == "admitted"
    result = await runner.run(**{**args, "board_task_revision": 3, "admission_board_task_revision": 2}, admission_only=False)
    assert result["status"] == "succeeded", result
    assert len(timeouts) == 2
    assert max(timeouts) < 30_000
    assert min(timeouts) > 0


@pytest.mark.asyncio
async def test_partial_launch_cancellation_closes_created_browser_and_never_claims_success(tmp_path: Path) -> None:
    class PartialBrowser:
        def __init__(self) -> None:
            self.context_started = asyncio.Event()
            self.closed = False

        async def new_context(self, **_: Any) -> Any:
            self.context_started.set()
            await asyncio.Event().wait()

        async def close(self) -> None:
            self.closed = True

    jobs = FakeJobs()
    browser = PartialBrowser()
    runner = BrowserTaskRunner(
        jobs=jobs,
        browser_launcher=lambda: browser,
        runtime_controls=lambda **_: True,
        workspace_root=tmp_path,
    )
    args = {
        "task_id": "task-partial-launch-cancel",
        "attempt_id": "attempt-1",
        "owner_principal_id": "operator-1",
        "owner_session_id": "session-1",
        "goal_id": "goal-1",
        "goal_revision": 1,
        "board_task_revision": 2,
        "board_fencing_token": 3,
        "input_artifact_id": "art-partial-launch-cancel",
        "input_artifact_digest": _ARTIFACT_DIGEST,
        "inputs": _input(),
        "runtime_seconds": 30,
        "task_priority": 50,
    }
    assert (await runner.run(**args, admission_only=True))["status"] == "admitted"
    execution = asyncio.create_task(runner.run(**args, admission_only=False))
    await asyncio.wait_for(browser.context_started.wait(), timeout=1.0)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    assert browser.closed is True
    assert "transition:succeeded" not in jobs.calls


@pytest.mark.asyncio
async def test_launch_timeout_records_cleanup_verified_and_never_not_needed(tmp_path: Path) -> None:
    class TimedBrowser:
        def __init__(self) -> None:
            self.context_started = asyncio.Event()
            self.closed = False

        async def new_context(self, **_: Any) -> Any:
            self.context_started.set()
            await asyncio.Event().wait()

        async def close(self) -> None:
            self.closed = True

    jobs = FakeJobs()
    browser = TimedBrowser()
    runner = BrowserTaskRunner(
        jobs=jobs,
        browser_launcher=lambda: browser,
        runtime_controls=lambda **_: True,
        workspace_root=tmp_path,
    )
    args = {
        "task_id": "task-launch-timeout",
        "attempt_id": "attempt-1",
        "owner_principal_id": "operator-1",
        "owner_session_id": "session-1",
        "goal_id": "goal-1",
        "goal_revision": 1,
        "board_task_revision": 2,
        "board_fencing_token": 3,
        "input_artifact_id": "art-launch-timeout",
        "input_artifact_digest": _ARTIFACT_DIGEST,
        "inputs": _input(),
        "runtime_seconds": 1,
        "task_priority": 50,
    }
    assert (await runner.run(**args, admission_only=True))["status"] == "admitted"
    result = await runner.run(**args, admission_only=False)

    assert result["status"] == "blocked"
    assert result["reason_code"] == "browser_runtime_deadline"
    assert result["cleanup_status"] == "cleanup_verified"
    assert browser.closed is True
    assert "transition:succeeded" not in jobs.calls


@pytest.mark.asyncio
async def test_cleanup_timeout_is_bounded_and_holder_retains_partial_resource(tmp_path: Path) -> None:
    class SlowBrowser:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.closed = False

        async def close(self) -> None:
            self.started.set()
            await asyncio.sleep(0.15)
            self.closed = True

    browser = SlowBrowser()
    resources = task_runner_module._BrowserLaunchResources(
        launch_attempted=True,
        browser=browser,
    )
    runner = BrowserTaskRunner(jobs=FakeJobs(), workspace_root=tmp_path)
    started = time.monotonic()
    assert await runner._close_launch_resources_bounded(resources, timeout_seconds=0.01) is False
    assert time.monotonic() - started < 0.10
    assert resources.context_not_started is False
    await asyncio.sleep(0.18)
    assert browser.closed is True


@pytest.mark.asyncio
async def test_total_action_budget_stops_late_action_before_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = FakeJobs()
    browser = FakeBrowser({})
    runner = BrowserTaskRunner(
        jobs=jobs,
        browser_launcher=lambda: browser,
        runtime_controls=lambda **_: True,
        workspace_root=tmp_path,
    )

    async def slow_action(
        state: Any,
        page: Any,
        model: BrowserTaskInput,
        action: Any,
        *,
        index: int | None = None,
        is_initial: bool = False,
    ) -> None:
        await asyncio.sleep(0.35)

    monkeypatch.setattr(runner, "_run_action", slow_action)
    args = {
        "task_id": "task-total-budget",
        "attempt_id": "attempt-1",
        "owner_principal_id": "operator-1",
        "owner_session_id": "session-1",
        "goal_id": "goal-1",
        "goal_revision": 1,
        "board_task_revision": 2,
        "board_fencing_token": 3,
        "input_artifact_id": "art-total-budget",
        "input_artifact_digest": _ARTIFACT_DIGEST,
        "inputs": _input(),
        "runtime_seconds": 1,
        "task_priority": 50,
    }
    assert (await runner.run(**args, admission_only=True))["status"] == "admitted"
    result = await runner.run(**args, admission_only=False)

    assert result["status"] == "blocked"
    assert result["reason_code"] == "browser_runtime_deadline"
    assert "transition:succeeded" not in jobs.calls
    assert browser.closed is True


def test_sqlite_naive_durable_deadline_is_interpreted_as_utc() -> None:
    future = (datetime.now(timezone.utc) + timedelta(seconds=5)).replace(tzinfo=None).isoformat()
    assert BrowserTaskRunner._durable_remaining_seconds({"deadline_at": future}) > 0


@pytest.mark.asyncio
async def test_heartbeat_cancel_treats_child_cancellation_as_clean() -> None:
    async def heartbeat() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    assert await BrowserTaskRunner._cancel_task_bounded(task, timeout_seconds=1.0) is True
    assert task.cancelled() is True


@pytest.mark.asyncio
async def test_heartbeat_cancel_preserves_parent_cancellation() -> None:
    release = asyncio.Event()

    async def heartbeat() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Keep the child pending long enough for a parent cancellation to
            # be distinguishable from its expected CancelledError.
            await release.wait()

    async def parent() -> None:
        task = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        parent_task = asyncio.current_task()
        assert parent_task is not None
        asyncio.get_running_loop().call_soon(parent_task.cancel)
        with pytest.raises(asyncio.CancelledError):
            await BrowserTaskRunner._cancel_task_bounded(task, timeout_seconds=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    await parent()


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_publish_durable_success(tmp_path: Path) -> None:
    jobs = FakeJobs()
    responses = {
        "https://fixture.example/docs": PinnedBrowserResponse(200, {"content-type": "text/html"}, b"docs", "https://fixture.example/docs", "93.184.216.34"),
        "https://fixture.example/reference": PinnedBrowserResponse(200, {"content-type": "text/html"}, b"reference", "https://fixture.example/reference", "93.184.216.34"),
    }
    browser = FakeBrowser(responses, close_error=RuntimeError("fixture cleanup failure"))

    async def fixture(request: PinnedBrowserRequest) -> PinnedBrowserResponse:
        return responses[request.url]

    runner = BrowserTaskRunner(
        jobs=jobs,
        browser_launcher=lambda: browser,
        runtime_controls=lambda **_: True,
        transport_factory=lambda: PinnedBrowserTransport(
            resolver=lambda *_: ["93.184.216.34"],
            injected_fetch=fixture,
            site_policy=_policy,
        ),
        workspace_root=tmp_path,
    )
    admission = await runner.run(
        task_id="task-cleanup-failure",
        attempt_id="attempt-1",
        owner_principal_id="operator-1",
        owner_session_id="session-1",
        goal_id="goal-1",
        goal_revision=1,
        board_task_revision=2,
        board_fencing_token=3,
        input_artifact_id="art_cleanup_failure",
        input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),
        runtime_seconds=180,
        admission_only=True,
        task_priority=50,
    )
    assert admission["status"] == "admitted"
    result = await runner.run(
        task_id="task-cleanup-failure",
        attempt_id="attempt-1",
        owner_principal_id="operator-1",
        owner_session_id="session-1",
        goal_id="goal-1",
        goal_revision=1,
        board_task_revision=2,
        board_fencing_token=3,
        input_artifact_id="art_cleanup_failure",
        input_artifact_digest=_ARTIFACT_DIGEST,
        inputs=_input(),
        runtime_seconds=180,
        admission_only=False,
        task_priority=50,
    )
    assert result["status"] == "unknown_external_effect"
    assert result["reason_code"] == "browser_cleanup_failed"
    assert "transition:succeeded" not in jobs.calls
    assert "transition:unknown_external_effect" in jobs.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("include_allowed_request", [False, True])
async def test_blocked_route_receipts_share_bound_and_never_publish_after_overflow(
    tmp_path: Path,
    include_allowed_request: bool,
) -> None:
    """Blocked method/resource callbacks cannot create an unbounded DB write loop."""

    class FloodPage(FakePage):
        async def goto(self, url: str, **_: Any) -> SimpleNamespace:
            self.url = url
            if include_allowed_request:
                allowed_route = FakeRoute()
                await self.route_handler(allowed_route, FakeRequest(url))
                assert allowed_route.fulfilled is not None

            for index in range(task_runner_module.BROWSER_MAX_REQUESTS + 8):
                blocked_route = FakeRoute()
                if index % 2:
                    request = FakeRequest(
                        f"https://fixture.example/docs/blocked-resource-{index}",
                        method="GET",
                        resource_type="script",
                    )
                else:
                    request = FakeRequest(
                        f"https://fixture.example/docs/blocked-method-{index}",
                        method="POST",
                        resource_type="document",
                    )
                await self.route_handler(blocked_route, request)
                assert blocked_route.aborted is True
            raise RuntimeError("blocked request flood")

    class FloodBrowser(FakeBrowser):
        def __init__(self) -> None:
            super().__init__({})
            self.context.page = FloodPage({})

    jobs = CheckpointRecordingJobs()
    browser = FloodBrowser()
    network_calls: list[str] = []

    async def fixture(request: PinnedBrowserRequest) -> PinnedBrowserResponse:
        network_calls.append(request.url)
        return PinnedBrowserResponse(
            200,
            {"content-type": "text/html"},
            b"fixture",
            request.url,
            "93.184.216.34",
        )

    runner = BrowserTaskRunner(
        jobs=jobs,
        browser_launcher=lambda: browser,
        runtime_controls=lambda **_: True,
        transport_factory=lambda: PinnedBrowserTransport(
            resolver=lambda *_: ["93.184.216.34"],
            injected_fetch=fixture,
            site_policy=_policy,
        ),
        workspace_root=tmp_path,
    )
    args = {
        "task_id": f"task-receipt-cap-{int(include_allowed_request)}",
        "attempt_id": "attempt-1",
        "owner_principal_id": "operator-1",
        "owner_session_id": "session-1",
        "goal_id": "goal-1",
        "goal_revision": 1,
        "board_task_revision": 2,
        "board_fencing_token": 3,
        "input_artifact_id": f"art-receipt-cap-{int(include_allowed_request)}",
        "input_artifact_digest": _ARTIFACT_DIGEST,
        "inputs": _input(),
        "runtime_seconds": 30,
        "task_priority": 50,
    }
    assert (await runner.run(**args, admission_only=True))["status"] == "admitted"
    result = await runner.run(**args, admission_only=False)

    assert result["reason_code"] == "receipt_limit"
    expected_status = "unknown_external_effect" if include_allowed_request else "blocked"
    assert result["status"] == expected_status
    assert browser.closed is True
    assert "artifact" not in jobs.calls
    assert "transition:succeeded" not in jobs.calls
    assert len(network_calls) == (1 if include_allowed_request else 0)

    progress = [
        checkpoint
        for checkpoint in jobs.checkpoints
        if str(checkpoint.get("checkpoint_id", "")).startswith("network-progress-")
    ]
    assert len(progress) <= task_runner_module.BROWSER_MAX_REQUESTS
    assert progress
    assert max(int(item["checkpoint_payload"]["request_count"]) for item in progress) <= task_runner_module.BROWSER_MAX_REQUESTS
    assert all(
        int(item["checkpoint_payload"]["request_count"]) <= task_runner_module.BROWSER_MAX_REQUESTS
        for item in progress
    )
    assert len(jobs.checkpoints) <= task_runner_module.BROWSER_MAX_REQUESTS + 2


def _append(target: list[dict[str, Any]], value: dict[str, Any]) -> None:
    target.append(dict(value))
