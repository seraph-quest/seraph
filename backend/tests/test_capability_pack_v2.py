"""Focused local-contract proof for the governed capability-pack v2 seam."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import stat
import tarfile
import zipfile

import pytest

from src.extensions.capability_pack import (
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
  approval: on_authority_expansion
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
        parse_capability_pack_manifest({**migrated, "dependencies": [{"id": migrated["id"]}]})


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
    reviewed = parse_capability_pack_manifest(
        {
            **pack.model_dump(mode="json"),
            "signature": {
                "state": "integrity-checked",
                "algorithm": "seraph-sha256-v1",
                "digest": capability_pack_digest(root),
            },
        }
    )
    trust = publisher_trust_status(reviewed)
    assert trust["integrity_checked"] is True
    assert trust["publisher_verified"] is False
    assert "publisher label" in trust["reason"]


def test_review_binds_digest_version_goal_and_authority_delta(tmp_path: Path):
    root, first = _package(tmp_path, extra_file="first")
    store = CapabilityPackLifecycle(tmp_path / "state.json")
    review = store.review(first, root_path=root, goal_id="goal-1")["review"]
    active = store.activate(first, root_path=root, goal_id="goal-1", review_id=review["review_id"])
    assert active["pointer"]["digest"] == review["digest"]
    assert "root_path" not in active["pointer"]
    assert all("root_path" not in receipt.get("details", {}) for receipt in store.status(first.id)["receipts"])

    with pytest.raises(CapabilityPackLifecycleError, match="goal"):
        store.activate(first, root_path=root, goal_id="goal-2", review_id=review["review_id"])

    expanded = parse_capability_pack_manifest(_manifest(extra_authority=["write_file"]))
    assert authority_delta(first, expanded)["requires_approval"] is True
    expanded_root, _ = _package(tmp_path / "expanded", manifest_text=_manifest(extra_authority=["write_file"]))
    expanded_review = store.review(expanded, root_path=expanded_root, goal_id="goal-1")["review"]
    with pytest.raises(CapabilityPackLifecycleError, match="expansion"):
        store.update(expanded, root_path=expanded_root, goal_id="goal-1", review_id=expanded_review["review_id"])
    approved = store.review(expanded, root_path=expanded_root, goal_id="goal-1", authority_expansion_approved=True)["review"]
    store.update(expanded, root_path=expanded_root, goal_id="goal-1", review_id=approved["review_id"])
    assert store.status(first.id)["active"]["version"] == expanded.version


def test_concurrent_activation_has_one_pointer_and_atomic_write_failure_keeps_old(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    first_root, first = _package(tmp_path / "first")
    second_root, second = _package(tmp_path / "second", manifest_text=_manifest(version="2.0.0"))
    store = CapabilityPackLifecycle(tmp_path / "state.json")
    first_review = store.review(first, root_path=first_root, goal_id="goal-1")["review"]
    second_review = store.review(second, root_path=second_root, goal_id="goal-1")["review"]

    def activate(item):
        pack, root, review = item
        try:
            return store.activate(pack, root_path=root, goal_id="goal-1", review_id=review["review_id"])["status"]
        except CapabilityPackLifecycleError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(activate, [(first, first_root, first_review), (second, second_root, second_review)]))
    assert sorted(outcomes) == ["active", "rejected"]
    assert store.status(first.id)["active"]["status"] == "active"

    before = store.status(first.id)["active"]
    original_save = store._atomic_save
    monkeypatch.setattr(store, "_atomic_save", lambda _state: (_ for _ in ()).throw(OSError("simulated crash")))
    with pytest.raises(OSError, match="simulated crash"):
        store.pause(first.id)
    assert store.status(first.id)["active"] == before
    monkeypatch.setattr(store, "_atomic_save", original_save)

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    state["active"][first.id]["digest"] = "0" * 64
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    assert store.status(first.id)["active"]["status"] == "invalid"
    with pytest.raises(CapabilityPackLifecycleError, match="binding is invalid"):
        store.run_secondary_canary(first.id, goal_id="goal-1")


def test_revoke_rollback_uninstall_and_canaries_preserve_receipts(tmp_path: Path):
    first_root, first = _package(tmp_path / "first", extra_file="first")
    second_root, second = _package(tmp_path / "second", manifest_text=_manifest(version="2.0.0"), extra_file="second")
    remote_root, remote = _package(tmp_path / "remote", manifest_text=_manifest(pack_id="seraph.remote-pack", network=True, cost=25), extra_file="remote")
    store = CapabilityPackLifecycle(tmp_path / "state.json")
    first_review = store.review(first, root_path=first_root, goal_id="goal-1")["review"]
    store.activate(first, root_path=first_root, goal_id="goal-1", review_id=first_review["review_id"])
    second_review = store.review(second, root_path=second_root, goal_id="goal-1")["review"]
    store.update(second, root_path=second_root, goal_id="goal-1", review_id=second_review["review_id"])

    store.revoke(first.id, digest=first_review["digest"])
    with pytest.raises(CapabilityPackLifecycleError, match="revoked"):
        store.rollback(first.id)
    uninstalled = store.uninstall(first.id)
    assert uninstalled["status"] == "uninstalled"
    assert len(store.status(first.id)["receipts"]) >= 4

    remote_review = store.review(remote, root_path=remote_root, goal_id="goal-remote")["review"]
    store.activate(remote, root_path=remote_root, goal_id="goal-remote", review_id=remote_review["review_id"])
    primary = store.run_primary_canary(remote.id, goal_id="goal-remote", artifact_root=tmp_path / "artifacts")
    secondary = store.run_secondary_canary(remote.id, goal_id="goal-remote", artifact_root=tmp_path / "artifacts")
    assert primary["status"] == "succeeded"
    assert primary["provider_calls"] == 0
    assert primary["artifact"]["readback_ok"] is True
    assert secondary["status"] == "succeeded"
    assert secondary["memory"]["status"] == "no_learning"

    local_root, local = _package(tmp_path / "local")
    local_review = store.review(local, root_path=local_root, goal_id="goal-local")["review"]
    store.activate(local, root_path=local_root, goal_id="goal-local", review_id=local_review["review_id"])
    blocked = store.run_primary_canary(local.id, goal_id="goal-local")
    assert blocked["status"] == "blocked"
    assert blocked["provider_calls"] == 0


def test_canary_runner_is_local_and_artifact_result_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root, pack = _package(tmp_path / "pack")
    store = CapabilityPackLifecycle(tmp_path / "state.json")
    review = store.review(pack, root_path=root, goal_id="goal-1")["review"]
    store.activate(pack, root_path=root, goal_id="goal-1", review_id=review["review_id"])

    def fail_transport(*_args, **_kwargs):
        raise AssertionError("capability-pack canary attempted provider transport")

    import httpx

    monkeypatch.setattr(httpx, "request", fail_transport)
    canary = store.run_secondary_canary(
        pack.id,
        goal_id="goal-1",
        artifact_root=tmp_path / "artifacts",
        runner=lambda _request: {
            "outcome": "fixture-ok",
            "secret": "sk-live-inline",
            "sources": ["fixture://local"],
        },
    )
    assert canary["provider_calls"] == 0
    artifact = next((tmp_path / "artifacts").glob("*.json"))
    assert "sk-live-inline" not in artifact.read_text(encoding="utf-8")
    assert json.loads(artifact.read_text(encoding="utf-8"))["result"] == {
        "outcome": "fixture-ok",
        "readback": True,
        "sources": ["fixture://local"],
    }
