#!/usr/bin/env python3
import hashlib, json, os, re, sys
from pathlib import Path
from datetime import datetime, timezone

r=json.load(sys.stdin)
def fail(x): raise SystemExit('GPU predeploy invalid: '+x)
if r.get('local_hostname') != os.environ.get('SERAPH_GPU_EXPECTED_HOSTNAME'): fail('hostname mismatch')
if r.get('machine_identity_sha256') != os.environ.get('SERAPH_GPU_MACHINE_IDENTITY_SHA256'): fail('machine identity mismatch')
try: age=(datetime.now(timezone.utc)-datetime.fromisoformat(r['captured_at'].replace('Z','+00:00'))).total_seconds()
except Exception: fail('invalid captured_at')
if age < -30 or age > int(os.environ.get('SERAPH_HOST_INVENTORY_MAX_AGE_SECONDS','900')): fail('stale receipt')
def artifact(path):
    p=Path(path); h=hashlib.sha256()
    with p.open('rb') as stream:
        for chunk in iter(lambda:stream.read(8*1024*1024),b''): h.update(chunk)
    return {'filename':p.name,'sha256':h.hexdigest(),'size':p.stat().st_size}
configured_model_dir=Path(os.environ['SERAPH_GPU_MODEL_DIR'])
if not configured_model_dir.is_absolute(): fail('GPU artifact root must be an absolute existing directory')
model_dir=configured_model_dir.resolve(strict=True)
if not model_dir.is_dir(): fail('GPU artifact root must be an absolute existing directory')
expected={'image_ref':os.environ['SERAPH_GPU_MODEL_IMAGE'],'alias':os.environ['SERAPH_GPU_MODEL_ALIAS'],'artifact_root':str(model_dir),'model':artifact(model_dir/os.environ['SERAPH_GPU_MODEL_FILE']),'mmproj':artifact(model_dir/os.environ['SERAPH_GPU_MMPROJ_FILE']),'ctx_size':int(os.environ.get('SERAPH_GPU_MODEL_CTX_SIZE','32768')),'layers':int(os.environ.get('SERAPH_GPU_MODEL_LAYERS','999')),'command_contract':'llama-server-gemma4-v1'}
if r.get('gpu_release')!=expected: fail('GPU release artifact manifest mismatch')
allowed={'127.0.0.1','::1','[::1]'} | set(r.get('docker_bridge_addresses',[]))
for line in str(r.get('ss_lntp','')).splitlines():
    m=re.search(r'LISTEN\s+\d+\s+\d+\s+(\S+):(8000|8001|8004)\b',line)
    if not m: continue
    address,port=m.groups()
    fail(f'old unmanaged service listener {address}:{port} must be stopped before managed cutover')
print('GPU predeploy valid: local identity verified; unmanaged model/wrapper/backend listeners stopped')
