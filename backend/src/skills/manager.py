"""Skill manager — singleton that wraps the loader with runtime state."""

import json
import logging
import os

from src.extensions.permissions import evaluate_tool_permissions
from src.extensions.registry import ExtensionRegistry, ExtensionRegistrySnapshot
from src.native_tools.registry import canonical_tool_name
from src.skills.loader import Skill, scan_skill_paths

logger = logging.getLogger(__name__)


class SkillManager:
    def __init__(self) -> None:
        self._skills: list[Skill] = []
        self._load_errors: list[dict[str, str]] = []
        self._skills_dir: str = ""
        self._manifest_roots: list[str] = []
        self._config_path: str = ""
        self._disabled: set[str] = set()
        self._registry: ExtensionRegistry | None = None

    def init(self, skills_dir: str, *, manifest_roots: list[str] | None = None) -> None:
        """Load skills from disk and restore disabled state from config."""
        self._skills_dir = skills_dir
        self._manifest_roots = list(manifest_roots or [os.path.join(os.path.dirname(skills_dir), "extensions")])
        self._config_path = os.path.join(
            os.path.dirname(skills_dir), "skills-config.json"
        )
        self._registry = ExtensionRegistry(
            manifest_roots=self._manifest_roots,
            skill_dirs=[skills_dir],
            workflow_dirs=[],
            mcp_runtime=None,
        )
        self._load_config()
        self._reload_from_registry()
        self._apply_disabled()
        logger.info(
            "SkillManager initialized: %d skills loaded", len(self._skills)
        )

    def _reload_from_registry(self) -> None:
        snapshot = self._snapshot()
        contribution_paths: list[str] = []
        contribution_index: dict[str, tuple[str, str | None, int]] = {}
        for contribution in snapshot.list_contributions("skills"):
            resolved_path = contribution.metadata.get("resolved_path")
            path = str(resolved_path) if isinstance(resolved_path, str) and resolved_path else contribution.reference
            normalized_path = os.path.abspath(path)
            contribution_paths.append(path)
            contribution_index[normalized_path] = (
                contribution.source,
                contribution.extension_id,
                int(contribution.metadata.get("manifest_root_index", len(self._manifest_roots))),
            )

        skills, parse_errors = scan_skill_paths(contribution_paths)
        manifest_priority_by_path: dict[str, int] = {}
        for skill in skills:
            source, extension_id, manifest_root_index = contribution_index.get(
                os.path.abspath(skill.file_path),
                ("legacy", None, len(self._manifest_roots)),
            )
            skill.source = source
            skill.extension_id = extension_id
            manifest_priority_by_path[os.path.abspath(skill.file_path)] = manifest_root_index

        load_errors = [
            {
                "file_path": error.source,
                "message": error.message,
                "phase": error.phase,
            }
            for error in snapshot.load_errors
            if self._error_affects_skills(error.source, error.phase)
        ]
        for error in parse_errors:
            path = str(error.get("file_path") or "")
            source = contribution_index.get(
                os.path.abspath(path),
                ("legacy", None, len(self._manifest_roots)),
            )[0]
            load_errors.append(
                {
                    "file_path": path,
                    "message": str(error.get("message") or "skill parse error"),
                    "phase": "manifest-skills" if source == "manifest" else "legacy-skills",
                }
            )

        deduped_skills: list[Skill] = []
        by_name: dict[str, Skill] = {}
        for skill in sorted(
            skills,
            key=lambda item: (
                0 if item.source == "manifest" else 1,
                manifest_priority_by_path.get(os.path.abspath(item.file_path), len(self._manifest_roots)),
                item.file_path,
            ),
        ):
            existing = by_name.get(skill.name)
            if existing is not None:
                load_errors.append(
                    {
                        "file_path": skill.file_path,
                        "message": (
                            f"Duplicate skill name '{skill.name}' from {skill.file_path}; "
                            f"keeping {existing.file_path}"
                        ),
                        "phase": "duplicate-skill-name",
                    }
                )
                continue
            by_name[skill.name] = skill
            deduped_skills.append(skill)

        self._skills = deduped_skills
        self._load_errors = load_errors

    def _snapshot(self) -> ExtensionRegistrySnapshot:
        if self._registry is None:
            self._registry = ExtensionRegistry(
                manifest_roots=self._manifest_roots,
                skill_dirs=[self._skills_dir] if self._skills_dir else [],
                workflow_dirs=[],
                mcp_runtime=None,
            )
        return self._registry.snapshot()

    def _error_affects_skills(self, source: str, phase: str) -> bool:
        if phase == "legacy-skills":
            return True
        if phase not in {"manifest", "compatibility", "layout"}:
            return False
        package_root = source
        if os.path.basename(source) in {"manifest.yaml", "manifest.yml"}:
            package_root = os.path.dirname(source)
        return os.path.isdir(os.path.join(package_root, "skills"))

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
        tool_set = {canonical_tool_name(name) for name in available_tools}
        snapshot = self._snapshot()
        extensions_by_id = {extension.id: extension for extension in snapshot.extensions}
        result = []
        for skill in self._skills:
            if not skill.enabled:
                continue
            permission_profile = evaluate_tool_permissions(
                extensions_by_id.get(skill.extension_id) if skill.extension_id else None,
                tool_names=skill.requires_tools,
            )
            if not permission_profile["ok"]:
                continue
            if skill.requires_tools and not all(
                canonical_tool_name(t) in tool_set for t in skill.requires_tools
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
        snapshot = self._snapshot()
        extensions_by_id = {extension.id: extension for extension in snapshot.extensions}
        items: list[dict] = []
        for skill in self._skills:
            permission_profile = evaluate_tool_permissions(
                extensions_by_id.get(skill.extension_id) if skill.extension_id else None,
                tool_names=skill.requires_tools,
            )
            items.append(
                {
                    "name": skill.name,
                    "description": skill.description,
                    "requires_tools": skill.requires_tools,
                    "user_invocable": skill.user_invocable,
                    "enabled": skill.enabled,
                    "file_path": skill.file_path,
                    "source": skill.source,
                    "extension_id": skill.extension_id,
                    "permission_status": permission_profile["status"],
                    "missing_manifest_tools": list(permission_profile["missing_tools"]),
                    "required_execution_boundaries": list(permission_profile["required_execution_boundaries"]),
                    "missing_manifest_execution_boundaries": list(permission_profile["missing_execution_boundaries"]),
                    "requires_network": bool(permission_profile["requires_network"]),
                    "missing_manifest_network": bool(permission_profile["missing_network"]),
                    "risk_level": permission_profile["risk_level"],
                    "approval_behavior": permission_profile["approval_behavior"],
                    "requires_approval": bool(permission_profile["requires_approval"]),
                    "accepts_secret_refs": bool(permission_profile["accepts_secret_refs"]),
                }
            )
        return items

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
            self._reload_from_registry()
            self._apply_disabled()
        return self.list_skills()

    def get_diagnostics(self) -> dict[str, object]:
        return {
            "skills": self.list_skills(),
            "load_errors": list(self._load_errors),
            "loaded_count": len(self._skills),
            "error_count": len(self._load_errors),
        }


skill_manager = SkillManager()
