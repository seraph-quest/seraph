from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


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
