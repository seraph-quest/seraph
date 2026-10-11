"""Source-ready accounting owner capacity checks; execution requires Root release."""
from datetime import datetime, timezone
import json

import pytest

from src.db.models import InferenceAccountingOwner, InferenceCostReservation
from src.workflows.inference_accounting import (
    _accounting_model_upper, _ledger_digest, _witness,
    original_accounting_witness_numeric_schedule,
)


def current_rows():
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    account = InferenceAccountingOwner(deployment_id="deployment", ceiling_microusd=100,
        settings_history_json='[{"label":"żółć\\n😀"}]', created_at=now, updated_at=now)
    row = InferenceCostReservation(operation_id="operation", deployment_id="deployment", job_id="job",
        owner_id="owner", payload_digest="a" * 64, policy_digest="b" * 64, runtime_path="governed",
        profile_id="profile", period_id="2026-10", settings_revision=1, ceiling_microusd=100,
        bound_microusd=10, sequence=1, priority=1, deadline_at=now, job_fencing_token=1,
        evidence_json='[{"message":"quotes \\\" slash \\\\ and 😀"}]', created_at=now, updated_at=now)
    return account, row


def test_accounting_encoded_bound_covers_real_owner_and_existing_reservation():
    account, row = current_rows()
    for model, operation in ((account, False), (row, True)):
        payload = model.model_dump(mode="json", exclude={"group_lookup_key"})
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
        assert _accounting_model_upper(model, operation=operation) >= len(encoded)
    entries = original_accounting_witness_numeric_schedule(account, [row])
    assert any("operation-dump" in appearance for appearance, _ in entries)
    assert any("record-datetime-parse" in appearance for appearance, _ in entries)


def test_original_ledger_runs_only_after_all_owner_precharges(monkeypatch):
    from src.workspace import accounting_witness
    account, row = current_rows()
    paid = []
    original = accounting_witness.ledger_digest

    class Capacity:
        def debit(self, upper, *, appearance):
            paid.append((appearance, upper))

    def observed(owner, operations):
        assert operations and any("operation-dump" in appearance for appearance, _ in paid)
        assert any("encoded-json" in appearance for appearance, _ in paid)
        return original(owner, operations)

    monkeypatch.setattr(accounting_witness, "ledger_digest", observed)
    paid_result = _ledger_digest(account, [row], header_budget=Capacity())
    assert paid_result == _ledger_digest(account, [row])


def test_witness_capacity_failure_blocks_original_output():
    account, _ = current_rows()

    class Exhausted:
        def debit(self, upper, *, appearance):
            raise RuntimeError("capacity exhausted")

    with pytest.raises(RuntimeError, match="^capacity exhausted$"):
        _witness(account, header_budget=Exhausted())
    assert _witness(account)["deployment_id"] == account.deployment_id
