"""Offline CPU-host launch and private-listener contract tests."""

from __future__ import annotations

from pathlib import Path
import os
import shlex
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from production_preflight import build_preflight_report, main  # noqa: E402
from src.workspace import ProductionWorkspace  # noqa: E402


def _production_env(workspace: Path) -> dict[str, str]:
    return {
        "DEPLOYMENT_ENVIRONMENT": "production",
        "OPERATOR_AUTH_SECRET": "operator-secret-for-test",
        "OPERATOR_AUTH_SECRET_HASH": "",
        "WORKSPACE_DIR": str(workspace),
        "LLM_API_BASE": "https://openrouter.ai/api/v1",
        "DEFAULT_MODEL": "openrouter/anthropic/claude-sonnet-4",
        "OPENROUTER_API_KEY": "",
        "OPENROUTER_ALLOWED_UPSTREAMS": "",
        "OPENROUTER_PROVIDER_ONLY": "true",
        "OPENROUTER_ALLOW_FALLBACKS": "false",
        "OPENROUTER_REQUIRE_PARAMETERS": "true",
        "OPENROUTER_DATA_COLLECTION": "deny",
        "FALLBACK_MODEL": "",
        "FALLBACK_MODELS": "",
        "FALLBACK_LLM_API_BASE": "",
    }


def test_missing_gpu_and_vlm_do_not_block_cpu_core(tmp_path):
    report = build_preflight_report(_production_env(tmp_path))

    assert report["host_profile"] == "cpu"
    assert report["core"]["status"] == "ready"
    assert report["inference"]["provider"] == "openrouter"
    assert report["inference"]["status"] == "configuration_required"
    assert "openrouter_api_key_missing" in report["inference"]["reasons"]
    assert "openrouter_upstream_allowlist_missing" in report["inference"]["reasons"]
    assert report["inference"]["live_proof"] == "unknown"
    assert report["inference"]["probe_performed"] is False
    assert all(value == "not_required" for value in report["dependencies"].values())
    mount_check = next(
        check for check in report["core"]["checks"] if check["name"] == "canonical_workspace_mount"
    )
    assert mount_check["status"] == "deferred"


def test_production_core_requires_exactly_one_server_side_auth_credential(tmp_path):
    missing = _production_env(tmp_path)
    missing["OPERATOR_AUTH_SECRET"] = ""
    report = build_preflight_report(missing)
    assert report["core"]["status"] == "configuration_required"
    assert any(check["name"] == "operator_auth" for check in report["core"]["checks"])

    both = _production_env(tmp_path)
    both["OPERATOR_AUTH_SECRET_HASH"] = "pbkdf2_sha256$600000$c2FsdA==$ZGlnZXN0"
    report = build_preflight_report(both)
    assert report["core"]["status"] == "invalid"

    malformed = _production_env(tmp_path)
    malformed["OPERATOR_AUTH_SECRET"] = ""
    malformed["OPERATOR_AUTH_SECRET_HASH"] = "x"
    report = build_preflight_report(malformed)
    assert report["core"]["status"] == "invalid"
    auth_check = next(check for check in report["core"]["checks"] if check["name"] == "operator_auth")
    assert auth_check["detail"] == "operator PBKDF2 hash has invalid shape"


def test_complete_openrouter_config_remains_unverified_without_live_probe(tmp_path):
    env = _production_env(tmp_path)
    env.update(
        OPENROUTER_API_KEY="configured-but-not-probed",
        OPENROUTER_ALLOWED_UPSTREAMS="anthropic",
    )
    report = build_preflight_report(env)

    assert report["core"]["status"] == "ready"
    assert report["inference"]["status"] == "configuration_required"
    assert report["inference"]["configuration_status"] == "configured"
    assert report["inference"]["live_proof"] == "unknown"
    assert report["inference"]["probe_performed"] is False
    assert report["inference"]["local_fallback"] == "disabled"


def test_missing_model_is_reported_without_inventing_a_paid_default(tmp_path):
    env = _production_env(tmp_path)
    env.pop("DEFAULT_MODEL")
    report = build_preflight_report(env)

    assert report["inference"]["model"] == "unknown"
    assert "openrouter_model_missing" in report["inference"]["reasons"]
    assert report["inference"]["status"] == "configuration_required"


def test_preflight_cli_only_blocks_unstartable_core(tmp_path, monkeypatch, capsys):
    env = _production_env(tmp_path)
    monkeypatch.setattr("production_preflight.os.environ", env)

    assert main(["--format", "json"]) == 0
    output = capsys.readouterr().out
    assert '"status": "configuration_required"' in output
    assert '"host_profile": "cpu"' in output

    env["OPERATOR_AUTH_SECRET"] = ""
    assert main(["--format", "json"]) == 78

    env_file = tmp_path / ".env.prod"
    env_file.write_text(
        "\n".join(
            (
                "DEPLOYMENT_ENVIRONMENT=production",
                "OPERATOR_AUTH_SECRET=from-file",
                f"WORKSPACE_DIR={tmp_path}",
                "OPENROUTER_API_KEY=",
                "OPENROUTER_ALLOWED_UPSTREAMS=",
            )
        )
        + "\n"
    )
    monkeypatch.setattr("production_preflight.os.environ", {})
    assert main(["--env-file", str(env_file), "--format", "json"]) == 0
    assert '"host_profile": "cpu"' in capsys.readouterr().out


def test_production_compose_keeps_backend_private_and_has_no_gpu_or_vlm_gate():
    compose = (ROOT / "docker-compose.prod.yaml").read_text()
    backend = compose.split("\n  backend-prod:\n", 1)[1].split("\nnetworks:\n", 1)[0]

    assert "ports:" not in backend
    assert '      - "8003"' in backend
    assert "production_preflight.py --format json" in backend
    assert "http://127.0.0.1:8003/health" in backend
    assert "nvidia" not in compose.lower()
    assert "cuda" not in compose.lower()
    assert "gpu-model" not in compose
    assert "vlm-wrapper" not in compose
    assert "8000" not in compose
    assert "8001" not in compose
    assert "LOCAL_LLM_API_BASE: \"\"" in compose
    assert "SERAPH_VLM_BASE_URL: \"\"" in compose
    assert 'SERAPH_PRODUCTION_MOUNT_CHECK: "true"' in compose
    assert "SERAPH_PRODUCTION_MOUNT_SOURCE" in compose


def test_production_mount_preflight_fails_closed_without_mountinfo(tmp_path):
    env = _production_env(tmp_path)
    env["WORKSPACE_DIR"] = "/app/data"
    env["SERAPH_PRODUCTION_MOUNT_CHECK"] = "true"
    env["BACKEND_DATA_PATH_PROD"] = str(tmp_path)
    env["SERAPH_PRODUCTION_MOUNT_SOURCE"] = "/dev/test"
    env["SERAPH_PRODUCTION_BIND_IDENTITY"] = ProductionWorkspace(host_root=tmp_path).bind_identity_digest
    report = build_preflight_report(env)
    mount_check = next(
        check for check in report["core"]["checks"] if check["name"] == "canonical_workspace_mount"
    )
    assert mount_check["status"] == "invalid"


def test_managed_local_ports_bind_loopback_and_production_env_uses_prod_paths():
    manage = (ROOT / "manage.sh").read_text()
    env = (ROOT / "env.prod.example").read_text()

    assert '--host 127.0.0.1 --port "$6"' in manage
    assert '--host 127.0.0.1 --port "$4"' in manage
    assert '--host 0.0.0.0' not in manage
    assert "reject_prod_local_stack" in manage
    assert "production authentication requires HTTPS" in manage
    assert 'exit "$LOCAL_EXIT_STATUS"' in manage
    assert "HOST_DATA_ROOT_PROD=/srv/seraph/docker-data/prod" in env
    assert "BACKEND_DATA_PATH_PROD=/srv/seraph/docker-data/prod/backend/data" in env
    assert "BACKEND_LOGS_PATH_PROD=/srv/seraph/docker-data/prod/backend/logs" in env
    assert "SERAPH_PRODUCTION_MOUNT_CHECK=false" in env
    assert "SERAPH_PRODUCTION_BIND_IDENTITY=" in env
    assert "LOCAL_LLM_API_BASE=" in env
    assert "SERAPH_VLM_BASE_URL=" in env
    assert "SERAPH_VLM_BACKEND_URL=" in env
    assert "OPERATOR_AUTH_COOKIE_SECURE=true" in env
    assert "OPERATOR_AUTH_ALLOWED_ORIGINS=https://localhost" in env
    assert "refresh_production_bind_identity" in manage
    assert 'COMMAND" = "up"' in manage
    assert 'export SERAPH_PRODUCTION_BIND_IDENTITY="$identity"' in manage


def test_prod_local_up_rejects_plain_http_before_start(tmp_path):
    started = tmp_path / "started"
    script = "\n".join(
        (
            "set -u",
            "export SERAPH_MANAGE_SOURCE_ONLY=true",
            f"source {shlex.quote(str(ROOT / 'manage.sh'))}",
            "PROG_NAME=./manage.sh",
            "ENV=prod",
            f"LOCAL_WORKSPACE_DIR={shlex.quote(str(tmp_path / 'resolved-workspace'))}",
            f"PID_DIR={shlex.quote(str(tmp_path / 'pids'))}",
            f"LOG_DIR={shlex.quote(str(tmp_path / 'logs'))}",
            f"start_local_backend() {{ printf started > {shlex.quote(str(started))}; return 0; }}",
            "local_up",
        )
    )
    result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, check=False)

    assert result.returncode == 1
    assert "production authentication requires HTTPS" in result.stderr
    assert "Use './manage.sh -e prod up -d'" in result.stderr
    assert not started.exists()


def test_preflight_script_is_dependency_free_and_does_not_open_provider_sockets():
    source = (BACKEND / "production_preflight.py").read_text()
    assert "urllib.request" not in source
    assert "urlopen" not in source
    assert "import socket" not in source
    compile(source, str(BACKEND / "production_preflight.py"), "exec")

    env = os.environ.copy()
    env.update(_production_env(BACKEND / "test-workspace"))
    result = subprocess.run(
        [sys.executable, str(BACKEND / "production_preflight.py"), "--format", "json"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert '"probe_performed": false' in result.stdout
