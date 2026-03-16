"""Git context source — reads reflog from filesystem, no subprocess."""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from config.settings import settings
from src.audit.runtime import log_integration_event_sync

logger = logging.getLogger(__name__)

# Reflog line format: <old-sha> <new-sha> <name> <email> <timestamp> <tz> <tab> <message>
_REFLOG_RE = re.compile(
    r"^[0-9a-f]+ [0-9a-f]+ .+ <.+> (\d+) [+-]\d{4}\t(.+)$"
)


def gather_git() -> dict | None:
    """Parse recent git reflog entries. Returns None if no .git dir found."""
    repo_path = settings.observer_git_repo_path or settings.workspace_dir
    git_dir = Path(repo_path) / ".git"

    if not git_dir.is_dir():
        log_integration_event_sync(
            integration_type="observer_source",
            name="git",
            outcome="unavailable",
            details={"reason": "missing_git_dir"},
        )
        return None

    reflog_path = git_dir / "logs" / "HEAD"
    if not reflog_path.exists():
        log_integration_event_sync(
            integration_type="observer_source",
            name="git",
            outcome="unavailable",
            details={"reason": "missing_reflog"},
        )
        return None

    try:
        lines = reflog_path.read_text().strip().splitlines()
    except OSError as exc:
        log_integration_event_sync(
            integration_type="observer_source",
            name="git",
            outcome="failed",
            details={"error": str(exc)},
        )
        logger.exception("Failed to read git reflog")
        return None

    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - 3600  # last 60 minutes

    recent = []
    for line in reversed(lines):
        match = _REFLOG_RE.match(line)
        if not match:
            continue
        timestamp = int(match.group(1))
        if timestamp < cutoff:
            break
        message = match.group(2)
        recent.append({
            "timestamp": datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(),
            "message": message,
        })
        if len(recent) >= 3:
            break

    if not recent:
        log_integration_event_sync(
            integration_type="observer_source",
            name="git",
            outcome="empty_result",
            details={"recent_activity_count": 0},
        )
        return {"recent_git_activity": None}

    log_integration_event_sync(
        integration_type="observer_source",
        name="git",
        outcome="succeeded",
        details={"recent_activity_count": len(recent)},
    )
    return {"recent_git_activity": recent}
