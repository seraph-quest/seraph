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


def test_m1_capability_contract_docs_pin_downstream_acceptance_and_proof() -> None:
    roadmap = _read_doc("docs/implementation/00-master-roadmap.md")
    docs_contract = _read_doc("docs/implementation/08-docs-contract.md")
    strategy_delivery = _read_doc("docs/implementation/11-world-class-strategy-delivery.md")

    assert "## M1 Capability Contract" in roadmap
    for downstream in ("M2", "M3", "M9"):
        assert downstream in roadmap
        assert downstream in strategy_delivery

    required_contract_terms = (
        "identity",
        "manifest",
        "permissions",
        "provenance",
        "mutation rights",
        "health",
        "compatibility",
        "lifecycle state",
        "trust level",
    )
    for term in required_contract_terms:
        assert term in strategy_delivery

    required_acceptance_terms = (
        "capability classes covered",
        "source and owner",
        "declared permissions",
        "the proof surface",
        "Do not mark M1 complete from docs alone",
    )
    for term in required_acceptance_terms:
        assert term in docs_contract

    required_proof_receipts = (
        "capability inventory payloads",
        "lifecycle/validation output",
        "audit or Activity Ledger receipts",
        "deterministic tests",
        "benchmark suites",
        "issue links",
        "PR validation",
        "implementation-doc paths",
    )
    for receipt in required_proof_receipts:
        assert receipt in strategy_delivery
