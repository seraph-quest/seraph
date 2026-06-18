from pathlib import Path

from src.extensions.layout import (
    MANIFEST_FILENAMES,
    expected_layout_prefixes,
    is_package_manifest_path,
    iter_extension_manifest_paths,
    resolve_package_reference,
    validate_contribution_layout,
)


def test_expected_layout_prefixes_cover_known_contribution_types():
    assert expected_layout_prefixes("skills") == ("skills/",)
    assert expected_layout_prefixes("mcp_servers") == ("mcp/",)
    assert expected_layout_prefixes("observer_connectors") == ("observers/connectors/",)


def test_validate_contribution_layout_accepts_canonical_prefixes():
    assert validate_contribution_layout("skills", "skills/web-briefing.md") == "skills/web-briefing.md"
    assert validate_contribution_layout("provider_presets", "presets/provider/openrouter.yaml") == (
        "presets/provider/openrouter.yaml"
    )


def test_iter_extension_manifest_paths_discovers_manifest_files_once(tmp_path: Path):
    root = tmp_path / "extensions"
    pack_a = root / "research-pack"
    pack_b = root / "connectors" / "github-pack"
    pack_a.mkdir(parents=True)
    pack_b.mkdir(parents=True)
    (pack_a / "manifest.yaml").write_text("id: a\nversion: 2026.3.21\ndisplay_name: A\nkind: capability-pack\ncompatibility:\n  seraph: '>=2026.4.10'\npublisher:\n  name: Seraph\ntrust: local\ncontributes:\n  skills:\n    - skills/a.md\n", encoding="utf-8")
    (pack_b / "manifest.yml").write_text("id: b\nversion: 2026.3.21\ndisplay_name: B\nkind: connector-pack\ncompatibility:\n  seraph: '>=2026.4.10'\npublisher:\n  name: Seraph\ntrust: local\ncontributes:\n  mcp_servers:\n    - mcp/b.json\n", encoding="utf-8")

    manifest_paths = iter_extension_manifest_paths([str(root), str(pack_a / "manifest.yaml")])

    assert MANIFEST_FILENAMES == ("manifest.yaml", "manifest.yml")
    assert manifest_paths == sorted(manifest_paths)
    assert len(manifest_paths) == 2
    assert {str(path.resolve()) for path in manifest_paths} == {
        str((pack_a / "manifest.yaml").resolve()),
        str((pack_b / "manifest.yml").resolve()),
    }


def test_iter_extension_manifest_paths_skips_nested_contribution_manifests(tmp_path: Path):
    root = tmp_path / "extensions"
    package_dir = root / "research-pack"
    nested_manifest = package_dir / "skills" / "manifest.yaml"
    package_dir.mkdir(parents=True)
    nested_manifest.parent.mkdir(parents=True)
    (package_dir / "manifest.yaml").write_text("id: a\nversion: 2026.3.21\ndisplay_name: A\nkind: capability-pack\ncompatibility:\n  seraph: '>=2026.4.10'\npublisher:\n  name: Seraph\ntrust: local\ncontributes:\n  skills:\n    - skills/a.md\n", encoding="utf-8")
    nested_manifest.write_text("ignored: true\n", encoding="utf-8")

    manifest_paths = iter_extension_manifest_paths([str(root)])

    assert len(manifest_paths) == 1
    assert is_package_manifest_path(package_dir / "manifest.yaml", root) is True
    assert is_package_manifest_path(nested_manifest, root) is False


def test_resolve_package_reference_rejects_symlink_escape(tmp_path: Path):
    package_dir = tmp_path / "package"
    skills_dir = package_dir / "skills"
    external_dir = tmp_path / "external"
    package_dir.mkdir()
    skills_dir.mkdir()
    external_dir.mkdir()
    external_file = external_dir / "secret.md"
    external_file.write_text("secret", encoding="utf-8")
    (skills_dir / "link.md").symlink_to(external_file)

    try:
        resolve_package_reference(package_dir, "skills/link.md")
    except ValueError as exc:
        assert "escapes the package root" in str(exc)
    else:
        raise AssertionError("expected symlink escape to be rejected")
