import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
import pytest

from config.settings import settings
from src.api import memory as memory_api
from src.auth.service import test_bypass_operator
from src.db.models import MemoryEdgeType, MemoryKind
from src.memory.hybrid_retrieval import retrieve_hybrid_memory
from src.memory.repository import memory_repository
from src.memory.retrieval_planner import plan_memory_retrieval
from src.security.trust_contract import PrincipalType


def test_memory_correction_route_binds_test_bypass_principal_without_database():
    operator = test_bypass_operator()
    request = SimpleNamespace(state=SimpleNamespace(operator=operator))

    async def capture(**kwargs):
        return kwargs

    async def invoke_route():
        with patch.object(memory_api, "correct_memory", capture):
            return await memory_api.create_memory_correction(
                request,
                memory_api.MemoryCorrectionRequest(
                    content="Route-binding unit test memory.",
                    actor="attacker",
                ),
            )

    receipt = asyncio.run(invoke_route())

    assert receipt["actor"] == "operator:test-bypass"


def test_memory_actor_rejects_non_operator_or_mismatched_session():
    operator = test_bypass_operator()
    request = SimpleNamespace(state=SimpleNamespace(operator=operator))

    for principal in (
        replace(operator.principal, principal_type=PrincipalType.SERVICE),
        replace(operator.principal, session_id="other-session"),
    ):
        forged_request = SimpleNamespace(
            state=SimpleNamespace(operator=replace(operator, principal=principal))
        )
        with pytest.raises(HTTPException) as raised:
            memory_api.authenticated_memory_actor(forged_request)
        assert raised.value.status_code == 401

    assert memory_api.authenticated_memory_actor(request) == "operator:test-bypass"


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
async def test_memory_correction_rejects_unknown_privacy_boundary_and_trusted_metadata_override(client):
    rejected = await client.post(
        "/api/memory/corrections",
        json={
            "content": "Operator-owned memory must fail closed.",
            "kind": "fact",
            "privacy_boundary": "public_everywhere",
        },
    )
    assert rejected.status_code == 400
    assert "unknown privacy_boundary" in rejected.json()["detail"]

    accepted = await client.post(
        "/api/memory/corrections",
        json={
            "content": "Operator-owned memory keeps trusted provenance.",
            "kind": "fact",
            "privacy_boundary": "source_bound",
            "metadata": {
                "provenance": {"kind": "caller_claimed_provider"},
                "operator_control": {"last_action": "caller_override"},
                "privacy_boundary": "private",
                "safe_note": "caller metadata that is allowed to remain",
            },
        },
    )

    assert accepted.status_code == 200
    memory = accepted.json()["memory"]
    assert memory["privacy_boundary"] == "source_bound"
    assert memory["provenance"]["kind"] == "operator_correction"
    assert memory["operator_control"]["last_action"] == "correction"
    assert memory["metadata"]["safe_note"] == "caller metadata that is allowed to remain"


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


@pytest.mark.asyncio
async def test_memory_live_controls_snapshot_and_review_action_emit_real_receipts(client):
    created = await memory_repository.create_memory(
        content="Guardian should prefer concise checkpoint updates.",
        kind=MemoryKind.communication_preference,
        summary="Prefers concise checkpoints",
        source_session_id="session-live-controls",
        confidence=0.45,
        importance=0.5,
    )
    other_session_memory = await memory_repository.create_memory(
        content="Another session private live-control memory.",
        kind=MemoryKind.fact,
        summary="Other private memory",
        source_session_id="session-other",
        confidence=0.75,
        importance=0.75,
        metadata={"privacy_boundary": "private"},
    )

    snapshot_response = await client.get("/api/memory/live-controls")
    assert snapshot_response.status_code == 200
    snapshot = snapshot_response.json()
    operator_snapshot_response = await client.get(
        "/api/operator/guardian-memory-live-control",
        params={"owner_session_id": "session-live-controls"},
    )
    assert operator_snapshot_response.status_code == 200
    operator_snapshot = operator_snapshot_response.json()
    assert operator_snapshot["summary"]["memory_candidate_count"] == 1
    assert operator_snapshot["memory_candidates"][0]["id"] == created.memory_id
    assert other_session_memory.memory_id not in {
        item["id"] for item in operator_snapshot["memory_candidates"]
    }
    assert "Another session private live-control memory" not in operator_snapshot_response.text
    assert snapshot["summary"]["operator_status"] == "guardian_memory_live_controls_visible"
    assert snapshot["summary"]["memory_candidate_count"] >= 1
    assert "guardian_or_memory_superiority" in snapshot["blocked_claims"]
    assert any(
        item["id"] == created.memory_id
        for item in snapshot["learning_memory_candidates"]["active"]
    )

    missing_ack = await client.post(
        "/api/memory/live-controls/actions",
        json={
            "action": "review_outcome",
            "memory_id": created.memory_id,
            "outcome": "accepted",
        },
    )
    assert missing_ack.status_code == 400
    assert "explicit acknowledgement" in missing_ack.json()["detail"]

    accepted = await client.post(
        "/api/memory/live-controls/actions",
        json={
            "action": "review_outcome",
            "acknowledged": True,
            "memory_id": created.memory_id,
            "outcome": "accepted",
            "reason": "Operator confirmed the live learning candidate.",
            "privacy_boundary": "operator_visible",
        },
    )

    updated = await memory_repository.get_memory(created.memory_id)
    audit = await client.get("/api/memory/audit", params={"memory_id": created.memory_id})

    assert accepted.status_code == 200
    payload = accepted.json()
    assert payload["receipt"]["action"] == "review_outcome"
    assert payload["receipt"]["acknowledged"] is True
    assert payload["receipt"]["changed_memory"] is True
    assert payload["memory"]["operator_control"]["review_outcome"] == "accepted"
    assert updated is not None
    assert updated.confidence == pytest.approx(1.0)
    assert audit.json()["events"][0]["event_type"] == "memory_live_control_review_outcome"


@pytest.mark.asyncio
async def test_memory_live_controls_decay_and_delete_export_are_bounded_operator_actions(client):
    created = await memory_repository.create_memory(
        content="The legacy export token is seraph-delete-me.",
        kind=MemoryKind.fact,
        summary="Legacy export token",
        confidence=0.8,
        importance=0.8,
    )

    decay = await client.post(
        "/api/memory/live-controls/actions",
        json={
            "action": "decay_stale_evidence",
            "acknowledged": True,
            "memory_id": created.memory_id,
            "reason": "Run live stale evidence reconciliation.",
        },
    )
    assert decay.status_code == 200
    assert decay.json()["receipt"]["decay"]["target_scope"] == "memory"
    assert decay.json()["receipt"]["decay"]["memory_id"] == created.memory_id
    assert decay.json()["receipt"]["decay"]["global_decay_ran"] is False

    exported = await client.post(
        "/api/operator/memory-live-controls/actions",
        json={
            "action": "propagate_delete_export",
            "acknowledged": True,
            "memory_id": created.memory_id,
            "reason": "Delete/export request propagated from live controls.",
            "privacy_boundary": "sensitive",
        },
    )
    stored = await memory_repository.get_memory(created.memory_id)

    assert exported.status_code == 200
    assert stored is not None
    assert stored.status.value == "archived"
    assert "seraph-delete-me" not in exported.text
    assert exported.json()["receipt"]["blocked_claims"]
    assert exported.json()["receipt"]["privacy_boundary"] == "sensitive"


@pytest.mark.asyncio
async def test_memory_live_controls_rollback_requires_specific_boundary_acknowledgement(client):
    created = await memory_repository.create_memory(
        content="Rollback candidate memory.",
        kind=MemoryKind.fact,
        summary="Rollback candidate",
        confidence=0.2,
        importance=0.2,
    )
    await client.post(
        "/api/memory/live-controls/actions",
        json={
            "action": "review_outcome",
            "acknowledged": True,
            "memory_id": created.memory_id,
            "outcome": "rejected",
            "reason": "Create archived rollback candidate.",
        },
    )

    refused = await client.post(
        "/api/operator/guardian-memory-live-control/actions",
        json={
            "action": "rollback_memory",
            "acknowledged": True,
            "memory_id": created.memory_id,
            "reason": "Plain acknowledgement is not enough for rollback.",
        },
    )
    accepted = await client.post(
        "/api/operator/guardian-memory-live-control/actions",
        json={
            "action": "rollback_memory",
            "acknowledge_rollback_boundary": True,
            "owner_session_id": "session-rollback",
            "memory_id": created.memory_id,
            "reason": "Operator acknowledged rollback boundary.",
        },
    )

    assert refused.status_code == 400
    assert "explicit acknowledgement" in refused.json()["detail"]
    assert accepted.status_code == 200
    assert accepted.json()["receipt"]["owner_session_id"] == "session-rollback"
    assert accepted.json()["memory"]["status"] == "active"


@pytest.mark.asyncio
async def test_memory_live_controls_quarantine_and_reinstate_provider_runtime_state(client):
    provider_inventory = {
        "providers": [
            {
                "name": "graph-memory",
                "provider_kind": "graph",
                "enabled": True,
                "configured": True,
                "runtime_state": "ready",
                "notes": [],
            }
        ],
        "summary": {"provider_count": 1, "ready_count": 1},
    }
    with patch("src.memory.control.list_memory_provider_inventory", return_value=provider_inventory):
        quarantined = await client.post(
            "/api/memory/live-controls/actions",
            json={
                "action": "quarantine_provider",
                "acknowledged": True,
                "provider_name": "graph-memory",
                "reason": "Provider returned stale advisory evidence.",
            },
        )
        assert quarantined.status_code == 200
        providers = quarantined.json()["snapshot"]["provider_states"]["providers"]
        assert providers[0]["runtime_state"] == "quarantined"
        assert providers[0]["runtime_state_before_quarantine"] == "ready"

        reinstated = await client.post(
            "/api/memory/live-controls/actions",
            json={
                "action": "reinstate_provider",
                "acknowledged": True,
                "provider_name": "graph-memory",
                "reason": "Operator completed provider review.",
            },
        )
        assert reinstated.status_code == 200
        providers = reinstated.json()["snapshot"]["provider_states"]["providers"]
        assert providers[0]["runtime_state"] == "ready"


@pytest.mark.asyncio
async def test_memory_mutation_routes_bind_authenticated_principal_and_ignore_body_actor(client):
    """Every canonical memory mutation and compatibility alias uses middleware identity."""

    principal_id = "operator:test-bypass"
    created = await memory_repository.create_memory(
        content="Principal-bound memory control candidate.",
        kind=MemoryKind.fact,
        summary="Principal-bound candidate",
        confidence=0.6,
        importance=0.6,
    )

    correction = await client.post(
        "/api/memory/corrections",
        json={
            "content": "Principal-bound corrected memory.",
            "kind": "fact",
            "corrects_memory_id": created.memory_id,
            "actor": "attacker",
            "reason": "Correction actor must come from the authenticated operator.",
        },
    )
    assert correction.status_code == 200
    corrected_id = correction.json()["memory"]["id"]
    assert correction.json()["receipt"]["actor"] == principal_id
    assert correction.json()["memory"]["provenance"]["actor"] == principal_id
    assert correction.json()["memory"]["operator_control"]["last_actor"] == principal_id

    pinned = await client.post(
        f"/api/memory/{corrected_id}/pin",
        json={"actor": "attacker", "reason": "Pin through the canonical route."},
    )
    forgotten = await client.post(
        f"/api/memory/{corrected_id}/forget",
        json={"actor": "attacker", "reason": "Forget through the canonical route."},
    )
    audited = await client.post(
        f"/api/memory/{corrected_id}/audit",
        json={"actor": "attacker", "reason": "Audit through the canonical route."},
    )
    assert pinned.status_code == 200
    assert forgotten.status_code == 200
    assert audited.status_code == 200
    assert all(response.json()["receipt"]["actor"] == principal_id for response in (pinned, forgotten, audited))

    live_control_routes = (
        "/api/memory/live-controls/actions",
        "/api/memory/guardian-memory-live-control/actions",
        "/api/operator/memory-live-controls/actions",
        "/api/operator/guardian-memory-live-control/actions",
    )
    for route in live_control_routes:
        response = await client.post(
            route,
            json={
                "action": "decay_stale_evidence",
                "acknowledged": True,
                "memory_id": corrected_id,
                "actor": "attacker",
                "reason": f"Live-control alias identity check for {route}.",
            },
        )
        assert response.status_code == 200
        assert response.json()["receipt"]["actor"] == principal_id

    legacy_control = await client.post(
        f"/api/operator/memory-control/{corrected_id}",
        json={
            "action": "audit",
            "note": "Legacy operator alias identity check.",
            "actor": "attacker",
        },
    )
    assert legacy_control.status_code == 200
    assert legacy_control.json()["receipt"]["actor"] == principal_id

    audit = await client.get("/api/memory/audit", params={"memory_id": corrected_id, "limit": 50})
    assert audit.status_code == 200
    assert "attacker" not in audit.text
    assert {event["actor"] for event in audit.json()["events"]} == {principal_id}


@pytest.mark.asyncio
async def test_memory_mutations_remain_blocked_when_test_auth_bypass_is_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)

    response = await client.post(
        "/api/memory/corrections",
        json={
            "content": "Unauthenticated correction must not be persisted.",
            "actor": "attacker",
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "auth_not_configured"
