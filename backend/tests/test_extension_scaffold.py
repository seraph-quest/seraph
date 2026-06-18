from pathlib import Path

import pytest

from src.extensions.scaffold import scaffold_extension_package, validate_extension_package


def test_scaffold_capability_pack_creates_valid_manifest_and_files(tmp_path: Path):
    package = scaffold_extension_package(
        tmp_path / "research-pack",
        extension_id="seraph.research-pack",
        display_name="Research Pack",
        contributions=["skills", "workflows", "runbooks"],
    )

    assert package.manifest_path.exists()
    assert (package.package_root / "skills" / "research-pack.md").exists()
    assert (package.package_root / "workflows" / "research-pack.md").exists()
    assert (package.package_root / "runbooks" / "research-pack.yaml").exists()
    workflow_contents = (package.package_root / "workflows" / "research-pack.md").read_text(encoding="utf-8")
    runbook_contents = (package.package_root / "runbooks" / "research-pack.yaml").read_text(encoding="utf-8")
    assert "name: research-pack" in workflow_contents
    assert "workflow: research-pack" in runbook_contents

    report = validate_extension_package(package.package_root)

    assert report.ok is True
    assert [result.extension_id for result in report.results] == ["seraph.research-pack"]


def test_scaffold_connector_pack_creates_wave2_connector_placeholders(tmp_path: Path):
    package = scaffold_extension_package(
        tmp_path / "reach-pack",
        extension_id="seraph.reach-pack",
        display_name="Reach Pack",
        kind="connector-pack",
        contributions=["browser_providers", "messaging_connectors", "node_adapters"],
    )

    assert package.manifest_path.exists()
    assert (package.package_root / "connectors" / "browser" / "reach-pack.yaml").exists()
    assert (package.package_root / "connectors" / "messaging" / "reach-pack.yaml").exists()
    assert (package.package_root / "connectors" / "nodes" / "reach-pack.yaml").exists()

    report = validate_extension_package(package.package_root)

    assert report.ok is True
    assert [result.extension_id for result in report.results] == ["seraph.reach-pack"]


def test_scaffold_supports_wave2_capability_placeholders(tmp_path: Path):
    package = scaffold_extension_package(
        tmp_path / "guardian-pack",
        extension_id="seraph.guardian-pack",
        display_name="Guardian Pack",
        contributions=["toolset_presets", "context_packs", "speech_profiles"],
    )

    assert (package.package_root / "presets" / "toolset" / "guardian-pack.yaml").exists()
    assert (package.package_root / "context" / "guardian-pack.yaml").exists()
    assert (package.package_root / "speech" / "guardian-pack.yaml").exists()


def test_scaffold_rejects_connector_pack_without_connector_contributions(tmp_path: Path):
    with pytest.raises(ValueError) as exc_info:
        scaffold_extension_package(
            tmp_path / "invalid-connector-pack",
            extension_id="seraph.invalid-connector-pack",
            display_name="Invalid Connector Pack",
            kind="connector-pack",
            contributions=["toolset_presets"],
        )

    assert "connector-pack scaffolds must include at least one connector contribution" in str(exc_info.value)


def test_validate_extension_package_reports_missing_scaffolded_files(tmp_path: Path):
    package = scaffold_extension_package(
        tmp_path / "broken-pack",
        extension_id="seraph.broken-pack",
        display_name="Broken Pack",
        contributions=["skills"],
    )
    (package.package_root / "skills" / "broken-pack.md").unlink()

    report = validate_extension_package(package.package_root)

    assert report.ok is False
    assert report.results[0].issues[0].code == "missing_reference"


def test_validate_extension_package_rejects_non_package_directory(tmp_path: Path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    with pytest.raises(ValueError) as exc_info:
        validate_extension_package(empty_dir)

    assert "no extension manifest found" in str(exc_info.value)


def test_scaffold_rejects_existing_manifest(tmp_path: Path):
    package_dir = tmp_path / "existing-pack"
    package_dir.mkdir()
    (package_dir / "manifest.yaml").write_text("id: existing\n", encoding="utf-8")

    with pytest.raises(FileExistsError) as exc_info:
        scaffold_extension_package(
            package_dir,
            extension_id="seraph.existing-pack",
            display_name="Existing Pack",
        )

    assert "manifest already exists" in str(exc_info.value)
    assert list(package_dir.iterdir()) == [package_dir / "manifest.yaml"]
