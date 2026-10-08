"""Stopped restore creates real higher blocked owners without historical authority."""
import json
import sqlite3

import pytest

from tests.test_runtime_composition_ownership import composition_db, bound_spec
from src.db.engine import get_session as canonical_session
from src.db.models import AuditEvent
from src.runtime_plugins.ownership import CompositionDependency, transition_owner, begin_native_writer
from src.workflows.job_runtime import DurableJobRepository
from src.workspace.production import maintenance_fence, read_lifecycle_receipt, read_accounting_checkpoint, ProductionWorkspaceReconciliationError


def backup(root, target):
    target.mkdir()
    with sqlite3.connect(root / "seraph.db") as source, sqlite3.connect(target / "seraph.db") as destination:
        source.backup(destination)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["newer_epoch", "same_epoch_wrong_owner", "same_epoch_wrong_digest"])
async def test_restore_rejects_destination_high_water_atomically(composition_db, tmp_path, damage):
    from src.workspace.accounting_continuity import retain_inference_accounting
    root, _, _, workspace = composition_db
    await DurableJobRepository().admit_job(await bound_spec(job_id="high-water"))
    target = tmp_path / "target"
    backup(root, target)
    with sqlite3.connect(target / "seraph.db") as db:
        assignment = {"newer_epoch": "epoch=2", "same_epoch_wrong_owner": "owner_kind='cordis'", "same_epoch_wrong_digest": "composition_digest='" + "b" * 64 + "'"}[damage]
        db.execute("UPDATE runtime_composition_states SET " + assignment + " WHERE runtime_domain='seraph.tasks.v1'")
        before = tuple(db.iterdump())
    receipt, checkpoint = read_lifecycle_receipt(workspace), read_accounting_checkpoint(workspace)
    with maintenance_fence(workspace), pytest.raises(ProductionWorkspaceReconciliationError, match="composition_restore_high_water_conflict"):
        retain_inference_accounting(active=root, target=target, database_path="seraph.db")
    with sqlite3.connect(target / "seraph.db") as db:
        assert tuple(db.iterdump()) == before
    assert read_lifecycle_receipt(workspace) == receipt
    assert read_accounting_checkpoint(workspace) == checkpoint


@pytest.mark.asyncio
async def test_restore_epoch_ceiling_blocks_before_copied_rows(composition_db, tmp_path):
    from src.workspace.accounting_continuity import retain_inference_accounting
    root, _, _, workspace = composition_db
    before_owner = CompositionDependency("seraph.tasks.v1", "legacy", 1, "a" * 64)
    after_owner = CompositionDependency("seraph.tasks.v1", "legacy", 2**63 - 1, "a" * 64)
    with maintenance_fence(workspace):
        async with canonical_session() as db:
            await begin_native_writer(db, owner="composition_maintenance")
            event = AuditEvent(event_type="runtime_composition_recovery", actor="managed_maintenance",
                details_json=json.dumps({"schema_version": 1, "runtime_domain": before_owner.runtime_domain,
                    "prior": before_owner.payload(), "target": after_owner.payload(), "state": "blocked",
                    "phase": "awaiting_boot", "prior_recovery_receipt_ref": None}))
            db.add(event)
            await db.flush()
            await transition_owner(db, runtime_domain=before_owner.runtime_domain, expected=before_owner,
                owner_kind=after_owner.owner_kind, epoch=after_owner.epoch, composition_digest=after_owner.composition_digest,
                state="blocked", recovery_receipt_ref=event.id)
    target = tmp_path / "ceiling"
    backup(root, target)
    with sqlite3.connect(target / "seraph.db") as db:
        before = tuple(db.iterdump())
    receipt, checkpoint = read_lifecycle_receipt(workspace), read_accounting_checkpoint(workspace)
    with maintenance_fence(workspace), pytest.raises(ProductionWorkspaceReconciliationError, match="composition_restore_epoch_exhausted"):
        retain_inference_accounting(active=root, target=target, database_path="seraph.db")
    with sqlite3.connect(target / "seraph.db") as db:
        assert tuple(db.iterdump()) == before
    assert read_lifecycle_receipt(workspace) == receipt
    assert read_accounting_checkpoint(workspace) == checkpoint


@pytest.mark.asyncio
async def test_second_real_restore_keeps_original_chain_and_adds_one_epoch(composition_db, tmp_path):
    from src.workspace.accounting_continuity import retain_inference_accounting, verify_promoted_composition
    from src.workspace.production import ProductionWorkspace, write_lifecycle_receipt
    root, _, _, workspace = composition_db
    await DurableJobRepository().admit_job(await bound_spec(job_id="twice-restored"))
    first = tmp_path / "first"
    backup(root, first)
    with maintenance_fence(workspace):
        retain_inference_accounting(active=root, target=first, database_path="seraph.db")
    promoted = ProductionWorkspace(host_root=first)
    # Publish only the real verified staged blocked generation, as maintenance
    # promotion does; this fixture does not claim a runtime boot or Ready.
    actual = verify_promoted_composition(promoted)
    receipt = read_lifecycle_receipt(workspace)
    write_lifecycle_receipt(promoted, {**receipt, "runtime_composition": actual,
        "deployment_binding": {"revision": receipt["deployment_binding"]["revision"] + 1,
            "root_path_digest": promoted.identity_digest, "host_bind_identity": promoted.bind_identity_digest}})
    with sqlite3.connect(first / "seraph.db") as db:
        original_audits = db.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        original_binding = db.execute("SELECT composition_binding_json FROM workflow_run_states WHERE run_identity='twice-restored'").fetchone()[0]
    second = tmp_path / "second"
    backup(first, second)
    with maintenance_fence(promoted):
        first_result = retain_inference_accounting(active=first, target=second, database_path="seraph.db")
        assert retain_inference_accounting(active=first, target=second, database_path="seraph.db") == first_result
    with sqlite3.connect(second / "seraph.db") as db:
        assert db.execute("SELECT COUNT(*) FROM runtime_composition_states WHERE epoch=3 AND state='blocked'").fetchone()[0] == 14
        assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 28
        for event in original_audits:
            assert db.execute("SELECT * FROM audit_events WHERE id=?", (event[0],)).fetchone() == event
        assert db.execute("SELECT composition_binding_json FROM workflow_run_states WHERE run_identity='twice-restored'").fetchone()[0] == original_binding
    assert all(item["state"] == "blocked" for item in read_accounting_checkpoint(promoted)["composition_target"]["inventory"])
