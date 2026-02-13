"""Skill manager — singleton that wraps the loader with runtime state."""

import json
import logging
import os

from src.skills.loader import Skill, load_skills

logger = logging.getLogger(__name__)


class SkillManager:
    def __init__(self) -> None:
        self._skills: list[Skill] = []
        self._skills_dir: str = ""
        self._config_path: str = ""
        self._disabled: set[str] = set()

    def init(self, skills_dir: str) -> None:
        """Load skills from disk and restore disabled state from config."""
        self._skills_dir = skills_dir
        self._config_path = os.path.join(
            os.path.dirname(skills_dir), "skills-config.json"
        )
        self._load_config()
        self._skills = load_skills(skills_dir)
        self._apply_disabled()
        logger.info(
            "SkillManager initialized: %d skills loaded", len(self._skills)
        )

    def _load_config(self) -> None:
        """Load disabled skill names from config file."""
        if os.path.isfile(self._config_path):
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._disabled = set(data.get("disabled", []))
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("Failed to load skills config: %s", e)
                self._disabled = set()
        else:
            self._disabled = set()

    def _save_config(self) -> None:
        """Persist disabled skill names to config file."""
        try:
            os.makedirs(os.path.dirname(self._config_path), exist_ok=True)
            with open(self._config_path, "w", encoding="utf-8") as f:
                json.dump({"disabled": sorted(self._disabled)}, f, indent=2)
        except OSError as e:
            logger.warning("Failed to save skills config: %s", e)

    def _apply_disabled(self) -> None:
        """Apply disabled state from config to loaded skills."""
        for skill in self._skills:
            if skill.name in self._disabled:
                skill.enabled = False

    def get_active_skills(self, available_tools: list[str]) -> list[Skill]:
        """Return enabled skills whose tool requirements are met."""
        tool_set = set(available_tools)
        result = []
        for skill in self._skills:
            if not skill.enabled:
                continue
            if skill.requires_tools and not all(
                t in tool_set for t in skill.requires_tools
            ):
                continue
            result.append(skill)
        return result

    def get_skill(self, name: str) -> Skill | None:
        """Look up a skill by name."""
        for skill in self._skills:
            if skill.name == name:
                return skill
        return None

    def list_skills(self) -> list[dict]:
        """Return all skills as dicts (for API responses)."""
        return [
            {
                "name": s.name,
                "description": s.description,
                "requires_tools": s.requires_tools,
                "user_invocable": s.user_invocable,
                "enabled": s.enabled,
                "file_path": s.file_path,
            }
            for s in self._skills
        ]

    def enable(self, name: str) -> bool:
        """Enable a skill by name. Returns False if not found."""
        skill = self.get_skill(name)
        if not skill:
            return False
        skill.enabled = True
        self._disabled.discard(name)
        self._save_config()
        return True

    def disable(self, name: str) -> bool:
        """Disable a skill by name. Returns False if not found."""
        skill = self.get_skill(name)
        if not skill:
            return False
        skill.enabled = False
        self._disabled.add(name)
        self._save_config()
        return True

    def reload(self) -> list[dict]:
        """Re-scan skills directory and return updated list."""
        if self._skills_dir:
            self._skills = load_skills(self._skills_dir)
            self._apply_disabled()
        return self.list_skills()


skill_manager = SkillManager()
