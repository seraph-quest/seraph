"""Two immutable process epochs; historical creation is never imported into current code.

Run exactly one test per epoch. Root pins the whole selected source snapshot and
supplies a private locator path; this packet supplies no witness or authority.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from config.settings import settings
from src.db.models import InferenceCostReservation, WorkflowRunState, RepoRepairSourcePacket
from src.workflows import repo_repair_source as source
from src.workflows.job_runtime import DurableJobRepository
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.repository_admission_lifecycle import repository_admission_signer


BASES = {
    "v1": "19399fbd52bee21242419fabba502d49fb3fa91b",
    "v2": "0f4910eb11dec08fcffd2bbcdb471bbad0c95110",
    "v3": "7c16313e34647fd9cae49f5d297bc64e69181df3",
}


def _packet_path():
    path = Path(os.environ["SERAPH_HISTORICAL_STARTUP_PACKET"])
    assert path.is_absolute() and path.parent.is_dir()
    return path


def _raw_rows(path):
    # Read-only SQLite: no migration, schema repair or synthetic fixture rows.
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        tables = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        values = {}
        for table in tables:
            assert table.replace("_", "").isalnum()
            rows = db.execute('SELECT * FROM "' + table + '"').fetchall()
            values[table] = sorted(repr(row) for row in rows)
        return values


@pytest.mark.asyncio
async def test_create_original_historical_startup_epoch(
        accounting_db, monkeypatch, repository_admission_signer):
    mode = os.environ["SERAPH_HISTORICAL_STARTUP_MODE"]
    assert mode in BASES
    path = _packet_path()
    assert not path.exists()
    if mode == "v3":
        # This helper exists only in the pinned historical v3 whole source tree.
        from tests.test_repo_work_original_producer_startup import registered_running_source
        captured = await registered_running_source(accounting_db, monkeypatch)
        jobs, job_id = captured["jobs"], captured["job_id"]
    else:
        from src.auth.service import authenticate_session
        from tests.test_repo_work_task_publication import actual_native_source
        factory, owner, service, jobs, binding, _ = await actual_native_source(
            accounting_db, monkeypatch, goal_capacity=2, claim_child=False)
        operator = await authenticate_session(owner.session_id, touch=False)
        prepared = await source.prepare_repository_native_source(service, jobs, binding,
            child_owner="historical-startup-creation-worker", principal=operator.principal)
        job_id = prepared["repository_job_id"]
    async with accounting_db[2]() as db:
        root = await jobs._fetch(db, job_id)
        binding = source.read_repository_original(root)[4]
        inventory = source.read_repository_inventory(root)
        assert inventory["schema"] == "repository.checkpoint_inventory." + mode
        producer_ids = [item["checkpoint_id"] for item in json.loads(root.checkpoint_receipts_json)
            if item["checkpoint_id"].startswith("repository:producer:")]
        assert bool(producer_ids) is (mode == "v3")
        if mode == "v3":
            from src.workflows.repo_repair_source_recovery import read_registered_repository_producer
            assert read_registered_repository_producer(root, iteration_index=1)["schema"] == "repository.original_producer.v1"
        packet = {"mode": mode, "historical_commit": BASES[mode],
            "workspace": str(accounting_db[0]),
            "lifecycle": os.environ["SERAPH_WORKSPACE_LIFECYCLE_PATH"],
            "root": root.run_identity, "native": binding.invocation_id,
            "parent": binding.parent_job_id}
    path.write_text(json.dumps(packet, sort_keys=True) + "\n")
    path.chmod(0o600)


async def _assert_genuine_historical_checkpoint_negatives(factory, database, packet):
    """Corrupt only genuine old-created rows, then roll back every probe."""
    from src.workflows.job_runtime import _canonical, _digest
    from src.work_board.repository import _begin_sqlite_immediate
    original_rows = _raw_rows(database)
    edges = ["state_digest", "state_keys", "kind", "packet_id", "artifact_digest",
        "input_digest", "source_manifest", "artifact_ref", "root", "attempt", "owner",
        "lease_fence", "wrapper_fence", "bool_fence", "wrong_literal_status",
        "extra_payload", "missing_payload", "extra_wrapper", "checkpoint_identity",
        "missing_record", "duplicate_record", "unsafe", "recorded_at"]
    probes = [(prefix, edge) for prefix in ("repo-repair-source-intent:", "repo-repair-source:")
        for edge in edges]
    probes.extend(("repo-repair-source-intent:", edge) for edge in
        ("principal", "session", "task", "goal", "goal_revision", "repository_ref", "base_digest"))
    probes.extend(("packet", edge) for edge in ("missing", "task", "artifact_digest", "state", "owner"))
    probes.extend(("protected", edge) for edge in ("reservation_digest", "preparation_owner", "foreign_record", "missing_prepared"))
    async with source._repository_startup_mutation_fence():
        for prefix, edge in probes:
            async with factory() as db:
                await _begin_sqlite_immediate(db)
                root = (await db.execute(select(WorkflowRunState).where(
                    WorkflowRunState.run_identity == packet["root"]))).scalar_one()
                history = json.loads(root.checkpoint_receipts_json)
                if prefix == "packet":
                    row = (await db.execute(select(RepoRepairSourcePacket).where(
                        RepoRepairSourcePacket.workflow_run_id == root.run_identity,
                        RepoRepairSourcePacket.input_digest == root.input_digest))).scalar_one()
                    if edge == "missing":
                        await db.delete(row)
                    elif edge == "task":
                        row.work_board_task_id = "foreign-task"
                    elif edge == "artifact_digest":
                        row.artifact_sha256 = "0" * 64
                    elif edge == "owner":
                        row.owner_principal_id = "foreign-owner"
                    else:
                        row.state = "pending"
                elif prefix == "protected":
                    if edge == "reservation_digest":
                        record = next(item for item in history
                            if item["checkpoint_id"] == "repo-repair-execution-reservation")
                        record["state_digest"] = "0" * 64
                    elif edge == "foreign_record":
                        foreign_payload = {"kind": "foreign_startup_history"}
                        history.append({"checkpoint_id": "foreign:startup-history", "safe": True,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "payload": foreign_payload, "state_digest": _digest(foreign_payload)})
                    else:
                        record = next(item for item in history
                            if item["checkpoint_id"].startswith("repository:prepared:"))
                        if edge == "missing_prepared":
                            history.remove(record)
                        else:
                            record["payload"]["repository_attempt_owner"] = "foreign-owner"
                            record["state_digest"] = _digest(record["payload"])
                else:
                    record = next(item for item in history
                        if item["checkpoint_id"] == prefix + packet["root"])
                    payload = record["payload"]
                    if edge == "state_digest":
                        record["state_digest"] = "0" * 64
                    elif edge == "state_keys":
                        record["state_keys"] = sorted(set(record["state_keys"]) - {"status"})
                    elif edge == "wrapper_fence":
                        record["fencing_token"] += 1
                    elif edge == "bool_fence":
                        record["fencing_token"] = True
                        payload["lease_fence"] = True
                    elif edge == "wrong_literal_status":
                        record["state_digest"] = _digest({"kind": payload["kind"],
                            "status": "foreign-status", "packet_id": payload["packet_id"],
                            "artifact_sha256": payload["artifact_sha256"]})
                    elif edge == "extra_payload":
                        payload["status"] = "publication_pending" if "intent" in prefix else "row_flushed"
                    elif edge == "missing_payload":
                        del payload["learning"]
                    elif edge == "extra_wrapper":
                        record["owner"] = payload["lease_owner"]
                    elif edge == "checkpoint_identity":
                        record["checkpoint_id"] += ":foreign"
                    elif edge == "missing_record":
                        history.remove(record)
                    elif edge == "duplicate_record":
                        history.append(json.loads(json.dumps(record)))
                    elif edge == "unsafe":
                        record["safe"] = False
                    elif edge == "recorded_at":
                        record["recorded_at"] = "foreign-date"
                    else:
                        field = {"kind": "kind", "packet_id": "packet_id",
                            "artifact_digest": "artifact_sha256", "input_digest": "input_digest",
                            "source_manifest": "source_manifest_digest", "artifact_ref": "artifact_ref",
                            "root": "workflow_run_id", "attempt": "attempt_id", "owner": "lease_owner",
                            "lease_fence": "lease_fence", "principal": "owner_principal_id",
                            "session": "owner_session_id", "task": "work_board_task_id", "goal": "goal_id",
                            "goal_revision": "goal_revision", "repository_ref": "repository_ref",
                            "base_digest": "base_snapshot_digest"}[edge]
                        payload[field] = payload[field] + 1 if type(payload[field]) is int else "foreign-value"
                root.checkpoint_receipts_json = _canonical(history)
                await db.flush()
                assert await source._repository_startup_protected_lineage(db) == {
                    packet["root"], packet["native"], packet["parent"]}, (prefix, edge)
                await db.rollback()
            assert _raw_rows(database) == original_rows, (prefix, edge)


@pytest.mark.asyncio
async def test_current_startup_classifies_genuine_historical_epoch(monkeypatch):
    packet = json.loads(_packet_path().read_text())
    assert set(packet) == {"mode", "historical_commit", "workspace", "lifecycle", "root", "native", "parent"}
    mode = packet["mode"]
    assert mode in BASES and packet["historical_commit"] == BASES[mode]
    workspace = Path(packet["workspace"])
    assert workspace.is_absolute() and workspace.is_dir()
    database = workspace / "seraph.db"
    assert database.is_file()
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    monkeypatch.setenv("SERAPH_WORKSPACE_LIFECYCLE_PATH", packet["lifecycle"])
    before = _raw_rows(database)
    engine = create_async_engine("sqlite+aiosqlite:///" + str(database), connect_args={"timeout": 5})
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def sessions():
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    try:
        # Bind original current repository/accounting owners to the SAME database.
        for target in ("src.workflows.job_runtime.get_session", "src.workflows.durable_state.get_session",
                       "src.db.engine.get_session"):
            monkeypatch.setattr(target, sessions)
        jobs = DurableJobRepository()
        async with factory() as db:
            root = (await db.execute(select(WorkflowRunState).where(
                WorkflowRunState.run_identity == packet["root"]))).scalar_one()
            binding = source.read_repository_original(root)[4]
            assert (binding.invocation_id, binding.parent_job_id) == (packet["native"], packet["parent"])
            assert source.read_repository_inventory(root)["schema"] == "repository.checkpoint_inventory." + mode
        async with source._repository_startup_mutation_fence():
            async with factory() as db:
                from src.work_board.repository import _begin_sqlite_immediate
                await _begin_sqlite_immediate(db)
                protected = await source._repository_startup_protected_lineage(db)
                expected = {packet["root"], packet["native"], packet["parent"]} if mode == "v3" else set()
                assert protected == expected
        assert _raw_rows(database) == before
        if mode == "v3":
            observed = datetime.now(timezone.utc) + timedelta(days=1)
            assert await jobs.recover_inference_accounting(now=observed) == []
            assert await jobs.recover_stale_jobs(now=observed) == []
            for identity in expected:
                receipt = await jobs.recover_stale_job(identity, now=observed)
                assert receipt["receipt"]["reason"] == "original_repository_source_recovery_required"
            assert _raw_rows(database) == before
        else:
            await _assert_genuine_historical_checkpoint_negatives(factory, database, packet)
            assert _raw_rows(database) == before
            # Genuine ordinary precontact Root: no producer or repository contact
            # reservation exists. Accounting must remain exact before ordinary CAS.
            observed = datetime.now(timezone.utc) + timedelta(days=1)
            async with factory() as db:
                original_root = await jobs._fetch(db, packet["root"])
                original_values = original_root.model_dump()
                assert original_root.status == "running"
                assert original_root.deadline_at is not None
                assert original_root.deadline_at.replace(tzinfo=timezone.utc) < observed
                reservations = list((await db.scalars(select(InferenceCostReservation).where(
                    InferenceCostReservation.job_id == packet["root"]))).all())
                assert reservations == []
            assert await jobs.recover_inference_accounting(now=observed, job_id=packet["root"]) == []
            assert _raw_rows(database) == before
            receipt = await jobs.recover_stale_job(packet["root"], now=observed)
            assert receipt["job_id"] == packet["root"]
            assert receipt["status"] == "failed"
            assert receipt["receipt"]["kind"] == "targeted_recovery"
            assert receipt["receipt"]["status"] == "failed"
            assert receipt["receipt"]["reason"] == "deadline_expired"
            assert receipt["receipt"]["revision"] == original_values["revision"] + 1
            assert receipt["receipt"]["fencing_token"] == original_values["fencing_token"] + 1
            assert receipt["receipt"]["operator_visible"] is True
            async with factory() as db:
                recovered_root = await jobs._fetch(db, packet["root"])
                after_values = recovered_root.model_dump()
                assert recovered_root.status == "failed"
                assert recovered_root.failure_reason == "deadline_expired"
                assert recovered_root.lease_owner is None and recovered_root.lease_expires_at is None
                assert recovered_root.revision == original_values["revision"] + 1
                assert recovered_root.fencing_token == original_values["fencing_token"] + 1
                changed = {"status", "failure_reason", "lease_owner", "lease_expires_at",
                    "revision", "fencing_token", "updated_at", "heartbeat_at", "finished_at"}
                assert {key: value for key, value in after_values.items() if key not in changed} == {
                    key: value for key, value in original_values.items() if key not in changed}
                for key in ("updated_at", "heartbeat_at", "finished_at"):
                    assert after_values[key].replace(tzinfo=timezone.utc) == observed
            after = _raw_rows(database)
            assert after != before
            assert {key: rows for key, rows in after.items() if key != WorkflowRunState.__tablename__} == {
                key: rows for key, rows in before.items() if key != WorkflowRunState.__tablename__}
            # Exact one-row delta; child, parent and all other run rows unchanged.
            old_runs, new_runs = map(set, (before[WorkflowRunState.__tablename__], after[WorkflowRunState.__tablename__]))
            assert len(old_runs - new_runs) == len(new_runs - old_runs) == 1
            repeated = await jobs.recover_stale_job(packet["root"], now=observed)
            assert repeated["receipt"]["status"] == "noop"
            assert _raw_rows(database) == after
    finally:
        await engine.dispose()
