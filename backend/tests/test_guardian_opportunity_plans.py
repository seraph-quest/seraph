"""Opportunity plans retain advisory staging and explicit queue acceptance."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.guardian.opportunity_contracts import OpportunityEvidence, digest


@pytest.mark.parametrize('mutation', [None, 'rotate', 'revoke', 'replace', 'disable', 'policy_revision', 'goal_revision', 'wrong_opportunity'])
async def test_auto_root_witness_is_separate_from_request_bearer_and_rechecks_races(async_db, monkeypatch, mutation):
    """Real internal Root authentication; isolated policy/source prerequisite only."""
    from types import SimpleNamespace
    from sqlalchemy import select, func
    from src.auth import service as auth
    from src.db.models import OperatorSession, GuardianOpportunity, WorkBoardTask, WorkBoardProposal, WorkflowRunState
    from src.guardian import opportunity_plans as plans
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.repository import BoardError
    from src.guardian.opportunity_contracts import OpportunityError
    principal = 'operator:root:autoplan-owner'
    current = datetime.now(timezone.utc)
    async with async_db() as db:
        db.add(OperatorSession(id='auto-root', principal_id=principal, token_hash='original-private-hash',
            idle_expires_at=current+timedelta(minutes=8), absolute_expires_at=current+timedelta(minutes=10)))
        db.add(GuardianOpportunity(id='auto-opportunity', owner_principal_id=principal, original_root_id='auto-root',
            goal_id='goal', goal_revision=1, policy_revision=1, watch_id='watch', watch_revision=1,
            source_packet_id='packet', source_digest='a'*64, source_token_json='{}', dedupe_key='auto',
            status='proposed', expires_at=current+timedelta(minutes=7), assessment_deadline_at=current))
        await db.commit()
    monkeypatch.setattr(auth, 'get_session', async_db)
    monkeypatch.setattr(plans.db_engine, 'get_session', async_db)
    enabled = True
    async def source_policy(*args, **kwargs):
        return None, None, None, SimpleNamespace(auto_stage_plan=enabled), None
    monkeypatch.setattr(plans, 'assert_opportunity_current', source_policy)
    operator = await auth.authenticate_session('auto-root', touch=False)
    assert operator._token_hash is None
    owner = WorkBoardOwner(principal_id=principal, session_id='auto-root')
    async with async_db() as db:
        with pytest.raises(BoardError):
            await plans._recheck_plan_operator(db, owner, operator, 'auto-opportunity')
    seen = []
    async def generation_boundary(*, operator, opportunity_id, request, server_witness):
        nonlocal enabled
        seen.append(server_witness)
        assert operator._token_hash is None
        assert 'original-private-hash' not in repr(server_witness)
        async with async_db() as db:
            await plans._recheck_plan_operator(db, owner, operator, opportunity_id, server_witness=server_witness)
        if mutation is None:
            return  # Proof of owner seam only; no seeded native/model result.
        async with async_db() as db:
            root = await db.get(OperatorSession, 'auto-root')
            row = await db.get(GuardianOpportunity, 'auto-opportunity')
            if mutation == 'rotate': root.token_hash = 'rotated-private-hash'
            elif mutation == 'revoke': root.revoked_at = current
            elif mutation == 'replace': root.replaced_by_id = 'another-root'
            elif mutation == 'policy_revision': row.policy_revision += 1
            elif mutation == 'goal_revision': row.goal_revision += 1
            elif mutation == 'disable': enabled = False
            await db.commit()
        async with async_db() as db:
            with pytest.raises(OpportunityError):
                await plans._recheck_plan_operator(db, owner, operator,
                    'other-opportunity' if mutation == 'wrong_opportunity' else opportunity_id,
                    server_witness=server_witness)
    monkeypatch.setattr(plans, 'generate_plan', generation_boundary)
    await plans.auto_stage_plan('auto-opportunity')
    assert len(seen) == 1
    async with async_db() as db:
        for model in (WorkBoardTask, WorkBoardProposal, WorkflowRunState):
            assert await db.scalar(select(func.count()).select_from(model)) == 0


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


async def test_optional_postcommit_auto_stage_root_revocation_has_no_new_effect(async_db, monkeypatch):
    """Isolated optional-hook auth race; does not seed or claim M2 success."""
    from types import SimpleNamespace
    from sqlalchemy import select, func
    from src.auth import service as auth
    from src.db.models import GuardianOpportunity, WorkBoardTask, WorkBoardProposal, WorkflowRunState
    from src.guardian import opportunity_plans as plans
    calls = []
    current = datetime.now(timezone.utc)
    async with async_db() as db:
        db.add(GuardianOpportunity(id='postcommit-opportunity', revision=2, status='proposed',
            owner_principal_id='owner', original_root_id='original-root', goal_id='goal', goal_revision=1,
            policy_revision=1, watch_id='watch', watch_revision=1, source_packet_id='packet',
            source_digest='a'*64, source_token_json='{}', dedupe_key='original',
            expires_at=current+timedelta(minutes=4), assessment_deadline_at=current))
        await db.commit()
    async def policy_prerequisite(*args, **kwargs):
        return None, None, None, SimpleNamespace(auto_stage_plan=True), None
    async def revoked_root(root_id, *, touch):
        calls.append((root_id, touch))
        raise auth.AuthFailure('root_revoked')
    async def forbidden_generation(**kwargs):
        raise AssertionError('revoked Root must not stage another card or contact')
    monkeypatch.setattr(plans.db_engine, 'get_session', async_db)
    monkeypatch.setattr(plans, 'assert_opportunity_current', policy_prerequisite)
    monkeypatch.setattr(auth, 'authenticate_session', revoked_root)
    monkeypatch.setattr(plans, 'generate_plan', forbidden_generation)
    await plans.auto_stage_plan('postcommit-opportunity')
    assert calls == [('original-root', False)]
    async with async_db() as db:
        opportunity = await db.get(GuardianOpportunity, 'postcommit-opportunity')
        assert (opportunity.status, opportunity.revision, opportunity.proposal_id) == ('proposed', 2, None)
        for model in (WorkBoardTask, WorkBoardProposal, WorkflowRunState):
            assert await db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize('expired,expected', [(False, None), (True, 'proposal_stale')])
async def test_work_get_does_not_finalize_expired_or_unverified_contact(async_db, monkeypatch, expired, expected):
    """Real SQL orphan metadata cannot authorize local adoption without native proof."""
    from src.db.models import GuardianOpportunity, WorkBoardProposal, WorkBoardTask, WorkflowRunState
    from src.guardian import opportunity_plans as plans
    from src.work_board.contracts import WorkBoardOwner
    from sqlalchemy import select, func
    current = datetime.now(timezone.utc)
    async with async_db() as db:
        db.add(GuardianOpportunity(id='orphan-opportunity', revision=2, status='proposed', proposal_id='orphan-proposal',
            owner_principal_id='owner', original_root_id='root', goal_id='goal', goal_revision=1, policy_revision=1,
            watch_id='watch', watch_revision=1, source_packet_id='packet', source_digest='a'*64,
            source_token_json='{}', dedupe_key='orphan', expires_at=current+timedelta(minutes=4), assessment_deadline_at=current))
        db.add(WorkBoardProposal(proposal_id='orphan-proposal', opportunity_id='orphan-opportunity',
            owner_principal_id='owner', owner_session_id='root', parent_task_id='original-card', kind='opportunity_plan',
            idempotency_key='original', status='pending_inference', provider_contact_started=True,
            provider_contact_state='unknown', proposal_json=json.dumps({'model_result': {'blueprint_id': 'public-browser-check'}}),
            expires_at=current+timedelta(minutes=-1 if expired else 4)))
        await db.commit()
    async def forbidden_finalizer(*args, **kwargs):
        raise AssertionError('unverified or expired output cannot reach finalization')
    monkeypatch.setattr(plans.db_engine, 'get_session', async_db)
    monkeypatch.setattr(plans, '_finalize_plan', forbidden_finalizer)
    reason = await plans.reconcile_generated_plan(WorkBoardOwner(principal_id='owner', session_id='root'),
        'orphan-proposal', operator=object())
    assert reason == expected
    async with async_db() as db:
        proposal = await db.get(WorkBoardProposal, 'orphan-proposal')
        assert (proposal.status, proposal.revision, proposal.provider_contact_state) == ('pending_inference', 1, 'unknown')
        for model in (WorkBoardTask, WorkflowRunState):
            assert await db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize('field', ['model_result', 'generation_binding', 'blueprint_id'])
def test_operation_metadata_cannot_replace_verified_original_generation_output(field):
    """Pure integrity negative, not a fabricated native-success receipt."""
    from copy import deepcopy
    from src.guardian.opportunity_plans import _assert_original_plan_output
    from src.guardian.opportunity_contracts import json_bytes, OpportunityError
    original = {'model_result': {'title': 'Original', 'blueprint_id': 'public-browser-check'},
        'generation_binding': {'inputs': {'request_digest': 'a'*64}}, 'blueprint_id': 'public-browser-check'}
    output_digest = digest(json_bytes(original))
    metadata = {**deepcopy(original), 'generation_result_json': json_bytes(original).decode(),
        'generation_output_digest': output_digest, 'kind': 'public-evidence-pipeline.v1', 'steps': []}
    _assert_original_plan_output(metadata, output_digest)  # Additive report fields preserve the original output.
    metadata[field] = 'public-evidence-report' if field == 'blueprint_id' else {'changed': True}
    with pytest.raises(OpportunityError, match='proposal_native_readback_required'):
        _assert_original_plan_output(metadata, output_digest)
