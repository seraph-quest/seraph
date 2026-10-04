"""Single physical repository-repair execution lane.

The durable job reservation is the authority; this flock only closes the
cross-process race between two dispatchers before either starts a subprocess.
An unresolved cleanup keeps the descriptor quarantined and the durable
reservation held, so releasing a child process or restarting a dispatcher
cannot silently authorize a successor.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path
import stat
from typing import Any

from src.workspace import canonical_workspace_root, canonical_workspace_root_identity


REPO_REPAIR_EXECUTION_LANE_LOCK_NAME = ".repo-repair-execution.lock"
_QUARANTINED_LANES: dict[str, "RepoRepairCapacityLane"] = {}


class RepoRepairCapacityError(RuntimeError):
    """Base error for the governed repository execution lane."""


class RepoRepairCapacityBusy(RepoRepairCapacityError):
    """Another process owns the one physical repair execution lane."""


class RepoRepairCapacityLane:
    """Non-blocking cross-process lock with an operator-safe owner marker."""

    def __init__(self, workspace_root: str | os.PathLike[str]) -> None:
        self.workspace_root = canonical_workspace_root(workspace_root)
        self.lock_path = self.workspace_root / REPO_REPAIR_EXECUTION_LANE_LOCK_NAME
        self._descriptor: int | None = None
        self._identity: dict[str, int | str] | None = None
        self._lock_identity: tuple[int, int] | None = None
        self._quarantine_job_id: str | None = None

    @property
    def acquired(self) -> bool:
        return self._descriptor is not None

    @property
    def quarantined(self) -> bool:
        return self._quarantine_job_id is not None

    def acquire(self) -> "RepoRepairCapacityLane":
        if self._descriptor is not None:
            return self
        initial_identity = canonical_workspace_root_identity(self.workspace_root)
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise RepoRepairCapacityError("repository repair execution lane is unavailable") from exc
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_uid != os.getuid()
                or stat.S_IMODE(opened.st_mode) & 0o077
            ):
                raise RepoRepairCapacityError(
                    "repository repair lane lock is not a safe regular file"
                )
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise RepoRepairCapacityBusy("repository repair execution lane is busy") from exc
                raise RepoRepairCapacityError("repository repair lane lock could not be acquired") from exc
            try:
                named = os.stat(self.lock_path, follow_symlinks=False)
            except OSError as exc:
                raise RepoRepairCapacityError("repository repair lane lock path changed") from exc
            if (
                not stat.S_ISREG(named.st_mode)
                or named.st_nlink != 1
                or named.st_uid != os.getuid()
                or stat.S_IMODE(named.st_mode) & 0o077
                or named.st_dev != opened.st_dev
                or named.st_ino != opened.st_ino
            ):
                raise RepoRepairCapacityError("repository repair lane lock identity changed")
            current_identity = canonical_workspace_root_identity(self.workspace_root)
            if any(
                current_identity[key] != initial_identity[key]
                for key in ("device", "inode", "path_digest")
            ):
                raise RepoRepairCapacityError("repository workspace root identity changed")
            self._descriptor = descriptor
            self._identity = current_identity
            self._lock_identity = (opened.st_dev, opened.st_ino)
            self._write_marker({"root_path_digest": current_identity["path_digest"]})
            return self
        except Exception:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(descriptor)
            self._descriptor = None
            self._identity = None
            self._lock_identity = None
            raise

    def try_acquire(self) -> bool:
        if str(self.workspace_root) in _QUARANTINED_LANES:
            return False
        try:
            self.acquire()
        except RepoRepairCapacityBusy:
            return False
        return True

    def bind_owner(
        self,
        *,
        job_id: str,
        attempt_id: str,
        fence_token: int,
        authority_digest: str,
    ) -> None:
        """Write the exact durable identity only after reservation succeeds."""

        if self._descriptor is None or self._identity is None:
            raise RepoRepairCapacityError("repository repair lane is not acquired")
        self._write_marker(
            {
                "root_path_digest": self._identity["path_digest"],
                "job_id": str(job_id)[:512],
                "attempt_id": str(attempt_id)[:512],
                "fence_token": int(fence_token),
                "authority_digest": str(authority_digest)[:128],
            }
        )

    def quarantine(self, job_id: str) -> None:
        """Retain the descriptor when child cleanup/readback is uncertain."""

        if self._descriptor is None:
            return
        self._quarantine_job_id = str(job_id)
        _QUARANTINED_LANES[str(self.workspace_root)] = self

    def clear_quarantine(self) -> None:
        """Release only after durable cleanup/readback reconciliation."""

        if not self.quarantined:
            # Normal terminal publication settles a lane that was never
            # quarantined.  Keep this method idempotent for both the normal
            # and same-job recovery paths.
            self.release()
            return
        _QUARANTINED_LANES.pop(str(self.workspace_root), None)
        self._quarantine_job_id = None
        self.release()

    def release_after_denied(self) -> None:
        """Drop a descriptor when durable admission proved no reservation.

        This is narrower than ``clear_quarantine``: callers may use it only
        for a deterministic pre-commit denial (for example a settled exact
        job).  Unknown failures must keep using ``quarantine`` so a successor
        cannot run while the durable row's ownership is unresolved.
        """

        if self._quarantine_job_id is not None:
            current = _QUARANTINED_LANES.get(str(self.workspace_root))
            if current is self:
                _QUARANTINED_LANES.pop(str(self.workspace_root), None)
            self._quarantine_job_id = None
        self.release()

    def _write_marker(self, fields: dict[str, Any]) -> None:
        if self._descriptor is None:
            return
        try:
            opened = os.fstat(self._descriptor)
            named = os.stat(self.lock_path, follow_symlinks=False)
        except OSError as exc:
            raise RepoRepairCapacityError("repository repair lane lock disappeared") from exc
        if (
            self._lock_identity is None
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) & 0o077
            or not stat.S_ISREG(named.st_mode)
            or named.st_nlink != 1
            or named.st_uid != os.getuid()
            or stat.S_IMODE(named.st_mode) & 0o077
            or (opened.st_dev, opened.st_ino) != self._lock_identity
            or (named.st_dev, named.st_ino) != self._lock_identity
        ):
            raise RepoRepairCapacityError("repository repair lane lock identity changed")
        marker = {
            "schema_version": 1,
            "pid": os.getpid(),
            **fields,
        }
        encoded = json.dumps(marker, sort_keys=True, separators=(",", ":")).encode("ascii")
        os.ftruncate(self._descriptor, 0)
        os.lseek(self._descriptor, 0, os.SEEK_SET)
        os.write(self._descriptor, encoded)
        os.fsync(self._descriptor)

    def release(self) -> None:
        if self.quarantined:
            return
        descriptor, self._descriptor = self._descriptor, None
        self._identity = None
        self._lock_identity = None
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "RepoRepairCapacityLane":
        return self.acquire()

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


def try_acquire_repo_repair_capacity(
    workspace_root: str | os.PathLike[str],
    *,
    job_id: str | None = None,
) -> RepoRepairCapacityLane | None:
    lane = RepoRepairCapacityLane(workspace_root)
    quarantined = _QUARANTINED_LANES.get(str(lane.workspace_root))
    if quarantined is not None:
        # A same-job recovery may continue using its already-held descriptor;
        # a different job must remain blocked until durable reconciliation
        # clears both the reservation and this process-local quarantine.
        if job_id and quarantined._quarantine_job_id == str(job_id):
            return quarantined
        return None
    if lane.try_acquire():
        return lane
    return None


def clear_exact_repo_repair_quarantine(workspace_root, *, job_id: str, attempt_id: str, fencing_token: int, authority_digest: str) -> None:
    """Clear the exact process-local descriptor only after durable settlement."""
    root = str(canonical_workspace_root(workspace_root))
    lane = _QUARANTINED_LANES.get(root)
    if lane is None:
        return
    if lane._quarantine_job_id != job_id or lane._descriptor is None:
        raise RepoRepairCapacityError("repository cleanup quarantine owner changed")
    marker = json.loads(os.pread(lane._descriptor, 4096, 0))
    if any(marker.get(key) != value for key,value in {"job_id":job_id,"attempt_id":attempt_id,
            "fence_token":fencing_token,"authority_digest":authority_digest}.items()):
        raise RepoRepairCapacityError("repository cleanup quarantine binding changed")
    lane.clear_quarantine()


def repo_repair_capacity_wait_reason(workspace_root: str | os.PathLike[str]) -> str | None:
    try:
        root = str(canonical_workspace_root(workspace_root))
    except (OSError, TypeError, ValueError):
        return None
    lane = _QUARANTINED_LANES.get(root)
    return "repo_repair_cleanup_required" if lane is not None and lane.quarantined else None


__all__ = [
    "REPO_REPAIR_EXECUTION_LANE_LOCK_NAME",
    "RepoRepairCapacityBusy",
    "RepoRepairCapacityError",
    "RepoRepairCapacityLane",
    "repo_repair_capacity_wait_reason",
    "try_acquire_repo_repair_capacity",
]
