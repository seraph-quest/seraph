"""Authenticated real repair producer and publication API, provider/account free."""
from pathlib import Path
import json
import uuid

from cryptography.fernet import Fernet
import httpx
import pytest

from config.settings import RepoSandboxSettings, settings
from src.execution.repo_publication import SourceGit
from src.execution.repo_publication_runtime import PROFILE
from src.extensions.github_followthrough import GitHubFollowthroughService
from src.vault.repository import vault_repository
from tests import test_repo_repair_local_vertical as native
from tests.repo_publication_support import GitDataTransport

ORIGIN = {"Origin": "http://localhost:3001"}


async def actual_repair(client, async_db, tmp_path, monkeypatch):
    with native._publication_worker_diagnostic_context(monkeypatch):
        return await _actual_repair(client, async_db, tmp_path, monkeypatch)


async def _actual_repair(client, async_db, tmp_path, monkeypatch):
    async def default_login():
        from src.auth.service import authenticate_session
        response = await client.post("/api/auth/login", json={"password": "native-vertical-auth-secret"}, headers=ORIGIN)
        assert response.status_code == 200, response.text
        token = response.cookies.get(settings.operator_auth_cookie_name)
        assert token
        return token, await authenticate_session(response.json()["session_id"], touch=False)
    monkeypatch.setattr(native, "create_session", default_login)
    monkeypatch.setattr(native, "RepoSandboxSettings", lambda **values: RepoSandboxSettings(profile=PROFILE, **values))
    monkeypatch.setenv("SERAPH_WORKSPACE_LIFECYCLE_PATH", str(tmp_path / "deployment-lifecycle"))
    original_configuration = native._configure_openrouter
    def managed_configuration():
        from src.workspace.production import ProductionWorkspace, prepare_lifecycle_directory
        prepare_lifecycle_directory(ProductionWorkspace(host_root=Path(settings.workspace_dir)))
        persist_proofs = original_configuration()
        async def finish_configuration():
            from src.workflows.job_runtime import durable_job_repository
            await durable_job_repository.configure_inference_accounting(25_000)
            await persist_proofs()
        return finish_configuration
    monkeypatch.setattr(native, "_configure_openrouter", managed_configuration)
    original_transport = native._model_transport
    def costed_fixture_transport(calls):
        transport = original_transport(calls)
        def complete(**values):
            response, body = transport(**values)
            return response, {**body, "id": "fixture-publication-proposal", "usage": {"cost": "0.000001"}}
        return complete
    monkeypatch.setattr(native, "_model_transport", costed_fixture_transport)
    flow = await native._prepare_native_flow(client, async_db, tmp_path, monkeypatch, test_sleep_seconds=0.01)
    job_id, packet = flow["job_id"], flow["packet"]
    response = await client.post(f"/api/workflows/repo-repair/{job_id}/code-egress-consent", json={"expected_job_revision": flow["job"]["revision"], "source_packet_digest": packet.artifact_sha256, "expected_source_manifest_digest": packet.source_manifest_digest, "expected_profile_id": "openrouter", "acknowledged_selected_source": True, "idempotency_key": "publication-native-egress"}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    await flow["dispatcher"].run_pass()
    pending = await client.get(f"/api/workflows/repo-repair/{job_id}")
    assert pending.status_code == 200, pending.text
    view = pending.json(); proposal = view["proposal"]
    assert proposal is not None, view
    approved = await client.post(f"/api/approvals/{proposal['approval_id']}/approve", headers=ORIGIN)
    assert approved.status_code == 200, approved.text
    response = await client.post(f"/api/workflows/repo-repair/{job_id}/resume", json={"expected_job_revision": view["revision"], "expected_proposal_revision": proposal["revision"], "proposal_id": proposal["proposal_id"], "approval_id": proposal["approval_id"], "idempotency_key": "publication-native-resume"}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    await flow["dispatcher"].run_pass()
    final = await client.get(f"/api/workflows/repo-repair/{job_id}")
    assert final.status_code == 200, final.text
    result = final.json()
    assert result["status"] == "succeeded", result
    assert result["execution"]["readback"]["verified"] is True
    flow["repair_view"] = result
    return flow


async def selected_connection(client, flow, monkeypatch):
    monkeypatch.setattr(settings, "vault_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr("src.vault.crypto._fernet", None)
    await vault_repository.store("publication-fixture-token", "fixture-token", owner_principal_id=flow["owner"].principal_id)
    request = {"repository": "acme/example", "vault_key": "publication-fixture-token", "mode": "active", "expected_revision": 0}
    missing = await client.put("/api/capabilities/github/connection", json=request, headers=ORIGIN)
    assert missing.status_code == 422, missing.text
    request["consent"] = {"acknowledged": True, "duration_seconds": 900, "actions": ["github_git_objects_write", "github_new_branch_write", "github_ready_pr_write"]}
    response = await client.put("/api/capabilities/github/connection", json=request, headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert response.json()["consent"]["state"] == "active", response.text
    read = await client.get("/api/capabilities/github/connection")
    assert read.json() == response.json()
    return response.json()


def request_for(flow, connection):
    view = flow["repair_view"]
    return {"repair_job_id": view["job_id"], "expected_repair_revision": view["revision"], "proposal_id": view["proposal"]["proposal_id"], "expected_proposal_revision": view["proposal"]["revision"], "expected_connection_revision": connection["revision"], "base_branch": "main", "expected_base_commit": SourceGit(flow["repository"]).head(), "branch_name": "feat/tested-publication", "commit_message": "Apply approved tested value repair", "title": "Apply tested value repair", "body": "Exact approved test input, published by Seraph", "idempotency_key": str(uuid.uuid4())}


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize("scenario", ["success", "unknown", "goal_changed"])
async def test_authenticated_actual_repair_producer_requires_new_exact_publication_approval(client, async_db, tmp_path, monkeypatch, scenario):
    flow = await actual_repair(client, async_db, tmp_path, monkeypatch)
    connection = await selected_connection(client, flow, monkeypatch)
    transport = GitDataTransport(flow["repository"])
    async def resolver(_host, _port):
        return ["93.184.216.34"]
    adapter = GitHubFollowthroughService(resolver=resolver, transport=httpx.MockTransport(transport.handler))
    monkeypatch.setattr("src.workflows.repo_publication.GitHubFollowthroughService", lambda: adapter)
    request = request_for(flow, connection)
    response = await client.post("/api/capabilities/github/repo-publication/prepare", json=request, headers=ORIGIN)
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["status"] == "awaiting_approval"
    assert view["approval_id"] != flow["repair_view"]["proposal"]["approval_id"]
    assert view["preview"]["required_permissions"] == ["local_host_execution", "github_git_objects_write", "github_new_branch_write", "github_ready_pr_write"]
    assert view["preview"]["tested_input"]["environment"]["profile"] == PROFILE
    assert view["preview"]["repair_binding"]["repair_job_id"] == flow["job_id"]
    assert transport.calls == []
    discovered = await client.get(f"/api/capabilities/github/repo-publication/repairs/{flow['job_id']}/jobs")
    assert discovered.status_code == 200, discovered.text
    assert discovered.json()["jobs"][0]["job_id"] == view["job_id"]
    assert discovered.json()["limit"] == 20 and discovered.json()["next_offset"] is None
    assert transport.calls == []
    repeated = await client.post("/api/capabilities/github/repo-publication/prepare", json=request, headers=ORIGIN)
    assert repeated.status_code == 200 and repeated.json()["job_id"] == view["job_id"]
    execute = await client.post(f"/api/capabilities/github/repo-publication/jobs/{view['job_id']}/execute", headers=ORIGIN)
    assert execute.status_code == 200 and execute.json()["status"] == "awaiting_approval"
    assert transport.calls == []
    proof = tmp_path / "actual-authenticated-publication-preview.json"
    proof.write_text(json.dumps({"repair": flow["repair_view"], "publication": view}, sort_keys=True))
    print("ACTUAL_AUTHENTICATED_PUBLICATION_PREVIEW=" + str(proof))
    approved = await client.post(f"/api/approvals/{view['approval_id']}/approve", headers=ORIGIN)
    assert approved.status_code == 200, approved.text
    transport.fail_after_pr = scenario != "success"
    executed = await client.post(f"/api/capabilities/github/repo-publication/jobs/{view['job_id']}/execute", headers=ORIGIN)
    assert executed.status_code == 200, executed.text
    result = executed.json()
    assert any(item["artifact_type"] == "repo_publication_supervisor_terminal" for item in result["artifacts"])
    (tmp_path / "publication-execution-readback.json").write_text(json.dumps({"publication": result, "transport_calls": transport.calls}, sort_keys=True))
    print("PUBLICATION_EXECUTION_READBACK=" + str(tmp_path / "publication-execution-readback.json"))
    if scenario != "success":
        assert result["status"] == "unknown_external_effect", result
        before = list(transport.calls)
        repeated = await client.post(f"/api/capabilities/github/repo-publication/jobs/{view['job_id']}/execute", headers=ORIGIN)
        assert repeated.json()["status"] == "unknown_external_effect" and transport.calls == before
        # Reopen physical file-backed SQLite connections and replace adapter
        # process-local capture state before recovery. Only canonical rows and
        # private producer receipts may survive this boundary.
        async with async_db() as db:
            physical_engine = db.bind
        await physical_engine.dispose()
        adapter = GitHubFollowthroughService(resolver=resolver, transport=httpx.MockTransport(transport.handler))
        from src.workflows.job_runtime import durable_job_repository
        restarted = await durable_job_repository.get_job(view["job_id"])
        assert restarted["status"] == "unknown_external_effect" and restarted["revision"] == result["revision"]
        print("ACTUAL_FILE_DB_REOPEN_AND_FRESH_ADAPTER=" + view["job_id"])
        stopped = await client.post("/api/capabilities/github/connection/revoke", json={"expected_revision": connection["revision"]}, headers=ORIGIN)
        assert stopped.status_code == 200, stopped.text
        if scenario == "goal_changed":
            from src.db import engine
            from src.db.models import Goal
            from sqlalchemy import update
            async with engine.get_session() as db:
                await db.execute(update(Goal).where(Goal.id == flow["repair_view"]["goal_id"]).values(revision=Goal.revision+1))
        reconciled = await client.post(f"/api/capabilities/github/repo-publication/jobs/{view['job_id']}/reconcile", json={"pr_number": 1, "acknowledged_readback": True, "expected_connection_revision": stopped.json()["revision"]}, headers=ORIGIN)
        assert reconciled.status_code == 200, reconciled.text
        result = reconciled.json()
        assert all(call[0] == "GET" for call in transport.calls[len(before):])
        assert len(transport.pulls) == 1
        if scenario == "goal_changed":
            assert result["status"] == "unknown_external_effect" and result["reason_code"] == "publication_current_goal_changed" and result["observation_only"] is True, result
            assert any(item["artifact_type"] == "github_recovery_observation" for item in result["artifacts"])
            proof = tmp_path / "actual-goal-blocked-publication-observation.json"
            proof.write_text(json.dumps({"publication": result, "transport_calls": transport.calls}, sort_keys=True))
            print("ACTUAL_GOAL_BLOCKED_PUBLICATION_OBSERVATION=" + str(proof))
            current = await durable_job_repository.get_job(view["job_id"])
            from src.extensions.github_capacity_closure import PublicationCloseRequest
            connection_row = await adapter._get_connection_row(flow["owner"].principal_id)
            close_body = {"acknowledged_capacity_close": True,
                "expected_job_revision": current["revision"],
                "expected_connection_revision": connection_row.revision,
                "expected_connection_fence": connection_row.active_fence,
                "idempotency_key": str(uuid.uuid4()), "pr_number": 1}
            from src.db.models import WorkflowRunState
            # Real canonical ledger injections test the inventory boundary;
            # they do not supply actual adapter or producer proof.
            canonical_effects = list(current["effects"])
            for status in ("succeeded", "failed", "intent", "dispatched", "unknown"):
                injected = canonical_effects + [{"effect_id": "possible-unrecognized-contact",
                    "effect_type": "other_external_write", "status": status,
                    "details": {"observation_only": True}}]
                async with engine.get_session() as db:
                    await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity == current["job_id"]).values(effect_receipts_json=json.dumps(injected)))
                injected_before = await durable_job_repository.get_job(current["job_id"])
                no_contact = list(transport.calls)
                rejected = await client.post(f"/api/capabilities/github/repo-publication/jobs/{view['job_id']}/close-capacity", json=close_body, headers=ORIGIN)
                assert rejected.status_code == 409, rejected.text
                assert await durable_job_repository.get_job(current["job_id"]) == injected_before
                assert transport.calls == no_contact
                reserved = await adapter._get_connection_row(flow["owner"].principal_id)
                assert reserved.active_job_id == current["job_id"] and reserved.active_fence == connection_row.active_fence
            async with engine.get_session() as db:
                await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity == current["job_id"]).values(effect_receipts_json=json.dumps(canonical_effects)))
            before_close = list(transport.calls)
            closed = await client.post(f"/api/capabilities/github/repo-publication/jobs/{view['job_id']}/close-capacity", json=close_body, headers=ORIGIN)
            assert closed.status_code == 200, closed.text
            closed_view = closed.json()
            assert closed_view["status"] == "unknown_external_effect"
            assert closed_view["revision"] == current["revision"] + 1
            assert closed_view["github_capacity_closure"]["observation_only"] is True
            assert closed_view["effects"] == current["effects"]
            after_connection = await adapter._get_connection_row(flow["owner"].principal_id)
            assert after_connection.active_job_id is None
            assert after_connection.active_fence == connection_row.active_fence
            assert all(call[0] == "GET" for call in transport.calls[len(before_close):])
            after_close = list(transport.calls)
            await physical_engine.dispose()
            repeated = await client.post(f"/api/capabilities/github/repo-publication/jobs/{view['job_id']}/close-capacity", json=close_body, headers=ORIGIN)
            assert repeated.status_code == 200 and repeated.json()["github_capacity_closure"] == closed_view["github_capacity_closure"], repeated.text
            assert transport.calls == after_close
            observed = await client.post(f"/api/capabilities/github/repo-publication/jobs/{view['job_id']}/reconcile", headers=ORIGIN,
                json={"acknowledged_readback": True, "expected_connection_revision": after_connection.revision, "pr_number": 1})
            assert observed.status_code == 200, observed.text
            assert observed.json()["observation_only"] is True and observed.json()["status"] == "unknown_external_effect"
            assert observed.json()["github_capacity_closure"] == closed_view["github_capacity_closure"]
            assert all(call[0] == "GET" for call in transport.calls[len(after_close):])
            unchanged = await adapter._get_connection_row(flow["owner"].principal_id)
            assert unchanged.model_dump(mode="json") == after_connection.model_dump(mode="json")
            retained = tmp_path / "actual-goal-stale-publication-capacity-close.json"
            retained.write_text(json.dumps({"publication": closed_view, "original_job": current,
                "post_close_observation": observed.json(), "close_request": close_body, "transport_calls": transport.calls}, sort_keys=True))
            print("ACTUAL_GOAL_STALE_PUBLICATION_CAPACITY_CLOSE=" + str(retained))
            return
    assert result["status"] == "succeeded", result
    assert result["result"]["learning"] == "no_learning", result
    assert len(transport.pulls) == 1
    assert result["result"]["remote_rest_commit"] == transport.refs[request["branch_name"]]
    before = list(transport.calls)
    repeated = await client.post(f"/api/capabilities/github/repo-publication/jobs/{view['job_id']}/execute", headers=ORIGIN)
    assert repeated.json()["status"] == "succeeded"
    assert transport.calls == before
    proof = tmp_path / "actual-authenticated-publication-complete.json"
    proof.write_text(json.dumps({"repair": flow["repair_view"], "publication": result, "transport_calls": transport.calls}, sort_keys=True))
    print("ACTUAL_AUTHENTICATED_PUBLICATION_COMPLETE=" + str(proof))
