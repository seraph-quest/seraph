"""Autouse leak checks for asyncio tasks, threads and child processes (#1223).

Registered as a pytest plugin by the root ``tests/conftest.py``. Each check
fails the test that left the resource behind, at teardown, after every one of
the test's own fixtures has been finalized:

``tasks``
    An asyncio task still pending when the test's event loop is about to
    close. pytest-asyncio's runner would otherwise cancel it silently, so a
    test that forgot to await or cancel its own background work stayed green.
    The check runs just before the runner closes the loop, after every async
    fixture on that loop (including the ``db`` fixture's tracked-task drain)
    has finished. A loop finalizer task closing an async generator nobody
    closed counts too, unless the generator belongs to a package in
    ``THIRD_PARTY_FINALIZED_GENERATORS`` (#1244). Only pytest-asyncio's runner
    loop is checked: tasks left on a loop a test runs itself (``asyncio.run``,
    an anyio portal) are cancelled by that loop's own close and not reported.
``threads``
    A thread started during the test that is still running test or product
    code once teardown is over. A thread that has already returned from its
    target and is only finishing the ``threading`` bootstrap is joined under a
    generous liveness bound instead of being reported: the decision depends on
    what the thread is executing, never on how long it took.
``children``
    A direct child process of the test process (running or an unreaped
    zombie) that did not exist before the test. ``os.waitpid`` is never used
    to look, because it would reap the zombie the check is meant to report.
    The standard library's per-process ``multiprocessing`` helpers (resource
    tracker, forkserver) are not counted.

Known limits: the thread and child baselines are taken before session- and
module-scoped fixtures set up, so a resource such a fixture starts would be
blamed on the first test that uses it (none does today).

The Linux child-subreaper flag is checked the same way: it is process-wide
kernel state, and a test that leaves it set makes every later orphan a
zombie of the test process.

A leaked resource is cleaned up after it is reported (tasks are cancelled by
the closing runner, children are killed and reaped, a still-running thread is
added to the baseline) so one leak fails one test, not every later test.

Narrow exceptions live in one registry, ``LEAK_EXCEPTIONS`` below. Each names
the check, the test files or node-id prefixes it covers, what it matches and
an open issue. ``PROCESS_LIFETIME_THREADS`` lists threads that src starts once
per process by design; they are not leaks of the test that happened to start
them. Never add an entry to make a check pass without an issue that owns the
root cause.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import gc
import inspect
import linecache
import os
import signal
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pytest

# A thread that has returned from its target finishes the ``threading``
# bootstrap in microseconds. This bound only turns a wedged exit into a
# readable failure; it never decides whether a thread leaked.
THREAD_EXIT_LIVENESS_SECONDS = 30.0
# A SIGKILLed child is normally reapable at once (see src.execution.process_reap).
CHILD_REAP_LIVENESS_SECONDS = 30.0


@dataclass(frozen=True)
class LeakException:
    """A narrow, issue-linked exception to one leak check.

    An excused child is still killed and reaped after the test, so it cannot
    reach the next test; only the failure is waived.
    """

    check: str  # "tasks" | "threads" | "children"
    tests: tuple[str, ...]  # node-id prefixes, e.g. "tests/test_x.py" or "tests/test_x.py::test_y"
    match: str  # substring of the leaked resource's description
    issue: int
    reason: str

    def covers(self, check: str, nodeid: str, description: str) -> bool:
        return (check == self.check and self.match in description
                and any(nodeid == prefix or nodeid.startswith(prefix if prefix.endswith((".py", "::")) else prefix + "::")
                        or nodeid.startswith(prefix + "[") for prefix in self.tests))


@dataclass(frozen=True)
class ProcessLifetimeThread:
    """A thread src starts at most once per process by design."""

    name_prefix: str
    owner: str
    reason: str


# One registry for every exception to the leak checks. Keep each entry as
# narrow as the evidence allows and delete it with the fix of its issue.
LEAK_EXCEPTIONS: tuple[LeakException, ...] = (
    LeakException(
        check="threads",
        tests=("tests/test_calendar_manual_negatives.py::"
               "test_calendar_real_vertical_model_timeout_keeps_root_liability_without_retry",),
        match="_connection_worker_thread",
        issue=1247,
        reason="the released late model callback can open an aiosqlite connection after the fixture "
               "disposed its engine; the test still waits on sleep(0.1) instead of the callback",
    ),
)

# Third-party packages whose internal async generators a loop finalizer may
# close after the owning code already closed what it holds (#1244): httpx
# iterates nested generators per response (aiter_bytes -> aiter_raw -> the
# transport stream) and closing the outer one does not close the inner ones.
# src closes every generator it owns with contextlib.aclosing; an unclosed
# generator from src or tests still fails the task check.
THIRD_PARTY_FINALIZED_GENERATORS: tuple[str, ...] = ("httpx", "httpcore")

PROCESS_LIFETIME_THREADS: tuple[ProcessLifetimeThread, ...] = (
    ProcessLifetimeThread(
        "LanceDBBackgroundEventLoop", "lancedb (src.memory.vector_store)",
        "lancedb runs one daemon event-loop thread per process for its sync API, started on first use",
    ),
    ProcessLifetimeThread(
        "seraph-browser-transport", "src.browser.pinned_transport._BLOCKING_TRANSPORT_EXECUTOR",
        "the two-slot process-wide blocking transport lane; its workers start on first use and live "
        "for the process by design",
    ),
    ProcessLifetimeThread(
        "DDGS_", "ddgs.DDGS.get_executor (src.tools.web_search_tool)",
        "the ddgs library caches one class-level ThreadPoolExecutor per process on first use",
    ),
)


def _exception_for(check: str, nodeid: str, description: str) -> LeakException | None:
    for entry in LEAK_EXCEPTIONS:
        if entry.covers(check, nodeid, description):
            return entry
    return None


# ── Child processes ─────────────────────────────────────


def _linux_children(pid: int) -> set[int] | None:
    base = Path("/proc") / str(pid) / "task"
    if not base.is_dir():
        return None
    children: set[int] = set()
    try:
        for task in base.iterdir():
            text = (task / "children").read_text()
            children.update(int(item) for item in text.split())
        return children
    except FileNotFoundError:
        pass  # kernel without CONFIG_PROC_CHILDREN: scan every process instead
    except OSError:
        return None
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        # The command name may contain spaces and parentheses; ppid follows the last ")".
        fields = stat.rsplit(")", 1)[-1].split()
        if len(fields) > 1 and int(fields[1]) == pid:
            children.add(int(entry.name))
    return children


_LIBPROC = None


def _darwin_children(pid: int) -> set[int] | None:
    global _LIBPROC
    if _LIBPROC is None:
        path = ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib"
        _LIBPROC = ctypes.CDLL(path, use_errno=True)
        _LIBPROC.proc_listchildpids.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        _LIBPROC.proc_listchildpids.restype = ctypes.c_int
    capacity = 64
    while True:
        buffer = (ctypes.c_int * capacity)()
        count = _LIBPROC.proc_listchildpids(pid, buffer, ctypes.sizeof(buffer))
        if count < 0:
            return None
        # Darwin returns a pid count; treat a full buffer as possibly truncated.
        if count < capacity:
            return {buffer[index] for index in range(count) if buffer[index] > 0}
        capacity *= 4


def child_pids() -> set[int]:
    """Direct children of this process, running or zombie, without reaping any."""

    pid = os.getpid()
    if sys.platform.startswith("linux"):
        children = _linux_children(pid)
    elif sys.platform == "darwin":
        children = _darwin_children(pid)
    else:  # pragma: no cover - CI and development hosts are Linux and macOS
        children = None
    if children is None:
        raise RuntimeError(f"cannot enumerate child processes on {sys.platform}")
    return children


def process_lifetime_children() -> set[int]:
    """Helper processes the standard library starts once per process by design.

    ``multiprocessing`` starts one resource tracker (and, with that start
    method, one forkserver) the first time a spawn/forkserver child starts;
    it serves every later child and exits with this process. It is not a
    leak of the test that happened to start the first child.
    """

    pids: set[int] = set()
    for module_name, attribute in (("multiprocessing.resource_tracker", "_resource_tracker"),
                                   ("multiprocessing.forkserver", "_forkserver")):
        module = sys.modules.get(module_name)
        helper = getattr(module, attribute, None) if module is not None else None
        pid = getattr(helper, "_pid", None) if helper is not None else None
        if isinstance(pid, int) and pid > 0:
            pids.add(pid)
    return pids


_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37


def _prctl():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    return libc.prctl


def child_subreaper() -> int | None:
    """This process's Linux child-subreaper flag, or None where there is none.

    The flag is process-wide kernel state: once set, every orphaned
    descendant of any later test is reparented to the test process and stays
    a zombie nobody waits for.
    """

    if not sys.platform.startswith("linux"):
        return None
    value = ctypes.c_int()
    if _prctl()(_PR_GET_CHILD_SUBREAPER, ctypes.addressof(value), 0, 0, 0) != 0:
        return None
    return value.value


def set_child_subreaper(value: int) -> None:
    if _prctl()(_PR_SET_CHILD_SUBREAPER, value, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_CHILD_SUBREAPER) failed")


def describe_child(pid: int) -> str:
    """A short description of a child for the failure message."""

    command = ""
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        pass
    if not command and sys.platform == "darwin" and _LIBPROC is not None:
        buffer = ctypes.create_string_buffer(4096)
        try:
            _LIBPROC.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            if _LIBPROC.proc_pidpath(pid, buffer, ctypes.sizeof(buffer)) > 0:
                command = buffer.value.decode(errors="replace")
        except (AttributeError, OSError):
            pass
    return f"pid {pid} ({command or 'zombie or exited; no command line'})"


def kill_and_reap(pid: int) -> str:
    """Kill a leaked child and collect it, so it fails one test only."""

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError as error:
        return f"not killed: {error}"
    reaped = threading.Event()
    outcome: list[str] = []

    def reap() -> None:
        try:
            os.waitpid(pid, 0)
            outcome.append("killed and reaped")
        except ChildProcessError:
            outcome.append("already reaped by its owner")
        finally:
            reaped.set()

    worker = threading.Thread(target=reap, name=f"leak-check-reap-{pid}", daemon=True)
    worker.start()
    if not reaped.wait(CHILD_REAP_LIVENESS_SECONDS):
        return f"killed, not reapable within the {CHILD_REAP_LIVENESS_SECONDS:g} s liveness bound"
    worker.join()
    return outcome[0]


# ── Threads ─────────────────────────────────────────────


def _threading_frames_only(thread: threading.Thread) -> bool:
    """True when the thread's ``run`` has returned and it is only exiting.

    Its stack then holds nothing but the ``threading`` bootstrap. A thread
    parked in ``Event.wait`` also shows only ``threading`` frames, but under
    ``Thread.run``, so it does not count.
    """

    frame = sys._current_frames().get(thread.ident)  # noqa: SLF001 - diagnostics
    if frame is None:
        return True
    threading_file = threading.__file__
    while frame is not None:
        if frame.f_code.co_filename != threading_file or frame.f_code.co_name == "run":
            return False
        frame = frame.f_back
    return True


def _aiosqlite_worker_finishing(thread: threading.Thread) -> bool:
    """An aiosqlite connection thread that is not parked on its request queue.

    ``Connection.close()`` returns once the worker has posted the stop result
    to the loop; the worker then breaks out of its loop without blocking
    again. A worker parked in ``queue.get`` serves an open connection and is
    reported; one still delivering its last result is waited for like an
    exiting thread. Decided by where the thread is, not by timing.
    """

    target = getattr(thread, "_target", None)
    if getattr(target, "__module__", None) != "aiosqlite.core":
        return False
    frame = sys._current_frames().get(thread.ident)  # noqa: SLF001 - diagnostics
    if frame is None:
        return True
    # The queue is a C SimpleQueue, so a parked worker's innermost Python
    # frame is the worker loop itself, on its ``tx.get()`` line.
    parked = (frame.f_code.co_name == "_connection_worker_thread"
              and ".get(" in linecache.getline(frame.f_code.co_filename, frame.f_lineno))
    return not parked


def _only_threading_code(thread: threading.Thread) -> bool:
    frame = sys._current_frames().get(thread.ident)  # noqa: SLF001 - diagnostics
    while frame is not None:
        if frame.f_code.co_filename != threading.__file__:
            return False
        frame = frame.f_back
    return True


def _is_process_lifetime(thread: threading.Thread) -> bool:
    return any(thread.name.startswith(entry.name_prefix) for entry in PROCESS_LIFETIME_THREADS)


def describe_thread(thread: threading.Thread) -> str:
    frame = sys._current_frames().get(thread.ident)  # noqa: SLF001 - diagnostics
    where = ""
    while frame is not None:
        if frame.f_code.co_filename != threading.__file__:
            where = f" in {frame.f_code.co_qualname} ({Path(frame.f_code.co_filename).name}:{frame.f_lineno})"
            break
        frame = frame.f_back
    target = getattr(thread, "_target", None)
    target_name = getattr(target, "__qualname__", None) or (repr(target) if target is not None else "run()")
    return f"thread {thread.name!r} daemon={thread.daemon} target={target_name}{where}"


def leaked_threads(before: Iterable[threading.Thread]) -> list[threading.Thread]:
    """Threads not in ``before`` that still run code after teardown.

    ``before`` holds the thread objects themselves, not their idents: the OS
    reuses idents, so a new thread could otherwise hide behind an old one's.
    """

    known = {id(thread) for thread in before}
    frames = sys._current_frames()  # noqa: SLF001 - diagnostics
    leaked = []
    for thread in threading.enumerate():
        if id(thread) in known or thread is threading.current_thread() or not thread.is_alive():
            continue
        if _is_process_lifetime(thread):
            continue
        if isinstance(thread, threading._DummyThread):  # noqa: SLF001
            # A thread threading did not start (``_thread`` or C code) that
            # called ``current_thread()``. Its ``is_alive`` is always True and
            # it cannot be joined, so it is judged only by whether the native
            # thread still runs.
            if thread.ident in frames:
                leaked.append(thread)
            continue
        # A cancelled ``threading.Timer`` (its ``finished`` event set) has been
        # told to stop and only has to wake and return; that is decided by the
        # event, not by timing, so wait for it like an exiting thread.
        # Only while nothing but ``threading`` code is on its stack: a callback
        # already running when ``cancel()`` arrived is still reported.
        cancelled_timer = (isinstance(thread, threading.Timer) and thread.finished.is_set()
                           and _only_threading_code(thread))
        if cancelled_timer or _threading_frames_only(thread) or _aiosqlite_worker_finishing(thread):
            thread.join(THREAD_EXIT_LIVENESS_SECONDS)
            if not thread.is_alive():
                continue
        leaked.append(thread)
    return leaked


# ── Tasks ───────────────────────────────────────────────


def describe_task(task: asyncio.Task) -> str:
    generator = finalized_generator(task)
    if generator is not None:
        code = generator.ag_code
        return (f"task {task.get_name()!r} finalizing the unclosed async generator {code.co_qualname} "
                f"({Path(code.co_filename).name}:{code.co_firstlineno}); close it with contextlib.aclosing")
    coro = task.get_coro()
    qualname = getattr(getattr(coro, "cr_code", None), "co_qualname", None) or repr(coro)
    return f"task {task.get_name()!r} running {qualname}"


def finalized_generator(task: asyncio.Task):
    """The async generator a loop finalizer task closes, if it is one.

    The loop's ``_asyncgen_finalizer_hook`` schedules ``agen.aclose()`` when
    the collector reclaims an async generator nobody closed.
    """

    coro = task.get_coro()
    if type(coro).__name__ != "async_generator_athrow":
        return None
    for referent in gc.get_referents(coro):
        if inspect.isasyncgen(referent):
            return referent
    return None


def _excused_finalizer(task: asyncio.Task) -> bool:
    generator = finalized_generator(task)
    if generator is None:
        return False
    filename = Path(generator.ag_code.co_filename).parts
    return any(package in filename for package in THIRD_PARTY_FINALIZED_GENERATORS)


def pending_tasks(loop: asyncio.AbstractEventLoop) -> list[asyncio.Task]:
    if loop.is_closed():
        return []
    return [task for task in asyncio.all_tasks(loop) if not task.done() and not _excused_finalizer(task)]


# ── Plugin ──────────────────────────────────────────────

_BEFORE_THREADS = pytest.StashKey[list]()
_BEFORE_CHILDREN = pytest.StashKey[set]()
_BEFORE_SUBREAPER = pytest.StashKey[object]()
_TASK_LEAKS = pytest.StashKey[list]()
_CURRENT_ITEM: list[pytest.Item] = []


def _split(item: pytest.Item, check: str, descriptions: list[str]) -> tuple[list[str], list[str]]:
    failing, excused = [], []
    for description in descriptions:
        entry = _exception_for(check, item.nodeid, description)
        if entry is None:
            failing.append(description)
        else:
            excused.append(f"{description} (#{entry.issue}: {entry.reason})")
    return failing, excused


def pytest_configure(config) -> None:
    # pytest-asyncio 1.3 asks ``asyncio.get_event_loop()`` for the "old" loop
    # each time it builds a runner. On a main thread whose loop was never set,
    # that call silently creates a loop nobody closes, which the collector
    # later reports as an unclosed loop and socket against an arbitrary test.
    # Marking the loop as explicitly unset makes that call raise instead (the
    # plugin handles it), so no implicit loop is ever made.
    asyncio.set_event_loop(None)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_setup(item: pytest.Item):
    item.stash[_BEFORE_THREADS] = list(threading.enumerate())
    item.stash[_BEFORE_CHILDREN] = child_pids()
    item.stash[_BEFORE_SUBREAPER] = child_subreaper()
    item.stash[_TASK_LEAKS] = []
    _CURRENT_ITEM[:] = [item]
    return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    result = yield
    # pytest-asyncio's ``_<scope>_scoped_runner`` owns the loop async tests and
    # fixtures run on. A finalizer added now runs before the runner closes the
    # loop and after every fixture that depends on the runner has finished.
    if fixturedef.argname.endswith("_scoped_runner") and isinstance(result, asyncio.Runner):
        runner = result

        def check_pending_tasks() -> None:
            item = _CURRENT_ITEM[0] if _CURRENT_ITEM else None
            try:
                loop = runner.get_loop()
            except RuntimeError:  # the runner was never started
                return
            leaked = pending_tasks(loop)
            if leaked and item is not None:
                item.stash.setdefault(_TASK_LEAKS, []).extend(describe_task(task) for task in leaked)

        fixturedef.addfinalizer(check_pending_tasks)
    return result


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem):
    try:
        return (yield)
    finally:
        _CURRENT_ITEM.clear()
        _check_after_teardown(item)


def _check_after_teardown(item: pytest.Item) -> None:
    problems: list[str] = []
    notes: list[str] = []

    tasks = item.stash.get(_TASK_LEAKS, [])
    failing, excused = _split(item, "tasks", tasks)
    notes += excused
    if failing:
        problems.append(
            "asyncio task(s) still pending when the test's event loop closed (await or cancel them, "
            "or spawn through src.utils.background.track_task): " + "; ".join(failing))

    before_threads = item.stash.get(_BEFORE_THREADS, [])
    threads = leaked_threads(before_threads)
    failing, excused = _split(item, "threads", [describe_thread(thread) for thread in threads])
    notes += excused
    if failing:
        problems.append("thread(s) started by the test still running after teardown (join them): "
                        + "; ".join(failing))

    before_children = item.stash.get(_BEFORE_CHILDREN, set())
    new_children = sorted(child_pids() - before_children - process_lifetime_children())
    descriptions = {pid: describe_child(pid) for pid in new_children}
    failing, excused = _split(item, "children", list(descriptions.values()))
    notes += excused
    if new_children:
        cleanup = {pid: kill_and_reap(pid) for pid in new_children}
        failing = [f"{text}: {cleanup[pid]}" for pid, text in descriptions.items() if text in failing]
    if failing:
        problems.append("child process(es) left running or unreaped by the test (own them with the "
                        "`spawned` fixture or reap them): " + "; ".join(failing))

    # FakeClock timers are process-wide (#1225): a timer still pending once the
    # test's loop has closed belongs to work nothing will ever run.
    clock_support = sys.modules.get("tests.support.clock")
    for fake_clock in list(getattr(clock_support, "installed_clocks", ())):
        pending_deadlines = getattr(fake_clock, "pending_deadlines", None)
        if callable(pending_deadlines) and pending_deadlines():
            problems.append(f"fake clock timer(s) still pending at teardown, due in {pending_deadlines()} fake "
                            "seconds: advance the clock to them or cancel what armed them")
    if clock_support is not None and hasattr(clock_support, "installed_clocks"):
        clock_support.installed_clocks.clear()

    before_subreaper = item.stash.get(_BEFORE_SUBREAPER, None)
    after_subreaper = child_subreaper()
    if before_subreaper is not None and after_subreaper != before_subreaper:
        set_child_subreaper(int(before_subreaper))
        problems.append(f"the test left this process's child-subreaper flag at {after_subreaper} (was "
                        f"{before_subreaper}; now restored): orphans of later tests would become unreaped zombies")

    for note in notes:
        item.add_report_section("teardown", "tracked leak exception", note)
    if problems:
        pytest.fail(f"{item.nodeid} leaked resources (#1223):\n- " + "\n- ".join(problems), pytrace=False)


__all__ = [
    "LEAK_EXCEPTIONS",
    "LeakException",
    "PROCESS_LIFETIME_THREADS",
    "ProcessLifetimeThread",
    "child_pids",
    "kill_and_reap",
    "leaked_threads",
]
