from __future__ import annotations

import subprocess
import sys
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CHECK = ROOT / "scripts/check_docs_contract.py"


def load_checker():
    spec = importlib.util.spec_from_file_location("check_docs_contract", CHECK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_check(*paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK), *(str(path) for path in paths)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_current_docs_satisfy_canonical_contract() -> None:
    result = run_check()
    assert result.returncode == 0, result.stderr
    assert "docs contract: ok" in result.stdout


def test_contract_rejects_external_agent_as_seraph_runtime(tmp_path: Path) -> None:
    contradiction = tmp_path / "entry.md"
    contradiction.write_text("Seraph uses Codex CLI as its runtime.\n", encoding="utf-8")

    result = run_check(contradiction)

    assert result.returncode == 1
    assert "forbidden target/runtime contradiction" in result.stderr


def test_contract_rejects_competing_research_target_and_target_capability_state(tmp_path: Path) -> None:
    contradiction = tmp_path / "ownership.md"
    contradiction.write_text(
        "The research tree defines the canonical target.\n\n**Target:** voice channel.\n",
        encoding="utf-8",
    )

    result = run_check(contradiction)

    assert result.returncode == 1
    assert result.stderr.count("forbidden target/runtime contradiction") == 2


def test_contract_rejects_world_class_strategy_authority_phrasings(tmp_path: Path) -> None:
    contradiction = tmp_path / "strategy.md"
    contradiction.write_text(
        "Canonical strategy source of truth: Research 17.\n"
        "Research docs own product strategy and milestone order.\n"
        "Docs own strategy, milestone definitions, and acceptance rules.\n",
        encoding="utf-8",
    )

    result = run_check(contradiction)

    assert result.returncode == 1
    assert result.stderr.count("forbidden target/runtime contradiction") == 3


def test_use_case_contract_requires_exact_identical_five_item_block(tmp_path: Path) -> None:
    checker = load_checker()
    valid = tmp_path / "valid.md"
    valid.write_text(
        checker.USE_CASE_START
        + "\n"
        + "\n".join(checker.EXPECTED_USE_CASES)
        + "\n"
        + checker.USE_CASE_END,
        encoding="utf-8",
    )
    changed = tmp_path / "changed.md"
    changed.write_text(valid.read_text(encoding="utf-8").replace("prioritized", "helpful"), encoding="utf-8")

    assert checker.check_mirrored_use_cases([valid]) == []
    assert "exact five mirrored" in checker.check_mirrored_use_cases([changed])[0]


def test_archived_stub_rejects_embedded_operating_prompt(tmp_path: Path) -> None:
    checker = load_checker()
    archived = tmp_path / "archive.md"
    archived.write_text(
        "**State:** Archived\n\nGit history for closed #475.\n\n## Ground Rules\n\n- [ ] Execute phase one\n",
        encoding="utf-8",
    )

    errors = checker.check_archive_stub(archived)

    assert any("operational archive marker" in error for error in errors)


def test_contract_machine_checks_route_and_adr_ownership() -> None:
    checker = load_checker()
    source = CHECK.read_text(encoding="utf-8")
    assert "duplicate explicit implementation slug" in source
    assert "sole implementation '/' owner" in source
    assert "ADR-{index:03d}" in source
    assert "docs/sidebars.implementation.ts" in source
    assert len(checker.ARCHIVED_STUBS) == 4
    assert len(checker.STABLE_PUBLIC_MARKERS) == 5
    assert checker.README_ONLY_PUBLIC_MARKERS == (
        "https://github.com/seraph-quest/seraph/releases/latest",
    )


def test_active_sidebars_resolve_to_scanned_source_documents(tmp_path: Path) -> None:
    checker = load_checker()
    active_docs, errors = checker.active_sidebar_documents()

    assert errors == []
    assert ROOT / "docs/research/17-seraph-world-class-strategy.md" in active_docs
    assert ROOT / "docs/implementation/11-world-class-strategy-delivery.md" in active_docs
    assert ROOT / "docs/implementation/00-master-roadmap.md" not in active_docs

    sidebar = tmp_path / "sidebars.ts"
    source = tmp_path / "source"
    source.mkdir()
    sidebar.write_text("const x = {items: ['missing-doc']};\n", encoding="utf-8")
    _, unresolved = checker.resolve_sidebar_documents(sidebar, source)
    assert "unresolved active doc id 'missing-doc'" in unresolved[0]


def test_constitution_and_ci_contract_do_not_pin_volatile_release_media() -> None:
    checker = load_checker()
    constitution = (ROOT / "docs/implementation/00-project-constitution.md").read_text(encoding="utf-8")
    check_source = CHECK.read_text(encoding="utf-8")

    for volatile in ("v2026.7.5", "b4624170-0982-475e-b1ad-709a92a21f24", "social-preview"):
        assert volatile not in constitution
        assert volatile not in check_source
