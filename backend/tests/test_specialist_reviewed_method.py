"""Genuine native delegation source -> reviewed method -> narrowed children."""
import json
import pytest
from sqlalchemy import select
from src.memory import task_methods as methods

pytestmark = pytest.mark.parametrize('async_db', ['file'], indirect=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',[None,'source_revision_missing','source_step_tamper','pin_tamper','grant_revoke','rollback'])
async def test_reviewed_parent_method_keeps_two_specialists_narrow(async_db, monkeypatch, tmp_path, mutation):
    from config.settings import settings
    from src.auth.service import create_session
    from src.auth.ownership import enroll
    from src.db.models import Goal, Session, WorkBoardTask, WorkBoardAttempt, WorkflowRunState, Memory
    from src.native_tools.registry import ToolRegistry
    from src.work_board.general_task import GeneralTaskService, digest
    from src.work_board.contracts import GeneralTaskCreate, GeneralTaskInput, PlanSpec, TaskLimits, WorkBoardOwner
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.memory import task_methods as methods
    from src.memory.task_lessons import LessonRequest, LessonScope, create_task_lesson, eligible_lesson_source
    from src.work_board.review import complete_review
    from tests.test_task_methods import review
    from tests.general_task_test_transport import prepare_literal_planner
    from src.work_board.repository import BoardError
    workspace=tmp_path/'workspace'; workspace.mkdir(mode=0o700)
    monkeypatch.setattr(settings,'workspace_dir',str(workspace))
    monkeypatch.setattr(settings,'operator_auth_secret','genuine-method-delegation')
    monkeypatch.setattr(settings,'use_delegation',True)
    monkeypatch.setattr('src.memory.m5.get_session',async_db)
    monkeypatch.setattr('src.workflows.job_runtime.get_session',async_db)
    _,operator=await create_session(); await enroll(operator)
    owner=WorkBoardOwner(principal_id=operator.principal.principal_id,session_id=operator.session_id)
    async with async_db() as db:
        db.add(Session(id=operator.session_id))
        db.add(Goal(id='goal',title='Create two bounded fragments',revision=1,status='active',
            owner_principal_id=owner.principal_id,owner_session_id=owner.session_id)); await db.commit()
    current=methods.CurrentMethod(); await current.start(); monkeypatch.setattr(methods,'current_method',current)
    registry=ToolRegistry(); registry.start()
    service=GeneralTaskService(registry,strategy_resolver=methods.TaskMethodStrategyResolver(current)); service.start()
    descriptors=registry.descriptors(); by_id={d.tool_id:d for d in descriptors}
    def plan(prefix):
        steps=[]
        for which in ('A','B'):
            steps.append({'step_id':'delegate-'+which,'tool_id':'delegate_task','input':{
                'role':'files','instruction':'Write '+prefix+'-'+which+'.txt','evidence_refs':[],
                'allowed_tool_ids':['write_file'],'limits':{'max_steps':1,'max_inference_calls':1,
                    'max_cost_microusd':100,'wall_seconds':300}},'output_contract':by_id['delegate_task'].output_schema})
        for which in ('A','B'):
            steps.append({'step_id':'read-'+which,'tool_id':'read_file','input':{'file_path':prefix+'-'+which+'.txt'},
                'depends_on':['delegate-'+which],'output_contract':by_id['read_file'].output_schema})
        return PlanSpec(revision=1,steps=steps)
    planner,transport=await prepare_literal_planner(async_db,workspace,monkeypatch,owner,plan('source'))
    service.planner=planner
    class ScriptedCalls(list):
        def append(self,body):
            super().append(body)
            payloads=[json.loads(m['content']) for m in body['messages'] if m['role']=='user']
            continuation=next((p for p in payloads if 'current_plan_revision' in p),None)
            specialist=next((p for p in payloads if 'specialist_role' in p),None)
            if specialist:
                path=payloads[0]['intent'].removeprefix('Write ')
                result=PlanSpec(revision=1,steps=[{'step_id':'write','tool_id':'write_file',
                    'input':{'file_path':path,'content':'actual distinct '+path},'output_contract':by_id['write_file'].output_schema}])
            elif continuation:
                prefix='source' if payloads[0]['intent']=='Create source fragments' else 'next'
                completed={p['step_id'] for p in continuation['step_statuses']}
                result=plan(prefix).model_copy(update={'revision':continuation['requested_revision'],
                    'steps':[s for s in plan(prefix).steps if s.step_id not in completed]})
            else: raise AssertionError('Explicit parent plan must not call initial planner')
            transport['content']=json.dumps(result.model_dump(mode='json'))
    transport['contacts']=ScriptedCalls()
    dispatcher=WorkBoardDispatcher(session_provider=async_db,general_tasks=service)
    # Bind the actual started owner through the existing Python lifecycle seam;
    # canonical method review reads its detached descriptors, never a fixture grant.
    monkeypatch.setattr('src.work_board.dispatcher._dispatcher',dispatcher)
    async def execute(prefix, *, blocked=False):
        request=GeneralTaskCreate(goal_revision=1,idempotency_key='genuine-'+prefix,accept=True,expected_plan_revision=1,
            input=GeneralTaskInput(goal_ref='goal',intent='Create '+prefix+' fragments',
                requested_output=by_id['read_file'].output_schema,
                tool_set_digest=digest([d.model_dump(mode='json') for d in sorted(descriptors,key=lambda d:d.tool_id)]),
                limits=TaskLimits(max_steps=4,max_inference_calls=12,max_cost_microusd=1000,wall_seconds=600),
                inference_egress_acknowledged=True),plan=plan(prefix))
        async with async_db() as db: created=await service.create(db,owner,request)
        await dispatcher.run_pass()
        for _ in range(16):
            await dispatcher.reconcile_linked_attempts()
            async with async_db() as db:
                task=await service.repository.get_task(db,owner,created.task.task_id)
                if task.status.value=='review': return task
                if task.status.value=='blocked' and task.block_reason!='general_task_native_wait': break
        async with async_db() as db:
            projection=await service.plan(db,owner,created.task.task_id)
        if blocked:
            assert task.status.value!='review'
            assert not (workspace/'next-A.txt').exists() and not (workspace/'next-B.txt').exists()
            return task
        pytest.fail('Original parent did not complete: '+json.dumps(projection,default=str))
    try:
        source=await execute('source')
        async with async_db() as db:
            attempt=await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id==source.task_id))
            done=await complete_review(db,owner,source.task_id,expected_revision=source.task_revision,attempt_id=attempt.attempt_id)
            await db.commit()
        if mutation in {'source_revision_missing','source_step_tamper'}:
            from src.workflows.general_task_guard import read_manifest
            async with async_db() as db:
                run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==attempt.workflow_run_id))
                manifest=read_manifest(run)
                record=next(r for r in json.loads(run.artifact_receipts_json)
                    if r['artifact_id']==manifest.revision_artifact_ids[1])
            path=workspace/record['file_path']
            if mutation=='source_revision_missing': path.unlink()
            else:
                payload=json.loads(path.read_text()); payload['plan']['steps'][0]['input']['instruction']='Changed historical admitted step'
                path.write_text(json.dumps(payload))
            eligible=await eligible_lesson_source(operator,source.task_id)
            assert eligible['eligible'] is False
            assert (await current.resolve(owner,'goal','work.general-task.v1')).status=='none'
            return
        eligible=await eligible_lesson_source(operator,source.task_id); assert eligible['eligible'],eligible
        lesson=await create_task_lesson(operator,LessonRequest(task_id=source.task_id,attempt_id=attempt.attempt_id,
            correction='Require verified readback.',source_refs=eligible['source_refs'],expected_revision=eligible['expected_revision'],
            scope=LessonScope(goal_id='goal',goal_revision=1,family='general')))
        inspected=await methods.inspect_method(operator,lesson['proposal_id'])
        await methods.review_method(operator,review(inspected,'accept','genuine-delegating-method'))
        pin=await current.resolve(owner,'goal','work.general-task.v1')
        assert pin.status=='active' and [s['tool_id'] for s in pin.typed_data['steps'] if s['kind']=='registered_tool']==['delegate_task','delegate_task','read_file','read_file']
        # Ordinary admission still selects the reviewed complete parent method;
        # a public child-shaped request cannot choose the private baseline.
        with pytest.raises(BoardError) as public_denied:
            await service.validate(owner,GeneralTaskCreate(goal_revision=1,idempotency_key='public-child-shaped',expected_plan_revision=1,
                input=GeneralTaskInput(goal_ref='goal',intent='Public narrowed read',requested_output=by_id['read_file'].output_schema,
                    tool_set_digest=digest([d.model_dump(mode='json') for d in sorted(descriptors,key=lambda d:d.tool_id)])),
                plan=PlanSpec(revision=1,steps=[{'step_id':'read','tool_id':'read_file','input':{'file_path':'source-A.txt'},
                    'output_contract':by_id['read_file'].output_schema}])))
        assert public_denied.value.code=='general_task_method_sequence_mismatch'
        original_propose=planner.propose_specialist
        mutations=[]
        async def propose_and_change(*args,**kwargs):
            result=await original_propose(*args,**kwargs)
            if mutation in {'pin_tamper','grant_revoke','rollback'} and not mutations:
                mutations.append(mutation)
                if mutation=='pin_tamper':
                    async with async_db() as db:
                        memory=await db.get(Memory,pin.version); memory.content+=' altered'; await db.commit()
                elif mutation=='grant_revoke':
                    from src.auth.service import revoke_session
                    await revoke_session(owner.session_id)
                else:
                    await methods.review_method(operator,review(await methods.inspect_method(operator,pin.method_id),'rollback','original-delegate-pin-rollback'))
            return result
        monkeypatch.setattr(planner,'propose_specialist',propose_and_change)
        if mutation in {'pin_tamper','grant_revoke'}:
            try: await execute('next',blocked=True)
            except BoardError:
                assert not (workspace/'next-A.txt').exists() and not (workspace/'next-B.txt').exists()
            assert mutations==[mutation]
            return
        selected=await execute('next')
        assert (workspace/'next-A.txt').read_bytes()!=(workspace/'next-B.txt').read_bytes()
        async with async_db() as db:
            projection=await service.plan(db,owner,selected.task_id)
            assert projection['strategy']['digest']==pin.digest
            children=list((await db.execute(select(WorkBoardTask).where(WorkBoardTask.idempotency_key.like('specialist:%')))).scalars())
            assert len(children)==4 and all(t.status.value=='done' for t in children)
        if mutation=='rollback':
            assert mutations==['rollback']
            assert (await current.resolve(owner,'goal','work.general-task.v1')).status=='none'
    finally:
        service.stop(); registry.stop(); await current.stop()
