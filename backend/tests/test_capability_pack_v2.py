"""Focused local-contract proof for the governed capability-pack v2 seam."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import io
import json
import multiprocessing
import os
from pathlib import Path
import stat
import tarfile
import zipfile

import pytest

from src.extensions.capability_pack import (
    CAPABILITY_PACK_RUNTIME_BLOCKED_REASON,
    CapabilityPackLifecycle,
    CapabilityPackLifecycleError,
    CapabilityPackManifestError,
    authority_delta,
    capability_pack_digest,
    migrate_capability_pack_v1,
    parse_capability_pack_manifest,
    publisher_trust_status,
    validate_capability_pack_archive,
    validate_capability_pack_package,
    validate_capability_pack_path,
)


def _manifest(
    *,
    pack_id: str = "seraph.research-pack",
    version: str = "1.0.0",
    network: bool = False,
    cost: int = 0,
    egress: list[str] | None = None,
    extra_authority: list[str] | None = None,
    approval: str = "always",
) -> str:
    egress = egress if egress is not None else (["cloud_openrouter"] if network else [])
    tools = ["read_file", *(extra_authority or [])]
    return f"""schema_version: 2
id: {pack_id}
version: {version}
kind: capability-pack
publisher:
  name: Seraph
  provenance: local-reviewed
signature:
  state: unsigned-local
  signer: null
compatibility:
  seraph: ">=1"
dependencies: []
contributes:
  capabilities: [research-brief]
  skills: []
  workflows: []
  prompts: []
  sources: []
  reports: []
  evals: []
  runbooks: []
authority:
  tools: {tools}
  filesystem: [workspace_read]
  network: {str(network).lower()}
  secrets: []
  approval: {approval}
resources:
  inference_priority: background
  max_inference_cost_microusd: {cost}
  max_runtime_seconds: 300
  max_artifact_bytes: 10485760
data_policy:
  classes: [public]
  egress: {egress}
policy_overlays: []
lifecycle:
  hooks:
    activate: required
    pause: required
    update: required
    revoke: required
    uninstall: required
  artifact_migration: preserve
  revoke_running_jobs: cancel_at_safe_checkpoint
"""


def _package(tmp_path: Path, *, manifest_text: str | None = None, extra_file: str | None = None) -> tuple[Path, object]:
    root = tmp_path / "pack"
    root.mkdir(parents=True)
    text = manifest_text or _manifest()
    (root / "manifest.yaml").write_text(text, encoding="utf-8")
    if extra_file:
        (root / "notes").mkdir()
        (root / "notes" / "brief.md").write_text(extra_file, encoding="utf-8")
    return root, parse_capability_pack_manifest(text)


def _approve(
    store: CapabilityPackLifecycle,
    pack,
    review: dict,
    *,
    action: str,
    goal_id: str,
    current_digest: str | None = None,
    delta: dict | None = None,
) -> str:
    return store.create_operator_approval(
        pack.id,
        action=action,
        goal_id=goal_id,
        digest=review["digest"],
        version=review["version"],
        current_digest=current_digest,
        authority_delta_payload=delta,
    )["approval"]["approval_id"]


def _activate_in_process(request: tuple[str, dict, str, str, str, str]) -> str:
    state_path, manifest, root_path, goal_id, review_id, approval_id = request
    store = CapabilityPackLifecycle(state_path)
    try:
        return store.activate(
            manifest,
            root_path=root_path,
            goal_id=goal_id,
            review_id=review_id,
            approval_id=approval_id,
        )["status"]
    except CapabilityPackLifecycleError:
        return "rejected"


def test_v2_schema_normalizes_priority_and_rejects_unsafe_limits():
    pack = parse_capability_pack_manifest(_manifest())

    assert pack.schema_version == 2
    assert pack.inference_priority == "screenshot_background"
    assert pack.resources.max_inference_cost_microusd == 0
    assert pack.lifecycle.hooks["activate"] == "required"

    with pytest.raises(CapabilityPackManifestError, match="greater than or equal to 0"):
        parse_capability_pack_manifest(_manifest().replace("max_inference_cost_microusd: 0", "max_inference_cost_microusd: -1"))
    with pytest.raises(CapabilityPackManifestError, match="executable hooks"):
        parse_capability_pack_manifest(_manifest().replace("activate: required", "activate: python:run"))
    with pytest.raises(CapabilityPackManifestError, match="gpu_class"):
        parse_capability_pack_manifest(_manifest() + "\n# v2 mixed field\nresources:\n  gpu_class: gpu\n")

    with pytest.raises(CapabilityPackManifestError, match="inline secret"):
        parse_capability_pack_manifest(_manifest().replace("secrets: []", "secrets: [sk-live-inline]"))
    with pytest.raises(CapabilityPackManifestError, match="named allow-list"):
        parse_capability_pack_manifest(_manifest().replace("egress: []", "egress: [https://evil.example]"))
    with pytest.raises(CapabilityPackManifestError, match="named allow-list"):
        parse_capability_pack_manifest(_manifest().replace("egress: []", "egress: [ftp://evil.example]"))
    with pytest.raises(CapabilityPackManifestError, match="boolean"):
        parse_capability_pack_manifest(_manifest().replace("network: false", 'network: "false"'))
    with pytest.raises(CapabilityPackManifestError, match="less than or equal"):
        parse_capability_pack_manifest(_manifest().replace("max_inference_cost_microusd: 0", "max_inference_cost_microusd: 1000000001"))
    with pytest.raises(CapabilityPackManifestError, match="privileged process"):
        parse_capability_pack_manifest(_manifest().replace("tools: ['read_file']", "tools: [shell]"))
    with pytest.raises(CapabilityPackManifestError, match="bounded workspace/artifact scope"):
        parse_capability_pack_manifest(_manifest().replace("filesystem: [workspace_read]", "filesystem: [host_root]"))

    for tool in (
        "shell_execute",
        "start_process",
        "list_processes",
        "read_process_output",
        "stop_process",
        "process_manager",
        "native.process.start",
    ):
        with pytest.raises(CapabilityPackManifestError, match="privileged process"):
            parse_capability_pack_manifest(_manifest().replace("tools: ['read_file']", f"tools: ['{tool}']"))


def test_v1_parser_and_migration_are_explicit_and_keyless():
    payload = {
        "schema_version": 1,
        "id": "seraph.legacy-pack",
        "version": "1.0.0",
        "publisher": {"name": "Legacy", "provenance": "local-reviewed"},
        "compatibility": {"seraph": ">=1"},
        "resources": {"gpu_class": "background"},
        "contributes": {"capabilities": ["research-brief"]},
    }

    with pytest.raises(CapabilityPackManifestError, match="explicit v1 migration"):
        parse_capability_pack_manifest(payload)
    migration = migrate_capability_pack_v1(payload)
    migrated = migration.payload
    assert migration.requires_review is True
    assert migrated["id"] == payload["id"]
    assert migrated["publisher"]["provenance"] == "local-reviewed"
    assert migrated["resources"]["inference_priority"] == "screenshot_background"
    assert migrated["resources"]["max_inference_cost_microusd"] == 0
    assert migrated["authority"]["network"] is False
    assert migrated["data_policy"]["egress"] == []
    assert parse_capability_pack_manifest(migrated).schema_version == 2

    with pytest.raises(CapabilityPackManifestError, match="unknown legacy gpu_class"):
        migrate_capability_pack_v1({**payload, "resources": {"gpu_class": "unbounded-gpu"}})
    with pytest.raises(CapabilityPackManifestError, match="mixes v2"):
        migrate_capability_pack_v1({**payload, "resources": {"gpu_class": "background", "inference_priority": "background"}})

    with pytest.raises(CapabilityPackManifestError, match="cannot depend on itself"):
        parse_capability_pack_manifest({**migrated, "dependencies": [{"id": migrated["id"], "digest": "0" * 64}]})


def test_archive_validation_rejects_traversal_links_and_oversized_members(tmp_path: Path):
    traversal = tmp_path / "traversal.zip"
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("manifest.yaml", _manifest())
        archive.writestr("../outside.txt", "no")
    report = validate_capability_pack_archive(traversal)
    assert report.ok is False
    assert any("traverses" in error or "relative" in error for error in report.errors)

    link_archive = tmp_path / "link.tar"
    with tarfile.open(link_archive, "w") as archive:
        manifest = tarfile.TarInfo("manifest.yaml")
        content = _manifest().encode()
        manifest.size = len(content)
        archive.addfile(manifest, io.BytesIO(content))
        link = tarfile.TarInfo("notes/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    link_report = validate_capability_pack_archive(link_archive)
    assert link_report.ok is False
    assert any("link" in error for error in link_report.errors)

    hook_archive = tmp_path / "hook.zip"
    with zipfile.ZipFile(hook_archive, "w") as archive:
        archive.writestr("manifest.yaml", _manifest())
        hook = zipfile.ZipInfo("hooks/run")
        hook.external_attr = (0o100755 << 16)
        archive.writestr(hook, "echo unsafe")
    hook_report = validate_capability_pack_archive(hook_archive)
    assert hook_report.ok is False
    assert any("hooks" in error for error in hook_report.errors)

    oversized = tmp_path / "oversized.zip"
    with zipfile.ZipFile(oversized, "w") as archive:
        archive.writestr("manifest.yaml", _manifest())
        archive.writestr("huge.bin", b"x" * 32)
    oversized_report = validate_capability_pack_archive(oversized, max_member_bytes=16)
    assert oversized_report.ok is False
    assert any("size limit" in error for error in oversized_report.errors)

    valid = tmp_path / "valid.zip"
    with zipfile.ZipFile(valid, "w") as archive:
        archive.writestr("manifest.yaml", _manifest())
    valid_report = validate_capability_pack_package(valid)
    assert valid_report["ok"] is True
    assert valid_report["manifest"]["schema_version"] == 2


def test_archive_directory_execute_bits_are_allowed_but_declared_directories_are_not(tmp_path: Path):
    directory_zip = tmp_path / "directory.zip"
    with zipfile.ZipFile(directory_zip, "w") as archive:
        archive.writestr("manifest.yaml", _manifest())
        directory = zipfile.ZipInfo("skills/")
        directory.external_attr = (stat.S_IFDIR | 0o755) << 16
        archive.writestr(directory, b"")
    directory_report = validate_capability_pack_archive(directory_zip)
    assert directory_report.ok is True
    assert "skills" in directory_report.members
    assert "skills" not in directory_report.regular_files

    directory_tar = tmp_path / "directory.tar"
    with tarfile.open(directory_tar, "w") as archive:
        manifest = tarfile.TarInfo("manifest.yaml")
        manifest_content = _manifest().encode()
        manifest.mode = 0o644
        manifest.size = len(manifest_content)
        archive.addfile(manifest, io.BytesIO(manifest_content))
        directory = tarfile.TarInfo("skills")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        archive.addfile(directory)
    tar_report = validate_capability_pack_archive(directory_tar)
    assert tar_report.ok is True
    assert "skills" in tar_report.members
    assert "skills" not in tar_report.regular_files

    declared_directory_zip = tmp_path / "declared-directory.zip"
    declared_manifest = _manifest().replace("  skills: []", "  skills: [skills/brief.md]", 1)
    with zipfile.ZipFile(declared_directory_zip, "w") as archive:
        archive.writestr("manifest.yaml", declared_manifest)
        directory = zipfile.ZipInfo("skills/")
        directory.external_attr = (stat.S_IFDIR | 0o755) << 16
        archive.writestr(directory, b"")
        contribution_directory = zipfile.ZipInfo("skills/brief.md/")
        contribution_directory.external_attr = (stat.S_IFDIR | 0o755) << 16
        archive.writestr(contribution_directory, b"")
    declared_report = validate_capability_pack_package(declared_directory_zip)
    assert declared_report["ok"] is False
    assert any("declared contribution file is not a regular file" in error for error in declared_report["errors"])


def test_package_path_and_digest_reject_symlinked_contributions(tmp_path: Path):
    root, pack = _package(tmp_path)
    (root / "skills").mkdir()
    (root / "skills" / "brief.md").symlink_to("/etc/passwd")
    changed_text = _manifest().replace("capabilities: [research-brief]", "capabilities: []")
    changed_text = changed_text.replace("  skills: []", "  skills: [skills/brief.md]", 1)
    changed = parse_capability_pack_manifest(changed_text)
    result = validate_capability_pack_path(root, changed)
    assert result["ok"] is False
    assert any("symlink" in error for error in result["errors"])
    with pytest.raises(ValueError, match="symlink"):
        capability_pack_digest(root)


def test_package_manifest_binding_is_checked_before_review(tmp_path: Path):
    root, pack = _package(tmp_path)
    different = parse_capability_pack_manifest(_manifest(version="2.0.0"))
    result = validate_capability_pack_path(root, different)
    assert result["ok"] is False
    assert "supplied manifest does not match" in " ".join(result["errors"])


def test_recomputed_fake_signature_never_becomes_publisher_trust(tmp_path: Path):
    root, pack = _package(tmp_path)
    digest = capability_pack_digest(root)
    reviewed = parse_capability_pack_manifest(
        {
            **pack.model_dump(mode="json"),
            "signature": {
                "state": "integrity-checked",
                "algorithm": "seraph-sha256-v1",
                "digest": digest,
            },
        }
    )
    trust = publisher_trust_status(reviewed, package_root=root)
    assert trust["integrity_checked"] is True
    assert trust["integrity_digest_match"] is True
    assert trust["publisher_verified"] is False
    assert "publisher label" in trust["reason"]
    metadata_only = publisher_trust_status(reviewed)
    assert metadata_only["integrity_checked"] is False
    assert metadata_only["integrity_digest_match"] is None

    mismatched = parse_capability_pack_manifest(
        {
            **pack.model_dump(mode="json"),
            "signature": {
                "state": "integrity-checked",
                "algorithm": "seraph-sha256-v1",
                "digest": "0" * 64,
            },
        }
    )
    mismatch_status = publisher_trust_status(mismatched, package_root=root)
    assert mismatch_status["integrity_checked"] is False
    assert mismatch_status["integrity_digest_match"] is False
    assert "does not match" in mismatch_status["reason"]
    with pytest.raises(CapabilityPackLifecycleError, match="does not match package content"):
        CapabilityPackLifecycle(tmp_path / "state.json").review(mismatched, root_path=root, goal_id="goal-1")

    unavailable = parse_capability_pack_manifest({**pack.model_dump(mode="json"), "signature": {"state": "cryptographic-unavailable"}})
    assert publisher_trust_status(unavailable)["integrity_checked"] is False


def test_review_binds_digest_version_goal_and_authority_delta(tmp_path: Path):
    root, first = _package(tmp_path, extra_file="first")
    store = CapabilityPackLifecycle(tmp_path / "state.json")
    review = store.review(first, root_path=root, goal_id="goal-1")["review"]
    with pytest.raises(CapabilityPackLifecycleError, match="durable operator approval"):
        store.activate(first, root_path=root, goal_id="goal-1", review_id=review["review_id"])
    activation_approval = _approve(store, first, review, action="activate", goal_id="goal-1")
    active = store.activate(first, root_path=root, goal_id="goal-1", review_id=review["review_id"], approval_id=activation_approval)
    assert active["pointer"]["digest"] == review["digest"]
    assert "root_path" not in active["pointer"]
    assert all("root_path" not in receipt.get("details", {}) for receipt in store.status(first.id)["receipts"])

    with pytest.raises(CapabilityPackLifecycleError, match="goal"):
        store.activate(first, root_path=root, goal_id="goal-2", review_id=review["review_id"])

    expanded = parse_capability_pack_manifest(_manifest(extra_authority=["write_file"]))
    assert authority_delta(first, expanded)["requires_approval"] is True
    expanded_root, _ = _package(tmp_path / "expanded", manifest_text=_manifest(extra_authority=["write_file"]))
    expanded_review = store.review(expanded, root_path=expanded_root, goal_id="goal-1")["review"]
    delta = authority_delta(first, expanded)
    with pytest.raises(CapabilityPackLifecycleError, match="durable operator approval"):
        store.update(expanded, root_path=expanded_root, goal_id="goal-1", review_id=expanded_review["review_id"])
    update_approval = _approve(store, expanded, expanded_review, action="update", goal_id="goal-1", current_digest=review["digest"], delta=delta)
    store.update(expanded, root_path=expanded_root, goal_id="goal-1", review_id=expanded_review["review_id"], approval_id=update_approval)
    assert store.status(first.id)["active"]["version"] == expanded.version


def test_initial_activation_binds_full_authority_delta(tmp_path: Path):
    root, pack = _package(tmp_path / "pack")
    store = CapabilityPackLifecycle(tmp_path / "state.json")
    review = store.review(pack, root_path=root, goal_id="goal-1")["review"]
    with pytest.raises(CapabilityPackLifecycleError, match="authority delta"):
        store.create_operator_approval(
            pack.id,
            action="activate",
            goal_id="goal-1",
            digest=review["digest"],
            version=review["version"],
            authority_delta_payload={},
        )
    approval = _approve(store, pack, review, action="activate", goal_id="goal-1")
    activated = store.activate(pack, root_path=root, goal_id="goal-1", review_id=review["review_id"], approval_id=approval)
    delta = activated["receipt"]["details"]["authority_delta"]
    assert delta["authority_digest_before"] is None
    assert delta["authority_digest_after"] == pack.authority_digest
    assert delta["added"]["tools"] == ["read_file"]
    assert delta["added"]["filesystem"] == ["workspace_read"]


def test_paused_update_preserves_pause_and_execution_requires_always_approval(tmp_path: Path):
    root, pack = _package(tmp_path / "first")
    store = CapabilityPackLifecycle(tmp_path / "state.json")
    review = store.review(pack, root_path=root, goal_id="goal-1")["review"]
    activation = _approve(store, pack, review, action="activate", goal_id="goal-1")
    store.activate(pack, root_path=root, goal_id="goal-1", review_id=review["review_id"], approval_id=activation)
    pause = _approve(store, pack, review, action="pause", goal_id="goal-1")
    store.pause(pack.id, approval_id=pause)

    updated_root, updated = _package(tmp_path / "updated", manifest_text=_manifest(version="2.0.0"))
    updated_review = store.review(updated, root_path=updated_root, goal_id="goal-1")["review"]
    update = _approve(
        store,
        updated,
        updated_review,
        action="update",
        goal_id="goal-1",
        current_digest=review["digest"],
        delta=authority_delta(pack, updated),
    )
    result = store.update(updated, root_path=updated_root, goal_id="goal-1", review_id=updated_review["review_id"], approval_id=update)
    assert result["pointer"]["status"] == "paused"

    restricted_root, restricted = _package(
        tmp_path / "restricted",
        manifest_text=_manifest(pack_id="seraph.restricted-pack", approval="on_authority_expansion"),
    )
    restricted_review = store.review(restricted, root_path=restricted_root, goal_id="goal-restricted")["review"]
    restricted_approval = _approve(store, restricted, restricted_review, action="activate", goal_id="goal-restricted")
    store.activate(restricted, root_path=restricted_root, goal_id="goal-restricted", review_id=restricted_review["review_id"], approval_id=restricted_approval)
    with pytest.raises(CapabilityPackLifecycleError, match="authority.approval: always"):
        store.build_execution_contract(restricted.id, goal_id="goal-restricted", job_id="restricted-job")
    with pytest.raises(CapabilityPackLifecycleError, match="authority.approval: always"):
        store.register_job(pack_id=restricted.id, goal_id="goal-restricted", job_id="restricted-job")


def test_concurrent_activation_has_one_pointer_and_atomic_write_failure_keeps_old(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    first_root, first = _package(tmp_path / "first")
    second_root, second = _package(tmp_path / "second", manifest_text=_manifest(version="2.0.0"))
    store = CapabilityPackLifecycle(tmp_path / "state.json")
    first_review = store.review(first, root_path=first_root, goal_id="goal-1")["review"]
    second_review = store.review(second, root_path=second_root, goal_id="goal-1")["review"]
    first_approval = _approve(store, first, first_review, action="activate", goal_id="goal-1")
    second_approval = _approve(store, second, second_review, action="activate", goal_id="goal-1")

    def activate(item):
        pack, root, review, approval = item
        try:
            return store.activate(pack, root_path=root, goal_id="goal-1", review_id=review["review_id"], approval_id=approval)["status"]
        except CapabilityPackLifecycleError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(activate, [(first, first_root, first_review, first_approval), (second, second_root, second_review, second_approval)]))
    assert sorted(outcomes) == ["active", "rejected"]
    assert store.status(first.id)["active"]["status"] == "active"

    before = store.status(first.id)["active"]
    pause_approval = _approve(store, first, first_review, action="pause", goal_id="goal-1")
    original_save = store._atomic_save
    monkeypatch.setattr(store, "_atomic_save", lambda _state: (_ for _ in ()).throw(OSError("simulated crash")))
    with pytest.raises(OSError, match="simulated crash"):
        store.pause(first.id, approval_id=pause_approval)
    assert store.status(first.id)["active"] == before
    monkeypatch.setattr(store, "_atomic_save", original_save)

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    state["active"][first.id]["digest"] = "0" * 64
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    assert store.status(first.id)["active"]["status"] == "invalid"
    with pytest.raises(CapabilityPackLifecycleError, match="binding is invalid"):
        store.run_secondary_canary(first.id, goal_id="goal-1")


@pytest.mark.skipif(
    os.name != "posix" or "fork" not in multiprocessing.get_all_start_methods(),
    reason="multiprocess lock proof requires POSIX fork and fcntl",
)
def test_multiprocess_activation_uses_durable_lock(tmp_path: Path):
    first_root, first = _package(tmp_path / "first")
    second_root, second = _package(tmp_path / "second", manifest_text=_manifest(version="2.0.0"))
    state_path = tmp_path / "state.json"
    store = CapabilityPackLifecycle(state_path)
    first_review = store.review(first, root_path=first_root, goal_id="goal-1")["review"]
    second_review = store.review(second, root_path=second_root, goal_id="goal-1")["review"]
    first_approval = _approve(store, first, first_review, action="activate", goal_id="goal-1")
    second_approval = _approve(store, second, second_review, action="activate", goal_id="goal-1")
    requests = [
        (str(state_path), first.model_dump(mode="json"), str(first_root), "goal-1", first_review["review_id"], first_approval),
        (str(state_path), second.model_dump(mode="json"), str(second_root), "goal-1", second_review["review_id"], second_approval),
    ]

    context = multiprocessing.get_context("fork")
    with context.Pool(2) as pool:
        outcomes = pool.map(_activate_in_process, requests)

    assert sorted(outcomes) == ["active", "rejected"]
    assert store.status(first.id)["active"]["status"] == "active"
    assert state_path.with_name("state.json.lock").stat().st_mode & 0o777 == 0o600


def test_revoke_rollback_uninstall_and_canaries_preserve_receipts(tmp_path: Path):
    first_root, first = _package(tmp_path / "first", extra_file="first")
    second_root, second = _package(tmp_path / "second", manifest_text=_manifest(version="2.0.0"), extra_file="second")
    remote_root, remote = _package(tmp_path / "remote", manifest_text=_manifest(pack_id="seraph.remote-pack", network=True, cost=25), extra_file="remote")
    store = CapabilityPackLifecycle(tmp_path / "state.json")
    first_review = store.review(first, root_path=first_root, goal_id="goal-1")["review"]
    first_approval = _approve(store, first, first_review, action="activate", goal_id="goal-1")
    store.activate(first, root_path=first_root, goal_id="goal-1", review_id=first_review["review_id"], approval_id=first_approval)
    second_review = store.review(second, root_path=second_root, goal_id="goal-1")["review"]
    update_approval = _approve(store, second, second_review, action="update", goal_id="goal-1", current_digest=first_review["digest"], delta=authority_delta(first, second))
    store.update(second, root_path=second_root, goal_id="goal-1", review_id=second_review["review_id"], approval_id=update_approval)

    revoke_approval = _approve(store, first, first_review, action="revoke", goal_id="goal-1")
    store.revoke(first.id, digest=first_review["digest"], approval_id=revoke_approval)
    with pytest.raises(CapabilityPackLifecycleError, match="revoked"):
        store.rollback(first.id)
    current = store.status(first.id)["active"]
    uninstall_review = {"digest": current["digest"], "version": current["version"]}
    uninstall_approval = _approve(store, first, uninstall_review, action="uninstall", goal_id="goal-1")
    uninstalled = store.uninstall(first.id, approval_id=uninstall_approval)
    assert uninstalled["status"] == "uninstalled"
    assert len(store.status(first.id)["receipts"]) >= 4

    remote_review = store.review(remote, root_path=remote_root, goal_id="goal-remote")["review"]
    remote_approval = _approve(store, remote, remote_review, action="activate", goal_id="goal-remote")
    store.activate(remote, root_path=remote_root, goal_id="goal-remote", review_id=remote_review["review_id"], approval_id=remote_approval)
    production = store.run_primary_canary(remote.id, goal_id="goal-remote", artifact_root=tmp_path / "artifacts")
    assert production["status"] == "blocked"
    primary = store.run_deterministic_primary_canary(remote.id, goal_id="goal-remote", artifact_root=tmp_path / "artifacts")
    secondary = store.run_deterministic_secondary_canary(remote.id, goal_id="goal-remote", artifact_root=tmp_path / "artifacts")
    assert primary["status"] == "succeeded"
    assert primary["provider_calls"] == 0
    assert primary["artifact"]["readback_ok"] is True
    assert secondary["status"] == "succeeded"
    assert secondary["memory"]["status"] == "no_learning"

    local_root, local = _package(tmp_path / "local")
    local_review = store.review(local, root_path=local_root, goal_id="goal-local")["review"]
    local_approval = _approve(store, local, local_review, action="activate", goal_id="goal-local")
    store.activate(local, root_path=local_root, goal_id="goal-local", review_id=local_review["review_id"], approval_id=local_approval)
    blocked = store.run_primary_canary(local.id, goal_id="goal-local")
    assert blocked["status"] == "blocked"
    assert blocked["provider_calls"] == 0


def test_canary_runner_is_local_and_artifact_result_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root, pack = _package(tmp_path / "pack")
    store = CapabilityPackLifecycle(tmp_path / "state.json")
    review = store.review(pack, root_path=root, goal_id="goal-1")["review"]
    activation_approval = _approve(store, pack, review, action="activate", goal_id="goal-1")
    store.activate(pack, root_path=root, goal_id="goal-1", review_id=review["review_id"], approval_id=activation_approval)

    def fail_transport(*_args, **_kwargs):
        raise AssertionError("capability-pack canary attempted provider transport")

    import httpx

    monkeypatch.setattr(httpx, "request", fail_transport)
    canary = store.run_deterministic_secondary_canary(pack.id, goal_id="goal-1", artifact_root=tmp_path / "artifacts")
    assert canary["provider_calls"] == 0
    artifact = next((tmp_path / "artifacts").glob("*.json"))
    assert "sk-live-inline" not in artifact.read_text(encoding="utf-8")
    assert json.loads(artifact.read_text(encoding="utf-8"))["result"] == {
        "outcome": "deterministic_fixture_passed",
        "readback": True,
        "sources": [],
    }


def test_compatibility_dependency_and_egress_contradictions_fail_closed(tmp_path: Path):
    incompatible = _manifest().replace('seraph: ">=1"', 'seraph: ">=9999"')
    root, pack = _package(tmp_path / "incompatible", manifest_text=incompatible)
    store = CapabilityPackLifecycle(tmp_path / "state.json")
    with pytest.raises(CapabilityPackLifecycleError, match="excludes Seraph"):
        store.review(pack, root_path=root, goal_id="goal-1")

    with pytest.raises(CapabilityPackManifestError, match="Field required"):
        parse_capability_pack_manifest(_manifest().replace("dependencies: []", "dependencies: [{id: seraph.other}]") )
    with pytest.raises(CapabilityPackManifestError, match="requires authority.network"):
        parse_capability_pack_manifest(_manifest().replace("egress: []", "egress: [cloud_openrouter]"))


def test_bounded_scanner_rejects_missing_declared_files_and_executables(tmp_path: Path):
    root, _ = _package(tmp_path / "directory")
    executable = root / "notes.sh"
    executable.write_text("echo unsafe", encoding="utf-8")
    executable.chmod(0o700)
    report = validate_capability_pack_path(root)
    assert report["ok"] is False
    assert any("executable" in error for error in report["errors"])

    missing_text = _manifest().replace("capabilities: [research-brief]", "capabilities: []").replace("  skills: []", "  skills: [skills/missing.md]", 1)
    missing = parse_capability_pack_manifest(missing_text)
    missing_report = validate_capability_pack_path(root, missing)
    assert missing_report["ok"] is False
    assert any("missing" in error for error in missing_report["errors"])

    archive = tmp_path / "missing.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("manifest.yaml", missing_text)
    archive_report = validate_capability_pack_package(archive)
    assert archive_report["ok"] is False
    assert any("missing" in error for error in archive_report["errors"])


def test_operator_approval_binds_rollback_delta_and_jobs_cancel(tmp_path: Path):
    first_root, first = _package(tmp_path / "first")
    second_root, second = _package(tmp_path / "second", manifest_text=_manifest(version="2.0.0", extra_authority=["write_file"]))
    store = CapabilityPackLifecycle(tmp_path / "state.json")
    first_review = store.review(first, root_path=first_root, goal_id="goal-1")["review"]
    first_approval = _approve(store, first, first_review, action="activate", goal_id="goal-1")
    store.activate(first, root_path=first_root, goal_id="goal-1", review_id=first_review["review_id"], approval_id=first_approval)
    job = store.register_job(pack_id=first.id, goal_id="goal-1", job_id="job-1")
    assert job["job"]["max_artifact_bytes"] == first.resources.max_artifact_bytes
    assert job["job"]["max_inference_cost_microusd"] == first.resources.max_inference_cost_microusd
    second_review = store.review(second, root_path=second_root, goal_id="goal-1")["review"]
    update_approval = _approve(store, second, second_review, action="update", goal_id="goal-1", current_digest=first_review["digest"], delta=authority_delta(first, second))
    store.update(second, root_path=second_root, goal_id="goal-1", review_id=second_review["review_id"], approval_id=update_approval)

    rollback_delta = authority_delta(second, first)
    with pytest.raises(CapabilityPackLifecycleError, match="durable operator approval"):
        store.rollback(first.id)
    rollback_approval = _approve(store, first, first_review, action="rollback", goal_id="goal-1", current_digest=second_review["digest"], delta=rollback_delta)
    restored = store.rollback(first.id, approval_id=rollback_approval)
    assert restored["pointer"]["digest"] == first_review["digest"]

    pause_approval = _approve(store, first, first_review, action="pause", goal_id="goal-1")
    store.pause(first.id, approval_id=pause_approval)
    assert store.status(first.id)["jobs"][0]["status"] == "cancelled"
    uninstall_approval = _approve(store, first, first_review, action="uninstall", goal_id="goal-1")
    assert store.uninstall(first.id, approval_id=uninstall_approval)["status"] == "uninstalled"


def test_rollback_revalidates_target_package_inside_transaction(tmp_path: Path):
    first_root, first = _package(tmp_path / "first", extra_file="first")
    second_root, second = _package(tmp_path / "second", manifest_text=_manifest(version="2.0.0"), extra_file="second")
    store = CapabilityPackLifecycle(tmp_path / "state.json")
    first_review = store.review(first, root_path=first_root, goal_id="goal-1")["review"]
    first_approval = _approve(store, first, first_review, action="activate", goal_id="goal-1")
    store.activate(first, root_path=first_root, goal_id="goal-1", review_id=first_review["review_id"], approval_id=first_approval)
    second_review = store.review(second, root_path=second_root, goal_id="goal-1")["review"]
    update_approval = _approve(
        store,
        second,
        second_review,
        action="update",
        goal_id="goal-1",
        current_digest=first_review["digest"],
        delta=authority_delta(first, second),
    )
    store.update(second, root_path=second_root, goal_id="goal-1", review_id=second_review["review_id"], approval_id=update_approval)

    (first_root / "notes" / "brief.md").write_text("tampered", encoding="utf-8")
    rollback_approval = _approve(
        store,
        first,
        first_review,
        action="rollback",
        goal_id="goal-1",
        current_digest=second_review["digest"],
        delta=authority_delta(second, first),
    )
    with pytest.raises(CapabilityPackLifecycleError, match="content digest"):
        store.rollback(first.id, approval_id=rollback_approval)
    assert store.status(first.id)["active"]["digest"] == second_review["digest"]


def test_pointer_binding_recomputes_canonical_authority_and_resources(tmp_path: Path):
    root, pack = _package(tmp_path / "pack")
    state_path = tmp_path / "state.json"
    store = CapabilityPackLifecycle(state_path)
    review = store.review(pack, root_path=root, goal_id="goal-1")["review"]
    approval = _approve(store, pack, review, action="activate", goal_id="goal-1")
    store.activate(pack, root_path=root, goal_id="goal-1", review_id=review["review_id"], approval_id=approval)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    record = state["versions"][pack.id][review["digest"]]
    record["authority"]["tools"] = ["read_file", "write_file"]
    record["resources"]["max_artifact_bytes"] += 1
    state_path.write_text(json.dumps(state), encoding="utf-8")

    assert store.status(pack.id)["active"]["status"] == "invalid"
    with pytest.raises(CapabilityPackLifecycleError, match="binding is invalid"):
        store.build_execution_contract(pack.id, goal_id="goal-1", job_id="job-1")


def test_production_canary_is_blocked_and_retry_ids_are_unique(tmp_path: Path):
    root, pack = _package(tmp_path / "pack")
    store = CapabilityPackLifecycle(tmp_path / "state.json")
    review = store.review(pack, root_path=root, goal_id="goal-1")["review"]
    approval = _approve(store, pack, review, action="activate", goal_id="goal-1")
    store.activate(pack, root_path=root, goal_id="goal-1", review_id=review["review_id"], approval_id=approval)
    first = store.run_secondary_canary(pack.id, goal_id="goal-1")
    second = store.run_secondary_canary(pack.id, goal_id="goal-1")
    assert first["status"] == second["status"] == "blocked"
    assert first["failure_reason"] == CAPABILITY_PACK_RUNTIME_BLOCKED_REASON
    assert first["id"] != second["id"]
    assert [first["attempt"], second["attempt"]] == [1, 2]
