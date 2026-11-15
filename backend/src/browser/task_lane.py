"""Small cross-process lane lock for the public browser capability.

The browser lane is a resource guard, not an authority source or a queue.  The
dispatcher acquires it before claiming a Ready board row and releases it only
after the runner has closed its browser context. The canonical workspace root
identity is checked around acquisition so a stale process cannot hold a lock
for a replaced workspace generation.
"""

from __future__ import annotations

import errno
import ctypes
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
import uuid
from typing import Any

from src.workspace import canonical_workspace_root, canonical_workspace_root_identity


BROWSER_TASK_LANE_LOCK_NAME = ".browser-task-lane.lock"
_QUARANTINED_LANES: dict[str, "BrowserTaskLane"] = {}


def linux_boot_session_id() -> str | None:
    """Read the kernel's actual Linux boot UUID; no portable approximation."""
    if sys.platform != "linux":
        return None
    try:
        with Path("/proc/sys/kernel/random/boot_id").open(encoding="ascii") as source:
            value = source.read(80).strip()
        parsed = uuid.UUID(value)
        return value if str(parsed) == value and parsed.int != 0 else None
    except (OSError, UnicodeError, ValueError):
        return None


def darwin_boot_session_id() -> str | None:
    """Read Apple's fixed read-only kernel boot UUID, with a bounded buffer."""
    if sys.platform != "darwin":
        return None
    try:
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        query = library.sysctlbyname
        query.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        query.restype = ctypes.c_int
        buffer, size = ctypes.create_string_buffer(37), ctypes.c_size_t(37)
        if query(b"kern.bootsessionuuid", buffer, ctypes.byref(size), None, 0) != 0 or size.value != 37:
            return None
        value = buffer.value.decode("ascii")
        parsed = uuid.UUID(value)
        return value if str(parsed) == value and parsed.int != 0 else None
    except (AttributeError, OSError, UnicodeError, ValueError):
        return None


def native_boot_session() -> tuple[str, str] | None:
    """Only verified native kernel sources are eligible for browser admission."""
    if sys.platform == "linux":
        value = linux_boot_session_id()
    elif sys.platform == "darwin":
        value = darwin_boot_session_id()
    else:
        return None
    try:
        parsed = uuid.UUID(value)
        return (sys.platform, value) if str(parsed) == value and parsed.int != 0 else None
    except (ValueError, TypeError, AttributeError):
        return None


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
        self._positive_proof: str | None = None
        self._marker_persisted = False

    @property
    def acquired(self) -> bool:
        return self._descriptor is not None

    def acquire(self, *, cleanup_job_id: str | None = None) -> "BrowserTaskLane":
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
                    if cleanup_job_id is None:
                        raise BrowserTaskLaneBusy("browser_cleanup_required")
                    import hashlib
                    expected = {"job_digest": hashlib.sha256(cleanup_job_id.encode()).hexdigest(),
                        "root_path_digest": current_identity["path_digest"],
                        "root_device": current_identity["device"], "root_inode": current_identity["inode"],
                        "lock_device": opened.st_dev, "lock_inode": opened.st_ino}
                    if previous.get("schema_version") != 4 or any(previous.get(k) != v for k, v in expected.items()):
                        raise BrowserTaskLaneError("browser exact cleanup witness mismatch")
                    self._descriptor, self._identity = descriptor, current_identity
                    self._cleanup_witness = self._marker_witness(previous)
                    self._marker_persisted = True
                    self._positive_proof = "owned_positive_close" if previous.get("cleanup") == "positively_closed" and previous.get("cleanup_proof") == "owned_positive_close" else None
                    return self
                if cleanup_job_id is not None:
                    raise BrowserTaskLaneError("browser exact cleanup witness is absent")
            elif cleanup_job_id is not None:
                raise BrowserTaskLaneError("browser exact cleanup witness is absent")
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
        _QUARANTINED_LANES[str(self.workspace_root)] = self

    def stage_prelaunch(self, job_id: str) -> dict[str, Any]:
        """Stage immutable provenance in memory under the original lane flock.

        The canonical reservation must commit before require_positive_cleanup
        writes and fsyncs this witness on the same inode. Staging cannot launch
        a child and creates no durable physical reservation by itself.
        """
        import hashlib
        if self._descriptor is None or self._identity is None or self._cleanup_witness is not None:
            raise BrowserTaskLaneError("browser cleanup witness cannot be replaced")
        boot = native_boot_session()
        if boot is None:
            raise BrowserTaskLaneError("browser_native_boot_proof_unavailable")
        held = os.fstat(self._descriptor)
        self._cleanup_witness = {"schema_version": 4, "pid": os.getpid(),
            "process_nonce": uuid.uuid4().hex, "context_nonce": uuid.uuid4().hex,
            "job_digest": hashlib.sha256(job_id.encode()).hexdigest(),
            "root_path_digest": self._identity["path_digest"],
            "root_device": self._identity["device"], "root_inode": self._identity["inode"],
            "lock_device": held.st_dev, "lock_inode": held.st_ino,
            "boot_platform": boot[0], "boot_session_id": boot[1],
            "positive_cleanup_required": True}
        return dict(self._cleanup_witness)

    def require_positive_cleanup(self, job_id: str) -> dict[str, Any]:
        import hashlib
        if self._cleanup_witness is None:
            self.stage_prelaunch(job_id)
        witness = self._cleanup_witness
        if witness["job_digest"] != hashlib.sha256(job_id.encode()).hexdigest():
            raise BrowserTaskLaneError("browser original cleanup witness changed")
        self._validate_original_witness(witness)
        self._write_marker(witness)
        self._marker_persisted = True
        return dict(witness)

    def retain_positive_cleanup(self, witness: dict[str, Any], *, proof: str = "owned_positive_close") -> None:
        """Persist physical closure while retaining capacity until audit commits."""
        self._validate_original_witness(witness)
        if proof not in {"owned_positive_close", "owned_no_child"}:
            raise BrowserTaskLaneError("browser original cleanup proof invalid")
        self._positive_proof = proof
        self._write_marker({**witness, "cleanup": "positively_closed", "cleanup_proof": self._positive_proof})
        self._marker_persisted = True

    def cleanup_proof(self, job_id: str) -> tuple[dict[str, Any], str]:
        """Only exact retained closure or a changed native boot proves cleanup."""
        import hashlib
        witness = self._cleanup_witness
        if self._descriptor is None or witness is None or witness.get("job_digest") != hashlib.sha256(job_id.encode()).hexdigest():
            raise BrowserTaskLaneError("browser exact cleanup witness mismatch")
        return self.cleanup_proof_from_witness(witness)

    def commit_cleanup_receipt(self, witness: dict[str, Any], proof: str) -> None:
        """Clear only after the caller persisted this exact cleanup-only receipt."""
        current, current_proof = self.cleanup_proof_from_witness(witness)
        if current != witness or current_proof != proof:
            raise BrowserTaskLaneError("browser cleanup proof changed before receipt commit")
        self._write_marker({**witness, "positive_cleanup_required": False,
            "cleanup": "positively_closed", "cleanup_proof": proof})
        self._cleanup_confirmed = True
        self._quarantine_job_id = None
        _QUARANTINED_LANES.pop(str(self.workspace_root), None)

    def clear_committed_cleanup_pointer(self, witness: dict[str, Any], proof: str) -> None:
        """Finish an exact already-journaled cleanup, never a later pointer.

        The canonical fixed helper must have verified its committed receipt
        before this local operation. Replaying that receipt needs no new boot
        proof; the same resource was already positively settled.
        """
        if proof not in {"owned_positive_close", "owned_no_child", "linux_boot_changed", "darwin_boot_changed"}:
            raise BrowserTaskLaneError("browser committed cleanup proof invalid")
        self._validate_inode_witness(witness)
        if witness != self._cleanup_witness:
            raise BrowserTaskLaneError("browser exact cleanup witness mismatch")
        self._write_marker({**witness, "positive_cleanup_required": False,
            "cleanup": "positively_closed", "cleanup_proof": proof})
        self._cleanup_confirmed = True
        self._quarantine_job_id = None
        _QUARANTINED_LANES.pop(str(self.workspace_root), None)

    def close_cleanup_observer(self) -> None:
        """Drop a recovery read lock without clearing its durable reservation."""
        if _QUARANTINED_LANES.get(str(self.workspace_root)) is self:
            return
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def cleanup_proof_from_witness(self, witness):
        self._validate_inode_witness(witness)
        if witness != self._cleanup_witness:
            raise BrowserTaskLaneError("browser original cleanup witness changed")
        if self._positive_proof in {"owned_positive_close", "owned_no_child"}:
            return dict(witness), self._positive_proof
        current = native_boot_session()
        original = witness.get("boot_session_id")
        if current is None or current[0] != witness.get("boot_platform") or original == current[1]:
            raise BrowserTaskLaneBusy("browser positive cleanup proof unavailable on this boot")
        try:
            if str(uuid.UUID(original)) != original or uuid.UUID(original).int == 0:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise BrowserTaskLaneError("browser original native boot proof is invalid") from None
        return dict(witness), current[0] + "_boot_changed"

    @staticmethod
    def _marker_witness(marker):
        witness = {k: v for k, v in marker.items() if k not in {"cleanup", "cleanup_proof"}}
        return witness

    def _validate_inode_witness(self, witness):
        if self._descriptor is None:
            raise BrowserTaskLaneError("browser cleanup descriptor absent")
        current = canonical_workspace_root_identity(self.workspace_root)
        named, held = os.stat(self.lock_path, follow_symlinks=False), os.fstat(self._descriptor)
        if current != self._identity or (named.st_dev, named.st_ino) != (held.st_dev, held.st_ino) or any(
            witness.get(k) != v for k, v in {"root_path_digest": current["path_digest"],
                "root_device": current["device"], "root_inode": current["inode"],
                "lock_device": held.st_dev, "lock_inode": held.st_ino}.items()):
            raise BrowserTaskLaneError("browser cleanup workspace/lock witness changed")
        if not self._marker_persisted:
            return
        os.lseek(self._descriptor, 0, os.SEEK_SET)
        raw = os.read(self._descriptor, 4097)
        try:
            marker = json.loads(raw) if len(raw) <= 4096 else None
        except (ValueError, TypeError):
            marker = None
        if not isinstance(marker, dict) or self._marker_witness(marker) != witness:
            raise BrowserTaskLaneError("browser exact cleanup marker changed")

    def _validate_original_witness(self, witness):
        if witness != self._cleanup_witness or witness.get("pid") != os.getpid():
            raise BrowserTaskLaneError("browser original cleanup witness changed")
        self._validate_inode_witness(witness)

    def confirm_positive_cleanup(self, witness: dict[str, Any]) -> None:
        """Accept only this original in-process owned context's closure receipt."""
        self._validate_original_witness(witness)
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


def acquire_browser_cleanup_lane(workspace_root, job_id) -> BrowserTaskLane:
    root = str(canonical_workspace_root(workspace_root))
    lane = _QUARANTINED_LANES.get(root)
    if lane is None:
        return BrowserTaskLane(root).acquire(cleanup_job_id=job_id)
    import hashlib
    if lane._cleanup_witness is None or lane._cleanup_witness.get("job_digest") != hashlib.sha256(job_id.encode()).hexdigest():
        raise BrowserTaskLaneError("browser exact cleanup witness mismatch")
    lane._validate_inode_witness(lane._cleanup_witness)
    return lane


def browser_cleanup_witness(workspace_root) -> dict[str, Any] | None:
    """Read only the exact pending resource pointer; no acquisition or clearing."""
    root = str(canonical_workspace_root(workspace_root))
    lane = _QUARANTINED_LANES.get(root)
    if lane is not None and lane._cleanup_witness is not None:
        lane._validate_inode_witness(lane._cleanup_witness)
        return dict(lane._cleanup_witness)
    path = Path(root) / BROWSER_TASK_LANE_LOCK_NAME
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        opened, named = os.fstat(descriptor), os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            return None
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        raw = os.read(descriptor, 4097)
        marker = json.loads(raw) if len(raw) <= 4096 else None
        identity = canonical_workspace_root_identity(root)
        if (not isinstance(marker, dict) or marker.get("schema_version") != 4 or marker.get("positive_cleanup_required") is not True
            or any(marker.get(k) != v for k, v in {"root_path_digest": identity["path_digest"],
                "root_device": identity["device"], "root_inode": identity["inode"],
                "lock_device": opened.st_dev, "lock_inode": opened.st_ino}.items())):
            return None
        return BrowserTaskLane._marker_witness(marker)
    except (OSError, ValueError, TypeError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


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
    "linux_boot_session_id",
    "browser_cleanup_witness",
    "acquire_browser_cleanup_lane",
    "try_acquire_browser_task_lane",
]
