"""Original CPU producer readback, bounded FIFO and final SQLite writer proofs."""
import asyncio
from dataclasses import replace
import os
import selectors
import sqlite3
import subprocess
import sys
import tempfile
import time

import pytest

from tests.test_inference_accounting import accounting_db
from tests.test_general_task_planner import forbid_external_inference
from tests.test_document_build_storage import setup, SPEC
from tests.test_document_build_native_capacity import admitted_build, build_admission_lifecycle
from src.work_board import document_build_storage as storage
from src.work_board import document_pairs as sources
from src.work_board.repository import BoardError


async def test_private_fifo_open_is_bounded_before_decryption(accounting_db, monkeypatch, record_property):
    from config.settings import settings
    _token, operator, owner, goal = await setup(accounting_db, monkeypatch)
    sessions = accounting_db[2].accounting_sessions
    async with sessions() as db:
        created = await storage.create(db, owner, operator, storage.BuildCreate(goal_id=goal.id,
            goal_revision=1, spec=SPEC, idempotency_key="fifo-reader"))
        row, value = await storage.owned(db, owner, created["build_id"])
        path = sources.source_path(row, value, "spec")
        receipt = value["sources"]["spec"]
    path.unlink(); os.mkfifo(path, 0o600)
    code = """import json,sys
from pathlib import Path
from config.settings import settings
settings.workspace_dir=sys.argv[1]
from src.work_board.document_pairs import read_private
print('READY',flush=True)
if sys.stdin.readline() != 'GO\\n':
 raise AssertionError('FIFO command missing')
print('READ_ENTERED',flush=True)
try:
 read_private(Path(sys.argv[2]),json.loads(sys.argv[3]),maximum=65536)
except ValueError:
 print('regular-file-denied')
else:
 raise AssertionError('FIFO unexpectedly read')
"""
    import json
    def run_fifo_child():
        argv = [sys.executable, "-c", code, str(settings.workspace_dir), str(path), json.dumps(receipt)]
        with tempfile.TemporaryFile() as stderr:
            child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=stderr, bufsize=0)
            try:
                startup_started = time.monotonic()
                ready = b""
                os.set_blocking(child.stdout.fileno(), False)
                with selectors.DefaultSelector() as selector:
                    selector.register(child.stdout, selectors.EVENT_READ)
                    while b"\n" not in ready:
                        remaining = 60 - (time.monotonic() - startup_started)
                        if remaining <= 0 or not selector.select(remaining):
                            raise subprocess.TimeoutExpired(argv, 60)
                        chunk = os.read(child.stdout.fileno(), 16)
                        assert chunk, "FIFO child exited before readiness"
                        ready += chunk
                        assert len(ready) <= 16, "Unexpected FIFO readiness output"
                assert ready == b"READY\n"
                startup_seconds = time.monotonic() - startup_started
                # One original three-second window includes GO, entry and exit.
                operation_started = time.monotonic()
                stdout, _ = child.communicate(b"GO\n", timeout=3 - (time.monotonic() - operation_started))
                operation_seconds = time.monotonic() - operation_started
                assert operation_seconds <= 3
                assert stdout.splitlines() == [b"READ_ENTERED", b"regular-file-denied"]
                assert child.returncode == 0
                assert child.wait(timeout=1) == 0
                return subprocess.CompletedProcess(argv, child.returncode,
                    stdout.decode().removeprefix("READ_ENTERED\n")), {
                    "pid": child.pid, "markers": ["READY", "READ_ENTERED", "regular-file-denied"],
                    "startup_seconds": startup_seconds, "operation_seconds": operation_seconds,
                    "operation_limit_seconds": 3, "returncode": child.returncode, "reaped": True,
                }
            except BaseException as error:
                stderr.seek(0)
                error.add_note("FIFO child stderr: " + stderr.read(65536).decode(errors="replace"))
                raise
            finally:
                try:
                    if child.poll() is None:
                        child.terminate()
                        try:
                            child.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=1)
                    assert child.poll() is not None, "FIFO child cleanup did not prove exit"
                finally:
                    child.stdin.close()
                    child.stdout.close()

    result, physical_receipt = await asyncio.to_thread(run_fifo_child)
    record_property("fifo_physical_readback", json.dumps(physical_receipt, sort_keys=True))
    print("fifo_physical_readback=" + json.dumps(physical_receipt, sort_keys=True))
    assert result.stdout.strip() == "regular-file-denied"
    async with sessions() as db:
        with pytest.raises(BoardError):
            await storage.retire(db, owner, operator, row.artifact_id, storage.BuildRetire(
                expected_revision=created["revision"], idempotency_key="fifo-retire"))
    async with sessions() as db:
        row, _value = await storage.owned(db, owner, row.artifact_id)
        assert row.document_reserved_bytes == storage.CHARGE


@pytest.mark.parametrize("mode", ["valid_and_foreign_proofs", "stale_reservation", "plaintext_mismatch"])
async def test_original_process_readback_seal_and_sql_writer(accounting_db, monkeypatch,
        forbid_external_inference, mode, build_admission_lifecycle):
    from src.native_tools.task_adapters import ToolRegistry
    from src.work_board.general_task import GeneralTaskService
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.work_board.general_task_native import run_native_step
    from src.vault import crypto
    from src.work_board import dispatcher as dispatch_module
    _token, operator, owner, goal = await setup(accounting_db, monkeypatch)
    await build_admission_lifecycle.start()
    sessions = accounting_db[2].accounting_sessions
    async with sessions() as db:
        foreign = await storage.create(db, owner, operator, storage.BuildCreate(goal_id=goal.id,
            goal_revision=1, spec=SPEC, idempotency_key="genuine-other-build"))
        foreign_row, foreign_value = await storage.owned(db, owner, foreign["build_id"])
    registry = ToolRegistry(); registry.start()
    service = GeneralTaskService(registry); service.start()
    dispatcher = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
    monkeypatch.setattr(dispatch_module, "_dispatcher", dispatcher)
    original_adopt, original_publish = storage.adopt_outputs, storage.publish_publications
    checked = []
    def publish(row, value, staged):
        if mode == "plaintext_mismatch":
            original_read = sources.read_private
            def changed_read(path, receipt, *, maximum):
                raw = original_read(path, receipt, maximum=maximum)
                if path.name == "g1-editable.fernet":
                    checked.append("mismatched_plaintext")
                    return raw[:-1]+bytes([raw[-1]^1])
                return raw
            with monkeypatch.context() as patch:
                patch.setattr(sources, "read_private", changed_read)
                return original_publish(row, value, staged)
        return original_publish(row, value, staged)
    monkeypatch.setattr(storage, "publish_publications", publish)
    def adopt(row, value, staged, *, readback, native_binding, reap):
        with sqlite3.connect(accounting_db[0]/"seraph.db", timeout=0) as contender:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("BEGIN IMMEDIATE")
        before = (row.revision, row.document_metadata_json, row.metadata_digest)
        if mode == "stale_reservation":
            # A real original owner metadata transition advances the reservation;
            # the previously verified bytes cannot grant adoption at that revision.
            checked.append("stale_reservation")
            storage.persist(row, value)
            return original_adopt(row, value, staged, readback=readback, native_binding=native_binding, reap=reap)
        for bad in (None, replace(readback), storage.BuildOutputReadback(staged, readback.binding_digest)):
            with pytest.raises(BoardError):
                original_adopt(row, value, staged, readback=bad, native_binding=native_binding, reap=reap)
            assert (row.revision, row.document_metadata_json, row.metadata_digest) == before
        with pytest.raises(BoardError):
            original_adopt(foreign_row, foreign_value, staged, readback=readback,
                native_binding=native_binding, reap=reap)
        with pytest.raises(BoardError):
            original_adopt(row, value, replace(staged), readback=readback, native_binding=native_binding, reap=reap)
        def forbidden_io(*args, **kwargs):
            raise AssertionError("output I/O inside final adoption writer")
        with monkeypatch.context() as patch:
            patch.setattr(os, "open", forbidden_io); patch.setattr(os, "read", forbidden_io)
            patch.setattr(sources, "read_private", forbidden_io); patch.setattr(crypto, "_get_fernet", forbidden_io)
            result = original_adopt(row, value, staged, readback=readback, native_binding=native_binding, reap=reap)
        checked.append(True)
        return result
    monkeypatch.setattr(storage, "adopt_outputs", adopt)
    try:
        binding, identifier = await admitted_build(accounting_db, service, dispatcher, operator, owner, goal,
            "readback-"+mode)
        if mode == "valid_and_foreign_proofs":
            await run_native_step(service, dispatcher.jobs, binding, child_owner="actual-readback",
                principal=operator.principal)
            assert checked == [True]
        else:
            with pytest.raises(Exception):
                await run_native_step(service, dispatcher.jobs, binding, child_owner="actual-readback",
                    principal=operator.principal)
            assert checked == ["stale_reservation"] if mode == "stale_reservation" else checked and set(checked) == {"mismatched_plaintext"}
        async with sessions() as db:
            row, value = await storage.owned(db, owner, identifier)
            assert row.document_reserved_bytes == storage.CHARGE
            if mode != "valid_and_foreign_proofs":
                assert not value.get("output") and value.get("pending")
            other, _value = await storage.owned(db, owner, foreign["build_id"])
            assert other.document_reserved_bytes == storage.CHARGE and other.revision == foreign["revision"]
        from sqlalchemy import select
        from src.db.models import InferenceCostReservation
        async with sessions() as db:
            assert list((await db.scalars(select(InferenceCostReservation))).all()) == []
    finally:
        service.stop(); registry.stop()
