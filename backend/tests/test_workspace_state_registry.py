import json
import sqlite3
from pathlib import Path

import pytest

from config.settings import settings
from src.artifacts.registry import build_artifact_record
from src.workspace import (
    AmbiguousWorkspaceRootsError,
    ExternalReferencePolicy,
    ExternalReferenceSpec,
    UnknownWorkspacePathError,
    UnsupportedWorkspaceEntryError,
    WorkspaceConfig,
    WorkspaceDatabaseObjectSpec,
    WorkspaceIdentity,
    WorkspacePathSpec,
    WorkspaceRootKind,
    WorkspaceStateClass,
    WorkspaceStateError,
    WorkspaceStateRegistry,
    canonical_workspace_database_path,
    canonical_workspace_registry,
    production_workspace_inventory,
)


def _config(root: Path, *, expected_objects=None) -> WorkspaceConfig:
    return WorkspaceConfig(
        identity=WorkspaceIdentity("synthetic-test", root),
        declared_paths=(
            WorkspacePathSpec("seraph.db", WorkspaceStateClass.CANONICAL),
            WorkspacePathSpec("soul.md", WorkspaceStateClass.CANONICAL),
            WorkspacePathSpec("extensions", WorkspaceStateClass.CANONICAL),
            WorkspacePathSpec(".vault-key", WorkspaceStateClass.SECRET_RECOVERY),
            WorkspacePathSpec("derived", WorkspaceStateClass.DERIVED),
            WorkspacePathSpec("cache", WorkspaceStateClass.CACHE),
            WorkspacePathSpec("tmp", WorkspaceStateClass.DISPOSABLE),
            WorkspacePathSpec(".seraph-synthetic-workspace", WorkspaceStateClass.DISPOSABLE),
        ),
        external_references=(
            ExternalReferenceSpec("screen-capture", ExternalReferencePolicy.CONSENTED_LOGICAL_ROOT),
            ExternalReferenceSpec("operator-input", ExternalReferencePolicy.PAIRED_EDGE_REFERENCE),
        ),
        expected_database_objects=tuple(
            expected_objects
            or (
                WorkspaceDatabaseObjectSpec("messages", "table", WorkspaceStateClass.CANONICAL),
                WorkspaceDatabaseObjectSpec("sessions", "table", WorkspaceStateClass.CANONICAL),
                WorkspaceDatabaseObjectSpec("ix_messages_session_id", "index", WorkspaceStateClass.DERIVED),
            )
        ),
    )


def _make_workspace(tmp_path: Path) -> tuple[Path, WorkspaceConfig]:
    root = tmp_path / "synthetic"
    root.mkdir()
    (root / ".seraph-synthetic-workspace").write_bytes(b"seraph-synthetic-workspace-v1\n")
    (root / "soul.md").write_text("# Synthetic operator\n", encoding="utf-8")
    (root / "extensions").mkdir()
    (root / "extensions" / "readme.txt").write_text("bounded", encoding="utf-8")
    (root / ".vault-key").write_text("SUPER-SECRET-KEY", encoding="utf-8")
    (root / "derived").mkdir()
    (root / "derived" / "fts.index").write_text("derived", encoding="utf-8")
    (root / "cache").mkdir()
    (root / "cache" / "screen.cache").write_text("cache", encoding="utf-8")
    (root / "tmp").mkdir()
    (root / "tmp" / "run.log").write_text("disposable", encoding="utf-8")
    with sqlite3.connect(root / "seraph.db") as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL);
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                role TEXT NOT NULL,
                content TEXT NOT NULL
            );
            CREATE INDEX ix_messages_session_id ON messages(session_id);
            INSERT INTO sessions(id, title) VALUES ('s1', 'first');
            INSERT INTO messages(id, session_id, role, content)
                VALUES ('m1', 's1', 'user', 'SENTINEL-DB-CONTENT');
            """
        )
    return root, _config(root)


def test_manifest_is_deterministic_redacted_and_explicit(tmp_path):
    root, config = _make_workspace(tmp_path)
    registry = WorkspaceStateRegistry(config)

    first = registry.build_manifest()
    second = registry.build_manifest()
    encoded = registry.build_manifest_json()

    assert first == second
    assert encoded == json.dumps(first, sort_keys=True, separators=(",", ":"))
    assert str(root) not in encoded
    assert "SUPER-SECRET-KEY" not in encoded
    assert "SENTINEL-DB-CONTENT" not in encoded
    assert first["manifest_sha256"]
    assert first["workspace_version"] == 1
    assert first["database"]["table_count"] == 2
    assert first["database"]["row_count"] == 2
    assert first["database"]["schema_fingerprint"]
    assert {item["state_class"] for item in first["database"]["tables"]} == {"canonical"}
    assert first["counts"]["external_references"] == 2
    assert [item["reference_id"] for item in first["external_references"]] == [
        "operator-input",
        "screen-capture",
    ]
    database_entry = next(item for item in first["entries"] if item["logical_path"] == "seraph.db")
    secret_entry = next(item for item in first["entries"] if item["logical_path"] == ".vault-key")
    assert database_entry["digest_scope"] == "redacted_metadata"
    assert secret_entry["digest_scope"] == "redacted_metadata"


def test_schema_fingerprint_is_content_independent_but_detects_schema_change(tmp_path):
    root, config = _make_workspace(tmp_path)
    registry = WorkspaceStateRegistry(config)
    baseline = registry.build_manifest()["database"]

    with sqlite3.connect(root / "seraph.db") as connection:
        connection.execute("INSERT INTO messages(id, session_id, role, content) VALUES (?, ?, ?, ?)", ("m2", "s1", "assistant", "different"))
        connection.commit()
    rows_changed = registry.build_manifest()["database"]
    assert rows_changed["schema_fingerprint"] == baseline["schema_fingerprint"]
    assert rows_changed["row_count"] == baseline["row_count"] + 1

    with sqlite3.connect(root / "seraph.db") as connection:
        connection.execute("ALTER TABLE messages ADD COLUMN metadata TEXT")
        connection.commit()
    schema_changed = registry.build_manifest()["database"]
    assert schema_changed["schema_fingerprint"] != baseline["schema_fingerprint"]


def test_unknown_files_and_symlinks_fail_closed(tmp_path):
    root, config = _make_workspace(tmp_path)
    (root / "unexpected.txt").write_text("not declared", encoding="utf-8")
    with pytest.raises(UnknownWorkspacePathError, match="unknown"):
        WorkspaceStateRegistry(config).build_manifest()

    (root / "unexpected.txt").unlink()
    (root / "extensions" / "escape").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(UnsupportedWorkspaceEntryError, match="symlink"):
        WorkspaceStateRegistry(config).build_manifest()


def test_root_and_declared_path_safety(tmp_path):
    root, config = _make_workspace(tmp_path)
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(root, target_is_directory=True)
    linked_config = _config(linked_root)
    with pytest.raises(UnsupportedWorkspaceEntryError):
        WorkspaceStateRegistry(linked_config).build_manifest()

    for unsafe in ("../outside", "/tmp/outside", "C:/outside", "~/outside", "file:///outside", "bad\x00path"):
        with pytest.raises(WorkspaceStateError):
            WorkspacePathSpec(unsafe, WorkspaceStateClass.CANONICAL)
    with pytest.raises(WorkspaceStateError):
        ExternalReferenceSpec("capture", "/host/private/screens")
    with pytest.raises(WorkspaceStateError):
        ExternalReferenceSpec("capture", "SENTINEL-SECRET-POLICY")
    with pytest.raises(WorkspaceStateError):
        ExternalReferenceSpec("secret-key", ExternalReferencePolicy.CONSENTED_LOGICAL_ROOT)
    with pytest.raises(WorkspaceStateError):
        WorkspacePathSpec("external", WorkspaceStateClass.EXTERNAL_REFERENCE)
    with pytest.raises(WorkspaceStateError):
        WorkspaceIdentity("synthetic-secret-key", root)


def test_database_is_read_only_and_missing_database_is_not_created(tmp_path):
    root, config = _make_workspace(tmp_path)
    database = root / "seraph.db"
    database.unlink()
    with pytest.raises(WorkspaceStateError, match="missing"):
        WorkspaceStateRegistry(config).build_manifest()
    assert not database.exists()


def test_configuration_rejects_ambiguous_roots_and_unknown_database_objects(tmp_path):
    root, _ = _make_workspace(tmp_path)
    with pytest.raises(AmbiguousWorkspaceRootsError):
        WorkspaceConfig(
            identity=WorkspaceIdentity("synthetic-test", root),
            declared_paths=(
                WorkspacePathSpec("seraph.db", WorkspaceStateClass.CANONICAL),
                WorkspacePathSpec("seraph.db", WorkspaceStateClass.DERIVED),
            ),
        )

    config = _config(
        root,
        expected_objects=(
            WorkspaceDatabaseObjectSpec("messages", "table", WorkspaceStateClass.CANONICAL),
            WorkspaceDatabaseObjectSpec("sessions", "table", WorkspaceStateClass.CANONICAL),
        ),
    )
    with pytest.raises(UnknownWorkspacePathError, match="SQLite"):
        WorkspaceStateRegistry(config).build_manifest()

    type_mismatch = _config(
        root,
        expected_objects=(
            WorkspaceDatabaseObjectSpec("messages", "view", WorkspaceStateClass.CANONICAL),
            WorkspaceDatabaseObjectSpec("sessions", "table", WorkspaceStateClass.CANONICAL),
            WorkspaceDatabaseObjectSpec("ix_messages_session_id", "index", WorkspaceStateClass.DERIVED),
        ),
    )
    with pytest.raises(WorkspaceStateError, match="type mismatch"):
        WorkspaceStateRegistry(type_mismatch).build_manifest()


def test_inventory_does_not_use_global_settings_or_materialize_external_roots(tmp_path, monkeypatch):
    root, config = _make_workspace(tmp_path)
    sentinel = tmp_path / "global-settings-must-not-change"
    monkeypatch.setenv("WORKSPACE_DIR", str(sentinel))
    manifest = WorkspaceStateRegistry(config).build_manifest()
    assert not sentinel.exists()
    assert "screen-capture" in {item["reference_id"] for item in manifest["external_references"]}


def test_only_marked_synthetic_roots_are_read(tmp_path):
    root = tmp_path / "unmarked"
    root.mkdir()
    (root / "secret.txt").write_text("must not be read", encoding="utf-8")
    config = _config(root)
    with pytest.raises(WorkspaceStateError, match="synthetic workspace marker"):
        WorkspaceStateRegistry(config).build_manifest()

    production_config = WorkspaceConfig(
        identity=WorkspaceIdentity("workspace-primary", root, WorkspaceRootKind.PRODUCTION),
        declared_paths=(WorkspacePathSpec("seraph.db", WorkspaceStateClass.CANONICAL),),
    )
    with pytest.raises(UnknownWorkspacePathError, match="unknown"):
        WorkspaceStateRegistry(production_config).build_manifest()


def test_more_link_and_path_forms_fail_closed(tmp_path):
    root, config = _make_workspace(tmp_path)
    (root / "linked-file").symlink_to(root / "soul.md")
    with pytest.raises(UnsupportedWorkspaceEntryError, match="symlink"):
        WorkspaceStateRegistry(config).build_manifest()

    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(root, target_is_directory=True)
    with pytest.raises(UnsupportedWorkspaceEntryError):
        WorkspaceStateRegistry(_config(linked_parent)).build_manifest()

    for unsafe in ("//server/share", "\\\\server\\share", "file://server/share"):
        with pytest.raises(WorkspaceStateError):
            WorkspacePathSpec(unsafe, WorkspaceStateClass.CANONICAL)


def test_runtime_registry_binds_existing_persistence_roots_without_scanning(tmp_path):
    registry = canonical_workspace_registry(tmp_path)

    assert canonical_workspace_database_path(tmp_path) == tmp_path / "seraph.db"
    assert registry.classify_path("seraph.db") is WorkspaceStateClass.CANONICAL
    assert registry.classify_path("artifacts/reports/daily.md") is WorkspaceStateClass.CANONICAL
    assert registry.classify_path("lance/memories") is WorkspaceStateClass.DERIVED
    assert registry.classify_path(".vault-key") is WorkspaceStateClass.SECRET_RECOVERY
    with pytest.raises(UnknownWorkspacePathError):
        registry.classify_path("backup-archive.tar")


def test_artifact_persistence_consumes_runtime_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))

    record = build_artifact_record(
        file_path="artifacts/reports/progress.md",
        producer="workflow",
        content="bounded report",
    )

    assert record["workspace_state_class"] == WorkspaceStateClass.CANONICAL.value
    assert record["workspace_state_status"] == "classified"


def _production_config(root: Path) -> WorkspaceConfig:
    return WorkspaceConfig(
        identity=WorkspaceIdentity("workspace-primary", root, WorkspaceRootKind.PRODUCTION),
        declared_paths=(
            WorkspacePathSpec("seraph.db", WorkspaceStateClass.CANONICAL),
            WorkspacePathSpec("artifacts", WorkspaceStateClass.CANONICAL),
            WorkspacePathSpec("derived", WorkspaceStateClass.DERIVED),
            WorkspacePathSpec("cache", WorkspaceStateClass.CACHE),
            WorkspacePathSpec("secret.bin", WorkspaceStateClass.SECRET),
            WorkspacePathSpec("tmp", WorkspaceStateClass.DISPOSABLE),
        ),
    )


def _make_production_workspace(tmp_path: Path) -> tuple[Path, WorkspaceConfig]:
    root = tmp_path / "production"
    root.mkdir()
    (root / "artifacts").mkdir()
    (root / "artifacts" / "report.md").write_text("canonical report", encoding="utf-8")
    (root / "derived").mkdir()
    (root / "derived" / "vectors.idx").write_text("rebuildable", encoding="utf-8")
    (root / "cache").mkdir()
    (root / "cache" / "screen.cache").write_text("cache", encoding="utf-8")
    (root / "secret.bin").write_text("PRODUCTION-SECRET-SENTINEL", encoding="utf-8")
    (root / "tmp").mkdir()
    with sqlite3.connect(root / "seraph.db") as connection:
        connection.executescript(
            """
            CREATE TABLE records (
                id TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT 'DATABASE-DEFAULT-SECRET-SENTINEL'
            );
            CREATE INDEX ix_records_value ON records(value);
            INSERT INTO records(id, value) VALUES ('r1', 'DATABASE-SECRET-SENTINEL');
            """
        )
    return root, _production_config(root)


def test_production_inventory_is_explicit_redacted_and_deterministic(tmp_path):
    root, config = _make_production_workspace(tmp_path)
    registry = WorkspaceStateRegistry(config)

    first = registry.build_manifest()
    second = registry.build_manifest()
    receipt = registry.build_inventory_receipt()
    encoded = registry.build_manifest_json()

    assert first == second
    assert encoded == json.dumps(first, sort_keys=True, separators=(",", ":"))
    assert first["root_kind"] == WorkspaceRootKind.PRODUCTION.value
    assert first["inventory"]["status"] == "ready"
    assert first["inventory"]["missing_declared_paths"] == []
    assert first["inventory"]["state_roles"][WorkspaceStateClass.CANONICAL.value] == "canonical_payload"
    assert first["inventory"]["state_roles"][WorkspaceStateClass.DERIVED.value] == "rebuildable"
    assert first["inventory"]["state_roles"][WorkspaceStateClass.CACHE.value] == "cache"
    assert first["inventory"]["state_roles"][WorkspaceStateClass.SECRET.value] == "secret"
    assert str(root) not in encoded
    assert "PRODUCTION-SECRET-SENTINEL" not in encoded
    assert "DATABASE-SECRET-SENTINEL" not in encoded
    assert "DATABASE-DEFAULT-SECRET-SENTINEL" not in encoded
    secret_entry = next(item for item in first["entries"] if item["logical_path"] == "secret.bin")
    assert secret_entry["state_class"] == WorkspaceStateClass.SECRET.value
    assert secret_entry["state_role"] == "secret"
    assert secret_entry["digest_scope"] == "redacted_metadata"
    assert receipt["status"] == "ready"
    assert receipt["operator_status"] == "workspace_inventory_ready"
    assert receipt["secret_values_included"] is False
    assert receipt["manifest_sha256"] == first["manifest_sha256"]


def test_production_inventory_reports_missing_declared_paths_as_degraded(tmp_path):
    root, config = _make_production_workspace(tmp_path)
    config = WorkspaceConfig(
        identity=config.identity,
        declared_paths=(*config.declared_paths, WorkspacePathSpec("optional", WorkspaceStateClass.DERIVED)),
    )

    manifest = WorkspaceStateRegistry(config).build_manifest()
    receipt = WorkspaceStateRegistry(config).build_inventory_receipt()

    assert manifest["inventory"] == {
        "status": "degraded",
        "missing_declared_paths": ["optional"],
        "state_roles": manifest["inventory"]["state_roles"],
    }
    assert receipt["status"] == "degraded"
    assert receipt["operator_status"] == "workspace_inventory_degraded"
    assert receipt["missing_declared_paths"] == ["optional"]
    assert receipt["degraded_reasons"] == ["declared_paths_missing"]
    assert receipt["blocked_reasons"] == []


def test_production_inventory_blocks_missing_secret_declarations(tmp_path):
    root, config = _make_production_workspace(tmp_path)
    (root / "secret.bin").unlink()
    registry = WorkspaceStateRegistry(config)

    with pytest.raises(WorkspaceStateError, match="required secret workspace path"):
        registry.build_manifest()
    receipt = registry.build_inventory_receipt()

    assert receipt["status"] == "blocked"
    assert receipt["operator_status"] == "workspace_inventory_blocked"
    assert receipt["blocked_reasons"] == ["required_secret_missing"]
    assert receipt["manifest"] is None
    assert receipt["missing_declared_paths"] == []


def test_production_inventory_blocks_unknown_entries_and_symlink_escape(tmp_path):
    root, config = _make_production_workspace(tmp_path)
    registry = WorkspaceStateRegistry(config)

    (root / "unexpected.txt").write_text("outside approved roots", encoding="utf-8")
    with pytest.raises(UnknownWorkspacePathError, match="unknown"):
        registry.build_manifest()
    blocked_unknown = registry.build_inventory_receipt()
    assert blocked_unknown["status"] == "blocked"
    assert blocked_unknown["operator_status"] == "workspace_inventory_blocked"
    assert blocked_unknown["blocked_reasons"] == ["unknown_workspace_path"]
    assert str(root) not in json.dumps(blocked_unknown, sort_keys=True)

    (root / "unexpected.txt").unlink()
    outside = tmp_path / "outside"
    outside.write_text("SYMLINK-SECRET-SENTINEL", encoding="utf-8")
    (root / "artifacts" / "escape").symlink_to(outside)
    with pytest.raises(UnsupportedWorkspaceEntryError, match="symlink"):
        registry.build_manifest()
    blocked_symlink = registry.build_inventory_receipt()
    assert blocked_symlink["status"] == "blocked"
    assert blocked_symlink["blocked_reasons"] == ["unsupported_or_symlink_entry"]
    assert "SYMLINK-SECRET-SENTINEL" not in json.dumps(blocked_symlink, sort_keys=True)


def test_production_inventory_requires_a_real_explicit_root(tmp_path):
    missing_root = tmp_path / "unknown-production-root"
    config = WorkspaceConfig(
        identity=WorkspaceIdentity("workspace-primary", missing_root, WorkspaceRootKind.PRODUCTION),
        declared_paths=(WorkspacePathSpec("seraph.db", WorkspaceStateClass.CANONICAL),),
    )
    receipt = WorkspaceStateRegistry(config).build_inventory_receipt()

    assert receipt["status"] == "blocked"
    assert receipt["blocked_reasons"] == ["workspace_root_invalid"]
    assert receipt["manifest"] is None
    assert str(missing_root) not in json.dumps(receipt, sort_keys=True)
    first_helper_receipt = production_workspace_inventory(missing_root)
    second_helper_receipt = production_workspace_inventory(missing_root)
    assert first_helper_receipt["status"] == "blocked"
    assert first_helper_receipt["blocked_reasons"] == ["workspace_root_invalid"]
    assert first_helper_receipt["receipt_id"] == second_helper_receipt["receipt_id"]

    linked_root = tmp_path / "linked-production-root"
    linked_root.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    linked_receipt = production_workspace_inventory(linked_root)
    assert linked_receipt["status"] == "blocked"
    assert linked_receipt["blocked_reasons"] == ["unsupported_or_symlink_entry"]
    assert str(linked_root) not in json.dumps(linked_receipt, sort_keys=True)
