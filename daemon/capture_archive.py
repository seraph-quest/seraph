"""Bounded age and size pruning of the daemon's local capture archive (#1263).

The daemon is the only writer of its archive directory, so it is the only
process that prunes it. Pure filesystem code: importable on any OS, no PyObjC.

Only files Seraph names are candidates: inside a ``YYYY-MM-DD`` directory
directly under the archive root, ``<HHMMSS>-<app slug>-<16 hex>`` followed by
``.png``, ``.analysis.json`` or ``.<provider slug>.json`` (``.codex.txt`` is
the legacy sidecar). Files sharing a day and stem are one capture and are
deleted together, so no sidecar is orphaned. Anything else is neither counted
nor deleted. Symlinks are never followed and never deleted.

Every failure deletes less, never more. ``now`` is always passed in.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

MIB = 1024 * 1024
DAY_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ARCHIVE_FILE_RE = re.compile(
    r"^(?P<stem>\d{6}-[a-z0-9_-]{1,40}-[0-9a-f]{16})\.(?:png|analysis\.json|[a-z0-9_-]{1,40}\.(?:json|txt))$"
)
MAX_SCAN_ENTRIES = 200_000
MAX_DELETE_CAPTURES = 5_000


@dataclass(frozen=True)
class ArchivePruneResult:
    status: str  # ok | skipped | error
    reason: str | None = None
    scanned_files: int = 0
    deleted_captures: int = 0
    deleted_files: int = 0
    freed_bytes: int = 0
    remaining_bytes: int = 0
    over_cap: bool = False

    def as_status(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason": self.reason,
            "scanned_files": self.scanned_files,
            "deleted_captures": self.deleted_captures,
            "deleted_files": self.deleted_files,
            "freed_bytes": self.freed_bytes,
            "remaining_bytes": self.remaining_bytes,
            "over_cap": self.over_cap,
        }


@dataclass
class _Capture:
    key: str
    mtime_ns: int
    files: list[tuple[Path, int]] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(size for _path, size in self.files)


def _unsafe_root_reason(root: Path) -> str | None:
    if root == Path(root.anchor) or root == Path.home().resolve():
        return "archive_root_refused"
    try:
        info = root.lstat()
    except FileNotFoundError:
        return "archive_missing"
    except OSError:
        return "archive_unreadable"
    if not stat.S_ISDIR(info.st_mode):
        return "archive_not_directory"
    if info.st_uid != os.getuid():
        return "archive_foreign_owner"
    return None


def _scan(root: Path, *, max_scan: int) -> tuple[list[_Capture], int, bool]:
    captures: dict[str, _Capture] = {}
    scanned = 0
    with os.scandir(root) as iterator:
        days = sorted(iterator, key=lambda item: item.name)
    for day in days:
        scanned += 1
        if scanned > max_scan:
            return list(captures.values()), scanned - 1, True
        if not DAY_DIR_RE.fullmatch(day.name) or not day.is_dir(follow_symlinks=False):
            continue
        try:
            with os.scandir(day.path) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError:
            continue
        for entry in entries:
            scanned += 1
            if scanned > max_scan:
                return list(captures.values()), scanned - 1, True
            match = ARCHIVE_FILE_RE.fullmatch(entry.name)
            if match is None:
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            key = f"{day.name}/{match.group('stem')}"
            capture = captures.get(key)
            if capture is None:
                capture = captures[key] = _Capture(key=key, mtime_ns=int(info.st_mtime_ns))
            capture.mtime_ns = min(capture.mtime_ns, int(info.st_mtime_ns))
            capture.files.append((Path(entry.path), int(info.st_size)))
    return list(captures.values()), scanned, False


def _delete_capture(capture: _Capture) -> tuple[int, int]:
    """Delete every regular file of one capture; returns (files, bytes) removed.

    Files that could not be removed stay in ``capture.files`` and keep counting.
    """
    removed = 0
    freed = 0
    kept: list[tuple[Path, int]] = []
    for path, size in capture.files:
        gone = False
        try:
            info = path.lstat()
        except FileNotFoundError:
            gone = True
        except OSError:
            info = None
        else:
            if stat.S_ISREG(info.st_mode):
                try:
                    os.unlink(path)
                    gone = True
                except FileNotFoundError:
                    gone = True
                except OSError:
                    gone = False
        if gone:
            removed += 1
            freed += size
        else:
            kept.append((path, size))
    capture.files = kept
    return removed, freed


def prune_capture_archive(
    archive_dir: str,
    *,
    max_bytes: int,
    retention_days: int | None,
    now: datetime,
    max_scan: int = MAX_SCAN_ENTRIES,
    max_delete: int = MAX_DELETE_CAPTURES,
) -> ArchivePruneResult:
    """Prune whole captures oldest first: age pass, then size pass. Never raises.

    ``max_bytes`` of ``0`` keeps no size cap; ``retention_days`` of ``None``
    skips the age pass.
    """
    try:
        root = Path(archive_dir).expanduser().resolve()
        reason = _unsafe_root_reason(root)
        if reason is not None:
            return ArchivePruneResult(status="skipped", reason=reason)
        if retention_days is None and max_bytes <= 0:
            return ArchivePruneResult(status="skipped", reason="no_limits")
        captures, scanned, truncated = _scan(root, max_scan=max_scan)
        captures.sort(key=lambda capture: (capture.mtime_ns, capture.key))

        deleted_captures = 0
        deleted_files = 0
        freed = 0

        def delete(capture: _Capture) -> int:
            nonlocal deleted_captures, deleted_files, freed
            removed, removed_bytes = _delete_capture(capture)
            if removed:
                deleted_captures += 1
                deleted_files += removed
                freed += removed_bytes
            return removed_bytes

        survivors: list[_Capture] = []
        cutoff_ns = (
            int((now - timedelta(days=max(1, retention_days))).timestamp() * 1_000_000_000)
            if retention_days is not None
            else None
        )
        for capture in captures:
            if cutoff_ns is not None and capture.mtime_ns < cutoff_ns and deleted_captures < max_delete:
                delete(capture)
            if capture.files:
                survivors.append(capture)  # an undeletable remainder still counts

        total = sum(capture.size for capture in survivors)
        if max_bytes > 0:
            for capture in survivors:
                if total <= max_bytes or deleted_captures >= max_delete:
                    break
                total -= delete(capture)

        for day in sorted(root.iterdir()):
            if DAY_DIR_RE.fullmatch(day.name) and day.is_dir() and not day.is_symlink():
                with contextlib.suppress(OSError):
                    day.rmdir()  # only succeeds when empty
        return ArchivePruneResult(
            status="ok",
            scanned_files=scanned,
            deleted_captures=deleted_captures,
            deleted_files=deleted_files,
            freed_bytes=freed,
            remaining_bytes=total,
            over_cap=(max_bytes > 0 and total > max_bytes) or truncated,
        )
    except Exception as exc:  # pragma: no cover - defensive: pruning never breaks capture
        return ArchivePruneResult(status="error", reason=type(exc).__name__)
