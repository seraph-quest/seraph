"""Owner-side reap of a signalled direct child process (#1212).

An owner that escalates SIGTERM to SIGKILL against a direct
``subprocess.Popen`` child also collects its exit status. Nothing is left for
``Popen.__del__`` or garbage collection to reap, so whether a zombie exists
after the owner returns no longer depends on collector timing.

The signal itself is sent by the caller's ``send_signal`` callable, which
returns whether it was delivered. The repository supervisors send through
``repo_supervisor.exact_signal`` so a PID is only signalled while its recorded
start identity still matches. A sender that raises ``OSError`` (for example
EPERM or a pidfd error) counts as not delivered; the error is kept on the
outcome.

What is bounded:

* After SIGTERM, the wait is limited by the caller's existing grace period.
* If SIGKILL was not delivered, the owner does not wait at all.
* After a delivered SIGKILL, the kernel normally makes the child reapable at
  once. A child stuck in uninterruptible kernel sleep (a hung NFS or FUSE
  mount, a frozen cgroup) is not reapable until that sleep ends, which may be
  never. The owner therefore waits at most ``KILL_REAP_LIVENESS_SECONDS`` and
  then reports ``kill_delivered_unreaped``: the child stays owned by its
  ``Popen`` handle and the caller's cleanup stays unproven. This is a liveness
  bound on cleanup so the owner itself cannot hang; it gates no feature and
  does not change any job deadline.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import signal
import subprocess
from typing import Any, Callable, Literal

# Liveness bound on collecting a child after a delivered SIGKILL (see module
# docstring). A killed child is normally reaped within milliseconds; this only
# stops an unkillable (uninterruptible-sleep) child from hanging its owner.
KILL_REAP_LIVENESS_SECONDS = 30.0

ReapStatus = Literal[
    "already_exited",
    "exited_after_terminate",
    "exited_before_kill",
    "killed_and_reaped",
    "kill_delivered_unreaped",
    "kill_not_delivered",
    "reap_failed",
]

SignalSender = Callable[[int], bool]


@dataclass(frozen=True)
class ReapOutcome:
    """Typed result of an owner's terminate/kill/reap escalation."""

    pid: int
    status: ReapStatus
    returncode: int | None
    terminate_delivered: bool | None = None
    kill_delivered: bool | None = None
    signal_errors: tuple[str, ...] = ()

    @property
    def reaped(self) -> bool:
        """True when the owner collected the child's exit status."""

        return self.returncode is not None

    def as_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["signal_errors"] = list(self.signal_errors)
        return {**record, "reaped": self.reaped}


class ProcessReapError(RuntimeError):
    """The owner signalled a child but could not collect its exit status."""

    def __init__(self, message: str, *, outcome: ReapOutcome) -> None:
        super().__init__(message)
        self.outcome = outcome


def _deliver(send_signal: SignalSender, signum: int, errors: list[str]) -> bool:
    """Send one signal; a raised OSError counts as not delivered and is kept."""

    try:
        return bool(send_signal(signum))
    except OSError as exc:
        errors.append(f"{signal.Signals(signum).name}: {exc!r}")
        return False


def kill_then_reap(
    process: subprocess.Popen,
    *,
    send_signal: SignalSender,
    terminate_delivered: bool | None = None,
    _errors: list[str] | None = None,
) -> ReapOutcome:
    """Send SIGKILL to an unreaped direct child and wait (bounded) for its exit status."""

    errors = _errors if _errors is not None else []
    returncode = process.poll()
    if returncode is not None:
        return ReapOutcome(process.pid, "exited_before_kill", returncode, terminate_delivered,
                           signal_errors=tuple(errors))
    killed = _deliver(send_signal, signal.SIGKILL, errors)
    if not killed:
        # Without a delivered SIGKILL the child may still run; never block.
        returncode = process.poll()
        status: ReapStatus = "exited_before_kill" if returncode is not None else "kill_not_delivered"
        return ReapOutcome(process.pid, status, returncode, terminate_delivered, False, tuple(errors))
    try:
        returncode = process.wait(timeout=KILL_REAP_LIVENESS_SECONDS)
    except subprocess.TimeoutExpired:
        # Not reapable yet (uninterruptible sleep); cleanup stays unproven.
        return ReapOutcome(process.pid, "kill_delivered_unreaped", None, terminate_delivered, True, tuple(errors))
    except OSError as exc:
        outcome = ReapOutcome(process.pid, "reap_failed", process.returncode, terminate_delivered, True,
                              tuple(errors))
        raise ProcessReapError(f"killed child {process.pid} could not be reaped: {exc}", outcome=outcome) from exc
    return ReapOutcome(process.pid, "killed_and_reaped", returncode, terminate_delivered, True, tuple(errors))


def terminate_then_reap(
    process: subprocess.Popen,
    *,
    send_signal: SignalSender,
    grace_seconds: float,
) -> ReapOutcome:
    """SIGTERM, wait up to the caller's grace, then SIGKILL and reap (bounded)."""

    returncode = process.poll()
    if returncode is not None:
        return ReapOutcome(process.pid, "already_exited", returncode)
    errors: list[str] = []
    terminated = _deliver(send_signal, signal.SIGTERM, errors)
    try:
        returncode = process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        return kill_then_reap(process, send_signal=send_signal, terminate_delivered=terminated, _errors=errors)
    return ReapOutcome(process.pid, "exited_after_terminate", returncode, terminated,
                       signal_errors=tuple(errors))
