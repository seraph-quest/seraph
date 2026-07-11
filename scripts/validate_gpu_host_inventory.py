#!/usr/bin/env python3
"""Validate an operator-produced GPU host listener/firewall receipt."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone


def fail(message: str) -> None:
    raise SystemExit(f"GPU host inventory invalid: {message}")


receipt = json.load(sys.stdin)
if any(key in receipt for key in ("machine_id", "raw_machine_id", "machine_identity_raw")):
    fail("raw machine identity material is forbidden")
expected_host = os.environ.get("SERAPH_GPU_EXPECTED_HOSTNAME", "")
expected_identity = os.environ.get("SERAPH_GPU_MACHINE_IDENTITY_SHA256", "")
expected_vlm_image = os.environ.get("SERAPH_VLM_IMAGE", "")
expected_vlm_contract = os.environ.get("SERAPH_VLM_INTERFACE_CONTRACT", "")
if not all((expected_host, expected_identity, expected_vlm_image, expected_vlm_contract)):
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
bridge_addresses = {str(item) for item in receipt.get("docker_bridge_addresses", [])}
if not bridge_addresses:
    fail("Docker bridge address inventory is missing")
bindings = receipt.get("docker_network_bindings", {})
if bindings.get("host-gateway") not in bridge_addresses:
    fail("current host-gateway binding is absent from Docker bridge inventory")
allowed_addresses = {"127.0.0.1", "::1", "[::1]"} | bridge_addresses
for line in ss_lntp.splitlines():
    match = re.search(r"LISTEN\s+\d+\s+\d+\s+(\S+):(8000|8001|8004)\b", line)
    if not match:
        continue
    address, port = match.groups()
    if address not in allowed_addresses:
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
elif expected_vlm_image not in vlm_observation.get("repo_digests", []):
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
expected={"ingress":"172.30.0.10","backend":"172.30.0.20","vlm-wrapper":"172.30.0.30"}
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
print("GPU host inventory valid: local identity, ss listeners, firewall receipt, and wrapper contract verified")
