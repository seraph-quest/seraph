"""Tests for SKILL.md plugin system (loader, manager, API)."""

import json
import os
import tempfile

import pytest
import pytest_asyncio

from src.skills.loader import Skill, _parse_skill_file, load_skills
from src.skills.manager import SkillManager


# ── Fixtures ─────────────────────────────────────────────


@pytest.fixture
def skills_dir(tmp_path):
    """Create a temp skills directory with test skill files."""
    d = tmp_path / "skills"
    d.mkdir()

    # Valid skill
    (d / "valid.md").write_text(
        "---\n"
        "name: test-skill\n"
        "description: A test skill\n"
        "requires:\n"
        "  tools: [web_search]\n"
        "user_invocable: true\n"
        "---\n\n"
        "Do something useful."
    )

    # Skill with no requirements
    (d / "simple.md").write_text(
        "---\n"
        "name: simple-skill\n"
        "description: No tool requirements\n"
        "---\n\n"
        "Just some instructions."
    )

    # Skill with enabled: false in frontmatter
    (d / "disabled.md").write_text(
        "---\n"
        "name: disabled-skill\n"
        "description: Disabled by default\n"
        "enabled: false\n"
        "---\n\n"
        "Should not be active."
    )

    return str(d)


@pytest.fixture
def invalid_skills_dir(tmp_path):
    """Create a temp directory with invalid skill files."""
    d = tmp_path / "bad_skills"
    d.mkdir()

    # Missing frontmatter
    (d / "no-frontmatter.md").write_text("Just plain markdown, no YAML.")

    # Missing name
    (d / "no-name.md").write_text(
        "---\n"
        "description: Missing the name field\n"
        "---\n\n"
        "Body text."
    )

    # Missing description
    (d / "no-desc.md").write_text(
        "---\n"
        "name: no-desc\n"
        "---\n\n"
        "Body text."
    )

    # Invalid YAML
    (d / "bad-yaml.md").write_text(
        "---\n"
        ": broken yaml [[\n"
        "---\n\n"
        "Body."
    )

    return str(d)


# ── TestSkillLoader ──────────────────────────────────────


class TestSkillLoader:
    def test_parse_valid_skill(self, skills_dir):
        path = os.path.join(skills_dir, "valid.md")
        skill = _parse_skill_file(path)
        assert skill is not None
        assert skill.name == "test-skill"
        assert skill.description == "A test skill"
        assert skill.requires_tools == ["web_search"]
        assert skill.user_invocable is True
        assert skill.enabled is True
        assert "Do something useful" in skill.instructions

    def test_parse_simple_skill(self, skills_dir):
        path = os.path.join(skills_dir, "simple.md")
        skill = _parse_skill_file(path)
        assert skill is not None
        assert skill.name == "simple-skill"
        assert skill.requires_tools == []
        assert skill.user_invocable is False

    def test_parse_disabled_skill(self, skills_dir):
        path = os.path.join(skills_dir, "disabled.md")
        skill = _parse_skill_file(path)
        assert skill is not None
        assert skill.enabled is False

    def test_load_skills_from_directory(self, skills_dir):
        skills = load_skills(skills_dir)
        assert len(skills) == 3
        names = {s.name for s in skills}
        assert names == {"test-skill", "simple-skill", "disabled-skill"}

    def test_load_skills_nonexistent_dir(self, tmp_path):
        skills = load_skills(str(tmp_path / "nonexistent"))
        assert skills == []

    def test_parse_missing_frontmatter(self, invalid_skills_dir):
        path = os.path.join(invalid_skills_dir, "no-frontmatter.md")
        assert _parse_skill_file(path) is None

    def test_parse_missing_name(self, invalid_skills_dir):
        path = os.path.join(invalid_skills_dir, "no-name.md")
        assert _parse_skill_file(path) is None

    def test_parse_missing_description(self, invalid_skills_dir):
        path = os.path.join(invalid_skills_dir, "no-desc.md")
        assert _parse_skill_file(path) is None

    def test_parse_bad_yaml(self, invalid_skills_dir):
        path = os.path.join(invalid_skills_dir, "bad-yaml.md")
        assert _parse_skill_file(path) is None

    def test_load_skips_invalid_files(self, invalid_skills_dir):
        skills = load_skills(invalid_skills_dir)
        assert skills == []

    def test_empty_body(self, tmp_path):
        d = tmp_path / "skills"
        d.mkdir()
        (d / "empty-body.md").write_text(
            "---\n"
            "name: empty\n"
            "description: Has empty body\n"
            "---\n"
        )
        skills = load_skills(str(d))
        assert len(skills) == 1
        assert skills[0].instructions == ""


# ── TestSkillManager ─────────────────────────────────────


class TestSkillManager:
    def test_init_loads_skills(self, skills_dir):
        mgr = SkillManager()
        mgr.init(skills_dir)
        assert len(mgr.list_skills()) == 3

    def test_get_active_skills_filters_disabled(self, skills_dir):
        mgr = SkillManager()
        mgr.init(skills_dir)
        active = mgr.get_active_skills(["web_search"])
        names = {s.name for s in active}
        assert "disabled-skill" not in names

    def test_get_active_skills_tool_gating(self, skills_dir):
        mgr = SkillManager()
        mgr.init(skills_dir)
        # With web_search available: test-skill + simple-skill (disabled excluded)
        active = mgr.get_active_skills(["web_search"])
        names = {s.name for s in active}
        assert "test-skill" in names
        assert "simple-skill" in names

        # Without web_search: test-skill excluded (requires it)
        active = mgr.get_active_skills(["other_tool"])
        names = {s.name for s in active}
        assert "test-skill" not in names
        assert "simple-skill" in names

    def test_get_skill_by_name(self, skills_dir):
        mgr = SkillManager()
        mgr.init(skills_dir)
        assert mgr.get_skill("test-skill") is not None
        assert mgr.get_skill("nonexistent") is None

    def test_enable_disable(self, skills_dir):
        mgr = SkillManager()
        mgr.init(skills_dir)

        # Disable test-skill
        assert mgr.disable("test-skill") is True
        skill = mgr.get_skill("test-skill")
        assert skill.enabled is False

        # Enable it back
        assert mgr.enable("test-skill") is True
        skill = mgr.get_skill("test-skill")
        assert skill.enabled is True

        # Non-existent
        assert mgr.disable("nonexistent") is False
        assert mgr.enable("nonexistent") is False

    def test_config_persistence(self, skills_dir, tmp_path):
        mgr = SkillManager()
        mgr.init(skills_dir)
        mgr.disable("test-skill")

        config_path = os.path.join(os.path.dirname(skills_dir), "skills-config.json")
        assert os.path.isfile(config_path)
        with open(config_path) as f:
            data = json.load(f)
        assert "test-skill" in data["disabled"]

        # New manager should restore disabled state
        mgr2 = SkillManager()
        mgr2.init(skills_dir)
        skill = mgr2.get_skill("test-skill")
        assert skill.enabled is False

    def test_reload(self, skills_dir):
        mgr = SkillManager()
        mgr.init(skills_dir)
        result = mgr.reload()
        assert len(result) == 3

    def test_list_skills_format(self, skills_dir):
        mgr = SkillManager()
        mgr.init(skills_dir)
        lst = mgr.list_skills()
        assert isinstance(lst, list)
        assert all("name" in s for s in lst)
        assert all("description" in s for s in lst)
        assert all("enabled" in s for s in lst)
        assert all("requires_tools" in s for s in lst)


# ── TestSkillAPI ─────────────────────────────────────────


@pytest.fixture
def _setup_skill_manager(skills_dir):
    """Initialize the global skill_manager for API tests."""
    from src.skills.manager import skill_manager
    skill_manager.init(skills_dir)
    yield
    # Reset
    skill_manager._skills = []
    skill_manager._disabled = set()


class TestSkillAPI:
    @pytest.mark.asyncio
    async def test_list_skills(self, client, _setup_skill_manager):
        resp = await client.get("/api/skills")
        assert resp.status_code == 200
        data = resp.json()
        assert "skills" in data
        assert len(data["skills"]) == 3

    @pytest.mark.asyncio
    async def test_enable_disable_skill(self, client, _setup_skill_manager):
        # Disable
        resp = await client.put(
            "/api/skills/test-skill",
            json={"enabled": False},
        )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

        # Verify via list
        resp = await client.get("/api/skills")
        skills = resp.json()["skills"]
        test_skill = next(s for s in skills if s["name"] == "test-skill")
        assert test_skill["enabled"] is False

        # Enable
        resp = await client.put(
            "/api/skills/test-skill",
            json={"enabled": True},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_update_nonexistent_skill(self, client, _setup_skill_manager):
        resp = await client.put(
            "/api/skills/nonexistent",
            json={"enabled": False},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_reload_skills(self, client, _setup_skill_manager):
        resp = await client.post("/api/skills/reload")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "reloaded"
        assert data["count"] == 3
