"""Bounded owner/Goal impact projection and explicit safety pauses.

GETs never mutate. Physical inspection precedes the short writer; current
canonical rows, source state and page revision are checked again before CAS.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from src.db.models import WorkBoardEvent, WorkBoardEvidenceDependency, WorkBoardStatus, WorkBoardTask
from src.memory.evidence_dependencies import (
    _json, canonical_source_token, dependency_rows, digest,
)
from src.memory.evidence_execution import _current_operator
from src.work_board.repository import BoardError, WorkBoardRepository, _begin_sqlite_immediate

PAGE_LIMIT = 50


class ImpactRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    source_id: str = Field(pattern='^[a-f0-9]{64}$')
    cursor: str | None = Field(default=None, max_length=2048)
    expected_snapshot_digest: str = Field(pattern='^[a-f0-9]{64}$')
    idempotency_key: str = Field(min_length=36, max_length=36)
    acknowledge_safety_pause: bool

    @field_validator('acknowledge_safety_pause')
    @classmethod
    def acknowledged(cls, value):
        if value is not True:
            raise ValueError('Explicit bounded safety-pause acknowledgment is required')
        return value

    @field_validator('idempotency_key')
    @classmethod
    def canonical_uuid(cls, value):
        from uuid import UUID
        if str(UUID(value)) != value:
            raise ValueError('Canonical UUID required')
        return value


def _cursor(value):
    return base64.urlsafe_b64encode(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).decode()


def _decode_cursor(value):
    try:
        if len(value)>2048:
            raise ValueError()
        result=json.loads(base64.b64decode(value,altchars=b'-_',validate=True))
        if (not isinstance(result,dict) or set(result)!={'scope','token_digest','after'}
            or not isinstance(result['after'],str) or not 1<=len(result['after'])<=128):
            raise ValueError()
        return result
    except (ValueError,TypeError) as exc:
        raise BoardError('evidence_impact_cursor_stale','Inspect a fresh bounded impact page',status_code=409) from exc


async def _token(db,owner,task,row):
    try:
        original=_json(row.resolved_token_json,maximum=8192)
        token=await canonical_source_token(db,owner,task,row.source_kind,row.canonical_source_id,
            original.get('lineage'))
        return {'state':'available','token':token}
    except BoardError as exc:
        # Missing/deleted/revoked sources remain visible as opaque reasons.
        # This unavailable state can only pause; it never grants use authority.
        return {'state':'unavailable','reason_code':exc.code}


def _physical(token,cache):
    if token['state']!='available':
        return False
    from src.memory.evidence_working_set import _read_file
    value=token['token']; paths=[]
    if value.get('file_path'):
        paths.append((value['file_path'],value['content_digest']))
    for ancestor in (value.get('upstream_permission') or {}).get('ancestors',[]):
        paths.append((ancestor['file_path'],ancestor['content_digest']))
    for path,checksum in paths:
        key=(path,checksum)
        if key not in cache:
            # At most 50 tasks * 16 selected rows * 3 fixed source files,
            # each bounded by the existing 64-KiB no-follow reader.
            if len(cache)>=PAGE_LIMIT*16*3:
                raise BoardError('evidence_dependency_limit','Impact physical inventory exceeds its finite bound')
            try:
                _read_file(path,checksum);cache[key]=True
            except (BoardError,OSError,ValueError):
                cache[key]=False
        if not cache[key]:
            return False
    return True


@dataclass
class Page:
    anchor: WorkBoardTask
    scope: str
    token_digest: str
    items: list[dict]
    next_cursor: str | None
    snapshot_digest: str
    projection: dict


async def _page(db,owner,task_id,source_id,cursor,*,physical=True,physical_state=None):
    anchor=await WorkBoardRepository().get_task(db,owner,task_id)
    if (anchor.owner_principal_id,anchor.owner_session_id)!=(owner.principal_id,owner.session_id):
        raise BoardError('evidence_impact_owner','Impact requires the original task owner',status_code=409)
    anchors=await dependency_rows(db,anchor)
    selected=[row for row in anchors if row.source_id==source_id]
    if not selected:
        raise BoardError('evidence_source_unavailable','Select an existing execution-bound source',status_code=404)
    reference=selected[0]
    if any((r.source_kind,r.canonical_source_id)!=(reference.source_kind,reference.canonical_source_id) for r in selected):
        raise BoardError('evidence_dependency_invalid','Source identity is ambiguous')
    state=await _token(db,owner,anchor,reference)
    scope=digest({'owner':owner.principal_id,'session':owner.session_id,'goal':anchor.goal_id,
        'kind':reference.source_kind,'canonical':reference.canonical_source_id,'source':source_id})
    token_digest=digest(state);after=None
    if cursor:
        decoded=_decode_cursor(cursor)
        if decoded['scope']!=scope or decoded['token_digest']!=token_digest:
            raise BoardError('evidence_impact_cursor_stale','The source changed after this impact page',status_code=409)
        after=decoded['after']
    query=select(WorkBoardTask).join(WorkBoardEvidenceDependency,
        WorkBoardEvidenceDependency.task_id==WorkBoardTask.task_id).where(
        WorkBoardTask.owner_principal_id==owner.principal_id,
        WorkBoardTask.owner_session_id==owner.session_id,WorkBoardTask.goal_id==anchor.goal_id,
        WorkBoardEvidenceDependency.owner_principal_id==owner.principal_id,
        WorkBoardEvidenceDependency.owner_session_id==owner.session_id,
        WorkBoardEvidenceDependency.goal_id==anchor.goal_id,
        WorkBoardEvidenceDependency.source_kind==reference.source_kind,
        WorkBoardEvidenceDependency.canonical_source_id==reference.canonical_source_id)
    if after is not None:
        query=query.where(WorkBoardTask.task_id>after)
    tasks=list((await db.execute(query.distinct().order_by(WorkBoardTask.task_id).limit(PAGE_LIMIT+1)
        .execution_options(populate_existing=True))).scalars())
    cache={};items=[]
    for task in tasks[:PAGE_LIMIT]:
        rows=await dependency_rows(db,task)
        selected_rows=[r for r in rows if (r.source_kind,r.canonical_source_id)==
            (reference.source_kind,reference.canonical_source_id)]
        current=await _token(db,owner,task,selected_rows[0])
        valid_bytes=(_physical(current,cache) if physical else
                     (physical_state or {}).get(task.task_id,False))
        stale=(not valid_bytes or current['state']!='available' or any(
            _json(row.resolved_token_json,maximum=8192)!=current.get('token') for row in selected_rows))
        items.append({'task_id':task.task_id,'task_revision':task.task_revision,
            'status':task.status.value,'stale':stale,'physical_valid':valid_bytes,
            'source_state_digest':digest(current),'selected_binding_digest':digest([
                {'dependency_id':r.dependency_id,'token':r.resolved_token_json,
                 'source_digest':r.source_digest,'span_digest':r.span_digest} for r in selected_rows]),
            'block_kind':task.block_kind,'pipeline_operation_id':task.pipeline_operation_id,
            'pipeline_slot':task.pipeline_slot})
    next_cursor=(_cursor({'scope':scope,'token_digest':token_digest,'after':items[-1]['task_id']})
                 if len(tasks)>PAGE_LIMIT else None)
    snapshot=digest({'scope':scope,'token_digest':token_digest,'cursor':cursor,'items':items})
    projection={'source_id':source_id,'snapshot_digest':snapshot,'cursor':cursor,'next_cursor':next_cursor,
        'tasks':[{'task_id':i['task_id'],'task_revision':i['task_revision'],'status':i['status'],
                  'stale':i['stale'],'reason_code':'evidence_dependency_stale' if i['stale'] else None,
                  'pipeline_slot':i['pipeline_slot']} for i in items],
        'limit':PAGE_LIMIT,'memory_status':'no_learning'}
    return Page(anchor,scope,token_digest,items,next_cursor,snapshot,projection)


async def inspect_impact(db,owner,task_id,source_id,cursor=None,*,pending=None):
    await WorkBoardRepository().get_task(db,owner,task_id)
    applied=None
    if pending is not None:
        if pending.source_id!=source_id or pending.cursor!=cursor:
            raise BoardError('evidence_impact_stale','Retained request does not match this exact page')
        checksum=digest({'task_id':task_id,'mutation':'evidence-impact','request':pending.model_dump()})
        applied=await _prior(db,owner,pending.idempotency_key,checksum)
    try:
        projection=(await _page(db,owner,task_id,source_id,cursor)).projection
    except BoardError as exc:
        if applied is None or exc.code!='evidence_source_unavailable':raise
        projection={'source_id':source_id,'snapshot_digest':None,'cursor':cursor,'next_cursor':None,
                    'tasks':[],'limit':PAGE_LIMIT,'memory_status':'no_learning'}
    return {**projection,'applied_result':applied}


async def _prior(db,owner,key,checksum):
    row=await db.scalar(select(WorkBoardEvent).where(WorkBoardEvent.owner_principal_id==owner.principal_id,
        WorkBoardEvent.owner_session_id==owner.session_id,WorkBoardEvent.mutation_idempotency_key==key))
    if row is None:return None
    if row.kind!='task.evidence.impact_evaluated' or row.mutation_request_digest!=checksum:
        raise BoardError('evidence_idempotency_conflict','The key belongs to another exact mutation')
    return _json(row.metadata_json)['applied_result']


async def evaluate_impact(db,owner,task_id,request,*,operator=None):
    await _current_operator(db,owner,operator)
    await WorkBoardRepository().get_task(db,owner,task_id)
    checksum=digest({'task_id':task_id,'mutation':'evidence-impact','request':request.model_dump()})
    prior=await _prior(db,owner,request.idempotency_key,checksum)
    if prior is not None:return prior
    staged=await _page(db,owner,task_id,request.source_id,request.cursor)
    if staged.snapshot_digest!=request.expected_snapshot_digest:
        raise BoardError('evidence_impact_stale','Inspect the changed impact page',status_code=409)
    physical={i['task_id']:i['physical_valid'] for i in staged.items}
    await db.rollback();await _begin_sqlite_immediate(db)
    await _current_operator(db,owner,operator)
    prior=await _prior(db,owner,request.idempotency_key,checksum)
    if prior is not None:return prior
    current=await _page(db,owner,task_id,request.source_id,request.cursor,
                        physical=False,physical_state=physical)
    await WorkBoardRepository().validate_task_goal(db,owner,current.anchor)
    if current.snapshot_digest!=staged.snapshot_digest:
        raise BoardError('evidence_impact_stale','Current task/source state changed before evaluation',status_code=409)
    paused=[];retained=[]
    for item in current.items:
        if not item['stale']:continue
        task=await WorkBoardRepository().get_task(db,owner,item['task_id'])
        if task.status in {WorkBoardStatus.todo,WorkBoardStatus.ready,WorkBoardStatus.triage}:
            previous=task.status.value
            await WorkBoardRepository()._cas_task_update(db,owner,task,expected_revision=item['task_revision'],
                values={'status':WorkBoardStatus.blocked,'block_kind':'dependency',
                    'block_reason':'Selected execution evidence requires review','block_source_status':previous,
                    'task_revision':item['task_revision']+1})
            paused.append(task.task_id)
        else:
            retained.append(task.task_id)
        db.add(WorkBoardEvent(task_id=task.task_id,owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,actor_principal_id=owner.principal_id,
            actor_session_id=owner.session_id,kind=('task.evidence.impact_paused' if task.task_id in paused
                else 'task.evidence.impact_observed'),metadata_json=json.dumps({
                'reason_code':'evidence_dependency_stale','task_revision':task.task_revision,
                'source_id':request.source_id,'status_preserved':task.task_id in retained,'no_learning':True})))
    result={'task_id':task_id,'source_id':request.source_id,'snapshot_digest':staged.snapshot_digest,
            'paused_task_ids':paused,'retained_task_ids':retained,'next_cursor':staged.next_cursor,
            'memory_status':'no_learning'}
    db.add(WorkBoardEvent(task_id=task_id,owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,actor_principal_id=owner.principal_id,
        actor_session_id=owner.session_id,kind='task.evidence.impact_evaluated',
        mutation_idempotency_key=request.idempotency_key,mutation_request_digest=checksum,
        metadata_json=json.dumps({'applied_result':result})))
    await db.flush();return result
