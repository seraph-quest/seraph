"""Read-only composition of adapter-owned authority; never an admission cache."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from sqlalchemy import select
from pydantic import BaseModel, ConfigDict, Field

from src.auth.service import authenticate_session, AuthFailure
import src.db.engine as database
from src.db.models import (Goal, GoogleServiceConnection, CalendarReadConsent, MailReadConsent,
    AudioConsentGrant, AudioIngressJob, GovernedScheduleBinding, GuardianSourceWatch,
    ApprovalRequest, WorkBoardTask, TelegramTransportState, GitHubFollowthroughConnection)

LIMIT = 100


def _iso(value):
    if value is None:
        return None
    return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()


def _revision(row):
    return int(getattr(row, 'revision', 1) or 1)


def _entry(kind, record_id, *, boundary, purpose, source, destination, state, revision,
           expiry=None, goal_id=None, limits=None, controls=None, jobs=None):
    capability={'calendar_consent':'calendar.observe_due_events.v1','mail_consent':'mail.read.v1','audio':'audio.push_to_talk.v1','goal':'goal.proactive.v1','source_watch':'guardian.research-watch.v1','github':'github.followthrough.v1','telegram':'telegram.transport.v1','node':'paired.observation.v1'}.get(kind.removesuffix('_model'),kind)
    return dict(grant_id=f'{kind}:{record_id}', kind=kind, capability_id=capability, record_id=record_id,
        boundary=boundary, purpose=purpose, source=source, destination=destination,
        state=state, revision=revision, expires_at=_iso(expiry), goal_id=goal_id,
        origin='current_authenticated_root', limits=limits or {},
        affected_jobs=jobs or [], controls=controls or [], authority_cache=False)


async def _operator(request):
    operator = getattr(request.state, 'operator', None)
    if operator is None:
        raise HTTPException(401, detail={'code': 'authenticated_operator_required'})
    try:
        current = await authenticate_session(operator.session_id)
    except AuthFailure as exc:
        raise HTTPException(401, detail={'code': exc.code}) from exc
    if current.principal.principal_id != operator.principal.principal_id:
        raise HTTPException(401, detail={'code': 'authenticated_operator_required'})
    return operator


async def inventory(request):
    operator = await _operator(request)
    principal, root = operator.principal.principal_id, operator.session_id
    rows, unavailable, truncated = [], [], []
    async with database.get_session() as db:
        tasks = (await db.execute(select(WorkBoardTask).where(
            WorkBoardTask.owner_principal_id == principal, WorkBoardTask.owner_session_id == root,
        ).limit(LIMIT + 1))).scalars().all()
        def jobs(goal_id=None):
            return [{'job_id': t.task_id, 'state': str(getattr(t.status, 'value', t.status)),
                     'kind': 'work_board_task'} for t in tasks[:LIMIT] if goal_id and t.goal_id == goal_id]
        async def owned(model, session_field='owner_session_id'):
            result = (await db.execute(select(model).where(model.owner_principal_id == principal,
                getattr(model, session_field) == root).limit(LIMIT + 1))).scalars().all()
            if len(result) > LIMIT:
                truncated.append(model.__tablename__)
            return result[:LIMIT]
        goals={g.id:g for g in await owned(Goal)}
        schedules=await owned(GovernedScheduleBinding)
        def consent_jobs(consent_id,kind):
            return [{'job_id':b.scheduled_job_id,'state':b.state,'kind':'scheduled_job'} for b in schedules
                    if b.read_consent_id==consent_id and b.consent_kind==('mail_read' if kind=='mail_consent' else 'calendar_read')]
        connections = {c.connection_id: c for c in await owned(GoogleServiceConnection)}
        for c in connections.values():
            kind = 'mail_connection' if c.service.startswith('gmail') or c.service.startswith('mail') else 'calendar_connection'
            rows.append(_entry(kind,c.connection_id,boundary='credential',purpose='adapter_connection',
                source='mail' if kind.startswith('mail') else 'calendar',destination='local_adapter',
                state=c.state,revision=c.revision,controls=['revoke'],limits={'credential_is_grant':False}))
        for model, kind in ((CalendarReadConsent,'calendar_consent'),(MailReadConsent,'mail_consent')):
            for c in await owned(model):
                connection=connections.get(c.connection_id)
                effective = c.state if connection and connection.state=='active' and connection.revision==c.connection_revision else 'blocked_connection'
                goal=goals.get(c.goal_id)
                if not goal or goal.revision!=c.goal_revision or str(getattr(goal.status,'value',goal.status))!='active':
                    effective='blocked_goal'
                if c.expires_at.replace(tzinfo=c.expires_at.tzinfo or timezone.utc)<=datetime.now(timezone.utc):
                    effective='expired'
                if kind=='mail_consent':
                    limits={'max_messages':c.max_messages,'window_days':c.window_days}
                    model_allowed=c.model_egress_allowed
                    observation=c.source_read_allowed
                else:
                    limits={'max_events':c.max_events,'window_minutes':c.window_minutes}
                    model_allowed=c.allow_remote_model
                    observation=True
                rows.append(_entry(kind,c.consent_id,boundary='observation',purpose='goal_source_read',
                    source='mail' if kind.startswith('mail') else 'calendar',destination='local_private_artifact',
                    state=effective if observation else 'denied',revision=c.revision,expiry=c.expires_at,
                    goal_id=c.goal_id,limits={**limits,'affected_jobs_scope':'exact_schedule_consent_bindings'},controls=['revoke'],jobs=consent_jobs(c.consent_id,kind)))
                rows.append(_entry(kind+'_model',c.consent_id,boundary='inference_egress',purpose='goal_source_model_context',
                    source='mail' if kind.startswith('mail') else 'calendar',destination='governed_provider',
                    state=effective if model_allowed else 'denied',revision=c.revision,expiry=c.expires_at,
                    goal_id=c.goal_id,controls=['revoke'],jobs=consent_jobs(c.consent_id,kind)))
        audio_jobs=await owned(AudioIngressJob,'operator_session_id')
        for c in await owned(AudioConsentGrant,'operator_session_id'):
            state=c.state if c.expires_at.replace(tzinfo=c.expires_at.tzinfo or timezone.utc)>datetime.now(timezone.utc) else 'expired'
            rows.append(_entry('audio',c.reference,boundary='observation' if c.boundary=='capture' else 'inference_egress',
                purpose='push_to_talk_'+c.boundary,source='microphone',destination='local_quarantine' if c.boundary=='capture' else 'governed_provider',
                state=state,revision=1,expiry=c.expires_at,controls=['revoke'],jobs=[
                    {'job_id':j.request_id,'state':j.status,'kind':'audio'} for j in audio_jobs
                    if c.reference in {j.capture_consent_reference,j.model_consent_reference}]))
        for g in goals.values():
            from src.guardian.source_watch import _goal_admission
            admitted,reason,budget=_goal_admission(g)
            if str(getattr(g.status,'value',g.status))!='active':
                admitted,reason=False,'goal_not_active'
            if admitted and (budget is None or budget.period_expires_at is None):
                admitted,reason=False,'finite_grant_period_required'
            rows.append(_entry('goal',g.id,boundary='observation',purpose='proactive_goal_work',source='goal',destination='governed_jobs',
                state='active' if admitted else 'blocked_'+reason,
                revision=g.revision,expiry=budget.period_expires_at if budget else None,goal_id=g.id,controls=['revoke'] if g.proactive_enabled else [],jobs=jobs(g.id)))
        for b in schedules:
            from src.scheduler import scheduled_jobs
            loaders={'calendar.observe_due_events.v1':scheduled_jobs._load_governed_authority,
                     'gmail.scan_metadata.v1':scheduled_jobs._load_governed_mail_authority,
                     'guardian.run_procedure.v2':scheduled_jobs._load_governed_procedure_authority}
            reason=None
            try:
                loader=loaders.get(b.action_type)
                if loader is None:
                    raise RuntimeError('schedule_action_unsupported')
                await loader(db,{'id':b.scheduled_job_id},b.binding_id)
            except Exception as exc:
                code=str(getattr(exc,'reason_code',None) or getattr(exc,'safe_code',None) or str(exc))
                reason=code if re.fullmatch(r'[a-z0-9_]{1,128}',code) else 'schedule_readiness_unavailable'
            entry=_entry('schedule',b.binding_id,boundary='observation',purpose='scheduled_source_read',source='adapter',destination='governed_jobs',
                state='blocked' if reason else 'active',revision=b.binding_revision,expiry=b.expires_at,goal_id=b.goal_id,controls=['revoke'],jobs=[
                    {'job_id':b.scheduled_job_id,'state':b.state,'kind':'scheduled_job'}])
            entry.update(stored_state=b.state,blocked_reason=reason)
            rows.append(entry)
        for w in await owned(GuardianSourceWatch):
            g=goals.get(w.goal_id)
            from src.goals.repository import deserialize_admission_budget
            budget=deserialize_admission_budget(g) if g else None
            from src.guardian.source_watch import source_watch_service
            reason=None
            try:
                await source_watch_service._assert_read_authority(w,db=db)
            except Exception as exc:
                code=str(getattr(exc,'code',None) or '')
                reason=code if re.fullmatch(r'[a-z0-9_]{1,128}',code) else 'watch_readiness_unavailable'
            entry=_entry('source_watch',w.id,boundary='observation',purpose='source_watch',source='reviewed_source_set',destination='local_evidence',
                state='blocked' if reason else 'active',revision=w.plan_revision,expiry=budget.period_expires_at if budget else None,goal_id=w.goal_id,controls=['revoke'],jobs=[
                    {'job_id':w.active_job_id or w.scheduled_job_id,'state':w.last_status or w.state,'kind':'source_watch'}])
            entry.update(stored_state=w.state,blocked_reason=reason)
            rows.append(entry)
        for a in await owned(ApprovalRequest,'operator_session_id'):
            from src.approval.repository import approval_state_revision
            rows.append(_entry('approval',a.id,boundary='external_mutation',purpose='one_exact_reviewed_action',source='operator_approval',destination='declared_tool',
                state=a.status,revision=approval_state_revision(a),expiry=a.expires_at,controls=['revoke'] if a.status in {'pending','approved'} else []))
        for t in await owned(TelegramTransportState,'operator_session_id'):
            from src.extensions.telegram_transport import telegram_state_revision
            for boundary,purpose,expiry,reference in (
                ('observation','telegram_transit',t.transit_consent_expires_at,t.transit_consent_reference),
                ('inference_egress','telegram_model_context',t.model_consent_expires_at,t.model_consent_reference)):
                active=bool(reference and expiry and expiry.replace(tzinfo=expiry.tzinfo or timezone.utc)>datetime.now(timezone.utc) and not t.revoked_at and t.pairing_state=='paired')
                rows.append(_entry('telegram_model' if boundary=='inference_egress' else 'telegram',t.id,boundary=boundary,purpose=purpose,source='telegram',destination='telegram_transit' if boundary=='observation' else 'governed_provider',
                    state='active' if active else 'denied',revision=telegram_state_revision(t),expiry=expiry,controls=['revoke']))
        # Unique principal is a current-root identity. Repository/address/key
        # remain private; this row describes reviewed mutation mode only.
        gh=(await db.execute(select(GitHubFollowthroughConnection).where(GitHubFollowthroughConnection.owner_principal_id==principal).limit(LIMIT))).scalars().all()
        for c in gh:
            from src.extensions.github_consent import projection
            consent = await projection(c, root)
            rows.append(_entry('github',c.id,boundary='external_mutation',purpose='reviewed_followthrough',source='reviewed_evidence',destination='github_repository',
                state=consent['state'],revision=c.revision,expiry=c.consent_expires_at,
                limits={'actions':','.join(consent['actions']),'root_bound':consent['root_bound'], 'credential_is_consent':False, 'maximum_duration_seconds':3600},
                controls=['revoke'],jobs=[{'job_id':c.active_job_id,'state':'reserved','kind':'publication'}] if c.active_job_id else []))
    # Paired inventory uses raw owner proof; foreign state yields only a neutral
    # reset requirement, never pairing ids, device names or credentials.
    from src.api.nodes import _node_inventory_for_owner
    from src.extensions.state import load_extension_state_payload
    from src.extensions.paired_edge import current_pairing
    payload=load_extension_state_payload()
    for adapter in _node_inventory_for_owner(principal):
        entry,_=current_pairing(payload,extension_id=adapter.extension_id,reference=adapter.reference,name=adapter.name)
        own=entry.get('owner_principal_id')==principal
        if entry:
            rows.append(_entry('node',adapter.reference,boundary='observation',purpose='paired_observation',source='paired_device',destination='local_observation',
                state=str(entry.get('lifecycle') or entry.get('pairing_state') or 'unverified') if own else 're_pair_required',
                revision=int(payload.get('revision') or 0),controls=['revoke'] if own else ['host_local_reset'],
                limits={'extension_id':adapter.extension_id,'reference':adapter.reference}))
    # #909 supplies its projection via the canonical provider owner. No import
    # fallback can authorize inference; unavailable owner is visibly degraded.
    try:
        from src.model_fabric.effective_policy import effective_policy_grants
        provider_rows=await effective_policy_grants(operator)
        if not isinstance(provider_rows,list) or not all(isinstance(row,dict) for row in provider_rows):
            raise ValueError('provider_projection_invalid')
        projected=[]
        for provider in provider_rows[:LIMIT]:
            provider=dict(provider)
            related=provider.get('affected_jobs')
            if not isinstance(related,list):
                raise ValueError('provider_projection_invalid')
            if len(related)>LIMIT:
                truncated.append('provider_affected_jobs')
            provider['affected_jobs']=related[:LIMIT]
            projected.append(provider)
        if len(provider_rows)>LIMIT:
            truncated.append('provider_policy')
        rows.extend(projected)
    except Exception:
        unavailable.append('provider_policy')
    canonical=json.dumps(rows,sort_keys=True,separators=(',',':'),default=str)
    return {'grants':rows,'snapshot_digest':hashlib.sha256(canonical.encode()).hexdigest(),
        'unavailable':unavailable,'truncated':truncated,'authority_cache':False,
        'generated_at':datetime.now(timezone.utc).isoformat(),'no_learning':True}


class RevokeRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    grant_id:str=Field(min_length=1,max_length=512)
    expected_revision:int=Field(ge=0)
    idempotency_key:str=Field(min_length=1,max_length=256)


def _with_body(request, body):
    encoded=json.dumps(body).encode()
    scope=dict(request.scope)
    scope['headers']=[(k,v) for k,v in scope.get('headers',[]) if k.lower()!=b'content-length']+[(b'content-length',str(len(encoded)).encode())]
    async def receive():
        return {'type':'http.request','body':encoded,'more_body':False}
    return Request(scope,receive)


async def revoke(request, body):
    await _operator(request)
    snapshot=await inventory(request)
    selected=next((g for g in snapshot['grants'] if g['grant_id']==body.grant_id),None)
    if not selected or ('revoke' not in selected['controls'] and 'deny' not in selected['controls'] and not (selected['kind']=='approval' and selected['state']=='consumed')):
        raise HTTPException(404,detail={'code':'effective_grant_not_found'})
    kind,record=selected['kind'],selected['record_id']
    if kind in {'audio'} and body.expected_revision!=selected['revision']:
        raise HTTPException(409,detail={'code':'effective_grant_revision_stale'})
    controls={'expected_revision':body.expected_revision,'idempotency_key':body.idempotency_key}
    from src.api import calendar, mail, audio, goals, approvals, nodes
    target=_with_body(request,controls)
    try:
        if kind.startswith('calendar_consent'):
            result=await calendar.revoke_consent(target,record)
        elif kind=='calendar_connection':
            result=await calendar.revoke_connection(target,record)
        elif kind.startswith('mail_consent'):
            result=await mail.revoke_consent(target,record)
        elif kind=='mail_connection':
            result=await mail.revoke_connection(target,record)
        elif kind=='audio':
            result=await audio.revoke_audio_consent(record,target)
        elif kind=='goal':
            result=await goals.update_goal(record,goals.GoalUpdate(proactive_enabled=False,expected_revision=body.expected_revision),target)
        elif kind=='schedule':
            result=await calendar.revoke_schedule(_with_body(request,{'expected_binding_revision':body.expected_revision,'idempotency_key':body.idempotency_key}),record)
        elif kind=='github':
            from src.extensions.github_followthrough import revoke_github_connection,ConnectionRevokeRequest
            result=await revoke_github_connection(ConnectionRevokeRequest(expected_revision=body.expected_revision),target)
        elif kind.startswith('telegram'):
            from src.api.telegram import default_telegram_transport
            operator=await _operator(request)
            from src.extensions.telegram_transport import TelegramTransportError
            try:
                result=await default_telegram_transport.revoke(owner_principal_id=operator.principal.principal_id,operator_session_id=operator.session_id,expected_revision=body.expected_revision)
            except TelegramTransportError as exc:
                raise HTTPException(409,detail={'code':exc.code}) from exc
        elif kind=='source_watch':
            from src.guardian.source_watch import update_source_watch, SourceWatchUpdateRequest
            result=await update_source_watch(record,SourceWatchUpdateRequest(expected_plan_revision=body.expected_revision,state='revoked'),target)
        elif kind=='approval':
            result=await approvals.revoke_unconsumed_approval(record,approvals.ApprovalRevokeRequest(expected_revision=body.expected_revision),target)
        elif kind=='node':
            result=await nodes.revoke_node_pairing(nodes.NodePairingMutationRequest(**selected['limits'],expected_revision=body.expected_revision),target)
        elif kind=='provider_policy':
            from src.model_fabric.effective_policy import revoke_effective_policy
            result=await revoke_effective_policy(request,body)
        else:
            raise HTTPException(404,detail={'code':'effective_grant_not_found'})
    except HTTPException as exc:
        if exc.status_code < 500:
            raise
        readback=await inventory(request)
        current=next((g for g in readback['grants'] if g['grant_id']==body.grant_id),None)
        return {'status':'partial_failure','local_state':current['state'] if current else 'unconfirmed',
            'local_fence_confirmed':bool(current and current['state'] in {'revoked','blocked_cleanup','denied','paused','blocked'}),
            'external_revocation':'not_confirmed','recovery_action':'retry_same_request_and_read_owner_metadata',
            'grant_id':body.grant_id,'readback':readback,'no_learning':True}
    readback=await inventory(request)
    if isinstance(result,dict) and result.get('outcome')=='already_consumed':
        return {'status':'already_consumed','external_revocation':'not_confirmed','grant_id':body.grant_id,'readback':readback,'no_learning':True}
    return {'status':'local_revocation_confirmed','external_revocation':'not_requested',
        'grant_id':body.grant_id,'readback':readback,'no_learning':True}
