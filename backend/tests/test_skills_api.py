from unittest.mock import AsyncMock, patch

import pytest

from src.extensions.registry import default_manifest_roots_for_workspace


@pytest.mark.asyncio
async def test_validate_skill_draft_returns_runtime_readiness(client):
    content = (
        "---\n"
        "name: Web Briefing\n"
        "description: Research helper\n"
        "requires:\n"
        "  tools: [web_search, write_file]\n"
        "user_invocable: true\n"
        "---\n\n"
        "Use the web tools.\n"
    )
    with patch(
        "src.api.skills.get_base_tools_and_active_skills",
        return_value=([type("Tool", (), {"name": "web_search"})()], [], "disabled"),
    ):
        resp = await client.post("/api/skills/validate", json={"content": content})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["valid"] is True
    assert payload["runtime_ready"] is False
    assert payload["missing_tools"] == ["write_file"]
    assert payload["skill"]["name"] == "Web Briefing"


@pytest.mark.asyncio
async def test_save_skill_draft_persists_and_reloads(client, tmp_path):
    content = (
        "---\n"
        "name: Web Briefing\n"
        "description: Research helper\n"
        "requires:\n"
        "  tools: [web_search]\n"
        "user_invocable: true\n"
        "---\n\n"
        "Use the web tools.\n"
    )
    skills_dir = tmp_path / "skills"
    with (
        patch("src.api.skills.skill_manager._skills_dir", str(skills_dir)),
        patch("src.api.skills.skill_manager._manifest_roots", []),
        patch("src.extensions.workspace_package.settings.workspace_dir", str(tmp_path)),
        patch(
            "src.api.skills.get_base_tools_and_active_skills",
            return_value=([type("Tool", (), {"name": "web_search"})()], [], "disabled"),
        ),
        patch("src.api.skills.skill_manager.init") as init_manager,
        patch(
            "src.api.skills.skill_manager.reload",
            return_value=[{"name": "Web Briefing", "enabled": True}],
        ) as reload_skills,
        patch("src.api.skills.log_integration_event", AsyncMock()) as log_event,
    ):
        resp = await client.post("/api/skills/save", json={"content": content})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "saved"
    assert payload["valid"] is True
    assert payload["file_path"].endswith("extensions/workspace-capabilities/skills/web-briefing.md")
    init_manager.assert_called_once_with(str(skills_dir), manifest_roots=default_manifest_roots_for_workspace(str(tmp_path)))
    reload_skills.assert_called_once()
    log_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_save_skill_draft_rejects_invalid_content(client):
    resp = await client.post("/api/skills/save", json={"content": "not frontmatter"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_save_skill_draft_rejects_path_traversal(client):
    content = (
        "---\n"
        "name: Safe Skill\n"
        "description: Research helper\n"
        "requires:\n"
        "  tools: [web_search]\n"
        "user_invocable: true\n"
        "---\n\n"
        "Use the web tools.\n"
    )
    with (
        patch("src.api.skills.skill_manager._skills_dir", "/tmp/skills"),
        patch("src.extensions.workspace_package.settings.workspace_dir", "/tmp"),
        patch(
            "src.api.skills.get_base_tools_and_active_skills",
            return_value=([type("Tool", (), {"name": "web_search"})()], [], "disabled"),
        ),
    ):
        resp = await client.post(
            "/api/skills/save",
            json={"content": content, "file_name": "../outside.md"},
        )

    assert resp.status_code == 400
    assert "managed workspace package" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_skill_diagnostics_returns_load_errors(client):
    with patch(
        "src.api.skills.skill_manager.get_diagnostics",
        return_value={
            "skills": [{"name": "safe-skill", "enabled": True}],
            "load_errors": [{"file_path": "/tmp/broken.md", "message": "Missing frontmatter"}],
            "loaded_count": 1,
            "error_count": 1,
        },
    ):
        resp = await client.get("/api/skills/diagnostics")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["loaded_count"] == 1
    assert payload["error_count"] == 1
    assert payload["load_errors"][0]["file_path"] == "/tmp/broken.md"
