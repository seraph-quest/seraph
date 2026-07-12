#!/usr/bin/env python3
"""Observe local host/firewall/wrapper state and emit a sanitized receipt."""
from __future__ import annotations

import argparse, hashlib, json, socket, subprocess
from datetime import datetime, timezone
from pathlib import Path

def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False)

VLM_PROBE = """import json,sys,urllib.error,urllib.request
path=sys.argv[1]; key=sys.stdin.read().strip(); headers={'Authorization':'Bearer '+key} if key else {}
try:
    with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8001'+path,headers=headers),timeout=5) as response:
        raw=response.read(4096); body=json.loads(raw) if raw else {}; print(json.dumps({'http_status':response.status,'body':body}))
except urllib.error.HTTPError as exc:
    raw=exc.read(4096); body=json.loads(raw) if raw else {}; print(json.dumps({'http_status':exc.code,'body':body}))
except Exception: raise SystemExit(1)
"""

def vlm_response(container_id: str, path: str, key: str = "") -> tuple[int, dict]:
    result=subprocess.run(['docker','exec','-i',container_id,'python','-c',VLM_PROBE,path],input=key,text=True,capture_output=True,check=False)
    try:
        payload=json.loads(result.stdout) if result.returncode == 0 else {}
        body=payload.get('body',{})
        return int(payload.get('http_status',0)), body if isinstance(body,dict) else {}
    except (ValueError,TypeError,json.JSONDecodeError): return 0, {}

def gpu_healthy(container_id: str) -> bool:
    result=run(['docker','exec',container_id,'curl','--fail','--silent','--show-error','--max-time','5','http://127.0.0.1:8000/health'])
    return result.returncode == 0

def canonical_repo_digest(reference: str) -> str:
    if '@sha256:' not in reference: return reference
    name,digest=reference.rsplit('@',1); slash=name.rfind('/'); colon=name.rfind(':')
    if colon > slash: name=name[:colon]
    return name+'@'+digest

p = argparse.ArgumentParser()
p.add_argument("--expected-vlm-image", required=True)
p.add_argument("--vlm-container", required=True)
p.add_argument("--ingress-container", required=True)
p.add_argument("--backend-container", required=True)
p.add_argument("--gpu-model-container", required=True)
p.add_argument("--expected-gpu-model-image", required=True)
p.add_argument("--expected-gpu-model-alias", required=True)
p.add_argument("--gpu-model-dir", type=Path, required=True)
p.add_argument("--gpu-model-file", required=True)
p.add_argument("--gpu-mmproj-file", required=True)
p.add_argument("--gpu-ctx-size", type=int, required=True)
p.add_argument("--gpu-layers", type=int, required=True)
p.add_argument("--vlm-api-key-file", type=Path)
p.add_argument("--docker-network", default="bridge")
args = p.parse_args()
def artifact(path: Path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(8*1024*1024),b''): h.update(chunk)
    return {'filename':path.name,'sha256':h.hexdigest(),'size':path.stat().st_size}
if not args.gpu_model_dir.is_absolute(): raise SystemExit('GPU artifact root must be an absolute existing directory')
gpu_root=args.gpu_model_dir.resolve(strict=True)
if not gpu_root.is_dir(): raise SystemExit('GPU artifact root must be an absolute existing directory')
gpu_release={'image_ref':args.expected_gpu_model_image,'alias':args.expected_gpu_model_alias,'artifact_root':str(gpu_root),'model':artifact(gpu_root/args.gpu_model_file),'mmproj':artifact(gpu_root/args.gpu_mmproj_file),'ctx_size':args.gpu_ctx_size,'layers':args.gpu_layers,'command_contract':'llama-server-gemma4-v1'}

machine_path = next((x for x in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")) if x.is_file()), None)
if machine_path is None:
    raise SystemExit("local machine-id source unavailable")
machine_digest = hashlib.sha256(machine_path.read_bytes().strip()).hexdigest()
ss_result = run(["ss", "-lntp"])
if ss_result.returncode:
    raise SystemExit("ss -lntp failed")

network_result = run(["docker", "network", "inspect", args.docker_network])
bridges, bindings = [], {}
if network_result.returncode == 0:
    network = json.loads(network_result.stdout)[0]
    gateway = str(network.get("IPAM", {}).get("Config", [{}])[0].get("Gateway", ""))
    if gateway:
        bridges.append(gateway); bindings["host-gateway"] = gateway

inspect = run(["docker", "inspect", args.vlm_container])
if inspect.returncode:
    raise SystemExit("configured VLM container is not running/inspectable")
container = json.loads(inspect.stdout)[0]
image_inspect = run(["docker", "image", "inspect", str(container.get("Image", ""))])
image_details = json.loads(image_inspect.stdout)[0] if image_inspect.returncode == 0 else {}
repo_digests = image_details.get("RepoDigests") or []
observed_image_id = image_details.get("Id", "")
is_local_id = args.expected_vlm_image.startswith("sha256:") and "@" not in args.expected_vlm_image
actual_image = args.expected_vlm_image if ((is_local_id and observed_image_id == args.expected_vlm_image) or (not is_local_id and canonical_repo_digest(args.expected_vlm_image) in {canonical_repo_digest(x) for x in repo_digests})) else "unverified"
container_running = bool(container.get("State", {}).get("Running"))
published_ports = container.get("NetworkSettings", {}).get("Ports", {})
networks = container.get("NetworkSettings", {}).get("Networks", {})
api_key = args.vlm_api_key_file.read_text().strip() if args.vlm_api_key_file else ""
checks = {}
for name, path in (("health", "/health"), ("backend", "/health/backend"), ("queue", "/queue/status")):
    checks[name] = vlm_response(args.vlm_container,path)[0] == 200
closed_status,closed=vlm_response(args.vlm_container,'/health/chat')
open_status,opened=vlm_response(args.vlm_container,'/health/chat',api_key)
checks['auth_closed'] = closed_status == 200 and closed.get('enabled') is True and closed.get('auth_configured') is True and closed.get('status') == 'auth_failed' and closed.get('auth_ok') is False
model_identity=opened.get('model',opened.get('model_alias'))
checks['auth_interface'] = open_status == 200 and opened.get('enabled') is True and opened.get('auth_configured') is True and opened.get('status') == 'ok' and opened.get('auth_ok') is True and model_identity == args.expected_gpu_model_alias

compose_containers={}
for service, container_id, expected_ip in (("ingress",args.ingress_container,"172.30.0.10"),("backend",args.backend_container,"172.30.0.20"),("vlm-wrapper",args.vlm_container,"172.30.0.30"),("gpu-model",args.gpu_model_container,"172.30.0.40")):
    observed=json.loads(run(["docker","inspect",container_id]).stdout)[0]
    observed_image=json.loads(run(["docker","image","inspect",str(observed.get("Image",""))]).stdout)[0]
    labels=observed.get("Config",{}).get("Labels",{}) or {}
    net=observed.get("NetworkSettings",{}).get("Networks",{}).get("seraph-core-prod",{})
    compose_containers[service]={"container_id":observed.get("Id",""),"image_id":observed_image.get("Id",""),"image_revision":(observed_image.get("Config",{}).get("Labels",{}) or {}).get("org.opencontainers.image.revision","") ,"repo_digests":observed_image.get("RepoDigests",[]) or [],"project":labels.get("com.docker.compose.project",""),"service":labels.get("com.docker.compose.service",""),"network_name":"seraph-core-prod" if net else "","network_id":net.get("NetworkID",""),"ip_address":net.get("IPAddress",""),"expected_ip":expected_ip}

payload = {
    "local_hostname": socket.gethostname(), "machine_identity_sha256": machine_digest,
    "captured_at": datetime.now(timezone.utc).isoformat(), "ss_lntp": ss_result.stdout,
    "docker_bridge_addresses": bridges, "docker_network_bindings": bindings,
    "vlm_image": actual_image, "vlm_interface_contract": "vlm-health-backend-queue-chat-auth-v1",
    "vlm_wrapper_contract_verified": container_running and actual_image != "unverified" and all(checks.values()),
    "vlm_observation": {"container": args.vlm_container, "container_id": container.get("Id", ""), "image_id": observed_image_id, "running": container_running, "repo_digests": repo_digests, "networks": networks, "published_ports": published_ports, "checks": checks},
    "gpu_release":gpu_release,
    "gpu_model_observation":{"image_ref":args.expected_gpu_model_image,"image_id":compose_containers["gpu-model"]["image_id"],"alias":args.expected_gpu_model_alias,"health":gpu_healthy(args.gpu_model_container)},
    "compose_observation":{"project":"seraph-prod","network_name":"seraph-core-prod","containers":compose_containers},
}
print(json.dumps(payload, indent=2, sort_keys=True))
