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


async def test_additive_linkage_migrates_historical_proposals_without_replacement(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.exc import IntegrityError
    from src.db.engine import _ensure_work_board_columns
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE work_board_proposals (proposal_id VARCHAR PRIMARY KEY, kind VARCHAR, status VARCHAR)")
            await conn.exec_driver_sql("INSERT INTO work_board_proposals VALUES ('old','specify','rejected')")
            await _ensure_work_board_columns(conn)
            await _ensure_work_board_columns(conn)
            row = (await conn.exec_driver_sql("SELECT proposal_id, kind, status, opportunity_id, opportunity_revision FROM work_board_proposals")).one()
            assert tuple(row) == ('old', 'specify', 'rejected', None, None)
            await conn.exec_driver_sql("INSERT INTO work_board_proposals (proposal_id,kind,status,opportunity_id,opportunity_revision) VALUES ('canonical','opportunity_plan','expired','opportunity',1)")
        async with engine.begin() as conn:
            with pytest.raises(IntegrityError):
                await conn.exec_driver_sql("INSERT INTO work_board_proposals (proposal_id,kind,status,opportunity_id,opportunity_revision) VALUES ('replacement','opportunity_plan','pending_inference','opportunity',1)")
    finally:
        await engine.dispose()


@pytest.mark.parametrize("contacted,expired,status,expected", [
    (False, False, 'blocked', True), (False, False, 'pending_inference', True),
    (True, False, 'blocked', False), (False, True, 'blocked', False),
    (False, False, 'expired', False), (False, False, 'rejected', False)])
async def test_retry_proof_keeps_original_never_contacted_unexpired_lineage(async_db, contacted, expired, status, expected):
    from src.db.models import WorkBoardTask, WorkBoardProposal, WorkBoardStatus
    from src.guardian.opportunity_plans import generation_retry_allowed
    current = datetime.now(timezone.utc)
    async with async_db() as db:
        task = WorkBoardTask(task_id='original-card', owner_principal_id='owner', owner_session_id='root',
            goal_id='goal', goal_revision=1, title='Original', status=WorkBoardStatus.triage, idempotency_key='original-card-key')
        proposal = WorkBoardProposal(proposal_id='original-proposal', owner_principal_id='owner', owner_session_id='root',
            parent_task_id=task.task_id, parent_revision=1, opportunity_id='original-opportunity', kind='opportunity_plan',
            idempotency_key='original-key', admission_job_id='original-native-job', status=status,
            provider_contact_started=contacted, provider_contact_state='unknown' if contacted else 'not_started',
            expires_at=current + timedelta(minutes=-1 if expired else 4))
        db.add(task); db.add(proposal); await db.flush()
        assert await generation_retry_allowed(db, proposal) is expected
        assert proposal.proposal_id == 'original-proposal' and proposal.admission_job_id == 'original-native-job'
        assert task.status == WorkBoardStatus.triage and task.task_revision == 1


@pytest.mark.parametrize("revision,status", [(2, 'proposed'), (1, 'dismissed')])
async def test_source_recheck_rejects_changed_opportunity_revision_or_dismissal(revision, status):
    from src.guardian.opportunity_plans import PlanSourceWitness, recheck_plan_source
    from src.guardian.opportunity_contracts import OpportunityError
    from src.db.models import GuardianOpportunity
    opportunity = GuardianOpportunity(id='opportunity', revision=revision, status=status,
        owner_principal_id='owner', original_root_id='root', goal_id='goal', goal_revision=1,
        policy_revision=1, watch_id='watch', watch_revision=1, source_packet_id='packet',
        source_digest='a'*64, source_token_json='{}', dedupe_key='original',
        expires_at=datetime.now(timezone.utc)+timedelta(minutes=4), assessment_deadline_at=datetime.now(timezone.utc))
    witness = PlanSourceWitness('opportunity', 1, 'owner', 'root', 'goal', 1, 1, 'watch', 1,
        'packet', 'a'*64, 'artifact', b'{}', b'{}', 'public', 'b'*64, 'https://example.com/public', b'{}')
    # Reject before any current-policy/native/physical getter can be invoked.
    with pytest.raises(OpportunityError, match='source_stale'):
        await recheck_plan_source(None, opportunity, source_witness=witness)


@pytest.mark.parametrize('status', ['pending_inference', 'blocked', 'expired', 'rejected'])
def test_unfinalized_native_output_digest_is_not_public_review_authority(status):
    from src.db.models import WorkBoardProposal
    from src.guardian.opportunity_plans import proposal_ref
    current = datetime.now(timezone.utc)
    proposal = WorkBoardProposal(owner_principal_id='owner', owner_session_id='root',
        parent_task_id='original-card', kind='opportunity_plan', idempotency_key='original',
        expires_at=current+timedelta(minutes=4), status=status, proposal_digest='a'*64,
        proposal_json=json.dumps({'model_result': {'blueprint_id': 'public-browser-check'},
            'generation_output_digest': 'a'*64, 'generation_result_json': '{}'}))
    assert proposal_ref(proposal)['proposal_digest'] is None
    assert proposal.proposal_digest == 'a'*64
