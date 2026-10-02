import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from config.settings import settings
from src.db.models import (
    Goal,
    Memory,
    MemoryEdge,
    MemoryEdgeType,
    MemoryKind,
    MemoryProposal,
    MemoryProposalDecisionEffect,
    MemoryProposalStatus,
    MemorySource,
    MemoryStatus,
    WorkBoardAttempt,
    WorkBoardTask,
)
from src.memory.repository import memory_repository
import src.memory.repository as memory_repository_module


OWNER_SESSION = "test-auth-bypass"
ORIGIN_HEADERS = {"Host": "localhost", "Origin": "http://localhost:3001"}


@pytest.fixture(autouse=True)
def enable_test_operator_bypass(monkeypatch):
    monkeypatch.setattr(settings, "deployment_environment", "test")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", True)
    monkeypatch.setattr(settings, "operator_auth_secret", "")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")


@pytest.mark.asyncio
async def test_owner_filter_applies_before_search_pagination(client):
    first = await memory_repository.create_memory(
        content="literal 100% _ marker \\ needle one",
        summary="needle one",
        kind=MemoryKind.preference,
        source_session_id=OWNER_SESSION,
    )
    second = await memory_repository.create_memory(
        content="needle two",
        summary="needle two",
        kind=MemoryKind.preference,
        source_session_id=OWNER_SESSION,
    )
    await memory_repository.create_memory(
        content="needle belongs to another session",
        summary="needle other",
        source_session_id="different-session",
    )
    await memory_repository.create_memory(
        content="needle is an ownerless legacy row",
        summary="needle legacy",
        source_session_id=None,
    )

    first_page = await client.get(
        "/api/memory/records",
        params={"q": "needle", "limit": 1},
        headers=ORIGIN_HEADERS,
    )
    assert first_page.status_code == 200
    first_payload = first_page.json()
    assert first_payload["total_count"] == 2
    assert len(first_payload["records"]) == 1
    assert first_payload["records"][0]["source_session_id"] == OWNER_SESSION
    assert first_payload["records"][0]["id"] in {first.memory_id, second.memory_id}
    assert first_payload["next_cursor"]

    second_page = await client.get(
        "/api/memory/records",
        params={"q": "needle", "limit": 1, "cursor": first_payload["next_cursor"]},
        headers=ORIGIN_HEADERS,
    )
    assert second_page.status_code == 200
    second_payload = second_page.json()
    assert second_payload["total_count"] == 2
    assert len(second_payload["records"]) == 1
    assert second_payload["records"][0]["id"] != first_payload["records"][0]["id"]
    assert second_payload["records"][0]["source_session_id"] == OWNER_SESSION
    assert second_payload["next_cursor"] is None

    literal = await client.get(
        "/api/memory/records",
        params={"q": "100% _", "limit": 50},
        headers=ORIGIN_HEADERS,
    )
    assert literal.status_code == 200
    assert [record["id"] for record in literal.json()["records"]] == [first.memory_id]


@pytest.mark.asyncio
async def test_ambiguous_legacy_memory_not_exposed(client):
    ownerless = await memory_repository.create_memory(
        content="Ownerless legacy private fact",
        source_session_id=None,
    )
    other = await memory_repository.create_memory(
        content="Other operator private fact",
        source_session_id="other-session",
    )

    listing = await client.get(
        "/api/memory/records",
        params={"q": "private"},
        headers=ORIGIN_HEADERS,
    )
    assert listing.status_code == 200
    assert listing.json()["records"] == []
    assert listing.json()["total_count"] == 0

    ownerless_detail = await client.get(
        f"/api/memory/records/{ownerless.memory_id}", headers=ORIGIN_HEADERS
    )
    other_detail = await client.get(
        f"/api/memory/records/{other.memory_id}", headers=ORIGIN_HEADERS
    )
    assert ownerless_detail.status_code == 404
    assert other_detail.status_code == 404
    assert ownerless_detail.json() == {"detail": {"code": "memory_record_not_found"}}
    assert other_detail.json() == ownerless_detail.json()


@pytest.mark.asyncio
async def test_record_detail_redacts_secret_and_preserves_provenance(client):
    memory = await memory_repository.create_memory(
        content="The private-value must never be shown in the browser.",
        summary="Private value summary",
        source_session_id=OWNER_SESSION,
        source_type="work_board_m5",
        source_message_id="message-1",
        source_snippet="Evidence private-value",
        metadata={
            "privacy_boundary": "operator_private",
            "work_board_provenance": {
                "schema_version": "work_board_provenance.v1",
                "proposal_id": "proposal-123",
                "source_task_id": "task-123",
                "source_attempt_id": "attempt-123",
                "artifact_ref": "artifact-123",
                "evidence_digest": "A" * 64,
                "secret_field": "private-value",
                "owner_principal_id": "operator-secret",
            },
        },
    )
    async with memory_repository_module.get_session() as db:
        db.add(
            Goal(
                id="goal-123",
                title="Memory provenance goal",
                owner_session_id=OWNER_SESSION,
            )
        )
        db.add(
            WorkBoardTask(
                task_id="task-123",
                owner_principal_id="operator:test-bypass",
                owner_session_id=OWNER_SESSION,
                goal_id="goal-123",
                idempotency_key="memory-provenance-task",
            )
        )
        await db.flush()
        db.add(
            WorkBoardAttempt(
                attempt_id="attempt-123",
                task_id="task-123",
            )
        )
        db.add(
            MemoryProposal(
                proposal_id="proposal-123",
                owner_principal_id="operator:test-bypass",
                owner_session_id=OWNER_SESSION,
                source_task_id="task-123",
                source_attempt_id="attempt-123",
                goal_id="goal-123",
                capability_id="workflow.goal-snapshot-to-file",
                evidence_digest="a" * 64,
                artifact_ref="artifact-123",
                accepted_memory_id=memory.memory_id,
                status=MemoryProposalStatus.accepted,
            )
        )

    async def redact(_db, value, *, fail_closed=True):
        return str(value).replace("private-value", "[redacted secret]")

    with patch(
        "src.vault.redaction.redact_secrets_in_text_readonly",
        new=AsyncMock(side_effect=redact),
    ):
        response = await client.get(
            f"/api/memory/records/{memory.memory_id}", headers=ORIGIN_HEADERS
        )

    assert response.status_code == 200
    payload = response.json()
    assert "private-value" not in json.dumps(payload)
    assert payload["content"] == "The [redacted secret] must never be shown in the browser."
    assert payload["redaction_state"] == "available"
    assert payload["safe_provenance"]["proposal_id"] == "proposal-123"
    assert payload["safe_provenance"]["source_task_id"] == "task-123"
    assert payload["safe_provenance"]["evidence_digest"] == "a" * 64
    assert payload["safe_provenance"]["source_types"] == ["work_board_m5"]
    assert "secret_field" not in payload["safe_provenance"]
    assert "owner_principal_id" not in payload["safe_provenance"]
    assert payload["privacy_boundary"] == "operator_private"
    assert payload["sources"][0]["source_session_id"] == OWNER_SESSION
    assert payload["safe_provenance"]["verified_source"] is False
    assert payload["safe_provenance"]["verification_state"] == "unverified"
    assert payload["tombstone_state"] == "none"
    assert payload["links"][0] == {
        "kind": "record",
        "label": "Memory record",
        "id": memory.memory_id,
        "href": f"/api/memory/records/{memory.memory_id}",
    }
    assert payload["links"][1] == {
        "kind": "audit",
        "label": "Audit history",
        "id": memory.memory_id,
        "href": f"/api/memory/audit?memory_id={memory.memory_id}",
    }
    assert payload["audit_links"] == [payload["links"][1]]


@pytest.mark.asyncio
async def test_stale_valid_m5_binding_never_reports_verified(client, monkeypatch):
    """A valid old MAC must not survive content or proposal lifecycle changes."""

    monkeypatch.setattr(settings, "capability_journal_secret", "records-m5-test-key")
    principal_id = "operator:test-bypass"
    task_id = "records-m5-task"
    attempt_id = "records-m5-attempt"
    goal_id = "records-m5-goal"
    proposal_id = "records-m5-proposal"
    capability_id = "workflow.goal-snapshot-to-file"
    source_context_digest = "records-m5-source-context"
    typed_input_digest = "b" * 64
    readback_ref = "readback:records-m5"
    readback_digest = "c" * 64
    artifact_ref = "artifact:records-m5"
    artifact_digest = "d" * 64
    content = "A canonical M5 memory with a current signed source."
    content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    scope = {
        "schema_version": "memory_scope.v1",
        "goal_id": goal_id,
        "goal_revision": 1,
        "source_context_digest": source_context_digest,
        "preferred_capability_id": capability_id,
        "preferred_capability_version": "1",
        "candidate_capability_ids": [capability_id],
    }

    async with memory_repository_module.get_session() as db:
        goal = Goal(id=goal_id, title="M5 record goal", owner_session_id=OWNER_SESSION)
        task = WorkBoardTask(
            task_id=task_id,
            owner_principal_id=principal_id,
            owner_session_id=OWNER_SESSION,
            goal_id=goal_id,
            idempotency_key="records-m5-idempotency",
        )
        attempt = WorkBoardAttempt(attempt_id=attempt_id, task_id=task_id)
        memory = Memory(
            id="records-m5-memory",
            content=content,
            summary="Signed M5 memory",
            kind=MemoryKind.fact,
            status=MemoryStatus.active,
            source_session_id=OWNER_SESSION,
        )
        proposal = MemoryProposal(
            proposal_id=proposal_id,
            owner_principal_id=principal_id,
            owner_session_id=OWNER_SESSION,
            source_task_id=task_id,
            source_attempt_id=attempt_id,
            goal_id=goal_id,
            capability_id=capability_id,
            capability_version="1",
            typed_input_digest=typed_input_digest,
            source_context_digest=source_context_digest,
            readback_ref=readback_ref,
            readback_digest=readback_digest,
            artifact_ref=artifact_ref,
            artifact_digest=artifact_digest,
            goal_revision=1,
            memory_scope_json=json.dumps(scope, sort_keys=True),
            decision_effect=MemoryProposalDecisionEffect.require_operator_confirmation,
            status=MemoryProposalStatus.accepted,
            accepted_memory_id=memory.id,
            accepted_memory_content_digest=content_digest,
            accepted_by_principal_id=principal_id,
            accepted_by_session_id=OWNER_SESSION,
            accepted_at=datetime.now(timezone.utc),
            preview_text=content,
            preview_text_digest=content_digest,
        )
        source_binding = memory_repository_module._m5_verified_source_binding(proposal)
        assert source_binding is not None
        provenance = {
            "schema_version": "work_board_provenance.v1",
            "proposal_id": proposal_id,
            "owner_principal_id": principal_id,
            "owner_session_id": OWNER_SESSION,
            "source_context_digest": source_context_digest,
            "accepted_content_digest": content_digest,
            "memory_scope": scope,
            "decision_effect": MemoryProposalDecisionEffect.require_operator_confirmation.value,
            "lifecycle_state": "active",
            "verified_source_binding": source_binding,
        }
        provenance["selection_binding_key_id"] = (
            memory_repository_module._m5_selection_binding_key_id()
        )
        provenance["selection_binding_mac"] = memory_repository_module._m5_selection_binding_mac(
            proposal_id=proposal_id,
            accepted_content_digest=content_digest,
            owner_principal_id=principal_id,
            owner_session_id=OWNER_SESSION,
            source_context_digest=source_context_digest,
            source_binding=proposal,
            decision_effect=proposal.decision_effect,
            memory_scope=scope,
        )
        memory.metadata_json = json.dumps(
            {"work_board_provenance": provenance},
            sort_keys=True,
        )
        db.add_all([goal, task])
        await db.flush()
        db.add(attempt)
        await db.flush()
        db.add(memory)
        await db.flush()
        db.add(proposal)
        db.add(
            MemorySource(
                memory_id=memory.id,
                source_type="work_board_m5",
                source_session_id=OWNER_SESSION,
                source_message_id=None,
            )
        )

    current = await client.get(
        f"/api/memory/records/{memory.id}", headers=ORIGIN_HEADERS
    )
    assert current.status_code == 200
    assert current.json()["safe_provenance"]["verified_source"] is True
    assert current.json()["safe_provenance"]["verification_state"] == "verified"
    safe_provenance = current.json()["safe_provenance"]
    assert safe_provenance["goal_id"] == goal_id
    assert safe_provenance["goal_revision"] == 1
    assert safe_provenance["readback_ref"] == readback_ref
    assert safe_provenance["readback_digest"] == readback_digest
    assert safe_provenance["typed_input_digest"] == typed_input_digest
    assert safe_provenance["capability_id"] == capability_id
    assert safe_provenance["capability_version"] == "1"
    assert {link["kind"] for link in current.json()["links"]} >= {
        "goal",
        "readback",
    }

    async with memory_repository_module.get_session() as db:
        stored = (await db.execute(select(Memory).where(Memory.id == memory.id))).scalar_one()
        stored.content = "The canonical content changed after signing."
        db.add(stored)

    stale_content = await client.get(
        f"/api/memory/records/{memory.id}", headers=ORIGIN_HEADERS
    )
    assert stale_content.status_code == 200
    assert stale_content.json()["safe_provenance"]["verified_source"] is False
    assert stale_content.json()["safe_provenance"]["verification_state"] == "unverified"

    async with memory_repository_module.get_session() as db:
        stored = (await db.execute(select(Memory).where(Memory.id == memory.id))).scalar_one()
        stored.content = content
        stored_proposal = (
            await db.execute(
                select(MemoryProposal).where(MemoryProposal.proposal_id == proposal_id)
            )
        ).scalar_one()
        stored_proposal.status = MemoryProposalStatus.blocked
        db.add_all([stored, stored_proposal])

    stale_lifecycle = await client.get(
        f"/api/memory/records/{memory.id}", headers=ORIGIN_HEADERS
    )
    assert stale_lifecycle.status_code == 200
    assert stale_lifecycle.json()["safe_provenance"]["verified_source"] is False
    assert stale_lifecycle.json()["safe_provenance"]["verification_state"] == "unverified"


@pytest.mark.asyncio
async def test_foreign_sources_are_filtered_before_count_and_detail_projection(client):
    memory = await memory_repository.create_memory(
        content="Owner-bound source projection",
        summary="Owner source",
        source_session_id=OWNER_SESSION,
    )
    async with memory_repository_module.get_session() as db:
        db.add_all(
            [
                MemorySource(
                    memory_id=memory.memory_id,
                    source_type="owner_source",
                    source_session_id=OWNER_SESSION,
                    source_message_id="owner-message",
                    snippet="Owner source snippet",
                ),
                MemorySource(
                    memory_id=memory.memory_id,
                    source_type="foreign_source",
                    source_session_id="other-session",
                    source_message_id="foreign-message",
                    snippet="foreign-secret-snippet",
                ),
                MemorySource(
                    memory_id=memory.memory_id,
                    source_type="legacy_source",
                    source_session_id=None,
                    source_message_id="ownerless-message",
                    snippet="ownerless-snippet",
                ),
            ]
        )

    listing = await client.get("/api/memory/records", headers=ORIGIN_HEADERS)
    detail = await client.get(
        f"/api/memory/records/{memory.memory_id}", headers=ORIGIN_HEADERS
    )

    assert listing.status_code == 200
    listed = next(row for row in listing.json()["records"] if row["id"] == memory.memory_id)
    assert listed["safe_provenance"]["source_count"] == 2
    assert listed["safe_provenance"]["source_types"] == ["owner_source", "session"]
    assert detail.status_code == 200
    detail_payload = detail.json()
    assert detail_payload["source_state"]["count"] == 2
    assert detail_payload["source_state"]["types"] == ["owner_source", "session"]
    assert "owner-message" in {
        source["source_message_id"] for source in detail_payload["sources"]
    }
    assert "foreign-message" not in json.dumps(detail_payload)
    assert "foreign-secret-snippet" not in json.dumps(detail_payload)


@pytest.mark.asyncio
async def test_source_and_edge_projection_is_bounded_with_truncation_markers(client):
    memory = await memory_repository.create_memory(
        content="Bounded canonical memory",
        summary="Bounded memory",
        source_session_id=OWNER_SESSION,
    )
    related: list[Memory] = []
    async with memory_repository_module.get_session() as db:
        for index in range(55):
            related_memory = Memory(
                content=f"Related memory {index}",
                summary=f"Related {index}",
                source_session_id=OWNER_SESSION,
                kind=MemoryKind.fact,
                status=MemoryStatus.active,
            )
            db.add(related_memory)
            related.append(related_memory)
        await db.flush()
        db.add_all(
            [
                MemorySource(
                    memory_id=memory.memory_id,
                    source_type="bounded_source",
                    source_session_id=OWNER_SESSION,
                    source_message_id=f"bounded-message-{index}",
                    snippet=f"bounded snippet {index}",
                )
                for index in range(55)
            ]
        )
        db.add_all(
            [
                MemoryEdge(
                    from_memory_id=memory.memory_id,
                    to_memory_id=related_memory.id,
                    edge_type=MemoryEdgeType.contradicts,
                )
                for related_memory in related
            ]
        )

    detail = await client.get(
        f"/api/memory/records/{memory.memory_id}", headers=ORIGIN_HEADERS
    )
    assert detail.status_code == 200
    payload = detail.json()
    assert len(payload["sources"]) == 50
    assert payload["source_state"]["count"] == 50
    assert payload["source_state"]["truncated"] is True
    assert payload["safe_provenance"]["sources_truncated"] is True
    assert len(payload["conflict_state"]["edges"]) == 50
    assert payload["conflict_state"]["truncated"] is True


@pytest.mark.asyncio
async def test_history_detail_is_owner_scoped_while_tombstones_remain_hidden(client):
    archived = await memory_repository.create_memory(
        content="Archived history record",
        summary="Archived history",
        source_session_id=OWNER_SESSION,
        status=MemoryStatus.archived,
    )
    superseded = await memory_repository.create_memory(
        content="Superseded history record",
        summary="Superseded history",
        source_session_id=OWNER_SESSION,
        status=MemoryStatus.superseded,
    )

    for created in (archived, superseded):
        response = await client.get(
            f"/api/memory/records/{created.memory_id}", headers=ORIGIN_HEADERS
        )
        assert response.status_code == 200
        assert response.json()["status"] in {"archived", "superseded"}

    archived_listing = await client.get(
        "/api/memory/records",
        params={"status": "archived"},
        headers=ORIGIN_HEADERS,
    )
    superseded_listing = await client.get(
        "/api/memory/records",
        params={"status": "superseded"},
        headers=ORIGIN_HEADERS,
    )
    assert archived.memory_id in {row["id"] for row in archived_listing.json()["records"]}
    assert superseded.memory_id in {row["id"] for row in superseded_listing.json()["records"]}


@pytest.mark.asyncio
async def test_list_returns_real_confirmation_timestamp_and_typed_page_shape(client):
    confirmed_at = datetime.now(timezone.utc) - timedelta(days=2)
    memory = await memory_repository.create_memory(
        content="Confirmed canonical memory",
        summary="Confirmed memory",
        source_session_id=OWNER_SESSION,
        last_confirmed_at=confirmed_at,
        metadata={"privacy_boundary": "operator_private"},
    )
    response = await client.get("/api/memory/records", headers=ORIGIN_HEADERS)
    assert response.status_code == 200
    payload = response.json()
    row = next(item for item in payload["records"] if item["id"] == memory.memory_id)
    assert payload["last_confirmed_at"] == row["last_confirmed_at"]
    assert payload["last_confirmed_at"] != datetime.now(timezone.utc).isoformat()
    assert payload["last_confirmed_at"].startswith(confirmed_at.strftime("%Y-%m-%d"))
    assert isinstance(row["links"], list)
    assert all(
        isinstance(link, dict)
        and isinstance(link.get("kind"), str)
        and isinstance(link.get("label"), str)
        and isinstance(link.get("id"), str)
        for link in row["links"]
    )
    assert row["privacy_boundary"] == "operator_private"


@pytest.mark.asyncio
async def test_large_owner_scope_uses_bounded_count_and_page_scan(client):
    for index in range(75):
        await memory_repository.create_memory(
            content=f"Large owner scope record {index}",
            summary=f"Large scope {index}",
            source_session_id=OWNER_SESSION,
        )

    with patch.object(
        memory_repository_module,
        "_canonical_memory_deletion_marker",
        wraps=memory_repository_module._canonical_memory_deletion_marker,
    ) as marker:
        response = await client.get(
            "/api/memory/records",
            params={"limit": 20},
            headers=ORIGIN_HEADERS,
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_count"] == 75
    assert len(payload["records"]) == 20
    assert payload["next_cursor"]
    assert payload["scan_truncated"] is False
    # The count is SQL-side and the read inspects only the bounded page plus
    # one lookahead row; it must not walk every owner row in Python.
    assert marker.call_count <= 25


@pytest.mark.asyncio
async def test_large_record_text_is_sql_capped_and_marked(client):
    memory = await memory_repository.create_memory(
        content="c" * 70_000,
        summary="s" * 9_000,
        source_session_id=OWNER_SESSION,
    )
    async with memory_repository_module.get_session() as db:
        db.add(
            MemorySource(
                memory_id=memory.memory_id,
                source_type="large-text",
                source_session_id=OWNER_SESSION,
                snippet="n" * 3_000,
            )
        )

    detail = await client.get(
        f"/api/memory/records/{memory.memory_id}", headers=ORIGIN_HEADERS
    )
    listing = await client.get("/api/memory/records", headers=ORIGIN_HEADERS)

    assert detail.status_code == 200
    detail_payload = detail.json()
    assert len(detail_payload["content"]) == 65_536
    assert detail_payload["content_truncated"] is True
    assert len(detail_payload["summary"]) == 8_192
    assert detail_payload["summary_truncated"] is True
    large_source = next(
        source for source in detail_payload["sources"] if source["source_type"] == "large-text"
    )
    assert len(large_source["snippet"]) == 2_048
    assert large_source["snippet_truncated"] is True
    assert detail_payload["source_state"]["snippets_truncated"] is True
    assert listing.status_code == 200
    listed = next(row for row in listing.json()["records"] if row["id"] == memory.memory_id)
    assert len(listed["summary"]) == 8_192
    assert listed["summary_truncated"] is True
    assert "content" not in listed


@pytest.mark.asyncio
async def test_record_read_bounds_are_client_errors(client):
    oversized_limit = await client.get(
        "/api/memory/records", params={"limit": 51}, headers=ORIGIN_HEADERS
    )
    oversized_query = await client.get(
        "/api/memory/records",
        params={"q": "x" * 201},
        headers=ORIGIN_HEADERS,
    )
    malformed_cursor = await client.get(
        "/api/memory/records",
        params={"cursor": "not-a-cursor"},
        headers=ORIGIN_HEADERS,
    )
    assert oversized_limit.status_code == 422
    assert oversized_query.status_code == 422
    assert malformed_cursor.status_code == 400


@pytest.mark.asyncio
async def test_tombstone_cannot_be_revived_by_record_read(client):
    memory = await memory_repository.create_memory(
        content="Terminal delete/export memory",
        summary="Terminal delete/export",
        source_session_id=OWNER_SESSION,
    )
    deleted = await client.post(
        "/api/memory/live-controls/actions",
        json={
            "action": "propagate_delete_export",
            "acknowledged": True,
            "memory_id": memory.memory_id,
            "reason": "Remove this canonical record from the operator library.",
        },
        headers=ORIGIN_HEADERS,
    )
    assert deleted.status_code == 200, deleted.text

    listing = await client.get(
        "/api/memory/records",
        params={"q": "Terminal delete/export"},
        headers=ORIGIN_HEADERS,
    )
    detail = await client.get(
        f"/api/memory/records/{memory.memory_id}", headers=ORIGIN_HEADERS
    )
    stored = await memory_repository.get_memory(memory.memory_id, include_deleted=True)
    tombstone = await memory_repository.get_memory_tombstone(memory.memory_id)

    assert listing.status_code == 200
    assert listing.json()["records"] == []
    assert detail.status_code == 404
    assert stored is not None
    assert stored.status is MemoryStatus.archived
    assert tombstone is not None


@pytest.mark.asyncio
async def test_read_does_not_call_embedding_provider(client):
    memory = await memory_repository.create_memory(
        content="Read-only memory does not need embeddings.",
        summary="No embedding on read",
        source_session_id=OWNER_SESSION,
    )
    with (
        patch("src.memory.embedder.embed", side_effect=AssertionError("embed called")),
        patch("src.memory.embedder.embed_batch", side_effect=AssertionError("embed_batch called")),
    ):
        listing = await client.get("/api/memory/records", headers=ORIGIN_HEADERS)
        detail = await client.get(
            f"/api/memory/records/{memory.memory_id}", headers=ORIGIN_HEADERS
        )

    assert listing.status_code == 200
    assert detail.status_code == 200
