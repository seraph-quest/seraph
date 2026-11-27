"""The source inventory guard (#1205, design #1026 §5).

Every lifecycle shape the AST scan finds in ``backend/src`` must be a row of
``runtime_plugins.inventory``: an owner with a real domain and its A3.3-1
bundle, a listed gap with tracked work, or a reasoned exclusion.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
import re
import textwrap

import pytest

from src.runtime_plugins import inventory
from src.runtime_plugins.lifecycle import job_domain
from src.runtime_plugins.ownership import DOMAINS
from src.runtime_plugins.profiles import BUNDLE_NAMES
from src.runtime_plugins.trust import TRUST_BUNDLE


@pytest.fixture(scope="module")
def scan() -> frozenset[tuple[str, str]]:
    """One AST pass over backend/src, shared by the tests in this file."""
    return frozenset(inventory.scan_lifecycle_shapes())


def test_every_lifecycle_shape_in_src_is_mapped_gapped_or_excluded(scan):
    missing = inventory.unaccounted(set(scan))
    assert not missing, (
        "runtime owners in backend/src with no row in src/runtime_plugins/inventory.py; add each to OWNERS "
        f"(an existing domain), GAPS (with its tracked slice) or EXCLUSIONS (with a reason): {missing}")


def test_no_inventory_row_is_stale(scan):
    gone = inventory.stale(set(scan))
    assert not gone, f"inventory rows the scan no longer finds (delete or rename them): {gone}"


def test_every_row_passes_row_problems():
    rows = inventory.rows()
    assert len(rows) == len(inventory.OWNERS) + len(inventory.GAPS) + len(inventory.EXCLUSIONS)
    assert inventory.OWNERS and inventory.GAPS and inventory.EXCLUSIONS
    bad = {key: problems for key, row in rows.items() if (problems := inventory.row_problems(row))}
    assert not bad, f"inventory rows that contradict the domain, bundle or owner catalogues: {bad}"
    for target, bundle in inventory.PLANNED_SEAMS.items():
        assert target is None or target not in DOMAINS, target
        assert bundle is None or bundle in BUNDLE_NAMES, target


def test_every_gap_cites_a_tracked_issue():
    assert not hasattr(inventory, "UNTRACKED_SEAM_SLICE")
    untracked = {row.key: row.owner for row in inventory.GAPS if not re.fullmatch(r"#[1-9][0-9]*", row.owner)}
    assert not untracked, f"gaps whose owner is not a tracked issue: {untracked}"
    assert all(re.fullmatch(r"#[1-9][0-9]*", owner) for owner in inventory.GAP_OWNERS), dict(inventory.GAP_OWNERS)
    assert {row.owner for row in inventory.GAPS} == set(inventory.GAP_OWNERS), "remove unused GAP_OWNERS entries"


_OWNER = inventory.Owner("src.pkg.mod:Thing", frozenset({"lifecycle_method"}), "seraph.memory.v1",
                         "seraph-domains", "python", False)
_GAP = inventory.Gap("src.pkg.mod:Thing", frozenset({"router"}), "seraph.http.v1", "seraph-cockpit", "python",
                     False, "#1208", "one line")


@pytest.mark.parametrize(("row", "problem"), [
    # A stated bundle that is not the domain's A3.3-1 bundle.
    (dataclasses.replace(_OWNER, bundle="seraph-agent"), "owner_bundle_mismatch"),
    (dataclasses.replace(_OWNER, bundle="seraph-nonesuch"), "bundle_unknown"),
    (dataclasses.replace(_OWNER, domain="seraph.nonesuch.v1"), "owner_domain_unknown"),
    (dataclasses.replace(_OWNER, required=True), "owner_required_mismatch"),
    (dataclasses.replace(_OWNER, domain="seraph.state.v1", bundle=TRUST_BUNDLE), "owner_required_mismatch"),
    (dataclasses.replace(_OWNER, provider="rust"), "provider_unknown"),
    (dataclasses.replace(_OWNER, shapes=frozenset({"teleport"})), "shape_unknown"),
    (dataclasses.replace(_OWNER, key="no-colon"), "key_invalid"),
    (dataclasses.replace(_GAP, bundle="seraph-edge"), "gap_bundle_mismatch"),
    (dataclasses.replace(_GAP, target="seraph.memory.v1"), "gap_has_owner_domain"),
    (dataclasses.replace(_GAP, owner="#9999"), "gap_owner_unknown"),
    (dataclasses.replace(_GAP, owner="untracked: some seam"), "gap_owner_untracked"),
    (dataclasses.replace(_GAP, reason="two\nlines"), "reason_not_one_line"),
    (inventory.Exclusion("src.pkg.mod:Thing", frozenset({"thread"}), "vibes", "why"), "exclusion_kind_unknown"),
    (inventory.Exclusion("src.pkg.mod:Thing", frozenset({"thread"}), "utility", " "), "reason_not_one_line"),
])
def test_row_problems_rejects_a_planted_bad_row(row, problem):
    assert inventory.row_problems(_OWNER) == [] and inventory.row_problems(_GAP) == []
    assert problem in inventory.row_problems(row)


def test_system_jobs_map_to_the_domain_that_gates_them(scan):
    jobs = {row.key: row for row in (*inventory.OWNERS, *inventory.GAPS) if "[job=" in row.key}
    found = {key for key, _shape in scan if "[job=" in key}
    assert found and set(jobs) == found
    for key, row in jobs.items():
        gate = job_domain(key.split("[job=", 1)[1].rstrip("]"))
        if gate is not None:
            assert isinstance(row, inventory.Owner) and row.domain == gate, key


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "src"
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
    return root


PLANTED = """
    import asyncio, subprocess, threading
    from fastapi import APIRouter, FastAPI

    router = APIRouter()

    class Planted:
        async def start(self):
            asyncio.create_task(self.run())
        async def stop(self):
            pass

    @router.websocket("/ws/planted")
    async def planted_socket(websocket):
        pass

    def schedule(scheduler):
        scheduler.add_job(print, id="planted")
        return [{"func": print, "trigger": None, "id": "planted_job"}]

    def spawn():
        subprocess.Popen(["true"])
        threading.Thread(target=spawn)

    def make():
        return FastAPI()

    def not_spawning(repository):
        return repository.create_task(title="a board row, not an asyncio task")
"""


def test_scan_finds_each_planted_shape(tmp_path):
    found = inventory.scan_lifecycle_shapes(_tree(tmp_path, {"pkg/planted.py": PLANTED}))
    assert found == {
        ("src.pkg.planted:router", "router"),
        ("src.pkg.planted:Planted", "lifecycle_method"),
        ("src.pkg.planted:Planted.start", "background_task"),
        ("src.pkg.planted:planted_socket", "websocket"),
        ("src.pkg.planted:schedule", "scheduler_job"),
        ("src.pkg.planted:schedule[job=planted_job]", "scheduler_job"),
        ("src.pkg.planted:spawn", "subprocess"),
        ("src.pkg.planted:spawn", "thread"),
        ("src.pkg.planted:make", "app_factory"),
    }


@pytest.mark.parametrize(("imports", "body", "shape"), [
    # Executor and timer threads.
    ("import asyncio", "asyncio.get_running_loop().run_in_executor(None, print)", "thread"),
    ("import threading", "threading.Timer(1, print)", "thread"),
    ("from threading import Timer", "Timer(1, print)", "thread"),
    ("from concurrent.futures import ThreadPoolExecutor", "ThreadPoolExecutor(thread_name_prefix='p')", "thread"),
    ("import concurrent.futures", "concurrent.futures.ThreadPoolExecutor(max_workers=1)", "thread"),
    ("from concurrent import futures", "futures.ThreadPoolExecutor()", "thread"),
    ("import asyncio", "asyncio.to_thread(print)", "thread"),
    ("from asyncio import to_thread", "to_thread(print)", "thread"),
    ("from threading import Thread as T", "T(target=print)", "thread"),
    # Subprocess aliases and forms.
    ("import subprocess as sp", "sp.run(['true'])", "subprocess"),
    ("from subprocess import run", "run(['true'])", "subprocess"),
    ("from subprocess import check_output", "check_output(['true'])", "subprocess"),
    ("from subprocess import Popen as P", "P(['true'])", "subprocess"),
    ("import os", "os.system('true')", "subprocess"),
    ("import os", "os.popen('true')", "subprocess"),
    ("import os", "os.execv('/bin/true', ['true'])", "subprocess"),
    ("import multiprocessing as mp", "mp.Process(target=print)", "subprocess"),
    ("from multiprocessing import Process", "Process(target=print)", "subprocess"),
    ("import multiprocessing", "multiprocessing.get_context('spawn').Process(target=print)", "subprocess"),
    ("from concurrent.futures import ProcessPoolExecutor", "ProcessPoolExecutor()", "subprocess"),
    ("import asyncio as aio", "aio.create_subprocess_exec('true')", "subprocess"),
    ("import asyncio", "asyncio.get_running_loop().subprocess_exec(object, 'true')", "subprocess"),
    # Task spawns.
    ("from asyncio import create_task", "create_task(print())", "background_task"),
    ("import asyncio", "asyncio.TaskGroup().create_task(print())", "background_task"),
    # Routes, hooks and process-wide handlers.
    ("", "app.add_api_route('/p', print)", "router"),
    ("", "app.add_event_handler('startup', print)", "lifespan_step"),
    ("import atexit", "atexit.register(print)", "process_hook"),
    ("import signal", "signal.signal(signal.SIGTERM, print)", "process_hook"),
    ("from signal import signal, SIGTERM", "signal(SIGTERM, print)", "process_hook"),
    ("import asyncio", "asyncio.get_running_loop().add_signal_handler(15, print)", "process_hook"),
])
def test_scan_finds_each_planted_form(tmp_path, imports, body, shape):
    source = f"{imports}\n\ndef planted(app):\n    {body}\n"
    found = inventory.scan_lifecycle_shapes(_tree(tmp_path, {"pkg/planted.py": source}))
    assert found == {("src.pkg.planted:planted", shape)}


def test_scan_finds_taskgroup_and_on_event_forms(tmp_path):
    source = """
        import asyncio
        import concurrent.futures

        POOL = concurrent.futures.ThreadPoolExecutor(thread_name_prefix="planted")

        async def grouped():
            async with asyncio.TaskGroup() as group:
                group.create_task(asyncio.sleep(0))

        @app.on_event("startup")
        async def boot():
            pass
    """
    found = inventory.scan_lifecycle_shapes(_tree(tmp_path, {"pkg/planted.py": source}))
    assert found == {("src.pkg.planted:<module>", "thread"), ("src.pkg.planted:grouped", "background_task"),
                     ("src.pkg.planted:boot", "lifespan_step")}


def test_scan_ignores_non_spawning_lookalikes(tmp_path):
    source = """
        from threading import Lock
        import signal

        def planted(repository, board):
            Lock()
            repository.create_task(title="a board row")
            board.run(["not", "subprocess"])
            signal.getsignal(signal.SIGTERM)
    """
    assert inventory.scan_lifecycle_shapes(_tree(tmp_path, {"pkg/planted.py": source})) == set()


def test_guard_fails_on_a_planted_unmapped_owner(tmp_path, scan):
    planted = inventory.scan_lifecycle_shapes(_tree(tmp_path, {"pkg/planted.py": PLANTED}))
    assert inventory.unaccounted(set(scan) | planted) == sorted(planted)
    # A new shape inside an already-listed owner is also caught.
    extra = ("src.conversation.task_context:TaskContinuityService", "subprocess")
    assert inventory.unaccounted(set(scan) | {extra}) == [extra]


def test_scan_keys_lifespan_steps_by_callee(tmp_path):
    source = """
        async def _lifespan_services(app):
            import logging
            logging.getLogger(__name__).warning("noise")
            await planted_service.start()
            async with planted_scope():
                pass
    """
    found = inventory.scan_lifecycle_shapes(_tree(tmp_path, {"app.py": source}))
    assert found == {("src.app:_lifespan_services[step=planted_service.start]", "lifespan_step"),
                     ("src.app:_lifespan_services[step=planted_scope]", "lifespan_step")}


def test_duplicate_keys_are_refused(monkeypatch):
    monkeypatch.setattr(inventory, "EXCLUSIONS", (*inventory.EXCLUSIONS, inventory.EXCLUSIONS[0]))
    with pytest.raises(ValueError, match="listed twice"):
        inventory.rows()
