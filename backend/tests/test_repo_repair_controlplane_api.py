"""Authenticated control-plane checks for the repository iteration API.

The positive journey below uses the genuine SQLite source producer and fixed
callback from ``test_repo_work_task_publication``.  The remaining tests keep
the request closure and fail-closed source-root checks narrow.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.api import workflows as workflows_api
from src.api.workflows import RepoRepairEgressConsentRequest
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_inference_accounting import accounting_db


def _legacy_consent() -> dict[str, object]:
    return {
        "expected_job_revision": 1,
        "source_packet_digest": "a" * 64,
        "expected_source_manifest_digest": "b" * 64,
        "expected_profile_id": "openrouter",
        "acknowledged_selected_source": True,
        "idempotency_key": "legacy-consent",
    }


def _iteration_consent() -> dict[str, object]:
    return {
        **_legacy_consent(),
        "expected_iteration_index": 1,
        "expected_iteration_id": "c" * 64,
        "expected_preparation_digest": "d" * 64,
        "expected_request_body_digest": "e" * 64,
        "expected_request_route_digest": "f" * 64,
        "expected_egress_envelope_digest": "0" * 64,
        "expected_diagnostics_digest": "1" * 64,
        "expected_redaction_version": "seraph.redaction.v1",
        "acknowledged_diagnostics": True,
    }


def test_legacy_and_iteration_consent_are_closed_variants():
    legacy = RepoRepairEgressConsentRequest.model_validate(_legacy_consent())
    iteration = RepoRepairEgressConsentRequest.model_validate(_iteration_consent())

    assert legacy.is_repository_iteration_variant is False
    assert iteration.is_repository_iteration_variant is True


@pytest.mark.parametrize(
    "payload",
    [
        {**_legacy_consent(), "expected_iteration_index": 1},
        {**_legacy_consent(), "expected_iteration_id": None},
        {**_iteration_consent(), "acknowledged_diagnostics": False},
        {**_legacy_consent(), "unexpected": "caller-field"},
        {key: value for key, value in _legacy_consent().items() if key != "idempotency_key"},
    ],
)
def test_iteration_consent_rejects_partial_mixed_extra_and_incomplete_requests(payload):
    with pytest.raises(ValidationError):
        RepoRepairEgressConsentRequest.model_validate(payload)


@pytest.mark.parametrize("field,value", [("expected_job_revision", "1"),
    ("expected_proposal_revision", True), ("proposal_id", None), ("extra", "caller")])
def test_source_resume_remains_exact_closed_request(field, value):
    payload = {"approval_id": "approval", "proposal_id": "proposal",
        "expected_proposal_revision": 1, "expected_job_revision": 1, "idempotency_key": "resume"}
    payload[field] = value
    with pytest.raises(ValidationError):
        workflows_api.RepoRepairResumeRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_source_root_rejects_legacy_only_consent_without_calling_legacy_resume(monkeypatch):
    operator = SimpleNamespace(
        principal=SimpleNamespace(principal_id="operator"),
        session_id="session",
    )
    monkeypatch.setattr(workflows_api, "_require_authenticated_capability_operator", lambda _request: operator)
    monkeypatch.setattr(
        workflows_api,
        "_owned_repo_repair_job",
        lambda _job_id, _operator: _owned_job(),
    )
    monkeypatch.setattr(workflows_api, "_repo_repair_source_root", lambda _job_id, _operator: _true())

    resumed = False

    async def forbidden_resume(*_args, **_kwargs):
        nonlocal resumed
        resumed = True

    monkeypatch.setattr(workflows_api, "_resume_repo_repair_board_attempt", forbidden_resume)

    with pytest.raises(HTTPException) as denied:
        await workflows_api.grant_repo_repair_code_egress_consent(
            "repo-job",
            RepoRepairEgressConsentRequest.model_validate(_legacy_consent()),
            object(),
        )

    assert denied.value.status_code == 422
    assert denied.value.detail["code"] == "repair_iteration_consent_required"
    assert resumed is False


@pytest.mark.asyncio
async def test_source_consent_reports_inactive_general_task_service(monkeypatch):
    operator = SimpleNamespace(
        principal=SimpleNamespace(principal_id="operator"),
        session_id="session",
    )
    monkeypatch.setattr(workflows_api, "_require_authenticated_capability_operator", lambda _request: operator)
    monkeypatch.setattr(
        workflows_api,
        "_owned_repo_repair_job",
        lambda _job_id, _operator: _owned_job(),
    )
    monkeypatch.setattr(workflows_api, "_repo_repair_source_root", lambda _job_id, _operator: _true())

    from src.api import work_board as work_board_api

    monkeypatch.setattr(work_board_api.dispatcher, "general_tasks", None)
    with pytest.raises(HTTPException) as denied:
        await workflows_api.grant_repo_repair_code_egress_consent(
            "repo-job",
            RepoRepairEgressConsentRequest.model_validate(_iteration_consent()),
            object(),
        )

    assert denied.value.status_code == 503
    assert denied.value.detail["code"] == "general_task_inactive"


@pytest.mark.asyncio
async def test_source_consent_requires_selected_source_acknowledgement(monkeypatch):
    operator = SimpleNamespace(
        principal=SimpleNamespace(principal_id="operator"),
        session_id="session",
    )
    monkeypatch.setattr(workflows_api, "_require_authenticated_capability_operator", lambda _request: operator)
    monkeypatch.setattr(
        workflows_api,
        "_owned_repo_repair_job",
        lambda _job_id, _operator: _owned_job(),
    )
    monkeypatch.setattr(workflows_api, "_repo_repair_source_root", lambda _job_id, _operator: _true())

    request = _iteration_consent()
    request["acknowledged_selected_source"] = False
    with pytest.raises(HTTPException) as denied:
        await workflows_api.grant_repo_repair_code_egress_consent(
            "repo-job",
            RepoRepairEgressConsentRequest.model_validate(request),
            object(),
        )

    assert denied.value.status_code == 422
    assert denied.value.detail["code"] == "repair_source_acknowledgement_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["preview", "status", "resume"])
async def test_corrupt_source_root_fails_closed_before_legacy_preview(monkeypatch, surface):
    operator = SimpleNamespace(
        principal=SimpleNamespace(principal_id="operator"),
        session_id="session",
    )
    monkeypatch.setattr(workflows_api, "_require_authenticated_capability_operator", lambda _request: operator)
    monkeypatch.setattr(
        workflows_api,
        "_owned_repo_repair_job",
        lambda _job_id, _operator: _owned_job(),
    )

    from src.workflows.job_runtime import DurableJobLeaseError

    async def corrupt_root(_job_id, _operator):
        raise DurableJobLeaseError("protected repository record changed")

    monkeypatch.setattr(workflows_api, "_repo_repair_source_root", corrupt_root)
    legacy_rows_read = False

    async def forbidden_rows(*_args, **_kwargs):
        nonlocal legacy_rows_read
        legacy_rows_read = True
        raise AssertionError("corrupt source evidence must not fall back to legacy rows")

    monkeypatch.setattr(workflows_api, "_repo_repair_rows", forbidden_rows)

    with pytest.raises(HTTPException) as denied:
        if surface == "preview":
            await workflows_api.get_repo_repair_source_preview("repo-job", object())
        elif surface == "status":
            await workflows_api.get_repo_repair("repo-job", object())
        else:
            await workflows_api.resume_repo_repair("repo-job", workflows_api.RepoRepairResumeRequest(
                approval_id="approval", proposal_id="proposal", expected_proposal_revision=1,
                expected_job_revision=1, idempotency_key="resume"), object())

    assert denied.value.status_code == 409
    assert denied.value.detail["code"] == "repair_recovery_blocked"
    assert legacy_rows_read is False


@pytest.mark.asyncio
async def test_authenticated_source_preview_and_consent_use_actual_source_services(
    accounting_db, monkeypatch, repository_admission_signer):
    """Exercise the real GET -> exact POST -> one callback control-plane path."""

    import httpx
    from sqlalchemy import select

    from config.settings import settings
    from src.api import work_board as work_board_api
    from src.api import workflows as workflows_api
    from src.app import create_app
    from src.auth import service as auth_service
    from src.db.models import InferenceCostReservation
    from tests.test_repo_work_task_publication import actual_native_source

    captured: dict[str, object] = {}
    original_create_session = auth_service.create_session

    async def capture_session(*args, **kwargs):
        token, operator = await original_create_session(*args, **kwargs)
        captured["token"] = token
        captured["operator"] = operator
        return token, operator

    # The existing source fixture creates the real owner session internally.
    # Capture its bearer so the API route is authenticated as that same owner.
    monkeypatch.setattr(auth_service, "create_session", capture_session)
    factory, owner, service, jobs, binding, _request = await actual_native_source(
        accounting_db, monkeypatch, goal_capacity=2, claim_child=False
    )
    token = captured["token"]
    operator = captured["operator"]
    assert isinstance(token, str)
    assert operator.principal.principal_id == owner.principal_id
    assert operator.session_id == owner.session_id

    from src.workflows.repo_repair_source import prepare_repository_native_source

    prepared = await prepare_repository_native_source(
        service,
        jobs,
        binding,
        child_owner="actual-api-source-worker",
        principal=operator.principal,
    )
    root_id = prepared["repository_job_id"]

    # The API creates its source service and uses its process-global dispatcher;
    # point those handles at the fixture's actual database and durable jobs.
    monkeypatch.setattr(workflows_api, "get_session", factory.accounting_sessions)
    monkeypatch.setattr(workflows_api, "durable_job_repository", jobs)
    monkeypatch.setattr(work_board_api, "get_session", factory.accounting_sessions)
    monkeypatch.setattr(work_board_api.dispatcher, "jobs", jobs)
    monkeypatch.setattr(work_board_api.dispatcher, "general_tasks", service)
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "localhost,test,127.0.0.1")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)

    app = create_app()
    headers = {"Origin": "http://localhost:3001"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers=headers,
    ) as client:
        client.cookies.set(settings.operator_auth_cookie_name, token)
        preview_response = await client.get(
            f"/api/workflows/repo-repair/{root_id}/source-preview"
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        review = preview["repository_review"]
        packet = preview["source_packet"]
        egress = preview["egress"]
        assert preview["job_id"] == root_id
        assert preview["provider_contacted"] is False
        assert review["native_child_id"] == binding.invocation_id
        assert review["repository_job_id"] == root_id
        assert review["iteration_index"] == prepared["iteration_index"] == 1
        assert review["iteration_id"] == prepared["iteration_id"]
        assert review["preparation_digest"] == prepared["preparation_digest"]
        assert review["contact_state"] == "not_started"
        assert packet["state"] == "verified"
        assert packet["selected_files"][0]["path"] == "calculator.py"
        assert set(egress["diagnostics"]) == {"stdout", "stderr", "redaction_version"}
        assert egress["diagnostics"]["stdout"] == ""
        assert egress["diagnostics"]["stderr"] == ""
        assert egress["diagnostics"]["redaction_version"] == egress["redaction_version"]

        consent_body = {
            "expected_job_revision": int(preview["revision"]),
            "source_packet_digest": packet["artifact_sha256"],
            "expected_source_manifest_digest": packet["source_manifest_sha256"],
            "expected_profile_id": egress["effective_profile_id"],
            "acknowledged_selected_source": True,
            "idempotency_key": "actual-api-first-contact",
            "expected_iteration_index": review["iteration_index"],
            "expected_iteration_id": review["iteration_id"],
            "expected_preparation_digest": review["preparation_digest"],
            "expected_request_body_digest": egress["request_body_digest"],
            "expected_request_route_digest": egress["request_route_digest"],
            "expected_egress_envelope_digest": egress["egress_envelope_digest"],
            "expected_diagnostics_digest": egress["diagnostics_digest"],
            "expected_redaction_version": egress["redaction_version"],
            "acknowledged_diagnostics": True,
        }
        before_child = await jobs.get_job(binding.invocation_id)
        before_root = await jobs.get_job(root_id)
        bad_body = {
            **consent_body,
            "expected_request_body_digest": "f" * 64,
        }
        denied = await client.post(
            f"/api/workflows/repo-repair/{root_id}/code-egress-consent",
            json=bad_body,
        )
        assert denied.status_code == 409, denied.text
        assert denied.json()["detail"]["code"] == "repository_iteration_consent_binding_changed"
        after_bad_root = await jobs.get_job(root_id)
        assert after_bad_root["revision"] == before_root["revision"]
        assert after_bad_root["status"] == before_root["status"]
        assert after_bad_root["lease"] == before_root["lease"]
        assert (await jobs.get_job(binding.invocation_id))["lease"] == before_child["lease"]
        async with factory() as db:
            assert list((await db.scalars(select(InferenceCostReservation))).all()) == []

        messages = egress["request_body"]["messages"]
        source = json.loads(messages[1]["content"])
        output = {
            "summary": "Correct addition",
            "base_snapshot_sha256": source["source_packet"]["base_snapshot_sha256"],
            "patch_unified_diff": "--- a/calculator.py\n+++ b/calculator.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n",
            "allowed_paths": ["calculator.py", "tests/test_calculator.py"],
            "test_args": ["pytest", "-q", "tests/test_calculator.py"],
            "expected_outcome": "The addition check passes",
        }
        contacted = []

        def final_http(request):
            assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
            assert json.loads(request.content) == egress["request_body"]
            contacted.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "scripted-api-source-final-transport",
                    "usage": {"cost": "0", "prompt_tokens": 1, "completion_tokens": 1},
                    "choices": [
                        {"message": {"role": "assistant", "content": json.dumps(output)}}
                    ],
                },
            )

        real_client = httpx.Client

        def owned_client(*args, **kwargs):
            return real_client(*args, **kwargs, transport=httpx.MockTransport(final_http))

        monkeypatch.setattr(httpx, "Client", owned_client)
        accepted = await client.post(
            f"/api/workflows/repo-repair/{root_id}/code-egress-consent",
            json=consent_body,
        )
        assert accepted.status_code == 200, accepted.text
        accepted_payload = accepted.json()
        assert accepted_payload["job_id"] == root_id
        assert accepted_payload["repository_outcome"]["awaiting_repository_wait"] is True
        assert len(contacted) == 1

    after_child = await jobs.get_job(binding.invocation_id)
    assert after_child["status"] == "paused"
    assert after_child["attempt_count"] == 1
    for key in ("owner", "fencing_token", "expires_at"):
        assert after_child["lease"][key] == before_child["lease"][key]
    assert after_child["lease"]["revision"] == before_child["lease"]["revision"] + 1
    async with factory() as db:
        rows = list((await db.scalars(select(InferenceCostReservation))).all())
        assert len(rows) == 1
        assert rows[0].state == "settled"
        assert rows[0].contact_started_at is not None
        assert rows[0].actual_cost_microusd == 0
        assert rows[0].job_id == root_id


async def _owned_job():
    return "repo-job", {"status": "running", "revision": 1}


async def _true():
    return True


@pytest.mark.asyncio
@pytest.mark.parametrize("language,readback_drift", [("test_python", False),
    ("test_node", False), ("test_node", True)])
async def test_authenticated_source_three_iteration_approval_execution_and_readback(
    accounting_db, monkeypatch, language, readback_drift, repository_admission_signer):
    """Real auth/API/SQLite/CPU execution; only final HTTP bytes are scripted."""
    import httpx
    from sqlalchemy import select
    from config.settings import settings
    from src.api import work_board as board_api
    from src.app import create_app
    from src.auth import service as auth_service
    from src.db.models import InferenceCostReservation
    from src.workflows.repo_repair_source import prepare_repository_native_source
    from tests.test_repo_work_task_publication import actual_native_source

    credentials = {}
    create_session = auth_service.create_session

    async def capture_session(*args, **kwargs):
        token, operator = await create_session(*args, **kwargs)
        credentials.update(token=token, operator=operator)
        return token, operator

    monkeypatch.setattr(auth_service, "create_session", capture_session)
    factory, owner, service, jobs, binding, _ = await actual_native_source(
        accounting_db, monkeypatch, goal_capacity=2, claim_child=False, language=language,
    )
    operator = credentials["operator"]
    prepared = await prepare_repository_native_source(service, jobs, binding,
        child_owner="actual-api-three-worker", principal=operator.principal)
    root_id = prepared["repository_job_id"]
    monkeypatch.setattr(workflows_api, "get_session", factory.accounting_sessions)
    monkeypatch.setattr(workflows_api, "durable_job_repository", jobs)
    monkeypatch.setattr(board_api, "get_session", factory.accounting_sessions)
    monkeypatch.setattr(board_api.dispatcher, "jobs", jobs)
    monkeypatch.setattr(board_api.dispatcher, "general_tasks", service)
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "localhost,test,127.0.0.1")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    captures = []
    contacted = []
    expected_body = None
    output = None

    def final_http(request):
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert json.loads(request.content) == expected_body
        contacted.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "scripted-api-three-final-http",
            "usage": {"cost": "0", "prompt_tokens": 1, "completion_tokens": 1},
            "choices": [{"message": {"role": "assistant", "content": json.dumps(output)}}],
        })

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs:
        real_client(*args, **kwargs, transport=httpx.MockTransport(final_http)))
    app = create_app()
    prefix = f"/api/workflows/repo-repair/{root_id}"
    initial_child = await jobs.get_job(binding.invocation_id)
    root_initial = await jobs.get_job(root_id)
    source_path = "calculator.js" if language == "test_node" else "calculator.py"
    original_bytes = (accounting_db[0] / "example" / source_path).read_bytes()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url="http://localhost", headers={"Origin": "http://localhost:3001"}) as client:
        async def call(method, path, *, body=None, status=200):
            response = await client.request(method, path, json=body)
            payload = response.json()
            captures.append({"method": method, "path": path, "request": body,
                "status": response.status_code, "response": payload})
            assert response.status_code == status, response.text
            return payload

        await call("GET", prefix, status=401)
        client.cookies.set(settings.operator_auth_cookie_name, credentials["token"])
        await call("GET", f"/api/workflows/repo-repair/{binding.invocation_id}", status=404)
        other_token, _other_operator = await create_session()
        client.cookies.set(settings.operator_auth_cookie_name, other_token)
        await call("GET", prefix, status=403)
        await call("GET", prefix + "/source-preview", status=403)
        await call("POST", prefix + "/resume", body={
            "approval_id": "wrong-owner", "proposal_id": "wrong-owner",
            "expected_proposal_revision": 1, "expected_job_revision": 1,
            "idempotency_key": "wrong-owner"}, status=403)
        client.cookies.set(settings.operator_auth_cookie_name, credentials["token"])
        for index in (1, 2, 3):
            def forbid_private(*_args, **_kwargs):
                raise AssertionError("status GET cannot read source, proposal or transport artifacts")
            with monkeypatch.context() as private_reads:
                private_reads.setattr(service.repository_source_service, "_read_private_artifact", forbid_private)
                before = await call("GET", prefix)
            assert before["repository_review"]["iteration_index"] == index
            preview = await call("GET", prefix + "/source-preview")
            review, egress, packet = preview["repository_review"], preview["egress"], preview["source_packet"]
            if index > 1:
                assert ("ERR_ASSERTION" if language == "test_node" else "FAILED") in egress["diagnostics"]["stdout"]
            expected_body = egress["request_body"]
            selected_source = json.loads(expected_body["messages"][1]["content"])
            expression = "a - b + 1" if index == 2 else "a + b" if index == 3 else "a - b + 0"
            patch = ("--- a/calculator.js\n+++ b/calculator.js\n@@ -1 +1 @@\n-exports.add = (a, b) => a - b;\n+exports.add = (a, b) => " + expression + ";\n"
                if language == "test_node" else "--- a/calculator.py\n+++ b/calculator.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return " + expression + "\n")
            output = {"summary": "Correct addition", "base_snapshot_sha256": selected_source["source_packet"]["base_snapshot_sha256"],
                "patch_unified_diff": patch,
                "allowed_paths": [source_path, "tests/calculator.test.js" if language == "test_node" else "tests/test_calculator.py"],
                "test_args": ["npm", "test"] if language == "test_node" else ["pytest", "-q", "tests/test_calculator.py"],
                "expected_outcome": "The addition check passes"}
            consent = {"expected_job_revision": preview["revision"], "source_packet_digest": packet["artifact_sha256"],
                "expected_source_manifest_digest": packet["source_manifest_sha256"], "expected_profile_id": egress["effective_profile_id"],
                "acknowledged_selected_source": True, "idempotency_key": f"api-contact-{index}",
                "expected_iteration_index": index, "expected_iteration_id": review["iteration_id"],
                "expected_preparation_digest": review["preparation_digest"], "expected_request_body_digest": egress["request_body_digest"],
                "expected_request_route_digest": egress["request_route_digest"], "expected_egress_envelope_digest": egress["egress_envelope_digest"],
                "expected_diagnostics_digest": egress["diagnostics_digest"], "expected_redaction_version": egress["redaction_version"],
                "acknowledged_diagnostics": True}
            accepted = await call("POST", prefix + "/code-egress-consent", body=consent)
            assert accepted["repository_outcome"]["awaiting_repository_wait"] is True
            assert len(contacted) == index
            with monkeypatch.context() as private_reads:
                private_reads.setattr(service.repository_source_service, "_read_private_artifact", forbid_private)
                pending = await call("GET", prefix)
            proposal = pending["patch_proposal"]
            resume = {"approval_id": proposal["approval_id"], "proposal_id": proposal["proposal_id"],
                "expected_proposal_revision": proposal["revision"], "expected_job_revision": pending["revision"],
                "idempotency_key": f"api-process-{index}"}
            await call("POST", prefix + "/resume", body={**resume, "extra": True}, status=422)
            await call("POST", prefix + "/resume", body=resume, status=409)
            approved = await call("POST", "/api/approvals/" + resume["approval_id"] + "/approve")
            with monkeypatch.context() as private_reads:
                private_reads.setattr(service.repository_source_service, "_read_private_artifact", forbid_private)
                approved_state = await call("GET", prefix)
            assert approved_state["recovery_action"] == "execute_approved_patch"
            assert approved_state["approval"]["status"] == "approved"
            assert approved_state["patch_proposal"] == proposal
            assert approved_state["revision"] == resume["expected_job_revision"]
            assert approved["status"] == "approved"
            await call("POST", prefix + "/resume", body={**resume, "expected_proposal_revision": proposal["revision"] + 1}, status=409)
            if readback_drift:
                sandbox = service.repository_source_service.sandbox
                original_read = sandbox._read_private_output
                def changed_output(directory, name):
                    raw = original_read(directory, name)
                    if name == "supervisor-result.json":
                        result = json.loads(raw)
                        result["tested_file_hash_metadata"][0]["sha256"] = "f" * 64
                        return json.dumps(result).encode()
                    return raw
                monkeypatch.setattr(sandbox, "_read_private_output", changed_output)
                await call("POST", prefix + "/resume", body=resume, status=503)
                with monkeypatch.context() as private_reads:
                    private_reads.setattr(service.repository_source_service, "_read_private_artifact", forbid_private)
                    unknown = await call("GET", prefix)
                assert unknown["status"] == "unknown_external_effect"
                assert unknown["recovery_action"] == "reconcile_original_repository"
                assert unknown["provider_contacted"] is True
                assert root_id in service.repository_source_service._iterative_lanes
                assert (await jobs.get_job(binding.invocation_id))["status"] == "running"
                await call("POST", prefix + "/resume", body=resume, status=409)
                assert len(contacted) == 1
                (accounting_db[0].parent / "api-literal-captures-r101.json").write_text(json.dumps({
                    "language": language, "readback_drift": True, "captures": captures,
                    "repository_root": await jobs.get_job(root_id),
                    "original_child": await jobs.get_job(binding.invocation_id),
                    "physical_source_unchanged": (accounting_db[0] / "example" / source_path).read_bytes() == original_bytes,
                }, indent=2, default=str) + "\n")
                return
            executed = await call("POST", prefix + "/resume", body=resume)
            assert executed["status"] == ("succeeded" if index == 3 else "failed")
            assert executed["cleanup_proven"] is True
            current_child = await jobs.get_job(binding.invocation_id)
            assert current_child["attempt_count"] == 1
            assert current_child["lease"]["fencing_token"] == initial_child["lease"]["fencing_token"]
            if index < 3:
                for key in ("owner", "expires_at"):
                    assert current_child["lease"][key] == initial_child["lease"][key]
                assert executed["repository_review"]["iteration_index"] == index + 1
            with monkeypatch.context() as private_reads:
                private_reads.setattr(service.repository_source_service, "_read_private_artifact", forbid_private)
                terminal = await call("GET", prefix)
            assert terminal["status"] == ("succeeded" if index == 3 else "running")
            assert len(terminal["iterations"]) == index
            for iteration in terminal["iterations"]:
                assert set(iteration) == {"index", "input_tree_digest", "patch_digest", "command_refs", "result_artifacts"}
                assert iteration["command_refs"] and len(iteration["result_artifacts"]) == 2
            for state in terminal["iteration_states"]:
                assert state["command_results_status"] == "recorded"
                assert state["command_results"] == [{"check": "test",
                    "status": "succeeded" if state["index"] == 3 else "failed",
                    "exit_code": 0 if state["index"] == 3 else 1}]
        assert (accounting_db[0] / "example" / source_path).read_bytes() == original_bytes
        root_final = await jobs.get_job(root_id)
        assert root_final["attempt_count"] == root_initial["attempt_count"] == 1
        assert root_final["lease"]["fencing_token"] == root_initial["lease"]["fencing_token"]
        assert root_id not in service.repository_source_service._iterative_lanes

    from src.workflows.general_task_guard import read_manifest
    from src.work_board.dispatcher import WorkBoardDispatcher
    async with factory() as db:
        parent = await jobs._fetch(db, binding.parent_job_id)
        manifest = read_manifest(parent)
        costs = list((await db.scalars(select(InferenceCostReservation))).all())
        cost_capture = [{"operation_id": row.operation_id, "job_id": row.job_id,
            "state": row.state, "actual_cost_microusd": row.actual_cost_microusd} for row in costs]
    assert len(costs) == 3 and all(row.state == "settled" for row in costs)
    assert len({row.operation_id for row in costs}) == 3
    resumed_parent = await jobs.resume_general_task_native_parent(binding.parent_job_id,
        owner="actual-api-parent-assembly", expected_revision=parent.revision,
        expected_manifest_revision=manifest.manifest_revision)
    parent_runtime = resumed_parent["job"]
    outcome = await service.execute(jobs, job_id=binding.parent_job_id,
        owner=parent_runtime["lease"]["owner"], fence=parent_runtime["lease"]["fencing_token"],
        envelope=None, principal=operator.principal)
    dispatcher = WorkBoardDispatcher(session_provider=factory.accounting_sessions, general_tasks=service)
    dispatcher.jobs = jobs
    await dispatcher._settle_parent(binding.parent_job_id, parent_runtime["lease"]["owner"],
        parent_runtime["lease"]["fencing_token"], outcome)
    parent_final = await jobs.get_job(binding.parent_job_id)
    assert parent_final["status"] == "succeeded"
    (accounting_db[0].parent / "api-literal-captures-r101.json").write_text(json.dumps({
        "language": language, "captures": captures, "costs": cost_capture,
        "original_child": await jobs.get_job(binding.invocation_id), "repository_root": root_final,
        "original_parent": parent_final, "physical_source_unchanged": True,
    }, indent=2, default=str) + "\n")
