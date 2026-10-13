"""Closed v5 codecs only; these objects grant no recovery/composition authority."""
import hashlib

import pytest

from src.db.models import WorkflowRunState, WorkBoardTask
from src.workflows.job_runtime import DurableJobLeaseError, _canonical
from src.workflows.repo_repair_source import _repository_format_schema
from src.workflows.repo_repair_stop import (
    _V4_STATIC_FIELDS, _V5_STATIC_FIELDS, _static_projection_v4,
    _static_projection_v5, _static_digest_v4, _static_digest_v5,
)
from src.workflows.repo_repair_source_recovery import (
    _COMMITMENT_SLOTS, _validate_recovery_commitment, RepositorySourceRecoveryError,
)


def root_values(binding=None):
    return WorkflowRunState(run_identity="root", root_run_identity="root",
        workflow_name="repository", composition_binding_json=binding).model_dump(mode="json")


def test_exact_current_field_inventory():
    assert set(WorkflowRunState.model_fields) == _V5_STATIC_FIELDS["WorkflowRunState"]
    assert len(WorkflowRunState.model_fields) == 66
    assert set(WorkBoardTask.model_fields) == _V5_STATIC_FIELDS["WorkBoardTask"]
    assert len(WorkBoardTask.model_fields) == 46
    assert {"priority_explicit", "admitted_method_json"} <= set(WorkBoardTask.model_fields)


@pytest.mark.parametrize("role,count", [("root", 62), ("original_parent", 63), ("native_invocation", 61)])
@pytest.mark.parametrize("binding", [None, '{"original":"bytes"}'])
def test_binding_is_literal_in_every_role(role, count, binding):
    values = root_values(binding)
    projected = _static_projection_v5(values, "WorkflowRunState", role)
    assert len(projected) == count
    assert "composition_binding_json" in projected
    assert projected["composition_binding_json"] == binding
    expected = hashlib.sha256(b"repository.stop_static.v3\0" + _canonical(projected).encode()).hexdigest()
    assert _static_digest_v5(values, "WorkflowRunState", role) == expected


@pytest.mark.parametrize("binding", [None, '{"original":"bytes"}'])
def test_historical_v4_rejects_extra_field_even_null(binding):
    with pytest.raises(DurableJobLeaseError, match="v4 static field inventory changed"):
        _static_projection_v4(root_values(binding), "WorkflowRunState", "root")


def test_historical_v4_domain_remains_literal():
    values = {name: None for name in _V4_STATIC_FIELDS["WorkflowRunState"]}
    projected = _static_projection_v4(values, "WorkflowRunState", "root")
    assert _static_digest_v4(values, "WorkflowRunState", "root") == hashlib.sha256(
        b"repository.stop_static.v2\0" + _canonical(projected).encode()).hexdigest()


@pytest.mark.parametrize("mutation", ["missing", "extra", "foreign_role", "wrong_binding_type", "empty_binding"])
def test_closed_v5_projection_rejects_unreviewed_shapes(mutation):
    values, role = root_values(), "root"
    if mutation == "missing": values.pop("composition_binding_json")
    elif mutation == "extra": values["unreviewed"] = None
    elif mutation == "foreign_role": role = "caller_supplied"
    elif mutation == "wrong_binding_type": values["composition_binding_json"] = {}
    else: values["composition_binding_json"] = ""
    with pytest.raises(DurableJobLeaseError):
        _static_projection_v5(values, "WorkflowRunState", role)


def test_binding_bytes_drift_changes_digest_without_normalization():
    assert _static_digest_v5(root_values('{"a":1}'), "WorkflowRunState", "root") != \
        _static_digest_v5(root_values('{ "a":1}'), "WorkflowRunState", "root")
    assert _static_digest_v5(root_values(), "WorkflowRunState", "root") != \
        _static_digest_v5(root_values("null"), "WorkflowRunState", "root")


@pytest.mark.parametrize("kind", ["stop_intent", "stop_snapshot", "stop_uncertainty_successor", "original_cleanup_artifact"])
def test_sealed_inventory_selects_distinct_grammar(kind):
    assert _repository_format_schema("repository.checkpoint_inventory.v5", kind) == "repository." + kind + ".v3"
    assert _repository_format_schema("repository.checkpoint_inventory.v4", kind) == "repository." + kind + ".v2"
    for version in (1, 2, 3):
        assert _repository_format_schema("repository.checkpoint_inventory.v" + str(version), kind) == "repository." + kind + ".v1"


@pytest.mark.parametrize("schema,kind", [("repository.checkpoint_inventory.v6", "stop_intent"),
    ("repository.checkpoint_inventory.v5", "caller_selected"), (None, "stop_intent")])
def test_foreign_inventory_or_kind_is_not_a_format_upgrade(schema, kind):
    with pytest.raises(DurableJobLeaseError):
        _repository_format_schema(schema, kind)


def commitment_values():
    # Parser evidence only; no registered Source, SQL owner or physical witness.
    return {"schema": "repository.recovery_commitment.v2",
        "domain": "repository.source_recovery_immutable.v2", "root_key": "workflow_run_states:root",
        "before_revision": 1, "root_static_digest": "a" * 64,
        "root_updated_at": "2026-10-13T00:00:00+00:00", "journal_prefix_count": 1,
        "journal_prefix_digest": "b" * 64, "owner_identity": {"id": "owner", "principal_id": "principal",
            "created_at": "2026-10-13T00:00:00+00:00", "absolute_expires_at": "2026-10-14T00:00:00+00:00"},
        "non_root_static": [{"slot": slot, "table": table, "key": str(index), "digest": "c" * 64}
            for index, (slot, table, _) in enumerate(_COMMITMENT_SLOTS)],
        "producer_registration_digest": "d" * 64, "producer_completion_digest": "e" * 64,
        "stop_digest": None, "unknown_projection_digest": None}


def test_commitment_requires_explicit_version_and_distinct_domain():
    value = commitment_values()
    assert _validate_recovery_commitment(value, _version="v2") is value
    with pytest.raises(RepositorySourceRecoveryError):
        _validate_recovery_commitment(value)


@pytest.mark.parametrize("mutation", ["schema_mix", "domain_mix", "bool_revision", "bool_count",
    "extra", "missing_slot", "duplicate_slot", "foreign_table", "owner_extra"])
def test_v5_commitment_rejects_mixed_or_unreviewed_evidence(mutation):
    value = commitment_values()
    if mutation == "schema_mix": value["schema"] = "repository.recovery_commitment.v1"
    elif mutation == "domain_mix": value["domain"] = "repository.source_recovery_immutable.v1"
    elif mutation == "bool_revision": value["before_revision"] = True
    elif mutation == "bool_count": value["journal_prefix_count"] = True
    elif mutation == "extra": value["derived_digest"] = "f" * 64
    elif mutation == "missing_slot": value["non_root_static"].pop()
    elif mutation == "duplicate_slot": value["non_root_static"][1] = dict(value["non_root_static"][0])
    elif mutation == "foreign_table": value["non_root_static"][0]["table"] = "caller_selected"
    else: value["owner_identity"]["token_hash"] = "private"
    with pytest.raises(RepositorySourceRecoveryError, match="original_recovery_commitment_changed"):
        _validate_recovery_commitment(value, _version="v2")
