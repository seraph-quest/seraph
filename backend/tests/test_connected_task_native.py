"""Exact current native task boundaries for explicitly selected cached sources."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from src.db.models import MailMessageBinding, MailReadConsent, OperatorSession, WorkflowRunState, Goal
from src.integrations.connected_source_contracts import ConnectedSourceTaskInput
from src.work_board.dispatcher import WorkBoardDispatcher, validate_capability_input, TypedInputError
from src.work_board.contracts import WorkBoardOwner
from src.workflows.job_runtime import DurableJobRepository
from tests.test_inference_accounting import accounting_db
from tests import test_mail_reply_vertical as mail
from tests.connected_source_native_fixture import synchronized_related_source

@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['item_before','grant_before','goal_before','root_before','runtime_missing','grant_after_model','item_terminal'])
async def test_selected_related_source_native_fences(accounting_db, monkeypatch, change):
    tmp, _, factory = accounting_db
    db_factory = factory.accounting_sessions
    await mail._seed(db_factory, monkeypatch)
    await mail._configure_model_route(db_factory, monkeypatch, tmp)
    owner = WorkBoardOwner(principal_id=mail.OWNER, session_id=mail.SESSION)
    runtime, source_provider, selections = await synchronized_related_source(db_factory, monkeypatch, owner, mail._operator(), mail.GOAL, 1)
    contacts = len(source_provider.calls)
    repository, owner, claim, inputs, created = await mail._create_claim(db_factory, monkeypatch, 'related-fence-task', {'connected_sources':selections,'acknowledge_connected_sources':True})
    async def mutate(kind):
        async with db_factory() as db:
            if kind.startswith('item'):
                row = (await db.execute(select(MailMessageBinding).where(MailMessageBinding.connection_id=='related-source-connection'))).scalar_one()
                row.revision += 1
            elif kind.startswith('grant'):
                row = await db.get(MailReadConsent, 'related-grant')
                row.state = 'revoked'
                row.source_read_allowed = False
            elif kind == 'goal_before':
                (await db.get(Goal, mail.GOAL)).revision += 1
            elif kind == 'root_before':
                (await db.get(OperatorSession, mail.SESSION)).revoked_at = datetime.now(timezone.utc)
    if change.endswith('_before'):
        await mutate(change)
    class Adapter:
        reads=0
        def __init__(self, _connection, *, contact_observer=None, **kwargs):
            self.observer=contact_observer
        async def get_message_full(self, _id):
            type(self).reads += 1
            if self.observer: self.observer()
            if change == 'grant_after_model' and type(self).reads == 2:
                await mutate(change)
            return mail._body(type(self).reads)
    models=[]
    def transport(**kwargs):
        models.append(kwargs['body'])
        content=json.dumps({'subject':'Re: Architecture review','body':'Bounded draft','caveats':[]})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(role='assistant',content=content))]), {'choices':[{'message':{'role':'assistant','content':content}}],'usage':{'cost':'0.000002'}}
    monkeypatch.setattr('src.integrations.gmail_read.GoogleGmailReadonlyAdapter', Adapter)
    monkeypatch.setattr('src.llm_runtime._governed_openai_chat_completion', transport)
    jobs=DurableJobRepository()
    if change == 'item_terminal':
        original_transition=jobs.transition_job
        async def terminal_race(job_id, status, **kwargs):
            if status == 'succeeded': await mutate(change)
            return await original_transition(job_id,status,**kwargs)
        monkeypatch.setattr(jobs,'transition_job',terminal_race)
    dispatcher=WorkBoardDispatcher(repository=repository,jobs=jobs,session_provider=db_factory)
    dispatcher.connection_sync_runtime=None if change=='runtime_missing' else runtime
    try:
        result=await dispatcher._admit_execute_direct(claim,inputs,runtime_seconds=120)
        assert result['blocked'] is True, result
        assert len(source_provider.calls)==contacts
        assert Adapter.reads == (2 if change in {'grant_after_model','item_terminal'} else 0)
        assert len(models) == (1 if change in {'grant_after_model','item_terminal'} else 0)
        async with db_factory() as db:
            roots=(await db.execute(select(WorkflowRunState).where(WorkflowRunState.job_kind=='mail_reply_draft'))).scalars().all()
            assert all(root.status != 'succeeded' for root in roots)
            if change in {'grant_after_model','item_terminal'}:
                assert len(roots)==1 and roots[0].status=='running'
                assert roots[0].lease_owner and roots[0].fencing_token > 0
                assert roots[0].artifact_receipts_json in {None,'[]'} or change=='item_terminal'
        assert 'private-body' not in json.dumps(models)
    finally:
        await runtime.stop()


def test_connected_task_bounds_ack_and_authority_keys():
    ref={'provider':'gmail','opaque_id':'opaque','revision':'revision','content_digest':'a'*64,'privacy':'owner_private','expires_at':'2026-10-09T00:00:00+00:00'}
    group={'connection_ref':{'id':'connection','revision':1},'item_refs':[ref]}
    with pytest.raises(ValueError): ConnectedSourceTaskInput.model_validate({'connected_sources':[group]})
    with pytest.raises(ValueError): ConnectedSourceTaskInput.model_validate({'connected_sources':[group]*4,'acknowledge_connected_sources':True})
    with pytest.raises(ValueError): ConnectedSourceTaskInput.model_validate({'connected_sources':[{**group,'item_refs':[ref]*2}],'acknowledge_connected_sources':True})
    body=mail._reply_body('typed-related-key');body.pop('idempotency_key')
    value=validate_capability_input('work.mail-reply-draft.v1',{**body,'connected_sources':[group],'acknowledge_connected_sources':True})
    assert value['connected_sources'][0]['item_refs'][0]['expires_at']==ref['expires_at']
    with pytest.raises(TypedInputError): validate_capability_input('work.mail-reply-draft.v1',{**body,'expires_at':ref['expires_at']})
    with pytest.raises(TypedInputError): validate_capability_input('work.mail-reply-draft.v1',{**body,'connected_sources':[{**group,'item_refs':[{**ref,'grant_id':'injected'}]}],'acknowledge_connected_sources':True})
    for misplaced in ({'connected_sources':[{'expires_at':ref['expires_at']}]}, {'connected_sources':[{'connection_ref':{'id':'connection','revision':1,'expires_at':ref['expires_at']},'item_refs':[ref]}]}, {'reply_intent':{'expires_at':ref['expires_at']}}):
        with pytest.raises(TypedInputError): validate_capability_input('work.mail-reply-draft.v1',{**body,**misplaced})
    from src.workflows.mail_reply_draft import input_digest, authority_payload, canonical_digest
    base=validate_capability_input('work.mail-reply-draft.v1',body)
    empty=validate_capability_input('work.mail-reply-draft.v1',{**body,'connected_sources':[]})
    assert input_digest(base)==input_digest(empty)
    task=SimpleNamespace(owner_principal_id='owner',owner_session_id='session',goal_id=body['goal_id'],goal_revision=1)
    assert canonical_digest(authority_payload(task=task,inputs=base))==canonical_digest(authority_payload(task=task,inputs=empty))
    assert input_digest(value)!=input_digest(base)
