"""Opportunity plans retain advisory staging and explicit queue acceptance."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.guardian.opportunity_contracts import OpportunityEvidence, digest


def evidence():
    return OpportunityEvidence(schema_version="seraph.opportunity.evidence.v1",
        packet_id="00000000-0000-0000-0000-000000000001", checkpoint_sha256="a" * 64,
        watch_revision=1, goal_revision=1, sources=[dict(source_key="public", identity_digest="b" * 64,
            target="https://example.com/public", new_hash="c" * 64,
            excerpt="Public release", excerpt_sha256=digest(b"Public release"))])


def test_plan_output_only_selects_offered_blueprint_and_exact_citations():
    from src.guardian.opportunity_plans import validate_plan_result
    raw = dict(schema_version="seraph.opportunity.plan.v1", blueprint_id="public-browser-check",
        title="Read public release", reason="Check the cited change", citations=[dict(source_id="public",
            start_line=1, end_line=1, span_sha256=digest(b"Public release"))])
    assert validate_plan_result(json.dumps(raw), evidence(), ("public-browser-check",)).title == raw["title"]
    for changed in ({**raw, "url": "https://invented.example/"}, {**raw, "blueprint_id": "public-evidence-report"}):
        with pytest.raises(ValueError):
            validate_plan_result(json.dumps(changed), evidence(), ("public-browser-check",))


@pytest.mark.parametrize("url", ["https://example.com/public?q=1", "https://example.com/public#fragment",
    "https://user@example.com/public", "http://example.com/public", "https://127.0.0.1/public"])
def test_plan_source_url_rejects_ambiguous_or_nonpublic_target(url):
    from src.guardian.opportunity_plans import fixed_browser_input
    with pytest.raises(ValueError):
        fixed_browser_input(url)


def test_fixed_browser_input_is_one_exact_readonly_source():
    from src.guardian.opportunity_plans import fixed_browser_input
    result = fixed_browser_input("https://example.com/public")
    assert result["allowed_hosts"] == ["example.com"]
    assert result["approved_url_prefixes"] == ["https://example.com/public"]
    assert [action["kind"] for action in result["actions"]] == ["navigate", "extract"]
    assert result["actions"][1]["selector"] == "body"
    assert result["actions"][1]["max_chars"] == 8192


def test_final_plan_citations_select_exact_second_source_and_lexical_tie():
    from src.guardian.opportunity_plans import validate_plan_result, selected_cited_source
    snapshot = evidence()
    second = snapshot.sources[0].model_copy(update={"source_key": "z-second", "target": "https://example.com/second"})
    snapshot = snapshot.model_copy(update={"sources": [second, snapshot.sources[0]]})
    second_citation = dict(source_id="z-second", start_line=1, end_line=1, span_sha256=digest(b"Public release"))
    raw = dict(schema_version="seraph.opportunity.plan.v1", blueprint_id="public-browser-check",
        title="Check second source", reason="The second cited source is the plan input", citations=[second_citation])
    result = validate_plan_result(json.dumps(raw), snapshot, ("public-browser-check",))
    assert selected_cited_source(snapshot, result.citations).target == "https://example.com/second"
    raw["citations"].append({**second_citation, "source_id": "public"})
    result = validate_plan_result(json.dumps(raw), snapshot, ("public-browser-check",))
    assert selected_cited_source(snapshot, result.citations).source_key == "public"


@pytest.mark.parametrize("contacted", [False, True])
async def test_daily_slots_use_actual_contact_day_and_exact_goal(async_db, monkeypatch, contacted):
    from src.db.models import GuardianOpportunity, WorkBoardProposal, InferenceCostReservation
    from src.guardian import opportunity_plans as plans
    current = datetime(2026, 10, 7, 0, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(plans, "now", lambda: current)
    async with async_db() as db:
        for identifier, goal in (("current", "goal"), ("foreign", "other-goal")):
            opportunity = GuardianOpportunity(id=identifier, owner_principal_id="owner", original_root_id="root",
                goal_id=goal, goal_revision=1, policy_revision=1, watch_id="watch", watch_revision=1,
                source_packet_id="packet", source_digest="a"*64, source_token_json="{}", dedupe_key=identifier,
                expires_at=current+timedelta(minutes=4), assessment_deadline_at=current)
            proposal = WorkBoardProposal(proposal_id=identifier, owner_principal_id="owner", owner_session_id="root",
                opportunity_id=identifier, opportunity_revision=1, parent_task_id=identifier, kind="opportunity_plan",
                idempotency_key=identifier, admission_job_id=identifier, created_at=current-timedelta(minutes=2),
                expires_at=current+timedelta(minutes=4), provider_contact_started=True)
            db.add(opportunity)
            db.add(proposal)
            db.add(InferenceCostReservation(operation_id=identifier, deployment_id="deployment", job_id=identifier,
                owner_id="owner", goal_id=goal, payload_digest="a"*64, policy_digest="b"*64,
                runtime_path="strategist_agent", profile_id="profile", period_id="period", settings_revision=1,
                ceiling_microusd=100, bound_microusd=1, sequence=1 if identifier=="current" else 2,
                priority=50, deadline_at=current+timedelta(minutes=4), state="unknown" if contacted else "reserved",
                job_fencing_token=1, contact_started_at=current if contacted or identifier=="foreign" else None))
        await db.flush()
        assert await plans.contacted_plan_count(db, "owner", "goal") == int(contacted)
        assert await plans.contacted_plan_count(db, "owner", "goal", exclude_job="current") == 0
