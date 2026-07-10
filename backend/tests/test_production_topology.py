import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timedelta, timezone


ROOT = Path(__file__).resolve().parents[2]


def test_production_compose_has_one_tls_ingress_and_no_backend_publication():
    compose = (ROOT / "docker-compose.prod.yaml").read_text()
    assert '"${SERAPH_HTTPS_BIND:-0.0.0.0}:${SERAPH_HTTPS_PORT:-443}:443"' in compose
    backend = compose.split("\n  backend:\n", 1)[1].split("\nnetworks:\n", 1)[0]
    assert "ports:" not in backend
    assert "OPERATOR_AUTH_COOKIE_SECURE: \"true\"" in backend
    assert "OPERATOR_AUTH_BACKEND_WORKERS: \"1\"" in backend
    assert 'OPERATOR_AUTH_TRUSTED_PROXY_IPS: "172.30.0.10"' in backend
    assert "--reload" not in (ROOT / "backend" / "Dockerfile").read_text()
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
    assert "production_start" in restart_case
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
    assert 'production_host_inventory_validate "$target_receipt" "$vlm_image"' in rollback


def test_release_identity_requires_clean_exact_head_and_hex_vlm_digest():
    manage = (ROOT / "manage.sh").read_text()
    assert 'git -C "$SCRIPT_DIR" rev-parse HEAD' in manage
    assert 'git -C "$SCRIPT_DIR" status --porcelain' in manage
    assert 'SERAPH_IMAGE_TAG:-}" =~ ^[0-9a-f]{40}$' in manage
    assert '@sha256:[0-9a-fA-F]{64}$' in manage
    assert "mismatched build identity; refusing overwrite" in manage
    non_hex_digest = "ghcr.io/example/wrapper@sha256:" + "z" * 64
    assert not __import__("re").match(r"^[^\s@]+@sha256:[0-9a-fA-F]{64}$", non_hex_digest)


def test_restore_adoption_inventory_and_atomic_state_failure_contracts():
    manage = (ROOT / "manage.sh").read_text()
    restore = manage.split("function production_restore_previous()", 1)[1].split("function production_restore_after_failure()", 1)[0]
    assert "production_preflight.py" in restore
    assert "seraph-prod-failed-restore.log" in restore
    assert "failed inference preflight" in restore
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


def _inventory(receipt: dict[str, object]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        SERAPH_GPU_SSH_HOST="jupyter",
        SERAPH_GPU_SSH_HOST_FINGERPRINT="SHA256:test-receipt",
        SERAPH_VLM_IMAGE="ghcr.io/seraph-quest/vlm-screenshot-server@sha256:" + "a" * 64,
        SERAPH_VLM_INTERFACE_CONTRACT="vlm-screenshot-server/v0.2-file-secrets",
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
        "ssh_host": "jupyter",
        "host_key_verified": True,
        "host_key_fingerprint": "SHA256:test-receipt",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "ss_lntp": 'LISTEN 0 4096 172.17.0.1:8000 0.0.0.0:* users:(("model",pid=1,fd=1))',
        "docker_bridge_addresses": ["172.17.0.1"],
        "docker_network_bindings": {"host-gateway": "172.17.0.1"},
        "firewall": {f"lan_ingress_{port}": "blocked" for port in (8000, 8001, 8004)},
        "vlm_image": "ghcr.io/seraph-quest/vlm-screenshot-server@sha256:" + "a" * 64,
        "vlm_interface_contract": "vlm-screenshot-server/v0.2-file-secrets",
        "vlm_wrapper_contract_verified": True,
    }
    assert _inventory(receipt).returncode == 0
    receipt["ss_lntp"] = "LISTEN 0 4096 0.0.0.0:8000 0.0.0.0:*"
    result = _inventory(receipt)
    assert result.returncode != 0
    assert "wildcard/LAN" in result.stderr


def test_host_inventory_gate_rejects_stale_and_identity_or_contract_mismatch():
    receipt = {
        "ssh_host": "jupyter",
        "host_key_verified": True,
        "host_key_fingerprint": "SHA256:test-receipt",
        "captured_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "ss_lntp": "LISTEN 0 4096 172.17.0.1:8000 0.0.0.0:*",
        "docker_bridge_addresses": ["172.17.0.1"],
        "docker_network_bindings": {"host-gateway": "172.17.0.1"},
        "firewall": {f"lan_ingress_{port}": "blocked" for port in (8000, 8001, 8004)},
        "vlm_image": "ghcr.io/seraph-quest/vlm-screenshot-server@sha256:" + "a" * 64,
        "vlm_interface_contract": "vlm-screenshot-server/v0.2-file-secrets",
        "vlm_wrapper_contract_verified": True,
    }
    assert "stale" in _inventory(receipt).stderr
    receipt["captured_at"] = datetime.now(timezone.utc).isoformat()
    receipt["ssh_host"] = "unexpected"
    assert "identity" in _inventory(receipt).stderr
    receipt["ssh_host"] = "jupyter"
    receipt["vlm_interface_contract"] = "wrong"
    assert "interface contract" in _inventory(receipt).stderr
