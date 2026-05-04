#!/usr/bin/env python3
"""Check that high-risk strategy wording points at the M0 claim ledger."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPETITION_TABLE = ROOT / "docs/research/18-agent-competition-truth-table.md"
LEDGER = ROOT / "docs/research/19-strategy-claim-ledger.md"
LEDGER_SLUG = "strategy-claim-ledger"

DEFAULT_SCOPE = [
    ROOT / "docs/research/09-reference-systems-and-evidence.md",
    ROOT / "docs/research/10-competitive-benchmark.md",
    ROOT / "docs/research/17-seraph-world-class-strategy.md",
    COMPETITION_TABLE,
    LEDGER,
    ROOT / "docs/implementation/08-docs-contract.md",
    ROOT / "docs/implementation/09-benchmark-status.md",
    ROOT / "docs/implementation/10-superiority-delivery.md",
    ROOT / "docs/implementation/11-world-class-strategy-delivery.md",
]

HIGH_RISK_PATTERN = re.compile(
    r"\b("
    r"world-class|best(?!-)|greatest|strongest|superior|superiority(?![-\s]+(?:program|delivery))|ahead|"
    r"production-ready|secure|private|trusted|complete|fully shipped|first-class|"
    r"only\s+(?:agent|assistant|product|system|platform|workspace|tool|one)"
    r")\b",
    re.IGNORECASE,
)
CLAIM_ID_PATTERN = re.compile(r"\bSCL-\d{3}\b")
CLAIM_ROW_PATTERN = re.compile(r"^\| `(?P<id>SCL-\d{3})` \|", re.MULTILINE)

REQUIRED_COMPETITION_SECTIONS = [
    "## Truth Table",
    "## Cross-Competitor Gap Map",
    "## Claim Rules",
]
REQUIRED_COMPETITORS = [
    "Hermes Agent",
    "OpenClaw",
    "IronClaw",
    "Claude Code",
    "Codex",
    "OpenHands",
    "Goose",
    "Aider",
    "Cline / Roo Code",
    "Devin",
    "Manus",
    "Browserbase / Stagehand",
    "AutoGPT / Forge",
    "CrewAI",
    "LangGraph",
]
REQUIRED_LEDGER_SECTIONS = [
    "## Status Model",
    "## Review Gate",
    "## Claim Ledger",
    "## Short Replacement Rules",
    "## Completion Gate",
]
REQUIRED_CLAIMS = {f"SCL-{index:03d}" for index in range(1, 11)}


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"missing expected file: {_display(path)}")


def check_competition_table() -> list[str]:
    text = _read(COMPETITION_TABLE)
    errors: list[str] = []

    for heading in REQUIRED_COMPETITION_SECTIONS:
        if heading not in text:
            errors.append(f"{_display(COMPETITION_TABLE)} missing required section {heading!r}")

    for competitor in REQUIRED_COMPETITORS:
        if competitor not in text:
            errors.append(f"{_display(COMPETITION_TABLE)} missing competitor {competitor!r}")

    return errors


def check_ledger() -> list[str]:
    text = _read(LEDGER)
    errors: list[str] = []

    for heading in REQUIRED_LEDGER_SECTIONS:
        if heading not in text:
            errors.append(f"{_display(LEDGER)} missing required section {heading!r}")

    found_claims = set(CLAIM_ROW_PATTERN.findall(text))
    missing_claims = sorted(REQUIRED_CLAIMS - found_claims)
    if missing_claims:
        errors.append(f"{_display(LEDGER)} missing required claim rows: {', '.join(missing_claims)}")

    for column in (
        "ID",
        "Claim area",
        "Allowed wording",
        "Forbidden wording",
        "Status",
        "Evidence",
        "Proof path",
        "Owner",
        "Milestone",
        "Issue link",
        "Trust/operator surface",
    ):
        if column not in text:
            errors.append(f"{_display(LEDGER)} missing claim ledger column {column!r}")

    return errors


def check_claim_links(paths: list[Path]) -> list[str]:
    errors: list[str] = []

    for path in paths:
        text = _read(path)
        if path in {COMPETITION_TABLE, LEDGER}:
            continue
        if not HIGH_RISK_PATTERN.search(text):
            continue
        if LEDGER_SLUG in text or CLAIM_ID_PATTERN.search(text):
            continue

        examples: list[str] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if HIGH_RISK_PATTERN.search(line):
                examples.append(f"{line_number}: {line.strip()}")
            if len(examples) == 3:
                break
        errors.append(
            f"{_display(path)} uses high-risk strategy wording without linking "
            f"the M0 claim ledger or a claim id. Examples: {' | '.join(examples)}"
        )

    return errors


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        help="Markdown files to check. Defaults to current strategic research/implementation docs.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    paths = [Path(path).resolve() for path in args.paths] if args.paths else DEFAULT_SCOPE
    paths = [path for path in paths if path.suffix == ".md"]

    errors = check_competition_table()
    errors.extend(check_ledger())
    errors.extend(check_claim_links(paths))

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
