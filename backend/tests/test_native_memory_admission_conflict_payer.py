"""Admission payer trust boundary and unchanged ordinary conflict semantics.

These tests do not issue a Source, certificate, ledger or read permission.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


def _ordinary_spec():
    identity = SimpleNamespace(job_id="job", job_kind="ordinary", capability_version="1",
        owner_kind="operator", owner_principal_id="operator:test", idempotency_scope="ordinary")
    return SimpleNamespace(identity=identity, service_id=None, dependencies=["b", "a", "a"],
        resource_claims=[], priority=3, max_attempts=1, session_id=None, parent_job_id=None,
        parent_fencing_token=None, goal_id=None, goal_revision=None, plan_revision=None,
        candidate_id=None, source_task_id=None, composition_binding=None,
        run_fingerprint=None, budget_digest=None, budget_microusd=None, declared_authority={})


def test_ordinary_admission_conflicts_keeps_normalization_and_optional_fields():
    from src.workflows.job_runtime import _admission_conflicts
    spec = _ordinary_spec()
    deadline = datetime(2026, 10, 12, tzinfo=timezone.utc)
    existing = SimpleNamespace(run_identity="job", input_digest="input", job_kind="ordinary",
        capability_version="1", owner_kind="operator", owner_principal_id="operator:test",
        service_id=None, authority_digest="authority", dependencies_json='["a","b"]',
        resource_claims_json="[]", deadline_at=deadline, priority=3, max_attempts=1,
        session_id=None, parent_job_id=None, parent_fencing_token=None, goal_id=None,
        goal_revision=None, plan_revision=None, candidate_id=None, source_task_id=None,
        composition_binding_json=None)
    assert _admission_conflicts(existing, spec=spec, input_digest="input",
        authority_digest="authority", deadline=deadline) == []
    existing.dependencies_json = "{}"
    existing.priority = 4
    assert _admission_conflicts(existing, spec=spec, input_digest="input",
        authority_digest="authority", deadline=deadline) == ["dependencies", "priority"]


@pytest.mark.parametrize("fingerprint", [False, True])
def test_unowned_admission_payer_denies_before_normalization_or_binding(fingerprint, monkeypatch):
    from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget
    from src.workflows import job_runtime
    budget = HeaderReadBudget()
    spec = _ordinary_spec()
    calls = []
    def private_constructor(*args, **kwargs):
        calls.append(True)
        raise AssertionError("unowned payer reached original constructor")
    monkeypatch.setattr(job_runtime, "_text", private_constructor)
    monkeypatch.setattr(job_runtime, "_string_list", private_constructor)
    monkeypatch.setattr(job_runtime, "_digest", private_constructor)
    with pytest.raises(HeaderBoundsError):
        if fingerprint:
            job_runtime._composition_fingerprint(spec, "input", _memory_header_budget=budget)
        else:
            job_runtime._admission_conflicts(object(), spec=spec, input_digest="input",
                authority_digest="authority", deadline=None, _memory_header_budget=budget)
    assert calls == []


@pytest.mark.parametrize("digest_only", [False, True])
def test_unowned_binding_payer_denies_before_payload_or_dependency_sizing(digest_only):
    from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget
    from src.workflows.job_runtime import _memory_admission_binding_json
    # No binding fields exist: a foreign payer must be denied at the exact
    # original REPORT owner gate before payload/dependency sizing can inspect
    # any field, whether serializing to_json or paying binding_digest only.
    with pytest.raises(HeaderBoundsError, match="memory_report_admission_numeric_owner_changed"):
        _memory_admission_binding_json(object(), HeaderReadBudget(),
            _construction_only=digest_only)
