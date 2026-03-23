from pathlib import Path

from src.runbooks.manager import RunbookManager


def _write_manifest_runbook(
    root: Path,
    *,
    package_name: str = "research-pack",
    extension_id: str = "seraph.research-pack",
    runbook_file_name: str = "research.yaml",
    runbook_id: str = "runbook:research-briefing",
    title: str = "Research Briefing",
) -> None:
    package_dir = root / "extensions" / package_name
    runbooks_dir = package_dir / "runbooks"
    runbooks_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "manifest.yaml").write_text(
        "id: " + extension_id + "\n"
        "version: 2026.3.21\n"
        "display_name: Research Pack\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.3.19\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        f"  runbooks:\n    - runbooks/{runbook_file_name}\n"
        "permissions:\n"
        "  tools: []\n"
        "  network: false\n",
        encoding="utf-8",
    )
    (runbooks_dir / runbook_file_name).write_text(
        f"id: {runbook_id}\n"
        f"title: {title}\n"
        "summary: Packaged runbook.\n"
        "workflow: web-brief-to-file\n",
        encoding="utf-8",
    )


def test_init_loads_manifest_backed_runbooks_alongside_legacy_runbooks(tmp_path: Path):
    runbooks_dir = tmp_path / "runbooks"
    runbooks_dir.mkdir()
    (runbooks_dir / "legacy.yaml").write_text(
        "id: runbook:legacy\n"
        "title: Legacy Runbook\n"
        "summary: Legacy runbook.\n"
        "command: Start a legacy flow.\n",
        encoding="utf-8",
    )
    _write_manifest_runbook(tmp_path)

    mgr = RunbookManager()
    mgr.init(str(runbooks_dir), manifest_roots=[str(tmp_path / "extensions")])

    listed = {runbook["id"]: runbook for runbook in mgr.list_runbooks()}
    assert set(listed) == {"runbook:legacy", "runbook:research-briefing"}
    assert listed["runbook:legacy"]["source"] == "legacy"
    assert listed["runbook:legacy"]["extension_id"].startswith("legacy.runbooks.")
    assert listed["runbook:research-briefing"]["source"] == "manifest"
    assert listed["runbook:research-briefing"]["extension_id"] == "seraph.research-pack"


def test_manifest_backed_runbook_ids_win_duplicate_collisions(tmp_path: Path):
    runbooks_dir = tmp_path / "runbooks"
    runbooks_dir.mkdir()
    (runbooks_dir / "legacy.yaml").write_text(
        "id: runbook:research-briefing\n"
        "title: Legacy Runbook\n"
        "summary: Legacy runbook.\n"
        "command: Start a legacy flow.\n",
        encoding="utf-8",
    )
    _write_manifest_runbook(tmp_path, title="Manifest Runbook")

    mgr = RunbookManager()
    mgr.init(str(runbooks_dir), manifest_roots=[str(tmp_path / "extensions")])

    assert [runbook["id"] for runbook in mgr.list_runbooks()] == ["runbook:research-briefing"]
    selected = mgr.get_runbook("runbook:research-briefing")
    assert selected is not None
    assert selected.source == "manifest"
    assert selected.title == "Manifest Runbook"
    assert any(error["phase"] == "duplicate-runbook-id" for error in mgr.get_diagnostics()["load_errors"])


def test_init_defaults_manifest_root_to_workspace_extensions(tmp_path: Path):
    workspace_dir = tmp_path / "workspace"
    runbooks_dir = workspace_dir / "runbooks"
    runbooks_dir.mkdir(parents=True, exist_ok=True)
    _write_manifest_runbook(workspace_dir)

    mgr = RunbookManager()
    mgr.init(str(runbooks_dir))

    listed = {runbook["id"]: runbook for runbook in mgr.list_runbooks()}
    assert "runbook:research-briefing" in listed
    assert listed["runbook:research-briefing"]["source"] == "manifest"


def test_manifest_root_errors_can_surface_as_shared_runbook_diagnostics(tmp_path: Path):
    runbooks_dir = tmp_path / "runbooks"
    runbooks_dir.mkdir()

    bad_dir = tmp_path / "extensions" / "broken-pack"
    bad_dir.mkdir(parents=True)
    (bad_dir / "manifest.yaml").write_text(
        "id: seraph.broken-pack\n"
        "version: 2026.3.21\n"
        "display_name: Broken Pack\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.3.19\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "permissions:\n"
        "  tools: []\n"
        "  network: false\n",
        encoding="utf-8",
    )

    mgr = RunbookManager()
    mgr.init(str(runbooks_dir), manifest_roots=[str(tmp_path / "extensions")])

    diagnostics = mgr.get_diagnostics()
    assert diagnostics["load_errors"] == []
    assert diagnostics["shared_error_count"] == 1
    assert diagnostics["shared_manifest_errors"][0]["phase"] == "manifest"


def test_incompatible_manifest_backed_runbook_surfaces_typed_load_error(tmp_path: Path):
    runbooks_dir = tmp_path / "runbooks"
    runbooks_dir.mkdir()

    package_dir = tmp_path / "extensions" / "future-pack"
    (package_dir / "runbooks").mkdir(parents=True)
    (package_dir / "manifest.yaml").write_text(
        "id: seraph.future-pack\n"
        "version: 2026.3.21\n"
        "display_name: Future Pack\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2027.1\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  runbooks:\n"
        "    - runbooks/research.yaml\n"
        "permissions:\n"
        "  tools: []\n"
        "  network: false\n",
        encoding="utf-8",
    )

    mgr = RunbookManager()
    mgr.init(str(runbooks_dir), manifest_roots=[str(tmp_path / "extensions")])

    diagnostics = mgr.get_diagnostics()
    assert diagnostics["loaded_count"] == 0
    assert diagnostics["error_count"] == 1
    assert diagnostics["load_errors"][0]["phase"] == "compatibility"
    assert diagnostics["shared_error_count"] == 0
