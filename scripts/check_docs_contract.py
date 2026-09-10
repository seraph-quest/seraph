#!/usr/bin/env python3
"""Validate Seraph's canonical documentation ownership and entry-point contract."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IMPLEMENTATION = ROOT / "docs/implementation"
CONSTITUTION = IMPLEMENTATION / "00-project-constitution.md"
ENTRY_POINTS = [
    ROOT / "README.md",
    ROOT / "docs/README.md",
    CONSTITUTION,
    IMPLEMENTATION / "12-current-app-guide.md",
    IMPLEMENTATION / "08-docs-contract.md",
]
OWNERSHIP_SURFACES = ENTRY_POINTS + [
    ROOT / "AGENTS.md",
    ROOT / "docs/research/00-synthesis.md",
    IMPLEMENTATION / "STATUS.md",
    IMPLEMENTATION / "10-superiority-delivery.md",
]
ARCHIVED_STUBS = [
    IMPLEMENTATION / "00-master-roadmap.md",
    IMPLEMENTATION / "16-agent-parity-execution-roadmap.md",
    IMPLEMENTATION / "17-full-production-parity-goal-prompt.md",
    ROOT / "docs/research/20-seraph-agent-parity-and-exceedance-goals.md",
]
ADRS = [
    IMPLEMENTATION / "decisions/001-inference-only-model-providers.md",
    IMPLEMENTATION / "decisions/002-one-gpu-serial-priority-scheduling.md",
    IMPLEMENTATION / "decisions/003-canonical-memory-boundary.md",
    IMPLEMENTATION / "decisions/004-gpu-core-mac-edge-topology.md",
    IMPLEMENTATION / "decisions/005-epic-integration-branch-workflow.md",
]
CAPABILITY_STATUS_TERMS = (
    "Shipped",
    "Partial",
    "Experimental",
    "Planned",
    "Deprecated",
    "Excluded",
)
DOCUMENT_STATUS_TERMS = ("Target", "Research", "Archived", "Blocked")
USE_CASE_START = "<!-- outcome-use-cases:start -->"
USE_CASE_END = "<!-- outcome-use-cases:end -->"
EXPECTED_USE_CASES = (
    "- Turn goals into prioritized plans, scheduled work, and evidence-backed progress reviews.",
    "- Monitor consented desktop context and produce searchable summaries that help the operator reflect and recover focus.",
    "- Continuously research operator-selected topics and connect material findings to active goals and decisions.",
    "- Execute bounded software-engineering and knowledge workflows with approvals, artifacts, checkpoints, and audit receipts.",
    "- Continue one trusted conversation across the cockpit, paired voice, and paired messaging surfaces.",
)
STABLE_PUBLIC_MARKERS = (
    "https://docs.seraph.quest",
    "CONTRIBUTING.md",
    "SUPPORT.md",
    "SECURITY.md",
    "https://github.com/seraph-quest/seraph/discussions",
)
README_ONLY_PUBLIC_MARKERS = ("https://github.com/seraph-quest/seraph/releases/latest",)

# These are affirmative target/runtime claims. Negated descriptions and explicit
# transitional history are allowed and checked separately by required wording.
FORBIDDEN_PATTERNS = (
    re.compile(r"Seraph\s+(?:uses|requires|depends on)\s+(?:the\s+)?Codex\s+(?:CLI|runtime|operator)", re.I),
    re.compile(r"Seraph\s+(?:uses|requires|depends on)\s+Claude\s+Code", re.I),
    re.compile(r"(?:Codex|Claude Code)\s+is\s+(?:the\s+)?(?:agent|operator|runtime)\s+for\s+Seraph", re.I),
    re.compile(r"Mac\s+is\s+(?:the\s+)?(?:canonical\s+)?(?:control plane|Seraph core)", re.I),
    re.compile(r"research\s+(?:tree|docs?|synthesis)?\s*(?:defines|owns|is)\s+(?:the\s+)?(?:canonical\s+)?target", re.I),
    re.compile(r"(?:canonical\s+strategy|design)\s+source\s+of\s+truth", re.I),
    re.compile(r"research\s+docs?\s+(?:define|own)\s+(?:the\s+)?product\s+strategy", re.I),
    re.compile(r"docs\s+own\s+strategy(?:,|\s)", re.I),
    re.compile(r"\*\*Target:\*\*", re.I),
)


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def display(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def explicit_slug(path: Path) -> str | None:
    text = read(path)
    if not text.startswith("---\n"):
        return None
    frontmatter = text.split("---", 2)[1]
    match = re.search(r"^slug:\s*(\S+)\s*$", frontmatter, re.MULTILINE)
    return match.group(1) if match else None


def docusaurus_doc_id(path: Path, source_root: Path) -> str:
    relative = path.relative_to(source_root).with_suffix("")
    parts = list(relative.parts)
    parts[-1] = re.sub(r"^\d+-", "", parts[-1])
    return "/".join(parts)


def sidebar_doc_ids(path: Path) -> tuple[str, ...]:
    ids: list[str] = []
    for line in read(path).splitlines():
        match = re.match(r"^\s*'([^']+)',?\s*$", line)
        if match:
            ids.append(match.group(1))
        inline_items = re.search(r"\bitems:\s*\[([^\]]*)\]", line)
        if inline_items:
            ids.extend(re.findall(r"'([^']+)'", inline_items.group(1)))
    return tuple(ids)


def resolve_sidebar_documents(sidebar: Path, source_root: Path) -> tuple[list[Path], list[str]]:
    by_id: dict[str, Path] = {}
    errors: list[str] = []
    for path in source_root.rglob("*.md"):
        doc_id = docusaurus_doc_id(path, source_root)
        if doc_id in by_id:
            errors.append(
                f"duplicate Docusaurus doc id {doc_id!r}: {display(by_id[doc_id])} and {display(path)}"
            )
        by_id[doc_id] = path

    resolved: list[Path] = []
    for doc_id in sidebar_doc_ids(sidebar):
        path = by_id.get(doc_id)
        if path is None:
            errors.append(f"{display(sidebar)} has unresolved active doc id {doc_id!r}")
        elif path not in ARCHIVED_STUBS:
            resolved.append(path)
    return resolved, errors


def active_sidebar_documents() -> tuple[list[Path], list[str]]:
    implementation_docs, implementation_errors = resolve_sidebar_documents(
        ROOT / "docs/sidebars.implementation.ts", IMPLEMENTATION
    )
    research_docs, research_errors = resolve_sidebar_documents(
        ROOT / "docs/sidebars.research.ts", ROOT / "docs/research"
    )
    return implementation_docs + research_docs, implementation_errors + research_errors


def extract_use_cases(text: str) -> tuple[str, ...]:
    if text.count(USE_CASE_START) != 1 or text.count(USE_CASE_END) != 1:
        return ()
    block = text.split(USE_CASE_START, 1)[1].split(USE_CASE_END, 1)[0]
    return tuple(line.strip() for line in block.splitlines() if line.strip())


def check_mirrored_use_cases(paths: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in paths:
        actual = extract_use_cases(read(path))
        if actual != EXPECTED_USE_CASES:
            errors.append(
                f"{display(path)} must contain the exact five mirrored outcome-first use cases"
            )
    return errors


def check_archive_stub(path: Path) -> list[str]:
    text = read(path)
    errors: list[str] = []
    if "**State:** Archived" not in text[:500]:
        errors.append(f"{display(path)} missing archived state")
    if len(text.splitlines()) > 30:
        errors.append(f"{display(path)} is not a concise historical stub")
    for marker in ("```", "- [ ]", "- [x]", "## Ground Rules", "## Plan", "Goal:", "Use this prompt"):
        if marker in text:
            errors.append(f"{display(path)} retains operational archive marker {marker!r}")
    if "Git history" not in text or "#475" not in text:
        errors.append(f"{display(path)} must link Git history context and closed #475")
    return errors


def check_structure() -> list[str]:
    errors: list[str] = []
    required = OWNERSHIP_SURFACES + ADRS + ARCHIVED_STUBS + [
        ROOT / "AGENTS.md",
        ROOT / "docs/sidebars.implementation.ts",
        ROOT / "docs/research/00-synthesis.md",
    ]
    for path in required:
        if not path.is_file():
            errors.append(f"missing required documentation contract file: {display(path)}")

    constitution = read(CONSTITUTION)
    for heading in (
        "## Product Promise",
        "## Four-Layer Architecture",
        "## Locked Decisions",
        "## Capability Vocabulary",
        "## Capability Status Vocabulary",
        "## Document And Decision States",
        "## Runtime Invariants",
        "## Documentation Ownership",
    ):
        if heading not in constitution:
            errors.append(f"{display(CONSTITUTION)} missing {heading!r}")
    for term in CAPABILITY_STATUS_TERMS + DOCUMENT_STATUS_TERMS:
        if f"**{term}**" not in constitution:
            errors.append(f"{display(CONSTITUTION)} missing status term {term!r}")

    errors.extend(check_mirrored_use_cases([ROOT / "README.md", CONSTITUTION]))
    for path in (ROOT / "README.md", CONSTITUTION):
        text = read(path)
        for marker in STABLE_PUBLIC_MARKERS:
            if marker not in text:
                errors.append(f"{display(path)} missing public entry marker {marker!r}")
    for marker in README_ONLY_PUBLIC_MARKERS:
        if marker not in read(ROOT / "README.md"):
            errors.append(f"README.md missing public entry marker {marker!r}")

    _, sidebar_resolution_errors = active_sidebar_documents()
    errors.extend(sidebar_resolution_errors)

    for index, path in enumerate(ADRS, start=1):
        text = read(path)
        expected = f"ADR-{index:03d}"
        for marker in (expected, "**Status:** Accepted", "## Context", "## Decision", "## Consequences", "## Verification"):
            if marker not in text:
                errors.append(f"{display(path)} missing ADR marker {marker!r}")
        if path.name not in constitution:
            errors.append(f"{display(CONSTITUTION)} does not link {path.name}")

    sidebar = read(ROOT / "docs/sidebars.implementation.ts")
    adr_doc_ids = (f"decisions/{re.sub(r'^[0-9]+-', '', path.stem)}" for path in ADRS)
    for doc_id in ("project-constitution", *adr_doc_ids):
        if f"'{doc_id}'" not in sidebar:
            errors.append(f"docs/sidebars.implementation.ts missing {doc_id!r}")

    root_owners: list[Path] = []
    seen: dict[str, Path] = {}
    for path in IMPLEMENTATION.rglob("*.md"):
        slug = explicit_slug(path)
        if slug is None:
            continue
        if slug in seen:
            errors.append(
                f"duplicate explicit implementation slug {slug!r}: "
                f"{display(seen[slug])} and {display(path)}"
            )
        seen[slug] = path
        if slug == "/":
            root_owners.append(path)
    if root_owners != [CONSTITUTION]:
        errors.append(
            "the constitution must be the sole implementation '/' owner; found: "
            + ", ".join(display(path) for path in root_owners)
        )

    required_links = {
        ROOT / "README.md": ("Project Constitution", "Current App Guide", "Development Status"),
        ROOT / "docs/README.md": ("Project Constitution", "Documentation Contract"),
        ROOT / "AGENTS.md": ("Project Constitution", "ADR-005"),
        ROOT / "docs/research/00-synthesis.md": ("Project Constitution", "Research evidence"),
    }
    for path, markers in required_links.items():
        text = read(path)
        for marker in markers:
            if marker not in text:
                errors.append(f"{display(path)} missing canonical marker {marker!r}")

    for path in ARCHIVED_STUBS:
        errors.extend(check_archive_stub(path))

    implementation_sidebar = read(ROOT / "docs/sidebars.implementation.ts")
    research_sidebar = read(ROOT / "docs/sidebars.research.ts")
    for stale_id, sidebar_text in (
        ("agent-parity-execution-roadmap", implementation_sidebar),
        ("full-production-parity-goal-prompt", implementation_sidebar),
        ("seraph-agent-parity-and-exceedance-goals", research_sidebar),
    ):
        if f"'{stale_id}'" in sidebar_text:
            errors.append(f"active sidebar still exposes archived operating doc {stale_id!r}")

    return errors


def check_contradictions(paths: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in paths:
        text = read(path)
        for pattern in FORBIDDEN_PATTERNS:
            match = pattern.search(text)
            if match:
                line = text.count("\n", 0, match.start()) + 1
                errors.append(
                    f"{display(path)}:{line} presents a forbidden target/runtime contradiction: {match.group(0)!r}"
                )
    return errors


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        help="Optional Markdown entry points to contradiction-check; structural checks still run.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    active_docs, _ = active_sidebar_documents()
    paths = (
        [Path(value).resolve() for value in args.paths]
        if args.paths
        else list(dict.fromkeys(OWNERSHIP_SURFACES + active_docs))
    )
    errors = check_structure()
    errors.extend(check_contradictions(paths))
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("docs contract: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
