from pathlib import Path

from src.extensions.doctor import doctor_extension, doctor_snapshot
from src.extensions.registry import ExtensionRegistry


def test_doctor_reports_missing_contribution_files(tmp_path: Path):
    pack_dir = tmp_path / "extensions" / "missing-files"
    pack_dir.mkdir(parents=True)
    (pack_dir / "manifest.yaml").write_text(
        """
id: seraph.missing-files
version: 2026.3.21
display_name: Missing Files
kind: capability-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes:
  skills:
    - skills/missing.md
permissions:
  tools:
    - web_search
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
    extension = snapshot.get_extension("seraph.missing-files")
    result = doctor_extension(extension)

    assert result.ok is False
    assert result.issues[0].code == "missing_reference"


def test_doctor_reports_skill_permission_mismatch(tmp_path: Path):
    pack_dir = tmp_path / "extensions" / "skill-pack"
    skill_dir = pack_dir / "skills"
    skill_dir.mkdir(parents=True)
    (pack_dir / "manifest.yaml").write_text(
        """
id: seraph.skill-pack
version: 2026.3.21
display_name: Skill Pack
kind: capability-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes:
  skills:
    - skills/web-briefing.md
permissions:
  tools:
    - read_file
""".strip(),
        encoding="utf-8",
    )
    (skill_dir / "web-briefing.md").write_text(
        "---\n"
        "name: web-briefing\n"
        "description: Research helper\n"
        "requires:\n"
        "  tools: [web_search]\n"
        "---\n\n"
        "Use web_search.\n",
        encoding="utf-8",
    )

    registry = ExtensionRegistry(
        manifest_roots=[str(tmp_path / "extensions")],
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
        seraph_version="2026.3.19",
    )

    result = doctor_extension(registry.snapshot().get_extension("seraph.skill-pack"))

    assert result.ok is False
    assert result.issues[0].code == "permission_mismatch"
    assert "web_search" in result.issues[0].message


def test_doctor_reports_unreadable_contribution_files(tmp_path: Path):
    pack_dir = tmp_path / "extensions" / "unreadable-pack"
    skill_dir = pack_dir / "skills"
    skill_dir.mkdir(parents=True)
    (pack_dir / "manifest.yaml").write_text(
        """
id: seraph.unreadable-pack
version: 2026.3.21
display_name: Unreadable Pack
kind: capability-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes:
  skills:
    - skills/binary.md
permissions:
  tools:
    - web_search
""".strip(),
        encoding="utf-8",
    )
    (skill_dir / "binary.md").write_bytes(b"\xff\xfe\x00")

    registry = ExtensionRegistry(
        manifest_roots=[str(tmp_path / "extensions")],
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
        seraph_version="2026.3.19",
    )

    result = doctor_extension(registry.snapshot().get_extension("seraph.unreadable-pack"))

    assert result.ok is False
    assert result.issues[0].code == "unreadable_contribution"


def test_doctor_reports_workflow_permission_mismatch(tmp_path: Path):
    pack_dir = tmp_path / "extensions" / "workflow-pack"
    workflow_dir = pack_dir / "workflows"
    workflow_dir.mkdir(parents=True)
    (pack_dir / "manifest.yaml").write_text(
        """
id: seraph.workflow-pack
version: 2026.3.21
display_name: Workflow Pack
kind: capability-pack
compatibility:
  seraph: ">=2026.3.19"
publisher:
  name: Seraph
trust: local
contributes:
  workflows:
    - workflows/web-brief.md
permissions:
  tools:
    - web_search
""".strip(),
        encoding="utf-8",
    )
    (workflow_dir / "web-brief.md").write_text(
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
        manifest_roots=[str(tmp_path / "extensions")],
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
        seraph_version="2026.3.19",
    )

    result = doctor_extension(registry.snapshot().get_extension("seraph.workflow-pack"))

    assert result.ok is False
    assert result.issues[0].code == "permission_mismatch"
    assert "write_file" in result.issues[0].message


def test_doctor_reports_connector_network_permission_mismatch(tmp_path: Path):
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
  network: false
""".strip(),
        encoding="utf-8",
    )
    (mcp_dir / "github.json").write_text(
        '{"name":"github","url":"https://api.githubcopilot.com/mcp/","transport":"streamable-http"}',
        encoding="utf-8",
    )

    registry = ExtensionRegistry(
        manifest_roots=[str(tmp_path / "extensions")],
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
        seraph_version="2026.3.19",
    )

    result = doctor_extension(registry.snapshot().get_extension("seraph.connector-pack"))

    assert result.ok is False
    assert result.issues[0].code == "permission_mismatch"
    assert "network transport" in result.issues[0].message


def test_doctor_reports_invalid_connector_payload(tmp_path: Path):
    pack_dir = tmp_path / "extensions" / "invalid-connector-pack"
    mcp_dir = pack_dir / "mcp"
    mcp_dir.mkdir(parents=True)
    (pack_dir / "manifest.yaml").write_text(
        """
id: seraph.invalid-connector-pack
version: 2026.3.21
display_name: Invalid Connector Pack
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
    (mcp_dir / "github.json").write_text("{not-json", encoding="utf-8")

    registry = ExtensionRegistry(
        manifest_roots=[str(tmp_path / "extensions")],
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
        seraph_version="2026.3.19",
    )

    result = doctor_extension(registry.snapshot().get_extension("seraph.invalid-connector-pack"))

    assert result.ok is False
    assert result.issues[0].code == "invalid_connector"


def test_doctor_snapshot_preserves_registry_load_errors(tmp_path: Path):
    pack_dir = tmp_path / "extensions" / "bad"
    pack_dir.mkdir(parents=True)
    (pack_dir / "manifest.yaml").write_text(
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
        seraph_version="2026.3.19",
    )

    report = doctor_snapshot(registry.snapshot())

    assert report.ok is False
    assert len(report.load_errors) == 1
    assert report.load_errors[0].phase == "manifest"
