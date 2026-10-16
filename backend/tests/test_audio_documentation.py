"""Actual SQLite/Vault/auth owner; final metadata HTTP only is scripted."""
from dataclasses import replace
import asyncio,json,hashlib
from pathlib import Path
import httpx,pytest
from pydantic import ValidationError
from sqlmodel import select
from sqlalchemy import text,event
from config.settings import settings
from src.auth.service import create_session,revoke_session
from src.db.models import ModelAudioDocumentationAttestationRecord as Bundle,ModelAudioDocumentationSourceRecord as Source
from src.model_fabric.repository import ModelFabricRepository
from src.model_fabric import audio_documentation as doc
from src.model_fabric.configuration import ModelFabricConfiguration,write_model_fabric_configuration,openrouter_profiles_for_setup
from src.vault.repository import vault_repository
from src.workspace.production import ProductionWorkspace,prepare_lifecycle_directory
from tests.test_audio_model_fabric_v3 import configured_audio

@pytest.fixture
async def documentary(async_db,tmp_path,monkeypatch):
    root=tmp_path/'workspace';root.mkdir()
    monkeypatch.setattr(settings,'workspace_dir',str(root))
    monkeypatch.setenv('SERAPH_WORKSPACE_LIFECYCLE_PATH',str(tmp_path/'lifecycle'))
    prepare_lifecycle_directory(ProductionWorkspace(host_root=root))
    monkeypatch.setattr(settings,'operator_auth_secret','offline-documentary-root')
    monkeypatch.setattr(settings,'openrouter_api_key','offline-inference-key')
    monkeypatch.setattr(settings,'openrouter_provider_only',True)
    token,operator=await create_session()
    setup=configured_audio();configuration=ModelFabricConfiguration(status='ready',openrouter_setup=setup,profiles=openrouter_profiles_for_setup(setup))
    write_model_fabric_configuration(configuration)
    await vault_repository.store('owned_management','offline-management-key',owner_principal_id=operator.principal.principal_id)
    repository=ModelFabricRepository();profile=doc.audio_profile(configuration)
    selection=await doc.select_metadata(repository,operator,configuration,doc.SelectMetadataCredential(action='select_existing_management_credential',vault_key_name='owned_management',expected_audio_profile_hash=profile.contract_hash,operation_scope='selected_audio_profile_metadata'))
    write_model_fabric_configuration(replace(configuration,egress_revision=2,audio_metadata_access=selection),expected_revision=1)
    request=doc.AcquireSelectedProfileMetadataV1(action='acquire_selected_profile_metadata',expected_egress_revision=2,expected_audio_profile_hash=profile.contract_hash,metadata_selection_ref=selection['selection_id'],expected_metadata_selection_digest=doc.digest(selection),official_supplement_catalog_id=doc.CATALOG)
    class Calls(list):
        mode = 'valid'
    original=httpx.AsyncClient;calls=Calls();calls.token=token
    async def resolve(*args,**kwargs):return [(2,1,6,'',('8.8.8.8',443))]
    monkeypatch.setattr(asyncio.get_running_loop(),'getaddrinfo',resolve)
    def respond(request):
        calls.append(request)
        assert request.headers['host']=='openrouter.ai'
        assert request.headers['authorization']=='Bearer offline-management-key'
        assert request.extensions['sni_hostname']=='openrouter.ai'
        value={'data':{'is_management_key':True}} if request.url.path.endswith('/key') else {'data':{'id':'vendor/audio','endpoints':[{'tag':'deepinfra/turbo'}]}}
        if calls.mode=='normal-key' and request.url.path.endswith('/key'):value={'data':{'is_management_key':False}}
        if calls.mode=='wrong-endpoint' and request.url.path.endswith('/endpoints'):value={'data':{'id':'vendor/audio','endpoints':[{'tag':'deepinfra'}]}}
        if calls.mode=='duplicate-endpoint' and request.url.path.endswith('/endpoints'):value={'data':{'id':'vendor/audio','endpoints':[{'tag':'deepinfra/turbo'},{'tag':'deepinfra/turbo'}]}}
        if calls.mode=='oversize':return httpx.Response(200,headers={'content-type':'application/json'},stream=httpx.ByteStream(b' '*16385))
        return httpx.Response(200,headers={'content-type':'application/json'},stream=httpx.ByteStream(json.dumps(value).encode()))
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(**kwargs,transport=httpx.MockTransport(respond)))
    return repository,operator,request,calls

@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_owner_private_readback_restart_reject(documentary,async_db):
    repository,operator,request,calls=documentary
    result=await repository.stage_audio_documentation(operator,request)
    assert result['state']=='staged' and not result['ready'] and not result['admission_ready']
    assert [r.url.path for r in calls]==['/api/v1/key','/api/v1/models/vendor/audio/endpoints']
    assert 'offline-management-key' not in json.dumps(result) and 'owned_management' not in json.dumps(result)
    async with async_db() as db:sources=(await db.execute(select(Source))).scalars().all()
    assert len(sources)==2
    for source in sources:assert hashlib.sha256(doc.private_read(source)).hexdigest()==source.sha256
    restarted=ModelFabricRepository();assert (await doc.owned_staging(restarted,operator))[0]==result
    accept=doc.AcceptStagedDocumentationV1(action='accept_staged_documentation',staged_ref=result['staged_ref'],expected_staged_revision=result['revision'],expected_bundle_digest=result['bundle_digest'])
    with pytest.raises(doc.DocumentationError,match='source_incomplete'):await restarted.accept_audio_documentation(operator,accept)
    with pytest.raises(doc.DocumentationError,match='quota_full'):await restarted.stage_audio_documentation(operator,request)
    assert len(calls)==2
    reject=doc.RejectStagedDocumentationV1(action='reject_staged_documentation',**accept.model_dump(exclude={'action'}))
    assert (await restarted.reject_audio_documentation(operator,reject))['state']=='rejected'
    assert not list((Path(settings.workspace_dir)/'.model-fabric/audio-documentation/objects').iterdir())
    async with async_db() as db:assert (await db.get(Bundle,result['staged_ref'])).reserved_bytes==0

@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_live_root_rotation_before_contact(documentary):
    repository,operator,request,calls=documentary
    with pytest.raises(doc.DocumentationError,match='authentication_required'):await repository.stage_audio_documentation(None,request)
    assert calls==[]
    await vault_repository.store('owned_management','rotated-key',owner_principal_id=operator.principal.principal_id)
    with pytest.raises(doc.DocumentationError,match='credential_changed'):await repository.stage_audio_documentation(operator,request)
    assert calls==[]
    await revoke_session(operator.session_id)
    with pytest.raises(doc.DocumentationError,match='root_inactive'):await repository.stage_audio_documentation(operator,request)
    assert calls==[]

@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_tamper_and_stale_revision(documentary,async_db):
    repository,operator,request,calls=documentary;result=await repository.stage_audio_documentation(operator,request)
    action=doc.AcceptStagedDocumentationV1(action='accept_staged_documentation',staged_ref=result['staged_ref'],expected_staged_revision=result['revision']+1,expected_bundle_digest=result['bundle_digest'])
    with pytest.raises(doc.DocumentationError,match='revision_changed'):await repository.accept_audio_documentation(operator,action)
    async with async_db() as db:source=(await db.execute(select(Source))).scalars().first()
    (Path(settings.workspace_dir)/'.model-fabric/audio-documentation/objects'/source.object_id).write_bytes(b'changed')
    with pytest.raises(doc.DocumentationError,match='private_object_invalid'):await repository.accept_audio_documentation(operator,action.model_copy(update={'expected_staged_revision':result['revision']}))
    assert len(calls)==2

def test_closed_review_and_scope():
    with pytest.raises(ValidationError):doc.AcceptStagedDocumentationV1.model_validate({'action':True})
    with pytest.raises(ValidationError):doc.SelectMetadataCredential.model_validate({'action':'select_existing_management_credential','vault_key_name':'owned','expected_audio_profile_hash':'a'*64,'operation_scope':'inference','approved':True})

@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('mode,expected,count',[('normal-key','management_key_required',1),('wrong-endpoint','exact_endpoint_missing',2),('duplicate-endpoint','exact_endpoint_missing',2),('oversize','response_oversized',1)])
async def test_unusable_source_stays_charged_owned_rejection(documentary,async_db,mode,expected,count):
    repository,operator,request,calls=documentary;calls.mode=mode
    with pytest.raises(doc.DocumentationError,match=expected):await repository.stage_audio_documentation(operator,request)
    assert len(calls)==count
    original=(await doc.owned_staging(ModelFabricRepository(),operator))[0]
    async with async_db() as db:assert (await db.get(Bundle,original['staged_ref'])).reserved_bytes==doc.RESERVED_BYTES
    reject=doc.RejectStagedDocumentationV1(action='reject_staged_documentation',staged_ref=original['staged_ref'],expected_staged_revision=original['revision'],expected_bundle_digest=original['bundle_digest'])
    with pytest.raises(doc.DocumentationError,match='cleanup_unknown'):
        await repository.reject_audio_documentation(operator,reject)
    async with async_db() as db:assert (await db.get(Bundle,original['staged_ref'])).reserved_bytes==doc.RESERVED_BYTES

def test_reject_ambiguous_nonfinite_metadata():
    for raw in [b'{"data":{},"data":{"is_management_key":true}}',b'{"price":NaN}']:
        with pytest.raises(doc.DocumentationError,match='json_invalid'):doc.strict_json(raw)

@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_loopback_is_not_metadata_authentication(documentary):
    from src.api.model_fabric_settings import audio_documentation_action
    from starlette.requests import Request
    from fastapi import HTTPException
    _,_,request,calls=documentary
    local=Request({'type':'http','method':'POST','path':'/api/settings/model-fabric/audio-documentation','client':('127.0.0.1',1),'headers':[]})
    with pytest.raises(HTTPException) as exc:await audio_documentation_action(request,local)
    assert exc.value.status_code==401 and calls==[]

def test_pre_capture_budget_and_calls_are_strict():
    from src.api.audio import OriginalAudioSelectionV1
    values={'action':'select_one_original_audio_call','conversation_session_id':'owned','audio_budget_microusd':1,'max_calls':1,'expected_audio_profile_hash':'a'*64,'documentation_attestation_ref':'original','expected_documentation_digest':'b'*64}
    for field,value in [('audio_budget_microusd',0),('audio_budget_microusd',True),('max_calls',True),('max_calls',2)]:
        with pytest.raises(ValidationError):OriginalAudioSelectionV1.model_validate({**values,field:value})

@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_original_selection_cannot_issue_grant_from_incomplete_sources(documentary,async_db):
    from src.api.audio import AudioConsentGrantBody,issue_audio_consent
    from src.db.models import AudioConsentGrant
    from starlette.requests import Request
    from fastapi import HTTPException
    _,operator,_,calls=documentary
    request=Request({'type':'http','method':'POST','path':'/api/audio/ptt/consent','client':('127.0.0.1',1),'headers':[]})
    request.state.operator=operator
    body=AudioConsentGrantBody.model_validate({'boundary':'cloud_upload','original_audio_selection':{'action':'select_one_original_audio_call','conversation_session_id':'owned','audio_budget_microusd':100,'max_calls':1,'expected_audio_profile_hash':'a'*64,'documentation_attestation_ref':'unissued','expected_documentation_digest':'b'*64}})
    with pytest.raises(HTTPException) as exc:await issue_audio_consent(body,request)
    assert exc.value.status_code==409 and exc.value.detail['code']=='audio_documentation_source_incomplete'
    async with async_db() as db:assert not (await db.execute(select(AudioConsentGrant))).scalars().all()
    assert calls==[]


@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('selection',['omitted','caller_fake','stale'])
@pytest.mark.parametrize('boundary',['model','cloud_upload'])
async def test_http_model_consent_never_issues_unbound_permission(client,documentary,async_db,monkeypatch,selection,boundary):
    from src.db.models import AudioConsentGrant, AudioIngressJob
    from src.guardian.audio_worker import default_audio_worker
    repository,operator,request,calls=documentary
    client.cookies.set(settings.operator_auth_cookie_name,calls.token)
    body={'boundary':boundary}
    if selection!='omitted':
        reference='caller-fake';expected='b'*64
        if selection=='stale':
            staged=await repository.stage_audio_documentation(operator,request)
            reference=staged['staged_ref'];expected='b'*64
        body['original_audio_selection']={'action':'select_one_original_audio_call','conversation_session_id':'owned',
            'audio_budget_microusd':100,'max_calls':1,'expected_audio_profile_hash':request.expected_audio_profile_hash,
            'documentation_attestation_ref':reference,'expected_documentation_digest':expected}
    before=len(calls)
    async def no_issue(*args,**kwargs):raise AssertionError('model permission issued before documentary authority')
    monkeypatch.setattr(default_audio_worker,'issue_consent_grant',no_issue)
    response=await client.post('/api/audio/ptt/consent',headers={'Origin':'http://localhost:3001'},json=body)
    assert response.status_code==(422 if selection=='omitted' else 409),response.text
    assert response.json()['detail']['code']==('audio_original_selection_required' if selection=='omitted' else 'audio_documentation_source_incomplete')
    async with async_db() as db:
        assert not (await db.execute(select(AudioConsentGrant))).scalars().all()
        assert not (await db.execute(select(AudioIngressJob))).scalars().all()
    assert len(calls)==before

@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_oversized_vault_cipher_never_resolves_credential(documentary,async_db):
    from src.db.models import Secret
    repository,operator,request,calls=documentary
    async with async_db() as db:
        secret=(await db.execute(select(Secret).where(Secret.key=='owned_management'))).scalars().one()
        secret.encrypted_value='x'*2000000;db.add(secret)
    with pytest.raises(doc.DocumentationError,match='credential_unavailable'):await repository.stage_audio_documentation(operator,request)
    assert calls==[]

@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_metadata_action_cannot_extend_legacy_schema(documentary):
    from tests.test_audio_model_fabric_v3 import legacy
    from src.model_fabric.configuration import read_model_fabric_configuration
    repository,operator,_,_=documentary
    configuration=replace(read_model_fabric_configuration(),openrouter_setup=legacy())
    with pytest.raises(doc.DocumentationError,match='audio_setup_v3_required'):
        await doc.select_metadata(repository,operator,configuration,doc.DisableMetadataCredential(action='disable'))

@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_real_cookie_settings_issuer_and_incomplete_grant_denial(client,documentary,async_db):
    from src.db.models import AudioConsentGrant
    repository,operator,request,calls=documentary
    client.cookies.set(settings.operator_auth_cookie_name,calls.token)
    origin={'Origin':'http://localhost:3001'}
    disabled=await client.put('/api/settings/model-fabric',headers=origin,json={'expected_policy_revision':2,'audio_metadata_access':{'action':'disable'}})
    assert disabled.status_code==200,disabled.text
    assert disabled.json()['audio_metadata_access'] is None
    selected=await client.put('/api/settings/model-fabric',headers=origin,json={'expected_policy_revision':3,'audio_metadata_access':{'action':'select_existing_management_credential','vault_key_name':'owned_management','expected_audio_profile_hash':request.expected_audio_profile_hash,'operation_scope':'selected_audio_profile_metadata'}})
    assert selected.status_code==200,selected.text
    value=selected.json()['audio_metadata_access']
    assert 'owned_management' not in json.dumps(value) and 'offline-management-key' not in json.dumps(value)
    acquire=await client.post('/api/settings/model-fabric/audio-documentation',headers=origin,json={'action':'acquire_selected_profile_metadata','expected_egress_revision':4,'expected_audio_profile_hash':request.expected_audio_profile_hash,'metadata_selection_ref':value['selection_ref'],'expected_metadata_selection_digest':value['selection_digest'],'official_supplement_catalog_id':doc.CATALOG})
    assert acquire.status_code==201,acquire.text
    staged=acquire.json()
    granted=await client.post('/api/audio/ptt/consent',headers=origin,json={'boundary':'cloud_upload','original_audio_selection':{'action':'select_one_original_audio_call','conversation_session_id':'owned','audio_budget_microusd':100,'max_calls':1,'expected_audio_profile_hash':request.expected_audio_profile_hash,'documentation_attestation_ref':staged['staged_ref'],'expected_documentation_digest':staged['bundle_digest']}})
    assert granted.status_code==409 and granted.json()['detail']['code']=='audio_documentation_source_incomplete'
    async with async_db() as db:assert not (await db.execute(select(AudioConsentGrant))).scalars().all()
    assert len(calls)==2

@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_metadata_key_cannot_become_current_inference_credential(documentary,monkeypatch):
    repository,operator,request,calls=documentary
    monkeypatch.setattr(settings,'openrouter_api_key','offline-management-key')
    with pytest.raises(doc.DocumentationError,match='inference_credential_forbidden'):
        await repository.stage_audio_documentation(operator,request)
    assert calls==[]

@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('address',['127.0.0.1','169.254.169.254','224.0.0.1'])
async def test_nonpublic_or_multicast_resolution_never_contacts(documentary,monkeypatch,address):
    repository,operator,request,calls=documentary
    async def resolve(*args,**kwargs):return [(2,1,6,'',(address,443))]
    monkeypatch.setattr(asyncio.get_running_loop(),'getaddrinfo',resolve)
    with pytest.raises(doc.DocumentationError,match='destination_invalid'):
        await repository.stage_audio_documentation(operator,request)
    assert calls==[]


@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('surface',['get','accept','reject','stage'])
@pytest.mark.parametrize('target,column',[
    ('bundle','binding_json'),('bundle','profile_hash'),('bundle','created_at'),
    ('bundle','expires_at'),('bundle','reserved_bytes'),
    ('source','source_url'),('source','acquired_at'),('source','size_bytes')])
@pytest.mark.parametrize('fault',['oversize','blob'])
async def test_http_documentary_corruption_never_materializes_or_renews(client,documentary,async_db,monkeypatch,surface,target,column,fault):
    repository,operator,request,calls=documentary
    staged=await repository.stage_audio_documentation(operator,request)
    async with async_db() as db:sources=(await db.execute(select(Source).order_by(Source.ordinal))).scalars().all()
    objects=Path(settings.workspace_dir)/'.model-fabric/audio-documentation/objects'
    before={source.object_id:(objects/source.object_id).read_bytes() for source in sources}
    table='model_audio_documentation_attestations' if target=='bundle' else 'model_audio_documentation_sources'
    identifier=staged['staged_ref'] if target=='bundle' else sources[0].id
    async with async_db() as db:
        await db.execute(text(f'UPDATE {table} SET {column}=:value WHERE id=:id'),
            {'value':'x'*1000000 if fault=='oversize' else b'corrupt','id':identifier})
        engine=db.bind.sync_engine
    def trap(connection,cursor,statement,parameters,context,executemany):
        if 'SELECT model_audio_documentation_attestations.id,' in statement or 'SELECT model_audio_documentation_sources.id,' in statement:
            raise AssertionError('corrupt documentary body materialized')
    def no_private_access(*args,**kwargs):raise AssertionError('corrupt documentary storage opened')
    monkeypatch.setattr(doc,'object_directory',no_private_access)
    event.listen(engine,'before_cursor_execute',trap)
    client.cookies.set(settings.operator_auth_cookie_name,calls.token)
    try:
        if surface=='get':response=await client.get('/api/settings/model-fabric/audio-documentation')
        else:
            body=request.model_dump() if surface=='stage' else {'action':('accept' if surface=='accept' else 'reject')+'_staged_documentation',
                'staged_ref':staged['staged_ref'],'expected_staged_revision':staged['revision'],'expected_bundle_digest':staged['bundle_digest']}
            response=await client.post('/api/settings/model-fabric/audio-documentation',headers={'Origin':'http://localhost:3001'},json=body)
    finally:event.remove(engine,'before_cursor_execute',trap)
    assert response.status_code==409,response.text
    assert response.json()['detail']['code']==('audio_documentation_quota_full' if surface=='stage' else 'audio_documentation_cleanup_unknown')
    assert len(calls)==2
    async with async_db() as db:
        assert (await db.execute(text('SELECT count(*) FROM model_audio_documentation_attestations'))).scalar_one()==1
        header=(await db.execute(text('SELECT revision,state,typeof(reserved_bytes),octet_length(reserved_bytes) FROM model_audio_documentation_attestations'))).one()
        assert header[:2]==(1,'staged')
        if target=='bundle' and column=='reserved_bytes':
            assert header[2:]==(('text',1000000) if fault=='oversize' else ('blob',7))
        else:assert (await db.execute(select(Bundle.reserved_bytes))).scalar_one()==doc.RESERVED_BYTES
    assert {name:(objects/name).read_bytes() for name in before}==before


@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_http_corrupt_manual_owner_and_revision_fences_precede_bodies(client,documentary,async_db,monkeypatch):
    repository,operator,request,calls=documentary
    staged=await repository.stage_audio_documentation(operator,request)
    async with async_db() as db:
        await db.execute(text('UPDATE model_audio_documentation_attestations SET binding_json=:value WHERE id=:id'),
            {'value':'x'*1000000,'id':staged['staged_ref']})
        engine=db.bind.sync_engine
    def trap(connection,cursor,statement,parameters,context,executemany):
        if 'SELECT model_audio_documentation_attestations.id,' in statement or 'SELECT model_audio_documentation_sources.id,' in statement:
            raise AssertionError('body loaded before owner/revision fence')
    def no_private_access(*args,**kwargs):raise AssertionError('private storage opened before owner/revision fence')
    monkeypatch.setattr(doc,'object_directory',no_private_access)
    token,_=await create_session()
    event.listen(engine,'before_cursor_execute',trap)
    try:
        client.cookies.set(settings.operator_auth_cookie_name,token)
        assert (await client.get('/api/settings/model-fabric/audio-documentation')).json()=={'staged':[]}
        for action in ['accept_staged_documentation','reject_staged_documentation']:
            body={'action':action,'staged_ref':staged['staged_ref'],'expected_staged_revision':staged['revision'],
                'expected_bundle_digest':staged['bundle_digest']}
            denied=await client.post('/api/settings/model-fabric/audio-documentation',headers={'Origin':'http://localhost:3001'},json=body)
            assert denied.status_code==404 and denied.json()['detail']['code']=='audio_documentation_not_found'
            client.cookies.set(settings.operator_auth_cookie_name,calls.token)
            stale=await client.post('/api/settings/model-fabric/audio-documentation',headers={'Origin':'http://localhost:3001'},
                json={**body,'expected_staged_revision':staged['revision']+1})
            assert stale.status_code==409 and stale.json()['detail']['code']=='audio_documentation_revision_changed'
            client.cookies.set(settings.operator_auth_cookie_name,token)
    finally:event.remove(engine,'before_cursor_execute',trap)
    async with async_db() as db:assert (await db.execute(select(Bundle.reserved_bytes))).scalar_one()==doc.RESERVED_BYTES
    assert len(calls)==2


@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('surface',['get','accept','reject','stage'])
async def test_http_extra_source_blocks_readback_review_and_new_stage(client,documentary,async_db,monkeypatch,surface):
    repository,operator,request,calls=documentary
    staged=await repository.stage_audio_documentation(operator,request)
    async with async_db() as db:
        sources=(await db.execute(select(Source))).scalars().all()
        db.add(Source(attestation_id=staged['staged_ref'],ordinal=2,source_id='extra',source_url='https://openrouter.ai',
            object_id='f'*32+'.source',sha256='a'*64,size_bytes=1))
        engine=db.bind.sync_engine
    objects=Path(settings.workspace_dir)/'.model-fabric/audio-documentation/objects'
    before={source.object_id:(objects/source.object_id).read_bytes() for source in sources}
    def trap(connection,cursor,statement,parameters,context,executemany):
        if 'SELECT model_audio_documentation_attestations.id,' in statement or 'SELECT model_audio_documentation_sources.id,' in statement:
            raise AssertionError('body loaded with extra Source rows')
    def no_private_access(*args,**kwargs):raise AssertionError('private storage opened with extra Source rows')
    monkeypatch.setattr(doc,'object_directory',no_private_access)
    client.cookies.set(settings.operator_auth_cookie_name,calls.token)
    event.listen(engine,'before_cursor_execute',trap)
    try:
        if surface=='get':response=await client.get('/api/settings/model-fabric/audio-documentation')
        else:
            body=request.model_dump() if surface=='stage' else {'action':('accept' if surface=='accept' else 'reject')+'_staged_documentation',
                'staged_ref':staged['staged_ref'],'expected_staged_revision':staged['revision'],'expected_bundle_digest':staged['bundle_digest']}
            response=await client.post('/api/settings/model-fabric/audio-documentation',headers={'Origin':'http://localhost:3001'},json=body)
    finally:event.remove(engine,'before_cursor_execute',trap)
    assert response.status_code==409,response.text
    assert response.json()['detail']['code']==('audio_documentation_quota_full' if surface=='stage' else 'audio_documentation_cleanup_unknown')
    async with async_db() as db:
        assert (await db.execute(select(Bundle.reserved_bytes))).scalar_one()==doc.RESERVED_BYTES
        assert (await db.execute(text('SELECT count(*) FROM model_audio_documentation_attestations'))).scalar_one()==1
    assert len(calls)==2 and {name:(objects/name).read_bytes() for name in before}==before


@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_stage_scalar_quota_admits_sixteenth_and_denies_seventeenth(documentary,async_db):
    repository,operator,request,calls=documentary
    # Capacity-only crash-state fixtures: initial rev-0 charged headers, not
    # documentary authority or completed acquisitions. No Sources are issued.
    binding={'inventory_schema':'audio-documentary-inventory.v1','selection_digest':request.expected_metadata_selection_digest,
        'model':'vendor/audio','endpoint_tag':'deepinfra/turbo','configuration_revision':2,'catalog_id':doc.CATALOG,'sources':[]}
    from datetime import datetime,timedelta,timezone
    async with async_db() as db:
        for index in range(15):
            db.add(Bundle(owner_principal_id=operator.principal.principal_id,original_root_id=f'{index%7:032x}',
                profile_hash=request.expected_audio_profile_hash,binding_json=doc.canonical(binding),bundle_digest=doc.digest(binding),
                expires_at=datetime.now(timezone.utc)+timedelta(minutes=10)))
        engine=db.bind.sync_engine
    def trap(connection,cursor,statement,parameters,context,executemany):
        if 'SELECT model_audio_documentation_attestations.id,' in statement:
            raise AssertionError('quota scan materialized full Bundle bodies')
    event.listen(engine,'before_cursor_execute',trap)
    try:staged=await repository.stage_audio_documentation(operator,request)
    finally:event.remove(engine,'before_cursor_execute',trap)
    assert staged['revision']==1 and len(calls)==2
    with pytest.raises(doc.DocumentationError,match='quota_full'):await repository.stage_audio_documentation(operator,request)
    async with async_db() as db:
        assert (await db.execute(text('SELECT count(*) FROM model_audio_documentation_attestations'))).scalar_one()==16
        assert (await db.execute(text('SELECT sum(reserved_bytes) FROM model_audio_documentation_attestations'))).scalar_one()==16*doc.RESERVED_BYTES
    assert len(calls)==2


@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_http_known_manual_readback_rejection_and_original_idempotence(client,documentary,async_db):
    repository,operator,request,calls=documentary
    staged=await repository.stage_audio_documentation(operator,request)
    client.cookies.set(settings.operator_auth_cookie_name,calls.token)
    readback=await client.get('/api/settings/model-fabric/audio-documentation')
    assert readback.status_code==200 and readback.json()=={'staged':[staged]}
    body={'staged_ref':staged['staged_ref'],'expected_staged_revision':staged['revision'],'expected_bundle_digest':staged['bundle_digest']}
    accepted=await client.post('/api/settings/model-fabric/audio-documentation',headers={'Origin':'http://localhost:3001'},
        json={**body,'action':'accept_staged_documentation'})
    assert accepted.status_code==409 and accepted.json()['detail']['code']=='audio_documentation_source_incomplete'
    rejected=await client.post('/api/settings/model-fabric/audio-documentation',headers={'Origin':'http://localhost:3001'},
        json={**body,'action':'reject_staged_documentation'})
    assert rejected.status_code==200 and rejected.json()['state']=='rejected'
    assert rejected.json()['revision']==2 and rejected.json()['bundle_digest']==staged['bundle_digest']
    repeated=await client.post('/api/settings/model-fabric/audio-documentation',headers={'Origin':'http://localhost:3001'},
        json={**body,'action':'reject_staged_documentation','expected_staged_revision':2})
    assert repeated.status_code==200 and repeated.json()==rejected.json()
    assert (await client.get('/api/settings/model-fabric/audio-documentation')).json()=={'staged':[]}
    async with async_db() as db:assert (await db.execute(select(Bundle.reserved_bytes))).scalar_one()==0
    assert not list((Path(settings.workspace_dir)/'.model-fabric/audio-documentation/objects').iterdir())
    assert len(calls)==2


@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('target',['bundle','source'])
async def test_http_storage_changed_after_preflight_is_not_returned_by_body_select(client,documentary,async_db,monkeypatch,target):
    from sqlalchemy.ext.asyncio import AsyncSession
    repository,operator,request,calls=documentary
    staged=await repository.stage_audio_documentation(operator,request)
    async with async_db() as db:sources=(await db.execute(select(Source).order_by(Source.ordinal))).scalars().all()
    original_execute=AsyncSession.execute
    changed=False
    async def changed_before_body(db,statement,*args,**kwargs):
        nonlocal changed
        prefix='SELECT model_audio_documentation_'+('attestations' if target=='bundle' else 'sources')+'.id,'
        if not changed and prefix in str(statement):
            changed=True
            table='model_audio_documentation_attestations' if target=='bundle' else 'model_audio_documentation_sources'
            column='binding_json' if target=='bundle' else 'source_url'
            identifier=staged['staged_ref'] if target=='bundle' else sources[0].id
            await original_execute(db,text(f'UPDATE {table} SET {column}=:value WHERE id=:id'),
                {'value':'x'*1000000,'id':identifier})
            result=await original_execute(db,statement,*args,**kwargs)
            frozen=result.freeze()
            rows=frozen().scalars().all()
            # Assert actual SQL readback excluded the just-corrupted body;
            # checking an eventual 409 alone would miss materialization.
            if target=='bundle':assert rows==[]
            else:assert len(rows)==1 and rows[0].id==sources[1].id
            return frozen()
        return await original_execute(db,statement,*args,**kwargs)
    def no_private_access(*args,**kwargs):raise AssertionError('changed storage opened private objects')
    monkeypatch.setattr(AsyncSession,'execute',changed_before_body)
    monkeypatch.setattr(doc,'object_directory',no_private_access)
    client.cookies.set(settings.operator_auth_cookie_name,calls.token)
    if target=='bundle':response=await client.get('/api/settings/model-fabric/audio-documentation')
    else:
        response=await client.post('/api/settings/model-fabric/audio-documentation',headers={'Origin':'http://localhost:3001'},
            json={'action':'accept_staged_documentation','staged_ref':staged['staged_ref'],
                'expected_staged_revision':staged['revision'],'expected_bundle_digest':staged['bundle_digest']})
    assert changed and response.status_code==409,response.text
    assert response.json()['detail']['code']=='audio_documentation_cleanup_unknown'
    async with async_db() as db:assert (await db.execute(select(Bundle.reserved_bytes))).scalar_one()==doc.RESERVED_BYTES
    assert len(calls)==2


@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_http_accept_refreshes_owner_changed_between_bounded_body_reads(client,documentary,async_db,monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession
    repository,operator,request,calls=documentary
    staged=await repository.stage_audio_documentation(operator,request)
    _,other=await create_session()
    execute=AsyncSession.execute
    bodies=0
    async def change_owner_on_second_body(db,statement,*args,**kwargs):
        nonlocal bodies
        if 'SELECT model_audio_documentation_attestations.id,' in str(statement):
            bodies+=1
            if bodies==2:
                await execute(db,text('UPDATE model_audio_documentation_attestations SET owner_principal_id=:owner,original_root_id=:root WHERE id=:id'),
                    {'owner':other.principal.principal_id,'root':other.session_id,'id':staged['staged_ref']})
        return await execute(db,statement,*args,**kwargs)
    def no_private_access(*args,**kwargs):raise AssertionError('stale ORM owner opened private objects')
    monkeypatch.setattr(AsyncSession,'execute',change_owner_on_second_body)
    monkeypatch.setattr(doc,'object_directory',no_private_access)
    client.cookies.set(settings.operator_auth_cookie_name,calls.token)
    response=await client.post('/api/settings/model-fabric/audio-documentation',headers={'Origin':'http://localhost:3001'},
        json={'action':'accept_staged_documentation','staged_ref':staged['staged_ref'],
            'expected_staged_revision':staged['revision'],'expected_bundle_digest':staged['bundle_digest']})
    assert bodies==2 and response.status_code==404,response.text
    assert response.json()['detail']['code']=='audio_documentation_not_found'
    async with async_db() as db:assert (await db.execute(select(Bundle.reserved_bytes))).scalar_one()==doc.RESERVED_BYTES
    assert len(calls)==2


@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('fault',['wrong_revision','blob_revision','wrong_error','null_error','oversize_body','blob_body','blob_digest'])
async def test_http_noncanonical_rejected_zero_remains_visible_and_blocks_stage(client,documentary,async_db,monkeypatch,fault):
    repository,operator,request,calls=documentary
    staged=await repository.stage_audio_documentation(operator,request)
    async with async_db() as db:sources=(await db.execute(select(Source))).scalars().all()
    objects=Path(settings.workspace_dir)/'.model-fabric/audio-documentation/objects'
    before_files={source.object_id:(objects/source.object_id).read_bytes() for source in sources}
    changes={'wrong_revision':('revision',1),'blob_revision':('revision',b'corrupt'),
        'wrong_error':('error_code','audio_documentation_acquisition_incomplete'),'null_error':('error_code',None),
        'oversize_body':('binding_json','x'*1000000),'blob_body':('binding_json',b'corrupt'),'blob_digest':('bundle_digest',b'corrupt')}
    column,value=changes[fault]
    snapshot=text("SELECT typeof(revision),CASE WHEN typeof(revision)='integer' THEN revision END,state,reserved_bytes,"
        "error_code,typeof(binding_json),octet_length(binding_json),typeof(bundle_digest),octet_length(bundle_digest) "
        "FROM model_audio_documentation_attestations")
    async with async_db() as db:
        await db.execute(text("UPDATE model_audio_documentation_attestations SET revision=2,state='rejected',reserved_bytes=0 WHERE id=:id"),
            {'id':staged['staged_ref']})
        await db.execute(text(f'UPDATE model_audio_documentation_attestations SET {column}=:value WHERE id=:id'),
            {'value':value,'id':staged['staged_ref']})
        before_row=(await db.execute(snapshot)).one()
        engine=db.bind.sync_engine
    def trap(connection,cursor,statement,parameters,context,executemany):
        if 'SELECT model_audio_documentation_attestations.id,' in statement or 'SELECT model_audio_documentation_sources.id,' in statement:
            raise AssertionError('noncanonical zero marker materialized a documentary body')
    def no_private_access(*args,**kwargs):raise AssertionError('noncanonical zero marker opened private storage')
    monkeypatch.setattr(doc,'object_directory',no_private_access)
    client.cookies.set(settings.operator_auth_cookie_name,calls.token)
    event.listen(engine,'before_cursor_execute',trap)
    try:
        readback=await client.get('/api/settings/model-fabric/audio-documentation')
        assert readback.status_code==409,readback.text
        assert readback.json()['detail']['code']=='audio_documentation_cleanup_unknown'
        renewed=await client.post('/api/settings/model-fabric/audio-documentation',headers={'Origin':'http://localhost:3001'},json=request.model_dump())
        assert renewed.status_code==409,renewed.text
        assert renewed.json()['detail']['code']=='audio_documentation_quota_full'
    finally:event.remove(engine,'before_cursor_execute',trap)
    async with async_db() as db:
        assert (await db.execute(snapshot)).one()==before_row
        assert (await db.execute(text('SELECT count(*) FROM model_audio_documentation_attestations'))).scalar_one()==1
    assert len(calls)==2 and {name:(objects/name).read_bytes() for name in before_files}==before_files


@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_http_actual_final_cas_zero_is_omitted_idempotent_and_frees_stage_slot(client,documentary,async_db):
    repository,operator,request,calls=documentary
    staged=await repository.stage_audio_documentation(operator,request)
    client.cookies.set(settings.operator_auth_cookie_name,calls.token)
    body={'action':'reject_staged_documentation','staged_ref':staged['staged_ref'],
        'expected_staged_revision':staged['revision'],'expected_bundle_digest':staged['bundle_digest']}
    rejected=await client.post('/api/settings/model-fabric/audio-documentation',headers={'Origin':'http://localhost:3001'},json=body)
    assert rejected.status_code==200 and rejected.json()['revision']==2
    assert (await client.get('/api/settings/model-fabric/audio-documentation')).json()=={'staged':[]}
    repeated=await client.post('/api/settings/model-fabric/audio-documentation',headers={'Origin':'http://localhost:3001'},
        json={**body,'expected_staged_revision':2})
    assert repeated.status_code==200 and repeated.json()==rejected.json()
    async with async_db() as db:
        original=await db.get(Bundle,staged['staged_ref'])
        assert original.revision==2 and original.state=='rejected' and original.reserved_bytes==0
        assert original.error_code=='audio_documentation_source_incomplete'
    assert not list((Path(settings.workspace_dir)/'.model-fabric/audio-documentation/objects').iterdir())
    renewed=await client.post('/api/settings/model-fabric/audio-documentation',headers={'Origin':'http://localhost:3001'},json=request.model_dump())
    assert renewed.status_code==201,renewed.text
    assert renewed.json()['staged_ref']!=staged['staged_ref'] and not renewed.json()['ready']
    assert len(calls)==4
