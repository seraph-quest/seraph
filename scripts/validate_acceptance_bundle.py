#!/usr/bin/env python3
import hashlib
import json
import os
import subprocess
import sys
import tempfile


def fail(message):
    raise SystemExit("acceptance bundle invalid: " + message)


bundle = json.load(sys.stdin)
if bundle.get("schema") != "seraph.production-acceptance.v1":
    fail("schema")
if bundle.get("app_sha") != os.environ.get("SERAPH_BUNDLE_APP_SHA") or bundle.get("vlm_image") != os.environ.get("SERAPH_BUNDLE_VLM_IMAGE"):
    fail("release tuple mismatch")
if bundle.get("compose_project") != "seraph-prod" or bundle.get("network_name") != "seraph-core-prod":
    fail("compose identity mismatch")
if bundle.get("expected_origin") != os.environ.get("SERAPH_EXPECTED_HTTPS_ORIGIN") or bundle.get("client_identity") != os.environ.get("SERAPH_MAC_PROBE_CLIENT_ID"):
    fail("operator acceptance identity mismatch")

for kind in ("challenged_attestation", "final_attestation", "mac_receipt"):
    item = bundle.get(kind, {})
    try:
        data = open(item["path"], "rb").read()
    except Exception:
        fail(kind + " missing")
    if hashlib.sha256(data).hexdigest() != item.get("sha256"):
        fail(kind + " hash mismatch")

ids = bundle.get("container_ids", {})
if set(ids) != {"ingress", "backend", "vlm-wrapper"} or not all(ids.values()):
    fail("container identity incomplete")
expected_ips = {"ingress": "172.30.0.10", "backend": "172.30.0.20", "vlm-wrapper": "172.30.0.30"}
def validate_attestation(kind):
    local = json.load(open(bundle[kind]["path"]))
    compose = local.get("compose_observation", {})
    if compose.get("project") != "seraph-prod" or compose.get("network_name") != "seraph-core-prod": fail(kind + " compose identity mismatch")
    observed = compose.get("containers", {}); network_ids = set()
    for service, expected_ip in expected_ips.items():
        item = observed.get(service, {})
        if item.get("container_id") != ids[service] or item.get("project") != "seraph-prod" or item.get("service") != service or item.get("network_name") != "seraph-core-prod" or item.get("ip_address") != expected_ip: fail(kind + " container binding mismatch: " + service)
        if not item.get("image_id"): fail(kind + " image identity missing: " + service)
        network_ids.add(item.get("network_id"))
    if network_ids != {bundle.get("network_id")}: fail(kind + " network ID mismatch")
    for service in ("ingress", "backend"):
        if observed[service].get("image_revision") != bundle["app_sha"]: fail(kind + " application image identity mismatch: " + service)
    vlm_observation = local.get("vlm_observation", {}); configured_vlm = bundle["vlm_image"]
    if not vlm_observation.get("image_id"): fail(kind + " VLM observed image ID missing")
    if configured_vlm.startswith("sha256:") and "@" not in configured_vlm:
        if vlm_observation.get("image_id") != configured_vlm: fail(kind + " VLM local image ID mismatch")
    elif configured_vlm not in vlm_observation.get("repo_digests", []): fail(kind + " VLM RepoDigest mismatch")
    immutable = {"project":compose.get("project"),"network_name":compose.get("network_name"),"containers":{s:{k:observed[s].get(k) for k in ("container_id","image_id","image_revision","project","service","network_name","network_id","ip_address")} for s in expected_ips},"vlm_configured_ref":configured_vlm,"vlm_observed_image_id":vlm_observation.get("image_id")}
    return local, observed, immutable

challenged, challenged_observed, challenged_immutable = validate_attestation("challenged_attestation")
local, observed, final_immutable = validate_attestation("final_attestation")
if challenged_immutable != final_immutable: fail("challenged/final immutable binding mismatch")

challenge = bundle.get("acceptance_challenge", {})
expected_challenge_fields = {
    "schema": "seraph.acceptance-challenge.v1",
    "stage": bundle.get("stage"),
    "app_sha": bundle["app_sha"],
    "vlm_image": bundle["vlm_image"],
    "local_attestation_sha256": bundle["challenged_attestation"]["sha256"],
    "compose_project": bundle["compose_project"],
    "network_name": bundle["network_name"],
    "network_id": bundle["network_id"],
    "container_ids": ids,
    "image_identities": {service: {"image_id": observed[service]["image_id"], "image_revision": observed[service].get("image_revision", "")} for service in expected_ips},
    "expected_origin": bundle["expected_origin"],
    "lan_host": os.environ.get("SERAPH_LAN_HOST"),
    "lan_ip": os.environ.get("SERAPH_LAN_IP"),
    "client_identity": bundle["client_identity"],
}
if bundle.get("stage") not in {"candidate", "rollback", "restore", "restart"}:
    fail("challenge stage mismatch")
for field, expected in expected_challenge_fields.items():
    if challenge.get(field) != expected:
        fail("challenge immutable field mismatch: " + field)
if not challenge.get("server_nonce") or not challenge.get("issued_at"):
    fail("challenge authority incomplete")

env = os.environ.copy()
env["SERAPH_MAC_RECEIPT_HISTORICAL"] = "true"
with tempfile.NamedTemporaryFile("w", encoding="utf-8") as expected_file:
    json.dump(challenge, expected_file, sort_keys=True)
    expected_file.flush()
    env["SERAPH_ACCEPTANCE_CHALLENGE_FILE"] = expected_file.name
    validator = os.path.join(os.path.dirname(__file__), "validate_mac_lan_negative.py")
    mac_result = subprocess.run([sys.executable, validator], input=open(bundle["mac_receipt"]["path"]).read(), text=True, capture_output=True, env=env)
if mac_result.returncode:
    fail("historical Mac signature/challenge invalid: " + mac_result.stderr.strip())
print("acceptance bundle valid")
