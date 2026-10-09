"""Real producer/SQLite/private files; process races only schedule existing code."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
import os
from pathlib import Path

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlmodel import select

from config.settings import settings
from src.db.models import OperatorSession
from src.model_fabric.repository import ModelFabricRepository
from tests.test_audio_documentation import documentary, Bundle, Source, doc


async def settled(documentary, async_db):
    repository,operator,request,calls=documentary
    result=await repository.stage_audio_documentation(operator,request)
    async with async_db() as db:
        row=await db.get(Bundle,result['staged_ref'])
        sources=(await db.execute(select(Source).order_by(Source.ordinal))).scalars().all()
    return repository,operator,row,sources


@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_post_actual_commit_cancellation_preserves_success(documentary,async_db):
    _,operator,request,_=documentary
    raised=False
    @asynccontextmanager
    async def provider():
        nonlocal raised
        success=False
        async with async_db() as db:
            yield db
            success=(await db.execute(select(Bundle.id).where(Bundle.revision==1))).first() is not None
        if success and not raised:
            raised=True
            raise asyncio.CancelledError('after actual SQLite success commit')
    with pytest.raises(asyncio.CancelledError):
        await ModelFabricRepository(provider).stage_audio_documentation(operator,request)
    async with async_db() as db: row=(await db.execute(select(Bundle))).scalars().one()
    assert raised and row.revision==1 and row.error_code=='audio_documentation_source_incomplete'
    assert row.reserved_bytes==doc.RESERVED_BYTES
    assert len(json.loads(row.binding_json)['sources'])==2


@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_cancelled_unsettled_acquisition_remains_charged(documentary,async_db,monkeypatch):
    repository,operator,request,_=documentary
    client=httpx.AsyncClient();constructor=type(client);await client.aclose()
    def respond(request):
        if request.url.path.endswith('/endpoints'): raise asyncio.CancelledError('scripted final HTTP cancellation')
        return httpx.Response(200,headers={'content-type':'application/json'},
            stream=httpx.ByteStream(b'{"data":{"is_management_key":true}}'))
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:constructor(**kwargs,transport=httpx.MockTransport(respond)))
    with pytest.raises(asyncio.CancelledError): await repository.stage_audio_documentation(operator,request)
    async with async_db() as db:
        row=(await db.execute(select(Bundle))).scalars().one()
        source=(await db.execute(select(Source))).scalars().one()
    assert row.revision==0 and row.error_code=='audio_documentation_acquisition_incomplete'
    assert (await repository.cleanup_settled_audio_documentation())=={'inspected':1,'deleted':0,'unknown':1}
    action=doc.RejectStagedDocumentationV1(action='reject_staged_documentation',staged_ref=row.id,
        expected_staged_revision=0,expected_bundle_digest=row.bundle_digest)
    with pytest.raises(doc.DocumentationError,match='cleanup_unknown'):
        await repository.reject_audio_documentation(operator,action)
    assert len(doc.private_read(source))==source.size_bytes


@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('reason',['expiry','revoked','active'])
async def test_autonomous_settled_cleanup(documentary,async_db,reason):
    repository,operator,row,sources=await settled(documentary,async_db)
    async with async_db() as db:
        if reason=='expiry': await db.execute(update(Bundle).where(Bundle.id==row.id).values(expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
        if reason=='revoked': await db.execute(update(OperatorSession).where(OperatorSession.id==operator.session_id).values(revoked_at=datetime.now(timezone.utc)))
    result=await repository.cleanup_settled_audio_documentation()
    assert result=={'inspected':1,'deleted':int(reason!='active'),'unknown':0}
    async with async_db() as db: current=await db.get(Bundle,row.id)
    assert current.reserved_bytes==(doc.RESERVED_BYTES if reason=='active' else 0)
    assert all((Path(settings.workspace_dir)/'.model-fabric/audio-documentation/objects'/s.object_id).exists()==(reason=='active') for s in sources)


@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('fault',['missing','extra','substituted','legacy','digest','reordered','mode','symlink','hash','failed-deleting','accepted','inflight'])
async def test_invalid_inventory_or_physical_state_stays_charged(documentary,async_db,fault):
    repository,_,row,sources=await settled(documentary,async_db)
    objects=Path(settings.workspace_dir)/'.model-fabric/audio-documentation/objects'
    async with async_db() as db:
        current=await db.get(Bundle,row.id);current.expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)
        if fault=='missing': await db.delete(await db.get(Source,sources[0].id))
        if fault=='extra': db.add(Source(attestation_id=row.id,ordinal=2,source_id='extra',source_url='https://openrouter.ai',object_id='f'*32+'.source',sha256='a'*64,size_bytes=1))
        if fault=='substituted':
            source=await db.get(Source,sources[0].id);source.object_id='f'*32+'.source';db.add(source)
        if fault=='legacy':
            binding=json.loads(current.binding_json);del binding['inventory_schema'];current.binding_json=doc.canonical(binding);current.bundle_digest=doc.digest(binding)
        if fault=='digest': current.bundle_digest='a'*64
        if fault=='reordered':
            binding=json.loads(current.binding_json);binding['sources'].reverse();current.binding_json=doc.canonical(binding);current.bundle_digest=doc.digest(binding)
        if fault=='failed-deleting': current.state='deleting';current.error_code='audio_documentation_acquisition_incomplete'
        if fault=='accepted': current.state='accepted'
        if fault=='inflight': current.revision=0;current.error_code=None
        db.add(current)
    target=objects/sources[0].object_id
    if fault=='mode': target.chmod(0o644)
    if fault=='symlink': target.unlink();target.symlink_to(objects/sources[1].object_id)
    if fault=='hash': target.write_bytes(b'x'*sources[0].size_bytes)
    result=await repository.cleanup_settled_audio_documentation()
    assert result=={'inspected':1,'deleted':0,'unknown':1}
    async with async_db() as db: assert (await db.get(Bundle,row.id)).reserved_bytes==doc.RESERVED_BYTES
    assert (objects/sources[1].object_id).exists()


@pytest.mark.parametrize('async_db',['file'],indirect=True)
async def test_deleting_positive_absence_resumes(documentary,async_db):
    repository,_,row,sources=await settled(documentary,async_db)
    async with async_db() as db: await db.execute(update(Bundle).where(Bundle.id==row.id).values(state='deleting'))
    (Path(settings.workspace_dir)/'.model-fabric/audio-documentation/objects'/sources[0].object_id).unlink()
    assert (await repository.cleanup_settled_audio_documentation())['deleted']==1


def process_cleaner(database_url,workspace,values,mode,barrier,results):
    settings.workspace_dir=workspace
    async def run():
        engine=create_async_engine(database_url,connect_args={'timeout':10})
        factory=async_sessionmaker(engine,expire_on_commit=False)
        sessions=0;physical=0;unlinks=0;final_cas=[]
        original_remove=doc.remove_original_sources;original_unlink=doc.os.unlink
        @asynccontextmanager
        async def provider():
            nonlocal sessions
            sessions+=1
            if (mode=='claim' and sessions==1) or (mode=='final' and sessions==2): barrier.wait(10)
            async with factory() as db:
                execute=db.execute
                async def observed(statement,*args,**kwargs):
                    result=await execute(statement,*args,**kwargs)
                    if str(statement).startswith('UPDATE model_audio_documentation_attestations') and statement.compile().params.get('state')=='rejected':
                        final_cas.append(result.rowcount)
                    return result
                db.execute=observed
                try:
                    yield db
                    await db.commit()
                except BaseException:
                    await db.rollback();raise
        def remove(sources):
            nonlocal physical
            physical+=1;return original_remove(sources)
        def unlink(*args,**kwargs):
            nonlocal unlinks
            unlinks+=1
            if mode=='continuation' and unlinks==1: barrier.wait(10)
            return original_unlink(*args,**kwargs)
        doc.remove_original_sources=remove;doc.os.unlink=unlink
        try:
            deleted=await doc.delete_successful(ModelFabricRepository(provider),Bundle.model_validate(values),automatic=False)
            results.put({'pid':os.getpid(),'deleted':int(deleted),'unknown':int(not deleted),'physical':physical,'unlinks':unlinks,'final_cas_rowcounts':final_cas})
        finally: await engine.dispose()
    try: asyncio.run(run())
    except BaseException as exc: results.put({'pid':os.getpid(),'error':type(exc).__name__})


@pytest.mark.parametrize('async_db',['file'],indirect=True)
@pytest.mark.parametrize('mode',['claim','continuation','final'])
async def test_real_two_process_cleanup_interleavings(documentary,async_db,tmp_path,mode):
    _,_,row,sources=await settled(documentary,async_db)
    before={'row_id':row.id,'revision':row.revision,'state':row.state,'reserved_bytes':row.reserved_bytes,
        'bundle_digest':row.bundle_digest,'inventory':json.loads(row.binding_json)['sources'],
        'physical':[{'object_id':source.object_id,'actual_size':len(doc.private_read(source)),
            'actual_sha256':doc.hashlib.sha256(doc.private_read(source)).hexdigest()} for source in sources]}
    if mode!='claim':
        async with async_db() as db: await db.execute(update(Bundle).where(Bundle.id==row.id).values(state='deleting'))
        row.state='deleting'
    async with async_db() as db: url=str(db.bind.url)
    context=multiprocessing.get_context('spawn');barrier=context.Barrier(2);results=context.Queue()
    children=[context.Process(target=process_cleaner,args=(url,str(settings.workspace_dir),row.model_dump(),mode,barrier,results)) for _ in range(2)]
    for child in children: child.start()
    try:
        for child in children:
            await asyncio.to_thread(child.join,15)
            if child.is_alive(): pytest.fail('bounded cleaner process timed out')
            assert child.exitcode==0
    finally:
        for child in children:
            if child.is_alive(): child.kill()
            child.join(3)
    receipts=[results.get(timeout=3) for _ in children]
    results.close();results.join_thread()
    assert len({r['pid'] for r in receipts})==2 and all('error' not in r for r in receipts),receipts
    assert sum(r['deleted'] for r in receipts)==1 and sum(r['unknown'] for r in receipts)==1,receipts
    assert [count for r in receipts for count in r['final_cas_rowcounts']]==[1]
    if mode=='claim': assert next(r for r in receipts if r['unknown'])['physical']==0
    async with async_db() as db:
        current=await db.get(Bundle,row.id)
        assert current.state=='rejected' and current.revision==2 and current.reserved_bytes==0
    assert not any((Path(settings.workspace_dir)/'.model-fabric/audio-documentation/objects'/s.object_id).exists() for s in sources)
    after={'state':current.state,'revision':current.revision,'reserved_bytes':current.reserved_bytes,
        'original_objects_absent':True}
    (tmp_path/f'actual-process-{mode}.json').write_text(json.dumps({'before':before,'processes':receipts,
        'after':after,'final_rule':'one committed CAS winner; loser fails current witness before stale UPDATE'},indent=2))
