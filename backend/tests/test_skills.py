"""Tests for the SKILL.md skill system (loader, manager, API)."""

import json
import os
import tempfile

import pytest
import pytest_asyncio

from src.audit.repository import audit_repository
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

    def test_manifest_backed_skill_with_missing_manifest_permissions_is_not_active(self, tmp_path):
        skills_dir = tmp_path / "skills"
        extensions_dir = tmp_path / "extensions" / "skill-pack"
        skills_dir.mkdir()
        extensions_dir.mkdir(parents=True)

        (extensions_dir / "manifest.yaml").write_text(
            "id: seraph.skill-pack\n"
            "version: 2026.3.21\n"
            "display_name: Skill Pack\n"
            "kind: capability-pack\n"
            "compatibility:\n"
            "  seraph: \">=2026.3.19\"\n"
            "publisher:\n"
            "  name: Seraph\n"
            "trust: local\n"
            "contributes:\n"
            "  skills:\n"
            "    - skills/web-briefing.md\n"
            "permissions:\n"
            "  tools: []\n"
            "  network: false\n",
            encoding="utf-8",
        )
        (extensions_dir / "skills").mkdir()
        (extensions_dir / "skills" / "web-briefing.md").write_text(
            "---\n"
            "name: web-briefing\n"
            "description: Research helper\n"
            "requires:\n"
            "  tools: [web_search]\n"
            "---\n\n"
            "Use web_search.\n",
            encoding="utf-8",
        )

        mgr = SkillManager()
        mgr.init(str(skills_dir), manifest_roots=[str(tmp_path / "extensions")])

        listed = {skill["name"]: skill for skill in mgr.list_skills()}
        assert listed["web-briefing"]["permission_status"] == "insufficient"
        assert listed["web-briefing"]["missing_manifest_tools"] == ["web_search"]
        assert mgr.get_active_skills(["web_search"]) == []

    def test_init_loads_manifest_backed_skills_alongside_legacy_skills(self, tmp_path):
        skills_dir = tmp_path / "skills"
        extensions_dir = tmp_path / "extensions" / "research-pack"
        skills_dir.mkdir()
        extensions_dir.mkdir(parents=True)

        (skills_dir / "legacy.md").write_text(
            "---\n"
            "name: legacy-skill\n"
            "description: Legacy skill\n"
            "---\n\n"
            "Legacy instructions.\n"
        )
        (extensions_dir / "manifest.yaml").write_text(
            "id: seraph.research-pack\n"
            "version: 2026.3.21\n"
            "display_name: Research Pack\n"
            "kind: capability-pack\n"
            "compatibility:\n"
            "  seraph: \">=2026.3.19\"\n"
            "publisher:\n"
            "  name: Seraph\n"
            "trust: local\n"
            "contributes:\n"
            "  skills:\n"
            "    - skills/research-pack.md\n"
            "permissions:\n"
            "  tools: []\n"
            "  network: false\n",
            encoding="utf-8",
        )
        (extensions_dir / "skills").mkdir()
        (extensions_dir / "skills" / "research-pack.md").write_text(
            "---\n"
            "name: packaged-skill\n"
            "description: Packaged skill\n"
            "requires:\n"
            "  tools: []\n"
            "user_invocable: true\n"
            "---\n\n"
            "Packaged instructions.\n",
            encoding="utf-8",
        )

        mgr = SkillManager()
        mgr.init(str(skills_dir), manifest_roots=[str(tmp_path / "extensions")])

        listed = {skill["name"]: skill for skill in mgr.list_skills()}
        assert set(listed) == {"legacy-skill", "packaged-skill"}
        assert listed["legacy-skill"]["source"] == "legacy"
        assert listed["legacy-skill"]["extension_id"].startswith("legacy.skills.")
        assert listed["packaged-skill"]["source"] == "manifest"
        assert listed["packaged-skill"]["extension_id"] == "seraph.research-pack"

    def test_manifest_backed_skill_parse_errors_surface_in_diagnostics(self, tmp_path):
        skills_dir = tmp_path / "skills"
        extensions_dir = tmp_path / "extensions" / "broken-pack"
        skills_dir.mkdir()
        extensions_dir.mkdir(parents=True)

        (extensions_dir / "manifest.yaml").write_text(
            "id: seraph.broken-pack\n"
            "version: 2026.3.21\n"
            "display_name: Broken Pack\n"
            "kind: capability-pack\n"
            "compatibility:\n"
            "  seraph: \">=2026.3.19\"\n"
            "publisher:\n"
            "  name: Seraph\n"
            "trust: local\n"
            "contributes:\n"
            "  skills:\n"
            "    - skills/broken.md\n"
            "permissions:\n"
            "  tools: []\n"
            "  network: false\n",
            encoding="utf-8",
        )
        (extensions_dir / "skills").mkdir()
        (extensions_dir / "skills" / "broken.md").write_text("not frontmatter", encoding="utf-8")

        mgr = SkillManager()
        mgr.init(str(skills_dir), manifest_roots=[str(tmp_path / "extensions")])

        diagnostics = mgr.get_diagnostics()
        assert diagnostics["loaded_count"] == 0
        assert diagnostics["error_count"] == 1
        assert diagnostics["load_errors"][0]["phase"] == "manifest-skills"
        assert diagnostics["load_errors"][0]["file_path"].endswith("broken.md")

    def test_manifest_backed_skill_names_win_duplicate_name_collisions(self, tmp_path):
        skills_dir = tmp_path / "skills"
        extensions_dir = tmp_path / "extensions" / "research-pack"
        skills_dir.mkdir()
        extensions_dir.mkdir(parents=True)

        (skills_dir / "duplicate.md").write_text(
            "---\n"
            "name: shared-skill\n"
            "description: Legacy skill\n"
            "---\n\n"
            "Legacy instructions.\n",
            encoding="utf-8",
        )
        (extensions_dir / "manifest.yaml").write_text(
            "id: seraph.research-pack\n"
            "version: 2026.3.21\n"
            "display_name: Research Pack\n"
            "kind: capability-pack\n"
            "compatibility:\n"
            "  seraph: \">=2026.3.19\"\n"
            "publisher:\n"
            "  name: Seraph\n"
            "trust: local\n"
            "contributes:\n"
            "  skills:\n"
            "    - skills/research-pack.md\n"
            "permissions:\n"
            "  tools: []\n"
            "  network: false\n",
            encoding="utf-8",
        )
        (extensions_dir / "skills").mkdir()
        (extensions_dir / "skills" / "research-pack.md").write_text(
            "---\n"
            "name: shared-skill\n"
            "description: Manifest skill\n"
            "---\n\n"
            "Manifest instructions.\n",
            encoding="utf-8",
        )

        mgr = SkillManager()
        mgr.init(str(skills_dir), manifest_roots=[str(tmp_path / "extensions")])

        assert [skill["name"] for skill in mgr.list_skills()] == ["shared-skill"]
        selected = mgr.get_skill("shared-skill")
        assert selected is not None
        assert selected.source == "manifest"
        assert selected.description == "Manifest skill"
        assert any(error["phase"] == "duplicate-skill-name" for error in mgr.get_diagnostics()["load_errors"])

    def test_list_skills_format(self, skills_dir):
        mgr = SkillManager()
        mgr.init(skills_dir)
        lst = mgr.list_skills()
        assert isinstance(lst, list)
        assert all("name" in s for s in lst)
        assert all("description" in s for s in lst)
        assert all("enabled" in s for s in lst)
        assert all("requires_tools" in s for s in lst)
        assert all("source" in s for s in lst)
        assert all("extension_id" in s for s in lst)

    def test_mcp_dependent_skill_inactive_without_tool(self, tmp_path):
        """A skill requiring http_request should be inactive when tool is unavailable."""
        d = tmp_path / "skills"
        d.mkdir()
        (d / "mcp-skill.md").write_text(
            "---\n"
            "name: mcp-skill\n"
            "description: Needs MCP tool\n"
            "requires:\n"
            "  tools: [http_request]\n"
            "---\n\n"
            "Use http_request to do things."
        )
        mgr = SkillManager()
        mgr.init(str(d))

        # Without http_request in available tools
        active = mgr.get_active_skills(["web_search", "read_file"])
        names = {s.name for s in active}
        assert "mcp-skill" not in names

    def test_mcp_dependent_skill_active_with_tool(self, tmp_path):
        """A skill requiring http_request should be active when tool is available."""
        d = tmp_path / "skills"
        d.mkdir()
        (d / "mcp-skill.md").write_text(
            "---\n"
            "name: mcp-skill\n"
            "description: Needs MCP tool\n"
            "requires:\n"
            "  tools: [http_request]\n"
            "---\n\n"
            "Use http_request to do things."
        )
        mgr = SkillManager()
        mgr.init(str(d))

        # With http_request available
        active = mgr.get_active_skills(["http_request", "web_search"])
        names = {s.name for s in active}
        assert "mcp-skill" in names


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


@pytest.fixture
def _setup_manifest_skill_manager(tmp_path):
    from src.skills.manager import skill_manager

    skills_dir = tmp_path / "skills"
    extensions_dir = tmp_path / "extensions" / "research-pack"
    skills_dir.mkdir()
    extensions_dir.mkdir(parents=True)

    (skills_dir / "legacy.md").write_text(
        "---\n"
        "name: legacy-skill\n"
        "description: Legacy skill\n"
        "---\n\n"
        "Legacy instructions.\n",
        encoding="utf-8",
    )
    (extensions_dir / "manifest.yaml").write_text(
        "id: seraph.research-pack\n"
        "version: 2026.3.21\n"
        "display_name: Research Pack\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.3.19\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  skills:\n"
        "    - skills/research-pack.md\n"
        "permissions:\n"
        "  tools: []\n"
        "  network: false\n",
        encoding="utf-8",
    )
    (extensions_dir / "skills").mkdir()
    (extensions_dir / "skills" / "research-pack.md").write_text(
        "---\n"
        "name: packaged-skill\n"
        "description: Packaged skill\n"
        "requires:\n"
        "  tools: []\n"
        "user_invocable: true\n"
        "---\n\n"
        "Packaged instructions.\n",
        encoding="utf-8",
    )

    skill_manager.init(str(skills_dir), manifest_roots=[str(tmp_path / "extensions")])
    yield
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
    async def test_list_skills_includes_manifest_backed_entries(self, client, _setup_manifest_skill_manager):
        resp = await client.get("/api/skills")

        assert resp.status_code == 200
        skills = {item["name"]: item for item in resp.json()["skills"]}
        assert set(skills) == {"legacy-skill", "packaged-skill"}
        assert skills["packaged-skill"]["source"] == "manifest"
        assert skills["packaged-skill"]["extension_id"] == "seraph.research-pack"

    @pytest.mark.asyncio
    async def test_enable_disable_manifest_backed_skill(self, client, _setup_manifest_skill_manager):
        resp = await client.put(
            "/api/skills/packaged-skill",
            json={"enabled": False},
        )
        assert resp.status_code == 200

        resp = await client.get("/api/skills")
        skills = {item["name"]: item for item in resp.json()["skills"]}
        assert skills["packaged-skill"]["enabled"] is False

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

    @pytest.mark.asyncio
    async def test_update_skill_logs_runtime_audit(self, async_db, client, _setup_skill_manager):
        resp = await client.put(
            "/api/skills/test-skill",
            json={"enabled": False},
        )
        assert resp.status_code == 200

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "integration_succeeded"
            and event["tool_name"] == "skill:test-skill"
            and event["details"]["enabled"] is False
            for event in events
        )

    @pytest.mark.asyncio
    async def test_update_nonexistent_skill_logs_runtime_audit(self, async_db, client, _setup_skill_manager):
        resp = await client.put(
            "/api/skills/nonexistent",
            json={"enabled": False},
        )
        assert resp.status_code == 404

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "integration_failed"
            and event["tool_name"] == "skill:nonexistent"
            and event["details"]["status"] == "not_found"
            for event in events
        )

    @pytest.mark.asyncio
    async def test_reload_skills_logs_runtime_audit(self, async_db, client, _setup_skill_manager):
        resp = await client.post("/api/skills/reload")
        assert resp.status_code == 200

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "integration_succeeded"
            and event["tool_name"] == "skills:reload"
            and event["details"]["count"] == 3
            and sorted(event["details"]["skill_names"]) == ["disabled-skill", "simple-skill", "test-skill"]
            for event in events
        )
