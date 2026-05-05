from unittest.mock import patch

import pytest

from src.db.models import MemoryEdgeType, MemoryKind
from src.memory.hybrid_retrieval import retrieve_hybrid_memory
from src.memory.repository import memory_repository
from src.memory.retrieval_planner import plan_memory_retrieval


@pytest.mark.asyncio
async def test_memory_correction_creates_receipt_and_suppresses_corrected_memory(client):
    stale = await memory_repository.create_memory(
        content="Atlas launch is delayed.",
        kind=MemoryKind.project,
        summary="Atlas launch delayed",
        confidence=0.55,
        importance=0.55,
    )

    response = await client.post(
        "/api/memory/corrections",
        json={
            "content": "Atlas launch is on track.",
            "kind": "project",
            "summary": "Atlas launch on track",
            "corrects_memory_id": stale.memory_id,
            "reason": "Operator corrected the project status.",
            "privacy_boundary": "source_bound",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    corrected = await memory_repository.get_memory(stale.memory_id)
    edges = await memory_repository.list_edges(from_memory_id=payload["memory"]["id"])
    audit = await client.get("/api/memory/audit", params={"memory_id": payload["memory"]["id"]})

    assert payload["receipt"]["action"] == "correct"
    assert payload["receipt"]["changed_decision"] is True
    assert payload["receipt"]["privacy_boundary"] == "source_bound"
    assert payload["memory"]["provenance"]["kind"] == "operator_correction"
    assert corrected is not None
    assert corrected.status.value == "superseded"
    assert {edge.edge_type for edge in edges} == {MemoryEdgeType.supersedes, MemoryEdgeType.contradicts}
    assert audit.json()["events"][0]["event_type"] == "memory_corrected"

    with patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)):
        retrieval = await retrieve_hybrid_memory(
            query="Atlas launch status",
            active_projects=("Atlas",),
            limit=4,
        )

    assert "Atlas launch on track" in retrieval.context
    assert "Atlas launch delayed" not in retrieval.context


@pytest.mark.asyncio
async def test_memory_pin_and_forget_are_audited_and_change_active_recall(client):
    created = await memory_repository.create_memory(
        content="User prefers detailed implementation receipts.",
        kind=MemoryKind.communication_preference,
        summary="Prefers detailed implementation receipts",
        confidence=0.4,
        importance=0.4,
    )

    pinned_response = await client.post(
        f"/api/memory/{created.memory_id}/pin",
        json={
            "reason": "Keep this preference prominent.",
            "privacy_boundary": "sensitive",
        },
    )
    assert pinned_response.status_code == 200
    pinned = await memory_repository.get_memory(created.memory_id)
    assert pinned is not None
    assert pinned.confidence == pytest.approx(1.0)
    assert pinned.importance == pytest.approx(1.0)
    assert pinned_response.json()["receipt"]["action"] == "pin"
    assert pinned_response.json()["memory"]["operator_control"]["pinned"] is True

    forgotten_response = await client.post(
        f"/api/memory/{created.memory_id}/forget",
        json={
            "reason": "Operator removed this preference.",
            "mode": "archive",
        },
    )
    assert forgotten_response.status_code == 200
    forgotten = await memory_repository.get_memory(created.memory_id)
    assert forgotten is not None
    assert forgotten.status.value == "archived"
    assert forgotten_response.json()["receipt"]["suppression_state"] == "archived_status"

    with patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)):
        retrieval = await retrieve_hybrid_memory(
            query="implementation receipts",
            active_projects=(),
            limit=4,
        )

    audit = await client.get("/api/memory/audit", params={"memory_id": created.memory_id})
    event_types = [event["event_type"] for event in audit.json()["events"]]

    assert retrieval.context == ""
    assert event_types[:2] == ["memory_forgotten", "memory_pinned"]


@pytest.mark.asyncio
async def test_memory_redaction_and_audit_receipts_are_queryable_without_leaking_content(client):
    created = await memory_repository.create_memory(
        content="The deployment token is seraph-secret-token.",
        kind=MemoryKind.fact,
        summary="Deployment token is stored in memory",
        confidence=0.8,
        importance=0.8,
    )

    redacted_response = await client.post(
        f"/api/memory/{created.memory_id}/forget",
        json={
            "reason": "Operator requested secret redaction.",
            "mode": "redact",
            "privacy_boundary": "sensitive",
        },
    )
    audit_response = await client.post(
        f"/api/memory/{created.memory_id}/audit",
        json={"reason": "Verify redaction receipt remains visible."},
    )
    receipts_response = await client.get("/api/memory/audit", params={"memory_id": created.memory_id})

    redacted = await memory_repository.get_memory(created.memory_id)
    event_types = [event["event_type"] for event in receipts_response.json()["events"]]

    assert redacted_response.status_code == 200
    assert audit_response.status_code == 200
    assert receipts_response.status_code == 200
    assert redacted is not None
    assert redacted.content == "[forgotten by operator]"
    assert redacted.summary == "[forgotten by operator]"
    assert "seraph-secret-token" not in redacted_response.text
    assert "seraph-secret-token" not in audit_response.text
    assert redacted_response.json()["receipt"]["privacy_boundary"] == "sensitive"
    assert audit_response.json()["receipt"]["action"] == "audit"
    assert audit_response.json()["receipt"]["changed_memory"] is False
    assert event_types[:2] == ["memory_audited", "memory_forgotten"]


@pytest.mark.asyncio
async def test_memory_retrieval_decision_receipt_reports_suppression_and_capability_choice(async_db):
    await memory_repository.create_memory(
        content="Atlas launch is delayed.",
        kind=MemoryKind.project,
        summary="Atlas launch delayed",
        confidence=0.55,
        importance=0.55,
    )
    await memory_repository.create_memory(
        content="Atlas launch is on track.",
        kind=MemoryKind.project,
        summary="Atlas launch on track",
        confidence=0.95,
        importance=0.95,
    )

    with patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)):
        retrieval = await plan_memory_retrieval(
            query="Atlas launch status",
            active_projects=("Atlas",),
        )

    receipt = retrieval.decision_receipt

    assert receipt["receipt_type"] == "memory_decision"
    assert receipt["changed_decision"] is True
    assert receipt["capability_choice"]["canonical_guardian_memory"] is True
    assert receipt["capability_choice"]["lane"] == "hybrid"
    assert receipt["suppression"]["lower_ranked_contradiction_count"] == 1
    assert "lower_ranked_contradiction" in receipt["suppression"]["reasons"]
    assert "Atlas launch on track" in retrieval.semantic_context
    assert "Atlas launch delayed" not in retrieval.semantic_context
