import os
import logging

from config.settings import settings

logger = logging.getLogger(__name__)

_soul_path = os.path.join(settings.workspace_dir, settings.soul_file)

_DEFAULT_SOUL = """# Soul of the Traveler

## Identity
- Name: Traveler
- Role: Unknown
- Context: New arrival

## Values
(Not yet discovered)

## Goals
(Not yet defined)

## Personality Notes
(Seraph is still learning about this human)
"""


def read_soul() -> str:
    """Read the soul file. Returns default template if not found."""
    try:
        with open(_soul_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return _DEFAULT_SOUL


def write_soul(content: str) -> None:
    """Write the full soul file."""
    os.makedirs(os.path.dirname(_soul_path), exist_ok=True)
    with open(_soul_path, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info("Soul file updated")


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
