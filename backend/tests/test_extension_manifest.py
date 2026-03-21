from pathlib import Path

import pytest

from src.extensions.manifest import ExtensionManifestError, load_extension_manifest, parse_extension_manifest


def test_parse_capability_pack_manifest_with_mixed_contributions():
    manifest = parse_extension_manifest(
        """
id: seraph.research-briefing
version: 2026.3.21
display_name: Research Briefing
kind: capability-pack
compatibility:
  seraph: ">=2026.3.19,<2027"
publisher:
  name: Seraph
trust: bundled
contributes:
  skills:
    - skills/web-briefing.md
  workflows:
    - workflows/web-brief-to-file.md
  runbooks:
    - runbooks/research-briefing.yaml
  mcp_servers:
    - mcp/http-request.json
permissions:
  tools:
    - web_search
    - write_file
  network: true
"""
    )

    assert manifest.id == "seraph.research-briefing"
    assert manifest.kind.value == "capability-pack"
    assert manifest.trust.value == "bundled"
    assert manifest.contributed_types() == {"skills", "workflows", "runbooks", "mcp_servers"}
    assert manifest.permissions.tools == ["web_search", "write_file"]
    assert manifest.is_compatible_with("2026.3.19") is True
    assert manifest.is_compatible_with("2027.1.0") is False


def test_parse_rejects_absolute_or_traversing_contribution_paths():
    with pytest.raises(ExtensionManifestError) as exc_info:
        parse_extension_manifest(
            """
id: seraph.bad-paths
version: 2026.3.21
display_name: Bad Paths
kind: capability-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes:
  skills:
    - /tmp/rooted.md
  workflows:
    - ../escape.md
"""
        )

    assert "must be relative" in str(exc_info.value) or "must not traverse outside the package" in str(exc_info.value)


def test_parse_rejects_directory_shaped_contribution_paths():
    with pytest.raises(ExtensionManifestError) as exc_info:
        parse_extension_manifest(
            """
id: seraph.directory-paths
version: 2026.3.21
display_name: Directory Paths
kind: capability-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes:
  skills:
    - skills/
"""
        )

    assert "must reference a file, not a directory" in str(exc_info.value)


def test_parse_rejects_empty_contributions():
    with pytest.raises(ExtensionManifestError) as exc_info:
        parse_extension_manifest(
            """
id: seraph.empty-pack
version: 2026.3.21
display_name: Empty Pack
kind: capability-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes: {}
"""
        )

    assert "must contribute at least one typed surface" in str(exc_info.value)


def test_parse_rejects_undeclared_future_native_tool_contribution():
    with pytest.raises(ExtensionManifestError) as exc_info:
        parse_extension_manifest(
            """
id: seraph.bad-native-tools
version: 2026.3.21
display_name: Bad Native Tools
kind: capability-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes:
  native_tools:
    - native_tools/custom_tool.py
"""
        )

    assert "Extra inputs are not permitted" in str(exc_info.value)


def test_parse_rejects_deferred_trusted_plugin_kind():
    with pytest.raises(ExtensionManifestError) as exc_info:
        parse_extension_manifest(
            """
id: seraph.trusted-without-code
version: 2026.3.21
display_name: Trusted Without Code
kind: trusted-plugin
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes:
  skills:
    - skills/helper.md
"""
        )

    assert "Input should be 'capability-pack' or 'connector-pack'" in str(exc_info.value)


def test_parse_rejects_connector_pack_without_connector_surface():
    with pytest.raises(ExtensionManifestError) as exc_info:
        parse_extension_manifest(
            """
id: seraph.connector-without-connector
version: 2026.3.21
display_name: Connector Without Connector
kind: connector-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes:
  skills:
    - skills/helper.md
"""
        )

    assert "connector-pack manifests must contribute at least one connector surface" in str(exc_info.value)


def test_parse_rejects_invalid_compatibility_specifier():
    with pytest.raises(ExtensionManifestError) as exc_info:
        parse_extension_manifest(
            """
id: seraph.bad-compat
version: 2026.3.21
display_name: Bad Compat
kind: capability-pack
compatibility:
  seraph: "later maybe"
publisher:
  name: Seraph
trust: local
contributes:
  skills:
    - skills/helper.md
"""
        )

    assert "invalid specifier" in str(exc_info.value)


def test_parse_rejects_invalid_manifest_version():
    with pytest.raises(ExtensionManifestError) as exc_info:
        parse_extension_manifest(
            """
id: seraph.bad-version
version: not-a-version
display_name: Bad Version
kind: capability-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes:
  skills:
    - skills/helper.md
"""
        )

    assert "invalid version" in str(exc_info.value)


def test_load_extension_manifest_from_disk(tmp_path: Path):
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        """
id: seraph.local-example
version: 2026.3.21
display_name: Local Example
kind: connector-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes:
  mcp_servers:
    - mcp/github.json
permissions:
  network: true
""".strip(),
        encoding="utf-8",
    )

    manifest = load_extension_manifest(manifest_path)

    assert manifest.display_name == "Local Example"
    assert manifest.permissions.network is True
    assert manifest.contributes.mcp_servers == ["mcp/github.json"]


def test_parse_rejects_duplicate_paths_within_contribution_list():
    with pytest.raises(ExtensionManifestError) as exc_info:
        parse_extension_manifest(
            """
id: seraph.duplicate-paths
version: 2026.3.21
display_name: Duplicate Paths
kind: capability-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes:
  skills:
    - skills/helper.md
    - skills/helper.md
"""
        )

    assert "duplicate path" in str(exc_info.value)
