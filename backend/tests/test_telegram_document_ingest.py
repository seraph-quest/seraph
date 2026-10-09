"""Genuine selected event and private bytes; only final Telegram HTTP scripted."""
import hashlib
import json

import httpx
import pytest
from sqlalchemy import select

from src.db.models import WorkBoardInputArtifact, TelegramInboundUpdate
from src.extensions.telegram_document_transport import TelegramDocumentHTTP
from src.work_board import document_pairs as sources
from src.work_board.channel_capture import TelegramDocumentAcquisitionSelection
from src.work_board.contracts import WorkBoardOwner
from src.work_board.document_channel_ingest import (
    acquire_original_document, resolve_original_document, reserve_channel_source, validate_file_path,
)
from src.work_board.documents import DocumentService, DocumentReadInput, projection
from src.work_board.repository import BoardError
from tests.test_channel_telegram_capture import selected_pair
from tests.test_first_result_setup import authenticated_setup_operator, setup_workspace
from tests.test_general_documents import fixture_bytes

pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize("async_db", ["file"], indirect=True)]

ACTION = {"action": "acquire_one_original_task_document", "max_sources": 1,
    "source_cap_bytes": 16777216, "docx_cap_bytes": 10485760,
    "formats": ["pdf", "docx", "xlsx", "csv"], "no_learning": True}


async def prepare(client, monkeypatch, async_db, fmt="csv", *, action=True, file_path="documents/file_1.csv", body=None):
    from src.native_tools.registry import ToolRegistry
    registry = ToolRegistry(); registry.start()
    adapter, boundary, chosen, task_service = await selected_pair(client, monkeypatch, registry)
    goal = await client.post("/api/goals", json={"title": "Acquire selected private document",
        "admission_budget": {"reviewed_grant": True, "grant_id": "original-document-local",
            "max_outstanding_jobs": 1, "max_attempts": 2, "max_runtime_seconds": 70}})
    assert goal.status_code == 200, goal.text
    selected = await client.put("/api/telegram/capture-selection", json={
        "expected_revision": chosen["state_revision"], "enabled": True,
        "goal_id": goal.json()["id"], "goal_revision": goal.json()["revision"],
        "requested_output": {"type": "object"}, "limits": {"max_inference_calls": 0, "max_cost_microusd": 0},
        "inference_egress_acknowledged": False, "document_acquisition": ACTION if action else None})
    assert selected.status_code == 200, selected.text
    raw = fixture_bytes(fmt)
    contacts = []
    async def final_http(request):
        contacts.append(request.method)
        async with async_db() as db:
            row = (await db.scalars(select(WorkBoardInputArtifact).where(
                WorkBoardInputArtifact.document_reserved_bytes > 0))).one()
            value = sources.metadata(row)
            assert row.document_reserved_bytes == 32*1024*1024
            assert row.metadata_digest is None and value["live_writer"]
            assert value["upload_binding"]["binding"]["schema"] == "telegram-document-upload-lease.v1"
            assert "source_digest" not in value["upload_binding"]["binding"]
        if request.method == "POST":
            assert json.loads(request.content) == {"file_id": "actual_downloadable_id"}
            return httpx.Response(200, json={"ok": True, "result": {"file_path": file_path}})
        assert request.url.host == "api.telegram.org"
        assert request.url.path.endswith("/" + file_path)
        return httpx.Response(200, content=raw if body is None else body)
    adapter.document_http = TelegramDocumentHTTP(transport=httpx.MockTransport(final_http))
    service = DocumentService(); await service.start()
    adapter.document_service = service
    media = {"csv": "text/csv", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}[fmt]
    event = {"update_id": 51, "message": {"message_id": 151, "from": {"id": 42}, "chat": {"id": 77},
        "caption": "/task Keep this original document in private quarantine",
        "document": {"file_id": "actual_downloadable_id", "file_unique_id": "not_downloadable",
            "file_size": len(raw), "file_name": "private." + fmt, "mime_type": media}}}
    owner = WorkBoardOwner(principal_id=selected.json()["capture_binding"]["selection"]["owner_principal_id"]
        if action else selected.json()["capture_binding"]["owner_principal_id"],
        session_id=selected.json()["capture_binding"]["selection"]["original_root_id"]
        if action else selected.json()["capture_binding"]["original_root_id"])
    return adapter, boundary, service, event, owner, raw, contacts


@pytest.mark.parametrize("fmt", ["csv", "docx"])
async def test_actual_original_event_quota_before_http_physical_first_seal_and_explicit_local_read(client, async_db, setup_workspace, monkeypatch, fmt):
    adapter, boundary, service, event, owner, raw, contacts = await prepare(client, monkeypatch, async_db, fmt)
    try:
        receipt = await adapter._ingest_update(event, owner_principal_id=owner.principal_id, operator_session_id=owner.session_id)
        assert receipt["channel_task_capture"]["document_acquisition"]["provider_file_id"] == "actual_downloadable_id"
        event_key = receipt["idempotency_key"]
        async with async_db() as db:
            witness = await resolve_original_document(db, owner, event_key)
            row, value = await reserve_channel_source(db, owner, witness)
            pending = projection(row)
            assert pending["source_digest"] is None and pending["typed_input_ref"] is None
            deadline = pending["ingest_deadline"]
            with pytest.raises(BoardError):
                await sources.complete(db, owner, row.artifact_id, row.revision, capability=sources.SOURCE_CAPABILITY)
        linked = await client.post("/api/telegram/updates", json=event)
        assert linked.status_code == 200, linked.text
        capture_task_id = linked.json()["channel_task_capture"]["task_id"]
        assert "provider_file_id" not in linked.text and "actual_downloadable_id" not in linked.text
        async with async_db() as db:
            stored_event = await db.scalar(select(TelegramInboundUpdate).where(TelegramInboundUpdate.idempotency_key == event_key))
            original_receipt = json.loads(stored_event.receipt_json)
            result = original_receipt["channel_task_capture"]["document_source"]
            assert original_receipt["channel_task_capture"]["reservation"] == receipt["channel_task_capture"]["reservation"]
        assert contacts == ["POST", "GET"]
        async with async_db() as db:
            row = await db.get(WorkBoardInputArtifact, result["artifact_id"])
            value = sources.metadata(row)
            assert value["phase"] == "sealed" and row.metadata_digest
            assert value["channel_ingest"]["task_id"] == capture_task_id and result["task_id"] == capture_task_id
            assert value["input"]["source"] == {"size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            assert value["ingest_deadline"] == deadline
            cipher_path = sources.source_path(row, value, "source")
            assert cipher_path.read_bytes() != raw and raw not in cipher_path.read_bytes()
            assert sources.read_private(cipher_path, value["sources"]["source"], maximum=len(raw)) == raw
            evidence = await service.read(db, owner, DocumentReadInput(artifact_ref="document-source:" + row.artifact_id, format=fmt))
            assert evidence["status"] == "succeeded" and evidence["evidence"]["no_learning"]
            assert evidence["evidence"]["source_digest"] == hashlib.sha256(raw).hexdigest()
            current = await db.get(WorkBoardInputArtifact, row.artifact_id, populate_existing=True)
            leaf = next((cell for section in evidence["evidence"]["sections"] for cell in section["table_cells"]), evidence["evidence"]["sections"][0])
            preparation_body = {"artifact_ref": "document-source:" + current.artifact_id,
                "expected_source_revision": current.revision, "citation_refs": [leaf["source_ref"]],
                "acknowledge_local_use": True, "idempotency_key": "separate-explicit-local-preparation"}
        from src.api import work_board, documents as documents_api
        monkeypatch.setattr(documents_api, "get_session", async_db)
        from src.work_board.dispatcher import WorkBoardDispatcher, _parse_typed_input
        dispatcher = WorkBoardDispatcher(session_provider=async_db, general_tasks=work_board.dispatcher.general_tasks)
        monkeypatch.setattr(work_board, "dispatcher", dispatcher)
        prepared = await client.post("/api/documents/preparations", json=preparation_body)
        assert prepared.status_code == 200, prepared.text
        preparation_task = prepared.json()["task"]
        assert preparation_task["task_id"] != capture_task_id
        promoted = await client.post(f"/api/work-board/tasks/{preparation_task['task_id']}/actions", json={
            "action": "promote", "expected_revision": preparation_task["task_revision"]})
        assert promoted.status_code == 200, promoted.text
        outcome = await dispatcher.run_pass()
        assert outcome["completed"] == 1, outcome
        prepared_view = await client.get(f"/api/documents/preparations/{preparation_task['task_id']}")
        assert prepared_view.status_code == 200, prepared_view.text
        assert prepared_view.json()["sections"][0]["source_ref"] == leaf["source_ref"]
        assert prepared_view.json()["no_learning"] and prepared_view.json()["provider_contacts"] == 0
        from src.db.models import WorkBoardTask, InferenceCostReservation
        async with async_db() as db:
            captured = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == capture_task_id))
            assert _parse_typed_input(captured)["task_input"].get("document_source") is None
            assert not (await db.scalars(select(InferenceCostReservation))).all()
        replay = await client.post("/api/telegram/updates", json=event)
        assert replay.status_code == 200 and replay.json()["channel_task_capture"]["task_id"] == capture_task_id, replay.text
        same = await acquire_original_document(adapter, owner, event_key)
        assert same["artifact_id"] == result["artifact_id"] and contacts == ["POST", "GET"]
        async with async_db() as db:
            source = await db.get(WorkBoardInputArtifact, result["artifact_id"])
            assert source.bound_task_id is None
            source_revision = source.revision
            task_rows = (await db.scalars(select(WorkBoardTask))).all()
            tasks_before = {task.task_id: task.model_dump(mode="json") for task in task_rows}
            copied_inputs = {}
            for task in task_rows:
                artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id)
                copied_inputs[artifact.artifact_id] = sources._payload_path(artifact).read_bytes()
        retired = await client.delete(f"/api/documents/sources/{result['artifact_id']}", params={"expected_revision": source_revision})
        assert retired.status_code == 200, retired.text
        async with async_db() as db:
            source = await db.get(WorkBoardInputArtifact, result["artifact_id"])
            assert source.bound_task_id is None and sources.metadata(source)["channel_ingest"]["task_id"] == capture_task_id
            assert source.document_reserved_bytes == 0 and source.state == "deleted"
            assert {task.task_id: task.model_dump(mode="json") for task in (await db.scalars(select(WorkBoardTask))).all()} == tasks_before
            for identifier, original_bytes in copied_inputs.items():
                assert sources._payload_path(await db.get(WorkBoardInputArtifact, identifier)).read_bytes() == original_bytes
            assert not (await db.scalars(select(InferenceCostReservation))).all()
        denied_replay = await client.post("/api/telegram/updates", json=event)
        assert denied_replay.status_code == 409 and contacts == ["POST", "GET"], denied_replay.text
        denied_preparation_view = await client.get(f"/api/documents/preparations/{preparation_task['task_id']}")
        assert denied_preparation_view.status_code == 409, denied_preparation_view.text
        denied_original_acceptance = await client.post(f"/api/work-board/tasks/{capture_task_id}/actions", json={
            "action": "promote", "expected_revision": tasks_before[capture_task_id]["task_revision"]})
        assert denied_original_acceptance.status_code == 409, denied_original_acceptance.text
        assert denied_original_acceptance.json()["detail"]["code"] == "channel_document_source_unsealed"
        assert contacts == ["POST", "GET"]
    finally:
        await service.stop(); await boundary.http.aclose(); adapter.document_service = None


@pytest.mark.parametrize("shape", ["short", "excess", "path"])
async def test_unknown_never_seals_or_retries_and_exact_restart_cleanup_retains_then_releases(client, async_db, setup_workspace, monkeypatch, shape):
    raw = fixture_bytes("csv")
    adapter, boundary, service, event, owner, _raw, contacts = await prepare(client, monkeypatch, async_db,
        file_path="../secret" if shape == "path" else "documents/file_1.csv",
        body=raw[:-1] if shape == "short" else raw+b"x" if shape == "excess" else None)
    try:
        receipt = await adapter._ingest_update(event, owner_principal_id=owner.principal_id, operator_session_id=owner.session_id)
        with pytest.raises(BoardError):
            await acquire_original_document(adapter, owner, receipt["idempotency_key"])
        original_contacts = list(contacts)
        with pytest.raises(BoardError):
            await acquire_original_document(adapter, owner, receipt["idempotency_key"])
        assert contacts == original_contacts
        async with async_db() as db:
            row = (await db.scalars(select(WorkBoardInputArtifact))).one()
            assert row.metadata_digest is None and row.document_reserved_bytes == 32*1024*1024
            assert sources.metadata(row)["live_writer"] and sources.metadata(row)["channel_ingest"]["contact"] == "unknown"
            identifier, revision = row.artifact_id, row.revision
        await service.stop(); await service.start()
        async with async_db() as db:
            reconciled = await sources.reconcile_upload(db, owner, identifier, revision)
        async with async_db() as db:
            deleted = await sources.reset_unbound(db, owner, identifier, reconciled["revision"], retry=False, capability=sources.SOURCE_CAPABILITY)
        assert deleted["quota_reserved_bytes"] == 0 and contacts == original_contacts
    finally:
        await service.stop(); await boundary.http.aclose()


@pytest.mark.parametrize("path", ["/file", "a/", "a//b", ".", "..", "a/../b", "a%2fb", "a?b", "a#b", "https://x/a", "a\\b", "å", "a\0b", "a/" + "x"*129])
async def test_closed_provider_path_grammar_rejects_before_secret_url(path):
    with pytest.raises(BoardError) as denied:
        validate_file_path(path)
    assert denied.value.code == "document_path_rejected"


async def test_selection_rejects_boolean_integer_and_changed_caps():
    from pydantic import ValidationError
    for changes in ({"max_sources": True}, {"source_cap_bytes": 1}, {"formats": ["csv"]}, {"no_learning": 1}):
        with pytest.raises(ValidationError):
            TelegramDocumentAcquisitionSelection.model_validate({**ACTION, **changes})


@pytest.mark.parametrize("changed", ["root", "goal", "file_id", "event", "source"])
async def test_changed_actual_authority_denies_before_provider_contact(client, async_db, setup_workspace, monkeypatch, changed):
    from src.db.models import OperatorSession, Goal, Message
    from src.work_board.document_pairs import now
    adapter, boundary, service, event, owner, raw, contacts = await prepare(client, monkeypatch, async_db)
    try:
        receipt = await adapter._ingest_update(event, owner_principal_id=owner.principal_id, operator_session_id=owner.session_id)
        async with async_db() as db:
            witness = await resolve_original_document(db, owner, receipt["idempotency_key"])
            row, value = await reserve_channel_source(db, owner, witness)
            identifier = row.artifact_id
            if changed == "root":
                (await db.get(OperatorSession, owner.session_id)).revoked_at = now()
            elif changed == "goal":
                (await db.get(Goal, witness.binding.goal_id)).revision += 1
            elif changed == "file_id":
                message = await db.get(Message, witness.binding.canonical_message_id)
                metadata = json.loads(message.metadata_json)
                metadata["telegram"]["document"]["file_id"] = "different_actual_file"
                message.metadata_json = json.dumps(metadata)
            elif changed == "event":
                actual = await db.scalar(select(TelegramInboundUpdate).where(TelegramInboundUpdate.idempotency_key == receipt["idempotency_key"]))
                actual.request_digest = "0"*64
            else:
                row.goal_revision += 1
            await db.commit()
        with pytest.raises(BoardError):
            await acquire_original_document(adapter, owner, receipt["idempotency_key"])
        assert contacts == []
        async with async_db() as db:
            retained = await db.get(WorkBoardInputArtifact, identifier)
            assert retained.metadata_digest is None and retained.document_reserved_bytes == 32*1024*1024
    finally:
        await service.stop(); await boundary.http.aclose()


async def test_revocation_after_getfile_denies_bytes_and_retains_unknown_charge(client, async_db, setup_workspace, monkeypatch):
    from src.db.models import OperatorSession
    from src.work_board.document_pairs import now
    adapter, boundary, service, event, owner, raw, contacts = await prepare(client, monkeypatch, async_db)
    async def final_http(request):
        contacts.append(request.method)
        assert request.method == "POST"
        async with async_db() as db:
            (await db.get(OperatorSession, owner.session_id)).revoked_at = now()
            await db.commit()
        return httpx.Response(200, json={"ok": True, "result": {"file_path": "documents/file.csv"}})
    adapter.document_http = TelegramDocumentHTTP(transport=httpx.MockTransport(final_http))
    try:
        receipt = await adapter._ingest_update(event, owner_principal_id=owner.principal_id, operator_session_id=owner.session_id)
        with pytest.raises(BoardError):
            await acquire_original_document(adapter, owner, receipt["idempotency_key"])
        assert contacts == ["POST"]
        async with async_db() as db:
            row = (await db.scalars(select(WorkBoardInputArtifact))).one()
            value = sources.metadata(row)
            assert row.metadata_digest is None and row.document_reserved_bytes == 32*1024*1024
            assert value["live_writer"] and value["channel_ingest"]["contact"] == "unknown"
    finally:
        await service.stop(); await boundary.http.aclose()


async def test_document_action_absent_never_acquires(client, async_db, setup_workspace, monkeypatch):
    adapter, boundary, service, event, owner, raw, contacts = await prepare(client, monkeypatch, async_db, action=False)
    try:
        receipt = await adapter._ingest_update(event, owner_principal_id=owner.principal_id, operator_session_id=owner.session_id)
        with pytest.raises(BoardError):
            await acquire_original_document(adapter, owner, receipt["idempotency_key"])
        assert contacts == []
        async with async_db() as db:
            assert not (await db.scalars(select(WorkBoardInputArtifact))).all()
    finally:
        await service.stop(); await boundary.http.aclose()


async def test_consumed_document_selection_never_acquires_second_event(client, async_db, setup_workspace, monkeypatch):
    adapter, boundary, service, event, owner, raw, contacts = await prepare(client, monkeypatch, async_db)
    try:
        first = await adapter._ingest_update(event, owner_principal_id=owner.principal_id, operator_session_id=owner.session_id)
        second_event = json.loads(json.dumps(event))
        second_event["update_id"] += 1; second_event["message"]["message_id"] += 1
        second = await adapter._ingest_update(second_event, owner_principal_id=owner.principal_id, operator_session_id=owner.session_id)
        assert "document_acquisition" not in second["channel_task_capture"]
        with pytest.raises(BoardError):
            await acquire_original_document(adapter, owner, second["idempotency_key"])
        await acquire_original_document(adapter, owner, first["idempotency_key"])
        assert contacts == ["POST", "GET"]
    finally:
        await service.stop(); await boundary.http.aclose()


@pytest.mark.parametrize("physical", ["held", "replaced", "fifo"])
async def test_original_kernel_lease_physical_mismatch_denies_before_contact(client, async_db, setup_workspace, monkeypatch, physical):
    import os
    import uuid
    from src.work_board.document_channel_ingest import stage_channel_lease
    adapter, boundary, service, event, owner, raw, contacts = await prepare(client, monkeypatch, async_db)
    descriptor = -1
    try:
        receipt = await adapter._ingest_update(event, owner_principal_id=owner.principal_id, operator_session_id=owner.session_id)
        async with async_db() as db:
            witness = await resolve_original_document(db, owner, receipt["idempotency_key"])
            row, value = await reserve_channel_source(db, owner, witness)
        descriptor, _binding = stage_channel_lease(row, value, uuid.uuid4().hex, service._upload_profile)
        lease_path = sources.source_path(row, value, "source").parent / "g1-source.upload-lock"
        if physical != "held":
            os.close(descriptor); descriptor = -1
            lease_path.unlink()
            if physical == "fifo":
                os.mkfifo(lease_path, 0o600)
            else:
                lease_path.write_bytes(b"unrelated private inode")
                lease_path.chmod(0o600)
        with pytest.raises(BoardError) as rejected:
            await acquire_original_document(adapter, owner, receipt["idempotency_key"])
        assert rejected.value.code == "document_upload_lock_unavailable" and contacts == []
        async with async_db() as db:
            retained = await db.get(WorkBoardInputArtifact, row.artifact_id)
            assert retained.document_reserved_bytes == 32*1024*1024 and retained.metadata_digest is None
    finally:
        if descriptor >= 0: os.close(descriptor)
        await service.stop(); await boundary.http.aclose()


async def test_actual_http_close_failure_cannot_issue_observed_source_or_first_seal(client, async_db, setup_workspace, monkeypatch):
    adapter, boundary, service, event, owner, raw, contacts = await prepare(client, monkeypatch, async_db)
    closed = []
    class OriginalStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield raw
        async def aclose(self):
            closed.append(True)
            raise OSError("scripted final boundary closure failure")
    async def final_http(request):
        contacts.append(request.method)
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "documents/file.csv"}})
        return httpx.Response(200, stream=OriginalStream())
    adapter.document_http = TelegramDocumentHTTP(transport=httpx.MockTransport(final_http))
    try:
        receipt = await adapter._ingest_update(event, owner_principal_id=owner.principal_id, operator_session_id=owner.session_id)
        with pytest.raises(BoardError):
            await acquire_original_document(adapter, owner, receipt["idempotency_key"])
        assert closed and contacts == ["POST", "GET"]
        with pytest.raises(BoardError):
            await acquire_original_document(adapter, owner, receipt["idempotency_key"])
        assert contacts == ["POST", "GET"]
        async with async_db() as db:
            row = (await db.scalars(select(WorkBoardInputArtifact))).one()
            value = sources.metadata(row)
            assert row.metadata_digest is None and row.document_reserved_bytes == 32*1024*1024
            assert value["sources"] == {} and value["channel_ingest"]["publication"] is None
            assert value["channel_ingest"]["contact"] == "unknown"
    finally:
        await service.stop(); await boundary.http.aclose()


async def test_copy_of_private_original_witness_cannot_reserve(client, async_db, setup_workspace, monkeypatch):
    from dataclasses import replace
    adapter, boundary, service, event, owner, raw, contacts = await prepare(client, monkeypatch, async_db)
    try:
        receipt = await adapter._ingest_update(event, owner_principal_id=owner.principal_id, operator_session_id=owner.session_id)
        async with async_db() as db:
            genuine = await resolve_original_document(db, owner, receipt["idempotency_key"])
            with pytest.raises(BoardError):
                await reserve_channel_source(db, owner, replace(genuine))
        assert contacts == []
        async with async_db() as db:
            assert not (await db.scalars(select(WorkBoardInputArtifact))).all()
    finally:
        await service.stop(); await boundary.http.aclose()


async def ready_source(client, async_db, monkeypatch):
    from src.api import documents as documents_api
    monkeypatch.setattr(documents_api, "get_session", async_db)
    adapter, boundary, service, event, owner, raw, contacts = await prepare(client, monkeypatch, async_db)
    response = await client.post("/api/telegram/updates", json=event)
    assert response.status_code == 200, response.text
    async with async_db() as db:
        original = await db.scalar(select(TelegramInboundUpdate).where(TelegramInboundUpdate.update_id == 51))
        identifier = json.loads(original.receipt_json)["channel_task_capture"]["document_source"]["artifact_id"]
        evidence = await service.read(db, owner, DocumentReadInput(artifact_ref="document-source:"+identifier, format="csv"))
        assert evidence["status"] == "succeeded"
    return adapter, boundary, service, event, owner, identifier, evidence, contacts


async def test_actual_original_read_claim_delete_race_nested_citations_and_tombstone_denial(client, async_db, setup_workspace, monkeypatch):
    from dataclasses import replace
    from src.work_board.contracts import DocumentTaskBinding
    from src.work_board.document_channel_ingest import channel_source_read_claim
    from src.work_board.document_preparation import resolve
    from src.work_board.general_task import digest
    adapter, boundary, service, event, owner, identifier, evidence, contacts = await ready_source(client, async_db, monkeypatch)
    try:
        leaf = evidence["evidence"]["sections"][0]["table_cells"][0]
        async with async_db() as db:
            row = await db.get(WorkBoardInputArtifact, identifier)
            value, original_revision = sources.metadata(row), row.revision
            binding = DocumentTaskBinding(artifact_ref="document-source:"+identifier, source_revision=row.revision,
                metadata_digest=row.metadata_digest, citation_refs=[leaf["source_ref"]],
                selection_digest=digest([leaf["source_ref"]]), acknowledge_local_use=True)
            async with channel_source_read_claim(owner, row, value) as claim:
                denied = await client.delete(f"/api/documents/sources/{identifier}", params={"expected_revision": original_revision})
                assert denied.status_code == 409, denied.text
                denied_citations = await client.get(f"/api/documents/sources/{identifier}/citations")
                assert denied_citations.status_code == 409, denied_citations.text
                selected, view = await resolve(db, owner, binding, _source_claim=claim)
                assert selected.artifact_id == identifier and view[0]["source_ref"] == leaf["source_ref"]
                with pytest.raises(BoardError):
                    await resolve(db, owner, binding, _source_claim=replace(claim))
            retained = await db.get(WorkBoardInputArtifact, identifier, populate_existing=True)
            assert retained.revision == original_revision and retained.document_reserved_bytes == 32*1024*1024
        citations = await client.get(f"/api/documents/sources/{identifier}/citations")
        assert citations.status_code == 200 and citations.json()["citations"], citations.text
        retired = await client.delete(f"/api/documents/sources/{identifier}", params={"expected_revision": original_revision})
        assert retired.status_code == 200 and retired.json()["quota_reserved_bytes"] == 0, retired.text
        assert not list(sources.source_path(row, value, "source").parent.iterdir())
        assert (await client.get(f"/api/documents/sources/{identifier}/citations")).status_code == 409
        async with async_db() as db:
            with pytest.raises(BoardError):
                await resolve(db, owner, binding)
            with pytest.raises(BoardError):
                await service.read(db, owner, DocumentReadInput(artifact_ref="document-source:"+identifier, format="csv"))
        assert contacts == ["POST", "GET"]
    finally:
        await service.stop(); await boundary.http.aclose()


async def test_metadata_only_source_uses_actual_lexical_root_frame(client, async_db, setup_workspace, monkeypatch):
    import asyncio
    from src.work_board.channel_capture import staged_captured_source_identity, _staged_source_root_for_sql
    from src.work_board.contracts import DocumentTaskBinding
    from src.work_board.document_preparation import resolve
    from src.work_board.general_task import digest
    from src.work_board.input_artifacts import _begin_immediate
    adapter, boundary, service, event, owner, identifier, evidence, contacts = await ready_source(client, async_db, monkeypatch)
    try:
        leaf = evidence["evidence"]["sections"][0]["table_cells"][0]["source_ref"]
        async with async_db() as db:
            row = await db.get(WorkBoardInputArtifact, identifier)
            binding = DocumentTaskBinding(artifact_ref="document-source:"+identifier, source_revision=row.revision,
                metadata_digest=row.metadata_digest, citation_refs=[leaf],
                selection_digest=digest([leaf]), acknowledge_local_use=True)
            with pytest.raises(BoardError):
                await resolve(db, owner, binding, metadata_only=True)
            await db.rollback()
            with staged_captured_source_identity():
                root = _staged_source_root_for_sql()
                with pytest.raises(TypeError):
                    root["inode"] = 0
                async def foreign_task():
                    with pytest.raises(BoardError):
                        _staged_source_root_for_sql()
                await asyncio.create_task(foreign_task())
                await _begin_immediate(db)
                selected, view = await resolve(db, owner, binding, metadata_only=True)
                assert selected.artifact_id == identifier and view is None
                await db.rollback()
            with pytest.raises(BoardError):
                _staged_source_root_for_sql()
        assert contacts == ["POST", "GET"]
    finally:
        await service.stop(); await boundary.http.aclose()


@pytest.mark.parametrize("damage", ["bound", "foreign", "both_links", "replaced_witness", "missing_inventory", "partial_unlink"])
async def test_retirement_unknown_physical_or_generic_owner_retains_charge(client, async_db, setup_workspace, monkeypatch, damage):
    import os
    adapter, boundary, service, event, owner, identifier, evidence, contacts = await ready_source(client, async_db, monkeypatch)
    try:
        async with async_db() as db:
            row = await db.get(WorkBoardInputArtifact, identifier)
            value = sources.metadata(row)
            revision = row.revision
            if damage == "bound":
                row.bound_task_id = "unexpected_generic_owner"
                await db.commit()
            elif damage == "missing_inventory":
                value["channel_ingest"].pop("readers")
                from src.work_board.document_channel_ingest import cleanup_channel_files
                with pytest.raises(OSError, match="original reader inventory unavailable"):
                    cleanup_channel_files(row, value)
                row.document_metadata_json = sources.canonical(value).decode()
                await db.commit()
        directory = sources.source_path(row, value, "source").parent
        if damage == "foreign":
            (directory/"foreign-private-fragment").write_bytes(b"foreign")
        elif damage == "both_links":
            os.link(sources.source_path(row, value, "source"), directory/value["channel_ingest"]["publication"]["source"]["temporary"])
        elif damage == "replaced_witness":
            path = directory/value["channel_ingest"]["readers"][0]["witness"]["file"]
            raw = path.read_bytes(); path.unlink(); path.write_bytes(raw); path.chmod(0o600)
        elif damage == "partial_unlink":
            sources.source_path(row, value, "source").write_bytes(b"wrong original source ciphertext")
        denied = await client.delete(f"/api/documents/sources/{identifier}", params={"expected_revision": revision})
        assert denied.status_code == 409, denied.text
        async with async_db() as db:
            retained = await db.get(WorkBoardInputArtifact, identifier)
            assert retained.document_reserved_bytes == 32*1024*1024 and retained.state != "deleted"
            assert sources.metadata(retained)["channel_ingest"]["task_id"] == value["channel_ingest"]["task_id"]
            if damage == "partial_unlink":
                assert sources.metadata(retained)["phase"] == "cleanup_tombstone"
                assert not sources._payload_path(row).exists()
        assert contacts == ["POST", "GET"]
    finally:
        await service.stop(); await boundary.http.aclose()


async def test_final_source_release_cas_denies_unexpected_nonnull_generic_owner(client, async_db, setup_workspace, monkeypatch):
    from src.work_board.document_channel_ingest import _retirement_cas
    from src.work_board.input_artifacts import _begin_immediate
    adapter, boundary, service, event, owner, identifier, evidence, contacts = await ready_source(client, async_db, monkeypatch)
    try:
        async with async_db() as db:
            row = await db.get(WorkBoardInputArtifact, identifier)
            value = sources.metadata(row)
            directory = sources.source_path(row, value, "source").parent
            (directory/"unknown-fragment").write_bytes(b"hold original charge")
            revision = row.revision
        denied = await client.delete(f"/api/documents/sources/{identifier}", params={"expected_revision": revision})
        assert denied.status_code == 409
        async with async_db() as db:
            row = await db.get(WorkBoardInputArtifact, identifier)
            assert sources.metadata(row)["phase"] == "cleanup_tombstone"
            row.bound_task_id = "unexpected_late_generic_owner"
            await db.commit()
            await _begin_immediate(db)
            row = await db.get(WorkBoardInputArtifact, identifier, populate_existing=True)
            attempted = sources.metadata(row)
            attempted["phase"] = "deleted"; attempted["sources"] = {}
            with pytest.raises(BoardError):
                await _retirement_cas(db, row, attempted, release=True)
            await db.rollback()
        async with async_db() as db:
            retained = await db.get(WorkBoardInputArtifact, identifier)
            assert retained.bound_task_id == "unexpected_late_generic_owner" and retained.document_reserved_bytes == 32*1024*1024
            assert retained.state != "deleted" and sources.metadata(retained)["phase"] == "cleanup_tombstone"
        assert contacts == ["POST", "GET"]
    finally:
        await service.stop(); await boundary.http.aclose()
