#!/usr/bin/env python3
"""Validate an operator-produced GPU host listener/firewall receipt."""

from __future__ import annotations

import json
import hashlib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


def fail(message: str) -> None:
    raise SystemExit(f"GPU host inventory invalid: {message}")


def canonical_repo_digest(reference: str) -> str:
    if "@sha256:" not in reference: return reference
    name,digest=reference.rsplit("@",1); slash=name.rfind("/"); colon=name.rfind(":")
    if colon > slash: name=name[:colon]
    return name+"@"+digest


def repo_digest_matches(configured: str, observed: list[str]) -> bool:
    return canonical_repo_digest(configured) in {canonical_repo_digest(value) for value in observed}


receipt = json.load(sys.stdin)
if any(key in receipt for key in ("machine_id", "raw_machine_id", "machine_identity_raw")):
    fail("raw machine identity material is forbidden")
expected_host = os.environ.get("SERAPH_GPU_EXPECTED_HOSTNAME", "")
expected_identity = os.environ.get("SERAPH_GPU_MACHINE_IDENTITY_SHA256", "")
expected_vlm_image = os.environ.get("SERAPH_VLM_IMAGE", "")
expected_gpu_model_image = os.environ.get("SERAPH_GPU_MODEL_IMAGE", "")
expected_gpu_model_alias = os.environ.get("SERAPH_GPU_MODEL_ALIAS", "")
expected_vlm_contract = os.environ.get("SERAPH_VLM_INTERFACE_CONTRACT", "")
if not all((expected_host, expected_identity, expected_vlm_image, expected_vlm_contract, expected_gpu_model_image, expected_gpu_model_alias)):
    fail("configured local hostname, machine identity, VLM digest, and interface contract are required")
if receipt.get("local_hostname") != expected_host:
    fail("local hostname does not match configured host")
if receipt.get("machine_identity_sha256") != expected_identity:
    fail("local machine identity does not match configured digest")
try:
    captured = datetime.fromisoformat(str(receipt["captured_at"]).replace("Z", "+00:00"))
    if captured.tzinfo is None or captured.utcoffset() != timezone.utc.utcoffset(captured):
        fail("captured_at must carry an explicit UTC offset")
    age = (datetime.now(timezone.utc) - captured.astimezone(timezone.utc)).total_seconds()
except (KeyError, TypeError, ValueError):
    fail("captured_at must be an ISO-8601 UTC timestamp")
max_age = int(os.environ.get("SERAPH_HOST_INVENTORY_MAX_AGE_SECONDS", "900"))
if age < -30 or (age > max_age and os.environ.get("SERAPH_ALLOW_PREVIOUS_ACCEPTED_RECEIPT") != "true"):
    fail("inventory receipt is stale or from the future")
ss_lntp = str(receipt.get("ss_lntp", ""))
if not ss_lntp.strip():
    fail("raw ss -lntp receipt is missing")

unsafe = []
for line in ss_lntp.splitlines():
    match = re.search(r"LISTEN\s+\d+\s+\d+\s+(\S+):(8000|8001|8004)\b", line)
    if not match:
        continue
    address, port = match.groups()
    unsafe.append({"address": address, "port": int(port), "line": line})
if unsafe:
    fail(f"private ports have wildcard/LAN listeners: {unsafe}")

if receipt.get("vlm_image") != expected_vlm_image:
    fail("managed VLM wrapper immutable reference does not match configured release")
if receipt.get("vlm_interface_contract") != expected_vlm_contract:
    fail("managed VLM wrapper interface contract does not match configured contract")
if receipt.get("vlm_wrapper_contract_verified") is not True:
    fail("managed VLM wrapper image/interface contract is unverified")
vlm_observation = receipt.get("vlm_observation", {})
if not vlm_observation.get("image_id"): fail("managed VLM observed image ID missing")
if expected_vlm_image.startswith("sha256:") and "@" not in expected_vlm_image:
    if vlm_observation.get("image_id") != expected_vlm_image: fail("managed VLM local image ID mismatch")
elif not repo_digest_matches(expected_vlm_image, vlm_observation.get("repo_digests", [])):
    fail("managed VLM RepoDigest mismatch")
if vlm_observation.get("running") is not True:
    fail("managed VLM wrapper container is not running")
if any(value for value in vlm_observation.get("published_ports", {}).values()):
    fail("managed VLM wrapper still publishes a host port")
checks = vlm_observation.get("checks", {})
if not all(checks.get(name) is True for name in ("health", "backend", "queue", "auth_closed", "auth_interface")):
    fail("managed VLM wrapper active interface checks are incomplete")
compose = receipt.get("compose_observation", {})
if compose.get("project") != "seraph-prod" or compose.get("network_name") != "seraph-core-prod":
    fail("compose project/network identity mismatch")
containers=compose.get("containers",{})
expected={"ingress":"172.30.0.10","backend":"172.30.0.20","vlm-wrapper":"172.30.0.30","gpu-model":"172.30.0.40"}
network_ids=set()
app_revisions=set()
for service,ip in expected.items():
    item=containers.get(service,{})
    if not item.get("container_id") or item.get("project")!="seraph-prod" or item.get("service")!=service or item.get("network_name")!="seraph-core-prod" or item.get("ip_address")!=ip:
        fail(f"compose container binding mismatch: {service}")
    network_ids.add(item.get("network_id"))
    if not item.get("image_id"): fail(f"container image ID missing: {service}")
    if service in {"ingress","backend"}: app_revisions.add(item.get("image_revision"))
if len(network_ids)!=1 or not next(iter(network_ids),""):
    fail("compose network ID mismatch")
if len(app_revisions)!=1 or not next(iter(app_revisions),""):
    fail("application image revision mismatch")
gpu=receipt.get("gpu_model_observation",{})
if gpu.get("image_ref")!=expected_gpu_model_image or not repo_digest_matches(expected_gpu_model_image,containers.get("gpu-model",{}).get("repo_digests",[])) or gpu.get("image_id")!=containers.get("gpu-model",{}).get("image_id") or gpu.get("alias")!=expected_gpu_model_alias or gpu.get("health") is not True:
    fail("GPU model immutable identity/health mismatch")
def artifact(path):
    p=Path(path); h=hashlib.sha256()
    with p.open('rb') as stream:
        for chunk in iter(lambda:stream.read(8*1024*1024),b''): h.update(chunk)
    return {'filename':p.name,'sha256':h.hexdigest(),'size':p.stat().st_size}
configured_model_dir=Path(os.environ['SERAPH_GPU_MODEL_DIR'])
if not configured_model_dir.is_absolute(): fail('GPU artifact root must be an absolute existing directory')
model_dir=configured_model_dir.resolve(strict=True)
if not model_dir.is_dir(): fail('GPU artifact root must be an absolute existing directory')
expected_release={'image_ref':expected_gpu_model_image,'alias':expected_gpu_model_alias,'artifact_root':str(model_dir),'model':artifact(model_dir/os.environ['SERAPH_GPU_MODEL_FILE']),'mmproj':artifact(model_dir/os.environ['SERAPH_GPU_MMPROJ_FILE']),'ctx_size':int(os.environ.get('SERAPH_GPU_MODEL_CTX_SIZE','32768')),'layers':int(os.environ.get('SERAPH_GPU_MODEL_LAYERS','999')),'command_contract':'llama-server-gemma4-v1'}
if receipt.get('gpu_release')!=expected_release: fail('GPU release artifact manifest mismatch')
print("GPU host inventory valid: local identity, ss listeners, firewall receipt, and wrapper contract verified")
