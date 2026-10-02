"""Explicit host-local reset of exactly one pairing, never browser authority."""
from datetime import datetime, timezone
import hashlib
import re

from sqlalchemy import select
import src.db.engine as database
from src.db.models import Secret
from src.extensions.state import (load_extension_state_payload,save_extension_state_payload,
    revoke_node_adapter_pairing_entry,clear_node_adapter_pairing_entry,ExtensionStateRevisionConflict)
from src.extensions.paired_edge import current_pairing,PAIRING_CREDENTIAL_PREFIX


async def reset_pairing(*,extension_id,reference,expected_revision):
    from src.api.nodes import _node_inventory, _find_adapter
    payload=load_extension_state_payload()
    revision=int(payload.get('revision') or 0)
    if revision!=expected_revision:
        raise ExtensionStateRevisionConflict(expected_revision,revision)
    adapter=_find_adapter(_node_inventory(payload),extension_id=extension_id,reference=reference)
    entry,_=current_pairing(payload,extension_id=extension_id,reference=reference,name=adapter.name)
    if not entry:
        raise ValueError('exact_pairing_not_found')
    credential_ref=entry.get('credential_ref')
    revoke_node_adapter_pairing_entry(payload,extension_id=extension_id,reference=reference,
        name=adapter.name,reason='explicit_host_local_owner_reset',revoked_at=datetime.now(timezone.utc).isoformat())
    revoked_revision=save_extension_state_payload(payload,expected_revision=expected_revision)
    # Local state fences first. Cleanup failure keeps the revoked binding and
    # must not advertise fresh pairing until exact credential invalidation.
    async with database.get_session() as db:
        secrets=(await db.execute(select(Secret).where(Secret.key.like('seraph-node-pairing-%')))).scalars().all()
        for secret in secrets:
            if re.fullmatch(r'seraph-node-pairing-[0-9a-f]{40}',secret.key) and credential_ref==PAIRING_CREDENTIAL_PREFIX+hashlib.sha256(secret.key.encode()).hexdigest()[:24]:
                secret.revoked_at=datetime.now(timezone.utc)
                secret.encrypted_value='revoked:host-local-pairing-reset'
    payload=load_extension_state_payload()
    if int(payload.get('revision') or 0)!=revoked_revision:
        raise ExtensionStateRevisionConflict(revoked_revision,int(payload.get('revision') or 0))
    clear_node_adapter_pairing_entry(payload,extension_id=extension_id,reference=reference,name=adapter.name)
    final=save_extension_state_payload(payload,expected_revision=revoked_revision)
    return {'state':'fresh_pairing_required','revision':final,'authority_imported':False,'history_imported':False}
