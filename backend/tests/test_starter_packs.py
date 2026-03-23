from pathlib import Path

from src.starter_packs.manager import StarterPackManager


def _write_manifest_starter_pack(
    root: Path,
    *,
    package_name: str = "research-pack",
    extension_id: str = "seraph.research-pack",
    pack_file_name: str = "research.json",
    pack_name: str = "research-briefing",
    label: str = "Research Briefing",
) -> None:
    package_dir = root / "extensions" / package_name
    starter_packs_dir = package_dir / "starter-packs"
    starter_packs_dir.mkdir(parents=True, exist_ok=True)
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
        f"  starter_packs:\n    - starter-packs/{pack_file_name}\n"
        "permissions:\n"
        "  tools: []\n"
        "  network: false\n",
        encoding="utf-8",
    )
    (starter_packs_dir / pack_file_name).write_text(
        "{\n"
        f'  "name": "{pack_name}",\n'
        f'  "label": "{label}",\n'
        '  "description": "Packaged starter pack.",\n'
        '  "skills": ["web-briefing"],\n'
        '  "workflows": ["web-brief-to-file"],\n'
        '  "install_items": ["http-request"],\n'
        '  "sample_prompt": "Prepare a research brief."\n'
        "}\n",
        encoding="utf-8",
    )


def test_init_loads_manifest_backed_starter_packs_alongside_legacy_packs(tmp_path: Path):
    legacy_path = tmp_path / "starter-packs.json"
    legacy_path.write_text(
        '{\n  "packs": [\n    {\n      "name": "legacy-pack",\n      "label": "Legacy Pack",\n      "description": "Legacy starter pack.",\n      "skills": [],\n      "workflows": []\n    }\n  ]\n}\n',
        encoding="utf-8",
    )
    _write_manifest_starter_pack(tmp_path)

    mgr = StarterPackManager()
    mgr.init(str(legacy_path), manifest_roots=[str(tmp_path / "extensions")])

    listed = {pack["name"]: pack for pack in mgr.list_packs()}
    assert set(listed) == {"legacy-pack", "research-briefing"}
    assert listed["legacy-pack"]["source"] == "legacy"
    assert listed["legacy-pack"]["extension_id"].startswith("legacy.starter_packs.")
    assert listed["research-briefing"]["source"] == "manifest"
    assert listed["research-briefing"]["extension_id"] == "seraph.research-pack"


def test_manifest_backed_starter_pack_names_win_duplicate_collisions(tmp_path: Path):
    legacy_path = tmp_path / "starter-packs.json"
    legacy_path.write_text(
        '{\n  "packs": [\n    {\n      "name": "research-briefing",\n      "label": "Legacy Pack",\n      "description": "Legacy starter pack.",\n      "skills": [],\n      "workflows": []\n    }\n  ]\n}\n',
        encoding="utf-8",
    )
    _write_manifest_starter_pack(tmp_path, label="Manifest Pack")

    mgr = StarterPackManager()
    mgr.init(str(legacy_path), manifest_roots=[str(tmp_path / "extensions")])

    assert [pack["name"] for pack in mgr.list_packs()] == ["research-briefing"]
    selected = mgr.get_pack("research-briefing")
    assert selected is not None
    assert selected.source == "manifest"
    assert selected.label == "Manifest Pack"
    assert any(error["phase"] == "duplicate-starter-pack-name" for error in mgr.get_diagnostics()["load_errors"])


def test_init_defaults_manifest_root_to_workspace_extensions(tmp_path: Path):
    workspace_dir = tmp_path / "workspace"
    legacy_path = workspace_dir / "starter-packs.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"packs": []}\n', encoding="utf-8")
    _write_manifest_starter_pack(workspace_dir)

    mgr = StarterPackManager()
    mgr.init(str(legacy_path))

    listed = {pack["name"]: pack for pack in mgr.list_packs()}
    assert "research-briefing" in listed
    assert listed["research-briefing"]["source"] == "manifest"


def test_manifest_root_errors_can_surface_as_shared_starter_pack_diagnostics(tmp_path: Path):
    legacy_path = tmp_path / "workspace" / "starter-packs.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"packs": []}\n', encoding="utf-8")

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

    mgr = StarterPackManager()
    mgr.init(str(legacy_path), manifest_roots=[str(tmp_path / "extensions")])

    diagnostics = mgr.get_diagnostics()
    assert diagnostics["load_errors"] == []
    assert diagnostics["shared_error_count"] == 1
    assert diagnostics["shared_manifest_errors"][0]["phase"] == "manifest"


def test_incompatible_manifest_backed_starter_pack_surfaces_typed_load_error(tmp_path: Path):
    legacy_path = tmp_path / "workspace" / "starter-packs.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text('{"packs": []}\n', encoding="utf-8")

    package_dir = tmp_path / "extensions" / "future-pack"
    (package_dir / "starter-packs").mkdir(parents=True)
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
        "  starter_packs:\n"
        "    - starter-packs/research.json\n"
        "permissions:\n"
        "  tools: []\n"
        "  network: false\n",
        encoding="utf-8",
    )

    mgr = StarterPackManager()
    mgr.init(str(legacy_path), manifest_roots=[str(tmp_path / "extensions")])

    diagnostics = mgr.get_diagnostics()
    assert diagnostics["loaded_count"] == 0
    assert diagnostics["error_count"] == 1
    assert diagnostics["load_errors"][0]["phase"] == "compatibility"
    assert diagnostics["shared_error_count"] == 0
