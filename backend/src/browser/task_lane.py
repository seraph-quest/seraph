"""Small cross-process lane lock for the public browser capability.

The browser lane is a resource guard, not an authority source or a queue.  The
dispatcher acquires it before claiming a Ready board row and releases it only
after the runner has closed its browser context. The canonical workspace root
identity is checked around acquisition so a stale process cannot hold a lock
for a replaced workspace generation.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path
import stat
import uuid
from typing import Any

from src.workspace import canonical_workspace_root, canonical_workspace_root_identity


BROWSER_TASK_LANE_LOCK_NAME = ".browser-task-lane.lock"
_QUARANTINED_LANES: dict[str, "BrowserTaskLane"] = {}


class BrowserTaskLaneError(RuntimeError):
    """Base error for a browser lane that cannot be acquired safely."""


class BrowserTaskLaneBusy(BrowserTaskLaneError):
    """Another process currently owns the single browser lane."""


class BrowserTaskLane:
    """Non-blocking, process-safe lease for the one browser execution lane."""

    def __init__(self, workspace_root: str | os.PathLike[str]) -> None:
        self.workspace_root = canonical_workspace_root(workspace_root)
        self.lock_path = self.workspace_root / BROWSER_TASK_LANE_LOCK_NAME
        self._descriptor: int | None = None
        self._identity: dict[str, int | str] | None = None
        self._quarantine_job_id: str | None = None
        self._cleanup_witness: dict[str, Any] | None = None
        self._cleanup_confirmed = False

    @property
    def acquired(self) -> bool:
        return self._descriptor is not None

    def acquire(self) -> "BrowserTaskLane":
        if self._descriptor is not None:
            return self
        initial_identity = canonical_workspace_root_identity(self.workspace_root)
        try:
            open_flags = (
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(
                self.lock_path,
                open_flags,
                0o600,
            )
        except OSError as exc:
            raise BrowserTaskLaneError("browser lane lock is unavailable") from exc
        try:
            mode = os.fstat(descriptor).st_mode
            if not stat.S_ISREG(mode):
                raise BrowserTaskLaneError("browser lane lock is not a regular file")
            # Keep the permission change tied to the descriptor we opened.
            # A pathname chmod could affect a replacement made after open.
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise BrowserTaskLaneBusy("browser execution lane is busy") from exc
                raise BrowserTaskLaneError("browser lane lock could not be acquired") from exc
            # O_NOFOLLOW protects the final component at open time, but the
            # directory entry can still be replaced while the descriptor is
            # held.  Compare the no-follow pathname identity after acquiring
            # the lock so we never publish ownership for an orphaned inode.
            opened = os.fstat(descriptor)
            try:
                named = os.stat(self.lock_path, follow_symlinks=False)
            except OSError as exc:
                raise BrowserTaskLaneError("browser lane lock path changed") from exc
            if (
                not stat.S_ISREG(named.st_mode)
                or named.st_dev != opened.st_dev
                or named.st_ino != opened.st_ino
            ):
                raise BrowserTaskLaneError("browser lane lock path identity changed")
            current_identity = canonical_workspace_root_identity(self.workspace_root)
            if (
                current_identity["device"] != initial_identity["device"]
                or current_identity["inode"] != initial_identity["inode"]
                or current_identity["path_digest"] != initial_identity["path_digest"]
            ):
                raise BrowserTaskLaneError("browser workspace root identity changed")
            os.lseek(descriptor, 0, os.SEEK_SET)
            raw_marker = os.read(descriptor, 4097)
            if len(raw_marker) > 4096:
                raise BrowserTaskLaneError("browser lane marker exceeds bounds")
            if raw_marker:
                try:
                    previous = json.loads(raw_marker)
                except (ValueError, TypeError):
                    raise BrowserTaskLaneError("browser lane marker is malformed") from None
                if not isinstance(previous, dict):
                    raise BrowserTaskLaneError("browser lane marker is malformed")
                if previous.get("positive_cleanup_required") is True:
                    raise BrowserTaskLaneBusy("browser_cleanup_required")
            self._descriptor = descriptor
            self._identity = current_identity
            self._write_owner_marker(current_identity)
            return self
        except Exception:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(descriptor)
            raise

    def try_acquire(self) -> bool:
        if str(self.workspace_root) in _QUARANTINED_LANES:
            return False
        try:
            self.acquire()
        except BrowserTaskLaneBusy:
            return False
        return True

    @property
    def quarantined(self) -> bool:
        return self._quarantine_job_id is not None

    def quarantine(self, job_id: str) -> None:
        """Retain this descriptor until the owning process exits.

        An unverified browser teardown cannot safely release the physical
        one-context guard. The quarantine is deliberately process-local and
        has no authority or reset API; managed owner-process death closes the
        descriptor naturally.
        """

        if self._descriptor is None:
            return
        self._quarantine_job_id = str(job_id)
        if self._cleanup_witness is not None and self._cleanup_confirmed:
            self._cleanup_confirmed = False
            self._write_marker(self._cleanup_witness)
        _QUARANTINED_LANES[str(self.workspace_root)] = self

    def require_positive_cleanup(self, job_id: str) -> dict[str, Any]:
        """Persist v2's resource witness before any context may be launched.

        This remains in the existing physical lane inode. All acquirers honor
        it after owner death; an unlocked fd is never positive context closure.
        The held lock serializes marker writes, and fsync precedes launch.
        """
        import hashlib
        if self._descriptor is None or self._identity is None or self._cleanup_witness is not None:
            raise BrowserTaskLaneError("browser cleanup witness cannot be replaced")
        self._cleanup_witness = {"schema_version": 2, "pid": os.getpid(),
            "process_nonce": uuid.uuid4().hex, "context_nonce": uuid.uuid4().hex,
            "job_digest": hashlib.sha256(job_id.encode()).hexdigest(),
            "root_path_digest": self._identity["path_digest"],
            "root_device": self._identity["device"], "root_inode": self._identity["inode"],
            "positive_cleanup_required": True}
        self._write_marker(self._cleanup_witness)
        return dict(self._cleanup_witness)

    def confirm_positive_cleanup(self, witness: dict[str, Any]) -> None:
        """Accept only this original in-process owned context's closure receipt."""
        if (self._descriptor is None or self._cleanup_witness is None
            or witness != self._cleanup_witness or witness["pid"] != os.getpid()):
            raise BrowserTaskLaneError("browser original cleanup witness changed")
        current = canonical_workspace_root_identity(self.workspace_root)
        if current != self._identity:
            raise BrowserTaskLaneError("browser cleanup workspace identity changed")
        named = os.stat(self.lock_path, follow_symlinks=False)
        held = os.fstat(self._descriptor)
        if (named.st_dev, named.st_ino) != (held.st_dev, held.st_ino):
            raise BrowserTaskLaneError("browser cleanup lane identity changed")
        self._write_marker({**witness, "positive_cleanup_required": False, "cleanup": "positively_closed"})
        self._cleanup_confirmed = True

    def _write_marker(self, marker):
        if self._descriptor is None:
            raise BrowserTaskLaneError("browser lane descriptor is absent")
        encoded = json.dumps(marker, sort_keys=True, separators=(",", ":")).encode("ascii")
        os.ftruncate(self._descriptor, 0)
        os.lseek(self._descriptor, 0, os.SEEK_SET)
        if os.write(self._descriptor, encoded) != len(encoded):
            raise BrowserTaskLaneError("browser lane marker write is incomplete")
        os.fsync(self._descriptor)

    def _write_owner_marker(self, identity: dict[str, int | str]) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        marker: dict[str, Any] = {
            "schema_version": 1,
            "pid": os.getpid(),
            "root_path_digest": identity["path_digest"],
        }
        encoded = json.dumps(marker, sort_keys=True, separators=(",", ":")).encode("ascii")
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, encoded)
        os.fsync(descriptor)

    def release(self) -> None:
        if self._quarantined:
            return
        if self._cleanup_witness is not None and not self._cleanup_confirmed:
            self.quarantine(self._cleanup_witness["job_digest"])
            return
        descriptor, self._descriptor = self._descriptor, None
        self._identity = None
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    @property
    def _quarantined(self) -> bool:
        return self._quarantine_job_id is not None

    def __enter__(self) -> "BrowserTaskLane":
        return self.acquire()

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


def try_acquire_browser_task_lane(workspace_root: str | os.PathLike[str]) -> BrowserTaskLane | None:
    """Return an acquired lane or ``None`` when another process owns it."""

    lane = BrowserTaskLane(workspace_root)
    if lane.try_acquire():
        return lane
    return None


def browser_task_lane_wait_reason(workspace_root: str | os.PathLike[str]) -> str | None:
    """Return the bounded operator reason for a process-local quarantine.

    A normal cross-process lane owner is deliberately indistinguishable here:
    it is ordinary resource contention and has no durable, owner-safe reason
    to publish.  A quarantined lane is retained by this process and therefore
    can be projected read-only to the authenticated task owner.  The helper
    never exposes the held job identity and performs no writes.
    """

    try:
        root = str(canonical_workspace_root(workspace_root))
    except (OSError, TypeError, ValueError):
        return None
    lane = _QUARANTINED_LANES.get(root)
    if lane is not None and lane.quarantined:
        return "browser_cleanup_required"
    descriptor = None
    try:
        path = Path(root) / BROWSER_TASK_LANE_LOCK_NAME
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        opened = os.fstat(descriptor)
        named = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            return None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return None
        raw = os.read(descriptor, 4097)
        marker = json.loads(raw) if raw and len(raw) <= 4096 else {}
        return "browser_cleanup_required" if isinstance(marker, dict) and marker.get("positive_cleanup_required") is True else None
    except (OSError, ValueError, TypeError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


__all__ = [
    "BROWSER_TASK_LANE_LOCK_NAME",
    "BrowserTaskLane",
    "BrowserTaskLaneBusy",
    "BrowserTaskLaneError",
    "browser_task_lane_wait_reason",
    "try_acquire_browser_task_lane",
]
