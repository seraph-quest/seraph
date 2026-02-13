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


def _parse_skill_file(path: str) -> Skill | None:
    """Parse a single SKILL.md file. Returns None on validation failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        logger.warning("Failed to read skill file %s: %s", path, e)
        return None

    # Split YAML frontmatter from body
    if not content.startswith("---"):
        logger.warning("Skill file %s missing YAML frontmatter", path)
        return None

    parts = content.split("---", 2)
    if len(parts) < 3:
        logger.warning("Skill file %s has malformed frontmatter", path)
        return None

    frontmatter_str = parts[1].strip()
    body = parts[2].strip()

    try:
        frontmatter = yaml.safe_load(frontmatter_str)
    except yaml.YAMLError as e:
        logger.warning("Skill file %s has invalid YAML: %s", path, e)
        return None

    if not isinstance(frontmatter, dict):
        logger.warning("Skill file %s frontmatter is not a mapping", path)
        return None

    # Validate required fields
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not name or not isinstance(name, str):
        logger.warning("Skill file %s missing required 'name' field", path)
        return None
    if not description or not isinstance(description, str):
        logger.warning("Skill file %s missing required 'description' field", path)
        return None

    # Optional fields
    requires = frontmatter.get("requires", {})
    requires_tools = requires.get("tools", []) if isinstance(requires, dict) else []
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


_loaded_skills: list[Skill] = []


def load_skills(skills_dir: str) -> list[Skill]:
    """Scan directory for *.md files and parse them as skills."""
    global _loaded_skills
    skills: list[Skill] = []

    if not os.path.isdir(skills_dir):
        logger.info("Skills directory %s does not exist, skipping", skills_dir)
        _loaded_skills = []
        return []

    for filename in sorted(os.listdir(skills_dir)):
        if not filename.endswith(".md"):
            continue
        path = os.path.join(skills_dir, filename)
        skill = _parse_skill_file(path)
        if skill:
            skills.append(skill)
            logger.info("Loaded skill: %s from %s", skill.name, filename)

    _loaded_skills = skills
    return skills


def reload_skills(skills_dir: str) -> list[Skill]:
    """Force re-scan of the skills directory."""
    return load_skills(skills_dir)


def get_loaded_skills() -> list[Skill]:
    """Return the cached loaded skills."""
    return list(_loaded_skills)
