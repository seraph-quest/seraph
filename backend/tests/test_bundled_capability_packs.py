from pathlib import Path

from src.extensions.registry import bundled_manifest_root, default_manifest_roots_for_workspace
from src.extensions.scaffold import validate_extension_package
from src.runbooks.manager import RunbookManager
from src.skills.manager import SkillManager
from src.starter_packs.manager import StarterPackManager
from src.workflows.manager import WorkflowManager


def _write_override_pack(workspace_dir: Path) -> Path:
    package_dir = workspace_dir / "extensions" / "local-overrides"
    (package_dir / "skills").mkdir(parents=True)
    (package_dir / "workflows").mkdir(parents=True)
    (package_dir / "runbooks").mkdir(parents=True)
    (package_dir / "starter-packs").mkdir(parents=True)
    (package_dir / "manifest.yaml").write_text(
        "id: seraph.local-overrides\n"
        "version: 2026.3.21\n"
        "display_name: Local Overrides\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.4.10\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  skills:\n"
        "    - skills/daily-standup.md\n"
        "  workflows:\n"
        "    - workflows/web-brief-to-file.md\n"
        "  runbooks:\n"
        "    - runbooks/research-briefing.yaml\n"
        "  starter_packs:\n"
        "    - starter-packs/research-briefing.json\n"
        "permissions:\n"
        "  tools: [read_file, web_search, write_file]\n"
        "  network: true\n",
        encoding="utf-8",
    )
    (package_dir / "skills" / "daily-standup.md").write_text(
        "---\n"
        "name: daily-standup\n"
        "description: Local daily standup override\n"
        "requires:\n"
        "  tools: []\n"
        "user_invocable: true\n"
        "---\n\n"
        "Use the local override instead of the bundled default.\n",
        encoding="utf-8",
    )
    (package_dir / "workflows" / "web-brief-to-file.md").write_text(
        "---\n"
        "name: web-brief-to-file\n"
        "description: Local workflow override\n"
        "requires:\n"
        "  tools: [web_search, write_file]\n"
        "steps:\n"
        "  - id: search\n"
        "    tool: web_search\n"
        "    arguments:\n"
        "      query: test\n"
        "  - id: save\n"
        "    tool: write_file\n"
        "    arguments:\n"
        "      file_path: notes/local.md\n"
        "      content: local\n"
        "---\n\n"
        "Override workflow.\n",
        encoding="utf-8",
    )
    (package_dir / "runbooks" / "research-briefing.yaml").write_text(
        "id: runbook:research-briefing\n"
        "title: Local Research Briefing\n"
        "summary: Local runbook override.\n"
        "starter_pack: research-briefing\n",
        encoding="utf-8",
    )
    (package_dir / "starter-packs" / "research-briefing.json").write_text(
        "{\n"
        '  "name": "research-briefing",\n'
        '  "label": "Local Research Briefing",\n'
        '  "description": "Local starter pack override.",\n'
        '  "skills": ["daily-standup"],\n'
        '  "workflows": ["web-brief-to-file"],\n'
        '  "install_items": [],\n'
        '  "sample_prompt": "Use the local override."\n'
        "}\n",
        encoding="utf-8",
    )
    return package_dir


def test_core_capabilities_bundle_validates_cleanly():
    package_root = Path(__file__).resolve().parents[1] / "src" / "defaults" / "extensions" / "core-capabilities"

    report = validate_extension_package(package_root)

    assert report.ok is True
    assert report.load_errors == []
    assert [result.extension_id for result in report.results] == ["seraph.core-capabilities"]
    assert report.results[0].issues == []


def test_default_manifest_roots_include_workspace_and_bundled_root(tmp_path: Path):
    workspace_dir = tmp_path / "workspace"

    roots = default_manifest_roots_for_workspace(str(workspace_dir))

    assert roots == [str(workspace_dir / "extensions"), bundled_manifest_root()]


def test_bundled_core_capabilities_load_through_runtime_manifest_roots(tmp_path: Path):
    workspace_dir = tmp_path / "workspace"
    skills_dir = workspace_dir / "skills"
    workflows_dir = workspace_dir / "workflows"
    runbooks_dir = workspace_dir / "runbooks"
    starter_packs_path = workspace_dir / "starter-packs.json"
    skills_dir.mkdir(parents=True)
    workflows_dir.mkdir()
    runbooks_dir.mkdir()
    manifest_roots = default_manifest_roots_for_workspace(str(workspace_dir))

    skill_manager = SkillManager()
    skill_manager.init(str(skills_dir), manifest_roots=manifest_roots)
    workflow_manager = WorkflowManager()
    workflow_manager.init(str(workflows_dir), manifest_roots=manifest_roots)
    runbook_manager = RunbookManager()
    runbook_manager.init(str(runbooks_dir), manifest_roots=manifest_roots)
    starter_pack_manager = StarterPackManager()
    starter_pack_manager.init(str(starter_packs_path), manifest_roots=manifest_roots)

    bundled_skill = skill_manager.get_skill("daily-standup")
    assert bundled_skill is not None
    assert bundled_skill.source == "manifest"
    assert bundled_skill.extension_id == "seraph.core-capabilities"

    source_daily_skill = skill_manager.get_skill("source-daily-review")
    assert source_daily_skill is not None
    assert source_daily_skill.source == "manifest"
    assert source_daily_skill.extension_id == "seraph.core-capabilities"

    bundled_workflow = workflow_manager.get_workflow("web-brief-to-file")
    assert bundled_workflow is not None
    assert bundled_workflow.source == "manifest"
    assert bundled_workflow.extension_id == "seraph.core-capabilities"

    bundled_runbook = runbook_manager.get_runbook("runbook:research-briefing")
    assert bundled_runbook is not None
    assert bundled_runbook.source == "manifest"
    assert bundled_runbook.extension_id == "seraph.core-capabilities"

    source_daily_runbook = runbook_manager.get_runbook("runbook:source-daily-review")
    assert source_daily_runbook is not None
    assert source_daily_runbook.source == "manifest"
    assert source_daily_runbook.extension_id == "seraph.core-capabilities"

    bundled_pack = starter_pack_manager.get_pack("research-briefing")
    assert bundled_pack is not None
    assert bundled_pack.source == "manifest"
    assert bundled_pack.extension_id == "seraph.core-capabilities"

    source_daily_pack = starter_pack_manager.get_pack("source-daily-review")
    assert source_daily_pack is not None
    assert source_daily_pack.source == "manifest"
    assert source_daily_pack.extension_id == "seraph.core-capabilities"


def test_workspace_manifest_packages_override_bundled_core_capabilities(tmp_path: Path):
    workspace_dir = tmp_path / "workspace"
    skills_dir = workspace_dir / "skills"
    workflows_dir = workspace_dir / "workflows"
    runbooks_dir = workspace_dir / "runbooks"
    starter_packs_path = workspace_dir / "starter-packs.json"
    skills_dir.mkdir(parents=True)
    workflows_dir.mkdir()
    runbooks_dir.mkdir()
    _write_override_pack(workspace_dir)
    manifest_roots = default_manifest_roots_for_workspace(str(workspace_dir))

    skill_manager = SkillManager()
    skill_manager.init(str(skills_dir), manifest_roots=manifest_roots)
    workflow_manager = WorkflowManager()
    workflow_manager.init(str(workflows_dir), manifest_roots=manifest_roots)
    runbook_manager = RunbookManager()
    runbook_manager.init(str(runbooks_dir), manifest_roots=manifest_roots)
    starter_pack_manager = StarterPackManager()
    starter_pack_manager.init(str(starter_packs_path), manifest_roots=manifest_roots)

    bundled_skill = skill_manager.get_skill("daily-standup")
    assert bundled_skill is not None
    assert bundled_skill.description == "Local daily standup override"
    assert bundled_skill.extension_id == "seraph.local-overrides"

    bundled_workflow = workflow_manager.get_workflow("web-brief-to-file")
    assert bundled_workflow is not None
    assert bundled_workflow.description == "Local workflow override"
    assert bundled_workflow.extension_id == "seraph.local-overrides"

    bundled_runbook = runbook_manager.get_runbook("runbook:research-briefing")
    assert bundled_runbook is not None
    assert bundled_runbook.title == "Local Research Briefing"
    assert bundled_runbook.extension_id == "seraph.local-overrides"

    bundled_pack = starter_pack_manager.get_pack("research-briefing")
    assert bundled_pack is not None
    assert bundled_pack.label == "Local Research Briefing"
    assert bundled_pack.extension_id == "seraph.local-overrides"
