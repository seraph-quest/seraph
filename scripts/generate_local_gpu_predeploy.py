#!/usr/bin/env python3
"""Generate local identity, listener, and immutable GPU artifact gate."""
import hashlib, json, os, socket, subprocess
from datetime import datetime, timezone
from pathlib import Path

def artifact(path):
    p=Path(path); h=hashlib.sha256()
    with p.open('rb') as stream:
        for chunk in iter(lambda:stream.read(8*1024*1024),b''): h.update(chunk)
    return {'filename':p.name,'sha256':h.hexdigest(),'size':p.stat().st_size}

machine=next((p for p in (Path('/etc/machine-id'),Path('/var/lib/dbus/machine-id')) if p.is_file()),None)
if not machine: raise SystemExit('machine-id unavailable')
ss=subprocess.run(['ss','-lntp'],text=True,capture_output=True,check=True).stdout
network=subprocess.run(['docker','network','inspect','bridge'],text=True,capture_output=True,check=True)
gateway=str(json.loads(network.stdout)[0].get('IPAM',{}).get('Config',[{}])[0].get('Gateway',''))
configured_model_dir=Path(os.environ['SERAPH_GPU_MODEL_DIR'])
if not configured_model_dir.is_absolute(): raise SystemExit('GPU artifact root must be an absolute existing directory')
model_dir=configured_model_dir.resolve(strict=True)
if not model_dir.is_dir(): raise SystemExit('GPU artifact root must be an absolute existing directory')
gpu_release={'image_ref':os.environ['SERAPH_GPU_MODEL_IMAGE'],'alias':os.environ['SERAPH_GPU_MODEL_ALIAS'],'artifact_root':str(model_dir),'model':artifact(model_dir/os.environ['SERAPH_GPU_MODEL_FILE']),'mmproj':artifact(model_dir/os.environ['SERAPH_GPU_MMPROJ_FILE']),'ctx_size':int(os.environ.get('SERAPH_GPU_MODEL_CTX_SIZE','32768')),'layers':int(os.environ.get('SERAPH_GPU_MODEL_LAYERS','999')),'command_contract':'llama-server-gemma4-v1'}
print(json.dumps({'local_hostname':socket.gethostname(),'machine_identity_sha256':hashlib.sha256(machine.read_bytes().strip()).hexdigest(),'captured_at':datetime.now(timezone.utc).isoformat(),'ss_lntp':ss,'docker_bridge_addresses':[gateway] if gateway else [],'docker_network_bindings':{'host-gateway':gateway} if gateway else {},'gpu_release':gpu_release},indent=2,sort_keys=True))
