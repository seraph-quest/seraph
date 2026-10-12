"""Isolated parser payment tests; these do not issue a Source or ledger."""

import pytest

from src.memory import composition_headers
from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget, MAX_BYTES
from src.workspace import accounting_witness as witness


def _preceding_checks(monkeypatch):
    # Isolate the final original parser from unrelated candidate/claim owners.
    monkeypatch.setattr(witness, "_checked_memory_candidate_row",
        lambda row: (None, {"result": None}, None))
    monkeypatch.setattr(witness, "preflight_native_memory_reference_journal",
        lambda text: (None, None))


def test_exhausted_original_budget_denies_before_effect_receipt_decode(monkeypatch):
    _preceding_checks(monkeypatch)
    budget = HeaderReadBudget()
    budget.debit(MAX_BYTES)
    monkeypatch.setattr(composition_headers, "current_budget", lambda: budget)
    from src.memory import header_bounds
    monkeypatch.setattr(header_bounds, "strict_json_loads",
        lambda text: pytest.fail("decode ran before its payment"))
    monkeypatch.setattr(witness, "_checked_memory_claim_row",
        lambda *args: pytest.fail("claim ran after parser payment failed"))
    with pytest.raises(witness.ProductionWorkspaceReconciliationError):
        witness._checked_preoriginal_memory_row({
            "run_identity": "parser-budget-unit", "checkpoint_receipts_json": "[]",
            "effect_receipts_json": "[]", "status": "running"})
    assert budget.remaining == 0


def test_invalid_preceding_shape_does_not_charge_or_decode_unused_parser(monkeypatch):
    _preceding_checks(monkeypatch)
    budget = HeaderReadBudget()
    monkeypatch.setattr(composition_headers, "current_budget", lambda: budget)
    from src.memory import header_bounds
    monkeypatch.setattr(header_bounds, "strict_json_loads",
        lambda text: pytest.fail("short-circuited parser ran"))
    with pytest.raises(witness.ProductionWorkspaceReconciliationError):
        witness._checked_preoriginal_memory_row({
            "run_identity": "parser-budget-unit", "checkpoint_receipts_json": "[]",
            "effect_receipts_json": "[]", "status": "succeeded"})
    assert budget.remaining == MAX_BYTES


def test_unknown_liability_parser_denies_before_decode_when_budget_spent(monkeypatch):
    budget = HeaderReadBudget()
    budget.debit(MAX_BYTES)
    monkeypatch.setattr(composition_headers, "current_budget", lambda: budget)
    from src.memory import header_bounds
    monkeypatch.setattr(header_bounds, "strict_json_loads",
        lambda text: pytest.fail("unknown parser ran before payment"))
    payload = {"state": "unknown", "reason_code": "original_projection_unavailable",
        "original_checkpoint_digest": None, "owner_events": [], "absences": []}
    with pytest.raises(HeaderBoundsError):
        witness._checked_memory_unknown_unverified_row({
            "run_identity": "parser-budget-unit", "effect_receipts_json": "[]",
            "status": "blocked"}, None, {"result": None}, None, {"payload": payload})
    assert budget.remaining == 0


def test_unknown_invalid_reason_preserves_parser_short_circuit(monkeypatch):
    budget = HeaderReadBudget()
    monkeypatch.setattr(composition_headers, "current_budget", lambda: budget)
    from src.memory import header_bounds
    monkeypatch.setattr(header_bounds, "strict_json_loads",
        lambda text: pytest.fail("unknown short-circuited parser ran"))
    payload = {"state": "unknown", "reason_code": "invalid",
        "original_checkpoint_digest": None}
    with pytest.raises(ValueError, match="memory_unknown_unverified_shape_changed"):
        witness._checked_memory_unknown_unverified_row({
            "run_identity": "parser-budget-unit", "effect_receipts_json": "[]"},
            None, {"result": None}, None, {"payload": payload})
    assert budget.remaining == MAX_BYTES
