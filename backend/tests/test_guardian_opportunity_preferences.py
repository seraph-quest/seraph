"""Closed population/signature/archive mechanics, not usefulness/native success proof."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select, delete

from src.guardian import opportunity_preferences as pref
from src.memory import repository as repo
from src.db.models import MemoryProposal, MemoryProposalStatus, MemoryProposalDecisionEffect
from tests.test_memory_local_recovery import local_memory_db, _runtime_operator
from src.auth.service import test_bypass_operator as make_test_bypass_operator


def _scope():
    return {"schema_version": pref.SCOPE_SCHEMA, "owner_principal_id": "operator:scope-test", "owner_session_id": "root",
        "goal_id": "goal", "goal_revision": 1, "action": "prefer_blueprint", "blueprint_id": "public-evidence-report",
        "watch_id": None, "watch_revision": None, "source_context_digest": "1" * 64,
        "generation_cutoff_at": "2026-10-06T12:00:00+00:00", "window_days": 30,
        "population_count": 2, "feedback_event_count": 3,
        "population_members": [{"opportunity_id": "a", "intervention_id": "i-a", "feedback_revision": 1,
            "feedback_event_id": "00000000-0000-0000-0000-000000000001", "feedback_at": "2026-10-06T11:00:00+00:00",
            "feedback_binding_digest": "2" * 64}, {"opportunity_id": "b", "intervention_id": "i-b", "feedback_revision": 2,
            "feedback_event_id": "00000000-0000-0000-0000-000000000002", "feedback_at": "2026-10-06T11:30:00+00:00",
            "feedback_binding_digest": "3" * 64}], "population_digest": "4" * 64, "bundle_digest": "5" * 64}


def _binding():
    return {"owner_principal_id": "operator:scope-test", "owner_session_id": "root", "goal_id": "goal",
        "source_task_id": "actual-cpu-source-id", "source_attempt_id": "cpu-attempt", "capability_id": pref.CAPABILITY_ID,
        "source_context_digest": "1" * 64, "evidence_digest": "e" * 64, "source_evidence_ids": ["cpu-readback"]}


def _mac(scope, key=b"isolated-opportunity-signature-test-key"):
    return repo._m5_selection_binding_mac(proposal_id="proposal", accepted_content_digest="a" * 64,
        owner_principal_id="operator:scope-test", owner_session_id="root", source_context_digest="1" * 64,
        source_binding=_binding(), decision_effect="require_operator_confirmation", memory_scope=scope, _signing_key=key)


@pytest.mark.parametrize("field,value", [("population_digest", "7" * 64), ("bundle_digest", "8" * 64),
    ("feedback_event_count", 4), ("goal_revision", 2), ("generation_cutoff_at", "2026-10-06T12:00:01+00:00")])
def test_population_and_bundle_are_separately_signed(field, value):
    assert _mac(_scope()) != _mac({**_scope(), field: value})


@pytest.mark.parametrize("mutation", [lambda s: s.update(extra=True), lambda s: s.update(goal_revision=True),
    lambda s: s.update(population_count=1), lambda s: s.update(feedback_event_count=101),
    lambda s: s.update(population_members=list(reversed(s["population_members"]))),
    lambda s: s["population_members"][1].update(opportunity_id="a"),
    lambda s: s["population_members"][0].update(feedback_revision=0),
    lambda s: s.update(watch_id="invented-watch"), lambda s: s.update(window_days=31)])
def test_closed_scope_rejects_bad_members_fields_and_counts(mutation):
    value = deepcopy(_scope())
    mutation(value)
    assert repo._m5_selection_scope(value) is None


def test_member_tip_and_whole_history_binding_are_mac_covered():
    altered = deepcopy(_scope())
    altered["population_members"][0]["feedback_binding_digest"] = "b" * 64
    assert _mac(altered) != _mac(_scope())
    assert repo._m5_selection_scope(_scope()) == _scope()


def test_staged_baseline_and_selection_never_load_key(monkeypatch):
    key = b"isolated-opportunity-signature-test-key"
    def forbidden():
        raise AssertionError("key I/O in writer")
    monkeypatch.setattr(repo, "_effect_mac_key", forbidden)
    receipt = {"receipt_id": "receipt", "receipt_stage": "source_baseline", "source_context_digest": "1" * 64}
    receipt["receipt_integrity_mac"] = repo._m5_receipt_integrity_mac(receipt, _signing_key=key)
    assert repo._m5_receipt_integrity_matches(receipt, _signing_key=key)
    receipt["source_context_digest"] = "9" * 64
    assert not repo._m5_receipt_integrity_matches(receipt, _signing_key=key)
    assert len(_mac(_scope(), key)) == 64


@pytest.mark.parametrize("revision", [-1, True, "0", 1.5])
def test_recommendation_feedback_revision_is_strict(revision):
    with pytest.raises(ValidationError):
        pref.OpportunityRecommendationRequest(expected_opportunity_revision=1,
            expected_feedback_revision=revision, idempotency_key=str(uuid4()))


def _population(votes):
    scope = pref.OpportunityPreferenceScope.model_validate(_scope())
    return pref.PopulationWitness("operator:scope-test", "root", "goal", 1, "anchor", 1, 0,
        str(uuid4()), datetime(2026, 10, 6, 12, tzinfo=timezone.utc), datetime(2026, 10, 6, 12, 5, tzinfo=timezone.utc),
        scope.population_members if votes else (), 3 if votes else 0, "4" * 64, b"[]", (), None,
        pref.canonical(votes), None)


def test_zero_revision_empty_population_returns_closed_no_learning_output():
    population = _population([])
    cpu_input = population.cpu_input()
    assert cpu_input.expected_feedback_revision == 0
    output = pref.calculate_recommendation(population, cpu_input)
    assert output.status == "no_learning"
    assert output.action is None
    assert not {"bundle_digest", "source_context_digest", "proposal_id", "selection_binding_mac"} & set(output.model_dump())


def test_distinct_explicit_votes_order_preference_and_conflict_abstains():
    votes = [{"opportunity_id": key, "feedback_type": "helpful", "blueprint_id": "public-evidence-report",
        "watch_id": "watch", "watch_revision": 1} for key in ("a", "b")]
    population = _population(votes)
    output = pref.calculate_recommendation(population, population.cpu_input())
    assert output.action == "prefer_blueprint" and output.blueprint_id == "public-evidence-report"
    negative = {**votes[0], "feedback_type": "not_helpful"}
    veto = replace(population, vote_bytes=pref.canonical([votes[0], negative]))
    assert pref.calculate_recommendation(veto, veto.cpu_input()).status == "no_learning"
    with pytest.raises(Exception, match="complete authorized population"):
        pref.calculate_recommendation(population, population.cpu_input().model_copy(update={"population_digest": "9" * 64}))


def test_display_effect_never_reorders_missing_blueprint_or_suppresses_recovery():
    offers = [{"blueprint_id": "public-browser-check"}, {"blueprint_id": "public-evidence-report"}]
    preference = {"status": "active", "scope": {"action": "prefer_blueprint", "blueprint_id": "public-evidence-report"}}
    assert pref.order_eligible_offers(offers, preference)[0]["blueprint_id"] == "public-evidence-report"
    assert pref.order_eligible_offers(offers[:1], preference) == offers[:1]
    suppression = {"status": "active", "scope": {"action": "suppress_watch"}}
    assert pref.suppress_optional_opportunity(suppression, optional=True)
    assert not pref.suppress_optional_opportunity(suppression, optional=True, security_or_recovery=True)
    assert not pref.suppress_optional_opportunity(suppression, optional=False)


def _proposal():
    return MemoryProposal(proposal_id="proposal", schema_version=pref.PROPOSAL_SCHEMA,
        owner_principal_id="operator:scope-test", owner_session_id="root", source_task_id="cpu-source",
        source_attempt_id="cpu-attempt", goal_id="goal", goal_revision=1, capability_id=pref.CAPABILITY_ID,
        source_context_digest="1" * 64, evidence_digest="e" * 64, request_binding_digest="c" * 64,
        memory_scope_json=pref.canonical(_scope()).decode(), memory_kind="pattern",
        decision_effect=MemoryProposalDecisionEffect.require_operator_confirmation,
        status=MemoryProposalStatus.rejected, preview_text_digest="a" * 64,
        created_at=datetime(2026, 10, 6, tzinfo=timezone.utc), updated_at=datetime(2026, 10, 6, tzinfo=timezone.utc))


def test_archive_preserves_specialized_schema_scope_and_evidence_bundle_distinction():
    exported = repo._m5_proposal_archive_payload(_proposal())
    normalized = repo._m5_normalize_proposal_record(exported,
        owner_principal_id="operator:scope-test", owner_session_id="root")
    assert normalized["schema_version"] == pref.PROPOSAL_SCHEMA
    assert json.loads(normalized["memory_scope_json"]) == _scope()
    assert normalized["evidence_digest"] == "e" * 64 != _scope()["bundle_digest"]
    with pytest.raises(ValueError, match="specialized opportunity scope"):
        repo._m5_normalize_proposal_record({**exported, "schema_version": "memory_proposal.v1"},
            owner_principal_id="operator:scope-test", owner_session_id="root")


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_normalized_specialized_row_actual_insert_stays_outside_generic_selection(async_db):
    # This is archive parser/insertion mechanics, no fabricated native success/accepted memory.
    exported = repo._m5_proposal_archive_payload(_proposal())
    normalized = repo._m5_normalize_proposal_record(exported,
        owner_principal_id="operator:scope-test", owner_session_id="root")
    async with async_db() as db:
        db.add(MemoryProposal(**normalized))
    async with async_db() as db:
        row = await db.get(MemoryProposal, "proposal")
        assert row.schema_version == pref.PROPOSAL_SCHEMA
        assert json.loads(row.memory_scope_json) == _scope()
        # Malicious/unsupported accepted metadata is never even visited by the generic selector.
        row.status = MemoryProposalStatus.accepted
        db.add(row)
        await db.flush()
        candidates = await repo.memory_repository.list_m5_accepted_memory_candidates(db,
            owner_principal_id="operator:scope-test", owner_session_id="root", goal_id="goal", goal_revision=1,
            source_context_digest="1" * 64)
        assert candidates == []
        assert row.status == MemoryProposalStatus.accepted


@pytest.mark.asyncio
async def test_actual_export_restore_inserts_exact_specialized_pair_and_does_not_recreate_preview(local_memory_db):
    # Rejected content-free metadata tests real archive restoration, not native/adoption success.
    factory, database_path = local_memory_db
    operator = make_test_bypass_operator()
    row = _proposal()
    row.owner_principal_id, row.owner_session_id = operator.principal.principal_id, operator.session_id
    scope = {**_scope(), "owner_principal_id": row.owner_principal_id, "owner_session_id": row.owner_session_id}
    row.memory_scope_json = pref.canonical(scope).decode()
    row.preview_text = "Private preview must not be reconstructed by restore."
    async with factory() as db:
        db.add(row)
    with _runtime_operator(operator):
        archive = await repo.memory_repository.export_canonical_memory_state(actor=row.owner_principal_id,
            owner_session_id=row.owner_session_id, authenticated_session_id=row.owner_session_id)
    assert archive["m5_proposals"][0]["schema_version"] == pref.PROPOSAL_SCHEMA
    assert archive["m5_proposals"][0]["memory_scope"] == scope
    assert "preview_text" not in archive["m5_proposals"][0]
    async with factory() as db:
        await db.execute(delete(MemoryProposal))
    with _runtime_operator(operator):
        result = await repo.memory_repository.restore_canonical_memory_state(archive, actor=row.owner_principal_id,
            owner_session_id=row.owner_session_id, authenticated_session_id=row.owner_session_id)
    assert result["m5_restored_proposal_ids"] == [row.proposal_id]
    async with factory() as db:
        restored = (await db.execute(select(MemoryProposal).where(MemoryProposal.proposal_id == row.proposal_id))).scalar_one()
        assert restored.schema_version == pref.PROPOSAL_SCHEMA
        assert json.loads(restored.memory_scope_json) == scope
        assert restored.preview_text is None and restored.provenance_json == "{}"


@pytest.mark.asyncio
async def test_fresh_valid_different_outcome_cannot_reauthorize_old_tip(monkeypatch):
    from src.guardian import feedback
    cpu_request = pref.OpportunityRecommendationRequest(expected_opportunity_revision=1,
        expected_feedback_revision=1, idempotency_key=str(uuid4()))
    owner = SimpleNamespace(principal_id="owner", session_id="root")
    anchor = SimpleNamespace(id="a", revision=1, owner_principal_id="owner", original_root_id="root",
        goal_id="goal", goal_revision=1, expires_at=pref.now() + timedelta(hours=1))
    root = SimpleNamespace(idle_expires_at=anchor.expires_at, absolute_expires_at=anchor.expires_at)
    tip = {"event_id": str(uuid4()), "feedback_at": pref.now().isoformat(), "feedback_type": "helpful",
        "outcome_binding_digest": "a" * 64}
    history = SimpleNamespace(revision=1, tip_bytes=pref.canonical(tip), event_count=1, history_digest="h")
    # A fresh source witness may verify a DIFFERENT current execution. The old vote cannot transfer.
    witness = SimpleNamespace(intervention_id="i-a", history=history, outcome_binding_digest="b" * 64,
        outcomes=(object(),), blueprint_id="public-evidence-report")
    async def staged(*args, **kwargs):
        return witness
    async def current_root(*args, **kwargs):
        return root
    async def inventory(*args, **kwargs):
        return [SimpleNamespace(id="i-a")], b"inventory"
    monkeypatch.setattr(feedback, "stage_opportunity_feedback_source", staged)
    monkeypatch.setattr(pref, "assert_current_root", current_root)
    monkeypatch.setattr(pref, "_inventory", inventory)
    with pytest.raises(Exception, match="different or unverified outcome"):
        await pref.stage_population(None, owner, anchor=anchor, request=cpu_request, cutoff_at=pref.now(),
            operator=SimpleNamespace(idle_expires_at=root.idle_expires_at, absolute_expires_at=root.absolute_expires_at))


@pytest.mark.asyncio
async def test_later_database_idle_touch_cannot_extend_captured_root_authority(monkeypatch):
    current = pref.now()
    root = SimpleNamespace(idle_expires_at=current + timedelta(hours=1), absolute_expires_at=current + timedelta(hours=2))
    operator = SimpleNamespace(idle_expires_at=current - timedelta(seconds=1), absolute_expires_at=root.absolute_expires_at)
    async def canonical_root(*args, **kwargs):
        return root
    monkeypatch.setattr(pref, "assert_current_root", canonical_root)
    with pytest.raises(Exception, match="captured Root authority expired"):
        await pref._assert_original_root(None, operator)
