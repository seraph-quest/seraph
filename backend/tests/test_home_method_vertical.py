"""Actual completed native source → reviewed method → Home historical metadata."""
from pathlib import Path
import json

from sqlalchemy import event

from config.settings import settings
from src.auth import service as auth
from src.work_board.dispatcher import WorkBoardDispatcher
from src.work_board.historical_method import historical_method_service
from src.operator.home_projection import home_projection
from tests.test_home_continuation import accounting_db, home_setup
from tests.test_general_task_planner import forbid_external_inference
from tests.test_general_task_methods import test_genuine_completed_native_source_review_next_task_and_future_rollback as genuine_method_journey
from tests.test_general_task_methods import method_admission_lifecycle


async def test_genuine_native_method_history_read_without_body_or_file_access(accounting_db,monkeypatch,tmp_path,forbid_external_inference,method_admission_lifecycle):
    client,_ = await home_setup(accounting_db,monkeypatch)
    actual_create = auth.create_session
    actual_pass = WorkBoardDispatcher.run_pass
    snapshots = []
    async def capture_actual_root(*args,**kwargs):
        result = await actual_create(*args,**kwargs)
        client.cookies.set(settings.operator_auth_cookie_name,result[0])
        await method_admission_lifecycle.start()
        assert historical_method_service.signing_key is not None
        home_projection.stop()
        home_projection.start()
        return result
    async def read_home():
        statements = []
        def inspect_sql(connection,cursor,statement,parameters,context,many):
            statements.append(statement)
            lowered = statement.lower()
            assert "m.content" not in lowered and "m.summary" not in lowered
            assert "memories.content" not in lowered and "memories.summary" not in lowered
            assert "from secrets" not in lowered
        def deny_file(*args,**kwargs):
            raise AssertionError("Home attempted a physical method/artifact read")
        event.listen(accounting_db[1].sync_engine,"before_cursor_execute",inspect_sql)
        try:
            with monkeypatch.context() as scoped:
                scoped.setattr(Path,"read_text",deny_file)
                scoped.setattr(Path,"read_bytes",deny_file)
                response = await client.get("/api/operator/continuation")
            assert response.status_code==200,response.text
            assert len([s for s in statements if s.lstrip().upper().startswith(("SELECT","WITH"))])<=18
            assert not any(s.lstrip().upper().startswith(("INSERT","UPDATE","DELETE")) for s in statements)
            snapshots.append(response.json())
            number = len(snapshots)
            (accounting_db[0]/f"home-method-wire-{number}.json").write_text(response.text)
            (accounting_db[0]/f"home-method-receipt-{number}.json").write_text(json.dumps({
                "route":"/api/operator/continuation","status":response.status_code,
                "cursor":response.headers.get("x-continuation-cursor"),
                "selects":len([s for s in statements if s.lstrip().upper().startswith(("SELECT","WITH"))])},sort_keys=True))
        finally:
            event.remove(accounting_db[1].sync_engine,"before_cursor_execute",inspect_sql)
    async def pass_with_actual_home(self,*args,**kwargs):
        await read_home()
        result = await actual_pass(self,*args,**kwargs)
        await read_home()
        return result
    monkeypatch.setattr(auth,"create_session",capture_actual_root)
    monkeypatch.setattr(WorkBoardDispatcher,"run_pass",pass_with_actual_home)
    try:
        async with client:
            # Unchanged owning journey admits/completes a real read_file child,
            # operator-completes review, reviews actual inert correction,
            # admits another Task and rolls back during its actual claim.
            native_root = tmp_path/"actual-native-source"
            native_root.mkdir()
            await genuine_method_journey(accounting_db[2].accounting_sessions,monkeypatch,native_root,forbid_external_inference,method_admission_lifecycle)
            original_pin = next(row["method"] for body in snapshots for section in ("task_next_actions","prepared_outputs")
                for row in body[section]["items"] if row.get("method") and row["method"]["status"]=="admitted")
            from src.db.models import Memory,MemoryTombstone
            sessions = accounting_db[2].accounting_sessions
            async with sessions() as db:
                memory = await db.get(Memory,original_pin["version"])
                original_metadata = memory.metadata_json
            from src.memory import task_methods
            from src.work_board.repository import BoardError
            from dataclasses import replace
            import pytest
            operator = await auth.authenticate_home_token_readonly(client.cookies.get(settings.operator_auth_cookie_name))
            inspected = await task_methods.inspect_method(operator,original_pin['method_id'])
            assert inspected['status']=='rolled_back' and inspected['new_method']
            from src.db.models import MemoryProposal
            async with sessions() as db:
                proposal = await db.get(MemoryProposal,original_pin['method_id'])
                proposal.accepted_memory_id = None
            def deny_missing_link_artifact(*args,**kwargs):
                raise AssertionError('Missing accepted link reached private artifact read')
            with monkeypatch.context() as scoped:
                scoped.setattr(task_methods,'read_private_proof',deny_missing_link_artifact)
                with pytest.raises(BoardError) as failure:
                    await task_methods.inspect_method(operator,original_pin['method_id'])
                assert failure.value.code=='method_version_unavailable'
            async with sessions() as db:
                proposal = await db.get(MemoryProposal,original_pin['method_id'])
                proposal.accepted_memory_id = original_pin['version']
            actual_stage = task_methods._stage
            async def divergent_candidate(*args,**kwargs):
                witness = await actual_stage(*args,**kwargs)
                changed = witness.candidate.model_copy(update={'input_parameters':{'home_integrity_test':True}})
                return replace(witness,candidate=changed)
            with monkeypatch.context() as scoped:
                scoped.setattr(task_methods,'_stage',divergent_candidate)
                with pytest.raises(BoardError) as failure:
                    await task_methods.inspect_method(operator,original_pin['method_id'])
                assert failure.value.code=='method_original_candidate_invalid'
            async def tombstone_after_physical_stage(*args,**kwargs):
                witness = await actual_stage(*args,**kwargs)
                async with sessions() as db:
                    db.add(MemoryTombstone(memory_id=original_pin['version']))
                return witness
            with monkeypatch.context() as scoped:
                scoped.setattr(task_methods,'_stage',tombstone_after_physical_stage)
                with pytest.raises(BoardError) as failure:
                    await task_methods.inspect_method(operator,original_pin['method_id'])
                assert failure.value.code=='method_version_unavailable'
            async with sessions() as db:
                from sqlalchemy import delete
                await db.execute(delete(MemoryTombstone).where(MemoryTombstone.memory_id==original_pin['version']))
            for control,expected in [({"delete_export_state":"canonical_memory_redacted"},"suppressed_metadata"),
                ("malformed","unavailable")]:
                async with sessions() as db:
                    memory = await db.get(Memory,original_pin["version"])
                    metadata = json.loads(original_metadata)
                    metadata["operator_control"] = control
                    memory.metadata_json = json.dumps(metadata)
                def deny_private_candidate(*args,**kwargs):
                    raise AssertionError('Suppressed accepted candidate reached private artifact read')
                with monkeypatch.context() as scoped:
                    scoped.setattr(task_methods,'read_private_proof',deny_private_candidate)
                    with pytest.raises(BoardError):
                        await task_methods.inspect_method(operator,original_pin['method_id'])
                await read_home()
                affected = [row["method"] for section in ("task_next_actions","prepared_outputs")
                    for row in snapshots[-1][section]["items"] if row.get("method")
                    and row["method"]["method_id"]==original_pin["method_id"]]
                assert affected and all(m["lifecycle"]==expected and m["target"] is None for m in affected)
            async with sessions() as db:
                memory = await db.get(Memory,original_pin["version"])
                memory.metadata_json = original_metadata
                db.add(MemoryTombstone(memory_id=original_pin["version"]))
            await read_home()
            affected = [row["method"] for section in ("task_next_actions","prepared_outputs")
                for row in snapshots[-1][section]["items"] if row.get("method")
                and row["method"]["method_id"]==original_pin["method_id"]]
            assert affected and all(m["lifecycle"]=="suppressed_metadata" and m["target"] is None for m in affected)
        methods = [row["method"] for body in snapshots for section in ("task_next_actions","prepared_outputs")
            for row in body[section]["items"] if row.get("method")]
        assert any(m["status"]=="baseline" for m in methods)
        active = [m for m in methods if m["status"]=="admitted" and m["lifecycle"]=="active_metadata"]
        rolled_back = [m for m in methods if m["status"]=="admitted" and m["lifecycle"]=="rolled_back_metadata"]
        assert active and rolled_back,methods
        assert active[0]["target"] and rolled_back[0]["target"]
        assert (active[0]["method_id"],active[0]["version"],active[0]["digest"]) == (
            rolled_back[0]["method_id"],rolled_back[0]["version"],rolled_back[0]["digest"])
    finally:
        await method_admission_lifecycle.close()
