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
