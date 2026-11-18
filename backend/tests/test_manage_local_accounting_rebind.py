"""Managed local-mode accounting rebind, identity and status (#1277).

Every case drives ``manage.sh`` against temporary roots: a fake repository
whose ``manage.sh`` and ``backend`` are symlinks to this checkout, its own
``.env.dev`` and ``docker-data/dev/workspace-lifecycle``. No operator workspace,
descriptor, running stack or provider is touched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess

import pytest

from tests.test_inference_accounting import accounting_db  # noqa: F401 - fixture
from tests.test_inference_accounting_rebind import prepare_source
from src.workspace.production import ProductionWorkspace, prepare_lifecycle_directory, read_lifecycle_receipt

# ADR-035: the ledger fixture pins historical OpenRouter mechanics that still
# settle retained liabilities; transport stays intercepted.
pytestmark = pytest.mark.openrouter_legacy

ROOT = Path(__file__).resolve().parents[2]
REBIND_HINT = "local accounting-rebind --from-root /absolute/prior/root --confirm"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _host_env() -> dict[str, str]:
    # Only what the shell and a version-manager shim need; never the caller's
    # workspace, descriptor or credential variables.
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "TZ": "UTC"}
    if os.environ.get("HOME"):
        env["HOME"] = os.environ["HOME"]
    return env


def _fake_repo(tmp_path: Path, workspace: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "manage.sh").symlink_to(ROOT / "manage.sh")
    (repo / "backend").symlink_to(ROOT / "backend", target_is_directory=True)
    (repo / ".env.dev").write_text(
        f"LOCAL_WORKSPACE_DIR={workspace}\nLOCAL_BACKEND_PORT={_free_port()}\n", encoding="utf-8")
    return repo


def _manage(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(repo / "manage.sh"), "-e", "dev", "local", *arguments],
        env=_host_env(), cwd=repo, text=True, capture_output=True, timeout=60, check=False)


def _sourced(script: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    prologue = "\n".join((
        "set -u",
        "export SERAPH_MANAGE_SOURCE_ONLY=true",
        f"source {shlex.quote(str(ROOT / 'manage.sh'))}",
        "PROG_NAME=./manage.sh",
        "ENV=dev",
    ))
    return subprocess.run(["bash", "-c", prologue + "\n" + script], env=env or _host_env(),
        text=True, capture_output=True, timeout=60, check=False)


def _moved_root(tmp_path: Path) -> tuple[Path, Path, Path]:
    """An old root bound in the descriptor, and the new root the env now names."""
    repo_lifecycle = tmp_path / "repo-lifecycle-staging"
    old_root, new_root = tmp_path / "old-root", tmp_path / "new-root"
    old_root.mkdir()
    new_root.mkdir()
    prepare_lifecycle_directory(ProductionWorkspace(host_root=old_root, lifecycle_path=repo_lifecycle))
    return old_root, new_root, repo_lifecycle


def test_local_up_and_lifecycle_commands_share_one_path_resolution(tmp_path):
    fake = tmp_path / "checkout"
    result = _sourced("\n".join((
        f"SCRIPT_DIR={shlex.quote(str(fake))}",
        "COMMAND=local",
        "unset LOCAL_WORKSPACE_DIR",
        "BACKEND_DATA_PATH_DEV=/ignored/for/local",
        "resolve_local_workspace_paths",
        'printf "%s\\n" "$LOCAL_WORKSPACE_DIR" "$SERAPH_WORKSPACE_LIFECYCLE_PATH"',
        "LOCAL_WORKSPACE_DIR=relative/dev-data",
        "resolve_local_workspace_paths",
        'printf "%s\\n" "$LOCAL_WORKSPACE_DIR"',
    )))
    assert result.returncode == 0, result.stderr
    default_root, lifecycle, relative = result.stdout.splitlines()
    assert default_root == os.path.realpath("/tmp") + "/seraph-dev-data"
    assert lifecycle == f"{fake}/docker-data/dev/workspace-lifecycle"
    assert relative == f"{fake}/relative/dev-data"

    manage = (ROOT / "manage.sh").read_text()
    # One resolution, called once; the lifecycle CLI reads only its outputs.
    assert manage.count("function resolve_local_workspace_paths()") == 1
    assert manage.count("\nresolve_local_workspace_paths\n") == 1
    assert manage.count('export SERAPH_WORKSPACE_LIFECYCLE_PATH="$SCRIPT_DIR/docker-data/$ENV/workspace-lifecycle"') == 1
    assert ('BACKEND_DATA_PATH_PROD="$LOCAL_WORKSPACE_DIR" WORKSPACE_DIR="$LOCAL_WORKSPACE_DIR"' in manage)


@pytest.mark.parametrize("command", ["identity", "accounting-rebind"])
def test_local_lifecycle_refuses_while_the_local_backend_runs(tmp_path, command):
    invoked = tmp_path / "cli-invoked"
    result = _sourced("\n".join((
        f"LOCAL_BACKEND_PID_FILE={shlex.quote(str(tmp_path / 'backend.pid'))}",
        "LOCAL_BACKEND_PORT=18004",
        'echo "$$" > "$LOCAL_BACKEND_PID_FILE"',
        "pids_listening_on_port() { :; }",
        f"local_workspace_cli() {{ printf invoked > {shlex.quote(str(invoked))}; }}",
        f"local_workspace_lifecycle {command} --from-root /prior --confirm",
    )))
    assert result.returncode == 1
    assert "the local backend is running (pid_only)" in result.stderr
    assert "./manage.sh -e dev local down" in result.stderr
    assert not invoked.exists()


def test_local_workspace_status_is_read_only_and_runs_with_the_stack_up(tmp_path):
    result = _sourced("\n".join((
        f"LOCAL_BACKEND_PID_FILE={shlex.quote(str(tmp_path / 'backend.pid'))}",
        "LOCAL_BACKEND_PORT=18004",
        'echo "$$" > "$LOCAL_BACKEND_PID_FILE"',
        "pids_listening_on_port() { :; }",
        'local_workspace_cli() { printf \'{"command": "%s"}\' "$1"; }',
        "local_workspace_lifecycle status",
    )))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"command": "status"}


def test_prod_lifecycle_gate_is_unchanged_and_points_dev_at_the_local_form(tmp_path):
    invoked = tmp_path / "cli-invoked"
    result = _sourced("\n".join((
        f"run_workspace_cli() {{ printf invoked > {shlex.quote(str(invoked))}; }}",
        "production_workspace_lifecycle accounting-rebind --from-root /prior --confirm",
    )))
    assert result.returncode == 1
    assert "production workspace lifecycle commands are production-only" in result.stderr
    assert "./manage.sh -e dev local accounting-rebind" in result.stderr
    assert not invoked.exists()

    manage = (ROOT / "manage.sh").read_text()
    assert 'run_workspace_cli python3 "$lifecycle_command" "$@"' in manage
    assert "identity_json=$(run_workspace_cli python3 identity)" in manage


def test_moved_root_identity_and_boot_check_name_the_rebind_command(tmp_path):
    old_root, new_root, staged = _moved_root(tmp_path)
    repo = _fake_repo(tmp_path, new_root)
    lifecycle = repo / "docker-data" / "dev" / "workspace-lifecycle"
    lifecycle.parent.mkdir(parents=True)
    staged.rename(lifecycle)

    identity = _manage(repo, "identity")
    assert identity.returncode == 0, identity.stdout + identity.stderr
    receipt = json.loads(identity.stdout)
    assert receipt["accounting_continuity_status"] == "blocked"
    assert receipt["accounting_continuity_reason_code"] == "accounting_deployment_binding_unavailable"
    assert receipt["lifecycle_directory"] == str(lifecycle)
    assert str(new_root) not in identity.stdout and str(old_root) not in identity.stdout
    assert REBIND_HINT in identity.stderr

    # The boot-time check `local up` runs after preparing the descriptor.
    boot = _sourced("\n".join((
        f"LOCAL_WORKSPACE_DIR={shlex.quote(str(new_root))}",
        f"export SERAPH_WORKSPACE_LIFECYCLE_PATH={shlex.quote(str(lifecycle))}",
        "check_local_accounting_binding",
    )))
    assert boot.returncode == 0, boot.stderr
    assert "accounting_deployment_binding_unavailable" in boot.stderr
    assert REBIND_HINT in boot.stderr
    assert boot.stdout == ""
    assert "\n    check_local_accounting_binding\n" in (ROOT / "manage.sh").read_text()

    status = _manage(repo, "workspace-status")
    assert status.returncode == 0, status.stdout + status.stderr
    assert json.loads(status.stdout)["schema_version"] == "seraph.production-workspace-lifecycle.v1"


@pytest.mark.asyncio
async def test_local_accounting_rebind_retains_the_ledger_and_revokes_policy(accounting_db, tmp_path, monkeypatch):  # noqa: F811
    from config.settings import settings
    from src.model_fabric.configuration import read_model_fabric_configuration
    from src.workspace.accounting_continuity import verify_accounting_generation

    root, _calls = await prepare_source(accounting_db)
    source_workspace = ProductionWorkspace(host_root=root)
    deployment_id = read_lifecycle_receipt(source_workspace)["inference_accounting"]["deployment_id"]
    # The operator moved the local workspace: LOCAL_WORKSPACE_DIR now names a copy.
    target = tmp_path / "moved-dev-data"
    shutil.copytree(root, target)
    repo = _fake_repo(tmp_path, target)
    lifecycle = repo / "docker-data" / "dev" / "workspace-lifecycle"
    lifecycle.parent.mkdir(parents=True)
    (tmp_path / "deployment-lifecycle").rename(lifecycle)
    monkeypatch.setenv("SERAPH_WORKSPACE_LIFECYCLE_PATH", str(lifecycle))

    blocked = _manage(repo, "identity")
    assert json.loads(blocked.stdout)["accounting_continuity_reason_code"] == "accounting_deployment_binding_unavailable"

    unconfirmed = _manage(repo, "accounting-rebind", "--from-root", str(root))
    assert unconfirmed.returncode != 0
    assert json.loads(unconfirmed.stdout)["status"] == "blocked"

    rebound = _manage(repo, "accounting-rebind", "--from-root", str(root), "--confirm")
    assert rebound.returncode == 0, rebound.stdout + rebound.stderr
    receipt = json.loads(rebound.stdout)
    assert receipt["status"] == "rebound" and receipt["liabilities_retained"] == 2
    assert receipt["deployment_id"] == deployment_id
    assert "give your OK again in Settings" in rebound.stderr

    target_workspace = ProductionWorkspace(host_root=target)
    lifecycle_receipt = read_lifecycle_receipt(target_workspace)
    assert lifecycle_receipt["deployment_binding"]["root_path_digest"] == target_workspace.identity_digest
    account, rows = verify_accounting_generation(target / "seraph.db", lifecycle_receipt["inference_accounting"])
    assert account["deployment_id"] == deployment_id
    assert sum(row["actual_cost_microusd"] or 0 for row in rows) == 17
    monkeypatch.setattr(settings, "workspace_dir", str(target))
    assert read_model_fabric_configuration().egress_revoked is True

    ready = _manage(repo, "identity")
    assert ready.returncode == 0, ready.stderr
    assert json.loads(ready.stdout)["accounting_continuity_status"] == "ready"
    assert "accounting-rebind" not in ready.stderr

    again = _manage(repo, "accounting-rebind", "--from-root", str(root), "--confirm")
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["status"] == "already_rebound"
