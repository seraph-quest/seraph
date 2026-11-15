"""#1198 (ADR-036 slice 1): the pinned ``seraph.tasks.v1`` owner row, the one pin
predicate the cutover code consults, and the read-only, secret-free composition
dump (runtime status, the sibling route and ``./manage.sh -e dev composition dump``).
Real SQLite, real fenced owner transitions, a real vault-seeded workspace."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from tests.cordis_research import seed_composition_inventory
from tests.test_inference_accounting import accounting_db  # noqa: F401
from tests.test_research_native_vertical import real_auth  # noqa: F401
from src.runtime_plugins.bridge import CordisHost
from src.runtime_plugins.composition_dump import live_composition_dump, main as dump_main, offline_composition_dump
from src.runtime_plugins.cutover import cutover_manifest, register_cutover_domain, transition_domain
from src.runtime_plugins.ownership import (DOMAINS, PYTHON_HOSTED_DOMAINS, CompositionBindingError,
                                           CompositionDependency, composition_writer, inventory,
                                           legacy_composition_digest, transition_owner)
from src.runtime_plugins.trust import (DOMAIN_BUNDLES, PIN_REFUSED_EVENT, PIN_REFUSED_REASON, PINNED_DOMAINS,
                                       TRUST_SERVICES, is_pinned_domain)
from src.workspace.lifecycle import lifecycle_fence_marker

pytestmark = pytest.mark.asyncio
TASKS = "seraph.tasks.v1"
REPO = Path(__file__).resolve().parents[2]
OPERATOR_SECRET = "research-vertical-private-secret"


async def _rows():
    from src.db.engine import get_session
    async with get_session() as db:
        return await inventory(db)


async def _refusals():
    from src.db.engine import get_session
    from src.db.models import AuditEvent
    async with get_session() as db:
        return [json.loads(row) for row in (await db.execute(select(AuditEvent.details_json).where(
            AuditEvent.event_type == PIN_REFUSED_EVENT))).scalars()]


def _dump_domains(dump):
    return {item["runtime_domain"]: item for bundle in dump["bundles"] for item in bundle["domains"]}


async def test_pinned_set_is_the_adr_table_and_every_cutover_path_consults_it():
    # #1199 adds the credential vault (ADR-036 slice 2).
    assert PINNED_DOMAINS == {"seraph.state.v1", "seraph.authority.v1", "seraph.audit.v1", TASKS, "seraph.vault.v1"}
    assert PYTHON_HOSTED_DOMAINS is PINNED_DOMAINS
    assert all(is_pinned_domain(domain) for domain in PINNED_DOMAINS)
    assert not is_pinned_domain("seraph.inference.v1") and not is_pinned_domain("seraph.agent-loop.v1")
    # The ADR names inference, the execution guard and authentication; their pins are targets.
    assert {item.service for item in TRUST_SERVICES if not item.owner_row_frozen} == {
        "inference_policy", "execution_guard", "operator_authentication"}
    # Every runtime domain has exactly one declared bundle, and the pinned ones are seraph-trust.
    assert set(DOMAIN_BUNDLES) == set(DOMAINS)
    assert all(DOMAIN_BUNDLES[domain] == "seraph-trust" for domain in PINNED_DOMAINS)
    assert not any(is_pinned_domain(entry.domain) for entry in cutover_manifest())
    for domain in PINNED_DOMAINS:
        with pytest.raises(CompositionBindingError) as refused:
            register_cutover_domain(domain)
        assert refused.value.reason_code == PIN_REFUSED_REASON


async def test_tasks_owner_row_is_frozen_but_its_legacy_epoch_fence_still_moves(accounting_db):  # noqa: F811
    await seed_composition_inventory()
    legacy = CompositionDependency(TASKS, "legacy", 1, legacy_composition_digest(TASKS))
    with lifecycle_fence_marker():
        from src.db.engine import get_session
        async with get_session() as db:
            async with composition_writer(db):
                await transition_owner(db, runtime_domain=TASKS, expected=legacy, expected_state="ready",
                                       owner_kind="legacy", epoch=1, composition_digest=legacy.composition_digest,
                                       state="draining")
        # A transfer to Cordis, or to any non-legacy digest, is refused before any write.
        for owner_kind, digest in (("cordis", "c" * 64), ("legacy", "d" * 64)):
            async with get_session() as db:
                with pytest.raises(CompositionBindingError) as refused:
                    async with composition_writer(db):
                        await transition_owner(db, runtime_domain=TASKS, expected=legacy, expected_state="draining",
                                               owner_kind=owner_kind, epoch=2, composition_digest=digest,
                                               state="blocked")
                assert refused.value.reason_code == PIN_REFUSED_REASON
        # The legacy epoch fence is not a transfer.
        async with get_session() as db:
            async with composition_writer(db):
                await transition_owner(db, runtime_domain=TASKS, expected=legacy, expected_state="draining",
                                       owner_kind="legacy", epoch=2, composition_digest=legacy.composition_digest,
                                       state="blocked")
    row = (await _rows())[TASKS]
    assert (row.owner_kind, row.epoch, row.state, row.composition_digest) == (
        "legacy", 2, "blocked", legacy.composition_digest)


async def test_a_tampered_cordis_tasks_row_fails_the_inventory_closed(accounting_db):  # noqa: F811
    from src.db.engine import get_session
    from src.db.models import RuntimeCompositionState
    await seed_composition_inventory()
    async with get_session() as db:
        await db.execute(update(RuntimeCompositionState).where(RuntimeCompositionState.runtime_domain == TASKS)
                         .values(owner_kind="cordis", epoch=2, composition_digest="c" * 64))
    with pytest.raises(CompositionBindingError) as invalid:
        await _rows()
    assert invalid.value.reason_code == "composition_inventory_invalid"
    dump = await live_composition_dump(CordisHost())
    assert dump["inventory"] == {"state": "invalid", "reason": "composition_inventory_invalid"}


async def test_transition_domain_refuses_tasks_and_leaves_an_audit_row(accounting_db):  # noqa: F811
    await seed_composition_inventory()
    before = (await _rows())[TASKS]
    for target in ("cordis", "legacy"):
        with pytest.raises(CompositionBindingError) as refused:
            await transition_domain(TASKS, target, host=None, actor="operator:test", reason="probe")
        assert refused.value.reason_code == PIN_REFUSED_REASON
    after = (await _rows())[TASKS]
    assert (after.owner_kind, after.epoch, after.state) == (before.owner_kind, before.epoch, before.state)
    assert await _refusals() == [
        {"runtime_domain": TASKS, "target": target, "path": "transition_domain", "reason_code": PIN_REFUSED_REASON}
        for target in ("cordis", "legacy")]


async def test_operator_api_refuses_a_pinned_transfer_with_its_reason_and_serves_the_dump(
        accounting_db, real_auth, monkeypatch):  # noqa: F811
    from src.api import auth, runtime_composition
    from src.auth.middleware import OperatorAuthMiddleware
    from src.runtime_plugins import bridge
    await seed_composition_inventory()
    monkeypatch.setattr(bridge, "cordis_host", CordisHost())
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(runtime_composition.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers={"origin": "http://localhost:3001"}) as client:
        path = f"/api/runtime/composition/domains/{TASKS}/owner"
        body = {"target": "cordis", "expected_epoch": 1, "confirm_domain": TASKS}
        assert (await client.post(path, json=body)).status_code == 401
        assert (await client.get("/api/runtime/composition/dump")).status_code == 401
        assert (await client.post("/api/auth/login", json={"password": OPERATOR_SECRET})).status_code == 200
        refused = await client.post(path, json=body)
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == PIN_REFUSED_REASON
        dump = await client.get("/api/runtime/composition/dump")
        assert dump.status_code == 200
        assert _dump_domains(dump.json())[TASKS]["pinned"] is True
    [audit] = await _refusals()
    assert audit["runtime_domain"] == TASKS and audit["reason_code"] == PIN_REFUSED_REASON
    row = (await _rows())[TASKS]
    assert (row.owner_kind, row.epoch) == ("legacy", 1)


async def test_the_dump_matches_the_actual_domain_rows(accounting_db):  # noqa: F811
    await seed_composition_inventory()
    # Move one fence so the rows are not all identical seeds.
    legacy = CompositionDependency(TASKS, "legacy", 1, legacy_composition_digest(TASKS))
    with lifecycle_fence_marker():
        from src.db.engine import get_session
        async with get_session() as db:
            async with composition_writer(db):
                await transition_owner(db, runtime_domain=TASKS, expected=legacy, expected_state="ready",
                                       owner_kind="legacy", epoch=1, composition_digest=legacy.composition_digest,
                                       state="draining")
    rows = await _rows()
    from config.settings import settings
    for dump in (await live_composition_dump(CordisHost()),
                 offline_composition_dump(Path(settings.workspace_dir))):
        assert dump["schema"] == "seraph-composition-dump.v1"
        assert dump["inventory"] == {"state": "ok", "reason": None}
        domains = _dump_domains(dump)
        assert list(domains) == [domain for bundle in dict.fromkeys(DOMAIN_BUNDLES.values())
                                 for domain in DOMAINS if DOMAIN_BUNDLES[domain] == bundle]
        assert set(domains) == set(DOMAINS) == set(rows)
        for domain, row in rows.items():
            entry = domains[domain]
            assert (entry["owner_kind"], entry["epoch"], entry["state"], entry["owner_digest"]) == (
                row.owner_kind, row.epoch, row.state, row.composition_digest)
            assert entry["pinned"] is (domain in PINNED_DOMAINS)
        assert domains[TASKS]["state"] == "draining"
        assert dump["pinned_domains"] == sorted(PINNED_DOMAINS)
        assert dump["effective_patch"] == [] and dump["profile"] is None
        assert [bundle["bundle"] for bundle in dump["bundles"] if bundle["pinned"]] == ["seraph-trust"]
    assert (await live_composition_dump(CordisHost()))["source"] == "live"


async def test_the_dump_holds_no_secrets_from_a_vault_seeded_workspace(accounting_db, real_auth, tmp_path,
                                                                        capsys):  # noqa: F811
    from config.settings import settings
    from src.vault.crypto import vault_key_material
    from src.vault.repository import vault_repository
    await seed_composition_inventory()
    secrets = {"near_api_key": "near-live-key-1198-SHOULD-NEVER-APPEAR",
               "github/token": "ghp_1198_never_in_a_composition_dump",
               "mail/password": "mail-password-1198-private"}
    for key, value in secrets.items():
        await vault_repository.store(key, value, owner_principal_id="operator:root")
    assert await vault_repository.get("near_api_key", owner_principal_id="operator:root") == secrets["near_api_key"]
    key_material = vault_key_material()
    forbidden = [*secrets.values(), OPERATOR_SECRET, key_material.decode(),
                 base64.urlsafe_b64decode(key_material).hex(), settings.openrouter_api_key]
    workspace = Path(settings.workspace_dir)
    assert dump_main(["dump", "--workspace", str(workspace)]) == 0
    printed = capsys.readouterr().out
    for dump_text in (json.dumps(await live_composition_dump(CordisHost())),
                      json.dumps(offline_composition_dump(workspace)), printed):
        for value in forbidden:
            assert value not in dump_text
        # No secret key names either: the dump carries no vault content at all.
        assert "near_api_key" not in dump_text and "github/token" not in dump_text


async def test_manage_sh_composition_dump_reads_the_dev_workspace(accounting_db, tmp_path):  # noqa: F811
    from config.settings import settings
    await seed_composition_inventory()
    workspace = Path(settings.workspace_dir)
    # A private launcher copy: the operator's own .env.dev is never read or written.
    launcher = tmp_path / "launcher"
    launcher.mkdir()
    shutil.copy2(REPO / "manage.sh", launcher / "manage.sh")
    (launcher / "backend").symlink_to(REPO / "backend")
    (launcher / ".env.dev").write_text(f"LOCAL_WORKSPACE_DIR={workspace}\nOPERATOR_AUTH_SECRET={OPERATOR_SECRET}\n")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    result = subprocess.run(["bash", str(launcher / "manage.sh"), "-e", "dev", "composition", "dump"],
                            cwd=launcher, env=env, capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    dump = json.loads(result.stdout)
    assert dump["source"] == "offline" and dump["inventory"]["state"] == "ok"
    rows = await _rows()
    assert {domain: (item["owner_kind"], item["epoch"]) for domain, item in _dump_domains(dump).items()} == {
        domain: (row.owner_kind, row.epoch) for domain, row in rows.items()}
    assert OPERATOR_SECRET not in result.stdout + result.stderr
    unknown = subprocess.run(["bash", str(launcher / "manage.sh"), "-e", "dev", "composition", "edit"],
                             cwd=launcher, env=env, capture_output=True, text=True, timeout=60, check=False)
    assert unknown.returncode != 0 and "Use: dump" in unknown.stdout + unknown.stderr
