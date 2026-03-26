import logging
import os

from config.settings import settings
from src.audit.runtime import log_integration_event_sync

logger = logging.getLogger(__name__)

_soul_path = os.path.join(settings.workspace_dir, settings.soul_file)
_SOUL_SECTION_ORDER = (
    "Identity",
    "Values",
    "Goals",
    "Personality Notes",
)
_DEFAULT_SOUL_SECTIONS = {
    "Identity": "- Name:\n- Role:\n- Context: Baseline not yet established",
    "Values": "(Not yet discovered)",
    "Goals": "(Not yet defined)",
    "Personality Notes": "(Seraph is still learning how this human works best)",
}


def default_soul_sections() -> dict[str, str]:
    return dict(_DEFAULT_SOUL_SECTIONS)


def render_soul_text(sections: dict[str, str] | None = None) -> str:
    resolved_sections = dict(sections or {})
    blocks = ["# Guardian Record", ""]

    ordered_sections: list[tuple[str, str]] = []
    for section_name in _SOUL_SECTION_ORDER:
        content = str(
            resolved_sections.get(section_name)
            or _DEFAULT_SOUL_SECTIONS[section_name]
        ).strip()
        ordered_sections.append((section_name, content))

    for section_name, content in resolved_sections.items():
        normalized_name = str(section_name).strip()
        normalized_content = str(content).strip()
        if (
            not normalized_name
            or normalized_name in _SOUL_SECTION_ORDER
            or not normalized_content
        ):
            continue
        ordered_sections.append((normalized_name, normalized_content))

    for index, (section_name, content) in enumerate(ordered_sections):
        blocks.append(f"## {section_name}")
        blocks.append(content)
        if index != len(ordered_sections) - 1:
            blocks.append("")

    return "\n".join(blocks)


_DEFAULT_SOUL = render_soul_text(default_soul_sections()) + "\n"


def parse_soul_sections(text: str | None) -> dict[str, str]:
    if not isinstance(text, str):
        return default_soul_sections()

    sections: dict[str, str] = {}
    current_section: str | None = None
    current_lines: list[str] = []

    def _flush() -> None:
        nonlocal current_section, current_lines
        if current_section is None:
            return
        content = "\n".join(current_lines).strip()
        sections[current_section] = content or "(Empty)"

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("## "):
            _flush()
            current_section = stripped[3:].strip()
            current_lines = []
            continue
        if current_section is not None:
            current_lines.append(raw_line.rstrip())

    _flush()
    if sections:
        return sections

    stripped_text = text.strip()
    if stripped_text:
        return {"Notes": stripped_text}
    return default_soul_sections()


def read_soul_file_text() -> str | None:
    try:
        with open(_soul_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def get_soul_file_mtime() -> float | None:
    try:
        return os.path.getmtime(_soul_path)
    except FileNotFoundError:
        return None


def _log_soul_event(outcome: str, details: dict | None = None) -> None:
    log_integration_event_sync(
        integration_type="soul_file",
        name=os.path.basename(_soul_path),
        outcome=outcome,
        details=details,
    )


def read_soul() -> str:
    """Read the soul file. Returns default template if not found."""
    text = read_soul_file_text()
    if text is not None:
        _log_soul_event(
            "succeeded",
            details={"operation": "read", "used_default": False, "length": len(text)},
        )
        return text

    _log_soul_event(
        "empty_result",
        details={"operation": "read", "reason": "missing_file", "used_default": True},
    )
    return _DEFAULT_SOUL


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
    """Update a specific section in the soul file and return the rendered text."""
    normalized_section = " ".join(str(section).strip().split())
    if not normalized_section:
        raise ValueError("section must be non-empty")

    existing_text = read_soul_file_text()
    sections = (
        parse_soul_sections(existing_text)
        if existing_text is not None
        else default_soul_sections()
    )
    sections[normalized_section] = str(content).strip()

    updated = render_soul_text(sections)
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
