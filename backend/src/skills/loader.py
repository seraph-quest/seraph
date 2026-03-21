"""SKILL.md loader — parses markdown files with YAML frontmatter into Skill objects."""

import logging
import os
from dataclasses import dataclass, field

import yaml

logger = logging.getLogger(__name__)


@dataclass
class Skill:
    name: str
    description: str
    instructions: str  # markdown body
    requires_tools: list[str] = field(default_factory=list)
    user_invocable: bool = False
    enabled: bool = True
    file_path: str = ""
    source: str = "legacy"
    extension_id: str | None = None


def _record_skill_error(
    errors: list[dict[str, str]] | None,
    *,
    path: str,
    message: str,
) -> None:
    logger.warning(message)
    if errors is not None:
        errors.append({"file_path": path, "message": message})


def parse_skill_content(
    content: str,
    *,
    path: str = "<draft>",
    errors: list[dict[str, str]] | None = None,
) -> Skill | None:
    """Parse SKILL markdown content. Returns None on validation failure."""
    # Split YAML frontmatter from body
    if not content.startswith("---"):
        _record_skill_error(errors, path=path, message=f"Skill file {path} missing YAML frontmatter")
        return None

    parts = content.split("---", 2)
    if len(parts) < 3:
        _record_skill_error(errors, path=path, message=f"Skill file {path} has malformed frontmatter")
        return None

    frontmatter_str = parts[1].strip()
    body = parts[2].strip()

    try:
        frontmatter = yaml.safe_load(frontmatter_str)
    except yaml.YAMLError as e:
        _record_skill_error(errors, path=path, message=f"Skill file {path} has invalid YAML: {e}")
        return None

    if not isinstance(frontmatter, dict):
        _record_skill_error(errors, path=path, message=f"Skill file {path} frontmatter is not a mapping")
        return None

    # Validate required fields
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not name or not isinstance(name, str):
        _record_skill_error(errors, path=path, message=f"Skill file {path} missing required 'name' field")
        return None
    if not description or not isinstance(description, str):
        _record_skill_error(errors, path=path, message=f"Skill file {path} missing required 'description' field")
        return None

    # Optional fields
    requires = frontmatter.get("requires", {})
    requires_tools = requires.get("tools", []) if isinstance(requires, dict) else []
    if not isinstance(requires_tools, list) or not all(isinstance(item, str) for item in requires_tools):
        _record_skill_error(errors, path=path, message=f"Skill file {path} has invalid requires.tools")
        return None
    user_invocable = bool(frontmatter.get("user_invocable", False))
    enabled = bool(frontmatter.get("enabled", True))

    return Skill(
        name=name,
        description=description,
        instructions=body,
        requires_tools=requires_tools if isinstance(requires_tools, list) else [],
        user_invocable=user_invocable,
        enabled=enabled,
        file_path=path,
    )


def _parse_skill_file(path: str) -> Skill | None:
    """Parse a single SKILL.md file. Returns None on validation failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        logger.warning("Failed to read skill file %s: %s", path, e)
        return None

    return parse_skill_content(content, path=path)


_loaded_skills: list[Skill] = []


def scan_skill_paths(skill_paths: list[str]) -> tuple[list[Skill], list[dict[str, str]]]:
    """Parse an explicit set of skill file paths with structured errors."""
    global _loaded_skills
    skills: list[Skill] = []
    errors: list[dict[str, str]] = []

    for path in sorted(skill_paths):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                content = handle.read()
        except OSError as exc:
            _record_skill_error(errors, path=path, message=f"Failed to read skill file {path}: {exc}")
            continue
        skill = parse_skill_content(content, path=path, errors=errors)
        if skill:
            skills.append(skill)
            logger.info("Loaded skill: %s from %s", skill.name, os.path.basename(path))

    _loaded_skills = skills
    return skills, errors


def load_skills(skills_dir: str) -> list[Skill]:
    """Scan directory for *.md files and parse them as skills."""
    skills, _ = scan_skills(skills_dir)
    return skills


def scan_skills(skills_dir: str) -> tuple[list[Skill], list[dict[str, str]]]:
    """Scan directory for *.md files and parse them as skills with structured errors."""
    global _loaded_skills
    skills: list[Skill] = []
    errors: list[dict[str, str]] = []

    if not os.path.isdir(skills_dir):
        logger.info("Skills directory %s does not exist, skipping", skills_dir)
        _loaded_skills = []
        return [], []

    skill_paths = [
        os.path.join(skills_dir, filename)
        for filename in sorted(os.listdir(skills_dir))
        if filename.endswith(".md")
    ]
    return scan_skill_paths(skill_paths)


def reload_skills(skills_dir: str) -> list[Skill]:
    """Force re-scan of the skills directory."""
    return load_skills(skills_dir)


def get_loaded_skills() -> list[Skill]:
    """Return the cached loaded skills."""
    return list(_loaded_skills)
