from pathlib import Path

from src.extensions.registry import ExtensionRegistry


def test_registry_loads_manifest_backed_extensions(tmp_path: Path):
    pack_dir = tmp_path / "extensions" / "research-briefing"
    pack_dir.mkdir(parents=True)
    (pack_dir / "manifest.yaml").write_text(
        """
id: seraph.research-briefing
version: 2026.3.21
display_name: Research Briefing
kind: capability-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: bundled
contributes:
  skills:
    - skills/web-briefing.md
  workflows:
    - workflows/web-brief-to-file.md
""".strip(),
        encoding="utf-8",
    )

    registry = ExtensionRegistry(
        manifest_roots=[str(tmp_path / "extensions")],
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    )

    snapshot = registry.snapshot()

    assert snapshot.load_errors == []
    extension = snapshot.get_extension("seraph.research-briefing")
    assert extension is not None
    assert extension.source == "manifest"
    assert {item.contribution_type for item in extension.contributions} == {"skills", "workflows"}
    assert extension.metadata["compatibility"] == ">=2026.3.19"


def test_registry_records_invalid_manifest_errors(tmp_path: Path):
    bad_dir = tmp_path / "extensions" / "bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "manifest.yaml").write_text(
        """
id: seraph.bad
version: 2026.3.21
display_name: Bad
kind: connector-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes:
  skills:
    - skills/helper.md
""".strip(),
        encoding="utf-8",
    )

    registry = ExtensionRegistry(
        manifest_roots=[str(tmp_path / "extensions")],
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    )

    snapshot = registry.snapshot()

    assert snapshot.extensions == []
    assert len(snapshot.load_errors) == 1
    assert snapshot.load_errors[0].phase == "manifest"
    assert "connector surface" in snapshot.load_errors[0].message


def test_registry_records_incompatible_manifest_as_load_error(tmp_path: Path):
    pack_dir = tmp_path / "extensions" / "future-pack"
    pack_dir.mkdir(parents=True)
    (pack_dir / "manifest.yaml").write_text(
        """
id: seraph.future-pack
version: 2026.4.1
display_name: Future Pack
kind: capability-pack
compatibility:
  seraph: ">=2027.1"
publisher:
  name: Seraph
trust: bundled
contributes:
  skills:
    - skills/future.md
""".strip(),
        encoding="utf-8",
    )

    registry = ExtensionRegistry(
        manifest_roots=[str(tmp_path / "extensions")],
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
        seraph_version="2026.3.19",
    )

    snapshot = registry.snapshot()

    assert snapshot.extensions == []
    assert len(snapshot.load_errors) == 1
    assert snapshot.load_errors[0].phase == "compatibility"
    assert "current runtime is 2026.3.19" in snapshot.load_errors[0].message


def test_registry_synthesizes_legacy_skill_and_workflow_sources(tmp_path: Path):
    skills_dir = tmp_path / "workspace" / "skills"
    workflows_dir = tmp_path / "workspace" / "workflows"
    skills_dir.mkdir(parents=True)
    workflows_dir.mkdir(parents=True)

    (skills_dir / "brief.md").write_text(
        "---\n"
        "name: research-brief\n"
        "description: Research brief helper\n"
        "requires:\n"
        "  tools: [web_search]\n"
        "---\n\n"
        "Use web_search and summarize.\n",
        encoding="utf-8",
    )
    (workflows_dir / "brief.md").write_text(
        "---\n"
        "name: Web Brief\n"
        "description: Fetch and save a brief\n"
        "requires:\n"
        "  tools: [web_search, write_file]\n"
        "steps:\n"
        "  - tool: web_search\n"
        "    arguments:\n"
        "      query: test\n"
        "  - tool: write_file\n"
        "    arguments:\n"
        "      file_path: notes/output.md\n"
        "      content: done\n"
        "---\n",
        encoding="utf-8",
    )

    registry = ExtensionRegistry(
        manifest_roots=[],
        skill_dirs=[str(skills_dir)],
        workflow_dirs=[str(workflows_dir)],
        mcp_runtime=None,
    )

    snapshot = registry.snapshot()
    skill_extension = next(item for item in snapshot.extensions if item.id.startswith("legacy.skills."))
    workflow_extension = next(item for item in snapshot.extensions if item.id.startswith("legacy.workflows."))

    assert skill_extension.kind == "capability-pack"
    assert skill_extension.contributions[0].metadata["name"] == "research-brief"
    assert workflow_extension.contributions[0].metadata["tool_name"] == "workflow_web_brief"
    assert snapshot.list_contributions("skills")[0].reference.endswith("brief.md")


def test_registry_surfaces_legacy_loader_errors(tmp_path: Path):
    skills_dir = tmp_path / "workspace" / "skills"
    workflows_dir = tmp_path / "workspace" / "workflows"
    skills_dir.mkdir(parents=True)
    workflows_dir.mkdir(parents=True)

    (skills_dir / "broken.md").write_text("no frontmatter", encoding="utf-8")
    (workflows_dir / "broken.md").write_text(
        "---\nname: Broken workflow\n---\n",
        encoding="utf-8",
    )

    registry = ExtensionRegistry(
        manifest_roots=[],
        skill_dirs=[str(skills_dir)],
        workflow_dirs=[str(workflows_dir)],
        mcp_runtime=None,
    )

    snapshot = registry.snapshot()

    assert snapshot.extensions == []
    assert {item.phase for item in snapshot.load_errors} == {"legacy-skills", "legacy-workflows"}


def test_registry_synthesizes_legacy_mcp_runtime_config():
    class StubMCPRuntime:
        _config_path = "/tmp/runtime-mcp.json"

        @staticmethod
        def get_config():
            return [
                {
                    "name": "github",
                    "url": "https://example.test/mcp",
                    "enabled": True,
                    "connected": False,
                    "status": "auth_required",
                    "tool_count": 0,
                }
            ]

    registry = ExtensionRegistry(
        manifest_roots=[],
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=StubMCPRuntime(),
    )

    snapshot = registry.snapshot()

    extension = next(item for item in snapshot.extensions if item.id.startswith("legacy.mcp-runtime."))
    assert extension.kind == "connector-pack"
    assert extension.contributions[0].contribution_type == "mcp_servers"
    assert extension.contributions[0].reference == "github"
    assert extension.contributions[0].metadata["status"] == "auth_required"


def test_registry_surfaces_legacy_mcp_runtime_failures():
    class BrokenMCPRuntime:
        _config_path = "/tmp/broken-mcp.json"

        @staticmethod
        def get_config():
            raise RuntimeError("boom")

    registry = ExtensionRegistry(
        manifest_roots=[],
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=BrokenMCPRuntime(),
    )

    snapshot = registry.snapshot()

    assert snapshot.extensions == []
    assert len(snapshot.load_errors) == 1
    assert snapshot.load_errors[0].phase == "legacy-mcp"
    assert "boom" in snapshot.load_errors[0].message


def test_registry_prefers_manifest_backed_entries_over_matching_legacy_sources(tmp_path: Path):
    skills_dir = tmp_path / "workspace" / "skills"
    skills_dir.mkdir(parents=True)

    legacy_skill_path = skills_dir / "research-briefing.md"
    legacy_skill_path.write_text(
        "---\nname: research-briefing\ndescription: Legacy skill\n---\n",
        encoding="utf-8",
    )
    (skills_dir / "manifest.yaml").write_text(
        """
id: seraph.research-briefing
version: 2026.3.21
display_name: Research Briefing
kind: capability-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes:
  skills:
    - research-briefing.md
""".strip(),
        encoding="utf-8",
    )

    registry = ExtensionRegistry(
        manifest_roots=[str(skills_dir)],
        skill_dirs=[str(skills_dir)],
        workflow_dirs=[],
        mcp_runtime=None,
        seraph_version="2026.3.19",
    )

    snapshot = registry.snapshot()

    assert snapshot.get_extension("seraph.research-briefing") is not None
    assert not any(item.id.startswith("legacy.skills.") for item in snapshot.extensions)
