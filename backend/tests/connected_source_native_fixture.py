"""Owned synchronized source fixture used by existing native task journeys."""
import json
from datetime import datetime, timedelta, timezone
from src.db.models import GoogleServiceConnection, MailReadConsent, MailLabelBinding
from src.integrations.connection_sync import ConnectionSyncService, SyncRequest
from src.integrations.gmail_read import GMAIL_READONLY_SCOPE, message_key
from src.vault import encrypt, vault_repository
from tests.test_connection_sync import Provider, install_provider

async def synchronized_related_source(async_db, monkeypatch, owner, operator, goal_id, goal_revision):
    from src.auth.ownership import enroll
    await enroll(operator)
    timestamp = datetime.now(timezone.utc)
    from src.integrations import connection_sync
    monkeypatch.setattr(connection_sync, 'get_session', async_db)
    connection_id = 'related-source-connection'
    async with async_db() as db:
        db.add(GoogleServiceConnection(connection_id=connection_id, owner_principal_id=owner.principal_id, owner_session_id=owner.session_id, service='gmail_readonly', setup_idempotency_key='related-source-setup', vault_secret_key='related-source-secret', credential_fingerprint='related-fingerprint', state='active', revision=1, declared_scopes_json=json.dumps([GMAIL_READONLY_SCOPE])))
        db.add(MailLabelBinding(label_id='related-label', owner_principal_id=owner.principal_id, owner_session_id=owner.session_id, connection_id=connection_id, connection_revision=1, provider_label_id_ciphertext=encrypt('INBOX'), provider_label_digest='related-label-digest', state='active'))
        db.add(MailReadConsent(consent_id='related-grant', owner_principal_id=owner.principal_id, owner_session_id=owner.session_id, connection_id=connection_id, connection_revision=1, goal_id=goal_id, goal_revision=goal_revision, label_ids_json='["related-label"]', sync_metadata_limit=50, max_messages=10, source_read_allowed=True, source_revision=1, source_digest='related-grant-digest', allowed_body_fields_json='["plainbody"]', expires_at=timestamp + timedelta(hours=1), created_at=timestamp))
    await vault_repository.store('related-source-secret', json.dumps({'client_id':'related-client','refresh_token':'related-refresh'}))
    provider = Provider(timestamp, count=1)
    install_provider(monkeypatch, provider)
    runtime = ConnectionSyncService()
    await runtime.start()
    request = SyncRequest.model_validate({'request_uuid':'related-native-sync', 'input': {'goal_ref':{'id':goal_id,'revision':goal_revision}, 'connection_ref':{'id':connection_id,'revision':1},'source_scope':{'provider':'gmail','consents':[{'id':'related-grant','revision':1}],'label_ids':['related-label'], 'selected_private_items':[message_key(owner.principal_id, connection_id, 'm0')], 'acknowledge_private_read':True},'window':{'start':(timestamp-timedelta(days=1)).isoformat(),'end':timestamp.isoformat()},'max_items':1}})
    result = await runtime.synchronize(owner, request, authenticated_token_hash=operator._token_hash)
    selection = [{'connection_ref':{'id':connection_id,'revision':1},'item_refs':result['items']}]
    return runtime, provider, selection
