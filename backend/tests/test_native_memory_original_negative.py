"""Original authenticated admission/claim and negative CAS; no effect Source."""
from datetime import datetime, timedelta, timezone
import json
import sys

import pytest
from sqlalchemy import event, text

from src.db.engine import get_session as canonical_session, override_session_factory
from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth
from tests.test_native_memory_report_source_vertical import NODE


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,stage", [("cancel", "accepted"), ("tree", "accepted"),
    ("cancel", "claimed"), ("targeted_reaper", "claimed"), ("global_reaper", "claimed"), ("tree", "claimed")])
async def test_actual_preoriginal_claim_negative_owner_cas(accounting_db, real_auth, monkeypatch, operation, stage):
    import src.db.engine as original
    from src.auth.service import create_session
    from src.agent.session import SessionManager
    from src.auth.ownership import _current_root
    from src.memory.repository import memory_repository
    from src.memory.header_bounds import HeaderReadBudget, OPERATOR_SESSION
    from src.runtime_plugins.bridge import CordisHost
    from src.runtime_plugins.composition import reviewed_composition
    from src.runtime_plugins.dispatch import NativeServiceDispatcher
    from src.runtime_plugins.ownership import (DOMAINS, begin_native_writer,
        initialize_fresh_deployment, bind_invocation)
    from src.runtime_plugins.memory_producer import (prepare_memory_admission,
        native_memory_spec, validate_original_memory_owner, _MEMORY_SOURCES)
    from src.workflows.job_runtime import DurableJobRepository
    from src.workspace.production import ProductionWorkspace, maintenance_fence
    from src.workspace.accounting_witness import preflight_native_memory_reference_journal

    root, engine, factory = accounting_db
    monkeypatch.setattr(original, "engine", engine)
    monkeypatch.setattr(original, "_db_path", str(root / "seraph.db"))
    monkeypatch.setattr(original, "async_session_factory", factory)
    for target in ("src.db.engine.get_session", "src.workflows.job_runtime.get_session",
        "src.workflows.durable_state.get_session", "src.auth.service.get_session",
        "src.memory.repository.get_session", "src.audit.repository.get_session",
        "src.vault.repository.get_session", "src.approval.repository.get_session",
        "src.goals.repository.get_session", "src.agent.session.get_session"):
        monkeypatch.setattr(target, canonical_session)
    await original.init_db()
    repository = DurableJobRepository()
    reviewed = reviewed_composition(node_path=NODE)
    host = CordisHost(node_path=NODE, service_dispatch=NativeServiceDispatcher(jobs=repository))
    monkeypatch.setattr("src.runtime_plugins.bridge.cordis_host", host)
    sources_before = tuple(_MEMORY_SOURCES)
    budget = None
    numeric_trace = []
    stages = []
    snapshot_trace = []
    last_snapshot = None
    def observed_statement(connection, cursor, statement, parameters, context, executemany):
        nonlocal last_snapshot
        driver = connection.connection.driver_connection
        last_snapshot = {"connection": id(connection), "transaction": id(connection.get_transaction()),
            "driver": id(driver), "driver_in_transaction": driver.in_transaction,
            "total_changes": driver.total_changes, "sql_verb": statement.split(None, 1)[0],
            "begin": statement if statement in ("BEGIN", "BEGIN IMMEDIATE") else None}
        snapshot_trace.append(dict(last_snapshot))
    original_debit = HeaderReadBudget.debit
    def observed_debit(frame, amount, *, appearance=None):
        label = list(appearance[:2]) if isinstance(appearance, tuple) else None
        caller = sys._getframe(1)
        numeric_trace.append({"frame": "native" if frame is budget else "maintenance",
            "frame_identity": id(frame), "amount": amount, "remaining_before": frame.remaining,
            "references": len(frame.references), "physical_references": len(frame.physical_references),
            "future_references": len(frame.future_references), "label": label,
            "site": [caller.f_code.co_name, caller.f_lineno], "last_statement_snapshot": last_snapshot})
        return original_debit(frame, amount, appearance=appearance)
    monkeypatch.setattr(HeaderReadBudget, "debit", observed_debit)
    with override_session_factory(factory):
        _, operator = await create_session()
        await SessionManager().get_or_create(operator.session_id, owner_principal_id=operator.principal.principal_id)
        record = await memory_repository.create_memory(content="Original untouched negative owner record",
            source_session_id=operator.session_id, summary="Original untouched summary")
        workspace = ProductionWorkspace(host_root=root)
        with maintenance_fence(workspace):
            async with canonical_session() as db:
                await begin_native_writer(db, owner="composition_maintenance", fresh=True)
                await initialize_fresh_deployment(db,
                    composition_digests={domain: reviewed.composition_digest for domain in DOMAINS})
        try:
            assert await host.start(), host.snapshot()
            assert host.process is not None and host.process.returncode is None
            event.listen(engine.sync_engine, "before_cursor_execute", observed_statement)
            budget = HeaderReadBudget()
            async with canonical_session() as db:
                await db.execute(text("BEGIN"))
                await budget.certify(db, OPERATOR_SESSION, (operator.session_id,))
                await _current_root(db, operator)
                binding = await bind_invocation(db, method="memory.forget", native_branch="base",
                    goal_bound=False, reviewed_composition=host.reviewed, header_budget=budget)
                stages.append({"stage": "bound", "remaining": budget.remaining})
                admission = await prepare_memory_admission(db, operator=operator, method="memory.forget",
                    request={"record_ref": record.memory_id, "mode": "archive", "privacy_boundary": "private", "reason": None},
                    idempotency_key="actual-negative-" + operation, host_boot_nonce=host.boot_nonce,
                    composition_binding_digest=binding.binding_digest,
                    original_deadline=datetime.now(timezone.utc) + timedelta(seconds=30), header_budget=budget)
                stages.append({"stage": "prepared", "remaining": budget.remaining})
                await db.rollback()
                await begin_native_writer(db, owner="durable_jobs", header_budget=budget)
                async def original_admission_check(writer, run):
                    await budget.certify(writer, OPERATOR_SESSION, (operator.session_id,))
                    await _current_root(writer, operator)
                    await validate_original_memory_owner(writer, admission)
                spec = await native_memory_spec(db, admission=admission, operator=operator, binding=binding)
                admitted = await repository._admit_in_session(db, spec, native_memory_admission=admission,
                    native_memory_host=host, native_memory_host_boot_nonce=host.boot_nonce,
                    admission_authority_check=original_admission_check)
                stages.append({"stage": "admitted", "remaining": budget.remaining})
            assert admitted["status"] == "accepted"
            claim = None
            payload = {"lease_owner": None, "fencing_token": None}
            if stage == "claimed":
                await repository.transition_job(spec.identity.job_id, "queued",
                    expected_revision=admitted["revision"], header_budget=budget)
                stages.append({"stage": "queued", "remaining": budget.remaining})
                claim = await repository.claim_service_job(spec.identity.job_id, host=host,
                    owner="original-negative-worker", lease_seconds=30,
                    claim_authority_check=original_admission_check, header_budget=budget,
                    native_memory_admission=admission)
                stages.append({"stage": "claimed", "remaining": budget.remaining})
                payload = claim.checkpoint["payload"]
            async with engine.connect() as connection:
                before = dict((await connection.execute(text("SELECT * FROM workflow_run_states WHERE run_identity=:job"),
                    {"job": spec.identity.job_id})).mappings().one())
                memory_before = tuple((await connection.execute(text("SELECT * FROM memories WHERE id=:record"),
                    {"record": record.memory_id})).one())
                liability_before = [tuple(row) for row in (await connection.execute(text("SELECT * FROM inference_cost_reservations"))).all()]
            traces = []
            executed_writes = []
            def trace(connection, cursor, statement, parameters, context, executemany):
                traces.append((statement, parameters))
            def executed(connection, cursor, statement, parameters, context, executemany):
                if statement.startswith("UPDATE workflow_run_states"):
                    executed_writes.append({"statement": statement, "parameters": parameters,
                        "rowcount": cursor.rowcount,
                        "total_changes_after": connection.connection.driver_connection.total_changes})
            event.listen(engine.sync_engine, "before_cursor_execute", trace)
            event.listen(engine.sync_engine, "after_cursor_execute", executed)
            try:
                if operation == "cancel":
                    result = await repository.cancel_job(spec.identity.job_id, owner=payload["lease_owner"],
                        fencing_token=payload["fencing_token"], expected_revision=before["revision"])
                elif operation == "tree":
                    result = await repository.cancel_job_tree(spec.identity.job_id)
                else:
                    observed = datetime.fromisoformat(claim.job["lease"]["expires_at"]) + timedelta(seconds=1)
                    result = (await repository.recover_stale_job(spec.identity.job_id, now=observed)
                        if operation == "targeted_reaper" else await repository.recover_stale_jobs(now=observed))
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", trace)
                event.remove(engine.sync_engine, "after_cursor_execute", executed)
                (root.parent / "original-negative-sql-trace.json").write_text(json.dumps(
                    {"executed_writes": executed_writes, "statements": traces}, indent=2, default=str) + "\n")
            assert result
            writes = [(sql, binds) for sql, binds in traces if sql.startswith("UPDATE workflow_run_states")]
            assert len(writes) == 1
            assert all(name in writes[0][0] for name in ("checkpoint_receipts_json", "revision", "WHERE", "run_identity", "status"))
            statements = [sql for sql, _ in traces]
            immediate = statements.index("BEGIN IMMEDIATE")
            first_body = next(i for i, sql in enumerate(statements) if sql.startswith("SELECT workflow_run_states."))
            assert immediate < first_body
            assert any("FROM sqlite_schema" in sql for sql in statements[immediate:first_body])
            async with engine.connect() as connection:
                after = dict((await connection.execute(text("SELECT * FROM workflow_run_states WHERE run_identity=:job"),
                    {"job": spec.identity.job_id})).mappings().one())
                assert tuple((await connection.execute(text("SELECT * FROM memories WHERE id=:record"),
                    {"record": record.memory_id})).one()) == memory_before
                assert [tuple(row) for row in (await connection.execute(text("SELECT * FROM inference_cost_reservations"))).all()] == liability_before
            original_record, current = preflight_native_memory_reference_journal(after["checkpoint_receipts_json"])
            assert original_record is None
            assert current["payload"]["state"] == "unknown"
            assert current["payload"]["reason_code"] == "original_projection_unavailable"
            assert current["payload"]["rows"] == [] and current["payload"]["rows_digest"] is None
            assert after["revision"] == before["revision"] + 1
            assert after["fencing_token"] == before["fencing_token"] + (operation.endswith("reaper"))
            for field in ("arguments_json", "input_digest", "authority_digest", "run_fingerprint",
                "composition_binding_json", "checkpoint_context_json", "effect_receipts_json", "artifact_receipts_json"):
                assert after[field] == before[field]
            assert tuple(_MEMORY_SOURCES) == sources_before
        finally:
            if event.contains(engine.sync_engine, "before_cursor_execute", observed_statement):
                event.remove(engine.sync_engine, "before_cursor_execute", observed_statement)
            (root.parent / "original-memory-numeric-trace.json").write_text(json.dumps(
                {"stages": stages, "charges": numeric_trace, "statement_snapshots": snapshot_trace}, indent=2) + "\n")
            await host.stop(preserve_blocked=host.state == "blocked")
            if host._cleanup_task is not None:
                await host._cleanup_task
            assert host.snapshot()["cleanup"]["process_reaped"] is True
