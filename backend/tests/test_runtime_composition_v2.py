"""#1024 A3.1: the state domain, the v2 method manifest with its frozen v1 decoder,
the one-row migration and Python-hosted owners that never move to Cordis."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlmodel import SQLModel

from src.db.engine import get_session
from src.db.models import AuditEvent, RuntimeCompositionState, WorkflowRunState
from src.runtime_plugins.cutover import register_cutover_domain
from src.runtime_plugins.ownership import (
    DOMAINS, DOMAINS_V1, MANIFEST_VERSION, MANIFEST_VERSION_V1, METHOD_DEPENDENCIES_V1, PYTHON_HOSTED_DOMAINS,
    STATE_DOMAIN, STATE_DOMAIN_EVENT, CompositionBindingError, CompositionDependency, RuntimeCompositionBinding,
    bind_invocation, composition_fence_predicates, composition_writer, legacy_composition_digest,
    migrate_state_domain_sync, seed_legacy_inventory_sync, transition_owner)
from src.workspace.lifecycle import lifecycle_fence_marker
from tests.test_cordis_services_core import service_db  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures" / "runtime_composition" / "v1"


def _fixtures():
    return sorted(FIXTURES.glob("*.json"))


def test_frozen_domain_and_manifest_tables():
    assert DOMAINS == (STATE_DOMAIN, *DOMAINS_V1) and len(DOMAINS_V1) == 14
    assert STATE_DOMAIN not in DOMAINS_V1
    assert PYTHON_HOSTED_DOMAINS == {STATE_DOMAIN, "seraph.authority.v1", "seraph.audit.v1"}
    assert MANIFEST_VERSION == "runtime-service-methods.v2" and MANIFEST_VERSION_V1 == "runtime-service-methods.v1"
    assert all(STATE_DOMAIN not in deps for deps in METHOD_DEPENDENCIES_V1.values())


@pytest.mark.parametrize("path", _fixtures(), ids=lambda path: path.stem)
def test_committed_v1_bindings_round_trip_byte_identically(path):
    raw = path.read_text(encoding="utf-8").strip()
    binding = RuntimeCompositionBinding.from_json(raw)
    assert binding.method_manifest_version == MANIFEST_VERSION_V1
    assert STATE_DOMAIN not in {item.runtime_domain for item in binding.dependency_vector}
    assert binding.to_json() == raw
    assert binding.binding_digest == json.loads(raw)["binding_digest"]
    # A v1 binding cannot be relabelled as v2 (or the reverse) without its digest failing.
    relabelled = json.loads(raw) | {"method_manifest_version": MANIFEST_VERSION}
    with pytest.raises(CompositionBindingError):
        RuntimeCompositionBinding.from_json(json.dumps(relabelled))


def test_fixture_set_covers_every_native_branch():
    branches = {RuntimeCompositionBinding.from_json(path.read_text().strip()).native_branch for path in _fixtures()}
    assert {"base", "artifact", "workflow", "governed", "agent_loop", "programme_discovery"} <= branches


@pytest.mark.asyncio
async def test_new_bindings_are_v2_and_depend_on_state(service_db):
    async with get_session() as db:
        binding = await bind_invocation(db, method="memory.retrieve", native_branch="artifact")
    assert binding.method_manifest_version == MANIFEST_VERSION
    assert STATE_DOMAIN in {item.runtime_domain for item in binding.dependency_vector}
    assert RuntimeCompositionBinding.from_json(binding.to_json()) == binding


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "unknown_external_effect", "cost_liability"])
async def test_persisted_v1_bindings_still_match_their_cas_predicates(service_db, status):
    """Pre-upgrade live and liability jobs keep settling against unchanged rows."""
    raw = (FIXTURES / "memory_retrieve__artifact.json").read_text().strip()
    async with get_session() as db:
        db.add(WorkflowRunState(run_identity=f"v1-{status}", root_run_identity=f"v1-{status}",
                                workflow_name="v1", tool_name="v1", status=status, job_kind="runtime_service_read_v1",
                                capability_version="1", composition_binding_json=raw,
                                started_at=datetime.now(timezone.utc)))
    async with get_session() as db:
        matched = (await db.execute(select(WorkflowRunState.run_identity).where(
            WorkflowRunState.run_identity == f"v1-{status}", *composition_fence_predicates(raw)))).scalars().all()
    assert matched == [f"v1-{status}"]


async def _cutover_to_blocked(domain, *, owner_kind="legacy", digest=None):
    async with get_session() as db:
        row = await db.get(RuntimeCompositionState, domain, populate_existing=True)
        expected = CompositionDependency(row.runtime_domain, row.owner_kind, row.epoch, row.composition_digest)
    with lifecycle_fence_marker():
        async with get_session() as db:
            async with composition_writer(db):
                await transition_owner(db, runtime_domain=domain, expected=expected, expected_state="ready",
                                       owner_kind=expected.owner_kind, epoch=expected.epoch,
                                       composition_digest=expected.composition_digest, state="draining")
                await transition_owner(db, runtime_domain=domain, expected=expected, expected_state="draining",
                                       owner_kind=owner_kind, epoch=expected.epoch + 1,
                                       composition_digest=digest or expected.composition_digest, state="blocked")


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", sorted(PYTHON_HOSTED_DOMAINS))
async def test_python_hosted_owners_never_move_to_cordis(service_db, domain):
    with pytest.raises(CompositionBindingError) as excinfo:
        await _cutover_to_blocked(domain, owner_kind="cordis", digest="c" * 64)
    assert excinfo.value.reason_code == "composition_owner_not_transferable"
    with pytest.raises(CompositionBindingError):
        register_cutover_domain(domain)
    # A legacy epoch change (maintenance) remains possible.
    await _cutover_to_blocked(domain)


@pytest.mark.asyncio
@pytest.mark.parametrize("status, refused", [("running", True), ("unknown_external_effect", True),
                                             ("cost_liability", True), ("queued", False)])
async def test_undecodable_binding_on_live_or_liability_work_refuses_quiescence(service_db, status, refused):
    async with get_session() as db:
        db.add(WorkflowRunState(run_identity=f"corrupt-{status}", root_run_identity=f"corrupt-{status}",
                                workflow_name="corrupt", tool_name="corrupt", status=status,
                                job_kind="runtime_service_read_v1", capability_version="1",
                                composition_binding_json='{"not":"a binding"}', started_at=datetime.now(timezone.utc)))
    if refused:
        with pytest.raises(CompositionBindingError) as excinfo:
            await _cutover_to_blocked("seraph.memory.v1")
        assert excinfo.value.reason_code == "composition_not_quiescent"
    else:
        # Unstarted work keeps today's rule: blocked visibly, never stranded.
        await _cutover_to_blocked("seraph.memory.v1")
        async with get_session() as db:
            row = (await db.execute(select(WorkflowRunState).where(
                WorkflowRunState.run_identity == f"corrupt-{status}"))).scalar_one()
        assert (row.status, row.failure_reason) == ("blocked", "composition_owner_changed")


# ------------------------------------------------------------------ migration

def _sync_db(path: Path, domains):
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        for domain in domains:
            connection.execute(text(
                "INSERT INTO runtime_composition_states (runtime_domain, owner_kind, epoch, composition_digest, "
                "state, recovery_receipt_ref) VALUES (:d, 'legacy', 1, :g, 'ready', NULL)"),
                {"d": domain, "g": legacy_composition_digest(domain) if domain in DOMAINS else "e" * 64})
    return engine


def _rows(engine):
    with engine.connect() as connection:
        domains = connection.execute(text("SELECT runtime_domain FROM runtime_composition_states")).scalars().all()
        events = connection.execute(text("SELECT details_json FROM audit_events WHERE event_type = :e"),
                                    {"e": STATE_DOMAIN_EVENT}).scalars().all()
    return set(domains), events


def test_exact_pre_a31_inventory_gains_one_state_row_and_audit_event(tmp_path):
    engine = _sync_db(tmp_path / "pre.db", DOMAINS_V1)
    with engine.begin() as connection:
        assert seed_legacy_inventory_sync(connection) == 0
        assert migrate_state_domain_sync(connection) is True
    with engine.begin() as connection:
        assert migrate_state_domain_sync(connection) is False  # forward-only, once
    domains, events = _rows(engine)
    assert domains == set(DOMAINS) and len(events) == 1
    assert json.loads(events[0])["runtime_domain"] == STATE_DOMAIN


def test_empty_inventory_is_seeded_whole_including_state(tmp_path):
    engine = _sync_db(tmp_path / "fresh.db", ())
    with engine.begin() as connection:
        assert seed_legacy_inventory_sync(connection) == len(DOMAINS) == 15
        assert migrate_state_domain_sync(connection) is False
    assert _rows(engine) == (set(DOMAINS), [])


@pytest.mark.parametrize("domains", [DOMAINS_V1[:-1], (*DOMAINS_V1[:-1], "seraph.unknown.v1")],
                         ids=["missing-one", "unknown-one"])
def test_any_other_partial_inventory_is_left_alone(tmp_path, domains):
    engine = _sync_db(tmp_path / "partial.db", domains)
    with engine.begin() as connection:
        assert seed_legacy_inventory_sync(connection) == 0
        assert migrate_state_domain_sync(connection) is False
    assert _rows(engine) == (set(domains), [])


@pytest.mark.asyncio
async def test_init_db_runs_the_migration_on_a_pre_a31_database():
    """The next backend start after restoring a pre-#1024 backup gains the row."""
    from src.db.engine import init_db
    await init_db()
    async with get_session() as db:
        await db.execute(delete(RuntimeCompositionState).where(RuntimeCompositionState.runtime_domain == STATE_DOMAIN))
    await init_db()
    async with get_session() as db:
        rows = {row.runtime_domain: row for row in (await db.execute(select(RuntimeCompositionState))).scalars()}
        events = (await db.execute(select(AuditEvent).where(AuditEvent.event_type == STATE_DOMAIN_EVENT))).scalars().all()
    assert set(rows) == set(DOMAINS)
    assert (rows[STATE_DOMAIN].owner_kind, rows[STATE_DOMAIN].epoch, rows[STATE_DOMAIN].state) == ("legacy", 1, "ready")
    assert events


def test_composition_rows_epochs_and_migration_survive_cli_backup_and_restore(tmp_path):
    """Backup and restore stay with the host CLI (v2 §5); a restored pre-#1024
    inventory gains its state row on the next start."""
    import os
    import sqlite3
    from tests.test_workspace_production import ROOT, _cli, _env, _workspace
    root = _workspace(tmp_path)
    (root / "seraph.db").unlink()
    engine = _sync_db(root / "seraph.db", DOMAINS_V1)
    with engine.begin() as connection:
        connection.execute(text("UPDATE runtime_composition_states SET epoch = 3 "
                                "WHERE runtime_domain = 'seraph.memory.v1'"))
    engine.dispose()
    environment = {**os.environ, **_env(root), "PYTHONPATH": str(ROOT / "backend")}
    archive = _cli(tmp_path, environment, "backup", "--archive", "pre-a31.zip")
    with sqlite3.connect(root / "seraph.db") as database:
        database.execute("DELETE FROM runtime_composition_states")
    _cli(tmp_path, environment, "restore", "--archive", archive["archive_path"], "--confirm")
    restored = create_engine(f"sqlite:///{root / 'seraph.db'}")
    with restored.connect() as connection:
        epochs = dict(connection.execute(text(
            "SELECT runtime_domain, epoch FROM runtime_composition_states")).all())
    assert set(epochs) == set(DOMAINS_V1) and epochs["seraph.memory.v1"] == 3
    with restored.begin() as connection:
        assert migrate_state_domain_sync(connection) is True
    domains, events = _rows(restored)
    restored.dispose()
    assert domains == set(DOMAINS) and len(events) == 1
