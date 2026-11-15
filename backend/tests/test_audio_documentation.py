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

# ADR-035 retired OpenRouter by policy: audio documentation metadata reads are
# refused (tests/test_openrouter_retirement.py). This module pins the historical
# documentary mechanics behind the explicit legacy fixture.
pytestmark = pytest.mark.openrouter_legacy

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
    result=await doc.stage(repository,operator,request)
    assert result['state']=='staged' and not result['ready'] and not result['admission_ready']
    assert [r.url.path for r in calls]==['/api/v1/key','/api/v1/models/vendor/audio/endpoints']
    assert 'offline-management-key' not in json.dumps(result) and 'owned_management' not in json.dumps(result)
    async with async_db() as db:sources=(await db.execute(select(Source))).scalars().all()
    assert len(sources)==2
    for source in sources:assert hashlib.sha256(doc.private_read(source)).hexdigest()==source.sha256
    restarted=ModelFabricRepository();assert (await doc.owned_staging(restarted,operator))[0]==result
    accept=doc.AcceptStagedDocumentationV1(action='accept_staged_documentation',staged_ref=result['staged_ref'],expected_staged_revision=result['revision'],expected_bundle_digest=result['bundle_digest'])
    with pytest.raises(doc.DocumentationError,match='source_incomplete'):await doc.review(restarted,operator,accept)
    with pytest.raises(doc.DocumentationError,match='quota_full'):await doc.stage(restarted,operator,request)
    assert len(calls)==2
    reject=doc.RejectStagedDocumentationV1(action='reject_staged_documentation',**accept.model_dump(exclude={'action'}))
    assert (await doc.review(restarted,operator,reject))['state']=='rejected'
    assert not list((Path(settings.workspace_dir)/'.model-fabric/audio-documentation/objects').iterdir())
    async with async_db() as db:assert (await db.get(Bundle,result['staged_ref'])).reserved_bytes==0

@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_live_root_rotation_before_contact(documentary):
    repository,operator,request,calls=documentary
    with pytest.raises(doc.DocumentationError,match='authentication_required'):await doc.stage(repository,None,request)
    assert calls==[]
    await vault_repository.store('owned_management','rotated-key',owner_principal_id=operator.principal.principal_id)
    with pytest.raises(doc.DocumentationError,match='credential_changed'):await doc.stage(repository,operator,request)
    assert calls==[]
    await revoke_session(operator.session_id)
    with pytest.raises(doc.DocumentationError,match='root_inactive'):await doc.stage(repository,operator,request)
    assert calls==[]

@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_tamper_and_stale_revision(documentary,async_db):
    repository,operator,request,calls=documentary;result=await doc.stage(repository,operator,request)
    action=doc.AcceptStagedDocumentationV1(action='accept_staged_documentation',staged_ref=result['staged_ref'],expected_staged_revision=result['revision']+1,expected_bundle_digest=result['bundle_digest'])
    with pytest.raises(doc.DocumentationError,match='revision_changed'):await doc.review(repository,operator,action)
    async with async_db() as db:source=(await db.execute(select(Source))).scalars().first()
    (Path(settings.workspace_dir)/'.model-fabric/audio-documentation/objects'/source.object_id).write_bytes(b'changed')
    with pytest.raises(doc.DocumentationError,match='private_object_invalid'):await doc.review(repository,operator,action.model_copy(update={'expected_staged_revision':result['revision']}))
    assert len(calls)==2

def test_closed_review_and_scope():
    with pytest.raises(ValidationError):doc.AcceptStagedDocumentationV1.model_validate({'action':True})
    with pytest.raises(ValidationError):doc.SelectMetadataCredential.model_validate({'action':'select_existing_management_credential','vault_key_name':'owned','expected_audio_profile_hash':'a'*64,'operation_scope':'inference','approved':True})

@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('mode,expected,count',[('normal-key','management_key_required',1),('wrong-endpoint','exact_endpoint_missing',2),('duplicate-endpoint','exact_endpoint_missing',2),('oversize','response_oversized',1)])
async def test_unusable_source_stays_charged_owned_rejection(documentary,async_db,mode,expected,count):
    repository,operator,request,calls=documentary;calls.mode=mode
    with pytest.raises(doc.DocumentationError,match=expected):await doc.stage(repository,operator,request)
    assert len(calls)==count
    original=(await doc.owned_staging(ModelFabricRepository(),operator))[0]
    async with async_db() as db:assert (await db.get(Bundle,original['staged_ref'])).reserved_bytes==doc.RESERVED_BYTES
    reject=doc.RejectStagedDocumentationV1(action='reject_staged_documentation',staged_ref=original['staged_ref'],expected_staged_revision=original['revision'],expected_bundle_digest=original['bundle_digest'])
    with pytest.raises(doc.DocumentationError,match='cleanup_unknown'):
        await doc.review(repository,operator,reject)
    async with async_db() as db:assert (await db.get(Bundle,original['staged_ref'])).reserved_bytes==doc.RESERVED_BYTES

def test_reject_ambiguous_nonfinite_metadata():
    for raw in [b'{"data":{},"data":{"is_management_key":true}}',b'{"price":NaN}']:
        with pytest.raises(doc.DocumentationError,match='json_invalid'):doc.strict_json(raw)

@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_oversized_vault_cipher_never_resolves_credential(documentary,async_db):
    from src.db.models import Secret
    repository,operator,request,calls=documentary
    async with async_db() as db:
        secret=(await db.execute(select(Secret).where(Secret.key=='owned_management'))).scalars().one()
        secret.encrypted_value='x'*2000000;db.add(secret)
    with pytest.raises(doc.DocumentationError,match='credential_unavailable'):await doc.stage(repository,operator,request)
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
async def test_metadata_key_cannot_become_current_inference_credential(documentary,monkeypatch):
    repository,operator,request,calls=documentary
    monkeypatch.setattr(settings,'openrouter_api_key','offline-management-key')
    with pytest.raises(doc.DocumentationError,match='inference_credential_forbidden'):
        await doc.stage(repository,operator,request)
    assert calls==[]

@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('address',['127.0.0.1','169.254.169.254','224.0.0.1'])
async def test_nonpublic_or_multicast_resolution_never_contacts(documentary,monkeypatch,address):
    repository,operator,request,calls=documentary
    async def resolve(*args,**kwargs):return [(2,1,6,'',(address,443))]
    monkeypatch.setattr(asyncio.get_running_loop(),'getaddrinfo',resolve)
    with pytest.raises(doc.DocumentationError,match='destination_invalid'):
        await doc.stage(repository,operator,request)
    assert calls==[]

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
    try:staged=await doc.stage(repository,operator,request)
    finally:event.remove(engine,'before_cursor_execute',trap)
    assert staged['revision']==1 and len(calls)==2
    with pytest.raises(doc.DocumentationError,match='quota_full'):await doc.stage(repository,operator,request)
    async with async_db() as db:
        assert (await db.execute(text('SELECT count(*) FROM model_audio_documentation_attestations'))).scalar_one()==16
        assert (await db.execute(text('SELECT sum(reserved_bytes) FROM model_audio_documentation_attestations'))).scalar_one()==16*doc.RESERVED_BYTES
    assert len(calls)==2
