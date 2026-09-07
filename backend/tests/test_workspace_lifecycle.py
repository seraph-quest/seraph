from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
import zipfile

import pytest
import src.workspace.lifecycle as workspace_lifecycle

from src.workspace import (
    ExternalReferencePolicy,
    ExternalReferenceSpec,
    InterruptedWorkspaceRestore,
    InvalidWorkspaceArchiveError,
    MissingSecretMaterialError,
    UnknownWorkspacePathError,
    WorkspaceConfig,
    WorkspaceDatabaseObjectSpec,
    WorkspaceIdentity,
    WorkspacePathSpec,
    WorkspaceStateClass,
    WorkspaceStateError,
    WorkspaceStateRegistry,
    backup_workspace,
    cleanup_workspace_backups,
    recover_interrupted_restore,
    restore_workspace,
    rollback_workspace,
    workspace_backup_dir,
    workspace_restore_staging_dir,
)


def _config(root: Path) -> WorkspaceConfig:
    return WorkspaceConfig(
        identity=WorkspaceIdentity("synthetic-lifecycle", root),
        declared_paths=(
            WorkspacePathSpec("seraph.db", WorkspaceStateClass.CANONICAL),
            WorkspacePathSpec("soul.md", WorkspaceStateClass.CANONICAL),
            WorkspacePathSpec("artifacts", WorkspaceStateClass.CANONICAL),
            WorkspacePathSpec("extensions", WorkspaceStateClass.CANONICAL),
            WorkspacePathSpec(".vault-key", WorkspaceStateClass.SECRET_RECOVERY),
            WorkspacePathSpec("derived", WorkspaceStateClass.DERIVED),
            WorkspacePathSpec("cache", WorkspaceStateClass.CACHE),
            WorkspacePathSpec("tmp", WorkspaceStateClass.DISPOSABLE),
            WorkspacePathSpec(".seraph-synthetic-workspace", WorkspaceStateClass.DISPOSABLE),
        ),
        external_references=(
            ExternalReferenceSpec("screen-capture", ExternalReferencePolicy.CONSENTED_LOGICAL_ROOT),
        ),
        expected_database_objects=(
            WorkspaceDatabaseObjectSpec("sessions", "table", WorkspaceStateClass.CANONICAL),
            WorkspaceDatabaseObjectSpec("messages", "table", WorkspaceStateClass.CANONICAL),
            WorkspaceDatabaseObjectSpec("ix_messages_session_id", "index", WorkspaceStateClass.DERIVED),
        ),
    )


def _workspace(tmp_path: Path) -> tuple[Path, WorkspaceStateRegistry]:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".seraph-synthetic-workspace").write_bytes(b"seraph-synthetic-workspace-v1\n")
    (root / "soul.md").write_text("original soul\n", encoding="utf-8")
    (root / "artifacts").mkdir()
    (root / "artifacts" / "report.md").write_text("canonical report\n", encoding="utf-8")
    (root / "extensions").mkdir()
    (root / ".vault-key").write_text("SECRET-SENTINEL-DO-NOT-ARCHIVE\n", encoding="utf-8")
    (root / "derived").mkdir()
    (root / "derived" / "vectors.idx").write_text("derived\n", encoding="utf-8")
    (root / "cache").mkdir()
    (root / "cache" / "cache.bin").write_text("cache\n", encoding="utf-8")
    (root / "tmp").mkdir()
    (root / "tmp" / "run.log").write_text("disposable\n", encoding="utf-8")
    with sqlite3.connect(root / "seraph.db") as connection:
        connection.executescript(
            """
            CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL);
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                role TEXT NOT NULL,
                content TEXT NOT NULL
            );
            CREATE INDEX ix_messages_session_id ON messages(session_id);
            INSERT INTO sessions(id, title) VALUES ('s1', 'session');
            INSERT INTO messages(id, session_id, role, content)
                VALUES ('m1', 's1', 'user', 'SECRET-DB-CONTENT');
            """
        )
    registry = WorkspaceStateRegistry(_config(root))
    return root, registry


def _archive_with_extra_member(source: Path, destination: Path, name: str, payload: bytes) -> None:
    with zipfile.ZipFile(source, "r") as reader, zipfile.ZipFile(destination, "w") as writer:
        for info in reader.infolist():
            writer.writestr(info, reader.read(info))
        writer.writestr(name, payload)


def test_backup_restore_round_trip_and_rollback_preserve_secret_boundary(tmp_path):
    root, registry = _workspace(tmp_path)
    backup = backup_workspace(root, registry=registry)
    archive = Path(backup["archive_path"])

    assert backup["secret_values_included"] is False
    assert backup["derived_state_included"] is False
    assert "SECRET-SENTINEL-DO-NOT-ARCHIVE" not in archive.read_bytes().decode("latin1")
    with zipfile.ZipFile(archive, "r") as archive_reader:
        archive_manifest = json.loads(archive_reader.read("manifest.json"))
        database_entry = next(
            entry
            for entry in archive_manifest["archive_entries"]
            if entry["logical_path"] == "seraph.db"
        )
        database_payload = archive_reader.read("payload/seraph.db")
    assert database_entry["digest_scope"] == "redacted_metadata"
    assert database_entry["sha256"] != database_entry["payload_sha256"]
    assert database_entry["payload_sha256"] == hashlib.sha256(database_payload).hexdigest()

    (root / "soul.md").write_text("changed soul\n", encoding="utf-8")
    restore = restore_workspace(
        root,
        archive,
        registry=registry,
        confirm=True,
        restore_id="restore-roundtrip-01",
    )
    assert restore["status"] == "restored"
    assert restore["rollback_available"] is True
    assert (root / "soul.md").read_text(encoding="utf-8") == "original soul\n"
    assert (root / ".vault-key").read_text(encoding="utf-8") == "SECRET-SENTINEL-DO-NOT-ARCHIVE\n"
    assert not (root / "derived" / "vectors.idx").exists()

    rollback = rollback_workspace(root, "restore-roundtrip-01")
    assert rollback["status"] == "rolled_back"
    assert (root / "soul.md").read_text(encoding="utf-8") == "changed soul\n"


@pytest.mark.parametrize("archive_kind", ["corrupt", "missing-manifest"])
def test_restore_rejects_corrupt_or_incomplete_archive(tmp_path, archive_kind):
    root, registry = _workspace(tmp_path)
    archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    invalid = tmp_path / f"{archive_kind}.zip"
    if archive_kind == "corrupt":
        invalid.write_bytes(b"not a zip archive")
    else:
        with zipfile.ZipFile(invalid, "w") as output:
            output.writestr("payload/seraph.db", b"incomplete")
    with pytest.raises(InvalidWorkspaceArchiveError):
        restore_workspace(root, invalid, registry=registry, confirm=True, restore_id="restore-invalid-01")


def test_restore_rejects_payload_traversal_and_archive_symlink(tmp_path):
    root, registry = _workspace(tmp_path)
    archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    traversal = tmp_path / "traversal.zip"
    _archive_with_extra_member(archive, traversal, "payload/../outside", b"escape")
    with pytest.raises(InvalidWorkspaceArchiveError, match="unsafe|unexpected"):
        restore_workspace(root, traversal, registry=registry, confirm=True, restore_id="restore-traversal-01")

    linked = tmp_path / "linked.zip"
    linked.symlink_to(archive)
    with pytest.raises(InvalidWorkspaceArchiveError):
        restore_workspace(root, linked, registry=registry, confirm=True, restore_id="restore-link-01")


def test_archive_member_bound_is_checked_before_payload_read(tmp_path, monkeypatch):
    root, registry = _workspace(tmp_path)
    archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    with zipfile.ZipFile(archive, "r") as archive_reader:
        manifest_size = archive_reader.getinfo("manifest.json").file_size
        database_size = archive_reader.getinfo("payload/seraph.db").file_size
    assert database_size > manifest_size
    monkeypatch.setattr(workspace_lifecycle, "MAX_ARCHIVE_MEMBER_BYTES", manifest_size + 1)
    original_read = zipfile.ZipFile.read

    def guarded_read(reader, member, *args, **kwargs):
        info = member if isinstance(member, zipfile.ZipInfo) else reader.getinfo(member)
        if info.filename == "payload/seraph.db":
            raise AssertionError("oversized payload was read before validation")
        return original_read(reader, member, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", guarded_read)
    with pytest.raises(InvalidWorkspaceArchiveError, match="bounded size"):
        restore_workspace(root, archive, registry=registry, confirm=True, restore_id="restore-size-01")


def test_interrupted_restore_is_recovered_from_journal(tmp_path):
    root, registry = _workspace(tmp_path)
    archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    (root / "soul.md").write_text("changed before interruption\n", encoding="utf-8")
    with pytest.raises(InterruptedWorkspaceRestore):
        restore_workspace(
            root,
            archive,
            registry=registry,
            confirm=True,
            restore_id="restore-interrupted-01",
            interrupt_after_active_move=True,
        )
    assert not root.exists()
    recovery = recover_interrupted_restore(root)
    assert recovery["status"] == "recovered"
    assert recovery["recovered"] == [
        {"restore_id": "restore-interrupted-01", "action": "promoted_staging"}
    ]
    assert (root / "soul.md").read_text(encoding="utf-8") == "original soul\n"


def test_restore_requires_existing_secret_material_and_rejects_secret_symlink(tmp_path):
    root, registry = _workspace(tmp_path)
    archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    (root / ".vault-key").unlink()
    with pytest.raises(MissingSecretMaterialError):
        restore_workspace(root, archive, registry=registry, confirm=True, restore_id="restore-secret-01")

    # Recreate the fixture and ensure a symlink cannot be copied into staging.
    shutil.rmtree(root)
    root, registry = _workspace(tmp_path)
    archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    outside = tmp_path / "outside-secret"
    outside.write_text("outside", encoding="utf-8")
    (root / ".vault-key").unlink()
    (root / ".vault-key").symlink_to(outside)
    with pytest.raises(WorkspaceStateError):
        restore_workspace(root, archive, registry=registry, confirm=True, restore_id="restore-secret-02")


def test_cleanup_retention_is_bounded_to_derived_backup_sidecar(tmp_path):
    root, registry = _workspace(tmp_path)
    archives = [
        Path(backup_workspace(root, registry=registry)["archive_path"])
        for _ in range(4)
    ]
    receipt = cleanup_workspace_backups(root, keep=1)
    assert receipt["retention"] == 1
    assert len(receipt["retained"]) == 1
    assert len(receipt["removed"]) == 3
    assert root.exists()
    assert all(path.exists() for path in archives if path.name in receipt["retained"])
    assert workspace_backup_dir(root).name == "workspace.backups"
    assert workspace_restore_staging_dir(root).name == "workspace.restore-staging"


def test_restore_requires_explicit_confirmation_and_rejects_bad_id(tmp_path):
    root, registry = _workspace(tmp_path)
    archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    with pytest.raises(WorkspaceStateError, match="confirm"):
        restore_workspace(root, archive, registry=registry)
    with pytest.raises(WorkspaceStateError, match="restore_id"):
        restore_workspace(root, archive, registry=registry, confirm=True, restore_id="../escape")
