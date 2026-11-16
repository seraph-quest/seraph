"""Meta-tests: each leak check fails a deliberately leaky test (#1223).

Every case runs a small test file in a fresh pytest subprocess (pytester) with
only the plugin under test loaded, so the leak it provokes never reaches this
process and nothing here depends on the root conftest. Each leaky test has a
clean twin that must pass, so a check that fails everything is caught too.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.support import leaks
from tests.support.processes import spawned  # noqa: F401 - fixture

pytest_plugins = ["pytester"]

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _run(pytester: pytest.Pytester, source: str, *plugins: str, conftest: str | None = None):
    pytester.makepyfile(test_subject=source)
    if conftest is not None:
        pytester.makeconftest(conftest)
    pytester.makeini("[pytest]\nasyncio_mode = auto\nasyncio_default_fixture_loop_scope = function\n")
    arguments = ["-p", "no:cacheprovider", "-p", "no:randomly", "-q", "-rA"]
    for plugin in plugins:
        arguments += ["-p", plugin]
    return pytester.runpytest_subprocess(*arguments)


@pytest.fixture
def leak_pytester(pytester, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(BACKEND_ROOT), os.environ.get("PYTHONPATH", "")]))
    return pytester


TASKS = '''
import asyncio

async def test_leaks_a_task():
    asyncio.create_task(asyncio.Event().wait(), name="forgotten-waiter")

async def test_awaits_its_task():
    task = asyncio.create_task(asyncio.sleep(0), name="awaited")
    await task

async def test_cancels_its_task():
    task = asyncio.create_task(asyncio.Event().wait(), name="cancelled")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
'''


def test_task_check_fails_a_test_that_leaves_a_task_pending(leak_pytester):
    result = _run(leak_pytester, TASKS, "tests.support.leaks")
    result.assert_outcomes(passed=3, errors=1)
    result.stdout.fnmatch_lines([
        "*ERROR at teardown of test_leaks_a_task*",
        "*asyncio task(s) still pending*'forgotten-waiter'*",
    ])


TASK_IN_ASYNC_FIXTURE = '''
import asyncio
import pytest_asyncio

@pytest_asyncio.fixture
async def background():
    yield asyncio.create_task(asyncio.Event().wait(), name="fixture-spawned")

def test_sync_test_with_a_leaky_async_fixture(background):
    assert not background.done()
'''


def test_task_check_covers_tasks_left_by_async_fixtures_of_sync_tests(leak_pytester):
    result = _run(leak_pytester, TASK_IN_ASYNC_FIXTURE, "tests.support.leaks")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*asyncio task(s) still pending*'fixture-spawned'*"])


THREADS = '''
import threading

STOP = threading.Event()

def test_leaks_a_thread():
    threading.Thread(target=STOP.wait, name="forgotten-thread", daemon=True).start()

def test_joins_its_thread():
    done = threading.Event()
    worker = threading.Thread(target=done.set, name="joined-thread")
    worker.start()
    worker.join()
    assert done.is_set()

def test_zz_release():
    STOP.set()
'''


def test_thread_check_fails_a_test_that_leaves_a_thread_running(leak_pytester):
    result = _run(leak_pytester, THREADS, "tests.support.leaks")
    result.assert_outcomes(passed=3, errors=1)
    result.stdout.fnmatch_lines([
        "*ERROR at teardown of test_leaks_a_thread*",
        "*thread(s) started by the test still running after teardown*'forgotten-thread'*Event.wait*",
    ])


CHILDREN = f'''
import subprocess
import sys

# Holding the handle keeps Popen.__del__ from reaping the zombie behind the check.
HANDLES = []

def test_leaks_a_running_child():
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])

def test_leaks_an_unreaped_child():
    process = subprocess.Popen([sys.executable, "-c", "pass"], stdout=subprocess.PIPE)
    HANDLES.append(process)
    # EOF on stdout: the child exited. Nothing has waited for it, so it is a zombie.
    assert process.stdout.read() == b""
    process.stdout.close()

def test_reaps_its_child():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    assert process.wait() == 0

def test_runs_a_child_to_completion():
    assert subprocess.run([sys.executable, "-c", "pass"]).returncode == 0
'''


def test_child_check_fails_running_and_zombie_children_and_reaps_them(leak_pytester):
    result = _run(leak_pytester, CHILDREN, "tests.support.leaks")
    result.assert_outcomes(passed=4, errors=2)
    result.stdout.fnmatch_lines([
        "*ERROR at teardown of test_leaks_a_running_child*",
        "*child process(es) left running or unreaped*killed and reaped*",
        "*ERROR at teardown of test_leaks_an_unreaped_child*",
        "*child process(es) left running or unreaped*",
    ])


EXCUSED_CONFTEST = '''
from tests.support import leaks

leaks.LEAK_EXCEPTIONS = (
    leaks.LeakException(check="children", tests=("test_subject.py::test_excused",), match="pid",
                        issue=99999, reason="meta-test exception"),
)
'''

EXCUSED = '''
import subprocess
import sys

def test_excused():
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])

def test_not_excused():
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
'''


def test_an_exception_covers_only_its_named_tests_and_still_cleans_up(leak_pytester):
    result = _run(leak_pytester, EXCUSED, "tests.support.leaks", conftest=EXCUSED_CONFTEST)
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(["*ERROR at teardown of test_not_excused*"])
    result.stdout.fnmatch_lines(["*tracked leak exception*", "*#99999: meta-test exception*"])
    # The excused child was killed and reaped too: the next test did not inherit it.
    assert "ERROR test_subject.py::test_excused" not in result.stdout.str()


STATE_CONFTEST = '''
import asyncio
import sys
import threading
import types

from tests.support import state_isolation
from tests.support.process_state import Entry, Restore

fake = types.ModuleType("fake_state_module")
fake.CACHE = {"seed": 1}
fake.counter = 0
fake.LOCK = threading.Lock()
fake.ALOCK = asyncio.Lock()
fake.PERMITS = threading.Semaphore(2)

class Service:
    def __init__(self):
        self.jobs = {}
        self.started = False

fake.service = Service()
sys.modules["fake_state_module"] = fake

state_isolation.registry_provider = lambda: {
    "fake_state_module:CACHE": Entry("container", Restore()),
    "fake_state_module:counter": Entry("rebound", Restore()),
    "fake_state_module:LOCK": Entry("primitive", Restore()),
    "fake_state_module:ALOCK": Entry("primitive", Restore()),
    "fake_state_module:PERMITS": Entry("primitive", Restore()),
    "fake_state_module:service": Entry("singleton", Restore()),
}
'''

STATE = '''
import asyncio
import fake_state_module as fake

def test_a_mutates_everything():
    fake.CACHE["leaked"] = True
    fake.counter = 41
    fake.service.jobs["job"] = "running"
    fake.service.started = True
    fake.service.extra = "attribute"

def test_b_sees_pristine_state():
    assert fake.CACHE == {"seed": 1}
    assert fake.counter == 0
    assert fake.service.jobs == {} and fake.service.started is False
    assert not hasattr(fake.service, "extra")

def test_c_leaves_a_lock_held():
    fake.LOCK.acquire()

def test_d_lock_was_reported_not_hidden():
    # The restore does not release a leaked lock behind the owner's back.
    assert fake.LOCK.locked()
    fake.LOCK.release()

def test_e_leaves_a_permit_taken():
    fake.PERMITS.acquire()

async def test_f_contends_an_asyncio_lock():
    async with fake.ALOCK:
        waiter = asyncio.ensure_future(fake.ALOCK.acquire())
        await asyncio.sleep(0)
    await waiter
    fake.ALOCK.release()

async def test_g_reuses_the_asyncio_lock_on_a_new_loop():
    async with fake.ALOCK:
        waiter = asyncio.ensure_future(fake.ALOCK.acquire())
        await asyncio.sleep(0)
    await waiter
    fake.ALOCK.release()
'''


def test_state_isolation_restores_registered_state_and_fails_held_primitives(leak_pytester):
    result = _run(leak_pytester, STATE, "tests.support.state_isolation", conftest=STATE_CONFTEST)
    result.assert_outcomes(passed=7, errors=2)
    result.stdout.fnmatch_lines([
        "*ERROR at teardown of test_c_leaves_a_lock_held*",
        "*fake_state_module:LOCK is still held after the test*",
        "*ERROR at teardown of test_e_leaves_a_permit_taken*",
        "*fake_state_module:PERMITS has 1 free permit(s), expected 2*",
    ])


LAZY_CONFTEST = '''
from tests.support import state_isolation
from tests.support.process_state import Entry, Restore

state_isolation.registry_provider = lambda: {"lazy_state:CACHE": Entry("container", Restore())}
'''

LAZY = '''
import sys

def test_a_imports_and_mutates():
    assert "lazy_state" not in sys.modules
    import lazy_state
    lazy_state.CACHE["leaked"] = True

def test_b_sees_the_import_time_state():
    import lazy_state
    assert lazy_state.CACHE == {"seed": 1}
'''


def test_state_isolation_restores_a_module_first_imported_during_a_test(leak_pytester):
    leak_pytester.makepyfile(lazy_state='CACHE = {"seed": 1}\n')
    result = _run(leak_pytester, LAZY, "tests.support.state_isolation", conftest=LAZY_CONFTEST)
    result.assert_outcomes(passed=2)


def test_child_enumeration_sees_a_child_without_reaping_it(spawned):
    import subprocess

    process = spawned.popen([sys.executable, "-c", "pass"], stdout=subprocess.PIPE)
    assert process.stdout.read() == b""
    assert process.pid in leaks.child_pids()
    # Looking did not reap it: the owner still collects the exit status.
    assert process.wait() == 0
    assert process.pid not in leaks.child_pids()


SUBREAPER = '''
from tests.support import leaks

def test_leaves_the_subreaper_flag_set():
    leaks.set_child_subreaper(1)

def test_next_test_finds_it_restored():
    assert leaks.child_subreaper() == 0
'''


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the child-subreaper flag exists only on Linux")
def test_subreaper_check_fails_a_test_that_leaves_the_flag_set_and_restores_it(leak_pytester):
    result = _run(leak_pytester, SUBREAPER, "tests.support.leaks")
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(["*ERROR at teardown of test_leaves_the_subreaper_flag_set*",
                                 "*child-subreaper flag at 1 (was 0; now restored)*"])
