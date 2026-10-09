"""Actual authenticated private spec/row/review/retirement; no provider sockets."""
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from config.settings import settings
from src.db.models import WorkBoardInputArtifact
from tests.test_inference_accounting import accounting_db
from tests.test_general_task_planner import forbid_external_inference
from tests.test_work_board_m6_provider_free_journey import _goal
from src.work_board import document_build_storage as builds
from src.work_board import document_pairs as sources
from src.work_board.contracts import WorkBoardOwner, ToolDescriptor
from src.work_board.repository import BoardError


SPEC = {"kind": "report", "title": "Private result", "sections": [
    {"heading": "Result", "paragraphs": ["Private literal unavailable to generic Task events"], "citation_refs": []}],
    "tables": [], "citations": [], "style_preset": "plain"}


def descriptor():
    # Review primitive test uses a concrete descriptor; native integration uses
    # the stock registry descriptor in its separate original C1 journey.
    return ToolDescriptor(tool_id="document_build", version="1", input_schema={"type": "object"},
        output_schema={"type": "object"}, effects=["owner_private_read", "local_compute", "owner_private_artifact_write"],
        permissions=["capability_execute", "document_local_use", "document_private_artifact_write"],
        deadline=30, verifier="document_build_private_readback.v1", policy_digest="a"*64)


async def setup(accounting_db, monkeypatch):
    from src.auth.service import create_session
    from src.vault import crypto
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "private-build-isolated-password")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "vault_encryption_key", "")
    monkeypatch.setattr(crypto, "_fernet", None)
    token, operator = await create_session()
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    goal = _goal("private-build-goal", "Exact private build goal")
    goal.owner_principal_id, goal.owner_session_id = owner.principal_id, owner.session_id
    async with accounting_db[2].accounting_sessions() as db:
        db.add(goal)
    return token, operator, owner, goal


@pytest.mark.parametrize("case", ["success", "foreign_file", "tampered_file", "goal_drift", "unknown_writer"])
async def test_private_build_encrypted_spec_review_and_retire(accounting_db, monkeypatch, case):
    from src.db.models import Goal
    _token, operator, owner, goal = await setup(accounting_db, monkeypatch)
    original_sessions = accounting_db[2].accounting_sessions
    @asynccontextmanager
    async def sessions():
        from src.work_board.channel_capture import staged_captured_source_identity
        with staged_captured_source_identity():
            async with original_sessions() as db:
                yield db
    request = builds.BuildCreate(goal_id=goal.id, goal_revision=1, spec=SPEC, idempotency_key="one")
    async with sessions() as db:
        created = await builds.create(db, owner, operator, request)
    identifier = created["build_id"]
    assert created["quota_reserved_bytes"] == 24*1024*1024
    assert "Private literal" not in json.dumps(created)
    async with sessions() as db:
        replay = await builds.create(db, owner, operator, request)
        assert replay == created
        row, value = await builds.owned(db, owner, identifier)
        assert "Private literal" not in row.document_metadata_json
        path = sources.source_path(row, value, "spec")
        assert b"Private literal" not in path.read_bytes()
        # Re-open the durable key and row in a fresh reader; content is neither
        # recovered from generic Task JSON nor from a retained plaintext cache.
        from src.vault import crypto
        monkeypatch.setattr(crypto, "_fernet", None)
        preview = await builds.preview(db, owner, operator, identifier, descriptor=descriptor())
        assert preview["spec"] == SPEC
        valid = await builds.verify_review(db, owner, row, value, preview["review"], descriptor=descriptor())
        assert valid == preview["review"]
        altered = json.loads(json.dumps(preview["review"]))
        altered["binding"]["spec_digest"] = "b"*64
        with pytest.raises(BoardError, match="Reload"):
            await builds.verify_review(db, owner, row, value, altered, descriptor=descriptor())
        real_now = builds.now
        with monkeypatch.context() as time_patch:
            time_patch.setattr(builds, "now", lambda: real_now()+timedelta(minutes=6))
            with pytest.raises(BoardError):
                await builds.verify_review(db, owner, row, value, preview["review"], descriptor=descriptor())
        if case == "foreign_file":
            path.with_name("foreign.private").write_bytes(b"unowned")
        elif case == "tampered_file":
            raw = path.read_bytes(); path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
        elif case == "goal_drift":
            current_goal = await db.get(Goal, goal.id); current_goal.revision += 1
        elif case == "unknown_writer":
            value["live_writer"] = {"slot": "renderer", "token": "f"*32}
            value["phase"] = "unknown"; builds.persist(row, value)
            created["revision"] = row.revision
    async with sessions() as db:
        request = builds.BuildRetire(expected_revision=created["revision"], idempotency_key="retire-one")
        if case in {"foreign_file", "tampered_file", "unknown_writer"}:
            with pytest.raises(BoardError):
                await builds.retire(db, owner, operator, identifier, request)
        else:
            result = await builds.retire(db, owner, operator, identifier, request)
            assert result["state"] == "deleted" and result["quota_reserved_bytes"] == 0
            assert await builds.retire(db, owner, operator, identifier, request) == result
            with pytest.raises(BoardError):
                await builds.retire(db, owner, operator, identifier,
                    request.model_copy(update={"idempotency_key": "different-retire"}))
    async with sessions() as db:
        row, value = await builds.owned(db, owner, identifier)
        if case in {"foreign_file", "tampered_file", "unknown_writer"}:
            assert row.document_reserved_bytes == builds.CHARGE
        with pytest.raises(BoardError):
            await builds.preview(db, owner, operator, identifier, descriptor=descriptor())


@pytest.mark.parametrize("case", ["full", "unknown_charge", "unknown_family", "root_revoked", "mac_key_missing"])
async def test_build_quota_and_authority_fail_closed(accounting_db, monkeypatch, case):
    from src.db.models import OperatorSession, WorkBoardInputArtifact
    _token, operator, owner, goal = await setup(accounting_db, monkeypatch)
    sessions = accounting_db[2].accounting_sessions
    async with sessions() as db:
        first = await builds.create(db, owner, operator, builds.BuildCreate(goal_id=goal.id,
            goal_revision=1, spec=SPEC, idempotency_key="one"))
        second = await builds.create(db, owner, operator, builds.BuildCreate(goal_id=goal.id,
            goal_revision=1, spec=SPEC, idempotency_key="two"))
    async with sessions() as db:
        row, value = await builds.owned(db, owner, first["build_id"])
        if case == "root_revoked":
            root = await db.get(OperatorSession, owner.session_id)
            root.revoked_at = datetime.now(timezone.utc)
            await db.flush()
        elif case == "unknown_charge": row.document_reserved_bytes = 1; await db.flush()
        elif case == "unknown_family": row.capability_id = "unknown.document.v1"; await db.flush()
        elif case == "mac_key_missing":
            from src.extensions.capability_execution import CapabilityJournalError
            def missing(): raise CapabilityJournalError("execution journal MAC key unavailable")
            monkeypatch.setattr("src.memory.repository._effect_mac_key", missing)
        if case in {"full", "unknown_charge", "unknown_family"}:
            with pytest.raises(BoardError):
                await builds.create(db, owner, operator, builds.BuildCreate(goal_id=goal.id,
                    goal_revision=1, spec=SPEC, idempotency_key="three"))
        else:
            with pytest.raises(Exception):
                await builds.preview(db, owner, operator, first["build_id"], descriptor=descriptor())


async def test_authenticated_build_api_private_preview_and_owner_switch(accounting_db, monkeypatch):
    from src.api import documents
    from src.auth.service import authenticate_token, create_session
    from types import SimpleNamespace
    token, operator, owner, goal = await setup(accounting_db, monkeypatch)
    monkeypatch.setattr(documents, "get_session", accounting_db[2].accounting_sessions)
    monkeypatch.setattr(documents, "_build_descriptor", descriptor)
    app = FastAPI()
    credentials = {"token": token}
    @app.middleware("http")
    async def auth(request, call_next):
        request.state.operator = await authenticate_token(credentials["token"], touch=False)
        return await call_next(request)
    app.include_router(documents.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        response = await client.post("/api/documents/builds", json={"goal_id": goal.id, "goal_revision": 1,
            "spec": SPEC, "idempotency_key": "api-one"})
        assert response.status_code == 200, response.text
        identifier = response.json()["build_id"]
        retained = await client.get("/api/documents/builds?limit=1")
        assert retained.status_code == 200 and retained.json()["builds"][0]["build_id"] == identifier
        assert "Private literal" not in retained.text and retained.headers["cache-control"] == "no-store"
        preview = await client.get(f"/api/documents/builds/{identifier}/preview")
        assert preview.status_code == 200, preview.text
        assert preview.headers["cache-control"] == "no-store"
        assert preview.json()["spec"] == SPEC
        wrong = await client.post("/api/documents/builds", json={"goal_id": goal.id, "goal_revision": 1,
            "spec": {**SPEC, "path": "/tmp/arbitrary"}, "idempotency_key": "bad"})
        assert wrong.status_code == 422
        assert "Private literal" not in wrong.text and len(wrong.json()["detail"]["errors"]) <= 32
        formula_spec = {**SPEC, "kind": "table_workbook", "tables": [{"sheet_names": ["Results"],
            "cells": [], "formulas": [{"sheet": "Results", "cell": "A1",
                "expression": 'WEBSERVICE("Private expression must not be reflected")'}], "formats": []}]}
        invalid_formula = await client.post("/api/documents/builds", json={"goal_id": goal.id,
            "goal_revision": 1, "spec": formula_spec, "idempotency_key": "bad-formula"})
        assert invalid_formula.status_code == 422, invalid_formula.text
        error = invalid_formula.json()["detail"]["errors"][0]
        assert error["sheet"] == "Results" and error["cell"] == "A1"
        assert error["code"] == "document_formula_function_unsupported"
        assert "Private expression" not in invalid_formula.text
        assert invalid_formula.headers["cache-control"] == "no-store"
        assert len((await client.get("/api/documents/builds")).json()["builds"]) == 1
        oversized = await client.post("/api/documents/builds", content=b" "*81921,
            headers={"content-type": "application/json"})
        assert oversized.status_code == 413
        other_token, _other = await create_session()
        credentials["token"] = other_token
        assert (await client.get("/api/documents/builds")).json()["builds"] == []
        denied = await client.get(f"/api/documents/builds/{identifier}/preview")
        # Roots are distinct even when the authenticated principal is shared.
        assert denied.status_code == 404, denied.text


async def test_actual_adopted_source_selection_and_mixed_family_quota(accounting_db, monkeypatch):
    from tests.test_general_documents import fixture_bytes, read_request
    from src.work_board.documents import DocumentSourceReserve, DocumentService
    _token, operator, owner, goal = await setup(accounting_db, monkeypatch)
    sessions = accounting_db[2].accounting_sessions
    raw = fixture_bytes("csv")
    service = DocumentService()
    await service.start()
    try:
        async with sessions() as db:
            reserved = await sources.reserve(db, owner, DocumentSourceReserve(format="csv",
                source={"size_bytes": len(raw), "sha256": sources.sha256(raw)}, goal_id=goal.id,
                goal_revision=1, idempotency_key="mixed-source", no_learning=True))
            identifier = reserved["artifact_id"]
            async def stream(): yield raw
            uploaded = await sources.upload(db, owner, identifier, reserved["revision"], "source",
                stream(), capability=sources.SOURCE_CAPABILITY, upload_profile=service._upload_profile)
            row, value = await sources.owned(db, owner, identifier, capability=sources.SOURCE_CAPABILITY)
            await sources.complete(db, owner, identifier, row.revision, capability=sources.SOURCE_CAPABILITY)
            parsed = await service.read(db, owner, read_request("csv", identifier), operator=operator)
            assert parsed["status"] == "succeeded" and parsed["cleanup"] == "wait_reaped"
        async with sessions() as db:
            listing = await builds.citations(db, owner, operator, identifier)
            ref = listing["citations"][0]["source_ref"]
            selected = await builds.select_source(db, owner, operator, identifier, builds.BuildSourceSelection(
                expected_revision=listing["source_revision"], citation_refs=[ref], acknowledge_local_use=True))
            assert selected["source"]["citation_refs"] == [ref]
            cited = {**SPEC, "citations": [{"source_ref": ref, "label": "Selected source"}]}
            build = await builds.create(db, owner, operator, builds.BuildCreate(goal_id=goal.id,
                goal_revision=1, spec=cited, source=selected["source"], idempotency_key="mixed-build"))
            rows = list((await db.scalars(select(WorkBoardInputArtifact)
                .where(WorkBoardInputArtifact.document_reserved_bytes > 0))).all())
            assert sum(row.document_reserved_bytes for row in rows) == 56*1024*1024
            preview = await builds.preview(db, owner, operator, build["build_id"], descriptor=descriptor())
            assert preview["selection"] == selected["selection"]
        async with sessions() as db:
            with pytest.raises(BoardError):
                await builds.create(db, owner, operator, builds.BuildCreate(goal_id=goal.id,
                    goal_revision=1, spec=SPEC, idempotency_key="exceeds-64"))
    finally:
        await service.stop()
