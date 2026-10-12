"""Observe genuine pre-effect capacity facts without supplying a forecast."""
import json
import os

import pytest
from sqlalchemy import func, select

from tests.test_native_memory_producer import actual_memory
from src.db.engine import get_session
from src.db.models import AuditEvent, Memory, MemoryProposal
from src.memory import header_bounds, m5
from src.memory.header_bounds import HeaderBoundsError
from src.workspace import accounting_witness


@pytest.mark.asyncio
async def test_actual_source_numeric_prefix_stops_without_full_forecast(actual_memory, monkeypatch, tmp_path):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    # This diagnostic does not uninstall, replace or weaken a real composer.
    assert getattr(accounting_witness, "_native_memory_effect_numeric_schedule", None) is None
    original_factory = header_bounds._reserve_native_memory_effect_ledger
    original_apply = m5._apply_native_memory_mutation_plan
    observations, apply_calls = [], []

    async def mutation_counts():
        async with get_session() as db:
            return (
                await db.scalar(select(func.count()).select_from(MemoryProposal)),
                await db.scalar(select(func.count()).select_from(Memory)),
                await db.scalar(select(func.count()).select_from(AuditEvent).where(
                    AuditEvent.event_type.in_(("memory_learning_proposed", "memory_learning_accepted", "memory_forgotten")))),
            )

    before_mutations = await mutation_counts()

    async def observe_apply(*args, **kwargs):
        apply_calls.append(1)
        return await original_apply(*args, **kwargs)

    async def observe_factory(db, run, operation, prepared, *, existing_references, before_row, certificate):
        # These exact records were issued and paid by the real original begin
        # path. Only its original helper APIs authenticate/use their values.
        guard = db.info["composition_guard"]
        retained = guard._native_memory_retention_numeric_facts
        capacity = guard._native_memory_source_capacity_facts
        assert retained[0] is operation and retained[1] is prepared and retained[2] is certificate
        assert capacity[0] is operation and capacity[1] is prepared and capacity[2] is certificate
        assert operation._effect is None and prepared.consumed is False
        widths, facts = retained[3], capacity[3]
        common = guard._native_memory_source_common_numeric[2]
        snapshot = guard._native_memory_source_snapshot_numeric[2]
        assert guard._native_memory_source_common_numeric[0] is operation
        assert guard._native_memory_source_common_numeric[1] is certificate
        assert guard._native_memory_source_snapshot_numeric[0] is operation
        assert guard._native_memory_source_snapshot_numeric[1] is certificate

        def metrics(entries):
            amounts = [amount for _appearance, amount in entries]
            assert all(type(amount) is int and amount >= 0 for amount in amounts)
            return {"appearances": len(amounts), "total": sum(amounts), "maximum": max(amounts, default=0)}

        data = {
            "schema_version": 1,
            "issuer_common": metrics(common),
            "actual_snapshot": metrics(snapshot),
            "selected_rows": len(facts["row_uppers"]),
            "retained_references": len(existing_references),
            "row_upper_total": sum(facts["row_uppers"].values()),
            "raw_tuple_upper_total": sum(facts["raw_tuple_uppers"].values()),
            "remaining_before_factory": guard.header_budget.remaining,
            "future_text_total": sum(widths["future_text_bytes"].values()),
            "completion_future_text_total": sum(widths["completion_future_text_bytes"].values()),
            "widths": {name: widths[name] for name in (
                "original_upper", "original_record_upper", "current_upper", "current_record_upper",
                "own_upper", "own_raw_upper", "context_upper", "journal_upper", "selected_rows_upper")},
            "minimum_own_current_invocations": 2,
            "full_forecast": 0,
            "factory_denied": 0,
        }
        # ONE original owner vector per measurement. These are contributions,
        # never a supplied fee-list/ledger or an optimistic complete total.
        for label, helper, extra in (
            ("one_original_preflight", accounting_witness._native_memory_original_preflight_numeric_entries, {}),
            ("one_current_construction", accounting_witness._native_memory_current_construction_numeric_entries, {}),
            ("one_capture_allocation", accounting_witness._native_memory_capture_allocation_numeric_entries, {}),
            ("one_future_body", accounting_witness._native_memory_generic_future_numeric_schedule, {"facts": facts}),
        ):
            try:
                entries = helper(operation, prepared, certificate=certificate, retention_widths=widths, **extra)
            except HeaderBoundsError:
                data[label] = {"supported": 0}
            else:
                data[label] = {"supported": 1, **metrics(entries)}
        observations.append(data)
        # Call the genuine original factory with its exact original arguments.
        # A successful factory would make this diagnostic fail, not run M5.
        try:
            await original_factory(db, run, operation, prepared,
                existing_references=existing_references, before_row=before_row, certificate=certificate)
        except HeaderBoundsError as error:
            assert str(error) == "memory_complete_numeric_forecast_unavailable"
            assert operation._effect is None and prepared.consumed is False
            data["factory_denied"] = 1
            raise
        else:
            # This branch is unreachable under the required genuine absence;
            # do not return an unexpected ledger and permit effects.
            pytest.fail("original missing-forecast gate unexpectedly returned a ledger")

    monkeypatch.setattr(header_bounds, "_reserve_native_memory_effect_ledger", observe_factory)
    monkeypatch.setattr(m5, "_apply_native_memory_mutation_plan", observe_apply)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "pre-effect-numeric-observation",
    })
    assert response.status_code == 409
    assert len(observations) == 1 and observations[0]["factory_denied"] == 1
    assert apply_calls == [] and await mutation_counts() == before_mutations
    # The artifact has fixed public labels and numerical dimensions only.
    # No bodies, address keys, hashes, claims, configured secrets or errors.
    artifact = tmp_path / "native-memory-pre-effect-numeric.json"
    descriptor = os.open(artifact, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(observations[0], output, sort_keys=True)
        output.write("\n")
    assert artifact.stat().st_mode & 0o777 == 0o600
