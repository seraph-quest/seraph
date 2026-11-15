"""#1199 (ADR-036 slice 2): the credential vault as a pinned, Python-hosted
trust service. The provider worker hosts a mandatory ``vault`` provider with
its own ``seraph.vault.v1`` owner row, frozen in ``seraph-trust`` like tasks;
the row is introduced idempotently onto an existing install without touching a
stored secret; and no vault value, key material or key name reaches the
composition dump, runtime status, provider reports or logs."""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, delete, select, text
from sqlmodel import SQLModel

from config.settings import settings
from src.db.engine import set_state_gate
from src.runtime_plugins import worker as worker_module
from src.runtime_plugins.bridge import CordisHost
from src.runtime_plugins.composition_dump import live_composition_dump, main as dump_main, offline_composition_dump
from src.runtime_plugins.cutover import cutover_manifest, register_cutover_domain, transition_domain
from src.runtime_plugins.ownership import (DOMAINS, PRE_VAULT_DOMAINS, PYTHON_HOSTED_DOMAINS, VAULT_DOMAIN,
                                           CompositionBindingError, CompositionDependency, composition_writer,
                                           introduce_vault_domain_sync, inventory, legacy_composition_digest,
                                           transition_owner)
from src.runtime_plugins.trust import (DOMAIN_BUNDLES, PIN_REFUSED_EVENT, PIN_REFUSED_REASON, PINNED_DOMAINS,
                                       TRUST_BUNDLE, TRUST_SERVICES, is_pinned_domain)
from src.runtime_plugins.worker import (MANDATORY, PYTHON_ONLY, ProviderActivationError, ProviderWorker, attach)
from src.workspace.lifecycle import lifecycle_fence_marker
from tests.cordis_research import seed_composition_inventory
from tests.test_cordis_services_core import service_db  # noqa: F401
from tests.test_inference_accounting import accounting_db  # noqa: F401
from tests.test_research_native_vertical import real_auth  # noqa: F401

OPERATOR_SECRET = "research-vertical-private-secret"
SECRETS = {"vault_1199_canary_name": "vault-1199-value-SHOULD-NEVER-APPEAR",
           "github/1199-token": "ghp_1199_never_in_status_or_logs",
           "mail/1199-password": "mail-1199-private-password"}


@pytest.fixture(autouse=True)
def _detached_gate():
    set_state_gate(None)
    worker_module._set_current(None)
    yield
    set_state_gate(None)
    worker_module._set_current(None)


async def _rows():
    from src.db.engine import get_session
    async with get_session() as db:
        return await inventory(db)


def _dump_domains(dump):
    return {item["runtime_domain"]: item for bundle in dump["bundles"] for item in bundle["domains"]}


async def _seed_vault():
    from src.vault.repository import vault_repository
    for key, value in SECRETS.items():
        await vault_repository.store(key, value, owner_principal_id="operator:root")


def _forbidden():
    from src.vault.crypto import vault_key_material
    material = vault_key_material()
    return [*SECRETS, *SECRETS.values(), material.decode(), base64.urlsafe_b64decode(material).hex()]


# ------------------------------------------------------------------ the pin


@pytest.mark.asyncio
async def test_the_vault_is_a_pinned_trust_service_with_its_own_domain_row():
    [vault] = [item for item in TRUST_SERVICES if item.service == "credential_vault"]
    assert (vault.identity, vault.runtime_domain, vault.owner_row_frozen, vault.pinned_by) == (
        VAULT_DOMAIN, VAULT_DOMAIN, True, "#1199")
    assert VAULT_DOMAIN in DOMAINS and VAULT_DOMAIN not in PRE_VAULT_DOMAINS
    assert is_pinned_domain(VAULT_DOMAIN) and VAULT_DOMAIN in PINNED_DOMAINS
    assert PYTHON_HOSTED_DOMAINS is PINNED_DOMAINS
    assert DOMAIN_BUNDLES[VAULT_DOMAIN] == TRUST_BUNDLE
    assert VAULT_DOMAIN not in {entry.domain for entry in cutover_manifest()}
    with pytest.raises(CompositionBindingError) as refused:
        register_cutover_domain(VAULT_DOMAIN)
    assert refused.value.reason_code == PIN_REFUSED_REASON
    # Python-only: the vault exposes no Cordis service method and has no Cordis plugin.
    from src.runtime_plugins.composition import KNOWN_PLUGINS
    from src.runtime_plugins.contracts import METHOD_DOMAINS
    from src.runtime_plugins.protocol import PROVIDER_DOMAINS
    assert VAULT_DOMAIN not in set(METHOD_DOMAINS.values()) | set(KNOWN_PLUGINS) | PROVIDER_DOMAINS
    assert "vault" in MANDATORY and "vault" in PYTHON_ONLY


@pytest.mark.asyncio
async def test_the_vault_owner_row_is_frozen_but_its_legacy_epoch_fence_still_moves(accounting_db):  # noqa: F811
    from src.db.engine import get_session
    await seed_composition_inventory()
    legacy = CompositionDependency(VAULT_DOMAIN, "legacy", 1, legacy_composition_digest(VAULT_DOMAIN))
    with lifecycle_fence_marker():
        async with get_session() as db:
            async with composition_writer(db):
                await transition_owner(db, runtime_domain=VAULT_DOMAIN, expected=legacy, expected_state="ready",
                                       owner_kind="legacy", epoch=1, composition_digest=legacy.composition_digest,
                                       state="draining")
        for owner_kind, digest in (("cordis", "c" * 64), ("legacy", "d" * 64)):
            async with get_session() as db:
                with pytest.raises(CompositionBindingError) as refused:
                    async with composition_writer(db):
                        await transition_owner(db, runtime_domain=VAULT_DOMAIN, expected=legacy,
                                               expected_state="draining", owner_kind=owner_kind, epoch=2,
                                               composition_digest=digest, state="blocked")
                assert refused.value.reason_code == PIN_REFUSED_REASON
    row = (await _rows())[VAULT_DOMAIN]
    assert (row.owner_kind, row.epoch, row.state) == ("legacy", 1, "draining")


@pytest.mark.asyncio
async def test_transition_domain_and_the_operator_api_refuse_the_vault_with_an_audit_row(
        accounting_db, real_auth, monkeypatch):  # noqa: F811
    from src.api import auth, runtime_composition
    from src.auth.middleware import OperatorAuthMiddleware
    from src.db.engine import get_session
    from src.db.models import AuditEvent
    from src.runtime_plugins import bridge
    await seed_composition_inventory()
    with pytest.raises(CompositionBindingError) as refused:
        await transition_domain(VAULT_DOMAIN, "cordis", host=None, actor="operator:test", reason="probe")
    assert refused.value.reason_code == PIN_REFUSED_REASON
    monkeypatch.setattr(bridge, "cordis_host", CordisHost())
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(runtime_composition.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers={"origin": "http://localhost:3001"}) as client:
        assert (await client.post("/api/auth/login", json={"password": OPERATOR_SECRET})).status_code == 200
        response = await client.post(f"/api/runtime/composition/domains/{VAULT_DOMAIN}/owner",
                                     json={"target": "cordis", "expected_epoch": 1, "confirm_domain": VAULT_DOMAIN})
        assert response.status_code == 409 and response.json()["detail"]["code"] == PIN_REFUSED_REASON
    async with get_session() as db:
        refusals = [json.loads(item) for item in (await db.execute(select(AuditEvent.details_json).where(
            AuditEvent.event_type == PIN_REFUSED_EVENT))).scalars()]
    assert [item["runtime_domain"] for item in refusals] == [VAULT_DOMAIN, VAULT_DOMAIN]
    row = (await _rows())[VAULT_DOMAIN]
    assert (row.owner_kind, row.epoch, row.state) == ("legacy", 1, "ready")


# ------------------------------------------------------------------ the provider


@pytest.mark.asyncio
async def test_the_worker_hosts_the_vault_provider_and_never_reports_it_to_the_child(service_db):  # noqa: F811
    worker = attach(ProviderWorker())
    await worker.activate()
    assert worker.status["vault"].state == "active" and worker.snapshot()["mandatory_ready"] is True
    reports = await worker.provider_reports()
    assert reports and VAULT_DOMAIN not in {item["domain"] for item in reports}
    # Every report the child gets still passes the closed frame check.
    from src.runtime_plugins.protocol import validate_provider_report
    for report in reports:
        validate_provider_report(dict(report), "provider.readiness")


@pytest.mark.asyncio
async def test_a_failing_vault_provider_aborts_startup_typed(async_db, monkeypatch):
    from src.db.engine import state_gate

    async def fail():
        raise ProviderActivationError("vault", "vault_storage_unavailable")
    monkeypatch.setitem(worker_module._PREDICATES, "vault", fail)
    worker = attach(ProviderWorker())
    with pytest.raises(ProviderActivationError) as excinfo:
        await worker.activate()
    assert (excinfo.value.provider, excinfo.value.reason_code) == ("vault", "vault_storage_unavailable")
    assert worker.status["vault"].state == "failed" and worker.snapshot()["mandatory_ready"] is False
    assert state_gate() == ("failed", "vault_storage_unavailable")


@pytest.mark.asyncio
async def test_a_vault_recheck_failure_fails_the_mandatory_provider_and_recovers(async_db, monkeypatch):
    worker = attach(ProviderWorker())
    await worker.activate()
    original = worker_module._PREDICATES["vault"]

    async def fail():
        raise ProviderActivationError("vault", "vault_tables_missing")
    monkeypatch.setitem(worker_module._PREDICATES, "vault", fail)
    snapshot = await worker.readiness()
    assert snapshot["mandatory_ready"] is False
    assert {"provider": "vault", "state": "failed", "reason_code": "vault_tables_missing"} in snapshot["providers"]
    monkeypatch.setitem(worker_module._PREDICATES, "vault", original)
    assert (await worker.readiness())["mandatory_ready"] is True


@pytest.mark.asyncio
async def test_the_vault_predicate_refuses_a_bad_key_and_a_missing_store(async_db, monkeypatch):
    from src.workspace import canonical_workspace_root
    monkeypatch.setattr(settings, "vault_encryption_key", "")
    key = canonical_workspace_root(settings.workspace_dir) / ".vault-key"
    existed = key.exists()
    original = key.read_bytes() if existed else None
    try:
        key.write_text("not-a-fernet-key")
        key.chmod(0o600)
        with pytest.raises(ProviderActivationError) as excinfo:
            await worker_module._vault_predicate()
        assert (excinfo.value.provider, excinfo.value.reason_code) == ("vault", "vault_key_unavailable")
        key.chmod(0o644)
        assert worker_module._vault_key_resolves("vault") == "vault_key_unsafe"
        # Authority keeps its own check and reason codes (#1024): it derives local keys from the vault key.
        assert worker_module._vault_key_resolves() == "authority_vault_key_unsafe"
    finally:
        if existed:
            key.write_bytes(original)
            key.chmod(0o600)
        else:
            key.unlink(missing_ok=True)
    monkeypatch.setattr(worker_module, "_VAULT_TABLES", ("secrets_missing_1199",))
    with pytest.raises(ProviderActivationError) as excinfo:
        await worker_module._vault_predicate()
    assert excinfo.value.reason_code == "vault_tables_missing"


# ------------------------------------------------------------------ upgrade


def _sync_db(path: Path, domains):
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        for domain in domains:
            connection.execute(text(
                "INSERT INTO runtime_composition_states (runtime_domain, owner_kind, epoch, composition_digest, "
                "state, recovery_receipt_ref) VALUES (:d, 'legacy', 1, :g, 'ready', NULL)"),
                {"d": domain, "g": legacy_composition_digest(domain)})
    return engine


def test_the_vault_row_is_introduced_once_onto_the_exact_prior_inventory_only(tmp_path):
    engine = _sync_db(tmp_path / "pre.db", PRE_VAULT_DOMAINS)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO secrets (id, key, encrypted_value, created_at, updated_at) "
                                "VALUES ('s1', 'k1', 'ciphertext-unchanged', '2026-11-01', '2026-11-01')"))
        connection.execute(text("UPDATE runtime_composition_states SET epoch = 3 "
                                "WHERE runtime_domain = 'seraph.memory.v1'"))
    with engine.begin() as connection:
        before = connection.execute(text("SELECT * FROM runtime_composition_states ORDER BY runtime_domain")).all()
        assert introduce_vault_domain_sync(connection) is True
    with engine.begin() as connection:
        assert introduce_vault_domain_sync(connection) is False  # idempotent
        rows = {row[0]: tuple(row[1:]) for row in connection.execute(text(
            "SELECT runtime_domain, owner_kind, epoch, composition_digest, state, recovery_receipt_ref "
            "FROM runtime_composition_states"))}
        after = connection.execute(text("SELECT * FROM runtime_composition_states WHERE runtime_domain != :v "
                                        "ORDER BY runtime_domain"), {"v": VAULT_DOMAIN}).all()
        events = [json.loads(item) for item in connection.execute(text(
            "SELECT details_json FROM audit_events WHERE event_type = 'runtime_composition_domain_introduced'"))
            .scalars()]
        secrets = connection.execute(text("SELECT key, encrypted_value FROM secrets")).all()
    engine.dispose()
    assert set(rows) == set(DOMAINS)
    assert rows[VAULT_DOMAIN] == ("legacy", 1, legacy_composition_digest(VAULT_DOMAIN), "ready", None)
    assert after == before  # no existing owner row changed
    assert events == [{"runtime_domain": VAULT_DOMAIN, "owner_kind": "legacy", "epoch": 1, "introduced_by": "1199",
                       "prior_domains": len(PRE_VAULT_DOMAINS)}]
    assert secrets == [("k1", "ciphertext-unchanged")]


@pytest.mark.parametrize("domains", [
    tuple(domain for domain in PRE_VAULT_DOMAINS if domain != "seraph.memory.v1"),
    tuple(domain for domain in PRE_VAULT_DOMAINS if domain != "seraph.dispatch.v1")],
    ids=["missing-original", "pre-dispatch"])
def test_any_other_inventory_never_gains_the_vault_row(tmp_path, domains):
    engine = _sync_db(tmp_path / "partial.db", domains)
    with engine.begin() as connection:
        assert introduce_vault_domain_sync(connection) is False
        names = {row[0] for row in connection.execute(text("SELECT runtime_domain FROM runtime_composition_states"))}
    engine.dispose()
    assert VAULT_DOMAIN not in names


def test_a_corrupt_prior_inventory_refuses_the_vault_introduction_and_inserts_nothing(tmp_path):
    engine = _sync_db(tmp_path / "corrupt.db", PRE_VAULT_DOMAINS)
    with engine.begin() as connection:
        connection.execute(text("UPDATE runtime_composition_states SET owner_kind = 'cordis' "
                                "WHERE runtime_domain = 'seraph.tasks.v1'"))
    with pytest.raises(CompositionBindingError) as refused:
        with engine.begin() as connection:
            introduce_vault_domain_sync(connection)
    assert refused.value.reason_code == "composition_inventory_invalid"
    with engine.connect() as connection:
        names = {row[0] for row in connection.execute(text("SELECT runtime_domain FROM runtime_composition_states"))}
        events = connection.execute(text("SELECT COUNT(*) FROM audit_events")).scalar_one()
    engine.dispose()
    assert VAULT_DOMAIN not in names and events == 0


@pytest.mark.asyncio
async def test_init_db_upgrades_an_existing_vault_install_and_its_secrets_stay_readable(
        service_db, monkeypatch):  # noqa: F811
    from src.db import engine as db_engine
    from src.db.engine import get_session, init_db
    from src.db.models import AuditEvent, RuntimeCompositionState, Secret
    from src.vault.repository import vault_repository
    from tests.test_cordis_services_core import _engine
    # ``init_db`` migrates the module engine: point it at this test's workspace database.
    migration_engine = _engine(service_db)
    monkeypatch.setattr(db_engine, "engine", migration_engine)
    await init_db()
    await _seed_vault()
    async with get_session() as db:
        # A pre-#1199 install: the exact post-#1025 inventory, with stored secrets.
        await db.execute(delete(RuntimeCompositionState).where(RuntimeCompositionState.runtime_domain == VAULT_DOMAIN))
        stored = {row.key: row.encrypted_value for row in (await db.execute(select(Secret))).scalars()}
    for _ in range(2):
        await init_db()
    async with get_session() as db:
        rows = {row.runtime_domain: row for row in (await db.execute(select(RuntimeCompositionState))).scalars()}
        events = [json.loads(item) for item in (await db.execute(select(AuditEvent.details_json).where(
            AuditEvent.event_type == "runtime_composition_domain_introduced"))).scalars()]
        after = {row.key: row.encrypted_value for row in (await db.execute(select(Secret))).scalars()}
    assert set(rows) == set(DOMAINS)
    assert (rows[VAULT_DOMAIN].owner_kind, rows[VAULT_DOMAIN].epoch, rows[VAULT_DOMAIN].state) == ("legacy", 1, "ready")
    assert [item["introduced_by"] for item in events if item["runtime_domain"] == VAULT_DOMAIN] == ["1199"]
    assert after == stored
    for key, value in SECRETS.items():
        assert await vault_repository.get(key, owner_principal_id="operator:root") == value
    worker = attach(ProviderWorker())
    await worker.activate()
    assert worker.status["vault"].state == "active"
    await migration_engine.dispose()


# ------------------------------------------------------------------ secret-free


@pytest.mark.asyncio
async def test_a_vault_seeded_workspace_leaks_nothing_into_dump_status_reports_or_logs(
        accounting_db, real_auth, monkeypatch, caplog, capsys):  # noqa: F811
    import src.app as app_module
    from src.api import auth, runtime_composition
    from src.auth.middleware import OperatorAuthMiddleware
    from src.runtime_plugins import bridge
    from src.runtime_plugins.guardian_turns import runtime_domains_status
    await seed_composition_inventory()
    await _seed_vault()
    # Capture from here on: only the provider, dump and status reads are under test.
    caplog.clear()
    caplog.set_level(logging.DEBUG)
    forbidden = _forbidden()
    worker = attach(ProviderWorker())
    await worker.activate()
    snapshot = await worker.readiness(probe_writes=True)
    assert worker.status["vault"].state == "active"
    reports = await worker.provider_reports()
    live = await live_composition_dump(CordisHost())
    workspace = Path(settings.workspace_dir)
    offline = offline_composition_dump(workspace)
    assert dump_main(["dump", "--workspace", str(workspace)]) == 0
    printed = capsys.readouterr().out
    for dump in (live, offline):
        entry = _dump_domains(dump)[VAULT_DOMAIN]
        assert (entry["pinned"], entry["owner_kind"], entry["epoch"], entry["state"]) == (True, "legacy", 1, "ready")
        assert entry["owner_digest"] == legacy_composition_digest(VAULT_DOMAIN)
        assert entry["provider_digest"] is None  # no Cordis plugin: Python-hosted only
        assert VAULT_DOMAIN in dump["pinned_domains"]
        [vault] = [item for item in dump["trust_services"] if item["service"] == "credential_vault"]
        assert vault["owner_row_frozen"] is True and vault["runtime_domain"] == VAULT_DOMAIN
    monkeypatch.setattr(bridge, "cordis_host", CordisHost())
    monkeypatch.setattr(app_module, "cordis_host", CordisHost())
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(runtime_composition.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers={"origin": "http://localhost:3001"}) as client:
        assert (await client.post("/api/auth/login", json={"password": OPERATOR_SECRET})).status_code == 200
        served = await client.get("/api/runtime/composition/dump")
        domains = await client.get("/api/runtime/composition/domains")
    assert served.status_code == 200 and domains.status_code == 200
    status_parts = [await app_module._composition_dump_or_unavailable(), await runtime_domains_status(CordisHost()),
                    await app_module._provider_readiness(type("App", (), {"state": type("S", (), {
                        "provider_worker": worker})()})())]
    texts = [json.dumps(item, sort_keys=True) for item in (live, offline, snapshot, reports, *status_parts)]
    texts += [printed, served.text, domains.text, caplog.text]
    for blob in texts:
        for value in forbidden:
            assert value not in blob
