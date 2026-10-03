"""Protected schema/signing mechanics; no task outcomes or memory fabricated."""
from copy import deepcopy

import pytest

from src.memory import repository as repo


def _scope():
    return {"schema_version": "procedure_preference.v1", "owner_principal_id": "operator:root:scope-test",
        "owner_session_id": "root", "goal_id": "goal", "goal_revision": 1,
        "routine_id": "routine", "routine_revision": 1, "version_id": "version", "version": 1,
        "template_id": "public-browser-check", "source_context_digest": "1" * 64,
        "plan_digest": "2" * 64, "package_digest": "3" * 64, "copied_input_digest": "4" * 64,
        "membership_digest": "5" * 64, "bundle_digest": "6" * 64, "source_task_ids": ["task-a", "task-b"]}


def _binding():
    return {"owner_principal_id": "operator:root:scope-test", "owner_session_id": "root",
        "source_task_id": "task-a", "source_attempt_id": "attempt-a", "goal_id": "goal",
        "capability_id": "guardian-routine.v2", "source_context_digest": "1" * 64,
        "source_evidence_ids": ["parent-readback", "leaf-readback"]}


def _mac(scope, key):
    return repo._m5_selection_binding_mac(proposal_id="proposal", accepted_content_digest="a" * 64,
        owner_principal_id="operator:root:scope-test", owner_session_id="root", source_context_digest="1" * 64,
        source_binding=_binding(), decision_effect="require_operator_confirmation", memory_scope=scope,
        _signing_key=key)


@pytest.mark.parametrize("field,new_value", [("routine_id", "other"), ("version_id", "other"),
    ("membership_digest", "a" * 64), ("bundle_digest", "b" * 64), ("package_digest", "c" * 64),
    ("copied_input_digest", "d" * 64), ("goal_revision", 2), ("source_task_ids", ["task-a"])])
def test_exact_procedure_fields_change_signature(field, new_value):
    key = b"isolated-signature-mechanics-key"
    altered = {**_scope(), field: new_value}
    assert _mac(_scope(), key) != _mac(altered, key)


def test_specialized_scope_rejects_extra_fields_unsorted_duplicates_and_bool_revision():
    assert repo._m5_selection_scope({**_scope(), "grant": True}) is None
    assert repo._m5_selection_scope({**_scope(), "source_task_ids": ["task-b", "task-a"]}) is None
    assert repo._m5_selection_scope({**_scope(), "source_task_ids": ["task-a", "task-a"]}) is None
    assert repo._m5_selection_scope({**_scope(), "version": True}) is None


def test_staged_signer_and_verifier_never_load_key(monkeypatch):
    key = b"isolated-signature-mechanics-key"
    def forbidden():
        raise AssertionError("key load inside pure signing path")
    monkeypatch.setattr(repo, "_effect_mac_key", forbidden)
    provenance = {"proposal_id": "proposal", "accepted_content_digest": "a" * 64,
        "owner_principal_id": "operator:root:scope-test", "owner_session_id": "root",
        "source_context_digest": "1" * 64, "decision_effect": "require_operator_confirmation",
        "lifecycle_state": "active", "memory_scope": _scope(), "verified_source_binding": _binding(),
        "selection_binding_key_id": repo._m5_selection_binding_key_id(_signing_key=key),
        "selection_binding_mac": _mac(_scope(), key)}
    assert repo._m5_selection_binding_matches(provenance, proposal_id="proposal", accepted_content_digest="a" * 64,
        decision_effect="require_operator_confirmation", memory_scope=_scope(), source_binding=_binding(), _signing_key=key)
    changed = deepcopy(provenance)
    changed["memory_scope"]["bundle_digest"] = "7" * 64
    assert not repo._m5_selection_binding_matches(changed, proposal_id="proposal", accepted_content_digest="a" * 64,
        decision_effect="require_operator_confirmation", memory_scope=changed["memory_scope"], source_binding=_binding(), _signing_key=key)


def test_ordinary_scope_and_signature_keep_original_bytes(monkeypatch):
    key = b"isolated-signature-mechanics-key"
    monkeypatch.setattr(repo, "_effect_mac_key", lambda: key)
    scope = {"schema_version": repo._M5_SCOPE_SCHEMA_VERSION, "goal_id": "goal", "goal_revision": 1,
        "source_context_digest": "1" * 64, "preferred_capability_id": "browser.public-task.v1",
        "candidate_capability_ids": ["browser.public-task.v1"], "owner_principal_id": "ignored-existing-field"}
    projected = {"schema_version": scope["schema_version"], "goal_id": "goal", "goal_revision": 1,
        "source_context_digest": "1" * 64, "preferred_capability_id": "browser.public-task.v1",
        "preferred_capability_version": None, "candidate_capability_ids": ["browser.public-task.v1"]}
    assert repo._m5_selection_scope(scope) == projected
    kwargs = dict(proposal_id="proposal", accepted_content_digest="a" * 64,
        owner_principal_id="operator:root:scope-test", owner_session_id="root", source_context_digest="1" * 64,
        source_binding=_binding(), decision_effect="require_operator_confirmation", memory_scope=scope)
    normal = repo._m5_selection_binding_mac(**kwargs)
    assert normal == repo._m5_selection_binding_mac(**kwargs, _signing_key=key)
    # Regression constant from the pre-919 canonical payload/key semantics.
    payload = {"version": repo._M5_SELECTION_BINDING_SCHEMA_VERSION, "proposal_id": "proposal",
        "accepted_content_digest": "a" * 64, "owner_principal_id": "operator:root:scope-test", "owner_session_id": "root",
        "source_context_digest": "1" * 64, "verified_source_binding": repo._m5_verified_source_binding(_binding()),
        "decision_effect": "require_operator_confirmation", "memory_scope": projected,
        "correction_target": {"corrects_memory_id": None, "corrected_memory_previous_status": None,
            "corrected_memory_content_digest": None}, "recovered_from_proposal_id": None, "lifecycle_state": "active"}
    assert normal == repo._mac(payload, key=key)
