"""Test ownership of direct child processes (#1217 slice 7, #1224).

``spawned`` owns every ``subprocess.Popen`` a test creates through it. At
teardown it kills any child still running, reaps every child with
``Popen.wait()`` and closes the pipes, so no zombie or open descriptor is left
for ``Popen.__del__`` and garbage collection to settle later.

``ReadyPipe`` replaces marker-file polling for direct children: the child
writes one byte to an inherited descriptor when it is ready and the test blocks
on the read. The read has a generous liveness watchdog that fails with a
diagnostic; it is never the synchronisation itself.

Import the fixture into a test module with ``from tests.support.processes
import spawned``.
"""
from __future__ import annotations

import os
import select
import subprocess
from typing import Any, Iterator, Sequence

import pytest

from src.execution.process_reap import ReapOutcome, kill_then_reap

# Bound at import so a test that replaces ``subprocess.Popen`` for code under
# test still gets a real owned child from ``spawned``.
_POPEN = subprocess.Popen

# Liveness watchdog only: a healthy child signals in milliseconds.
READY_WATCHDOG_SECONDS = 30.0


class ReadyPipe:
    """A one-byte readiness channel from a direct child to the test."""

    def __init__(self) -> None:
        self._read_fd, self._write_fd = os.pipe()
        os.set_inheritable(self._write_fd, True)

    @property
    def child_fd(self) -> int:
        """The descriptor number the child inherits and writes one byte to."""

        if self._write_fd < 0:
            raise RuntimeError("the child end was already handed over")
        return self._write_fd

    def release_child_end(self) -> None:
        """Close the parent's copy of the child end once the child holds it."""

        if self._write_fd >= 0:
            os.close(self._write_fd)
            self._write_fd = -1

    def wait(self, *, watchdog_seconds: float = READY_WATCHDOG_SECONDS) -> bytes:
        """Block until the child writes its readiness byte."""

        if self._write_fd >= 0:
            raise RuntimeError("release_child_end() before waiting, or EOF can never arrive")
        readable, _, _ = select.select([self._read_fd], [], [], watchdog_seconds)
        if not readable:
            raise AssertionError(f"child did not signal readiness within the {watchdog_seconds:g} s liveness watchdog")
        data = os.read(self._read_fd, 1)
        if not data:
            raise AssertionError("child closed its readiness descriptor without signalling ready")
        return data

    def close(self) -> None:
        self.release_child_end()
        if self._read_fd >= 0:
            os.close(self._read_fd)
            self._read_fd = -1


def _send(process: subprocess.Popen):
    def send(signum: int) -> bool:
        try:
            process.send_signal(signum)
        except ProcessLookupError:
            return False
        return True
    return send


class SpawnedProcesses:
    """Owner of every direct child a test starts through it."""

    def __init__(self) -> None:
        self.processes: list[subprocess.Popen] = []
        self._pipes: list[ReadyPipe] = []

    def ready_pipe(self) -> ReadyPipe:
        pipe = ReadyPipe()
        self._pipes.append(pipe)
        return pipe

    def popen(self, args: Sequence[str], *, ready: ReadyPipe | None = None, **kwargs: Any) -> subprocess.Popen:
        """Start and own a child; with ``ready`` the child inherits its write end."""

        if ready is not None:
            kwargs["pass_fds"] = tuple(kwargs.get("pass_fds", ())) + (ready.child_fd,)
        try:
            process = _POPEN(args, **kwargs)
        finally:
            if ready is not None:
                ready.release_child_end()
        self.processes.append(process)
        return process

    def adopt(self, process: subprocess.Popen) -> subprocess.Popen:
        """Own a child that production code started on the test's behalf."""

        self.processes.append(process)
        return process

    def reap_all(self) -> list[ReapOutcome]:
        """Kill any child still running, reap every child and close its pipes."""

        outcomes = []
        for process in self.processes:
            outcomes.append(kill_then_reap(process, send_signal=_send(process)))
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        self.processes.clear()
        for pipe in self._pipes:
            pipe.close()
        self._pipes.clear()
        unreaped = [outcome for outcome in outcomes if not outcome.reaped]
        if unreaped:
            raise AssertionError(f"spawned children left unreaped at teardown: {unreaped}")
        return outcomes


@pytest.fixture
def spawned() -> Iterator[SpawnedProcesses]:
    owner = SpawnedProcesses()
    try:
        yield owner
    finally:
        owner.reap_all()


__all__ = ["ReadyPipe", "SpawnedProcesses", "spawned"]
