#!/usr/bin/env python3
"""Observe local host/firewall/wrapper state and emit a sanitized receipt."""
from __future__ import annotations

import argparse, hashlib, json, socket, subprocess, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path

def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False)

def get(url: str, key: str = "") -> int:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5) as response:  # noqa: S310
            response.read(4096); return response.status
    except urllib.error.HTTPError as exc:
        return exc.code

p = argparse.ArgumentParser()
p.add_argument("--expected-vlm-image", required=True)
p.add_argument("--vlm-container", required=True)
p.add_argument("--ingress-container", required=True)
p.add_argument("--backend-container", required=True)
p.add_argument("--vlm-api-key-file", type=Path)
p.add_argument("--docker-network", default="bridge")
args = p.parse_args()

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
actual_image = args.expected_vlm_image if ((is_local_id and observed_image_id == args.expected_vlm_image) or (not is_local_id and args.expected_vlm_image in repo_digests)) else "unverified"
container_running = bool(container.get("State", {}).get("Running"))
published_ports = container.get("NetworkSettings", {}).get("Ports", {})
networks = container.get("NetworkSettings", {}).get("Networks", {})
container_ip = next((str(value.get("IPAddress", "")) for value in networks.values() if value.get("IPAddress")), "")
base_url = f"http://{container_ip}:8001" if container_ip else ""
api_key = args.vlm_api_key_file.read_text().strip() if args.vlm_api_key_file else ""
checks = {}
for name, path in (("health", "/health"), ("backend", "/health/backend"), ("queue", "/queue/status")):
    try: checks[name] = get(base_url + path) == 200
    except Exception: checks[name] = False
try:
    checks['auth_closed'] = get(base_url + '/health/chat') in (401,403)
    checks['auth_interface'] = get(base_url + '/health/chat', api_key) == 200
except Exception:
    checks['auth_closed'] = checks['auth_interface'] = False

compose_containers={}
for service, container_id, expected_ip in (("ingress",args.ingress_container,"172.30.0.10"),("backend",args.backend_container,"172.30.0.20"),("vlm-wrapper",args.vlm_container,"172.30.0.30")):
    observed=json.loads(run(["docker","inspect",container_id]).stdout)[0]
    observed_image=json.loads(run(["docker","image","inspect",str(observed.get("Image",""))]).stdout)[0]
    labels=observed.get("Config",{}).get("Labels",{}) or {}
    net=observed.get("NetworkSettings",{}).get("Networks",{}).get("seraph-core-prod",{})
    compose_containers[service]={"container_id":observed.get("Id",""),"image_id":observed_image.get("Id",""),"image_revision":(observed_image.get("Config",{}).get("Labels",{}) or {}).get("org.opencontainers.image.revision","") ,"project":labels.get("com.docker.compose.project",""),"service":labels.get("com.docker.compose.service",""),"network_name":"seraph-core-prod" if net else "","network_id":net.get("NetworkID",""),"ip_address":net.get("IPAddress",""),"expected_ip":expected_ip}

payload = {
    "local_hostname": socket.gethostname(), "machine_identity_sha256": machine_digest,
    "captured_at": datetime.now(timezone.utc).isoformat(), "ss_lntp": ss_result.stdout,
    "docker_bridge_addresses": bridges, "docker_network_bindings": bindings,
    "vlm_image": actual_image, "vlm_interface_contract": "vlm-health-backend-queue-chat-auth-v1",
    "vlm_wrapper_contract_verified": container_running and actual_image != "unverified" and all(checks.values()),
    "vlm_observation": {"container": args.vlm_container, "container_id": container.get("Id", ""), "image_id": observed_image_id, "running": container_running, "repo_digests": repo_digests, "networks": networks, "published_ports": published_ports, "checks": checks},
    "compose_observation":{"project":"seraph-prod","network_name":"seraph-core-prod","containers":compose_containers},
}
print(json.dumps(payload, indent=2, sort_keys=True))
