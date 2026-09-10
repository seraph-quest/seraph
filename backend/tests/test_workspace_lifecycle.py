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
    WorkspaceLifecycleError,
    WorkspacePathSpec,
    WorkspaceRootKind,
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


def _rewrite_archive_manifest(source: Path, destination: Path, mutate) -> None:
    with zipfile.ZipFile(source, "r") as reader:
        members = {info.filename: reader.read(info) for info in reader.infolist()}
    manifest = json.loads(members["manifest.json"])
    mutate(manifest)
    source_manifest = manifest.get("workspace_manifest")
    if isinstance(source_manifest, dict) and "manifest_sha256" in source_manifest:
        source_digest_body = dict(source_manifest)
        source_digest_body.pop("manifest_sha256", None)
        source_manifest["manifest_sha256"] = hashlib.sha256(
            json.dumps(source_digest_body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        manifest["source_manifest_sha256"] = source_manifest["manifest_sha256"]
    digest_body = dict(manifest)
    digest_body.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(digest_body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    with zipfile.ZipFile(destination, "w") as writer:
        for name, payload in members.items():
            if name == "manifest.json":
                writer.writestr(name, json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            else:
                writer.writestr(name, payload)


def test_production_backup_stops_when_required_secret_is_missing(tmp_path):
    root, fixture_registry = _workspace(tmp_path)
    fixture_config = fixture_registry.config
    production_registry = WorkspaceStateRegistry(
        WorkspaceConfig(
            identity=WorkspaceIdentity("workspace-primary", root, WorkspaceRootKind.PRODUCTION),
            declared_paths=fixture_config.declared_paths,
            database_path=fixture_config.database_path,
            workspace_version=fixture_config.workspace_version,
            external_references=fixture_config.external_references,
            expected_database_objects=fixture_config.expected_database_objects,
        )
    )
    (root / ".vault-key").unlink()
    archive_path = tmp_path / "production-backup.zip"

    with pytest.raises(WorkspaceStateError, match="required secret workspace path"):
        backup_workspace(root, registry=production_registry, archive_path=archive_path)

    assert not archive_path.exists()
    assert not workspace_backup_dir(root).exists()


def test_backup_rejects_secret_directory_shape(tmp_path):
    root, registry = _workspace(tmp_path)
    (root / ".vault-key").unlink()
    (root / ".vault-key").mkdir()

    with pytest.raises(WorkspaceLifecycleError, match="regular file"):
        backup_workspace(root, registry=registry, archive_path=tmp_path / "secret-directory.zip")


def test_backup_and_restore_reject_symlinked_archive_paths(tmp_path):
    root, registry = _workspace(tmp_path)
    outside_archive = tmp_path / "outside.zip"
    outside_archive.write_bytes(b"outside-sentinel")
    linked_destination = tmp_path / "linked-destination.zip"
    linked_destination.symlink_to(outside_archive)

    with pytest.raises(WorkspaceLifecycleError, match="symlink"):
        backup_workspace(root, registry=registry, archive_path=linked_destination)
    assert outside_archive.read_bytes() == b"outside-sentinel"

    archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    linked_parent = tmp_path / "linked-archive-parent"
    linked_parent.symlink_to(archive.parent, target_is_directory=True)
    linked_archive = linked_parent / archive.name
    with pytest.raises(InvalidWorkspaceArchiveError, match="symlink"):
        restore_workspace(
            root,
            linked_archive,
            registry=registry,
            confirm=True,
            restore_id="restore-linked-archive-01",
        )


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


def test_restore_rejects_archive_requiredness_downgrade(tmp_path):
    root, registry = _workspace(tmp_path)
    archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    downgraded = tmp_path / "downgraded-requiredness.zip"

    def downgrade(manifest):
        source_entry = next(
            entry
            for entry in manifest["workspace_manifest"]["entries"]
            if entry["logical_path"] == ".vault-key"
        )
        archive_entry = next(
            entry for entry in manifest["archive_entries"] if entry["logical_path"] == ".vault-key"
        )
        source_entry["required"] = False
        archive_entry["required"] = False

    _rewrite_archive_manifest(archive, downgraded, downgrade)
    with pytest.raises(InvalidWorkspaceArchiveError, match="requiredness"):
        restore_workspace(root, downgraded, registry=registry, confirm=True, restore_id="restore-downgrade-01")


def test_restore_rejects_archive_missing_required_secret_metadata(tmp_path):
    root, registry = _workspace(tmp_path)
    archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    missing = tmp_path / "missing-required-secret.zip"

    def remove_required_secret(manifest):
        manifest["workspace_manifest"]["entries"] = [
            entry
            for entry in manifest["workspace_manifest"]["entries"]
            if entry["logical_path"] != ".vault-key"
        ]
        manifest["archive_entries"] = [
            entry for entry in manifest["archive_entries"] if entry["logical_path"] != ".vault-key"
        ]

    _rewrite_archive_manifest(archive, missing, remove_required_secret)
    with pytest.raises(InvalidWorkspaceArchiveError, match="required secret material metadata"):
        restore_workspace(root, missing, registry=registry, confirm=True, restore_id="restore-missing-secret-01")


def test_restore_accepts_legacy_archive_without_requiredness_metadata(tmp_path):
    root, base_registry = _workspace(tmp_path)
    base_config = base_registry.config
    optional_path = "google_calendar_token.json"
    (root / optional_path).write_text("legacy-calendar-token", encoding="utf-8")
    registry = WorkspaceStateRegistry(
        WorkspaceConfig(
            identity=base_config.identity,
            declared_paths=(
                *base_config.declared_paths,
                WorkspacePathSpec(optional_path, WorkspaceStateClass.SECRET, required=False),
            ),
            database_path=base_config.database_path,
            workspace_version=base_config.workspace_version,
            external_references=base_config.external_references,
            expected_database_objects=base_config.expected_database_objects,
        )
    )
    archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    legacy = tmp_path / "legacy-v1.zip"

    def remove_requiredness_metadata(manifest):
        for entry in manifest["workspace_manifest"]["entries"]:
            entry.pop("required", None)
        for entry in manifest["archive_entries"]:
            entry.pop("required", None)

    _rewrite_archive_manifest(archive, legacy, remove_requiredness_metadata)
    (root / optional_path).unlink()
    restore = restore_workspace(
        root,
        legacy,
        registry=registry,
        confirm=True,
        restore_id="restore-legacy-v1-01",
    )

    assert restore["status"] == "restored"
    assert not (root / optional_path).exists()


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


def test_recovery_rejects_tampered_journal_before_moving_roots(tmp_path):
    root, registry = _workspace(tmp_path)
    archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    restore_id = "restore-journal-integrity-01"
    with pytest.raises(InterruptedWorkspaceRestore):
        restore_workspace(
            root,
            archive,
            registry=registry,
            confirm=True,
            restore_id=restore_id,
            interrupt_after_active_move=True,
        )

    journal_path = workspace_backup_dir(root) / restore_id / "restore-journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["journal_sha256"]
    journal["status"] = "promoted"
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(WorkspaceLifecycleError, match="checksum"):
        recover_interrupted_restore(root)
    assert not root.exists()
    assert (workspace_backup_dir(root) / restore_id / "previous-workspace").is_dir()
    assert (workspace_restore_staging_dir(root) / restore_id).is_dir()


def test_recovery_rejects_valid_but_unknown_journal_state(tmp_path):
    root, registry = _workspace(tmp_path)
    archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    restore_id = "restore-journal-binding-01"
    with pytest.raises(InterruptedWorkspaceRestore):
        restore_workspace(
            root,
            archive,
            registry=registry,
            confirm=True,
            restore_id=restore_id,
            interrupt_after_active_move=True,
        )

    journal_path = workspace_backup_dir(root) / restore_id / "restore-journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["status"] = "unknown-state"
    workspace_lifecycle._refresh_journal_digest(journal)
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(WorkspaceLifecycleError, match="unknown state"):
        recover_interrupted_restore(root)
    assert not root.exists()
    assert (workspace_backup_dir(root) / restore_id / "previous-workspace").is_dir()
    assert (workspace_restore_staging_dir(root) / restore_id).is_dir()


def test_recovery_rejects_journal_record_identity_mismatch(tmp_path):
    root, registry = _workspace(tmp_path)
    archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    restore_id = "restore-journal-binding-01"
    with pytest.raises(InterruptedWorkspaceRestore):
        restore_workspace(
            root,
            archive,
            registry=registry,
            confirm=True,
            restore_id=restore_id,
            interrupt_after_active_move=True,
        )

    journal_path = workspace_backup_dir(root) / restore_id / "restore-journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["restore_id"] = "restore-other-record-01"
    workspace_lifecycle._refresh_journal_digest(journal)
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(WorkspaceLifecycleError, match="identity"):
        recover_interrupted_restore(root)
    assert not root.exists()
    assert (workspace_backup_dir(root) / restore_id / "previous-workspace").is_dir()
    assert (workspace_restore_staging_dir(root) / restore_id).is_dir()


def test_recovery_preflights_all_records_before_moving_any_root(tmp_path):
    root, registry = _workspace(tmp_path)
    archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    valid_id = "restore-a-valid-01"
    with pytest.raises(InterruptedWorkspaceRestore):
        restore_workspace(
            root,
            archive,
            registry=registry,
            confirm=True,
            restore_id=valid_id,
            interrupt_after_active_move=True,
        )

    corrupt_id = "restore-z-corrupt-01"
    corrupt_record = workspace_backup_dir(root) / corrupt_id
    corrupt_record.mkdir()
    (corrupt_record / "restore-journal.json").write_bytes(b"{not-json")

    with pytest.raises(WorkspaceLifecycleError, match="unreadable"):
        recover_interrupted_restore(root, registry=registry)
    assert not root.exists()
    assert (workspace_backup_dir(root) / valid_id / "previous-workspace").is_dir()
    assert (workspace_restore_staging_dir(root) / valid_id).is_dir()


def test_recovery_skips_superseded_terminal_restore_records(tmp_path):
    root, registry = _workspace(tmp_path)
    archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    terminal_id = "restore-old-terminal-01"
    restore_workspace(
        root,
        archive,
        registry=registry,
        confirm=True,
        restore_id=terminal_id,
    )

    terminal_journal_path = workspace_backup_dir(root) / terminal_id / "restore-journal.json"
    terminal_journal = json.loads(terminal_journal_path.read_text(encoding="utf-8"))
    terminal_journal["created_at"] = "2020-01-01T00:00:00+00:00"
    workspace_lifecycle._refresh_journal_digest(terminal_journal)
    terminal_journal_path.write_text(json.dumps(terminal_journal), encoding="utf-8")

    in_flight_id = "restore-new-inflight-01"
    with pytest.raises(InterruptedWorkspaceRestore):
        restore_workspace(
            root,
            archive,
            registry=registry,
            confirm=True,
            restore_id=in_flight_id,
            interrupt_after_active_move=True,
        )
    assert not root.exists()

    recovery = recover_interrupted_restore(root, registry=registry)
    assert recovery["status"] == "recovered"
    assert recovery["recovered"] == [
        {"restore_id": in_flight_id, "action": "promoted_staging"}
    ]
    assert (root / "soul.md").read_text(encoding="utf-8") == "original soul\n"
    assert (workspace_backup_dir(root) / terminal_id / "previous-workspace").is_dir()


def test_recovery_binds_journal_to_the_target_workspace_identity(tmp_path):
    root, registry = _workspace(tmp_path)
    archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    restore_id = "restore-identity-bound-01"
    with pytest.raises(InterruptedWorkspaceRestore):
        restore_workspace(
            root,
            archive,
            registry=registry,
            confirm=True,
            restore_id=restore_id,
            interrupt_after_active_move=True,
        )

    config = registry.config
    other_registry = WorkspaceStateRegistry(
        WorkspaceConfig(
            identity=WorkspaceIdentity("workspace-other", root),
            declared_paths=config.declared_paths,
            database_path=config.database_path,
            workspace_version=config.workspace_version,
            external_references=config.external_references,
            expected_database_objects=config.expected_database_objects,
        )
    )
    with pytest.raises(WorkspaceLifecycleError, match="workspace identity"):
        recover_interrupted_restore(root, registry=other_registry)
    assert not root.exists()
    assert (workspace_backup_dir(root) / restore_id / "previous-workspace").is_dir()
    assert (workspace_restore_staging_dir(root) / restore_id).is_dir()


def test_rollback_binds_journal_to_the_target_workspace_identity(tmp_path):
    root, registry = _workspace(tmp_path)
    archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    (root / "soul.md").write_text("changed soul\n", encoding="utf-8")
    restore_id = "restore-rollback-identity-01"
    restore_workspace(
        root,
        archive,
        registry=registry,
        confirm=True,
        restore_id=restore_id,
    )

    config = registry.config
    other_registry = WorkspaceStateRegistry(
        WorkspaceConfig(
            identity=WorkspaceIdentity("workspace-other", root),
            declared_paths=config.declared_paths,
            database_path=config.database_path,
            workspace_version=config.workspace_version,
            external_references=config.external_references,
            expected_database_objects=config.expected_database_objects,
        )
    )
    with pytest.raises(WorkspaceLifecycleError, match="workspace identity"):
        rollback_workspace(root, restore_id, registry=other_registry)
    assert (root / "soul.md").read_text(encoding="utf-8") == "original soul\n"
    assert (workspace_backup_dir(root) / restore_id / "previous-workspace").is_dir()

    rollback = rollback_workspace(root, restore_id, registry=registry)
    assert rollback["status"] == "rolled_back"
    assert (root / "soul.md").read_text(encoding="utf-8") == "changed soul\n"


def test_recovery_rejects_legacy_v1_journal_before_moving_any_root(tmp_path):
    root, registry = _workspace(tmp_path)
    archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    restore_id = "restore-legacy-journal-01"
    with pytest.raises(InterruptedWorkspaceRestore):
        restore_workspace(
            root,
            archive,
            registry=registry,
            confirm=True,
            restore_id=restore_id,
            interrupt_after_active_move=True,
        )

    journal_path = workspace_backup_dir(root) / restore_id / "restore-journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["journal_version"] = 1
    journal.pop("journal_sha256")
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(WorkspaceLifecycleError, match="unsupported restore journal version"):
        recover_interrupted_restore(root, registry=registry)
    assert not root.exists()
    assert (workspace_backup_dir(root) / restore_id / "previous-workspace").is_dir()
    assert (workspace_restore_staging_dir(root) / restore_id).is_dir()


def test_oversized_journal_write_leaves_existing_record_unchanged(tmp_path, monkeypatch):
    root, registry = _workspace(tmp_path)
    archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    restore_id = "restore-journal-size-01"
    with pytest.raises(InterruptedWorkspaceRestore):
        restore_workspace(
            root,
            archive,
            registry=registry,
            confirm=True,
            restore_id=restore_id,
            interrupt_after_active_move=True,
        )

    journal_path = workspace_backup_dir(root) / restore_id / "restore-journal.json"
    original = journal_path.read_bytes()
    journal = json.loads(original)
    monkeypatch.setattr(workspace_lifecycle, "MAX_JOURNAL_BYTES", len(original) - 1)
    with pytest.raises(WorkspaceLifecycleError, match="bounded size"):
        workspace_lifecycle._write_journal(journal_path, journal)
    assert journal_path.read_bytes() == original
    assert not list(journal_path.parent.glob("*.tmp"))


def test_recovery_rejects_free_form_stage_receipt_data(tmp_path):
    root, registry = _workspace(tmp_path)
    archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    restore_id = "restore-receipt-shape-01"
    with pytest.raises(InterruptedWorkspaceRestore):
        restore_workspace(
            root,
            archive,
            registry=registry,
            confirm=True,
            restore_id=restore_id,
            interrupt_after_active_move=True,
        )

    journal_path = workspace_backup_dir(root) / restore_id / "restore-journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["stage_receipt"]["raw_output"] = "SECRET-SHOULD-NOT-PERSIST"
    workspace_lifecycle._refresh_journal_digest(journal)
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(WorkspaceLifecycleError, match="unsupported fields"):
        recover_interrupted_restore(root, registry=registry)
    assert not root.exists()
    assert (workspace_backup_dir(root) / restore_id / "previous-workspace").is_dir()
    assert (workspace_restore_staging_dir(root) / restore_id).is_dir()


def test_recovery_rejects_secret_like_stage_receipt_identifier(tmp_path):
    root, registry = _workspace(tmp_path)
    archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    restore_id = "restore-receipt-secret-01"
    with pytest.raises(InterruptedWorkspaceRestore):
        restore_workspace(
            root,
            archive,
            registry=registry,
            confirm=True,
            restore_id=restore_id,
            interrupt_after_active_move=True,
        )

    journal_path = workspace_backup_dir(root) / restore_id / "restore-journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["stage_receipt"]["restore_reconciliation"] = {
        "status": "ready",
        "derived_rebuild": {
            "status": "clean_targets_recreated",
            "rebuilt_directories": [],
            "stored_derived_files": 0,
        },
        "authority_invalidation": {
            "status": "applied",
            "tables_present": ["SECRET-SENTINEL"],
            "operator_sessions_invalidated": 0,
            "workflow_authority_rows_blocked": 0,
        },
        "token_invalidation": {
            "status": "applied",
            "optional_credentials_invalidated": [],
        },
        "secret_values_included": False,
    }
    workspace_lifecycle._refresh_journal_digest(journal)
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(WorkspaceLifecycleError, match="secret-like"):
        recover_interrupted_restore(root, registry=registry)
    assert not root.exists()
    assert (workspace_backup_dir(root) / restore_id / "previous-workspace").is_dir()
    assert (workspace_restore_staging_dir(root) / restore_id).is_dir()


def test_recovery_discards_empty_unjournaled_record_without_moving_root(tmp_path):
    root, registry = _workspace(tmp_path)
    restore_id = "restore-unjournaled-empty-01"
    record_root = workspace_backup_dir(root) / restore_id
    stage = workspace_restore_staging_dir(root) / restore_id
    record_root.mkdir(parents=True)
    stage.mkdir(parents=True)

    recovery = recover_interrupted_restore(root, registry=registry)

    assert recovery == {
        "status": "recovered",
        "recovered": [
            {"restore_id": restore_id, "action": "discarded_unjournaled_record"}
        ],
    }
    assert root.is_dir()
    assert (root / "soul.md").read_text(encoding="utf-8") == "original soul\n"
    assert not record_root.exists()
    assert not stage.exists()


def test_recovery_keeps_evidence_for_unjournaled_moved_root(tmp_path):
    root, registry = _workspace(tmp_path)
    restore_id = "restore-unjournaled-moved-01"
    record_root = workspace_backup_dir(root) / restore_id
    previous = record_root / "previous-workspace"
    record_root.mkdir(parents=True)
    previous.mkdir()
    (previous / "soul.md").write_text("previous\n", encoding="utf-8")
    shutil.rmtree(root)

    with pytest.raises(WorkspaceLifecycleError, match="manual recovery"):
        recover_interrupted_restore(root, registry=registry)
    assert not root.exists()
    assert previous.is_dir()
    assert record_root.is_dir()


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


def test_restore_allows_missing_optional_secret_and_preserves_degraded_state(tmp_path):
    root, base_registry = _workspace(tmp_path)
    base_config = base_registry.config
    optional_path = "optional-token.json"
    (root / optional_path).write_text("optional-calendar-token", encoding="utf-8")
    registry = WorkspaceStateRegistry(
        WorkspaceConfig(
            identity=base_config.identity,
            declared_paths=(
                *base_config.declared_paths,
                WorkspacePathSpec(optional_path, WorkspaceStateClass.SECRET, required=False),
            ),
            database_path=base_config.database_path,
            workspace_version=base_config.workspace_version,
            external_references=base_config.external_references,
            expected_database_objects=base_config.expected_database_objects,
        )
    )
    backup = Path(backup_workspace(root, registry=registry)["archive_path"])
    with zipfile.ZipFile(backup, "r") as archive_reader:
        archive_manifest = json.loads(archive_reader.read("manifest.json"))
    optional_entry = next(
        entry
        for entry in archive_manifest["archive_entries"]
        if entry["logical_path"] == optional_path
    )
    assert optional_entry["required"] is False

    (root / "soul.md").write_text("changed while token is present\n", encoding="utf-8")
    restored_with_token = restore_workspace(
        root,
        backup,
        registry=registry,
        confirm=True,
        restore_id="restore-optional-secret-present-01",
    )
    assert restored_with_token["status"] == "restored"
    assert (root / optional_path).read_text(encoding="utf-8") == "optional-calendar-token"
    assert (root / "soul.md").read_text(encoding="utf-8") == "original soul\n"

    (root / optional_path).unlink()
    (root / "soul.md").write_text("changed while token is unavailable\n", encoding="utf-8")
    restore = restore_workspace(
        root,
        backup,
        registry=registry,
        confirm=True,
        restore_id="restore-optional-secret-01",
    )

    assert restore["status"] == "restored"
    assert not (root / optional_path).exists()
    assert (root / "soul.md").read_text(encoding="utf-8") == "original soul\n"


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
