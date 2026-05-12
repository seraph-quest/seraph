from pathlib import Path

import pytest

from config.settings import settings
from src.extensions.governance import (
    ExtensionGovernanceError,
    assert_governance_allows_update_transition,
    assert_governance_allows_lifecycle,
    build_package_review_receipt,
    build_governance_status,
    extension_permission_fingerprint,
    governance_package_digest,
    governance_signature_value,
)
from src.extensions.lifecycle import (
    configure_extension,
    list_extensions,
    save_extension_source,
    _raise_if_governance_blocks_lifecycle,
)
from src.extensions.manifest import ExtensionManifest, load_extension_manifest
from src.extensions.registry import ExtensionRecord, default_manifest_roots_for_workspace
from src.runbooks.manager import runbook_manager
from src.skills.manager import skill_manager
from src.starter_packs.manager import starter_pack_manager
from src.tools.mcp_manager import mcp_manager
from src.workflows.manager import workflow_manager


_KEY_ID = "seraph-root-2026"


def _write_skill(package_dir: Path, *, body: str = "Use the helper skill.\n") -> None:
    (package_dir / "skills").mkdir(parents=True, exist_ok=True)
    (package_dir / "skills" / "helper.md").write_text(
        "---\n"
        "name: helper-skill\n"
        "description: Helper skill\n"
        "requires:\n"
        "  tools: []\n"
        "user_invocable: true\n"
        "---\n\n"
        f"{body}",
        encoding="utf-8",
    )


@pytest.fixture
def extension_runtime(tmp_path: Path):
    original_workspace_dir = settings.workspace_dir
    original_skill_manager = (
        list(skill_manager._skills),
        list(skill_manager._load_errors),
        skill_manager._skills_dir,
        list(skill_manager._manifest_roots),
        skill_manager._config_path,
        set(skill_manager._disabled),
        skill_manager._registry,
    )
    original_workflow_manager = (
        list(workflow_manager._workflows),
        list(workflow_manager._load_errors),
        list(workflow_manager._shared_manifest_errors),
        workflow_manager._workflows_dir,
        list(workflow_manager._manifest_roots),
        workflow_manager._config_path,
        set(workflow_manager._disabled),
        workflow_manager._registry,
    )
    original_runbook_manager = (
        list(runbook_manager._runbooks),
        list(runbook_manager._load_errors),
        list(runbook_manager._shared_manifest_errors),
        runbook_manager._runbooks_dir,
        list(runbook_manager._manifest_roots),
        runbook_manager._registry,
    )
    original_starter_pack_manager = (
        list(starter_pack_manager._packs),
        list(starter_pack_manager._load_errors),
        list(starter_pack_manager._shared_manifest_errors),
        starter_pack_manager._legacy_path,
        list(starter_pack_manager._manifest_roots),
        starter_pack_manager._registry,
    )
    original_mcp_manager = (
        dict(mcp_manager._config),
        dict(mcp_manager._status),
        dict(mcp_manager._clients),
        dict(mcp_manager._tools),
        mcp_manager._config_path,
    )

    workspace_dir = tmp_path / "workspace"
    skills_dir = workspace_dir / "skills"
    workflows_dir = workspace_dir / "workflows"
    runbooks_dir = workspace_dir / "runbooks"
    extensions_dir = workspace_dir / "extensions"
    skills_dir.mkdir(parents=True)
    workflows_dir.mkdir()
    runbooks_dir.mkdir()
    extensions_dir.mkdir()

    settings.workspace_dir = str(workspace_dir)
    manifest_roots = default_manifest_roots_for_workspace(str(workspace_dir))
    skill_manager.init(str(skills_dir), manifest_roots=manifest_roots)
    workflow_manager.init(str(workflows_dir), manifest_roots=manifest_roots)
    runbook_manager.init(str(runbooks_dir), manifest_roots=manifest_roots)
    starter_pack_manager.init(str(workspace_dir / "starter-packs.json"), manifest_roots=manifest_roots)
    mcp_manager.disconnect_all()
    mcp_manager._config = {}
    mcp_manager._status = {}
    mcp_manager._tools = {}
    mcp_manager._config_path = str(workspace_dir / "mcp-servers.json")

    yield workspace_dir

    settings.workspace_dir = original_workspace_dir
    (
        skill_manager._skills,
        skill_manager._load_errors,
        skill_manager._skills_dir,
        skill_manager._manifest_roots,
        skill_manager._config_path,
        skill_manager._disabled,
        skill_manager._registry,
    ) = original_skill_manager
    (
        workflow_manager._workflows,
        workflow_manager._load_errors,
        workflow_manager._shared_manifest_errors,
        workflow_manager._workflows_dir,
        workflow_manager._manifest_roots,
        workflow_manager._config_path,
        workflow_manager._disabled,
        workflow_manager._registry,
    ) = original_workflow_manager
    (
        runbook_manager._runbooks,
        runbook_manager._load_errors,
        runbook_manager._shared_manifest_errors,
        runbook_manager._runbooks_dir,
        runbook_manager._manifest_roots,
        runbook_manager._registry,
    ) = original_runbook_manager
    (
        starter_pack_manager._packs,
        starter_pack_manager._load_errors,
        starter_pack_manager._shared_manifest_errors,
        starter_pack_manager._legacy_path,
        starter_pack_manager._manifest_roots,
        starter_pack_manager._registry,
    ) = original_starter_pack_manager
    mcp_manager.disconnect_all()
    (
        mcp_manager._config,
        mcp_manager._status,
        mcp_manager._clients,
        mcp_manager._tools,
        mcp_manager._config_path,
    ) = original_mcp_manager


def _verified_manifest(digest: str, signature: str) -> str:
    return (
        "id: seraph.verified-pack\n"
        "version: 2026.3.21\n"
        "display_name: Verified Pack\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.4.10\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: verified\n"
        "governance:\n"
        "  provenance:\n"
        "    source: seraph-catalog\n"
        "    publisher_id: seraph\n"
        "  signature:\n"
        "    algorithm: seraph-sha256-v1\n"
        f"    key_id: {_KEY_ID}\n"
        f"    digest: {digest}\n"
        f"    signature: {signature}\n"
        "contributes:\n"
        "  skills:\n"
        "    - skills/helper.md\n"
        "permissions:\n"
        "  tools: []\n"
        "  network: false\n"
    )


def _verified_connector_manifest(digest: str, signature: str) -> str:
    return (
        "id: seraph.verified-connectors\n"
        "version: 2026.3.21\n"
        "display_name: Verified Connectors\n"
        "kind: connector-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.4.10\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: verified\n"
        "governance:\n"
        "  provenance:\n"
        "    source: seraph-catalog\n"
        "    publisher_id: seraph\n"
        "  signature:\n"
        "    algorithm: seraph-sha256-v1\n"
        f"    key_id: {_KEY_ID}\n"
        f"    digest: {digest}\n"
        f"    signature: {signature}\n"
        "contributes:\n"
        "  mcp_servers:\n"
        "    - mcp/github.json\n"
        "  managed_connectors:\n"
        "    - connectors/managed/github.yaml\n"
        "permissions:\n"
        "  execution_boundaries: [external_mcp]\n"
        "  audit_events: [mcp_request]\n"
        "  network: true\n"
    )


def _write_verified_package(tmp_path: Path, *, invalid_signature: bool = False) -> tuple[Path, ExtensionManifest]:
    package_dir = tmp_path / "verified-pack"
    package_dir.mkdir()
    _write_skill(package_dir)
    zero_digest = "0" * 64
    (package_dir / "manifest.yaml").write_text(
        _verified_manifest(zero_digest, governance_signature_value(key_id=_KEY_ID, digest=zero_digest)),
        encoding="utf-8",
    )
    digest = governance_package_digest(package_dir)
    assert digest is not None
    signature = governance_signature_value(key_id=_KEY_ID, digest=digest)
    if invalid_signature:
        signature = f"{signature}-tampered"
    (package_dir / "manifest.yaml").write_text(
        _verified_manifest(digest, signature),
        encoding="utf-8",
    )
    return package_dir, load_extension_manifest(package_dir / "manifest.yaml")


def _write_verified_connector_package(tmp_path: Path) -> tuple[Path, ExtensionManifest]:
    package_dir = tmp_path / "verified-connectors"
    (package_dir / "mcp").mkdir(parents=True)
    (package_dir / "connectors" / "managed").mkdir(parents=True)
    (package_dir / "mcp" / "github.json").write_text(
        "{\n"
        '  "name": "verified-github",\n'
        '  "url": "https://example.test/mcp",\n'
        '  "description": "Verified GitHub MCP",\n'
        '  "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"},\n'
        '  "auth_hint": "Set GITHUB_TOKEN before enabling the connector",\n'
        '  "transport": "streamable-http"\n'
        "}\n",
        encoding="utf-8",
    )
    (package_dir / "connectors" / "managed" / "github.yaml").write_text(
        "name: verified-github-managed\n"
        "provider: github\n"
        "description: Verified GitHub connector\n"
        "auth_kind: oauth\n"
        "config_fields:\n"
        "  - key: installation_id\n"
        "    label: Installation ID\n"
        "    required: true\n",
        encoding="utf-8",
    )
    zero_digest = "0" * 64
    (package_dir / "manifest.yaml").write_text(
        _verified_connector_manifest(zero_digest, governance_signature_value(key_id=_KEY_ID, digest=zero_digest)),
        encoding="utf-8",
    )
    digest = governance_package_digest(package_dir)
    assert digest is not None
    signature = governance_signature_value(key_id=_KEY_ID, digest=digest)
    (package_dir / "manifest.yaml").write_text(
        _verified_connector_manifest(digest, signature),
        encoding="utf-8",
    )
    return package_dir, load_extension_manifest(package_dir / "manifest.yaml")


def _reviewed_state(manifest: ExtensionManifest, package_dir: Path) -> dict[str, object]:
    digest = governance_package_digest(package_dir)
    assert digest is not None
    return {
        "governance": {
            "review_status": "approved",
            "reviewed_digest": digest,
            "reviewed_key_id": _KEY_ID,
            "reviewed_permission_fingerprint": extension_permission_fingerprint(manifest),
        }
    }


def _record(manifest: ExtensionManifest, package_dir: Path) -> ExtensionRecord:
    return ExtensionRecord(
        id=manifest.id,
        display_name=manifest.display_name,
        kind=manifest.kind.value,
        trust=manifest.trust.value,
        source="manifest",
        root_path=str(package_dir),
        manifest_path=str(package_dir / "manifest.yaml"),
        manifest=manifest,
        contributions=[],
    )


def test_valid_verified_pack_exposes_reviewed_governance_status(tmp_path: Path):
    package_dir, manifest = _write_verified_package(tmp_path)
    state_entry = _reviewed_state(manifest, package_dir)

    status = build_governance_status(manifest, root_path=package_dir, state_entry=state_entry)

    assert status["status"] == "verified"
    assert status["signature_status"] == "valid"
    assert status["review_status"] == "reviewed"
    assert status["revocation_status"] == "not_revoked"
    assert status["provenance"] == {"source": "seraph-catalog", "publisher_id": "seraph", "url": None, "catalog_entry": None}
    assert status["current_digest"] == status["reviewed_digest"]
    assert status["current_digest"] == status["signed_digest"]
    assert status["signing_key_id"] == _KEY_ID
    assert status["permission_drift"] is False
    assert status["fail_closed_reason"] is None
    assert status["review_receipt"]["key_id"] == _KEY_ID
    assert status["review_receipt"]["permission_fingerprint"] == extension_permission_fingerprint(manifest)
    assert status["review_receipt"]["supply_chain_status"] == "verified_signature_valid"
    assert status["claim_boundary"] == "local_governed_marketplace_foundations_not_production_marketplace_security"


def test_package_review_receipt_exposes_compatibility_and_supply_chain_verdicts(tmp_path: Path):
    package_dir, manifest = _write_verified_package(tmp_path)
    state_entry = _reviewed_state(manifest, package_dir)

    receipt = build_package_review_receipt(
        manifest,
        root_path=package_dir,
        state_entry=state_entry,
        seraph_version="2026.4.10",
    )

    assert receipt["status"] == "reviewable"
    assert receipt["review_status"] == "reviewed"
    assert receipt["compatibility_status"] == "compatible"
    assert receipt["supply_chain_status"] == "verified_signature_valid"
    assert receipt["trust_downgrade_status"] == "not_downgraded"
    assert receipt["blocking_reasons"] == []


def test_package_review_receipt_blocks_missing_verified_review(tmp_path: Path):
    package_dir, manifest = _write_verified_package(tmp_path)

    receipt = build_package_review_receipt(
        manifest,
        root_path=package_dir,
        state_entry={},
        seraph_version="2026.4.10",
    )

    assert receipt["review_status"] == "missing"
    assert receipt["status"] == "blocked"
    assert "review_missing" in receipt["blocking_reasons"]
    assert receipt["recommended_action"] == "reject_or_re_review"


def test_invalid_signature_fails_closed_for_lifecycle_actions(tmp_path: Path):
    package_dir, manifest = _write_verified_package(tmp_path, invalid_signature=True)
    state_entry = _reviewed_state(manifest, package_dir)

    status = build_governance_status(manifest, root_path=package_dir, state_entry=state_entry)

    assert status["signature_status"] == "invalid"
    assert status["fail_closed_reason"] == "signature_invalid"
    with pytest.raises(ExtensionGovernanceError, match="blocks install: signature_invalid"):
        assert_governance_allows_lifecycle(
            manifest,
            root_path=package_dir,
            state_entry=state_entry,
            action="install",
        )


def test_tampered_digest_fails_closed_for_update(tmp_path: Path):
    package_dir, manifest = _write_verified_package(tmp_path)
    state_entry = _reviewed_state(manifest, package_dir)
    _write_skill(package_dir, body="Tampered after review.\n")

    status = build_governance_status(manifest, root_path=package_dir, state_entry=state_entry)

    assert status["signature_status"] == "digest_mismatch"
    assert status["review_status"] == "stale"
    with pytest.raises(ExtensionGovernanceError, match="blocks update: signature_digest_mismatch"):
        assert_governance_allows_lifecycle(
            manifest,
            root_path=package_dir,
            state_entry=state_entry,
            action="update",
        )


def test_revoked_verified_pack_fails_closed_for_enable(tmp_path: Path):
    package_dir, manifest = _write_verified_package(tmp_path)
    state_entry = _reviewed_state(manifest, package_dir)
    state_entry["governance"]["revoked_digests"] = [governance_package_digest(package_dir)]
    state_entry["governance"]["revocation_reason"] = "operator revoked digest"

    status = build_governance_status(manifest, root_path=package_dir, state_entry=state_entry)

    assert status["revocation_status"] == "revoked"
    assert status["fail_closed_reason"] == "operator revoked digest"
    with pytest.raises(ExtensionGovernanceError, match="operator revoked digest"):
        _raise_if_governance_blocks_lifecycle(
            _record(manifest, package_dir),
            action="enable",
            state_entry=state_entry,
        )


def test_stale_review_fails_closed_for_connector_toggle(tmp_path: Path):
    package_dir, manifest = _write_verified_package(tmp_path)
    state_entry = _reviewed_state(manifest, package_dir)
    state_entry["governance"]["reviewed_digest"] = "1" * 64

    status = build_governance_status(manifest, root_path=package_dir, state_entry=state_entry)

    assert status["signature_status"] == "valid"
    assert status["review_status"] == "stale"
    assert status["fail_closed_reason"] == "review_stale"
    with pytest.raises(ExtensionGovernanceError, match="blocks enable connector: review_stale"):
        assert_governance_allows_lifecycle(
            manifest,
            root_path=package_dir,
            state_entry=state_entry,
            action="enable connector",
        )


def test_stale_review_fails_closed_for_configure_extension(extension_runtime: Path):
    package_dir, manifest = _write_verified_package(extension_runtime / "extensions")
    state_payload = {
        "extensions": {
            manifest.id: _reviewed_state(manifest, package_dir),
        }
    }
    state_payload["extensions"][manifest.id]["governance"]["reviewed_digest"] = "1" * 64
    from src.extensions.state import save_extension_state_payload

    save_extension_state_payload(state_payload)

    with pytest.raises(ExtensionGovernanceError, match="blocks configure: review_stale"):
        configure_extension(manifest.id, {})


def test_revoked_verified_pack_fails_closed_for_source_save(extension_runtime: Path):
    package_dir, manifest = _write_verified_package(extension_runtime / "extensions")
    state_entry = _reviewed_state(manifest, package_dir)
    state_entry["governance"]["revoked_digests"] = [governance_package_digest(package_dir)]
    from src.extensions.state import save_extension_state_payload

    save_extension_state_payload({"extensions": {manifest.id: state_entry}})

    with pytest.raises(ExtensionGovernanceError, match="blocks source-save: revoked"):
        save_extension_source(manifest.id, "skills/helper.md", "Updated helper body.\n")

    assert (package_dir / "skills" / "helper.md").read_text(encoding="utf-8").endswith("Use the helper skill.\n")


def test_blocked_verified_pack_sync_disables_already_enabled_connector_access(extension_runtime: Path):
    package_dir, manifest = _write_verified_connector_package(extension_runtime / "extensions")
    state_entry = _reviewed_state(manifest, package_dir)
    state_entry["connector_state"] = {
        "connectors/managed/github.yaml": {"enabled": True},
    }
    state_entry["governance"]["revoked"] = True
    state_entry["governance"]["revocation_reason"] = "operator revoked pack"
    from src.extensions.state import load_extension_state_payload, save_extension_state_payload

    save_extension_state_payload({"extensions": {manifest.id: state_entry}})
    mcp_manager._config["verified-github"] = {
        "url": "https://example.test/mcp",
        "enabled": True,
        "extension_id": manifest.id,
        "extension_reference": "mcp/github.json",
        "extension_display_name": manifest.display_name,
        "source": "extension",
    }

    payload = list_extensions()
    extension_payload = next(item for item in payload["extensions"] if item["id"] == manifest.id)
    managed = next(item for item in extension_payload["contributions"] if item["type"] == "managed_connectors")

    assert extension_payload["governance"]["fail_closed"] is True
    assert mcp_manager._config["verified-github"]["enabled"] is False
    assert load_extension_state_payload()["extensions"][manifest.id]["connector_state"]["connectors/managed/github.yaml"]["enabled"] is False
    assert managed["enabled"] is False


def test_local_unsigned_pack_remains_allowed(tmp_path: Path):
    package_dir = tmp_path / "local-pack"
    package_dir.mkdir()
    _write_skill(package_dir)
    (package_dir / "manifest.yaml").write_text(
        "id: seraph.local-pack\n"
        "version: 2026.3.21\n"
        "display_name: Local Pack\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.4.10\"\n"
        "publisher:\n"
        "  name: Local Operator\n"
        "trust: local\n"
        "contributes:\n"
        "  skills:\n"
        "    - skills/helper.md\n",
        encoding="utf-8",
    )
    manifest = load_extension_manifest(package_dir / "manifest.yaml")

    status = build_governance_status(manifest, root_path=package_dir, state_entry={})

    assert status["status"] == "local_unsigned"
    assert status["signature_status"] == "unsigned_allowed"
    assert status["review_status"] == "not_required"
    assert status["fail_closed"] is False
    assert_governance_allows_lifecycle(
        manifest,
        root_path=package_dir,
        state_entry={},
        action="install",
    )


def test_verified_provider_update_blocks_trust_downgrade(tmp_path: Path):
    existing_dir, existing_manifest = _write_verified_connector_package(tmp_path)
    state_entry = _reviewed_state(existing_manifest, existing_dir)
    candidate_dir = tmp_path / "local-connectors"
    (candidate_dir / "mcp").mkdir(parents=True)
    (candidate_dir / "connectors" / "managed").mkdir(parents=True)
    (candidate_dir / "mcp" / "github.json").write_text((existing_dir / "mcp" / "github.json").read_text(encoding="utf-8"), encoding="utf-8")
    (candidate_dir / "connectors" / "managed" / "github.yaml").write_text(
        (existing_dir / "connectors" / "managed" / "github.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (candidate_dir / "manifest.yaml").write_text(
        "id: seraph.verified-connectors\n"
        "version: 2026.3.22\n"
        "display_name: Verified Connectors\n"
        "kind: connector-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.4.10\"\n"
        "publisher:\n"
        "  name: Local Operator\n"
        "trust: local\n"
        "governance:\n"
        "  provenance:\n"
        "    source: local-authoring\n"
        "contributes:\n"
        "  mcp_servers:\n"
        "    - mcp/github.json\n"
        "  managed_connectors:\n"
        "    - connectors/managed/github.yaml\n"
        "permissions:\n"
        "  execution_boundaries: [external_mcp]\n"
        "  audit_events: [mcp_request]\n"
        "  network: true\n",
        encoding="utf-8",
    )
    candidate_manifest = load_extension_manifest(candidate_dir / "manifest.yaml")

    with pytest.raises(ExtensionGovernanceError, match="trust_downgrade"):
        assert_governance_allows_update_transition(
            candidate_manifest,
            existing_manifest=existing_manifest,
            root_path=candidate_dir,
            state_entry=state_entry,
            seraph_version="2026.4.10",
        )


def test_verified_provider_update_blocks_downgrade_version(tmp_path: Path):
    existing_dir, existing_manifest = _write_verified_connector_package(tmp_path)
    downgraded_dir, downgraded_manifest = _write_verified_connector_package(tmp_path / "candidate")
    (downgraded_dir / "manifest.yaml").write_text(
        (downgraded_dir / "manifest.yaml").read_text(encoding="utf-8").replace(
            "version: 2026.3.21",
            "version: 2026.3.20",
        ),
        encoding="utf-8",
    )
    downgraded_manifest = load_extension_manifest(downgraded_dir / "manifest.yaml")
    state_entry = _reviewed_state(existing_manifest, existing_dir)

    with pytest.raises(ExtensionGovernanceError, match="version_downgrade"):
        assert_governance_allows_update_transition(
            downgraded_manifest,
            existing_manifest=existing_manifest,
            root_path=downgraded_dir,
            state_entry=state_entry,
            seraph_version="2026.4.10",
        )
