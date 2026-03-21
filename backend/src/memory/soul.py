import os
import logging

from config.settings import settings
from src.audit.runtime import log_integration_event_sync

logger = logging.getLogger(__name__)

_soul_path = os.path.join(settings.workspace_dir, settings.soul_file)

_DEFAULT_SOUL = """# Guardian Record

## Identity
- Name:
- Role:
- Context: Baseline not yet established

## Values
(Not yet discovered)

## Goals
(Not yet defined)

## Personality Notes
(Seraph is still learning how this human works best)
"""


def _log_soul_event(outcome: str, details: dict | None = None) -> None:
    log_integration_event_sync(
        integration_type="soul_file",
        name=os.path.basename(_soul_path),
        outcome=outcome,
        details=details,
    )


def read_soul() -> str:
    """Read the soul file. Returns default template if not found."""
    try:
        with open(_soul_path, "r", encoding="utf-8") as f:
            text = f.read()
            _log_soul_event(
                "succeeded",
                details={"operation": "read", "used_default": False, "length": len(text)},
            )
            return text
    except FileNotFoundError:
        _log_soul_event(
            "empty_result",
            details={"operation": "read", "reason": "missing_file", "used_default": True},
        )
        return _DEFAULT_SOUL
    except Exception as exc:
        _log_soul_event(
            "failed",
            details={"operation": "read", "error": str(exc)},
        )
        raise


def write_soul(content: str) -> None:
    """Write the full soul file."""
    try:
        os.makedirs(os.path.dirname(_soul_path), exist_ok=True)
        with open(_soul_path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("Soul file updated")
        _log_soul_event(
            "succeeded",
            details={"operation": "write", "length": len(content)},
        )
    except Exception as exc:
        _log_soul_event(
            "failed",
            details={"operation": "write", "length": len(content), "error": str(exc)},
        )
        raise


def update_soul_section(section: str, content: str) -> str:
    """Update a specific section in the soul file.

    Finds the ## Section header and replaces everything until the next ## header.
    If the section doesn't exist, appends it.

    Returns the updated soul text.
    """
    soul = read_soul()
    header = f"## {section}"
    lines = soul.split("\n")
    new_lines = []
    in_section = False
    section_found = False

    for line in lines:
        if line.strip().startswith("## "):
            if line.strip() == header:
                in_section = True
                section_found = True
                new_lines.append(line)
                new_lines.append(content)
                continue
            else:
                in_section = False

        if not in_section:
            new_lines.append(line)

    if not section_found:
        new_lines.append("")
        new_lines.append(header)
        new_lines.append(content)

    updated = "\n".join(new_lines)
    write_soul(updated)
    return updated


def ensure_soul_exists() -> None:
    """Create the soul file with defaults if it doesn't exist."""
    if not os.path.exists(_soul_path):
        write_soul(_DEFAULT_SOUL)
        logger.info("Created default soul file at %s", _soul_path)
        _log_soul_event(
            "succeeded",
            details={"operation": "ensure", "created": True},
        )
    else:
        _log_soul_event(
            "skipped",
            details={"operation": "ensure", "created": False},
        )
