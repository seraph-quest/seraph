"""Git context source — reads reflog from filesystem, no subprocess."""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from config.settings import settings

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
        return None

    reflog_path = git_dir / "logs" / "HEAD"
    if not reflog_path.exists():
        return None

    try:
        lines = reflog_path.read_text().strip().splitlines()
    except OSError:
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

    return {"recent_git_activity": recent if recent else None}
