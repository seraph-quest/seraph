"""Actual SQLite/Vault/auth owner; final metadata HTTP only is scripted."""
from dataclasses import replace
import asyncio,json,hashlib
from pathlib import Path
import httpx,pytest
from pydantic import ValidationError
from sqlmodel import select
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
