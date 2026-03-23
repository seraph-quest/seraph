from pathlib import Path

from src.extensions.scaffold import scaffold_extension_package, validate_extension_package


def _read_package_contents(package_root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(package_root)): path.read_text(encoding="utf-8")
        for path in sorted(package_root.rglob("*"))
        if path.is_file()
    }


def test_research_pack_example_validates_cleanly():
    repo_root = Path(__file__).resolve().parents[2]
    package_root = repo_root / "examples" / "extensions" / "research-pack"

    report = validate_extension_package(package_root)

    assert report.ok is True
    assert report.load_errors == []
    assert [result.extension_id for result in report.results] == ["seraph.research-pack"]
    assert report.results[0].issues == []


def test_research_pack_example_matches_current_scaffold_output(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    example_root = repo_root / "examples" / "extensions" / "research-pack"

    scaffold_extension_package(
        tmp_path / "research-pack",
        extension_id="seraph.research-pack",
        display_name="Research Pack",
        contributions=["skills", "workflows", "runbooks"],
        version="2026.3.21",
    )

    assert _read_package_contents(tmp_path / "research-pack") == _read_package_contents(example_root)
