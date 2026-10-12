"""Cold constructor component checks; no Source or effect-ledger issuance."""

import json

import pytest


def test_candidate_constructor_upper_covers_materialized_metadata():
    from src.workspace import accounting_witness as witness

    run_id = 'cold-"\\\n' * 12
    entries = witness._native_memory_cold_candidate_numeric_entries(
        run_id, row_upper=1048576, context_upper=1048576,
        arguments_upper=1048576, authority_upper=1048576)
    assert len(entries) == 33
    size = lambda value: len(json.dumps(value, ensure_ascii=True, separators=(",", ":")))
    # Independently measure labels, pairs and both growing/result containers.
    observed = sum(size(label) + size((label, amount)) for label, amount in entries)
    observed += 2 * size(entries)
    upper = witness._native_memory_cold_vector_construction_upper(
        run_id, entries=33, groups=6, sizing_fields=4)
    assert upper >= observed


def test_runtime_candidate_denies_before_building_entries(monkeypatch):
    from src.memory import composition_headers
    from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget, MAX_BYTES
    from src.runtime_plugins import memory_producer
    from src.workspace import accounting_witness as witness

    budget = HeaderReadBudget()
    budget.debit(MAX_BYTES)
    # Isolate the resource payer boundary. These mocks confer no Source,
    # certificate, body-read permission, or complete-ledger authority.
    monkeypatch.setattr(composition_headers, "current_budget", lambda: budget)
    monkeypatch.setattr(memory_producer, "_native_memory_source_numeric_frame", lambda: budget)

    def unpaid_builder(*args, **kwargs):
        pytest.fail("cold entries were constructed before their allocation fee")

    monkeypatch.setattr(witness, "_native_memory_cold_numeric_entries", unpaid_builder)
    with pytest.raises(HeaderBoundsError):
        witness._native_memory_cold_candidate_numeric_entries(
            "run", row_upper=100, context_upper=100,
            arguments_upper=100, authority_upper=100, _runtime_payer=budget)


def test_wrong_resource_frame_denies_before_building_entries(monkeypatch):
    from src.memory import composition_headers
    from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget
    from src.runtime_plugins import memory_producer
    from src.workspace import accounting_witness as witness

    budget = HeaderReadBudget()
    monkeypatch.setattr(composition_headers, "current_budget", lambda: budget)
    monkeypatch.setattr(memory_producer, "_native_memory_source_numeric_frame", lambda: None)
    with pytest.raises(HeaderBoundsError, match="memory_cold_construction_frame_changed"):
        witness._native_memory_cold_claim_numeric_entries(
            "run", 100, claimed=False, _runtime_payer=budget)


def test_binding_digest_denies_before_nested_original_builder(monkeypatch):
    from src.memory import composition_headers
    from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget, MAX_BYTES
    from src.runtime_plugins import memory_producer
    from src.workflows import inference_accounting
    from src.workspace import accounting_witness as witness

    budget = HeaderReadBudget()
    budget.debit(MAX_BYTES)
    monkeypatch.setattr(composition_headers, "current_budget", lambda: budget)
    monkeypatch.setattr(memory_producer, "_native_memory_source_numeric_frame", lambda: budget)

    def unpaid_original(*args, **kwargs):
        pytest.fail("original binding60 constructed before cold constructor payment")

    monkeypatch.setattr(inference_accounting, "_original_native_binding_numeric_entries", unpaid_original)
    with pytest.raises(HeaderBoundsError):
        witness._native_memory_cold_binding_digest_numeric_entries(
            "run", 100, owner="candidate", occurrences=2, _runtime_payer=budget)


def test_native_run_binding_constructor_covers_manifest_labels():
    from src.workflows import inference_accounting
    from src.workspace import accounting_witness as witness

    run_id = "native-memory:" + "a" * 64
    entries = inference_accounting._original_native_binding_numeric_entries(run_id, 100)
    assert len(entries) == 60
    assert any(label[2] == ("manifest", 1, "origin-dependencies")
               for label, _amount in entries)
    size = lambda value: len(json.dumps(value, ensure_ascii=True, separators=(",", ":")))
    # This observes actual original labels, including their real native ID and
    # nested manifest tuples, independently of the scalar planning formula.
    observed = sum(size(label) + size((label, amount)) for label, amount in entries)
    observed += 2 * size(entries)
    upper = witness._native_memory_cold_binding_construction_upper(run_id)
    assert upper >= observed
    short_upper = witness._native_memory_cold_binding_construction_upper("run")
    assert upper > short_upper
