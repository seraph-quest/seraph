"""#1018 acceptance: governed capability and inference services through Cordis.

Real Auth, SQLite, durable jobs, CapabilityExecutionHost, model-fabric
execute_streaming behind the single remote_inference_admission_broker and the
real reviewed Cordis child. Model inference uses fixed in-memory transport
responses; Python socket connects and DNS are denied.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import os
import socket
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from tests.cordis_research import needs_cordis, package_variant, research_host
from tests.test_inference_accounting import accounting_db, request as broker_request, setup_configuration  # noqa: F401
from tests.test_research_native_vertical import ResponseBytes, real_auth  # noqa: F401
from src.auth.middleware import OperatorAuthMiddleware
from src.runtime_plugins.dispatch import NativeServiceBlocked
from src.workflows.job_runtime import DurableJobRepository

pytestmark = [needs_cordis, pytest.mark.asyncio]
SECRET = "VAULT_SECRET_CANARY_1018_value"


def _deny_sockets(monkeypatch):
    def denied(*_args, **_kwargs):
        raise RuntimeError("external transport denied in #1018 acceptance tests")
    for target, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"), (socket, "create_connection"),
                         (socket, "getaddrinfo")):
        monkeypatch.setattr(target, name, denied)


class Model:
    """Fixed in-memory provider: canary JSON, then scripted SSE streams."""

    def __init__(self):
        self.calls, self.gates, self.script, self.bodies = [], {}, {}, []

    def transport(self):
        model = self

        class Boundary(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                body = json.loads(request.content)
                model.bodies.append(request.content)
                if not body.get("stream"):
                    payload = {"id": "canary", "choices": [{"message": {"role": "assistant", "content": "CANARY_OK"}}],
                               "usage": {"cost": "0.000001", "prompt_tokens": 1, "completion_tokens": 1}}
                    return httpx.Response(200, request=request, headers={"content-type": "application/json"},
                                          stream=ResponseBytes(json.dumps(payload).encode()))
                tag = body["messages"][-1]["content"]
                if tag != "Reply with CANARY_OK only.":
                    model.calls.append(tag)
                behaviour = model.script.get(tag, {})
                if tag in model.gates:
                    await model.gates[tag].wait()
                if behaviour.get("sleep"):
                    await asyncio.sleep(behaviour["sleep"])
                if behaviour.get("lost"):
                    raise httpx.ReadError("owned scripted provider response lost", request=request)
                text = behaviour.get("text", "Answer for " + tag)
                events = [{"choices": [{"delta": {"content": part}}]} for part in (text[:5], text[5:]) if part]
                events.append({"choices": [{"delta": {}}], "usage": {"cost": "0.000002", "prompt_tokens": 3, "completion_tokens": 3}})
                raw = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
                return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                                      stream=ResponseBytes(raw.encode()))
        return Boundary()


@asynccontextmanager
async def governed(accounting_db, monkeypatch, *, package_root=None, hook=None):  # noqa: F811
    from src.api import auth, model_fabric_settings
    from src.model_fabric.configuration import write_model_fabric_configuration
    root, _, _ = accounting_db
    configured = setup_configuration()
    # Streaming inference requires an operator route that declares and proves streaming, as chat does.
    write_model_fabric_configuration(replace(model_fabric_settings._setup_configuration(
        replace(configured.openrouter_setup, timeout_seconds=30, capabilities=("text", "streaming")), profiles=(), policies=()),
        egress_revision=configured.egress_revision + 1))
    jobs = DurableJobRepository()
    await jobs.configure_inference_accounting(1000)
    # Interactive chat and Cordis share one broker instance (the module singleton).
    from src.model_fabric import remote_inference_admission
    monkeypatch.setattr("src.model_fabric.execution.gpu_admission_broker",
                        remote_inference_admission.remote_inference_admission_broker)
    model = Model()
    original_client = httpx.AsyncClient

    def clients(**kwargs):
        kwargs.setdefault("transport", model.transport())
        return original_client(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", clients)
    _deny_sockets(monkeypatch)
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(model_fabric_settings.router, prefix="/api")
    async with original_client(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"origin": "http://localhost:3001"}) as client:
        assert (await client.post("/api/auth/login", json={"password": "research-vertical-private-secret"})).status_code == 200
        for capability in ("text", "streaming", "latency_ms", "health"):
            canary = await client.post("/api/settings/model-fabric/canary", json={
                "profile_id": "openrouter", "capability": capability, "timeout_seconds": 30})
            assert canary.json()["outcome"] == "passed", canary.text
    from src.auth.service import create_session
    _, operator = await create_session()
    frames = []

    async def observe(frame):
        frames.append((frame["method"], dict(frame["payload"])))
        if hook is not None:
            await hook(frame)
    async with research_host(jobs, package_root=package_root, observe=observe) as host:
        yield {"jobs": jobs, "host": host, "operator": operator, "model": model, "frames": frames, "root": root,
               "broker": remote_inference_admission.remote_inference_admission_broker}


async def _until(predicate, attempts=200):
    for _ in range(attempts):
        if await predicate():
            return True
        await asyncio.sleep(0.02)
    return False


def _messages(tag):
    return [{"role": "system", "content": "Answer briefly."}, {"role": "user", "content": tag}]


# ---------------------------------------------------------------- capabilities

async def test_capability_invoke_runs_the_governed_host_and_never_returns_vault_secrets(accounting_db, real_auth, monkeypatch):  # noqa: F811
    from config.settings import settings
    from src.runtime_plugins.governed_services import capability_descriptors, execute_native_capability
    from src.vault.repository import vault_repository
    monkeypatch.setattr(settings, "capability_journal_secret", "test-capability-journal-secret-1018")
    monkeypatch.setattr(settings, "capability_journal_secret_hash", "")
    async with governed(accounting_db, monkeypatch) as env:
        await vault_repository.store("cordis-1018-secret", SECRET)
        (Path(settings.workspace_dir) / "notes.txt").write_text("Release notes; token " + SECRET + " end\n")
        descriptors = capability_descriptors()
        assert descriptors["native_tool:read_file"]["state"] == "available"
        assert descriptors["native_tool:read_file"]["input_schema"] == {"file_path": {"type": "string", "nullable": False}}
        assert descriptors["native_tool:get_secret"]["reason_code"] == "vault_secret_tool_denied"
        assert descriptors["native_tool:write_file"]["reason_code"] == "capability_requires_approval"
        done = await execute_native_capability(operator=env["operator"], capability_id="native_tool:read_file",
                                               arguments={"file_path": "notes.txt"}, idempotency_key="cap-1", host=env["host"])
        assert done["job"]["status"] == "succeeded" and done["memory_status"] == "no_learning"
        assert "Release notes" in done["output"] and SECRET not in json.dumps(done)
        assert env["frames"] == [("capabilities.invoke", {"request_ref": done["job"]["job_id"]})]
        # An exact retry reuses the job and never re-executes the effect.
        again = await execute_native_capability(operator=env["operator"], capability_id="native_tool:read_file",
                                                arguments={"file_path": "notes.txt"}, idempotency_key="cap-1", host=env["host"])
        assert again["replayed"] is True and len(env["frames"]) == 1
        for capability_id, arguments, reason in (
                ("native_tool:read_file", {"file_path": 3}, "native_capability_schema_invalid"),
                ("native_tool:read_file", {"file_path": "notes.txt", "extra": "x"}, "native_capability_schema_invalid"),
                ("native_tool:get_secret", {"key": "cordis-1018-secret"}, "vault_secret_tool_denied"),
                ("native_tool:write_file", {"file_path": "x.txt", "content": "y"}, "capability_requires_approval")):
            with pytest.raises(NativeServiceBlocked) as denied:
                await execute_native_capability(operator=env["operator"], capability_id=capability_id, arguments=arguments,
                                                idempotency_key="cap-denied", host=env["host"])
            assert denied.value.reason_code == reason
        assert len(env["frames"]) == 1  # Denied before any job or crossing.


# ---------------------------------------------------------------- inference: one broker

async def test_one_broker_preserves_priority_cancellation_and_unknown_cost(accounting_db, real_auth, monkeypatch):  # noqa: F811
    from src.runtime_plugins.governed_services import execute_native_inference
    async with governed(accounting_db, monkeypatch) as env:
        broker, jobs, model = env["broker"], env["jobs"], env["model"]
        started, release, order = asyncio.Event(), asyncio.Event(), []

        async def active():
            order.append("interactive-active")
            started.set()
            await release.wait()
            return {"usage": {"cost": "0.000001"}}

        async def foreground():
            order.append("interactive-queued")
            return {"usage": {"cost": "0.000001"}}
        holder = asyncio.create_task(broker.execute(broker_request("interactive-active"), active))
        await started.wait()
        background = asyncio.create_task(execute_native_inference(operator=env["operator"], purpose="background",
            messages=_messages("cordis-background"), max_tokens=64, idempotency_key="inf-bg", host=env["host"]))
        assert await _until(lambda: _has_row(jobs, "remote:cordis-svc:"))
        interactive = asyncio.create_task(broker.execute(broker_request("interactive-queued"), foreground))
        assert await _until(lambda: _has_row(jobs, "interactive-queued"))
        release.set()
        await asyncio.gather(holder, interactive)
        done = await background
        # Interactive work outranks the queued Cordis background request in the same serial broker.
        assert order == ["interactive-active", "interactive-queued"] and model.calls == ["cordis-background"]
        assert "".join(done["frames"]) == done["output"] == "Answer for cordis-background"
        assert done["route"] == "cordis_background_inference"
        assert [row["state"] for row in done["usage"]] == ["settled"]
        assert done["job"]["status"] == "succeeded"

        # Cancellation: a queued Cordis request is released without contacting the provider.
        started2, release2 = asyncio.Event(), asyncio.Event()

        async def active2():
            started2.set()
            await release2.wait()
            return {"usage": {"cost": "0.000001"}}
        holder2 = asyncio.create_task(broker.execute(broker_request("interactive-active-2"), active2))
        await started2.wait()
        queued = asyncio.create_task(execute_native_inference(operator=env["operator"], purpose="background",
            messages=_messages("cordis-cancelled"), max_tokens=64, idempotency_key="inf-cancel", host=env["host"]))
        assert await _until(lambda: _count_rows(jobs, "remote:cordis-svc:", 2))
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        release2.set()
        await holder2
        await env["host"].stop()  # The owner's cancellation fenced this boot; restart it.
        assert await env["host"].start()
        cancelled = [row for row in (await jobs.inference_accounting_snapshot())["operations"]
                     if row["operation_id"].startswith("remote:cordis-svc:") and row["state"] != "settled"]
        assert [row["state"] for row in cancelled] == ["released"] and "cordis-cancelled" not in model.calls

        # Unknown cost: a lost response after contact stays unresolved and blocks the job.
        model.script["cordis-lost"] = {"lost": True}
        with pytest.raises(NativeServiceBlocked) as lost:
            await execute_native_inference(operator=env["operator"], purpose="interactive",
                messages=_messages("cordis-lost"), max_tokens=64, idempotency_key="inf-lost", host=env["host"])
        assert lost.value.reason_code in {"inference_cost_unresolved", "inference_request_failed"}
        snapshot = await jobs.inference_accounting_snapshot()
        unresolved = [row for row in snapshot["operations"] if row["operation_id"].startswith("remote:cordis-svc:")
                      and row["state"] == "unknown"]
        assert len(unresolved) == 1 and snapshot["unknown_microusd"] > 0
        assert model.calls.count("cordis-lost") == 1


async def _rows(jobs, prefix):
    return [row for row in (await jobs.inference_accounting_snapshot()).get("operations", [])
            if row["operation_id"].startswith(prefix)]


async def _has_row(jobs, prefix):
    return bool(await _rows(jobs, prefix))


async def _count_rows(jobs, prefix, count):
    return len(await _rows(jobs, prefix)) >= count


# ---------------------------------------------------------------- fail closed

async def test_late_output_and_revoked_authority_fail_closed(accounting_db, real_auth, monkeypatch):  # noqa: F811
    from src.runtime_plugins import service_jobs
    from src.runtime_plugins.governed_services import execute_native_inference
    state = {"revoke": False}

    async def hook(frame):
        if state["revoke"] and frame["method"] == "inference.request":
            from src.auth.service import revoke_session
            await revoke_session(env["operator"].session_id)
    async with governed(accounting_db, monkeypatch, hook=hook) as env:
        jobs, model = env["jobs"], env["model"]
        # Late plugin output: the provider answers after the original deadline; nothing is sealed or adopted.
        from datetime import timedelta
        monkeypatch.setattr(service_jobs, "SERVICE_TIMEOUT", timedelta(seconds=3))
        model.script["cordis-late"] = {"sleep": 4}
        with pytest.raises(NativeServiceBlocked):
            await execute_native_inference(operator=env["operator"], purpose="interactive", messages=_messages("cordis-late"),
                                           max_tokens=64, idempotency_key="inf-late", host=env["host"])
        late = await _governed_jobs(jobs)
        from src.runtime_plugins.dispatch import read_context
        async with jobs._session() as db:
            run = await jobs._fetch(db, late[-1]["job_id"])
            assert read_context(run)["result"] is None  # The late answer was never sealed.
        monkeypatch.setattr(service_jobs, "SERVICE_TIMEOUT", timedelta(seconds=30))
        if not env["host"].admitting:
            await env["host"].stop()
            assert await env["host"].start()
        # Revoked authority: the Root is revoked as the governed call arrives; no effect follows.
        state["revoke"] = True
        with pytest.raises(NativeServiceBlocked) as revoked:
            await execute_native_inference(operator=env["operator"], purpose="background", messages=_messages("cordis-revoked"),
                                           max_tokens=64, idempotency_key="inf-revoked", host=env["host"])
        assert revoked.value.reason_code in {"native_original_root_inactive", "native_original_policy_changed"}
        assert "cordis-revoked" not in model.calls


async def test_unavailable_route_fails_closed_before_contact(accounting_db, real_auth, monkeypatch):  # noqa: F811
    from src.model_fabric.configuration import read_model_fabric_configuration, write_model_fabric_configuration
    from src.runtime_plugins.governed_services import execute_native_inference
    async with governed(accounting_db, monkeypatch) as env:
        configured = read_model_fabric_configuration()
        write_model_fabric_configuration(replace(configured, egress_revoked=True, egress_revision=configured.egress_revision + 1))
        with pytest.raises(NativeServiceBlocked) as unavailable:
            await execute_native_inference(operator=env["operator"], purpose="background", messages=_messages("cordis-noroute"),
                                           max_tokens=64, idempotency_key="inf-noroute", host=env["host"])
        assert unavailable.value.reason_code == "inference_route_unavailable"
        assert env["model"].calls == []
        job = (await _governed_jobs(env["jobs"]))[-1]
        assert job["status"] == "blocked" and job["failure_reason"] == "inference_route_unavailable"


async def _governed_jobs(jobs):
    from sqlalchemy import select
    from src.db.models import WorkflowRunState
    from src.runtime_plugins.governed_services import GOVERNED_JOB_KIND
    async with jobs._session() as db:
        rows = (await db.execute(select(WorkflowRunState.run_identity).where(
            WorkflowRunState.job_kind == GOVERNED_JOB_KIND).order_by(WorkflowRunState.started_at))).scalars().all()
    return [await jobs.get_job(job_id) for job_id in rows]


# ---------------------------------------------------------------- unload

async def test_unloading_withdraws_registration_but_keeps_in_flight_recovery(accounting_db, real_auth, monkeypatch, tmp_path):  # noqa: F811
    from src.runtime_plugins.dispatch import read_context
    from src.runtime_plugins.governed_services import execute_native_inference

    def without_inference(profile):
        profile["plugins"] = [spec for spec in profile["plugins"] if spec["id"] != "seraph.inference.v1"]
    variant = package_variant(tmp_path / "variant", without_inference)
    async with governed(accounting_db, monkeypatch) as env:
        jobs, model = env["jobs"], env["model"]
        model.gates["cordis-inflight"] = asyncio.Event()
        inflight = asyncio.create_task(execute_native_inference(operator=env["operator"], purpose="background",
            messages=_messages("cordis-inflight"), max_tokens=64, idempotency_key="inf-unload", host=env["host"]))
        assert await _until(lambda: _contacted(model, "cordis-inflight"))
        await env["host"].stop()  # Unload while the governed call is in flight.
        with pytest.raises(Exception):
            await inflight
        job = (await _governed_jobs(jobs))[-1]
        async with jobs._session() as db:
            context = read_context(await jobs._fetch(db, job["job_id"]))
        # The durable intent and its cost liability remain for recovery; nothing is sealed or replayed.
        assert [op["op"] for op in context["operations"]] == ["intent"] and context["result"] is None
        row = next(row for row in (await jobs.inference_accounting_snapshot())["operations"]
                   if row["operation_id"] == "remote:" + job["job_id"])
        assert row["state"] in {"unknown", "released"}
        assert model.calls.count("cordis-inflight") == 1
        # Re-admission of the same key reuses the held job and never re-dispatches it.
        assert await env["host"].start()
        replay = await execute_native_inference(operator=env["operator"], purpose="background",
            messages=_messages("cordis-inflight"), max_tokens=64, idempotency_key="inf-unload", host=env["host"])
        assert replay["replayed"] is True and model.calls.count("cordis-inflight") == 1
        await env["host"].stop()
        # A composition without the inference provider withdraws the registration entirely.
        async with research_host(jobs, package_root=variant) as unloaded:
            assert not any(spec["id"] == "seraph.inference.v1" for spec in unloaded.reviewed.profile["plugins"])
            with pytest.raises(NativeServiceBlocked) as withdrawn:
                await execute_native_inference(operator=env["operator"], purpose="background", messages=_messages("cordis-after"),
                                               max_tokens=64, idempotency_key="inf-after", host=unloaded)
            assert withdrawn.value.reason_code == "service_provider_not_composed"
            assert "cordis-after" not in model.calls
        assert await env["host"].start()


async def _contacted(model, tag):
    return tag in model.calls


# ---------------------------------------------------------------- connections

async def test_bound_adapter_requires_a_current_connection_and_finite_operation_then_fails_closed(accounting_db, real_auth, monkeypatch):  # noqa: F811
    from src.db.engine import get_session
    from src.db.models import GoogleServiceConnection
    from src.runtime_plugins.governed_services import execute_native_connection_operation
    async with governed(accounting_db, monkeypatch) as env:
        operator = env["operator"]
        async with get_session() as db:
            db.add(GoogleServiceConnection(connection_id="conn-1018", owner_principal_id=operator.principal.principal_id,
                owner_session_id=operator.session_id, service="calendar_readonly", vault_secret_key="vault-conn-1018",
                state="ready", revision=2))
        for kwargs, reason in (
                ({"expected_connection_revision": 2, "operation": "calendar.read_events"}, "native_adapter_operation_not_composed"),
                ({"expected_connection_revision": 1, "operation": "calendar.read_events"}, "native_connection_candidate_changed"),
                ({"expected_connection_revision": 2, "operation": "gmail.read_messages"}, "native_adapter_operation_unknown")):
            with pytest.raises(NativeServiceBlocked) as blocked:
                await execute_native_connection_operation(operator=operator, connection_ref="conn-1018", host=env["host"],
                    idempotency_key="conn-" + str(kwargs["expected_connection_revision"]) + kwargs["operation"], **kwargs)
            assert blocked.value.reason_code == reason
            job = (await _governed_jobs(env["jobs"]))[-1]
            # The block is durable and operator-visible; no credential or effect was touched.
            assert job["status"] == "blocked" and job["failure_reason"] == reason and job["effects"] == []
        assert [method for method, _ in env["frames"]] == ["connections.invokeBoundAdapter"] * 3
        assert env["frames"][0][1] == {"operation_ref": (await _governed_jobs(env["jobs"]))[0]["job_id"]}
        with pytest.raises(NativeServiceBlocked) as unknown:
            await execute_native_connection_operation(operator=operator, connection_ref="conn-1018", expected_connection_revision=2,
                                                      operation="calendar.delete_everything", idempotency_key="conn-x", host=env["host"])
        assert unknown.value.reason_code == "native_adapter_operation_unknown" and len(env["frames"]) == 3


# ---------------------------------------------------------------- security review fixes

async def test_secret_runtime_paths_failed_reads_and_policy_changes_never_succeed(accounting_db, real_auth, monkeypatch):  # noqa: F811
    from config.settings import settings
    from src.runtime_plugins import governed_services
    from src.runtime_plugins.governed_services import execute_native_capability
    monkeypatch.setattr(settings, "capability_journal_secret", "test-capability-journal-secret-1018")
    monkeypatch.setattr(settings, "capability_journal_secret_hash", "")
    state = {"revoke_policy": False}

    async def hook(frame):
        if state["revoke_policy"]:
            monkeypatch.setattr(governed_services, "READ_ONLY_CAPABILITIES", frozenset())
    async with governed(accounting_db, monkeypatch, hook=hook) as env:
        workspace = Path(settings.workspace_dir)
        (workspace / ".vault-key").write_text("MASTER_KEY_CANARY_1018")
        (workspace / "d").mkdir()
        (workspace / "notes").mkdir(exist_ok=True)
        (workspace / "notes" / "credentials.txt").write_text("x")
        # Vault/secret/recovery and runtime-owned files are denied before any job or crossing.
        # A hard link is a different name for the same protected file: denied by identity.
        os.link(workspace / ".vault-key", workspace / "notes" / "alias.txt")
        for index, path in enumerate((".vault-key", "d/../.vault-key", "./.vault-key", "seraph.db", "seraph.db-wal",
                                      "google_calendar_token.json", "../outside.txt", "",
                                      # Case-insensitive filesystems open these as the protected files.
                                      ".Vault-Key", ".VAULT-KEY", "SERAPH.DB", "Google_Calendar_Token.json",
                                      "notes/alias.txt")):
            with pytest.raises(NativeServiceBlocked) as denied:
                await execute_native_capability(operator=env["operator"], capability_id="native_tool:read_file",
                                                arguments={"file_path": path}, idempotency_key=f"vk-{index}", host=env["host"])
            assert denied.value.reason_code == "native_capability_path_denied", path
        assert env["frames"] == [] and await _governed_jobs(env["jobs"]) == []
        # preview_workspace_patch returns a pre-encoded JSON diff in which a Vault value can be
        # escaped or split across lines, so it is not a Cordis read-only capability.
        with pytest.raises(NativeServiceBlocked) as preview:
            await execute_native_capability(operator=env["operator"], capability_id="native_tool:preview_workspace_patch",
                arguments={"file_path": "notes/plan.txt", "old_text": "M", "new_text": "N", "expected_occurrences": 1},
                idempotency_key="preview", host=env["host"])
        assert preview.value.reason_code == "capability_requires_approval"
        # A raising handler is a failed effect: blocked, never a successful empty result.
        with pytest.raises(NativeServiceBlocked) as failed:
            await execute_native_capability(operator=env["operator"], capability_id="native_tool:read_file",
                                            arguments={"file_path": "notes/credentials.txt"}, idempotency_key="failed",
                                            host=env["host"])
        assert failed.value.reason_code == "native_capability_failed"
        job = (await _governed_jobs(env["jobs"]))[-1]
        assert job["status"] == "blocked" and job["failure_reason"] == "native_capability_failed"
        # Large read-only output is truncated with a marker, not failed after the effect.
        (workspace / "big.txt").write_text("y" * 40000)
        big = await execute_native_capability(operator=env["operator"], capability_id="native_tool:read_file",
                                              arguments={"file_path": "big.txt"}, idempotency_key="big", host=env["host"])
        assert big["job"]["status"] == "succeeded" and "[output truncated by Seraph:" in big["output"]
        assert len(big["output"].encode()) <= governed_services.MAX_OUTPUT_BYTES
        # Policy is rechecked at effect time: a capability withdrawn after admission never runs.
        (workspace / "late-policy.txt").write_text("never read")
        state["revoke_policy"] = True
        with pytest.raises(NativeServiceBlocked) as changed:
            await execute_native_capability(operator=env["operator"], capability_id="native_tool:read_file",
                                            arguments={"file_path": "late-policy.txt"}, idempotency_key="policy", host=env["host"])
        assert changed.value.reason_code == "native_capability_policy_changed"
        job = (await _governed_jobs(env["jobs"]))[-1]
        assert job["status"] == "blocked" and job["effects"] == []


async def test_late_and_cancelled_capability_results_are_settled_and_recoverable(accounting_db, real_auth, monkeypatch):  # noqa: F811
    import threading
    from datetime import timedelta
    from config.settings import settings
    from src.extensions import capability_execution
    from src.runtime_plugins import service_jobs
    from src.runtime_plugins.dispatch import read_context
    from src.runtime_plugins.governed_services import execute_native_capability
    monkeypatch.setattr(settings, "capability_journal_secret", "test-capability-journal-secret-1018")
    monkeypatch.setattr(settings, "capability_journal_secret_hash", "")
    real_host, gate, entered = capability_execution.current_capability_execution_host, threading.Event(), threading.Event()

    class SlowHost:
        def execute(self, request):
            entered.set()
            gate.wait(10)
            return real_host().execute(request)
    monkeypatch.setattr(capability_execution, "current_capability_execution_host", lambda: SlowHost())
    async with governed(accounting_db, monkeypatch) as env:
        jobs = env["jobs"]
        (Path(settings.workspace_dir) / "notes.txt").write_text("late notes")
        # Late: the handler answers after the original deadline; nothing is sealed.
        monkeypatch.setattr(service_jobs, "SERVICE_TIMEOUT", timedelta(seconds=3))
        late = asyncio.create_task(execute_native_capability(operator=env["operator"], capability_id="native_tool:read_file",
                                   arguments={"file_path": "notes.txt"}, idempotency_key="late-cap", host=env["host"]))
        await asyncio.to_thread(entered.wait, 10)
        await asyncio.sleep(3.5)
        gate.set()
        with pytest.raises(NativeServiceBlocked):
            await late
        job = (await _governed_jobs(jobs))[-1]
        async with jobs._session() as db:
            assert read_context(await jobs._fetch(db, job["job_id"]))["result"] is None
        # The expired attempt lost its authority to settle; stale-lease recovery owns it and
        # keeps the recorded intent visible as an unresolved effect.
        if job["status"] == "running":
            from datetime import datetime, timezone
            job = await jobs.recover_stale_job(job["job_id"], now=datetime.now(timezone.utc) + timedelta(hours=1))
        assert (job["status"], job["failure_reason"]) == ("failed", "deadline_expired")
        assert [(item["receipt_kind"], item["status"]) for item in job["effects"]] == [("effect", "intent")]
        monkeypatch.setattr(service_jobs, "SERVICE_TIMEOUT", timedelta(seconds=30))
        if not env["host"].admitting:
            await env["host"].stop()
            assert await env["host"].start()
        # Cancelled: the owner settles the claimed job as blocked with its intent kept.
        gate.clear(), entered.clear()
        cancelled = asyncio.create_task(execute_native_capability(operator=env["operator"],
            capability_id="native_tool:read_file", arguments={"file_path": "notes.txt"}, idempotency_key="cancel-cap",
            host=env["host"]))
        await asyncio.to_thread(entered.wait, 10)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        gate.set()
        job = (await _governed_jobs(jobs))[-1]
        assert job["status"] == "blocked" and job["failure_reason"] == "native_governed_cancelled"
        async with jobs._session() as db:
            assert [op["op"] for op in read_context(await jobs._fetch(db, job["job_id"]))["operations"]] == ["intent"]
        assert [item["status"] for item in job["effects"]] == ["intent"]


async def test_outbound_redaction_cordis_lanes_and_unknown_cost_fence(accounting_db, real_auth, monkeypatch):  # noqa: F811
    import time as clock
    from src.runtime_plugins import governed_services
    from src.runtime_plugins.governed_services import execute_native_inference
    from src.vault.repository import vault_repository
    async with governed(accounting_db, monkeypatch) as env:
        broker, jobs, model = env["broker"], env["jobs"], env["model"]
        await vault_repository.store("cordis-1018-outbound", SECRET)
        # Outbound messages are Vault-redacted before any provider sees them, including JSON-escaped forms.
        done = await execute_native_inference(operator=env["operator"], purpose="interactive",
            messages=[{"role": "system", "content": 'cfg {"k": "' + SECRET + '"}'}, {"role": "user", "content": "say " + SECRET}],
            max_tokens=64, idempotency_key="outbound", host=env["host"])
        assert done["job"]["status"] == "succeeded"
        assert model.bodies and all(SECRET.encode() not in body for body in model.bodies) and SECRET not in json.dumps(done)

        # Cordis interactive outranks Cordis background through the service, in one broker.
        started, release = asyncio.Event(), asyncio.Event()

        async def active():
            started.set()
            await release.wait()
            return {"usage": {"cost": "0.000001"}}
        holder = asyncio.create_task(broker.execute(broker_request("hold-lanes"), active))
        await started.wait()
        background = asyncio.create_task(execute_native_inference(operator=env["operator"], purpose="background",
            messages=_messages("lane-background"), max_tokens=64, idempotency_key="lane-bg", host=env["host"]))
        assert await _until(lambda: _count_rows(jobs, "remote:cordis-svc:", 2))
        interactive = asyncio.create_task(execute_native_inference(operator=env["operator"], purpose="interactive",
            messages=_messages("lane-interactive"), max_tokens=64, idempotency_key="lane-int", host=env["host"]))
        assert await _until(lambda: _count_rows(jobs, "remote:cordis-svc:", 3))
        release.set()
        await asyncio.gather(holder, background, interactive)
        assert model.calls[-2:] == ["lane-interactive", "lane-background"]

        # Cordis has its own per-owner sub-quota: a burst is refused before the broker, chat keeps its slots.
        monkeypatch.setattr(governed_services, "CORDIS_INFERENCE_PER_OWNER", 1)
        model.gates["lane-burst-1"] = asyncio.Event()
        first = asyncio.create_task(execute_native_inference(operator=env["operator"], purpose="background",
            messages=_messages("lane-burst-1"), max_tokens=64, idempotency_key="burst-1", host=env["host"]))
        assert await _until(lambda: _contacted(model, "lane-burst-1"))
        with pytest.raises(NativeServiceBlocked) as burst:
            await execute_native_inference(operator=env["operator"], purpose="background", messages=_messages("lane-burst-2"),
                                           max_tokens=64, idempotency_key="burst-2", host=env["host"])
        assert burst.value.reason_code == "cordis_inference_capacity" and "lane-burst-2" not in model.calls
        chat = asyncio.create_task(broker.execute(broker_request("chat-during-burst"), _usage))
        assert await _until(lambda: _has_row(jobs, "chat-during-burst"))  # Admitted, not refused for capacity.
        model.gates["lane-burst-1"].set()
        assert (await first)["job"]["status"] == "succeeded"
        assert (await chat)["usage"]
        monkeypatch.setattr(governed_services, "CORDIS_INFERENCE_PER_OWNER", 4)

        # Unknown cost: by broker design an uncertain provider result keeps the serial slot
        # until it is reconciled, so the next request (Cordis or chat) waits rather than
        # risking double spend; once reconciled it proceeds.
        model.script["lane-lost"] = {"lost": True}
        with pytest.raises(NativeServiceBlocked):
            await execute_native_inference(operator=env["operator"], purpose="interactive", messages=_messages("lane-lost"),
                                           max_tokens=64, idempotency_key="lane-lost", host=env["host"])
        lost_job = (await _governed_jobs(jobs))[-1]
        held = broker.receipt_for("remote:" + lost_job["job_id"])
        assert held.status == "blocked" and held.reconciliation_required
        after = asyncio.create_task(execute_native_inference(operator=env["operator"], purpose="interactive",
            messages=_messages("lane-after-lost"), max_tokens=64, idempotency_key="lane-after", host=env["host"]))
        assert await _until(lambda: _count_rows(jobs, "remote:cordis-svc:", 6))
        await asyncio.sleep(0.5)
        assert "lane-after-lost" not in model.calls and not after.done()
        await broker.reconcile(held.operation_id, owner_id=held.owner_id, job_id=held.job_id,
                               fencing_token=held.fencing_token, outcome="failed", actual_cost_microusd=2)
        assert (await after)["job"]["status"] == "succeeded" and "lane-after-lost" in model.calls


async def _usage():
    return {"usage": {"cost": "0.000001"}}


async def test_identity_check_covers_case_insensitive_spellings_and_redaction_covers_multiline_and_non_ascii(accounting_db, real_auth, monkeypatch):  # noqa: F811
    from config.settings import settings
    from src.runtime_plugins import governed_services
    from src.runtime_plugins.governed_services import execute_native_capability
    from src.vault.repository import vault_repository
    monkeypatch.setattr(settings, "capability_journal_secret", "test-capability-journal-secret-1018")
    monkeypatch.setattr(settings, "capability_journal_secret_hash", "")
    async with governed(accounting_db, monkeypatch) as env:
        workspace = Path(settings.workspace_dir)
        (workspace / ".vault-key").write_text("MASTER_KEY_CANARY_1018")
        (workspace / "Notes.txt").write_text("ordinary")
        # Simulate a case-insensitive filesystem: this spelling opens the Vault master key.
        real_stat = os.stat

        def folded_stat(path, *args, **kwargs):
            if str(path).endswith("Notes.txt"):
                return real_stat(workspace / ".vault-key")
            return real_stat(path, *args, **kwargs)
        monkeypatch.setattr(governed_services.os, "stat", folded_stat)
        with pytest.raises(NativeServiceBlocked) as denied:
            await execute_native_capability(operator=env["operator"], capability_id="native_tool:read_file",
                                            arguments={"file_path": "Notes.txt"}, idempotency_key="folded", host=env["host"])
        assert denied.value.reason_code == "native_capability_path_denied" and env["frames"] == []
        monkeypatch.setattr(governed_services.os, "stat", real_stat)
        # Multi-line and non-ASCII Vault values never leave in a capability result.
        for index, value in enumerate(("first line\nsecond line CANARY_1018", "caf\u00e9 \"quoted\" \\ CANARY_1018")):
            await vault_repository.store(f"cordis-1018-shape-{index}", value)
            (workspace / f"shape-{index}.txt").write_text("before " + value + " after\n")
            done = await execute_native_capability(operator=env["operator"], capability_id="native_tool:read_file",
                                                   arguments={"file_path": f"shape-{index}.txt"},
                                                   idempotency_key=f"shape-{index}", host=env["host"])
            assert "before" in done["output"] and value not in done["output"] and value not in json.dumps(done)
            assert "CANARY_1018" not in done["output"]


async def test_benign_inference_denials_leave_no_intent(accounting_db, real_auth, monkeypatch):  # noqa: F811
    from src.runtime_plugins import governed_services
    from src.runtime_plugins.dispatch import read_context
    from src.runtime_plugins.governed_services import execute_native_inference
    async with governed(accounting_db, monkeypatch) as env:
        monkeypatch.setattr(governed_services, "CORDIS_INFERENCE_PER_OWNER", 0)
        with pytest.raises(NativeServiceBlocked) as full:
            await execute_native_inference(operator=env["operator"], purpose="background", messages=_messages("cordis-full"),
                                           max_tokens=64, idempotency_key="inf-full", host=env["host"])
        assert full.value.reason_code == "cordis_inference_capacity"
        job = (await _governed_jobs(env["jobs"]))[-1]
        async with env["jobs"]._session() as db:
            assert read_context(await env["jobs"]._fetch(db, job["job_id"]))["operations"] == []
        assert job["status"] == "blocked" and job["effects"] == [] and env["model"].calls == []
        assert governed_services._cordis_inflight == {}
