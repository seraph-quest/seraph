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
    ProductionWorkspace,
    ProductionWorkspaceConfigurationError,
    ProductionWorkspaceError,
    ProductionWorkspaceMountError,
    maintenance_fence,
    read_lifecycle_receipt,
    runtime_workspace_owner,
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
    assert receipt["active_root_is_host_bind"] is False
    assert receipt["host_bind_identity"] == "configured_path_and_stat_digest"
    assert receipt["host_bind_identity_digest"] == workspace.bind_identity_digest
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
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "1 2 0:3 / /app/data rw,nosuid,nodev - ext4 /dev/test rw\n",
        encoding="utf-8",
    )
    values = {
        "WORKSPACE_DIR": "/app/data",
        "BACKEND_DATA_PATH_PROD": str(mount),
        "SERAPH_PRODUCTION_MOUNT_SOURCE": "/dev/test",
        "SERAPH_PRODUCTION_BIND_IDENTITY": ProductionWorkspace(host_root=mount).bind_identity_digest,
    }
    assert validate_container_workspace_mount(
        values,
        mounted_root=mount,
        mountinfo=mountinfo,
    )["canonical_mount"] is True

    with pytest.raises(ProductionWorkspaceMountError, match="identity is required"):
        validate_container_workspace_mount(
            {"WORKSPACE_DIR": "/app/data", "BACKEND_DATA_PATH_PROD": str(mount)},
            mounted_root=mount,
            mountinfo=mountinfo,
        )

    with pytest.raises(ProductionWorkspaceMountError, match="dedicated bind mount evidence"):
        validate_container_workspace_mount(
            {
                "WORKSPACE_DIR": "/app/data",
                "BACKEND_DATA_PATH_PROD": str(mount),
                "SERAPH_PRODUCTION_MOUNT_SOURCE": "/dev/wrong",
                "SERAPH_PRODUCTION_BIND_IDENTITY": ProductionWorkspace(host_root=mount).bind_identity_digest,
            },
            mounted_root=mount,
            mountinfo=mountinfo,
        )

    with pytest.raises(ProductionWorkspaceMountError):
        validate_container_workspace_mount({"WORKSPACE_DIR": str(mount)}, mounted_root=mount)

    link = tmp_path / "mount-link"
    link.symlink_to(mount, target_is_directory=True)
    with pytest.raises(ProductionWorkspaceMountError):
        validate_container_workspace_mount(
            {
                "WORKSPACE_DIR": "/app/data",
                "SERAPH_PRODUCTION_MOUNT_SOURCE": "/dev/test",
                "BACKEND_DATA_PATH_PROD": str(mount),
                "SERAPH_PRODUCTION_BIND_IDENTITY": ProductionWorkspace(host_root=mount).bind_identity_digest,
            },
            mounted_root=link,
            mountinfo=mountinfo,
        )

    with pytest.raises(ProductionWorkspaceMountError):
        validate_container_workspace_mount(
            {
                "WORKSPACE_DIR": "/app/data",
                "BACKEND_DATA_PATH_PROD": str(mount),
                "SERAPH_PRODUCTION_MOUNT_SOURCE": "/dev/test",
                "SERAPH_PRODUCTION_BIND_IDENTITY": ProductionWorkspace(host_root=mount).bind_identity_digest,
            },
            mounted_root=mount,
            mountinfo=tmp_path / "missing",
        )


def test_container_mount_identity_cannot_be_reused_for_a_different_directory(tmp_path):
    mount = tmp_path / "mounted"
    mount.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "1 2 0:3 / /app/data rw,nosuid,nodev - ext4 /dev/test rw\n",
        encoding="utf-8",
    )
    values = {
        "WORKSPACE_DIR": "/app/data",
        "BACKEND_DATA_PATH_PROD": str(mount),
        "SERAPH_PRODUCTION_MOUNT_SOURCE": "/dev/test",
        "SERAPH_PRODUCTION_BIND_IDENTITY": ProductionWorkspace(host_root=other).bind_identity_digest,
    }
    with pytest.raises(ProductionWorkspaceMountError, match="does not match"):
        validate_container_workspace_mount(values, mounted_root=mount, mountinfo=mountinfo)


def test_maintenance_fence_rejects_duplicate_owner(tmp_path):
    root = _workspace(tmp_path)
    workspace = resolve_production_workspace(_env(root), base_dir=tmp_path)
    with maintenance_fence(workspace):
        with pytest.raises(DuplicateWorkspaceOwnerError):
            with maintenance_fence(workspace):
                pass


def test_runtime_owner_blocks_maintenance_until_backend_releases_bind(tmp_path):
    root = _workspace(tmp_path)
    workspace = resolve_production_workspace(_env(root), base_dir=tmp_path)
    with runtime_workspace_owner(root):
        with pytest.raises(DuplicateWorkspaceOwnerError):
            with maintenance_fence(workspace):
                pass
    with maintenance_fence(workspace):
        pass


def test_managed_lifecycle_is_fenced_across_processes(tmp_path):
    """A second process cannot begin a lifecycle operation under the owner lease."""
    root = _workspace(tmp_path)
    environment = os.environ.copy()
    environment.update(_env(root))
    environment["PYTHONPATH"] = str(ROOT / "backend")
    holder_code = """
import sys
from pathlib import Path
from src.workspace import maintenance_fence, resolve_production_workspace

root = Path(sys.argv[1])
workspace = resolve_production_workspace(
    {
        "BACKEND_DATA_PATH_PROD": str(root),
        "WORKSPACE_DIR": str(root),
    },
    base_dir=root.parent,
)
with maintenance_fence(workspace):
    print("owner-ready", flush=True)
    sys.stdin.read(1)
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code, str(root)],
        env=environment,
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "owner-ready"
        blocked = subprocess.run(
            [sys.executable, str(CLI), "--base-dir", str(tmp_path), "backup"],
            env=environment,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert blocked.returncode == 78
        receipt = json.loads(blocked.stdout)
        assert receipt["reason_code"] == "production_workspace_owner_busy"
        assert "SECRET-SENTINEL" not in blocked.stdout
    finally:
        if holder.stdin is not None:
            holder.stdin.write("x")
            holder.stdin.close()
        holder.wait(timeout=10)
        if holder.returncode != 0:
            assert holder.stderr is not None
            raise AssertionError(holder.stderr.read())


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
    restore_receipt = json.loads(restore.stdout)
    assert restore_receipt["status"] == "restored"
    assert restore_receipt["bind_identity_refresh"]["status"] == "ready"
    assert restore_receipt["bind_identity_refresh"]["bind_identity"] == ProductionWorkspace(
        host_root=root
    ).bind_identity_digest
    assert (root / "soul.md").read_text(encoding="utf-8") == "canonical\n"


def test_managed_cli_identity_is_redacted_and_reproducible(tmp_path):
    root = _workspace(tmp_path)
    environment = os.environ.copy()
    environment.update(_env(root))
    environment["PYTHONPATH"] = str(ROOT / "backend")
    identity = subprocess.run(
        [sys.executable, str(CLI), "--base-dir", str(tmp_path), "identity"],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert identity.returncode == 0, identity.stderr
    receipt = json.loads(identity.stdout)
    assert receipt["status"] == "ready"
    assert receipt["bind_identity"] == ProductionWorkspace(host_root=root).bind_identity_digest
    assert str(root) not in identity.stdout
    assert receipt["secret_values_included"] is False


def test_managed_restore_invalidates_authority_sessions_and_optional_tokens(tmp_path):
    root = _workspace(tmp_path)
    (root / "google_calendar_token.json").write_text("OPTIONAL-TOKEN\n", encoding="utf-8")
    with sqlite3.connect(root / "seraph.db") as database:
        database.execute(
            "CREATE TABLE operator_sessions (id TEXT PRIMARY KEY, revoked_at TEXT)"
        )
        database.execute("INSERT INTO operator_sessions VALUES ('session-1', NULL)")
        database.execute(
            "CREATE TABLE production_workflow_authority_states "
            "(id TEXT PRIMARY KEY, workflow_phase TEXT, safe_replay_decision TEXT, "
            "blocked_replay_reason TEXT)"
        )
        database.execute(
            "INSERT INTO production_workflow_authority_states VALUES "
            "('run-1', 'running', 'unsafe', NULL)"
        )
    environment = os.environ.copy()
    environment.update(_env(root))
    environment["PYTHONPATH"] = str(ROOT / "backend")
    backup = subprocess.run(
        [sys.executable, str(CLI), "--base-dir", str(tmp_path), "backup", "--archive", "auth.zip"],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert backup.returncode == 0, backup.stderr
    archive = Path(json.loads(backup.stdout)["archive_path"])
    restore = subprocess.run(
        [sys.executable, str(CLI), "--base-dir", str(tmp_path), "restore", "--archive", str(archive), "--confirm"],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert restore.returncode == 0, restore.stderr
    receipt = json.loads(restore.stdout)
    reconciliation = receipt["stage_receipt"]["restore_reconciliation"]
    assert reconciliation["status"] == "ready"
    assert reconciliation["authority_invalidation"]["operator_sessions_invalidated"] == 1
    assert reconciliation["authority_invalidation"]["workflow_authority_rows_blocked"] == 1
    assert reconciliation["token_invalidation"]["optional_credentials_invalidated"] == [
        "google_calendar_token.json"
    ]
    with sqlite3.connect(root / "seraph.db") as database:
        assert database.execute(
            "SELECT revoked_at FROM operator_sessions WHERE id = 'session-1'"
        ).fetchone()[0]
        assert database.execute(
            "SELECT workflow_phase, safe_replay_decision, blocked_replay_reason "
            "FROM production_workflow_authority_states WHERE id = 'run-1'"
        ).fetchone() == (
            "blocked",
            "unsafe",
            "workspace_restore_requires_reconciliation",
        )
    assert not (root / "google_calendar_token.json").exists()


def test_managed_restore_blocks_when_a_stored_derived_index_needs_rebuild(tmp_path):
    root = _workspace(tmp_path)
    (root / "lance").mkdir()
    (root / "lance" / "index.bin").write_bytes(b"derived-index")
    environment = os.environ.copy()
    environment.update(_env(root))
    environment["PYTHONPATH"] = str(ROOT / "backend")
    backup = subprocess.run(
        [sys.executable, str(CLI), "--base-dir", str(tmp_path), "backup", "--archive", "derived.zip"],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert backup.returncode == 0, backup.stderr
    archive = json.loads(backup.stdout)["archive_path"]
    restore = subprocess.run(
        [sys.executable, str(CLI), "--base-dir", str(tmp_path), "restore", "--archive", archive, "--confirm"],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert restore.returncode == 78
    assert json.loads(restore.stdout)["reason_code"] == "production_workspace_reconciliation_required"
    assert (root / "lance" / "index.bin").read_bytes() == b"derived-index"


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

    status_after_block = subprocess.run(
        [sys.executable, str(CLI), "--base-dir", str(tmp_path), "status"],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert status_after_block.returncode == 0
    assert json.loads(status_after_block.stdout)["status"] == "blocked"


def test_managed_status_and_rollback_are_durable_and_operator_visible(tmp_path):
    root = _workspace(tmp_path)
    environment = os.environ.copy()
    environment.update(_env(root))
    environment["PYTHONPATH"] = str(ROOT / "backend")
    backup = subprocess.run(
        [sys.executable, str(CLI), "--base-dir", str(tmp_path), "backup", "--archive", "status.zip"],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert backup.returncode == 0, backup.stderr
    status = subprocess.run(
        [sys.executable, str(CLI), "--base-dir", str(tmp_path), "status"],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert status.returncode == 0, status.stderr
    status_receipt = json.loads(status.stdout)
    assert status_receipt["status"] == "ready"
    assert status_receipt["last_result"]["operation"] == "backup"
    assert status_receipt["rollback_available"] is False

    archive = Path(json.loads(backup.stdout)["archive_path"])
    (root / "soul.md").write_text("before-restore\n", encoding="utf-8")
    restored = subprocess.run(
        [sys.executable, str(CLI), "--base-dir", str(tmp_path), "restore", "--archive", str(archive), "--confirm"],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert restored.returncode == 0, restored.stderr
    restore_id = json.loads(restored.stdout)["restore_id"]
    assert (root / "soul.md").read_text(encoding="utf-8") == "canonical\n"
    rollback = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "--base-dir",
            str(tmp_path),
            "rollback",
            "--restore-id",
            restore_id,
            "--confirm",
        ],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rollback.returncode == 0, rollback.stderr
    assert json.loads(rollback.stdout)["status"] == "rolled_back"
    assert (root / "soul.md").read_text(encoding="utf-8") == "before-restore\n"
    durable = read_lifecycle_receipt(resolve_production_workspace(_env(root), base_dir=tmp_path))
    assert durable is not None
    assert durable["operation"] == "rollback"


def test_managed_restore_rejects_relative_archive_escape(tmp_path):
    root = _workspace(tmp_path)
    environment = os.environ.copy()
    environment.update(_env(root))
    environment["PYTHONPATH"] = str(ROOT / "backend")
    blocked = subprocess.run(
        [sys.executable, str(CLI), "--base-dir", str(tmp_path), "restore", "--archive", "../outside.zip", "--confirm"],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert blocked.returncode == 78
    assert blocked.stdout


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


def test_isolated_backup_restore_restart_and_rollback_drill_preserves_state(tmp_path):
    """Exercise the managed path with representative durable state and readback."""
    root = _workspace(tmp_path)
    (root / "google_calendar_token.json").write_text("OPTIONAL-TOKEN\n", encoding="utf-8")
    with sqlite3.connect(root / "seraph.db") as database:
        database.executescript(
            """
            CREATE TABLE goals (id TEXT PRIMARY KEY, title TEXT NOT NULL);
            CREATE TABLE memory_items (id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE artifact_registry (artifact_id TEXT PRIMARY KEY, logical_path TEXT NOT NULL);
            CREATE TABLE tombstones (id TEXT PRIMARY KEY, reason TEXT NOT NULL);
            CREATE TABLE revocations (id TEXT PRIMARY KEY, revoked_at TEXT NOT NULL);
            CREATE TABLE config_versions (id TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE cost_liabilities (id TEXT PRIMARY KEY, amount INTEGER NOT NULL);
            CREATE TABLE operator_sessions (id TEXT PRIMARY KEY, revoked_at TEXT);
            CREATE TABLE production_workflow_authority_states (
                id TEXT PRIMARY KEY,
                workflow_phase TEXT,
                safe_replay_decision TEXT,
                blocked_replay_reason TEXT
            );
            INSERT INTO goals VALUES ('goal-1', 'recover the workspace');
            INSERT INTO memory_items VALUES ('memory-1', 'durable memory');
            INSERT INTO artifact_registry VALUES ('artifact-1', 'artifacts/report.md');
            INSERT INTO tombstones VALUES ('tombstone-1', 'operator deleted item');
            INSERT INTO revocations VALUES ('grant-1', '2026-09-01T00:00:00Z');
            INSERT INTO config_versions VALUES ('config-1', 'v1');
            INSERT INTO cost_liabilities VALUES ('liability-1', 17);
            INSERT INTO operator_sessions VALUES ('session-1', NULL);
            INSERT INTO production_workflow_authority_states VALUES
                ('run-1', 'running', 'unsafe', NULL);
            """
        )
    environment = os.environ.copy()
    environment.update(_env(root))
    environment["PYTHONPATH"] = str(ROOT / "backend")

    def run(*arguments: str) -> dict[str, object]:
        completed = subprocess.run(
            [sys.executable, str(CLI), "--base-dir", str(tmp_path), *arguments],
            env=environment,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr or completed.stdout
        assert "SECRET-SENTINEL" not in completed.stdout
        assert "OPTIONAL-TOKEN" not in completed.stdout
        return json.loads(completed.stdout)

    def digest(path: Path) -> str:
        import hashlib

        return hashlib.sha256(path.read_bytes()).hexdigest()

    source_hashes = {
        "soul": digest(root / "soul.md"),
        "report": digest(root / "artifacts" / "report.md"),
    }
    with sqlite3.connect(root / "seraph.db") as database:
        source_counts = {
            table: int(database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "goals",
                "memory_items",
                "artifact_registry",
                "tombstones",
                "revocations",
                "config_versions",
                "cost_liabilities",
            )
        }
        artifact_ids = [
            row[0]
            for row in database.execute(
                "SELECT artifact_id FROM artifact_registry ORDER BY artifact_id"
            ).fetchall()
        ]

    backup = run("backup", "--archive", "isolated-drill.zip")
    archive = Path(str(backup["archive_path"]))
    assert backup["member_count"] > 0
    assert archive.exists()
    assert "OPTIONAL-TOKEN" not in archive.read_bytes().decode("latin1")

    (root / "soul.md").write_text("mutated soul\n", encoding="utf-8")
    (root / "artifacts" / "report.md").write_text("mutated report\n", encoding="utf-8")
    with sqlite3.connect(root / "seraph.db") as database:
        database.execute("UPDATE records SET value = 'mutated' WHERE id = 'r1'")
        database.execute("DELETE FROM tombstones WHERE id = 'tombstone-1'")
        database.execute("DELETE FROM revocations WHERE id = 'grant-1'")
        database.execute("DELETE FROM config_versions WHERE id = 'config-1'")
        database.execute("DELETE FROM cost_liabilities WHERE id = 'liability-1'")
        database.execute(
            "INSERT INTO artifact_registry VALUES ('artifact-mutated', 'artifacts/report.md')"
        )

    restored = run("restore", "--archive", str(archive), "--confirm")
    restore_id = str(restored["restore_id"])
    assert restored["stage_receipt"]["database_verified"] is True
    assert restored["stage_receipt"]["restore_reconciliation"]["status"] == "ready"
    assert digest(root / "soul.md") == source_hashes["soul"]
    assert digest(root / "artifacts" / "report.md") == source_hashes["report"]
    with sqlite3.connect(root / "seraph.db") as database:
        assert {
            table: int(database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in source_counts
        } == source_counts
        assert [
            row[0]
            for row in database.execute(
                "SELECT artifact_id FROM artifact_registry ORDER BY artifact_id"
            ).fetchall()
        ] == artifact_ids
        assert database.execute(
            "SELECT revoked_at FROM operator_sessions WHERE id = 'session-1'"
        ).fetchone()[0]
        assert database.execute(
            "SELECT workflow_phase FROM production_workflow_authority_states WHERE id = 'run-1'"
        ).fetchone()[0] == "blocked"
    assert not (root / "google_calendar_token.json").exists()

    restarted_status = run("status")
    assert restarted_status["status"] == "ready"
    assert restarted_status["last_result"]["operation"] == "restore"

    rolled_back = run("rollback", "--restore-id", restore_id, "--confirm")
    assert rolled_back["status"] == "rolled_back"
    assert rolled_back["rollback_reconciliation"]["status"] == "ready"
    assert (root / "soul.md").read_text(encoding="utf-8") == "mutated soul\n"
    assert (root / "artifacts" / "report.md").read_text(encoding="utf-8") == "mutated report\n"
    with sqlite3.connect(root / "seraph.db") as database:
        assert database.execute("SELECT value FROM records WHERE id = 'r1'").fetchone()[0] == "mutated"
        assert database.execute("SELECT COUNT(*) FROM tombstones WHERE id = 'tombstone-1'").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM revocations WHERE id = 'grant-1'").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM config_versions WHERE id = 'config-1'").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM cost_liabilities WHERE id = 'liability-1'").fetchone()[0] == 1
        assert database.execute(
            "SELECT revoked_at FROM operator_sessions WHERE id = 'session-1'"
        ).fetchone()[0]
        assert database.execute(
            "SELECT workflow_phase FROM production_workflow_authority_states WHERE id = 'run-1'"
        ).fetchone()[0] == "blocked"
    assert not (root / "google_calendar_token.json").exists()


def workspace_backup_dir_for(root: Path) -> Path:
    return root.parent / f"{root.name}.backups"
