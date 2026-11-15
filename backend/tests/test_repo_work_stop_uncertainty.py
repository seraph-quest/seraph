"""Exact original uncertainty successor, real writer rollback and read-only drift."""
import asyncio
import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.workflows.job_runtime import DurableJobLeaseError, _digest
from src.workflows import repo_repair_source as source_module
from src.workflows.repo_repair_stop import stop_repository_root
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_repo_work_stop_metadata import stopped_original, forbid_private_metadata_reads


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["v3", "v4"])
async def test_first_original_stop_intent_persists_selected_snapshot_version(
        accounting_db, monkeypatch, repository_admission_signer, version):
    from src.workflows import repo_repair_stop as stop_owner
    from src.workflows.repo_repair_source_recovery import _repository_recovery_fence
    real_append = source_module._append_repository_record
    sealed = []

    def original_version_seal(run, identity, payload, **kwargs):
        if identity == "repository:inventory:v1":
            # Legacy fixture selection occurs at the authentic first server
            # seal, before any dispatch. Never rewrite a sealed Root, copy an
            # owner, or upgrade an old inventory during Stop.
            assert source_module._repository_record(run, identity) is None
            assert payload["schema"] in {"repository.checkpoint_inventory.v3", "repository.checkpoint_inventory.v4"}
            payload = {**payload, "schema": "repository.checkpoint_inventory." + version}
            sealed.append(run.run_identity)
        return real_append(run, identity, payload, **kwargs)

    monkeypatch.setattr(source_module, "_append_repository_record", original_version_seal)
    factory, owner, service, jobs, binding, root_id, _ = await stopped_original(
        accounting_db, monkeypatch, prepared=True)
    assert sealed == [root_id]
    monkeypatch.setattr(source_module, "_append_repository_record", real_append)
    async with factory() as db:
        root = await jobs._fetch(db, root_id)
        before = root.model_dump(mode="json")
        hold = jobs._repo_repair_reservation_state(root)
        assert source_module.read_repository_inventory(root)["schema"] == "repository.checkpoint_inventory." + version
        assert source_module._repository_record(root, stop_owner.STOP_ID) is None
        assert not any(item["checkpoint_id"].startswith("repository:execution:")
            for item in json.loads(root.checkpoint_receipts_json))
    async with _repository_recovery_fence(service, jobs, job_id=root_id, owner=owner) as fence:
        context = await stop_owner._context(service, jobs, job_id=root_id, owner=owner)
        committed = await stop_owner._persist_repository_stop_intent_locked(service, jobs,
            context=context, owner=owner, reason="operator_cancelled", fence=fence)
        stop_owner.assert_repository_stop_context(committed, service=service, jobs=jobs)
    async with factory() as db:
        root = await jobs._fetch(db, root_id)
        intent = source_module._repository_record(root, stop_owner.STOP_ID)
        assert root.revision == before["revision"] + 1
        assert root.status == before["status"] == "running"
        assert jobs._repo_repair_reservation_state(root) == hold
        assert (root.lease_owner, root.lease_expires_at, root.fencing_token) == (
            before["lease_owner"], context["run"].lease_expires_at, before["fencing_token"])
        schema_version = "v2" if version == "v4" else "v1"
        assert intent["schema"] == "repository.stop_intent." + schema_version
    snapshot = json.loads(service._read_private_artifact(intent["snapshot_artifact_ref"],
        expected_digest=intent["snapshot_artifact_digest"]))
    assert snapshot == {"schema": "repository.stop_snapshot." + schema_version,
        "static_rows": intent["static_rows"], "repository_job_id": root_id,
        "source_checkpoint_digest": source_module._source_digest(committed["original"])}


async def original_and_owners(accounting_db, monkeypatch):
    from src.auth import service as auth_service
    credentials = {}
    create_session = auth_service.create_session
    async def captured_session(*args, **kwargs):
        token, operator = await create_session(*args, **kwargs)
        credentials.update(token=token, operator=operator)
        return token, operator
    monkeypatch.setattr(auth_service, "create_session", captured_session)
    factory, owner, source, jobs, binding, root_id, service = await stopped_original(
        accounting_db, monkeypatch, prepared=True)
    factory.uncertainty_credentials = credentials
    validator = source_module.validate_repository_stop_witness
    async def rollback_terminal(*args, **kwargs):
        await validator(*args, **kwargs)
        raise DurableJobLeaseError("actual terminal writer retained Pending")
    monkeypatch.setattr(source_module, "validate_repository_stop_witness", rollback_terminal)
    async with factory() as db:
        run = await jobs._fetch(db, root_id)
        original = source_module.read_repository_original(run)[0]
        lease_owner, fencing_token = run.lease_owner, run.fencing_token
    iteration_id = source_module.iteration_identity(root_id, original["repository_attempt_id"],
        source_module._source_digest(original["original_input"]), 1)
    async def stop():
        return await stop_repository_root(source, jobs, job_id=root_id, owner=owner,
            general_task_service=service, reason="operator_cancelled")
    async def uncertain():
        return await source_module._quarantine_original_uncertainty(source, jobs, job_id=root_id, owner=owner,
            lease_owner=lease_owner, fencing_token=fencing_token,
            reason="repository_process_closure_unproven", result={"no_learning": True,
                "operator_action": "reconcile_original_process", "iteration_id": iteration_id})
    return factory, owner, source, jobs, root_id, service, stop, uncertain


async def actual_source_response(factory, jobs, service, root_id, monkeypatch):
    import httpx
    from config.settings import settings
    from src.api import workflows as workflows_api, work_board as board_api
    from src.app import create_app
    monkeypatch.setattr(workflows_api, "get_session", factory.accounting_sessions)
    monkeypatch.setattr(workflows_api, "durable_job_repository", jobs)
    monkeypatch.setattr(board_api, "get_session", factory.accounting_sessions)
    monkeypatch.setattr(board_api.dispatcher, "general_tasks", service)
    monkeypatch.setattr(board_api.dispatcher, "jobs", jobs)
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "localhost,test,127.0.0.1")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()),
            base_url="http://localhost", headers={"Origin": "http://localhost:3001"}) as client:
        client.cookies.set(settings.operator_auth_cookie_name, factory.uncertainty_credentials["token"])
        response = await client.get("/api/workflows/repo-repair/" + root_id)
    return response


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["stop", "uncertain"])
async def test_original_uncertainty_and_stop_actual_lock_race(accounting_db, monkeypatch, repository_admission_signer, first):
    factory, owner, source, jobs, root_id, service, stop, uncertain = await original_and_owners(accounting_db, monkeypatch)
    from src.model_fabric.effective_policy import configuration_mutation_lock
    await configuration_mutation_lock.acquire()
    try:
        owners = [stop, uncertain] if first == "stop" else [uncertain, stop]
        tasks = [asyncio.create_task(operation()) for operation in owners]
        await asyncio.sleep(0)
    finally:
        configuration_mutation_lock.release()
    await asyncio.gather(*tasks)
    forbid_private_metadata_reads(monkeypatch, source)
    projection = await source_module.repository_operator_projection(source, jobs, job_id=root_id, owner=owner)
    assert projection["status"] == "unknown_external_effect" and projection["repository_stop"]["pending"] is True
    async with factory() as db:
        run = await jobs._fetch(db, root_id)
        assert jobs._repo_repair_reservation_state(run)["status"] == "held"
        assert (source_module._repository_record(run, "repository:stop-uncertainty-successor:v1") is not None) == (first == "stop")


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["stop_digest", "predecessor_digest", "successor_digest", "fencing_token", "foreign_key"])
async def test_original_successor_tamper_denies_without_private_read(accounting_db, monkeypatch, repository_admission_signer, drift):
    factory, owner, source, jobs, root_id, service, stop, uncertain = await original_and_owners(accounting_db, monkeypatch)
    await stop()
    await uncertain()
    async with factory() as db:
        run = await jobs._fetch(db, root_id)
        journal = json.loads(run.checkpoint_receipts_json)
        record = next(item for item in journal if item["checkpoint_id"] == "repository:stop-uncertainty-successor:v1")
        record["payload"][drift] = "0" * 64
        record["state_digest"] = _digest(record["payload"])
        run.checkpoint_receipts_json = json.dumps(journal)
        await db.commit()
    forbid_private_metadata_reads(monkeypatch, source)
    with pytest.raises(DurableJobLeaseError):
        await source_module.repository_operator_projection(source, jobs, job_id=root_id, owner=owner)
    async with factory() as db:
        run = await jobs._fetch(db, root_id)
        assert run.status == "unknown_external_effect"
        assert jobs._repo_repair_reservation_state(run)["status"] == "held"


@pytest.mark.asyncio
async def test_original_uncertainty_real_sql_rollback_has_no_partial_successor(accounting_db, monkeypatch, repository_admission_signer):
    factory, owner, source, jobs, root_id, service, stop, uncertain = await original_and_owners(accounting_db, monkeypatch)
    await stop()
    async with factory() as db:
        run = await jobs._fetch(db, root_id)
        before = run.model_dump(mode="json")
        await db.execute(text("CREATE TRIGGER reject_original_uncertainty BEFORE UPDATE OF status ON workflow_run_states "
            "WHEN NEW.run_identity = '" + root_id + "' AND NEW.status = 'unknown_external_effect' "
            "BEGIN SELECT RAISE(ABORT, 'disposable original transition rollback'); END"))
        await db.commit()
    with pytest.raises(IntegrityError):
        await uncertain()
    async with factory() as db:
        run = await jobs._fetch(db, root_id)
        assert run.model_dump(mode="json") == before
        assert source_module._repository_record(run, "repository:stop-uncertainty-successor:v1") is None
        assert jobs._repo_repair_reservation_state(run)["status"] == "held"


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["lower_pair", "incremented_pair", "from_only", "to_only",
    "negative_from", "bool_from", "string_to", "current_revision", "priority", "dependencies",
    "artifact_receipts", "effect_receipts", "authority", "fence", "predecessor_lease",
    "predecessor_extra", "predecessor_timestamp", "successor_result"])
async def test_original_projection_drift_actual_authenticated_source_denies(accounting_db, monkeypatch, repository_admission_signer, drift):
    from src.workflows.repo_repair_stop import _static
    factory, owner, source, jobs, root_id, service, stop, uncertain = await original_and_owners(accounting_db, monkeypatch)
    await stop()
    await uncertain()
    async with factory() as db:
        run = await jobs._fetch(db, root_id)
        journal = json.loads(run.checkpoint_receipts_json)
        record = next(item for item in journal if item["checkpoint_id"] == "repository:stop-uncertainty-successor:v1")
        payload = record["payload"]
        if drift in {"lower_pair", "incremented_pair"}:
            delta = -1 if drift == "lower_pair" else 1
            payload["from_revision"] += delta
            payload["to_revision"] += delta
        elif drift == "from_only":
            payload["from_revision"] -= 1
        elif drift == "to_only":
            payload["to_revision"] += 1
        elif drift == "negative_from":
            payload["from_revision"], payload["to_revision"] = -1, 0
        elif drift == "bool_from":
            payload["from_revision"] = True
        elif drift == "string_to":
            payload["to_revision"] = str(payload["to_revision"])
        elif drift == "current_revision":
            run.revision += 1
        elif drift == "predecessor_lease":
            payload["predecessor_projection"]["lease_owner"] = "foreign-original-owner"
        elif drift == "predecessor_extra":
            payload["predecessor_projection"]["priority"] = run.priority
        elif drift == "predecessor_timestamp":
            payload["predecessor_projection"]["lease_expires_at"] = "not-a-time"
        elif drift == "successor_result":
            run.result_digest = "0" * 64
            payload["successor_projection"]["result_digest"] = run.result_digest
        else:
            if drift == "priority":
                run.priority += 1
            elif drift == "dependencies":
                run.dependencies_json = '["foreign"]'
            elif drift == "artifact_receipts":
                run.artifact_receipts_json = '[{"foreign":true}]'
            elif drift == "effect_receipts":
                run.effect_receipts_json = '[{"foreign":true}]'
            elif drift == "authority":
                run.authority_digest = "0" * 64
                payload["authority_digest"] = run.authority_digest
            elif drift == "fence":
                run.fencing_token += 1
                payload["fencing_token"] = run.fencing_token
        binding = source_module.read_repository_original(run)[4]
        payload["successor_digest"] = _static(run, {"run": run, "binding": binding})
        record["state_digest"] = _digest(payload)
        run.checkpoint_receipts_json = json.dumps(journal)
        await db.commit()
    forbid_private_metadata_reads(monkeypatch, source)
    response = await actual_source_response(factory, jobs, service, root_id, monkeypatch)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "repair_recovery_blocked"
    (source._workspace() / ("actual-source-negative-" + drift + ".json")).write_text(json.dumps({
        "method": "GET", "path": "/api/workflows/repo-repair/" + root_id,
        "owner": {"principal_id": owner.principal_id, "session_id": owner.session_id},
        "status": response.status_code, "response": response.json()}, sort_keys=True))
    async with factory() as db:
        run = await jobs._fetch(db, root_id)
        assert run.status == "unknown_external_effect"
        if drift != "fence":
            assert jobs._repo_repair_reservation_state(run)["status"] == "held"
