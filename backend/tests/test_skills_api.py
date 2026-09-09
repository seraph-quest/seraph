from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
import pytest
from starlette.requests import Request

from src.approval.runtime import get_current_session_id, get_current_trust_principal
from src.auth.service import test_bypass_operator as _test_bypass_operator
from src.extensions.registry import default_manifest_roots_for_workspace
from src.security.trust_contract import PrincipalType


def _skill_mutator_request(operator, path: str = "/api/skills/save"):
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "query_string": b"",
            "state": {"operator": operator},
        }
    )


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
async def test_save_skill_draft_rejects_reserved_evolution_candidate_name():
    from src.api.skills import SkillDraftRequest, save_skill_draft
    from src.extensions.workspace_package import EVOLUTION_CANDIDATE_FILE_NAME_ERROR

    operator = _test_bypass_operator()
    validation = {
        "valid": True,
        "errors": [],
        "skill": {
            "name": "Review Candidate",
            "description": "",
            "requires_tools": [],
            "user_invocable": True,
            "enabled": True,
            "file_path": "<draft>",
        },
        "runtime_ready": True,
        "missing_tools": [],
    }
    with (
        patch("src.api.skills.context_manager.get_context", return_value=SimpleNamespace(approval_mode="safe")),
        patch("src.api.skills._validate_skill_content", return_value=validation),
        patch("src.api.skills._ensure_skill_manager_workspace_extensions_loaded"),
        patch(
            "src.api.skills.save_workspace_contribution",
            side_effect=ValueError(EVOLUTION_CANDIDATE_FILE_NAME_ERROR),
        ),
    ):
        with pytest.raises(HTTPException) as raised:
            await save_skill_draft(
                SkillDraftRequest(content="draft", file_name="ReviewReview-Candidate.MD"),
                _skill_mutator_request(operator),
            )

    assert raised.value.status_code == 409
    assert raised.value.detail == EVOLUTION_CANDIDATE_FILE_NAME_ERROR


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


@pytest.mark.asyncio
async def test_skill_mutators_deny_invalid_middleware_authority_before_side_effects():
    from src.api.skills import SkillDraftRequest, UpdateSkillRequest, reload_skills, save_skill_draft, update_skill

    operator = _test_bypass_operator()
    invalid_operators = (
        None,
        object(),
        replace(operator, principal=replace(operator.principal, authenticated=False)),
        replace(operator, principal=replace(operator.principal, revoked=True)),
        replace(operator, principal=replace(operator.principal, session_id="other-session")),
        replace(operator, principal=replace(operator.principal, principal_type=PrincipalType.SERVICE)),
        replace(operator, principal=replace(operator.principal, grants=())),
    )

    with (
        patch("src.api.skills._validate_skill_content") as validate,
        patch("src.api.skills._ensure_skill_manager_workspace_extensions_loaded") as ensure_workspace,
        patch("src.api.skills.save_workspace_contribution") as save_workspace,
        patch("src.api.skills.skill_manager.enable") as enable,
        patch("src.api.skills.skill_manager.disable") as disable,
        patch("src.api.skills.skill_manager.reload") as reload_skills_mock,
        patch("src.api.skills.log_integration_event", new_callable=AsyncMock) as log,
        patch("src.api.skills.context_manager.get_context") as get_context,
    ):
        for invalid_operator in invalid_operators:
            with pytest.raises(HTTPException) as save_error:
                await save_skill_draft(
                    SkillDraftRequest(content="draft"),
                    _skill_mutator_request(invalid_operator),
                )
            assert save_error.value.status_code == 401
            assert save_error.value.detail == {"code": "authentication_required"}

            with pytest.raises(HTTPException) as update_error:
                await update_skill(
                    "example",
                    UpdateSkillRequest(enabled=False),
                    _skill_mutator_request(invalid_operator, "/api/skills/example"),
                )
            assert update_error.value.status_code == 401
            assert update_error.value.detail == {"code": "authentication_required"}

            with pytest.raises(HTTPException) as reload_error:
                await reload_skills(_skill_mutator_request(invalid_operator, "/api/skills/reload"))
            assert reload_error.value.status_code == 401
            assert reload_error.value.detail == {"code": "authentication_required"}

    validate.assert_not_called()
    ensure_workspace.assert_not_called()
    save_workspace.assert_not_called()
    enable.assert_not_called()
    disable.assert_not_called()
    reload_skills_mock.assert_not_called()
    log.assert_not_awaited()
    get_context.assert_not_called()


@pytest.mark.asyncio
async def test_save_skill_draft_binds_operator_for_full_route_and_resets_context():
    from src.api.skills import SkillDraftRequest, save_skill_draft

    operator = _test_bypass_operator()
    observed: list[tuple[str, object]] = []
    validation = {
        "valid": True,
        "errors": [],
        "skill": {
            "name": "Bound Skill",
            "description": "",
            "requires_tools": [],
            "user_invocable": True,
            "enabled": True,
            "file_path": "<draft>",
        },
        "runtime_ready": True,
        "missing_tools": [],
    }

    def observe(label: str):
        observed.append((label, get_current_trust_principal()))

    with (
        patch("src.api.skills.context_manager.get_context", return_value=SimpleNamespace(approval_mode="balanced")),
        patch("src.api.skills._validate_skill_content", side_effect=lambda *_args, **_kwargs: (observe("validation") or validation)),
        patch(
            "src.api.skills._ensure_skill_manager_workspace_extensions_loaded",
            side_effect=lambda: observe("workspace_extensions"),
        ),
        patch(
            "src.api.skills.save_workspace_contribution",
            side_effect=lambda *_args, **_kwargs: (
                observe("workspace_write") or "/tmp/workspace-capabilities/skills/bound-skill.md"
            ),
        ),
        patch(
            "src.api.skills.skill_manager.reload",
            side_effect=lambda: (observe("reload") or [{"name": "Bound Skill", "enabled": True}]),
        ),
        patch(
            "src.api.skills.log_integration_event",
            new_callable=AsyncMock,
            side_effect=lambda **_kwargs: observe("audit"),
        ) as log,
    ):
        payload = await save_skill_draft(
            SkillDraftRequest(content="draft"),
            _skill_mutator_request(operator),
        )

    assert payload["status"] == "saved"
    assert [label for label, _principal in observed] == [
        "validation",
        "workspace_extensions",
        "workspace_write",
        "reload",
        "audit",
        "validation",
    ]
    assert all(
        principal is not None
        and principal.principal_id == operator.principal.principal_id
        and principal.principal_type is PrincipalType.OPERATOR
        and principal.session_id == operator.session_id
        for _label, principal in observed
    )
    log.assert_awaited_once()
    assert get_current_session_id() is None
    assert get_current_trust_principal() is None


@pytest.mark.asyncio
async def test_skill_manager_mutators_bind_operator_and_reset_context():
    from src.api.skills import UpdateSkillRequest, reload_skills, update_skill

    operator = _test_bypass_operator()
    observed: list[tuple[str, object]] = []

    def observe(label: str):
        observed.append((label, get_current_trust_principal()))

    with (
        patch("src.api.skills.context_manager.get_context", return_value=SimpleNamespace(approval_mode="high_risk")),
        patch("src.api.skills.skill_manager.enable", side_effect=lambda _name: (observe("enable") or True)),
        patch(
            "src.api.skills.skill_manager.reload",
            side_effect=lambda: (observe("reload") or [{"name": "Bound Skill", "enabled": True}]),
        ),
        patch(
            "src.api.skills.log_integration_event",
            new_callable=AsyncMock,
            side_effect=lambda **_kwargs: observe("audit"),
        ) as log,
    ):
        update_payload = await update_skill(
            "bound-skill",
            UpdateSkillRequest(enabled=True),
            _skill_mutator_request(operator, "/api/skills/bound-skill"),
        )
        reload_payload = await reload_skills(_skill_mutator_request(operator, "/api/skills/reload"))

    assert update_payload == {"status": "updated", "name": "bound-skill", "enabled": True}
    assert reload_payload["status"] == "reloaded"
    assert [label for label, _principal in observed] == ["enable", "audit", "reload", "audit"]
    assert all(
        principal is not None
        and principal.principal_id == operator.principal.principal_id
        and principal.principal_type is PrincipalType.OPERATOR
        and principal.session_id == operator.session_id
        for _label, principal in observed
    )
    assert log.await_count == 2
    assert get_current_session_id() is None
    assert get_current_trust_principal() is None


@pytest.mark.asyncio
async def test_save_skill_draft_resets_operator_context_when_validation_raises():
    from src.api.skills import SkillDraftRequest, save_skill_draft

    operator = _test_bypass_operator()
    observed: list[object] = []

    def fail_validation(*_args, **_kwargs):
        observed.append(get_current_trust_principal())
        raise RuntimeError("validation failed")

    with (
        patch("src.api.skills.context_manager.get_context", return_value=SimpleNamespace(approval_mode="high_risk")),
        patch("src.api.skills._validate_skill_content", side_effect=fail_validation),
        patch("src.api.skills.save_workspace_contribution") as save_workspace,
        patch("src.api.skills.skill_manager.reload") as reload_skills_mock,
        patch("src.api.skills.log_integration_event", new_callable=AsyncMock) as log,
    ):
        with pytest.raises(RuntimeError, match="validation failed"):
            await save_skill_draft(
                SkillDraftRequest(content="draft"),
                _skill_mutator_request(operator),
            )

    assert len(observed) == 1
    assert observed[0] is not None
    assert observed[0].principal_id == operator.principal.principal_id
    assert observed[0].session_id == operator.session_id
    save_workspace.assert_not_called()
    reload_skills_mock.assert_not_called()
    log.assert_not_awaited()
    assert get_current_session_id() is None
    assert get_current_trust_principal() is None


@pytest.mark.asyncio
async def test_update_skill_resets_operator_context_when_manager_raises():
    from src.api.skills import UpdateSkillRequest, update_skill

    operator = _test_bypass_operator()
    observed: list[object] = []

    def fail_enable(_name):
        observed.append(get_current_trust_principal())
        raise RuntimeError("manager failed")

    with (
        patch("src.api.skills.context_manager.get_context", return_value=SimpleNamespace(approval_mode="high_risk")),
        patch("src.api.skills.skill_manager.enable", side_effect=fail_enable),
        patch("src.api.skills.log_integration_event", new_callable=AsyncMock) as log,
    ):
        with pytest.raises(RuntimeError, match="manager failed"):
            await update_skill(
                "bound-skill",
                UpdateSkillRequest(enabled=True),
                _skill_mutator_request(operator, "/api/skills/bound-skill"),
            )

    assert len(observed) == 1
    assert observed[0] is not None
    assert observed[0].principal_id == operator.principal.principal_id
    assert observed[0].session_id == operator.session_id
    log.assert_not_awaited()
    assert get_current_session_id() is None
    assert get_current_trust_principal() is None
