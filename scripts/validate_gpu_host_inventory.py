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
expected_host = os.environ.get("SERAPH_GPU_SSH_HOST", "")
expected_fingerprint = os.environ.get("SERAPH_GPU_SSH_HOST_FINGERPRINT", "")
expected_vlm_image = os.environ.get("SERAPH_VLM_IMAGE", "")
expected_vlm_contract = os.environ.get("SERAPH_VLM_INTERFACE_CONTRACT", "")
if not all((expected_host, expected_fingerprint, expected_vlm_image, expected_vlm_contract)):
    fail("configured host identity, fingerprint, VLM digest, and interface contract are required")
if receipt.get("ssh_host") != expected_host:
    fail("SSH host identity does not match configured host")
if receipt.get("host_key_verified") is not True:
    fail("host key was not verified")
if receipt.get("host_key_fingerprint") != expected_fingerprint:
    fail("host key fingerprint does not match configured fingerprint")
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

firewall = receipt.get("firewall", {})
for port in (8000, 8001, 8004):
    if firewall.get(f"lan_ingress_{port}") != "blocked":
        fail(f"firewall receipt does not prove LAN ingress {port} blocked")
if receipt.get("vlm_image") != expected_vlm_image:
    fail("managed VLM wrapper digest does not match configured release")
if receipt.get("vlm_interface_contract") != expected_vlm_contract:
    fail("managed VLM wrapper interface contract does not match configured contract")
if receipt.get("vlm_wrapper_contract_verified") is not True:
    fail("managed VLM wrapper image/interface contract is unverified")
print("GPU host inventory valid: host key, ss listeners, firewall gates, and wrapper contract verified")
