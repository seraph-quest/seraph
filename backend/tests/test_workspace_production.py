from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from src.workspace import (
    DuplicateWorkspaceOwnerError,
    ProductionWorkspaceConfigurationError,
    ProductionWorkspaceError,
    ProductionWorkspaceMountError,
    maintenance_fence,
    resolve_production_workspace,
    validate_container_workspace_mount,
)


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "backend" / "workspace_cli.py"


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "production-data"
    (root / "artifacts").mkdir(parents=True)
    (root / ".vault-key").write_text("SECRET-SENTINEL\n", encoding="utf-8")
    (root / "soul.md").write_text("canonical\n", encoding="utf-8")
    (root / "artifacts" / "report.md").write_text("report\n", encoding="utf-8")
    with sqlite3.connect(root / "seraph.db") as database:
        database.execute("CREATE TABLE records (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
        database.execute("INSERT INTO records VALUES ('r1', 'database-value')")
    return root


def _env(root: Path) -> dict[str, str]:
    return {
        "BACKEND_DATA_PATH_PROD": str(root),
        "WORKSPACE_DIR": str(root),
        "DEPLOYMENT_ENVIRONMENT": "production",
    }


def test_production_resolution_binds_only_the_configured_host_root(tmp_path):
    root = _workspace(tmp_path)
    values = _env(root)
    values["BACKEND_DATA_PATH_PROD"] = str(root.relative_to(tmp_path))
    workspace = resolve_production_workspace(values, base_dir=tmp_path)

    assert workspace.host_root == root
    assert workspace.container_root == Path("/app/data")
    assert workspace.backup_root == tmp_path / "production-data.backups"
    assert workspace.restore_staging_root == tmp_path / "production-data.restore-staging"
    receipt = workspace.receipt()
    assert receipt["canonical_container_mount"] == "/app/data"
    assert receipt["active_root_is_host_bind"] is True
    assert receipt["sidecars_are_active_roots"] is False
    assert str(root) not in json.dumps(receipt, sort_keys=True)


def test_production_resolution_rejects_missing_or_duplicate_owner(tmp_path):
    root = _workspace(tmp_path)
    with pytest.raises(ProductionWorkspaceConfigurationError):
        resolve_production_workspace({"WORKSPACE_DIR": str(root)}, base_dir=tmp_path)

    other = _workspace(tmp_path / "other")
    values = _env(root)
    values["WORKSPACE_DIR"] = str(other)
    with pytest.raises(ProductionWorkspaceError, match="different roots"):
        resolve_production_workspace(values, base_dir=tmp_path)

    linked = tmp_path / "linked-data"
    linked.symlink_to(root, target_is_directory=True)
    values = _env(linked)
    values.pop("WORKSPACE_DIR")
    with pytest.raises(ProductionWorkspaceConfigurationError):
        resolve_production_workspace(values, base_dir=tmp_path)


def test_container_mount_requires_exact_app_data_and_safe_directory(tmp_path):
    mount = tmp_path / "mounted"
    mount.mkdir()
    assert validate_container_workspace_mount(
        {"WORKSPACE_DIR": "/app/data"}, mounted_root=mount
    )["canonical_mount"] is True

    with pytest.raises(ProductionWorkspaceMountError):
        validate_container_workspace_mount({"WORKSPACE_DIR": str(mount)}, mounted_root=mount)

    link = tmp_path / "mount-link"
    link.symlink_to(mount, target_is_directory=True)
    with pytest.raises(ProductionWorkspaceMountError):
        validate_container_workspace_mount({"WORKSPACE_DIR": "/app/data"}, mounted_root=link)


def test_maintenance_fence_rejects_duplicate_owner(tmp_path):
    root = _workspace(tmp_path)
    workspace = resolve_production_workspace(_env(root), base_dir=tmp_path)
    with maintenance_fence(workspace):
        with pytest.raises(DuplicateWorkspaceOwnerError):
            with maintenance_fence(workspace):
                pass


def test_managed_cli_backup_restore_is_redacted_and_staged(tmp_path):
    root = _workspace(tmp_path)
    environment = os.environ.copy()
    environment.update(_env(root))
    environment["PYTHONPATH"] = str(ROOT / "backend")

    backup = subprocess.run(
        [sys.executable, str(CLI), "--base-dir", str(tmp_path), "backup", "--archive", "roundtrip.zip"],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert backup.returncode == 0, backup.stderr
    backup_receipt = json.loads(backup.stdout)
    assert backup_receipt["status"] == "created"
    assert backup_receipt["secret_values_included"] is False
    assert "SECRET-SENTINEL" not in backup.stdout
    archive = Path(backup_receipt["archive_path"])
    assert archive.parent == workspace_backup_dir_for(root)
    assert "SECRET-SENTINEL" not in archive.read_bytes().decode("latin1")

    (root / "soul.md").write_text("changed\n", encoding="utf-8")
    restore = subprocess.run(
        [sys.executable, str(CLI), "--base-dir", str(tmp_path), "restore", "--archive", str(archive), "--confirm"],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert restore.returncode == 0, restore.stderr
    assert json.loads(restore.stdout)["status"] == "restored"
    assert (root / "soul.md").read_text(encoding="utf-8") == "canonical\n"


def test_manage_prod_commands_use_host_bind_and_never_start_compose(tmp_path):
    root = _workspace(tmp_path)
    env_file = ROOT / ".env.prod"
    assert not env_file.exists()
    env_file.write_text(
        "\n".join(
            (
                "DEPLOYMENT_ENVIRONMENT=production",
                f"BACKEND_DATA_PATH_PROD={root}",
                f"WORKSPACE_DIR={root}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        backup = subprocess.run(
            ["bash", "manage.sh", "-e", "prod", "backup", "--archive", "managed.zip"],
            env={**os.environ, "PATH": os.environ["PATH"]},
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert backup.returncode == 0, backup.stderr
        receipt = json.loads(backup.stdout)
        archive = Path(receipt["archive_path"])
        assert archive == root.parent / f"{root.name}.backups" / "managed.zip"
        assert receipt["workspace_ownership"]["canonical_container_mount"] == "/app/data"
    finally:
        env_file.unlink(missing_ok=True)


def test_managed_cli_restore_rejects_corrupt_archive_and_confirmation_gap(tmp_path):
    root = _workspace(tmp_path)
    environment = os.environ.copy()
    environment.update(_env(root))
    environment["PYTHONPATH"] = str(ROOT / "backend")
    corrupt = tmp_path / "corrupt.zip"
    corrupt.write_bytes(b"not-a-zip")

    missing_confirmation = subprocess.run(
        [sys.executable, str(CLI), "--base-dir", str(tmp_path), "restore", "--archive", str(corrupt)],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing_confirmation.returncode == 78
    assert json.loads(missing_confirmation.stdout)["reason_code"] == "workspace_state_invalid"

    blocked = subprocess.run(
        [sys.executable, str(CLI), "--base-dir", str(tmp_path), "restore", "--archive", str(corrupt), "--confirm"],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert blocked.returncode == 78
    assert json.loads(blocked.stdout)["reason_code"] == "invalid_workspace_archive"


def test_managed_cli_blocks_unknown_entries_and_missing_secret(tmp_path):
    root = _workspace(tmp_path)
    environment = os.environ.copy()
    environment.update(_env(root))
    environment["PYTHONPATH"] = str(ROOT / "backend")

    (root / "unexpected.txt").write_text("do not archive\n", encoding="utf-8")
    blocked = subprocess.run(
        [sys.executable, str(CLI), "--base-dir", str(tmp_path), "backup"],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert blocked.returncode == 78
    assert json.loads(blocked.stdout)["reason_code"] == "workspace_state_invalid"
    (root / "unexpected.txt").unlink()
    (root / ".vault-key").unlink()
    blocked = subprocess.run(
        [sys.executable, str(CLI), "--base-dir", str(tmp_path), "backup"],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert blocked.returncode == 78
    assert json.loads(blocked.stdout)["reason_code"] == "workspace_state_invalid"
    assert "SECRET-SENTINEL" not in blocked.stdout


def workspace_backup_dir_for(root: Path) -> Path:
    return root.parent / f"{root.name}.backups"
