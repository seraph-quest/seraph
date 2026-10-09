"""Authenticated documentary metadata, never empirical audio availability.

No complete official codec/context/template/charge catalog is currently known.
Consequently retained metadata can be reviewed/rejected but cannot be accepted
as execution authority. This is an explicit source-completeness boundary.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
from typing import Literal, Annotated
from uuid import uuid4

import httpx
from cryptography.fernet import InvalidToken
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text, update
from sqlmodel import select
from config.settings import settings
from src.db.models import (OperatorSession, ModelAudioDocumentationAttestationRecord as Bundle,
    ModelAudioDocumentationSourceRecord as Source)
from src.workspace.state_registry import canonical_workspace_root

SHA = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
MISSING_FACTS = ('exact_codec_container_media_type', 'audio_context_rounding_and_template',
    'complete_charge_inventory_minimum_rounding_cache', 'exact_endpoint_zdr_applicability')
CATALOG = 'openrouter-audio-guide.v1'
RESERVED_BYTES = 16384 + 2 * 262144
# Fixed storage shapes for this documentary producer only. Partial acquisition
# and rejected readback are bounded too; they never become cleanup authority.
BUNDLE_STORAGE = ("typeof(id)='text' AND octet_length(id)=32 "
    "AND typeof(revision)='integer' AND revision BETWEEN 0 AND 2 "
    "AND typeof(reserved_bytes)='integer' AND reserved_bytes IN (0,540672) "
    "AND typeof(owner_principal_id)='text' AND octet_length(owner_principal_id) BETWEEN 1 AND 128 "
    "AND typeof(original_root_id)='text' AND octet_length(original_root_id) BETWEEN 1 AND 128 "
    "AND typeof(profile_hash)='text' AND octet_length(profile_hash)=64 "
    "AND typeof(state)='text' AND state IN ('staged','deleting','rejected','accepted') "
    "AND typeof(binding_json)='text' AND octet_length(binding_json) BETWEEN 1 AND 16384 "
    "AND typeof(bundle_digest)='text' AND octet_length(bundle_digest)=64 "
    "AND (error_code IS NULL OR (typeof(error_code)='text' AND octet_length(error_code) BETWEEN 1 AND 64)) "
    "AND typeof(created_at)='text' AND octet_length(created_at) BETWEEN 1 AND 32 "
    "AND typeof(expires_at)='text' AND octet_length(expires_at) BETWEEN 1 AND 32 "
    "AND ((revision=0 AND state='staged' AND reserved_bytes=540672 AND (error_code IS NULL OR error_code='audio_documentation_acquisition_incomplete')) "
    "OR (revision=1 AND state IN ('staged','deleting') AND reserved_bytes=540672 AND error_code='audio_documentation_source_incomplete') "
    "OR (revision=2 AND state='rejected' AND reserved_bytes=0 AND error_code='audio_documentation_source_incomplete'))")
SOURCE_STORAGE = ("typeof(id)='text' AND octet_length(id)=32 "
    "AND typeof(attestation_id)='text' AND octet_length(attestation_id)=32 "
    "AND typeof(source_id)='text' AND octet_length(source_id) BETWEEN 1 AND 15 "
    "AND typeof(source_url)='text' AND octet_length(source_url) BETWEEN 1 AND 320 "
    "AND typeof(object_id)='text' AND octet_length(object_id)=39 "
    "AND typeof(sha256)='text' AND octet_length(sha256)=64 "
    "AND typeof(acquired_at)='text' AND octet_length(acquired_at) BETWEEN 1 AND 32 "
    "AND typeof(ordinal)='integer' AND ordinal IN (0,1) "
    "AND typeof(size_bytes)='integer' AND size_bytes BETWEEN 1 AND 262144")
OPEN_RESERVATION = "NOT (typeof(reserved_bytes)='integer' AND reserved_bytes=0 AND state='rejected')"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class DocumentationError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


class Closed(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)


class SelectMetadataCredential(Closed):
    action: Literal['select_existing_management_credential']
    vault_key_name: str = Field(min_length=1, max_length=128)
    expected_audio_profile_hash: SHA
    operation_scope: Literal['selected_audio_profile_metadata']


class DisableMetadataCredential(Closed):
    action: Literal['disable']


MetadataSelection = Annotated[SelectMetadataCredential | DisableMetadataCredential, Field(discriminator='action')]


class AudioMetadataAccessV1(Closed):
    schema_version: Literal['audio-metadata-access.v1']
    selection_id: str
    owner_principal_id: str
    original_root_id: str
    profile_hash: SHA
    egress_revision: int = Field(ge=1)
    vault_key_name: str = Field(min_length=1, max_length=128)
    vault_binding_digest: SHA
    vault_identity: dict
    credential_class: Literal['openrouter_management']
    operation_scope: Literal['selected_audio_profile_metadata']
    configured_at: str
    expires_at: str


class AcquireSelectedProfileMetadataV1(Closed):
    action: Literal['acquire_selected_profile_metadata']
    expected_egress_revision: int = Field(ge=1)
    expected_audio_profile_hash: SHA
    metadata_selection_ref: str = Field(min_length=1, max_length=128)
    expected_metadata_selection_digest: SHA
    official_supplement_catalog_id: Literal['openrouter-audio-guide.v1']


class AcceptStagedDocumentationV1(Closed):
    action: Literal['accept_staged_documentation']
    staged_ref: str = Field(min_length=1, max_length=128)
    expected_staged_revision: int = Field(ge=0)
    expected_bundle_digest: SHA


class RejectStagedDocumentationV1(Closed):
    action: Literal['reject_staged_documentation']
    staged_ref: str = Field(min_length=1, max_length=128)
    expected_staged_revision: int = Field(ge=0)
    expected_bundle_digest: SHA


DocumentationAction = Annotated[AcquireSelectedProfileMetadataV1 | AcceptStagedDocumentationV1 |
    RejectStagedDocumentationV1, Field(discriminator='action')]


async def current_owner(repository, operator):
    from src.auth.service import authenticate_session, AuthFailure
    principal = getattr(operator, 'principal', None)
    root_id = getattr(operator, 'session_id', None)
    if not root_id or not principal or not principal.authenticated or principal.revoked:
        raise DocumentationError('authentication_required', 401)
    try:
        current = await authenticate_session(root_id, touch=False)
    except AuthFailure:
        raise DocumentationError('audio_documentation_root_inactive', 403) from None
    if current.session_id != root_id or current.principal.principal_id != principal.principal_id:
        raise DocumentationError('audio_documentation_owner_invalid', 403)
    async with repository._session() as db:
        root = await db.get(OperatorSession, root_id)
        if root is None or root.revoked_at is not None or utc(root.absolute_expires_at) <= datetime.now(timezone.utc):
            raise DocumentationError('audio_documentation_root_inactive', 403)
        cutoff = min(utc(root.absolute_expires_at), utc(root.idle_expires_at))
    if cutoff <= datetime.now(timezone.utc):
        raise DocumentationError('audio_documentation_root_inactive', 403)
    return principal.principal_id, root_id, cutoff


def audio_profile(configuration):
    from .configuration import OPENROUTER_SETUP_V3_SCHEMA_VERSION, openrouter_profiles_for_setup
    setup = configuration.openrouter_setup
    if (configuration.status != 'ready' or configuration.egress_revoked or setup is None
        or setup.schema_version != OPENROUTER_SETUP_V3_SCHEMA_VERSION):
        raise DocumentationError('audio_setup_v3_required')
    profiles = openrouter_profiles_for_setup(setup, existing=configuration.profiles)
    profile = next((p for p in profiles if p.id == 'openrouter.audio' and p.enabled), None)
    if profile is None:
        raise DocumentationError('audio_route_disabled')
    return profile


def access_readback(access):
    if access is None:
        return None
    access = AudioMetadataAccessV1.model_validate(access)
    return {'selection_ref': access.selection_id, 'selection_digest': digest(access.model_dump()),
        'profile_hash': access.profile_hash, 'expires_at': access.expires_at,
        'operation_scope': access.operation_scope, 'credential_class': access.credential_class,
        'management_scope': 'account_level_unscoped_administration', 'availability': 'unverified'}


async def select_metadata(repository, operator, configuration, request):
    from src.vault.repository import vault_repository
    from .configuration import OPENROUTER_SETUP_V3_SCHEMA_VERSION
    owner, root, cutoff = await current_owner(repository, operator)
    if configuration.openrouter_setup is None or configuration.openrouter_setup.schema_version != OPENROUTER_SETUP_V3_SCHEMA_VERSION:
        raise DocumentationError('audio_setup_v3_required')
    if isinstance(request, DisableMetadataCredential):
        return None
    profile = audio_profile(configuration)
    if profile.contract_hash != request.expected_audio_profile_hash:
        raise DocumentationError('audio_profile_revision_changed')
    if request.vault_key_name == 'openrouter_api_key':
        raise DocumentationError('audio_metadata_inference_credential_forbidden')
    try:
        snapshot = await vault_repository.bounded_snapshot(request.vault_key_name, owner_principal_id=owner)
    except (ValueError, InvalidToken, UnicodeError):
        raise DocumentationError('audio_metadata_credential_unavailable') from None
    if snapshot is None:
        raise DocumentationError('audio_metadata_credential_unavailable')
    if snapshot.value == str(settings.openrouter_api_key or '').strip():
        raise DocumentationError('audio_metadata_inference_credential_forbidden')
    now = datetime.now(timezone.utc)
    return AudioMetadataAccessV1(schema_version='audio-metadata-access.v1', selection_id='audio-metadata:'+uuid4().hex,
        owner_principal_id=owner, original_root_id=root, profile_hash=profile.contract_hash,
        egress_revision=configuration.egress_revision+1, vault_key_name=request.vault_key_name,
        vault_binding_digest=snapshot.binding_digest, vault_identity=snapshot.identity,
        credential_class='openrouter_management', operation_scope='selected_audio_profile_metadata',
        configured_at=now.isoformat(), expires_at=min(cutoff, now+timedelta(hours=24)).isoformat()).model_dump()


async def current_access(repository, operator, request=None, expected=None):
    from .configuration import read_model_fabric_configuration
    from src.vault.repository import vault_repository
    owner, root, cutoff = await current_owner(repository, operator)
    configuration = read_model_fabric_configuration()
    profile = audio_profile(configuration)
    try:
        access = AudioMetadataAccessV1.model_validate(configuration.audio_metadata_access)
    except (ValueError, TypeError):
        raise DocumentationError('audio_metadata_selection_unavailable') from None
    if (access.owner_principal_id != owner or access.original_root_id != root
        or access.egress_revision != configuration.egress_revision or access.profile_hash != profile.contract_hash
        or datetime.fromisoformat(access.expires_at) <= datetime.now(timezone.utc)):
        raise DocumentationError('audio_metadata_selection_stale')
    selection_digest = digest(access.model_dump())
    if expected is not None and selection_digest != expected:
        raise DocumentationError('audio_metadata_selection_changed')
    if request is not None and (request.expected_egress_revision != configuration.egress_revision
        or request.expected_audio_profile_hash != profile.contract_hash
        or request.metadata_selection_ref != access.selection_id
        or request.expected_metadata_selection_digest != selection_digest):
        raise DocumentationError('audio_metadata_selection_changed')
    try:
        snapshot = await vault_repository.bounded_snapshot(access.vault_key_name, owner_principal_id=owner)
    except (ValueError, InvalidToken, UnicodeError):
        raise DocumentationError('audio_metadata_credential_unavailable') from None
    if snapshot is None or snapshot.binding_digest != access.vault_binding_digest or snapshot.identity != access.vault_identity:
        raise DocumentationError('audio_metadata_credential_changed')
    if snapshot.value == str(settings.openrouter_api_key or '').strip():
        raise DocumentationError('audio_metadata_inference_credential_forbidden')
    return configuration, profile, access, snapshot, min(cutoff, datetime.fromisoformat(access.expires_at))


async def metadata_get(url, credential, maximum):
    """One fixed-host, public-IP-pinned TLS read, with no redirects or retry."""
    if not re.fullmatch(r'https://openrouter\.ai/api/v1/(key|models/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/endpoints)', url):
        raise DocumentationError('audio_metadata_path_invalid')
    loop = asyncio.get_running_loop()
    addresses = await loop.getaddrinfo('openrouter.ai', 443, type=socket.SOCK_STREAM)
    ips = sorted({entry[4][0] for entry in addresses})
    def public(ip):
        address = ipaddress.ip_address(ip)
        return address.is_global and not (address.is_multicast or address.is_reserved
            or address.is_loopback or address.is_unspecified)
    if not ips or not all(public(ip) for ip in ips):
        raise DocumentationError('audio_metadata_destination_invalid')
    host = '['+ips[0]+']' if ':' in ips[0] else ips[0]
    path = url[len('https://openrouter.ai'):]
    try:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=10) as client:
            async with client.stream('GET', 'https://'+host+path,
                headers={'host':'openrouter.ai', 'authorization':'Bearer '+credential,
                    'accept':'application/json', 'accept-encoding':'identity'},
                extensions={'sni_hostname':'openrouter.ai'}) as response:
                if response.status_code != 200:
                    raise DocumentationError('audio_metadata_http_rejected')
                if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                    raise DocumentationError('audio_metadata_encoding_rejected')
                if response.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
                    raise DocumentationError('audio_metadata_media_type_rejected')
                advertised = response.headers.get('content-length')
                if advertised is not None and (not advertised.isascii() or not advertised.isdigit()
                    or len(advertised)>8 or int(advertised)>maximum):
                    raise DocumentationError('audio_metadata_response_oversized')
                raw = bytearray()
                async for chunk in response.aiter_raw():
                    if len(raw)+len(chunk)>maximum:
                        raise DocumentationError('audio_metadata_response_oversized')
                    raw.extend(chunk)
                if not raw or advertised is not None and len(raw)!=int(advertised):
                    raise DocumentationError('audio_metadata_response_incomplete')
                return bytes(raw)
    except httpx.HTTPError:
        raise DocumentationError('audio_metadata_transfer_failed') from None


def object_directory():
    root = canonical_workspace_root(settings.workspace_dir)
    fd = os.open(root, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for name in ('.model-fabric', 'audio-documentation', 'objects'):
            try: os.mkdir(name, 0o700, dir_fd=fd)
            except FileExistsError: pass
            child = os.open(name, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=fd)
            facts=os.fstat(child)
            if facts.st_uid!=os.getuid() or stat.S_IMODE(facts.st_mode)!=0o700:
                os.close(child)
                raise DocumentationError('audio_documentation_private_directory_invalid')
            os.close(fd); fd=child
        return fd
    except BaseException:
        os.close(fd); raise


def private_write(identifier, raw):
    if not re.fullmatch(r'[0-9a-f]{32}\.source', identifier):
        raise DocumentationError('audio_documentation_object_invalid')
    parent=object_directory()
    try:
        fd=os.open(identifier, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            view=memoryview(raw)
            while view:
                count=os.write(fd,view)
                if count<=0: raise OSError('private write incomplete')
                view=view[count:]
            os.fsync(fd)
        finally: os.close(fd)
        os.fsync(parent)
    finally: os.close(parent)


def private_read(source):
    if not re.fullmatch(r'[0-9a-f]{32}\.source', source.object_id):
        raise DocumentationError('audio_documentation_object_invalid')
    parent=object_directory()
    try: fd=os.open(source.object_id, os.O_RDONLY|os.O_NOFOLLOW, dir_fd=parent)
    finally: os.close(parent)
    try:
        facts=os.fstat(fd)
        if (not stat.S_ISREG(facts.st_mode) or facts.st_nlink!=1 or facts.st_uid!=os.getuid()
            or stat.S_IMODE(facts.st_mode)!=0o600 or facts.st_size!=source.size_bytes
            or not 1<=facts.st_size<=262144):
            raise DocumentationError('audio_documentation_private_object_invalid')
        raw=bytearray()
        while len(raw)<facts.st_size:
            chunk=os.read(fd,min(16384,facts.st_size-len(raw)))
            if not chunk: raise DocumentationError('audio_documentation_source_incomplete')
            raw.extend(chunk)
        if os.read(fd,1) or hashlib.sha256(raw).hexdigest()!=source.sha256:
            raise DocumentationError('audio_documentation_source_changed')
        return bytes(raw)
    finally: os.close(fd)


def staged_readback(row):
    return {'state':row.state, 'staged_ref':row.id, 'revision':row.revision,
        'bundle_digest':row.bundle_digest, 'expires_at':utc(row.expires_at).isoformat(),
        'coverage':{'complete':False, 'missing':list(MISSING_FACTS)},
        'error_code':row.error_code, 'admission_ready':False, 'ready':False,
        'availability':'unverified'}


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate metadata field')
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError('nonfinite metadata number')
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (ValueError, UnicodeError, RecursionError):
        raise DocumentationError('audio_metadata_json_invalid') from None


def successful_inventory(row, sources):
    """Authenticate the exact producer-issued physical inventory, never infer it."""
    if (row.revision != 1 or row.error_code != 'audio_documentation_source_incomplete'
        or row.reserved_bytes != RESERVED_BYTES or row.state not in {'staged','deleting'}
        or len(row.binding_json.encode()) > 16384):
        raise DocumentationError('audio_documentation_cleanup_unknown')
    binding = strict_json(row.binding_json)
    keys = {'inventory_schema','selection_digest','model','endpoint_tag',
        'configuration_revision','catalog_id','sources'}
    if (not isinstance(binding,dict) or set(binding)!=keys
        or binding['inventory_schema']!='audio-documentary-inventory.v1'
        or canonical(binding)!=row.binding_json or digest(binding)!=row.bundle_digest
        or binding['catalog_id']!=CATALOG
        or not isinstance(binding['model'],str)
        or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',binding['model'])
        or not isinstance(binding['selection_digest'],str)
        or not re.fullmatch(r'[0-9a-f]{64}',binding['selection_digest'])
        or not isinstance(binding['endpoint_tag'],str) or not 1<=len(binding['endpoint_tag'])<=256
        or type(binding['configuration_revision']) is not int or binding['configuration_revision']<1
        or not isinstance(binding['sources'],list) or len(binding['sources'])!=2 or len(sources)!=2):
        raise DocumentationError('audio_documentation_cleanup_unknown')
    expected = [('key-type','https://openrouter.ai/api/v1/key',16384),
        ('model-endpoints','https://openrouter.ai/api/v1/models/'+binding['model']+'/endpoints',262144)]
    for ordinal,(source,entry,(identifier,url,maximum)) in enumerate(zip(sources,binding['sources'],expected)):
        facts={'ordinal':source.ordinal,'source_row_id':source.id,'source_id':source.source_id,
            'source_url':source.source_url,'object_id':source.object_id,'sha256':source.sha256,
            'size_bytes':source.size_bytes}
        if (not isinstance(entry,dict) or entry!=facts or set(entry)!=set(facts)
            or type(entry['ordinal']) is not int or entry['ordinal']!=ordinal
            or type(entry['size_bytes']) is not int or not 1<=entry['size_bytes']<=maximum
            or source.attestation_id!=row.id or source.source_id!=identifier or source.source_url!=url
            or not re.fullmatch(r'[0-9a-f]{32}\.source',source.object_id)
            or not re.fullmatch(r'[0-9a-f]{64}',source.sha256)):
            raise DocumentationError('audio_documentation_cleanup_unknown')
    if sources[0].id==sources[1].id or sources[0].object_id==sources[1].object_id:
        raise DocumentationError('audio_documentation_cleanup_unknown')


async def source_storage_count(db, identifier, *, partial=False):
    count,valid=(await db.execute(text("SELECT count(*), sum(CASE WHEN "+SOURCE_STORAGE+
        " THEN 1 ELSE 0 END) FROM model_audio_documentation_sources WHERE attestation_id=:id"),
        {'id':identifier})).one()
    if not (0<=count<=2 if partial else count==2) or (valid or 0)!=count:
        raise DocumentationError('audio_documentation_cleanup_unknown')


async def bounded_bundle(db, identifier, *, settled_only=False):
    condition=BUNDLE_STORAGE
    if settled_only:
        condition+=" AND revision=1 AND reserved_bytes=540672 AND state IN ('staged','deleting') AND error_code='audio_documentation_source_incomplete'"
    revision=(await db.execute(select(Bundle.revision).where(Bundle.id==identifier,text(condition)))).scalar_one_or_none()
    if revision is None:raise DocumentationError('audio_documentation_cleanup_unknown')
    await source_storage_count(db,identifier,partial=revision==0)
    # Repeat the predicate on the actual body SELECT: preflight must not be a
    # byte-bound TOCTOU if another writer changes storage between these reads.
    try:
        row=(await db.execute(select(Bundle).where(Bundle.id==identifier,text(condition))
            .execution_options(populate_existing=True))).scalar_one_or_none()
        if row is None:raise DocumentationError('audio_documentation_cleanup_unknown')
        return row
    except (ValueError,TypeError):raise DocumentationError('audio_documentation_cleanup_unknown') from None


async def cleanup_witness(db, identifier):
    row=await bounded_bundle(db,identifier,settled_only=True)
    try:
        sources=(await db.execute(select(Source).where(Source.attestation_id==identifier,text(SOURCE_STORAGE))
            .order_by(Source.ordinal).limit(4))).scalars().all()
    except (ValueError,TypeError):raise DocumentationError('audio_documentation_cleanup_unknown') from None
    successful_inventory(row,sources)
    return row,sources


def same_witness(row, state):
    return (Bundle.id==row.id,Bundle.revision==1,Bundle.state==state,
        Bundle.error_code=='audio_documentation_source_incomplete',Bundle.reserved_bytes==RESERVED_BYTES,
        Bundle.owner_principal_id==row.owner_principal_id,Bundle.original_root_id==row.original_root_id,
        Bundle.profile_hash==row.profile_hash,Bundle.binding_json==row.binding_json,
        Bundle.bundle_digest==row.bundle_digest,Bundle.expires_at==row.expires_at)


async def expired_owner(db,row):
    root=await db.get(OperatorSession,row.original_root_id)
    if root is not None and root.principal_id!=row.owner_principal_id:
        raise DocumentationError('audio_documentation_cleanup_unknown')
    now=datetime.now(timezone.utc)
    return (utc(row.expires_at)<=now or root is None or root.revoked_at is not None
        or utc(root.idle_expires_at)<=now or utc(root.absolute_expires_at)<=now)


def remove_original_sources(sources):
    parent=object_directory()
    try:
        for source in sources:
            try:
                fd=os.open(source.object_id,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent)
            except FileNotFoundError: continue
            try:
                facts=os.fstat(fd)
                if (not stat.S_ISREG(facts.st_mode) or facts.st_nlink!=1 or facts.st_uid!=os.getuid()
                    or stat.S_IMODE(facts.st_mode)!=0o600 or facts.st_size!=source.size_bytes):
                    raise DocumentationError('audio_documentation_cleanup_unknown')
                hashed=hashlib.sha256();remaining=source.size_bytes
                while remaining:
                    chunk=os.read(fd,min(16384,remaining))
                    if not chunk: raise DocumentationError('audio_documentation_cleanup_unknown')
                    remaining-=len(chunk);hashed.update(chunk)
                if os.read(fd,1) or hashed.hexdigest()!=source.sha256:
                    raise DocumentationError('audio_documentation_cleanup_unknown')
                try:
                    current=os.stat(source.object_id,dir_fd=parent,follow_symlinks=False)
                    if (current.st_dev,current.st_ino)!=(facts.st_dev,facts.st_ino):
                        raise DocumentationError('audio_documentation_cleanup_unknown')
                    os.unlink(source.object_id,dir_fd=parent)
                except FileNotFoundError: pass
            finally: os.close(fd)
        os.fsync(parent)
    finally: os.close(parent)


async def delete_successful(repository, original, *, automatic):
    """Same-witness deletion; only the exact final CAS winner releases quota."""
    try:
        async with repository._session() as db:
            await db.execute(text('BEGIN IMMEDIATE'))
            row,sources=await cleanup_witness(db,original.id)
            if any(getattr(row,key)!=getattr(original,key) for key in
                ('revision','state','bundle_digest','binding_json','owner_principal_id','original_root_id')):
                return False
            if automatic:
                eligible=await expired_owner(db,row)
                if row.state=='staged' and not eligible: return False
            if row.state=='staged':
                changed=await db.execute(update(Bundle).where(*same_witness(row,'staged')).values(state='deleting'))
                if changed.rowcount!=1: return False
        remove_original_sources(sources)
        async with repository._session() as db:
            await db.execute(text('BEGIN IMMEDIATE'))
            current,current_sources=await cleanup_witness(db,row.id)
            if current.binding_json!=row.binding_json or current.bundle_digest!=row.bundle_digest: return False
            changed=await db.execute(update(Bundle).where(*same_witness(row,'deleting')).values(
                state='rejected',reserved_bytes=0,revision=2))
            return changed.rowcount==1
    except (DocumentationError,OSError,ValueError):
        return False


async def cleanup_settled(repository):
    counts={'inspected':0,'deleted':0,'unknown':0}
    async with repository._session() as db:
        # Corrupt headers count as unknown without returning their raw bytes.
        # The fixed CASE projections precede even cleanup_witness's preflight.
        rows=(await db.execute(text("SELECT "
            "CASE WHEN typeof(id)='text' AND octet_length(id)=32 THEN id END, "
            "CASE WHEN typeof(revision)='integer' THEN revision END, "
            "CASE WHEN typeof(state)='text' AND octet_length(state) BETWEEN 1 AND 8 THEN state END, "
            "CASE WHEN typeof(error_code)='text' AND octet_length(error_code) BETWEEN 1 AND 38 THEN error_code END "
            "FROM model_audio_documentation_attestations WHERE reserved_bytes>0 LIMIT 17"))).all()
    if len(rows)>16: return {'inspected':17,'deleted':0,'unknown':17}
    for identifier,revision,state,error in rows:
        counts['inspected']+=1
        if identifier is None or revision!=1 or state not in {'staged','deleting'} or error!='audio_documentation_source_incomplete':
            counts['unknown']+=1;continue
        try:
            async with repository._session() as db:
                row,_=await cleanup_witness(db,identifier)
                if row.state=='staged' and not await expired_owner(db,row): continue
            if await delete_successful(repository,row,automatic=True): counts['deleted']+=1
            else: counts['unknown']+=1
        except (DocumentationError,OSError,ValueError): counts['unknown']+=1
    return counts


async def stage(repository, operator, request):
    configuration,profile,access,snapshot,cutoff=await current_access(repository,operator,request)
    model=profile.model
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', model):
        raise DocumentationError('audio_metadata_model_path_invalid')
    await cleanup_settled(repository)
    binding={'inventory_schema':'audio-documentary-inventory.v1', 'selection_digest':digest(access.model_dump()),'model':model,
        'endpoint_tag':profile.options['provider']['only'][0], 'configuration_revision':configuration.egress_revision,
        'catalog_id':request.official_supplement_catalog_id, 'sources':[]}
    now=datetime.now(timezone.utc)
    row=Bundle(owner_principal_id=access.owner_principal_id,original_root_id=access.original_root_id,
        profile_hash=profile.contract_hash,binding_json=canonical(binding),bundle_digest=digest(binding),
        expires_at=min(cutoff,now+timedelta(minutes=10)))
    async with repository._session() as db:
        await db.execute(text('BEGIN IMMEDIATE'))
        rows=(await db.execute(text("SELECT CASE WHEN "+BUNDLE_STORAGE+" THEN 1 ELSE 0 END, "
            "CASE WHEN typeof(id)='text' AND octet_length(id)=32 THEN id END, "
            "CASE WHEN typeof(original_root_id)='text' AND octet_length(original_root_id) BETWEEN 1 AND 128 THEN original_root_id END, "
            "CASE WHEN typeof(profile_hash)='text' AND octet_length(profile_hash)=64 THEN profile_hash END, "
            "CASE WHEN typeof(state)='text' AND state IN ('staged','deleting','rejected','accepted') THEN state END, "
            "CASE WHEN typeof(revision)='integer' AND revision BETWEEN 0 AND 2 THEN revision END "
            "FROM model_audio_documentation_attestations WHERE "+OPEN_RESERVATION+" LIMIT 17"))).all()
        if any(not valid for valid,*_ in rows):raise DocumentationError('audio_documentation_quota_full')
        try:
            for _,identifier,_,_,_,revision in rows:
                await source_storage_count(db,identifier,partial=revision==0)
        except DocumentationError:raise DocumentationError('audio_documentation_quota_full') from None
        pairs={(original_root,profile_hash) for _,_,original_root,profile_hash,_,_ in rows}
        if (len(rows)>=16 or ((access.original_root_id,profile.contract_hash) not in pairs and len(pairs)>=8)
            or any(original_root==access.original_root_id and profile_hash==profile.contract_hash
                and state in {'staged','deleting'} for _,_,original_root,profile_hash,state,_ in rows)):
            raise DocumentationError('audio_documentation_quota_full')
        db.add(row); await db.flush()
    try:
        async with asyncio.timeout(10):
            for ordinal,(source_id,url,maximum) in enumerate((('key-type','https://openrouter.ai/api/v1/key',16384),
                ('model-endpoints','https://openrouter.ai/api/v1/models/'+model+'/endpoints',262144))):
                _,_,_,current_secret,_=await current_access(repository,operator,expected=binding['selection_digest'])
                raw=await metadata_get(url,current_secret.value,maximum)
                payload=strict_json(raw)
                if ordinal==0:
                    if not isinstance(payload,dict) or not isinstance(payload.get('data'),dict) or payload['data'].get('is_management_key') is not True:
                        raise DocumentationError('audio_metadata_management_key_required')
                else:
                    data=payload.get('data') if isinstance(payload,dict) else None
                    if not isinstance(data,dict) or data.get('id')!=model or not isinstance(data.get('endpoints'),list):
                        raise DocumentationError('audio_metadata_model_mismatch')
                    matches=[item for item in data['endpoints'] if isinstance(item,dict) and item.get('tag')==binding['endpoint_tag']]
                    if len(matches)!=1: raise DocumentationError('audio_metadata_exact_endpoint_missing')
                source=Source(attestation_id=row.id,ordinal=ordinal,source_id=source_id,source_url=url,
                    object_id=uuid4().hex+'.source',sha256=hashlib.sha256(raw).hexdigest(),size_bytes=len(raw))
                # Canonical intent precedes physical publication; crashes keep quota.
                async with repository._session() as db: db.add(source); await db.flush()
                private_write(source.object_id,raw); private_read(source)
                binding['sources'].append({'ordinal':ordinal,'source_row_id':source.id,
                    'source_id':source.source_id,'source_url':source.source_url,'object_id':source.object_id,
                    'sha256':source.sha256,'size_bytes':len(raw)})
            await current_access(repository,operator,expected=binding['selection_digest'])
        row.binding_json=canonical(binding); row.bundle_digest=digest(binding)
        row.error_code='audio_documentation_source_incomplete'; row.revision=1
        async with repository._session() as db:
            changed=await db.execute(update(Bundle).where(Bundle.id==row.id,Bundle.revision==0,Bundle.state=='staged',Bundle.error_code.is_(None)).values(
                binding_json=row.binding_json,bundle_digest=row.bundle_digest,error_code=row.error_code,revision=1))
            if changed.rowcount!=1: raise DocumentationError('audio_documentation_revision_changed')
        return staged_readback(row)
    except BaseException:
        async with repository._session() as db:
            await db.execute(update(Bundle).where(Bundle.id==row.id,Bundle.revision==0,
                Bundle.state=='staged',Bundle.error_code.is_(None)).values(
                error_code='audio_documentation_acquisition_incomplete'))
        raise


async def review(repository, operator, request):
    owner,root,_=await current_owner(repository,operator)
    async with repository._session() as db:
        header=(await db.execute(text("SELECT "
            "CASE WHEN typeof(owner_principal_id)='text' AND octet_length(owner_principal_id) BETWEEN 1 AND 128 THEN owner_principal_id END, "
            "CASE WHEN typeof(original_root_id)='text' AND octet_length(original_root_id) BETWEEN 1 AND 128 THEN original_root_id END, "
            "CASE WHEN typeof(revision)='integer' AND revision BETWEEN 0 AND 2 THEN revision END, "
            "CASE WHEN typeof(bundle_digest)='text' AND octet_length(bundle_digest)=64 THEN bundle_digest END "
            "FROM model_audio_documentation_attestations WHERE id=:id"),{'id':request.staged_ref})).one_or_none()
        if header is None or header[0]!=owner or header[1]!=root:
            raise DocumentationError('audio_documentation_not_found',404)
        if header[2] is None or header[3] is None:raise DocumentationError('audio_documentation_cleanup_unknown')
        if header[2]!=request.expected_staged_revision or header[3]!=request.expected_bundle_digest:
            raise DocumentationError('audio_documentation_revision_changed')
        row=await bounded_bundle(db,request.staged_ref)
        if row.owner_principal_id!=owner or row.original_root_id!=root:
            raise DocumentationError('audio_documentation_not_found',404)
        if row.revision!=request.expected_staged_revision or row.bundle_digest!=request.expected_bundle_digest:
            raise DocumentationError('audio_documentation_revision_changed')
        if isinstance(request,AcceptStagedDocumentationV1):
            if row.state!='staged' or utc(row.expires_at)<=datetime.now(timezone.utc):
                raise DocumentationError('audio_documentation_staging_expired')
            row,sources=await cleanup_witness(db,row.id)
            if row.owner_principal_id!=owner or row.original_root_id!=root:
                raise DocumentationError('audio_documentation_not_found',404)
            if row.revision!=request.expected_staged_revision or row.bundle_digest!=request.expected_bundle_digest:
                raise DocumentationError('audio_documentation_revision_changed')
            binding=strict_json(row.binding_json)
            await current_access(repository,operator,expected=binding['selection_digest'])
            for source in sources: private_read(source)
            # There is deliberately no operator Boolean/extracted-JSON escape.
            raise DocumentationError('audio_documentation_source_incomplete')
        if row.state=='rejected': return staged_readback(row)
        if row.state not in {'staged','deleting'}: raise DocumentationError('audio_documentation_not_staged')
    if not await delete_successful(repository, row, automatic=False):
        raise DocumentationError('audio_documentation_cleanup_unknown')
    row.state='rejected';row.revision+=1;row.reserved_bytes=0
    return staged_readback(row)


async def require_execution_documentation(repository, operator, reference, expected_digest):
    """No complete catalog currently exists; neither staging nor fixtures issue authority."""
    await current_owner(repository,operator)
    raise DocumentationError('audio_documentation_source_incomplete')


async def owned_staging(repository, operator):
    owner, root, _ = await current_owner(repository, operator)
    async with repository._session() as db:
        identifiers=(await db.execute(text("SELECT CASE WHEN typeof(id)='text' AND octet_length(id)=32 THEN id END "
            "FROM model_audio_documentation_attestations WHERE owner_principal_id=:owner AND original_root_id=:root AND "+OPEN_RESERVATION+
            " ORDER BY CASE WHEN typeof(created_at)='text' AND octet_length(created_at) BETWEEN 1 AND 32 THEN created_at END DESC LIMIT 2"),
            {'owner':owner,'root':root})).scalars().all()
        rows=[]
        for identifier in identifiers:
            if identifier is None:raise DocumentationError('audio_documentation_cleanup_unknown')
            rows.append(await bounded_bundle(db,identifier))
        return [staged_readback(row) for row in rows]
