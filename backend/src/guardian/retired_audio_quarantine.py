"""One-shot startup removal of the retired voice-input quarantine (#1179).

The removed audio worker kept short-lived raw and normalized recordings under
the fixed default root ``realpath(tempfile.gettempdir())/seraph-audio``.
Retention was at most 15 minutes, but a backend stopped mid-retention could
leave files behind and nothing cleans them any more. This best-effort pass
removes only that fixed default directory: never an operator-configured path,
never through a symlink. If the root or any entry below it is a symlink, or
anything other than a regular file or directory, nothing is deleted. It never
raises; it returns counts for the startup log.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import stat
import tempfile

logger = logging.getLogger(__name__)

QUARANTINE_DIRNAME = "seraph-audio"


def default_quarantine_root() -> Path:
    # Only the platform temp base is resolved (macOS /var -> /private/var);
    # the quarantine directory itself must not be a symlink.
    return Path(os.path.realpath(tempfile.gettempdir())) / QUARANTINE_DIRNAME


def remove_retired_audio_quarantine(root: Path | None = None) -> dict[str, object]:
    """Delete regular files, then empty directories, under the fixed root."""
    root = default_quarantine_root() if root is None else root
    result: dict[str, object] = {"status": "absent", "files": 0, "directories": 0}
    try:
        try:
            info = root.lstat()
        except FileNotFoundError:
            return result
        if stat.S_ISLNK(info.st_mode):
            result["status"] = "refused_symlink"
            return result
        if not stat.S_ISDIR(info.st_mode):
            result["status"] = "refused_not_directory"
            return result
        files: list[Path] = []
        directories: list[Path] = []
        # Pre-scan everything before deleting anything; refuse on any symlink
        # or special file so no deletion can be redirected.
        for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            base = Path(current)
            for name in (*dirnames, *filenames):
                mode = (base / name).lstat().st_mode
                if stat.S_ISLNK(mode):
                    result["status"] = "refused_symlink"
                    return result
                if stat.S_ISDIR(mode):
                    directories.append(base / name)
                elif stat.S_ISREG(mode):
                    files.append(base / name)
                else:
                    result["status"] = "refused_special_file"
                    return result
        for path in files:
            path.unlink()
            result["files"] = int(result["files"]) + 1
        for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            path.rmdir()
            result["directories"] = int(result["directories"]) + 1
        root.rmdir()
        result["directories"] = int(result["directories"]) + 1
        result["status"] = "removed"
    except Exception as exc:  # best effort: startup must never fail here
        result["status"] = "failed:" + type(exc).__name__
    return result


def cleanup_retired_audio_quarantine_at_startup() -> dict[str, object]:
    result = remove_retired_audio_quarantine()
    if result["status"] != "absent":
        logger.warning(
            "Retired voice-input quarantine cleanup (#1179): %s; removed %d file(s), %d dir(s)",
            result["status"], result["files"], result["directories"],
        )
    return result
