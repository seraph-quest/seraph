#!/usr/bin/env python3
import argparse, hashlib, json, secrets
from datetime import datetime, timezone

p=argparse.ArgumentParser()
for name in ('stage','app-sha','vlm-image','local-attestation','origin','lan-host','lan-ip','client-identity'): p.add_argument('--'+name,required=True)
a=p.parse_args(); raw=open(a.local_attestation,'rb').read(); local=json.loads(raw)
containers=local['compose_observation']['containers']; network_ids={v['network_id'] for v in containers.values()}
if len(network_ids)!=1: raise SystemExit('local attestation network mismatch')
gpu=local['gpu_model_observation']
challenge={'schema':'seraph.acceptance-challenge.v1','stage':a.stage,'app_sha':a.app_sha,'vlm_image':a.vlm_image,'gpu_model_image':gpu['image_ref'],'gpu_model_alias':gpu['alias'],'local_attestation_sha256':hashlib.sha256(raw).hexdigest(),'compose_project':'seraph-prod','network_name':'seraph-core-prod','network_id':next(iter(network_ids)),'container_ids':{k:v['container_id'] for k,v in containers.items()},'image_identities':{k:{'image_id':v['image_id'],'image_revision':v.get('image_revision','')} for k,v in containers.items()},'expected_origin':a.origin,'lan_host':a.lan_host,'lan_ip':a.lan_ip,'client_identity':a.client_identity,'server_nonce':secrets.token_hex(32),'issued_at':datetime.now(timezone.utc).isoformat()}
challenge['gpu_release']=local['gpu_release']
print(json.dumps(challenge,indent=2,sort_keys=True))
