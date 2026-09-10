from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read_doc(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_strategy_claim_gate_passes_for_current_docs() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_strategy_claims.py")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_strategy_claim_gate_rejects_unlinked_high_risk_claim(tmp_path: Path) -> None:
    unchecked_doc = tmp_path / "unchecked.md"
    unchecked_doc.write_text("Seraph is the best secure agent.\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_strategy_claims.py"), str(unchecked_doc)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "M0 claim ledger" in result.stderr


def test_strategy_claim_gate_covers_security_docs_and_claim_rows() -> None:
    script = (ROOT / "scripts/check_strategy_claims.py").read_text(encoding="utf-8")
    ledger = _read_doc("docs/research/19-strategy-claim-ledger.md")
    parity_goals = _read_doc("docs/research/20-seraph-agent-parity-and-exceedance-goals.md")
    roadmap = _read_doc("docs/implementation/16-agent-parity-execution-roadmap.md")
    constitution = _read_doc("docs/implementation/00-project-constitution.md")

    assert "20-seraph-agent-parity-and-exceedance-goals.md" in script
    assert "16-agent-parity-execution-roadmap.md" in script
    assert "STATUS.md" in script
    assert "SCL-014" in ledger
    assert "SCL-020" in ledger
    assert "SCL-028" in ledger
    assert "SCL-029" in ledger
    assert "SCL-030" in ledger
    assert "SCL-031" in ledger
    assert "**State:** Archived" in parity_goals
    assert "**State:** Archived" in roadmap
    assert "Git history" in parity_goals
    assert "Git history" in roadmap
    assert "sole accepted-target authority" in parity_goals
    assert "sole product and accepted-target authority" in constitution


def test_constitution_pins_capability_contract_and_locked_decisions() -> None:
    constitution = _read_doc("docs/implementation/00-project-constitution.md")
    docs_contract = _read_doc("docs/implementation/08-docs-contract.md")

    for status in ("Shipped", "Partial", "Experimental", "Planned", "Deprecated", "Excluded"):
        assert f"**{status}**" in constitution
        assert f"**{status}**" in docs_contract
    for adr in range(1, 6):
        assert f"ADR-{adr:03d}" in constitution
    assert "sole authority" in docs_contract
    assert "A capability must never be labeled Target" in docs_contract
