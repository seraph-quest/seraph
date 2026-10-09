"""Actual raw certificate/conflict mechanics; no execution Source is minted."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import shutil
import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlmodel import SQLModel, Session

from src.db.models import Goal, OperatorIdentity, OperatorSession, WorkflowRunState
from src.goals.contracts import GoalProgramme, GoalProgrammeAuthorityBinding
from src.work_board.research_parent import GoalDiscoveryAuthority, DISCOVERY_KIND, DISCOVERY_SERVICE
from src.runtime_plugins.ownership import RuntimeCompositionBinding, CompositionDependency, method_closure, method_dependencies
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError, COMPOSITION_DESCRIPTORS
from src.memory.composition_headers import read_programme_identity, _validate
from src.workflows.job_runtime import _digest
from src.workspace.accounting_continuity import _RawRollbackPair


def test_fresh_writer_recompares_real_concurrent_goal_change(selected_paths, monkeypatch):
    pair = _RawRollbackPair(*selected_paths, HeaderReadBudget())
    try:
        pair.preflight_programme_conflicts()
        original = pair.destination.rollback
        def concurrent_change():
            original()
            with sqlite3.connect(selected_paths[1]) as writer:
                writer.execute("UPDATE goals SET status='paused' WHERE id='original-goal'")
        monkeypatch.setattr(pair.destination, "rollback", concurrent_change)
        with pytest.raises(HeaderBoundsError, match="programme_goal_raw_conflict"):
            pair.begin_current_writer()
        assert pair.destination._db.total_changes == 0
        with sqlite3.connect(selected_paths[1]) as readback:
            assert readback.execute("SELECT status FROM goals WHERE id='original-goal'").fetchone() == ("paused",)
    finally:
        pair.close()


def test_browser_tombstone_remains_original_programme_provenance(selected_paths):
    for path in selected_paths:
        with sqlite3.connect(path) as fixture:
            fixture.execute("UPDATE operator_sessions SET is_bearer_tombstone=1,revoked_at='2026-10-10 12:00:00' WHERE id='original-issuer'")
    pair = _RawRollbackPair(*selected_paths, HeaderReadBudget())
    try:
        _source, _destination, selection, _identities = pair.preflight_programme_conflicts()
        assert selection.issuers["original-issuer"]["is_bearer_tombstone"] == 1
        assert selection.identity_ids == ("original-identity",)
        assert pair.destination._db.total_changes == 0
    finally:
        pair.close()


def test_copied_selection_cannot_issue_identity_component(selected_paths):
    from dataclasses import replace
    from src.memory.composition_headers import preflight_programme_identity_component
    pair = _RawRollbackPair(*selected_paths, HeaderReadBudget())
    try:
        source, _destination, selection, _identities = pair.preflight_programme_conflicts()
        copied = replace(selection)
        with pytest.raises(HeaderBoundsError, match="programme_original_selection_unavailable"):
            preflight_programme_identity_component(source, original_selection=copied)
        assert pair.destination._db.total_changes == 0
    finally:
        pair.close()


@pytest.fixture
def selected_paths(tmp_path):
    source, destination = tmp_path / "source.sqlite3", tmp_path / "destination.sqlite3"
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    identity = OperatorIdentity(id="original-identity", created_at=now)
    issuer = OperatorSession(id="original-issuer", token_hash="original-private-token-hash",
        principal_id="operator:original", operator_identity_id=identity.id,
        created_at=now, last_seen_at=now, idle_expires_at=now + timedelta(hours=1),
        absolute_expires_at=now + timedelta(hours=2))
    brief = "Original public brief"
    programme = GoalProgramme(id="1" * 32, goal_id="original-goal", goal_revision=1,
        public_brief=brief, brief_digest=hashlib.sha256(brief.encode()).hexdigest(),
        grant_revision=1, expires_at=now + timedelta(days=1), confirmed_at=now,
        capability_ids=["guardian.goal-discovery.v1"], budget={"max_inference_microusd": 1000},
        notification_limits={"per_day": 0}, state="active", artifact_prefix="goal-programmes/" + "1" * 32 + "/",
        owner_identity_id=identity.id, issuer_root_id=issuer.id, issuer_principal_id=issuer.principal_id,
        route_epoch=1, route_digest="original-route", review_digest="original-review")
    goal = Goal(id=programme.goal_id, title="Original raw goal", owner_principal_id=issuer.principal_id,
        owner_session_id=issuer.id, goal_programmes_json=json.dumps({"revision": 1, "preview": None,
            "generations": [programme.model_dump(mode="json")]}), created_at=now, updated_at=now)
    binding = GoalProgrammeAuthorityBinding.from_programme(programme, "guardian.goal-discovery.v1")
    job_id = "goal-discovery:" + "a" * 32
    authority = GoalDiscoveryAuthority(authority_type="goal_programme_discovery_v1", principal=DISCOVERY_SERVICE,
        owner_kind="service", service_id=DISCOVERY_SERVICE, capability_id="guardian.goal-discovery.v1",
        capability_version="1", goal_owner_principal_id=issuer.principal_id, goal_owner_session_id=issuer.id,
        programme_binding=binding, plan_ref={"artifact_id": "original-plan", "digest": "b" * 64, "schema_version": 1},
        occurrence_day="2026-10-10", original_job_id=job_id, budget_microusd=1000, no_learning=True)
    methods = method_closure("research.executeAccepted", "public_research")
    domains = set(method_dependencies("research.executeAccepted", native_branch="public_research", programme_bound=True))
    for method in methods:
        domains.update(method_dependencies(method))
    composition = RuntimeCompositionBinding("seraph.research.v1", "research.executeAccepted", "public_research",
        methods, tuple(CompositionDependency(domain, "cordis", 1, "c" * 64) for domain in sorted(domains)),
        "d" * 64, "e" * 64)
    inputs = {"native_mechanics_only": True}
    raw_authority = authority.model_dump(mode="json")
    # Original parentless native constructor uses its own job ID as root and
    # records the native kind in both workflow/tool projections. Data only:
    # this fixture does not execute admission or issue a Source.
    run = WorkflowRunState(run_identity=job_id, root_run_identity=job_id,
        workflow_name=DISCOVERY_KIND, tool_name=DISCOVERY_KIND,
        job_kind=DISCOVERY_KIND, capability_version=authority.capability_version, owner_kind="service",
        owner_principal_id=DISCOVERY_SERVICE, service_id=DISCOVERY_SERVICE,
        goal_id=goal.id, goal_revision=1, composition_binding_json=composition.to_json(),
        declared_authority_json=json.dumps(raw_authority), authority_digest=_digest(raw_authority),
        arguments_json=json.dumps(inputs), input_digest=_digest(inputs), status="blocked")
    engine = create_engine("sqlite:///" + str(source))
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.add_all([identity, issuer, goal, run])
            session.commit()
    finally:
        engine.dispose()
    shutil.copyfile(source, destination)
    return source, destination


def test_named_identity_and_fresh_writer_share_exact_frame(selected_paths):
    budget = HeaderReadBudget()
    pair = _RawRollbackPair(*selected_paths, budget)
    try:
        source, destination, selection, compared = pair.preflight_programme_conflicts()
        assert "operator_identities" not in COMPOSITION_DESCRIPTORS
        source_identity, target_identity, source_rows, target_rows = compared
        assert selection.identity_ids == ("original-identity",)
        assert source_rows == target_rows
        assert source_identity.common_certificate is source
        assert target_identity.common_certificate is destination
        assert source_identity.budget is target_identity.budget is budget
        spent = budget.remaining
        _source, fresh_destination, fresh_selection, fresh_compared = pair.begin_current_writer()
        assert fresh_destination.connection is destination.connection
        assert fresh_destination.transaction is not destination.transaction
        assert fresh_selection is not selection and budget.remaining < spent
        with pytest.raises(HeaderBoundsError):
            _validate(destination.connection, target_identity)
        assert read_programme_identity(fresh_compared[1], "original-identity") == source_rows["original-identity"]
    finally:
        pair.close()


@pytest.mark.parametrize("table,field,value", [
    ("goals", "title", "Harmless metadata difference"),
    ("goals", "status", "paused"), ("goals", "revision", 2),
    ("goals", "owner_principal_id", "other-owner"),
    ("operator_identities", "revoked_at", "2026-10-10 01:00:00.000000"),
    ("operator_identities", "created_at", "2026-10-09 01:00:00.000000"),
])
def test_complete_raw_conflict_stops_before_effect(selected_paths, table, field, value):
    with sqlite3.connect(selected_paths[1]) as connection:
        connection.execute(f'UPDATE "{table}" SET "{field}"=?', (value,))
    before = selected_paths[1].read_bytes()
    pair = _RawRollbackPair(*selected_paths, HeaderReadBudget())
    try:
        with pytest.raises(HeaderBoundsError, match="programme_.*_raw_conflict"):
            pair.preflight_programme_conflicts()
        assert pair.destination._db.total_changes == 0
    finally:
        pair.close()
    assert selected_paths[1].read_bytes() == before


def test_absent_destination_identity_reserves_actual_original_frame(selected_paths):
    with sqlite3.connect(selected_paths[1]) as connection:
        connection.execute("DELETE FROM operator_identities")
    pair = _RawRollbackPair(*selected_paths, HeaderReadBudget())
    try:
        pair.preflight_programme_conflicts()
        _source, _destination, _selection, compared = pair.begin_current_writer()
        assert compared[3]["original-identity"] is None
        spent = pair.destination._budget.remaining
        pair.reserve_programme_copies()
        expected = (pair.destination._namespace, "operator_identities", "id", "original-identity")
        assert expected in pair.destination._budget.future_references
        assert pair.destination._budget.remaining < spent
        assert pair.destination._db.total_changes == 0
    finally:
        pair.close()


@pytest.mark.parametrize("change", ["missing-source", "extra-column", "extra-index", "trigger"])
def test_identity_missing_or_unsupported_blocks_before_identity_body(selected_paths, change):
    source = selected_paths[0]
    with sqlite3.connect(source) as connection:
        if change == "missing-source":
            connection.execute("DELETE FROM operator_identities")
        elif change == "extra-column":
            connection.execute("ALTER TABLE operator_identities ADD COLUMN forbidden TEXT")
        elif change == "extra-index":
            connection.execute("CREATE INDEX forbidden_identity_index ON operator_identities(created_at)")
        else:
            connection.execute("CREATE TRIGGER forbidden_identity_trigger AFTER UPDATE ON operator_identities BEGIN SELECT 1; END")
    before = selected_paths[1].read_bytes()
    pair = _RawRollbackPair(*selected_paths, HeaderReadBudget())
    statements = []
    try:
        original = pair.source._trace
        def observe(statement):
            statements.append(statement)
            original(statement)
        pair.source._db.set_trace_callback(observe)
        with pytest.raises(HeaderBoundsError):
            pair.preflight_programme_conflicts()
        assert not any(sql.startswith('SELECT "id","created_at","revoked_at" FROM operator_identities')
            for sql in statements)
    finally:
        pair.close()
    assert selected_paths[1].read_bytes() == before
