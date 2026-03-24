from pathlib import Path

from src.extensions.channels import select_active_channel_adapters
from src.extensions.observers import select_active_observer_definitions
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


def test_registry_enriches_wave2_contribution_metadata(tmp_path: Path):
    pack_dir = tmp_path / "extensions" / "guardian-reach"
    (pack_dir / "presets" / "toolset").mkdir(parents=True)
    (pack_dir / "context").mkdir()
    (pack_dir / "prompts").mkdir()
    (pack_dir / "automation").mkdir()
    (pack_dir / "connectors" / "browser").mkdir(parents=True)
    (pack_dir / "connectors" / "messaging").mkdir()
    (pack_dir / "speech").mkdir()
    (pack_dir / "connectors" / "nodes").mkdir(parents=True)
    (pack_dir / "manifest.yaml").write_text(
        """
id: seraph.guardian-reach
version: 2026.3.23
display_name: Guardian Reach
kind: capability-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
permissions:
  tools: [read_file]
  network: true
contributes:
  toolset_presets:
    - presets/toolset/research.yaml
  context_packs:
    - context/research.yaml
  prompt_packs:
    - prompts/review.md
  automation_triggers:
    - automation/daily-brief.yaml
  browser_providers:
    - connectors/browser/browserbase.yaml
  messaging_connectors:
    - connectors/messaging/telegram.yaml
  speech_profiles:
    - speech/voice.yaml
  node_adapters:
    - connectors/nodes/companion.yaml
""".strip(),
        encoding="utf-8",
    )
    (pack_dir / "presets" / "toolset" / "research.yaml").write_text(
        "name: research\ninclude_tools:\n  - read_file\ncapabilities:\n  - analysis\n",
        encoding="utf-8",
    )
    (pack_dir / "context" / "research.yaml").write_text(
        "name: research\ninstructions: Keep context tight.\ndomains:\n  - research\n",
        encoding="utf-8",
    )
    (pack_dir / "prompts" / "review.md").write_text(
        "# Review Prompt\n\nUse this pack when reviewing artifacts.\n",
        encoding="utf-8",
    )
    (pack_dir / "automation" / "daily-brief.yaml").write_text(
        "name: daily-brief\ntrigger_type: webhook\nendpoint: /api/automation/webhooks/daily-brief\n",
        encoding="utf-8",
    )
    (pack_dir / "connectors" / "browser" / "browserbase.yaml").write_text(
        "name: browserbase\nprovider_kind: browserbase\nconfig_fields:\n  - key: api_key\n    label: API Key\n",
        encoding="utf-8",
    )
    (pack_dir / "connectors" / "messaging" / "telegram.yaml").write_text(
        "name: telegram\nplatform: telegram\ndelivery_modes:\n  - dm\n",
        encoding="utf-8",
    )
    (pack_dir / "speech" / "voice.yaml").write_text(
        "name: narrator\nprovider: openai\nsupports_tts: true\nvoice: alloy\n",
        encoding="utf-8",
    )
    (pack_dir / "connectors" / "nodes" / "companion.yaml").write_text(
        "name: companion\nadapter_kind: companion\nconfig_fields:\n  - key: node_url\n    label: Node URL\n    input: url\n",
        encoding="utf-8",
    )

    snapshot = ExtensionRegistry(
        manifest_roots=[str(tmp_path / "extensions")],
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    ).snapshot()

    extension = snapshot.get_extension("seraph.guardian-reach")
    assert extension is not None
    contributions = {item.contribution_type: item.metadata for item in extension.contributions}
    assert contributions["toolset_presets"]["include_tools"] == ["read_file"]
    assert contributions["context_packs"]["domains"] == ["research"]
    assert contributions["prompt_packs"]["name"] == "review-prompt"
    assert contributions["prompt_packs"]["title"] == "Review Prompt"
    assert contributions["automation_triggers"]["requires_network"] is True
    assert contributions["automation_triggers"]["endpoint"] == "/api/automation/webhooks/daily-brief"
    assert contributions["automation_triggers"]["config_fields"][0]["key"] == "signing_secret"
    assert contributions["browser_providers"]["provider_kind"] == "browserbase"
    assert contributions["messaging_connectors"]["platform"] == "telegram"
    assert contributions["speech_profiles"]["supports_tts"] is True
    assert contributions["node_adapters"]["requires_daemon"] is True


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


def test_registry_records_unreadable_manifest_errors(tmp_path: Path):
    bad_dir = tmp_path / "extensions" / "bad-utf8"
    bad_dir.mkdir(parents=True)
    (bad_dir / "manifest.yaml").write_bytes(b"\xff\xfe\x00")

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
    assert "not valid UTF-8" in snapshot.load_errors[0].message


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
    workspace_dir = tmp_path / "workspace"
    skills_dir = workspace_dir / "skills"
    skills_dir.mkdir(parents=True)

    legacy_skill_path = skills_dir / "research-briefing.md"
    legacy_skill_path.write_text(
        "---\nname: research-briefing\ndescription: Legacy skill\n---\n",
        encoding="utf-8",
    )
    (workspace_dir / "manifest.yaml").write_text(
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
    - skills/research-briefing.md
""".strip(),
        encoding="utf-8",
    )

    registry = ExtensionRegistry(
        manifest_roots=[str(workspace_dir)],
        skill_dirs=[str(skills_dir)],
        workflow_dirs=[],
        mcp_runtime=None,
        seraph_version="2026.3.19",
    )

    snapshot = registry.snapshot()

    assert snapshot.get_extension("seraph.research-briefing") is not None
    assert not any(item.id.startswith("legacy.skills.") for item in snapshot.extensions)


def test_registry_ignores_nested_contribution_manifests(tmp_path: Path):
    pack_dir = tmp_path / "extensions" / "research-pack"
    (pack_dir / "skills" / "examples").mkdir(parents=True)
    (pack_dir / "manifest.yaml").write_text(
        """
id: seraph.research-pack
version: 2026.3.23
display_name: Research Pack
kind: capability-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes:
  skills:
    - skills/research.md
""".strip(),
        encoding="utf-8",
    )
    (pack_dir / "skills" / "examples" / "manifest.yaml").write_text(
        """
id: seraph.nested-example
version: 2026.3.23
display_name: Nested Example
kind: capability-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes:
  skills:
    - skills/example.md
""".strip(),
        encoding="utf-8",
    )

    snapshot = ExtensionRegistry(
        manifest_roots=[str(tmp_path / "extensions")],
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    ).snapshot()

    assert snapshot.get_extension("seraph.research-pack") is not None
    assert snapshot.get_extension("seraph.nested-example") is None


def test_registry_prefers_manifest_backed_mcp_entries_over_matching_legacy_runtime(tmp_path: Path):
    pack_dir = tmp_path / "extensions" / "connector-pack"
    mcp_dir = pack_dir / "mcp"
    mcp_dir.mkdir(parents=True)
    (pack_dir / "manifest.yaml").write_text(
        """
id: seraph.connector-pack
version: 2026.3.21
display_name: Connector Pack
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
    (mcp_dir / "github.json").write_text(
        '{"name":"github","url":"https://example.test/mcp","description":"Packaged GitHub MCP"}',
        encoding="utf-8",
    )

    class StubMCPRuntime:
        _config_path = "/tmp/runtime-mcp.json"

        @staticmethod
        def get_config():
            return [
                {
                    "name": "github",
                    "url": "https://legacy.example.test/mcp",
                    "enabled": True,
                    "connected": True,
                    "status": "connected",
                    "tool_count": 2,
                }
            ]

    registry = ExtensionRegistry(
        manifest_roots=[str(tmp_path / "extensions")],
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=StubMCPRuntime(),
        seraph_version="2026.3.19",
    )

    snapshot = registry.snapshot()

    extension = snapshot.get_extension("seraph.connector-pack")
    assert extension is not None
    assert extension.contributions[0].metadata["name"] == "github"
    assert extension.contributions[0].metadata["url"] == "https://example.test/mcp"
    assert not any(item.id.startswith("legacy.mcp-runtime.") for item in snapshot.extensions)


def test_registry_duplicate_extension_ids_follow_manifest_root_precedence(tmp_path: Path):
    workspace_root = tmp_path / "workspace-roots"
    bundled_root = tmp_path / "bundled-roots"
    for root, label in ((workspace_root, "Workspace"), (bundled_root, "Bundled")):
        pack_dir = root / "duplicate-pack"
        (pack_dir / "context").mkdir(parents=True)
        (pack_dir / "manifest.yaml").write_text(
            f"""
id: seraph.duplicate-pack
version: 2026.3.23
display_name: {label} Duplicate
kind: capability-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes:
  context_packs:
    - context/research.yaml
""".strip(),
            encoding="utf-8",
        )
        (pack_dir / "context" / "research.yaml").write_text(
            f"name: research\ninstructions: keep {label.lower()} first\n",
            encoding="utf-8",
        )

    snapshot = ExtensionRegistry(
        manifest_roots=[str(workspace_root), str(bundled_root)],
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    ).snapshot()

    extension = snapshot.get_extension("seraph.duplicate-pack")
    assert extension is not None
    assert extension.display_name == "Workspace Duplicate"
    assert extension.metadata["manifest_root_index"] == 0


def test_registry_enriches_manifest_backed_managed_connector_metadata(tmp_path: Path):
    pack_dir = tmp_path / "extensions" / "managed-connector-pack"
    connector_dir = pack_dir / "connectors" / "managed"
    connector_dir.mkdir(parents=True)
    (pack_dir / "manifest.yaml").write_text(
        """
id: seraph.managed-github
version: 2026.3.21
display_name: Managed GitHub
kind: connector-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes:
  managed_connectors:
    - connectors/managed/github.yaml
permissions:
  network: true
""".strip(),
        encoding="utf-8",
    )
    (connector_dir / "github.yaml").write_text(
        """
name: github-managed
provider: github
description: Curated GitHub connector
auth_kind: oauth
capabilities:
  - pull_requests.read
  - issues.write
config_fields:
  - key: installation_id
    label: Installation ID
    required: true
  - key: api_base_url
    label: API Base URL
    required: false
    input: url
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

    extension = snapshot.get_extension("seraph.managed-github")
    assert extension is not None
    contribution = extension.contributions[0]
    assert contribution.contribution_type == "managed_connectors"
    assert contribution.metadata["name"] == "github-managed"
    assert contribution.metadata["provider"] == "github"
    assert contribution.metadata["auth_kind"] == "oauth"
    assert contribution.metadata["default_enabled"] is False
    assert contribution.metadata["capabilities"] == ["pull_requests.read", "issues.write"]
    assert contribution.metadata["config_fields"][1]["input"] == "url"


def test_registry_enriches_manifest_backed_observer_definition_metadata(tmp_path: Path):
    pack_dir = tmp_path / "extensions" / "observer-pack"
    observer_dir = pack_dir / "observers" / "definitions"
    observer_dir.mkdir(parents=True)
    (pack_dir / "manifest.yaml").write_text(
        """
id: seraph.core-observer-sources
version: 2026.3.21
display_name: Core Observer Sources
kind: capability-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes:
  observer_definitions:
    - observers/definitions/calendar.yaml
permissions:
  network: true
""".strip(),
        encoding="utf-8",
    )
    (observer_dir / "calendar.yaml").write_text(
        """
name: calendar
source_type: calendar
description: Curated calendar observer
enabled: true
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

    extension = snapshot.get_extension("seraph.core-observer-sources")
    assert extension is not None
    contribution = extension.contributions[0]
    assert contribution.contribution_type == "observer_definitions"
    assert contribution.metadata["name"] == "calendar"
    assert contribution.metadata["source_type"] == "calendar"
    assert contribution.metadata["default_enabled"] is True
    assert contribution.metadata["requires_network"] is True


def test_active_observer_definitions_prefer_lower_manifest_root_index(tmp_path: Path):
    workspace_root = tmp_path / "workspace-extensions"
    bundled_root = tmp_path / "bundled-extensions"
    workspace_pack = workspace_root / "calendar-override"
    bundled_pack = bundled_root / "core-observer-sources"
    (workspace_pack / "observers" / "definitions").mkdir(parents=True)
    (bundled_pack / "observers" / "definitions").mkdir(parents=True)

    (workspace_pack / "manifest.yaml").write_text(
        """
id: seraph.workspace-calendar
version: 2026.3.21
display_name: Workspace Calendar
kind: capability-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
permissions:
  network: true
contributes:
  observer_definitions:
    - observers/definitions/calendar.yaml
""".strip(),
        encoding="utf-8",
    )
    (workspace_pack / "observers" / "definitions" / "calendar.yaml").write_text(
        """
name: workspace-calendar
source_type: calendar
description: Workspace calendar override
enabled: true
""".strip(),
        encoding="utf-8",
    )

    (bundled_pack / "manifest.yaml").write_text(
        """
id: seraph.core-observer-sources
version: 2026.3.21
display_name: Core Observer Sources
kind: capability-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: bundled
permissions:
  network: true
contributes:
  observer_definitions:
    - observers/definitions/calendar.yaml
""".strip(),
        encoding="utf-8",
    )
    (bundled_pack / "observers" / "definitions" / "calendar.yaml").write_text(
        """
name: bundled-calendar
source_type: calendar
description: Bundled calendar observer
enabled: true
""".strip(),
        encoding="utf-8",
    )

    registry = ExtensionRegistry(
        manifest_roots=[str(workspace_root), str(bundled_root)],
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
        seraph_version="2026.3.19",
    )

    snapshot = registry.snapshot()
    active = select_active_observer_definitions(snapshot.list_contributions("observer_definitions"))

    assert len(active) == 1
    assert active[0].name == "workspace-calendar"
    assert active[0].manifest_root_index == 0


def test_registry_enriches_manifest_backed_channel_adapter_metadata(tmp_path: Path):
    pack_dir = tmp_path / "extensions" / "channel-pack"
    channel_dir = pack_dir / "channels"
    channel_dir.mkdir(parents=True)
    (pack_dir / "manifest.yaml").write_text(
        """
id: seraph.core-channel-adapters
version: 2026.3.21
display_name: Core Channel Adapters
kind: connector-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes:
  channel_adapters:
    - channels/websocket.yaml
""".strip(),
        encoding="utf-8",
    )
    (channel_dir / "websocket.yaml").write_text(
        """
name: browser-websocket
transport: websocket
description: Browser delivery adapter
enabled: true
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

    extension = snapshot.get_extension("seraph.core-channel-adapters")
    assert extension is not None
    contribution = extension.contributions[0]
    assert contribution.contribution_type == "channel_adapters"
    assert contribution.metadata["name"] == "browser-websocket"
    assert contribution.metadata["transport"] == "websocket"
    assert contribution.metadata["default_enabled"] is True
    assert contribution.metadata["requires_daemon"] is False


def test_active_channel_adapters_prefer_lower_manifest_root_index(tmp_path: Path):
    workspace_root = tmp_path / "workspace-extensions"
    bundled_root = tmp_path / "bundled-extensions"
    workspace_pack = workspace_root / "native-override"
    bundled_pack = bundled_root / "core-channel-adapters"
    (workspace_pack / "channels").mkdir(parents=True)
    (bundled_pack / "channels").mkdir(parents=True)

    (workspace_pack / "manifest.yaml").write_text(
        """
id: seraph.workspace-channel
version: 2026.3.21
display_name: Workspace Channel
kind: connector-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes:
  channel_adapters:
    - channels/native.yaml
""".strip(),
        encoding="utf-8",
    )
    (workspace_pack / "channels" / "native.yaml").write_text(
        """
name: workspace-native
transport: native_notification
description: Workspace native adapter
enabled: true
""".strip(),
        encoding="utf-8",
    )

    (bundled_pack / "manifest.yaml").write_text(
        """
id: seraph.core-channel-adapters
version: 2026.3.21
display_name: Core Channel Adapters
kind: connector-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: bundled
contributes:
  channel_adapters:
    - channels/native.yaml
""".strip(),
        encoding="utf-8",
    )
    (bundled_pack / "channels" / "native.yaml").write_text(
        """
name: bundled-native
transport: native_notification
description: Bundled native adapter
enabled: true
""".strip(),
        encoding="utf-8",
    )

    registry = ExtensionRegistry(
        manifest_roots=[str(workspace_root), str(bundled_root)],
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
        seraph_version="2026.3.19",
    )

    snapshot = registry.snapshot()
    active = select_active_channel_adapters(snapshot.list_contributions("channel_adapters"))

    assert len(active) == 1
    assert active[0].name == "workspace-native"
    assert active[0].manifest_root_index == 0


def test_registry_reports_layout_error_for_symlink_escape(tmp_path: Path):
    package_dir = tmp_path / "extensions" / "escape-pack"
    skills_dir = package_dir / "skills"
    external_dir = tmp_path / "external"
    package_dir.mkdir(parents=True)
    skills_dir.mkdir()
    external_dir.mkdir()
    (package_dir / "manifest.yaml").write_text(
        """
id: seraph.escape-pack
version: 2026.3.21
display_name: Escape Pack
kind: capability-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes:
  skills:
    - skills/escape.md
""".strip(),
        encoding="utf-8",
    )
    external_file = external_dir / "outside.md"
    external_file.write_text("---\nname: outside\ndescription: Outside\n---\n", encoding="utf-8")
    (skills_dir / "escape.md").symlink_to(external_file)

    registry = ExtensionRegistry(
        manifest_roots=[str(tmp_path / "extensions")],
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
        seraph_version="2026.3.19",
    )

    snapshot = registry.snapshot()

    assert snapshot.get_extension("seraph.escape-pack") is None
    assert len(snapshot.load_errors) == 1
    assert snapshot.load_errors[0].phase == "layout"
    assert "escapes the package root" in snapshot.load_errors[0].message
