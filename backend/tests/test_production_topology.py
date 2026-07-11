import json
import os
import hashlib
import hmac
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone


ROOT = Path(__file__).resolve().parents[2]

def _signed_mac_receipt(tmp_path: Path, captured_at: str, *, open_port: str = "", denied_error: str = "connection_refused", origin: str = "https://seraph.lan", host: str = "seraph.lan", client: str = "operator-mac", nonce: str = "a" * 48) -> tuple[Path, dict[str, str]]:
    key_file=tmp_path/'probe.key'; key_file.write_bytes(b'k'*32)
    challenge={'schema':'seraph.acceptance-challenge.v1','stage':'candidate','expected_origin':'https://seraph.lan','lan_host':'seraph.lan','lan_ip':'192.168.1.26','client_identity':'operator-mac','server_nonce':'f'*64}
    challenge_file=tmp_path/'challenge.json'; challenge_file.write_text(json.dumps(challenge))
    challenge_hash=hashlib.sha256(json.dumps(challenge,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    receipt={'schema':'seraph.mac-lan-acceptance.v1','challenge':challenge,'challenge_sha256':challenge_hash,'https_origin':origin,'lan_host':host,'lan_ip':'192.168.1.26','client_identity':client,'captured_at':captured_at,'nonce':nonce,'authenticated_session':True,'port_probes':{p:{'connected':p==open_port,'error_class':'' if p==open_port else denied_error,'latency_ms':1} for p in ('8000','8001','8004')}}
    receipt['hmac_sha256']=hmac.new(key_file.read_bytes(),json.dumps(receipt,sort_keys=True,separators=(',',':')).encode(),hashlib.sha256).hexdigest()
    path=tmp_path/('mac-'+nonce[:8]+'-'+client+'-'+captured_at.replace(':','_')+'.json'); path.write_text(json.dumps(receipt))
    env=os.environ.copy(); env.update(SERAPH_EXPECTED_HTTPS_ORIGIN='https://seraph.lan',SERAPH_LAN_HOST='seraph.lan',SERAPH_LAN_IP='192.168.1.26',SERAPH_MAC_PROBE_CLIENT_ID='operator-mac',SERAPH_MAC_PROBE_KEY_FILE=str(key_file),SERAPH_ACCEPTANCE_CHALLENGE_FILE=str(challenge_file))
    return path,env


def test_production_compose_has_one_tls_ingress_and_no_backend_publication():
    compose = (ROOT / "docker-compose.prod.yaml").read_text()
    assert '"${SERAPH_HTTPS_BIND:-0.0.0.0}:${SERAPH_HTTPS_PORT:-443}:443"' in compose
    backend = compose.split("\n  backend:\n", 1)[1].split("\nnetworks:\n", 1)[0]
    assert "ports:" not in backend
    assert "OPERATOR_AUTH_COOKIE_SECURE: \"true\"" in backend
    assert "OPERATOR_AUTH_BACKEND_WORKERS: \"1\"" in backend
    assert 'OPERATOR_AUTH_TRUSTED_PROXY_IPS: "172.30.0.10"' in backend
    assert "--reload" not in (ROOT / "backend" / "Dockerfile").read_text()
    assert 'CHAT_PROXY_API_KEY="$$(cat /run/secrets/vlm_api_key)"' in compose
    assert "CHAT_PROXY_API_KEY_FILE" not in compose
    assert "npm\", \"run\", \"dev" not in (ROOT / "frontend" / "Dockerfile.prod").read_text()


def test_ingress_overwrites_forwarded_identity_and_keeps_model_routes_server_side():
    nginx = (ROOT / "frontend" / "production" / "nginx.conf").read_text()
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in nginx
    assert "proxy_add_x_forwarded_for" not in nginx
    assert "location /api/" in nginx
    assert "location /ws/" in nginx
    health = nginx.split("location = /health", 1)[1].split("location /api/", 1)[0]
    assert "proxy_pass http://seraph_backend/health;" in health
    assert "try_files" not in health
    app = (ROOT / "backend" / "src" / "app.py").read_text()
    health_route = app.split('@app.get("/health")', 1)[1].split('@app.get("/api/runtime/status")', 1)[0]
    assert 'return {"status": "ok"}' in health_route
    assert "8000" not in nginx
    assert "8001" not in nginx


def test_production_example_uses_file_backed_secrets_and_exact_origin_placeholders():
    env = (ROOT / "env.prod.example").read_text()
    assert "OPERATOR_AUTH_SECRET_FILE=" in env
    assert "SERAPH_TLS_CERT_FILE=" in env
    assert "SERAPH_TLS_KEY_FILE=" in env
    assert "OPERATOR_AUTH_ALLOWED_ORIGINS=https://seraph.example.invalid" in env
    assert "LOCAL_LLM_API_BASE=http://host.docker.internal:8000/v1" in env
    assert "SERAPH_VLM_BASE_URL=http://vlm-wrapper:8001" in env
    assert "SERAPH_VLM_IMAGE=" in env
    assert "@sha256:" in env


def test_managed_start_probes_private_gpu_routes_and_rolls_back_on_failure():
    manage = (ROOT / "manage.sh").read_text()
    assert "production_preflight.py" in manage
    assert "candidate GPU model/VLM preflight failed" in manage
    preflight = (ROOT / "backend" / "production_preflight.py").read_text()
    assert 'probe("gpu_model"' in preflight
    assert 'probe("vlm_wrapper"' in preflight
    assert 'probe("vlm_backend"' in preflight
    assert "Authorization" in preflight
    assert "validate_production_listeners.py" in manage
    assert "validate_gpu_host_inventory.py" in manage
    restart_case = manage.split('restart)', 1)[1].split(';;', 1)[0]
    assert "production_restart" in restart_case
    assert " down " not in restart_case
    assert "restoring previous production release" in manage
    start = manage.split("function production_start()", 1)[1].split("function production_rollback()", 1)[0]
    assert start.index("production_prepare_app_images") < start.index(" up -d --no-build --wait")
    assert start.index(" up -d --no-build --wait") < start.index("production_preflight.py")
    assert start.index("production_restore_after_failure") > start.index("candidate containers did not become healthy")


def test_release_tuple_failure_paths_restore_or_emit_catastrophic_diagnostics():
    manage = (ROOT / "manage.sh").read_text()
    restore = manage.split("function production_restore_previous()", 1)[1].split("function production_start()", 1)[0]
    assert 'SERAPH_IMAGE_TAG="$previous_tag" SERAPH_VLM_IMAGE="$previous_vlm"' in restore
    assert "CATASTROPHIC: previous production release tuple could not be restored" in restore
    assert "return 2" in restore
    rollback = manage.split("function production_rollback()", 1)[1].split("if [ \"${SERAPH_MANAGE_SOURCE_ONLY", 1)[0]
    assert "requested rollback tuple failed; restoring original active tuple" in rollback
    assert 'production_restore_after_failure "$original_tag" "$original_vlm" "$original_receipt"' in rollback
    assert "seraph-prod-failed-rollback.log" in rollback
    assert 'production_host_inventory_validate "$target_attestation" "$vlm_image"' in rollback
    assert "rollback awaiting fresh Mac LAN acceptance" in rollback


def test_release_identity_requires_clean_exact_head_and_hex_vlm_digest():
    manage = (ROOT / "manage.sh").read_text()
    assert 'git -C "$SCRIPT_DIR" rev-parse HEAD' in manage
    assert 'git -C "$SCRIPT_DIR" status --porcelain' in manage
    assert 'SERAPH_IMAGE_TAG:-}" =~ ^[0-9a-f]{40}$' in manage
    assert 'sha256:[0-9a-fA-F]{64})$' in manage
    assert "registry RepoDigest or exact local sha256 image ID" in manage
    assert "mismatched build identity; refusing overwrite" in manage
    non_hex_digest = "ghcr.io/example/wrapper@sha256:" + "z" * 64
    assert not __import__("re").match(r"^[^\s@]+@sha256:[0-9a-fA-F]{64}$", non_hex_digest)


def test_candidate_generator_observes_container_and_auth_without_firewall_claims():
    generator = (ROOT / "scripts" / "generate_local_gpu_inventory.py").read_text()
    assert 'default="bridge"' in generator
    assert 'default="seraph-core-prod"' not in generator
    assert '"nft"' not in generator
    assert "--lan-ingress-" not in generator
    assert "--vlm-contract-verified" not in generator
    assert 'run(["docker", "inspect", args.vlm_container])' in generator
    assert "'/health/chat'" in generator
    assert "auth_closed" in generator


def test_restore_adoption_inventory_and_atomic_state_failure_contracts():
    manage = (ROOT / "manage.sh").read_text()
    restore = manage.split("function production_restore_previous()", 1)[1].split("function production_restore_after_failure()", 1)[0]
    assert "production_preflight.py" in restore
    assert "seraph-prod-failed-restore.log" in restore
    assert "failed inference preflight" in restore
    assert '["final_attestation"]["path"]' in restore
    assert '["local_attestation"]["path"]' not in restore
    start = manage.split("function production_start()", 1)[1].split("function production_write_accepted_state()", 1)[0]
    refusal = start.index("explicit adoption is required")
    assert refusal < start.index("production_prepare_app_images")
    assert "production_docker_inventory_validate true" in start
    assert "production_compose_state_validate" in start
    writer = manage.split("function production_write_accepted_state()", 1)[1].split("function production_rollback()", 1)[0]
    assert writer.index('cp "$receipt" "$receipt_tmp"') < writer.index('mv "$receipt_tmp" "$receipt_copy"')
    assert writer.index('printf \'%s\\n%s\\n%s\\n\'') < writer.index('mv "$state_tmp" "$state_file"')
    assert "chmod 0444" in writer and "chmod 0600" in writer


def test_compose_state_validator_requires_complete_healthy_runtime():
    validator = ROOT / "scripts" / "validate_production_compose_state.py"
    rows = [{"Service": service, "State": "running", "Health": "healthy"} for service in ("backend", "ingress", "vlm-wrapper")]
    good = subprocess.run([sys.executable, str(validator)], input=json.dumps(rows), text=True, capture_output=True)
    assert good.returncode == 0
    rows[0]["Health"] = "unhealthy"
    bad = subprocess.run([sys.executable, str(validator)], input=json.dumps(rows), text=True, capture_output=True)
    assert bad.returncode != 0


def test_compose_state_validator_rejects_duplicate_missing_and_extra_rows():
    validator = ROOT / "scripts" / "validate_production_compose_state.py"
    healthy = lambda service: {"Service": service, "State": "running", "Health": "healthy"}
    cases = [
        [healthy("backend"), healthy("backend"), healthy("ingress")],
        [healthy("backend"), healthy("ingress")],
        [healthy("backend"), healthy("ingress"), healthy("vlm-wrapper"), healthy("extra")],
    ]
    for rows in cases:
        result = subprocess.run(
            [sys.executable, str(validator)],
            input=json.dumps(rows),
            text=True,
            capture_output=True,
        )
        assert result.returncode != 0
        assert "exactly one row per service" in result.stderr


def test_acceptance_bundle_cross_binds_local_container_and_network_evidence(tmp_path):
    ids={s:s+'-id' for s in ('ingress','backend','vlm-wrapper')}; ips={'ingress':'172.30.0.10','backend':'172.30.0.20','vlm-wrapper':'172.30.0.30'}; app='a'*40
    vlm='repo/vlm@sha256:'+'b'*64
    local={"captured_at":"2026-01-01T00:00:00+00:00","compose_observation":{"project":"seraph-prod","network_name":"seraph-core-prod","containers":{s:{"container_id":ids[s],"image_id":s+'-image',"image_revision":app if s in {'ingress','backend'} else '',"project":"seraph-prod","service":s,"network_name":"seraph-core-prod","network_id":"net-id","ip_address":ips[s]} for s in ids}},"vlm_observation":{"image_id":"vlm-wrapper-image","repo_digests":[vlm]}}
    local_path=tmp_path/'challenged.json'; local_path.write_text(json.dumps(local)); final_path=tmp_path/'final.json'; final={**local,"captured_at":"2026-01-01T00:01:00+00:00"}; final_path.write_text(json.dumps(final))
    mac_path, mac_env = _signed_mac_receipt(tmp_path, datetime.now(timezone.utc).isoformat())
    sha=lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    challenge={"schema":"seraph.acceptance-challenge.v1","stage":"candidate","app_sha":app,"vlm_image":vlm,"local_attestation_sha256":sha(local_path),"compose_project":"seraph-prod","network_name":"seraph-core-prod","network_id":"net-id","container_ids":ids,"image_identities":{s:{"image_id":s+'-image',"image_revision":app if s in {'ingress','backend'} else ''} for s in ids},"expected_origin":"https://seraph.lan","lan_host":"seraph.lan","lan_ip":"192.168.1.26","client_identity":"operator-mac","server_nonce":"f"*64,"issued_at":datetime.now(timezone.utc).isoformat()}
    mac=json.loads(mac_path.read_text()); mac["challenge"]=challenge; mac["challenge_sha256"]=hashlib.sha256(json.dumps(challenge,sort_keys=True,separators=(',',':')).encode()).hexdigest(); mac.pop("hmac_sha256"); mac["hmac_sha256"]=hmac.new(Path(mac_env["SERAPH_MAC_PROBE_KEY_FILE"]).read_bytes(),json.dumps(mac,sort_keys=True,separators=(',',':')).encode(),hashlib.sha256).hexdigest(); mac_path.write_text(json.dumps(mac))
    bundle={"schema":"seraph.production-acceptance.v1","stage":"candidate","app_sha":app,"vlm_image":vlm,"compose_project":"seraph-prod","network_name":"seraph-core-prod","network_id":"net-id","container_ids":ids,"challenged_attestation":{"path":str(local_path),"sha256":sha(local_path)},"final_attestation":{"path":str(final_path),"sha256":sha(final_path)},"mac_receipt":{"path":str(mac_path),"sha256":sha(mac_path)},"acceptance_challenge":challenge,"expected_origin":"https://seraph.lan","client_identity":"operator-mac"}
    env=os.environ.copy(); env.update(mac_env); env.update(SERAPH_BUNDLE_APP_SHA=app,SERAPH_BUNDLE_VLM_IMAGE=vlm,SERAPH_EXPECTED_HTTPS_ORIGIN='https://seraph.lan',SERAPH_MAC_PROBE_CLIENT_ID='operator-mac',SERAPH_LAN_HOST='seraph.lan',SERAPH_LAN_IP='192.168.1.26')
    validator=ROOT/'scripts'/'validate_acceptance_bundle.py'
    assert subprocess.run([sys.executable,str(validator)],input=json.dumps(bundle),env=env,text=True).returncode == 0
    bundle['container_ids']['backend']='swapped'
    assert subprocess.run([sys.executable,str(validator)],input=json.dumps(bundle),env=env,text=True,capture_output=True).returncode != 0
    bundle['container_ids']['backend']='backend-id'; bundle['acceptance_challenge']['container_ids']['backend']='backend-id'; bundle['acceptance_challenge']['image_identities']['backend']['image_revision']='c'*40
    mismatch=subprocess.run([sys.executable,str(validator)],input=json.dumps(bundle),env=env,text=True,capture_output=True)
    assert mismatch.returncode != 0 and 'challenge immutable field mismatch' in mismatch.stderr
    bundle['acceptance_challenge']['image_identities']['backend']['image_revision']=app
    mutated=json.loads(final_path.read_text()); mutated['compose_observation']['containers']['backend']['image_id']='raced-image'; final_path.write_text(json.dumps(mutated)); bundle['final_attestation']['sha256']=sha(final_path)
    race=subprocess.run([sys.executable,str(validator)],input=json.dumps(bundle),env=env,text=True,capture_output=True)
    assert race.returncode != 0 and ('immutable binding mismatch' in race.stderr or 'container binding mismatch' in race.stderr)


def test_two_phase_predeploy_and_mac_negative_validators(tmp_path):
    env = os.environ.copy()
    env.update(SERAPH_GPU_EXPECTED_HOSTNAME="jupyter", SERAPH_GPU_MACHINE_IDENTITY_SHA256="c" * 64)
    predeploy = {"local_hostname": "jupyter", "machine_identity_sha256": "c" * 64, "captured_at": datetime.now(timezone.utc).isoformat(), "ss_lntp": "LISTEN 0 4096 172.17.0.1:8000 0.0.0.0:*", "docker_bridge_addresses": ["172.17.0.1"], "docker_network_bindings": {"host-gateway": "172.17.0.1"}}
    validator = ROOT / "scripts" / "validate_gpu_predeploy.py"
    assert subprocess.run([sys.executable, str(validator)], input=json.dumps(predeploy), env=env, text=True).returncode == 0
    predeploy["ss_lntp"] += "\nLISTEN 0 4096 0.0.0.0:8001 0.0.0.0:*"
    assert subprocess.run([sys.executable, str(validator)], input=json.dumps(predeploy), env=env, text=True, capture_output=True).returncode != 0
    mac_validator = ROOT / "scripts" / "validate_mac_lan_negative.py"
    mac_path, mac_env = _signed_mac_receipt(tmp_path, datetime.now(timezone.utc).isoformat())
    assert subprocess.run([sys.executable, str(mac_validator)], input=mac_path.read_text(), env=mac_env, text=True).returncode == 0
    open_path, open_env = _signed_mac_receipt(tmp_path, datetime.now(timezone.utc).isoformat(), open_port="8001")
    assert subprocess.run([sys.executable, str(mac_validator)], input=open_path.read_text(), env=open_env, text=True, capture_output=True).returncode != 0
    assert "container/network binding changed before acceptance" in (ROOT / "manage.sh").read_text()


def test_mac_hmac_identity_tamper_and_nonce_replay(tmp_path):
    validator=ROOT/"scripts"/"validate_mac_lan_negative.py"; now=datetime.now(timezone.utc).isoformat()
    valid,env=_signed_mac_receipt(tmp_path,now,nonce="1"*48)
    tampered=json.loads(valid.read_text()); tampered["port_probes"]["8000"]["latency_ms"]=999
    assert "HMAC mismatch" in subprocess.run([sys.executable,str(validator)],input=json.dumps(tampered),env=env,text=True,capture_output=True).stderr
    for kwargs in ({"origin":"https://wrong.lan","nonce":"2"*48},{"host":"wrong.lan","nonce":"3"*48},{"client":"wrong-mac","nonce":"4"*48}):
        path,bad_env=_signed_mac_receipt(tmp_path,now,**kwargs)
        result=subprocess.run([sys.executable,str(validator)],input=path.read_text(),env=env,text=True,capture_output=True)
        assert result.returncode != 0
    script=f'''export SERAPH_MANAGE_SOURCE_ONLY=true SERAPH_EXPECTED_HTTPS_ORIGIN=https://seraph.lan SERAPH_LAN_HOST=seraph.lan SERAPH_MAC_PROBE_CLIENT_ID=operator-mac SERAPH_MAC_PROBE_KEY_FILE="{env['SERAPH_MAC_PROBE_KEY_FILE']}"; source "{ROOT/'manage.sh'}"; PID_DIR="{tmp_path/'nonce-pids'}"; mkdir -p "$PID_DIR"; production_validate_mac_receipt "{valid}"; production_record_mac_nonce; production_validate_mac_receipt "{valid}" && exit 9; exit 0'''
    assert subprocess.run(["bash","-c",script],text=True,capture_output=True).returncode == 0


def test_mac_challenge_mismatch_and_unclassified_network_error_fail_closed(tmp_path):
    validator = ROOT / "scripts" / "validate_mac_lan_negative.py"
    receipt, env = _signed_mac_receipt(tmp_path, datetime.now(timezone.utc).isoformat())
    other = json.loads(Path(env["SERAPH_ACCEPTANCE_CHALLENGE_FILE"]).read_text())
    other["server_nonce"] = "e" * 64
    other_path = tmp_path / "other-challenge.json"
    other_path.write_text(json.dumps(other))
    mismatch_env = env.copy()
    mismatch_env["SERAPH_ACCEPTANCE_CHALLENGE_FILE"] = str(other_path)
    mismatch = subprocess.run([sys.executable, str(validator)], input=receipt.read_text(), env=mismatch_env, text=True, capture_output=True)
    assert mismatch.returncode != 0
    assert "acceptance challenge mismatch" in mismatch.stderr
    mismatch_env["SERAPH_MAC_RECEIPT_HISTORICAL"] = "true"
    historical_mismatch = subprocess.run([sys.executable, str(validator)], input=receipt.read_text(), env=mismatch_env, text=True, capture_output=True)
    assert historical_mismatch.returncode != 0
    assert "acceptance challenge mismatch" in historical_mismatch.stderr

    no_route, no_route_env = _signed_mac_receipt(
        tmp_path,
        datetime.now(timezone.utc).isoformat(),
        denied_error="invalid_network_error",
        nonce="d" * 48,
    )
    rejected = subprocess.run([sys.executable, str(validator)], input=no_route.read_text(), env=no_route_env, text=True, capture_output=True)
    assert rejected.returncode != 0
    assert "measured LAN denial missing" in rejected.stderr
    generator = (ROOT / "scripts" / "generate_mac_lan_acceptance.py").read_text()
    assert "socket.getaddrinfo" in generator and "resolved!={a.lan_ip}" in generator
    assert "socket.create_connection((a.lan_ip,port)" in generator
    assert "server_hostname=expected_host" in generator
    assert "self.sock.getpeername()[0]!=a.lan_ip" in generator


def test_lifecycle_lock_and_server_challenge_ledger_reject_reuse(tmp_path):
    lock_file = tmp_path / "pids" / "seraph-prod-lifecycle.lock"
    lock_file.parent.mkdir()
    holder = subprocess.Popen(["flock", str(lock_file), "sleep", "2"])
    try:
        time.sleep(0.1)
        probe = f'''export SERAPH_MANAGE_SOURCE_ONLY=true; source "{ROOT/'manage.sh'}"; PID_DIR="{lock_file.parent}"; production_lock_run true'''
        result = subprocess.run(["bash", "-c", probe], text=True, capture_output=True)
        assert result.returncode != 0
        assert "another production lifecycle mutation is running" in result.stderr
    finally:
        holder.terminate()
        holder.wait(timeout=3)

    receipt, env = _signed_mac_receipt(tmp_path, datetime.now(timezone.utc).isoformat(), nonce="9" * 48)
    consumed = tmp_path / "ledger-pids"
    consumed.mkdir()
    (consumed / "seraph-prod-consumed-acceptance").write_text("challenge:" + "f" * 64 + "\n")
    script = f'''export SERAPH_MANAGE_SOURCE_ONLY=true SERAPH_EXPECTED_HTTPS_ORIGIN=https://seraph.lan SERAPH_LAN_HOST=seraph.lan SERAPH_LAN_IP=192.168.1.26 SERAPH_MAC_PROBE_CLIENT_ID=operator-mac SERAPH_MAC_PROBE_KEY_FILE="{env['SERAPH_MAC_PROBE_KEY_FILE']}"; source "{ROOT/'manage.sh'}"; PID_DIR="{consumed}"; production_validate_mac_receipt "{receipt}" "{env['SERAPH_ACCEPTANCE_CHALLENGE_FILE']}"'''
    replay = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert replay.returncode != 0
    assert "server acceptance challenge was already used" in replay.stderr


def test_sourced_lifecycle_behavior_with_fake_docker(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "echo \"$*\" >>\"$DOCKER_CALLS\"\n"
        "[ \"$DOCKER_MODE\" = ps_fail ] && [ \"$1\" = ps ] && exit 9\n"
        "case \" $* \" in *\" exec -T backend \"*) exit 7;; esac\n"
        "case \" $* \" in *\"--format\"*) printf '%s\\n' \"$TEST_SHA\";; esac\n"
        "exit 0\n"
    )
    docker.chmod(0o755)
    calls = tmp_path / "calls"
    state = tmp_path / "state"
    state.write_text("truncated\nstate\n")
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}")
    script = f'''
set -u
export SERAPH_MANAGE_SOURCE_ONLY=true PATH="{fake_bin}:$PATH" DOCKER_CALLS="{calls}" TEST_SHA="{'a' * 40}"
source "{ROOT / 'manage.sh'}"
PID_DIR="{tmp_path / 'pids'}"; LOG_DIR="{tmp_path / 'logs'}"; mkdir -p "$PID_DIR" "$LOG_DIR"
ENV_FILE=/dev/null; COMPOSE_FILES=(-f /dev/null)
production_host_inventory_validate() {{ return 0; }}
production_read_validate_accepted_state "{state}" && exit 20
export DOCKER_MODE=ps_fail
production_docker_inventory_validate false && exit 21
export DOCKER_MODE=restore_fail
production_restore_previous "{'a' * 40}" "repo/wrapper@sha256:{'b' * 64}" "{receipt}"
[ "$?" -eq 2 ] || exit 22
export SERAPH_IMAGE_TAG="{'a' * 40}" SERAPH_VLM_IMAGE="repo/wrapper@sha256:{'b' * 64}"
production_write_accepted_state "{tmp_path / 'accepted'}" "{receipt}" || exit 23
[ "$(wc -l <"{tmp_path / 'accepted'}")" -eq 3 ] || exit 24
'''
    result = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    call_text = calls.read_text()
    assert "compose" in call_text and "exec -T backend" in call_text


def test_sourced_two_phase_accept_rejects_container_swap_and_stale_mac(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "docker").write_text('#!/bin/sh\n[ "$CONTAINER_MODE" = swapped ] && echo candidate-2 || echo candidate-1\n')
    (fake_bin / "docker").chmod(0o755)
    (fake_bin / "python3").write_text('#!/bin/sh\ncase "$*" in *validate_mac_lan_negative.py*) exec /usr/bin/python3 "$@";; *"-c"*) echo candidate-1;; *generate_local_gpu_inventory.py*) echo "{}";; esac\nexec /usr/bin/python3 "$@"\n')
    (fake_bin / "python3").chmod(0o755)
    now = datetime.now(timezone.utc).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    valid_mac, mac_env = _signed_mac_receipt(tmp_path, now)
    stale_mac, _ = _signed_mac_receipt(tmp_path, stale)
    rollback_mac, _ = _signed_mac_receipt(tmp_path, now, nonce="b"*48)
    restore_mac, _ = _signed_mac_receipt(tmp_path, now, nonce="c"*48)
    script = f'''
export SERAPH_MANAGE_SOURCE_ONLY=true PATH="{fake_bin}:$PATH" CONTAINER_MODE=stable SERAPH_EXPECTED_HTTPS_ORIGIN=https://seraph.lan SERAPH_LAN_HOST=seraph.lan SERAPH_LAN_IP=192.168.1.26 SERAPH_MAC_PROBE_CLIENT_ID=operator-mac SERAPH_MAC_PROBE_KEY_FILE="{mac_env['SERAPH_MAC_PROBE_KEY_FILE']}" SERAPH_ACCEPTANCE_CHALLENGE_FILE="{mac_env['SERAPH_ACCEPTANCE_CHALLENGE_FILE']}"
source "{ROOT / 'manage.sh'}"
python3() {{ if [ "$1" = "-c" ]; then echo candidate-1; elif echo "$1" | grep -q generate_local_gpu_inventory; then echo '{{}}'; else /usr/bin/python3 "$@"; fi; }}
production_validate_mac_receipt() {{ /usr/bin/python3 "{ROOT/'scripts/validate_mac_lan_negative.py'}" <"$1" || return 1; MAC_RECEIPT_NONCE=$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["nonce"])' "$1"); local nf="$PID_DIR/seraph-prod-mac-nonces"; [ -r "$nf" ] && grep -Fx "$MAC_RECEIPT_NONCE" "$nf" >/dev/null && return 1; return 0; }}
PID_DIR="{tmp_path / 'pids'}"; LOG_DIR="{tmp_path / 'logs'}"; mkdir -p "$PID_DIR" "$LOG_DIR"
ENV_FILE=/dev/null; COMPOSE_FILES=(-f /dev/null); SERAPH_VLM_API_KEY_FILE=/dev/null
touch "$PID_DIR/seraph-prod-candidate-release"
echo '{{"vlm_observation":{{"container_id":"candidate-1"}}}}' >"{tmp_path / 'candidate.json'}"
production_read_validate_accepted_state() {{ ACCEPTED_APP_TAG={'a' * 40}; ACCEPTED_VLM_IMAGE='repo/vlm@sha256:{'b' * 64}'; ACCEPTED_INVENTORY_RECEIPT="{tmp_path / 'candidate.json'}"; return 0; }}
production_host_inventory_validate() {{ return 0; }}
production_compose_state_validate() {{ return 0; }}
production_generate_local_attestation() {{ [ "$CONTAINER_MODE" != swapped ] || return 1; echo '{{}}' >"$1"; }}
production_write_accepted_state() {{ touch "$1"; return 0; }}
production_write_acceptance_bundle() {{ touch "$1"; return 0; }}
production_accept "{valid_mac}" || exit 31
[ -f "$PID_DIR/seraph-prod-active-release" ] || exit 32
touch "$PID_DIR/seraph-prod-candidate-release"; export CONTAINER_MODE=swapped
production_accept "{valid_mac}" && exit 33
export CONTAINER_MODE=stable
production_accept "{stale_mac}" && exit 34
touch "$PID_DIR/seraph-prod-rollback-release"; export CONTAINER_MODE=stable
production_accept_staged rollback "{rollback_mac}" || exit 35
touch "$PID_DIR/seraph-prod-restore-release"
production_accept_staged restore "{restore_mac}" || exit 36
exit 0
'''
    result = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def _entrypoint(tmp_path: Path, raw: str, hashed: str) -> subprocess.CompletedProcess[str]:
    raw_file = tmp_path / "raw"
    hash_file = tmp_path / "hash"
    raw_file.write_text(raw)
    hash_file.write_text(hashed)
    env = os.environ.copy()
    env.update(
        OPERATOR_AUTH_SECRET_FILE=str(raw_file),
        OPERATOR_AUTH_SECRET_HASH_FILE=str(hash_file),
        LOCAL_LLM_API_KEY_FILE="/dev/null",
        SERAPH_VLM_API_KEY_FILE="/dev/null",
    )
    return subprocess.run(
        ["sh", str(ROOT / "backend" / "docker-entrypoint.sh"), "/bin/true"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_entrypoint_accepts_exactly_one_raw_or_hash_credential(tmp_path):
    assert _entrypoint(tmp_path, "raw-secret\n", "").returncode == 0
    assert _entrypoint(tmp_path, "", "hash-value\n").returncode == 0
    assert _entrypoint(tmp_path, "raw-secret\n", "hash-value\n").returncode == 78
    assert _entrypoint(tmp_path, "", "").returncode == 78


def _inventory(receipt: dict[str, object], vlm_image: str = "") -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        SERAPH_GPU_EXPECTED_HOSTNAME="jupyter",
        SERAPH_GPU_MACHINE_IDENTITY_SHA256="c" * 64,
        SERAPH_VLM_IMAGE=vlm_image or "ghcr.io/seraph-quest/vlm-screenshot-server@sha256:" + "a" * 64,
        SERAPH_VLM_INTERFACE_CONTRACT="vlm-health-backend-queue-chat-auth-v1",
        SERAPH_HOST_INVENTORY_MAX_AGE_SECONDS="900",
    )
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate_gpu_host_inventory.py")],
        input=json.dumps(receipt),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_host_inventory_gate_accepts_private_listener_and_rejects_lan_listener():
    receipt = {
        "local_hostname": "jupyter",
        "machine_identity_sha256": "c" * 64,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "ss_lntp": 'LISTEN 0 4096 172.17.0.1:8000 0.0.0.0:* users:(("model",pid=1,fd=1))',
        "docker_bridge_addresses": ["172.17.0.1"],
        "docker_network_bindings": {"host-gateway": "172.17.0.1"},
        "firewall": {f"lan_ingress_{port}": "blocked" for port in (8000, 8001, 8004)},
        "firewall_provenance": {"source_command": "nft list ruleset", "captured_at": datetime.now(timezone.utc).isoformat(), "raw_output": "table inet filter { chain input { drop } }", "output_sha256": __import__("hashlib").sha256(b"table inet filter { chain input { drop } }").hexdigest()},
        "vlm_image": "ghcr.io/seraph-quest/vlm-screenshot-server@sha256:" + "a" * 64,
        "vlm_interface_contract": "vlm-health-backend-queue-chat-auth-v1",
        "vlm_wrapper_contract_verified": True,
        "vlm_observation": {"container_id": "candidate-1", "image_id": "vlm-wrapper-image", "repo_digests": ["ghcr.io/seraph-quest/vlm-screenshot-server@sha256:" + "a" * 64], "running": True, "published_ports": {}, "checks": {"health": True, "backend": True, "queue": True, "auth_closed": True, "auth_interface": True}},
        "compose_observation": {"project": "seraph-prod", "network_name": "seraph-core-prod", "containers": {service: {"container_id": service+"-1", "image_id": service+"-image", "image_revision": "a"*40 if service in {"ingress","backend"} else "", "project": "seraph-prod", "service": service, "network_name": "seraph-core-prod", "network_id": "net-1", "ip_address": ip} for service,ip in {"ingress":"172.30.0.10","backend":"172.30.0.20","vlm-wrapper":"172.30.0.30"}.items()}},
    }
    assert _inventory(receipt).returncode == 0
    local_id = "sha256:" + "d" * 64
    local_receipt = json.loads(json.dumps(receipt)); local_receipt["vlm_image"] = local_id; local_receipt["vlm_observation"]["image_id"] = local_id; local_receipt["vlm_observation"]["repo_digests"] = []
    assert _inventory(local_receipt, local_id).returncode == 0
    local_receipt["vlm_observation"]["image_id"] = "sha256:" + "e" * 64
    assert _inventory(local_receipt, local_id).returncode != 0
    receipt["vlm_observation"]["checks"]["auth_interface"] = False
    forged_contract = _inventory(receipt)
    assert forged_contract.returncode != 0
    assert "active interface checks" in forged_contract.stderr
    receipt["vlm_observation"]["checks"]["auth_interface"] = True
    receipt["vlm_observation"]["checks"]["auth_closed"] = False
    assert "active interface checks" in _inventory(receipt).stderr
    receipt["vlm_observation"]["checks"]["auth_closed"] = True
    receipt["ss_lntp"] = "LISTEN 0 4096 0.0.0.0:8000 0.0.0.0:*"
    result = _inventory(receipt)
    assert result.returncode != 0
    assert "wildcard/LAN" in result.stderr


def test_host_inventory_gate_rejects_stale_and_identity_or_contract_mismatch():
    receipt = {
        "local_hostname": "jupyter",
        "machine_identity_sha256": "c" * 64,
        "captured_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "ss_lntp": "LISTEN 0 4096 172.17.0.1:8000 0.0.0.0:*",
        "docker_bridge_addresses": ["172.17.0.1"],
        "docker_network_bindings": {"host-gateway": "172.17.0.1"},
        "firewall": {f"lan_ingress_{port}": "blocked" for port in (8000, 8001, 8004)},
        "firewall_provenance": {"source_command": "nft list ruleset", "captured_at": datetime.now(timezone.utc).isoformat(), "raw_output": "table inet filter { chain input { drop } }", "output_sha256": __import__("hashlib").sha256(b"table inet filter { chain input { drop } }").hexdigest()},
        "vlm_image": "ghcr.io/seraph-quest/vlm-screenshot-server@sha256:" + "a" * 64,
        "vlm_interface_contract": "vlm-health-backend-queue-chat-auth-v1",
        "vlm_wrapper_contract_verified": True,
        "vlm_observation": {"container_id": "candidate-1", "image_id": "vlm-wrapper-image", "repo_digests": ["ghcr.io/seraph-quest/vlm-screenshot-server@sha256:" + "a" * 64], "running": True, "published_ports": {}, "checks": {"health": True, "backend": True, "queue": True, "auth_closed": True, "auth_interface": True}},
        "compose_observation": {"project": "seraph-prod", "network_name": "seraph-core-prod", "containers": {service: {"container_id": service+"-1", "image_id": service+"-image", "image_revision": "a"*40 if service in {"ingress","backend"} else "", "project": "seraph-prod", "service": service, "network_name": "seraph-core-prod", "network_id": "net-1", "ip_address": ip} for service,ip in {"ingress":"172.30.0.10","backend":"172.30.0.20","vlm-wrapper":"172.30.0.30"}.items()}},
    }
    assert "stale" in _inventory(receipt).stderr
    receipt["captured_at"] = datetime.now(timezone.utc).isoformat()
    receipt["local_hostname"] = "unexpected"
    assert "hostname" in _inventory(receipt).stderr
    receipt["local_hostname"] = "jupyter"
    receipt["machine_identity_sha256"] = "d" * 64
    assert "machine identity" in _inventory(receipt).stderr
    receipt["machine_identity_sha256"] = "c" * 64
    receipt["vlm_interface_contract"] = "wrong"
    assert "interface contract" in _inventory(receipt).stderr
    receipt["vlm_interface_contract"] = "vlm-health-backend-queue-chat-auth-v1"
    receipt["raw_machine_id"] = "forged-secret-machine-id"
    assert "raw machine identity" in _inventory(receipt).stderr
